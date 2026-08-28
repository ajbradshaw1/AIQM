"""Read-only Bulbasaur/O-MBE interface cross-check runner.

This script is deliberately separate from the production GUI.  It observes one
interface at a time, records the evidence needed for later review, and never
calls an instrument setter.  It also never starts, stops, moves, minimizes, or
closes vendor applications.

Raw samples and OCR crops default to ``logs/ombe_interface_crosscheck`` (which
is gitignored).  Every run receives a unique directory containing:

* ``run_metadata.json`` -- environment, commit, command, and requested settings
* ``samples.jsonl`` -- one factual record per attempted sample
* ``summary.json`` / ``summary.csv`` -- small aggregate summaries
* ``SHA256SUMS.txt`` -- hashes for every other artifact in the run directory

Short smoke-test command shapes (use ``--duration-s 5``). For planned
ten-minute runs, use the durable launcher commands in
``docs/validation/ombe_interface_crosscheck_report_template.md``. Run Exactus
and Modbus separately because they contend for the same serial port:

    python scripts/ombe_interface_crosscheck.py pyrometer exactus --duration-s 5
    python scripts/ombe_interface_crosscheck.py pyrometer modbus --duration-s 5
    python scripts/ombe_interface_crosscheck.py pyrometer temperasure --duration-s 5
    python scripts/ombe_interface_crosscheck.py evap-paired --duration-s 5
    python scripts/ombe_interface_crosscheck.py mistral-ocr --duration-s 5
    python scripts/ombe_interface_crosscheck.py jsonrpc-diagnose
    python scripts/ombe_interface_crosscheck.py audit-modbus-config
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import math
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "logs" / "ombe_interface_crosscheck"
PLANNED_MINIMUM_DURATION_S = 600.0
SCHEMA_VERSION = 1
MISTRAL_KEYS = ("v_set", "v_actual", "i_set", "i_actual")
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")

EVAP_PRESSURE_REGEX = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*[eE]\s*([+-]?\d+)"
)
EVAP_PRESSURE_RANGE_MBAR = (1e-12, 1e-2)
MISTRAL_REGEX = {
    "v_set": re.compile(r"Set\s*Voltage\s*:\s*([-\d.]+)", re.IGNORECASE),
    "i_set": re.compile(r"Set\s*Current\s*:\s*([-\d.]+)", re.IGNORECASE),
    "v_actual": re.compile(
        r"Actual\s*Voltage\s*:\s*([-\d.]+)", re.IGNORECASE
    ),
    "i_actual": re.compile(
        r"Actual\s*Current\s*:\s*([-\d.]+)", re.IGNORECASE
    ),
}


def _serial_mutex_name(port: str) -> str:
    port_key = hashlib.sha256(port.strip().casefold().encode("utf-8")).hexdigest()
    return f"Local\\AIQM_OmbeInterfaceSerial_{port_key[:16]}"


class _SerialPortMutex:
    """Fail fast when another diagnostic runner is using the same port."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, port: Optional[str]):
        self.port = port
        self.name = _serial_mutex_name(port) if port else None
        self._handle: Any = None
        self._kernel32: Any = None

    def __enter__(self) -> "_SerialPortMutex":
        if os.name != "nt" or not self.name:
            return self
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
                f"another interface cross-check owns the diagnostic lock "
                f"for {self.port}; wait for it to finish"
            )
        self._kernel32 = kernel32
        self._handle = handle
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        if self._handle is None or self._kernel32 is None:
            return
        self._kernel32.ReleaseMutex(self._handle)
        self._kernel32.CloseHandle(self._handle)
        self._handle = None
        self._kernel32 = None


