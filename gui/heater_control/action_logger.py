"""Central UI log plus durable heater action/telemetry audit."""

from __future__ import annotations

import csv
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from gui.state import ActionLogEntry, PowerSupplyState, TemperatureState
from gui.heater_control.heater_commands import PowerSupplyCommandResult
from gui.heater_control.heater_session import (
    HeaterSessionLogger,
    state_snapshot,
    utc_now_iso,
)


MAX_ENTRIES = 10_000


class ActionLogger(QObject):
    """UI ring buffer backed by an append-only heater session.

    Ordinary state-changing commands must call :meth:`begin_action` before
    entering the worker queue. Safety callers may continue if that write
    fails, because logging must never inhibit an emergency shutdown.
    """

    entry_added = pyqtSignal(ActionLogEntry)
    cleared = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        log_root: str | Path | None = None,
        session_logger: Optional[HeaterSessionLogger] = None,
    ):
        super().__init__(parent)
        default_root = os.environ.get("AI4MBE_HEATER_LOG_ROOT", "logs/heater")
        self.session = session_logger or HeaterSessionLogger(log_root or default_root)
        self._entries: list[ActionLogEntry] = []
        self._latest_psu: Optional[PowerSupplyState] = None
        self._latest_temp: Optional[TemperatureState] = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._last_telemetry_key: Optional[tuple[int, int]] = None

    @property
    def entries(self) -> list[ActionLogEntry]:
        return self._entries

    @property
    def session_dir(self) -> Optional[Path]:
        return self.session.session_dir

    def ensure_session(self, metadata: Optional[dict[str, Any]] = None) -> Path:
        return self.session.ensure_started(metadata)

    def update_psu_state(self, state: PowerSupplyState):
        self._latest_psu = state

    def update_temp_state(self, state: TemperatureState):
        self._latest_temp = state

    def record_telemetry(self, state: PowerSupplyState) -> bool:
        """Persist one new valid sample; return whether a row was appended."""
        if not state.has_valid_reading:
            return False
        key = (state.connection_generation, state.sample_sequence)
        if key == self._last_telemetry_key:
            return False
        self.session.log_telemetry(state)
        self._last_telemetry_key = key
        return True

    def begin_action(
        self,
        category: str,
        action: str,
        *,
        source: str,
        requested: Any = None,
        effective: Any = None,
        details: str = "",
        request_id: Optional[str] = None,
        safety: bool = False,
    ) -> str:
        """Durably record REQUESTED before a state-changing command is queued."""
        request_id = request_id or str(uuid.uuid4())
        event_utc = utc_now_iso()
        event_ns = time.perf_counter_ns()
        before = state_snapshot(self._latest_psu)
        self.session.log_action(
            request_id=request_id,
            source=source,
            action=action,
            phase="REQUESTED",
            requested=requested,
            effective=effective,
            before_sample=before,
            event_at_utc=event_utc,
            event_monotonic_ns=event_ns,
            durable=safety,
        )
        self._pending[request_id] = {
            "category": category,
            "action": action,
            "source": source,
            "requested": requested,
            "effective": effective,
            "before": before,
            "details": details,
            "safety": safety,
        }
        self._append_ui(category, f"{action} REQUESTED", details)
        return request_id

    def complete_action(self, result: PowerSupplyCommandResult) -> None:
        # Keep the lifecycle pending until its terminal row has actually been
        # written.  This preserves the correlation context if a transient disk
        # error makes the caller retry the completion.
        pending = self._pending.get(result.request_id, {})
        category = pending.get("category", "Power Supply")
        action = pending.get("action", result.command)
        source = pending.get("source", result.source)
        requested = pending.get("requested")
        if requested is None:
            requested = result.requested
        effective = pending.get("effective")
        if effective is None:
            effective = result.effective
        before = pending.get("before")
        safety = bool(
            pending.get(
                "safety", result.command in {"safe_shutdown", "emergency_stop"},
            )
        )
        # A worker result is the confirmation observation.  The most recent
        # GUI poll may pre-date the command, so never present that cached poll
        # as if it confirmed the write/readback transaction.
        confirmation = None
        if result.readback:
            confirmation = {
                "connection_generation": result.connection_generation,
                "sample_sequence": result.confirmation_sample_sequence,
                **result.readback,
            }
        self.session.log_action(
            request_id=result.request_id,
            source=source,
            action=action,
            phase=result.status,
            requested=requested,
            effective=effective,
            before_sample=before,
            confirmation_sample=confirmation,
            readback=result.readback,
            error=result.error,
            event_at_utc=result.completed_at_utc,
            event_monotonic_ns=result.completed_monotonic_ns,
            durable=safety,
        )
        self._pending.pop(result.request_id, None)
        details = pending.get("details", "")
        if result.error:
            details = f"{details}; {result.error}" if details else result.error
        self._append_ui(category, f"{action} {result.status}", details)

    def fail_action(
        self,
        request_id: str,
        error: str,
        *,
        status: str = "FAILED",
    ) -> None:
        pending = self._pending.get(request_id, {})
        self.complete_action(PowerSupplyCommandResult(
            request_id=request_id,
            source=pending.get("source", "system"),
            command=pending.get("action", "unknown"),
            status=status,
            requested=pending.get("requested") or {},
            effective=pending.get("effective") or {},
            readback={},
            error=error,
            completed_at_utc=utc_now_iso(),
            completed_monotonic_ns=time.perf_counter_ns(),
        ))

    def immediate_action(
        self,
        category: str,
        action: str,
        *,
        source: str,
        requested: Any = None,
        effective: Any = None,
        details: str = "",
        safety: bool = False,
    ) -> str:
        request_id = self.begin_action(
            category,
            action,
            source=source,
            requested=requested,
            effective=effective,
            details=details,
            safety=safety,
        )
        self.complete_action(PowerSupplyCommandResult(
            request_id=request_id,
            source=source,
            command=action,
            status="CONFIRMED",
            requested=requested or {},
            effective=effective or {},
            readback={},
            error="",
            completed_at_utc=utc_now_iso(),
            completed_monotonic_ns=time.perf_counter_ns(),
        ))
        return request_id

    def log(self, category: str, action: str, details: str = ""):
        """Record an informational event, not a hardware success claim."""
        self._append_ui(category, action, details)
        if self.session.started:
            self.session.log_action(
                request_id=str(uuid.uuid4()),
                source="system",
                action=f"{category}: {action}",
                phase="EVENT",
                requested={"details": details} if details else None,
                before_sample=state_snapshot(self._latest_psu),
            )

    def _append_ui(self, category: str, action: str, details: str = "") -> None:
        entry = ActionLogEntry(
            timestamp=datetime.now(timezone.utc),
            category=category,
            action=action,
            details=details,
        )
        if self._latest_psu and self._latest_psu.connected:
            entry.psu_voltage = self._latest_psu.voltage_measured
            entry.psu_current = self._latest_psu.current_measured
            entry.psu_power = self._latest_psu.power_measured
            entry.psu_output = self._latest_psu.output_enabled
        if self._latest_temp and self._latest_temp.connected:
            entry.temperature = self._latest_temp.temperature
        self._entries.append(entry)
        if len(self._entries) > MAX_ENTRIES:
            self._entries = self._entries[-MAX_ENTRIES:]
        self.entry_added.emit(entry)

    def export_csv(self, filepath: str):
        """Export the current UI ring buffer; the session audit is unaffected."""
        with open(filepath, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "Timestamp", "Category", "Action", "Details",
                "PSU Voltage (V)", "PSU Current (A)", "PSU Power (W)",
                "PSU Output", "Temperature (C)",
            ])
            for entry in self._entries:
                writer.writerow([
                    entry.timestamp.isoformat(), entry.category, entry.action,
                    entry.details,
                    f"{entry.psu_voltage:.3f}" if entry.psu_voltage is not None else "",
                    f"{entry.psu_current:.3f}" if entry.psu_current is not None else "",
                    f"{entry.psu_power:.3f}" if entry.psu_power is not None else "",
                    "ON" if entry.psu_output else (
                        "OFF" if entry.psu_output is not None else ""
                    ),
                    f"{entry.temperature:.2f}" if entry.temperature is not None else "",
                ])

    def clear(self):
        """Clear only the UI ring buffer, retaining the append-only audit."""
        if self.session.started:
            self.session.log_action(
                request_id=str(uuid.uuid4()),
                source="manual_ui",
                action="ui_view_cleared",
                phase="CONFIRMED",
                requested={"visible_entries": len(self._entries)},
            )
        self._entries.clear()
        self.cleared.emit()

    def count(self) -> int:
        return len(self._entries)

    def close(self) -> None:
        self.session.close()
