"""
Power Supply tab widget.
"""

from collections import deque

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QGroupBox, QCheckBox,
)
from PyQt6.QtCore import pyqtSignal

import pyqtgraph as pg

from gui.state import PowerSupplyState
from gui.widgets import ValueDisplay, ControlPanel, ProtectionPanel
from gui.heater_control.action_logger import ActionLogger
from gui.heater_control.heater_commands import PowerSupplyCommandResult


class PowerSupplyTab(QWidget):
    """Power supply control and monitoring tab."""

    connect_requested = pyqtSignal()
    disconnect_requested = pyqtSignal()
    command_requested = pyqtSignal(str, tuple)  # (command_name, args)

    def __init__(self, action_logger: ActionLogger, parent=None):
        super().__init__(parent)
        self.action_logger = action_logger
        self.advanced_mode = True

        # Live plot buffers
        self._psu_generation = None
        self._psu_origin_ns = None
        self.psu_time = deque(maxlen=120)
        self.psu_voltage = deque(maxlen=120)
        self.psu_current = deque(maxlen=120)
        self.psu_power = deque(maxlen=120)

        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Top bar: connection and view toggle
        top_bar = QHBoxLayout()

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        top_bar.addWidget(self.connect_btn)

        self.status_label = QLabel("Disconnected")
        top_bar.addWidget(self.status_label)
        top_bar.addStretch()

        self.view_toggle = QCheckBox("Advanced View")
        self.view_toggle.setChecked(True)
        self.view_toggle.toggled.connect(self._toggle_view)
        top_bar.addWidget(self.view_toggle)

        layout.addLayout(top_bar)

        # Main content area
        content = QHBoxLayout()

        # Left panel: measurements + plot
        left_panel = QVBoxLayout()

        measurements = QGroupBox("Measurements")
        meas_layout = QGridLayout(measurements)
        self.voltage_display = ValueDisplay("Voltage", "V", 3)
        self.current_display = ValueDisplay("Current", "A", 3)
        self.power_display = ValueDisplay("Power", "W", 3)
        meas_layout.addWidget(self.voltage_display, 0, 0)
        meas_layout.addWidget(self.current_display, 0, 1)
        meas_layout.addWidget(self.power_display, 0, 2)
        left_panel.addWidget(measurements)

        self.setpoints_group = QGroupBox("Setpoints")
        setpoints_layout = QGridLayout(self.setpoints_group)
        self.voltage_sp_display = ValueDisplay("V Setpoint", "V", 2)
        self.current_sp_display = ValueDisplay("I Limit", "A", 3)
        setpoints_layout.addWidget(self.voltage_sp_display, 0, 0)
        setpoints_layout.addWidget(self.current_sp_display, 0, 1)
        left_panel.addWidget(self.setpoints_group)

        left_panel.addStretch()
        content.addLayout(left_panel, stretch=2)

        # Right panel: controls
        right_panel = QVBoxLayout()

        self.control_panel = ControlPanel(max_voltage=24.0, max_current=1.0)
        right_panel.addWidget(self.control_panel)

        self.protection_panel = ProtectionPanel()
        right_panel.addWidget(self.protection_panel)

        self.estop_btn = QPushButton("EMERGENCY STOP")
        self.estop_btn.setMinimumHeight(60)
        self.estop_btn.setStyleSheet("""
            QPushButton {
                background-color: #d32f2f;
                color: white;
                font-size: 18px;
                font-weight: bold;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #b71c1c;
            }
            QPushButton:pressed {
                background-color: #ff5252;
            }
        """)
        self.estop_btn.clicked.connect(self._on_emergency_stop)
        right_panel.addWidget(self.estop_btn)

        right_panel.addStretch()
        content.addLayout(right_panel, stretch=1)

        layout.addLayout(content)

        # Live plots: V / I / P stacked with linked X axes
        readings_group = QGroupBox("Live Readings")
        readings_layout = QVBoxLayout(readings_group)
        readings_layout.setContentsMargins(4, 4, 4, 4)
        readings_layout.setSpacing(2)

        self.v_plot = pg.PlotWidget(title="Voltage")
        self.v_plot.setLabel("left", "V", "V")
        self.v_plot.showGrid(x=True, y=True)
        self.v_curve = self.v_plot.plot(pen=pg.mkPen("y", width=2))

        self.i_plot = pg.PlotWidget(title="Current")
        self.i_plot.setLabel("left", "I", "A")
        self.i_plot.showGrid(x=True, y=True)
        self.i_curve = self.i_plot.plot(pen=pg.mkPen("c", width=2))

        self.p_plot = pg.PlotWidget(title="Power")
        self.p_plot.setLabel("left", "P", "W")
        self.p_plot.setLabel("bottom", "Time", "s")
        self.p_plot.showGrid(x=True, y=True)
        self.p_curve = self.p_plot.plot(pen=pg.mkPen("m", width=2))

        # Link X axes
        self.i_plot.setXLink(self.v_plot)
        self.p_plot.setXLink(self.v_plot)

        readings_layout.addWidget(self.v_plot)
        readings_layout.addWidget(self.i_plot)
        readings_layout.addWidget(self.p_plot)
        layout.addWidget(readings_group)

    def _connect_signals(self):
        self.control_panel.voltage_changed.connect(self._on_set_voltage)
        self.control_panel.current_changed.connect(self._on_set_current)
        self.control_panel.output_toggled.connect(self._on_output_toggled)
        self.protection_panel.ovp_changed.connect(self._on_set_ovp)
        self.protection_panel.ocp_changed.connect(self._on_set_ocp)

    # --- Signal handlers that log actions and forward commands ---

    def _on_connect_clicked(self):
        if self.connect_btn.text() == "Connect":
            self.connect_requested.emit()
        else:
            self.disconnect_requested.emit()

    def _on_set_voltage(self, voltage: float):
        self.command_requested.emit("set_voltage", (voltage,))

    def _on_set_current(self, current: float):
        self.command_requested.emit("set_current", (current,))

    def _on_output_toggled(self, enabled: bool):
        cmd = "output_on" if enabled else "output_off"
        self.command_requested.emit(cmd, ())

    def _on_set_ovp(self, voltage: float):
        self.command_requested.emit("set_ovp", (voltage,))

    def _on_set_ocp(self, current: float):
        self.command_requested.emit("set_ocp", (current,))

    def _on_emergency_stop(self):
        self.command_requested.emit("emergency_stop", ())

    def _toggle_view(self, advanced: bool):
        self.advanced_mode = advanced
        self.protection_panel.setVisible(advanced)
        self.action_logger.log(
            "Power Supply",
            "Toggle View",
            "Advanced" if advanced else "Simple",
        )

    # --- State update handler ---

    def update_state(self, state: PowerSupplyState):
        """Handle state update from worker."""
        if not state.connected:
            self.status_label.setText(f"Error: {state.error}")
            self.control_panel.mark_output_unknown()
            self.voltage_display.set_color("#888")
            self.current_display.set_color("#888")
            return
        if not state.has_valid_reading:
            self.status_label.setText(
                f"STALE — last poll failed: {state.error or 'no valid sample'}"
            )
            self.control_panel.mark_output_unknown()
            self.voltage_display.set_color("#888")
            self.current_display.set_color("#888")
            return

        self.status_label.setText(
            f"Connected  ⚠ {state.error}" if state.error else "Connected"
        )
        self.connect_btn.setText("Disconnect")

        # Update displays
        self.voltage_display.set_value(state.voltage_measured)
        self.current_display.set_value(state.current_measured)
        self.power_display.set_value(state.power_measured)
        self.voltage_sp_display.set_value(state.voltage_setpoint)
        self.current_sp_display.set_value(state.current_setpoint)

        # Update controls only from field-specific readbacks. Output can be
        # unknown/stale even when primary V/I/P is fresh.
        if state.has_fresh_output:
            self.control_panel.update_state(state)
        else:
            self.control_panel.mark_output_unknown()
        self.protection_panel.update_state(state)

        # Color code based on output state
        if state.has_fresh_output and state.output_enabled:
            self.voltage_display.set_color("#4CAF50")
            self.current_display.set_color("#4CAF50")
        else:
            self.voltage_display.set_color("#888")
            self.current_display.set_color("#888")

        # Plot only fresh hardware samples. The X axis is process-monotonic,
        # reset for every connection generation, so wall-clock corrections do
        # not move points backwards.
        if state.received_monotonic_ns is None:
            return
        if self._psu_generation != state.connection_generation:
            self._psu_generation = state.connection_generation
            self._psu_origin_ns = state.received_monotonic_ns
            self.psu_time.clear()
            self.psu_voltage.clear()
            self.psu_current.clear()
            self.psu_power.clear()
        now = (state.received_monotonic_ns - self._psu_origin_ns) / 1_000_000_000.0
        self.psu_time.append(now)
        self.psu_voltage.append(state.voltage_measured)
        self.psu_current.append(state.current_measured)
        self.psu_power.append(state.power_measured)

        t = list(self.psu_time)
        self.v_curve.setData(t, list(self.psu_voltage))
        self.i_curve.setData(t, list(self.psu_current))
        self.p_curve.setData(t, list(self.psu_power))

    def on_command_result(self, result: PowerSupplyCommandResult) -> None:
        if result.command not in ("output_on", "output_off"):
            return
        expected = result.command == "output_on"
        actual = bool(result.readback.get("output_enabled", not expected))
        self.control_panel.complete_output_request(result.confirmed, actual)

    def on_disconnected(self):
        """Reset UI on disconnect."""
        self.connect_btn.setText("Connect")
        self.status_label.setText("Disconnected")
        self.protection_panel.reset_initialized()
        self.control_panel.mark_output_unknown()
        self._psu_generation = None
        self._psu_origin_ns = None
