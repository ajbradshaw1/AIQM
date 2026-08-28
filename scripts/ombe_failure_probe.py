#!/usr/bin/env python3
"""Non-mutating O-MBE worker failure/recovery recorder.

The probe never controls an instrument, closes a vendor application, disables
an interface, or switches power. It observes software-source failures and,
inside an approved maintenance procedure, manually performed loss/restoration
of a dedicated telemetry link or eligible non-control-critical device. A
separate ``mark`` invocation timestamps each operator action.

For WGC validation, run this recorder from its failure-validation checkout and
point ``--software-root`` at the WGC checkout. The evidence then records both
Git commits and the actual worker/driver/backend used.

``finalize`` reconstructs a summary and SHA manifest from fsynced partial
evidence after a process or workstation interruption.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

RECORDER_ROOT = Path(__file__).resolve().parent.parent
if str(RECORDER_ROOT) not in sys.path:
    sys.path.insert(0, str(RECORDER_ROOT))

VALID_MODES: dict[str, tuple[str, ...]] = {
    "camera": ("screengrab", "screengrab_mss"),
    "pyrometer": ("screengrab", "modbus", "exactus"),
    "mistral": ("screengrab", "jsonrpc"),
    "evap": ("screengrab", "elog"),
}

EXPECTED_DRIVER_CLASSES: dict[tuple[str, str], str] = {
    ("camera", "screengrab"): "ScreenGrabCamera",
    ("camera", "screengrab_mss"): "ScreenGrabCamera",
    ("pyrometer", "screengrab"): "ScreenGrabPyrometer",
    ("pyrometer", "modbus"): "ModbusPyrometer",
    ("pyrometer", "exactus"): "ExactusSerialPyrometer",
    ("mistral", "screengrab"): "MistralGui",
    ("mistral", "jsonrpc"): "MistralJsonRpcClient",
    ("evap", "screengrab"): "EvapControl",
    ("evap", "elog"): "ElogReader",
}

MEASUREMENT_KEYS: dict[str, tuple[str, ...]] = {
    "camera": ("frame_number", "capture_sequence"),
    "pyrometer": (
        "temperature",
        "temperature_std",
        "temperature_n",
        "emissivity",
    ),
    "mistral": ("v_set", "v_actual", "i_set", "i_actual"),
    "evap": (
        "chamber_pressure_mbar",
        "substrate_temp_pv_C",
        "substrate_temp_setpoint_C",
        "cell_HTEC2_pv_C",
        "cell_Y_pv_C",
        "cell_Sr_pv_C",
        "cell_Eu_pv_C",
        "cell_Er_pv_C",
        "plasma_dc_bias_V",
        "plasma_forward_W",
        "plasma_reflected_W",
    ),
}

DRIVER_ATTRS = {
    "camera": "_camera",
    "pyrometer": "_sensor",
    "mistral": "_driver",
    "evap": "_driver",
}

FAULT_CLASSES: dict[str, dict[str, Any]] = {
    "offline-injection": {
        "safety_tier": "S0",
        "physical": False,
        "description": "Synthetic exception, timeout, freeze, or driver exit.",
    },
    "window-source-loss": {
        "safety_tier": "S1",
        "physical": False,
        "description": "Vendor window or display/update source becomes unavailable.",
    },
    "file-feed-stop": {
        "safety_tier": "S1",
        "physical": False,
        "description": "A read-only file/log feed stops advancing.",
    },
    "transport-loss": {
        "safety_tier": "S2",
        "physical": True,
        "description": (
            "Dedicated read-only serial/USB link is interrupted; Ethernet "
            "is disabled until the endpoint and active driver are bound."
        ),
    },
    "instrument-power-loss": {
        "safety_tier": "S3",
        "physical": True,
        "description": "Approved non-control-critical device or adapter loses power.",
    },
    "workstation-power-observation": {
        "safety_tier": "S4",
        "physical": True,
        "description": "Observe a planned workstation outage with independent evidence.",
    },
}

SAFETY_GATED_FAULT_CLASSES = set(FAULT_CLASSES) - {"offline-injection"}

MARKER_KINDS = (
    "action",
    "precheck-complete",
    "failure-start",
    "failure-observed",
    "recovery",
    "vendor-ready",
    "gui-reconnected",
    "rearmed",
    "abort",
    "persistent-failure",
    "incomplete",
)

RECOVERY_ACTION_KINDS = {
    "recovery",
    "vendor-ready",
    "gui-reconnected",
    "rearmed",
}

_RETAINED_LIVENESS_LEASES: list["_RecorderLivenessLease"] = []


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def current_boot_identity() -> tuple[str, str]:
    """Return a boot-stable host identity and its provenance."""
    if os.name == "nt":
        try:
            import winreg

            key_path = (
                r"SYSTEM\CurrentControlSet\Control\Session Manager"
                r"\Memory Management\PrefetchParameters"
            )
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                boot_id, _value_type = winreg.QueryValueEx(key, "BootId")
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
    try:
        import psutil

        boot_epoch_s = round(psutil.boot_time())
        source = "psutil_boot_time"
    except Exception:
        boot_epoch_s = round(time.time() - time.monotonic())
        source = "wall_clock_minus_monotonic_fallback"
    raw = f"{platform.node()}:{boot_epoch_s}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], source


def current_boot_id() -> str:
    return current_boot_identity()[0]


def _serial_mutex_name(port: str) -> str:
    port_key = hashlib.sha256(port.strip().casefold().encode("utf-8")).hexdigest()
    return f"Local\\AIQM_OmbeInterfaceSerial_{port_key[:16]}"


class _SerialPortMutex:
    """Coordinate direct serial diagnostics across validation branches."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, port: str | None):
        self.port = port
        self.name = _serial_mutex_name(port) if port else None
        self._handle: Any = None
        self._kernel32: Any = None
        self._retain_until_exit = False

    def acquire(self) -> None:
        if os.name != "nt" or not self.name:
            return
        import ctypes
        import ctypes.wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
        kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, True, self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise OSError(error, f"CreateMutexW failed for {self.port}")
        if error == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise RuntimeError(
                f"another validation runner owns the diagnostic lock for "
                f"{self.port}; wait for it to finish"
            )
        self._kernel32 = kernel32
        self._handle = handle

    def retain_until_process_exit(self) -> None:
        """Keep the OS-owned handle when an old reader may still be alive."""
        if self._handle is not None:
            self._retain_until_exit = True

    def release(self) -> None:
        if self._retain_until_exit:
            return
        if self._handle is None or self._kernel32 is None:
            return
        self._kernel32.ReleaseMutex(self._handle)
        self._kernel32.CloseHandle(self._handle)
        self._handle = None
        self._kernel32 = None

    def __enter__(self) -> "_SerialPortMutex":
        self.acquire()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()


class _EvidenceMutex:
    """Serialize marker, summary, and manifest updates across processes."""

    def __init__(self, output_dir: Path, timeout_ms: int = 10_000):
        resolved = str(output_dir.expanduser().resolve()).casefold()
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
        self.name = f"Local\\AIQM_OmbeFailureEvidence_{digest}"
        self.timeout_ms = timeout_ms
        self._handle: Any = None
        self._kernel32: Any = None

    def acquire(self) -> None:
        if os.name != "nt":
            return
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
        result = kernel32.WaitForSingleObject(handle, self.timeout_ms)
        if result not in {0x00000000, 0x00000080}:
            kernel32.CloseHandle(handle)
            raise RuntimeError(
                "timed out waiting for another marker/finalizer process"
            )
        self._kernel32 = kernel32
        self._handle = handle

    def release(self) -> None:
        if self._handle is None or self._kernel32 is None:
            return
        self._kernel32.ReleaseMutex(self._handle)
        self._kernel32.CloseHandle(self._handle)
        self._handle = None
        self._kernel32 = None

    def __enter__(self) -> "_EvidenceMutex":
        self.acquire()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


class _RecorderLivenessLease:
    """Kernel-owned proof that the recorder process still owns this run."""

    def __init__(self, output_dir: Path):
        resolved = str(output_dir.expanduser().resolve()).casefold()
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
        self.name = f"Local\\AIQM_OmbeFailureLive_{digest}"
        self.path = output_dir / ".recorder_liveness.lock"
        self._handle: Any = None
        self._kernel32: Any = None
        self._stream: Any = None
        self._retain_until_exit = False

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
                raise OSError(error, "CreateEventW failed for recorder lease")
            if ctypes.get_last_error() == 183:
                kernel32.CloseHandle(handle)
                raise RuntimeError(
                    "another recorder already owns this run directory"
                )
            self._kernel32 = kernel32
            self._handle = handle
            return

        import fcntl

        stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            stream.close()
            raise RuntimeError(
                "another recorder already owns this run directory"
            )
        stream.seek(0)
        stream.truncate()
        stream.write(f"{os.getpid()}\n")
        stream.flush()
        os.fsync(stream.fileno())
        self._stream = stream

    def release(self) -> None:
        if self._retain_until_exit:
            return
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
            kernel32.WaitForSingleObject.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.wintypes.DWORD,
            ]
            kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
            kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
            kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
            handle = kernel32.OpenEventW(0x00100000, False, self.name)
            if not handle:
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == 0
            finally:
                kernel32.CloseHandle(handle)

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

    def retain_until_process_exit(self) -> None:
        """Keep the kernel lease when an evidence writer may still be alive."""
        if not self._retain_until_exit:
            self._retain_until_exit = True
            _RETAINED_LIVENESS_LEASES.append(self)

    def __enter__(self) -> "_RecorderLivenessLease":
        self.acquire()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    # Camera pixels are intentionally not serialized. Avoid dataclasses.asdict,
    # which would deep-copy a full-resolution numpy frame on every emission.
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        return {
            "shape": list(value.shape),
            "dtype": str(value.dtype),
        }
    return str(value)


def state_payload(state: Any) -> dict[str, Any]:
    """Convert a worker state into stable, JSON-safe diagnostic fields."""
    if is_dataclass(state):
        raw = {field.name: getattr(state, field.name) for field in fields(state)}
    else:
        raw = vars(state)
    return {key: _json_value(value) for key, value in raw.items()}