def utc_now_iso() -> str:
    """Return a millisecond-resolution, timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso_offset_ms(later: Optional[str], earlier: Optional[str]) -> Optional[float]:
    if not later or not earlier:
        return None
    try:
        later_dt = datetime.fromisoformat(later)
        earlier_dt = datetime.fromisoformat(earlier)
    except (TypeError, ValueError):
        return None
    if later_dt.tzinfo is None or earlier_dt.tzinfo is None:
        return None
    return round((later_dt - earlier_dt).total_seconds() * 1000.0, 3)


def _jsonable(value: Any) -> Any:
    """Convert values to strict JSON without inventing replacements."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    # numpy scalar support without importing numpy in platform-neutral tests.
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    return repr(value)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        path,
        json.dumps(_jsonable(payload), indent=2, sort_keys=True, allow_nan=False)
        + "\n",
    )


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace a small evidence file only after its contents reach disk."""
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _git_commit(repo_root: Path = REPO_ROOT) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_sha256_manifest(run_dir: Path) -> Path:
    """Hash all run artifacts except the manifest itself."""
    manifest = run_dir / "SHA256SUMS.txt"
    entries: list[str] = []
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file()):
        if path == manifest:
            continue
        relative = path.relative_to(run_dir).as_posix()
        entries.append(f"{_sha256(path)}  {relative}")
    _atomic_write_text(
        manifest, "\n".join(entries) + ("\n" if entries else "")
    )
    return manifest


def _flatten_summary(
    value: Any, prefix: str = "", out: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    out = {} if out is None else out
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            _flatten_summary(child, name, out)
    elif isinstance(value, (list, tuple)):
        out[prefix] = json.dumps(_jsonable(value), separators=(",", ":"))
    else:
        out[prefix] = _jsonable(value)
    return out


def write_summary_csv(path: Path, summary: dict[str, Any]) -> None:
    flat = _flatten_summary(summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(flat))
            writer.writeheader()
            writer.writerow(flat)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


class RunArtifacts:
    """Unique, append-only evidence directory for one command invocation."""

    def __init__(
        self,
        output_root: Path,
        label: str,
        parameters: dict[str, Any],
        run_id: Optional[str] = None,
    ):
        if run_id is not None and not SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError(
                "run-id may contain only letters, digits, dot, underscore, hyphen"
            )
        if run_id is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_id = f"{stamp}_{label}_{uuid.uuid4().hex[:8]}"
        self.run_dir = Path(output_root).resolve() / run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.samples_path = self.run_dir / "samples.jsonl"
        self.started_at_utc = utc_now_iso()
        self.metadata: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "label": label,
            "started_at_utc": self.started_at_utc,
            "git_commit": _git_commit(),
            "repository": str(REPO_ROOT),
            "raw_artifact_path": str(self.run_dir),
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version,
            "command_line": sys.argv,
            "parameters": parameters,
            "safety_contract": {
                "read_only": True,
                "instrument_setters_called": False,
                "vendor_apps_started_or_closed": False,
                "vendor_windows_moved_or_minimized": False,
            },
        }
        write_json(self.run_dir / "run_metadata.json", self.metadata)

    def append_sample(self, sample: dict[str, Any]) -> None:
        with self.samples_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    _jsonable(sample), sort_keys=True, allow_nan=False
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())

    def finish(self, summary: dict[str, Any]) -> dict[str, Any]:
        completed_at = utc_now_iso()
        final = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.metadata["run_id"],
            "label": self.metadata["label"],
            "started_at_utc": self.started_at_utc,
            "completed_at_utc": completed_at,
            **summary,
        }
        write_json(self.run_dir / "summary.json", final)
        write_summary_csv(self.run_dir / "summary.csv", final)
        self.metadata["completed_at_utc"] = completed_at
        write_json(self.run_dir / "run_metadata.json", self.metadata)
        write_sha256_manifest(self.run_dir)
        return final


@dataclass
class CollectionResult:
    records: list[dict[str, Any]]
    interrupted: bool
    elapsed_s: float


def collect_timed(
    read_one: Callable[[int], dict[str, Any]],
    duration_s: float,
    interval_s: float,
    emit: Callable[[dict[str, Any]], None],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    utc_now: Callable[[], str] = utc_now_iso,
) -> CollectionResult:
    """Collect on a monotonic schedule; exceptions become factual records."""
    if duration_s <= 0:
        raise ValueError("duration-s must be > 0")
    if interval_s <= 0:
        raise ValueError("interval-s must be > 0")

    records: list[dict[str, Any]] = []
    started = clock()
    sequence = 0
    interrupted = False
    try:
        while True:
            due = started + sequence * interval_s
            if due >= started + duration_s:
                break
            now = clock()
            if now < due:
                sleep(due - now)

            read_started = clock()
            record: dict[str, Any] = {
                "sequence": sequence,
                "attempted_at_utc": utc_now(),
                "attempted_monotonic_s": read_started,
            }
            try:
                payload = read_one(sequence)
                record.update(payload)
                record["read_ok"] = True
                record["read_error"] = None
            except Exception as exc:  # diagnostic runner must retain failures
                record["read_ok"] = False
                record["read_error"] = f"{type(exc).__name__}: {exc}"
            record["read_duration_ms"] = round(
                (clock() - read_started) * 1000.0, 3
            )
            emit(record)
            records.append(record)
            sequence += 1
    except KeyboardInterrupt:
        interrupted = True
    return CollectionResult(
        records=records,
        interrupted=interrupted,
        elapsed_s=max(0.0, clock() - started),
    )


def _percentile(values: Iterable[float], percentile: float) -> Optional[float]:
    ordered = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def summarize_collection(result: CollectionResult) -> dict[str, Any]:
    durations = [
        float(r["read_duration_ms"])
        for r in result.records
        if r.get("read_duration_ms") is not None
    ]
    successes = sum(bool(r.get("read_ok")) for r in result.records)
    attempted_monotonic = [
        float(r["attempted_monotonic_s"])
        for r in result.records
        if r.get("attempted_monotonic_s") is not None
    ]
    intervals_ms = [
        (later - earlier) * 1000.0
        for earlier, later in zip(
            attempted_monotonic, attempted_monotonic[1:]
        )
    ]
    return {
        "run_status": "interrupted" if result.interrupted else "completed",
        "sample_attempts": len(result.records),
        "successful_sample_calls": successes,
        "failed_sample_calls": len(result.records) - successes,
        "elapsed_s": round(result.elapsed_s, 3),
        "read_duration_ms": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "p99": _percentile(durations, 0.99),
        },
        "actual_sample_interval_ms": {
            "p50": _percentile(intervals_ms, 0.50),
            "p95": _percentile(intervals_ms, 0.95),
            "p99": _percentile(intervals_ms, 0.99),
            "min": min(intervals_ms) if intervals_ms else None,
            "max": max(intervals_ms) if intervals_ms else None,
        },
    }


def _planned_duration_completed(
    result: CollectionResult, duration_s: float, interval_s: float
) -> bool:
    minimum_elapsed = max(0.0, duration_s - interval_s)
    return not result.interrupted and result.elapsed_s >= minimum_elapsed


def probe_pyrometer(
    sensor: Any,
    *,
    mode: str,
    duration_s: float,
    interval_s: float,
    samples_per_poll: int,
    emit: Callable[[dict[str, Any]], None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    utc_now: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
    """Run one sensor without invoking any optional setters."""
    if samples_per_poll < 1:
        raise ValueError("samples-per-poll must be >= 1")
    device_info: dict[str, Any] = {}
    connected = False
    connection_error: Optional[str] = None
    disconnect_error: Optional[str] = None
    result = CollectionResult([], False, 0.0)
    try:
        sensor.connect()
        connected = True
        if hasattr(sensor, "get_info"):
            try:
                device_info = _jsonable(sensor.get_info())
            except Exception as exc:
                device_info = {"read_error": f"{type(exc).__name__}: {exc}"}

        def read_one(_sequence: int) -> dict[str, Any]:
            readings = [
                float(sensor.read_temperature())
                for _ in range(samples_per_poll)
            ]
            sample: dict[str, Any] = {
                "temperature_C_readings": readings,
                "temperature_C_mean": statistics.fmean(readings),
                "temperature_C_std": (
                    statistics.pstdev(readings) if len(readings) > 1 else 0.0
                ),
                "temperature_n": len(readings),
            }
            if hasattr(sensor, "read_emissivity"):
                try:
                    sample["emissivity"] = sensor.read_emissivity()
                    sample["emissivity_error"] = None
                except Exception as exc:
                    sample["emissivity"] = None
                    sample["emissivity_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
            return sample

        result = collect_timed(
            read_one,
            duration_s,
            interval_s,
            emit,
            clock=clock,
            sleep=sleep,
            utc_now=utc_now,
        )
    except Exception as exc:
        connection_error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            sensor.disconnect()
        except Exception as exc:
            disconnect_error = f"{type(exc).__name__}: {exc}"

    summary = summarize_collection(result)
    finite_readings = [
        float(value)
        for record in result.records
        for value in record.get("temperature_C_readings", [])
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    valid_sample_count = sum(
        isinstance(record.get("temperature_C_mean"), (int, float))
        and math.isfinite(float(record["temperature_C_mean"]))
        for record in result.records
    )
    if connection_error:
        summary["run_status"] = "setup_error"
    elif valid_sample_count == 0 and not result.interrupted:
        summary["run_status"] = "completed_no_valid_data"
    summary.update(
        {
            "probe": "pyrometer",
            "mode": mode,
            "connected": connected,
            "connection_error": connection_error,
            "disconnect_error": disconnect_error,
            "device_info": device_info,
            "valid_numeric_samples": valid_sample_count,
            "valid_temperature_readings": len(finite_readings),
            "temperature_C": {
                "min": min(finite_readings) if finite_readings else None,
                "max": max(finite_readings) if finite_readings else None,
            },
            "samples_per_poll": samples_per_poll,
            "requested_duration_s": duration_s,
            "requested_interval_s": interval_s,
            "meets_planned_ten_minute_request": (
                duration_s >= PLANNED_MINIMUM_DURATION_S
                and _planned_duration_completed(
                    result, duration_s, interval_s
                )
                and connection_error is None
                and valid_sample_count > 0
            ),
        }
    )
    return summary


def make_pyrometer_sensor(args: argparse.Namespace) -> Any:
    """Build only read-capable drivers; auto-start is explicitly disabled."""
    if args.mode == "exactus":
        from drivers.pyrometer import ExactusSerialPyrometer

        return ExactusSerialPyrometer(
            port=args.port,
            baudrate=args.baudrate,
        )
    if args.mode == "modbus":
        from drivers.pyrometer import ModbusPyrometer

        return ModbusPyrometer(
            port=args.port,
            baudrate=args.baudrate,
            device_id=args.device_id,
        )
    if args.mode == "temperasure":
        from drivers.pyrometer import ScreenGrabPyrometer

        return ScreenGrabPyrometer(
            window_title=args.window_title,
            auto_start=False,
        )
    raise ValueError(f"unsupported pyrometer mode: {args.mode}")


def parse_evap_pressure_text(text: str) -> Optional[float]:
    match = EVAP_PRESSURE_REGEX.search(text or "")
    if not match:
        return None
    try:
        value = float(match.group(1)) * (10 ** int(match.group(2)))
    except (ValueError, OverflowError):
        return None
    lo, hi = EVAP_PRESSURE_RANGE_MBAR
    return value if lo <= value <= hi else None


def parse_mistral_text(text: str) -> dict[str, Optional[float]]:
    result: dict[str, Optional[float]] = {key: None for key in MISTRAL_KEYS}
    for key, regex in MISTRAL_REGEX.items():
        match = regex.search(text or "")
        if not match:
            continue
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        lo, hi = (-1.0, 500.0) if key.startswith("v_") else (-1.0, 100.0)
        if lo <= value <= hi:
            result[key] = value
    return result


def scale_bbox(
    bbox: tuple[int, int, int, int],
    calibration_size: tuple[int, int],
    frame: Any,
) -> tuple[int, int, int, int]:
    height, width = frame.shape[:2]
    cal_width, cal_height = calibration_size
    x, y, crop_width, crop_height = bbox
    return (
        round(x * width / cal_width),
        round(y * height / cal_height),
        round(crop_width * width / cal_width),
        round(crop_height * height / cal_height),
    )


def crop_frame(frame: Any, bbox: tuple[int, int, int, int]) -> Any:
    x, y, width, height = bbox
    crop = frame[y : y + height, x : x + width]
    if getattr(crop, "size", 0) == 0:
        raise RuntimeError(f"empty crop for bbox={bbox}, frame={frame.shape}")
    return crop


def save_rgb_png(image: Any, path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(image).save(path)


def get_window_metadata(hwnd: int) -> dict[str, Any]:
    """Read HWND geometry and DPI without changing window state."""
    if os.name != "nt":
        raise RuntimeError("window metadata is available only on Windows")
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    user32.GetWindowRect.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.POINTER(ctypes.wintypes.RECT),
    ]
    user32.GetWindowRect.restype = ctypes.wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindowVisible.restype = ctypes.wintypes.BOOL
    user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
    user32.IsIconic.restype = ctypes.wintypes.BOOL

    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError(f"GetWindowRect failed for HWND {hwnd}")
    title_len = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(title_len + 1)
    user32.GetWindowTextW(hwnd, buffer, title_len + 1)
    dpi: Optional[int] = None
    try:
        get_dpi = user32.GetDpiForWindow
        get_dpi.argtypes = [ctypes.wintypes.HWND]
        get_dpi.restype = ctypes.c_uint
        dpi_value = int(get_dpi(hwnd))
        dpi = dpi_value or None
    except (AttributeError, OSError):
        pass
    return {
        "hwnd": int(hwnd),
        "title": buffer.value,
        "rect": {
            "left": int(rect.left),
            "top": int(rect.top),
            "right": int(rect.right),
            "bottom": int(rect.bottom),
        },
        "window_width_px": int(rect.right - rect.left),
        "window_height_px": int(rect.bottom - rect.top),
        "dpi": dpi,
        "dpi_scale_percent": round(dpi / 96.0 * 100.0, 2) if dpi else None,
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "minimized": bool(user32.IsIconic(hwnd)),
    }


def acquire_evap_pair_sample(
    sequence: int,
    *,
    hwnd: int,
    crop_dir: Path,
    capture_fn: Callable[[int], Any],
    window_metadata_fn: Callable[[int], dict[str, Any]],
    bbox_fn: Callable[[Any], tuple[int, int, int, int]],
    ocr_fn: Callable[[Any], str],
    save_crop_fn: Callable[[Any, Path], None],
    elog_fn: Callable[[], dict[str, Any]],
    utc_now: Callable[[], str] = utc_now_iso,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> dict[str, Any]:
    """Acquire both sources independently so one failure does not hide the other."""
    record: dict[str, Any] = {
        "probe": "evap_paired",
        "pair_sequence": sequence,
        "elog_ok": False,
        "elog_error": None,
        "ocr_ok": False,
        "ocr_error": None,
    }
    elog_started_ns = monotonic_ns()
    record["elog_attempted_at_utc"] = utc_now()
    try:
        record.update(elog_fn())
        record["elog_ok"] = True
    except Exception as exc:
        record["elog_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        elog_completed_ns = monotonic_ns()
        record["elog_completed_at_utc"] = utc_now()
        record["elog_read_duration_ms"] = round(
            (elog_completed_ns - elog_started_ns) / 1_000_000.0, 3
        )
        if record["elog_ok"]:
            record["elog_received_at_utc"] = record["elog_completed_at_utc"]
            record["elog_received_monotonic_ns"] = elog_completed_ns

    ocr_started_ns = monotonic_ns()
    record["ocr_attempted_at_utc"] = utc_now()
    try:
        metadata = window_metadata_fn(hwnd)
        record["window"] = metadata
        frame = capture_fn(hwnd)
        frame_received_ns = monotonic_ns()
        frame_received_at_utc = utc_now()
        record.update(
            {
                "frame_size_px": {
                    "width": int(frame.shape[1]),
                    "height": int(frame.shape[0]),
                },
                "ocr_frame_received_at_utc": frame_received_at_utc,
                "ocr_frame_received_monotonic_ns": frame_received_ns,
                "ocr_capture_duration_ms": round(
                    (frame_received_ns - ocr_started_ns) / 1_000_000.0, 3
                ),
            }
        )
        bbox = bbox_fn(frame)
        record["ocr_bbox_px"] = list(bbox)
        crop = crop_frame(frame, bbox)
        crop_path = crop_dir / f"evap_{sequence:06d}.png"
        record["ocr_crop_path"] = str(crop_path)
        record["ocr_crop_saved"] = False
        save_crop_fn(crop, crop_path)
        record["ocr_crop_saved"] = True
        raw_text = ocr_fn(crop)
        record.update(
            {
                "ocr_text": raw_text,
                "ocr_text_nonempty": bool(raw_text),
                "ocr_pressure_mbar": parse_evap_pressure_text(raw_text),
            }
        )
        record["ocr_ok"] = bool(raw_text)
        if not raw_text:
            record["ocr_error"] = "empty OCR text"
    except Exception as exc:
        record["ocr_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        ocr_completed_ns = monotonic_ns()
        record["ocr_completed_at_utc"] = utc_now()
        record["ocr_pipeline_duration_ms"] = round(
            (ocr_completed_ns - ocr_started_ns) / 1_000_000.0, 3
        )

    elog_received_ns = record.get("elog_received_monotonic_ns")
    frame_received_ns = record.get("ocr_frame_received_monotonic_ns")
    if isinstance(elog_received_ns, int) and isinstance(frame_received_ns, int):
        record["pair_receive_offset_ms"] = round(
            (frame_received_ns - elog_received_ns) / 1_000_000.0, 3
        )
    else:
        record["pair_receive_offset_ms"] = None
    record["elog_source_age_at_ocr_ms"] = _iso_offset_ms(
        record.get("ocr_frame_received_at_utc"),
        record.get("elog_source_at_utc"),
    )

    elog_value = record.get("elog_pressure_mbar")
    ocr_value = record.get("ocr_pressure_mbar")
    pressure_lo, pressure_hi = EVAP_PRESSURE_RANGE_MBAR
    elog_valid = (
        isinstance(elog_value, (int, float))
        and math.isfinite(float(elog_value))
        and pressure_lo <= float(elog_value) <= pressure_hi
    )
    ocr_valid = (
        isinstance(ocr_value, (int, float))
        and math.isfinite(float(ocr_value))
        and pressure_lo <= float(ocr_value) <= pressure_hi
    )
    record["elog_pressure_valid"] = elog_valid
    record["ocr_pressure_valid"] = ocr_valid
    record["paired_pressure_valid"] = elog_valid and ocr_valid
    if record["paired_pressure_valid"]:
        record["pressure_difference_mbar"] = float(ocr_value) - float(elog_value)
        record["pressure_absolute_difference_mbar"] = abs(
            float(ocr_value) - float(elog_value)
        )
        record["pressure_ratio_ocr_over_elog"] = (
            float(ocr_value) / float(elog_value) if float(elog_value) != 0 else None
        )
    else:
        record["pressure_difference_mbar"] = None
        record["pressure_absolute_difference_mbar"] = None
        record["pressure_ratio_ocr_over_elog"] = None
    return record


def acquire_mistral_sample(
    sequence: int,
    *,
    hwnd: int,
    crop_dir: Path,
    capture_fn: Callable[[int], Any],
    window_metadata_fn: Callable[[int], dict[str, Any]],
    bbox_fn: Callable[[Any], tuple[int, int, int, int]],
    ocr_fn: Callable[[Any], str],
    save_crop_fn: Callable[[Any, Path], None],
    utc_now: Callable[[], str] = utc_now_iso,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> dict[str, Any]:
    ocr_started_ns = monotonic_ns()
    attempted_at_utc = utc_now()
    metadata = window_metadata_fn(hwnd)
    frame = capture_fn(hwnd)
    frame_received_ns = monotonic_ns()
    frame_received_at_utc = utc_now()
    bbox = bbox_fn(frame)
    crop = crop_frame(frame, bbox)
    crop_path = crop_dir / f"mistral_{sequence:06d}.png"
    save_crop_fn(crop, crop_path)
    values: dict[str, Optional[float]] = {
        key: None for key in MISTRAL_KEYS
    }
    record: dict[str, Any] = {
        "probe": "mistral_ocr",
        "ocr_attempted_at_utc": attempted_at_utc,
        "ocr_frame_received_at_utc": frame_received_at_utc,
        "ocr_frame_received_monotonic_ns": frame_received_ns,
        "ocr_capture_duration_ms": round(
            (frame_received_ns - ocr_started_ns) / 1_000_000.0, 3
        ),
        "window": metadata,
        "frame_size_px": {
            "width": int(frame.shape[1]),
            "height": int(frame.shape[0]),
        },
        "ocr_bbox_px": list(bbox),
        "ocr_crop_path": str(crop_path),
        "ocr_crop_saved": True,
        "ocr_ok": False,
        "ocr_error": None,
        "ocr_text": None,
        "ocr_text_nonempty": False,
    }
    try:
        raw_text = ocr_fn(crop)
        values = parse_mistral_text(raw_text)
        record.update(
            {
                "ocr_text": raw_text,
                "ocr_text_nonempty": bool(raw_text),
                "ocr_ok": bool(raw_text),
                "ocr_error": None if raw_text else "empty OCR text",
            }
        )
    except Exception as exc:
        record["ocr_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        ocr_completed_ns = monotonic_ns()
        record["ocr_completed_at_utc"] = utc_now()
        record["ocr_pipeline_duration_ms"] = round(
            (ocr_completed_ns - ocr_started_ns) / 1_000_000.0, 3
        )
    record.update(values)
    record["parsed_field_count"] = sum(
        value is not None for value in values.values()
    )
    record["all_fields_none"] = all(
        value is None for value in values.values()
    )
    return record


def diagnose_jsonrpc(
    client: Any,
    *,
    attempt_discovery: bool = True,
) -> dict[str, Any]:
    """Observe reachability and all-None behavior using read-only methods."""
    result: dict[str, Any] = {
        "probe": "mistral_jsonrpc",
        "connected": False,
        "connection_error": None,
        "values": {key: None for key in MISTRAL_KEYS},
        "read_error": None,
        "read_config": {},
        "all_values_none": None,
        "discovery_attempted": attempt_discovery,
        "discovery": {},
        "discovery_error": None,
        "disconnect_error": None,
    }
    try:
        client.connect()
        result["connected"] = bool(client.connected)
        try:
            result["read_config"] = _jsonable(client.get_read_config())
        except Exception as exc:
            result["read_config_error"] = f"{type(exc).__name__}: {exc}"
        try:
            values = client.read()
            result["values"] = {key: values.get(key) for key in MISTRAL_KEYS}
            result["all_values_none"] = all(
                result["values"][key] is None for key in MISTRAL_KEYS
            )
        except Exception as exc:
            result["read_error"] = f"{type(exc).__name__}: {exc}"
        if attempt_discovery:
            try:
                result["discovery"] = _jsonable(
                    client.try_standard_discovery()
                )
            except Exception as exc:
                result["discovery_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        result["connection_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            client.disconnect()
        except Exception as exc:
            result["disconnect_error"] = f"{type(exc).__name__}: {exc}"

    config_empty = not bool(result.get("read_config"))
    if not result["connected"]:
        result["diagnosis"] = "unreachable_or_connect_failed"
    elif result["read_error"]:
        result["diagnosis"] = "connected_but_read_failed"
    elif result["all_values_none"] and config_empty:
        result["diagnosis"] = "connected_with_empty_read_config_and_all_none"
    elif result["all_values_none"]:
        result["diagnosis"] = "connected_with_config_but_all_none"
    else:
        result["diagnosis"] = "one_or_more_values_available"
    return result


def _find_class_method(
    source: str, class_name: str, method_name: str
) -> ast.FunctionDef:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == method_name
                ):
                    return child
    raise ValueError(f"{class_name}.{method_name} not found")


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == name
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        )
    ]


def _call_evidence(calls: list[ast.Call]) -> list[dict[str, Any]]:
    return [
        {
            "line": call.lineno,
            "keywords": sorted(
                keyword.arg for keyword in call.keywords if keyword.arg
            ),
            "keyword_expressions": {
                str(keyword.arg): ast.unparse(keyword.value)
                for keyword in call.keywords
                if keyword.arg
            },
            "positional_argument_count": len(call.args),
        }
        for call in calls
    ]


def _call_forwards(
    calls: list[ast.Call], expected: dict[str, set[str]]
) -> bool:
    for call in calls:
        expressions = {
            str(keyword.arg): ast.unparse(keyword.value)
            for keyword in call.keywords
            if keyword.arg
        }
        if all(expressions.get(name) in allowed for name, allowed in expected.items()):
            return True
    return False


def _argument_forwarded(
    calls: list[ast.Call], name: str, allowed: set[str]
) -> bool:
    return _call_forwards(calls, {name: allowed})


def _constructor_defaults(source: str, class_name: str) -> dict[str, Any]:
    init = _find_class_method(source, class_name, "__init__")
    positional = list(init.args.args)
    defaults = list(init.args.defaults)
    first_default = len(positional) - len(defaults)
    result: dict[str, Any] = {}
    for index, arg in enumerate(positional):
        if index < first_default:
            continue
        try:
            result[arg.arg] = ast.literal_eval(defaults[index - first_default])
        except (ValueError, TypeError):
            result[arg.arg] = ast.unparse(defaults[index - first_default])
    return result


def audit_modbus_configuration(
    repo_root: Path,
    *,
    sources: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Trace GUI -> worker -> driver constructor settings with AST evidence."""
    paths = {
        "growth_app": repo_root / "gui" / "growth_app.py",
        "workers": repo_root / "gui" / "workers.py",
        "pyrometer": repo_root / "drivers" / "pyrometer.py",
    }
    source_text = (
        sources
        if sources is not None
        else {key: path.read_text(encoding="utf-8") for key, path in paths.items()}
    )

    app_tree = ast.parse(source_text["growth_app"])
    gui_worker_calls = _calls_named(app_tree, "PyrometerWorker")
    worker_method = _find_class_method(
        source_text["workers"], "PyrometerWorker", "_create_sensor"
    )
    modbus_calls = _calls_named(worker_method, "ModbusPyrometer")
    exactus_calls = _calls_named(worker_method, "ExactusSerialPyrometer")
    defaults = _constructor_defaults(
        source_text["pyrometer"], "ModbusPyrometer"
    )
    gui_expected = {
        "port": {"port", "exactus_port", "self.port"},
        "baudrate": {
            "baud",
            "baudrate",
            "exactus_baud",
            "self.baudrate",
        },
    }
    worker_expected = {
        "port": {"self.port"},
        "baudrate": {"self.baudrate"},
    }
    gui_passes_both = _call_forwards(gui_worker_calls, gui_expected)
    modbus_receives_both = _call_forwards(modbus_calls, worker_expected)
    exactus_receives_both = _call_forwards(exactus_calls, worker_expected)

    if gui_passes_both and not modbus_receives_both:
        finding = "gui_values_not_forwarded_to_modbus_driver"
    elif gui_passes_both and modbus_receives_both:
        finding = "gui_values_forwarded_to_modbus_driver"
    else:
        finding = "configuration_route_incomplete_or_unrecognized"

    return {
        "probe": "modbus_configuration_audit",
        "audit_type": "static_source_observation",
        "finding": finding,
        "gui_to_worker": {
            "passes_port": _argument_forwarded(
                gui_worker_calls, "port", gui_expected["port"]
            ),
            "passes_baudrate": _argument_forwarded(
                gui_worker_calls, "baudrate", gui_expected["baudrate"]
            ),
            "route_complete": gui_passes_both,
            "evidence": _call_evidence(gui_worker_calls),
        },
        "worker_to_modbus": {
            "passes_port": _argument_forwarded(
                modbus_calls, "port", worker_expected["port"]
            ),
            "passes_baudrate": _argument_forwarded(
                modbus_calls, "baudrate", worker_expected["baudrate"]
            ),
            "route_complete": modbus_receives_both,
            "evidence": _call_evidence(modbus_calls),
        },
        "worker_to_exactus": {
            "passes_port": _argument_forwarded(
                exactus_calls, "port", worker_expected["port"]
            ),
            "passes_baudrate": _argument_forwarded(
                exactus_calls, "baudrate", worker_expected["baudrate"]
            ),
            "route_complete": exactus_receives_both,
            "evidence": _call_evidence(exactus_calls),
        },
        "modbus_driver_constructor_defaults": defaults,
        "observed_effective_behavior": (
            "The GUI values reach PyrometerWorker, but the current Modbus "
            "factory call uses the ModbusPyrometer constructor defaults."
            if finding == "gui_values_not_forwarded_to_modbus_driver"
            else "See the structured route flags; no mismatch is asserted."
        ),
        "source_files": {
            key: {
                "path": str(paths[key]),
                "sha256": (
                    _sha256(paths[key])
                    if sources is None and paths[key].exists()
                    else hashlib.sha256(source_text[key].encode("utf-8")).hexdigest()
                ),
            }
            for key in paths
        },
        "exactus_route_complete": exactus_receives_both,
    }


