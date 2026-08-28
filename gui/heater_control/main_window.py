"""
Main window — orchestrates tabs, workers, and signal fan-out.
"""

import time
import uuid
import json
from dataclasses import dataclass, replace
from typing import Optional

from PyQt6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QTabWidget, QMessageBox
from PyQt6.QtCore import QTimer, pyqtSlot

from owon_power_supply import find_owon_supplies

from gui.state import PowerSupplyState, TemperatureState, CameraState, PyrometerState
from gui.workers import (
    PowerSupplyWorker, ThermocoupleWorker,
    RheedCameraWorker, PyrometerWorker,
)
from gui.heater_control.action_logger import ActionLogger
from gui.heater_control.power_supply_tab import PowerSupplyTab
from gui.heater_control.temperature_tab import TemperatureTab
from gui.heater_control.dashboard_tab import DashboardTab
from gui.heater_control.visuals_tab import VisualsTab
from gui.heater_control.config_tab import ConfigTab
from gui.heater_control.pid_controller import PIDController
from gui.heater_control.pid_tab import PIDTab
from gui.heater_control.action_log_tab import ActionLogTab
from gui.heater_control.rheed_tab import RheedTab
from gui.heater_control.pyrometer_tab import PyrometerTab
from gui.heater_control.heater_commands import PowerSupplyCommandResult
from gui.heater_control.heater_session import utc_now_iso


@dataclass
class ShutdownContext:
    generation: int
    request_id: str
    reason: str
    source: str
    disconnect_after: bool = False
    close_after: bool = False
    pid_terminal_state: Optional[str] = None
    parent_action_id: Optional[str] = None
    phase: str = "PENDING"
    error: str = ""
    terminal_status: Optional[str] = None
    audit_requested: bool = False


