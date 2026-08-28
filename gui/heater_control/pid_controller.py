"""
PID temperature controller — event-driven, lives on the main thread.

Receives TemperatureState readings via on_temp_state() called from
MainWindow's signal fan-out. Issues voltage commands to PowerSupplyWorker
via queue_command(). No serial ports opened here.

Safety:
- Hard cutoff at HARD_CUTOFF_C (300 °C): triggers emergency stop + FAULT
- Slew-rate limiting on voltage output
- Three gain bands with smooth interpolation (matching temp_pid.py pattern)
- PSU/TC disconnect detection while running → FAULT
"""

import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from gui.state import PowerSupplyState, TemperatureState
from gui.heater_control.action_logger import ActionLogger
from gui.heater_control.heater_commands import PowerSupplyCommandResult
from gui.heater_control.heater_session import utc_now_iso


HARD_CUTOFF_C: float = 300.0  # absolute system ceiling — Config tab cannot exceed this


# ------------------------------------------------------------------
# Configuration dataclasses
# ------------------------------------------------------------------

@dataclass
class GainBand:
    kp: float
    ki: float
    kd: float


@dataclass
class PIDConfig:
    target_c: float
    hold_s: float               # seconds (0 = run until manually stopped)
    margin_c: float             # ±margin for "in setpoint" logic
    max_voltage: float          # V
    current_limit_a: float      # A
    slew_rate_v_per_s: float    # max output change rate
    bands: list                 # [GainBand × 3] — low / mid / high
    threshold_t1: float         # °C — Band 1 → Band 2 crossover
    threshold_t2: float         # °C — Band 2 → Band 3 crossover
    interp_band_c: float        # °C — width of smooth transition zone
    hard_cutoff_c: float = 150.0  # °C — emergency stop if temperature reaches this


# ------------------------------------------------------------------
# Live run state (emitted as signal payload)
# ------------------------------------------------------------------

@dataclass
class PIDRunState:
    controller_state: str = "IDLE"   # IDLE / ARMED / RUNNING / COMPLETE / FAULT / STOPPED
    setpoint_c: float = 0.0
    measured_c: float = 0.0
    error_c: float = 0.0
    output_v: float = 0.0
    hold_elapsed_s: float = 0.0
    hold_total_s: float = 0.0
    in_margin: bool = False
    elapsed_s: float = 0.0
    active_band: int = 0             # 0 / 1 / 2
    fault_message: str = ""
    psu_connected: bool = False
    tc_connected: bool = False


# ------------------------------------------------------------------
# PID algorithm (extracted from temperature_pid_control.py)
# ------------------------------------------------------------------

try:
    from simple_pid import PID as SimplePID
except ImportError:
    raise ImportError(
        "simple-pid is required. Install with: pip install simple-pid"
    )


class _RobustPID:
    """PID wrapper around simple-pid with gain scheduling support.

    Hardware-agnostic — same class works for dummy loop, MBE, or any future system.
    Gains and limits are the only things that change between hardware configurations.

    Install: pip install simple-pid
    """

    def __init__(
        self,
        kp: float,
        ki: float,
        kd: float,
        output_min: float,
        output_max: float,
        integral_min: float = -200.0,
        integral_max: float = 200.0,
        derivative_alpha: float = 0.2,
    ):
        self._pid = SimplePID(
            Kp=kp,
            Ki=ki,
            Kd=kd,
            setpoint=0.0,
            output_limits=(output_min, output_max),
            sample_time=None,
            differential_on_measurement=True,
        )

    @property
    def kp(self) -> float:
        return self._pid.Kp

    @kp.setter
    def kp(self, value: float) -> None:
        self._pid.Kp = value

    @property
    def ki(self) -> float:
        return self._pid.Ki

    @ki.setter
    def ki(self, value: float) -> None:
        self._pid.Ki = value

    @property
    def kd(self) -> float:
        return self._pid.Kd

    @kd.setter
    def kd(self, value: float) -> None:
        self._pid.Kd = value

    def reset(self):
        self._pid.reset()

    @property
    def components(self) -> tuple:
        """Return (P, I, D) terms — useful for logging and tuning."""
        return self._pid.components

    def update(self, setpoint: float, measured: float, dt: float) -> float:
        dt = max(dt, 1e-3)
        self._pid.setpoint = setpoint
        return self._pid(measured, dt=dt)


