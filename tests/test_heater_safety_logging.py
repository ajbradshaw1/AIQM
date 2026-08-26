"""Offline regression tests for heater safety transactions and audit files."""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

import heater_control as heater_control_module
from gui.heater_control.action_logger import ActionLogger
from gui.heater_control.action_log_tab import ActionLogTab
from gui.heater_control import action_log_tab as action_log_tab_module
from gui.heater_control import main_window as main_window_module
from gui.heater_control.main_window import MainWindow, ShutdownContext
from gui.heater_control.heater_commands import (
    PowerSupplyCommand,
    PowerSupplyCommandResult,
)
from gui.heater_control.heater_session import HeaterSessionLogger, utc_now_iso
from gui.heater_control.pid_controller import GainBand, PIDConfig, PIDController
from gui.heater_control.dashboard_tab import DashboardTab
from gui.heater_control.power_supply_tab import PowerSupplyTab
from gui.state import ActionLogEntry, PowerSupplyState, TemperatureState
from gui.workers import PowerSupplyWorker
from gui.widgets import ControlPanel
from heater_control import (
    DummyTemperatureSensor,
    HeaterController,
    LegacyHeaterRuntime,
)


class FakeOWON:
    def __init__(self):
        self.calls: list[tuple[str, float | None]] = []
        self.voltage = 4.0
        self.current = 0.5
        self.output = True
        self.ovp = 24.0
        self.ocp = 1.0
        self.measure_count = 0

    def connect(self):
        self.calls.append(("connect", None))

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.disconnect()
        return False

    @staticmethod
    def identify():
        return "FAKE OWON"

    def disconnect(self):
        self.calls.append(("disconnect", None))

    def output_off(self):
        self.calls.append(("output_off", None))
        self.output = False

    def output_on(self):
        self.calls.append(("output_on", None))
        self.output = True

    def set_voltage(self, value):
        self.calls.append(("set_voltage", float(value)))
        self.voltage = float(value)

    def set_current(self, value):
        self.calls.append(("set_current", float(value)))
        self.current = float(value)

    def get_output_state(self):
        self.calls.append(("get_output_state", None))
        return self.output

    def get_voltage_setpoint(self):
        self.calls.append(("get_voltage_setpoint", None))
        return self.voltage

    def get_current_setpoint(self):
        self.calls.append(("get_current_setpoint", None))
        return self.current

    def set_ovp(self, value):
        self.ovp = float(value)

    def get_ovp(self):
        return self.ovp

    def set_ocp(self, value):
        self.ocp = float(value)

    def get_ocp(self):
        return self.ocp

    def measure_all(self):
        self.measure_count += 1
        return self.voltage, self.current, self.voltage * self.current


_QT_APP = None


def _app():
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


def _command(name: str) -> PowerSupplyCommand:
    return PowerSupplyCommand(
        request_id="request-1",
        source="test",
        command=name,
        requested_at_utc=utc_now_iso(),
        requested_monotonic_ns=time.perf_counter_ns(),
        priority=0,
    )


def test_safe_shutdown_order_and_forced_readback():
    _app()
    fake = FakeOWON()
    worker = PowerSupplyWorker("FAKE", psu_factory=lambda _resource: fake)
    worker._commands_gated = False
    worker.psu = fake

    result = worker._execute_transaction(_command("safe_shutdown"), PowerSupplyState())

    assert result.status == "CONFIRMED"
    assert fake.calls[:6] == [
        ("output_off", None),
        ("set_voltage", 0.0),
        ("set_current", 0.0),
        ("get_output_state", None),
        ("get_voltage_setpoint", None),
        ("get_current_setpoint", None),
    ]
    assert result.readback == {
        "output_enabled": False,
        "voltage_setpoint": 0.0,
        "current_setpoint": 0.0,
    }


def test_safe_shutdown_requires_readback_thresholds():
    class StuckVoltage(FakeOWON):
        def set_voltage(self, value):
            self.calls.append(("set_voltage", float(value)))

    fake = StuckVoltage()
    worker = PowerSupplyWorker("FAKE", psu_factory=lambda _resource: fake)
    worker.psu = fake

    result = worker._execute_transaction(_command("emergency_stop"), PowerSupplyState())

    assert result.status == "FAILED"
    assert "did not reach zero/off" in result.error


def test_safety_command_rejects_queued_ordinary_work():
    _app()
    fake = FakeOWON()
    worker = PowerSupplyWorker("FAKE", psu_factory=lambda _resource: fake)
    rejected = []
    worker.command_completed.connect(rejected.append)

    ordinary_id = worker.queue_command("set_voltage", 10.0, source="manual_ui")
    safety_id = worker.request_safe_shutdown(source="system")

    assert [(item.request_id, item.status) for item in rejected] == [
        (ordinary_id, "REJECTED")
    ]
    _priority, _sequence, queued = worker._command_queue.get_nowait()
    assert queued.request_id == safety_id
    assert queued.command == "safe_shutdown"