def _reported_context(args: argparse.Namespace) -> dict[str, Any]:
    versions: dict[str, str] = {}
    for item in getattr(args, "software_version", []) or []:
        name, separator, version = item.partition("=")
        name = name.strip()
        version = version.strip()
        if not separator or not name or not version:
            raise ValueError(
                "--software-version must use NAME=VERSION; it may be repeated"
            )
        versions[name] = version
    return {
        "operator": getattr(args, "operator", None),
        "reported_software_versions": versions,
    }


def _exit_code_for_summary(summary: dict[str, Any]) -> int:
    status = summary.get("run_status")
    if status == "completed":
        return 0
    if status == "setup_error":
        return 1
    if status == "interrupted":
        return 130
    return 3


def _run_pyrometer(args: argparse.Namespace) -> int:
    parameters = {
        **_reported_context(args),
        "mode": args.mode,
        "duration_s": args.duration_s,
        "interval_s": args.interval_s,
        "samples_per_poll": args.samples_per_poll,
        "port": args.port if args.mode in {"exactus", "modbus"} else None,
        "baudrate": args.baudrate if args.mode in {"exactus", "modbus"} else None,
        "device_id": args.device_id if args.mode == "modbus" else None,
        "window_title": args.window_title if args.mode == "temperasure" else None,
        "serial_runner_mutex": (
            _serial_mutex_name(args.port)
            if args.mode in {"exactus", "modbus"}
            else None
        ),
    }
    artifacts = RunArtifacts(
        args.output_root, f"pyrometer_{args.mode}", parameters, args.run_id
    )
    if args.mode in {"exactus", "modbus"}:
        print(
            "Read-only serial run: close TemperaSure manually if it owns the "
            "port. This script will not close it or change instrument settings.",
            flush=True,
        )
    serial_port = args.port if args.mode in {"exactus", "modbus"} else None
    try:
        with _SerialPortMutex(serial_port):
            sensor = make_pyrometer_sensor(args)
            summary = probe_pyrometer(
                sensor,
                mode=args.mode,
                duration_s=args.duration_s,
                interval_s=args.interval_s,
                samples_per_poll=args.samples_per_poll,
                emit=artifacts.append_sample,
            )
    except Exception as exc:
        summary = {
            "run_status": "setup_error",
            "probe": "pyrometer",
            "mode": args.mode,
            "connection_error": f"{type(exc).__name__}: {exc}",
            "sample_attempts": 0,
        }
    final = artifacts.finish(summary)
    print(json.dumps({"run_dir": str(artifacts.run_dir), **final}, indent=2))
    return _exit_code_for_summary(summary)