def frame_fingerprint(state: Any) -> dict[str, Any] | None:
    """Hash a small spatial sample without retaining or copying the frame."""
    frame = getattr(state, "frame", None)
    if frame is None or not hasattr(frame, "shape"):
        return None
    try:
        height, width = frame.shape[:2]
        y_step = max(1, int(height) // 64)
        x_step = max(1, int(width) // 64)
        sample = frame[::y_step, ::x_step]
        digest = hashlib.sha256(sample.tobytes()).hexdigest()
        return {
            "sha256": digest,
            "sample_shape": list(sample.shape),
            "source_shape": list(frame.shape),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def measurement_signature(source: str, payload: dict[str, Any]) -> tuple[Any, ...]:
    """Values whose unexpected retention can indicate stale-data behavior."""
    if source == "camera":
        capture_sequence = payload.get("capture_sequence")
        if capture_sequence not in (None, 0):
            return ("capture_sequence", capture_sequence)
        return ("frame_number", payload.get("frame_number"))
    return tuple(payload.get(key) for key in MEASUREMENT_KEYS[source])


def measurement_status(
    source: str,
    payload: dict[str, Any],
) -> tuple[bool, str, bool]:
    """Return ``(valid, reason, all_measurements_none)`` for one emission."""
    values = [payload.get(key) for key in MEASUREMENT_KEYS[source]]
    all_none = all(value is None for value in values)
    if payload.get("connected") is not True:
        return False, "disconnected", all_none
    if bool(payload.get("error")):
        return False, "worker_error", all_none
    if source == "camera" and payload.get("frame") is None:
        return False, "missing_frame", all_none
    if all_none:
        return False, "all_measurements_none", True
    return True, "valid", all_none


def data_age(
    payload: dict[str, Any],
    observed_at_utc: str,
    observed_monotonic_ns: int,
) -> tuple[float | None, str]:
    """Calculate data age from provenance fields when a State exposes them."""
    observed_dt = _parse_utc(observed_at_utc)
    for field_name in ("source_at_utc", "captured_at_utc", "received_at_utc"):
        source_dt = _parse_utc(payload.get(field_name))
        if source_dt is not None and observed_dt is not None:
            return (
                max(0.0, (observed_dt - source_dt).total_seconds() * 1000.0),
                field_name,
            )

    for field_name in ("received_monotonic_ns", "captured_monotonic_ns"):
        value = payload.get(field_name)
        if isinstance(value, (int, float)) and value > 0:
            return (
                max(0.0, (observed_monotonic_ns - int(value)) / 1_000_000.0),
                field_name,
            )

    frame_age = payload.get("frame_age_ms")
    if isinstance(frame_age, (int, float)) and math.isfinite(float(frame_age)):
        return max(0.0, float(frame_age)), "frame_age_ms"
    return None, "unavailable"


@dataclass
class ObservationTracker:
    previous_signature: tuple[Any, ...] | None = None
    last_changed_monotonic_s: float | None = None
    previous_frame_sha256: str | None = None
    last_frame_changed_monotonic_s: float | None = None


def annotate_observation(
    row: dict[str, Any],
    source: str,
    tracker: ObservationTracker,
) -> dict[str, Any]:
    """Attach change, validity, and freshness diagnostics to one row."""
    now_s = float(row["observed_monotonic_s"])
    payload = row["state"]
    signature = measurement_signature(source, payload)
    first = tracker.previous_signature is None
    changed = first or signature != tracker.previous_signature
    if changed:
        tracker.previous_signature = signature
        tracker.last_changed_monotonic_s = now_s
    unchanged_for_s = (
        0.0
        if tracker.last_changed_monotonic_s is None
        else max(0.0, now_s - tracker.last_changed_monotonic_s)
    )
    valid, reason, all_none = measurement_status(source, payload)
    age_ms, age_source = data_age(
        payload,
        str(row.get("observed_at_utc", "")),
        int(row.get("observed_monotonic_ns", round(now_s * 1_000_000_000))),
    )
    row.update({
        "value_changed": None if first else changed,
        "unchanged_for_s": unchanged_for_s,
        "measurement_valid": valid,
        "validity_reason": reason,
        "all_measurements_none": all_none,
        "data_age_ms": age_ms,
        "data_age_source": age_source,
    })
    fingerprint = row.get("frame_fingerprint")
    frame_sha = (
        fingerprint.get("sha256")
        if isinstance(fingerprint, dict)
        else None
    )
    if frame_sha:
        frame_first = tracker.previous_frame_sha256 is None
        frame_changed = frame_first or frame_sha != tracker.previous_frame_sha256
        if frame_changed:
            tracker.previous_frame_sha256 = frame_sha
            tracker.last_frame_changed_monotonic_s = now_s
        row["frame_content_changed"] = (
            None if frame_first else frame_changed
        )
        row["frame_content_unchanged_for_s"] = (
            0.0
            if tracker.last_frame_changed_monotonic_s is None
            else max(0.0, now_s - tracker.last_frame_changed_monotonic_s)
        )
    else:
        row["frame_content_changed"] = None
        row["frame_content_unchanged_for_s"] = None
    return row


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[round((len(ordered) - 1) * fraction)])


def _ensure_annotations(
    rows: list[dict[str, Any]],
    source: str,
) -> None:
    tracker = ObservationTracker()
    first_time = (
        float(rows[0]["observed_monotonic_s"]) if rows else 0.0
    )
    for row in rows:
        row.setdefault(
            "elapsed_s",
            float(row["observed_monotonic_s"]) - first_time,
        )
        if "measurement_valid" not in row:
            annotate_observation(row, source, tracker)


def _baseline_intervals(
    rows: list[dict[str, Any]],
) -> list[float]:
    intervals: list[float] = []
    for previous, current in zip(rows, rows[1:]):
        previous_boot = previous.get("boot_id")
        current_boot = current.get("boot_id")
        same_boot = (
            not previous_boot
            or not current_boot
            or previous_boot == current_boot
        )
        interval = (
            float(current["observed_monotonic_s"])
            - float(previous["observed_monotonic_s"])
        )
        pair_is_baseline = (
            same_boot
            and interval > 0
            and previous.get("measurement_valid") is True
            and current.get("measurement_valid") is True
            and not bool(previous["state"].get("error"))
            and not bool(current["state"].get("error"))
        )
        if pair_is_baseline:
            intervals.append(interval)
    return intervals


def _baseline_window(
    rows: list[dict[str, Any]],
    watchdog_rows: list[dict[str, Any]],
    markers: list[dict[str, Any]],
    duration_s: float,
) -> tuple[float, float, bool]:
    """Return the trailing pre-fault window, never an old startup window."""
    failure_starts = [
        float(marker["marked_monotonic_s"])
        for marker in markers
        if marker.get("kind") == "failure-start"
        and marker.get("marked_monotonic_s") is not None
    ]
    if failure_starts:
        end_s = min(failure_starts)
        return end_s - duration_s, end_s, True
    observed = [
        float(record["observed_monotonic_s"])
        for record in [*rows, *watchdog_rows]
        if record.get("observed_monotonic_s") is not None
    ]
    end_s = max(observed, default=0.0)
    return end_s - duration_s, end_s, False


def _baseline_span_s(rows: list[dict[str, Any]]) -> float:
    if len(rows) < 2:
        return 0.0
    boot_ids = {str(row.get("boot_id")) for row in rows if row.get("boot_id")}
    if len(boot_ids) > 1:
        return 0.0
    values = [float(row["observed_monotonic_s"]) for row in rows]
    return max(0.0, max(values) - min(values))


def _coverage_requirement_s(
    duration_s: float,
    cadence_s: float | None,
) -> float:
    tolerance = max(0.25, 2.0 * cadence_s) if cadence_s is not None else 0.25
    return max(0.0, duration_s - tolerance)


def _channel_baseline_health(
    baseline_rows: list[dict[str, Any]],
    baseline_watchdog: list[dict[str, Any]],
    *,
    state_healthy: bool,
    duration_s: float,
    cadence_s: float | None,
    stale_threshold_s: float,
) -> dict[str, dict[str, Any]]:
    requirement = _coverage_requirement_s(duration_s, cadence_s)

    def assess(
        records: list[dict[str, Any]],
        healthy: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any]:
        span_s = _baseline_span_s(records)
        return {
            "healthy": (
                len(records) >= 3
                and span_s >= requirement
                and all(healthy(record) for record in records)
            ),
            "samples": len(records),
            "span_s": span_s,
            "required_span_s": requirement,
        }

    emission = assess(
        baseline_watchdog,
        lambda row: (
            isinstance(row.get("last_emission_age_s"), (int, float))
            and float(row["last_emission_age_s"]) <= stale_threshold_s
        ),
    )
    serial_rows = [
        row for row in baseline_watchdog if row.get("serial_link") is not None
    ]
    serial = assess(
        serial_rows,
        lambda row: (row.get("serial_link") or {}).get("present") is True,
    )
    network_rows = [
        row
        for row in baseline_watchdog
        if row.get("network_interface") is not None
        and row.get("network_probe_new", True) is True
    ]
    network = assess(
        network_rows,
        lambda row: (
            (row.get("network_interface") or {}).get("present") is True
            and (row.get("network_interface") or {}).get("is_up") is True
        ),
    )
    source_rows = [
        row
        for row in baseline_watchdog
        if _elog_source_token(row) is not None
    ]
    source_tokens = [_elog_source_token(row) for row in source_rows]
    source = assess(
        source_rows,
        lambda row: (
            row.get("source_unchanged_for_s") is None
            or (
                isinstance(row.get("source_unchanged_for_s"), (int, float))
                and float(row["source_unchanged_for_s"]) <= stale_threshold_s
            )
        )
        and isinstance(_elog_source_age_s(row), (int, float))
        and 0.0 <= float(_elog_source_age_s(row)) <= stale_threshold_s,
    )
    source["advancing_source_timestamps"] = sum(
        current is not None
        and previous is not None
        and current > previous
        for previous, current in zip(source_tokens, source_tokens[1:])
    )
    source["healthy"] = bool(
        source["healthy"] and source["advancing_source_timestamps"] >= 2
    )
    return {
        "invalid_state": {
            "healthy": state_healthy,
            "samples": len(baseline_rows),
            "span_s": _baseline_span_s(baseline_rows),
            "required_span_s": requirement,
        },
        "emission_timeout": emission,
        "serial_missing": serial,
        "network_interface_down": network,
        "source_feed_stale": source,
    }


def _timeline_value(
    record: dict[str, Any],
    utc_field: str,
    monotonic_field: str,
    *,
    prefer_utc: bool,
) -> float:
    if prefer_utc:
        parsed = _parse_utc(record.get(utc_field))
        if parsed is not None:
            return parsed.timestamp()
    return float(record.get(monotonic_field, 0.0))


def _first_direct_read_streak(
    rows: list[dict[str, Any]],
    required: int,
    *,
    source: str,
    max_age_s: float,
) -> dict[str, Any] | None:
    """Find consecutive successful read cycles, not receipt-only updates."""
    streak = 0
    for row in rows:
        state = row.get("state", {})
        successful = (
            row.get("measurement_valid") is True
            and not bool(state.get("error"))
            and (
                row.get("data_age_ms") is None
                or float(row["data_age_ms"]) <= max_age_s * 1000.0
            )
        )
        if source == "pyrometer":
            successful = successful and int(state.get("temperature_n") or 0) > 0
        if successful:
            streak += 1
            if streak >= required:
                return row
        else:
            streak = 0
    return None


def _explicit_source_token(row: dict[str, Any]) -> tuple[str, Any] | None:
    """Return only source-origin provenance, never receipt/WGC counters."""
    state = row.get("state", {})
    exposure_sequence = state.get("exposure_sequence")
    if isinstance(exposure_sequence, (int, float)) and exposure_sequence > 0:
        return ("exposure_sequence", exposure_sequence)
    source_at = _parse_utc(state.get("source_at_utc"))
    if source_at is not None:
        return ("source_at_utc", source_at.timestamp())
    return None


def _first_advancing_source_streak(
    rows: list[dict[str, Any]],
    required: int,
    *,
    previous_token: tuple[str, Any] | None,
    max_age_s: float,
) -> dict[str, Any] | None:
    streak = 0
    token = previous_token
    for row in rows:
        current = _explicit_source_token(row)
        age_is_valid = (
            row.get("data_age_ms") is None
            or float(row["data_age_ms"]) <= max_age_s * 1000.0
        )
        if (
            row.get("measurement_valid") is not True
            or current is None
            or not age_is_valid
        ):
            streak = 0
            continue
        if token is None or current[0] != token[0] or current[1] <= token[1]:
            streak = 0
            token = current
            continue
        token = current
        streak += 1
        if streak >= required:
            return row
    return None


def _elog_source_token(row: dict[str, Any]) -> float | None:
    source_probe = row.get("source_probe") or {}
    parsed = _parse_utc(source_probe.get("elog_source_at_utc"))
    return parsed.timestamp() if parsed is not None else None


def _elog_source_age_s(row: dict[str, Any]) -> float | None:
    source = _elog_source_token(row)
    observed = _parse_utc(row.get("observed_at_utc"))
    if source is None or observed is None:
        return None
    return observed.timestamp() - source


def _first_elog_source_streak(
    rows: list[dict[str, Any]],
    required: int,
    *,
    previous_token: float | None,
    max_age_s: float,
) -> dict[str, Any] | None:
    """Require Elog record timestamps to advance; mtime alone is insufficient."""
    streak = 0
    token = previous_token
    for row in rows:
        current = _elog_source_token(row)
        age_s = _elog_source_age_s(row)
        if current is None or age_s is None or not (0.0 <= age_s <= max_age_s):
            streak = 0
            continue
        if token is None or current <= token:
            if token is None or current < token:
                streak = 0
            token = current
            continue
        token = current
        streak += 1
        if streak >= required:
            return row
    return None


def _recovery_confirmation(
    *,
    source: str,
    mode: str | None,
    fault_class: str | None,
    before_rows: list[dict[str, Any]],
    after_rows: list[dict[str, Any]],
    before_watchdog: list[dict[str, Any]],
    after_watchdog: list[dict[str, Any]],
    required: int,
    max_age_s: float,
) -> tuple[dict[str, Any] | None, str, str]:
    if fault_class == "window-source-loss":
        return (
            _first_direct_read_streak(
                after_rows,
                required,
                source=source,
                max_age_s=max_age_s,
            ),
            "consecutive_valid_display_reads",
            "Confirms the software display/capture path only.",
        )
    direct_modes = {
        ("pyrometer", "exactus"),
        ("pyrometer", "modbus"),
        ("mistral", "jsonrpc"),
    }
    if (source, mode) in direct_modes:
        return (
            _first_direct_read_streak(
                after_rows,
                required,
                source=source,
                max_age_s=max_age_s,
            ),
            "consecutive_successful_direct_reads",
            "Confirms the configured read path, not the instrument safe state.",
        )
    if source == "evap" and mode == "elog":
        previous = next(
            (
                token
                for token in (
                    _elog_source_token(row)
                    for row in reversed(before_watchdog)
                )
                if token is not None
            ),
            None,
        )
        if previous is None:
            return (
                None,
                "unavailable_no_pre_restore_elog_timestamp",
                "Elog recovery requires a pre-restore source timestamp.",
            )
        if fault_class != "file-feed-stop":
            return (
                None,
                "elog_timestamp_not_hardware_provenance",
                "New Elog records do not prove controller or instrument recovery.",
            )
        return (
            _first_elog_source_streak(
                after_watchdog,
                required,
                previous_token=previous,
                max_age_s=max_age_s,
            ),
            "advancing_elog_source_timestamps",
            "Confirms new Elog records, not the controller/instrument link.",
        )
    if source == "camera":
        previous = next(
            (
                token
                for token in (
                    _explicit_source_token(row)
                    for row in reversed(before_rows)
                )
                if token is not None and token[0] == "exposure_sequence"
            ),
            None,
        )
        if previous is None:
            return (
                None,
                "unavailable_no_exposure_provenance",
                "WGC sequence and pixel changes do not prove a new exposure.",
            )
        confirmed = _first_advancing_source_streak(
            after_rows,
            required,
            previous_token=previous,
            max_age_s=max_age_s,
        )
        return (
            confirmed,
            "advancing_exposure_sequence",
            "WGC sequence and pixel changes do not prove a new exposure.",
        )
    return (
        None,
        "unavailable_for_ocr_or_display_source",
        "OCR/display updates cannot independently confirm hardware recovery.",
    )


def _failure_episodes(
    rows: list[dict[str, Any]],
    markers: list[dict[str, Any]],
    stale_threshold_s: float,
    *,
    source: str,
    mode: str | None,
    watchdog_rows: list[dict[str, Any]] | None = None,
    baseline_healthy: bool,
    channel_baseline_health: dict[str, dict[str, Any]],
    required_fresh_samples: int,
) -> tuple[list[dict[str, Any]], list[int]]:
    watchdog_rows = watchdog_rows or []
    ordered_markers = sorted(
        markers,
        key=lambda marker: (
            _parse_utc(marker.get("marked_at_utc")).timestamp()
            if _parse_utc(marker.get("marked_at_utc")) is not None
            else float(marker.get("marked_monotonic_s", 0.0))
        ),
    )
    starts = [
        marker for marker in ordered_markers
        if marker.get("kind") == "failure-start"
    ]
    terminal_markers = [
        marker
        for marker in ordered_markers
        if marker.get("kind") in {
            "recovery",
            "abort",
            "persistent-failure",
            "incomplete",
        }
    ]

    episodes: list[dict[str, Any]] = []
    stale_indices: list[int] = []
    used_terminals: set[int] = set()
    for episode_number, start in enumerate(starts, start=1):
        episode_id = str(
            start.get("episode_id") or f"legacy-{episode_number}"
        )
        relevant_markers = [
            marker
            for marker in markers
            if marker.get("episode_id") == start.get("episode_id")
        ]
        boot_ids = {
            str(value)
            for value in [
                start.get("boot_id"),
                *(marker.get("boot_id") for marker in relevant_markers),
                *(row.get("boot_id") for row in rows),
                *(row.get("boot_id") for row in watchdog_rows),
            ]
            if value
        }
        boot_id_sources = {
            str(value)
            for value in [
                *(marker.get("boot_id_source") for marker in relevant_markers),
                *(row.get("boot_id_source") for row in rows),
                *(row.get("boot_id_source") for row in watchdog_rows),
            ]
            if value
        }
        cross_boot = len(boot_ids) > 1
        boot_identity_reliable = (
            not cross_boot
            or (
                bool(boot_id_sources)
                and all("fallback" not in source for source in boot_id_sources)
            )
        )
        utc_complete = (
            _parse_utc(start.get("marked_at_utc")) is not None
            and all(
                _parse_utc(row.get("observed_at_utc")) is not None
                for row in [*rows, *watchdog_rows]
            )
            and all(
                _parse_utc(marker.get("marked_at_utc")) is not None
                for marker in relevant_markers
            )
        )
        utc_monotonic = True
        if utc_complete:
            utc_monotonic = all(
                all(
                    current >= previous
                    for previous, current in zip(values, values[1:])
                )
                for values in (
                    [
                        _parse_utc(row.get("observed_at_utc")).timestamp()
                        for row in rows
                    ],
                    [
                        _parse_utc(row.get("observed_at_utc")).timestamp()
                        for row in watchdog_rows
                    ],
                    [
                        _parse_utc(marker.get("marked_at_utc")).timestamp()
                        for marker in relevant_markers
                    ],
                )
            )
        prefer_utc = cross_boot and utc_complete
        timeline_valid = (
            not cross_boot
            or (
                utc_complete
                and utc_monotonic
                and boot_identity_reliable
            )
        )
        start_s = _timeline_value(
            start,
            "marked_at_utc",
            "marked_monotonic_s",
            prefer_utc=prefer_utc,
        )
        terminal = None
        for terminal_index, marker in enumerate(terminal_markers):
            if terminal_index in used_terminals:
                continue
            marker_episode = marker.get("episode_id")
            if start.get("episode_id") and marker_episode != start.get("episode_id"):
                continue
            marker_s = _timeline_value(
                marker,
                "marked_at_utc",
                "marked_monotonic_s",
                prefer_utc=prefer_utc,
            )
            if marker_s > start_s:
                terminal = marker
                used_terminals.add(terminal_index)
                break
        recovery = (
            terminal
            if terminal is not None and terminal.get("kind") == "recovery"
            else None
        )
        end_s = (
            _timeline_value(
                terminal,
                "marked_at_utc",
                "marked_monotonic_s",
                prefer_utc=prefer_utc,
            )
            if terminal is not None
            else math.inf
        )
        start_boot_id = str(start.get("boot_id") or "")
        recovery_boot_id = str(
            recovery.get("boot_id") or ""
        ) if recovery is not None else ""
        cross_boot_order_valid = True
        if cross_boot and recovery is not None:
            cross_boot_order_valid = bool(
                start_boot_id
                and recovery_boot_id
                and start_boot_id != recovery_boot_id
            )
            if cross_boot_order_valid:
                for record, utc_field in (
                    *((row, "observed_at_utc") for row in rows),
                    *((row, "observed_at_utc") for row in watchdog_rows),
                    *((marker, "marked_at_utc") for marker in relevant_markers),
                ):
                    record_boot_id = str(record.get("boot_id") or "")
                    record_at = _parse_utc(record.get(utc_field))
                    if (
                        record_at is None
                        or record_boot_id
                        not in {start_boot_id, recovery_boot_id}
                    ):
                        cross_boot_order_valid = False
                        break
                    record_s = record_at.timestamp()
                    if (
                        record_boot_id == start_boot_id
                        and record_s > end_s
                    ) or (
                        record_boot_id == recovery_boot_id
                        and record_s < end_s
                    ):
                        cross_boot_order_valid = False
                        break
        timeline_valid = timeline_valid and cross_boot_order_valid

        def belongs_to_boot(
            record: dict[str, Any],
            expected_boot_id: str,
        ) -> bool:
            return (
                not cross_boot
                or (
                    bool(expected_boot_id)
                    and str(record.get("boot_id") or "")
                    == expected_boot_id
                )
            )

        episode_rows = [
            row for row in rows
            if belongs_to_boot(row, start_boot_id)
            and start_s <= _timeline_value(
                row,
                "observed_at_utc",
                "observed_monotonic_s",
                prefer_utc=prefer_utc,
            ) < end_s
        ]
        post_recovery_rows = [
            row for row in rows
            if recovery is not None
            and belongs_to_boot(row, recovery_boot_id)
            and _timeline_value(
                row,
                "observed_at_utc",
                "observed_monotonic_s",
                prefer_utc=prefer_utc,
            ) >= end_s
        ]
        episode_watchdog = [
            row for row in watchdog_rows
            if belongs_to_boot(row, start_boot_id)
            and start_s <= _timeline_value(
                row,
                "observed_at_utc",
                "observed_monotonic_s",
                prefer_utc=prefer_utc,
            ) < end_s
        ]
        post_recovery_watchdog = [
            row for row in watchdog_rows
            if recovery is not None
            and belongs_to_boot(row, recovery_boot_id)
            and _timeline_value(
                row,
                "observed_at_utc",
                "observed_monotonic_s",
                prefer_utc=prefer_utc,
            ) >= end_s
        ]
        before_restore_rows = [
            row
            for row in rows
            if recovery is not None
            and belongs_to_boot(row, start_boot_id)
            and _timeline_value(
                row,
                "observed_at_utc",
                "observed_monotonic_s",
                prefer_utc=prefer_utc,
            ) < end_s
        ]
        before_restore_watchdog = [
            row
            for row in watchdog_rows
            if recovery is not None
            and belongs_to_boot(row, start_boot_id)
            and _timeline_value(
                row,
                "observed_at_utc",
                "observed_monotonic_s",
                prefer_utc=prefer_utc,
            ) < end_s
        ]
        pre_failure_watchdog = [
            row
            for row in watchdog_rows
            if belongs_to_boot(row, start_boot_id)
            and _timeline_value(
                row,
                "observed_at_utc",
                "observed_monotonic_s",
                prefer_utc=prefer_utc,
            ) < start_s
        ]
        first_invalid = next(
            (
                row for row in episode_rows
                if row.get("measurement_valid") is False
            ),
            None,
        )
        first_worker_error = next(
            (
                row for row in episode_rows
                if bool(row.get("state", {}).get("error"))
            ),
            None,
        )
        first_disconnected = next(
            (
                row for row in episode_rows
                if row.get("state", {}).get("connected") is False
            ),
            None,
        )
        first_all_none = next(
            (
                row for row in episode_rows
                if row.get("all_measurements_none") is True
            ),
            None,
        )
        first_emission_timeout = next(
            (
                row for row in episode_watchdog
                if isinstance(row.get("last_emission_age_s"), (int, float))
                and float(row["last_emission_age_s"]) > stale_threshold_s
            ),
            None,
        )
        first_network_loss = next(
            (
                row for row in episode_watchdog
                if (
                    (row.get("network_interface") or {}).get("present") is False
                    or (row.get("network_interface") or {}).get("is_up") is False
                )
            ),
            None,
        )
        first_serial_loss = next(
            (
                row for row in episode_watchdog
                if (row.get("serial_link") or {}).get("present") is False
            ),
            None,
        )
        first_source_stale = next(
            (
                row for row in episode_watchdog
                if isinstance(row.get("source_unchanged_for_s"), (int, float))
                and float(row["source_unchanged_for_s"]) > stale_threshold_s
            ),
            None,
        )
        stale_rows = [
            row for row in episode_rows
            if row.get("measurement_valid") is True
            and (
                _timeline_value(
                    row,
                    "observed_at_utc",
                    "observed_monotonic_s",
                    prefer_utc=prefer_utc,
                ) - start_s
                > stale_threshold_s
            )
            and float(row.get("unchanged_for_s", 0.0)) > stale_threshold_s
        ]
        valid_after_deadline_rows = [
            row
            for row in episode_rows
            if row.get("measurement_valid") is True
            and (
                _timeline_value(
                    row,
                    "observed_at_utc",
                    "observed_monotonic_s",
                    prefer_utc=prefer_utc,
                ) - start_s
                > stale_threshold_s + 1e-9
            )
        ]
        visual_freeze_rows = [
            row for row in episode_rows
            if source == "camera"
            and row.get("measurement_valid") is True
            and isinstance(row.get("frame_content_unchanged_for_s"), (int, float))
            and float(row["frame_content_unchanged_for_s"]) > stale_threshold_s
        ]
        stale_indices.extend(
            int(row["emission_index"]) for row in stale_rows
        )
        state_baseline = channel_baseline_health.get("invalid_state") or {}
        state_baseline_samples = int(state_baseline.get("samples") or 0)
        state_baseline_span_s = float(state_baseline.get("span_s") or 0.0)
        relevant_sample_s = (
            state_baseline_span_s / (state_baseline_samples - 1)
            if state_baseline_samples >= 2
            else 0.0
        )
        fault_window_duration_s = (
            end_s - start_s
            if terminal is not None and math.isfinite(end_s)
            else None
        )
        fault_window_required_s = stale_threshold_s + relevant_sample_s
        fault_window_sufficient = (
            terminal is None
            or terminal.get("kind") != "recovery"
            or (
                fault_window_duration_s is not None
                and fault_window_duration_s + 1e-9
                >= fault_window_required_s
            )
        )
        production_detection_candidates: list[
            tuple[float, str, dict[str, Any]]
        ] = []
        first_invalid_s = (
            _timeline_value(
                first_invalid,
                "observed_at_utc",
                "observed_monotonic_s",
                prefer_utc=prefer_utc,
            )
            if first_invalid is not None
            else None
        )
        first_invalid_latency_s = (
            first_invalid_s - start_s
            if first_invalid_s is not None
            else None
        )
        if (
            first_invalid is not None
            and channel_baseline_health.get(
                "invalid_state", {}
            ).get("healthy") is True
            and timeline_valid
            and fault_window_sufficient
            and first_invalid_latency_s is not None
            and first_invalid_latency_s <= stale_threshold_s + 1e-9
        ):
            production_detection_candidates.append((
                first_invalid_s,
                "invalid_state",
                first_invalid,
            ))
        first_detection = min(
            production_detection_candidates,
            default=None,
        )
        late_production_detection = (
            first_invalid_latency_s is not None
            and first_invalid_latency_s > stale_threshold_s + 1e-9
            and timeline_valid
            and baseline_healthy
            and fault_window_sufficient
        )
        fault_evidence_candidates: list[
            tuple[float, str, dict[str, Any]]
        ] = []
        for channel, candidate, utc_field, monotonic_field in (
            (
                "emission_timeout",
                first_emission_timeout,
                "observed_at_utc",
                "observed_monotonic_s",
            ),
            (
                "network_interface_down",
                first_network_loss,
                "observed_at_utc",
                "observed_monotonic_s",
            ),
            (
                "serial_missing",
                first_serial_loss,
                "observed_at_utc",
                "observed_monotonic_s",
            ),
            (
                "source_feed_stale",
                first_source_stale,
                "observed_at_utc",
                "observed_monotonic_s",
            ),
        ):
            channel_healthy = bool(
                channel_baseline_health.get(channel, {}).get("healthy")
            )
            if candidate is not None and channel_healthy and timeline_valid:
                fault_evidence_candidates.append(
                    (
                        _timeline_value(
                            candidate,
                            utc_field,
                            monotonic_field,
                            prefer_utc=prefer_utc,
                        ),
                        channel,
                        candidate,
                    )
                )
        first_fault_evidence = min(
            fault_evidence_candidates,
            default=None,
        )
        first_post_emission = (
            post_recovery_rows[0] if post_recovery_rows else None
        )
        first_post_valid = next(
            (
                row for row in post_recovery_rows
                if row.get("measurement_valid") is True
            ),
            None,
        )
        first_post_change = next(
            (
                row for row in post_recovery_rows
                if row.get("value_changed") is True
            ),
            None,
        )
        first_post_frame_change = next(
            (
                row for row in post_recovery_rows
                if row.get("frame_content_changed") is True
            ),
            None,
        )
        first_link_recovery = next(
            (
                row for row in post_recovery_watchdog
                if (
                    (
                        (row.get("network_interface") or {}).get("present") is True
                        and (row.get("network_interface") or {}).get("is_up") is True
                    )
                    or (row.get("serial_link") or {}).get("present") is True
                )
            ),
            None,
        )
        confirmed_fresh, recovery_evidence, recovery_limitation = (
            _recovery_confirmation(
                source=source,
                mode=mode,
                fault_class=start.get("fault_class"),
                before_rows=before_restore_rows,
                after_rows=post_recovery_rows,
                before_watchdog=before_restore_watchdog,
                after_watchdog=post_recovery_watchdog,
                required=required_fresh_samples,
                max_age_s=stale_threshold_s,
            )
        )

        def interface_identity(
            row: dict[str, Any] | None,
        ) -> dict[str, Any] | None:
            if row is None:
                return None
            identity = row.get("driver_identity") or {}
            result = {
                key: identity.get(key)
                for key in (
                    "driver_class",
                    "port",
                    "baudrate",
                    "device_id",
                )
                if identity.get(key) is not None
            }
            link_kind = start.get("link_kind")
            if link_kind in {"serial", "usb"}:
                link = row.get("serial_link") or {}
                result.update({
                    key: link.get(key)
                    for key in (
                        "port",
                        "hwid",
                        "vid",
                        "pid",
                        "serial_number",
                    )
                    if link.get(key) is not None
                })
            elif link_kind in {"ethernet", "network"}:
                link = row.get("network_interface") or {}
                result.update({
                    key: link.get(key)
                    for key in (
                        "interface_index",
                        "interface",
                    )
                    if link.get(key) is not None
                })
            return result or None

        pre_interface_identity = interface_identity(
            pre_failure_watchdog[-1] if pre_failure_watchdog else None
        )
        post_interface_identity = interface_identity(first_link_recovery)
        identity_required = (
            start.get("fault_class")
            in {"transport-loss", "instrument-power-loss"}
            and start.get("link_kind")
            in {"serial", "usb", "ethernet", "network"}
        )
        identity_consistent = (
            None
            if not identity_required
            else (
                pre_interface_identity is not None
                and post_interface_identity == pre_interface_identity
            )
        )
        power_event_assertion_required = (
            start.get("fault_class") == "instrument-power-loss"
        )
        power_event_assertion_valid = (
            not power_event_assertion_required
            or (
                start.get("action_side") == "independent-observer"
                and bool(start.get("independent_observer_ref"))
            )
        )
        recovery_is_confirmed = (
            recovery is not None
            and confirmed_fresh is not None
            and timeline_valid
            and fault_window_sufficient
            and identity_consistent is not False
            and power_event_assertion_valid
        )

        def latency_from_start(candidate: dict[str, Any] | None) -> float | None:
            if candidate is None:
                return None
            return _timeline_value(
                candidate,
                "observed_at_utc",
                "observed_monotonic_s",
                prefer_utc=prefer_utc,
            ) - start_s

        def latency_from_recovery(
            candidate: dict[str, Any] | None,
        ) -> float | None:
            if candidate is None or recovery is None:
                return None
            return _timeline_value(
                candidate,
                "observed_at_utc",
                "observed_monotonic_s",
                prefer_utc=prefer_utc,
            ) - end_s

        related_actions = [
            marker.get("kind")
            for marker in ordered_markers
            if marker.get("episode_id") == start.get("episode_id")
            and marker.get("kind") in RECOVERY_ACTION_KINDS
        ]
        related_action_evidence = [
            {
                "kind": marker.get("kind"),
                "marked_at_utc": marker.get("marked_at_utc"),
                "action_side": marker.get("action_side"),
            }
            for marker in ordered_markers
            if marker.get("episode_id") == start.get("episode_id")
            and marker.get("kind") in RECOVERY_ACTION_KINDS
        ]
        episodes.append({
            "episode_id": episode_id,
            "label": start.get("label", ""),
            "note": start.get("note", ""),
            "fault_class": start.get("fault_class"),
            "safety_tier": start.get("safety_tier"),
            "timeline": "utc" if prefer_utc else "monotonic",
            "clock_or_boot_discontinuity": cross_boot,
            "boot_identity_sources": sorted(boot_id_sources),
            "boot_identity_reliable": boot_identity_reliable,
            "cross_boot_order_valid": cross_boot_order_valid,
            "timeline_valid": timeline_valid,
            "failure_started_at_utc": start.get("marked_at_utc"),
            "terminal_kind": terminal.get("kind") if terminal else None,
            "terminal_at_utc": (
                terminal.get("marked_at_utc") if terminal else None
            ),
            "recovery_at_utc": (
                recovery.get("marked_at_utc") if recovery else None
            ),
            "observed_emissions": len(episode_rows),
            "observed_watchdog_samples": len(episode_watchdog),
            "baseline_healthy": baseline_healthy,
            "channel_baseline_health": channel_baseline_health,
            "fault_window_duration_s": fault_window_duration_s,
            "fault_window_required_s": fault_window_required_s,
            "fault_window_sufficient": fault_window_sufficient,
            "assessment": (
                "not_evaluable_clock_or_boot_timeline"
                if not timeline_valid
                else "not_evaluable_no_healthy_baseline"
                if not baseline_healthy
                else "not_evaluable_missing_power_event_assertion"
                if not power_event_assertion_valid
                else "not_evaluable_short_fault_window"
                if not fault_window_sufficient
                else "detected"
                if first_detection is not None
                else "late_detection"
                if late_production_detection
                else "not_detected"
            ),
            "detection_semantics": (
                "Only target State invalid/error/disconnected/freshness "
                "guard outcomes count as production detection. Watchdog "
                "link/feed loss is external fault evidence only."
            ),
            "first_detection_channel": (
                first_detection[1] if first_detection else None
            ),
            "first_detection_latency_s": (
                first_detection[0] - start_s if first_detection else None
            ),
            "production_detection_deadline_s": stale_threshold_s,
            "late_production_detection": late_production_detection,
            "first_external_fault_evidence_channel": (
                first_fault_evidence[1] if first_fault_evidence else None
            ),
            "first_external_fault_evidence_latency_s": (
                first_fault_evidence[0] - start_s
                if first_fault_evidence else None
            ),
            "fault_event_confirmation": (
                "independent_observer_assertion_with_reference; "
                "read_path_response_only"
                if power_event_assertion_required
                and power_event_assertion_valid
                else "missing_or_invalid_independent_power_event_assertion"
                if power_event_assertion_required
                else "operator_marker_and_external_diagnostic_evidence"
            ),
            "power_event_assertion_required": (
                power_event_assertion_required
            ),
            "power_event_assertion_valid": power_event_assertion_valid,
            "independent_observer_ref": start.get(
                "independent_observer_ref"
            ),
            "first_invalid_emission_index": (
                int(first_invalid["emission_index"])
                if first_invalid is not None else None
            ),
            "invalid_detection_latency_s": (
                latency_from_start(first_invalid)
            ),
            "worker_error_detection_latency_s": latency_from_start(
                first_worker_error
            ),
            "disconnected_detection_latency_s": latency_from_start(
                first_disconnected
            ),
            "all_none_detection_latency_s": latency_from_start(first_all_none),
            "emission_timeout_detection_latency_s": latency_from_start(
                first_emission_timeout
            ),
            "network_interface_loss_detection_latency_s": latency_from_start(
                first_network_loss
            ),
            "serial_loss_detection_latency_s": latency_from_start(
                first_serial_loss
            ),
            "source_feed_stale_detection_latency_s": latency_from_start(
                first_source_stale
            ),
            "stale_valid_emission_indices": [
                int(row["emission_index"]) for row in stale_rows
            ],
            "valid_after_deadline_emission_indices": [
                int(row["emission_index"])
                for row in valid_after_deadline_rows
            ],
            "visual_freeze_candidate_emission_indices": [
                int(row["emission_index"]) for row in visual_freeze_rows
            ],
            "visual_freeze_interpretation": (
                "A repeated window fingerprint is only a candidate; a static "
                "surface or vendor repaint cannot prove camera exposure freshness."
            ),
            "first_emission_after_restore_latency_s": latency_from_recovery(
                first_post_emission
            ),
            "first_valid_after_restore_latency_s": latency_from_recovery(
                first_post_valid
            ),
            "first_value_change_after_restore_latency_s": latency_from_recovery(
                first_post_change
            ),
            "first_frame_change_after_restore_latency_s": latency_from_recovery(
                first_post_frame_change
            ),
            "first_link_recovery_latency_s": latency_from_recovery(
                first_link_recovery
            ),
            "recovery_evidence_kind": recovery_evidence,
            "recovery_evidence_limitation": recovery_limitation,
            "pre_failure_interface_identity": pre_interface_identity,
            "post_recovery_interface_identity": post_interface_identity,
            "interface_identity_required": identity_required,
            "interface_identity_consistent": identity_consistent,
            "required_consecutive_fresh_samples": required_fresh_samples,
            "recovery_confirmed": recovery_is_confirmed,
            "fresh_recovery_latency_s": latency_from_recovery(confirmed_fresh),
            "recovery_actions_marked": related_actions,
            "recovery_action_evidence": related_action_evidence,
            "rearm_verification": (
                "operator_marker_only_not_observed_gui_arm_state"
            ),
            "invalid_at_end": (
                rows[-1].get("measurement_valid") is False if rows else True
            ),
        })
    return episodes, sorted(set(stale_indices))


def summarize(
    rows: list[dict[str, Any]],
    source: str,
    *,
    mode: str | None = None,
    markers: list[dict[str, Any]] | None = None,
    watchdog_rows: list[dict[str, Any]] | None = None,
    baseline_p95_s: float | None = None,
    baseline_duration_s: float = 30.0,
    required_fresh_samples: int = 3,
) -> dict[str, Any]:
    """Summarize validity, emission cadence, and marker-aligned failures."""
    markers = markers or []
    watchdog_rows = watchdog_rows or []
    _ensure_annotations(rows, source)
    intervals = [
        float(current["observed_monotonic_s"])
        - float(previous["observed_monotonic_s"])
        for previous, current in zip(rows, rows[1:])
    ]
    baseline_start_s, baseline_end_s, baseline_ends_at_fault = (
        _baseline_window(
            rows,
            watchdog_rows,
            markers,
            baseline_duration_s,
        )
    )
    baseline_rows = [
        row
        for row in rows
        if baseline_start_s
        <= float(row.get("observed_monotonic_s", math.inf))
        < baseline_end_s
        or (
            not baseline_ends_at_fault
            and float(row.get("observed_monotonic_s", -math.inf))
            == baseline_end_s
        )
    ]
    measured_baseline = _baseline_intervals(baseline_rows)
    if baseline_p95_s is None:
        p95 = percentile(measured_baseline, 0.95)
        p95_source = "healthy_pre_failure_window"
    else:
        p95 = float(baseline_p95_s)
        p95_source = "supplied"
    stale_threshold = max(2.0 * p95, 3.0) if p95 is not None else 3.0
    healthy_baseline_rows = [
        row for row in baseline_rows
        if row.get("measurement_valid") is True
    ]
    baseline_boot_ids = {
        str(row.get("boot_id")) for row in baseline_rows if row.get("boot_id")
    }
    baseline_span_s = _baseline_span_s(baseline_rows)
    baseline_required_span_s = _coverage_requirement_s(
        baseline_duration_s,
        p95,
    )
    baseline_healthy = (
        len(healthy_baseline_rows) >= 3
        and len(healthy_baseline_rows) == len(baseline_rows)
        and len(measured_baseline) >= 2
        and len(baseline_boot_ids) <= 1
        and baseline_span_s >= baseline_required_span_s
    )
    baseline_watchdog = [
        row
        for row in watchdog_rows
        if baseline_start_s
        <= float(row.get("observed_monotonic_s", math.inf))
        < baseline_end_s
        or (
            not baseline_ends_at_fault
            and float(row.get("observed_monotonic_s", -math.inf))
            == baseline_end_s
        )
    ]
    channel_health = _channel_baseline_health(
        baseline_rows,
        baseline_watchdog,
        state_healthy=baseline_healthy,
        duration_s=baseline_duration_s,
        cadence_s=p95,
        stale_threshold_s=stale_threshold,
    )
    episodes, stale_indices = _failure_episodes(
        rows,
        markers,
        stale_threshold,
        source=source,
        mode=mode,
        watchdog_rows=watchdog_rows,
        baseline_healthy=baseline_healthy,
        channel_baseline_health=channel_health,
        required_fresh_samples=required_fresh_samples,
    )

    return {
        "source": source,
        "mode": mode,
        "emissions": len(rows),
        "connected_emissions": sum(
            row["state"].get("connected") is True for row in rows
        ),
        "error_emissions": sum(
            bool(row["state"].get("error")) for row in rows
        ),
        "valid_emissions": sum(
            row.get("measurement_valid") is True for row in rows
        ),
        "invalid_emissions": sum(
            row.get("measurement_valid") is False for row in rows
        ),
        "connected_but_invalid_emissions": sum(
            row["state"].get("connected") is True
            and row.get("measurement_valid") is False
            for row in rows
        ),
        "all_measurements_none_emissions": sum(
            row.get("all_measurements_none") is True for row in rows
        ),
        "value_change_emissions": sum(
            row.get("value_changed") is True for row in rows
        ),
        "data_age_available_emissions": sum(
            row.get("data_age_ms") is not None for row in rows
        ),
        "interval_s": {
            "median": statistics.median(intervals) if intervals else None,
            "p95": percentile(intervals, 0.95),
            "p99": percentile(intervals, 0.99),
            "max": max(intervals) if intervals else None,
        },
        "baseline_interval_s": {
            "source": p95_source,
            "duration_s": baseline_duration_s,
            "samples": len(measured_baseline),
            "p95": p95,
            "window_start_monotonic_s": baseline_start_s,
            "window_end_monotonic_s": baseline_end_s,
            "window_ends_at_failure_start": baseline_ends_at_fault,
        },
        "baseline_health": {
            "healthy": baseline_healthy,
            "rows": len(baseline_rows),
            "valid_rows": len(healthy_baseline_rows),
            "minimum_valid_rows": 3,
            "observed_span_s": baseline_span_s,
            "required_span_s": baseline_required_span_s,
            "single_boot": len(baseline_boot_ids) <= 1,
            "interpretation": (
                "Failure latency is not evaluable without a healthy valid "
                "pre-failure baseline."
            ),
        },
        "channel_baseline_health": channel_health,
        "watchdog_samples": len(watchdog_rows),
        "watchdog_emission_timeout_samples": sum(
            row.get("emission_timeout") is True for row in watchdog_rows
        ),
        "watchdog_network_interface_down_samples": sum(
            (row.get("network_interface") or {}).get("present") is False
            or (row.get("network_interface") or {}).get("is_up") is False
            for row in watchdog_rows
            if row.get("network_interface") is not None
        ),
        "watchdog_serial_missing_samples": sum(
            (row.get("serial_link") or {}).get("present") is False
            for row in watchdog_rows
        ),
        "watchdog_source_stale_samples": sum(
            isinstance(row.get("source_unchanged_for_s"), (int, float))
            and float(row["source_unchanged_for_s"]) > stale_threshold
            for row in watchdog_rows
        ),
        "stale_threshold_s": stale_threshold,
        "failure_marker_count": len(episodes),
        "failure_episodes": episodes,
        "stale_candidate_emission_indices": stale_indices,
        "stale_candidate_count": len(stale_indices),
        "stale_interpretation": (
            "Candidates are valid, unchanged values observed only inside an "
            "operator-marked failure window; constant values outside such a "
            "window are not labeled stale."
        ),
    }


def summary_incomplete_reasons(summary: dict[str, Any]) -> list[str]:
    episodes = summary.get("failure_episodes") or []
    if not episodes:
        return ["no failure-start episode was recorded"]
    reasons: list[str] = []
    for episode in episodes:
        episode_id = episode.get("episode_id")
        terminal = episode.get("terminal_kind")
        if terminal != "recovery":
            reasons.append(
                f"{episode_id}: terminal outcome is {terminal or 'missing'}"
            )
        if str(episode.get("assessment", "")).startswith("not_evaluable"):
            reasons.append(
                f"{episode_id}: {episode.get('assessment')}"
            )
        if terminal == "recovery" and episode.get("recovery_confirmed") is not True:
            reasons.append(
                f"{episode_id}: recovery lacks source-appropriate confirmation"
            )
        if (
            episode.get("fault_class") in SAFETY_GATED_FAULT_CLASSES
            and "rearmed" not in (episode.get("recovery_actions_marked") or [])
        ):
            reasons.append(f"{episode_id}: rearmed marker is missing")
    return reasons


def summary_validation_failure_reasons(
    summary: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    for episode in summary.get("failure_episodes") or []:
        episode_id = episode.get("episode_id")
        if episode.get("assessment") == "not_detected":
            reasons.append(
                f"{episode_id}: target State did not report a production "
                "detection"
            )
        if episode.get("assessment") == "late_detection":
            reasons.append(
                f"{episode_id}: target State detection exceeded the "
                "production deadline"
            )
        valid_after_deadline = (
            episode.get("valid_after_deadline_emission_indices") or []
        )
        if valid_after_deadline:
            reasons.append(
                f"{episode_id}: State remained valid after the production "
                f"deadline at emissions {valid_after_deadline}"
            )
        stale = episode.get("stale_valid_emission_indices") or []
        if stale:
            reasons.append(
                f"{episode_id}: stale valid data retained at emissions {stale}"
            )
    return reasons


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _git_dirty(root: Path) -> bool | None:
    try:
        output = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return bool(output.strip())
    except Exception:
        return None


def validate_source_mode(source: str, mode: str) -> None:
    allowed = VALID_MODES.get(source, ())
    if mode not in allowed:
        choices = ", ".join(allowed)
        raise ValueError(
            f"mode {mode!r} is invalid for {source}; choose one of: {choices}"
        )


def validate_fault_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Validate evidence metadata without authorizing any physical action."""
    spec = FAULT_CLASSES[args.fault_class]
    if args.fault_class == "offline-injection":
        raise ValueError(
            "offline-injection is available only in the synthetic unit-test "
            "harness; the live run command refuses to instantiate a real "
            "worker under an S0 label"
        )
    if args.fault_class in SAFETY_GATED_FAULT_CLASSES:
        required = {
            "scenario_id": args.scenario_id,
            "target": args.target,
            "boundary": args.boundary,
            "operator": args.operator,
            "observer": args.observer,
            "approver": args.approver,
            "authorization_ref": args.authorization_ref,
            "sop_ref": args.sop_ref,
            "safe_state_note": args.safe_state_note,
            "abort_criteria": args.abort_criteria,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(
                "manual fault plans require metadata: " + ", ".join(missing)
            )
        if not args.confirm_safe_state:
            raise ValueError(
                "manual fault plans require --confirm-safe-state; the "
                "observer still never performs the physical action"
            )
    if args.fault_class in {"transport-loss", "instrument-power-loss"} and (
        args.operator.strip().casefold() == args.observer.strip().casefold()
    ):
        raise ValueError(
            "S2/S3 requires distinct operator and observer identities"
        )
    if (
        args.fault_class == "instrument-power-loss"
        and not args.independent_observer_ref.strip()
    ):
        raise ValueError(
            "instrument-power-loss requires --independent-observer-ref; "
            "the result confirms an observer-asserted event and read-path "
            "response, not measured power state"
        )
    if args.fault_class == "transport-loss" and args.link_kind == "none":
        raise ValueError("transport-loss requires --link-kind")
    if args.fault_class == "transport-loss" and args.link_kind not in {
        "serial",
        "usb",
        "ethernet",
        "network",
    }:
        raise ValueError(
            "transport-loss supports only observable serial/USB or "
            "Ethernet/network boundaries"
        )
    if (
        args.fault_class == "transport-loss"
        and args.link_kind in {"ethernet", "network"}
    ):
        raise ValueError(
            "Ethernet/network transport-loss is not yet source-bound to an "
            "active GUI driver and is therefore not evaluable; add endpoint "
            "and driver identity evidence before enabling this S2 scenario"
        )
    if (
        args.fault_class in {"transport-loss", "instrument-power-loss"}
        and args.link_kind in {"serial", "usb"}
        and not (
            args.source == "pyrometer"
            and args.mode in {"exactus", "modbus"}
        )
    ):
        raise ValueError(
            "serial/USB transport-loss is currently supported only for "
            "pyrometer exactus/modbus, where the COM endpoint can be bound "
            "to the active driver; other USB sources require a dedicated "
            "PnP identity probe"
        )
    if args.fault_class == "instrument-power-loss" and not (
        args.source == "pyrometer"
        and args.mode in {"exactus", "modbus"}
        and args.link_kind in {"serial", "usb"}
    ):
        raise ValueError(
            "instrument-power-loss is currently supported only for an "
            "approved pyrometer exactus/modbus device or adapter on its "
            "bound COM endpoint; other devices need independent power and "
            "PnP identity evidence before this recorder can evaluate them"
        )
    if args.source == "pyrometer" and args.mode == "modbus" and (
        args.port.casefold() != "com4"
        or args.baudrate != 115200
        or args.device_id != 1
    ):
        raise ValueError(
            "the b24ceb1 PyrometerWorker does not forward non-default "
            "Modbus port/baud/device-id; this validation branch therefore "
            "permits only COM4/115200/device-id 1 and fails before opening "
            "any serial port"
        )
    if args.fault_class == "file-feed-stop" and not (
        args.source == "evap" and args.mode == "elog"
    ):
        raise ValueError("file-feed-stop requires --source evap --mode elog")
    if args.fault_class == "file-feed-stop" and args.link_kind != "file":
        raise ValueError("file-feed-stop requires --link-kind file")
    if args.fault_class == "window-source-loss" and args.mode not in {
        "screengrab",
        "screengrab_mss",
    }:
        raise ValueError(
            "window-source-loss requires a screengrab/UIA capture mode"
        )
    if args.fault_class == "window-source-loss" and args.link_kind != "video":
        raise ValueError("window-source-loss requires --link-kind video")
    if (
        args.fault_class == "workstation-power-observation"
        and not args.independent_observer_ref.strip()
    ):
        raise ValueError(
            "workstation power observation requires --independent-observer-ref"
        )
    if args.link_kind in {"ethernet", "network"} and not (
        isinstance(args.network_interface_index, int)
        and args.network_interface_index > 0
    ):
        raise ValueError(
            "Ethernet/network loss requires --network-interface-index for "
            "local netsh link-state evidence"
        )
    if (
        args.link_kind in {"ethernet", "network"}
        and args.network_probe_interval > 3.0
    ):
        raise ValueError(
            "Ethernet/network failure preflight requires "
            "--network-probe-interval <= 3 s"
        )
    return spec


def _configure_software_root(root: Path) -> Path:
    root = root.expanduser().resolve()
    workers_path = root / "gui" / "workers.py"
    if not workers_path.is_file():
        raise ValueError(
            f"--software-root does not contain gui/workers.py: {root}"
        )
    loaded_gui = sys.modules.get("gui")
    loaded_path = Path(getattr(loaded_gui, "__file__", "") or ".").resolve()
    if loaded_gui is not None and root not in loaded_path.parents:
        raise RuntimeError(
            "gui was already imported from a different checkout; run the "
            "probe in a fresh Python process"
        )
    root_text = str(root)
    if root_text in sys.path:
        sys.path.remove(root_text)
    sys.path.insert(0, root_text)
    return root


def _worker_factory(
    source: str,
    mode: str,
    interval: float,
    software_root: Path,
    *,
    port: str,
    baudrate: int,
):
    _configure_software_root(software_root)
    from gui.workers import (
        EvapControlWorker,
        MistralWorker,
        PyrometerWorker,
        RheedCameraWorker,
    )

    factories: dict[str, Callable[[], Any]] = {
        "camera": lambda: RheedCameraWorker(
            mode=mode, poll_interval=interval,
        ),
        "pyrometer": lambda: PyrometerWorker(
            mode=mode,
            poll_interval=interval,
            port=port,
            baudrate=baudrate,
        ),
        "mistral": lambda: MistralWorker(
            mode=mode, poll_interval=interval,
        ),
        "evap": lambda: EvapControlWorker(
            mode=mode, poll_interval=interval,
        ),
    }
    return factories[source]()


def driver_identity(worker: Any, source: str) -> dict[str, Any]:
    """Inspect already-created worker objects without initiating new reads."""
    driver = getattr(worker, DRIVER_ATTRS[source], None)
    identity: dict[str, Any] = {
        "worker_class": type(worker).__name__,
        "driver_class": type(driver).__name__ if driver is not None else None,
        "driver_module": type(driver).__module__ if driver is not None else None,
    }
    if driver is not None:
        identity["driver_connected"] = _json_value(
            getattr(driver, "connected", None)
        )
        for attribute in (
            "_port",
            "_baudrate",
            "_device_id",
            "_timeout",
            "_window_title",
            "_substring",
            "_log_dir",
            "_last_log_path",
            "_hwnd",
        ):
            if hasattr(driver, attribute):
                identity[attribute.removeprefix("_")] = _json_value(
                    getattr(driver, attribute)
                )
        backend = getattr(driver, "_backend", None)
        if backend is not None:
            identity["driver_backend"] = str(backend)
        session = getattr(driver, "_capture_session", None)
        if session is not None:
            identity["capture_session_class"] = type(session).__name__
            identity["capture_session_module"] = type(session).__module__
    return identity


def passive_network_interface_probe(
    interface_index: int | None,
    expected_name: str | None = None,
) -> dict[str, Any] | None:
    """Read ``netsh`` adapter link state without sending network traffic."""
    if interface_index is None:
        return None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            ["netsh", "interface", "ipv4", "show", "interfaces"],
            check=False,
            capture_output=True,
            timeout=5.0,
            creationflags=flags,
        )
    except Exception as exc:
        return {
            "interface_index": interface_index,
            "expected_name": expected_name,
            "present": None,
            "is_up": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if completed.returncode != 0:
        return {
            "interface_index": interface_index,
            "expected_name": expected_name,
            "present": None,
            "is_up": None,
            "error": (
                f"netsh exited {completed.returncode}: "
                + completed.stderr.decode("utf-8", errors="replace").strip()
            ),
        }
    match: list[bytes] | None = None
    for raw_line in completed.stdout.splitlines():
        fields = raw_line.strip().split(None, 4)
        if len(fields) == 5 and fields[0].isdigit():
            if int(fields[0]) == interface_index:
                match = fields
                break
    if match is None:
        return {
            "interface_index": interface_index,
            "expected_name": expected_name,
            "present": False,
            "is_up": False,
            "error": None,
        }
    try:
        actual_name = match[4].decode("utf-8")
    except UnicodeDecodeError:
        actual_name = match[4].decode(errors="replace")
    state = match[3].decode("ascii", errors="replace").casefold()
    name_matches = (
        None
        if not expected_name
        else actual_name.casefold() == expected_name.casefold()
    )
    return {
        "interface_index": interface_index,
        "interface": actual_name,
        "expected_name": expected_name,
        "name_matches": name_matches,
        "present": True,
        "is_up": state == "connected",
        "state": state,
        "metric": int(match[1]),
        "mtu": int(match[2]),
        "sampled_at_utc": utc_now(),
        "error": None,
    }


def passive_serial_probe(port: str | None) -> dict[str, Any] | None:
    """Enumerate a configured COM endpoint without opening or writing it."""
    if not port:
        return None
    try:
        from serial.tools import list_ports

        devices = list(list_ports.comports())
    except Exception as exc:
        return {
            "port": port,
            "present": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    match = next(
        (
            device for device in devices
            if str(device.device).casefold() == port.casefold()
        ),
        None,
    )
    if match is None:
        return {"port": port, "present": False, "error": None}
    return {
        "port": port,
        "present": True,
        "description": match.description,
        "hwid": match.hwid,
        "vid": match.vid,
        "pid": match.pid,
        "serial_number": match.serial_number,
        "error": None,
    }


def passive_source_probe(worker: Any, source: str) -> dict[str, Any] | None:
    """Inspect source-side freshness without changing driver or instrument state."""
    driver = getattr(worker, DRIVER_ATTRS[source], None)
    if driver is None:
        return None
    result: dict[str, Any] = {
        "driver_connected": _json_value(getattr(driver, "connected", None)),
    }
    if source != "evap" or type(driver).__name__ != "ElogReader":
        return result
    raw_path = getattr(driver, "_last_log_path", None)
    if raw_path is None:
        return {**result, "elog_path": None}
    path = Path(raw_path)
    result["elog_path"] = str(path)
    try:
        stat = path.stat()
        result.update({
            "elog_size_bytes": stat.st_size,
            "elog_mtime_ns": stat.st_mtime_ns,
        })
        with path.open("rb") as stream:
            stream.seek(max(0, stat.st_size - 4096))
            result["elog_tail_sha256"] = hashlib.sha256(
                stream.read()
            ).hexdigest()
        from drivers.elog import latest_record

        source_at, _ = latest_record(path, [])
        result["elog_source_at_utc"] = source_at.isoformat()
        result["error"] = None
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _append_jsonl(stream: Any, row: dict[str, Any]) -> None:
    stream.write(json.dumps(row, separators=(",", ":")) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def _append_jsonl_path(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        _append_jsonl(stream, row)


def prepare_output_dir(path: Path) -> Path:
    """Create a new evidence directory; never overwrite or merge old runs."""
    path = path.expanduser().resolve()
    if path.exists():
        raise FileExistsError(
            f"output directory already exists; choose a new run directory: {path}"
        )
    path.mkdir(parents=True, exist_ok=False)
    return path


def _load_jsonl(
    path: Path,
    label: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        return [], []
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                errors.append(f"{label}:{line_number}: {exc}")
    return rows, errors


def _load_markers(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    return _load_jsonl(path, "markers.jsonl")


def marker_pairing_errors(markers: list[dict[str, Any]]) -> list[str]:
    starts: dict[str, dict[str, Any]] = {}
    terminals: set[str] = set()
    errors: list[str] = []
    active_episode: str | None = None
    ordered = sorted(
        markers,
        key=lambda marker: (
            _parse_utc(marker.get("marked_at_utc")).timestamp()
            if _parse_utc(marker.get("marked_at_utc")) is not None
            else float(marker.get("marked_monotonic_s", 0.0))
        ),
    )
    failure_start_count = 0
    for marker in ordered:
        episode_id = str(marker.get("episode_id") or "")
        kind = marker.get("kind")
        if not episode_id:
            continue
        if kind == "failure-start":
            failure_start_count += 1
            if failure_start_count > 1:
                errors.append(
                    "multiple failure-start markers found; exactly one "
                    "fault episode is allowed per run"
                )
            if episode_id in starts:
                errors.append(f"duplicate failure-start for {episode_id}")
            if active_episode is not None and active_episode != episode_id:
                errors.append(
                    f"overlapping failure episodes: {active_episode}, {episode_id}"
                )
            starts[episode_id] = marker
            active_episode = episode_id
        elif kind in {"recovery", "abort", "persistent-failure", "incomplete"}:
            if episode_id not in starts:
                errors.append(
                    f"{kind} without failure-start for {episode_id}"
                )
            if episode_id in terminals:
                errors.append(f"duplicate terminal marker for {episode_id}")
            terminals.add(episode_id)
            if active_episode == episode_id:
                active_episode = None
    for episode_id in starts:
        if episode_id not in terminals:
            errors.append(f"failure-start without terminal marker for {episode_id}")
    return errors


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_sha_manifest(output_dir: Path) -> dict[str, str]:
    names = (
        "run_info.json",
        "states.jsonl",
        "watchdog.jsonl",
        "markers.jsonl",
        "summary.json",
    )
    manifest = {
        name: _sha256(output_dir / name)
        for name in names
        if (output_dir / name).is_file()
    }
    _write_json_atomic(output_dir / "sha256_manifest.json", manifest)
    return manifest


def run_probe(args: argparse.Namespace) -> int:
    from PyQt6.QtCore import QCoreApplication

    validate_source_mode(args.source, args.mode)
    fault_spec = validate_fault_plan(args)
    software_root = args.software_root.expanduser().resolve()
    app = QCoreApplication.instance() or QCoreApplication([])
    serial_port = (
        args.port
        if args.source == "pyrometer"
        and args.mode in {"exactus", "modbus"}
        else None
    )
    serial_mutex = _SerialPortMutex(serial_port)
    with serial_mutex:
        output_dir = prepare_output_dir(args.output_dir)
        with _RecorderLivenessLease(output_dir) as liveness:
            return _run_probe_locked(
                args,
                fault_spec,
                software_root,
                app,
                serial_mutex,
                output_dir,
                liveness,
            )


def _run_probe_locked(
    args: argparse.Namespace,
    fault_spec: dict[str, Any],
    software_root: Path,
    app: Any,
    serial_mutex: _SerialPortMutex,
    output_dir: Path,
    liveness: _RecorderLivenessLease,
) -> int:
    """Run while holding the shared serial-reader lock, when applicable."""
    from PyQt6.QtCore import QTimer

    worker = _worker_factory(
        args.source,
        args.mode,
        args.poll_interval,
        software_root,
        port=args.port,
        baudrate=args.baudrate,
    )
    rows: list[dict[str, Any]] = []
    tracker = ObservationTracker()
    watchdog_rows: list[dict[str, Any]] = []
    fatal_errors: list[str] = []
    state_path = output_dir / "states.jsonl"
    watchdog_path = output_dir / "watchdog.jsonl"
    with watchdog_path.open("x", encoding="utf-8"):
        pass
    started_monotonic = time.monotonic()
    started_monotonic_ns = time.monotonic_ns()
    boot_id, boot_id_source = current_boot_identity()
    emission_timeout_s = (
        args.emission_timeout_s
        if args.emission_timeout_s is not None
        else max(
            2.0 * (
                args.baseline_p95_s
                if args.baseline_p95_s is not None
                else args.poll_interval
            ),
            3.0,
        )
    )
    run_info: dict[str, Any] = {
        "started_at_utc": utc_now(),
        "recorder_git_commit": _git_commit(RECORDER_ROOT),
        "recorder_git_dirty": _git_dirty(RECORDER_ROOT),
        "recorder_root": str(RECORDER_ROOT),
        "software_git_commit": _git_commit(software_root),
        "software_git_dirty": _git_dirty(software_root),
        "software_root": str(software_root),
        "source": args.source,
        "mode": args.mode,
        "expected_capture_backend": args.expected_capture_backend,
        "duration_s_requested": args.duration_s,
        "poll_interval_s": args.poll_interval,
        "baseline_duration_s": args.baseline_duration_s,
        "baseline_p95_s_supplied": args.baseline_p95_s,
        "emission_timeout_s": emission_timeout_s,
        "watchdog_interval_s": args.watchdog_interval,
        "required_fresh_samples": args.required_fresh_samples,
        "boot_id": boot_id,
        "boot_id_source": boot_id_source,
        "campaign_id": args.campaign_id,
        "scenario_id": args.scenario_id,
        "fault_class": args.fault_class,
        "safety_tier": fault_spec["safety_tier"],
        "fault_description": fault_spec["description"],
        "physical_fault_planned": fault_spec["physical"],
        "target": args.target,
        "boundary": args.boundary,
        "link_kind": args.link_kind,
        "operator": args.operator,
        "observer": args.observer,
        "approver": args.approver,
        "authorization_ref": args.authorization_ref,
        "sop_ref": args.sop_ref,
        "safe_state_note": args.safe_state_note,
        "abort_criteria": args.abort_criteria,
        "safe_state_confirmed": args.confirm_safe_state,
        "independent_observer_ref": args.independent_observer_ref,
        "requested_interface": {
            "port": (
                args.port
                if args.source == "pyrometer"
                or args.link_kind in {"serial", "usb"}
                else None
            ),
            "baudrate": (
                args.baudrate if args.source == "pyrometer" else None
            ),
            "device_id": args.device_id if args.mode == "modbus" else None,
            "network_interface": args.network_interface,
            "network_interface_index": args.network_interface_index,
            "network_probe_interval_s": args.network_probe_interval,
            "serial_runner_mutex": serial_mutex.name,
        },
        "platform": platform.platform(),
        "python": sys.version,
        "command": " ".join([sys.executable, *sys.argv]),
        "operator_actions_are_manual": True,
        "state_changing_commands_sent": False,
        "direct_read_requests_may_be_sent": args.mode in {
            "exactus",
            "modbus",
            "jsonrpc",
        },
        "watchdog_execution": "dedicated_thread_same_process",
        "watchdog_limit": (
            "Independent of the Qt event loop/worker, but not of process, "
            "GIL, filesystem, or workstation failure."
        ),
        "recorder_pid": os.getpid(),
        "recorder_liveness_name": liveness.name,
        "worker_identity": driver_identity(worker, args.source),
    }
    _write_json_atomic(output_dir / "run_info.json", run_info)

    timer = QTimer()
    timer.setSingleShot(True)
    watchdog_stop_event = threading.Event()
    watchdog_thread: threading.Thread | None = None
    lifecycle = {
        "finishing": False,
        "finish_reason": "",
        "stop_timed_out": False,
        "worker_finished_unexpectedly": False,
        "backend_verified": args.expected_capture_backend is None,
        "interface_verified": args.source != "pyrometer",
        "last_emission_monotonic_s": None,
        "last_emission_index": None,
        "previous_source_token": None,
        "last_source_change_monotonic_s": None,
        "last_network_probe_monotonic_s": None,
        "network_probe_result": None,
        "watchdog_stop_timed_out": False,
    }

    def refresh_identity() -> None:
        identity = driver_identity(worker, args.source)
        if identity != run_info.get("worker_identity"):
            run_info["worker_identity"] = identity
            _write_json_atomic(output_dir / "run_info.json", run_info)
        actual_class = identity.get("driver_class")
        expected_class = EXPECTED_DRIVER_CLASSES[(args.source, args.mode)]
        if actual_class is not None and actual_class != expected_class:
            message = (
                f"mode {args.mode!r} created {actual_class}, expected "
                f"{expected_class}; refusing dummy/fallback validation"
            )
            if message not in fatal_errors:
                fatal_errors.append(message)
        if (
            args.source == "pyrometer"
            and args.mode in {"exactus", "modbus"}
            and actual_class is not None
        ):
            effective_port = identity.get("port")
            effective_baudrate = identity.get("baudrate")
            mismatches = []
            if effective_port != args.port:
                mismatches.append(
                    f"port requested={args.port!r} effective={effective_port!r}"
                )
            if effective_baudrate != args.baudrate:
                mismatches.append(
                    "baudrate requested="
                    f"{args.baudrate!r} effective={effective_baudrate!r}"
                )
            if args.mode == "modbus":
                effective_device_id = identity.get("device_id")
                if effective_device_id != args.device_id:
                    mismatches.append(
                        "device_id requested="
                        f"{args.device_id!r} effective={effective_device_id!r}"
                    )
            if mismatches:
                message = "effective interface mismatch: " + "; ".join(mismatches)
                if message not in fatal_errors:
                    fatal_errors.append(message)
            else:
                lifecycle["interface_verified"] = True
        elif args.source == "pyrometer" and actual_class is not None:
            lifecycle["interface_verified"] = True

    def finish(reason: str = "duration") -> None:
        if lifecycle["finishing"]:
            return
        lifecycle["finishing"] = True
        lifecycle["finish_reason"] = reason
        timer.stop()
        watchdog_stop_event.set()
        try:
            worker.stop()
        except Exception as exc:
            fatal_errors.append(f"worker stop raised: {exc!r}")
            serial_mutex.retain_until_process_exit()
        try:
            stopped = bool(worker.wait(5000))
        except Exception as exc:
            stopped = False
            fatal_errors.append(f"worker wait raised: {exc!r}")
        if not stopped:
            lifecycle["stop_timed_out"] = True
            fatal_errors.append("worker did not stop within 5 seconds")
            serial_mutex.retain_until_process_exit()
        app.quit()

    def on_state(state: Any) -> None:
        try:
            observed_at = utc_now()
            observed_s = time.monotonic()
            observed_ns = time.monotonic_ns()
            row = {
                "emission_index": len(rows),
                "observed_at_utc": observed_at,
                "observed_monotonic_s": observed_s,
                "observed_monotonic_ns": observed_ns,
                "elapsed_s": observed_s - started_monotonic,
                "boot_id": boot_id,
                "boot_id_source": boot_id_source,
                "frame_fingerprint": (
                    frame_fingerprint(state)
                    if args.source == "camera"
                    else None
                ),
                "state": state_payload(state),
            }
            annotate_observation(row, args.source, tracker)
            rows.append(row)
            _append_jsonl(states_stream, row)
            lifecycle["last_emission_monotonic_s"] = observed_s
            lifecycle["last_emission_index"] = row["emission_index"]
            refresh_identity()

            if args.expected_capture_backend is not None:
                actual_backend = row["state"].get("capture_backend")
                if actual_backend == args.expected_capture_backend:
                    lifecycle["backend_verified"] = True
                elif actual_backend not in (None, ""):
                    fatal_errors.append(
                        "capture backend mismatch: expected "
                        f"{args.expected_capture_backend!r}, observed "
                        f"{actual_backend!r}"
                    )

            print(
                f"{row['emission_index']:05d} {observed_at} "
                f"connected={row['state'].get('connected')} "
                f"valid={row['measurement_valid']} "
                f"changed={row['value_changed']} "
                f"age_ms={row['data_age_ms']} "
                f"error={row['state'].get('error')!r}",
                flush=True,
            )
            if fatal_errors:
                QTimer.singleShot(0, lambda: finish("validation-error"))
        except Exception as exc:
            fatal_errors.append(f"state recorder failed: {exc!r}")
            QTimer.singleShot(0, lambda: finish("recorder-error"))

    def on_watchdog() -> None:
        try:
            observed_s = time.monotonic()
            last_emission_s = lifecycle["last_emission_monotonic_s"]
            emission_age_s = (
                observed_s - float(last_emission_s)
                if last_emission_s is not None
                else observed_s - started_monotonic
            )
            serial_link = (
                passive_serial_probe(args.port)
                if args.link_kind in {"serial", "usb"}
                else None
            )
            source_probe = passive_source_probe(worker, args.source)
            source_token = None
            if source_probe:
                source_at = source_probe.get("elog_source_at_utc")
                if source_at:
                    source_token = ("elog_source_at_utc", source_at)
            source_changed: bool | None = None
            if source_token is not None:
                source_changed = (
                    lifecycle["previous_source_token"] is None
                    or source_token != lifecycle["previous_source_token"]
                )
                if source_changed:
                    lifecycle["previous_source_token"] = source_token
                    lifecycle["last_source_change_monotonic_s"] = observed_s
            source_unchanged_for_s = (
                observed_s - float(lifecycle["last_source_change_monotonic_s"])
                if source_token is not None
                and lifecycle["last_source_change_monotonic_s"] is not None
                else None
            )
            network_probe_new = False
            last_network_probe = lifecycle["last_network_probe_monotonic_s"]
            if args.network_interface_index is not None and (
                last_network_probe is None
                or observed_s - float(last_network_probe)
                >= args.network_probe_interval
            ):
                lifecycle["network_probe_result"] = (
                    passive_network_interface_probe(
                        args.network_interface_index,
                        args.network_interface,
                    )
                )
                lifecycle["last_network_probe_monotonic_s"] = observed_s
                network_probe_new = True
            watchdog_observed_at = utc_now()
            source_at = _parse_utc(
                (source_probe or {}).get("elog_source_at_utc")
            )
            observed_at = _parse_utc(watchdog_observed_at)
            source_age_ms = (
                (observed_at - source_at).total_seconds() * 1000.0
                if source_at is not None and observed_at is not None
                else None
            )
            row = {
                "watchdog_index": len(watchdog_rows),
                "observed_at_utc": watchdog_observed_at,
                "observed_monotonic_s": observed_s,
                "observed_monotonic_ns": time.monotonic_ns(),
                "elapsed_s": observed_s - started_monotonic,
                "boot_id": boot_id,
                "boot_id_source": boot_id_source,
                "last_emission_index": lifecycle["last_emission_index"],
                "last_emission_age_s": emission_age_s,
                "emission_timeout_s": emission_timeout_s,
                "emission_timeout": emission_age_s > emission_timeout_s,
                "worker_running": bool(worker.isRunning()),
                "driver_identity": driver_identity(worker, args.source),
                "network_interface": lifecycle["network_probe_result"],
                "network_probe_new": network_probe_new,
                "serial_link": serial_link,
                "source_probe": source_probe,
                "source_age_ms": source_age_ms,
                "source_changed": source_changed,
                "source_unchanged_for_s": source_unchanged_for_s,
            }
            if watchdog_stop_event.is_set():
                return
            _append_jsonl_path(watchdog_path, row)
            watchdog_rows.append(row)
            if (
                row["emission_timeout"]
                or (
                    (row.get("network_interface") or {}).get("present") is False
                    or (row.get("network_interface") or {}).get("is_up") is False
                )
                or (row.get("serial_link") or {}).get("present") is False
            ):
                print(
                    "WATCHDOG "
                    f"age_s={emission_age_s:.3f} "
                    f"timeout={row['emission_timeout']} "
                    f"network={row.get('network_interface')} "
                    f"serial={row.get('serial_link')}",
                    flush=True,
                )
        except Exception as exc:
            fatal_errors.append(f"watchdog recorder failed: {exc!r}")
            lifecycle["finish_reason"] = "watchdog-error"
            watchdog_stop_event.set()
            try:
                worker.stop()
            except Exception as stop_exc:
                fatal_errors.append(
                    f"worker stop after watchdog error raised: {stop_exc!r}"
                )
                serial_mutex.retain_until_process_exit()
            app.quit()

    def watchdog_loop() -> None:
        while not watchdog_stop_event.is_set():
            on_watchdog()
            watchdog_stop_event.wait(args.watchdog_interval)

    def on_worker_finished() -> None:
        refresh_identity()
        if not lifecycle["finishing"]:
            lifecycle["worker_finished_unexpectedly"] = True
            fatal_errors.append("worker exited before the requested duration")
            watchdog_stop_event.set()
            app.quit()

    worker.state_updated.connect(on_state)
    worker.finished.connect(on_worker_finished)
    timer.timeout.connect(lambda: finish("duration"))
    timer.start(max(1, round(args.duration_s * 1000)))

    with state_path.open(
        "x",
        encoding="utf-8",
        buffering=1,
    ) as states_stream:
        try:
            watchdog_thread = threading.Thread(
                target=watchdog_loop,
                name="ombe-failure-watchdog",
                daemon=True,
            )
            watchdog_thread.start()
            worker.start()
            app.exec()
        except KeyboardInterrupt:
            fatal_errors.append("probe interrupted by operator")
        except Exception as exc:
            fatal_errors.append(f"probe runtime failed: {exc!r}")
        finally:
            watchdog_stop_event.set()
            if not lifecycle["finishing"] and worker.isRunning():
                finish("finally")
            if watchdog_thread is not None:
                watchdog_thread.join(timeout=10.0)
                if watchdog_thread.is_alive():
                    lifecycle["watchdog_stop_timed_out"] = True
                    liveness.retain_until_process_exit()
                    fatal_errors.append(
                        "watchdog thread did not stop within 10 seconds; "
                        "manifest will not be written"
                    )
            states_stream.flush()
            os.fsync(states_stream.fileno())

    refresh_identity()
    if (
        args.expected_capture_backend is not None
        and not lifecycle["backend_verified"]
    ):
        fatal_errors.append(
            "expected capture backend was never observed in CameraState"
        )
    if not rows:
        fatal_errors.append("worker emitted no states")

    with _EvidenceMutex(output_dir):
        markers, marker_errors = _load_markers(
            output_dir / "markers.jsonl"
        )
        lifecycle_fatal_errors = list(fatal_errors)
        marker_evidence_errors = [
            *marker_errors,
            *marker_pairing_errors(markers),
        ]
        all_fatal_errors = [
            *lifecycle_fatal_errors,
            *marker_evidence_errors,
        ]
        summary = summarize(
            rows,
            args.source,
            mode=args.mode,
            markers=markers,
            watchdog_rows=watchdog_rows,
            baseline_p95_s=args.baseline_p95_s,
            baseline_duration_s=args.baseline_duration_s,
            required_fresh_samples=args.required_fresh_samples,
        )
        finished_at = utc_now()
        manifest_written = not lifecycle["watchdog_stop_timed_out"]
        summary.update({
            **run_info,
            "evidence_schema_version": 2,
            "finished_at_utc": finished_at,
            "duration_s_observed": (
                time.monotonic_ns() - started_monotonic_ns
            ) / 1_000_000_000.0,
            "finish_reason": lifecycle["finish_reason"],
            "stop_timed_out": lifecycle["stop_timed_out"],
            "watchdog_stop_timed_out": (
                lifecycle["watchdog_stop_timed_out"]
            ),
            "watchdog_evidence_may_be_incomplete": (
                lifecycle["watchdog_stop_timed_out"]
            ),
            "manifest_written": manifest_written,
            "liveness_retained_until_exit": (
                liveness._retain_until_exit
            ),
            "serial_mutex_retained_until_exit": (
                serial_mutex._retain_until_exit
            ),
            "worker_finished_unexpectedly": (
                lifecycle["worker_finished_unexpectedly"]
            ),
            "backend_verified": lifecycle["backend_verified"],
            "interface_verified": lifecycle["interface_verified"],
            "lifecycle_fatal_errors": lifecycle_fatal_errors,
            "marker_evidence_errors": marker_evidence_errors,
            "fatal_errors": all_fatal_errors,
            "markers_file": str(output_dir / "markers.jsonl"),
            "watchdog_file": str(watchdog_path),
        })
        incomplete_reasons = summary_incomplete_reasons(summary)
        validation_failures = summary_validation_failure_reasons(summary)
        summary["run_complete"] = (
            not all_fatal_errors and not incomplete_reasons
        )
        summary["incomplete_reasons"] = incomplete_reasons
        summary["validation_passed"] = (
            summary["run_complete"] and not validation_failures
        )
        summary["validation_failure_reasons"] = validation_failures
        run_info.update({
            "finished_at_utc": finished_at,
            "finish_reason": lifecycle["finish_reason"],
            "worker_identity": driver_identity(worker, args.source),
        })
        _write_json_atomic(output_dir / "run_info.json", run_info)
        _write_json_atomic(output_dir / "summary.json", summary)
        if manifest_written:
            write_sha_manifest(output_dir)
    print(json.dumps(summary, indent=2), flush=True)
    return (
        2
        if all_fatal_errors
        else 3
        if incomplete_reasons
        else 4
        if validation_failures
        else 0
    )


def finalize_interrupted_run(args: argparse.Namespace) -> int:
    """Rebuild summary/hash evidence after a crash or workstation restart."""
    output_dir = args.output_dir.expanduser().resolve()
    with _EvidenceMutex(output_dir):
        return _finalize_interrupted_run_locked(args, output_dir)


def _finalize_interrupted_run_locked(
    args: argparse.Namespace,
    output_dir: Path,
) -> int:
    run_info_path = output_dir / "run_info.json"
    if not run_info_path.is_file():
        raise FileNotFoundError(f"missing run_info.json: {output_dir}")
    if _RecorderLivenessLease(output_dir).is_live():
        raise RuntimeError(
            "refusing to finalize while the recorder liveness lease is "
            "active; wait for the recorder process to exit"
        )
    summary_path = output_dir / "summary.json"
    if summary_path.exists() and not args.force:
        raise FileExistsError(
            "summary.json already exists; use --force only to incorporate "
            "post-run markers into a refreshed summary"
        )
    existing_summary: dict[str, Any] = {}
    if summary_path.is_file():
        existing_summary = json.loads(
            summary_path.read_text(encoding="utf-8")
        )
    run_info = json.loads(run_info_path.read_text(encoding="utf-8"))
    rows, state_errors = _load_jsonl(
        output_dir / "states.jsonl", "states.jsonl"
    )
    watchdog_rows, watchdog_errors = _load_jsonl(
        output_dir / "watchdog.jsonl", "watchdog.jsonl"
    )
    markers, marker_errors = _load_markers(output_dir / "markers.jsonl")
    marker_evidence_errors = [
        *state_errors,
        *watchdog_errors,
        *marker_errors,
        *marker_pairing_errors(markers),
    ]
    if not rows:
        marker_evidence_errors.append(
            "no complete state rows were recoverable"
        )
    source = str(run_info.get("source", ""))
    if source not in VALID_MODES:
        raise ValueError(f"invalid or missing source in run_info.json: {source!r}")
    summary = summarize(
        rows,
        source,
        mode=run_info.get("mode"),
        markers=markers,
        watchdog_rows=watchdog_rows,
        baseline_p95_s=run_info.get("baseline_p95_s_supplied"),
        baseline_duration_s=float(
            run_info.get("baseline_duration_s", 30.0)
        ),
        required_fresh_samples=int(
            run_info.get("required_fresh_samples", 3)
        ),
    )
    finalizer_boot_id, finalizer_boot_id_source = current_boot_identity()
    preserved_keys = (
        "finished_at_utc",
        "duration_s_observed",
        "finish_reason",
        "stop_timed_out",
        "watchdog_stop_timed_out",
        "serial_mutex_retained_until_exit",
        "liveness_retained_until_exit",
        "worker_finished_unexpectedly",
        "backend_verified",
        "interface_verified",
        "watchdog_evidence_may_be_incomplete",
        "markers_file",
        "watchdog_file",
    )
    preserved = {
        key: existing_summary[key]
        for key in preserved_keys
        if key in existing_summary
    }
    if not existing_summary:
        lifecycle_fatal_errors = [
            "recorder terminated before normal lifecycle finalization"
        ]
    elif existing_summary.get("evidence_schema_version") == 2:
        lifecycle_fatal_errors = list(
            existing_summary.get("lifecycle_fatal_errors") or []
        )
    else:
        lifecycle_fatal_errors = list(
            existing_summary.get("fatal_errors") or []
        )
    all_fatal_errors = [
        *lifecycle_fatal_errors,
        *marker_evidence_errors,
    ]
    summary.update({
        **run_info,
        **preserved,
        "evidence_schema_version": 2,
        "finalized_at_utc": utc_now(),
        "finalized_after_interruption": existing_summary.get(
            "finalized_after_interruption",
            not bool(existing_summary),
        ),
        "summary_refresh_reason": (
            "post_run_marker" if existing_summary else "interruption_recovery"
        ),
        "finalizer_git_commit": _git_commit(RECORDER_ROOT),
        "finalizer_boot_id": finalizer_boot_id,
        "finalizer_boot_id_source": finalizer_boot_id_source,
        "boot_changed_since_start": (
            bool(run_info.get("boot_id"))
            and run_info.get("boot_id") != finalizer_boot_id
        ),
        "recovered_state_rows": len(rows),
        "recovered_watchdog_rows": len(watchdog_rows),
        "manifest_written": True,
        "lifecycle_fatal_errors": lifecycle_fatal_errors,
        "marker_evidence_errors": marker_evidence_errors,
        "fatal_errors": all_fatal_errors,
        "finalizer_errors": marker_evidence_errors,
    })
    incomplete_reasons = summary_incomplete_reasons(summary)
    validation_failures = summary_validation_failure_reasons(summary)
    summary["run_complete"] = (
        not all_fatal_errors and not incomplete_reasons
    )
    summary["incomplete_reasons"] = incomplete_reasons
    summary["validation_passed"] = (
        summary["run_complete"] and not validation_failures
    )
    summary["validation_failure_reasons"] = validation_failures
    _write_json_atomic(summary_path, summary)
    write_sha_manifest(output_dir)
    if not getattr(args, "quiet", False):
        print(json.dumps(summary, indent=2), flush=True)
    return (
        2
        if all_fatal_errors
        else 3
        if incomplete_reasons
        else 4
        if validation_failures
        else 0
    )


def _validate_interface_before_failure(
    run_info: dict[str, Any],
    output_dir: Path,
) -> None:
    """Fail closed when the declared physical boundary is not observable."""
    link_kind = str(run_info.get("link_kind") or "none")
    if link_kind not in {"serial", "usb", "ethernet", "network"}:
        return
    watchdog_rows, errors = _load_jsonl(
        output_dir / "watchdog.jsonl",
        "watchdog.jsonl",
    )
    if errors or not watchdog_rows:
        raise ValueError(
            "failure marker requires a complete watchdog sample for the "
            "declared interface"
        )
    latest = watchdog_rows[-1]
    latest_at = _parse_utc(latest.get("observed_at_utc"))
    max_age_s = max(
        3.0,
        2.0 * float(run_info.get("watchdog_interval_s", 0.5)),
    )
    if latest_at is None or (
        datetime.now(timezone.utc) - latest_at
    ).total_seconds() > max_age_s:
        raise ValueError(
            "latest watchdog interface sample is missing or too old; "
            "do not start the manual fault"
        )
    requested = run_info.get("requested_interface") or {}
    if link_kind in {"serial", "usb"}:
        serial_link = latest.get("serial_link") or {}
        if serial_link.get("present") is not True or not (
            serial_link.get("hwid") or serial_link.get("serial_number")
        ):
            raise ValueError(
                "serial/USB failure marker requires the configured endpoint "
                "to be present with HWID or serial-number evidence"
            )
        if (
            run_info.get("source") == "pyrometer"
            and run_info.get("mode") in {"exactus", "modbus"}
        ):
            identity = latest.get("driver_identity") or {}
            expected = {
                "port": requested.get("port"),
                "baudrate": requested.get("baudrate"),
            }
            if run_info.get("mode") == "modbus":
                expected["device_id"] = requested.get("device_id")
            mismatches = [
                f"{field}: expected {value!r}, observed {identity.get(field)!r}"
                for field, value in expected.items()
                if value is not None and identity.get(field) != value
            ]
            if mismatches:
                raise ValueError(
                    "serial identity is unknown or mismatched: "
                    + "; ".join(mismatches)
                )
    else:
        network = latest.get("network_interface") or {}
        expected_name = requested.get("network_interface")
        network_sampled_at = _parse_utc(network.get("sampled_at_utc"))
        network_sample_age_s = (
            (datetime.now(timezone.utc) - network_sampled_at).total_seconds()
            if network_sampled_at is not None
            else math.inf
        )
        if (
            network.get("present") is not True
            or network.get("is_up") is not True
            or not (0.0 <= network_sample_age_s <= 3.0)
            or network.get("interface_index")
            != requested.get("network_interface_index")
            or (
                expected_name
                and network.get("name_matches") is not True
            )
        ):
            raise ValueError(
                "network failure marker requires the exact declared local "
                "interface to be present and up"
            )


def _validate_live_baseline_before_failure(
    run_info: dict[str, Any],
    output_dir: Path,
    markers: list[dict[str, Any]],
) -> None:
    """Require a live recorder and a complete healthy pre-fault baseline."""
    if (output_dir / "summary.json").exists():
        raise ValueError(
            "failure-start refused because the recorder has already ended; "
            "start a new run"
        )
    if not _RecorderLivenessLease(output_dir).is_live():
        raise ValueError(
            "failure-start requires an active recorder process holding the "
            "run liveness lease"
        )
    rows, state_errors = _load_jsonl(
        output_dir / "states.jsonl",
        "states.jsonl",
    )
    watchdog_rows, watchdog_errors = _load_jsonl(
        output_dir / "watchdog.jsonl",
        "watchdog.jsonl",
    )
    evidence_errors = [*state_errors, *watchdog_errors]
    if evidence_errors:
        raise ValueError(
            "failure-start refused because live evidence is incomplete: "
            + "; ".join(evidence_errors)
        )
    if not rows or not watchdog_rows:
        raise ValueError(
            "failure-start requires live State and watchdog evidence plus "
            "a healthy baseline"
        )
    source = str(run_info.get("source") or "")
    if source not in VALID_MODES:
        raise ValueError(
            "failure-start requires a valid source in run_info.json"
        )
    _ensure_annotations(rows, source)
    now = datetime.now(timezone.utc)

    def require_fresh(
        record: dict[str, Any],
        *,
        label: str,
        interval_s: float,
    ) -> None:
        observed_at = _parse_utc(record.get("observed_at_utc"))
        age_s = (
            (now - observed_at).total_seconds()
            if observed_at is not None
            else math.inf
        )
        max_age_s = max(3.0, 2.0 * interval_s)
        if not (0.0 <= age_s <= max_age_s):
            raise ValueError(
                f"latest {label} sample is missing or too old "
                f"({age_s:.3f} s; limit {max_age_s:.3f} s)"
            )

    latest_state = rows[-1]
    latest_watchdog = watchdog_rows[-1]
    require_fresh(
        latest_state,
        label="State",
        interval_s=float(run_info.get("poll_interval_s", 1.0)),
    )
    require_fresh(
        latest_watchdog,
        label="watchdog",
        interval_s=float(run_info.get("watchdog_interval_s", 0.5)),
    )
    run_boot_id = str(run_info.get("boot_id") or "")
    current_boot_id, _current_boot_source = current_boot_identity()
    latest_boot_ids = {
        str(latest_state.get("boot_id") or ""),
        str(latest_watchdog.get("boot_id") or ""),
    }
    if (
        not run_boot_id
        or run_boot_id != current_boot_id
        or latest_boot_ids != {run_boot_id}
    ):
        raise ValueError(
            "failure-start requires recorder, State, watchdog, and marker "
            "processes to belong to the current boot"
        )
    if (
        latest_state.get("measurement_valid") is not True
        or bool((latest_state.get("state") or {}).get("error"))
    ):
        raise ValueError(
            "failure-start requires the latest State emission to be valid "
            "and error-free"
        )
    if latest_watchdog.get("worker_running") is not True:
        raise ValueError(
            "failure-start requires a live worker in the latest watchdog "
            "sample"
        )
    baseline = summarize(
        rows,
        source,
        mode=run_info.get("mode"),
        markers=markers,
        watchdog_rows=watchdog_rows,
        baseline_p95_s=run_info.get("baseline_p95_s_supplied"),
        baseline_duration_s=float(
            run_info.get("baseline_duration_s", 30.0)
        ),
        required_fresh_samples=int(
            run_info.get("required_fresh_samples", 3)
        ),
    )
    if baseline.get("baseline_health", {}).get("healthy") is not True:
        health = baseline.get("baseline_health") or {}
        raise ValueError(
            "failure-start requires a healthy baseline spanning the "
            "requested duration "
            f"(observed {health.get('observed_span_s')} s; "
            f"required {health.get('required_span_s')} s)"
        )
    required_channels = {"invalid_state", "emission_timeout"}
    link_kind = str(run_info.get("link_kind") or "none")
    if link_kind in {"serial", "usb"}:
        required_channels.add("serial_missing")
    elif link_kind in {"ethernet", "network"}:
        required_channels.add("network_interface_down")
    elif run_info.get("fault_class") == "file-feed-stop":
        required_channels.add("source_feed_stale")
    channel_health = baseline.get("channel_baseline_health") or {}
    unhealthy = sorted(
        channel
        for channel in required_channels
        if (channel_health.get(channel) or {}).get("healthy") is not True
    )
    if unhealthy:
        raise ValueError(
            "failure-start requires healthy baseline channels: "
            + ", ".join(unhealthy)
        )


def mark_event(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.expanduser().resolve()
    with _EvidenceMutex(output_dir):
        return _mark_event_locked(args, output_dir)


def _mark_event_locked(
    args: argparse.Namespace,
    output_dir: Path,
) -> int:
    run_info_path = output_dir / "run_info.json"
    if not run_info_path.is_file():
        raise FileNotFoundError(
            "run directory does not contain run_info.json; start the probe "
            f"first and verify --output-dir: {output_dir}"
        )
    if (
        (output_dir / "summary.json").is_file()
        and _RecorderLivenessLease(output_dir).is_live()
    ):
        raise RuntimeError(
            "recorder finalization is still active; retry the marker after "
            "the recorder process exits"
        )
    run_info = json.loads(run_info_path.read_text(encoding="utf-8"))
    episode_id = args.episode_id.strip()
    if args.kind != "action" and not episode_id:
        raise ValueError(f"--episode-id is required for marker kind {args.kind}")
    existing, errors = _load_markers(output_dir / "markers.jsonl")
    if errors:
        raise ValueError("; ".join(errors))
    starts = [
        marker for marker in existing
        if marker.get("kind") == "failure-start"
        and marker.get("episode_id") == episode_id
    ]
    terminal_kinds = {
        "recovery",
        "abort",
        "persistent-failure",
        "incomplete",
    }
    terminals = [
        marker for marker in existing
        if marker.get("kind") in terminal_kinds
        and marker.get("episode_id") == episode_id
    ]
    if args.kind == "failure-start":
        any_starts = [
            marker
            for marker in existing
            if marker.get("kind") == "failure-start"
        ]
        if any_starts:
            raise ValueError(
                "exactly one fault episode is allowed per run; start a new "
                "recorder directory for another failure-start"
            )
        if (
            run_info.get("fault_class") in SAFETY_GATED_FAULT_CLASSES
            and run_info.get("safe_state_confirmed") is not True
        ):
            raise ValueError(
                "manual failure marker refused: run_info does not confirm "
                "the approved safe state"
            )
        if (
            run_info.get("fault_class") == "instrument-power-loss"
            and args.action_side != "independent-observer"
        ):
            raise ValueError(
                "instrument-power-loss failure-start must be marked by the "
                "independent observer; this is still an assertion, not a "
                "power measurement"
            )
        prechecks = [
            marker
            for marker in existing
            if marker.get("kind") == "precheck-complete"
            and marker.get("episode_id") == episode_id
        ]
        if (
            run_info.get("fault_class") in SAFETY_GATED_FAULT_CLASSES
            and not prechecks
        ):
            raise ValueError(
                "manual failure marker requires a prior precheck-complete "
                f"marker for {episode_id}"
            )
        if run_info.get("fault_class") in SAFETY_GATED_FAULT_CLASSES:
            _validate_live_baseline_before_failure(
                run_info,
                output_dir,
                existing,
            )
        _validate_interface_before_failure(run_info, output_dir)
    if args.kind in terminal_kinds:
        if not starts:
            raise ValueError(
                f"{args.kind} without failure-start for {episode_id}"
            )
        if terminals:
            raise ValueError(f"duplicate terminal marker for {episode_id}")
    if args.kind == "failure-observed":
        if not starts:
            raise ValueError(
                f"failure-observed without failure-start for {episode_id}"
            )
        if terminals:
            raise ValueError(
                f"failure-observed after terminal marker for {episode_id}"
            )
    existing_kinds = [
        marker.get("kind")
        for marker in existing
        if marker.get("episode_id") == episode_id
    ]
    prerequisite = {
        "vendor-ready": "recovery",
        "gui-reconnected": "vendor-ready",
        "rearmed": "gui-reconnected",
    }.get(args.kind)
    if prerequisite and prerequisite not in existing_kinds:
        raise ValueError(
            f"{args.kind} requires a prior {prerequisite} marker for "
            f"{episode_id}"
        )
    path = output_dir / "markers.jsonl"
    marker_boot_id, marker_boot_id_source = current_boot_identity()
    row = {
        "marked_at_utc": utc_now(),
        "marked_monotonic_s": time.monotonic(),
        "marked_monotonic_ns": time.monotonic_ns(),
        "boot_id": marker_boot_id,
        "boot_id_source": marker_boot_id_source,
        "campaign_id": run_info.get("campaign_id"),
        "scenario_id": run_info.get("scenario_id"),
        "fault_class": run_info.get("fault_class"),
        "safety_tier": run_info.get("safety_tier"),
        "target": run_info.get("target"),
        "boundary": run_info.get("boundary"),
        "link_kind": run_info.get("link_kind"),
        "independent_observer_ref": run_info.get(
            "independent_observer_ref"
        ),
        "episode_id": episode_id or None,
        "kind": args.kind,
        "label": args.label,
        "note": args.note,
        "action_side": args.action_side,
        "marker_uncertainty_ms": args.marker_uncertainty_ms,
    }
    with path.open("a", encoding="utf-8") as stream:
        _append_jsonl(stream, row)
    # Keep summary and manifest synchronized when annotations are added later.
    if (output_dir / "summary.json").is_file():
        _finalize_interrupted_run_locked(
            argparse.Namespace(
                output_dir=output_dir,
                force=True,
                quiet=True,
            ),
            output_dir,
        )
    print(json.dumps(row))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Record one worker's emitted states.")
    run.add_argument(
        "--source",
        choices=tuple(VALID_MODES),
        required=True,
    )
    run.add_argument(
        "--mode",
        required=True,
        help="Validated against the selected source; dummy is never allowed.",
    )
    run.add_argument("--duration-s", type=float, default=300.0)
    run.add_argument("--poll-interval", type=float, default=1.0)
    run.add_argument("--watchdog-interval", type=float, default=0.5)
    run.add_argument(
        "--emission-timeout-s",
        type=float,
        help="Override max(2 x baseline/poll interval, 3 s).",
    )
    run.add_argument(
        "--required-fresh-samples",
        type=int,
        default=3,
        help="Consecutive valid advancing provenance samples for recovery.",
    )
    run.add_argument(
        "--baseline-duration-s",
        type=float,
        default=30.0,
        help="Healthy pre-failure window used to estimate p95 cadence.",
    )
    run.add_argument(
        "--baseline-p95-s",
        type=float,
        help="Optional healthy p95 cadence measured by the timing branch.",
    )
    run.add_argument(
        "--software-root",
        type=Path,
        default=RECORDER_ROOT,
        help=(
            "Checkout providing gui/workers.py. Point this at the WGC "
            "checkout for the WGC failure case."
        ),
    )
    run.add_argument(
        "--expected-capture-backend",
        choices=("wgc", "mss"),
        help="Camera-only guard; fail unless CameraState reports this backend.",
    )
    run.add_argument("--port", default="COM4")
    run.add_argument("--baudrate", type=int, default=115200)
    run.add_argument("--device-id", type=int, default=1)
    run.add_argument(
        "--network-interface",
        help=(
            "Optional expected adapter name paired with the interface index."
        ),
    )
    run.add_argument(
        "--network-interface-index",
        type=int,
        help=(
            "Local IPv4 interface index from `netsh interface ipv4 show "
            "interfaces`; required for Ethernet/network loss."
        ),
    )
    run.add_argument(
        "--network-probe-interval",
        type=float,
        default=2.0,
        help="Minimum seconds between local netsh adapter-state samples.",
    )
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    run.add_argument("--campaign-id", default="")
    run.add_argument("--scenario-id", default=f"scenario_{tag}")
    run.add_argument(
        "--fault-class",
        choices=tuple(FAULT_CLASSES),
        default="window-source-loss",
    )
    run.add_argument(
        "--link-kind",
        choices=(
            "none",
            "serial",
            "usb",
            "ethernet",
            "network",
            "file",
            "video",
        ),
        default="none",
    )
    run.add_argument("--target", default="")
    run.add_argument("--boundary", default="")
    run.add_argument("--operator", default="")
    run.add_argument("--observer", default="")
    run.add_argument("--approver", default="")
    run.add_argument("--authorization-ref", default="")
    run.add_argument("--sop-ref", default="")
    run.add_argument("--safe-state-note", default="")
    run.add_argument("--abort-criteria", default="")
    run.add_argument("--independent-observer-ref", default="")
    run.add_argument(
        "--confirm-safe-state",
        action="store_true",
        help=(
            "Record that the operator confirmed an approved maintenance-safe "
            "state; this does not authorize or perform a physical action."
        ),
    )
    run.add_argument(
        "--output-dir",
        type=Path,
        default=Path("logs") / "validation" / f"failure_probe_{tag}",
    )
    run.set_defaults(handler=run_probe)

    finalize = commands.add_parser(
        "finalize",
        help="Salvage fsynced evidence after process/PC interruption.",
    )
    finalize.add_argument("--output-dir", type=Path, required=True)
    finalize.add_argument(
        "--force",
        action="store_true",
        help="Recompute an existing summary after post-run markers.",
    )
    finalize.set_defaults(handler=finalize_interrupted_run)

    mark = commands.add_parser("mark", help="Timestamp an operator action.")
    mark.add_argument("--output-dir", type=Path, required=True)
    mark.add_argument(
        "--kind",
        choices=MARKER_KINDS,
        default="action",
    )
    mark.add_argument("--episode-id", default="")
    mark.add_argument("--label", required=True)
    mark.add_argument("--note", default="")
    mark.add_argument(
        "--action-side",
        choices=("operator", "observer", "independent-observer"),
        default="operator",
    )
    mark.add_argument("--marker-uncertainty-ms", type=float, default=0.0)
    mark.set_defaults(handler=mark_event)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "duration_s", 1.0) <= 0:
        parser.error("--duration-s must be positive")
    if getattr(args, "poll_interval", 1.0) <= 0:
        parser.error("--poll-interval must be positive")
    if getattr(args, "watchdog_interval", 1.0) <= 0:
        parser.error("--watchdog-interval must be positive")
    if getattr(args, "network_probe_interval", 1.0) <= 0:
        parser.error("--network-probe-interval must be positive")
    emission_timeout = getattr(args, "emission_timeout_s", None)
    if emission_timeout is not None and emission_timeout <= 0:
        parser.error("--emission-timeout-s must be positive")
    if getattr(args, "required_fresh_samples", 1) < 1:
        parser.error("--required-fresh-samples must be >= 1")
    if getattr(args, "marker_uncertainty_ms", 0.0) < 0:
        parser.error("--marker-uncertainty-ms must be >= 0")
    if getattr(args, "baseline_duration_s", 1.0) <= 0:
        parser.error("--baseline-duration-s must be positive")
    supplied_p95 = getattr(args, "baseline_p95_s", None)
    if supplied_p95 is not None and supplied_p95 <= 0:
        parser.error("--baseline-p95-s must be positive")
    if getattr(args, "expected_capture_backend", None) and (
        getattr(args, "source", None) != "camera"
    ):
        parser.error("--expected-capture-backend is camera-only")
    try:
        if getattr(args, "command", "") == "run":
            validate_source_mode(args.source, args.mode)
            validate_fault_plan(args)
        return int(args.handler(args))
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