def test_action_lifecycle_and_telemetry_are_separate_append_only_files(tmp_path):
    _app()
    logger = ActionLogger(log_root=tmp_path)
    logger.ensure_session({"test": True})
    request_id = logger.begin_action(
        "Power Supply",
        "Set Voltage",
        source="manual_ui",
        requested={"voltage_v": 2.5},
    )
    logger.fail_action(request_id, "readback mismatch")

    sample = PowerSupplyState(
        voltage_setpoint=2.5,
        current_setpoint=0.5,
        voltage_measured=2.49,
        current_measured=0.4,
        power_measured=0.996,
        output_enabled=True,
        connected=True,
        received_at_utc=utc_now_iso(),
        sample_sequence=1,
        acquire_started_monotonic_ns=100,
        received_monotonic_ns=200,
        worker_emitted_monotonic_ns=210,
        gui_received_monotonic_ns=220,
        read_duration_ms=0.0001,
        valid=True,
        connection_generation=1,
        settings_sample_sequence=1,
        settings_received_at_utc=utc_now_iso(),
        settings_received_monotonic_ns=200,
        settings_age_ms=0.0,
    )
    assert logger.record_telemetry(sample)
    assert not logger.record_telemetry(sample)
    logger.clear()
    session_dir = logger.session_dir
    logger.close()

    with (session_dir / "heater_actions.csv").open(newline="", encoding="utf-8") as fh:
        actions = list(csv.DictReader(fh))
    with (session_dir / "heater_telemetry.csv").open(newline="", encoding="utf-8") as fh:
        telemetry = list(csv.DictReader(fh))
    metadata = json.loads((session_dir / "session_metadata.json").read_text("utf-8"))

    assert [row["phase"] for row in actions[:2]] == ["REQUESTED", "FAILED"]
    assert actions[-1]["action"] == "ui_view_cleared"
    assert len(telemetry) == 1
    assert telemetry[0]["sample_sequence"] == "1"
    assert metadata["timestamp_policy"]["device_clock"] is None
    assert logger.count() == 0


def test_audit_effective_value_falls_back_to_worker_result(tmp_path):
    logger = ActionLogger(log_root=tmp_path)
    logger.ensure_session()
    logger.update_psu_state(PowerSupplyState(
        connected=True,
        valid=True,
        voltage_setpoint=9.0,
        sample_sequence=4,
        connection_generation=1,
    ))
    request_id = logger.begin_action(
        "Power Supply", "Set Voltage", source="manual_ui",
        requested={"args": [2.0]},
    )
    logger.complete_action(PowerSupplyCommandResult(
        request_id=request_id,
        source="manual_ui",
        command="set_voltage",
        status="CONFIRMED",
        requested={"args": [2.0]},
        effective={"voltage_setpoint": 2.0},
        readback={"voltage_setpoint": 2.0},
        error="",
        completed_at_utc=utc_now_iso(),
        completed_monotonic_ns=time.perf_counter_ns(),
    ))
    actions_path = logger.session.actions_path
    logger.close()
    with actions_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert json.loads(rows[-1]["effective_json"]) == {"voltage_setpoint": 2.0}
    confirmation = json.loads(rows[-1]["confirmation_sample_json"])
    assert confirmation["voltage_setpoint"] == 2.0
    assert confirmation["sample_sequence"] == 0


def test_invalid_poll_is_not_persisted(tmp_path):
    session = HeaterSessionLogger(tmp_path)
    session.ensure_started()
    session.log_telemetry(PowerSupplyState(connected=True, valid=False))
    path = session.telemetry_path
    session.close()
    with path.open(newline="", encoding="utf-8") as fh:
        assert list(csv.DictReader(fh)) == []


def test_legacy_runtime_auto_polls_and_serializes_safety(tmp_path):
    fake = FakeOWON()
    controller = HeaterController(fake, temp_sensor=DummyTemperatureSensor())
    session = HeaterSessionLogger(tmp_path)
    session.ensure_started()
    runtime = LegacyHeaterRuntime(controller, session, poll_interval=0.01)
    runtime.start()
    try:
        runtime.execute(
            "Emergency Stop",
            controller.emergency_stop,
            safety=True,
            validator=lambda state: (
                not state.output_enabled
                and abs(state.voltage_setpoint) <= 0.005
                and abs(state.current_limit) <= 0.005
            ),
        )
        deadline = time.monotonic() + 1.0
        while runtime._sample_sequence < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        runtime.stop()
        telemetry_path = session.telemetry_path
        actions_path = session.actions_path
        session.close()

    assert runtime._sample_sequence >= 1
    with telemetry_path.open(newline="", encoding="utf-8") as fh:
        assert len(list(csv.DictReader(fh))) >= 1
    with actions_path.open(newline="", encoding="utf-8") as fh:
        actions = list(csv.DictReader(fh))
    assert [row["phase"] for row in actions[:2]] == ["REQUESTED", "CONFIRMED"]


def test_session_directories_never_overwrite(tmp_path):
    first = HeaterSessionLogger(tmp_path)
    second = HeaterSessionLogger(tmp_path)
    assert first.ensure_started() != second.ensure_started()
    first.close()
    second.close()