def _run_evap_paired(args: argparse.Namespace) -> int:
    parameters = {
        **_reported_context(args),
        "duration_s": args.duration_s,
        "interval_s": args.interval_s,
        "window_title_substring": args.window_title_substring,
        "log_dir": str(args.log_dir) if args.log_dir else None,
    }
    artifacts = RunArtifacts(
        args.output_root, "evap_paired", parameters, args.run_id
    )
    try:
        from drivers.elog import find_current_log, latest_record
        from drivers.evap_control import (
            CALIBRATION_WINDOW_SIZE,
            OCR_CONFIG,
            PRESSURE_BBOX,
            ElogReader,
        )
        from drivers.ocr import capture_window, find_window, ocr_crop

        hwnd = find_window(args.window_title_substring)
        if not hwnd:
            raise RuntimeError(
                f"EvapControl window not found: {args.window_title_substring!r}"
            )
        log_dir = (
            str(args.log_dir)
            if args.log_dir
            else ElogReader._resolve_log_dir()
        )

        def read_elog() -> dict[str, Any]:
            path = find_current_log(log_dir)
            if path is None:
                raise RuntimeError(f"no current .elo file in {log_dir}")
            source_at, values = latest_record(path, ["MBE.Pressure"])
            pressure, format_string = values["MBE.Pressure"]
            return {
                "elog_path": str(path),
                "elog_source_at_utc": source_at.isoformat(),
                "elog_pressure_mbar": pressure,
                "elog_format_string": format_string,
                "elog_pressure_in_driver_range": (
                    math.isfinite(pressure)
                    and EVAP_PRESSURE_RANGE_MBAR[0]
                    <= pressure
                    <= EVAP_PRESSURE_RANGE_MBAR[1]
                ),
            }

        def bbox_for(frame: Any) -> tuple[int, int, int, int]:
            return scale_bbox(
                PRESSURE_BBOX, CALIBRATION_WINDOW_SIZE, frame
            )

        def run_ocr(crop: Any) -> str:
            height, width = crop.shape[:2]
            return ocr_crop(
                crop,
                (0, 0, width, height),
                OCR_CONFIG,
                label="evap_crosscheck",
            )

        result = collect_timed(
            lambda sequence: acquire_evap_pair_sample(
                sequence,
                hwnd=hwnd,
                crop_dir=artifacts.run_dir / "crops",
                capture_fn=capture_window,
                window_metadata_fn=get_window_metadata,
                bbox_fn=bbox_for,
                ocr_fn=run_ocr,
                save_crop_fn=save_rgb_png,
                elog_fn=read_elog,
            ),
            args.duration_s,
            args.interval_s,
            artifacts.append_sample,
        )
        summary = summarize_collection(result)
        paired_differences = [
            float(record["pressure_absolute_difference_mbar"])
            for record in result.records
            if record.get("paired_pressure_valid")
        ]
        pair_receive_offsets = [
            abs(float(record["pair_receive_offset_ms"]))
            for record in result.records
            if record.get("pair_receive_offset_ms") is not None
        ]
        elog_source_ages = [
            float(record["elog_source_age_at_ocr_ms"])
            for record in result.records
            if record.get("elog_source_age_at_ocr_ms") is not None
        ]
        elog_source_timestamps = [
            str(record["elog_source_at_utc"])
            for record in result.records
            if record.get("elog_pressure_valid")
            and record.get("elog_source_at_utc")
        ]
        unique_elog_timestamps = set(elog_source_timestamps)
        elog_valid_count = sum(
            bool(record.get("elog_pressure_valid"))
            for record in result.records
        )
        ocr_valid_count = sum(
            bool(record.get("ocr_pressure_valid"))
            for record in result.records
        )
        if not paired_differences and not result.interrupted:
            summary["run_status"] = "completed_no_paired_data"
        window_sizes = sorted(
            {
                (
                    record.get("window", {}).get("window_width_px"),
                    record.get("window", {}).get("window_height_px"),
                )
                for record in result.records
                if record.get("window")
            }
        )
        dpi_values = sorted(
            {
                record.get("window", {}).get("dpi")
                for record in result.records
                if record.get("window", {}).get("dpi") is not None
            }
        )
        summary.update(
            {
                "probe": "evap_paired",
                "requested_duration_s": args.duration_s,
                "requested_interval_s": args.interval_s,
                "elog_numeric_samples": elog_valid_count,
                "ocr_numeric_samples": ocr_valid_count,
                "paired_numeric_samples": len(paired_differences),
                "unique_elog_source_records": len(unique_elog_timestamps),
                "reused_elog_source_samples": (
                    len(elog_source_timestamps) - len(unique_elog_timestamps)
                ),
                "elog_paths_observed": sorted(
                    {
                        str(record["elog_path"])
                        for record in result.records
                        if record.get("elog_path")
                    }
                ),
                "window_titles_observed": sorted(
                    {
                        str(record.get("window", {}).get("title"))
                        for record in result.records
                        if record.get("window", {}).get("title")
                    }
                ),
                "window_sizes_observed": window_sizes,
                "dpi_values_observed": dpi_values,
                "absolute_difference_mbar": {
                    "p50": _percentile(paired_differences, 0.50),
                    "p95": _percentile(paired_differences, 0.95),
                    "max": max(paired_differences) if paired_differences else None,
                },
                "absolute_pair_receive_offset_ms": {
                    "p50": _percentile(pair_receive_offsets, 0.50),
                    "p95": _percentile(pair_receive_offsets, 0.95),
                    "max": max(pair_receive_offsets)
                    if pair_receive_offsets
                    else None,
                },
                "elog_source_age_at_ocr_ms": {
                    "p50": _percentile(elog_source_ages, 0.50),
                    "p95": _percentile(elog_source_ages, 0.95),
                    "max": max(elog_source_ages) if elog_source_ages else None,
                },
                "meets_planned_ten_minute_request": (
                    args.duration_s >= PLANNED_MINIMUM_DURATION_S
                    and _planned_duration_completed(
                        result, args.duration_s, args.interval_s
                    )
                    and bool(paired_differences)
                ),
                "interpretation": (
                    "Measurements only; no agreement threshold is imposed."
                ),
            }
        )
    except Exception as exc:
        summary = {
            "run_status": "setup_error",
            "probe": "evap_paired",
            "setup_error": f"{type(exc).__name__}: {exc}",
            "sample_attempts": 0,
        }
    final = artifacts.finish(summary)
    print(json.dumps({"run_dir": str(artifacts.run_dir), **final}, indent=2))
    return _exit_code_for_summary(summary)


