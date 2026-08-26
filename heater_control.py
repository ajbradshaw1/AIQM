"""
Heater Control Module

Provides safe control of a resistive heater via OWON power supply
with built-in safety limits and data logging.
"""

import time
import csv
import os
import math
import threading
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Any, Optional, Callable
from dataclasses import dataclass
from abc import ABC, abstractmethod

from owon_power_supply import OWONPowerSupply
from gui.state import PowerSupplyState
from gui.heater_control.heater_session import (
    HeaterSessionLogger,
    state_snapshot,
    utc_now_iso,
)

try:
    from dracal_tmc100k import DracalTMC100k, find_dracal_sensors
    DRACAL_AVAILABLE = True
except ImportError:
    DRACAL_AVAILABLE = False


@dataclass
class HeaterLimits:
    """Safety limits for heater operation."""
    max_voltage: float = 24.0       # V - heater rated voltage
    max_current: float = 1.0        # A - heater rated current (24W/24V)
    max_power: float = 24.0         # W - heater rated power
    max_temperature: float = 400.0  # °C - heater max temp rating
    min_temperature: float = 0.0    # °C - minimum allowed temp


@dataclass
class HeaterState:
    """Current state of the heater system."""
    timestamp: datetime
    voltage_setpoint: float
    current_limit: float
    voltage_measured: float
    current_measured: float
    power_measured: float
    output_enabled: bool
    temperature: Optional[float] = None  # None if no sensor connected
    primary_completed_at_utc: Optional[str] = None
    primary_completed_monotonic_ns: Optional[int] = None
    primary_read_duration_ms: Optional[float] = None


class TemperatureSensor(ABC):
    """Abstract base class for temperature sensors."""

    @abstractmethod
    def read_temperature(self) -> float:
        """Read current temperature in °C."""
        pass

    @abstractmethod
    def connect(self) -> None:
        """Connect to the sensor."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from the sensor."""
        pass


class DummyTemperatureSensor(TemperatureSensor):
    """
    Placeholder sensor for testing without hardware.
    Simulates temperature based on power input.
    """

    def __init__(self, ambient: float = 25.0, thermal_mass: float = 10.0):
        self.ambient = ambient
        self.thermal_mass = thermal_mass  # °C per Watt at steady state
        self._temperature = ambient
        self._last_update = time.perf_counter()
        self._power = 0.0

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def update_power(self, power: float) -> None:
        """Update the simulated power input."""
        self._power = power

    def read_temperature(self) -> float:
        """Simulate temperature based on power and thermal dynamics."""
        now = time.perf_counter()
        dt = now - self._last_update
        self._last_update = now

        # Simple first-order thermal model
        target_temp = self.ambient + (self._power * self.thermal_mass)
        tau = 30.0  # thermal time constant in seconds
        self._temperature += (target_temp - self._temperature) * (1 - pow(0.5, dt/tau))

        return self._temperature