def test_audit_failure_rejects_normal_command_but_not_emergency_stop():
    class FailingLogger:
        @staticmethod
        def begin_action(*_args, **_kwargs):
            raise OSError("disk full")

    class Worker:
        def __init__(self):
            self.commands = []

        @staticmethod
        def isRunning():
            return True

        def queue_command(self, command, *args, **kwargs):
            self.commands.append((command, args, kwargs))

    class StatusBar:
        def showMessage(self, _message):
            pass

    class Harness:
        def __init__(self):
            self.action_logger = FailingLogger()
            self.psu_worker = Worker()
            self._status = StatusBar()
            self.pid_controller = type(
                "Pid", (), {"begin_external_shutdown": lambda *_args: None}
            )()
            self.safety_requests = []

        def statusBar(self):
            return self._status

        def _begin_safe_shutdown(self, reason, **kwargs):
            self.safety_requests.append((reason, kwargs))

    harness = Harness()
    MainWindow._on_psu_command(harness, "set_voltage", (2.0,))
    assert harness.psu_worker.commands == []

    MainWindow._on_psu_command(harness, "emergency_stop", ())
    assert harness.safety_requests
    assert harness.safety_requests[0][0] == "Manual PSU Emergency Stop"


def test_two_safety_transactions_hold_gate_until_both_complete():
    _app()
    fake = FakeOWON()
    worker = PowerSupplyWorker("FAKE", psu_factory=lambda _resource: fake)
    worker.psu = fake
    worker._commands_gated = False
    results = []
    worker.command_completed.connect(results.append)

    first = worker.request_safe_shutdown(source="system")
    second = worker.request_safe_shutdown(source="pid")
    rejected = worker.queue_command("set_voltage", 3.0, source="pid")
    assert worker._pending_safety_count == 2
    assert any(item.request_id == rejected and item.status == "REJECTED" for item in results)

    assert worker._process_queued_commands(PowerSupplyState(connection_generation=1))
    assert worker._pending_safety_count == 0
    assert {
        item.request_id for item in results if item.status == "CONFIRMED"
    } >= {first, second}

    accepted = worker.queue_command("set_voltage", 1.0, source="pid")
    queued_ids = [item[2].request_id for item in list(worker._command_queue.queue)]
    assert accepted in queued_ids


def test_safety_cycle_defers_racing_ordinary_command_until_next_turn():
    fake = FakeOWON()
    worker = PowerSupplyWorker("FAKE", psu_factory=lambda _resource: fake)
    worker.psu = fake
    worker._commands_gated = False
    safety = _command("safe_shutdown")
    ordinary = PowerSupplyCommand(
        request_id="ordinary-after-safety",
        source="pid",
        command="set_voltage",
        args=(2.0,),
        priority=10,
    )
    worker._pending_safety_count = 1
    worker._command_queue.put((0, 0, safety))
    worker._command_queue.put((10, 1, ordinary))

    assert worker._process_queued_commands(PowerSupplyState())
    assert fake.voltage == 0.0
    assert [item[2].request_id for item in worker._command_queue.queue] == [
        "ordinary-after-safety"
    ]
    assert not worker._process_queued_commands(PowerSupplyState())
    assert fake.voltage == 2.0


def test_stopped_worker_rejects_safety_immediately():
    _app()
    worker = PowerSupplyWorker("FAKE", psu_factory=lambda _resource: FakeOWON())
    results = []
    worker.command_completed.connect(results.append)
    worker.stop()
    request_id = worker.request_safe_shutdown(source="system")
    assert [(item.request_id, item.status) for item in results] == [
        (request_id, "REJECTED")
    ]


def test_connection_generation_gate_rejects_normal_but_allows_safety():
    _app()
    worker = PowerSupplyWorker("FAKE", psu_factory=lambda _resource: FakeOWON())
    results = []
    worker.command_completed.connect(results.append)
    normal_id = worker.queue_command("set_voltage", 1.0, source="manual_ui")
    safety_id = worker.request_safe_shutdown(source="system")
    assert any(
        item.request_id == normal_id
        and item.status == "REJECTED"
        and "first valid sample" in item.error
        for item in results
    )
    assert safety_id in [item[2].request_id for item in worker._command_queue.queue]


def test_primary_measurement_survives_secondary_failure_and_has_freshness():
    app = _app()

    class OutputQueryFails(FakeOWON):
        def get_output_state(self):
            raise RuntimeError("OUTP? failed")

    fake = OutputQueryFails()
    worker = PowerSupplyWorker(
        "FAKE", poll_interval=0.01, psu_factory=lambda _resource: fake,
    )
    states = []
    events = []
    worker.state_updated.connect(states.append)
    worker.system_event.connect(events.append)
    worker.start()
    deadline = time.monotonic() + 2.0
    while not any(state.valid for state in states) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    worker.stop()
    assert worker.wait(2000)
    app.processEvents()

    state = next(state for state in states if state.valid)
    assert state.sample_sequence == 1
    assert state.primary_completed_at_utc
    assert state.primary_completed_monotonic_ns <= state.received_monotonic_ns
    assert state.primary_read_duration_ms <= state.read_duration_ms
    assert state.output_sample_sequence == 0
    assert state.voltage_setpoint_sample_sequence == 1
    assert state.current_setpoint_sample_sequence == 1
    assert state.ovp_sample_sequence == 1
    assert state.ocp_sample_sequence == 1
    assert "secondary query failed: output" in state.error
    assert any(event["action"] == "secondary_output_failed" for event in events)