def _run_mistral_ocr(args: argparse.Namespace) -> int:
    parameters = {
        **_reported_context(args),
        "duration_s": args.duration_s,
        "interval_s": args.interval_s,
        "window_title_substring": args.window_title_substring,
    }
    artifacts = RunArtifacts(
        args.output_root, "mistral_ocr", parameters, args.run_id
    )
    try:
        from drivers.mistral import (
            CALIBRATION_WINDOW_SIZE,
            OCR_CONFIG,
            VI_BBOX,
        )
        from drivers.ocr import capture_window, find_window, ocr_crop

        hwnd = find_window(args.window_title_substring)
        if not hwnd:
            raise RuntimeError(
                f"MISTRAL window not found: {args.window_title_substring!r}"
            )

        def bbox_for(frame: Any) -> tuple[int, int, int, int]:
            return scale_bbox(VI_BBOX, CALIBRATION_WINDOW_SIZE, frame)

        def run_ocr(crop: Any) -> str:
            height, width = crop.shape[:2]
            return ocr_crop(
                crop,
                (0, 0, width, height),
                OCR_CONFIG,
                label="mistral_crosscheck",
            )

        result = collect_timed(
            lambda sequence: acquire_mistral_sample(
                sequence,
                hwnd=hwnd,
                crop_dir=artifacts.run_dir / "crops",
                capture_fn=capture_window,
                window_metadata_fn=get_window_metadata,
                bbox_fn=bbox_for,
                ocr_fn=run_ocr,
                save_crop_fn=save_rgb_png,
            ),
            args.duration_s,
            args.interval_s,
            artifacts.append_sample,
        )
        summary = summarize_collection(result)
        parsed_samples_by_field = {
            key: sum(
                record.get(key) is not None for record in result.records
            )
            for key in MISTRAL_KEYS
        }
        valid_sample_count = sum(
            int(record.get("parsed_field_count", 0)) > 0
            for record in result.records
        )
        window_sizes = sorted(
            {
                (
                    record.get("window", {}).get("window_width_px"),
                    record.get("window", {}).get("window_height_px"),
                )
                for record in result.records
                if record.get("window")
            }
        )
        dpi_values = sorted(
            {
                record.get("window", {}).get("dpi")
                for record in result.records
                if record.get("window", {}).get("dpi") is not None
            }
        )
        if valid_sample_count == 0 and not result.interrupted:
            summary["run_status"] = "completed_no_valid_data"
        summary.update(
            {
                "probe": "mistral_ocr",
                "requested_duration_s": args.duration_s,
                "requested_interval_s": args.interval_s,
                "valid_numeric_samples": valid_sample_count,
                "parsed_samples_by_field": parsed_samples_by_field,
                "all_none_samples": sum(
                    bool(record.get("all_fields_none"))
                    for record in result.records
                ),
                "ocr_error_samples": sum(
                    bool(record.get("ocr_error"))
                    for record in result.records
                ),
                "window_sizes_observed": window_sizes,
                "dpi_values_observed": dpi_values,
                "window_titles_observed": sorted(
                    {
                        str(record.get("window", {}).get("title"))
                        for record in result.records
                        if record.get("window", {}).get("title")
                    }
                ),
                "resize_observed": len(window_sizes) > 1,
                "dpi_change_observed_within_run": len(dpi_values) > 1,
                "meets_planned_ten_minute_request": (
                    args.duration_s >= PLANNED_MINIMUM_DURATION_S
                    and _planned_duration_completed(
                        result, args.duration_s, args.interval_s
                    )
                    and valid_sample_count > 0
                ),
            }
        )
    except Exception as exc:
        summary = {
            "run_status": "setup_error",
            "probe": "mistral_ocr",
            "setup_error": f"{type(exc).__name__}: {exc}",
            "sample_attempts": 0,
        }
    final = artifacts.finish(summary)
    print(json.dumps({"run_dir": str(artifacts.run_dir), **final}, indent=2))
    return _exit_code_for_summary(summary)


