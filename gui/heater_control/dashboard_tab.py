"""
Combined dashboard tab — read-only overview of all instruments.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QGroupBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from gui.state import PowerSupplyState, TemperatureState
from gui.widgets import ValueDisplay


class DashboardTab(QWidget):
    """Read-only combined overview of all instruments."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # --- Top row: side-by-side summary panels ---
        panels = QHBoxLayout()

        # PSU panel
        psu_group = QGroupBox("Power Supply")
        psu_layout = QGridLayout(psu_group)

        self.psu_status = QLabel("Disconnected")
        self.psu_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        psu_layout.addWidget(self.psu_status, 0, 0, 1, 3)

        self.d_voltage = ValueDisplay("Voltage", "V", 3)
        self.d_current = ValueDisplay("Current", "A", 3)
        self.d_power = ValueDisplay("Power", "W", 3)
        psu_layout.addWidget(self.d_voltage, 1, 0)
        psu_layout.addWidget(self.d_current, 1, 1)
        psu_layout.addWidget(self.d_power, 1, 2)

        self.d_vsp = ValueDisplay("V Setpoint", "V", 2)
        self.d_isp = ValueDisplay("I Limit", "A", 3)
        psu_layout.addWidget(self.d_vsp, 2, 0)
        psu_layout.addWidget(self.d_isp, 2, 1)

        self.output_badge = QLabel("OUTPUT OFF")
        self.output_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.output_badge.setFont(QFont("Monospace", 14, QFont.Weight.Bold))
        self.output_badge.setStyleSheet("background-color: #666; color: white; padding: 6px;")
        psu_layout.addWidget(self.output_badge, 2, 2)

        panels.addWidget(psu_group)

        # Temperature panel
        temp_group = QGroupBox("Temperature")
        temp_layout = QGridLayout(temp_group)

        self.temp_status = QLabel("Disconnected")
        self.temp_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        temp_layout.addWidget(self.temp_status, 0, 0, 1, 2)

        self.d_tc = ValueDisplay("Thermocouple", "C", 2)
        self.d_cj = ValueDisplay("Cold Junction", "C", 2)
        temp_layout.addWidget(self.d_tc, 1, 0)
        temp_layout.addWidget(self.d_cj, 1, 1)

        self.d_channel = QLabel("Channel: --")
        self.d_channel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        temp_layout.addWidget(self.d_channel, 2, 0)

        self.d_device = QLabel("Device: --")
        self.d_device.setAlignment(Qt.AlignmentFlag.AlignCenter)
        temp_layout.addWidget(self.d_device, 2, 1)

        panels.addWidget(temp_group)

        layout.addLayout(panels)

    # --- State update methods (called via signal fan-out) ---

    def update_psu_state(self, state: PowerSupplyState):
        if not state.connected:
            self.psu_status.setText(f"Disconnected" if not state.error else f"Error: {state.error}")
            self._mark_output_unknown()
            return
        if not state.has_valid_reading:
            self.psu_status.setText(
                f"STALE: {state.error or 'waiting for a valid PSU sample'}"
            )
            self._mark_output_unknown()
            return

        self.psu_status.setText(
            f"Connected ⚠ {state.error}" if state.error else "Connected"
        )

        self.d_voltage.set_value(state.voltage_measured)
        self.d_current.set_value(state.current_measured)
        self.d_power.set_value(state.power_measured)
        self.d_vsp.set_value(state.voltage_setpoint)
        self.d_isp.set_value(state.current_setpoint)

        if not state.has_fresh_output:
            self._mark_output_unknown()
        elif state.output_enabled:
            self.output_badge.setText("OUTPUT ON")
            self.output_badge.setStyleSheet(
                "background-color: #4CAF50; color: white; padding: 6px;"
            )
            self.d_voltage.set_color("#4CAF50")
            self.d_current.set_color("#4CAF50")
        else:
            self.output_badge.setText("OUTPUT OFF")
            self.output_badge.setStyleSheet(
                "background-color: #666; color: white; padding: 6px;"
            )
            self.d_voltage.set_color("#888")
            self.d_current.set_color("#888")

    def _mark_output_unknown(self) -> None:
        self.output_badge.setText("OUTPUT UNKNOWN / STALE")
        self.output_badge.setStyleSheet(
            "background-color: #FF9800; color: black; padding: 6px;"
        )
        self.d_voltage.set_color("#888")
        self.d_current.set_color("#888")

    def update_temp_state(self, state: TemperatureState):
        if not state.connected:
            self.temp_status.setText("Disconnected" if not state.error else f"Error: {state.error}")
            return

        self.temp_status.setText("Connected")

        self.d_tc.set_value(state.temperature)
        self.d_cj.set_value(state.cold_junction)
        self.d_channel.setText(f"Channel: {state.channel}")
        self.d_device.setText(f"Device: {state.device_info}")
