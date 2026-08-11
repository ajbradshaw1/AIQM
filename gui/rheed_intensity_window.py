"""Floating RHEED intensity vs time window.

Receives CameraState updates via on_camera_state(); growers open it
with the 'RHEED Trend' button in the Monitor tab. Can be dragged to a
second monitor and kept open independently of the main GUI.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Optional

import numpy as np
from PyQt6.QtWidgets import QMainWindow, QPushButton, QVBoxLayout, QWidget

import pyqtgraph as pg

from gui.state import CameraState


class RheedIntensityWindow(QMainWindow):
    _MAXLEN = 600  # 10 min at 1 Hz

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("RHEED Intensity vs Time")
        self.setMinimumSize(600, 300)
        self._t0: Optional[float] = None
        self._times: deque[float] = deque(maxlen=self._MAXLEN)
        self._intensities: deque[float] = deque(maxlen=self._MAXLEN)

        plot = pg.PlotWidget()
        plot.setBackground("#0f1117")
        plot.setLabel("left", "Intensity", units="a.u.")
        plot.setLabel("bottom", "Elapsed", units="min")
        plot.getPlotItem().showGrid(x=True, y=True, alpha=0.3)
        self._curve = plot.plot(pen=pg.mkPen("#38bdf8", width=1.5))

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(80)
        clear_btn.clicked.connect(self.reset)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        layout.addWidget(plot, 1)
        layout.addWidget(clear_btn)
        self.setCentralWidget(central)

    def on_camera_state(self, state: CameraState) -> None:
        if not state.connected or not state.valid or math.isnan(state.intensity):
            return
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        self._times.append((now - self._t0) / 60.0)
        self._intensities.append(state.intensity)
        self._curve.setData(list(self._times), list(self._intensities))

    def reset(self) -> None:
        self._t0 = None
        self._times.clear()
        self._intensities.clear()
        self._curve.setData([], [])