def _run_jsonrpc(args: argparse.Namespace) -> int:
    parameters = {
        **_reported_context(args),
        "host": args.host,
        "port": args.port,
        "path": args.path,
        "timeout_s": args.timeout,
        "attempt_discovery": not args.no_discovery,
    }
    artifacts = RunArtifacts(
        args.output_root, "mistral_jsonrpc", parameters, args.run_id
    )
    try:
        from drivers.mistral_jsonrpc import MistralJsonRpcClient

        client = MistralJsonRpcClient(
            host=args.host,
            port=args.port,
            path=args.path,
            timeout=args.timeout,
        )
        record = diagnose_jsonrpc(
            client, attempt_discovery=not args.no_discovery
        )
        artifacts.append_sample(record)
        discovery = record.get("discovery", {})
        summary = {
            "run_status": "completed",
            "probe": "mistral_jsonrpc",
            "connected": record["connected"],
            "connection_error": record["connection_error"],
            "read_error": record["read_error"],
            "read_config_key_count": len(record.get("read_config") or {}),
            "all_values_none": record["all_values_none"],
            "diagnosis": record["diagnosis"],
            "discovery_attempted": record["discovery_attempted"],
            "discovery_ok_methods": sorted(
                name
                for name, evidence in discovery.items()
                if isinstance(evidence, dict) and evidence.get("ok")
            ),
            "discovery_error": record["discovery_error"],
        }
    except Exception as exc:
        summary = {
            "run_status": "setup_error",
            "probe": "mistral_jsonrpc",
            "setup_error": f"{type(exc).__name__}: {exc}",
            "sample_attempts": 0,
        }
    final = artifacts.finish(summary)
    print(json.dumps({"run_dir": str(artifacts.run_dir), **final}, indent=2))
    return _exit_code_for_summary(summary)


