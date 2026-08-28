"""Durable, hardware-independent audit files for heater operation.

The OWON supply does not expose a device clock.  UTC timestamps in these
files are therefore host timestamps, while ``perf_counter_ns`` values are the
authoritative source for ordering, duration, and sample age within one
process.
"""

from __future__ import annotations

import csv
import json
import os
import platform
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


ACTION_FIELDS = (
    "event_sequence",
    "request_id",
    "source",
    "action",
    "phase",
    "requested_json",
    "effective_json",
    "before_sample_json",
    "confirmation_sample_json",
    "readback_json",
    "error",
    "event_at_utc",
    "event_monotonic_ns",
)

TELEMETRY_FIELDS = (
    "connection_generation",
    "sample_sequence",
    "source_at_utc",
    "primary_completed_at_utc",
    "primary_completed_monotonic_ns",
    "primary_read_duration_ms",
    "received_at_utc",
    "acquire_started_monotonic_ns",
    "received_monotonic_ns",
    "read_duration_ms",
    "worker_emitted_monotonic_ns",
    "gui_received_monotonic_ns",
    "voltage_measured_v",
    "current_measured_a",
    "power_measured_w",
    "output_enabled",
    "output_valid",
    "voltage_setpoint_v",
    "current_setpoint_a",
    "ovp_limit_v",
    "ocp_limit_a",
    "settings_sample_sequence",
    "settings_received_at_utc",
    "settings_received_monotonic_ns",
    "settings_age_ms",
    "output_sample_sequence",
    "output_received_at_utc",
    "output_received_monotonic_ns",
    "output_age_ms",
    "voltage_setpoint_sample_sequence",
    "voltage_setpoint_received_at_utc",
    "voltage_setpoint_received_monotonic_ns",
    "voltage_setpoint_age_ms",
    "current_setpoint_sample_sequence",
    "current_setpoint_received_at_utc",
    "current_setpoint_received_monotonic_ns",
    "current_setpoint_age_ms",
    "ovp_sample_sequence",
    "ovp_received_at_utc",
    "ovp_received_monotonic_ns",
    "ovp_age_ms",
    "ocp_sample_sequence",
    "ocp_received_at_utc",
    "ocp_received_monotonic_ns",
    "ocp_age_ms",
    "valid",
)