def test_failed_primary_poll_keeps_secondary_values_but_advances_age():
    app = _app()

    class OneGoodPoll(FakeOWON):
        def measure_all(self):
            self.measure_count += 1
            if self.measure_count > 1:
                raise RuntimeError("primary read failed")
            return self.voltage, self.current, self.voltage * self.current

    fake = OneGoodPoll()
    worker = PowerSupplyWorker(
        "FAKE", poll_interval=0.01, psu_factory=lambda _resource: fake,
    )
    states = []
    worker.state_updated.connect(states.append)
    worker.start()
    deadline = time.monotonic() + 2.0
    while not any(not state.valid for state in states) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    worker.stop()
    assert worker.wait(2000)
    app.processEvents()

    valid = next(state for state in states if state.valid)
    failed = next(state for state in states if not state.valid)
    assert failed.sample_sequence == valid.sample_sequence == 1
    assert failed.output_sample_sequence == valid.output_sample_sequence == 1
    assert failed.output_enabled == valid.output_enabled
    assert failed.output_age_ms > valid.output_age_ms


def test_command_readback_refreshes_individual_field_provenance():
    fake = FakeOWON()
    worker = PowerSupplyWorker("FAKE", psu_factory=lambda _resource: fake)
    worker.psu = fake
    state = PowerSupplyState(connection_generation=3)
    command = PowerSupplyCommand(
        request_id="set-v",
        source="test",
        command="set_voltage",
        args=(2.0,),
    )
    result = worker._execute_transaction(command, state)
    assert result.confirmed
    assert result.connection_generation == 3
    assert state.voltage_setpoint_sample_sequence == 1
    assert state.voltage_setpoint_received_at_utc
    assert state.voltage_setpoint_received_monotonic_ns is not None


def _pid_config() -> PIDConfig:
    return PIDConfig(
        target_c=50.0,
        hold_s=0.0,
        margin_c=1.0,
        max_voltage=10.0,
        current_limit_a=0.5,
        slew_rate_v_per_s=1.0,
        bands=[GainBand(1.0, 0.0, 0.0) for _ in range(3)],
        threshold_t1=60.0,
        threshold_t2=100.0,
        interp_band_c=10.0,
        hard_cutoff_c=150.0,
    )


class _PIDWorker:
    def __init__(self):
        self.commands = []

    def queue_command(self, command, *args, **kwargs):
        self.commands.append((command, args, kwargs))
        return kwargs["request_id"]


def _command_result(request_id, command, status="CONFIRMED", generation=1):
    return PowerSupplyCommandResult(
        request_id=request_id,
        source="pid",
        command=command,
        status=status,
        requested={},
        effective={},
        readback={},
        error="" if status == "CONFIRMED" else "fake rejection",
        completed_at_utc=utc_now_iso(),
        completed_monotonic_ns=time.perf_counter_ns(),
        connection_generation=generation,
    )


def test_pid_start_waits_for_all_three_hardware_confirmations(tmp_path):
    logger = ActionLogger(log_root=tmp_path)
    logger.ensure_session()
    worker = _PIDWorker()
    pid = PIDController(logger)
    pid.set_psu_worker(worker)
    pid.on_psu_state(PowerSupplyState(
        connected=True,
        valid=True,
        voltage_measured=0.0,
        current_measured=0.0,
        power_measured=0.0,
        sample_sequence=1,
        connection_generation=1,
    ))
    pid.on_temp_state(TemperatureState(connected=True, temperature=25.0))
    pid.arm(_pid_config())
    pid.start()
    assert pid._run_state.controller_state == "STARTING"
    assert [item[0] for item in worker.commands] == ["set_current"]

    command, _args, kwargs = worker.commands[0]
    result = _command_result(kwargs["request_id"], command)
    logger.complete_action(result)
    pid.on_command_result(result)
    assert pid._run_state.controller_state == "STARTING"
    assert [item[0] for item in worker.commands] == [
        "set_current", "set_voltage",
    ]

    command, _args, kwargs = worker.commands[1]
    result = _command_result(kwargs["request_id"], command)
    logger.complete_action(result)
    pid.on_command_result(result)
    assert pid._run_state.controller_state == "STARTING"
    assert [item[0] for item in worker.commands] == [
        "set_current", "set_voltage", "output_on",
    ]

    command, _args, kwargs = worker.commands[2]
    result = _command_result(kwargs["request_id"], command)
    logger.complete_action(result)
    pid.on_command_result(result)
    assert pid._run_state.controller_state == "RUNNING"
    logger.close()