def _run_modbus_audit(args: argparse.Namespace) -> int:
    artifacts = RunArtifacts(
        args.output_root,
        "modbus_config_audit",
        {
            **_reported_context(args),
            "repository": str(REPO_ROOT),
        },
        args.run_id,
    )
    try:
        record = audit_modbus_configuration(REPO_ROOT)
        artifacts.append_sample(record)
        summary = {
            "run_status": "completed",
            "probe": "modbus_configuration_audit",
            "finding": record["finding"],
            "gui_passes_port": record["gui_to_worker"]["passes_port"],
            "gui_passes_baudrate": record["gui_to_worker"]["passes_baudrate"],
            "worker_passes_port_to_modbus": record["worker_to_modbus"][
                "passes_port"
            ],
            "worker_passes_baudrate_to_modbus": record["worker_to_modbus"][
                "passes_baudrate"
            ],
            "modbus_driver_constructor_defaults": record[
                "modbus_driver_constructor_defaults"
            ],
            "note": "Static source audit only; no serial port was opened.",
        }
    except Exception as exc:
        summary = {
            "run_status": "setup_error",
            "probe": "modbus_configuration_audit",
            "setup_error": f"{type(exc).__name__}: {exc}",
        }
    final = artifacts.finish(summary)
    print(json.dumps({"run_dir": str(artifacts.run_dir), **final}, indent=2))
    return _exit_code_for_summary(summary)