class MainWindow(QMainWindow):
    """Main application window — orchestrator role."""

    def __init__(self, resource: Optional[str] = None):
        super().__init__()

        self.setWindowTitle("Hardware Control Dashboard")
        self.setMinimumSize(900, 650)

        self.resource = resource
        self.psu_worker: Optional[PowerSupplyWorker] = None
        self.thermo_worker: Optional[ThermocoupleWorker] = None
        self.camera_worker: Optional[RheedCameraWorker] = None
        self.pyrometer_worker: Optional[PyrometerWorker] = None
        self._connect_request_id: Optional[str] = None
        self._shutdown_context: Optional[ShutdownContext] = None
        self._shutdown_generation = 0
        self._dialog_generation: Optional[int] = None
        self._terminal_shutdown_ids: set[str] = set()
        self._safe_credential_generation: Optional[int] = None
        self._pending_close = False
        self._close_authorized = False
        self._psu_state_knowledge = "never_connected"
        self._latest_psu_state: Optional[PowerSupplyState] = None
        self._latest_psu_generation = 0
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setSingleShot(True)
        self._shutdown_timer.timeout.connect(self._on_shutdown_timeout)

        # Central logging service
        self.action_logger = ActionLogger(self)

        # Build UI
        self._setup_ui()
        self._connect_tab_signals()

        self.action_logger.log("System", "Application Started", "")

        # Auto-connect if resource provided
        if self.resource:
            self._connect_to_psu()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        self.tabs = QTabWidget()

        self.psu_tab = PowerSupplyTab(self.action_logger)
        self.temp_tab = TemperatureTab(self.action_logger)
        self.rheed_tab = RheedTab(self.action_logger)
        self.pyrometer_tab = PyrometerTab(self.action_logger)
        self.dashboard_tab = DashboardTab()
        self.visuals_tab = VisualsTab()
        self.config_tab = ConfigTab(self.action_logger)
        self.pid_controller = PIDController(self.action_logger)
        self.pid_tab = PIDTab(self.pid_controller, self.config_tab)
        self.log_tab = ActionLogTab(self.action_logger)

        self.tabs.addTab(self.rheed_tab, "RHEED")
        self.tabs.addTab(self.pyrometer_tab, "Pyrometer")
        self.tabs.addTab(self.psu_tab, "Power Supply")
        self.tabs.addTab(self.temp_tab, "Thermocouple")
        self.tabs.addTab(self.dashboard_tab, "Dashboard")
        self.tabs.addTab(self.visuals_tab, "Visuals")
        self.tabs.addTab(self.config_tab, "Config")
        self.tabs.addTab(self.pid_tab, "PID")
        self.tabs.addTab(self.log_tab, "Action Log")

        main_layout.addWidget(self.tabs)
        self.statusBar().showMessage("Ready")

    def _connect_tab_signals(self):
        # Power Supply tab signals
        self.psu_tab.connect_requested.connect(self._connect_to_psu)
        self.psu_tab.disconnect_requested.connect(self._disconnect_from_psu)
        self.psu_tab.command_requested.connect(self._on_psu_command)

        # Temperature tab signals
        self.temp_tab.connect_requested.connect(self._connect_thermocouple)
        self.temp_tab.disconnect_requested.connect(self._disconnect_thermocouple)

        # PID tab emergency stop → same path as PSU E-Stop button
        self.pid_tab.emergency_stop_requested.connect(self._on_pid_emergency_stop)
        self.pid_controller.safety_shutdown_requested.connect(
            self._on_pid_safety_requested
        )
        self.pid_controller.pid_state_updated.connect(
            self._on_pid_state_for_safety
        )

        # RHEED camera tab signals
        self.rheed_tab.connect_requested.connect(self._connect_camera)
        self.rheed_tab.disconnect_requested.connect(self._disconnect_camera)

        # Pyrometer tab signals
        self.pyrometer_tab.connect_requested.connect(self._connect_pyrometer)
        self.pyrometer_tab.disconnect_requested.connect(self._disconnect_pyrometer)

    # --- PSU worker lifecycle ---

    @pyqtSlot()
    def _connect_to_psu(self):
        if self.psu_worker:
            if self.psu_worker.isRunning():
                return
            if self._psu_state_knowledge not in (
                "never_connected", "disconnected_confirmed",
            ):
                self.statusBar().showMessage(
                    "Reconnect blocked: the stopped worker left heater state unknown"
                )
                return
            self.psu_worker = None

        try:
            self.action_logger.ensure_session({
                "application": "heater_control_dashboard",
                "resource_requested": self.resource,
                "initial_config": {
                    "psu_poll_interval_s": self.config_tab.psu_poll_interval,
                    "thermocouple_poll_interval_s": self.config_tab.tc_poll_interval,
                    "pid_hard_cutoff_c": self.config_tab.hard_cutoff_c,
                },
            })
            self._connect_request_id = self.action_logger.begin_action(
                "Power Supply",
                "Connect",
                source="manual_ui",
                requested={"resource": self.resource},
            )
            self._psu_state_knowledge = "unknown"
            self._safe_credential_generation = None
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Audit Log Unavailable",
                "Connection was refused because its REQUESTED audit row could "
                f"not be written:\n{exc}",
            )
            return

        if not self.resource:
            try:
                supplies = find_owon_supplies()
            except Exception:
                supplies = []
            if not supplies:
                self.action_logger.fail_action(
                    self._connect_request_id, "No OWON power supply detected",
                )
                self._connect_request_id = None
                self._psu_state_knowledge = "never_connected"
                QMessageBox.warning(self, "Not Found", "No OWON power supply detected.")
                return
            self.resource, idn = supplies[0]
            self.statusBar().showMessage(f"Found: {idn}")

        try:
            self.psu_worker = PowerSupplyWorker(
                self.resource, poll_interval=self.config_tab.psu_poll_interval
            )
        except Exception as exc:
            self.action_logger.fail_action(self._connect_request_id, str(exc))
            self._connect_request_id = None
            self._psu_state_knowledge = "never_connected"
            QMessageBox.critical(self, "Connection Error", str(exc))
            return
        self.psu_worker.state_updated.connect(self._on_psu_state)
        self.psu_worker.command_completed.connect(self._on_psu_command_result)
        self.psu_worker.system_event.connect(self._on_psu_system_event)
        self.psu_worker.start()
        self.psu_tab.status_label.setText("Connecting...")
        self.pid_controller.set_psu_worker(self.psu_worker)

    @pyqtSlot()
    def _disconnect_from_psu(self):
        current = self._shutdown_context
        if current and current.phase in ("PENDING", "PROMPT") and (
            current.disconnect_after or current.close_after
        ):
            self.statusBar().showMessage(
                "Disconnect is already awaiting safe-shutdown confirmation"
            )
            return
        try:
            disconnect_id = self.action_logger.begin_action(
                "Power Supply",
                "Disconnect",
                source="manual_ui",
                requested={"resource": self.resource},
                safety=True,
            )
        except Exception as exc:
            self.statusBar().showMessage(
                f"Disconnect rejected: audit REQUESTED write failed: {exc}"
            )
            return
        pid_action = self.pid_controller.begin_external_shutdown("Disconnect")
        pid_terminal = "STOPPED" if pid_action else None
        if (
            self.psu_worker is None
            and self._psu_state_knowledge in (
                "never_connected", "disconnected_confirmed",
            )
        ):
            self._complete_disconnect(
                disconnect_id,
                close_after=False,
                pid_terminal_state=pid_terminal,
            )
            return
        self._begin_safe_shutdown(
            "Disconnect",
            source="manual_ui",
            disconnect_after=True,
            pid_terminal_state=pid_terminal,
            parent_action_id=disconnect_id,
        )

    @pyqtSlot(str, tuple)
    def _on_psu_command(self, cmd: str, args: tuple):
        self._safe_credential_generation = None
        safety = cmd == "emergency_stop"
        action_names = {
            "set_voltage": "Set Voltage",
            "set_current": "Set Current",
            "output_on": "Output On",
            "output_off": "Output Off",
            "set_ovp": "Set OVP",
            "set_ocp": "Set OCP",
            "emergency_stop": "Emergency Stop",
        }
        if safety:
            pid_action = self.pid_controller.begin_external_shutdown(
                "Manual PSU Emergency Stop"
            )
            self._begin_safe_shutdown(
                "Manual PSU Emergency Stop",
                source="manual_ui",
                pid_terminal_state="STOPPED" if pid_action else None,
            )
            self.statusBar().showMessage(
                "Emergency stop requested; awaiting OFF / 0 V / 0 A readback"
            )
            return
        request_id = str(uuid.uuid4())
        logged = False
        try:
            self.action_logger.begin_action(
                "Power Supply",
                action_names.get(cmd, cmd),
                source="manual_ui",
                requested={"args": list(args)},
                request_id=request_id,
                safety=False,
            )
            logged = True
        except Exception as exc:
            self.statusBar().showMessage(
                f"Command rejected: REQUESTED audit write failed: {exc}"
            )
            return

        if not self.psu_worker or not self.psu_worker.isRunning():
            if logged:
                self._on_psu_command_result(PowerSupplyCommandResult(
                    request_id=request_id,
                    source="manual_ui",
                    command=cmd,
                    status="REJECTED",
                    requested={"args": list(args)},
                    effective={},
                    readback={},
                    error="power supply worker is not running",
                    completed_at_utc=utc_now_iso(),
                    completed_monotonic_ns=time.perf_counter_ns(),
                ))
            return
        self._psu_state_knowledge = "unknown"
        self.psu_worker.queue_command(
            cmd, *args, request_id=request_id, source="manual_ui",
        )
        self.statusBar().showMessage(f"{action_names.get(cmd, cmd)} requested")

    @pyqtSlot()
    def _on_pid_emergency_stop(self):
        self.statusBar().showMessage(
            "PID emergency stop requested; awaiting hardware confirmation"
        )

    @pyqtSlot(str, str)
    def _on_pid_safety_requested(self, reason: str, terminal_state: str):
        self._begin_safe_shutdown(
            reason,
            source="pid",
            pid_terminal_state=terminal_state,
        )

    @pyqtSlot(object)
    def _on_pid_state_for_safety(self, state) -> None:
        if state.controller_state in ("STARTING", "RUNNING"):
            self._safe_credential_generation = None

    # --- Thermocouple worker lifecycle ---

    @pyqtSlot()
    def _connect_thermocouple(self):
        if self.thermo_worker and self.thermo_worker.isRunning():
            return

        self.thermo_worker = ThermocoupleWorker(
            poll_interval=self.config_tab.tc_poll_interval
        )
        self.thermo_worker.state_updated.connect(self._on_temp_state)
        self.thermo_worker.start()
        self.temp_tab.status_label.setText("Connecting...")

    @pyqtSlot()
    def _disconnect_thermocouple(self):
        if self.thermo_worker:
            self.thermo_worker.stop()
            self.thermo_worker.wait(3500)
            self.thermo_worker = None

        self.temp_tab.on_disconnected()

    # --- RHEED camera worker lifecycle ---

    @pyqtSlot(str)
    def _connect_camera(self, mode: str):
        if self.camera_worker and self.camera_worker.isRunning():
            return

        self.camera_worker = RheedCameraWorker(mode=mode, poll_interval=1.0)
        self.camera_worker.state_updated.connect(self._on_camera_state)
        self.camera_worker.start()
        self.rheed_tab.status_label.setText("Connecting...")

    @pyqtSlot()
    def _disconnect_camera(self):
        if self.camera_worker:
            self.camera_worker.stop()
            self.camera_worker.wait(3500)
            self.camera_worker = None

        self.rheed_tab.on_disconnected()

    # --- Pyrometer worker lifecycle ---

    @pyqtSlot(str)
    def _connect_pyrometer(self, mode: str):
        if self.pyrometer_worker and self.pyrometer_worker.isRunning():
            return

        self.pyrometer_worker = PyrometerWorker(mode=mode, poll_interval=0.5)
        self.pyrometer_worker.state_updated.connect(self._on_pyrometer_state)
        self.pyrometer_worker.start()
        self.pyrometer_tab.status_label.setText("Connecting...")

    @pyqtSlot()
    def _disconnect_pyrometer(self):
        if self.pyrometer_worker:
            self.pyrometer_worker.stop()
            self.pyrometer_worker.wait(3500)
            self.pyrometer_worker = None

        self.pyrometer_tab.on_disconnected()

    # --- Signal fan-out ---

    @pyqtSlot(PowerSupplyState)
    def _on_psu_state(self, state: PowerSupplyState):
        """Fan out PSU state to all consumers."""
        state = replace(state, gui_received_monotonic_ns=time.perf_counter_ns())
        self._latest_psu_state = state
        if (
            not state.has_valid_reading
            or (
                self._latest_psu_generation
                and state.connection_generation != self._latest_psu_generation
            )
        ):
            self._safe_credential_generation = None
        if state.has_valid_reading:
            if (
                self._latest_psu_generation
                and state.connection_generation != self._latest_psu_generation
            ):
                self._psu_state_knowledge = "unknown"
            self._latest_psu_generation = state.connection_generation
            if (
                state.output_sample_sequence > 0
                and (
                    state.output_enabled
                    or abs(state.voltage_setpoint) > 0.005
                    or abs(state.current_setpoint) > 0.005
                )
            ):
                self._psu_state_knowledge = "unknown"
        elif (
            not state.connected
            and state.connection_generation == 0
            and self._connect_request_id
        ):
            self._psu_state_knowledge = "never_connected"
        self.action_logger.update_psu_state(state)
        if state.has_valid_reading:
            try:
                self.action_logger.record_telemetry(state)
            except Exception as exc:
                # Telemetry failure is visible, but must not prevent E-stop.
                self.statusBar().showMessage(f"Heater telemetry write failed: {exc}")
        self.psu_tab.update_state(state)
        self.dashboard_tab.update_psu_state(state)
        self.visuals_tab.update_psu_state(state)
        self.pid_controller.on_psu_state(state)

        if self._connect_request_id:
            if state.has_valid_reading:
                request_id = self._connect_request_id
                self._connect_request_id = None
                self.action_logger.complete_action(PowerSupplyCommandResult(
                    request_id=request_id,
                    source="manual_ui",
                    command="Connect",
                    status="CONFIRMED",
                    requested={"resource": self.resource},
                    effective={"resource": self.resource},
                    readback={
                        "connected": True,
                        "connection_generation": state.connection_generation,
                        "sample_sequence": state.sample_sequence,
                    },
                    error="",
                    completed_at_utc=state.received_at_utc or utc_now_iso(),
                    completed_monotonic_ns=(
                        state.received_monotonic_ns or time.perf_counter_ns()
                    ),
                    confirmation_sample_sequence=state.sample_sequence,
                ))
            elif not state.connected and state.error:
                request_id = self._connect_request_id
                self._connect_request_id = None
                self.action_logger.fail_action(request_id, state.error)

    @pyqtSlot(object)
    def _on_psu_command_result(self, result: PowerSupplyCommandResult):
        context = self._shutdown_context
        is_active_shutdown = (
            context is not None and result.request_id == context.request_id
        )
        if is_active_shutdown or result.request_id in self._terminal_shutdown_ids:
            self.psu_tab.on_command_result(result)
            if (
                not is_active_shutdown
                or context.phase != "PENDING"
                or context.terminal_status is not None
            ):
                self._log_late_shutdown_result(result)
                return

            self._shutdown_timer.stop()
            self._terminalize_shutdown(
                context,
                result.status,
                error=result.error,
                result=result,
            )
            if result.confirmed:
                self.statusBar().showMessage(
                    "safe_shutdown confirmed by hardware"
                )
                self._resolve_safe_shutdown(context)
            else:
                self.statusBar().showMessage(
                    f"safe_shutdown {result.status}: "
                    f"{result.error or 'not confirmed'}"
                )
                context.phase = "PROMPT"
                context.error = result.error or "readback was not safe"
                # A stopped-worker queue can reject synchronously.  Deferring
                # the modal dialog prevents coordinator re-entry.
                QTimer.singleShot(
                    0,
                    lambda generation=context.generation:
                        self._show_context_generation(generation),
                )
            return

        try:
            self.action_logger.complete_action(result)
        except Exception as exc:
            self.statusBar().showMessage(f"Heater action audit write failed: {exc}")
        self.psu_tab.on_command_result(result)
        self.pid_controller.on_command_result(result)
        if result.confirmed:
            self.statusBar().showMessage(f"{result.command} confirmed by hardware")
        else:
            self.statusBar().showMessage(
                f"{result.command} {result.status}: {result.error or 'not confirmed'}"
            )


    @pyqtSlot(object)
    def _on_psu_system_event(self, event: dict):
        try:
            self.action_logger.log(
                "Power Supply Worker",
                event.get("action", "system_event"),
                json.dumps(event, ensure_ascii=False, sort_keys=True),
            )
        except Exception as exc:
            self.statusBar().showMessage(f"Worker event audit failed: {exc}")

    @pyqtSlot(TemperatureState)
    def _on_temp_state(self, state: TemperatureState):
        """Fan out temperature state to all consumers."""
        self.temp_tab.update_state(state)
        self.dashboard_tab.update_temp_state(state)
        self.visuals_tab.update_temp_state(state)
        self.action_logger.update_temp_state(state)
        self.pid_controller.on_temp_state(state)

    @pyqtSlot(CameraState)
    def _on_camera_state(self, state: CameraState):
        """Fan out RHEED camera state to consumers."""
        self.rheed_tab.update_state(state)

    @pyqtSlot(PyrometerState)
    def _on_pyrometer_state(self, state: PyrometerState):
        """Fan out pyrometer state to consumers."""
        self.pyrometer_tab.update_state(state)

    # --- Shutdown ---

    def _terminalize_shutdown(
        self,
        context: ShutdownContext,
        status: str,
        *,
        error: str = "",
        result: Optional[PowerSupplyCommandResult] = None,
        readback: Optional[dict] = None,
    ) -> bool:
        """Write at most one terminal row for a coordinator REQUESTED row."""
        if context.terminal_status is not None:
            return False
        terminal = result or PowerSupplyCommandResult(
            request_id=context.request_id,
            source=context.source,
            command="safe_shutdown",
            status=status,
            requested={"reason": context.reason},
            effective=(
                {
                    "output_enabled": False,
                    "voltage_setpoint": 0.0,
                    "current_setpoint": 0.0,
                }
                if status == "CONFIRMED" else {}
            ),
            readback=readback or {},
            error=error,
            completed_at_utc=utc_now_iso(),
            completed_monotonic_ns=time.perf_counter_ns(),
            connection_generation=self._latest_psu_generation,
        )
        if context.audit_requested:
            try:
                self.action_logger.complete_action(terminal)
            except Exception as exc:
                self.statusBar().showMessage(
                    f"Safe-shutdown terminal audit write failed: {exc}"
                )
                return False
        context.terminal_status = status
        self._terminal_shutdown_ids.add(context.request_id)
        return True

    def _log_late_shutdown_result(self, result: PowerSupplyCommandResult) -> None:
        """Persist a late hardware result as an independent EVENT."""
        try:
            self.action_logger.log(
                "Power Supply",
                "Late Safe Shutdown Result",
                json.dumps({
                    "original_request_id": result.request_id,
                    "status": result.status,
                    "readback": result.readback,
                    "error": result.error,
                    "completed_at_utc": result.completed_at_utc,
                }, ensure_ascii=False, sort_keys=True),
            )
        except Exception as exc:
            self.statusBar().showMessage(f"Late safety result audit failed: {exc}")
        self._terminal_shutdown_ids.discard(result.request_id)

    def _begin_safe_shutdown(
        self,
        reason: str,
        *,
        source: str,
        disconnect_after: bool = False,
        close_after: bool = False,
        pid_terminal_state: Optional[str] = None,
        parent_action_id: Optional[str] = None,
    ) -> None:
        current = self._shutdown_context
        if current and current.phase in ("PENDING", "PROMPT"):
            current.disconnect_after = current.disconnect_after or disconnect_after
            current.close_after = current.close_after or close_after
            if current.parent_action_id is None:
                current.parent_action_id = parent_action_id
            elif (
                parent_action_id is not None
                and parent_action_id != current.parent_action_id
            ):
                # A repeated UI request must not leave a second lifecycle in
                # REQUESTED forever while it joins the already-active physical
                # transaction.
                try:
                    self.action_logger.fail_action(
                        parent_action_id,
                        "joined an already pending safe shutdown",
                        status="REJECTED",
                    )
                except Exception:
                    pass
            if pid_terminal_state == "FAULT" or current.pid_terminal_state is None:
                current.pid_terminal_state = pid_terminal_state
            return

        self._shutdown_generation += 1
        request_id = str(uuid.uuid4())
        context = ShutdownContext(
            generation=self._shutdown_generation,
            request_id=request_id,
            reason=reason,
            source=source,
            disconnect_after=disconnect_after,
            close_after=close_after,
            pid_terminal_state=pid_terminal_state,
            parent_action_id=parent_action_id,
        )
        self._shutdown_context = context
        self._safe_credential_generation = None
        try:
            self.action_logger.begin_action(
                "Power Supply",
                "Safe Shutdown",
                source=source,
                requested={"reason": reason},
                request_id=request_id,
                safety=True,
            )
            context.audit_requested = True
        except Exception as exc:
            # Physical safety remains authorized. Only a manual override below
            # is forbidden when its own durable audit cannot be written.
            self.statusBar().showMessage(
                f"Audit failed; safety shutdown still requested: {exc}"
            )

        if not self.psu_worker or not self.psu_worker.isRunning():
            context.phase = "PROMPT"
            context.error = (
                "Power-supply worker is stopped; heater state cannot be read back"
            )
            self._terminalize_shutdown(
                context, "FAILED", error=context.error,
            )
            QTimer.singleShot(
                0,
                lambda generation=context.generation: self._show_context_generation(
                    generation
                ),
            )
            return

        self._shutdown_timer.start(15_000)
        self.statusBar().showMessage(
            "Safety shutdown requested; awaiting OFF / 0 V / 0 A readback"
        )
        try:
            self.psu_worker.request_safe_shutdown(
                request_id=request_id, source=source,
            )
        except Exception as exc:
            self._shutdown_timer.stop()
            context.phase = "PROMPT"
            context.error = f"could not queue safe shutdown: {exc}"
            self._terminalize_shutdown(
                context, "FAILED", error=context.error,
            )
            QTimer.singleShot(
                0,
                lambda generation=context.generation:
                    self._show_context_generation(generation),
            )

    def _on_shutdown_timeout(self) -> None:
        context = self._shutdown_context
        if context and context.phase == "PENDING":
            context.phase = "PROMPT"
            context.error = "15 second safety transaction deadline expired"
            self._terminalize_shutdown(
                context, "TIMED_OUT", error=context.error,
            )
            self._show_shutdown_failure(context)

    def _show_context_generation(self, generation: int) -> None:
        context = self._shutdown_context
        if context and context.generation == generation and context.phase == "PROMPT":
            self._show_shutdown_failure(context)

    def _show_shutdown_failure(self, context: ShutdownContext) -> None:
        if (
            self._shutdown_context is not context
            or self._dialog_generation == context.generation
        ):
            return
        self._dialog_generation = context.generation
        try:
            while (
                self._shutdown_context is context
                and context.phase == "PROMPT"
            ):
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Critical)
                box.setWindowTitle("Safe shutdown not confirmed")
                box.setText(
                    "The dashboard cannot complete this operation without "
                    "confirmed OUTP OFF, VSET=0, and ISET=0."
                )
                box.setInformativeText(context.error)
                retry = box.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
                cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
                manual = box.addButton(
                    "I confirmed OFF / 0 V / 0 A on the instrument",
                    QMessageBox.ButtonRole.DestructiveRole,
                )
                box.exec()
                clicked = box.clickedButton()

                if clicked is retry:
                    if (
                        self._safe_credential_generation == context.generation
                        and (context.disconnect_after or context.close_after)
                    ):
                        self._complete_disconnect(
                            context.parent_action_id,
                            close_after=context.close_after,
                            pid_terminal_state=context.pid_terminal_state,
                        )
                        return
                    self._terminalize_shutdown(
                        context,
                        "REJECTED",
                        error="safe shutdown attempt retired for retry",
                    )
                    context.phase = "RETIRED"
                    self._shutdown_context = None
                    self._begin_safe_shutdown(
                        context.reason,
                        source=context.source,
                        disconnect_after=context.disconnect_after,
                        close_after=context.close_after,
                        pid_terminal_state=context.pid_terminal_state,
                        parent_action_id=context.parent_action_id,
                    )
                    return

                if clicked is manual:
                    recorded, error = self._record_manual_confirmation(context)
                    if not recorded:
                        QMessageBox.critical(
                            self,
                            "Manual confirmation not recorded",
                            "The manual safety credential was not durably "
                            f"written, so exit remains blocked:\n{error}",
                        )
                        continue
                    self._terminalize_shutdown(
                        context,
                        "CONFIRMED",
                        readback={
                            "output_enabled": False,
                            "voltage_setpoint": 0.0,
                            "current_setpoint": 0.0,
                            "confirmation_source": "manual_front_panel",
                        },
                    )
                    self._resolve_safe_shutdown(context, manual=True)
                    return

                if clicked is cancel:
                    self._cancel_safe_shutdown(context)
                    return
        finally:
            if self._dialog_generation == context.generation:
                self._dialog_generation = None

    def _record_manual_confirmation(
        self, context: ShutdownContext,
    ) -> tuple[bool, str]:
        try:
            self.action_logger.immediate_action(
                "Power Supply",
                "Manual Front-Panel Safety Confirmation",
                source="manual_ui",
                requested={
                    "asserted_output_off": True,
                    "asserted_voltage_setpoint_v": 0.0,
                    "asserted_current_setpoint_a": 0.0,
                    "reason": context.reason,
                    "shutdown_generation": context.generation,
                },
                safety=True,
            )
        except Exception as exc:
            return False, str(exc)
        return True, ""

    def _resolve_safe_shutdown(
        self, context: ShutdownContext, *, manual: bool = False,
    ) -> None:
        if self._shutdown_context is not context:
            return
        context.phase = "CONFIRMED_MANUAL" if manual else "CONFIRMED"
        self._shutdown_timer.stop()
        self._safe_credential_generation = context.generation
        if context.pid_terminal_state:
            self.pid_controller.on_safety_resolution(
                confirmed=True,
                terminal_state=context.pid_terminal_state,
            )
        if context.disconnect_after or context.close_after:
            self._complete_disconnect(
                context.parent_action_id,
                close_after=context.close_after,
            )
            return
        self._shutdown_context = None
        self.statusBar().showMessage("Safe shutdown confirmed; PSU remains connected")

    def _cancel_safe_shutdown(self, context: ShutdownContext) -> None:
        if self._shutdown_context is not context:
            return
        context.phase = "CANCELLED"
        self._shutdown_timer.stop()
        self._terminalize_shutdown(
            context,
            "REJECTED",
            error=context.error or "safe shutdown cancelled",
        )
        self._safe_credential_generation = None
        if context.parent_action_id:
            try:
                self.action_logger.fail_action(
                    context.parent_action_id,
                    context.error or "safe shutdown cancelled",
                    status="REJECTED",
                )
            except Exception:
                pass
        if context.pid_terminal_state:
            self.pid_controller.on_safety_resolution(
                confirmed=False,
                terminal_state=context.pid_terminal_state,
                error=context.error or "safe shutdown cancelled",
            )
        self._pending_close = False
        self._shutdown_context = None
        self.statusBar().showMessage("Disconnect/close cancelled; heater state unknown")

    def _complete_disconnect(
        self,
        parent_action_id: Optional[str],
        *,
        close_after: bool,
        pid_terminal_state: Optional[str] = None,
    ) -> bool:
        if pid_terminal_state:
            self.pid_controller.on_safety_resolution(
                confirmed=True,
                terminal_state=pid_terminal_state,
            )
        worker = self.psu_worker
        if worker and worker.isRunning():
            worker.stop()
            if not worker.wait(3500):
                context = self._shutdown_context
                if context is None:
                    self._shutdown_generation += 1
                    context = ShutdownContext(
                        generation=self._shutdown_generation,
                        request_id=str(uuid.uuid4()),
                        reason="Stop PSU communication worker",
                        source="system",
                        disconnect_after=True,
                        close_after=close_after,
                        parent_action_id=parent_action_id,
                    )
                    self._shutdown_context = context
                context.phase = "PROMPT"
                context.error = (
                    "Hardware is safe, but the PSU worker did not stop within 3.5 s; "
                    "disconnect is blocked to avoid concurrent I/O"
                )
                QTimer.singleShot(
                    0,
                    lambda generation=context.generation: self._show_context_generation(
                        generation
                    ),
                )
                return False

        self.pid_controller.set_psu_worker(None)
        self.psu_worker = None
        self.resource = None
        self.psu_tab.on_disconnected()
        confirmed_generation = self._safe_credential_generation
        self._psu_state_knowledge = "disconnected_confirmed"
        if parent_action_id:
            try:
                self.action_logger.complete_action(PowerSupplyCommandResult(
                    request_id=parent_action_id,
                    source="manual_ui",
                    command="Disconnect",
                    status="CONFIRMED",
                    requested={},
                    effective={"connected": False},
                    readback={
                        "connected": False,
                        "safe_shutdown_generation": confirmed_generation,
                    },
                    error="",
                    completed_at_utc=utc_now_iso(),
                    completed_monotonic_ns=time.perf_counter_ns(),
                ))
            except Exception as exc:
                self.statusBar().showMessage(f"Disconnect audit completion failed: {exc}")
        self._safe_credential_generation = None
        self._shutdown_context = None
        if close_after or self._pending_close:
            self._finish_close()
        return True

    def _finish_close(self) -> None:
        self._disconnect_thermocouple()
        self._disconnect_camera()
        self._disconnect_pyrometer()
        self._close_authorized = True
        self.close()

    def closeEvent(self, event):
        if self._close_authorized:
            try:
                self.action_logger.log("System", "Application Closing", "")
            finally:
                self.action_logger.close()
            event.accept()
            return
        event.ignore()
        current = self._shutdown_context
        if (
            self._pending_close
            and current is not None
            and current.phase in ("PENDING", "PROMPT")
        ):
            # Window managers may deliver several close events.  Keep a
            # single coordinator generation and a single Close lifecycle.
            if current.phase == "PROMPT":
                QTimer.singleShot(
                    0,
                    lambda generation=current.generation:
                        self._show_context_generation(generation),
                )
            return
        self._pending_close = True
        close_action_id = None
        if (
            self.action_logger.session.started
            or self._psu_state_knowledge != "never_connected"
        ):
            try:
                close_action_id = self.action_logger.begin_action(
                    "System", "Close", source="system", safety=True,
                )
            except Exception as exc:
                self.statusBar().showMessage(f"Close audit request failed: {exc}")
        pid_action = self.pid_controller.begin_external_shutdown("Application Close")
        pid_terminal = "STOPPED" if pid_action else None
        if (
            self.psu_worker is None
            and self._psu_state_knowledge in (
                "never_connected", "disconnected_confirmed",
            )
        ):
            self._complete_disconnect(
                close_action_id,
                close_after=True,
                pid_terminal_state=pid_terminal,
            )
            return
        self._begin_safe_shutdown(
            "Application Close",
            source="system",
            disconnect_after=True,
            close_after=True,
            pid_terminal_state=pid_terminal,
            parent_action_id=close_action_id,
        )