def utc_now_iso() -> str:
    """Return an explicit UTC host timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    if isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _json_cell(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)


class HeaterSessionLogger:
    """Append-only action and telemetry files for one heater session.

    Creation is lazy so merely opening the dashboard does not create an empty
    run.  ``ensure_started`` must be called before a connection is attempted.
    """

    def __init__(self, log_root: str | Path = "logs/heater"):
        self.log_root = Path(log_root)
        self.session_id = ""
        self.session_dir: Optional[Path] = None
        self.actions_path: Optional[Path] = None
        self.telemetry_path: Optional[Path] = None
        self.metadata_path: Optional[Path] = None
        self._action_file = None
        self._telemetry_file = None
        self._action_writer = None
        self._telemetry_writer = None
        self._event_sequence = 0
        self._lock = threading.RLock()
        self._closed = False

    @property
    def started(self) -> bool:
        return self.session_dir is not None and not self._closed

    def ensure_started(self, metadata: Optional[dict[str, Any]] = None) -> Path:
        with self._lock:
            if self.started:
                return self.session_dir  # type: ignore[return-value]
            if self._closed:
                raise RuntimeError("heater audit session is already closed")

            self.log_root.mkdir(parents=True, exist_ok=True)
            started = datetime.now(timezone.utc)
            self.session_id = str(uuid.uuid4())
            stamp = started.strftime("%Y%m%dT%H%M%S_%fZ")
            session_dir = self.log_root / f"heater_{stamp}_{self.session_id}"
            session_dir.mkdir(exist_ok=False)

            self.session_dir = session_dir
            self.actions_path = session_dir / "heater_actions.csv"
            self.telemetry_path = session_dir / "heater_telemetry.csv"
            self.metadata_path = session_dir / "session_metadata.json"

            payload = {
                "schema_version": 3,
                "session_id": self.session_id,
                "started_at_utc": started.isoformat(timespec="milliseconds"),
                "timestamp_policy": {
                    "device_clock": None,
                    "utc": "host clock at completed operation/read",
                    "ordering_and_duration": "time.perf_counter_ns process-local monotonic clock",
                },
                "host": platform.node(),
                "python": platform.python_version(),
                **(metadata or {}),
            }
            with self.metadata_path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

            self._action_file = self.actions_path.open("x", newline="", encoding="utf-8")
            self._telemetry_file = self.telemetry_path.open("x", newline="", encoding="utf-8")
            self._action_writer = csv.DictWriter(self._action_file, fieldnames=ACTION_FIELDS)
            self._telemetry_writer = csv.DictWriter(
                self._telemetry_file, fieldnames=TELEMETRY_FIELDS,
            )
            self._action_writer.writeheader()
            self._telemetry_writer.writeheader()
            self._flush(self._action_file, durable=True)
            self._flush(self._telemetry_file, durable=True)
            return session_dir

    def log_action(
        self,
        *,
        request_id: str,
        source: str,
        action: str,
        phase: str,
        requested: Any = None,
        effective: Any = None,
        before_sample: Any = None,
        confirmation_sample: Any = None,
        readback: Any = None,
        error: str = "",
        event_at_utc: Optional[str] = None,
        event_monotonic_ns: Optional[int] = None,
        durable: bool = False,
    ) -> int:
        with self._lock:
            self.ensure_started()
            self._event_sequence += 1
            row = {
                "event_sequence": self._event_sequence,
                "request_id": request_id,
                "source": source,
                "action": action,
                "phase": phase,
                "requested_json": _json_cell(requested),
                "effective_json": _json_cell(effective),
                "before_sample_json": _json_cell(before_sample),
                "confirmation_sample_json": _json_cell(confirmation_sample),
                "readback_json": _json_cell(readback),
                "error": error,
                "event_at_utc": event_at_utc or utc_now_iso(),
                "event_monotonic_ns": (
                    event_monotonic_ns
                    if event_monotonic_ns is not None
                    else time.perf_counter_ns()
                ),
            }
            self._action_writer.writerow(row)
            self._flush(self._action_file, durable=durable)
            return self._event_sequence

    def log_telemetry(self, state: Any) -> None:
        """Append exactly one successful PSU poll."""
        if not getattr(state, "valid", False):
            return
        with self._lock:
            self.ensure_started()
            row = {
                "connection_generation": getattr(state, "connection_generation", 0),
                "sample_sequence": getattr(state, "sample_sequence", 0),
                "source_at_utc": getattr(state, "source_at_utc", None) or "",
                "primary_completed_at_utc": getattr(
                    state, "primary_completed_at_utc", None,
                ) or "",
                "primary_completed_monotonic_ns": getattr(
                    state, "primary_completed_monotonic_ns", None,
                ),
                "primary_read_duration_ms": getattr(
                    state, "primary_read_duration_ms", None,
                ),
                "received_at_utc": getattr(state, "received_at_utc", None) or "",
                "acquire_started_monotonic_ns": getattr(
                    state, "acquire_started_monotonic_ns", None,
                ),
                "received_monotonic_ns": getattr(state, "received_monotonic_ns", None),
                "read_duration_ms": getattr(state, "read_duration_ms", None),
                "worker_emitted_monotonic_ns": getattr(
                    state, "worker_emitted_monotonic_ns", None,
                ),
                "gui_received_monotonic_ns": getattr(
                    state, "gui_received_monotonic_ns", None,
                ),
                "voltage_measured_v": getattr(state, "voltage_measured", None),
                "current_measured_a": getattr(state, "current_measured", None),
                "power_measured_w": getattr(state, "power_measured", None),
                "output_enabled": int(bool(getattr(state, "output_enabled", False))),
                "output_valid": int(bool(getattr(state, "output_valid", False))),
                "voltage_setpoint_v": getattr(state, "voltage_setpoint", None),
                "current_setpoint_a": getattr(state, "current_setpoint", None),
                "ovp_limit_v": getattr(state, "ovp_limit", None),
                "ocp_limit_a": getattr(state, "ocp_limit", None),
                "settings_sample_sequence": getattr(
                    state, "settings_sample_sequence", 0,
                ),
                "settings_received_at_utc": getattr(
                    state, "settings_received_at_utc", None,
                ) or "",
                "settings_received_monotonic_ns": getattr(
                    state, "settings_received_monotonic_ns", None,
                ),
                "settings_age_ms": getattr(state, "settings_age_ms", None),
                "output_sample_sequence": getattr(state, "output_sample_sequence", 0),
                "output_received_at_utc": getattr(state, "output_received_at_utc", None) or "",
                "output_received_monotonic_ns": getattr(state, "output_received_monotonic_ns", None),
                "output_age_ms": getattr(state, "output_age_ms", None),
                "voltage_setpoint_sample_sequence": getattr(state, "voltage_setpoint_sample_sequence", 0),
                "voltage_setpoint_received_at_utc": getattr(state, "voltage_setpoint_received_at_utc", None) or "",
                "voltage_setpoint_received_monotonic_ns": getattr(state, "voltage_setpoint_received_monotonic_ns", None),
                "voltage_setpoint_age_ms": getattr(state, "voltage_setpoint_age_ms", None),
                "current_setpoint_sample_sequence": getattr(state, "current_setpoint_sample_sequence", 0),
                "current_setpoint_received_at_utc": getattr(state, "current_setpoint_received_at_utc", None) or "",
                "current_setpoint_received_monotonic_ns": getattr(state, "current_setpoint_received_monotonic_ns", None),
                "current_setpoint_age_ms": getattr(state, "current_setpoint_age_ms", None),
                "ovp_sample_sequence": getattr(state, "ovp_sample_sequence", 0),
                "ovp_received_at_utc": getattr(state, "ovp_received_at_utc", None) or "",
                "ovp_received_monotonic_ns": getattr(state, "ovp_received_monotonic_ns", None),
                "ovp_age_ms": getattr(state, "ovp_age_ms", None),
                "ocp_sample_sequence": getattr(state, "ocp_sample_sequence", 0),
                "ocp_received_at_utc": getattr(state, "ocp_received_at_utc", None) or "",
                "ocp_received_monotonic_ns": getattr(state, "ocp_received_monotonic_ns", None),
                "ocp_age_ms": getattr(state, "ocp_age_ms", None),
                "valid": 1,
            }
            self._telemetry_writer.writerow(row)
            self._flush(self._telemetry_file, durable=False)

    @staticmethod
    def _flush(handle, *, durable: bool) -> None:
        handle.flush()
        if durable:
            os.fsync(handle.fileno())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            for handle in (self._action_file, self._telemetry_file):
                if handle is not None:
                    self._flush(handle, durable=True)
                    handle.close()
            self._closed = True


def state_snapshot(state: Any) -> Optional[dict[str, Any]]:
    """Return the compact PSU fields useful in an action row."""
    if state is None:
        return None
    names = (
        "connection_generation", "sample_sequence", "received_at_utc",
        "received_monotonic_ns", "voltage_setpoint", "current_setpoint",
        "voltage_measured", "current_measured", "power_measured",
        "output_enabled", "valid", "connected",
    )
    return {name: getattr(state, name, None) for name in names}