def _add_artifact_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"artifact root (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="optional unique directory name; existing directories are refused",
    )
    parser.add_argument(
        "--operator",
        default=None,
        help="operator name or initials recorded as metadata",
    )
    parser.add_argument(
        "--software-version",
        action="append",
        default=[],
        metavar="NAME=VERSION",
        help="vendor/software version metadata; repeat for multiple programs",
    )


def _add_timed_args(
    parser: argparse.ArgumentParser, *, interval_s: float
) -> None:
    parser.add_argument(
        "--duration-s",
        type=float,
        default=PLANNED_MINIMUM_DURATION_S,
        help="planned live-test duration; 600 s matches the test plan",
    )
    parser.add_argument(
        "--interval-s",
        type=float,
        default=interval_s,
        help="monotonic interval between sample starts",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only Bulbasaur/O-MBE interface cross-checks. Run only one "
            "serial pyrometer mode at a time."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pyrometer = subparsers.add_parser(
        "pyrometer",
        help="timed Exactus, Modbus, or existing TemperaSure UIA reads",
    )
    pyrometer.add_argument(
        "mode", choices=("exactus", "modbus", "temperasure")
    )
    _add_artifact_args(pyrometer)
    _add_timed_args(pyrometer, interval_s=0.5)
    pyrometer.add_argument("--samples-per-poll", type=int, default=5)
    pyrometer.add_argument("--port", default="COM4")
    pyrometer.add_argument("--baudrate", type=int, default=115200)
    pyrometer.add_argument("--device-id", type=int, default=1)
    pyrometer.add_argument(
        "--window-title",
        default="BASF TemperaSure 5.7.0.4 Advanced Mode",
    )
    pyrometer.set_defaults(handler=_run_pyrometer)

    evap = subparsers.add_parser(
        "evap-paired", help="paired EvapControl .elo tail + OCR evidence"
    )
    _add_artifact_args(evap)
    _add_timed_args(evap, interval_s=1.0)
    evap.add_argument(
        "--window-title-substring", default="Evaporation control"
    )
    evap.add_argument("--log-dir", type=Path, default=None)
    evap.set_defaults(handler=_run_evap_paired)

    mistral = subparsers.add_parser(
        "mistral-ocr", help="MISTRAL OCR evidence with window/DPI metadata"
    )
    _add_artifact_args(mistral)
    _add_timed_args(mistral, interval_s=1.0)
    mistral.add_argument("--window-title-substring", default="MistralGui")
    mistral.set_defaults(handler=_run_mistral_ocr)

    jsonrpc = subparsers.add_parser(
        "jsonrpc-diagnose",
        help="MISTRAL JSON-RPC reachability/read-config/all-None diagnosis",
    )
    _add_artifact_args(jsonrpc)
    jsonrpc.add_argument("--host", default="10.0.42.231")
    jsonrpc.add_argument("--port", type=int, default=9000)
    jsonrpc.add_argument("--path", default="/api")
    jsonrpc.add_argument("--timeout", type=float, default=2.0)
    jsonrpc.add_argument(
        "--no-discovery",
        action="store_true",
        help="skip rpc.discover/system.listMethods/system.describe",
    )
    jsonrpc.set_defaults(handler=_run_jsonrpc)

    audit = subparsers.add_parser(
        "audit-modbus-config",
        help="static GUI -> worker -> Modbus constructor configuration audit",
    )
    _add_artifact_args(audit)
    audit.set_defaults(handler=_run_modbus_audit)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
