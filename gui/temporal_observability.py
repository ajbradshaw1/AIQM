"""Read-only timing helpers for O-MBE acquisition observability.

Wall-clock timestamps are for joining evidence.  All durations and ages use
``time.perf_counter_ns`` so an NTP or daylight-saving adjustment cannot create
negative latency.  The helpers intentionally do not define a production
freshness threshold; the live tests establish that threshold from measured
distributions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import os
import threading
import time
from typing import Any, Mapping, Optional


def utc_now_iso() -> str:
    """Return a timezone-explicit UTC timestamp with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class TimingSnapshot:
    """Immutable timing view of one cached source at one snapshot instant."""

    source: str
    mode: str
    sequence: int
    source_at_utc: Optional[str]
    received_at_utc: Optional[str]
    acquire_started_monotonic_ns: Optional[int]
    received_monotonic_ns: Optional[int]
    worker_emitted_monotonic_ns: Optional[int]
    gui_received_monotonic_ns: Optional[int]
    read_duration_ms: Optional[float]
    age_ms: Optional[float]
    worker_to_gui_ms: Optional[float]
    valid: bool
    connected: bool
    error: str
    capture_completed_at_utc: Optional[str] = None
    capture_completed_monotonic_ns: Optional[int] = None
    processing_duration_ms: Optional[float] = None
    sample_span_ms: Optional[float] = None
    attempt_capture_completed_at_utc: Optional[str] = None
    attempt_capture_completed_monotonic_ns: Optional[int] = None
    attempt_completed_at_utc: Optional[str] = None
    attempt_completed_monotonic_ns: Optional[int] = None
    attempt_duration_ms: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _finite_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def snapshot_state(
    source: str,
    state: Any,
    snapshot_monotonic_ns: Optional[int] = None,
) -> TimingSnapshot:
    """Copy timing metadata from a State without mutating it."""
    now_ns = snapshot_monotonic_ns or time.perf_counter_ns()
    if state is None:
        return TimingSnapshot(
            source=source, mode="", sequence=0, source_at_utc=None,
            received_at_utc=None, acquire_started_monotonic_ns=None,
            received_monotonic_ns=None, worker_emitted_monotonic_ns=None,
            gui_received_monotonic_ns=None, read_duration_ms=None,
            age_ms=None, worker_to_gui_ms=None, valid=False,
            connected=False, error="state unavailable",
        )

    received_ns = _positive_int(getattr(
        state, "received_monotonic_ns",
        getattr(state, "captured_monotonic_ns", None),
    ))
    emitted_ns = _positive_int(getattr(state, "worker_emitted_monotonic_ns", None))
    gui_ns = _positive_int(getattr(state, "gui_received_monotonic_ns", None))
    age_ms = None
    if received_ns is not None:
        age_ms = max(0.0, (now_ns - received_ns) / 1_000_000.0)
    worker_to_gui_ms = None
    if emitted_ns is not None and gui_ns is not None:
        worker_to_gui_ms = max(0.0, (gui_ns - emitted_ns) / 1_000_000.0)

    sequence = getattr(
        state, "sample_sequence", getattr(state, "capture_sequence", 0),
    )
    valid_default = bool(getattr(state, "connected", False) and received_ns)
    return TimingSnapshot(
        source=source,
        mode=str(getattr(state, "mode", "") or ""),
        sequence=int(sequence or 0),
        source_at_utc=getattr(state, "source_at_utc", None),
        received_at_utc=(
            getattr(state, "received_at_utc", None)
            or getattr(state, "captured_at_utc", None)
        ),
        acquire_started_monotonic_ns=_positive_int(
            getattr(state, "acquire_started_monotonic_ns", None),
        ),
        received_monotonic_ns=received_ns,
        worker_emitted_monotonic_ns=emitted_ns,
        gui_received_monotonic_ns=gui_ns,
        read_duration_ms=_finite_float(getattr(state, "read_duration_ms", None)),
        age_ms=age_ms,
        worker_to_gui_ms=worker_to_gui_ms,
        valid=bool(getattr(state, "valid", valid_default)),
        connected=bool(getattr(state, "connected", False)),
        error=str(getattr(state, "error", "") or ""),
        capture_completed_at_utc=getattr(
            state, "capture_completed_at_utc", None,
        ),
        capture_completed_monotonic_ns=_positive_int(getattr(
            state, "capture_completed_monotonic_ns", None,
        )),
        processing_duration_ms=_finite_float(getattr(
            state, "processing_duration_ms", None,
        )),
        sample_span_ms=_finite_float(getattr(state, "sample_span_ms", None)),
        attempt_capture_completed_at_utc=getattr(
            state, "attempt_capture_completed_at_utc", None,
        ),
        attempt_capture_completed_monotonic_ns=_positive_int(getattr(
            state, "attempt_capture_completed_monotonic_ns", None,
        )),
        attempt_completed_at_utc=getattr(
            state, "attempt_completed_at_utc", None,
        ),
        attempt_completed_monotonic_ns=_positive_int(getattr(
            state, "attempt_completed_monotonic_ns", None,
        )),
        attempt_duration_ms=_finite_float(getattr(
            state, "attempt_duration_ms", None,
        )),
    )


def synchronization_summary(
    snapshots: Mapping[str, TimingSnapshot],
    required_sources: tuple[str, ...] = (
        "rheed", "pyrometer", "mistral", "evap",
    ),
) -> dict[str, Any]:
    """Return structural cross-source completeness and receive-time span.

    ``sync_valid`` means all required samples exist and were valid at the
    snapshot.  It deliberately does not claim that the measured span is
    physically acceptable; that decision is made after the live baseline.
    """
    required = [snapshots.get(name) for name in required_sources]
    complete = all(
        item is not None
        and item.sequence > 0
        and item.received_monotonic_ns is not None
        for item in required
    )
    valid = bool(complete and all(item.valid for item in required if item))
    receive_times = [
        item.received_monotonic_ns for item in required
        if item is not None and item.received_monotonic_ns is not None
    ]
    span_ms = None
    if len(receive_times) >= 2:
        span_ms = (max(receive_times) - min(receive_times)) / 1_000_000.0
    return {
        "sync_span_ms": span_ms,
        "sync_complete": complete,
        "sync_valid": valid,
        "sync_valid_definition": "all_required_samples_present_and_valid",
    }


def process_metrics() -> dict[str, Any]:
    """Return lightweight runtime health metrics without requiring psutil."""
    metrics: dict[str, Any] = {
        "pid": os.getpid(),
        "python_thread_count": threading.active_count(),
    }
    try:
        import psutil  # type: ignore
        process = psutil.Process()
        memory = process.memory_info()
        metrics.update({
            "os_thread_count": process.num_threads(),
            "rss_bytes": int(memory.rss),
            "cpu_percent": float(process.cpu_percent(interval=None)),
        })
    except (ImportError, OSError, RuntimeError):
        metrics.update({
            "os_thread_count": None,
            "rss_bytes": None,
            "cpu_percent": None,
        })
    return metrics