def test_pid_start_failure_and_generation_change_route_safety_coordinator(tmp_path):
    logger = ActionLogger(log_root=tmp_path)
    logger.ensure_session()
    worker = _PIDWorker()
    pid = PIDController(logger)
    requests = []
    pid.safety_shutdown_requested.connect(lambda reason, target: requests.append((reason, target)))
    valid = PowerSupplyState(
        connected=True,
        valid=True,
        voltage_measured=0.0,
        current_measured=0.0,
        power_measured=0.0,
        sample_sequence=1,
        connection_generation=1,
    )
    pid.set_psu_worker(worker)
    pid.on_psu_state(valid)
    pid.arm(_pid_config())
    pid.start()
    command, _args, kwargs = worker.commands[0]
    rejected = _command_result(kwargs["request_id"], command, "REJECTED")
    logger.complete_action(rejected)
    pid.on_command_result(rejected)
    assert pid._run_state.controller_state == "STOPPING"
    assert requests[-1] == ("PID Start Failed", "FAULT")
    assert not any(item[0] == "output_on" for item in worker.commands)
    pid.on_safety_resolution(
        confirmed=False, terminal_state="FAULT", error="worker dead",
    )
    assert pid._run_state.controller_state == "FAULT"

    # A running controller treats a new connection generation as fail-safe.
    second = PIDController(logger)
    second.set_psu_worker(worker)
    second._run_state.controller_state = "RUNNING"
    second._expected_generation = 1
    second_requests = []
    second.safety_shutdown_requested.connect(
        lambda reason, target: second_requests.append((reason, target))
    )
    second.on_psu_state(replace(valid, connection_generation=2))
    assert second._run_state.controller_state == "STOPPING"
    assert second_requests[-1][1] == "FAULT"
    logger.close()


def test_real_worker_pid_start_voltage_failure_never_calls_output_on(tmp_path):
    app = _app()

    class VoltageReadbackFails(FakeOWON):
        def set_voltage(self, value):
            self.calls.append(("set_voltage", float(value)))
            # Keep the old 4 V readback so the 0 V startup step fails.

    fake = VoltageReadbackFails()
    logger = ActionLogger(log_root=tmp_path)
    logger.ensure_session()
    worker = PowerSupplyWorker(
        "FAKE", poll_interval=0.01, psu_factory=lambda _resource: fake,
    )
    pid = PIDController(logger)
    pid.set_psu_worker(worker)
    latest = []
    worker.state_updated.connect(latest.append)

    def complete(result):
        logger.complete_action(result)
        pid.on_command_result(result)

    worker.command_completed.connect(complete)
    worker.start()
    deadline = time.monotonic() + 2.0
    while not any(state.valid for state in latest) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    pid.on_psu_state(next(state for state in latest if state.valid))
    pid.arm(_pid_config())
    pid.start()
    deadline = time.monotonic() + 2.0
    while pid._run_state.controller_state == "STARTING" and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    worker.stop()
    assert worker.wait(2000)
    app.processEvents()

    assert pid._run_state.controller_state == "STOPPING"
    assert ("set_voltage", 0.0) in fake.calls
    assert not any(call[0] == "output_on" for call in fake.calls)
    logger.close()


def test_pid_fault_while_stopping_upgrades_coordinator_terminal_to_fault(tmp_path):
    logger = ActionLogger(log_root=tmp_path)
    logger.ensure_session()
    pid = PIDController(logger)
    pid._run_state.controller_state = "STOPPING"
    pid._safety_target_state = "STOPPED"
    requests = []
    pid.safety_shutdown_requested.connect(
        lambda reason, target: requests.append((reason, target))
    )

    pid._fault("connection generation changed", emergency=True)

    assert pid._run_state.controller_state == "STOPPING"
    assert pid._run_state.fault_message == "connection generation changed"
    assert pid._safety_target_state == "FAULT"
    assert requests[-1] == ("PID Emergency Fault", "FAULT")
    logger.close()


def test_pid_estop_and_idle_reset_have_full_lifecycles(tmp_path):
    logger = ActionLogger(log_root=tmp_path)
    logger.ensure_session()
    pid = PIDController(logger)
    requests = []
    pid.safety_shutdown_requested.connect(
        lambda reason, target: requests.append((reason, target))
    )

    pid.emergency_stop()
    assert pid._run_state.controller_state == "STOPPING"
    assert requests == [("PID Emergency Stop", "STOPPED")]
    pid.on_safety_resolution(confirmed=True, terminal_state="STOPPED")
    pid.reset()
    actions_path = logger.session.actions_path
    logger.close()

    with actions_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    phases = {}
    for row in rows:
        phases.setdefault(row["action"], []).append(row["phase"])
    assert phases["PID Emergency Stop"] == ["REQUESTED", "CONFIRMED"]
    assert phases["Reset"] == ["REQUESTED", "CONFIRMED"]


def test_pid_start_interrupted_by_disconnect_is_terminal_and_cancels_steps(tmp_path):
    logger = ActionLogger(log_root=tmp_path)
    logger.ensure_session()
    worker = _PIDWorker()
    pid = PIDController(logger)
    pid.set_psu_worker(worker)
    pid.on_psu_state(PowerSupplyState(
        connected=True, valid=True, sample_sequence=1, connection_generation=1,
    ))
    pid.arm(_pid_config())
    pid.start()
    assert [item[0] for item in worker.commands] == ["set_current"]

    assert pid.begin_external_shutdown("Disconnect")
    assert pid._run_state.controller_state == "STOPPING"
    assert pid._start_pending == {}
    actions_path = logger.session.actions_path
    logger.close()

    with actions_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [
        row["phase"] for row in rows if row["action"] == "Start"
    ] == ["REQUESTED", "REJECTED"]