class DataLogger:
    """Logs heater data to CSV files."""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        self.file = None
        self.writer = None
        self.filename = None

    def start(self, prefix: str = "heater_log") -> str:
        """Start a new log file. Returns the filename."""
        os.makedirs(self.log_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.filename = os.path.join(self.log_dir, f"{prefix}_{timestamp}.csv")

        self.file = open(self.filename, 'w', newline='')
        self.writer = csv.writer(self.file)

        # Write header
        self.writer.writerow([
            "timestamp",
            "elapsed_seconds",
            "voltage_setpoint",
            "current_limit",
            "voltage_measured",
            "current_measured",
            "power_measured",
            "output_enabled",
            "temperature"
        ])
        self.file.flush()
        self._start_time = time.time()

        return self.filename

    def log(self, state: HeaterState) -> None:
        """Log a heater state entry."""
        if self.writer is None:
            raise RuntimeError("Logger not started. Call start() first.")

        elapsed = time.time() - self._start_time

        self.writer.writerow([
            state.timestamp.isoformat(),
            f"{elapsed:.3f}",
            f"{state.voltage_setpoint:.3f}",
            f"{state.current_limit:.3f}",
            f"{state.voltage_measured:.3f}",
            f"{state.current_measured:.3f}",
            f"{state.power_measured:.3f}",
            1 if state.output_enabled else 0,
            f"{state.temperature:.2f}" if state.temperature is not None else ""
        ])
        self.file.flush()

    def stop(self) -> None:
        """Close the log file."""
        if self.file:
            self.file.close()
            self.file = None
            self.writer = None


class HeaterController:
    """
    Safe controller for resistive heater via OWON power supply.

    Includes:
    - Configurable safety limits
    - Automatic limit enforcement
    - Data logging
    - Temperature sensor integration
    """

    def __init__(
        self,
        psu: OWONPowerSupply,
        limits: Optional[HeaterLimits] = None,
        temp_sensor: Optional[TemperatureSensor] = None,
    ):
        self.psu = psu
        self.limits = limits or HeaterLimits()
        self.temp_sensor = temp_sensor
        self.logger = DataLogger()
        self._logging = False
        self._emergency_stop = False
        # One owner lock serializes legacy CLI polling and interactive VISA
        # calls. RLock permits get_state() from within an audited action.
        self._io_lock = threading.RLock()

    def _enforce_limits(self, voltage: float, current: float) -> tuple[float, float]:
        """Enforce safety limits on voltage and current."""
        voltage = max(0, min(voltage, self.limits.max_voltage))
        current = max(0, min(current, self.limits.max_current))

        # Also limit by power
        if voltage * current > self.limits.max_power:
            # Scale down current to meet power limit
            current = self.limits.max_power / voltage if voltage > 0 else 0

        return voltage, current

    def _check_temperature_limits(self) -> bool:
        """Check if temperature is within limits. Returns True if safe."""
        if self.temp_sensor is None:
            return True

        try:
            temp = self.temp_sensor.read_temperature()
            if temp > self.limits.max_temperature:
                print(f"WARNING: Temperature {temp:.1f}°C exceeds limit {self.limits.max_temperature}°C!")
                return False
            if temp < self.limits.min_temperature:
                print(f"WARNING: Temperature {temp:.1f}°C below minimum {self.limits.min_temperature}°C!")
                return False
        except Exception as e:
            print(f"WARNING: Failed to read temperature: {e}")
            # Fail-safe: return False if we can't read temperature
            return False

        return True

    def get_state(self) -> HeaterState:
        """Get current heater state."""
        with self._io_lock:
            primary_started_ns = time.perf_counter_ns()
            v, i, p = self.psu.measure_all()
            primary_completed_ns = time.perf_counter_ns()
            primary_completed_at_utc = utc_now_iso()

            temp = None
            if self.temp_sensor:
                try:
                    temp = self.temp_sensor.read_temperature()
                except Exception:
                    pass

            return HeaterState(
                timestamp=datetime.now(timezone.utc),
                voltage_setpoint=self.psu.get_voltage_setpoint(),
                current_limit=self.psu.get_current_setpoint(),
                voltage_measured=v,
                current_measured=i,
                power_measured=p,
                output_enabled=self.psu.get_output_state(),
                temperature=temp,
                primary_completed_at_utc=primary_completed_at_utc,
                primary_completed_monotonic_ns=primary_completed_ns,
                primary_read_duration_ms=max(
                    0.0,
                    (primary_completed_ns - primary_started_ns) / 1_000_000.0,
                ),
            )

    def set_output(self, voltage: float, current: float) -> None:
        """Set voltage and current with safety limits enforced."""
        if self._emergency_stop:
            raise RuntimeError("Emergency stop active. Call reset_emergency() first.")

        voltage, current = self._enforce_limits(voltage, current)
        with self._io_lock:
            self.psu.set_voltage(voltage)
            self.psu.set_current(current)

    def enable(self) -> bool:
        """Enable output. Returns False if blocked by safety check."""
        if self._emergency_stop:
            print("Cannot enable: Emergency stop active.")
            return False

        if not self._check_temperature_limits():
            print("Cannot enable: Temperature out of limits.")
            return False

        with self._io_lock:
            self.psu.output_on()
        return True

    def disable(self) -> None:
        """Disable output immediately."""
        with self._io_lock:
            self.psu.output_off()

    def safe_shutdown(
        self,
        *,
        deadline_ns: Optional[int] = None,
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> dict[str, Any]:
        """Zero/off with a deadline check before and after every SCPI step."""
        with self._io_lock:
            instrument = getattr(self.psu, "instrument", None)
            original_timeout = (
                getattr(instrument, "timeout", None)
                if instrument is not None else None
            )

            def check_remaining() -> None:
                if deadline_ns is None:
                    return
                remaining_ns = deadline_ns - monotonic_ns()
                if remaining_ns <= 0:
                    raise TimeoutError(
                        "legacy safety transaction exceeded 15 seconds"
                    )
                if instrument is not None and original_timeout is not None:
                    remaining_ms = max(
                        1, int(math.ceil(remaining_ns / 1_000_000.0)),
                    )
                    try:
                        instrument.timeout = min(
                            int(original_timeout), remaining_ms,
                        )
                    except Exception:
                        # Unknown/fake drivers need not expose a mutable VISA
                        # timeout; pre/post deadline checks still apply.
                        pass

            def step(operation: Callable[[], Any]) -> Any:
                check_remaining()
                value = operation()
                check_remaining()
                return value

            try:
                step(self.psu.output_off)
                step(lambda: self.psu.set_voltage(0.0))
                step(lambda: self.psu.set_current(0.0))
                output_enabled = bool(step(self.psu.get_output_state))
                voltage_setpoint = float(step(self.psu.get_voltage_setpoint))
                current_setpoint = float(step(self.psu.get_current_setpoint))
                readback = {
                    "output_enabled": output_enabled,
                    "voltage_setpoint": voltage_setpoint,
                    "current_setpoint": current_setpoint,
                }
            finally:
                if instrument is not None and original_timeout is not None:
                    try:
                        instrument.timeout = original_timeout
                    except Exception:
                        pass
        if (
            readback["output_enabled"]
            or abs(readback["voltage_setpoint"]) > 0.005
            or abs(readback["current_setpoint"]) > 0.005
        ):
            raise RuntimeError("safe shutdown readback did not reach OFF / 0 V / 0 A")
        return readback

    def emergency_stop(
        self,
        *,
        deadline_ns: Optional[int] = None,
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> dict[str, Any]:
        """Emergency stop - disable output and prevent re-enabling."""
        self._emergency_stop = True
        readback = self.safe_shutdown(
            deadline_ns=deadline_ns, monotonic_ns=monotonic_ns,
        )
        print("!!! EMERGENCY STOP ACTIVATED !!!")
        return readback

    def reset_emergency(self) -> None:
        """Reset emergency stop state."""
        self._emergency_stop = False
        print("Emergency stop reset.")

    def start_logging(self, prefix: str = "heater_log") -> str:
        """Start data logging. Returns log filename."""
        filename = self.logger.start(prefix)
        self._logging = True
        print(f"Logging to: {filename}")
        return filename

    def stop_logging(self) -> None:
        """Stop data logging."""
        self._logging = False
        self.logger.stop()
        print("Logging stopped.")

    def log_state(self) -> HeaterState:
        """Get current state and log it if logging is active."""
        state = self.get_state()
        if self._logging:
            self.logger.log(state)
        return state

    def print_status(self) -> None:
        """Print current status to console."""
        state = self.get_state()

        print(f"\n{'='*50}")
        print(f"Heater Status - {state.timestamp.strftime('%H:%M:%S')}")
        print(f"{'='*50}")
        print(f"Output:      {'ON' if state.output_enabled else 'OFF'}")
        print(f"V Setpoint:  {state.voltage_setpoint:.2f} V")
        print(f"I Limit:     {state.current_limit:.3f} A")
        print(f"V Measured:  {state.voltage_measured:.3f} V")
        print(f"I Measured:  {state.current_measured:.3f} A")
        print(f"Power:       {state.power_measured:.3f} W")
        if state.temperature is not None:
            print(f"Temperature: {state.temperature:.1f} °C")
        print(f"{'='*50}")


class _LegacyRejected(RuntimeError):
    """An audited legacy action was intentionally refused."""


class LegacyHeaterRuntime:
    """Automatic telemetry and audited calls for the interactive legacy CLI."""

    def __init__(
        self,
        controller: HeaterController,
        session: HeaterSessionLogger,
        poll_interval: float = 0.5,
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
    ):
        self.controller = controller
        self.session = session
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sample_sequence = 0
        self._safety_pending = threading.Event()
        self._safety_lock = threading.Lock()
        self._safety_count = 0
        self._execution_lock = threading.Lock()
        self._monotonic_ns = monotonic_ns

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="legacy-heater-telemetry",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 3.5) -> bool:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout_s)
            return not self._thread.is_alive()
        return True

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._safety_pending.is_set():
                self._stop_event.wait(min(self.poll_interval, 0.05))
                continue
            started_ns = self._monotonic_ns()
            try:
                state = self.controller.get_state()
                received_ns = self._monotonic_ns()
                self._sample_sequence += 1
                received_at_utc = utc_now_iso()
                sample = PowerSupplyState(
                    voltage_setpoint=state.voltage_setpoint,
                    current_setpoint=state.current_limit,
                    voltage_measured=state.voltage_measured,
                    current_measured=state.current_measured,
                    power_measured=state.power_measured,
                    output_enabled=state.output_enabled,
                    connected=True,
                    source_at_utc=None,
                    primary_completed_at_utc=state.primary_completed_at_utc,
                    primary_completed_monotonic_ns=(
                        state.primary_completed_monotonic_ns
                    ),
                    primary_read_duration_ms=state.primary_read_duration_ms,
                    received_at_utc=received_at_utc,
                    sample_sequence=self._sample_sequence,
                    read_duration_ms=max(
                        0.0, (received_ns - started_ns) / 1_000_000.0,
                    ),
                    acquire_started_monotonic_ns=started_ns,
                    received_monotonic_ns=received_ns,
                    worker_emitted_monotonic_ns=received_ns,
                    valid=True,
                    connection_generation=1,
                    settings_sample_sequence=self._sample_sequence,
                    settings_received_at_utc=received_at_utc,
                    settings_received_monotonic_ns=received_ns,
                    settings_age_ms=0.0,
                    output_sample_sequence=self._sample_sequence,
                    output_valid=True,
                    output_received_at_utc=received_at_utc,
                    output_received_monotonic_ns=received_ns,
                    output_age_ms=0.0,
                    voltage_setpoint_sample_sequence=self._sample_sequence,
                    voltage_setpoint_received_at_utc=received_at_utc,
                    voltage_setpoint_received_monotonic_ns=received_ns,
                    voltage_setpoint_age_ms=0.0,
                    current_setpoint_sample_sequence=self._sample_sequence,
                    current_setpoint_received_at_utc=received_at_utc,
                    current_setpoint_received_monotonic_ns=received_ns,
                    current_setpoint_age_ms=0.0,
                )
                self.session.log_telemetry(sample)
            except Exception as exc:
                try:
                    self.session.log_action(
                        request_id=str(uuid.uuid4()),
                        source="legacy_cli",
                        action="telemetry_poll",
                        phase="FAILED",
                        error=str(exc),
                    )
                except Exception:
                    pass
            self._stop_event.wait(self.poll_interval)

    def execute(
        self,
        action: str,
        callback: Callable[[], Any],
        *,
        requested: Optional[dict[str, Any]] = None,
        effective: Optional[dict[str, Any]] = None,
        safety: bool = False,
        validator: Optional[Callable[[HeaterState], bool]] = None,
    ) -> Any:
        request_id = str(uuid.uuid4())
        if not safety and self._safety_pending.is_set():
            self.session.log_action(
                request_id=request_id,
                source="legacy_cli",
                action=action,
                phase="REQUESTED",
                requested=requested,
                effective=effective,
            )
            self.session.log_action(
                request_id=request_id,
                source="legacy_cli",
                action=action,
                phase="REJECTED",
                requested=requested,
                effective=effective,
                error="safety shutdown pending",
            )
            raise RuntimeError("safety shutdown pending")
        if safety:
            with self._safety_lock:
                self._safety_count += 1
                self._safety_pending.set()
        started_ns = self._monotonic_ns()
        deadline_ns = started_ns + 15_000_000_000 if safety else None
        logged = False
        try:
            self.session.log_action(
                request_id=request_id,
                source="legacy_cli",
                action=action,
                phase="REQUESTED",
                requested=requested,
                effective=effective,
                durable=safety,
            )
            logged = True
        except Exception:
            if not safety:
                raise
        try:
            # Serialize the complete callback + forced readback as one legacy
            # transaction.  HeaterController uses an RLock, so its public
            # methods remain compatible while the polling thread cannot
            # interleave VISA queries between a write and its confirmation.
            with self._execution_lock:
                if not safety and self._safety_pending.is_set():
                    raise _LegacyRejected("safety shutdown pending")
                with self.controller._io_lock:
                    if safety:
                        # This check occurs only after waiting for the single
                        # I/O owner lock.  If that wait consumed the budget,
                        # no safety SCPI command is started.
                        if self._monotonic_ns() >= deadline_ns:
                            raise TimeoutError(
                                "legacy safety transaction exceeded 15 seconds"
                            )
                        callback_name = getattr(callback, "__name__", "")
                        if callback_name not in {
                            "safe_shutdown", "emergency_stop", "disable",
                        }:
                            raise _LegacyRejected(
                                "safety request requires a deadline-aware "
                                "HeaterController shutdown operation"
                            )
                        if callback_name == "emergency_stop":
                            self.controller._emergency_stop = True
                        # Do not call the generic callback: every safety path
                        # uses the explicit deadline-aware OFF/V0/I0/readback
                        # transaction so no hidden step can begin out of time.
                        result = self.controller.safe_shutdown(
                            deadline_ns=deadline_ns,
                            monotonic_ns=self._monotonic_ns,
                        )
                        confirmation = HeaterState(
                            timestamp=datetime.now(timezone.utc),
                            voltage_setpoint=result["voltage_setpoint"],
                            current_limit=result["current_setpoint"],
                            voltage_measured=0.0,
                            current_measured=0.0,
                            power_measured=0.0,
                            output_enabled=result["output_enabled"],
                        )
                    else:
                        result = callback()
                        if result is False:
                            raise _LegacyRejected(
                                f"{action} callback rejected the request"
                            )
                        confirmation = self.controller.get_state()
                    if validator is not None and not validator(confirmation):
                        raise RuntimeError(f"{action} readback confirmation failed")

            if logged:
                try:
                    self.session.log_action(
                        request_id=request_id,
                        source="legacy_cli",
                        action=action,
                        phase="CONFIRMED",
                        requested=requested,
                        effective=effective,
                        confirmation_sample={
                            "voltage_setpoint": confirmation.voltage_setpoint,
                            "current_setpoint": confirmation.current_limit,
                            "voltage_measured": confirmation.voltage_measured,
                            "current_measured": confirmation.current_measured,
                            "power_measured": confirmation.power_measured,
                            "output_enabled": confirmation.output_enabled,
                        },
                        readback=result if isinstance(result, dict) else None,
                        durable=safety,
                    )
                except Exception:
                    if not safety:
                        raise
            return result
        except Exception as exc:
            if logged:
                try:
                    self.session.log_action(
                        request_id=request_id,
                        source="legacy_cli",
                        action=action,
                        phase=(
                            "REJECTED" if isinstance(exc, _LegacyRejected)
                            else "FAILED"
                        ),
                        requested=requested,
                        effective=effective,
                        error=str(exc),
                        durable=safety,
                    )
                except Exception:
                    pass
            raise
        finally:
            if safety:
                with self._safety_lock:
                    self._safety_count = max(0, self._safety_count - 1)
                    if self._safety_count == 0:
                        self._safety_pending.clear()


def manual_control_session(resource: str = "ASRL/dev/tty.usbserial-110::INSTR"):
    """Audited legacy CLI with automatic telemetry and serialized PSU I/O."""
    print("\n" + "=" * 60)
    print("HEATER MANUAL CONTROL SESSION")
    print("=" * 60)

    limits = HeaterLimits()
    session = HeaterSessionLogger(
        os.environ.get("AI4MBE_HEATER_LOG_ROOT", "logs/heater")
    )
    session.ensure_started({
        "application": "legacy_heater_cli",
        "resource_requested": resource,
    })
    print(f"Audit directory: {session.session_dir}")

    connect_id = str(uuid.uuid4())
    session.log_action(
        request_id=connect_id,
        source="legacy_cli",
        action="Connect",
        phase="REQUESTED",
        requested={"resource": resource},
    )

    connected = False
    disconnect_id: Optional[str] = None
    try:
        with ExitStack() as supply_stack:
            psu = supply_stack.enter_context(OWONPowerSupply(resource))
            connected = True
            session.log_action(
                request_id=connect_id,
                source="legacy_cli",
                action="Connect",
                phase="CONFIRMED",
                requested={"resource": resource},
                readback={"identity": psu.identify()},
            )

            sensor = None
            if DRACAL_AVAILABLE:
                try:
                    sensors = find_dracal_sensors()
                    if sensors:
                        port, identity = sensors[0]
                        sensor = DracalTMC100k(port=port)
                        sensor.connect()
                        print(f"Thermocouple: {identity} on {port}")
                except Exception as exc:
                    print(f"Failed to connect to Dracal sensor: {exc}")
                    sensor = None
            if sensor is None:
                sensor = DummyTemperatureSensor()
                sensor.connect()
                print("Using simulated temperature sensor.")

            controller = HeaterController(psu, limits, sensor)
            runtime = LegacyHeaterRuntime(controller, session)

            is_safe = lambda state: (
                not state.output_enabled
                and abs(state.voltage_setpoint) <= 0.005
                and abs(state.current_limit) <= 0.005
            )
            try:
                runtime.execute(
                    "Initial Safe Shutdown",
                    controller.safe_shutdown,
                    safety=True,
                    validator=is_safe,
                )
            except Exception:
                # Never let the context manager disconnect an unknown/unsafe
                # supply merely because initial safety confirmation failed.
                supply_stack.pop_all()
                raise
            runtime.start()

            print("Commands: v <V>, i <A>, on, off, s, log, stoplog, stop, reset, q")
            try:
                while True:
                    try:
                        raw = input("\n> ").strip()
                    except EOFError:
                        raw = "q"
                    if not raw:
                        continue
                    parts = raw.split()
                    action = parts[0].lower()

                    try:
                        if action == "q":
                            runtime.execute(
                                "Quit Safe Shutdown",
                                controller.safe_shutdown,
                                safety=True,
                                validator=is_safe,
                            )
                            break
                        if action == "v" and len(parts) == 2:
                            requested = float(parts[1])
                            effective_v, effective_i = controller._enforce_limits(
                                requested, controller.get_state().current_limit,
                            )
                            runtime.execute(
                                "Set Voltage",
                                lambda: controller.set_output(effective_v, effective_i),
                                requested={
                                    "requested_voltage_v": requested,
                                },
                                effective={
                                    "voltage_setpoint_v": effective_v,
                                    "current_limit_a": effective_i,
                                },
                                validator=lambda state: abs(
                                    state.voltage_setpoint - effective_v
                                ) <= 0.005,
                            )
                            print(f"Voltage set to {effective_v:.3f} V")
                        elif action == "i" and len(parts) == 2:
                            requested = float(parts[1])
                            effective_v, effective_i = controller._enforce_limits(
                                controller.get_state().voltage_setpoint, requested,
                            )
                            runtime.execute(
                                "Set Current",
                                lambda: controller.set_output(effective_v, effective_i),
                                requested={
                                    "requested_current_a": requested,
                                },
                                effective={
                                    "voltage_setpoint_v": effective_v,
                                    "current_limit_a": effective_i,
                                },
                                validator=lambda state: abs(
                                    state.current_limit - effective_i
                                ) <= 0.005,
                            )
                            print(f"Current limit set to {effective_i:.3f} A")
                        elif action == "on":
                            runtime.execute(
                                "Output On",
                                controller.enable,
                                validator=lambda state: state.output_enabled,
                            )
                            print("Output ENABLED")
                        elif action == "off":
                            runtime.execute(
                                "Output Off",
                                controller.disable,
                                safety=True,
                                validator=lambda state: not state.output_enabled,
                            )
                            print("Output DISABLED")
                        elif action == "stop":
                            runtime.execute(
                                "Emergency Stop",
                                controller.emergency_stop,
                                safety=True,
                                validator=is_safe,
                            )
                        elif action == "reset":
                            runtime.execute("Reset Emergency", controller.reset_emergency)
                        elif action == "s":
                            controller.print_status()
                        elif action == "log":
                            print(f"Mandatory audit directory: {session.session_dir}")
                        elif action == "stoplog":
                            rejected_id = str(uuid.uuid4())
                            session.log_action(
                                request_id=rejected_id,
                                source="legacy_cli",
                                action="Stop Mandatory Audit",
                                phase="REQUESTED",
                            )
                            session.log_action(
                                request_id=rejected_id,
                                source="legacy_cli",
                                action="Stop Mandatory Audit",
                                phase="REJECTED",
                                error="mandatory heater audit cannot be disabled",
                            )
                            print("Mandatory heater audit cannot be stopped.")
                        else:
                            print("Unknown command: v, i, on, off, s, log, stoplog, stop, reset, q")
                    except (ValueError, RuntimeError) as exc:
                        print(f"Command failed: {exc}")

                    if isinstance(sensor, DummyTemperatureSensor):
                        sensor.update_power(controller.get_state().power_measured)
            finally:
                final_safe_confirmed = False
                try:
                    runtime.execute(
                        "Final Safe Shutdown",
                        controller.safe_shutdown,
                        safety=True,
                        validator=is_safe,
                    )
                    final_safe_confirmed = True
                finally:
                    if not runtime.stop(timeout_s=15.0):
                        session.log_action(
                            request_id=str(uuid.uuid4()),
                            source="legacy_cli",
                            action="Stop Telemetry Thread",
                            phase="FAILED",
                            error=(
                                "telemetry thread still owns serialized I/O; "
                                "blocking disconnect until it exits"
                            ),
                            durable=True,
                        )
                        print(
                            "Telemetry thread is still inside I/O; disconnect is blocked."
                        )
                        # Detach the context callback before raising: automatic
                        # disconnect here would race the still-live I/O owner.
                        supply_stack.pop_all()
                        raise RuntimeError(
                            "legacy telemetry thread did not stop within 15 seconds; "
                            "power-supply disconnect was blocked"
                        )
                    sensor.disconnect()
                    if not final_safe_confirmed:
                        supply_stack.pop_all()

            disconnect_id = str(uuid.uuid4())
            session.log_action(
                request_id=disconnect_id,
                source="legacy_cli",
                action="Disconnect",
                phase="REQUESTED",
                durable=True,
            )
            # Physical disconnect completes before its CONFIRMED audit row.
            supply_stack.close()
        # The context manager has now completed its physical disconnect.  Do
        # not claim CONFIRMED while ``__exit__`` is still pending.
        if disconnect_id is not None:
            session.log_action(
                request_id=disconnect_id,
                source="legacy_cli",
                action="Disconnect",
                phase="CONFIRMED",
                durable=True,
            )
            connected = False
    except Exception as exc:
        try:
            if disconnect_id is not None:
                session.log_action(
                    request_id=disconnect_id,
                    source="legacy_cli",
                    action="Disconnect",
                    phase="FAILED",
                    error=str(exc),
                    durable=True,
                )
            session.log_action(
                request_id=connect_id if not connected else str(uuid.uuid4()),
                source="legacy_cli",
                action="Connect" if not connected else "Legacy CLI Session",
                phase="FAILED",
                error=str(exc),
                durable=True,
            )
        except Exception:
            pass
        raise
    finally:
        session.close()
        print("Session ended. Mandatory audit closed.")


if __name__ == "__main__":
    manual_control_session()
