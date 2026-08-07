"""Floating RHEED ROI intensity monitor.

Growers open this window with ``RHEED Trend`` in the production Growth
Monitor.  It supports a manually drawn rectangle or a grower-confirmed,
one-shot three-spot proposal.  Every accepted ROI is frozen to the capture
backend/HWND/geometry/frame size that defined it.
"""
from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Optional

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPen, QPixmap
from PyQt6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg

from gui.equalizer_alignment import MIN_DETECTION_SNR
from gui.rheed_intensity import (
    NormalizedRoi,
    RheedIntensitySample,
    RheedRoiDefinition,
    auto_three_spot_roi_definition,
    manual_roi_definition,
    measure_camera_state,
)
from gui.state import CameraState


class RheedRoiImageView(QGraphicsView):
    """Frame-pixel scene with drag selection and ROI overlays."""

    region_selected = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item: Optional[QGraphicsPixmapItem] = None
        self._overlay_items: list[QGraphicsRectItem] = []
        self._drag_item: Optional[QGraphicsRectItem] = None
        self._drag_origin: Optional[QPointF] = None
        self._selecting = False
        self._frame_size = (0, 0)
        self.setMinimumSize(400, 280)
        self.setBackgroundBrush(QColor("#050608"))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)

    @property
    def frame_size(self) -> tuple[int, int]:
        return self._frame_size

    @property
    def is_selecting(self) -> bool:
        return self._selecting

    def start_selection(self) -> bool:
        if self._pixmap_item is None:
            return False
        self._selecting = True
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        return True

    def cancel_selection(self) -> None:
        self._selecting = False
        self._drag_origin = None
        self.viewport().unsetCursor()
        if self._drag_item is not None:
            self._scene.removeItem(self._drag_item)
            self._drag_item = None

    def set_frame(
        self,
        frame: np.ndarray,
        *,
        active: Optional[RheedRoiDefinition] = None,
        candidate: Optional[RheedRoiDefinition] = None,
    ) -> None:
        arr = np.asarray(frame)
        if arr.ndim == 2:
            mono = np.ascontiguousarray(arr.astype(np.uint8, copy=False))
            height, width = mono.shape
            image = QImage(
                mono.data, width, height, mono.strides[0],
                QImage.Format.Format_Grayscale8,
            ).copy()
        elif arr.ndim == 3 and arr.shape[2] >= 3:
            rgb = np.ascontiguousarray(arr[:, :, :3].astype(np.uint8, copy=False))
            height, width = rgb.shape[:2]
            image = QImage(
                rgb.data, width, height, rgb.strides[0],
                QImage.Format.Format_RGB888,
            ).copy()
        else:
            raise ValueError(f"Unsupported RHEED frame shape: {arr.shape}")

        self.cancel_selection()
        self._scene.clear()
        self._overlay_items.clear()
        self._pixmap_item = self._scene.addPixmap(QPixmap.fromImage(image))
        self._frame_size = (width, height)
        self._scene.setSceneRect(0.0, 0.0, float(width), float(height))
        if active is not None:
            self._draw_regions(active.regions, QColor("#22c55e"), Qt.PenStyle.SolidLine)
        if candidate is not None:
            self._draw_regions(candidate.regions, QColor("#f59e0b"), Qt.PenStyle.DashLine)
        self._fit_frame()

    def clear_frame(self) -> None:
        self.cancel_selection()
        self._scene.clear()
        self._pixmap_item = None
        self._overlay_items.clear()
        self._frame_size = (0, 0)

    def _draw_regions(
        self,
        regions: tuple[NormalizedRoi, ...],
        color: QColor,
        style: Qt.PenStyle,
    ) -> None:
        width, height = self._frame_size
        pen = QPen(color, max(1.0, width / 320.0), style)
        pen.setCosmetic(True)
        for region in regions:
            x0, y0, x1, y1 = region.pixel_bounds(width, height)
            item = self._scene.addRect(
                QRectF(float(x0), float(y0), float(x1 - x0), float(y1 - y0)),
                pen,
            )
            item.setZValue(10.0)
            self._overlay_items.append(item)

    def _fit_frame(self) -> None:
        if self._pixmap_item is not None:
            self.fitInView(
                self._scene.sceneRect(),
                Qt.AspectRatioMode.KeepAspectRatio,
            )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._fit_frame()

    def _clamped_scene_point(self, position) -> QPointF:
        point = self.mapToScene(position)
        rect = self._scene.sceneRect()
        return QPointF(
            max(rect.left(), min(point.x(), rect.right())),
            max(rect.top(), min(point.y(), rect.bottom())),
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._selecting and event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = self._clamped_scene_point(event.position().toPoint())
            pen = QPen(QColor("#38bdf8"), 1.5, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            self._drag_item = self._scene.addRect(QRectF(self._drag_origin, self._drag_origin), pen)
            self._drag_item.setZValue(20.0)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._selecting and self._drag_origin is not None and self._drag_item is not None:
            current = self._clamped_scene_point(event.position().toPoint())
            self._drag_item.setRect(QRectF(self._drag_origin, current).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if (
            self._selecting
            and self._drag_origin is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            current = self._clamped_scene_point(event.position().toPoint())
            rect = QRectF(self._drag_origin, current).normalized()
            width, height = self._frame_size
            self.cancel_selection()
            if width > 0 and height > 0 and rect.width() >= 2.0 and rect.height() >= 2.0:
                normalized = (
                    max(0.0, min(1.0, rect.left() / width)),
                    max(0.0, min(1.0, rect.top() / height)),
                    max(0.0, min(1.0, rect.right() / width)),
                    max(0.0, min(1.0, rect.bottom() / height)),
                )
                if normalized[2] > normalized[0] and normalized[3] > normalized[1]:
                    self.region_selected.emit(normalized)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class RheedIntensityWindow(QMainWindow):
    """Live ROI display-luminance sum monitor with capture provenance."""

    measurement_ready = pyqtSignal(object)
    roi_event = pyqtSignal(str, object, str)
    _MAXLEN = 3600  # one hour at the current 1 Hz camera worker cadence

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("RHEED ROI Intensity")
        self.setMinimumSize(900, 480)
        self._latest_state: Optional[CameraState] = None
        self._active_roi: Optional[RheedRoiDefinition] = None
        self._candidate_roi: Optional[RheedRoiDefinition] = None
        self._manual_selection_state: Optional[CameraState] = None
        self._baseline_sum: Optional[float] = None
        self._last_sample_key: Optional[tuple[str, int, str, int]] = None
        self._t0_ns: Optional[int] = None
        self._times: deque[float] = deque(maxlen=self._MAXLEN)
        self._intensities: deque[float] = deque(maxlen=self._MAXLEN)

        self.image_view = RheedRoiImageView()
        self.image_view.region_selected.connect(self._on_manual_region)

        self.select_btn = QPushButton("Select rectangle")
        self.select_btn.clicked.connect(self._start_manual_selection)
        self.auto_btn = QPushButton("Detect 1×1 triplet")
        self.auto_btn.clicked.connect(self._detect_three_spots)
        self.accept_btn = QPushButton("Use detected ROI")
        self.accept_btn.clicked.connect(self._accept_candidate)
        self.accept_btn.setEnabled(False)
        self.clear_roi_btn = QPushButton("Clear ROI")
        self.clear_roi_btn.clicked.connect(self.clear_roi)
        self.clear_roi_btn.setEnabled(False)

        self.spot_size = QSpinBox()
        self.spot_size.setRange(3, 31)
        self.spot_size.setSingleStep(2)
        self.spot_size.setValue(7)
        self.spot_size.setToolTip("Odd box width in the Equalizer's 128×96 coordinate frame")

        self.reset_btn = QPushButton("Clear plot")
        self.reset_btn.clicked.connect(self.reset)

        controls = QHBoxLayout()
        controls.addWidget(self.select_btn)
        controls.addWidget(self.auto_btn)
        controls.addWidget(self.accept_btn)
        controls.addWidget(self.clear_roi_btn)
        controls.addWidget(QLabel("Spot box (128×96 px):"))
        controls.addWidget(self.spot_size)
        controls.addStretch()
        controls.addWidget(self.reset_btn)

        self.status_label = QLabel(
            "No ROI. Draw one rectangle or detect a three-spot candidate."
        )
        self.status_label.setWordWrap(True)
        self.value_label = QLabel("Sum: —   Mean: —   Pixels: —   Δ: —")

        plot = pg.PlotWidget()
        plot.setBackground("#0f1117")
        plot.setLabel("left", "BT.601 display-luminance sum", units="a.u.")
        plot.setLabel("bottom", "Elapsed capture time", units="min")
        plot.getPlotItem().showGrid(x=True, y=True, alpha=0.3)
        self._curve = plot.plot(pen=pg.mkPen("#38bdf8", width=1.5))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.image_view)
        splitter.addWidget(plot)
        splitter.setSizes([480, 520])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(controls)
        layout.addWidget(self.status_label)
        layout.addWidget(self.value_label)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    @property
    def active_roi(self) -> Optional[RheedRoiDefinition]:
        return self._active_roi

    @property
    def latest_sample(self) -> Optional[RheedIntensitySample]:
        return getattr(self, "_latest_sample", None)

    def _usable_latest(self) -> tuple[CameraState, np.ndarray] | None:
        state = self._latest_state
        if (
            state is None
            or not state.connected
            or not state.valid
            or state.frame is None
        ):
            self.status_label.setText("RHEED frame unavailable; ROI selection is disabled.")
            return None
        return state, np.asarray(state.frame)

    def _start_manual_selection(self) -> None:
        current = self._usable_latest()
        if current is None:
            return
        state, frame = current
        self._manual_selection_state = replace(
            state,
            frame=np.array(frame, copy=True),
        )
        self._candidate_roi = None
        self.accept_btn.setEnabled(False)
        self._refresh_image()
        if self.image_view.start_selection():
            self.status_label.setText("Drag on the RHEED image to define one fixed ROI.")

    def _on_manual_region(self, normalized_rect: object) -> None:
        state = self._manual_selection_state
        self._manual_selection_state = None
        if state is None or state.frame is None:
            self.status_label.setText("ROI selection snapshot was unavailable; try again.")
            return
        frame = np.asarray(state.frame)
        try:
            roi = manual_roi_definition(tuple(normalized_rect), state, frame)
        except (TypeError, ValueError) as exc:
            self.status_label.setText(f"ROI rejected: {exc}")
            return
        self._activate_roi(roi, "defined")

    def _detect_three_spots(self) -> None:
        current = self._usable_latest()
        if current is None:
            return
        self.image_view.cancel_selection()
        self._manual_selection_state = None
        state, frame = current
        size = int(self.spot_size.value())
        if size % 2 == 0:
            size += 1
            self.spot_size.setValue(size)
        try:
            candidate = auto_three_spot_roi_definition(
                state, frame, box_size_processed_px=size,
            )
        except (TypeError, ValueError) as exc:
            self._candidate_roi = None
            self.accept_btn.setEnabled(False)
            self.status_label.setText(str(exc))
            self._refresh_image()
            return
        self._candidate_roi = candidate
        self.accept_btn.setEnabled(True)
        snr = candidate.detector_peak_snr or 0.0
        confidence = candidate.detector_confidence or 0.0
        warning = " LOW SNR — inspect the orange boxes." if snr < MIN_DETECTION_SNR else ""
        self.status_label.setText(
            f"Three bright spots proposed (SNR {snr:.1f}, confidence "
            f"{confidence:.2f}).{warning} Click ‘Use detected ROI’ to confirm; "
            "this geometry match does not itself prove the surface is 1×1."
        )
        self._refresh_image()

    def _accept_candidate(self) -> None:
        if self._candidate_roi is None:
            return
        roi = self._candidate_roi
        self._candidate_roi = None
        self.accept_btn.setEnabled(False)
        self._activate_roi(roi, "defined")

    def _activate_roi(self, roi: RheedRoiDefinition, event: str) -> None:
        previous = self._active_roi
        if previous is not None:
            self.roi_event.emit("superseded", previous, "replaced by a new ROI")
        self._active_roi = roi
        self._candidate_roi = None
        self.clear_roi_btn.setEnabled(True)
        self._reset_history()
        if roi.mode == "manual_rect":
            region = roi.regions[0]
            self.status_label.setText(
                "Manual ROI active: "
                f"x={region.left:.3f}–{region.right:.3f}, "
                f"y={region.top:.3f}–{region.bottom:.3f}."
            )
        else:
            self.status_label.setText(
                "Confirmed three-spot ROI active: three fixed boxes, union-summed once per capture."
            )
        self.roi_event.emit(event, roi, "")
        self._refresh_image()
        if self._latest_state is not None:
            self._measure_state(self._latest_state)

    def clear_roi(self) -> None:
        previous = self._active_roi or self._candidate_roi
        self.image_view.cancel_selection()
        self._active_roi = None
        self._candidate_roi = None
        self._manual_selection_state = None
        self.accept_btn.setEnabled(False)
        self.clear_roi_btn.setEnabled(False)
        self._reset_history()
        self.status_label.setText(
            "No ROI. Draw one rectangle or detect a three-spot candidate."
        )
        if previous is not None:
            self.roi_event.emit("cleared", previous, "cleared by operator")
        self._refresh_image()

    def invalidate_roi(self, reason: str) -> None:
        previous = self._active_roi
        self.image_view.cancel_selection()
        self._active_roi = None
        self._candidate_roi = None
        self._manual_selection_state = None
        self.accept_btn.setEnabled(False)
        self.clear_roi_btn.setEnabled(False)
        self._reset_history()
        self.status_label.setText(f"ROI invalidated: {reason}. Select it again.")
        if previous is not None:
            self.roi_event.emit("invalidated", previous, reason)
        self._refresh_image()

    def on_camera_state(self, state: CameraState) -> None:
        self._latest_state = state
        if not state.connected or not state.valid or state.frame is None:
            self._manual_selection_state = None
            if self._active_roi is not None:
                self.invalidate_roi(state.error or "RHEED capture unavailable")
            self.image_view.clear_frame()
            self.value_label.setText("Sum: —   Mean: —   Pixels: —   Δ: —")
            return

        if self._active_roi is not None:
            reason = self._active_roi.compatibility_error(state, np.asarray(state.frame))
            if reason:
                self.invalidate_roi(reason)
        if self._candidate_roi is not None:
            reason = self._candidate_roi.compatibility_error(state, np.asarray(state.frame))
            if reason:
                self._candidate_roi = None
                self.accept_btn.setEnabled(False)
                self.status_label.setText(f"Detected ROI candidate discarded: {reason}.")

        self._refresh_image()
        self._measure_state(state)

    def _measure_state(self, state: CameraState) -> None:
        roi = self._active_roi
        if roi is None:
            return
        key = (
            str(state.capture_backend or ""),
            int(state.source_hwnd or 0),
            str(state.captured_at_utc or ""),
            int(state.capture_sequence or state.sample_sequence or state.frame_number),
        )
        if key == self._last_sample_key:
            return
        try:
            sample = measure_camera_state(
                state, roi, baseline_sum=self._baseline_sum,
            )
        except ValueError as exc:
            self.invalidate_roi(str(exc))
            return
        if self._baseline_sum is None:
            self._baseline_sum = sample.intensity_sum
            sample = replace(sample, relative_change_pct=0.0)
        self._last_sample_key = key
        self._latest_sample = sample
        if self._t0_ns is None:
            self._t0_ns = sample.captured_monotonic_ns
        elapsed_min = max(
            0.0, (sample.captured_monotonic_ns - self._t0_ns) / 60_000_000_000.0,
        )
        self._times.append(elapsed_min)
        self._intensities.append(sample.intensity_sum)
        self._curve.setData(list(self._times), list(self._intensities))
        self.value_label.setText(
            f"Sum: {sample.intensity_sum:.3f} a.u.   "
            f"Mean: {sample.intensity_mean:.3f}   "
            f"Pixels: {sample.pixel_count}   "
            f"Δ: {sample.relative_change_pct:+.3f}%   "
            f"Seq: {sample.capture_sequence}"
        )
        self.measurement_ready.emit(sample)

    def _refresh_image(self) -> None:
        state = self._latest_state
        if (
            state is None
            or state.frame is None
            or not state.connected
            or not state.valid
            or self.image_view.is_selecting
        ):
            return
        try:
            self.image_view.set_frame(
                np.asarray(state.frame),
                active=self._active_roi,
                candidate=self._candidate_roi,
            )
        except ValueError as exc:
            self.status_label.setText(f"RHEED preview unavailable: {exc}")

    def _reset_history(self) -> None:
        self._t0_ns = None
        self._baseline_sum = None
        self._last_sample_key = None
        self._latest_sample = None
        self._times.clear()
        self._intensities.clear()
        self._curve.setData([], [])
        self.value_label.setText("Sum: —   Mean: —   Pixels: —   Δ: —")

    def reset(self) -> None:
        """Clear the plot while retaining a still-compatible ROI."""
        self._reset_history()
        if self._active_roi is not None and self._latest_state is not None:
            self._measure_state(self._latest_state)


__all__ = ["RheedIntensityWindow", "RheedRoiImageView"]
