"""
PID temperature control tab.

Layout:
  - Status bar: state badge, live T / err / V-out / active band, hold progress
  - Setpoint & Safety: target, hold time, margin, max voltage, current, slew, hard cutoff
  - Gain Schedule: 3×3 grid (Kp/Ki/Kd × Band1/2/3) + threshold spinboxes
  - Controls: Arm / Start / Stop / Reset  +  EMERGENCY STOP
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QDoubleSpinBox, QProgressBar,
)
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFont

from gui.heater_control.pid_controller import PIDController, PIDConfig, GainBand, HARD_CUTOFF_C, PIDRunState


_STATE_COLORS = {
    "IDLE":     "#888888",
    "ARMED":    "#FF9800",
    "STARTING": "#FF9800",
    "RUNNING":  "#4CAF50",
    "STOPPING": "#FF9800",
    "COMPLETE": "#2196F3",
    "FAULT":    "#F44336",
    "STOPPED":  "#9E9E9E",
}


class PIDTab(QWidget):
    """Temperature PID control panel."""

    emergency_stop_requested = pyqtSignal()

    def __init__(self, pid_controller: PIDController, config_tab, parent=None):
        super().__init__(parent)
        self.pid_controller = pid_controller
        self._config_tab = config_tab
        self._build_ui()
        self.pid_controller.pid_state_updated.connect(self._on_pid_state)
        self._update_button_states("IDLE")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(self._build_status_group())

        mid = QHBoxLayout()
        mid.addWidget(self._build_setpoint_group(), stretch=1)
        mid.addWidget(self._build_gain_group(), stretch=2)
        layout.addLayout(mid)

        layout.addWidget(self._build_controls_group())
        layout.addStretch()

    def _build_status_group(self) -> QGroupBox:
        group = QGroupBox("Status")
        outer = QVBoxLayout(group)

        # Row 1: state badge + live readings
        row1 = QHBoxLayout()

        self.state_label = QLabel("IDLE")
        self.state_label.setFont(QFont("Monospace", 13, QFont.Weight.Bold))
        self.state_label.setMinimumWidth(90)
        self.state_label.setStyleSheet(f"color: {_STATE_COLORS['IDLE']};")
        row1.addWidget(self.state_label)

        sep = QLabel("|")
        sep.setStyleSheet("color: #555;")
        row1.addWidget(sep)

        self.temp_label   = QLabel("T: --.- °C")
        self.error_label  = QLabel("err: --.- °C")
        self.voltage_label = QLabel("V-out: --.- V")
        self.band_label   = QLabel("Band: -")

        for lbl in (self.temp_label, self.error_label, self.voltage_label, self.band_label):
            lbl.setFont(QFont("Monospace", 10))
            row1.addWidget(lbl)
            row1.addSpacing(12)

        row1.addStretch()

        self.device_status_label = QLabel("PSU: — | TC: —")
        self.device_status_label.setStyleSheet("color: #888; font-style: italic;")
        row1.addWidget(self.device_status_label)

        outer.addLayout(row1)

        # Row 2: hold progress bar + fault message
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Hold:"))
        self.hold_bar = QProgressBar()
        self.hold_bar.setRange(0, 1000)
        self.hold_bar.setValue(0)
        self.hold_bar.setFormat("--.-s / --.-s")
        self.hold_bar.setMaximumHeight(18)
        row2.addWidget(self.hold_bar, stretch=1)

        self.fault_label = QLabel("")
        self.fault_label.setStyleSheet("color: #F44336; font-weight: bold;")
        row2.addWidget(self.fault_label)

        outer.addLayout(row2)
        return group

    def _build_setpoint_group(self) -> QGroupBox:
        group = QGroupBox("Setpoint & Safety")
        form = QFormLayout(group)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self.target_spin = QDoubleSpinBox()
        self.target_spin.setRange(0.0, HARD_CUTOFF_C - 0.1)
        self.target_spin.setValue(100.0)
        self.target_spin.setSuffix(" °C")
        self.target_spin.setDecimals(1)
        form.addRow("Target:", self.target_spin)

        self.hold_spin = QDoubleSpinBox()
        self.hold_spin.setRange(0.0, 999.0)
        self.hold_spin.setValue(5.0)
        self.hold_spin.setSuffix(" min")
        self.hold_spin.setDecimals(1)
        form.addRow("Hold time:", self.hold_spin)

        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.1, 50.0)
        self.margin_spin.setValue(1.0)
        self.margin_spin.setSuffix(" °C")
        self.margin_spin.setDecimals(1)
        form.addRow("Margin ±:", self.margin_spin)

        self.max_v_spin = QDoubleSpinBox()
        self.max_v_spin.setRange(0.1, 24.0)
        self.max_v_spin.setValue(24.0)
        self.max_v_spin.setSuffix(" V")
        self.max_v_spin.setDecimals(1)
        form.addRow("Max voltage:", self.max_v_spin)

        self.curr_spin = QDoubleSpinBox()
        self.curr_spin.setRange(0.01, 1.0)
        self.curr_spin.setValue(1.0)
        self.curr_spin.setSuffix(" A")
        self.curr_spin.setDecimals(3)
        form.addRow("Current limit:", self.curr_spin)

        self.slew_spin = QDoubleSpinBox()
        self.slew_spin.setRange(0.01, 10.0)
        self.slew_spin.setValue(1.0)
        self.slew_spin.setSuffix(" V/s")
        self.slew_spin.setDecimals(2)
        form.addRow("Slew rate:", self.slew_spin)

        cutoff_display = QLabel()
        cutoff_display.setStyleSheet("color: #FF9800; font-weight: bold;")
        cutoff_display.setToolTip("Configured in the Config tab → Safety section.")

        def _refresh_cutoff():
            cutoff_display.setText(
                f"{self._config_tab.hard_cutoff_c:.1f} °C  (set in Config tab)"
            )

        # Update the display whenever the spinbox in Config tab changes
        self._config_tab.hard_cutoff_spin.valueChanged.connect(
            lambda _: _refresh_cutoff()
        )
        _refresh_cutoff()
        form.addRow("Hard cutoff:", cutoff_display)

        return group

    def _build_gain_group(self) -> QGroupBox:
        group = QGroupBox("Gain Schedule")
        layout = QVBoxLayout(group)

        # Threshold configuration
        thresh_row = QHBoxLayout()
        thresh_form = QFormLayout()

        self.t1_spin = QDoubleSpinBox()
        self.t1_spin.setRange(10.0, HARD_CUTOFF_C - 20.0)
        self.t1_spin.setValue(60.0)
        self.t1_spin.setSuffix(" °C")
        self.t1_spin.setDecimals(1)
        thresh_form.addRow("Threshold T1:", self.t1_spin)

        self.t2_spin = QDoubleSpinBox()
        self.t2_spin.setRange(10.0, HARD_CUTOFF_C - 10.0)
        self.t2_spin.setValue(110.0)
        self.t2_spin.setSuffix(" °C")
        self.t2_spin.setDecimals(1)
        thresh_form.addRow("Threshold T2:", self.t2_spin)

        self.interp_spin = QDoubleSpinBox()
        self.interp_spin.setRange(1.0, 50.0)
        self.interp_spin.setValue(10.0)
        self.interp_spin.setSuffix(" °C")
        self.interp_spin.setDecimals(1)
        thresh_form.addRow("Interp band:", self.interp_spin)

        thresh_row.addLayout(thresh_form)
        thresh_row.addStretch()

        note = QLabel(
            "Band 1: T < T1\n"
            "Band 2: T1 – T2\n"
            "Band 3: T > T2\n"
            "(smooth blend ±interp/2)"
        )
        note.setStyleSheet("color: #888; font-size: 10px;")
        thresh_row.addWidget(note)

        layout.addLayout(thresh_row)

        # Gains grid: rows = Kp/Ki/Kd, columns = Band 1/2/3
        grid = QGridLayout()
        grid.setSpacing(6)

        bold = QFont()
        bold.setBold(True)

        headers = ["", "Band 1\n(cold)", "Band 2\n(mid)", "Band 3\n(hot)"]
        for col, text in enumerate(headers):
            lbl = QLabel(text)
            lbl.setFont(bold)
            lbl.setStyleSheet("color: #aaa;")
            grid.addWidget(lbl, 0, col)

        # Default gains scaled for 0-300 °C range.
        # These are starting points — will need tuning on real hardware.
        # Loosely inspired by the p/i/d values in temp_pid.py but rescaled
        # for a much lower temperature range and direct voltage (not Modbus) control.
        gain_defaults = {
            #         Band1  Band2  Band3
            "Kp":   [2.0,   1.5,   1.0],
            "Ki":   [0.10,  0.05,  0.02],
            "Kd":   [0.00,  0.00,  0.00],
        }

        self.gain_spins: dict = {}   # (param, band_idx) → QDoubleSpinBox

        for row_idx, (param, defaults) in enumerate(gain_defaults.items(), start=1):
            lbl = QLabel(param)
            lbl.setFont(bold)
            grid.addWidget(lbl, row_idx, 0)

            for band_idx, default in enumerate(defaults):
                spin = QDoubleSpinBox()
                spin.setRange(0.0, 999.0)
                spin.setValue(default)
                spin.setDecimals(4)
                spin.setSingleStep(0.01)
                spin.setMinimumWidth(80)
                grid.addWidget(spin, row_idx, band_idx + 1)
                self.gain_spins[(param, band_idx)] = spin

        layout.addLayout(grid)
        return group

    def _build_controls_group(self) -> QGroupBox:
        group = QGroupBox("Controls")
        layout = QHBoxLayout(group)

        self.arm_btn = QPushButton("Arm")
        self.arm_btn.setToolTip(
            "Validate parameters and prepare the controller.\n"
            "Both PSU and thermocouple must be connected first."
        )
        self.arm_btn.clicked.connect(self._on_arm)

        self.start_btn = QPushButton("Start")
        self.start_btn.setToolTip("Begin the control loop (must be Armed first).")
        self.start_btn.clicked.connect(self._on_start)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setToolTip("Gracefully stop: zero voltage, output off.")
        self.stop_btn.clicked.connect(self._on_stop)

        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setToolTip("Return to IDLE from any terminal state.")
        self.reset_btn.clicked.connect(self._on_reset)

        self.estop_btn = QPushButton("EMERGENCY STOP")
        self.estop_btn.setMinimumHeight(50)
        self.estop_btn.setStyleSheet("""
            QPushButton {
                background-color: #d32f2f;
                color: white;
                font-size: 13px;
                font-weight: bold;
                border-radius: 6px;
            }
            QPushButton:hover   { background-color: #b71c1c; }
            QPushButton:pressed { background-color: #ff5252; }
        """)
        self.estop_btn.clicked.connect(self._on_emergency_stop)

        for btn in (self.arm_btn, self.start_btn, self.stop_btn, self.reset_btn):
            btn.setMinimumHeight(34)
            layout.addWidget(btn)

        layout.addStretch()
        layout.addWidget(self.estop_btn)
        return group

    # ------------------------------------------------------------------
    # PID state update
    # ------------------------------------------------------------------

    def _on_pid_state(self, state: PIDRunState):
        color = _STATE_COLORS.get(state.controller_state, "#888")
        self.state_label.setText(state.controller_state)
        self.state_label.setStyleSheet(f"color: {color}; font-weight: bold;")

        if state.controller_state in (
            "STARTING", "RUNNING", "STOPPING", "COMPLETE", "FAULT", "STOPPED",
        ):
            self.temp_label.setText(f"T: {state.measured_c:.2f} °C")
            self.error_label.setText(f"err: {state.error_c:+.2f} °C")
            self.voltage_label.setText(f"V-out: {state.output_v:.3f} V")
            self.band_label.setText(f"Band: {state.active_band + 1}")

        if state.hold_total_s > 0:
            frac = min(state.hold_elapsed_s / state.hold_total_s, 1.0)
            self.hold_bar.setValue(int(frac * 1000))
            self.hold_bar.setFormat(
                f"{state.hold_elapsed_s:.1f}s / {state.hold_total_s:.1f}s"
            )
        else:
            self.hold_bar.setValue(0)
            self.hold_bar.setFormat("No hold configured")

        psu_str = "PSU: ✓" if state.psu_connected else "PSU: ✗"
        tc_str  = "TC: ✓"  if state.tc_connected  else "TC: ✗"
        self.device_status_label.setText(f"{psu_str}  |  {tc_str}")

        if state.controller_state == "FAULT":
            self.fault_label.setText(f"FAULT: {state.fault_message}")
        else:
            self.fault_label.setText("")

        self._update_button_states(state.controller_state)

    def _update_button_states(self, state: str):
        self.arm_btn.setEnabled(state in ("IDLE", "STOPPED", "COMPLETE", "FAULT"))
        self.start_btn.setEnabled(state == "ARMED")
        self.stop_btn.setEnabled(state in ("RUNNING", "ARMED"))
        self.reset_btn.setEnabled(state in ("STOPPED", "COMPLETE", "FAULT"))

        editable = state not in ("RUNNING", "ARMED", "STARTING", "STOPPING")
        for spin in (
            self.target_spin, self.hold_spin, self.margin_spin,
            self.max_v_spin, self.curr_spin, self.slew_spin,
            self.t1_spin, self.t2_spin, self.interp_spin,
        ):
            spin.setEnabled(editable)
        for spin in self.gain_spins.values():
            spin.setEnabled(editable)

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_arm(self):
        self.pid_controller.arm(self._build_config())

    def _on_start(self):
        self.pid_controller.start()

    def _on_stop(self):
        self.pid_controller.stop()

    def _on_reset(self):
        self.pid_controller.reset()

    def _on_emergency_stop(self):
        self.emergency_stop_requested.emit()
        self.pid_controller.emergency_stop()

    def _build_config(self) -> PIDConfig:
        bands = [
            GainBand(
                kp=self.gain_spins[("Kp", i)].value(),
                ki=self.gain_spins[("Ki", i)].value(),
                kd=self.gain_spins[("Kd", i)].value(),
            )
            for i in range(3)
        ]
        return PIDConfig(
            target_c=self.target_spin.value(),
            hold_s=self.hold_spin.value() * 60.0,
            margin_c=self.margin_spin.value(),
            max_voltage=self.max_v_spin.value(),
            current_limit_a=self.curr_spin.value(),
            slew_rate_v_per_s=self.slew_spin.value(),
            bands=bands,
            threshold_t1=self.t1_spin.value(),
            threshold_t2=self.t2_spin.value(),
            interp_band_c=self.interp_spin.value(),
            hard_cutoff_c=self._config_tab.hard_cutoff_c,
        )
