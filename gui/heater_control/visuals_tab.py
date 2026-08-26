"""
Visuals tab — all instrument graphs consolidated in one place.

Separate plots for Voltage, Current, Power, and Temperature.
"""

import time
from collections import deque

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QSplitter
from PyQt6.QtCore import Qt

import pyqtgraph as pg

from gui.state import PowerSupplyState, TemperatureState


class VisualsTab(QWidget):
    """Consolidated graphing tab with separate plots per measurement."""

    def __init__(self, parent=None):
        super().__init__(parent)

        # PSU data buffers
        self.psu_time = deque(maxlen=120)
        self.psu_voltage = deque(maxlen=120)
        self.psu_current = deque(maxlen=120)
        self.psu_power = deque(maxlen=120)
        self._psu_generation = None
        self._psu_origin_ns = None

        # Temperature data buffers
        self.temp_time = deque(maxlen=240)
        self.temp_tc = deque(maxlen=240)
        self.temp_cj = deque(maxlen=240)
        self.temp_start = time.time()

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # --- Voltage plot ---
        self.voltage_plot = pg.PlotWidget(title="Voltage")
        self.voltage_plot.setLabel("left", "Voltage", "V")
        self.voltage_plot.setLabel("bottom", "Time", "s")
        self.voltage_plot.showGrid(x=True, y=True)
        self.v_curve = self.voltage_plot.plot(pen=pg.mkPen("y", width=2))
        splitter.addWidget(self.voltage_plot)

        # --- Current plot ---
        self.current_plot = pg.PlotWidget(title="Current")
        self.current_plot.setLabel("left", "Current", "A")
        self.current_plot.setLabel("bottom", "Time", "s")
        self.current_plot.showGrid(x=True, y=True)
        self.i_curve = self.current_plot.plot(pen=pg.mkPen("c", width=2))
        splitter.addWidget(self.current_plot)

        # --- Power plot ---
        self.power_plot = pg.PlotWidget(title="Power")
        self.power_plot.setLabel("left", "Power", "W")
        self.power_plot.setLabel("bottom", "Time", "s")
        self.power_plot.showGrid(x=True, y=True)
        self.p_curve = self.power_plot.plot(pen=pg.mkPen("m", width=2))
        splitter.addWidget(self.power_plot)

        # --- Temperature plot (TC + CJ on same axes) ---
        self.temp_plot = pg.PlotWidget(title="Temperature")
        self.temp_plot.setLabel("left", "Temperature", "C")
        self.temp_plot.setLabel("bottom", "Time", "s")
        self.temp_plot.addLegend()
        self.temp_plot.showGrid(x=True, y=True)
        self.tc_curve = self.temp_plot.plot(pen=pg.mkPen("r", width=2), name="Thermocouple")
        self.cj_curve = self.temp_plot.plot(pen=pg.mkPen("b", width=2), name="Cold Junction")
        splitter.addWidget(self.temp_plot)

        # Link X axes so panning/zooming one scrolls all PSU plots together
        self.current_plot.setXLink(self.voltage_plot)
        self.power_plot.setXLink(self.voltage_plot)

        layout.addWidget(splitter)

    # --- State update methods (called via signal fan-out) ---

    def update_psu_state(self, state: PowerSupplyState):
        if not state.has_valid_reading or state.received_monotonic_ns is None:
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

    def update_temp_state(self, state: TemperatureState):
        if not state.connected:
            return

        now = time.time() - self.temp_start
        self.temp_time.append(now)
        self.temp_tc.append(state.temperature)
        self.temp_cj.append(state.cold_junction)

        t = list(self.temp_time)
        self.tc_curve.setData(t, list(self.temp_tc))
        self.cj_curve.setData(t, list(self.temp_cj))
