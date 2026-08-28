"""Offline performance probe for point-event review and Equalizer startup.

The probe deliberately exercises one selected event only.  It opens exactly
one raw frame from the immutable session archive, loads the four active
simulator bases once, and performs edits against a temporary report copy.
No classifier/model is imported and no whole-session image precomputation is
performed.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid
import zipfile

from .loopback_service import EqualizerRequest
from .point_events import PointEventSidecarStore
from .report_builder import load_report_payload


PROBE_SCHEMA_VERSION = "rheed-point-event-performance-v1"
DEFAULT_EDIT_ITERATIONS = 50


class _WindowsProcessMemoryCounters(ctypes.Structure):
    """Win32 ``PROCESS_MEMORY_COUNTERS`` with pointer-sized fields."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


@lru_cache(maxsize=1)
def _windows_process_memory_api() -> tuple[Any, Any]:
    """Return correctly typed Win32 process-memory functions and handle."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    get_process_memory_info = psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    return get_process_memory_info, get_current_process()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _milliseconds(start_ns: int, end_ns: int) -> float:
    return (end_ns - start_ns) / 1_000_000.0


def _timed(function: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter_ns()
    value = function()
    return value, _milliseconds(started, time.perf_counter_ns())


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return math.nan
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def timing_summary(values_ms: Sequence[float]) -> dict[str, float | int]:
    """Return stable latency summary fields without a statistics dependency."""

    values = [float(value) for value in values_ms]
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min_ms": min(values),
        "mean_ms": statistics.fmean(values),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "p99_ms": _percentile(values, 0.99),
        "max_ms": max(values),
    }


def _process_rss_bytes() -> int | None:
    """Return current resident/working-set bytes without requiring psutil."""

    if sys.platform == "win32":
        counters = _WindowsProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        try:
            get_process_memory_info, process = _windows_process_memory_api()
            ok = get_process_memory_info(
                process, ctypes.byref(counters), counters.cb,
            )
        except (AttributeError, OSError):
            return None
        return int(counters.WorkingSetSize) if ok else None
    proc_statm = Path("/proc/self/statm")
    if proc_statm.is_file():
        try:
            resident_pages = int(proc_statm.read_text(encoding="ascii").split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
        except (IndexError, OSError, TypeError, ValueError):
            return None
    return None


class _PeakMemorySampler:
    """Sample native process RSS so Qt/Pillow allocations are represented."""

    def __init__(self, interval_s: float = 0.01) -> None:
        self.interval_s = interval_s
        self.samples: list[int] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        first = _process_rss_bytes()
        if first is not None:
            self.samples.append(first)
        self._thread = threading.Thread(
            target=self._sample,
            name="rheed-performance-memory-sampler",
            daemon=True,
        )
        self._thread.start()

    def _sample(self) -> None:
        while not self._stop.wait(self.interval_s):
            value = _process_rss_bytes()
            if value is not None:
                self.samples.append(value)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        final = _process_rss_bytes()
        if final is not None:
            self.samples.append(final)

    @property
    def peak(self) -> int | None:
        return max(self.samples) if self.samples else None


@contextmanager
def _temporary_report_copy(report_path: Path) -> Iterator[Path]:
    """Isolate all revision writes from the real report and source ZIP."""

    with tempfile.TemporaryDirectory(prefix="rheed-point-probe-") as temporary:
        destination = Path(temporary) / "interactive_report.html"
        shutil.copy2(report_path, destination)
        yield destination


def _valid_git_oid(value: str) -> bool:
    text = str(value or "").strip().lower()
    return len(text) in {40, 64} and all(
        character in "0123456789abcdef" for character in text
    )


def _git_metadata_commit(repo_root: Path) -> str:
    """Read HEAD from ordinary or linked-worktree metadata without Git."""

    marker = repo_root / ".git"
    try:
        if marker.is_dir():
            git_dir = marker.resolve()
        else:
            declaration = marker.read_text(encoding="utf-8").strip()
            if not declaration.lower().startswith("gitdir:"):
                return ""
            raw_path = declaration.split(":", 1)[1].strip()
            candidate = Path(raw_path)
            git_dir = (
                candidate if candidate.is_absolute() else repo_root / candidate
            ).resolve()
        common_marker = git_dir / "commondir"
        if common_marker.is_file():
            common_value = common_marker.read_text(encoding="utf-8").strip()
            common_candidate = Path(common_value)
            common_dir = (
                common_candidate
                if common_candidate.is_absolute()
                else git_dir / common_candidate
            ).resolve()
        else:
            common_dir = git_dir
        head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
        if _valid_git_oid(head):
            return head.lower()
        if not head.startswith("ref: "):
            return ""
        reference = head[5:].strip().replace("\\", "/")
        if (
            not reference
            or reference.startswith("/")
            or ".." in reference.split("/")
        ):
            return ""
        for base in (git_dir, common_dir):
            loose = base.joinpath(*reference.split("/"))
            if loose.is_file():
                value = loose.read_text(encoding="ascii").strip()
                if _valid_git_oid(value):
                    return value.lower()
        packed = common_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="ascii").splitlines():
                if not line or line.startswith(("#", "^")):
                    continue
                fields = line.split(" ", 1)
                if (
                    len(fields) == 2
                    and fields[1] == reference
                    and _valid_git_oid(fields[0])
                ):
                    return fields[0].lower()
    except (OSError, UnicodeError):
        return ""
    return ""


def _git_environment(repo_root: Path) -> dict[str, object]:
    git_executable = shutil.which("git")

    def command(*args: str) -> str | None:
        if not git_executable:
            return None
        try:
            result = subprocess.run(
                [git_executable, "-C", str(repo_root), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip()

    command_commit = command("rev-parse", "HEAD")
    commit = command_commit or _git_metadata_commit(repo_root)
    status = command("status", "--porcelain", "--untracked-files=no")
    return {
        "commit": commit,
        "git_commit_source": (
            "git-command"
            if command_commit
            else "git-metadata"
            if commit
            else "unavailable"
        ),
        "git_status_available": status is not None,
        "tracked_worktree_dirty": None if status is None else bool(status),
    }


def _system_memory_bytes() -> int | None:
    if sys.platform != "win32":
        return None

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    value = MemoryStatusEx()
    value.dwLength = ctypes.sizeof(value)
    return int(value.ullTotalPhys) if ctypes.windll.kernel32.GlobalMemoryStatusEx(
        ctypes.byref(value)
    ) else None


def _environment(repo_root: Path) -> dict[str, object]:
    return {
        "recorded_at_utc": _utc_now(),
        "platform": platform.platform(),
        "windows_build": platform.version() if sys.platform == "win32" else "",
        "processor": platform.processor(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "python_bits": 64 if sys.maxsize > 2**32 else 32,
        "logical_cpu_count": os.cpu_count(),
        "system_memory_bytes": _system_memory_bytes(),
        **_git_environment(repo_root),
    }


def _choose_event(
    states: Mapping[str, Mapping[str, Any]], event_id: str | None,
) -> tuple[str, dict[str, Any]]:
    if event_id:
        event = states.get(event_id)
        if event is None:
            raise ValueError(f"Unknown point event: {event_id}")
        return event_id, dict(event)
    active = [
        (identifier, event)
        for identifier, event in states.items()
        if event.get("review", {}).get("disposition") == "active"
    ]
    if not active:
        raise ValueError("The report has no active labelable point event")
    active.sort(
        key=lambda item: (
            float(item[1]["review"]["anchor"]["elapsed_s"]), item[0],
        )
    )
    identifier, event = active[0]
    return identifier, dict(event)


def _linear_slope(values: Sequence[int]) -> float:
    if len(values) < 2:
        return 0.0
    centre_x = (len(values) - 1) / 2.0
    centre_y = statistics.fmean(values)
    denominator = sum((index - centre_x) ** 2 for index in range(len(values)))
    if denominator == 0:
        return 0.0
    return sum(
        (index - centre_x) * (value - centre_y)
        for index, value in enumerate(values)
    ) / denominator


def _prepare_equalizer(context: Any) -> dict[str, object]:
    """Construct the real retrospective widget without showing a window."""

    # Set before importing Qt.  A caller with an existing QApplication keeps
    # its selected platform; the standalone probe is safely headless.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import_started = time.perf_counter_ns()
    from PyQt6.QtWidgets import QApplication
    from gui.live_equalizer_tab import LiveEqualizerTab
    from scripts.equalizer_ui import auto_fit_details
    import_ms = _milliseconds(import_started, time.perf_counter_ns())

    app_started = time.perf_counter_ns()
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication([])
    app_init_ms = _milliseconds(app_started, time.perf_counter_ns())

    panel_started = time.perf_counter_ns()
    panel = LiveEqualizerTab(retrospective=True)
    app.processEvents()
    panel_ms = _milliseconds(panel_started, time.perf_counter_ns())
    bundle = panel.get_basis_bundle()
    if bundle is None:
        panel.close()
        raise ValueError("Canonical Equalizer basis could not be loaded")
    active = bundle.active_images()
    expected = ("1x1", "Tw(2x1)", "c(6x2)", "RT13")
    if tuple(active) != expected or len(active) != 4:
        panel.close()
        raise ValueError("Performance probe requires exactly four active simulator bases")

    metadata = context.metadata
    panel.set_session_id(str(metadata["session_id"]))
    panel.update_qc_context(
        session_active=True,
        view_segment_id=int(metadata["view_segment_id"]),
        visual_history_generation=int(metadata["visual_history_generation"]),
        gun_aligned=True,
        realignment_active=False,
    )
    panel.set_save_enabled(True)
    frame_started = time.perf_counter_ns()
    panel.update_camera_frame(context.rgb, metadata)
    app.processEvents()
    frame_ms = _milliseconds(frame_started, time.perf_counter_ns())
    snapshot = panel.get_current_snapshot()
    if snapshot is None:
        panel.close()
        raise ValueError("Equalizer did not retain the exact selected frame")

    # This times the four-basis numerical kernel only.  It is not a model
    # inference and its uncalibrated output is intentionally discarded.
    fit_started = time.perf_counter_ns()
    raw, normalized = auto_fit_details(active, snapshot.grayscale)
    fit_ms = _milliseconds(fit_started, time.perf_counter_ns())
    if set(raw) != set(expected) or set(normalized) != set(expected):
        panel.close()
        raise ValueError("Equalizer fit used an unexpected basis set")

    panel.close()
    panel.deleteLater()
    app.processEvents()
    # QApplication is intentionally allowed to die with the local reference.
    # Calling quit() could terminate a caller-owned application.
    del panel
    if owns_app:
        app.processEvents()
    return {
        "python_qt_import_ms": import_ms,
        "qapplication_init_ms": app_init_ms,
        "panel_create_and_four_basis_load_ms": panel_ms,
        "selected_frame_application_ms": frame_ms,
        "four_basis_fit_kernel_ms": fit_ms,
        "active_basis_count": len(active),
        "active_basis_labels": list(active),
        "unavailable_basis_labels": ["HTR"],
        "basis_bundle_id": bundle.bundle_id,
        "classifier_model_imported": False,
        "model_inference_executed": False,
        "fit_output_saved": False,
        "fit_output_interpretable_without_calibration": False,
    }


def run_performance_probe(
    report_path: str | Path,
    session_path: str | Path,
    *,
    event_id: str | None = None,
    edit_iterations: int = DEFAULT_EDIT_ITERATIONS,
    actor: str = "Ch-MBE performance probe",
) -> dict[str, object]:
    """Measure the offline point-event path without modifying source data."""

    report = Path(report_path).expanduser().resolve()
    session_path = Path(session_path).expanduser().resolve()
    if not report.is_file() or report.name != "interactive_report.html":
        raise ValueError("performance probe requires interactive_report.html")
    if not session_path.is_file():
        raise ValueError("performance probe requires a readable session ZIP")
    if edit_iterations < 1 or edit_iterations > 10_000:
        raise ValueError("edit_iterations must be between 1 and 10000")
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("performance probe actor is required")

    repo_root = Path(__file__).resolve().parents[2]
    modules_before = set(sys.modules)
    source_stat_before = session_path.stat()
    rss_start = _process_rss_bytes()
    sampler = _PeakMemorySampler()
    sampler.start()
    tracing_before = tracemalloc.is_tracing()
    if not tracing_before:
        tracemalloc.start()
    traced_start_current, _ = tracemalloc.get_traced_memory()

    try:
        with _temporary_report_copy(report) as scratch_report:
            payload, payload_ms = _timed(lambda: load_report_payload(scratch_report))
            store, store_ms = _timed(
                lambda: PointEventSidecarStore(scratch_report, session_path)
            )
            selected_id, selected = _choose_event(store.states, event_id)
            anchor = selected["review"]["anchor"]

            archive, archive_ms = _timed(store.ensure_session_verified)
            if archive.sha256 != payload["config"]["dataset"]["source_archive_sha256"]:
                raise ValueError("verified archive hash differs from report provenance")
            request = EqualizerRequest(
                request_id=str(uuid.uuid4()),
                event_id=selected_id,
                review_frame_index=int(anchor["frame_index"]),
                reviewer=actor,
            )
            from .offline_equalizer import resolve_offline_frame
            context, frame_ms = _timed(
                lambda: resolve_offline_frame(
                    scratch_report,
                    archive,
                    request,
                    additional_event_ids=set(store.event_ids),
                )
            )
            equalizer, equalizer_ms = _timed(lambda: _prepare_equalizer(context))
            equalizer["total_prepare_ms"] = equalizer_ms
            selected_member = archive.frames[int(anchor["frame_index"]) - 1].member
            with zipfile.ZipFile(session_path) as source_archive:
                selected_frame_bytes = int(source_archive.getinfo(selected_member).file_size)

            edit_latencies: list[float] = []
            edit_rss: list[int] = []
            current = store.states[selected_id]
            initial_revision_count = len(store.revisions)
            for index in range(edit_iterations):
                comment = f"performance probe scratch edit {index + 1}/{edit_iterations}"
                command = {
                    "dataset_id": store.dataset.get("dataset_id", ""),
                    "action": "edit",
                    "event_id": selected_id,
                    "base_revision_id": current.get("revision_id", ""),
                    "actor": actor,
                    "changes": {"comment": comment},
                }
                result, elapsed_ms = _timed(lambda command=command: store.apply_revision(command))
                current = result["event"]
                edit_latencies.append(elapsed_ms)
                rss = _process_rss_bytes()
                if rss is not None:
                    edit_rss.append(rss)

            final_revision_count = len(store.revisions)
            midpoint = max(1, len(edit_latencies) // 2)
            first_half = edit_latencies[:midpoint]
            second_half = edit_latencies[midpoint:] or edit_latencies[-1:]
            revision_integrity = (
                final_revision_count - initial_revision_count == edit_iterations
                and store.states[selected_id]["review"]["comment"]
                == f"performance probe scratch edit {edit_iterations}/{edit_iterations}"
            )
            source_stat_after = session_path.stat()
            modules_added = set(sys.modules) - modules_before
            hardware_roots = sorted({
                name.split(".", 1)[0]
                for name in modules_added
                if name.split(".", 1)[0] in {
                    "pyads", "pymodbus", "pyvisa", "serial", "pytesseract",
                }
            })
            model_roots = sorted({
                name.split(".", 1)[0]
                for name in modules_added
                if name.split(".", 1)[0] in {"timm", "torch", "torchvision"}
            })

            result = {
                "schema_version": PROBE_SCHEMA_VERSION,
                "purpose": "Ch-MBE offline point-event and Equalizer performance",
                "completed_without_exception": True,
                "environment": _environment(repo_root),
                "input": {
                    "report_path": str(report),
                    "session_path": str(session_path),
                    "dataset_id": store.dataset.get("dataset_id", ""),
                    "source_archive_sha256": archive.sha256,
                    "source_archive_bytes": source_stat_before.st_size,
                    "selected_event_id": selected_id,
                    "selected_event_source": selected["source"]["kind"],
                    "selected_frame_index": int(anchor["frame_index"]),
                    "selected_capture_sequence": int(anchor["capture_sequence"]),
                    "selected_frame_sha256": context.raw_frame_sha256,
                    "selected_frame_bytes": selected_frame_bytes,
                    "selected_frame_width": int(context.rgb.shape[1]),
                    "selected_frame_height": int(context.rgb.shape[0]),
                },
                "startup": {
                    "report_payload_load_ms": payload_ms,
                    "scratch_sidecar_startup_ms": store_ms,
                    "archive_hash_and_index_ms": archive_ms,
                },
                "selected_raw_frame": {
                    "resolve_read_decode_verify_ms": frame_ms,
                    "frames_decoded": 1,
                    "whole_session_image_precompute": False,
                },
                "equalizer": equalizer,
                "continuous_event_edits": {
                    "iterations": edit_iterations,
                    "all_revisions_durable_and_replayable": revision_integrity,
                    "latency": timing_summary(edit_latencies),
                    "first_half_mean_ms": statistics.fmean(first_half),
                    "second_half_mean_ms": statistics.fmean(second_half),
                    "second_to_first_latency_ratio": (
                        statistics.fmean(second_half) / statistics.fmean(first_half)
                        if statistics.fmean(first_half) > 0 else None
                    ),
                    "rss_start_bytes": edit_rss[0] if edit_rss else None,
                    "rss_end_bytes": edit_rss[-1] if edit_rss else None,
                    "rss_growth_bytes": (
                        edit_rss[-1] - edit_rss[0] if len(edit_rss) >= 2 else None
                    ),
                    "rss_slope_bytes_per_iteration": (
                        _linear_slope(edit_rss) if edit_rss else None
                    ),
                    "writes_target": "temporary_report_copy_only",
                },
                "safety": {
                    "source_zip_opened_read_only": True,
                    "source_zip_stat_unchanged": (
                        source_stat_before.st_size == source_stat_after.st_size
                        and source_stat_before.st_mtime_ns == source_stat_after.st_mtime_ns
                    ),
                    "source_report_annotations_modified": False,
                    "selected_raw_frames_loaded": 1,
                    "active_simulator_bases_loaded": 4,
                    "classifier_or_model_inference": False,
                    "new_model_runtime_module_roots": model_roots,
                    "new_instrument_interface_module_roots": hardware_roots,
                    "laboratory_hardware_access": False,
                    "instrument_setpoints_changed": False,
                },
                "interpretation": {
                    "measures": (
                        "software startup, source verification, one exact-frame decode, "
                        "Equalizer widget/basis preparation, and scratch revision writes"
                    ),
                    "does_not_measure": (
                        "human alignment time, physical display latency, classifier "
                        "inference, camera acquisition, or instrument response"
                    ),
                    "pass_fail_thresholds_applied": False,
                    "tracemalloc_enabled_during_latency_measurements": True,
                },
            }
    finally:
        current_traced, peak_traced = tracemalloc.get_traced_memory()
        if not tracing_before:
            tracemalloc.stop()
        sampler.stop()

    rss_end = _process_rss_bytes()
    result["memory"] = {
        "process_rss_start_bytes": rss_start,
        "process_rss_peak_sampled_bytes": sampler.peak,
        "process_rss_end_bytes": rss_end,
        "process_rss_growth_bytes": (
            rss_end - rss_start
            if rss_start is not None and rss_end is not None else None
        ),
        "python_traced_start_bytes": traced_start_current,
        "python_traced_end_bytes": current_traced,
        "python_traced_peak_bytes": peak_traced,
        "native_rss_sample_interval_ms": sampler.interval_s * 1000.0,
    }
    return result


def write_performance_report(path: str | Path, result: Mapping[str, object]) -> Path:
    """Atomically write one compact, machine-readable probe result."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(destination.parent), prefix=f".{destination.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return destination


__all__ = [
    "DEFAULT_EDIT_ITERATIONS",
    "PROBE_SCHEMA_VERSION",
    "run_performance_probe",
    "timing_summary",
    "write_performance_report",
]
