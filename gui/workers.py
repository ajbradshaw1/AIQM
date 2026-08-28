"""
Background worker threads for instrument communication.
"""

import logging
import itertools
import queue
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from PyQt6.QtCore import QMutex, QThread, pyqtSignal

from drivers.rheed_camera import FrameNotYetAvailableError

log = logging.getLogger(__name__)


def _utc_iso_now() -> str:
    """Return a timezone-explicit UTC receive timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _mark_sample_received(
    state,
    read_started_ns: int,
    source_at_utc: Optional[str] = None,
) -> int:
    """Attach provenance after one poll returns without raising.

    A failed read does not call this helper, so its state retains the previous
    sequence/timestamp and downstream ``age_ms`` increases naturally.
    """
    received_ns = time.perf_counter_ns()
    state.acquire_started_monotonic_ns = read_started_ns
    state.source_at_utc = source_at_utc
    state.received_at_utc = _utc_iso_now()
    state.sample_sequence += 1
    state.read_duration_ms = max(
        0.0, (received_ns - read_started_ns) / 1_000_000.0,
    )
    state.received_monotonic_ns = received_ns
    state.valid = True
    return received_ns


def _emission_snapshot(state):
    """Return a fresh State stamped immediately before queued emission."""
    if not hasattr(state, "worker_emitted_monotonic_ns"):
        return replace(state)
    return replace(
        state,
        worker_emitted_monotonic_ns=time.perf_counter_ns(),
    )


def _mark_read_failed(
    state, error: object, read_started_ns: Optional[int] = None,
) -> int:
    """Invalidate the current poll without advancing its success sequence."""
    failed_ns = time.perf_counter_ns()
    state.valid = False
    state.error = str(error or "read returned no usable values")
    if read_started_ns is not None:
        state.acquire_started_monotonic_ns = int(read_started_ns)
        state.read_duration_ms = max(
            0.0,
            (failed_ns - read_started_ns) / 1_000_000.0,
        )
    return failed_ns


def _mark_attempt_completed(
    state,
    *,
    read_started_ns: int,
    completed_ns: int,
    capture_at_utc: Optional[str],
    capture_monotonic_ns: Optional[int],
    succeeded: bool,
) -> None:
    """Attach one poll attempt without cross-wiring sample provenance.

    The regular capture/receive fields describe the latest successful sample.
    The ``attempt_*`` fields advance for both successes and failures so OCR
    screenshot and processing time remain observable when parsing fails.
    """
    state.attempt_capture_completed_at_utc = capture_at_utc
    state.attempt_capture_completed_monotonic_ns = capture_monotonic_ns
    state.attempt_completed_monotonic_ns = completed_ns
    state.attempt_completed_at_utc = (
        state.received_at_utc if succeeded else _utc_iso_now()
    )
    state.attempt_duration_ms = max(
        0.0, (completed_ns - read_started_ns) / 1_000_000.0,
    )
    if not succeeded:
        return
    state.capture_completed_at_utc = capture_at_utc
    state.capture_completed_monotonic_ns = capture_monotonic_ns
    state.processing_duration_ms = None
    if capture_monotonic_ns is not None:
        state.processing_duration_ms = max(
            0.0,
            (completed_ns - capture_monotonic_ns) / 1_000_000.0,
        )


def _frame_luminance(frame: np.ndarray) -> float:
    """Grayscale-equivalent mean intensity from a camera frame.

    Handles both 2-D (H, W) raw and 3-D (H, W, 3) RGB frames.
    Uses ITU-R BT.601 luminance weights for RGB so the result is
    consistent regardless of whether the BGW palette was applied.
    Green-channel-only mean is biased: BGW saturates G=255 across
    the upper half of the LUT (indices 128-255), hiding R/B variation.
    """
    if frame.ndim == 2:
        return float(frame.mean())
    r = frame[:, :, 0].astype(np.float32)
    g = frame[:, :, 1].astype(np.float32)
    b = frame[:, :, 2].astype(np.float32)
    return float((0.299 * r + 0.587 * g + 0.114 * b).mean())

from gui.state import (
    CameraState, ClassifierState, EvapControlState, MistralState, PowerSupplyState,
    PyrometerState, RheedQcState, TemperatureState, WeakPrimaryShadowState,
)
from gui.heater_control.heater_commands import (
    SAFETY_COMMANDS,
    PowerSupplyCommand,
    PowerSupplyCommandResult,
)
from gui.heater_control.heater_session import utc_now_iso

# ---------------------------------------------------------------------------
# Optional heater-control deps (pyvisa + owon_power_supply)
# ---------------------------------------------------------------------------
# These are used ONLY by PowerSupplyWorker (the OWON PSU driver for the
# separate Heater Control Dashboard). Making them optional here means
# importing gui.workers stays cheap on machines that don't have pyvisa
# or the owon_power_supply local module — Bulbasaur's Growth Monitor,
# CI runners, and any Mac dev workflow that doesn't touch the heater
# dashboard. PowerSupplyWorker guards its actual usage below by
# checking these for None.
try:
    import pyvisa  # noqa: F401
except ImportError:  # pragma: no cover — env-specific
    pyvisa = None  # type: ignore[assignment]

try:
    from owon_power_supply import OWONPowerSupply
except ImportError:  # pragma: no cover — env-specific
    OWONPowerSupply = None  # type: ignore[assignment]


class PowerSupplyWorker(QThread):
    """Background thread for power supply communication.

    Requires the ``pyvisa`` package + the ``owon_power_supply`` local
    module. Both are guarded at module import (see top of file); if
    either is unavailable, this worker cannot be instantiated and
    raises ``ImportError`` at ``__init__`` time. Growth Monitor never
    constructs this class — it's used by the separate Heater Control
    Dashboard.
    """

    state_updated = pyqtSignal(PowerSupplyState)
    command_completed = pyqtSignal(object)  # PowerSupplyCommandResult
    system_event = pyqtSignal(object)  # append-only audit event payload

    SAFE_VOLTAGE_TOLERANCE = 0.005
    SAFE_CURRENT_TOLERANCE = 0.005
    SAFE_TRANSACTION_TIMEOUT_S = 15.0

    def __init__(
        self,
        resource: str,
        poll_interval: float = 0.5,
        *,
        psu_factory=None,
    ):
        if psu_factory is None and (pyvisa is None or OWONPowerSupply is None):
            raise ImportError(
                "PowerSupplyWorker requires pyvisa + owon_power_supply. "
                "Install pyvisa (`pip install pyvisa`) and make sure "
                "owon_power_supply.py is on PYTHONPATH."
            )
        super().__init__()
        self.resource = resource
        self.poll_interval = poll_interval
        self._psu_factory = psu_factory or OWONPowerSupply
        self.psu = None
        # Set in __init__ (not run()) to close the stop()-before-run
        # race — if stop() fires between start() and the first line of
        # run(), it must not be undone by a re-assignment. See
        # worker_run_race_followup memory for the full rationale.
        self.running = True
        self._stop_event = threading.Event()
        self._command_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._queue_counter = itertools.count()
        self._queue_state_lock = threading.RLock()
        self._pending_safety_count = 0
        self._safety_epoch = 0
        self._commands_gated = True
        self._consecutive_failures = 0
        self._poll_counter = 0
        self._connection_generation = 0

    def run(self):
        """Main worker loop."""
        state = PowerSupplyState()

        # Connect
        try:
            self.psu = self._psu_factory(self.resource)
            self.psu.connect()
            self._connection_generation += 1
            state.connection_generation = self._connection_generation
            state.connected = True
        except Exception as e:
            self.running = False
            state.connected = False
            state.error = str(e)
            state.valid = False
            self.state_updated.emit(_emission_snapshot(state))
            self._reject_all_pending(f"PSU connection failed: {e}")
            return

        # Main polling loop
        while self.running:
            loop_started_ns = time.perf_counter_ns()
            safety_processed = self._process_queued_commands(state)
            if safety_processed:
                # A safety command's forced readback is the authoritative
                # result for this cycle. Do not immediately issue unrelated
                # polling queries after it.
                self._update_secondary_ages(state, time.perf_counter_ns())
                self.state_updated.emit(_emission_snapshot(state))
                self._stop_event.wait(self.poll_interval)
                continue

            read_started_ns = time.perf_counter_ns()
            try:
                # MEAS:ALL? is the primary sample. Its success is sufficient
                # for valid V/I/P telemetry; secondary queries below cannot
                # retroactively invalidate it.
                v, i, p = self.psu.measure_all()
                state.voltage_measured = v
                state.current_measured = i
                state.power_measured = p
                state.connected = True
                state.error = ""
                self._consecutive_failures = 0
                primary_completed_ns = time.perf_counter_ns()
                state.primary_completed_at_utc = _utc_iso_now()
                state.primary_completed_monotonic_ns = primary_completed_ns
                state.primary_read_duration_ms = max(
                    0.0,
                    (primary_completed_ns - read_started_ns) / 1_000_000.0,
                )

            except Exception as e:
                _mark_read_failed(state, e, read_started_ns)
                self._consecutive_failures += 1
                self._emit_system_event(
                    "primary_poll_failed", str(e), state.connection_generation,
                )

                # If repeated VISA timeouts happen, force reconnect path
                if self._is_timeout_error(e) and self._consecutive_failures >= 3:
                    self._reject_pending_normal("PSU reconnect in progress")
                    state.connected = False
                    self._emit_system_event(
                        "reconnect_started", str(e), state.connection_generation,
                    )
                    if self._reconnect():
                        self._connection_generation += 1
                        state.connection_generation = self._connection_generation
                        state.connected = True
                        state.error = ""
                        state.valid = False
                        self._consecutive_failures = 0
                        self._poll_counter = 0
                        with self._queue_state_lock:
                            self._commands_gated = True
                        self._reset_secondary_freshness(state)
                        self._emit_system_event(
                            "reconnect_succeeded",
                            "waiting for first valid primary sample",
                            state.connection_generation,
                        )
                    else:
                        self._emit_system_event(
                            "reconnect_failed", str(e), state.connection_generation,
                        )

                # Secondary values deliberately retain their last readback on
                # a failed primary poll.  Their ages must still advance so a
                # consumer cannot mistake that retained value for a fresh
                # setting/output observation.
                self._update_secondary_ages(state, time.perf_counter_ns())
                self.state_updated.emit(_emission_snapshot(state))
                elapsed_s = (
                    time.perf_counter_ns() - loop_started_ns
                ) / 1_000_000_000.0
                self._stop_event.wait(max(0.0, self.poll_interval - elapsed_s))
                continue

            secondary_errors = []
            if not self._refresh_secondary(
                state, "output", "output_enabled", self.psu.get_output_state,
            ):
                secondary_errors.append("output")

            if self._poll_counter % 5 == 0:
                secondary_specs = (
                    ("voltage_setpoint", "voltage_setpoint", self.psu.get_voltage_setpoint),
                    ("current_setpoint", "current_setpoint", self.psu.get_current_setpoint),
                    ("ovp", "ovp_limit", self.psu.get_ovp),
                    ("ocp", "ocp_limit", self.psu.get_ocp),
                )
                for freshness_name, value_name, getter in secondary_specs:
                    if not self._refresh_secondary(
                        state, freshness_name, value_name, getter,
                    ):
                        secondary_errors.append(freshness_name)

            self._poll_counter += 1
            state.error = (
                "secondary query failed: " + ", ".join(secondary_errors)
                if secondary_errors else ""
            )
            # ``received_*`` describes the end of the complete host query
            # round.  The earlier MEAS:ALL? completion remains separately
            # available through ``primary_completed_*``.
            now_ns = time.perf_counter_ns()
            state.acquire_started_monotonic_ns = read_started_ns
            state.received_at_utc = _utc_iso_now()
            state.received_monotonic_ns = now_ns
            state.read_duration_ms = max(
                0.0, (now_ns - read_started_ns) / 1_000_000.0,
            )
            state.sample_sequence += 1
            state.valid = True
            self._update_secondary_ages(state, now_ns)
            with self._queue_state_lock:
                if self._commands_gated:
                    self._commands_gated = False
                    self._emit_system_event(
                        "connection_generation_ready",
                        "first valid primary sample received",
                        state.connection_generation,
                    )

            self.state_updated.emit(_emission_snapshot(state))
            elapsed_s = (time.perf_counter_ns() - loop_started_ns) / 1_000_000_000.0
            self._stop_event.wait(max(0.0, self.poll_interval - elapsed_s))

        # Cleanup
        self.running = False
        self._reject_all_pending("power supply worker stopped")
        if self.psu:
            try:
                self.psu.disconnect()
            except Exception:
                pass

    def _is_timeout_error(self, exc: Exception) -> bool:
        """Return True if exception is a VISA timeout."""
        if pyvisa is not None and isinstance(exc, pyvisa.errors.VisaIOError):
            return exc.error_code == pyvisa.constants.VI_ERROR_TMO
        return "VI_ERROR_TMO" in str(exc)

    def _reconnect(self) -> bool:
        """Attempt to re-establish PSU connection after repeated failures."""
        try:
            if self.psu:
                try:
                    self.psu.disconnect()
                except Exception:
                    pass
            self.psu = self._psu_factory(self.resource)
            self.psu.connect()
            return True
        except Exception:
            return False

    def _emit_system_event(
        self, action: str, error: str, connection_generation: int,
    ) -> None:
        self.system_event.emit({
            "action": action,
            "error": error,
            "connection_generation": connection_generation,
            "event_at_utc": _utc_iso_now(),
            "event_monotonic_ns": time.perf_counter_ns(),
        })

    def _refresh_secondary(
        self,
        state: PowerSupplyState,
        freshness_name: str,
        value_name: str,
        getter,
    ) -> bool:
        try:
            value = getter()
            setattr(state, value_name, value)
            self._stamp_secondary(state, freshness_name)
            return True
        except Exception as exc:
            if freshness_name == "output":
                state.output_valid = False
            self._emit_system_event(
                f"secondary_{freshness_name}_failed",
                str(exc),
                state.connection_generation,
            )
            return False

    @staticmethod
    def _stamp_secondary(state: PowerSupplyState, name: str) -> None:
        now_ns = time.perf_counter_ns()
        sequence_name = f"{name}_sample_sequence"
        setattr(state, sequence_name, getattr(state, sequence_name) + 1)
        setattr(state, f"{name}_received_at_utc", _utc_iso_now())
        setattr(state, f"{name}_received_monotonic_ns", now_ns)
        if name == "output":
            state.output_valid = True
        if name in {"voltage_setpoint", "current_setpoint", "ovp", "ocp"}:
            state.settings_sample_sequence += 1
            state.settings_received_at_utc = _utc_iso_now()
            state.settings_received_monotonic_ns = now_ns

    @staticmethod
    def _update_secondary_ages(state: PowerSupplyState, now_ns: int) -> None:
        for name in (
            "output", "voltage_setpoint", "current_setpoint", "ovp", "ocp",
        ):
            received_ns = getattr(state, f"{name}_received_monotonic_ns")
            age = None
            if received_ns is not None:
                age = max(0.0, (now_ns - received_ns) / 1_000_000.0)
            setattr(state, f"{name}_age_ms", age)
        if state.settings_received_monotonic_ns is not None:
            state.settings_age_ms = max(
                0.0,
                (now_ns - state.settings_received_monotonic_ns) / 1_000_000.0,
            )

    @staticmethod
    def _reset_secondary_freshness(state: PowerSupplyState) -> None:
        """Invalidate field provenance when a new connection generation starts."""
        state.settings_sample_sequence = 0
        state.settings_received_at_utc = None
        state.settings_received_monotonic_ns = None
        state.settings_age_ms = None
        state.output_valid = False
        for name in (
            "output", "voltage_setpoint", "current_setpoint", "ovp", "ocp",
        ):
            setattr(state, f"{name}_sample_sequence", 0)
            setattr(state, f"{name}_received_at_utc", None)
            setattr(state, f"{name}_received_monotonic_ns", None)
            setattr(state, f"{name}_age_ms", None)

    def _process_queued_commands(self, state: PowerSupplyState) -> bool:
        safety_processed = False
        while self.running:
            try:
                priority, sequence, command = self._command_queue.get_nowait()
            except queue.Empty:
                return safety_processed
            if safety_processed and not command.safety_critical:
                # Never execute a newly-arrived ordinary command in the same
                # loop turn as a safety transaction.  Re-queue it for the
                # next turn, after the confirmed safe snapshot has first been
                # emitted to the GUI/audit consumers.
                self._command_queue.put((priority, sequence, command))
                return True
            result = self._execute_transaction(command, state)
            self.command_completed.emit(result)
            self._command_queue.task_done()
            if command.safety_critical:
                safety_processed = True
                with self._queue_state_lock:
                    self._pending_safety_count = max(
                        0, self._pending_safety_count - 1,
                    )
        return safety_processed

    def _execute_transaction(
        self, command: PowerSupplyCommand, state: PowerSupplyState,
    ) -> PowerSupplyCommandResult:
        requested = {"args": list(command.args)}
        effective = {}
        readback = {}
        error = ""
        status = "CONFIRMED"
        started_ns = time.perf_counter_ns()

        try:
            if not self.psu:
                raise RuntimeError("power supply is not connected")

            cmd = command.command
            args = command.args
            if cmd == "set_voltage":
                value = float(args[0])
                effective = {"voltage_setpoint": value}
                self.psu.set_voltage(value)
                actual = self.psu.get_voltage_setpoint()
                readback = {"voltage_setpoint": actual}
                if abs(actual - value) > self.SAFE_VOLTAGE_TOLERANCE:
                    raise RuntimeError(
                        f"voltage readback {actual:.6g} V does not confirm {value:.6g} V"
                    )
                state.voltage_setpoint = actual
                self._stamp_secondary(state, "voltage_setpoint")
            elif cmd == "set_current":
                value = float(args[0])
                effective = {"current_setpoint": value}
                self.psu.set_current(value)
                actual = self.psu.get_current_setpoint()
                readback = {"current_setpoint": actual}
                if abs(actual - value) > self.SAFE_CURRENT_TOLERANCE:
                    raise RuntimeError(
                        f"current readback {actual:.6g} A does not confirm {value:.6g} A"
                    )
                state.current_setpoint = actual
                self._stamp_secondary(state, "current_setpoint")
            elif cmd in ("output_on", "output_off"):
                expected = cmd == "output_on"
                effective = {"output_enabled": expected}
                (self.psu.output_on if expected else self.psu.output_off)()
                actual = bool(self.psu.get_output_state())
                readback = {"output_enabled": actual}
                if actual is not expected:
                    raise RuntimeError(
                        f"output readback {actual!r} does not confirm {expected!r}"
                    )
                state.output_enabled = actual
                self._stamp_secondary(state, "output")
            elif cmd == "set_ovp":
                value = float(args[0])
                effective = {"ovp_limit": value}
                self.psu.set_ovp(value)
                actual = self.psu.get_ovp()
                readback = {"ovp_limit": actual}
                if abs(actual - value) > 0.01:
                    raise RuntimeError("OVP readback mismatch")
                state.ovp_limit = actual
                self._stamp_secondary(state, "ovp")
            elif cmd == "set_ocp":
                value = float(args[0])
                effective = {"ocp_limit": value}
                self.psu.set_ocp(value)
                actual = self.psu.get_ocp()
                readback = {"ocp_limit": actual}
                if abs(actual - value) > self.SAFE_CURRENT_TOLERANCE:
                    raise RuntimeError("OCP readback mismatch")
                state.ocp_limit = actual
                self._stamp_secondary(state, "ocp")
            elif cmd in SAFETY_COMMANDS:
                effective = {
                    "output_enabled": False,
                    "voltage_setpoint": 0.0,
                    "current_setpoint": 0.0,
                }
                # Keep this exact order: remove output energy first, then erase
                # both setpoints, then query all three safety predicates.
                self.psu.output_off()
                self.psu.set_voltage(0.0)
                self.psu.set_current(0.0)
                readback = {
                    "output_enabled": bool(self.psu.get_output_state()),
                    "voltage_setpoint": float(self.psu.get_voltage_setpoint()),
                    "current_setpoint": float(self.psu.get_current_setpoint()),
                }
                state.output_enabled = readback["output_enabled"]
                state.voltage_setpoint = readback["voltage_setpoint"]
                state.current_setpoint = readback["current_setpoint"]
                self._stamp_secondary(state, "output")
                self._stamp_secondary(state, "voltage_setpoint")
                self._stamp_secondary(state, "current_setpoint")
                safe = (
                    not readback["output_enabled"]
                    and abs(readback["voltage_setpoint"])
                    <= self.SAFE_VOLTAGE_TOLERANCE
                    and abs(readback["current_setpoint"])
                    <= self.SAFE_CURRENT_TOLERANCE
                )
                if not safe:
                    raise RuntimeError("safe shutdown readback did not reach zero/off")
                if (
                    time.perf_counter_ns() - started_ns
                    > self.SAFE_TRANSACTION_TIMEOUT_S * 1_000_000_000
                ):
                    raise TimeoutError("safe shutdown exceeded 15 second deadline")
            else:
                status = "REJECTED"
                raise ValueError(f"unknown PSU command: {cmd}")
        except Exception as exc:
            if status != "REJECTED":
                status = "FAILED"
            error = str(exc)

        return PowerSupplyCommandResult(
            request_id=command.request_id,
            source=command.source,
            command=command.command,
            status=status,
            requested=requested,
            effective=effective,
            readback=readback,
            error=error,
            completed_at_utc=utc_now_iso(),
            completed_monotonic_ns=time.perf_counter_ns(),
            confirmation_sample_sequence=state.sample_sequence,
            connection_generation=state.connection_generation,
            safety_epoch=command.safety_epoch,
        )

    def queue_command(
        self,
        cmd: str,
        *args,
        request_id: Optional[str] = None,
        source: str = "system",
        requested_at_utc: Optional[str] = None,
        requested_monotonic_ns: Optional[int] = None,
    ) -> str:
        """Queue a transaction and return its correlation id.

        Safety transactions pre-empt and reject ordinary queued work.  Calls
        made while a safety transaction is pending are rejected immediately.
        """
        request_id = request_id or str(uuid.uuid4())
        rejected = []
        with self._queue_state_lock:
            safety = cmd in SAFETY_COMMANDS
            if safety:
                self._safety_epoch += 1
                safety_epoch = self._safety_epoch
            else:
                safety_epoch = 0
            command = PowerSupplyCommand(
                request_id=request_id,
                source=source,
                command=cmd,
                args=tuple(args),
                requested_at_utc=requested_at_utc or utc_now_iso(),
                requested_monotonic_ns=(
                    requested_monotonic_ns
                    if requested_monotonic_ns is not None
                    else time.perf_counter_ns()
                ),
                priority=0 if safety else 10,
                safety_epoch=safety_epoch,
            )
            if not self.running or self.isFinished():
                rejected.append(self._rejected(command, "power supply worker is stopped"))
            elif safety:
                self._pending_safety_count += 1
                rejected.extend(
                    self._drain_pending_normal_locked(
                        "superseded by safety shutdown",
                    )
                )
                self._command_queue.put(
                    (command.priority, next(self._queue_counter), command)
                )
            elif self._pending_safety_count > 0:
                rejected.append(self._rejected(command, "safety shutdown pending"))
            elif self._commands_gated:
                rejected.append(self._rejected(
                    command,
                    "commands gated until first valid sample in connection generation",
                ))
            else:
                self._command_queue.put(
                    (command.priority, next(self._queue_counter), command)
                )
        for result in rejected:
            self.command_completed.emit(result)
        return request_id

    def request_safe_shutdown(
        self, *, request_id: Optional[str] = None, source: str = "system",
    ) -> str:
        return self.queue_command(
            "safe_shutdown", request_id=request_id, source=source,
        )

    def _reject_pending_normal(self, reason: str) -> None:
        with self._queue_state_lock:
            rejected = self._drain_pending_normal_locked(reason)
        for result in rejected:
            self.command_completed.emit(result)

    def _drain_pending_normal_locked(self, reason: str) -> list:
        retained = []
        rejected = []
        while True:
            try:
                item = self._command_queue.get_nowait()
            except queue.Empty:
                break
            command = item[2]
            if command.safety_critical:
                retained.append(item)
            else:
                rejected.append(self._rejected(command, reason))
            self._command_queue.task_done()
        for item in retained:
            self._command_queue.put(item)
        return rejected

    def _reject_all_pending(self, reason: str) -> None:
        rejected = []
        with self._queue_state_lock:
            while True:
                try:
                    _priority, _sequence, command = self._command_queue.get_nowait()
                except queue.Empty:
                    break
                if command.safety_critical:
                    self._pending_safety_count = max(
                        0, self._pending_safety_count - 1,
                    )
                rejected.append(self._rejected(command, reason))
                self._command_queue.task_done()
        for result in rejected:
            self.command_completed.emit(result)

    @staticmethod
    def _rejected(command: PowerSupplyCommand, reason: str) -> PowerSupplyCommandResult:
        return PowerSupplyCommandResult(
            request_id=command.request_id,
            source=command.source,
            command=command.command,
            status="REJECTED",
            requested={"args": list(command.args)},
            effective={},
            readback={},
            error=reason,
            completed_at_utc=utc_now_iso(),
            completed_monotonic_ns=time.perf_counter_ns(),
            safety_epoch=command.safety_epoch,
        )

    def stop(self):
        """Stop the worker thread."""
        with self._queue_state_lock:
            self.running = False
        self._stop_event.set()


class ThermocoupleWorker(QThread):
    """Background thread for Dracal TMC100k thermocouple communication."""

    state_updated = pyqtSignal(TemperatureState)

    def __init__(self, port: Optional[str] = None, poll_interval: float = 0.5):
        super().__init__()
        self.port = port
        self.poll_interval = poll_interval
        # True from __init__ to close the stop()-before-run race
        # (see PowerSupplyWorker for the full comment).
        self.running = True
        self._sensor = None

    def run(self):
        """Main worker loop."""
        state = TemperatureState()

        # Import here to avoid hard dependency at module level
        try:
            from dracal_tmc100k import DracalTMC100k, find_dracal_sensors
        except ImportError:
            state.error = "dracal_tmc100k module not available"
            self.state_updated.emit(_emission_snapshot(state))
            return

        # Auto-detect port if not specified
        port = self.port
        if not port:
            try:
                sensors = find_dracal_sensors()
                if sensors:
                    port = sensors[0][0]
                    state.device_info = sensors[0][1]
            except Exception:
                pass

        if not port:
            state.error = "No Dracal TMC100k sensor detected."
            self.state_updated.emit(_emission_snapshot(state))
            return

        # Connect
        try:
            self._sensor = DracalTMC100k(port=port)
            self._sensor.connect()
            state.connected = True

            # Get device info
            try:
                info = self._sensor.get_info()
                state.product_id = info.get("product_id", "")
                state.serial_number = info.get("serial_number", "")
                state.device_info = f"{state.product_id} {state.serial_number}".strip()
            except Exception:
                state.device_info = port

            self.state_updated.emit(state)
        except Exception as e:
            state.connected = False
            state.error = str(e)
            self.state_updated.emit(state)
            return

        # Main polling loop
        while self.running:
            try:
                tc_temp = self._sensor.read_temperature()
                state.temperature = tc_temp

                try:
                    state.cold_junction = self._sensor.read_cold_junction()
                except RuntimeError:
                    pass

                state.connected = True
                state.error = ""
                self.state_updated.emit(state)

            except Exception as e:
                state.error = str(e)
                self.state_updated.emit(state)

            time.sleep(self.poll_interval)

        # Cleanup
        if self._sensor:
            try:
                self._sensor.disconnect()
            except Exception:
                pass

    def stop(self):
        """Stop the thermocouple worker thread."""
        self.running = False


class RheedCameraWorker(QThread):
    """Background thread for RHEED camera frame acquisition."""

    state_updated = pyqtSignal(CameraState)

    # Lower bound on the starvation deadlines, in seconds.
    #
    # The deadline scales with the trigger period (three periods), but a fast
    # trigger must not make the GUI twitchy about a camera that is merely slow
    # to produce its first frame — SDK startup, exposure application and the
    # first integration all land inside that window. Five seconds is long
    # enough to cover a 500 ms exposure plus SDK warm-up and short enough that
    # a grower does not sit in front of a dead preview wondering.
    MIN_FRAME_DEADLINE_S = 5.0

    def _frame_deadline_s(self) -> float:
        """Seconds of silence tolerated before the camera is called dead.

        Three trigger periods, floored by MIN_FRAME_DEADLINE_S. Three rather
        than one so a single missed callback — which happens routinely — is
        never enough on its own to tear down an arm.
        """
        period = 1.0 / self.trigger_hz if self.trigger_hz > 0 else 1.0
        return max(3.0 * period, self.MIN_FRAME_DEADLINE_S)

    def __init__(
        self,
        mode: str = "dummy",
        poll_interval: float = 1.0,
        *,
        camera_index: int = 0,
        trigger_hz: float = 1.0,
        exposure_us: Optional[float] = None,
    ):
        super().__init__()
        self.mode = mode
        self.poll_interval = poll_interval
        self.camera_index = camera_index
        self.trigger_hz = trigger_hz
        self.exposure_us = exposure_us
        # True from __init__ to close the stop()-before-run race
        # (see PowerSupplyWorker for the full comment).
        self.running = True
        self._camera = None

    @property
    def sensor_settings_at_connect(self) -> dict:
        """Defensive copy of the direct camera's open-time settings.

        Screengrab and dummy drivers expose no hardware feature snapshot and
        therefore return an empty mapping.  The Vimba driver deliberately
        retains its snapshot after disconnect so session shutdown can still
        persist it.
        """
        try:
            settings = getattr(
                self._camera, "sensor_settings_at_connect", None,
            )
            return deepcopy(settings or {})
        except Exception as exc:  # noqa: BLE001 — provenance is non-fatal
            log.warning(
                "RHEED camera-open settings unavailable during shutdown: %s",
                exc,
            )
            return {}

    def run(self):
        """Main worker loop — connect camera and emit frames."""
        backend = {
            "screengrab": "wgc",
            "screengrab_mss": "mss",
        }.get(self.mode, self.mode)
        state = CameraState(mode=self.mode, capture_backend=backend)

        # Create camera driver based on mode
        try:
            camera = self._create_camera()
            # Publish BEFORE connect() so stop() can reach it, then re-check
            # `running`: a DISARM landing between _create_camera() and this
            # assignment used to find self._camera still None, drop the
            # cancellation on the floor, and let connect() proceed for the
            # full 45 s timeout — writing exposure and starting a stream after
            # the grower had already disarmed.
            self._camera = camera
            if not self.running:
                request_stop = getattr(camera, "request_stop", None)
                if callable(request_stop):
                    request_stop()
                raise RuntimeError(
                    "Camera connect cancelled before it began (DISARM)"
                )
            self._camera.connect()
            state.connected = True
            state.exposure_us = getattr(self._camera, "exposure_us", None)
        except Exception as e:
            state.connected = False
            state.error = str(e)
            if self._camera is not None:
                try:
                    self._camera.disconnect()
                except Exception:
                    pass
            self.state_updated.emit(_emission_snapshot(state))
            return

        frame_count = 0
        fps_start = time.time()
        fps_frame_count = 0
        # Reference point for the starvation deadline. Starts at connect so an
        # arm that never produces a first frame is bounded; each valid frame
        # moves it forward so a mid-session stall is bounded too.
        last_progress_monotonic = time.monotonic()
        starvation_reported = False

        while self.running:
            read_started_ns = time.perf_counter_ns()
            try:
                frame = self._camera.read_frame()
                frame_count += 1
                fps_frame_count += 1

                # Compute FPS over a rolling 1-second window
                elapsed = time.time() - fps_start
                if elapsed >= 1.0:
                    state.fps = fps_frame_count / elapsed
                    fps_start = time.time()
                    fps_frame_count = 0

                state.frame = frame
                state.frame_number = frame_count
                state.height, state.width = frame.shape[:2]
                state.intensity = _frame_luminance(frame)
                state.connected = True
                state.error = ""
                geometry_id = getattr(
                    self._camera, "capture_geometry_id", None,
                )
                if geometry_id is None and self.mode not in {"vimba", "direct"}:
                    geometry_id = f"{self.mode}:full-frame"
                # Direct Vimba geometry is safety/provenance-critical. If the
                # driver could not prove every spatial readback, propagate an
                # empty ID; dimensions alone must never become a fabricated
                # full-frame identity.
                state.capture_geometry_id = str(geometry_id or "")
                capture = getattr(self._camera, "last_capture", None)
                if capture is not None:
                    state.capture_backend = capture.backend
                    state.captured_at_utc = capture.captured_at_utc
                    state.capture_sequence = capture.sequence
                    state.frame_age_ms = capture.age_ms()
                    state.source_hwnd = capture.source_hwnd
                    state.captured_monotonic_ns = capture.captured_monotonic_ns
                else:
                    state.capture_backend = self.mode
                    state.captured_at_utc = (
                        datetime.now(timezone.utc)
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z")
                    )
                    state.capture_sequence = frame_count
                    state.frame_age_ms = 0.0
                    state.source_hwnd = 0
                    state.captured_monotonic_ns = time.perf_counter_ns()

                read_finished_ns = time.perf_counter_ns()
                state.acquire_started_monotonic_ns = read_started_ns
                state.received_at_utc = state.captured_at_utc
                state.received_monotonic_ns = state.captured_monotonic_ns
                state.sample_sequence = state.capture_sequence
                state.read_duration_ms = max(
                    0.0, (read_finished_ns - read_started_ns) / 1_000_000.0,
                )
                state.valid = True
                # A real frame is the only thing that counts as progress.
                # Reset both the deadline reference and the reported flag so a
                # camera that recovers is announced healthy again, and a later
                # stall is reported afresh rather than swallowed by the
                # earlier one.
                last_progress_monotonic = time.monotonic()
                if starvation_reported:
                    starvation_reported = False
                    log.info(
                        "RHEED camera recovered: frame %d delivered after a "
                        "starvation report", frame_count,
                    )

            except FrameNotYetAvailableError as e:
                # Direct Vimba reads are edge-triggered: an SDK callback must
                # have delivered a new frame since the previous read. A gap
                # between callbacks is NORMAL and must stay quiet — emitting an
                # error state here would make every ordinary poll look like a
                # camera fault, and (since a refused ARM now disarms) would
                # tear down the arm during routine warm-up.
                #
                # But the driver raises this for two different situations, and
                # only one of them is benign. "No frame yet" also covers a
                # camera that never delivers a first frame and one whose
                # callbacks have stalled. Retrying those forever would leave
                # the GUI armed, silent, and showing nothing. START is now
                # gated on a live frame rather than the armed state alone, so
                # this deadline is also what RE-CLOSES that gate when delivery
                # stops mid-arm — without it, a camera that dies after one
                # good frame would leave START enabled indefinitely.
                #
                # So: quiet inside the deadline, explicit failure past it.
                idle_s = time.monotonic() - last_progress_monotonic
                deadline_s = self._frame_deadline_s()
                if idle_s < deadline_s:
                    time.sleep(self.poll_interval)
                    continue
                if not starvation_reported:
                    starvation_reported = True
                    had_frame = frame_count > 0
                    log.error(
                        "RHEED camera produced no %s frame for %.1fs "
                        "(deadline %.1fs): %s",
                        "new" if had_frame else "first", idle_s, deadline_s, e,
                    )
                    _mark_read_failed(state, (
                        f"No {'new' if had_frame else 'first'} RHEED frame in "
                        f"{idle_s:.0f}s. The camera is connected but not "
                        f"delivering; check kSA/Vimba access and the trigger."
                    ), read_started_ns)
                    state.frame = None
                    state.connected = False
                    self.state_updated.emit(_emission_snapshot(state))
                # Keep polling rather than exiting the loop. A later callback
                # can still recover the camera, and during a running session
                # sensor logging must continue regardless.
                time.sleep(self.poll_interval)
                continue
            except Exception as e:
                _mark_read_failed(state, e)
                state.acquire_started_monotonic_ns = read_started_ns
                state.read_duration_ms = max(
                    0.0,
                    (time.perf_counter_ns() - read_started_ns) / 1_000_000.0,
                )
                state.frame = None
                # A failed read is never a connected/usable observation.
                # Recovering backends may retry on the next loop; a later
                # successful frame sets ``connected=True`` again.
                state.connected = False
                if self.mode == "screengrab":
                    self.state_updated.emit(_emission_snapshot(state))
                    self.running = False
                    break

            # CameraState is a Python object carried through a queued Qt
            # signal. Emit a fresh dataclass snapshot so the next worker
            # iteration cannot mutate image provenance before the GUI reads it.
            self.state_updated.emit(_emission_snapshot(state))
            time.sleep(self.poll_interval)

        # Cleanup
        if self._camera is not None:
            try:
                self._camera.disconnect()
            except Exception:
                pass

    def _create_camera(self):
        """Factory method — import and instantiate camera driver."""
        if self.mode in ("vimba", "direct"):
            from drivers.rheed_camera import VmbCamera
            return VmbCamera(
                camera_index=self.camera_index,
                trigger_hz=self.trigger_hz,
                exposure_us=self.exposure_us,
            )
        elif self.mode == "screengrab":
            from drivers.rheed_camera import ScreenGrabCamera
            return ScreenGrabCamera()
        elif self.mode == "screengrab_mss":
            from drivers.rheed_camera import ScreenGrabCamera
            return ScreenGrabCamera.legacy_mss()
        else:
            from drivers.rheed_camera import DummyCamera
            preset = self.mode if self.mode in DummyCamera.PRESETS else None
            return DummyCamera(preset=preset)

    def stop(self):
        """Stop the camera worker thread.

        Clearing `running` alone was not enough. A worker blocked inside
        VmbCamera.connect() — which waits up to CONNECT_TIMEOUT_S for the
        setup thread — never reaches the loop condition, so the driver's own
        stop event stayed unset and setup carried on writing exposure and
        starting streams after the grower pressed DISARM.

        request_stop() is non-blocking by contract: it signals cancellation
        and returns. Joining and SDK teardown stay in the worker thread, so
        DISARM never stalls the GUI thread on a wedged camera.
        """
        self.running = False
        camera = self._camera
        if camera is not None:
            request_stop = getattr(camera, "request_stop", None)
            if callable(request_stop):
                try:
                    request_stop()
                except Exception:  # noqa: BLE001 — never let DISARM raise
                    log.debug("camera.request_stop() raised", exc_info=True)


class PyrometerWorker(QThread):
    """Background thread for pyrometer temperature polling."""

    state_updated = pyqtSignal(PyrometerState)

    def __init__(
        self,
        mode: str = "dummy",
        poll_interval: float = 0.5,
        samples_per_poll: int = 5,
        port: str = "COM4",
        baudrate: int = 115200,
        device_id: int = 1,
        rts: Optional[bool] = None,
        modbus_backend: str = "pymodbus",
    ):
        super().__init__()
        self.mode = mode
        self.poll_interval = poll_interval
        # RTS line state for the Modbus driver, from
        # MBESystemConfig.pyrometer_rts. None means "no decision recorded
        # for this chamber" and leaves the serial library alone; the
        # driver logs a warning naming the field when it sees None.
        self.rts = rts
        self.modbus_backend = modbus_backend
        # Number of rapid sub-readings to average per poll cycle. 5 ≈ 0.5 s
        # at the Exactus default rate (~10 reads/s); for screengrab mode it
        # samples whatever jitter the GUI exposes between refreshes.
        self.samples_per_poll = max(1, int(samples_per_poll))
        self.port = port
        self.baudrate = baudrate
        self.device_id = int(device_id)
        # True from __init__ to close the stop()-before-run race
        # (see PowerSupplyWorker for the full comment).
        self.running = True
        self._sensor = None

    def run(self):
        """Main worker loop — connect pyrometer and emit temperatures."""
        state = PyrometerState(mode=self.mode)

        try:
            self._sensor = self._create_sensor()
            self._sensor.connect()
            state.connected = True

            # Get device info if available
            if hasattr(self._sensor, "get_info"):
                try:
                    info = self._sensor.get_info()
                    parts = []
                    if "name" in info:
                        parts.append(info["name"])
                    if "serial" in info:
                        parts.append(f"S/N: {info['serial']}")
                    state.device_info = " | ".join(parts) if parts else self.mode
                except Exception:
                    state.device_info = self.mode
            else:
                state.device_info = self.mode

            self.state_updated.emit(_emission_snapshot(state))

        except Exception as e:
            state.connected = False
            state.error = str(e)
            self.state_updated.emit(_emission_snapshot(state))
            return

        while self.running:
            # Take N rapid sub-readings within each poll cycle so the
            # emitted state carries a mean ± std rather than a single
            # noisy point estimate. Polybot-inspired statistical
            # consistency without a second analysis pass.
            read_started_ns = time.perf_counter_ns()
            readings: list[float] = []
            subread_ns: list[int] = []
            read_error: Optional[Exception] = None
            for _ in range(self.samples_per_poll):
                try:
                    readings.append(self._sensor.read_temperature())
                    subread_ns.append(time.perf_counter_ns())
                except Exception as e:
                    read_error = e
                    break
            complete_batch = len(readings) == self.samples_per_poll
            arr = None
            if complete_batch:
                try:
                    arr = np.asarray(readings, dtype=float)
                    if (
                        arr.size != self.samples_per_poll
                        or not np.isfinite(arr).all()
                    ):
                        raise ValueError(
                            "pyrometer batch contains a non-finite reading"
                        )
                except (TypeError, ValueError) as exc:
                    read_error = exc
                    complete_batch = False
            if complete_batch:
                assert arr is not None
                state.temperature = float(arr.mean())
                state.temperature_std = float(arr.std(ddof=0)) if arr.size > 1 else 0.0
                state.temperature_n = int(arr.size)
                state.connected = True
                state.error = ""
                state.subread_monotonic_ns = tuple(subread_ns)
                state.sample_span_ms = (
                    (subread_ns[-1] - subread_ns[0]) / 1_000_000.0
                    if len(subread_ns) > 1 else 0.0
                )
            else:
                _mark_read_failed(state, read_error, read_started_ns)
                # Batch entirely failed. Reset the reading fields to their
                # None sentinels so we don't leak the previous cycle's
                # temperature into the next emission timestamp. `connected`
                # stays True because the sensor object is still open — only
                # the reading itself is stale. `state.error` was set in the
                # except clause above.
                state.temperature = None
                state.temperature_std = None
                state.temperature_n = 0
                state.subread_monotonic_ns = ()
                state.sample_span_ms = None

            # Emissivity is a slow-moving config value — single read is fine.
            if hasattr(self._sensor, "read_emissivity"):
                try:
                    state.emissivity = self._sensor.read_emissivity()
                except Exception:
                    pass

            if complete_batch:
                _mark_sample_received(state, read_started_ns)
            # PyQt queues custom Python objects by reference. Emit a fresh
            # dataclass so the next poll cannot mutate timing provenance
            # before the GUI/logger consumes this sample.
            self.state_updated.emit(_emission_snapshot(state))
            time.sleep(self.poll_interval)

        # Cleanup
        if self._sensor is not None:
            try:
                self._sensor.disconnect()
            except Exception:
                pass

    def _create_sensor(self):
        """Factory method — import and instantiate pyrometer driver."""
        if self.mode == "modbus":
            from drivers.pyrometer import ModbusPyrometer
            # Forward the worker's own inputs, exactly as the exactus
            # branch below does. This factory used to call
            # ModbusPyrometer() bare, discarding all three: on Jul 30 2026
            # the O-MBE GUI logged "RTS not configured for this chamber"
            # and its Modbus reads failed, seconds after the standalone
            # scripts had read the probe on the same port. The chamber
            # config was correct; the worker threw it away.
            return ModbusPyrometer(
                port=self.port,
                baudrate=self.baudrate,
                device_id=self.device_id,
                rts=self.rts,
                backend=self.modbus_backend,
            )
        elif self.mode == "screengrab":
            from drivers.pyrometer import ScreenGrabPyrometer
            return ScreenGrabPyrometer()
        elif self.mode == "exactus":
            from drivers.pyrometer import ExactusSerialPyrometer
            return ExactusSerialPyrometer(port=self.port, baudrate=self.baudrate)
        else:
            from drivers.pyrometer import DummyPyrometer
            return DummyPyrometer()

    def stop(self):
        """Stop the pyrometer worker thread."""
        self.running = False


class MistralWorker(QThread):
    """Background thread for MistralGui V/I OCR polling.

    Uses lazy connect — if MISTRAL isn't running when the worker starts,
    the loop keeps trying each cycle until the window appears. Same
    behavior if MISTRAL is closed mid-session.
    """

    state_updated = pyqtSignal(MistralState)

    def __init__(
        self,
        mode: str = "screengrab",
        poll_interval: float = 1.0,
        chamber_config=None,
    ):
        super().__init__()
        self.mode = mode
        self.poll_interval = poll_interval
        # chamber_config: MBESystemConfig (from drivers.config). Required
        # when mode="ads" — supplies per-chamber netid / ports / cell_count
        # for MistralAdsClient. Explicit passing (not get_active_config()
        # lookup) so worker instances stay coupled only to the config they
        # were given, not to env vars.
        self._chamber_config = chamber_config
        # True from __init__ to close the stop()-before-run race
        # (see PowerSupplyWorker for the full comment).
        self.running = True
        self._driver = None

    def run(self):
        state = MistralState(mode=self.mode)
        self._driver = self._create_driver()

        while self.running:
            if not self._driver.connected:
                try:
                    self._driver.connect()
                    state.connected = True
                    state.error = ""
                except Exception as e:
                    state.connected = False
                    _mark_read_failed(state, e)
                    self.state_updated.emit(_emission_snapshot(state))
                    time.sleep(self.poll_interval)
                    continue

            try:
                read_started_ns = time.perf_counter_ns()
                vals = self._driver.read()
                state.v_set = vals.get("v_set")
                state.v_actual = vals.get("v_actual")
                state.i_set = vals.get("i_set")
                state.i_actual = vals.get("i_actual")
                # ads_cells: full extended read() dict when mode="ads"
                # (Beckhoff TwinCAT ADS, both chambers). Includes per-cell
                # T / T_set / active_setpoint / V / I / prog_V / prog_A / power
                # / state / shutter_* plus ion gauge, turbo, service_mode.
                # None in other modes.
                state.ads_cells = vals if self.mode == "ads" else None
                state.connected = True
                succeeded = False
                if self.mode == "ads":
                    # ADS supplies a union of all cells and chamber telemetry.
                    # A Cell1 alias outage must not discard valid Cells2-6 or
                    # pressure data; per-field None still records partialness.
                    has_usable_value = any(
                        value is not None for value in vals.values()
                    )
                else:
                    has_usable_value = any(vals.get(key) is not None for key in (
                        "v_set", "v_actual", "i_set", "i_actual",
                    ))
                if has_usable_value:
                    state.error = ""
                    completed_ns = _mark_sample_received(
                        state, read_started_ns,
                    )
                    succeeded = True
                else:
                    completed_ns = _mark_read_failed(
                        state, "read returned no MISTRAL values", read_started_ns,
                    )
            except Exception as e:
                succeeded = False
                completed_ns = _mark_read_failed(state, e, read_started_ns)

            _mark_attempt_completed(
                state,
                read_started_ns=read_started_ns,
                completed_ns=completed_ns,
                capture_at_utc=getattr(
                    self._driver, "last_capture_at_utc", None,
                ),
                capture_monotonic_ns=getattr(
                    self._driver, "last_capture_monotonic_ns", None,
                ),
                succeeded=succeeded,
            )

            self.state_updated.emit(_emission_snapshot(state))
            time.sleep(self.poll_interval)

        if self._driver is not None:
            try:
                self._driver.disconnect()
            except Exception:
                pass

    def _create_driver(self):
        if self.mode == "screengrab":
            from drivers.mistral import MistralGui
            return MistralGui()
        elif self.mode == "jsonrpc":
            # Direct-read via the Jun 23 2026 discovered backend at
            # http://10.0.42.231:9000/api (see docs/mistral_jsonrpc_discovery.md
            # and drivers/mistral_jsonrpc.py). Multi-client safe at the HTTP
            # layer — the client won't disrupt a live MistralGui session.
            #
            # Read-config is NOT populated yet: until the discovery probes
            # identify the actual V/I method names, read() returns the
            # 4-key all-None dict. The worker still emits state (connected
            # true, values None) so the GUI shows a working driver with
            # no readings — same shape as before, no crashes.
            #
            # After discovery: populate MistralJsonRpcClient.set_read_config
            # here (or add a DEFAULT_READ_CONFIG class attribute to the
            # client) with the discovered method names, then this driver
            # replaces MistralGui as the primary path.
            from drivers.mistral_jsonrpc import MistralJsonRpcClient
            return MistralJsonRpcClient()
        elif self.mode == "ads":
            # Beckhoff TwinCAT ADS direct-read — per-chamber config.
            # Ch-MBE validated Jul 22 2026 (Task #191, netId 10.0.42.112.1.1,
            # 7 cells). Bulbasaur/O-MBE validated Jul 27 2026 via direct pyads
            # to netId 10.0.42.111.1.1 with 6 cells, bypassing Bulbasaur's
            # Kestrel JSON-RPC gateway which exposes zero introspection.
            # v_actual/v_set = Cell1_V / Cell1_prog_V (manipulator); similar
            # for current. READ ONLY — write_by_name is never called.
            from drivers.mistral_ads import MistralAdsClient
            if self._chamber_config is None:
                raise RuntimeError(
                    "MistralWorker mode='ads' requires chamber_config "
                    "(pass it via __init__ from GrowthApp)"
                )
            cfg = self._chamber_config
            return MistralAdsClient(
                netid=cfg.ads_netid,
                port_main=cfg.ads_port_main,
                port_pid=cfg.ads_port_pid,
                cell_count=cfg.ads_cell_count,
            )
        else:
            from drivers.mistral import DummyMistralGui
            return DummyMistralGui()

    def stop(self):
        self.running = False


class EvapControlWorker(QThread):
    """Background thread for Evap Control chamber pressure OCR polling."""

    state_updated = pyqtSignal(EvapControlState)

    def __init__(
        self,
        mode: str = "screengrab",
        poll_interval: float = 1.0,
        chamber_config=None,
        log_dir: Optional[str] = None,
    ):
        super().__init__()
        self.mode = mode
        self.poll_interval = poll_interval
        self._chamber_config = chamber_config
        # ``log_dir`` remains a compatibility entry point for diagnostics and
        # older callers.  The production GUI passes ``chamber_config``; when
        # both are supplied, its chamber-bound path deliberately wins.
        self.log_dir = str(log_dir or "").strip()
        # True from __init__ to close the stop()-before-run race
        # (see PowerSupplyWorker for the full comment).
        self.running = True
        self._driver = None
        # Last connect-failure message logged, so the per-poll retry loop
        # reports each distinct cause once instead of every second.
        self._last_logged_error: Optional[str] = None

    def run(self):
        state = EvapControlState(mode=self.mode)
        self._driver = self._create_driver()
        state.source_path = str(getattr(self._driver, "source_path", "") or "")

        while self.running:
            if not self._driver.connected:
                try:
                    self._driver.connect()
                    state.connected = True
                    state.error = ""
                    state.source_path = str(
                        getattr(self._driver, "source_path", "") or ""
                    )
                except Exception as e:
                    state.connected = False
                    _mark_read_failed(state, e)
                    # Log the first occurrence of each distinct message.
                    # This block previously recorded the reason in
                    # state.error and nothing more; no consumer surfaces
                    # that string, so a driver that never connected was
                    # indistinguishable at the console from one quietly
                    # reading nothing. Deduped because the retry loop
                    # re-enters every poll_interval and would flood.
                    if state.error != self._last_logged_error:
                        self._last_logged_error = state.error
                        log.warning(
                            "EvapControlWorker (%s mode) not connected: %s",
                            self.mode, state.error,
                        )
                    self.state_updated.emit(_emission_snapshot(state))
                    time.sleep(self.poll_interval)
                    continue

            try:
                read_started_ns = time.perf_counter_ns()
                vals = self._driver.read()
                state.source_path = str(
                    getattr(self._driver, "source_path", "") or ""
                )
                # Pressure: populated by both screengrab and elog modes.
                state.chamber_pressure_mbar = vals.get("chamber_pressure_mbar")
                # Substrate + cells + plasma: populated by elog mode only;
                # screengrab returns None for these keys. The same field
                # set is fanned out either way — downstream consumers see
                # None for variables not present in the active mode.
                state.substrate_temp_pv_C = vals.get("substrate_temp_pv_C")
                state.substrate_temp_setpoint_C = vals.get("substrate_temp_setpoint_C")
                state.cell_HTEC2_pv_C = vals.get("cell_HTEC2_pv_C")
                state.cell_Y_pv_C = vals.get("cell_Y_pv_C")
                state.cell_Sr_pv_C = vals.get("cell_Sr_pv_C")
                state.cell_Eu_pv_C = vals.get("cell_Eu_pv_C")
                state.cell_Er_pv_C = vals.get("cell_Er_pv_C")
                state.cell_Fe_pv_C = vals.get("cell_Fe_pv_C")
                state.cell_Te_pv_C = vals.get("cell_Te_pv_C")
                state.cell_Se_pv_C = vals.get("cell_Se_pv_C")
                state.plasma_dc_bias_V = vals.get("plasma_dc_bias_V")
                state.plasma_forward_W = vals.get("plasma_forward_W")
                state.plasma_reflected_W = vals.get("plasma_reflected_W")
                state.connected = True
                # ElogReader exposes the source record inspected by this
                # attempt separately from the latest accepted sample.  Older
                # compatible drivers do not implement these properties and
                # retain their historical value-based success behavior.
                tracks_source_records = hasattr(
                    self._driver, "source_record_advanced",
                )
                attempt_source_at_utc = getattr(
                    self._driver, "last_source_at_utc", None,
                )
                state.attempt_source_at_utc = attempt_source_at_utc
                state.source_age_ms = getattr(
                    self._driver, "last_source_age_ms", None,
                )
                state.source_record_advanced = bool(getattr(
                    self._driver, "source_record_advanced", False,
                ))
                state.source_stale = bool(getattr(
                    self._driver, "source_stale", False,
                ))
                state.source_status = str(getattr(
                    self._driver,
                    "source_status",
                    "untracked" if not tracks_source_records else "unavailable",
                ) or "unavailable")
                succeeded = False
                has_values = any(value is not None for value in vals.values())
                source_is_new = (
                    not tracks_source_records
                    or (
                        state.source_record_advanced
                        and not state.source_stale
                    )
                )
                if has_values and source_is_new:
                    state.error = ""
                    completed_ns = _mark_sample_received(
                        state,
                        read_started_ns,
                        source_at_utc=attempt_source_at_utc,
                    )
                    succeeded = True
                elif has_values and state.source_stale:
                    age_text = (
                        f"{state.source_age_ms / 1000.0:.1f}s"
                        if state.source_age_ms is not None else "unknown age"
                    )
                    completed_ns = _mark_read_failed(
                        state,
                        f"EvapControl source record is stale ({age_text})",
                        read_started_ns,
                    )
                elif has_values and tracks_source_records:
                    completed_ns = _mark_read_failed(
                        state,
                        "EvapControl source record did not advance "
                        f"({state.source_status})",
                        read_started_ns,
                    )
                else:
                    completed_ns = _mark_read_failed(
                        state, "read returned no EvapControl values", read_started_ns,
                    )
            except Exception as e:
                succeeded = False
                completed_ns = _mark_read_failed(state, e, read_started_ns)

            _mark_attempt_completed(
                state,
                read_started_ns=read_started_ns,
                completed_ns=completed_ns,
                capture_at_utc=getattr(
                    self._driver, "last_capture_at_utc", None,
                ),
                capture_monotonic_ns=getattr(
                    self._driver, "last_capture_monotonic_ns", None,
                ),
                succeeded=succeeded,
            )

            self.state_updated.emit(_emission_snapshot(state))
            time.sleep(self.poll_interval)

        if self._driver is not None:
            try:
                self._driver.disconnect()
            except Exception:
                pass

    def _create_driver(self):
        if self.mode == "screengrab":
            from drivers.evap_control import EvapControl
            return EvapControl()
        elif self.mode == "elog":
            from drivers.evap_control import ElogReader
            log_dir = (
                self._chamber_config.evap_log_dir
                if self._chamber_config is not None
                else self.log_dir
            )
            var_map = (
                getattr(self._chamber_config, "evap_elog_var_map", None)
                if self._chamber_config is not None
                else None
            )
            return ElogReader(log_dir=log_dir or None, var_map=var_map)
        else:
            from drivers.evap_control import DummyEvapControl
            return DummyEvapControl()

    def stop(self):
        self.running = False


class ClassifierWorker(QThread):
    """Runs Classifier2 inference off the UI thread at a bounded rate.

    Consumes RHEED frames via :py:meth:`on_rheed_state` — connect this
    slot to ``RheedCameraWorker.state_updated``. Publishes
    ``ClassifierState`` with EMA-smoothed percentages normalized via the
    "Equalizer recipe" (clip negatives → divide by sum → uniform
    fallback), matching ``scripts/equalizer_ui.py:auto_fit``.

    Threading contract:
        - :py:meth:`on_rheed_state` runs in the SENDER's thread (the RHEED
          worker's thread). It writes the latest frame under a QMutex —
          no blocking work, just an attribute swap. Drop-old semantics:
          stale unclassified frames are silently overwritten by newer
          ones, preventing queue growth when the classifier is slower
          than the camera.
        - :py:meth:`run` executes in this worker's own thread. It reads
          the latest frame under the mutex, classifies, normalizes,
          updates the EMA, and emits ``state_updated`` at
          ``POLL_INTERVAL_S`` cadence.

    OOD handling:
        When the classifier's ``quality`` drops below
        ``OOD_QUALITY_THRESHOLD``, ``ClassifierState.is_ood`` is set True
        and the EMA is NOT advanced — the smoothed percentages freeze at
        their last confident values. The UI is expected to grey out the
        sliders while ``is_ood``.

    Failure handling:
        - Startup: bridge load failure emits ``error`` and returns
          (thread ends; recreate the worker to retry).
        - Runtime: classify failures are counted; after
          ``MAX_CONSECUTIVE_FAILS`` in a row the worker emits an error
          state and resets the counter. The loop continues so a transient
          issue can recover on its own.
    """

    state_updated = pyqtSignal(ClassifierState)

    # Class-level knobs — instance-override in tests via monkey-patching.
    POLL_INTERVAL_S = 0.5           # 2 Hz classification cadence
    EMA_ALPHA = 0.2                 # ~5 s time constant at 2 Hz
    OOD_QUALITY_THRESHOLD = 0.3     # below this = freeze EMA + set is_ood
    MAX_CONSECUTIVE_FAILS = 5       # symmetric with Jul-2 driver-hardening pattern
    # The currently deployed bridge calls ``classify(frame)`` and is therefore
    # single-frame-only.  The 32-frame GRU remains an offline experiment until
    # a temporal runtime bridge supplies the full causal tensor.
    HISTORY_FRAMES_REQUIRED = 0
    OFFLINE_TEMPORAL_HISTORY_FRAMES = 32

    def __init__(self, ai_repo_root, model_path=None):
        super().__init__()
        self.ai_repo_root = ai_repo_root
        self.model_path = model_path
        # True from __init__ to close the stop()-before-run race
        # (see PowerSupplyWorker for the full comment).
        self.running = True

        # Frame handoff — mutex-protected latest frame (drop-old, not FIFO).
        self._frame_mutex = QMutex()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_frame_number = -1
        self._latest_frame_key: Optional[tuple] = None
        self._latest_capture_sequence = 0
        self._latest_received_monotonic_ns = 0

        # Per-cycle state carried across iterations.
        self._smoothed: dict[str, float] = {}   # float internal for EMA math
        self._consecutive_failures = 0
        self._last_classified_frame_number = -1
        self._last_classified_frame_key: Optional[tuple] = None
        # Latches True on first non-OOD classification; never resets within
        # a single worker lifetime (recreate the worker to reset).
        self._has_confident_data = False
        # Model identity ("filename (YYYY-MM-DD)") — resolved once at
        # bridge-load time in run() and emitted on every subsequent state.
        self._model_version = ""

        # Acquisition-side view state.  The worker defaults to aligned for
        # backwards compatibility with standalone demos/tests; GrowthApp
        # explicitly sets alignment to unknown at every session start.
        # ``_view_epoch`` invalidates an in-flight result whenever a session
        # or gun-alignment boundary is crossed.
        self._gun_aligned: Optional[bool] = True
        self._view_segment_id: Optional[int] = 0
        self._view_epoch = 0
        self._applied_view_epoch = -1
        self._history_frame_count = 0
        self._model_input_mode = "unknown"

    def _emit_classifier_state(self, state: ClassifierState) -> None:
        """Emit an immutable snapshot across the queued Qt connection.

        Reusing one mutable dataclass lets a later inference overwrite a
        reset/ready state before the GUI event loop consumes it.
        """
        state.worker_emitted_monotonic_ns = time.perf_counter_ns()
        self.state_updated.emit(deepcopy(state))

    def _stamp_current_view(self, state: ClassifierState) -> None:
        """Attach the current acquisition generation to any emitted state."""
        self._frame_mutex.lock()
        try:
            state.gun_aligned = self._gun_aligned
            state.view_segment_id = self._view_segment_id
            state.visual_history_generation = self._view_epoch
        finally:
            self._frame_mutex.unlock()

    # ---- Slot: runs in the sender's thread; mutex-protected write ----
    def on_rheed_state(self, camera_state: CameraState) -> None:
        """Store the latest frame + number under mutex. Runs in sender's thread.

        Fast attribute swap; no I/O, no inference. Silently overwrites any
        prior unclassified frame (drop-old semantics).
        """
        self._frame_mutex.lock()
        try:
            if camera_state.frame is None:
                self._latest_frame = None
                self._latest_frame_number = -1
                self._latest_frame_key = None
                self._latest_capture_sequence = 0
                self._latest_received_monotonic_ns = 0
            else:
                self._latest_frame = camera_state.frame
                self._latest_frame_number = camera_state.frame_number
                self._latest_capture_sequence = int(
                    camera_state.capture_sequence or 0,
                )
                self._latest_received_monotonic_ns = int(
                    camera_state.captured_monotonic_ns or 0,
                )
                if camera_state.captured_monotonic_ns:
                    self._latest_frame_key = (
                        "capture",
                        camera_state.capture_sequence,
                        camera_state.captured_monotonic_ns,
                    )
                elif camera_state.capture_sequence:
                    self._latest_frame_key = (
                        "sequence",
                        camera_state.mode,
                        camera_state.capture_sequence,
                    )
                else:
                    # Compatibility for direct unit-test states and older
                    # camera sources that expose only a worker-local number.
                    self._latest_frame_key = (
                        "frame_number",
                        camera_state.frame_number,
                    )
        finally:
            self._frame_mutex.unlock()

    def set_rheed_qc_state(
        self,
        qc_state: RheedQcState,
        *,
        force_reset: bool = False,
    ) -> None:
        """Apply an operator-known view boundary without stopping the worker.

        A realignment boundary clears only pixel-coordinate-dependent state:
        the latest frame handoff, EMA display state, and visual history count.
        The worker/model stays loaded, while temperature and process histories
        owned by GrowthApp/GrowthLogger are untouched.

        ``force_reset`` is used at a new session or after a camera reconnect,
        where the stable segment identifier may be unchanged but visual
        continuity has nevertheless been interrupted.
        """
        self._frame_mutex.lock()
        try:
            changed = (
                qc_state.gun_aligned != self._gun_aligned
                or qc_state.view_segment_id != self._view_segment_id
            )
            self._gun_aligned = qc_state.gun_aligned
            self._view_segment_id = qc_state.view_segment_id
            if changed or force_reset:
                requested_generation = int(
                    qc_state.visual_history_generation
                )
                self._view_epoch = max(
                    self._view_epoch + 1,
                    requested_generation,
                )
                # Prevent the frame captured immediately before the boundary
                # from being classified as part of the new segment.
                self._latest_frame = None
                self._latest_frame_number = -1
                self._latest_frame_key = None
        finally:
            self._frame_mutex.unlock()

    def _create_bridge(self):
        """Factory for the ClassifierBridge. Override in tests via monkey-patch."""
        from gui.classifier_bridge import ClassifierBridge
        return ClassifierBridge(self.ai_repo_root, self.model_path)

    def run(self) -> None:
        """Main loop — load bridge once, then classify at POLL_INTERVAL_S."""
        from gui.recon_labels import RECON_LABELS

        # Emit initial loading state so the UI can show "Loading classifier…"
        state = ClassifierState()  # loading=True, ready=False, error=""
        self._stamp_current_view(state)
        self._emit_classifier_state(state)

        # Load bridge — blocking, ~1-2 s. This is precisely why we're on our
        # own thread: the UI stays responsive during model load.
        try:
            bridge = self._create_bridge()
        except Exception as e:
            state.loading = False
            state.ready = False
            state.error = f"Failed to load classifier: {e}"
            self._stamp_current_view(state)
            self._emit_classifier_state(state)
            return

        self._model_input_mode = str(
            getattr(bridge, "input_mode", "single_frame")
        )
        # A bridge that only exposes ``classify(frame)`` must never make the
        # GUI claim that a 32-frame GRU has warmed up.
        temporal_runtime = bool(
            getattr(bridge, "uses_temporal_history", False)
        )
        if temporal_runtime:
            state.loading = False
            state.ready = False
            state.error = (
                "Temporal bridge capability is declared but this runtime "
                "does not provide a history inference API."
            )
            state.model_input_mode = self._model_input_mode
            self._stamp_current_view(state)
            self._emit_classifier_state(state)
            return

        # Derive model identity string once — filename + file mtime as
        # YYYY-MM-DD. Falls back to "unknown" if the bridge didn't expose
        # a model_path attribute or the file has vanished since load.
        self._model_version = self._derive_model_version(bridge)

        # Ready — initialize EMA at uniform, emit uniform placeholder.
        # First real inference will EMA-blend into this baseline, so the
        # sliders visibly "settle" onto the model's answer rather than
        # snapping — feels more natural to growers.
        self._smoothed = {lbl: 20.0 for lbl in RECON_LABELS}
        uniform = {lbl: 20 for lbl in RECON_LABELS}
        state.loading = False
        state.ready = True
        state.error = ""
        state.normalized_percent = uniform.copy()
        state.smoothed_percent = uniform.copy()
        state.model_version = self._model_version
        self._frame_mutex.lock()
        try:
            state.gun_aligned = self._gun_aligned
            state.view_segment_id = self._view_segment_id
            self._applied_view_epoch = self._view_epoch
            state.visual_history_generation = self._view_epoch
        finally:
            self._frame_mutex.unlock()
        state.history_frame_count = 0
        state.history_required = self.HISTORY_FRAMES_REQUIRED
        state.history_ready = False
        state.prediction_actionable = False
        state.model_input_mode = self._model_input_mode
        self._emit_classifier_state(state)

        # Main polling loop
        while self.running:
            # Snapshot the latest frame and its acquisition-side view epoch
            # atomically.  The epoch is checked again after inference so a
            # result started before realignment cannot leak into the new
            # stable segment.
            self._frame_mutex.lock()
            try:
                frame = self._latest_frame
                frame_number = self._latest_frame_number
                frame_key = self._latest_frame_key
                source_capture_sequence = self._latest_capture_sequence
                source_received_monotonic_ns = self._latest_received_monotonic_ns
                gun_aligned = self._gun_aligned
                view_segment_id = self._view_segment_id
                view_epoch = self._view_epoch
            finally:
                self._frame_mutex.unlock()

            if view_epoch != self._applied_view_epoch:
                self._applied_view_epoch = view_epoch
                self._history_frame_count = 0
                self._last_classified_frame_number = -1
                self._last_classified_frame_key = None
                self._has_confident_data = False
                self._smoothed = {lbl: 20.0 for lbl in RECON_LABELS}

                # Emit an explicit non-actionable reset state immediately.
                # This removes stale percentages from the UI even when no
                # post-boundary frame has arrived yet.
                state.error = ""
                state.last_frame_number = -1
                state.raw_scores = {}
                state.normalized_percent = uniform.copy()
                state.smoothed_percent = uniform.copy()
                state.raw_sum = 0.0
                state.quality = 0.0
                state.is_bad = False
                state.bad_confidence = 0.0
                state.is_ood = False
                state.has_confident_data = False
                state.inference_ms = 0.0
                state.source_capture_sequence = 0
                state.source_received_monotonic_ns = 0
                state.inference_started_monotonic_ns = 0
                state.inference_completed_monotonic_ns = 0
                state.gun_aligned = gun_aligned
                state.view_segment_id = view_segment_id
                state.visual_history_generation = view_epoch
                state.history_frame_count = 0
                state.history_required = self.HISTORY_FRAMES_REQUIRED
                state.history_ready = False
                state.prediction_actionable = False
                state.model_input_mode = self._model_input_mode
                self._emit_classifier_state(state)

            # During alignment the camera and temperature logs keep running,
            # but image inference is intentionally frozen.
            if gun_aligned is not True:
                time.sleep(self.POLL_INTERVAL_S)
                continue

            # Skip if no frame yet, or same frame we already classified
            if frame is None or frame_key == self._last_classified_frame_key:
                time.sleep(self.POLL_INTERVAL_S)
                continue

            # Classify — the heavy work happens here in this worker's thread
            inference_started_ns = time.perf_counter_ns()
            try:
                result = bridge.classify(frame)
                inference_completed_ns = time.perf_counter_ns()
                inference_ms = (
                    inference_completed_ns - inference_started_ns
                ) / 1_000_000.0
                self._consecutive_failures = 0
            except Exception as e:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILS:
                    state.error = (
                        f"Classifier failed {self._consecutive_failures}x: {e}"
                    )
                    state.visual_history_generation = view_epoch
                    self._emit_classifier_state(state)
                    self._consecutive_failures = 0  # reset after emitting; retry
                time.sleep(self.POLL_INTERVAL_S)
                continue

            # Capture may fail closed while inference is running. Discard a
            # result whose source frame was cleared or superseded so no stale
            # classification reaches the GUI or session logs.
            self._frame_mutex.lock()
            try:
                source_still_current = (
                    self._latest_frame is not None
                    and self._latest_frame_key == frame_key
                    and self._view_epoch == view_epoch
                    and self._gun_aligned is True
                )
            finally:
                self._frame_mutex.unlock()
            if not source_still_current:
                time.sleep(self.POLL_INTERVAL_S)
                continue

            self._last_classified_frame_number = frame_number
            self._last_classified_frame_key = frame_key
            # This bridge classifies only the current frame.  Counting calls
            # is not equivalent to feeding a causal history into the model.
            self._history_frame_count = 0
            history_ready = False

            # Normalize via Equalizer recipe (clip → sum → divide → uniform fallback)
            scores = result.get("classification_scores", {}) or {}
            normalized, raw_sum = self._normalize(scores)

            # OOD gate: freeze the EMA when the model isn't confident
            quality = float(result.get("quality") or 0.0)
            is_ood = quality < self.OOD_QUALITY_THRESHOLD

            if not is_ood:
                for lbl in RECON_LABELS:
                    new = float(normalized.get(lbl, 0))
                    self._smoothed[lbl] = (
                        self.EMA_ALPHA * new
                        + (1.0 - self.EMA_ALPHA) * self._smoothed.get(lbl, 0.0)
                    )
                self._has_confident_data = True

            # Emit populated state
            state.error = ""
            state.last_frame_number = frame_number
            state.raw_scores = dict(scores)
            state.normalized_percent = normalized
            state.smoothed_percent = {
                lbl: int(round(v)) for lbl, v in self._smoothed.items()
            }
            state.raw_sum = raw_sum
            state.quality = quality
            state.is_bad = bool(result.get("is_bad", False))
            state.bad_confidence = float(result.get("bad_confidence", 0.0))
            state.is_ood = is_ood
            state.has_confident_data = self._has_confident_data
            state.inference_ms = inference_ms
            state.source_capture_sequence = source_capture_sequence
            state.source_received_monotonic_ns = source_received_monotonic_ns
            state.inference_started_monotonic_ns = inference_started_ns
            state.inference_completed_monotonic_ns = inference_completed_ns
            state.model_version = self._model_version
            state.gun_aligned = True
            state.view_segment_id = view_segment_id
            state.visual_history_generation = view_epoch
            state.history_frame_count = self._history_frame_count
            state.history_required = self.HISTORY_FRAMES_REQUIRED
            state.history_ready = history_ready
            state.prediction_actionable = False
            state.model_input_mode = self._model_input_mode
            self._emit_classifier_state(state)

            time.sleep(self.POLL_INTERVAL_S)

    def stop(self) -> None:
        """Signal the run loop to exit at its next iteration."""
        self.running = False

    @staticmethod
    def _derive_model_version(bridge) -> str:
        """Return "filename (YYYY-MM-DD)" for the loaded model checkpoint.

        Reads ``bridge.model_path`` (set by ClassifierBridge post-Jul-6);
        falls back to "unknown" if the attribute doesn't exist or the
        file is missing. Never raises — this is UI transparency, not
        critical-path logic.
        """
        from datetime import datetime as _dt
        from pathlib import Path as _Path

        try:
            model_path = getattr(bridge, "model_path", None)
            if model_path is None:
                return "unknown"
            p = _Path(model_path)
            if not p.exists():
                return f"{p.name} (missing)"
            mtime = _dt.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
            return f"{p.name} ({mtime})"
        except Exception:
            return "unknown"

    @staticmethod
    def _normalize(scores: dict) -> tuple[dict, float]:
        """Equalizer recipe: clip → sum → divide → uniform fallback.

        Returns ``(percentages, raw_sum)`` where:
            - ``percentages`` — dict[label, int] summing to 100 across
              ``RECON_LABELS``.
            - ``raw_sum`` — sum of the raw scores BEFORE clip, for the
              "Sum: X.XX" transparency label on the UI. Tells the grower
              how much scaling the display is doing.

        Uniform fallback triggers when the positive mass is zero (all
        scores <= 0), producing 20/20/20/20/20 as a neutral display.
        """
        from gui.recon_labels import RECON_LABELS
        vals = np.array(
            [float(scores.get(lbl, 0.0)) for lbl in RECON_LABELS],
            dtype=np.float64,
        )
        raw_sum = float(vals.sum())  # BEFORE clip — for transparency
        vals = np.clip(vals, 0.0, None)
        s = vals.sum()
        if s <= 0:
            vals = np.full(len(RECON_LABELS), 1.0 / len(RECON_LABELS))
        else:
            vals = vals / s
        return (
            {lbl: int(round(100 * v)) for lbl, v in zip(RECON_LABELS, vals)},
            raw_sum,
        )


class WeakPrimaryShadowWorker(QThread):
    """Independent low-rate worker for the brightness-robust shadow."""

    state_updated = pyqtSignal(WeakPrimaryShadowState)
    POLL_INTERVAL_S = 2.0
    MAX_CONSECUTIVE_FAILS = 3

    def __init__(self, ai_repo_root, artifact_root=None, device=None):
        super().__init__()
        self.ai_repo_root = ai_repo_root
        self.artifact_root = artifact_root
        self.device = device
        self.running = True
        self._frame_mutex = QMutex()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_frame_number = -1
        self._latest_frame_key: Optional[tuple] = None
        self._latest_capture_sequence = 0
        self._latest_received_monotonic_ns = 0
        self._last_frame_key: Optional[tuple] = None

    def _emit(self, state: WeakPrimaryShadowState) -> None:
        state.worker_emitted_monotonic_ns = time.perf_counter_ns()
        self.state_updated.emit(deepcopy(state))

    def on_rheed_state(self, camera_state: CameraState) -> None:
        self._frame_mutex.lock()
        try:
            if (
                camera_state.frame is None
                or not camera_state.connected
                or not camera_state.valid
            ):
                self._latest_frame = None
                self._latest_frame_key = None
                return
            self._latest_frame = camera_state.frame
            self._latest_frame_number = camera_state.frame_number
            self._latest_capture_sequence = int(camera_state.capture_sequence or 0)
            self._latest_received_monotonic_ns = int(
                camera_state.captured_monotonic_ns or 0
            )
            self._latest_frame_key = (
                int(camera_state.capture_sequence or 0),
                int(camera_state.captured_monotonic_ns or 0),
                int(camera_state.frame_number),
            )
        finally:
            self._frame_mutex.unlock()

    def _create_bridge(self):
        from gui.weak_primary_shadow import WeakPrimaryShadowBridge
        return WeakPrimaryShadowBridge(
            self.ai_repo_root,
            artifact_root=self.artifact_root,
            device=self.device,
        )

    def run(self) -> None:
        state = WeakPrimaryShadowState()
        self._emit(state)
        try:
            bridge = self._create_bridge()
        except Exception as error:
            state.loading = False
            state.ready = False
            state.error = f"Weak-primary shadow unavailable: {error}"
            self._emit(state)
            return
        state.loading = False
        state.ready = True
        state.error = ""
        state.checkpoint_count = int(bridge.checkpoint_count)
        state.ensemble_id = str(bridge.ensemble_id)
        self._emit(state)
        consecutive_failures = 0

        while self.running:
            self._frame_mutex.lock()
            try:
                frame = self._latest_frame
                frame_number = self._latest_frame_number
                frame_key = self._latest_frame_key
                sequence = self._latest_capture_sequence
                received_ns = self._latest_received_monotonic_ns
            finally:
                self._frame_mutex.unlock()
            if frame is None or frame_key == self._last_frame_key:
                time.sleep(self.POLL_INTERVAL_S)
                continue
            started_ns = time.perf_counter_ns()
            try:
                result = bridge.classify(frame)
                completed_ns = time.perf_counter_ns()
                consecutive_failures = 0
            except Exception as error:
                consecutive_failures += 1
                if consecutive_failures >= self.MAX_CONSECUTIVE_FAILS:
                    state.error = (
                        f"Weak-primary shadow failed {consecutive_failures}x: {error}"
                    )
                    self._emit(state)
                    consecutive_failures = 0
                time.sleep(self.POLL_INTERVAL_S)
                continue
            self._frame_mutex.lock()
            try:
                still_current = (
                    self._latest_frame is not None
                    and self._latest_frame_key == frame_key
                )
            finally:
                self._frame_mutex.unlock()
            if not still_current:
                time.sleep(self.POLL_INTERVAL_S)
                continue
            self._last_frame_key = frame_key
            state.error = ""
            state.last_frame_number = int(frame_number)
            state.source_capture_sequence = int(sequence)
            state.source_received_monotonic_ns = int(received_ns)
            state.inference_started_monotonic_ns = started_ns
            state.inference_completed_monotonic_ns = completed_ns
            state.inference_ms = (completed_ns - started_ns) / 1_000_000.0
            for name in (
                "conditional_probabilities", "predicted_class",
                "predicted_applicability", "normalized_entropy",
                "checkpoint_disagreement", "checkpoint_count", "ensemble_id",
                "bundle_family", "brightness_policy", "output_classes",
                "lambda_pair", "execution_scope", "actionable", "abstain_reason",
            ):
                setattr(state, name, result[name])
            self._emit(state)
            time.sleep(self.POLL_INTERVAL_S)

    def stop(self) -> None:
        self.running = False