def test_dead_worker_shutdown_enters_prompt_instead_of_finishing(monkeypatch):
    class NoSingleShot:
        @staticmethod
        def singleShot(_delay, _callback):
            pass

    monkeypatch.setattr(main_window_module, "QTimer", NoSingleShot)

    class Logger:
        @staticmethod
        def begin_action(*_args, request_id=None, **_kwargs):
            return request_id

        @staticmethod
        def complete_action(_result):
            pass

    class DeadWorker:
        @staticmethod
        def isRunning():
            return False

    class Timer:
        def start(self, _ms):
            raise AssertionError("dead worker must not start a confirmation timer")

        def stop(self):
            pass

    class Harness:
        def __init__(self):
            self._shutdown_context = None
            self._shutdown_generation = 0
            self._dialog_generation = None
            self._terminal_shutdown_ids = set()
            self._safe_credential_generation = None
            self._latest_psu_generation = 0
            self.action_logger = Logger()
            self.psu_worker = DeadWorker()
            self._shutdown_timer = Timer()
            self.messages = []

        def statusBar(self):
            return type("Bar", (), {"showMessage": self.messages.append})()

        _terminalize_shutdown = MainWindow._terminalize_shutdown

    harness = Harness()
    MainWindow._begin_safe_shutdown(
        harness, "Application Close", source="system", close_after=True,
    )
    assert harness._shutdown_context.phase == "PROMPT"
    assert "state cannot be read back" in harness._shutdown_context.error
    assert harness.psu_worker is not None


def test_offscreen_close_with_dead_worker_is_blocked_and_has_separate_lifecycles(
    tmp_path, monkeypatch,
):
    app = _app()
    monkeypatch.setenv("AI4MBE_HEATER_LOG_ROOT", str(tmp_path))
    window = MainWindow()

    class DeadWorker:
        @staticmethod
        def isRunning():
            return False

    class CloseEvent:
        accepted = False
        ignored = False

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.ignored = True

    prompts = []
    window.psu_worker = DeadWorker()
    window._psu_state_knowledge = "unknown"
    window._show_shutdown_failure = prompts.append
    event = CloseEvent()
    window.closeEvent(event)
    repeated_event = CloseEvent()
    window.closeEvent(repeated_event)
    deadline = time.monotonic() + 1.0
    while not prompts and time.monotonic() < deadline:
        app.processEvents()

    assert event.ignored and not event.accepted
    assert repeated_event.ignored and not repeated_event.accepted
    assert prompts
    context = prompts[0]
    assert context.phase == "PROMPT"
    assert context.parent_action_id
    assert context.parent_action_id != context.request_id
    actions_path = window.action_logger.session.actions_path
    window.action_logger.close()
    with actions_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    requested = [row for row in rows if row["phase"] == "REQUESTED"]
    assert [row["action"] for row in requested[-2:]] == ["Close", "Safe Shutdown"]
    assert requested[-2]["request_id"] != requested[-1]["request_id"]
    window.psu_worker = None
    window._shutdown_context = None
    window.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("invalidate", ["reconnect", "pid_starting"])
def test_old_safe_credential_never_bypasses_close_transaction(
    tmp_path, monkeypatch, invalidate,
):
    app = _app()
    monkeypatch.setenv("AI4MBE_HEATER_LOG_ROOT", str(tmp_path / invalidate))
    window = MainWindow()

    class RunningWorker:
        @staticmethod
        def isRunning():
            return True

    class CloseEvent:
        def accept(self):
            raise AssertionError("close must await a new safety transaction")

        def ignore(self):
            self.ignored = True

    window.psu_worker = RunningWorker()
    window._psu_state_knowledge = "unknown"
    window._safe_credential_generation = 7
    window._latest_psu_generation = 1
    if invalidate == "reconnect":
        window._on_psu_state(PowerSupplyState(
            connected=False,
            valid=False,
            connection_generation=2,
            error="reconnect invalid",
        ))
    else:
        window._on_pid_state_for_safety(type(
            "State", (), {"controller_state": "STARTING"},
        )())
    assert window._safe_credential_generation is None

    requests = []
    window._begin_safe_shutdown = (
        lambda reason, **kwargs: requests.append((reason, kwargs))
    )
    event = CloseEvent()
    window.closeEvent(event)
    assert event.ignored
    assert requests and requests[-1][1]["close_after"]
    window.psu_worker = None
    window.action_logger.close()
    window.deleteLater()
    app.processEvents()


