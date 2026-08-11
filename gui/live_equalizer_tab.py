"""Live, grower-reviewed RHEED Equalizer with fail-closed alignment.

The simulator basis is canonical.  A calibration maps the four supported
simulator classes into one immutable camera-frame snapshot; HTR is deliberately
unavailable until it has a canonical basis.  Detection only proposes alignment
candidates.  GrowthApp remains the authority that accepts or invalidates a
calibration.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPainter, QPixmap, QTransform
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from gui.equalizer_alignment import (
    ACTIVE_SIMULATOR_LABELS,
    AlignmentCandidate,
    BasisBundle,
    CalibrationRecord,
    LandmarkDetection,
    PROCESS_H,
    PROCESS_W,
    PROCESS_WH,
    RheedFrameSnapshot,
    calibration_is_stale,
    detect_basis_landmarks,
    detect_live_landmarks,
    enumerate_alignment_candidates,
    validate_manual_landmarks,
    warp_basis_bundle,
)
from gui.equalizer_label_contract import (
    build_equalizer_payload,
    fit_residual_rms,
)
from gui.state import ClassifierState


_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


CLASS_LABELS = [*ACTIVE_SIMULATOR_LABELS, "HTR"]
CLASSIFIER_LABEL_MAP = {
    "1x1": "1x1",
    "Twinned (2x1)": "Tw(2x1)",
    "c(6x2)": "c(6x2)",
    "rt13xrt13": "RT13",
    "HTR": "HTR",
}
MAX_SNAPSHOT_AGE_MS = 3000.0


class LiveEqualizerTab(QWidget):
    """Live Equalizer UI; accepted calibration ownership stays in GrowthApp."""

    live_label_save_requested = pyqtSignal(dict)
    calibration_accept_requested = pyqtSignal(object)
    calibration_invalidation_requested = pyqtSignal(str)

    ZOOM_MIN = 0.5
    ZOOM_MAX = 3.0
    ZOOM_STEP = 0.1

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        retrospective: bool = False,
    ):
        super().__init__(parent)
        self._retrospective = bool(retrospective)
        self._basis_bundle: Optional[BasisBundle] = None
        self._basis: dict[str, np.ndarray] = {}
        self._basis_error: Optional[str] = None
        self._basis_detection: Optional[LandmarkDetection] = None

        self._current_snapshot: Optional[RheedFrameSnapshot] = None
        self._calibration_snapshot: Optional[RheedFrameSnapshot] = None
        self._accepted_calibration: Optional[CalibrationRecord] = None
        self._pending_calibration: Optional[CalibrationRecord] = None
        self._candidates: tuple[AlignmentCandidate, ...] = ()
        self._selected_candidate: Optional[AlignmentCandidate] = None
        self._warped_basis: Optional[dict[str, np.ndarray]] = None
        self._valid_mask: Optional[np.ndarray] = None
        self._preview_basis_label = "1x1"

        self._session_id = ""
        self._session_save_enabled = False
        self._qc_context = {
            "session_active": False,
            "view_segment_id": None,
            "visual_history_generation": 0,
            "gun_aligned": None,
            "realignment_active": False,
        }

        self._sliders: dict[str, QSlider] = {}
        self._slider_value_labels: dict[str, QLabel] = {}
        self._classifier_value_labels: dict[str, QLabel] = {}
        self._adjusting = False
        self._paused = False
        self._calibrating = False
        self._manual_clicks: list[tuple[float, float]] = []
        self._zoom = 1.0
        self._brightness = 0.0
        self._contrast = 1.0
        self._fit_mode = "manual"
        self._raw_fit_weights: Optional[dict[str, float]] = None
        self._normalization_applied = False

        self._build_ui()
        self._load_basis()
        self._reset_to_uniform()
        self._gate_timer = QTimer(self)
        self._gate_timer.setInterval(500)
        self._gate_timer.timeout.connect(self._update_button_states)
        self._gate_timer.start()

    # ----- Basis ---------------------------------------------------------

    def _load_basis(self) -> None:
        try:
            from scripts.equalizer_ui import load_basis_bundle

            bundle = load_basis_bundle()
            if not isinstance(bundle, BasisBundle):
                raise TypeError("load_basis_bundle did not return BasisBundle")
            self._basis_bundle = bundle
            self._basis = bundle.active_images()
            self._basis_detection = detect_basis_landmarks(self._basis["1x1"])
            if not self._basis_detection.success:
                self._basis_error = (
                    "Canonical 1x1 landmark detection failed: "
                    f"{self._basis_detection.reason}"
                )
        except Exception as exc:
            self._basis_error = f"Basis load failed: {exc}"
            self._basis_bundle = None
            self._basis = {}
            self._basis_detection = None
        if self._basis_error:
            self._show_scene_text(self._constructed_scene, self._basis_error)

    # ----- Rendering, zoom, and contrast --------------------------------

    @property
    def _view_scale(self) -> float:
        try:
            from scripts.equalizer_ui import DISPLAY_WH

            return DISPLAY_WH[0] / PROCESS_W
        except Exception:
            return 520 / PROCESS_W

    def _apply_zoom(self) -> None:
        transform = QTransform()
        transform.scale(self._view_scale * self._zoom, self._view_scale * self._zoom)
        self._selected_view.setTransform(transform)
        self._constructed_view.setTransform(transform)
        pct = int(round(self._zoom * 100))
        self._selected_group.setTitle(f"Selected (live RHEED) — {pct}%")
        if self._calibrating and self._selected_candidate is not None:
            self._constructed_group.setTitle(
                f"Alignment: live green / {self._preview_basis_label} magenta "
                f"— {pct}%"
            )
        else:
            self._constructed_group.setTitle(f"Constructed / alignment — {pct}%")

    def _set_zoom(self, value: float) -> None:
        value = round(value / self.ZOOM_STEP) * self.ZOOM_STEP
        self._zoom = max(self.ZOOM_MIN, min(self.ZOOM_MAX, value))
        self._apply_zoom()

    @staticmethod
    def _set_scene_rgb(scene: QGraphicsScene, rgb: np.ndarray) -> None:
        rgb = np.ascontiguousarray(np.clip(rgb, 0, 255).astype(np.uint8))
        height, width, _ = rgb.shape
        image = QImage(
            rgb.data, width, height, width * 3, QImage.Format.Format_RGB888,
        ).copy()
        scene.clear()
        scene.addItem(QGraphicsPixmapItem(QPixmap.fromImage(image)))
        scene.setSceneRect(0, 0, width, height)

    def _render_display(self, scene: QGraphicsScene, array: np.ndarray) -> None:
        from scripts.equalizer_ui import apply_green_palette

        display = np.asarray(array, dtype=np.float32)
        if scene is self._selected_scene:
            display = display * self._contrast + self._brightness
        rgb = apply_green_palette(np.clip(display, 0, 255).astype(np.uint8))
        self._set_scene_rgb(scene, rgb)

    @staticmethod
    def _show_scene_text(scene: QGraphicsScene, text: str) -> None:
        scene.clear()
        item = scene.addText(text)
        item.setDefaultTextColor(Qt.GlobalColor.lightGray)
        rect = item.boundingRect()
        item.setPos((PROCESS_W - rect.width()) / 2, (PROCESS_H - rect.height()) / 2)

    def _refresh_selected_display(self) -> None:
        snapshot = self._calibration_snapshot if self._calibrating else self._current_snapshot
        if snapshot is not None:
            self._render_display(self._selected_scene, snapshot.grayscale)

    def _on_contrast_plus(self) -> None:
        self._contrast = min(4.0, self._contrast + 0.2)
        self._refresh_selected_display()

    def _on_contrast_minus(self) -> None:
        self._contrast = max(0.1, self._contrast - 0.2)
        self._refresh_selected_display()

    def _on_brightness_plus(self) -> None:
        self._brightness = min(255.0, self._brightness + 10.0)
        self._refresh_selected_display()

    def _on_brightness_minus(self) -> None:
        self._brightness = max(-255.0, self._brightness - 10.0)
        self._refresh_selected_display()

    def _on_contrast_reset(self) -> None:
        self._brightness = 0.0
        self._contrast = 1.0
        self._refresh_selected_display()

    # ----- UI ------------------------------------------------------------

    def _make_view(self, scene: QGraphicsScene, object_name: str) -> QGraphicsView:
        view = QGraphicsView(scene)
        view.setObjectName(object_name)
        view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setFrameShape(QGraphicsView.Shape.NoFrame)
        view.setStyleSheet(
            "QGraphicsView { background-color: #111; border: 1px solid #333; }"
        )
        view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        view.viewport().installEventFilter(self)
        return view

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        title = QLabel(
            "Live RHEED Equalizer — calibrate camera geometry, review the "
            "overlay, then fit or label the frozen provenance-bound frame."
        )
        title.setStyleSheet("font-size: 12px; color: #aaa;")
        outer.addWidget(title)

        images = QHBoxLayout()
        self._selected_group = QGroupBox("Selected (live RHEED) — 100%")
        selected_layout = QVBoxLayout(self._selected_group)
        contrast_row = QHBoxLayout()
        for text, name, handler, tip in (
            ("C−", "contrastMinusButton", self._on_contrast_minus, "Decrease contrast"),
            ("C+", "contrastPlusButton", self._on_contrast_plus, "Increase contrast"),
            ("B−", "brightnessMinusButton", self._on_brightness_minus, "Decrease brightness"),
            ("B+", "brightnessPlusButton", self._on_brightness_plus, "Increase brightness"),
            ("Reset", "contrastResetButton", self._on_contrast_reset, "Reset display contrast"),
        ):
            button = QPushButton(text)
            button.setObjectName(name)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            contrast_row.addWidget(button)
        contrast_row.addStretch(1)
        selected_layout.addLayout(contrast_row)
        self._selected_scene = QGraphicsScene(self)
        self._selected_scene.setSceneRect(0, 0, PROCESS_W, PROCESS_H)
        self._selected_view = self._make_view(self._selected_scene, "selectedRheedView")
        selected_layout.addWidget(self._selected_view, 1)
        images.addWidget(self._selected_group, 1)

        self._constructed_group = QGroupBox("Constructed / alignment — 100%")
        constructed_layout = QVBoxLayout(self._constructed_group)
        self._constructed_scene = QGraphicsScene(self)
        self._constructed_scene.setSceneRect(0, 0, PROCESS_W, PROCESS_H)
        self._constructed_view = self._make_view(
            self._constructed_scene, "constructedRheedView",
        )
        constructed_layout.addWidget(self._constructed_view, 1)
        images.addWidget(self._constructed_group, 1)
        outer.addLayout(images, 1)

        bottom = QHBoxLayout()
        classifier_group = QGroupBox("Classifier % breakdown")
        classifier_layout = QGridLayout(classifier_group)
        for row, label in enumerate(CLASS_LABELS):
            classifier_layout.addWidget(QLabel(label), row, 0)
            value = QLabel("—")
            value.setObjectName(f"classifier_{label}_value")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            value.setStyleSheet("color: #0d9488; font-weight: bold;")
            classifier_layout.addWidget(value, row, 1)
            self._classifier_value_labels[label] = value
        bottom.addWidget(classifier_group, 1)

        grower_group = QGroupBox("Grower % breakdown")
        grower_layout = QGridLayout(grower_group)
        grower_layout.setColumnStretch(1, 1)
        for row, label in enumerate(CLASS_LABELS):
            display_name = label if label != "HTR" else "HTR (canonical pending)"
            grower_layout.addWidget(QLabel(display_name), row, 0)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setObjectName(f"grower_{label}_slider")
            slider.setRange(0, 100)
            slider.setTickInterval(10)
            slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            slider.valueChanged.connect(self._on_slider_changed)
            grower_layout.addWidget(slider, row, 1)
            value = QLabel("0%")
            value.setObjectName(f"grower_{label}_value")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            value.setStyleSheet("color: #d97706; font-weight: bold;")
            grower_layout.addWidget(value, row, 2)
            self._sliders[label] = slider
            self._slider_value_labels[label] = value
        htr = self._sliders["HTR"]
        htr.setValue(0)
        htr.setEnabled(False)
        htr.setToolTip("N/A — canonical HTR basis pending")
        self._slider_value_labels["HTR"].setText("N/A")
        self._slider_value_labels["HTR"].setToolTip("Canonical HTR basis pending")

        candidate_row = QHBoxLayout()
        candidate_row.addWidget(QLabel("Alignment candidate:"))
        self._candidate_combo = QComboBox()
        self._candidate_combo.setObjectName("alignmentCandidateSelector")
        self._candidate_combo.currentIndexChanged.connect(self._on_candidate_changed)
        self._candidate_combo.hide()
        candidate_row.addWidget(self._candidate_combo, 1)
        self._cal_status_label = QLabel("Not calibrated — Auto-fit and Save disabled")
        self._cal_status_label.setObjectName("alignmentStatusLabel")
        self._cal_status_label.setStyleSheet("color: #d97706; font-size: 11px;")
        candidate_row.addWidget(self._cal_status_label, 2)
        grower_layout.addLayout(candidate_row, len(CLASS_LABELS), 0, 1, 3)

        evidence_row = QHBoxLayout()
        evidence_row.addWidget(QLabel("Overlay basis:"))
        self._preview_basis_combo = QComboBox()
        self._preview_basis_combo.setObjectName("alignmentPreviewBasisSelector")
        for label in ACTIVE_SIMULATOR_LABELS:
            self._preview_basis_combo.addItem(label, label)
        self._preview_basis_combo.currentIndexChanged.connect(
            self._on_preview_basis_changed,
        )
        self._preview_basis_combo.setToolTip(
            "Select the canonical reconstruction shown in magenta over the "
            "frozen live frame."
        )
        evidence_row.addWidget(self._preview_basis_combo)

        evidence_row.addWidget(QLabel("Handedness evidence:"))
        self._handedness_evidence_combo = QComboBox()
        self._handedness_evidence_combo.setObjectName(
            "handednessEvidenceSelector",
        )
        self._handedness_evidence_combo.addItem(
            "Select asymmetric evidence…", "",
        )
        for label in ACTIVE_SIMULATOR_LABELS[1:]:
            self._handedness_evidence_combo.addItem(label, label)
        self._handedness_evidence_combo.addItem(
            "Clear live streak / tail", "streak/tail",
        )
        self._handedness_evidence_combo.currentIndexChanged.connect(
            self._on_handedness_evidence_changed,
        )
        evidence_row.addWidget(self._handedness_evidence_combo)

        self._handedness_confirm = QCheckBox(
            "I confirm this asymmetric evidence establishes handedness",
        )
        self._handedness_confirm.setObjectName(
            "handednessEvidenceConfirmation",
        )
        self._handedness_confirm.setToolTip(
            "Required before acceptance. A symmetric 1x1 triplet alone is "
            "not handedness evidence."
        )
        self._handedness_confirm.toggled.connect(
            self._on_handedness_confirmation_toggled,
        )
        self._preview_basis_combo.setEnabled(False)
        self._handedness_evidence_combo.setEnabled(False)
        self._handedness_confirm.setEnabled(False)
        evidence_row.addWidget(self._handedness_confirm, 1)
        grower_layout.addLayout(
            evidence_row, len(CLASS_LABELS) + 1, 0, 1, 3,
        )

        buttons = QHBoxLayout()
        self._auto_fit_btn = QPushButton("Auto-fit")
        self._auto_fit_btn.setObjectName("autoFitButton")
        self._auto_fit_btn.clicked.connect(self._on_auto_fit)
        buttons.addWidget(self._auto_fit_btn)
        self._normalize_btn = QPushButton("Normalize")
        self._normalize_btn.setObjectName("normalizeButton")
        self._normalize_btn.clicked.connect(self._on_normalize)
        buttons.addWidget(self._normalize_btn)
        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setObjectName("resetWeightsButton")
        self._reset_btn.clicked.connect(self._reset_to_uniform)
        buttons.addWidget(self._reset_btn)
        self._pause_btn = QPushButton("Freeze frame")
        self._pause_btn.setObjectName("freezeFrameButton")
        self._pause_btn.setCheckable(True)
        self._pause_btn.toggled.connect(self._on_pause_toggled)
        self._pause_btn.setStyleSheet(
            "QPushButton:checked { background-color: #d97706; color: white; }"
        )
        buttons.addWidget(self._pause_btn)
        self._calibrate_btn = QPushButton("Calibrate")
        self._calibrate_btn.setObjectName("calibrateButton")
        self._calibrate_btn.clicked.connect(self._on_calibrate_clicked)
        buttons.addWidget(self._calibrate_btn)
        self._clear_cal_btn = QPushButton("Clear Cal")
        self._clear_cal_btn.setObjectName("clearCalibrationButton")
        self._clear_cal_btn.clicked.connect(self._on_clear_cal_clicked)
        self._clear_cal_btn.hide()
        buttons.addWidget(self._clear_cal_btn)
        buttons.addStretch(1)
        buttons.addWidget(QLabel("Confidence:"))
        self._label_confidence_combo = QComboBox()
        self._label_confidence_combo.setObjectName("equalizerLabelConfidence")
        self._label_confidence_combo.addItem("not rated", "")
        self._label_confidence_combo.addItem("low", "0.5")
        self._label_confidence_combo.addItem("medium", "0.75")
        self._label_confidence_combo.addItem("high", "1.0")
        buttons.addWidget(self._label_confidence_combo)
        self._save_btn = QPushButton("Save label")
        self._save_btn.setObjectName("saveLiveLabelButton")
        self._save_btn.clicked.connect(self._on_save)
        self._save_btn.setStyleSheet(
            "QPushButton { background-color: #0d9488; color: white; "
            "font-weight: bold; padding: 6px 16px; }"
            "QPushButton:disabled { background-color: #222; color: #666; }"
        )
        buttons.addWidget(self._save_btn)
        grower_layout.addLayout(buttons, len(CLASS_LABELS) + 2, 0, 1, 3)
        bottom.addWidget(grower_group, 2)
        outer.addLayout(bottom, 1)

        self._show_scene_text(self._selected_scene, "Waiting for live camera stream…")
        self._show_scene_text(self._constructed_scene, "Calibration required")
        self._apply_zoom()

    # ----- Public camera and lifecycle interface ------------------------

    def update_camera_frame(
        self, frame: Optional[np.ndarray], capture_metadata: Optional[dict] = None,
    ) -> None:
        if frame is None:
            return
        metadata = dict(capture_metadata or {})
        try:
            from PIL import Image

            raw = np.asarray(frame)
            if raw.ndim == 2:
                rgb = np.repeat(raw[..., None], 3, axis=2)
            elif raw.ndim == 3 and raw.shape[2] >= 3:
                rgb = raw[..., :3]
            else:
                return
            rgb = np.clip(rgb, 0, 255).astype(np.uint8)
            processed = Image.fromarray(rgb).convert("L").resize(
                PROCESS_WH, Image.Resampling.LANCZOS,
            )
            gray = np.asarray(processed, dtype=np.float32)
        except Exception:
            return

        received_ns = int(
            metadata.get("received_monotonic_ns")
            or metadata.get("captured_monotonic_ns")
            or 0
        )
        height, width = rgb.shape[:2]
        incoming_snapshot = RheedFrameSnapshot.freeze(
            rgb,
            gray,
            captured_at_utc=str(metadata.get("captured_at_utc", "")),
            received_monotonic_ns=received_ns,
            capture_sequence=int(metadata.get("capture_sequence") or 0),
            source_hwnd=int(metadata.get("source_hwnd") or 0),
            capture_backend=str(metadata.get("capture_backend", "")),
            capture_geometry_id=str(metadata.get("capture_geometry_id", "")),
            camera_width=int(metadata.get("camera_width") or width),
            camera_height=int(metadata.get("camera_height") or height),
            session_id=self._session_id,
            view_segment_id=(
                None
                if metadata.get(
                    "view_segment_id", self._qc_context["view_segment_id"],
                ) is None
                else int(metadata.get(
                    "view_segment_id", self._qc_context["view_segment_id"],
                ))
            ),
            visual_history_generation=int(
                metadata.get(
                    "visual_history_generation",
                    self._qc_context["visual_history_generation"],
                ),
            ),
            gun_aligned=self._qc_context["gun_aligned"] is True,
            realignment_active=bool(self._qc_context["realignment_active"]),
            source_frame_age_ms=(
                float(metadata["frame_age_ms"])
                if self._retrospective and metadata.get("frame_age_ms") not in (None, "")
                else None
            ),
            retrospective=self._retrospective,
        )
        # Freeze only the grower's displayed/labeling frame.  Provenance from
        # every incoming frame must still be observed so a DPI/resize/backend
        # discontinuity cannot remain hidden while the UI is paused.
        if self._paused:
            self._invalidate_if_incompatible(incoming_snapshot)
            self._update_button_states()
            return
        self._current_snapshot = incoming_snapshot
        self._invalidate_if_incompatible(incoming_snapshot)
        if not self._calibrating:
            self._render_display(self._selected_scene, gray)
        self._update_button_states()

    def get_current_snapshot(self) -> Optional[RheedFrameSnapshot]:
        return self._current_snapshot

    def get_basis_bundle_id(self) -> str:
        return self._basis_bundle.bundle_id if self._basis_bundle is not None else ""

    def get_basis_bundle(self) -> Optional[BasisBundle]:
        """Return the immutable canonical bundle used by this component."""
        return self._basis_bundle

    def get_current_full_frame(self) -> Optional[np.ndarray]:
        if self._current_snapshot is None:
            return None
        return np.array(self._current_snapshot.rgb, copy=True)

    def get_current_capture_metadata(self) -> dict:
        snapshot = self._current_snapshot
        if snapshot is None:
            return {}
        calibration = self._accepted_calibration
        return {
            "capture_backend": snapshot.capture_backend,
            "capture_geometry_id": snapshot.capture_geometry_id,
            "captured_at_utc": snapshot.captured_at_utc,
            "captured_monotonic_ns": snapshot.received_monotonic_ns,
            "capture_sequence": snapshot.capture_sequence,
            "frame_age_ms": snapshot.logging_age_ms(),
            "source_hwnd": snapshot.source_hwnd,
            "camera_width": snapshot.camera_width,
            "camera_height": snapshot.camera_height,
            "session_id": snapshot.session_id,
            "view_segment_id": snapshot.view_segment_id,
            "visual_history_generation": snapshot.visual_history_generation,
            "calibration_id": calibration.calibration_id if calibration else "",
            "basis_bundle_id": self.get_basis_bundle_id(),
            "equalizer_active_classes": list(ACTIVE_SIMULATOR_LABELS),
        }

    def clear_camera_frame(self, message: str = "Waiting for live camera stream…") -> None:
        self._current_snapshot = None
        self.invalidate_calibration("camera disconnected", emit=True)
        self._show_scene_text(self._selected_scene, message)
        self._update_button_states()

    def set_save_enabled(self, enabled: bool) -> None:
        """Legacy GrowthApp session gate; QC remains an independent AND gate."""
        self._session_save_enabled = bool(enabled)
        self._update_button_states()

    def set_session_id(self, session_id: str) -> None:
        session_id = str(session_id or "")
        if session_id != self._session_id and (
            self._accepted_calibration is not None or self._calibrating
        ):
            self.invalidate_calibration("session changed", emit=True)
        self._session_id = session_id
        self._update_button_states()

    def update_qc_context(
        self,
        *,
        session_active: bool,
        view_segment_id: Optional[int],
        visual_history_generation: int,
        gun_aligned: Optional[bool],
        realignment_active: bool,
    ) -> None:
        old = dict(self._qc_context)
        new_view_segment_id = (
            None if view_segment_id is None else int(view_segment_id)
        )
        self._qc_context.update({
            "session_active": bool(session_active),
            "view_segment_id": new_view_segment_id,
            "visual_history_generation": int(visual_history_generation),
            "gun_aligned": gun_aligned,
            "realignment_active": bool(realignment_active),
        })
        reason = ""
        if not session_active and not self._retrospective:
            reason = "session ended"
        elif realignment_active and not old["realignment_active"]:
            reason = "gun realignment started"
        elif gun_aligned is not True:
            reason = "gun alignment not confirmed"
        elif new_view_segment_id != old["view_segment_id"]:
            reason = "view segment changed"
        elif int(visual_history_generation) != int(old["visual_history_generation"]):
            reason = "visual-history generation changed"
        if reason and (self._accepted_calibration is not None or self._calibrating):
            self.invalidate_calibration(reason, emit=True)
        else:
            self._invalidate_if_incompatible()
        self._update_button_states()

    def reset_for_new_session(self) -> None:
        self.invalidate_calibration("session reset", emit=True)
        self._current_snapshot = None
        self._session_id = ""
        self._session_save_enabled = False
        self._qc_context.update({
            "session_active": False,
            "gun_aligned": None,
            "realignment_active": False,
        })
        for label in self._classifier_value_labels.values():
            label.setText("—")
        self._pause_btn.setChecked(False)
        self._reset_to_uniform()
        self._show_scene_text(self._selected_scene, "Waiting for live camera stream…")
        self._update_button_states()

    # ----- Classifier and sliders ---------------------------------------

    def update_classifier_state(self, state: Optional[ClassifierState]) -> None:
        if state is None or not state.smoothed_percent:
            for label in self._classifier_value_labels.values():
                label.setText("—")
            return
        for source, target in CLASSIFIER_LABEL_MAP.items():
            value = state.smoothed_percent.get(source)
            self._classifier_value_labels[target].setText(
                "—" if value is None else f"{int(value)}%",
            )

    def _on_slider_changed(self) -> None:
        if not self._adjusting:
            if self._fit_mode == "least_squares":
                self._fit_mode = "least_squares_then_manual"
            elif self._fit_mode != "least_squares_then_manual":
                self._fit_mode = "manual"
                self._raw_fit_weights = None
            self._normalization_applied = False
            self._refresh_slider_labels()
            self._update_reconstruction()

    def _refresh_slider_labels(self) -> None:
        for label in ACTIVE_SIMULATOR_LABELS:
            self._slider_value_labels[label].setText(f"{self._sliders[label].value()}%")
        self._slider_value_labels["HTR"].setText("N/A")

    def _current_weights(self) -> dict[str, Optional[float]]:
        weights: dict[str, Optional[float]] = {
            label: self._sliders[label].value() / 100.0
            for label in ACTIVE_SIMULATOR_LABELS
        }
        weights["HTR"] = None
        return weights

    def _set_weights(self, weights: dict[str, Optional[float]]) -> None:
        self._adjusting = True
        try:
            for label in ACTIVE_SIMULATOR_LABELS:
                value = weights.get(label, 0.0)
                self._sliders[label].setValue(int(round(float(value or 0.0) * 100)))
            self._sliders["HTR"].setValue(0)
        finally:
            self._adjusting = False
        self._refresh_slider_labels()
        self._update_reconstruction()

    def _reset_to_uniform(self) -> None:
        uniform = 1.0 / len(ACTIVE_SIMULATOR_LABELS)
        self._set_weights({label: uniform for label in ACTIVE_SIMULATOR_LABELS})
        self._fit_mode = "manual"
        self._raw_fit_weights = None
        self._normalization_applied = False

    def _on_normalize(self) -> None:
        weights = self._current_weights()
        total = sum(float(weights[label] or 0.0) for label in ACTIVE_SIMULATOR_LABELS)
        if total > 0:
            self._set_weights({
                label: float(weights[label] or 0.0) / total
                for label in ACTIVE_SIMULATOR_LABELS
            })
            self._normalization_applied = True

    def _update_reconstruction(self) -> None:
        if not self._warped_basis:
            if not self._basis_error:
                self._show_scene_text(self._constructed_scene, "Calibration required")
            return
        weights = self._current_weights()
        reconstruction = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32)
        for label in ACTIVE_SIMULATOR_LABELS:
            reconstruction += float(weights[label] or 0.0) * self._warped_basis[label]
        self._render_display(self._constructed_scene, reconstruction)

    def _on_auto_fit(self) -> None:
        ok, _ = self._action_gate(require_calibration=True)
        if not ok or self._current_snapshot is None or not self._warped_basis:
            self._update_button_states()
            return
        from scripts.equalizer_ui import auto_fit_details

        raw, fitted = auto_fit_details(
            self._warped_basis, self._current_snapshot.grayscale,
        )
        self._set_weights(fitted)
        self._raw_fit_weights = {
            label: max(0.0, float(raw.get(label, 0.0)))
            for label in ACTIVE_SIMULATOR_LABELS
        }
        self._fit_mode = "least_squares"
        self._normalization_applied = True

    def _equalizer_label_payload(self) -> dict[str, object]:
        """Bind UI values and fit diagnostics without assigning a primary type."""
        if (
            self._current_snapshot is None
            or self._warped_basis is None
            or self._valid_mask is None
        ):
            raise ValueError("Equalizer payload requires a calibrated frozen frame")
        weights = self._current_weights()
        residual = fit_residual_rms(
            self._warped_basis,
            self._current_snapshot.grayscale,
            weights,
            self._valid_mask,
        )
        return build_equalizer_payload(
            raw_weights=self._raw_fit_weights,
            final_weights=weights,
            fit_mode=self._fit_mode,
            normalization_applied=self._normalization_applied,
            residual_rms=residual,
            valid_coverage=float(np.mean(self._valid_mask)),
            confidence=self._label_confidence_combo.currentData() or "",
        )

    def _on_save(self) -> None:
        ok, reason = self._action_gate(require_calibration=True)
        if not ok:
            self._set_status(reason, error=True)
            self._update_button_states()
            return
        try:
            payload = self._equalizer_label_payload()
        except (TypeError, ValueError) as exc:
            self._set_status(f"Label payload invalid: {exc}", error=True)
            return
        self.live_label_save_requested.emit(payload)

    def _on_pause_toggled(self, checked: bool) -> None:
        self._paused = bool(checked)
        self._pause_btn.setText("Resume live" if checked else "Freeze frame")

    # ----- Calibration ---------------------------------------------------

    def get_calibration(self) -> Optional[CalibrationRecord]:
        return self._accepted_calibration

    def get_calibration_snapshot(self) -> Optional[RheedFrameSnapshot]:
        """Return the immutable frame currently backing calibration review.

        GrowthApp reads this synchronously when handling
        ``calibration_accept_requested`` so it can persist and hash the exact
        evidence frame before activating the accepted record.
        """
        return self._calibration_snapshot

    def get_handedness_evidence_kind(self) -> str:
        """Return confirmed asymmetric evidence, or ``""`` if unconfirmed."""
        candidate = self._selected_candidate
        evidence = str(self._handedness_evidence_combo.currentData() or "")
        if (
            not self._calibrating
            or candidate is None
            or not candidate.valid
            or not evidence
            or not self._handedness_confirm.isChecked()
        ):
            return ""
        # Reconstruction evidence is meaningful only for the exact canonical
        # basis shown in the green/magenta review overlay.  A grower may still
        # use a visibly asymmetric streak/tail as independent evidence.
        if evidence != "streak/tail" and evidence != self._preview_basis_label:
            return ""
        return evidence

    def get_handedness_evidence(self) -> dict[str, object]:
        """Expose review context without granting this widget acceptance."""
        candidate = self._selected_candidate
        evidence_kind = self.get_handedness_evidence_kind()
        return {
            "confirmed": bool(evidence_kind),
            "kind": evidence_kind,
            "preview_basis_label": self._preview_basis_label,
            "candidate_id": candidate.candidate_id if candidate is not None else "",
            "parity": candidate.parity if candidate is not None else "",
            "endpoint_order": (
                candidate.endpoint_order if candidate is not None else ""
            ),
        }

    def has_valid_calibration(self) -> bool:
        ok, _ = self._action_gate(require_calibration=True, require_session=False)
        return ok

    def set_accepted_calibration(self, record: CalibrationRecord) -> bool:
        """Activate only an app-accepted, provenance-compatible record."""
        if not isinstance(record, CalibrationRecord) or not record.grower_accepted:
            self._set_status("Application did not provide an accepted calibration", error=True)
            return False
        if self._basis_bundle is None or self._current_snapshot is None:
            self._set_status("Basis or camera snapshot is unavailable", error=True)
            return False
        stale, reason = self._record_staleness(record)
        if stale:
            self.invalidate_calibration(reason, emit=False)
            return False
        try:
            warped, valid = warp_basis_bundle(self._basis_bundle, record.matrix)
        except Exception as exc:
            self._set_status(f"Alignment warp failed: {exc}", error=True)
            return False
        self._accepted_calibration = record
        self._pending_calibration = None
        self._warped_basis = warped
        self._valid_mask = valid
        self._calibrating = False
        self._candidates = ()
        self._selected_candidate = None
        self._calibration_snapshot = None
        self._manual_clicks.clear()
        self._candidate_combo.clear()
        self._candidate_combo.hide()
        self._selected_view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._selected_view.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        self._refresh_selected_display()
        self._update_reconstruction()
        self._update_button_states()
        return True

    def invalidate_calibration(self, reason: str, *, emit: bool = False) -> None:
        reason = str(reason or "calibration invalidated")
        had_state = (
            self._accepted_calibration is not None
            or self._pending_calibration is not None
            or self._calibrating
        )
        self._accepted_calibration = None
        self._pending_calibration = None
        self._warped_basis = None
        self._valid_mask = None
        self._calibrating = False
        self._calibration_snapshot = None
        self._candidates = ()
        self._selected_candidate = None
        self._manual_clicks.clear()
        self._candidate_combo.clear()
        self._candidate_combo.hide()
        self._reset_handedness_review(reset_choices=True)
        self._selected_view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._selected_view.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        self._refresh_selected_display()
        self._update_reconstruction()
        self._set_status(reason, error=True)
        self._update_button_states()
        if emit and had_state:
            self.calibration_invalidation_requested.emit(reason)

    def clear_calibration(self) -> None:
        self.invalidate_calibration("calibration cleared by grower", emit=True)

    @staticmethod
    def _is_local_demo_snapshot(snapshot: RheedFrameSnapshot) -> bool:
        """Return whether a frame comes from a repository-backed dummy camera."""
        backend = str(snapshot.capture_backend or "")
        return backend == "dummy" or backend.startswith("dummy_")

    def _record_staleness(
        self,
        record: CalibrationRecord,
        snapshot: Optional[RheedFrameSnapshot] = None,
    ) -> tuple[bool, str]:
        snapshot = self._current_snapshot if snapshot is None else snapshot
        if snapshot is None or self._basis_bundle is None:
            return True, "camera snapshot or basis unavailable"
        local_demo = self._is_local_demo_snapshot(snapshot)
        if not local_demo and self._qc_context["gun_aligned"] is not True:
            return True, "gun alignment not confirmed"
        if not local_demo and self._qc_context["realignment_active"]:
            return True, "gun realignment active"
        return calibration_is_stale(
            record,
            source_hwnd=snapshot.source_hwnd,
            camera_width=snapshot.camera_width,
            camera_height=snapshot.camera_height,
            capture_backend=snapshot.capture_backend,
            capture_geometry_id=snapshot.capture_geometry_id,
            view_segment_id=(
                snapshot.view_segment_id
                if local_demo else self._qc_context["view_segment_id"]
            ),
            visual_history_generation=(
                snapshot.visual_history_generation
                if local_demo
                else int(self._qc_context["visual_history_generation"])
            ),
            session_id=snapshot.session_id if local_demo else self._session_id,
            basis_bundle_id=self._basis_bundle.bundle_id,
            gun_aligned=(
                snapshot.gun_aligned
                if local_demo else self._qc_context["gun_aligned"] is True
            ),
            realignment_active=(
                snapshot.realignment_active
                if local_demo else bool(self._qc_context["realignment_active"])
            ),
            session_active=(
                True
                if self._retrospective or local_demo
                else bool(self._qc_context["session_active"])
            ),
        )

    @staticmethod
    def _capture_discontinuity(
        reference: RheedFrameSnapshot | CalibrationRecord,
        current: RheedFrameSnapshot,
    ) -> tuple[bool, str]:
        """Compare capture geometry strictly, including missing identities."""
        if int(current.source_hwnd) != int(reference.source_hwnd):
            return (
                True,
                f"camera HWND changed ({reference.source_hwnd} -> "
                f"{current.source_hwnd})",
            )
        if (
            int(current.camera_width) != int(reference.camera_width)
            or int(current.camera_height) != int(reference.camera_height)
        ):
            return True, "camera resolution changed"
        if str(current.capture_backend) != str(reference.capture_backend):
            return (
                True,
                f"capture backend changed ({reference.capture_backend} -> "
                f"{current.capture_backend})",
            )
        if str(current.capture_geometry_id) != str(reference.capture_geometry_id):
            return True, "capture ROI/chrome geometry changed"
        return False, ""

    def _invalidate_if_incompatible(
        self,
        snapshot: Optional[RheedFrameSnapshot] = None,
    ) -> None:
        current = self._current_snapshot if snapshot is None else snapshot
        if current is None:
            return

        reference: RheedFrameSnapshot | CalibrationRecord | None = None
        if self._accepted_calibration is not None:
            reference = self._accepted_calibration
        elif (
            self._calibrating
            or self._pending_calibration is not None
            or self._calibration_snapshot is not None
        ):
            reference = self._calibration_snapshot
            if reference is None:
                self.invalidate_calibration(
                    "frozen calibration snapshot unavailable",
                    emit=True,
                )
                return

        if reference is not None:
            stale, reason = self._capture_discontinuity(reference, current)
            if stale:
                self.invalidate_calibration(reason, emit=True)
                return

        if self._accepted_calibration is not None:
            stale, reason = self._record_staleness(
                self._accepted_calibration,
                current,
            )
            if stale:
                self.invalidate_calibration(reason, emit=True)

    def _action_gate(
        self, *, require_calibration: bool, require_session: bool = True,
    ) -> tuple[bool, str]:
        if self._basis_bundle is None or self._basis_error:
            return False, self._basis_error or "canonical basis unavailable"
        snapshot = self._current_snapshot
        if snapshot is None:
            return False, "fresh RHEED frame required"
        local_demo = self._is_local_demo_snapshot(snapshot)
        if (
            not self._retrospective
            and not local_demo
            and snapshot.age_ms() > MAX_SNAPSHOT_AGE_MS
        ):
            return False, "RHEED frame is stale"
        if self._retrospective:
            if not self._session_id:
                return False, "historical frame session ID required"
        elif not local_demo and require_session and (
            not self._session_save_enabled or not self._qc_context["session_active"]
        ):
            return False, "active growth session required"
        if not local_demo and self._qc_context["gun_aligned"] is not True:
            return False, "gun alignment must be confirmed"
        if not local_demo and self._qc_context["realignment_active"]:
            return False, "gun realignment is active"
        if require_calibration:
            if self._accepted_calibration is None:
                return False, "grower-accepted camera calibration required"
            stale, reason = self._record_staleness(self._accepted_calibration)
            if stale:
                return False, reason
        return True, ""

    def _on_calibrate_clicked(self) -> None:
        if self._selected_candidate is not None:
            self._request_candidate_acceptance()
            return
        if self._calibrating:
            return
        ok, reason = self._action_gate(require_calibration=False, require_session=True)
        if not ok:
            self._set_status(reason, error=True)
            return
        if self._basis_detection is None or not self._basis_detection.success:
            reason = self._basis_detection.reason if self._basis_detection else "missing basis"
            self._set_status(f"Canonical landmark detection failed: {reason}", error=True)
            return
        assert self._current_snapshot is not None
        if self._accepted_calibration is not None:
            self.invalidate_calibration("recalibration started", emit=True)
        self._reset_handedness_review(reset_choices=True)
        source = self._current_snapshot
        self._calibration_snapshot = RheedFrameSnapshot.freeze(
            source.rgb,
            source.grayscale,
            captured_at_utc=source.captured_at_utc,
            received_monotonic_ns=source.received_monotonic_ns,
            capture_sequence=source.capture_sequence,
            source_hwnd=source.source_hwnd,
            capture_backend=source.capture_backend,
            capture_geometry_id=source.capture_geometry_id,
            camera_width=source.camera_width,
            camera_height=source.camera_height,
            session_id=source.session_id,
            view_segment_id=source.view_segment_id,
            visual_history_generation=source.visual_history_generation,
            gun_aligned=source.gun_aligned,
            realignment_active=source.realignment_active,
            source_frame_age_ms=source.source_frame_age_ms,
            retrospective=source.retrospective,
        )
        self._calibrating = True
        self._manual_clicks.clear()
        self._selected_view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._refresh_selected_display()
        detected = detect_live_landmarks(self._calibration_snapshot.grayscale)
        if detected.success:
            self._build_candidates(detected)
        else:
            self._selected_view.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self._set_status(
                f"Automatic detection failed ({detected.reason}); click left, centre, right",
            )
            self._update_button_states()

    def _reset_handedness_review(self, *, reset_choices: bool) -> None:
        """Clear acceptance state whenever candidate/evidence context changes."""
        self._pending_calibration = None
        self._handedness_confirm.blockSignals(True)
        self._handedness_confirm.setChecked(False)
        self._handedness_confirm.blockSignals(False)
        if reset_choices:
            self._preview_basis_combo.blockSignals(True)
            preview_index = self._preview_basis_combo.findData("1x1")
            self._preview_basis_combo.setCurrentIndex(max(0, preview_index))
            self._preview_basis_combo.blockSignals(False)
            self._preview_basis_label = "1x1"
            self._handedness_evidence_combo.blockSignals(True)
            self._handedness_evidence_combo.setCurrentIndex(0)
            self._handedness_evidence_combo.blockSignals(False)

    def _on_preview_basis_changed(self, _index: int) -> None:
        label = str(self._preview_basis_combo.currentData() or "1x1")
        if label not in ACTIVE_SIMULATOR_LABELS:
            label = "1x1"
        self._preview_basis_label = label
        self._reset_handedness_review(reset_choices=False)
        if self._selected_candidate is not None:
            self._render_candidate_preview(self._selected_candidate)
        self._update_button_states()

    def _on_handedness_evidence_changed(self, _index: int) -> None:
        self._reset_handedness_review(reset_choices=False)
        evidence = str(self._handedness_evidence_combo.currentData() or "")
        if evidence in ACTIVE_SIMULATOR_LABELS:
            preview_index = self._preview_basis_combo.findData(evidence)
            if preview_index >= 0:
                self._preview_basis_combo.setCurrentIndex(preview_index)
        self._update_button_states()

    def _on_handedness_confirmation_toggled(self, _checked: bool) -> None:
        self._pending_calibration = None
        self._update_button_states()

    def _build_candidates(self, live: LandmarkDetection | np.ndarray) -> None:
        if self._calibration_snapshot is None or self._basis_bundle is None:
            return
        assert self._basis_detection is not None
        try:
            self._candidates = enumerate_alignment_candidates(
                self._basis_detection,
                live,
                self._basis["1x1"],
                self._calibration_snapshot.grayscale,
            )
        except Exception as exc:
            self._set_status(f"Landmarks rejected: {exc}; click Retry", error=True)
            self._update_button_states()
            return
        self._candidate_combo.blockSignals(True)
        self._candidate_combo.clear()
        for candidate in self._candidates:
            valid = "valid" if candidate.valid else "invalid"
            hypotheses = ", ".join(candidate.equivalent_hypotheses)
            self._candidate_combo.addItem(
                f"{hypotheses} — "
                f"corr {candidate.correlation:.3f}, RMS {candidate.rms_residual_px:.2f}px ({valid})"
            )
        self._candidate_combo.blockSignals(False)
        self._candidate_combo.show()
        if self._candidates:
            self._candidate_combo.setCurrentIndex(0)
            self._select_candidate(0)

    def _on_candidate_changed(self, index: int) -> None:
        self._select_candidate(index)

    def _select_candidate(self, index: int) -> None:
        if not 0 <= index < len(self._candidates) or self._basis_bundle is None:
            self._selected_candidate = None
            return
        self._reset_handedness_review(reset_choices=False)
        candidate = self._candidates[index]
        self._selected_candidate = candidate
        try:
            self._warped_basis, self._valid_mask = warp_basis_bundle(
                self._basis_bundle, candidate,
            )
        except Exception as exc:
            self._warped_basis = None
            self._valid_mask = None
            self._set_status(f"Candidate warp failed: {exc}", error=True)
            return
        self._render_candidate_preview(candidate)
        reason = candidate.validation_reason or "ready for grower review"
        self._set_status(
            f"{candidate.parity}/{candidate.endpoint_order}: rotation "
            f"{candidate.rotation_deg:.1f}°, scale {candidate.scale:.3f}, "
            f"RMS {candidate.rms_residual_px:.2f}px, max "
            f"{candidate.max_residual_px:.2f}px, coverage "
            f"{candidate.valid_coverage:.0%}; {reason}",
            error=not candidate.valid,
        )
        self._update_button_states()

    @staticmethod
    def _draw_line(
        image: np.ndarray,
        start: np.ndarray,
        end: np.ndarray,
        color: tuple[int, int, int],
    ) -> None:
        steps = max(1, int(np.ceil(np.max(np.abs(end - start)))))
        xs = np.rint(np.linspace(start[0], end[0], steps + 1)).astype(int)
        ys = np.rint(np.linspace(start[1], end[1], steps + 1)).astype(int)
        valid = (
            (xs >= 0) & (xs < image.shape[1]) & (ys >= 0) & (ys < image.shape[0])
        )
        image[ys[valid], xs[valid]] = color

    @staticmethod
    def _draw_cross(
        image: np.ndarray, point: np.ndarray, color: tuple[int, int, int], radius: int = 3,
    ) -> None:
        x, y = (int(round(point[0])), int(round(point[1])))
        if 0 <= y < image.shape[0]:
            image[y, max(0, x - radius):min(image.shape[1], x + radius + 1)] = color
        if 0 <= x < image.shape[1]:
            image[max(0, y - radius):min(image.shape[0], y + radius + 1), x] = color

    def _render_candidate_preview(self, candidate: AlignmentCandidate) -> None:
        if self._calibration_snapshot is None or not self._warped_basis:
            return
        live = np.clip(self._calibration_snapshot.grayscale, 0, 255).astype(np.uint8)
        basis_label = (
            self._preview_basis_label
            if self._preview_basis_label in self._warped_basis
            else "1x1"
        )
        basis = np.clip(self._warped_basis[basis_label], 0, 255).astype(np.uint8)
        overlay = np.zeros((PROCESS_H, PROCESS_W, 3), dtype=np.uint8)
        overlay[..., 1] = live
        overlay[..., 0] = basis
        overlay[..., 2] = basis
        for projected, observed in zip(candidate.projected_points, candidate.live_points):
            self._draw_line(overlay, projected, observed, (255, 255, 0))
            self._draw_cross(overlay, projected, (255, 0, 255))
            self._draw_cross(overlay, observed, (0, 255, 0))
        self._set_scene_rgb(self._constructed_scene, overlay)
        self._constructed_group.setTitle(
            f"Alignment: live green / {basis_label} magenta — "
            f"{int(round(self._zoom * 100))}%"
        )

    def _request_candidate_acceptance(self) -> None:
        candidate = self._selected_candidate
        if (
            candidate is None
            or not candidate.valid
            or self._basis_bundle is None
            or self._calibration_snapshot is None
        ):
            self._set_status(
                candidate.validation_reason if candidate else "No valid candidate selected",
                error=True,
            )
            return
        evidence_kind = self.get_handedness_evidence_kind()
        if not evidence_kind:
            self._set_status(
                "Select asymmetric handedness evidence and explicitly confirm it",
                error=True,
            )
            self._update_button_states()
            return
        self._pending_calibration = CalibrationRecord.from_candidate(
            candidate,
            self._basis_bundle,
            self._calibration_snapshot,
            grower_accepted=False,
        )
        self.calibration_accept_requested.emit(self._pending_calibration)
        self._set_status("Acceptance requested; awaiting GrowthApp confirmation")
        self._update_button_states()

    def _on_clear_cal_clicked(self) -> None:
        if self._calibrating:
            # Retry stays bound to the immutable snapshot captured by the
            # original Calibrate action.  New camera callbacks may continue,
            # but cannot silently move the grower's three-point target.
            self._pending_calibration = None
            self._candidates = ()
            self._selected_candidate = None
            self._warped_basis = None
            self._valid_mask = None
            self._manual_clicks.clear()
            self._reset_handedness_review(reset_choices=False)
            self._candidate_combo.clear()
            self._candidate_combo.hide()
            self._selected_view.setDragMode(QGraphicsView.DragMode.NoDrag)
            self._selected_view.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self._refresh_selected_display()
            self._set_status("Retry on frozen frame — click left, centre, right")
            self._update_button_states()
        else:
            self.clear_calibration()

    def _handle_calibration_click(self, viewport_x: float, viewport_y: float) -> None:
        if not self._calibrating or self._selected_candidate is not None:
            return
        point = self._selected_view.mapToScene(int(viewport_x), int(viewport_y))
        xy = (
            max(0.0, min(PROCESS_W - 1.0, float(point.x()))),
            max(0.0, min(PROCESS_H - 1.0, float(point.y()))),
        )
        if len(self._manual_clicks) < 3:
            self._manual_clicks.append(xy)
        self._render_manual_markers()
        if len(self._manual_clicks) == 3:
            points = np.asarray(self._manual_clicks, dtype=np.float64)
            detection = validate_manual_landmarks(
                self._calibration_snapshot.grayscale,
                points,
            )
            if detection.success:
                self._build_candidates(detection)
            else:
                self._set_status(
                    f"Manual landmarks rejected: {detection.reason}",
                    error=True,
                )
                self._update_button_states()
        else:
            self._set_status(
                f"Manual landmarks: {len(self._manual_clicks)}/3 — click left, centre, right",
            )
            self._update_button_states()

    def _render_manual_markers(self) -> None:
        if self._calibration_snapshot is None:
            return
        from scripts.equalizer_ui import apply_green_palette

        rgb = apply_green_palette(
            np.clip(self._calibration_snapshot.grayscale, 0, 255).astype(np.uint8),
        )
        for point in self._manual_clicks:
            self._draw_cross(rgb, np.asarray(point), (255, 255, 0), radius=4)
        self._set_scene_rgb(self._selected_scene, rgb)

    # ----- State/UI gates ------------------------------------------------

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._cal_status_label.setText(text)
        color = "#ef4444" if error else "#d97706"
        self._cal_status_label.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _update_button_states(self) -> None:
        base_ok, base_reason = self._action_gate(
            require_calibration=False, require_session=True,
        )
        calibrated_ok, calibrated_reason = self._action_gate(
            require_calibration=True, require_session=True,
        )
        selected_valid = bool(
            self._selected_candidate is not None and self._selected_candidate.valid
        )
        evidence_selected = bool(
            self._handedness_evidence_combo.currentData()
        )
        evidence_confirmed = bool(self.get_handedness_evidence_kind())
        review_active = self._calibrating and self._selected_candidate is not None
        self._preview_basis_combo.setEnabled(review_active)
        self._handedness_evidence_combo.setEnabled(review_active and selected_valid)
        self._handedness_confirm.setEnabled(
            review_active and selected_valid and evidence_selected
        )
        self._auto_fit_btn.setEnabled(calibrated_ok and not self._calibrating)
        save_context_ok = bool(
            self._retrospective
            or (
                self._session_save_enabled
                and self._qc_context["session_active"]
            )
        )
        self._save_btn.setEnabled(
            calibrated_ok and save_context_ok and not self._calibrating
        )
        self._normalize_btn.setEnabled(bool(self._basis))
        self._reset_btn.setEnabled(bool(self._basis))

        if self._calibrating and self._selected_candidate is not None:
            self._calibrate_btn.setText("Request acceptance")
            self._calibrate_btn.setEnabled(
                selected_valid
                and evidence_confirmed
                and self._pending_calibration is None
            )
            self._clear_cal_btn.setText("Retry")
            self._clear_cal_btn.show()
        elif self._calibrating:
            self._calibrate_btn.setText(f"Clicking ({len(self._manual_clicks)}/3)")
            self._calibrate_btn.setEnabled(False)
            self._clear_cal_btn.setText("Retry")
            self._clear_cal_btn.show()
        elif self._accepted_calibration is not None:
            self._calibrate_btn.setText("Re-calibrate")
            self._calibrate_btn.setEnabled(base_ok)
            self._clear_cal_btn.setText("Clear Cal")
            self._clear_cal_btn.show()
            if calibrated_ok:
                self._cal_status_label.setText(
                    f"Calibrated — {self._accepted_calibration.parity}, "
                    f"ID {self._accepted_calibration.calibration_id[:8]}"
                )
                self._cal_status_label.setStyleSheet("color: #22c55e; font-size: 11px;")
        else:
            self._calibrate_btn.setText("Calibrate")
            self._calibrate_btn.setEnabled(base_ok)
            self._clear_cal_btn.hide()
            if not base_ok and not self._calibrating:
                self._set_status(base_reason or calibrated_reason, error=True)

    def eventFilter(self, watched, event):
        selected_view = getattr(self, "_selected_view", None)
        constructed_view = getattr(self, "_constructed_view", None)
        if selected_view is not None and constructed_view is not None:
            if watched in (selected_view.viewport(), constructed_view.viewport()):
                if (
                    event.type() == QEvent.Type.Wheel
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier
                ):
                    self._set_zoom(
                        self._zoom + self.ZOOM_STEP
                        if event.angleDelta().y() > 0
                        else self._zoom - self.ZOOM_STEP
                    )
                    return True
        if (
            selected_view is not None
            and watched is selected_view.viewport()
            and self._calibrating
            and self._selected_candidate is None
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._handle_calibration_click(event.position().x(), event.position().y())
            return True
        return super().eventFilter(watched, event)
