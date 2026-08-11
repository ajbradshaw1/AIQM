"""
RHEED camera tab — live frame display, ROI intensity tracking, oscillation plot.

Supports two frame sources:
  - Screen grab of kSA 400 window (primary — matches Classifier2 training data)
  - Direct vmbpy camera access (secondary — when kSA not running)
"""
from __future__ import annotations  # Defer X | None evaluation for Python 3.9 (Mac venv).

import time
from collections import deque

import numpy as np
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
    QComboBox, QSpinBox, QSplitter, QRubberBand,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QRect, QSize
from PyQt6.QtGui import QImage, QPixmap, QFont, QPainter, QColor, QPen

import pyqtgraph as pg

from gui.state import CameraState
from gui.heater_control.action_logger import ActionLogger


class RheedImageLabel(QLabel):
    """QLabel subclass that supports rubber-band ROI selection."""

    region_selected = pyqtSignal(QRect)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: black;")

        self._rubber_band = None
        self._origin = QPoint()
        self._roi: QRect | None = None
        self._selecting = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._selecting:
            self._origin = event.pos()
            if self._rubber_band is None:
                self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)
            self._rubber_band.setGeometry(QRect(self._origin, QSize()))
            self._rubber_band.show()

    def mouseMoveEvent(self, event):
        if self._rubber_band and self._selecting:
            self._rubber_band.setGeometry(
                QRect(self._origin, event.pos()).normalized()
            )

    def mouseReleaseEvent(self, event):
        if self._rubber_band and self._selecting:
            self._roi = self._rubber_band.geometry()
            self._rubber_band.hide()
            self._selecting = False
            self.region_selected.emit(self._roi)

    def start_selection(self):
        """Enable ROI selection mode."""
        self._selecting = True
        self.setCursor(Qt.CursorShape.CrossCursor)

    @property
    def roi(self) -> QRect | None:
        return self._roi

    def clear_roi(self):
        self._roi = None