def test_late_shutdown_result_cannot_authorize_after_timeout():
    context = ShutdownContext(
        generation=4,
        request_id="late",
        reason="close",
        source="system",
        phase="PROMPT",
    )

    class Harness:
        def __init__(self):
            self._shutdown_context = context
            self._terminal_shutdown_ids = {"late"}
            self.resolved = False
            self.late_logged = False
            self.action_logger = type("Log", (), {"complete_action": lambda *_: None})()
            self.psu_tab = type("Tab", (), {"on_command_result": lambda *_: None})()
            self.pid_controller = type("Pid", (), {"on_command_result": lambda *_: None})()
            self._shutdown_timer = type("Timer", (), {"stop": lambda *_: None})()

        def statusBar(self):
            return type("Bar", (), {"showMessage": lambda *_: None})()

        def _resolve_safe_shutdown(self, _context):
            self.resolved = True

        def _log_late_shutdown_result(self, _result):
            self.late_logged = True

    harness = Harness()
    MainWindow._on_psu_command_result(
        harness, _command_result("late", "safe_shutdown")
    )
    assert not harness.resolved
    assert harness.late_logged
    assert harness._shutdown_context.phase == "PROMPT"


def test_timeout_has_one_terminal_and_late_result_is_independent_event(
    tmp_path, monkeypatch,
):
    app = _app()
    monkeypatch.setenv("AI4MBE_HEATER_LOG_ROOT", str(tmp_path))
    window = MainWindow()

    class SilentWorker:
        @staticmethod
        def isRunning():
            return True

        @staticmethod
        def request_safe_shutdown(**_kwargs):
            return "queued"

    window.psu_worker = SilentWorker()
    window._show_shutdown_failure = lambda _context: None
    window._begin_safe_shutdown("timeout test", source="system")
    context = window._shutdown_context
    window._on_shutdown_timeout()
    assert context.terminal_status == "TIMED_OUT"
    window._on_psu_command_result(
        _command_result(context.request_id, "safe_shutdown")
    )
    actions_path = window.action_logger.session.actions_path
    window.action_logger.close()

    with actions_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    same_request = [row for row in rows if row["request_id"] == context.request_id]
    assert [row["phase"] for row in same_request] == ["REQUESTED", "TIMED_OUT"]
    late = [row for row in rows if row["action"].endswith("Late Safe Shutdown Result")]
    assert len(late) == 1
    assert late[0]["phase"] == "EVENT"
    assert late[0]["request_id"] != context.request_id
    window.psu_worker = None
    window._shutdown_context = None
    window.deleteLater()
    app.processEvents()


def test_manual_confirmation_audit_failure_does_not_authorize():
    context = ShutdownContext(
        generation=1,
        request_id="manual",
        reason="close",
        source="system",
    )
    harness = type(
        "Harness",
        (),
        {
            "action_logger": type(
                "Log",
                (),
                {"immediate_action": lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full"))},
            )(),
        },
    )()
    recorded, error = MainWindow._record_manual_confirmation(harness, context)
    assert not recorded
    assert "disk full" in error


def test_output_button_rolls_back_until_confirmed():
    _app()
    panel = ControlPanel()
    panel.output_btn.setChecked(True)
    panel._on_output_toggle()
    assert not panel.output_btn.isChecked()
    assert not panel.output_btn.isEnabled()
    panel.complete_output_request(False, True)
    assert not panel.output_btn.isChecked()
    assert panel.output_btn.isEnabled()
    panel.complete_output_request(True, True)
    assert panel.output_btn.isChecked()


def test_output_secondary_failure_or_old_age_displays_unknown():
    _app()
    dashboard = DashboardTab()
    base = PowerSupplyState(
        connected=True,
        valid=True,
        sample_sequence=1,
        voltage_measured=1.0,
        current_measured=0.1,
        power_measured=0.1,
        output_enabled=True,
        output_sample_sequence=1,
        output_valid=False,
        output_age_ms=10.0,
    )
    dashboard.update_psu_state(base)
    assert "UNKNOWN" in dashboard.output_badge.text()
    dashboard.update_psu_state(replace(
        base, output_valid=True, output_age_ms=2_001.0,
    ))
    assert "STALE" in dashboard.output_badge.text()


def test_valid_output_on_then_primary_invalid_clears_both_ui_claims(tmp_path):
    _app()
    logger = ActionLogger(log_root=tmp_path)
    psu_tab = PowerSupplyTab(logger)
    dashboard = DashboardTab()
    valid_on = PowerSupplyState(
        connected=True,
        valid=True,
        sample_sequence=1,
        connection_generation=1,
        received_monotonic_ns=100,
        voltage_measured=1.0,
        current_measured=0.1,
        power_measured=0.1,
        output_enabled=True,
        output_sample_sequence=1,
        output_valid=True,
        output_age_ms=0.0,
    )
    psu_tab.update_state(valid_on)
    dashboard.update_psu_state(valid_on)
    assert psu_tab.control_panel.output_btn.isChecked()
    assert dashboard.output_badge.text() == "OUTPUT ON"

    invalid = replace(valid_on, valid=False, error="primary poll failed")
    psu_tab.update_state(invalid)
    dashboard.update_psu_state(invalid)
    assert not psu_tab.control_panel.output_btn.isChecked()
    assert not psu_tab.control_panel.output_btn.isEnabled()
    assert "UNKNOWN" in psu_tab.control_panel.output_btn.text()
    assert "UNKNOWN" in dashboard.output_badge.text()


def test_action_log_widget_has_physical_row_cap(tmp_path, monkeypatch):
    _app()
    monkeypatch.setattr(action_log_tab_module, "MAX_ENTRIES", 3)
    logger = ActionLogger(log_root=tmp_path)
    tab = ActionLogTab(logger)
    for index in range(5):
        tab._on_entry_added(ActionLogEntry(action=f"event-{index}"))
    assert tab.table.rowCount() == 3
    assert tab.table.item(0, 2).text() == "event-2"


def test_legacy_false_callback_is_rejected_and_safety_deadline_is_total(tmp_path):
    fake = FakeOWON()
    controller = HeaterController(fake, temp_sensor=DummyTemperatureSensor())

    rejected_session = HeaterSessionLogger(tmp_path / "rejected")
    rejected_session.ensure_started()
    rejected_runtime = LegacyHeaterRuntime(controller, rejected_session)
    with pytest.raises(RuntimeError, match="callback rejected"):
        rejected_runtime.execute("Output On", lambda: False)
    rejected_path = rejected_session.actions_path
    rejected_session.close()
    with rejected_path.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle))[-1]["phase"] == "REJECTED"

    clock_values = iter((0, 16_000_000_001))
    deadline_session = HeaterSessionLogger(tmp_path / "deadline")
    deadline_session.ensure_started()
    deadline_runtime = LegacyHeaterRuntime(
        controller,
        deadline_session,
        monotonic_ns=lambda: next(clock_values),
    )
    with pytest.raises(TimeoutError, match="exceeded 15 seconds"):
        deadline_runtime.execute(
            "Safe Shutdown", controller.safe_shutdown, safety=True,
        )
    assert not any(
        name in {"output_off", "set_voltage", "set_current"}
        for name, _value in fake.calls
    )
    assert not deadline_runtime._safety_pending.is_set()
    deadline_session.close()