def _lerp(a: float, b: float, r: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, r))


# ------------------------------------------------------------------
# Controller
# ------------------------------------------------------------------

class PIDController(QObject):
    """
    Event-driven PID controller for temperature regulation.

    Lifecycle:
        arm(config)  →  start()  →  [RUNNING]  →  stop() / complete / fault
        reset()  →  back to IDLE
    """

    pid_state_updated = pyqtSignal(object)   # payload: PIDRunState
    safety_shutdown_requested = pyqtSignal(str, str)  # reason, terminal state

    # Minimum seconds between "Measurement" action log entries (avoids flooding)
    _LOG_INTERVAL_S = 5.0

    def __init__(self, action_logger: ActionLogger, parent=None):
        super().__init__(parent)
        self.action_logger = action_logger

        self._psu_worker = None
        self._config: Optional[PIDConfig] = None
        self._pid: Optional[_RobustPID] = None

        self._run_state = PIDRunState()
        self._commanded_v: float = 0.0
        self._run_start: Optional[float] = None
        self._last_tick: Optional[float] = None
        self._hold_elapsed: float = 0.0
        self._last_log_time: float = 0.0
        self._start_action_id: Optional[str] = None
        self._start_pending: dict[str, str] = {}
        self._start_step_index = 0
        self._runtime_command_ids: set[str] = set()
        self._safety_action_id: Optional[str] = None
        self._safety_target_state = "STOPPED"
        self._expected_generation: Optional[int] = None
        self._latest_psu_state: Optional[PowerSupplyState] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_psu_worker(self, worker):
        """Called by MainWindow when a PSU worker is created or torn down."""
        self._psu_worker = worker

    def arm(self, config: PIDConfig):
        """Validate config and transition to ARMED."""
        if self._run_state.controller_state == "RUNNING":
            return
        try:
            arm_request = self.action_logger.begin_action(
                "PID", "Arm", source="pid", requested=asdict(config),
            )
        except Exception:
            return
        effective_cutoff = min(config.hard_cutoff_c, HARD_CUTOFF_C)
        if config.target_c >= effective_cutoff:
            self.action_logger.fail_action(
                arm_request,
                "target is at or above the effective hard cutoff",
                status="REJECTED",
            )
            self._fault(
                f"Target {config.target_c:.1f} °C is at or above the "
                f"configured hard cutoff ({effective_cutoff:.1f} °C)"
            )
            return
        if config.threshold_t1 >= config.threshold_t2:
            self.action_logger.fail_action(
                arm_request,
                "gain schedule threshold T1 must be less than T2",
                status="REJECTED",
            )
            self._fault("Gain schedule threshold T1 must be less than T2")
            return

        self._config = config
        self._pid = _RobustPID(
            kp=config.bands[0].kp,
            ki=config.bands[0].ki,
            kd=config.bands[0].kd,
            output_min=0.0,
            output_max=config.max_voltage,
        )

        self._run_state.controller_state = "ARMED"
        self._run_state.setpoint_c = config.target_c
        self._run_state.hold_total_s = config.hold_s
        self._run_state.hold_elapsed_s = 0.0
        self._run_state.elapsed_s = 0.0
        self._run_state.fault_message = ""

        try:
            self.action_logger.complete_action(PowerSupplyCommandResult(
                request_id=arm_request,
                source="pid",
                command="Arm",
                status="CONFIRMED",
                requested=asdict(config),
                effective={**asdict(config), "hard_cutoff_c": effective_cutoff},
                readback={},
                error="",
                completed_at_utc=utc_now_iso(),
                completed_monotonic_ns=time.perf_counter_ns(),
            ))
        except Exception as exc:
            # Arming has not touched hardware, so fail closed to IDLE if its
            # durable terminal audit row cannot be written.
            self._run_state.controller_state = "IDLE"
            self._run_state.fault_message = f"PID Arm audit failed: {exc}"
        self.pid_state_updated.emit(self._run_state)

    def start(self):
        """Queue startup transactions; RUNNING requires all three confirms."""
        if self._run_state.controller_state != "ARMED":
            return
        if (
            not self._psu_worker
            or self._latest_psu_state is None
            or not self._latest_psu_state.has_valid_reading
        ):
            return
        try:
            self._start_action_id = self.action_logger.begin_action(
                "PID",
                "Start",
                source="pid",
                requested={
                    "current_limit_a": self._config.current_limit_a,
                    "initial_voltage_v": 0.0,
                    "output_enabled": True,
                },
            )
        except Exception:
            return

        self._run_state.controller_state = "STARTING"
        self._run_state.hold_elapsed_s = 0.0
        self._run_state.elapsed_s = 0.0
        self.pid_state_updated.emit(self._run_state)

        self._start_pending = {}
        self._start_step_index = 0
        self._queue_next_start_step()

    def stop(self):
        """Request coordinator-owned safe shutdown for a PID stop."""
        if self._run_state.controller_state not in (
            "RUNNING", "ARMED", "STARTING",
        ):
            return
        self._request_coordinated_safety("PID Stop", "STOPPED")

    def emergency_stop(self):
        """Request, but do not claim, a coordinator-owned emergency stop."""
        self._request_coordinated_safety("PID Emergency Stop", "STOPPED")

    def reset(self):
        """Return to IDLE, with a lifecycle record even when already IDLE."""
        try:
            request_id = self.action_logger.begin_action(
                "PID", "Reset", source="pid",
                requested={"from_state": self._run_state.controller_state},
            )
        except Exception:
            return
        if self._run_state.controller_state in (
            "RUNNING", "STARTING", "STOPPING",
        ):
            self.action_logger.fail_action(
                request_id,
                "reset rejected while PID output may be active",
                status="REJECTED",
            )
            return
        self._run_state = PIDRunState(
            psu_connected=self._run_state.psu_connected,
            tc_connected=self._run_state.tc_connected,
        )
        self._config = None
        self._pid = None
        self._commanded_v = 0.0
        self._expected_generation = None
        self._start_step_index = 0
        self.action_logger.complete_action(self._synthetic_result(
            request_id, "Reset", "CONFIRMED",
        ))
        self.pid_state_updated.emit(self._run_state)

    def on_command_result(self, result: PowerSupplyCommandResult) -> None:
        if result.request_id in self._start_pending:
            self._start_pending.pop(result.request_id, None)
            if self._run_state.controller_state != "STARTING":
                return
            if not result.confirmed:
                self._fail_start(
                    result.error or f"{result.command} was not confirmed",
                )
                return
            self._start_step_index += 1
            if self._start_step_index < 3:
                self._queue_next_start_step()
                return
            if not self._start_pending:
                self._commanded_v = 0.0
                self._hold_elapsed = 0.0
                self._run_start = time.perf_counter()
                self._last_tick = self._run_start
                self._last_log_time = 0.0
                self._pid.reset()
                self._expected_generation = result.connection_generation
                if self._start_action_id:
                    try:
                        self.action_logger.complete_action(self._synthetic_result(
                            self._start_action_id,
                            "Start",
                            "CONFIRMED",
                            readback={"startup_commands_confirmed": 3},
                        ))
                    except Exception as exc:
                        self._start_action_id = None
                        self._run_state.fault_message = (
                            f"PID Start completion audit failed: {exc}"
                        )
                        # Output has already been enabled.  A terminal audit
                        # failure must therefore route through physical safety
                        # instead of stranding the controller in STARTING.
                        self._request_coordinated_safety(
                            "PID Start Audit Failed", "FAULT",
                        )
                        return
                self._start_action_id = None
                self._run_state.controller_state = "RUNNING"
                self.pid_state_updated.emit(self._run_state)
            return
        if result.request_id in self._runtime_command_ids:
            self._runtime_command_ids.discard(result.request_id)
            if not result.confirmed and self._run_state.controller_state == "RUNNING":
                self._fault(
                    result.error or "PID voltage command was not confirmed",
                    emergency=True,
                )

    def begin_external_shutdown(self, reason: str) -> Optional[str]:
        """Freeze PID updates while disconnect/close owns the coordinator."""
        if self._run_state.controller_state not in (
            "RUNNING", "ARMED", "STARTING",
        ):
            return None
        self._begin_safety_lifecycle(reason, "STOPPED")
        # Return a truthy participation marker even if the durable PID audit
        # itself failed; MainWindow must still resolve STOPPING on
        # confirm/cancel/dead-worker paths.
        return reason

    def on_safety_resolution(
        self,
        *,
        confirmed: bool,
        terminal_state: Optional[str],
        error: str = "",
    ) -> None:
        if terminal_state is None and self._safety_action_id is None:
            return
        target = terminal_state or self._safety_target_state
        request_id = self._safety_action_id
        self._safety_action_id = None
        if confirmed:
            self._run_state.controller_state = target
            self._run_state.output_v = 0.0
            if request_id:
                try:
                    self.action_logger.complete_action(self._synthetic_result(
                        request_id,
                        "Safety Shutdown",
                        "CONFIRMED",
                        readback={"terminal_state": target},
                    ))
                except Exception as exc:
                    self._run_state.fault_message = (
                        f"PID terminal audit write failed: {exc}"
                    )
        else:
            self._run_state.controller_state = "FAULT"
            self._run_state.fault_message = error or "safe shutdown was not confirmed"
            if request_id:
                try:
                    self.action_logger.fail_action(
                        request_id, self._run_state.fault_message,
                    )
                except Exception:
                    pass
        self.pid_state_updated.emit(self._run_state)

    # ------------------------------------------------------------------
    # Slots called from MainWindow fan-out
    # ------------------------------------------------------------------

    def on_psu_state(self, state: PowerSupplyState):
        self._latest_psu_state = state
        self._run_state.psu_connected = state.connected
        if self._run_state.controller_state == "RUNNING":
            if not state.connected or not state.valid:
                self._fault(
                    state.error or "PSU sample became invalid during run",
                    emergency=True,
                )
            elif (
                self._expected_generation is not None
                and state.connection_generation != self._expected_generation
            ):
                self._fault(
                    "PSU connection generation changed during PID run",
                    emergency=True,
                )

    def on_temp_state(self, state: TemperatureState):
        self._run_state.tc_connected = state.connected

        if not state.connected and self._run_state.controller_state == "RUNNING":
            self._fault("Thermocouple connection lost during run")
            return

        if self._run_state.controller_state != "RUNNING":
            return

        now = time.perf_counter()
        dt = max(now - self._last_tick, 1e-3)
        self._last_tick = now
        temp_c = state.temperature

        # Hard cutoff — use the configured value, capped at the system ceiling
        effective_cutoff = min(self._config.hard_cutoff_c, HARD_CUTOFF_C)
        if temp_c >= effective_cutoff:
            self._fault(
                f"Hard cutoff reached: {temp_c:.2f} °C ≥ {effective_cutoff:.1f} °C",
                emergency=True,
            )
            return

        # Gain schedule
        active_band = self._apply_gain_schedule(temp_c)

        # PID step
        desired_v = self._pid.update(
            setpoint=self._config.target_c,
            measured=temp_c,
            dt=dt,
        )

        # Slew-rate clamp
        max_delta = self._config.slew_rate_v_per_s * dt
        delta = desired_v - self._commanded_v
        if abs(delta) > max_delta:
            desired_v = self._commanded_v + math.copysign(max_delta, delta)
        self._commanded_v = max(0.0, min(self._config.max_voltage, desired_v))

        if self._psu_worker:
            request_id = str(uuid.uuid4())
            self._runtime_command_ids.add(request_id)
            if not self._queue_command(
                "set_voltage", self._commanded_v, request_id=request_id,
            ):
                self._runtime_command_ids.discard(request_id)
                self._fault("PID voltage command could not be durably queued")
                return

        # Hold logic
        error_c = self._config.target_c - temp_c
        in_margin = abs(error_c) <= self._config.margin_c
        if in_margin:
            self._hold_elapsed += dt
        else:
            self._hold_elapsed = 0.0

        elapsed = now - self._run_start

        # Update state
        self._run_state.measured_c = temp_c
        self._run_state.error_c = error_c
        self._run_state.output_v = self._commanded_v
        self._run_state.hold_elapsed_s = self._hold_elapsed
        self._run_state.in_margin = in_margin
        self._run_state.elapsed_s = elapsed
        self._run_state.active_band = active_band

        # Throttled action log for measurements
        if now - self._last_log_time >= self._LOG_INTERVAL_S:
            self.action_logger.log(
                "PID", "Measurement",
                f"T={temp_c:.2f}°C  SP={self._config.target_c:.1f}°C  "
                f"err={error_c:+.2f}°C  V={self._commanded_v:.3f}V  "
                f"hold={self._hold_elapsed:.1f}/{self._config.hold_s:.1f}s  "
                f"band={active_band + 1}"
            )
            self._last_log_time = now

        # Hold complete?
        if self._config.hold_s > 0 and self._hold_elapsed >= self._config.hold_s:
            self._request_coordinated_safety("PID Hold Complete", "COMPLETE")

        self.pid_state_updated.emit(self._run_state)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_gain_schedule(self, temp_c: float) -> int:
        """
        Update PID gains by interpolating across three temperature bands.
        Returns the primary active band index (0, 1, or 2).
        Mirrors the logic in temp_pid.py update_pid() / temperature_pid_control.py.
        """
        cfg = self._config
        t1, t2 = cfg.threshold_t1, cfg.threshold_t2
        half = cfg.interp_band_c / 2.0
        b = cfg.bands

        if temp_c < t1 - half:
            kp, ki, kd, idx = b[0].kp, b[0].ki, b[0].kd, 0
        elif temp_c < t1 + half:
            r = (temp_c - (t1 - half)) / max(cfg.interp_band_c, 1e-6)
            kp = _lerp(b[0].kp, b[1].kp, r)
            ki = _lerp(b[0].ki, b[1].ki, r)
            kd = _lerp(b[0].kd, b[1].kd, r)
            idx = 0
        elif temp_c < t2 - half:
            kp, ki, kd, idx = b[1].kp, b[1].ki, b[1].kd, 1
        elif temp_c < t2 + half:
            r = (temp_c - (t2 - half)) / max(cfg.interp_band_c, 1e-6)
            kp = _lerp(b[1].kp, b[2].kp, r)
            ki = _lerp(b[1].ki, b[2].ki, r)
            kd = _lerp(b[1].kd, b[2].kd, r)
            idx = 1
        else:
            kp, ki, kd, idx = b[2].kp, b[2].ki, b[2].kd, 2

        self._pid.kp, self._pid.ki, self._pid.kd = kp, ki, kd
        return idx

    def _queue_command(
        self,
        command: str,
        *args,
        request_id: Optional[str] = None,
    ) -> Optional[str]:
        if not self._psu_worker:
            return None
        request_id = request_id or str(uuid.uuid4())
        try:
            self.action_logger.begin_action(
                "PID",
                command,
                source="pid",
                requested={"args": list(args)},
                request_id=request_id,
            )
        except Exception:
            return None
        return self._psu_worker.queue_command(
            command, *args, request_id=request_id, source="pid",
        )

    def _begin_safety_lifecycle(self, reason: str, target: str) -> Optional[str]:
        self._interrupt_start(reason)
        if self._safety_action_id is not None:
            if target == "FAULT":
                self._safety_target_state = "FAULT"
            return self._safety_action_id
        try:
            self._safety_action_id = self.action_logger.begin_action(
                "PID",
                reason,
                source="pid",
                requested={
                    "target_terminal_state": target,
                    "temperature_c": self._run_state.measured_c,
                },
                safety=True,
            )
        except Exception:
            # The MainWindow still sends physical safety commands when the
            # audit disk fails; only manual override authorization requires a
            # successful durable record.
            self._safety_action_id = None
        self._safety_target_state = target
        self._run_state.controller_state = "STOPPING"
        self._commanded_v = 0.0
        self._run_state.output_v = 0.0
        self.pid_state_updated.emit(self._run_state)
        return self._safety_action_id

    def _request_coordinated_safety(self, reason: str, target: str) -> None:
        self._begin_safety_lifecycle(reason, target)
        self.safety_shutdown_requested.emit(reason, target)

    def _queue_next_start_step(self) -> None:
        """Queue exactly one PID startup command after its predecessor confirms."""
        if self._run_state.controller_state != "STARTING":
            return
        commands = (
            ("set_current", (self._config.current_limit_a,)),
            ("set_voltage", (0.0,)),
            ("output_on", ()),
        )
        command, args = commands[self._start_step_index]
        request_id = str(uuid.uuid4())
        self._start_pending = {request_id: command}
        if not self._queue_command(command, *args, request_id=request_id):
            self._start_pending.clear()
            self._fail_start("startup command could not be durably queued")

    def _interrupt_start(self, reason: str) -> None:
        """Give an interrupted Start lifecycle one terminal audit row."""
        if self._run_state.controller_state != "STARTING":
            return
        request_id = self._start_action_id
        self._start_action_id = None
        self._start_pending.clear()
        self._start_step_index = 0
        if request_id:
            try:
                self.action_logger.fail_action(
                    request_id,
                    f"PID Start interrupted by {reason}",
                    status="REJECTED",
                )
            except Exception:
                pass

    def _fail_start(self, error: str) -> None:
        if self._run_state.controller_state not in ("STARTING", "ARMED"):
            return
        request_id = self._start_action_id
        self._start_action_id = None
        self._start_pending.clear()
        self._start_step_index = 0
        if request_id:
            try:
                self.action_logger.fail_action(request_id, error)
            except Exception:
                pass
        self._run_state.fault_message = error
        self._request_coordinated_safety("PID Start Failed", "FAULT")

    @staticmethod
    def _synthetic_result(
        request_id: str,
        command: str,
        status: str,
        *,
        readback: Optional[dict] = None,
        error: str = "",
    ) -> PowerSupplyCommandResult:
        return PowerSupplyCommandResult(
            request_id=request_id,
            source="pid",
            command=command,
            status=status,
            requested={},
            effective={},
            readback=readback or {},
            error=error,
            completed_at_utc=utc_now_iso(),
            completed_monotonic_ns=time.perf_counter_ns(),
        )

    def _fault(self, message: str, emergency: bool = False):
        """Route every PID fault through MainWindow's timed coordinator."""
        reason = "PID Emergency Fault" if emergency else "PID Fault"
        if self._run_state.controller_state == "STOPPING":
            if self._safety_target_state != "FAULT":
                self._safety_target_state = "FAULT"
            self._run_state.fault_message = message
            # Upgrade MainWindow's active coordinator context as well.  It may
            # have started as a normal PID Stop whose confirmed terminal state
            # was STOPPED; a later invalid sample/generation change must end in
            # FAULT instead.
            self.safety_shutdown_requested.emit(reason, "FAULT")
            self.pid_state_updated.emit(self._run_state)
            return
        self._run_state.fault_message = message
        self._request_coordinated_safety(reason, "FAULT")