class RheedTab(QWidget):
    """RHEED camera monitoring tab with live display and intensity oscillation plot."""

    # mode: "direct", "screengrab", "screengrab_mss", or "dummy"
    connect_requested = pyqtSignal(str)
    disconnect_requested = pyqtSignal()
    save_frame_requested = pyqtSignal()  # single frame capture
    stream_start_requested = pyqtSignal(float, float)  # (duration_s, freq_hz)
    stream_stop_requested = pyqtSignal()

    def __init__(self, action_logger: ActionLogger, parent=None):
        super().__init__(parent)
        self.action_logger = action_logger

        # Intensity oscillation buffers
        self._plot_start = time.time()
        self._plot_times = deque(maxlen=15000)
        self._plot_intensities = deque(maxlen=15000)

        self._roi: QRect | None = None
        self._current_frame: np.ndarray | None = None

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ─── Top control bar ───
        top_bar = QHBoxLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            "Windows Capture (kSA)",
            "Legacy mss (diagnostic)",
            "Direct Camera",
            "Dummy (Test)",
        ])
        top_bar.addWidget(QLabel("Mode:"))
        top_bar.addWidget(self.mode_combo)

        self.connect_btn = QPushButton("Connect Camera")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        top_bar.addWidget(self.connect_btn)

        self.status_label = QLabel("Disconnected")
        top_bar.addWidget(self.status_label)
        top_bar.addStretch()

        # ROI controls
        self.roi_btn = QPushButton("Select ROI")
        self.roi_btn.clicked.connect(self._on_roi_clicked)
        self.roi_btn.setEnabled(False)
        top_bar.addWidget(self.roi_btn)

        self.clear_roi_btn = QPushButton("Clear ROI")
        self.clear_roi_btn.clicked.connect(self._on_clear_roi)
        self.clear_roi_btn.setEnabled(False)
        top_bar.addWidget(self.clear_roi_btn)

        layout.addLayout(top_bar)

        # ─── Main content: image + plot in splitter ───
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Live RHEED image
        image_group = QGroupBox("Live RHEED")
        image_layout = QVBoxLayout(image_group)
        image_layout.setContentsMargins(2, 2, 2, 2)

        self.image_label = RheedImageLabel()
        self.image_label.region_selected.connect(self._on_region_selected)
        image_layout.addWidget(self.image_label)

        # Frame info bar
        info_bar = QHBoxLayout()
        self.frame_info = QLabel("No frame")
        self.fps_label = QLabel("FPS: --")
        info_bar.addWidget(self.frame_info)
        info_bar.addStretch()
        info_bar.addWidget(self.fps_label)
        image_layout.addLayout(info_bar)

        splitter.addWidget(image_group)

        # Intensity oscillation plot
        plot_group = QGroupBox("RHEED Intensity Oscillations")
        plot_layout = QVBoxLayout(plot_group)
        plot_layout.setContentsMargins(4, 4, 4, 4)

        self.intensity_plot = pg.PlotWidget()
        self.intensity_plot.setLabel("left", "Intensity", "a.u.")
        self.intensity_plot.setLabel("bottom", "Time", "s")
        self.intensity_plot.showGrid(x=True, y=True)
        self.intensity_curve = self.intensity_plot.plot(
            pen=pg.mkPen("g", width=2), name="ROI Intensity"
        )

        plot_layout.addWidget(self.intensity_plot)

        # Reset plot button
        reset_bar = QHBoxLayout()
        reset_btn = QPushButton("Reset Plot")
        reset_btn.clicked.connect(self._reset_plot)
        reset_bar.addStretch()
        reset_bar.addWidget(reset_btn)
        plot_layout.addLayout(reset_bar)

        splitter.addWidget(plot_group)

        # Give image 2/3 of space
        splitter.setSizes([400, 200])

        layout.addWidget(splitter)

    def _mode_string(self) -> str:
        idx = self.mode_combo.currentIndex()
        return ["screengrab", "screengrab_mss", "direct", "dummy"][idx]

    def _on_connect_clicked(self):
        if self.connect_btn.text() == "Connect Camera":
            mode = self._mode_string()
            self.connect_requested.emit(mode)
            self.action_logger.log("RHEED", "Connect", f"Mode: {mode}")
        else:
            self.disconnect_requested.emit()
            self.action_logger.log("RHEED", "Disconnect", "Requested disconnection")

    def _on_roi_clicked(self):
        self.image_label.start_selection()
        self.status_label.setText("Click and drag to select ROI...")

    def _on_region_selected(self, rect: QRect):
        self._roi = rect
        self.status_label.setText(
            f"ROI: ({rect.x()}, {rect.y()}) {rect.width()}x{rect.height()}"
        )
        self.clear_roi_btn.setEnabled(True)
        self._reset_plot()

    def _on_clear_roi(self):
        self._roi = None
        self.image_label.clear_roi()
        self.clear_roi_btn.setEnabled(False)
        self._reset_plot()

    def _reset_plot(self):
        self._plot_start = time.time()
        self._plot_times.clear()
        self._plot_intensities.clear()
        self.intensity_curve.setData([], [])

    def update_state(self, state: CameraState):
        """Handle camera state update from worker."""
        if not state.connected and state.error:
            self._current_frame = None
            self.image_label.clear()
            self.status_label.setText(f"Error: {state.error}")
            return

        if not state.connected:
            self._current_frame = None
            self.image_label.clear()
            return

        self.status_label.setText("Connected")
        self.connect_btn.setText("Disconnect Camera")
        self.mode_combo.setEnabled(False)
        self.roi_btn.setEnabled(True)

        self.fps_label.setText(f"FPS: {state.fps:.1f}")
        self.frame_info.setText(
            f"Frame #{state.frame_number}  {state.width}x{state.height}"
        )

        # Display the frame
        if state.frame is not None:
            frame = np.asarray(state.frame)
            self._current_frame = frame
            self._display_frame(frame)

            # Compute ROI intensity and update oscillation plot
            if self._roi is not None:
                intensity = self._compute_roi_intensity(frame, self._roi)
                now = time.time() - self._plot_start
                self._plot_times.append(now)
                self._plot_intensities.append(intensity)
                self.intensity_curve.setData(
                    list(self._plot_times), list(self._plot_intensities)
                )

    def _display_frame(self, frame: np.ndarray):
        """Convert numpy RGB array to QPixmap and display with overlays."""
        h, w = frame.shape[:2]
        if frame.ndim == 3:
            bytes_per_line = 3 * w
            qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        else:
            qimg = QImage(frame.data, w, h, w, QImage.Format.Format_Grayscale8)

        pixmap = QPixmap.fromImage(qimg)

        # Draw ROI rectangle overlay
        if self._roi is not None:
            painter = QPainter(pixmap)
            pen = QPen(QColor(0, 255, 0), 2)
            painter.setPen(pen)
            painter.drawRect(self._roi)
            painter.end()

        # Scale to fit label while preserving aspect ratio
        scaled = pixmap.scaled(
            self.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    @staticmethod
    def _compute_roi_intensity(frame: np.ndarray, roi: QRect) -> float:
        """Compute mean intensity within the ROI rectangle."""
        x, y, w, h = roi.x(), roi.y(), roi.width(), roi.height()
        h_frame, w_frame = frame.shape[:2]

        # Clamp to frame bounds
        x1 = max(0, min(x, w_frame - 1))
        y1 = max(0, min(y, h_frame - 1))
        x2 = max(0, min(x + w, w_frame))
        y2 = max(0, min(y + h, h_frame))

        if x2 <= x1 or y2 <= y1:
            return 0.0

        region = frame[y1:y2, x1:x2]

        # Use green channel if RGB (matches existing convention)
        if region.ndim == 3:
            return float(region[:, :, 1].mean())
        return float(region.mean())

    def on_disconnected(self):
        """Reset UI on disconnect."""
        self.connect_btn.setText("Connect Camera")
        self.mode_combo.setEnabled(True)
        self.status_label.setText("Disconnected")
        self.roi_btn.setEnabled(False)
        self.image_label.clear()
        self.image_label.setStyleSheet("background-color: black;")