def test_legacy_step_expiry_does_not_start_the_next_safety_scpi():
    fake = FakeOWON()
    controller = HeaterController(fake, temp_sensor=DummyTemperatureSensor())
    clock_values = iter((0, 15_000_000_001))

    with pytest.raises(TimeoutError, match="exceeded 15 seconds"):
        controller.safe_shutdown(
            deadline_ns=15_000_000_000,
            monotonic_ns=lambda: next(clock_values),
        )

    assert fake.calls == [("output_off", None)]


def test_legacy_cli_initial_safety_precedes_polling_and_disconnects_after_stop(
    tmp_path, monkeypatch,
):
    fake = FakeOWON()

    class TrackingRuntime(LegacyHeaterRuntime):
        def start(self):
            fake.calls.append(("runtime_start", None))
            super().start()

    monkeypatch.setattr(
        heater_control_module, "OWONPowerSupply", lambda _resource: fake,
    )
    monkeypatch.setattr(heater_control_module, "LegacyHeaterRuntime", TrackingRuntime)
    monkeypatch.setattr(heater_control_module, "DRACAL_AVAILABLE", False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    monkeypatch.setenv("AI4MBE_HEATER_LOG_ROOT", str(tmp_path))

    heater_control_module.manual_control_session("FAKE")

    names = [item[0] for item in fake.calls]
    assert names.index("output_off") < names.index("runtime_start")
    assert names[-1] == "disconnect"


def test_legacy_cli_stop_deadline_fails_without_concurrent_disconnect(
    tmp_path, monkeypatch,
):
    fake = FakeOWON()
    stop_timeouts = []

    class RefusesStopRuntime(LegacyHeaterRuntime):
        def start(self):
            fake.calls.append(("runtime_start", None))

        def stop(self, timeout_s=3.5):
            stop_timeouts.append(timeout_s)
            return False

    monkeypatch.setattr(
        heater_control_module, "OWONPowerSupply", lambda _resource: fake,
    )
    monkeypatch.setattr(
        heater_control_module, "LegacyHeaterRuntime", RefusesStopRuntime,
    )
    monkeypatch.setattr(heater_control_module, "DRACAL_AVAILABLE", False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    monkeypatch.setenv("AI4MBE_HEATER_LOG_ROOT", str(tmp_path))

    with pytest.raises(RuntimeError, match="disconnect was blocked"):
        heater_control_module.manual_control_session("FAKE")

    assert stop_timeouts == [15.0]
    assert not any(item[0] == "disconnect" for item in fake.calls)
    actions_path = next(tmp_path.glob("heater_*/heater_actions.csv"))
    with actions_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert any(
        row["action"] == "Stop Telemetry Thread" and row["phase"] == "FAILED"
        for row in rows
    )


def test_legacy_stop_reports_thread_that_still_owns_io(tmp_path):
    class StillAlive:
        def join(self, timeout=None):
            self.timeout = timeout

        @staticmethod
        def is_alive():
            return True

    runtime = LegacyHeaterRuntime(
        HeaterController(FakeOWON(), temp_sensor=DummyTemperatureSensor()),
        HeaterSessionLogger(tmp_path),
    )
    runtime._thread = StillAlive()
    assert not runtime.stop(timeout_s=0.01)
