#!/usr/bin/env python3
"""Read-only Windows GUI lifecycle and session-artifact observer.

This companion binds one exact top-level HWND to one already-running process,
keeps a query/wait-only process handle open, and records whether the main
window disappears, the original process exits, or same-process top-level
windows remain.  It never sends a window message, terminates a process,
injects input, connects to an instrument, or changes an instrument setting.

Use ``scripts/ombe_failure_probe.py`` separately to validate worker State and
stale-value behavior when a vendor data window disappears.  This script proves
only the selected GUI/process lifecycle and what happened to an explicitly
pinned Growth Monitor session directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import platform
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


EXIT_PASS = 0
EXIT_EVIDENCE_FATAL = 2
EXIT_INCOMPLETE = 3
EXIT_VALIDATION_FAILURE = 4

TARGET_ROLES = (
    "growth-monitor",
    "vendor-data-window",
    "vendor-application",
)

MARKER_KINDS = (
    "precheck-complete",
    "close-action",
    "main-window-gone",
    "process-exited",
    "gui-restarted",
    "idle-confirmed",
    "gui-rearmed",
    "new-session-started",
    "abort",
    "incomplete",
    "note",
)

KNOWN_SESSION_FILES = (
    "sensor_log.csv",
    "heartbeat_log.csv",
    "commit_log.csv",
    "auto_capture_events.csv",
    "set_change_events.csv",
    "manual_events.csv",
    "live_labels.csv",
    "session_metadata.json",
    "growth_log.xlsx",
    "growth_log_export.csv",
    "temperature_profile.png",
)
FULL_HASH_SUFFIXES = {".csv", ".json", ".jsonl", ".txt"}

def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def current_boot_identity() -> tuple[str, str]:
    """Return a boot-stable host identity without contacting the network."""
    if os.name == "nt":
        try:
            import winreg

            key_path = (
                r"SYSTEM\CurrentControlSet\Control\Session Manager"
                r"\Memory Management\PrefetchParameters"
            )
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                boot_id, _ = winreg.QueryValueEx(key, "BootId")
            raw = f"{platform.node()}:windows-registry-boot-id:{boot_id}"
            return (
                hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
                "windows_registry_prefetch_boot_id",
            )
        except Exception:
            pass
    proc_boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        raw = f"{platform.node()}:{proc_boot_id.read_text().strip()}"
        return (
            hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
            "linux_kernel_boot_id",
        )
    except Exception:
        pass
    boot_epoch_s = round(time.time() - time.monotonic())
    raw = f"{platform.node()}:{boot_epoch_s}"
    return (
        hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "wall_clock_minus_monotonic_fallback",
    )


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _append_jsonl(stream: Any, row: dict[str, Any]) -> None:
    stream.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def _load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], []
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{path.name}:{line_number}: {exc}")
                continue
            if not isinstance(value, dict):
                errors.append(f"{path.name}:{line_number}: row is not an object")
                continue
            rows.append(value)
    return rows, errors


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tail_sha256(path: Path, size: int, tail_bytes: int = 4096) -> str:
    length = min(size, tail_bytes)
    with path.open("rb") as stream:
        stream.seek(size - length)
        return hashlib.sha256(stream.read(length)).hexdigest()


def _git_value(root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return result.stdout.strip()


def prepare_output_dir(output_dir: Path, session_dir: Path | None) -> Path:
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(
            f"output directory already exists; choose a new path: {output_dir}"
        )
    if session_dir is not None:
        session_dir = session_dir.expanduser().resolve()
        try:
            output_dir.relative_to(session_dir)
        except ValueError:
            pass
        else:
            raise ValueError(
                "output directory must not be inside the observed session"
            )
    output_dir.mkdir(parents=True, exist_ok=False)
    return output_dir


class _EvidenceMutex:
    """Serialize marker, terminal summary, re-audit, and manifest updates."""

    def __init__(self, output_dir: Path, timeout_ms: int = 10_000):
        resolved = str(output_dir.expanduser().resolve()).casefold()
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
        self.name = f"Local\\AIQM_OmbeGuiLifecycle_{digest}"
        self.path = output_dir / ".evidence.lock"
        self.timeout_ms = timeout_ms
        self._handle: Any = None
        self._kernel32: Any = None
        self._stream: Any = None

    def acquire(self) -> None:
        if os.name == "nt":
            import ctypes
            import ctypes.wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p,
                ctypes.wintypes.BOOL,
                ctypes.wintypes.LPCWSTR,
            ]
            kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.wintypes.DWORD,
            ]
            kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
            kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
            kernel32.ReleaseMutex.restype = ctypes.wintypes.BOOL
            kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
            kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                error = ctypes.get_last_error()
                raise OSError(error, "CreateMutexW failed for evidence lock")
            wait_result = kernel32.WaitForSingleObject(
                handle, self.timeout_ms
            )
            if wait_result not in {0x00000000, 0x00000080}:
                kernel32.CloseHandle(handle)
                raise RuntimeError(
                    "timed out waiting for another evidence writer"
                )
            self._kernel32 = kernel32
            self._handle = handle
            return

        import fcntl

        stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except Exception:
            stream.close()
            raise
        self._stream = stream

    def release(self) -> None:
        if os.name == "nt":
            if self._handle is not None and self._kernel32 is not None:
                self._kernel32.ReleaseMutex(self._handle)
                self._kernel32.CloseHandle(self._handle)
                self._handle = None
                self._kernel32 = None
            return
        if self._stream is not None:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "_EvidenceMutex":
        self.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


class _ObserverLease:
    """Kernel-owned proof that an observer is still writing this run."""

    def __init__(self, output_dir: Path):
        resolved = str(output_dir.expanduser().resolve()).casefold()
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
        self.name = f"Local\\AIQM_OmbeGuiObserver_{digest}"
        self.path = output_dir / ".observer.lock"
        self._handle: Any = None
        self._kernel32: Any = None
        self._stream: Any = None

    def acquire(self) -> None:
        if os.name == "nt":
            import ctypes
            import ctypes.wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateEventW.argtypes = [
                ctypes.c_void_p,
                ctypes.wintypes.BOOL,
                ctypes.wintypes.BOOL,
                ctypes.wintypes.LPCWSTR,
            ]
            kernel32.CreateEventW.restype = ctypes.wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
            kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
            ctypes.set_last_error(0)
            handle = kernel32.CreateEventW(None, True, True, self.name)
            if not handle:
                error = ctypes.get_last_error()
                raise OSError(error, "CreateEventW failed for observer lease")
            if ctypes.get_last_error() == 183:
                kernel32.CloseHandle(handle)
                raise RuntimeError(
                    "another observer already owns this evidence directory"
                )
            self._kernel32 = kernel32
            self._handle = handle
            return

        import fcntl

        stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(
                stream.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except Exception:
            stream.close()
            raise RuntimeError(
                "another observer already owns this evidence directory"
            )
        self._stream = stream

    def is_live(self) -> bool:
        if os.name == "nt":
            import ctypes
            import ctypes.wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenEventW.argtypes = [
                ctypes.wintypes.DWORD,
                ctypes.wintypes.BOOL,
                ctypes.wintypes.LPCWSTR,
            ]
            kernel32.OpenEventW.restype = ctypes.wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
            kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
            handle = kernel32.OpenEventW(
                Win32Target.SYNCHRONIZE, False, self.name
            )
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True

        import fcntl

        if not self.path.is_file():
            return False
        with self.path.open("a+", encoding="utf-8") as stream:
            try:
                fcntl.flock(
                    stream.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                return True
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                return False

    def release(self) -> None:
        if os.name == "nt":
            if self._handle is not None and self._kernel32 is not None:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None
                self._kernel32 = None
            return
        if self._stream is not None:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "_ObserverLease":
        self.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    owner_pid: int
    title: str
    class_name: str
    visible: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "owner_pid": self.owner_pid,
            "title": self.title,
            "class_name": self.class_name,
            "visible": self.visible,
        }


@dataclass(frozen=True)
class TargetIdentity:
    pid: int
    creation_time_utc: str
    image_path: str
    command_line: str | None
    command_line_sha256: str | None
    command_line_source: str
    main_hwnd: int
    main_title: str
    main_class_name: str
    requested_process_access: int
    terminate_right_requested: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "creation_time_utc": self.creation_time_utc,
            "image_path": self.image_path,
            "command_line": self.command_line,
            "command_line_sha256": self.command_line_sha256,
            "command_line_source": self.command_line_source,
            "main_hwnd": self.main_hwnd,
            "main_title": self.main_title,
            "main_class_name": self.main_class_name,
            "requested_process_access": self.requested_process_access,
            "terminate_right_requested": self.terminate_right_requested,
        }


class TargetProbe(Protocol):
    identity: TargetIdentity

    def sample(self) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


class Win32Target:
    """Held read/wait-only handle plus one PID-bound top-level HWND."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    STILL_ACTIVE = 259

    def __init__(
        self,
        pid: int,
        *,
        expected_title: str | None = None,
        hwnd: int | None = None,
        expected_image: str | None = None,
        expected_command_token: str | None = None,
    ):
        if os.name != "nt":
            raise OSError("GUI lifecycle observation requires Windows")
        if pid <= 0:
            raise ValueError("PID must be positive")
        if pid == os.getpid():
            raise ValueError("refusing to observe the lifecycle probe itself")
        if hwnd is None and not expected_title:
            raise ValueError("provide --hwnd or an exact --expected-title")

        import ctypes
        import ctypes.wintypes

        self._ctypes = ctypes
        self._wintypes = ctypes.wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._configure_api()

        access = self.PROCESS_QUERY_LIMITED_INFORMATION | self.SYNCHRONIZE
        handle = self._kernel32.OpenProcess(access, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            raise OSError(error, f"OpenProcess failed for PID {pid}")
        self._handle = handle
        self._pid = pid

        try:
            image_path = self._query_image_path()
            creation_time_utc = self._query_creation_time()
            self._assert_original_alive("before command-line identity query")
            command_line, command_line_source = (
                self._query_command_line_best_effort()
            )
            self._assert_original_alive("after command-line identity query")
            if expected_image and (
                os.path.normcase(os.path.abspath(image_path))
                != os.path.normcase(os.path.abspath(expected_image))
            ):
                raise ValueError(
                    f"process image mismatch: {image_path!r} != "
                    f"{expected_image!r}"
                )
            if expected_command_token:
                if command_line is None:
                    raise ValueError(
                        "could not read the command line required by "
                        "--expected-command-token"
                    )
                if expected_command_token not in command_line:
                    raise ValueError(
                        f"command line does not contain required token "
                        f"{expected_command_token!r}"
                    )

            if hwnd is None:
                matches = [
                    window
                    for window in self._enum_owned_windows()
                    if window.title == expected_title
                ]
                if len(matches) != 1:
                    raise ValueError(
                        f"expected exactly one PID {pid} window titled "
                        f"{expected_title!r}; found {len(matches)}"
                    )
                main = matches[0]
            else:
                top_level_matches = [
                    window
                    for window in self._enum_owned_windows()
                    if window.hwnd == hwnd
                ]
                if not top_level_matches:
                    raise ValueError(f"HWND {hwnd} does not exist")
                main = top_level_matches[0]
                if main.owner_pid != pid:
                    raise ValueError(
                        f"HWND {hwnd} belongs to PID {main.owner_pid}, not {pid}"
                    )
                if expected_title and main.title != expected_title:
                    raise ValueError(
                        f"window title mismatch: {main.title!r} != "
                        f"{expected_title!r}"
                    )

            self.identity = TargetIdentity(
                pid=pid,
                creation_time_utc=creation_time_utc,
                image_path=image_path,
                command_line=command_line,
                command_line_sha256=(
                    hashlib.sha256(command_line.encode("utf-8")).hexdigest()
                    if command_line is not None else None
                ),
                command_line_source=command_line_source,
                main_hwnd=main.hwnd,
                main_title=main.title,
                main_class_name=main.class_name,
                requested_process_access=access,
            )
        except Exception:
            self.close()
            raise

    def _configure_api(self) -> None:
        c = self._ctypes
        w = self._wintypes
        self._kernel32.OpenProcess.argtypes = [
            w.DWORD,
            w.BOOL,
            w.DWORD,
        ]
        self._kernel32.OpenProcess.restype = w.HANDLE
        self._kernel32.CloseHandle.argtypes = [w.HANDLE]
        self._kernel32.CloseHandle.restype = w.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self._kernel32.WaitForSingleObject.restype = w.DWORD
        self._kernel32.GetExitCodeProcess.argtypes = [
            w.HANDLE,
            c.POINTER(w.DWORD),
        ]
        self._kernel32.GetExitCodeProcess.restype = w.BOOL
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            w.HANDLE,
            w.DWORD,
            w.LPWSTR,
            c.POINTER(w.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = w.BOOL
        self._kernel32.GetProcessTimes.argtypes = [
            w.HANDLE,
            c.POINTER(w.FILETIME),
            c.POINTER(w.FILETIME),
            c.POINTER(w.FILETIME),
            c.POINTER(w.FILETIME),
        ]
        self._kernel32.GetProcessTimes.restype = w.BOOL

        self._user32.IsWindow.argtypes = [w.HWND]
        self._user32.IsWindow.restype = w.BOOL
        self._user32.IsWindowVisible.argtypes = [w.HWND]
        self._user32.IsWindowVisible.restype = w.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            w.HWND,
            c.POINTER(w.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = w.DWORD
        self._user32.GetWindowTextLengthW.argtypes = [w.HWND]
        self._user32.GetWindowTextLengthW.restype = c.c_int
        self._user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
        self._user32.GetWindowTextW.restype = c.c_int
        self._user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
        self._user32.GetClassNameW.restype = c.c_int
        self._enum_windows_callback_type = c.WINFUNCTYPE(
            w.BOOL,
            w.HWND,
            w.LPARAM,
        )
        self._user32.EnumWindows.argtypes = [
            self._enum_windows_callback_type,
            w.LPARAM,
        ]
        self._user32.EnumWindows.restype = w.BOOL

    def _query_image_path(self) -> str:
        size = self._wintypes.DWORD(32768)
        buffer = self._ctypes.create_unicode_buffer(size.value)
        if not self._kernel32.QueryFullProcessImageNameW(
            self._handle, 0, buffer, self._ctypes.byref(size)
        ):
            error = self._ctypes.get_last_error()
            raise OSError(error, "QueryFullProcessImageNameW failed")
        return buffer.value

    def _query_creation_time(self) -> str:
        values = [self._wintypes.FILETIME() for _ in range(4)]
        if not self._kernel32.GetProcessTimes(
            self._handle, *(self._ctypes.byref(value) for value in values)
        ):
            error = self._ctypes.get_last_error()
            raise OSError(error, "GetProcessTimes failed")
        creation = values[0]
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        unix_seconds = (ticks - 116444736000000000) / 10_000_000
        return (
            datetime.fromtimestamp(unix_seconds, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def _assert_original_alive(self, context: str) -> None:
        wait_result = int(
            self._kernel32.WaitForSingleObject(self._handle, 0)
        )
        if wait_result != self.WAIT_TIMEOUT:
            raise RuntimeError(
                f"target process exited {context}; refusing a potentially "
                "reused PID"
            )

    def _query_command_line_best_effort(self) -> tuple[str | None, str]:
        """Read Win32_Process.CommandLine through read-only CIM.

        The held process handle and HWND/PID binding remain the authoritative
        identity. CIM is supplementary because policy or service availability
        can prevent it; callers can make it mandatory with
        ``--expected-command-token``.
        """
        script = (
            "$ErrorActionPreference='Stop';"
            f"$p=Get-CimInstance Win32_Process -Filter "
            f"'ProcessId = {self._pid}';"
            "if($null -eq $p){exit 3};"
            "[pscustomobject]@{"
            "ProcessId=[int]$p.ProcessId;"
            "CommandLine=[string]$p.CommandLine"
            "}|ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            value = json.loads(result.stdout)
            if int(value.get("ProcessId", -1)) != self._pid:
                return None, "cim_pid_mismatch"
            command_line = value.get("CommandLine")
            if not isinstance(command_line, str) or not command_line:
                return None, "cim_empty"
            return command_line, "win32_process_cim"
        except Exception as exc:
            return None, f"cim_unavailable:{type(exc).__name__}"

    def _window_info(self, hwnd: int) -> WindowInfo | None:
        if not self._user32.IsWindow(hwnd):
            return None
        owner = self._wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(
            hwnd, self._ctypes.byref(owner)
        )
        title_length = self._user32.GetWindowTextLengthW(hwnd)
        title_buffer = self._ctypes.create_unicode_buffer(title_length + 1)
        self._user32.GetWindowTextW(
            hwnd, title_buffer, len(title_buffer)
        )
        class_buffer = self._ctypes.create_unicode_buffer(512)
        self._user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        return WindowInfo(
            hwnd=int(hwnd),
            owner_pid=int(owner.value),
            title=title_buffer.value,
            class_name=class_buffer.value,
            visible=bool(self._user32.IsWindowVisible(hwnd)),
        )

    def _enum_owned_windows(self) -> list[WindowInfo]:
        found: list[WindowInfo] = []

        @self._enum_windows_callback_type
        def callback(hwnd: int, _lparam: int) -> bool:
            info = self._window_info(int(hwnd))
            if info is not None and info.owner_pid == self._pid:
                found.append(info)
            return True

        if not self._user32.EnumWindows(callback, 0):
            error = self._ctypes.get_last_error()
            raise OSError(error, "EnumWindows failed")
        return sorted(found, key=lambda item: item.hwnd)

    def require_owned_top_level_window(self, hwnd: int) -> WindowInfo:
        matches = [
            window
            for window in self._enum_owned_windows()
            if window.hwnd == hwnd
        ]
        if len(matches) != 1:
            raise ValueError(
                f"paired source HWND {hwnd} is not one top-level window "
                f"owned by target PID {self._pid}"
            )
        return matches[0]

    def sample(self) -> dict[str, Any]:
        wait_result = int(self._kernel32.WaitForSingleObject(self._handle, 0))
        if wait_result == self.WAIT_TIMEOUT:
            alive = True
            exit_code: int | None = None
        elif wait_result == self.WAIT_OBJECT_0:
            alive = False
            raw_exit_code = self._wintypes.DWORD()
            if not self._kernel32.GetExitCodeProcess(
                self._handle, self._ctypes.byref(raw_exit_code)
            ):
                error = self._ctypes.get_last_error()
                raise OSError(error, "GetExitCodeProcess failed")
            exit_code = int(raw_exit_code.value)
        else:
            raise OSError(
                self._ctypes.get_last_error(),
                f"WaitForSingleObject returned 0x{wait_result:08x}",
            )

        main = self._window_info(self.identity.main_hwnd)
        main_owned = (
            main is not None
            and main.owner_pid == self.identity.pid
            and main.title == self.identity.main_title
            and main.class_name == self.identity.main_class_name
        )
        # Never enumerate by PID after the held original process exits: a
        # recycled PID must not be mistaken for recovery or a residual window.
        owned = self._enum_owned_windows() if alive else []
        return {
            "process_alive": alive,
            "process_wait_result": wait_result,
            "exit_code": exit_code,
            "main_window_exists": main is not None,
            "main_window_owned_by_target": main_owned,
            "main_window_identity_matches": main_owned,
            "main_window": main.as_dict() if main is not None else None,
            "owned_top_level_windows": [
                window.as_dict() for window in owned
            ],
        }

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        kernel32 = getattr(self, "_kernel32", None)
        if handle and kernel32:
            kernel32.CloseHandle(handle)
            self._handle = None

    def __enter__(self) -> "Win32Target":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


@dataclass(frozen=True)
class SessionSnapshot:
    entries: dict[str, dict[str, Any]]
    errors: tuple[str, ...]

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.entries, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def total_bytes(self) -> int:
        return sum(entry["size"] for entry in self.entries.values())


class SessionSampler:
    """Stat-only session tree sampler with text-tail rewrite detection."""

    def __init__(self, session_dir: Path):
        self.session_dir = session_dir.expanduser().resolve()
        if not self.session_dir.is_dir():
            raise NotADirectoryError(
                f"session directory does not exist: {self.session_dir}"
            )
        root_stat = self.session_dir.stat()
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        self._last_entries: dict[str, dict[str, Any]] = {}

    def snapshot(self) -> SessionSnapshot:
        entries: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        root_stat = self.session_dir.stat()
        if (root_stat.st_dev, root_stat.st_ino) != self._root_identity:
            raise RuntimeError("observed session root identity changed")
        for root, dir_names, file_names in os.walk(
            self.session_dir, followlinks=False
        ):
            root_path = Path(root)
            dir_names[:] = [
                name
                for name in dir_names
                if not (root_path / name).is_symlink()
            ]
            for name in sorted(file_names):
                path = root_path / name
                relative = path.relative_to(self.session_dir).as_posix()
                if path.is_symlink():
                    errors.append(f"{relative}: skipped symbolic link")
                    continue
                try:
                    for attempt in range(2):
                        before = path.stat(follow_symlinks=False)
                        prior = self._last_entries.get(relative)
                        if (
                            prior is not None
                            and prior["size"] == before.st_size
                            and prior["mtime_ns"] == before.st_mtime_ns
                        ):
                            tail_hash = prior["tail_sha256"]
                            content_hash = prior.get("content_sha256")
                        else:
                            tail_hash = _tail_sha256(
                                path, before.st_size
                            )
                            content_hash = (
                                _sha256(path)
                                if path.suffix.lower() in FULL_HASH_SUFFIXES
                                else None
                            )
                        after = path.stat(follow_symlinks=False)
                        if (
                            before.st_size == after.st_size
                            and before.st_mtime_ns == after.st_mtime_ns
                        ):
                            entries[relative] = {
                                "size": before.st_size,
                                "mtime_ns": before.st_mtime_ns,
                                "tail_sha256": tail_hash,
                                "content_sha256": content_hash,
                            }
                            break
                        if attempt == 1:
                            errors.append(
                                f"{relative}: changed during sampling"
                            )
                            entries[relative] = {
                                "size": after.st_size,
                                "mtime_ns": after.st_mtime_ns,
                                "tail_sha256": _tail_sha256(
                                    path, after.st_size
                                ),
                                "content_sha256": (
                                    _sha256(path)
                                    if path.suffix.lower()
                                    in FULL_HASH_SUFFIXES
                                    else None
                                ),
                            }
                except OSError as exc:
                    errors.append(f"{relative}: {type(exc).__name__}: {exc}")
        self._last_entries = entries
        return SessionSnapshot(entries=entries, errors=tuple(errors))


def bind_paired_failure_run(
    failure_run: Path,
    target: TargetProbe,
    target_role: str,
) -> dict[str, Any]:
    """Bind a vendor lifecycle observer to the actual source-worker HWND."""
    failure_run = failure_run.expanduser().resolve()
    info_path = failure_run / "run_info.json"
    if not info_path.is_file():
        raise FileNotFoundError(
            f"paired failure run has no run_info.json: {failure_run}"
        )
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if info.get("fault_class") != "window-source-loss":
        raise ValueError(
            "paired failure run must use fault-class window-source-loss"
        )
    worker_identity = info.get("worker_identity") or {}
    raw_hwnd = worker_identity.get("hwnd")
    if not isinstance(raw_hwnd, int) or raw_hwnd <= 0:
        raise ValueError(
            "paired failure worker has no bound source HWND yet"
        )
    if target_role == "vendor-data-window":
        if raw_hwnd != target.identity.main_hwnd:
            raise ValueError(
                f"lifecycle HWND {target.identity.main_hwnd} does not match "
                f"paired worker source HWND {raw_hwnd}"
            )
        source_window = {
            "hwnd": raw_hwnd,
            "owner_pid": target.identity.pid,
            "title": target.identity.main_title,
            "class_name": target.identity.main_class_name,
        }
    else:
        if not isinstance(target, Win32Target):
            raise TypeError(
                "vendor-application pairing requires a Win32Target"
            )
        source_window = target.require_owned_top_level_window(
            raw_hwnd
        ).as_dict()
    return {
        "output_dir": str(failure_run),
        "run_info_sha256": _sha256(info_path),
        "campaign_id": info.get("campaign_id"),
        "scenario_id": info.get("scenario_id"),
        "source": info.get("source"),
        "mode": info.get("mode"),
        "fault_class": info.get("fault_class"),
        "source_hwnd": raw_hwnd,
        "source_window": source_window,
        "worker_identity": worker_identity,
    }


def diff_artifacts(
    before: SessionSnapshot | None,
    after: SessionSnapshot,
) -> list[dict[str, Any]]:
    if before is None:
        return []
    changes: list[dict[str, Any]] = []
    names = sorted(set(before.entries) | set(after.entries))
    for name in names:
        old = before.entries.get(name)
        new = after.entries.get(name)
        if old is None:
            kind = "created"
        elif new is None:
            kind = "deleted"
        elif new["size"] > old["size"]:
            kind = "appended"
        elif new["size"] < old["size"]:
            kind = "truncated"
        elif (
            old.get("content_sha256") is not None
            and new.get("content_sha256") is not None
            and old.get("content_sha256") == new.get("content_sha256")
        ):
            kind = "touch-only"
        elif (
            new.get("tail_sha256") != old.get("tail_sha256")
            or new["mtime_ns"] != old["mtime_ns"]
        ):
            kind = "same-size-rewrite"
        else:
            continue
        changes.append({
            "path": name,
            "kind": kind,
            "before": old,
            "after": new,
        })
    return changes


def _is_substantive(change: dict[str, Any]) -> bool:
    return change["kind"] != "touch-only"


def inspect_csv(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "row_count": 0,
        "data_row_count": 0,
        "column_count": None,
        "last_row": None,
        "final_newline": None,
        "errors": [],
    }
    if not path.is_file():
        return result
    try:
        raw = path.read_bytes()
        result["size"] = len(raw)
        result["sha256"] = hashlib.sha256(raw).hexdigest()
        result["final_newline"] = not raw or raw.endswith((b"\n", b"\r"))
        text = raw.decode("utf-8-sig")
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        result["errors"].append(f"CSV parse error: {exc}")
        return result
    if not rows:
        result["errors"].append("empty CSV")
        return result
    width = len(rows[0])
    result["row_count"] = len(rows)
    result["data_row_count"] = max(0, len(rows) - 1)
    result["column_count"] = width
    result["last_row"] = rows[-1]
    for index, row in enumerate(rows[1:], 2):
        if len(row) != width:
            result["errors"].append(
                f"row {index} has {len(row)} columns; expected {width}"
            )
    if raw and not result["final_newline"]:
        result["errors"].append("file does not end with a newline")
    return result


def inspect_image(path: Path) -> list[str]:
    """Perform dependency-free checks that catch common partial PNG/BMP writes."""
    errors: list[str] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [f"{type(exc).__name__}: {exc}"]
    suffix = path.suffix.lower()
    if suffix == ".png":
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            errors.append("invalid PNG signature")
        if not raw.endswith(b"IEND\xaeB`\x82"):
            errors.append("missing PNG IEND")
    elif suffix == ".bmp":
        if len(raw) < 14 or not raw.startswith(b"BM"):
            errors.append("invalid BMP header")
        elif int.from_bytes(raw[2:6], "little") != len(raw):
            errors.append("BMP declared size does not match file size")
    return errors


def audit_frame_references(session_dir: Path) -> dict[str, Any]:
    """Report missing CSV targets and image files left without a CSV owner."""
    referenced_files: set[str] = set()
    referenced_directories: set[str] = set()
    missing: list[dict[str, str]] = []
    unsafe: list[dict[str, str]] = []
    parse_errors: list[str] = []
    session_root = session_dir.resolve()

    for csv_path in sorted(session_dir.rglob("*.csv")):
        if csv_path.is_symlink():
            continue
        try:
            with csv_path.open(
                "r", encoding="utf-8-sig", newline=""
            ) as stream:
                reader = csv.DictReader(stream)
                for row_number, row in enumerate(reader, 2):
                    for field_name in ("frame_path", "buffer_dir"):
                        raw_value = (row.get(field_name) or "").strip()
                        if not raw_value:
                            continue
                        raw_path = Path(raw_value).expanduser()
                        resolved = (
                            raw_path.resolve()
                            if raw_path.is_absolute()
                            else (session_root / raw_path).resolve()
                        )
                        try:
                            relative = resolved.relative_to(
                                session_root
                            ).as_posix()
                        except ValueError:
                            unsafe.append({
                                "csv": csv_path.relative_to(
                                    session_root
                                ).as_posix(),
                                "row": str(row_number),
                                "field": field_name,
                                "value": raw_value,
                            })
                            continue
                        if not resolved.exists():
                            missing.append({
                                "csv": csv_path.relative_to(
                                    session_root
                                ).as_posix(),
                                "row": str(row_number),
                                "field": field_name,
                                "value": raw_value,
                            })
                        elif resolved.is_dir():
                            referenced_directories.add(relative)
                        else:
                            referenced_files.add(relative)
        except Exception as exc:
            parse_errors.append(
                f"{csv_path.relative_to(session_root).as_posix()}: "
                f"{type(exc).__name__}: {exc}"
            )

    image_paths = {
        path.relative_to(session_root).as_posix()
        for path in session_dir.rglob("*")
        if (
            path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in {".png", ".bmp"}
            and "frames" in path.relative_to(session_root).parts
        )
    }
    covered = set(referenced_files)
    for directory in referenced_directories:
        prefix = directory.rstrip("/") + "/"
        covered.update(
            image_path
            for image_path in image_paths
            if image_path.startswith(prefix)
        )
    return {
        "referenced_files": sorted(referenced_files),
        "referenced_directories": sorted(referenced_directories),
        "missing_references": missing,
        "unsafe_external_references": unsafe,
        "unreferenced_image_candidates": sorted(image_paths - covered),
        "parse_errors": parse_errors,
    }


def audit_session(session_dir: Path) -> dict[str, Any]:
    if not session_dir.is_dir():
        raise NotADirectoryError(
            f"observed session directory is unavailable: {session_dir}"
        )
    csv_audit: dict[str, Any] = {}
    json_audit: dict[str, Any] = {}
    image_errors: dict[str, list[str]] = {}
    file_manifest: dict[str, dict[str, Any]] = {}
    audit_errors: list[str] = []
    for path in sorted(
        item
        for item in session_dir.rglob("*")
        if item.is_file() and not item.is_symlink()
    ):
        relative = path.relative_to(session_dir).as_posix()
        stat = path.stat()
        digest = _sha256(path)
        after_hash = path.stat()
        stable_during_hash = (
            stat.st_size == after_hash.st_size
            and stat.st_mtime_ns == after_hash.st_mtime_ns
        )
        if not stable_during_hash:
            audit_errors.append(f"{relative}: changed during final hashing")
        file_manifest[relative] = {
            "size": after_hash.st_size,
            "mtime_ns": after_hash.st_mtime_ns,
            "sha256": digest,
            "stable_during_hash": stable_during_hash,
        }
        if path.suffix.lower() == ".csv":
            csv_audit[relative] = inspect_csv(path)
        elif path.suffix.lower() == ".json":
            json_result = {
                "path": str(path),
                "valid": True,
                "error": None,
            }
            try:
                def reject_nonfinite(value: str) -> Any:
                    raise ValueError(f"non-finite JSON constant: {value}")

                with path.open("r", encoding="utf-8-sig") as stream:
                    json.load(
                        stream,
                        parse_constant=reject_nonfinite,
                    )
            except Exception as exc:
                json_result["valid"] = False
                json_result["error"] = f"{type(exc).__name__}: {exc}"
            json_audit[relative] = json_result
        elif path.suffix.lower() in {".png", ".bmp"}:
            errors = inspect_image(path)
            if errors:
                image_errors[relative] = errors
        final_stat = path.stat()
        inspection_digest = _sha256(path)
        after_inspection_hash = path.stat()
        stable_through_inspection = (
            after_hash.st_size == final_stat.st_size
            and after_hash.st_mtime_ns == final_stat.st_mtime_ns
            and final_stat.st_size == after_inspection_hash.st_size
            and final_stat.st_mtime_ns == after_inspection_hash.st_mtime_ns
            and digest == inspection_digest
        )
        file_manifest[relative][
            "stable_through_inspection"
        ] = stable_through_inspection
        file_manifest[relative][
            "inspection_sha256"
        ] = inspection_digest
        if not stable_through_inspection:
            audit_errors.append(
                f"{relative}: changed during final inspection"
            )
    return {
        "observed_at_utc": utc_now(),
        "session_dir": str(session_dir),
        "csv": csv_audit,
        "json": json_audit,
        "image_errors": image_errors,
        "audit_errors": audit_errors,
        "file_manifest": file_manifest,
        "frame_references": audit_frame_references(session_dir),
    }


def reaudit_session(output_dir: Path) -> dict[str, Any] | None:
    """Compare the pinned old session with the observer's terminal audit."""
    run_info = json.loads(
        (output_dir / "run_info.json").read_text(encoding="utf-8")
    )
    raw_session_dir = run_info.get("session_dir")
    original_path = output_dir / "session_audit.json"
    if not raw_session_dir:
        return None
    if not original_path.is_file():
        raise FileNotFoundError(
            "terminal session_audit.json is missing; cannot re-audit"
        )
    original = json.loads(original_path.read_text(encoding="utf-8"))
    current = audit_session(Path(raw_session_dir))
    before = original.get("file_manifest", {})
    after = current.get("file_manifest", {})
    changes: list[dict[str, Any]] = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name)
        new = after.get(name)
        if old == new:
            continue
        if old is None:
            kind = "created"
        elif new is None:
            kind = "deleted"
        elif new.get("sha256") != old.get("sha256"):
            kind = "content-changed"
        else:
            kind = "metadata-changed"
        changes.append({
            "path": name,
            "kind": kind,
            "before": old,
            "after": new,
        })
    boot_id, boot_source = current_boot_identity()
    result = {
        "observed_at_utc": utc_now(),
        "observed_monotonic_ns": time.monotonic_ns(),
        "boot_id": boot_id,
        "boot_id_source": boot_source,
        "session_dir": raw_session_dir,
        "changes_since_terminal_audit": changes,
        "current_audit": current,
    }
    _write_json_atomic(output_dir / "session_reaudit.json", result)
    return result


def _marker_rows(output_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    marker_dir = output_dir / "markers"
    if not marker_dir.is_dir():
        return [], []
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in sorted(marker_dir.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(row, dict):
            errors.append(f"{path.name}: marker is not an object")
            continue
        rows.append(row)
    rows.sort(
        key=lambda row: (
            str(row.get("at_utc", "")),
            str(row.get("marker_id", "")),
        )
    )
    return rows, errors


def _stream_advanced(
    samples: list[dict[str, Any]],
    path: str,
    before_monotonic_ns: int | None,
) -> bool:
    for row in samples:
        if (
            before_monotonic_ns is not None
            and row.get("observed_monotonic_ns", 0) >= before_monotonic_ns
        ):
            continue
        for change in row.get("artifact_changes", []):
            if (
                change.get("path") == path
                and change.get("kind") in {"created", "appended"}
            ):
                return True
    return False


def _stream_health(
    samples: list[dict[str, Any]],
    path: str,
    before_monotonic_ns: int | None,
    *,
    max_age_s: float,
) -> dict[str, Any]:
    change_times = [
        row.get("observed_monotonic_ns")
        for row in samples
        if (
            before_monotonic_ns is None
            or row.get("observed_monotonic_ns", 0) < before_monotonic_ns
        )
        and any(
            change.get("path") == path
            and change.get("kind") in {"created", "appended"}
            for change in row.get("artifact_changes", [])
        )
    ]
    change_times = [
        int(value) for value in change_times
        if isinstance(value, (int, float))
    ]
    last_age_s = (
        (before_monotonic_ns - change_times[-1]) / 1_000_000_000
        if before_monotonic_ns is not None and change_times
        else None
    )
    return {
        "advance_count": len(change_times),
        "last_advance_age_s": last_age_s,
        "max_age_s": max_age_s,
        "healthy": (
            len(change_times) >= 2
            and last_age_s is not None
            and 0 <= last_age_s <= max_age_s
        ),
    }


def _evidence_fatal_summary(
    *,
    target_role: Any,
    errors: list[str],
    sample_count: int = 0,
) -> tuple[dict[str, Any], int]:
    """Return a durable, minimal result when evidence cannot be interpreted."""
    return (
        {
            "generated_at_utc": utc_now(),
            "target_role": (
                target_role if isinstance(target_role, str) else None
            ),
            "outcome": "evidence-fatal",
            "exit_code": EXIT_EVIDENCE_FATAL,
            "sample_count": sample_count,
            "observer_completed": False,
            "evidence_errors": errors,
            "limitations": [
                "Lifecycle metrics were not derived from malformed evidence."
            ],
        },
        EXIT_EVIDENCE_FATAL,
    )


def summarize_run(output_dir: Path) -> tuple[dict[str, Any], int]:
    run_info_path = output_dir / "run_info.json"
    if not run_info_path.is_file():
        raise FileNotFoundError(f"missing {run_info_path}")
    run_info = json.loads(run_info_path.read_text(encoding="utf-8"))
    if not isinstance(run_info, dict):
        return _evidence_fatal_summary(
            target_role=None,
            errors=["run_info.json is not an object"],
        )

    run_schema_errors: list[str] = []
    if run_info.get("target_role") not in TARGET_ROLES:
        run_schema_errors.append("run_info.json has an invalid target_role")
    if run_info.get("fault_style") not in {
        "graceful",
        "abrupt",
        "spontaneous",
    }:
        run_schema_errors.append("run_info.json has an invalid fault_style")
    if run_info.get("test_context") not in {
        "offline-tabletop",
        "live-idle-advisory",
    }:
        run_schema_errors.append("run_info.json has an invalid test_context")
    if (
        run_info.get("test_context") == "live-idle-advisory"
        and run_info.get("gui_state")
        not in {"idle-disarmed", "advisory-running"}
    ):
        run_schema_errors.append("run_info.json has an invalid gui_state")
    for name in ("operator", "observer", "approver"):
        if not str(run_info.get(name, "")).strip():
            run_schema_errors.append(
                f"run_info.json has an invalid {name}"
            )
    for name in (
        "sample_interval_s",
        "duration_s",
        "baseline_duration_s",
        "post_loss_observation_s",
        "post_loss_settle_s",
        "orphan_grace_s",
    ):
        value = run_info.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            run_schema_errors.append(
                f"run_info.json has an invalid {name}"
            )
    required_paths = run_info.get("require_session_advance")
    if (
        not isinstance(required_paths, list)
        or not all(isinstance(item, str) for item in required_paths)
    ):
        run_schema_errors.append(
            "run_info.json require_session_advance is not a string list"
        )
    if not isinstance(run_info.get("target_identity"), dict):
        run_schema_errors.append(
            "run_info.json target_identity is not an object"
        )
    if run_schema_errors:
        return _evidence_fatal_summary(
            target_role=run_info.get("target_role"),
            errors=run_schema_errors,
        )

    raw_samples, sample_errors = _load_jsonl(
        output_dir / "samples.jsonl"
    )
    raw_markers, marker_errors = _marker_rows(output_dir)
    lifecycle_errors = sample_errors + marker_errors
    samples: list[dict[str, Any]] = []
    previous_monotonic_ns: int | None = None
    for expected_index, row in enumerate(raw_samples):
        row_valid = True
        sample_index = row.get("sample_index")
        if (
            isinstance(sample_index, bool)
            or sample_index != expected_index
        ):
            lifecycle_errors.append(
                f"samples.jsonl row {expected_index + 1}: non-contiguous "
                "sample_index"
            )
            row_valid = False
        observed_ns = row.get("observed_monotonic_ns")
        if isinstance(observed_ns, bool) or not isinstance(observed_ns, int):
            lifecycle_errors.append(
                f"samples.jsonl row {expected_index + 1}: invalid monotonic "
                "timestamp"
            )
            row_valid = False
        elif (
            previous_monotonic_ns is not None
            and observed_ns <= previous_monotonic_ns
        ):
            lifecycle_errors.append(
                f"samples.jsonl row {expected_index + 1}: monotonic time "
                "did not increase"
            )
            row_valid = False
        else:
            previous_monotonic_ns = observed_ns
        if _parse_utc(row.get("observed_at_utc")) is None:
            lifecycle_errors.append(
                f"samples.jsonl row {expected_index + 1}: observed_at_utc "
                "is not a valid UTC timestamp"
            )
            row_valid = False
        target = row.get("target")
        if not isinstance(target, dict):
            lifecycle_errors.append(
                f"samples.jsonl row {expected_index + 1}: target is not an "
                "object"
            )
            row_valid = False
        elif (
            not isinstance(target.get("process_alive"), bool)
            or not isinstance(
                target.get("main_window_owned_by_target"), bool
            )
        ):
            lifecycle_errors.append(
                f"samples.jsonl row {expected_index + 1}: target lifecycle "
                "flags are not booleans"
            )
            row_valid = False
        artifact_changes = row.get("artifact_changes", [])
        if not isinstance(artifact_changes, list):
            lifecycle_errors.append(
                f"samples.jsonl row {expected_index + 1}: artifact_changes "
                "is not a list"
            )
            row_valid = False
        elif not all(
            isinstance(change, dict)
            and isinstance(change.get("kind"), str)
            and isinstance(change.get("path"), str)
            for change in artifact_changes
        ):
            lifecycle_errors.append(
                f"samples.jsonl row {expected_index + 1}: artifact_changes "
                "contains a malformed item"
            )
            row_valid = False
        session = row.get("session")
        if "session" in row and not isinstance(session, dict):
            lifecycle_errors.append(
                f"samples.jsonl row {expected_index + 1}: session is not an "
                "object"
            )
            row_valid = False
        elif isinstance(session, dict) and not isinstance(
            session.get("errors", []), list
        ):
            lifecycle_errors.append(
                f"samples.jsonl row {expected_index + 1}: session errors "
                "is not a list"
            )
            row_valid = False
        if row_valid:
            samples.append(row)

    markers: list[dict[str, Any]] = []
    for index, marker in enumerate(raw_markers, 1):
        if (
            not isinstance(marker.get("kind"), str)
            or _parse_utc(marker.get("at_utc")) is None
            or isinstance(marker.get("monotonic_ns"), bool)
            or not isinstance(marker.get("monotonic_ns"), int)
            or not isinstance(marker.get("boot_id"), str)
        ):
            lifecycle_errors.append(
                f"marker {index}: malformed lifecycle fields"
            )
            continue
        markers.append(marker)
    if (
        float(run_info.get("post_loss_settle_s", 0.0))
        >= float(run_info.get("post_loss_observation_s", 0.0))
    ):
        lifecycle_errors.append(
            "post-loss settle is not shorter than the observation window"
        )
    run_complete_path = output_dir / "run_complete.json"
    run_complete: dict[str, Any] | None = None
    if run_complete_path.is_file():
        try:
            run_complete = json.loads(
                run_complete_path.read_text(encoding="utf-8")
            )
            if not isinstance(run_complete, dict):
                lifecycle_errors.append(
                    "run_complete.json is not an object"
                )
                run_complete = None
            else:
                completion_errors = run_complete.get(
                    "lifecycle_errors", []
                )
                if not isinstance(completion_errors, list) or not all(
                    isinstance(item, str) for item in completion_errors
                ):
                    lifecycle_errors.append(
                        "run_complete.json lifecycle_errors is malformed"
                    )
                else:
                    lifecycle_errors.extend(completion_errors)
        except Exception as exc:
            lifecycle_errors.append(
                f"run_complete.json: {type(exc).__name__}: {exc}"
            )

    first_main_missing = next(
        (
            row
            for row in samples
            if not row.get("target", {}).get(
                "main_window_owned_by_target", False
            )
        ),
        None,
    )
    first_exit = next(
        (
            row
            for row in samples
            if not row.get("target", {}).get("process_alive", True)
        ),
        None,
    )
    role = run_info["target_role"]
    # The fault boundary begins with the first loss of the bound main-window
    # identity. Process exit is a separate outcome and can occur much later.
    # If an entire process disappears between samples, first_exit is the
    # fallback because the HWND necessarily disappeared with it.
    event_row = first_main_missing or first_exit
    event_ns = (
        event_row.get("observed_monotonic_ns") if event_row is not None else None
    )

    baseline_duration_s = run_info["baseline_duration_s"]
    baseline_ok = False
    if event_ns is not None and samples:
        baseline_ok = (
            event_ns - samples[0]["observed_monotonic_ns"]
        ) / 1_000_000_000 >= baseline_duration_s

    stream_max_age_s = max(
        3.0,
        3.0 * float(run_info.get("sample_interval_s", 0.5)),
    )
    required_stream_health = {
        path: _stream_health(
            samples,
            path,
            event_ns,
            max_age_s=stream_max_age_s,
        )
        for path in run_info.get("require_session_advance", [])
    }
    required_advances = {
        path: result["healthy"]
        for path, result in required_stream_health.items()
    }
    baseline_ok = baseline_ok and all(required_advances.values())

    session_observation_errors = [
        {
            "observed_at_utc": row.get("observed_at_utc"),
            "errors": row.get("session", {}).get("errors", []),
        }
        for row in samples
        if row.get("session", {}).get("errors")
    ]
    sample_gaps_s = [
        (
            current["observed_monotonic_ns"]
            - previous["observed_monotonic_ns"]
        ) / 1_000_000_000
        for previous, current in zip(samples, samples[1:])
        if isinstance(previous.get("observed_monotonic_ns"), (int, float))
        and isinstance(current.get("observed_monotonic_ns"), (int, float))
    ]
    max_sample_gap_s = max(sample_gaps_s, default=None)
    allowed_sample_gap_s = max(
        3.0,
        3.0 * float(run_info.get("sample_interval_s", 0.5)),
    )
    sampling_cadence_ok = (
        max_sample_gap_s is None
        or max_sample_gap_s <= allowed_sample_gap_s
    )

    settle_ns = round(run_info["post_loss_settle_s"] * 1_000_000_000)
    late_changes: list[dict[str, Any]] = []
    if event_ns is not None:
        for row in samples:
            if row["observed_monotonic_ns"] <= event_ns + settle_ns:
                continue
            substantive = [
                change
                for change in row.get("artifact_changes", [])
                if _is_substantive(change)
            ]
            if substantive:
                late_changes.append({
                    "observed_at_utc": row["observed_at_utc"],
                    "changes": substantive,
                })

    last_sample_ns = (
        samples[-1]["observed_monotonic_ns"] if samples else None
    )
    post_loss_observed_s = (
        (last_sample_ns - event_ns) / 1_000_000_000
        if event_ns is not None and last_sample_ns is not None
        else None
    )
    post_loss_complete = (
        post_loss_observed_s is not None
        and post_loss_observed_s >= run_info["post_loss_observation_s"]
    )

    process_exit_required = role in {"growth-monitor", "vendor-application"}
    process_state_ok = (
        first_exit is not None
        if process_exit_required
        else first_main_missing is not None
        and first_exit is None
    )
    session_quiet_required = role == "growth-monitor" and (
        run_info.get("session_dir") is not None
    )
    reaudit_changes: list[dict[str, Any]] = []
    reaudit_record: dict[str, Any] | None = None
    reaudit_path = output_dir / "session_reaudit.json"
    if reaudit_path.is_file():
        try:
            reaudit = json.loads(reaudit_path.read_text(encoding="utf-8"))
            if not isinstance(reaudit, dict):
                raise TypeError("session re-audit is not an object")
            reaudit_record = reaudit
            raw_reaudit_changes = reaudit.get(
                "changes_since_terminal_audit", []
            )
            if not isinstance(raw_reaudit_changes, list) or not all(
                isinstance(item, dict) for item in raw_reaudit_changes
            ):
                raise TypeError(
                    "session re-audit changes are malformed"
                )
            reaudit_changes = raw_reaudit_changes
        except Exception as exc:
            lifecycle_errors.append(
                f"session_reaudit.json: {type(exc).__name__}: {exc}"
            )
    session_quiet_ok = (
        not late_changes and not reaudit_changes
        if session_quiet_required else True
    )

    audit: dict[str, Any] | None = None
    audit_path = output_dir / "session_audit.json"
    if audit_path.is_file():
        try:
            raw_audit = json.loads(audit_path.read_text(encoding="utf-8"))
            if not isinstance(raw_audit, dict):
                raise TypeError("session audit is not an object")
            audit = raw_audit
        except Exception as exc:
            lifecycle_errors.append(
                f"session_audit.json: {type(exc).__name__}: {exc}"
            )
    integrity_errors: list[str] = []
    if audit:
        audit_errors = audit.get("audit_errors", [])
        csv_results = audit.get("csv", {})
        image_results = audit.get("image_errors", {})
        json_results = audit.get("json", {})
        frame_references = audit.get("frame_references", {})
        file_manifest = audit.get("file_manifest", {})
        if not isinstance(audit_errors, list):
            lifecycle_errors.append("session audit errors are malformed")
            audit_errors = []
        if not isinstance(csv_results, dict):
            lifecycle_errors.append("session audit CSV results are malformed")
            csv_results = {}
        if not isinstance(image_results, dict):
            lifecycle_errors.append(
                "session audit image results are malformed"
            )
            image_results = {}
        if not isinstance(json_results, dict):
            lifecycle_errors.append(
                "session audit JSON results are malformed"
            )
            json_results = {}
        if not isinstance(frame_references, dict):
            lifecycle_errors.append(
                "session audit frame references are malformed"
            )
            frame_references = {}
        if not isinstance(file_manifest, dict):
            lifecycle_errors.append(
                "session audit file manifest is malformed"
            )
            file_manifest = {}
        integrity_errors.extend(str(item) for item in audit_errors)
        for name, result in csv_results.items():
            if not isinstance(result, dict):
                lifecycle_errors.append(
                    f"session audit CSV result {name!r} is malformed"
                )
                continue
            csv_errors = result.get("errors", [])
            if not isinstance(csv_errors, list):
                lifecycle_errors.append(
                    f"session audit CSV errors for {name!r} are malformed"
                )
                continue
            integrity_errors.extend(
                f"{name}: {error}" for error in csv_errors
            )
        for name, errors in image_results.items():
            if not isinstance(errors, list):
                lifecycle_errors.append(
                    f"session audit image result {name!r} is malformed"
                )
                continue
            integrity_errors.extend(f"{name}: {error}" for error in errors)
        for name, result in json_results.items():
            if not isinstance(result, dict):
                lifecycle_errors.append(
                    f"session audit JSON result {name!r} is malformed"
                )
                continue
            if result.get("valid") is not True:
                integrity_errors.append(
                    f"{name}: {result.get('error') or 'invalid JSON'}"
                )
        reference_fields = {
            "parse_errors": "frame reference parse",
            "missing_references": "missing frame reference",
            "unsafe_external_references": (
                "unsafe external frame reference"
            ),
            "unreferenced_image_candidates": (
                "unreferenced image candidate"
            ),
        }
        for field, label in reference_fields.items():
            values = frame_references.get(field, [])
            if not isinstance(values, list):
                lifecycle_errors.append(
                    f"session audit {field} is malformed"
                )
                continue
            integrity_errors.extend(
                f"{label}: {value}" for value in values
            )
        if (
            role == "growth-monitor"
            and run_info.get("fault_style") == "graceful"
            and "session_metadata.json"
            not in file_manifest
        ):
            integrity_errors.append(
                "graceful Growth Monitor close did not produce "
                "session_metadata.json"
            )
    elif run_info.get("session_dir") is not None:
        lifecycle_errors.append(
            "session_audit.json is missing for the pinned session"
        )

    observed_kinds = {marker.get("kind") for marker in markers}
    marker_sequence_errors: list[str] = []
    prechecks = [
        marker for marker in markers
        if marker.get("kind") == "precheck-complete"
    ]
    actions = [
        marker for marker in markers
        if marker.get("kind") == "close-action"
    ]
    action_marker = actions[0] if len(actions) == 1 else None
    if len(prechecks) != 1:
        marker_sequence_errors.append(
            "exactly one precheck-complete marker is required"
        )
    if run_info.get("fault_style") != "spontaneous" and len(actions) != 1:
        marker_sequence_errors.append(
            "exactly one close-action marker is required"
        )
    if run_info.get("fault_style") == "spontaneous" and actions:
        marker_sequence_errors.append(
            "spontaneous exit must not be paired with close-action"
        )
    expected_boot_id = run_info.get("observer_boot_id")
    relevant_markers = prechecks + actions
    for marker in relevant_markers:
        if marker.get("boot_id") != expected_boot_id:
            marker_sequence_errors.append(
                f"{marker.get('kind')} marker belongs to another boot"
            )
        expected_actor = (
            run_info.get("observer")
            if marker.get("kind") == "precheck-complete"
            else run_info.get("operator")
        )
        if (
            str(marker.get("operator", "")).strip().casefold()
            != str(expected_actor).strip().casefold()
        ):
            marker_sequence_errors.append(
                f"{marker.get('kind')} marker has the wrong actor"
            )
        if run_info.get("test_context") == "live-idle-advisory":
            evidence_error = _verify_evidence_binding(
                marker.get("evidence_binding")
            )
            if evidence_error:
                marker_sequence_errors.append(
                    f"{marker.get('kind')} evidence verification failed: "
                    f"{evidence_error}"
                )
    if len(prechecks) == 1 and event_ns is not None:
        precheck_ns = prechecks[0].get("monotonic_ns", 0)
        if precheck_ns > event_ns:
            marker_sequence_errors.append(
                "precheck-complete occurred after target loss"
            )
        if samples and (
            precheck_ns - samples[0]["observed_monotonic_ns"]
        ) / 1_000_000_000 < baseline_duration_s:
            marker_sequence_errors.append(
                "precheck-complete occurred before the required baseline "
                "elapsed"
            )
    if len(prechecks) == 1 and action_marker is not None:
        if prechecks[0].get("monotonic_ns", 0) >= action_marker.get(
            "monotonic_ns", 0
        ):
            marker_sequence_errors.append(
                "precheck-complete must precede close-action"
            )
    if action_marker is not None and event_ns is not None:
        if action_marker.get("monotonic_ns", 0) > event_ns:
            marker_sequence_errors.append(
                "close-action marker occurred after target loss"
            )
        if samples and (
            action_marker.get("monotonic_ns", 0)
            - samples[0]["observed_monotonic_ns"]
        ) / 1_000_000_000 < baseline_duration_s:
            marker_sequence_errors.append(
                "close-action occurred before the required baseline elapsed"
            )
    detection_latency_ms = (
        round(
            (
                event_ns - action_marker["monotonic_ns"]
            ) / 1_000_000,
            3,
        )
        if action_marker is not None
        and event_ns is not None
        and action_marker.get("monotonic_ns", 0) <= event_ns
        else None
    )
    action_to_main_window_loss_ms = (
        round(
            (
                first_main_missing["observed_monotonic_ns"]
                - action_marker["monotonic_ns"]
            ) / 1_000_000,
            3,
        )
        if action_marker is not None
        and first_main_missing is not None
        and action_marker.get("monotonic_ns", 0)
        <= first_main_missing["observed_monotonic_ns"]
        else None
    )
    action_to_process_exit_ms = (
        round(
            (
                first_exit["observed_monotonic_ns"]
                - action_marker["monotonic_ns"]
            ) / 1_000_000,
            3,
        )
        if action_marker is not None
        and first_exit is not None
        and action_marker.get("monotonic_ns", 0)
        <= first_exit["observed_monotonic_ns"]
        else None
    )
    main_window_loss_to_process_exit_ms = (
        round(
            (
                first_exit["observed_monotonic_ns"]
                - first_main_missing["observed_monotonic_ns"]
            ) / 1_000_000,
            3,
        )
        if first_main_missing is not None and first_exit is not None
        else None
    )
    restart_markers = {
        kind: kind in observed_kinds
        for kind in (
            "gui-restarted",
            "idle-confirmed",
            "gui-rearmed",
            "new-session-started",
        )
    }
    terminal_markers = [
        marker
        for marker in markers
        if marker.get("kind") in {"abort", "incomplete"}
    ]
    recovery_errors: list[str] = []
    if run_info.get("require_recovery_validation", False):
        completion_ns = (
            run_complete.get("completed_monotonic_ns")
            if isinstance(run_complete, dict)
            else None
        )
        if isinstance(completion_ns, bool) or not isinstance(
            completion_ns, int
        ):
            recovery_errors.append(
                "observer completion has no valid monotonic timestamp"
            )
        recovery_order = [
            "gui-restarted",
            "idle-confirmed",
            "gui-rearmed",
        ]
        if run_info.get("session_dir") is not None:
            recovery_order.append("new-session-started")
        recovery_rows: list[dict[str, Any]] = []
        for kind in recovery_order:
            matches = [
                marker for marker in markers
                if marker.get("kind") == kind
            ]
            if len(matches) != 1:
                recovery_errors.append(
                    f"exactly one {kind} marker is required"
                )
            else:
                recovery_marker = matches[0]
                recovery_rows.append(recovery_marker)
                marker_ns = recovery_marker.get("monotonic_ns")
                if event_ns is None or marker_ns <= event_ns:
                    recovery_errors.append(
                        f"{kind} did not occur after target loss"
                    )
                if (
                    isinstance(completion_ns, int)
                    and marker_ns <= completion_ns
                ):
                    recovery_errors.append(
                        f"{kind} did not occur after observer completion"
                    )
                expected_actor = (
                    run_info.get("observer")
                    if kind in {"gui-restarted", "idle-confirmed"}
                    else run_info.get("operator")
                )
                if (
                    str(recovery_marker.get("operator", "")).strip().casefold()
                    != str(expected_actor).strip().casefold()
                ):
                    recovery_errors.append(
                        f"{kind} was not written by the required run role"
                    )
                if recovery_marker.get("boot_id") != expected_boot_id:
                    recovery_errors.append(
                        f"{kind} marker belongs to another boot"
                    )
                if kind in {
                    "idle-confirmed",
                    "gui-rearmed",
                    "new-session-started",
                } and not str(matches[0].get("evidence_ref") or "").strip():
                    recovery_errors.append(
                        f"{kind} lacks a read-only evidence reference"
                    )
                if kind in {
                    "idle-confirmed",
                    "gui-rearmed",
                    "new-session-started",
                }:
                    if (
                        recovery_marker.get(
                            "recovery_target_reverified"
                        )
                        is not True
                    ):
                        recovery_errors.append(
                            f"{kind} did not re-verify the restarted GUI"
                        )
                    evidence_error = _verify_evidence_binding(
                        recovery_marker.get("evidence_binding")
                    )
                    if evidence_error:
                        recovery_errors.append(
                            f"{kind} evidence verification failed: "
                            f"{evidence_error}"
                        )
        marker_times: list[int] = []
        if len(recovery_rows) == len(recovery_order):
            marker_times = [
                marker.get("monotonic_ns") for marker in recovery_rows
            ]
            if (
                not all(isinstance(value, int) for value in marker_times)
                or marker_times != sorted(marker_times)
                or len(set(marker_times)) != len(marker_times)
            ):
                recovery_errors.append(
                    "recovery markers are out of order"
                )
        restart = next(
            (
                marker for marker in markers
                if marker.get("kind") == "gui-restarted"
            ),
            None,
        )
        if restart and restart.get("new_target_identity_verified") is not True:
            recovery_errors.append(
                "gui-restarted lacks a verified new PID/HWND identity"
            )
        if restart:
            raw_new_identity = restart.get("new_target_identity")
            new_identity = (
                raw_new_identity
                if isinstance(raw_new_identity, dict)
                else {}
            )
            if not isinstance(restart.get("new_target_binding"), dict):
                recovery_errors.append(
                    "gui-restarted lacks a reusable target binding"
                )
            original_identity = run_info.get("target_identity", {})
            same_pid = (
                new_identity.get("pid") == original_identity.get("pid")
            )
            if role == "vendor-data-window" and same_pid:
                if (
                    new_identity.get("creation_time_utc")
                    != original_identity.get("creation_time_utc")
                    or new_identity.get("main_hwnd")
                    == original_identity.get("main_hwnd")
                ):
                    recovery_errors.append(
                        "same-process vendor recovery did not bind a new "
                        "top-level data HWND on the original process"
                    )
            elif (
                same_pid
                or new_identity.get("creation_time_utc")
                == original_identity.get("creation_time_utc")
            ):
                recovery_errors.append(
                    "gui-restarted did not establish a distinct replacement "
                    "process identity"
                )
        if run_info.get("session_dir") is not None:
            new_session_marker = next(
                (
                    marker
                    for marker in markers
                    if marker.get("kind") == "new-session-started"
                ),
                None,
            )
            binding = (
                new_session_marker.get("new_session_binding")
                if isinstance(new_session_marker, dict)
                else None
            )
            if not isinstance(binding, dict):
                recovery_errors.append(
                    "new-session-started lacks a verified session binding"
                )
            else:
                try:
                    new_session_path = Path(str(binding["path"]))
                    session_stat = new_session_path.stat()
                    sensor_result = inspect_csv(
                        new_session_path / "sensor_log.csv"
                    )
                    if (
                        not new_session_path.is_dir()
                        or session_stat.st_dev != binding.get("st_dev")
                        or session_stat.st_ino != binding.get("st_ino")
                        or sensor_result.get("errors")
                        or sensor_result.get("data_row_count", 0)
                        < binding.get(
                            "sensor_log_data_rows_at_marker", 1
                        )
                    ):
                        raise RuntimeError(
                            "new session identity or sensor stream is invalid"
                        )
                except Exception as exc:
                    recovery_errors.append(
                        "new-session-started verification failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        if (
            run_info.get("session_dir") is not None
            and not reaudit_path.is_file()
        ):
            recovery_errors.append(
                "old session has not been re-audited after restart"
            )
        elif run_info.get("session_dir") is not None:
            reaudit_ns = (
                reaudit_record.get("observed_monotonic_ns")
                if isinstance(reaudit_record, dict)
                else None
            )
            reaudit_boot = (
                reaudit_record.get("boot_id")
                if isinstance(reaudit_record, dict)
                else None
            )
            if (
                isinstance(reaudit_ns, bool)
                or not isinstance(reaudit_ns, int)
                or reaudit_boot != expected_boot_id
                or not marker_times
                or reaudit_ns <= max(marker_times)
            ):
                recovery_errors.append(
                    "old session re-audit was not recorded after the "
                    "complete recovery marker sequence on the same boot"
                )

    graceful_exit_error: str | None = None
    if (
        run_info.get("fault_style") == "graceful"
        and process_exit_required
        and first_exit is not None
        and first_exit.get("target", {}).get("exit_code") != 0
    ):
        graceful_exit_error = (
            "graceful close produced nonzero process exit code "
            f"{first_exit.get('target', {}).get('exit_code')!r}"
        )

    lifecycle_errors.extend(
        f"session sample: {item['observed_at_utc']}: {error}"
        for item in session_observation_errors
        for error in item["errors"]
    )

    if run_complete is None:
        exit_code = EXIT_EVIDENCE_FATAL
        outcome = "evidence-fatal-observer-did-not-complete"
    elif lifecycle_errors:
        exit_code = EXIT_EVIDENCE_FATAL
        outcome = "evidence-fatal"
    elif terminal_markers:
        exit_code = EXIT_INCOMPLETE
        outcome = "aborted-or-incomplete"
    elif (
        event_row is None
        or not baseline_ok
        or not post_loss_complete
        or not sampling_cadence_ok
        or (
            run_info.get("require_action_markers", False)
            and marker_sequence_errors
        )
        or recovery_errors
    ):
        exit_code = EXIT_INCOMPLETE
        outcome = "incomplete-or-not-evaluable"
    elif (
        not process_state_ok
        or not session_quiet_ok
        or integrity_errors
        or graceful_exit_error is not None
    ):
        exit_code = EXIT_VALIDATION_FAILURE
        outcome = "validation-failure"
    else:
        exit_code = EXIT_PASS
        outcome = "lifecycle-phase-pass"

    summary = {
        "generated_at_utc": utc_now(),
        "target_role": role,
        "outcome": outcome,
        "exit_code": exit_code,
        "sample_count": len(raw_samples),
        "observer_completed": run_complete is not None,
        "first_main_window_missing": first_main_missing,
        "first_process_exit": first_exit,
        "baseline_duration_required_s": baseline_duration_s,
        "baseline_ok": baseline_ok,
        "required_session_advances": required_advances,
        "required_stream_health": required_stream_health,
        "session_observation_errors": session_observation_errors,
        "max_sample_gap_s": max_sample_gap_s,
        "allowed_sample_gap_s": allowed_sample_gap_s,
        "sampling_cadence_ok": sampling_cadence_ok,
        "action_to_loss_detection_ms": detection_latency_ms,
        "action_to_main_window_loss_ms": action_to_main_window_loss_ms,
        "action_to_process_exit_ms": action_to_process_exit_ms,
        "main_window_loss_to_process_exit_ms": (
            main_window_loss_to_process_exit_ms
        ),
        "action_markers_required": run_info.get(
            "require_action_markers", False
        ),
        "marker_sequence_errors": marker_sequence_errors,
        "post_loss_observed_s": post_loss_observed_s,
        "post_loss_observation_complete": post_loss_complete,
        "late_session_changes": late_changes,
        "post_run_old_session_changes": reaudit_changes,
        "session_quiet_ok": session_quiet_ok,
        "session_integrity_errors": integrity_errors,
        "restart_markers": restart_markers,
        "recovery_validation_required": run_info.get(
            "require_recovery_validation", False
        ),
        "recovery_errors": recovery_errors,
        "terminal_markers": terminal_markers,
        "graceful_exit_error": graceful_exit_error,
        "markers": markers,
        "evidence_errors": lifecycle_errors,
        "limitations": [
            "A lifecycle-phase pass does not prove worker fail-closed behavior.",
            "Markers are operator/observer assertions unless independently corroborated.",
            "Session flush/parseability does not prove power-loss durability.",
            "The observer never infers clean versus abrupt shutdown from metadata alone.",
        ],
    }
    return summary, exit_code


def _evidence_paths(output_dir: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for name in (
        "run_info.json",
        "observer_status.json",
        "samples.jsonl",
        "session_audit.json",
        "session_reaudit.json",
        "summary.json",
        "run_complete.json",
    ):
        path = output_dir / name
        if path.is_file():
            paths[name] = path
    marker_dir = output_dir / "markers"
    if marker_dir.is_dir():
        for path in sorted(marker_dir.glob("*.json")):
            paths[f"markers/{path.name}"] = path
    return paths


def write_evidence_manifest(output_dir: Path) -> dict[str, str]:
    manifest = {
        name: _sha256(path)
        for name, path in _evidence_paths(output_dir).items()
    }
    _write_json_atomic(output_dir / "sha256_manifest.json", manifest)
    return manifest


def verify_existing_manifest(output_dir: Path) -> None:
    """Refuse to bless post-run edits by simply replacing the manifest."""
    path = output_dir / "sha256_manifest.json"
    if not path.is_file():
        return
    try:
        expected = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"cannot parse existing evidence manifest: {exc}"
        ) from exc
    if not isinstance(expected, dict):
        raise RuntimeError("existing evidence manifest is not an object")
    mismatches: list[str] = []
    actual_names = set(_evidence_paths(output_dir))
    expected_names = set(expected)
    for name in sorted(actual_names - expected_names):
        mismatches.append(f"{name}: untracked evidence file")
    for name in sorted(expected_names - actual_names):
        mismatches.append(f"{name}: tracked evidence file is missing")
    for name, digest in expected.items():
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts:
            mismatches.append(f"{name}: unsafe manifest path")
            continue
        evidence_path = output_dir / name
        if not evidence_path.is_file():
            mismatches.append(f"{name}: missing")
        elif not isinstance(digest, str) or _sha256(evidence_path) != digest:
            mismatches.append(f"{name}: SHA-256 mismatch")
    if mismatches:
        raise RuntimeError(
            "existing evidence failed manifest verification: "
            + "; ".join(mismatches)
        )


def _named_windows_event_exists(name: Any) -> bool:
    if os.name != "nt" or not isinstance(name, str) or not name:
        return False
    import ctypes
    import ctypes.wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.LPCWSTR,
    ]
    kernel32.OpenEventW.restype = ctypes.wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    handle = kernel32.OpenEventW(
        Win32Target.SYNCHRONIZE,
        False,
        name,
    )
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _validate_paired_failure_health(run_info: dict[str, Any]) -> None:
    paired = run_info.get("paired_failure_run")
    if not paired:
        return
    paired_dir = Path(paired["output_dir"])
    paired_info = json.loads(
        (paired_dir / "run_info.json").read_text(encoding="utf-8")
    )
    if _sha256(paired_dir / "run_info.json") != paired["run_info_sha256"]:
        raise RuntimeError("paired failure run_info changed after binding")
    states, state_errors = _load_jsonl(paired_dir / "states.jsonl")
    watchdog, watchdog_errors = _load_jsonl(
        paired_dir / "watchdog.jsonl"
    )
    if state_errors or watchdog_errors or not states or not watchdog:
        raise RuntimeError(
            "paired failure recorder evidence is missing or malformed"
        )
    latest_state = states[-1]
    latest_watchdog = watchdog[-1]
    if not isinstance(paired_info, dict):
        raise RuntimeError("paired failure run_info is malformed")
    latest_utc = _parse_utc(latest_state.get("observed_at_utc"))
    if latest_utc is None:
        raise RuntimeError("paired failure State has no valid UTC time")
    age_s = (datetime.now(timezone.utc) - latest_utc).total_seconds()
    max_age_s = max(
        3.0,
        3.0 * float(paired_info.get("poll_interval_s", 1.0)),
    )
    if age_s < -1.0 or age_s > max_age_s:
        raise RuntimeError(
            f"paired failure State age {age_s:.3f} s exceeds "
            f"{max_age_s:.3f} s"
        )
    if latest_state.get("measurement_valid") is not True:
        raise RuntimeError("paired failure State baseline is not valid")
    if latest_watchdog.get("worker_running") is not True:
        raise RuntimeError("paired failure worker is not running")
    watchdog_utc = _parse_utc(latest_watchdog.get("observed_at_utc"))
    watchdog_age_s = (
        (datetime.now(timezone.utc) - watchdog_utc).total_seconds()
        if watchdog_utc is not None
        else math.inf
    )
    watchdog_max_age_s = max(
        3.0,
        3.0 * float(paired_info.get("watchdog_interval_s", 0.5)),
    )
    if watchdog_age_s < -1.0 or watchdog_age_s > watchdog_max_age_s:
        raise RuntimeError(
            f"paired failure watchdog age {watchdog_age_s:.3f} s exceeds "
            f"{watchdog_max_age_s:.3f} s"
        )
    run_boot_id = paired_info.get("boot_id")
    current_boot_id = current_boot_identity()[0]
    if (
        not isinstance(run_boot_id, str)
        or not run_boot_id
        or run_boot_id != current_boot_id
        or latest_state.get("boot_id") != run_boot_id
        or latest_watchdog.get("boot_id") != run_boot_id
    ):
        raise RuntimeError(
            "paired failure recorder, State, and watchdog are not on the "
            "current boot"
        )
    if not _named_windows_event_exists(
        paired_info.get("recorder_liveness_name")
    ):
        raise RuntimeError("paired failure recorder liveness lease is absent")
    driver_identity = latest_watchdog.get("driver_identity") or {}
    if driver_identity.get("hwnd") != paired.get("source_hwnd"):
        raise RuntimeError(
            "paired failure worker HWND changed after lifecycle binding"
        )


def _validate_pre_action_marker(
    output_dir: Path,
    run_info: dict[str, Any],
    kind: str,
    actor: str,
) -> None:
    """Fail closed before an operator is told to perform a GUI action."""
    if kind not in {"precheck-complete", "close-action"}:
        return
    if not _ObserverLease(output_dir).is_live():
        raise RuntimeError("observer is not live; refusing an action marker")
    if (output_dir / "run_complete.json").exists():
        raise RuntimeError("observer already completed; refusing action marker")
    status_path = output_dir / "observer_status.json"
    if not status_path.is_file():
        raise RuntimeError(
            "observer sampling status is missing; refusing an action marker"
        )
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(status, dict):
        raise RuntimeError("observer sampling status is malformed")
    if status.get("sampling_active") is not True:
        raise RuntimeError(
            "observer sampling phase has ended; refusing an action marker"
        )
    if status.get("observer_pid") != run_info.get("observer_pid"):
        raise RuntimeError("observer sampling status PID does not match")
    current_boot_id = current_boot_identity()[0]
    if current_boot_id != run_info.get("observer_boot_id"):
        raise RuntimeError("action marker belongs to a different Windows boot")
    if status.get("observer_boot_id") != current_boot_id:
        raise RuntimeError("observer sampling status belongs to another boot")

    expected_actor = (
        run_info.get("observer")
        if kind == "precheck-complete"
        else run_info.get("operator")
    )
    if actor.strip().casefold() != str(expected_actor).strip().casefold():
        raise ValueError(
            f"{kind} must be written by the configured "
            f"{'observer' if kind == 'precheck-complete' else 'operator'}"
        )

    markers, marker_errors = _marker_rows(output_dir)
    if marker_errors:
        raise RuntimeError("; ".join(marker_errors))
    kinds = [marker.get("kind") for marker in markers]
    if any(item in {"abort", "incomplete"} for item in kinds):
        raise RuntimeError("run is already aborted or incomplete")
    if kind in kinds:
        raise RuntimeError(f"duplicate {kind} marker")
    if kind == "close-action" and kinds.count("precheck-complete") != 1:
        raise RuntimeError(
            "close-action requires exactly one precheck-complete marker"
        )

    samples, sample_errors = _load_jsonl(output_dir / "samples.jsonl")
    if sample_errors:
        raise RuntimeError("; ".join(sample_errors))
    if len(samples) < 2:
        raise RuntimeError("not enough observer samples for a baseline")
    first = samples[0]
    latest = samples[-1]
    try:
        first_ns = int(first["observed_monotonic_ns"])
        latest_ns = int(latest["observed_monotonic_ns"])
        target = latest["target"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid latest observer sample: {exc}") from exc
    baseline_span_s = (latest_ns - first_ns) / 1_000_000_000
    if baseline_span_s < float(run_info["baseline_duration_s"]):
        raise RuntimeError(
            f"baseline is only {baseline_span_s:.3f} s; "
            f"{run_info['baseline_duration_s']} s is required"
        )
    if not isinstance(target, dict):
        raise RuntimeError("latest observer target is malformed")

    now_monotonic_ns = time.monotonic_ns()
    max_sample_age_s = max(
        3.0,
        3.0 * float(run_info.get("sample_interval_s", 0.5)),
    )
    monotonic_sample_age_s = (
        now_monotonic_ns - latest_ns
    ) / 1_000_000_000
    if (
        monotonic_sample_age_s < -0.1
        or monotonic_sample_age_s > max_sample_age_s
    ):
        raise RuntimeError(
            f"latest observer monotonic age {monotonic_sample_age_s:.3f} s "
            f"exceeds the {max_sample_age_s:.3f} s action gate"
        )
    deadline_ns = run_info.get("observer_deadline_monotonic_ns")
    if isinstance(deadline_ns, bool) or not isinstance(deadline_ns, int):
        raise RuntimeError("observer deadline is missing or malformed")
    reserve_s = float(run_info["post_loss_observation_s"]) + max(
        1.0,
        2.0 * float(run_info["sample_interval_s"]),
    )
    remaining_s = (deadline_ns - now_monotonic_ns) / 1_000_000_000
    if remaining_s < reserve_s:
        raise RuntimeError(
            f"observer has only {remaining_s:.3f} s remaining; "
            f"{reserve_s:.3f} s is required to cover the action"
        )

    latest_utc = _parse_utc(latest.get("observed_at_utc"))
    if latest_utc is None:
        raise RuntimeError("latest observer sample has no valid UTC time")
    sample_age_s = (
        datetime.now(timezone.utc) - latest_utc
    ).total_seconds()
    if sample_age_s < -1.0 or sample_age_s > max_sample_age_s:
        raise RuntimeError(
            f"latest observer sample age {sample_age_s:.3f} s exceeds "
            f"the {max_sample_age_s:.3f} s action gate"
        )
    if target.get("process_alive") is not True:
        raise RuntimeError("target process is not alive")
    if target.get("main_window_identity_matches") is not True:
        # Older synthetic rows use the original field name.
        if target.get("main_window_owned_by_target") is not True:
            raise RuntimeError("bound main-window identity is unavailable")
    main_window = target.get("main_window") or {}
    if main_window.get("visible") is not True:
        raise RuntimeError("bound main window is not visible")
    latest_session = latest.get("session", {})
    if not isinstance(latest_session, dict):
        raise RuntimeError("latest session sample is malformed")
    if latest_session.get("errors"):
        raise RuntimeError(
            "latest session sample contains observation errors"
        )
    recent = samples[-min(5, len(samples)):]
    for previous, current in zip(recent, recent[1:]):
        gap_s = (
            int(current["observed_monotonic_ns"])
            - int(previous["observed_monotonic_ns"])
        ) / 1_000_000_000
        if gap_s <= 0 or gap_s > max_sample_age_s:
            raise RuntimeError(
                f"recent observer cadence gap {gap_s:.3f} s is invalid"
            )
    for row in samples:
        row_target = row.get("target")
        if not isinstance(row_target, dict):
            raise RuntimeError("observer sample target is malformed")
        identity_matches = row_target.get(
            "main_window_identity_matches",
            row_target.get("main_window_owned_by_target"),
        )
        if (
            row_target.get("process_alive") is not True
            or identity_matches is not True
        ):
            raise RuntimeError(
                "target loss was already observed; refusing an action marker"
            )

    stream_max_age_s = max_sample_age_s
    for path in run_info.get("require_session_advance", []):
        health = _stream_health(
            samples,
            path,
            latest_ns + 1,
            max_age_s=stream_max_age_s,
        )
        if not health["healthy"]:
            raise RuntimeError(
                f"required stream {path!r} lacks a fresh, repeated baseline: "
                f"{health}"
            )
    _validate_paired_failure_health(run_info)


def _build_sample(
    index: int,
    target: TargetProbe,
    sampler: SessionSampler | None,
    previous: SessionSnapshot | None,
    *,
    now_utc: Callable[[], str],
    monotonic_ns: Callable[[], int],
) -> tuple[dict[str, Any], SessionSnapshot | None]:
    sample_started_perf_ns = time.perf_counter_ns()
    target_sample = target.sample()
    session_scan_started_perf_ns = time.perf_counter_ns()
    current = sampler.snapshot() if sampler is not None else None
    session_scan_ended_perf_ns = time.perf_counter_ns()
    changes = (
        diff_artifacts(previous, current)
        if current is not None
        else []
    )
    sample_ended_perf_ns = time.perf_counter_ns()
    observed_ns = monotonic_ns()
    sample_duration_ns = sample_ended_perf_ns - sample_started_perf_ns
    row: dict[str, Any] = {
        "sample_index": index,
        "observed_at_utc": now_utc(),
        "observed_monotonic_ns": observed_ns,
        "sample_started_monotonic_ns_estimate": (
            observed_ns - sample_duration_ns
        ),
        "sample_duration_ms": round(sample_duration_ns / 1_000_000, 3),
        "session_scan_duration_ms": round(
            (
                session_scan_ended_perf_ns
                - session_scan_started_perf_ns
            ) / 1_000_000,
            3,
        ),
        "target": target_sample,
        "artifact_changes": changes,
    }
    if current is not None:
        row["session"] = {
            "snapshot_sha256": current.digest,
            "file_count": len(current.entries),
            "total_bytes": current.total_bytes,
            "errors": list(current.errors),
            "known_files": {
                name: current.entries.get(name)
                for name in KNOWN_SESSION_FILES
            },
        }
    return row, current


def observe(
    args: argparse.Namespace,
    *,
    target: TargetProbe | None = None,
    sampler: SessionSampler | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    now_utc: Callable[[], str] = utc_now,
) -> int:
    session_dir = (
        args.session_dir.expanduser().resolve()
        if args.session_dir is not None
        else None
    )
    owns_target = target is None
    if target is None:
        target = Win32Target(
            args.pid,
            expected_title=args.expected_title,
            hwnd=args.hwnd,
            expected_image=args.expected_image,
            expected_command_token=args.expected_command_token,
        )
    try:
        if sampler is None and session_dir is not None:
            sampler = SessionSampler(session_dir)
        output_dir = prepare_output_dir(args.output_dir, session_dir)
    except Exception:
        if owns_target:
            target.close()
        raise

    lease = _ObserverLease(output_dir)
    try:
        lease.acquire()
        boot_id, boot_source = current_boot_identity()
        start_ns = monotonic_ns()
        deadline_ns = start_ns + round(args.duration_s * 1_000_000_000)
        repo_root = Path(__file__).resolve().parent.parent
        git_status = _git_value(repo_root, "status", "--porcelain")
        paired_failure = (
            bind_paired_failure_run(
                args.paired_failure_run,
                target,
                args.target_role,
            )
            if getattr(args, "paired_failure_run", None) is not None
            else None
        )
        run_info = {
            "started_at_utc": now_utc(),
            "observer_started_monotonic_ns": start_ns,
            "observer_deadline_monotonic_ns": deadline_ns,
            "observer_pid": os.getpid(),
            "observer_boot_id": boot_id,
            "observer_boot_id_source": boot_source,
            "observer_liveness_name": lease.name,
            "observer_commit": _git_value(repo_root, "rev-parse", "HEAD"),
            "observer_dirty": (
                bool(git_status) if git_status is not None else None
            ),
            "target_role": args.target_role,
            "scenario_id": args.scenario_id,
            "fault_style": args.fault_style,
            "test_context": args.test_context,
            "gui_state": getattr(args, "gui_state", None),
            "operator": args.operator,
            "observer": args.observer,
            "approver": args.approver,
            "authorization_ref": args.authorization_ref,
            "sop_ref": args.sop_ref,
            "safe_state_note": args.safe_state_note,
            "abort_criteria": args.abort_criteria,
            "safe_state_confirmed": args.confirm_safe_state,
            "require_action_markers": getattr(
                args, "require_action_markers", False
            ),
            "require_recovery_validation": getattr(
                args, "require_recovery_validation", False
            ),
            "target_identity": target.identity.as_dict(),
            "paired_failure_run": paired_failure,
            "session_dir": (
                str(session_dir) if session_dir is not None else None
            ),
            "sample_interval_s": args.sample_interval_s,
            "duration_s": args.duration_s,
            "baseline_duration_s": args.baseline_duration_s,
            "post_loss_observation_s": args.post_loss_observation_s,
            "post_loss_settle_s": args.post_loss_settle_s,
            "orphan_grace_s": args.orphan_grace_s,
            "require_session_advance": list(args.require_session_advance),
            "safety_contract": {
                "process_access_mask": (
                    Win32Target.PROCESS_QUERY_LIMITED_INFORMATION
                    | Win32Target.SYNCHRONIZE
                ),
                "terminate_right_requested": False,
                "sends_window_messages": False,
                "injects_input": False,
                "opens_instrument_interfaces": False,
            },
        }
        with _EvidenceMutex(output_dir):
            _write_json_atomic(output_dir / "run_info.json", run_info)
            _write_json_atomic(
                output_dir / "observer_status.json",
                {
                    "updated_at_utc": now_utc(),
                    "updated_monotonic_ns": start_ns,
                    "observer_pid": os.getpid(),
                    "observer_boot_id": boot_id,
                    "sampling_active": True,
                    "phase": "sampling",
                },
            )
            (output_dir / "markers").mkdir(exist_ok=True)
    except Exception:
        lease.release()
        if owns_target:
            target.close()
        raise

    previous: SessionSnapshot | None = None
    loss_ns: int | None = None
    main_loss_ns: int | None = None
    process_exit_ns: int | None = None
    lifecycle_errors: list[str] = []
    index = 0
    sampling_sealed = False

    def seal_sampling_locked() -> None:
        nonlocal sampling_sealed
        if sampling_sealed:
            return
        _write_json_atomic(
            output_dir / "observer_status.json",
            {
                "updated_at_utc": now_utc(),
                "updated_monotonic_ns": monotonic_ns(),
                "observer_pid": os.getpid(),
                "observer_boot_id": boot_id,
                "sampling_active": False,
                "phase": "final-audit",
            },
        )
        sampling_sealed = True

    try:
        with (output_dir / "samples.jsonl").open(
            "a", encoding="utf-8"
        ) as stream:
            while True:
                should_stop = False
                with _EvidenceMutex(output_dir):
                    try:
                        row, current = _build_sample(
                            index,
                            target,
                            sampler,
                            previous,
                            now_utc=now_utc,
                            monotonic_ns=monotonic_ns,
                        )
                        _append_jsonl(stream, row)
                        previous = current
                        sample_ns = row["observed_monotonic_ns"]
                        target_row = row["target"]

                        if (
                            main_loss_ns is None
                            and not target_row.get(
                                "main_window_owned_by_target", False
                            )
                        ):
                            main_loss_ns = sample_ns
                        if (
                            process_exit_ns is None
                            and not target_row.get("process_alive", True)
                        ):
                            process_exit_ns = sample_ns

                        if args.target_role == "vendor-data-window":
                            loss_ns = main_loss_ns
                        else:
                            loss_ns = process_exit_ns

                        should_stop = (
                            loss_ns is not None
                            and (sample_ns - loss_ns) / 1_000_000_000
                            >= args.post_loss_observation_s
                        )
                        should_stop = should_stop or (
                            args.target_role
                            in {"growth-monitor", "vendor-application"}
                            and main_loss_ns is not None
                            and process_exit_ns is None
                            and (
                                sample_ns - main_loss_ns
                            ) / 1_000_000_000
                            >= args.orphan_grace_s
                        )
                        should_stop = should_stop or (
                            (sample_ns - start_ns) / 1_000_000_000
                            >= args.duration_s
                        )
                    except Exception as exc:
                        lifecycle_errors.append(
                            f"{type(exc).__name__}: {exc}"
                        )
                        should_stop = True
                    except BaseException:
                        seal_sampling_locked()
                        raise
                    if should_stop:
                        seal_sampling_locked()
                if should_stop:
                    break
                index += 1
                sleep(args.sample_interval_s)
    finally:
        try:
            if not sampling_sealed:
                with _EvidenceMutex(output_dir):
                    seal_sampling_locked()
        finally:
            if owns_target:
                target.close()
            if sys.exc_info()[0] is not None:
                lease.release()

    try:
        if session_dir is not None:
            try:
                _write_json_atomic(
                    output_dir / "session_audit.json",
                    audit_session(session_dir),
                )
            except Exception as exc:
                lifecycle_errors.append(
                    f"session audit failed: {type(exc).__name__}: {exc}"
                )
        with _EvidenceMutex(output_dir):
            verify_existing_manifest(output_dir)
            _write_json_atomic(
                output_dir / "run_complete.json",
                {
                    "completed_at_utc": now_utc(),
                    "completed_monotonic_ns": monotonic_ns(),
                    "observer_boot_id": boot_id,
                    "lifecycle_errors": lifecycle_errors,
                },
            )
            summary, exit_code = summarize_run(output_dir)
            _write_json_atomic(output_dir / "summary.json", summary)
            write_evidence_manifest(output_dir)
        return exit_code
    finally:
        lease.release()


def _bind_evidence_reference(raw_value: str) -> dict[str, Any]:
    """Bind a recovery assertion to one stable, existing read-only file."""
    path = Path(raw_value).expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(
            f"evidence reference must be a regular file: {path}"
        )
    before = path.stat()
    digest = _sha256(path)
    after = path.stat()
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise RuntimeError(
            f"evidence reference changed while hashing: {path}"
        )
    return {
        "path": str(path),
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "sha256": digest,
    }


def _verify_evidence_binding(binding: Any) -> str | None:
    """Return a diagnostic if a bound read-only evidence file changed."""
    if not isinstance(binding, dict):
        return "no bound evidence file"
    try:
        path = Path(str(binding["path"]))
        stat = path.stat()
        if (
            not path.is_file()
            or path.is_symlink()
            or stat.st_size != binding.get("size")
            or stat.st_mtime_ns != binding.get("mtime_ns")
            or _sha256(path) != binding.get("sha256")
        ):
            return "evidence file identity or digest changed"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _verify_recovery_target(
    binding: dict[str, Any],
    expected_identity: dict[str, Any],
    *,
    supplied_target: TargetProbe | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-open and verify the restarted GUI at every recovery checkpoint."""
    if not isinstance(binding, dict) or not isinstance(
        expected_identity, dict
    ):
        raise RuntimeError("recovery target binding is malformed")
    owns_target = supplied_target is None
    target = supplied_target
    if target is None:
        target = Win32Target(
            int(binding["pid"]),
            hwnd=int(binding["hwnd"]),
            expected_title=str(binding["expected_title"]),
            expected_image=str(binding["expected_image"]),
            expected_command_token=str(binding["expected_command_token"]),
        )
    try:
        identity = target.identity.as_dict()
        for field in (
            "pid",
            "creation_time_utc",
            "image_path",
            "main_hwnd",
            "main_title",
            "main_class_name",
        ):
            if identity.get(field) != expected_identity.get(field):
                raise RuntimeError(
                    f"restarted GUI identity field {field!r} changed"
                )
        sample = target.sample()
        if not isinstance(sample, dict):
            raise RuntimeError("restarted GUI sample is malformed")
        identity_matches = sample.get(
            "main_window_identity_matches",
            sample.get("main_window_owned_by_target"),
        )
        if (
            sample.get("process_alive") is not True
            or identity_matches is not True
        ):
            raise RuntimeError(
                "restarted GUI process/window identity is not live"
            )
        main_window = sample.get("main_window")
        if not isinstance(main_window, dict) or (
            main_window.get("visible") is not True
        ):
            raise RuntimeError("restarted GUI main window is not visible")
        return identity, sample
    finally:
        if owns_target:
            target.close()


def mark_event(
    args: argparse.Namespace,
    *,
    restart_target: TargetProbe | None = None,
) -> int:
    output_dir = args.output_dir.expanduser().resolve()
    if not (output_dir / "run_info.json").is_file():
        raise FileNotFoundError("run_info.json not found in output directory")
    if args.kind not in MARKER_KINDS:
        raise ValueError(f"unsupported marker kind: {args.kind}")
    with _EvidenceMutex(output_dir):
        verify_existing_manifest(output_dir)
        run_info = json.loads(
            (output_dir / "run_info.json").read_text(encoding="utf-8")
        )
        if not isinstance(run_info, dict):
            raise TypeError("run_info.json is not an object")
        if not args.label.strip() or not args.operator.strip():
            raise ValueError("marker label and operator must not be empty")
        configured_actors = {
            str(run_info.get(name, "")).strip().casefold()
            for name in ("operator", "observer", "approver")
        }
        if (
            args.operator.strip().casefold() not in configured_actors
            and args.kind != "note"
        ):
            raise ValueError(
                "marker actor is not one of the configured run roles"
            )
        if (
            run_info.get("test_context") == "live-idle-advisory"
            and args.kind in {"precheck-complete", "close-action"}
            and not str(getattr(args, "evidence_ref", "") or "").strip()
        ):
            raise ValueError(
                f"live {args.kind} requires --evidence-ref"
            )
        _validate_pre_action_marker(
            output_dir,
            run_info,
            args.kind,
            args.operator,
        )
        existing_markers, existing_errors = _marker_rows(output_dir)
        if existing_errors:
            raise RuntimeError("; ".join(existing_errors))
        terminal_kinds = {
            marker.get("kind")
            for marker in existing_markers
            if marker.get("kind") in {"abort", "incomplete"}
        }
        if terminal_kinds and args.kind != "note":
            raise RuntimeError(
                f"run already has terminal marker(s): "
                f"{sorted(terminal_kinds)}"
            )
        singleton_kinds = {
            "precheck-complete",
            "close-action",
            "gui-restarted",
            "idle-confirmed",
            "gui-rearmed",
            "new-session-started",
            "abort",
            "incomplete",
        }
        if (
            args.kind in singleton_kinds
            and any(
                marker.get("kind") == args.kind
                for marker in existing_markers
            )
        ):
            raise RuntimeError(f"duplicate {args.kind} marker")
        if run_info.get("require_recovery_validation", False):
            recovery_kinds = {
                "gui-restarted",
                "idle-confirmed",
                "gui-rearmed",
                "new-session-started",
            }
            if args.kind in recovery_kinds:
                if not (output_dir / "run_complete.json").is_file():
                    raise RuntimeError(
                        "recovery markers require the lifecycle observer to "
                        "complete first"
                    )
                pre_recovery_summary, pre_recovery_code = summarize_run(
                    output_dir
                )
                if pre_recovery_code == EXIT_EVIDENCE_FATAL:
                    raise RuntimeError(
                        "lifecycle evidence is fatal; refusing recovery "
                        "markers"
                    )
                if (
                    pre_recovery_summary.get(
                        "first_main_window_missing"
                    )
                    is None
                    and pre_recovery_summary.get("first_process_exit")
                    is None
                ):
                    raise RuntimeError(
                        "target loss was not observed; refusing recovery "
                        "markers"
                    )
            recovery_actor = {
                "gui-restarted": run_info.get("observer"),
                "idle-confirmed": run_info.get("observer"),
                "gui-rearmed": run_info.get("operator"),
                "new-session-started": run_info.get("operator"),
            }.get(args.kind)
            if (
                recovery_actor is not None
                and args.operator.strip().casefold()
                != str(recovery_actor).strip().casefold()
            ):
                raise ValueError(
                    f"{args.kind} must be written by the configured "
                    + (
                        "observer"
                        if args.kind in {
                            "gui-restarted",
                            "idle-confirmed",
                        }
                        else "operator"
                    )
                )
            required_previous = {
                "idle-confirmed": "gui-restarted",
                "gui-rearmed": "idle-confirmed",
                "new-session-started": "gui-rearmed",
            }
            previous_kind = required_previous.get(args.kind)
            if previous_kind and sum(
                marker.get("kind") == previous_kind
                for marker in existing_markers
            ) != 1:
                raise RuntimeError(
                    f"{args.kind} requires exactly one {previous_kind} marker"
                )
            if args.kind in {
                "idle-confirmed",
                "gui-rearmed",
                "new-session-started",
            } and not str(getattr(args, "evidence_ref", "")).strip():
                raise ValueError(
                    f"{args.kind} requires --evidence-ref"
                )
        evidence_binding: dict[str, Any] | None = None
        raw_evidence_ref = str(
            getattr(args, "evidence_ref", "") or ""
        ).strip()
        if raw_evidence_ref:
            evidence_binding = _bind_evidence_reference(
                raw_evidence_ref
            )
        new_target_identity: dict[str, Any] | None = None
        new_target_sample: dict[str, Any] | None = None
        new_target_binding: dict[str, Any] | None = None
        new_session_binding: dict[str, Any] | None = None
        new_target_identity_verified = False
        recovery_target_reverified = False
        if args.kind == "gui-restarted":
            if args.new_pid is None or args.new_pid <= 0:
                raise ValueError("gui-restarted requires a positive --new-pid")
            original_identity = run_info["target_identity"]
            same_pid = args.new_pid == original_identity["pid"]
            if same_pid and run_info.get("target_role") != "vendor-data-window":
                raise ValueError(
                    "restart must use a new PID; refusing the original target "
                    "PID"
                )
            if run_info.get("require_recovery_validation", False):
                owns_restart_target = restart_target is None
                new_command_token = str(
                    getattr(
                        args,
                        "new_expected_command_token",
                        "growth_monitor",
                    )
                    or ""
                ).strip()
                if not new_command_token:
                    raise ValueError(
                        "--new-expected-command-token must not be empty"
                    )
                if restart_target is None:
                    new_hwnd = getattr(args, "new_hwnd", None)
                    new_title = getattr(args, "new_expected_title", None)
                    if new_hwnd is not None and new_hwnd <= 0:
                        raise ValueError("--new-hwnd must be positive")
                    if (
                        new_title is not None
                        and not str(new_title).strip()
                    ):
                        raise ValueError(
                            "--new-expected-title must not be empty"
                        )
                    if new_hwnd is None and not new_title:
                        raise ValueError(
                            "verified restart requires --new-hwnd or "
                            "--new-expected-title"
                        )
                    restart_target = Win32Target(
                        args.new_pid,
                        hwnd=new_hwnd,
                        expected_title=new_title,
                        expected_image=getattr(
                            args, "new_expected_image", None
                        ),
                        expected_command_token=new_command_token,
                    )
                try:
                    new_target_sample = restart_target.sample()
                    if (
                        new_target_sample.get("process_alive") is not True
                        or new_target_sample.get(
                            "main_window_identity_matches",
                            new_target_sample.get(
                                "main_window_owned_by_target", False
                            ),
                        ) is not True
                    ):
                        raise RuntimeError(
                            "new GUI process/window identity is not live"
                        )
                    if (
                        new_target_sample.get("main_window") or {}
                    ).get("visible") is not True:
                        raise RuntimeError("new GUI main window is not visible")
                    new_target_identity = (
                        restart_target.identity.as_dict()
                    )
                    if new_target_identity.get("pid") != args.new_pid:
                        raise RuntimeError(
                            "verified recovery PID does not match --new-pid"
                        )
                    if same_pid:
                        if (
                            new_target_identity.get("creation_time_utc")
                            != original_identity.get("creation_time_utc")
                            or new_target_identity.get("main_hwnd")
                            == original_identity.get("main_hwnd")
                        ):
                            raise RuntimeError(
                                "same-process data-window recovery requires "
                                "the original process creation time and a "
                                "different top-level HWND"
                            )
                    elif (
                        new_target_identity.get("creation_time_utc")
                        == original_identity.get("creation_time_utc")
                    ):
                        raise RuntimeError(
                            "replacement GUI has the original process "
                            "creation time"
                        )
                    new_target_binding = {
                        "pid": new_target_identity["pid"],
                        "hwnd": new_target_identity["main_hwnd"],
                        "expected_title": new_target_identity["main_title"],
                        "expected_image": new_target_identity["image_path"],
                        "expected_command_token": new_command_token,
                    }
                    new_target_identity_verified = True
                finally:
                    if owns_restart_target:
                        restart_target.close()
        elif (
            run_info.get("require_recovery_validation", False)
            and args.kind
            in {"idle-confirmed", "gui-rearmed", "new-session-started"}
        ):
            restart_rows = [
                marker
                for marker in existing_markers
                if marker.get("kind") == "gui-restarted"
            ]
            if len(restart_rows) != 1:
                raise RuntimeError(
                    f"{args.kind} requires exactly one verified "
                    "gui-restarted marker"
                )
            restart_row = restart_rows[0]
            new_target_identity, new_target_sample = (
                _verify_recovery_target(
                    restart_row.get("new_target_binding"),
                    restart_row.get("new_target_identity"),
                    supplied_target=restart_target,
                )
            )
            new_target_binding = restart_row.get("new_target_binding")
            new_target_identity_verified = True
            recovery_target_reverified = True
        if args.kind == "new-session-started":
            if args.new_session_dir is None:
                raise ValueError(
                    "new-session-started requires --new-session-dir"
                )
            resolved_new_session = (
                args.new_session_dir.expanduser().resolve()
            )
            old_session = run_info.get("session_dir")
            if old_session and (
                resolved_new_session == Path(old_session).resolve()
            ):
                raise ValueError(
                    "restart must START a new session, not reuse the old "
                    "directory"
                )
            if (
                run_info.get("require_recovery_validation", False)
                and not resolved_new_session.is_dir()
            ):
                raise NotADirectoryError(
                    "new session directory does not exist"
                )
            if run_info.get("require_recovery_validation", False):
                sensor_result = inspect_csv(
                    resolved_new_session / "sensor_log.csv"
                )
                if (
                    sensor_result.get("errors")
                    or sensor_result.get("data_row_count", 0) < 1
                ):
                    raise RuntimeError(
                        "new session must contain a parseable sensor_log.csv "
                        "with at least one flushed data row"
                    )
                session_stat = resolved_new_session.stat()
                new_session_binding = {
                    "path": str(resolved_new_session),
                    "st_dev": session_stat.st_dev,
                    "st_ino": session_stat.st_ino,
                    "sensor_log_sha256": sensor_result.get("sha256"),
                    "sensor_log_data_rows_at_marker": sensor_result.get(
                        "data_row_count"
                    ),
                }
        marker = {
            "marker_id": uuid.uuid4().hex,
            "at_utc": utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            "boot_id": current_boot_identity()[0],
            "pid": os.getpid(),
            "kind": args.kind,
            "label": args.label,
            "operator": args.operator,
            "new_pid": args.new_pid,
            "new_session_dir": (
                str(args.new_session_dir.expanduser().resolve())
                if args.new_session_dir is not None else None
            ),
            "new_session_binding": new_session_binding,
            "new_target_identity_verified": (
                new_target_identity_verified
            ),
            "new_target_identity": new_target_identity,
            "new_target_sample": new_target_sample,
            "new_target_binding": new_target_binding,
            "recovery_target_reverified": recovery_target_reverified,
            "evidence_ref": getattr(args, "evidence_ref", None),
            "evidence_binding": evidence_binding,
        }
        marker_dir = output_dir / "markers"
        marker_dir.mkdir(exist_ok=True)
        _write_json_atomic(
            marker_dir
            / f"{marker['at_utc'].replace(':', '').replace('-', '')}_"
            f"{marker['marker_id']}.json",
            marker,
        )
        if (output_dir / "run_complete.json").is_file():
            summary, exit_code = summarize_run(output_dir)
            _write_json_atomic(output_dir / "summary.json", summary)
            write_evidence_manifest(output_dir)
            return exit_code
    return EXIT_PASS


def finalize(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.expanduser().resolve()
    if _ObserverLease(output_dir).is_live():
        raise RuntimeError(
            "observer is still active; finalize only after its process exits"
        )
    with _EvidenceMutex(output_dir):
        verify_existing_manifest(output_dir)
        if getattr(args, "reaudit_session", False):
            reaudit_session(output_dir)
        summary, exit_code = summarize_run(output_dir)
        _write_json_atomic(output_dir / "summary.json", summary)
        write_evidence_manifest(output_dir)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Observe one already-running GUI.")
    run.add_argument("--pid", type=int, required=True)
    run.add_argument("--hwnd", type=lambda value: int(value, 0))
    run.add_argument("--expected-title")
    run.add_argument(
        "--expected-image",
        help="Optional exact executable path; Python script identity must be "
        "recorded separately because python.exe alone is not unique.",
    )
    run.add_argument(
        "--expected-command-token",
        default="growth_monitor",
        help="Exact substring required in the target command line. The "
        "default prevents binding an unrelated python.exe; use a vendor "
        "executable token for vendor targets.",
    )
    run.add_argument(
        "--target-role", choices=TARGET_ROLES, required=True
    )
    run.add_argument("--scenario-id", required=True)
    run.add_argument(
        "--fault-style",
        choices=("graceful", "abrupt", "spontaneous"),
        required=True,
    )
    run.add_argument(
        "--test-context",
        choices=("offline-tabletop", "live-idle-advisory"),
        required=True,
    )
    run.add_argument(
        "--gui-state",
        choices=("idle-disarmed", "advisory-running"),
        help="Required for live tests. Advisory-running requires a pinned "
        "session and at least one advancing session stream; idle-disarmed "
        "forbids session logging.",
    )
    run.add_argument("--operator", required=True)
    run.add_argument("--observer", required=True)
    run.add_argument("--approver", required=True)
    run.add_argument("--authorization-ref", required=True)
    run.add_argument("--sop-ref", required=True)
    run.add_argument("--safe-state-note", required=True)
    run.add_argument("--abort-criteria", required=True)
    run.add_argument("--confirm-safe-state", action="store_true", required=True)
    run.add_argument(
        "--require-action-markers",
        action="store_true",
        help="Require precheck-complete and, except for spontaneous exits, "
        "close-action markers for an evaluable result. Required for live "
        "workstation tests.",
    )
    run.add_argument(
        "--require-recovery-validation",
        action="store_true",
        help="Keep the run incomplete until a verified new GUI identity, "
        "idle/re-ARM sequence, new session (when applicable), and old-session "
        "re-audit are recorded. Required for live workstation tests.",
    )
    run.add_argument("--session-dir", type=Path)
    run.add_argument(
        "--paired-failure-run",
        type=Path,
        help="Required for live vendor-window/application tests; binds this "
        "target to ombe_failure_probe.py's actual worker source HWND.",
    )
    run.add_argument(
        "--require-session-advance",
        action="append",
        default=[],
        metavar="RELATIVE_PATH",
        help="Require a create/append before target loss; repeat as needed.",
    )
    run.add_argument("--sample-interval-s", type=float, default=0.5)
    run.add_argument("--duration-s", type=float, default=300.0)
    run.add_argument("--baseline-duration-s", type=float, default=5.0)
    run.add_argument("--post-loss-observation-s", type=float, default=5.0)
    run.add_argument("--post-loss-settle-s", type=float, default=1.0)
    run.add_argument("--orphan-grace-s", type=float, default=30.0)
    run.add_argument("--output-dir", type=Path, required=True)
    run.set_defaults(func=observe)

    mark = commands.add_parser(
        "mark", help="Write one operator/observer assertion atomically."
    )
    mark.add_argument("--output-dir", type=Path, required=True)
    mark.add_argument("--kind", choices=MARKER_KINDS, required=True)
    mark.add_argument("--label", required=True)
    mark.add_argument("--operator", required=True)
    mark.add_argument("--new-pid", type=int)
    mark.add_argument("--new-session-dir", type=Path)
    mark.add_argument(
        "--evidence-ref",
        help="Screenshot, UIA dump, operator log, or other read-only evidence "
        "supporting live precheck/close and idle/re-ARM/new-session "
        "assertions.",
    )
    mark.add_argument(
        "--new-hwnd", type=lambda value: int(value, 0)
    )
    mark.add_argument("--new-expected-title")
    mark.add_argument("--new-expected-image")
    mark.add_argument(
        "--new-expected-command-token",
        default="growth_monitor",
    )
    mark.set_defaults(func=mark_event)

    finish = commands.add_parser(
        "finalize", help="Rebuild summary/manifest from durable evidence."
    )
    finish.add_argument("--output-dir", type=Path, required=True)
    finish.add_argument(
        "--reaudit-session",
        action="store_true",
        help="Hash the pinned old session again and fail if it changed after "
        "the terminal audit (for example after GUI restart).",
    )
    finish.set_defaults(func=finalize)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.command != "run":
        return
    if args.pid <= 0:
        raise ValueError("--pid must be positive")
    if args.hwnd is None and not str(args.expected_title or "").strip():
        raise ValueError("--hwnd or --expected-title is required")
    if args.hwnd is not None and args.hwnd <= 0:
        raise ValueError("--hwnd must be positive")
    if (
        args.expected_title is not None
        and not args.expected_title.strip()
    ):
        raise ValueError("--expected-title must not be empty")
    if not str(args.expected_command_token or "").strip():
        raise ValueError("--expected-command-token must not be empty")
    if (
        args.expected_image is not None
        and not args.expected_image.strip()
    ):
        raise ValueError("--expected-image must not be empty")
    for name in (
        "sample_interval_s",
        "duration_s",
        "baseline_duration_s",
        "post_loss_observation_s",
        "post_loss_settle_s",
        "orphan_grace_s",
    ):
        value = getattr(args, name)
        if not math.isfinite(value):
            raise ValueError(
                f"--{name.replace('_', '-')} must be finite"
            )
    for name in (
        "sample_interval_s",
        "duration_s",
        "baseline_duration_s",
        "post_loss_observation_s",
        "orphan_grace_s",
    ):
        if getattr(args, name) <= 0:
            raise ValueError(
                f"--{name.replace('_', '-')} must be positive"
            )
    if args.post_loss_settle_s < 0:
        raise ValueError("--post-loss-settle-s must be nonnegative")
    if args.post_loss_settle_s >= args.post_loss_observation_s:
        raise ValueError(
            "post-loss settle must be shorter than post-loss observation"
        )
    minimum_duration_s = (
        args.baseline_duration_s
        + args.post_loss_observation_s
        + max(1.0, 2.0 * args.sample_interval_s)
    )
    if args.duration_s < minimum_duration_s:
        raise ValueError(
            "duration must cover baseline, post-loss observation, and at "
            f"least one action-gate reserve ({minimum_duration_s:.3f} s)"
        )
    if args.require_session_advance and args.session_dir is None:
        raise ValueError(
            "--require-session-advance requires --session-dir"
        )
    for path in args.require_session_advance:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(
                "required session paths must be safe relative paths"
            )
    if not args.confirm_safe_state:
        raise ValueError("--confirm-safe-state is required")
    for name in (
        "scenario_id",
        "operator",
        "observer",
        "approver",
        "authorization_ref",
        "sop_ref",
        "safe_state_note",
        "abort_criteria",
    ):
        if not str(getattr(args, name, "")).strip():
            raise ValueError(f"--{name.replace('_', '-')} must not be empty")
    if args.operator.strip().casefold() == args.observer.strip().casefold():
        raise ValueError("operator and observer must be different people")
    if (
        args.fault_style == "abrupt"
        and args.test_context != "offline-tabletop"
    ):
        raise ValueError(
            "abrupt termination is gated to --test-context "
            "offline-tabletop; this observer never authorizes a live kill"
        )
    if (
        args.fault_style == "abrupt"
        and not getattr(args, "require_action_markers", False)
    ):
        raise ValueError("abrupt tabletop tests require action markers")
    if (
        args.test_context == "live-idle-advisory"
        and not getattr(args, "require_action_markers", False)
    ):
        raise ValueError(
            "live workstation tests require --require-action-markers"
        )
    if (
        args.test_context == "live-idle-advisory"
        and not getattr(args, "require_recovery_validation", False)
    ):
        raise ValueError(
            "live workstation tests require --require-recovery-validation"
        )
    gui_state = getattr(args, "gui_state", None)
    if args.test_context == "live-idle-advisory":
        if gui_state not in {"idle-disarmed", "advisory-running"}:
            raise ValueError(
                "live workstation tests require --gui-state "
                "idle-disarmed or advisory-running"
            )
        if gui_state == "advisory-running":
            if args.session_dir is None or not args.require_session_advance:
                raise ValueError(
                    "advisory-running tests require --session-dir and at "
                    "least one --require-session-advance"
                )
        elif args.session_dir is not None or args.require_session_advance:
            raise ValueError(
                "idle-disarmed tests must omit session logging arguments"
            )
    if (
        args.test_context == "live-idle-advisory"
        and args.target_role in {"vendor-data-window", "vendor-application"}
        and getattr(args, "paired_failure_run", None) is None
    ):
        raise ValueError(
            "live vendor GUI tests require --paired-failure-run"
        )
    if (
        args.test_context == "live-idle-advisory"
        and args.target_role == "vendor-data-window"
        and args.post_loss_observation_s < 30.0
    ):
        raise ValueError(
            "live vendor data-window tests require at least 30 s of "
            "post-loss observation"
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        return int(args.func(args))
    except (
        OSError,
        ValueError,
        RuntimeError,
        AttributeError,
        KeyError,
        OverflowError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_EVIDENCE_FATAL


if __name__ == "__main__":
    raise SystemExit(main())
