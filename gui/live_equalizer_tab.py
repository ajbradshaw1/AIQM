"""
Live Equalizer tab — real-time RHEED labeling as a first-class GUI tab.

Ships workstream #4 from the Jul 10 2026 group-meeting queue. Per PI
direction (Jul 10 2026 same-day pivot): NOT a popup / separate window,
but a full tab in the Growth Monitor alongside Monitor / Direct-read /
Events / Scrubber / Session.

Layout — 2×2 grid:
  ┌─────────────────────────┬─────────────────────────┐
  │ Selected (live RHEED)   │ Constructed (blend)    │
  │ [live camera frame]     │ [reconstruction image] │
  ├─────────────────────────┼─────────────────────────┤
  │ Classifier %:           │ Grower %:              │
  │ 1x1        60%          │ 1x1     [═══◆═══] 60% │
  │ Twinned    20%          │ Twinned [═◆═════] 20% │
  │ c(6x2)     10%          │ c(6x2)  [◆═══════] 10% │
  │ rt13xrt13   5%          │ rt13    [◆═══════]  5% │
  │ HTR         5%          │ HTR     [◆═══════]  5% │
  │                         │ [Auto-fit][Norm][Reset]│
  │                         │ [                Save ]│
  └─────────────────────────┴─────────────────────────┘

Reuses ``scripts/equalizer_ui.py`` helpers verbatim:
  - ``PROCESS_WH`` (128×96) — internal processing resolution for the
    basis + target so pixels are comparable.
  - ``DISPLAY_WH`` (520×390) — upscaled display size in both panes.
  - ``load_class_means(target_wh)`` — the 5-class basis (npz cache or
    training-set means).
  - ``auto_fit(means, target)`` — least-squares fit clipped to [0, 1]
    with normalization.
  - ``array_to_pixmap(arr, display_wh)`` — grayscale → phosphor-green
    QPixmap (matches kSA's BGW ramp so growers see the same visual
    identity as the live RHEED they know).

Data flow:
  1. Camera frame → ``update_camera_frame(np.ndarray)`` (called by
     GrowthApp on ``camera_worker.state_updated``). Downsamples to
     PROCESS_WH grayscale + updates the 'Selected' pane.
  2. Classifier state → ``update_classifier_state(ClassifierState)``.
     Populates the 5 read-only classifier % labels. Mirrors the
     Monitor tab's classifier slider values.
  3. Grower drags sliders → constructed pane rebuilds via
     ``_update_reconstruction`` (weighted sum of basis images).
  4. Grower clicks Save → ``live_label_save_requested`` signal fires
     with the weights dict. GrowthApp handles the sensor snapshot +
     ``GrowthLogger.record_live_label`` call.

Design notes:
  - Class label vocabulary matches ``scripts/equalizer_ui.py``
    (``1x1``, ``Tw(2x1)``, ``c(6x2)``, ``RT13``, ``HTR``) NOT the
    Monitor tab's ``RECON_LABELS`` (``Twinned (2x1)``, ``rt13xrt13``).
    The two label sets are the same 5 reconstructions with different
    spellings; ``CLASSIFIER_LABEL_MAP`` bridges them so the classifier's
    smoothed_percent (keyed by RECON_LABELS) drives the classifier %
    display (keyed by our shorter names).
  - Pause via Freeze frame button (Jul 14 2026). When toggled on,
    ``update_camera_frame`` drops incoming frames so the Selected pane
    holds the last-displayed frame. Grower can then balance sliders +
    Save against a stable image. Label + Auto-fit + Save all operate on
    the frozen frame. Amber (#d97706) match the MARK EVENT identity so
    grower-in-the-loop UI has consistent visual cues across tabs.
  - No auto-save. Saving is explicit; the grower decides when the mix
    matches the target.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QPainter
from PyQt6.QtWidgets import (
    QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QSlider,
    QSizePolicy, QVBoxLayout, QWidget,
)

from gui.state import ClassifierState
from gui.equalizer_alignment import (
    Calibration,
    detect_basis_landmarks, detect_live_landmarks,
    compute_similarity, warp_basis_similarity,
    validate_similarity, calibration_is_stale,
    PROCESS_W, PROCESS_H,
)


# scripts/ isn't part of the gui/ package — add repo root to sys.path so
# ``scripts.equalizer_ui`` imports resolve regardless of launch path.
# events_tab.py already uses this pattern for the retrospective launcher.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# 5 reconstruction classes. Labels intentionally match the Equalizer's
# vocabulary (``Tw(2x1)`` short form, ``RT13`` uppercase) not the Monitor
# tab's ``RECON_LABELS`` — see docstring. The CLASSIFIER_LABEL_MAP below
# translates when we consume classifier state.
CLASS_LABELS = ["1x1", "Tw(2x1)", "c(6x2)", "RT13", "HTR"]

# Monitor-tab ``RECON_LABELS`` → Equalizer ``CLASS_LABELS``. The classifier's
# ``smoothed_percent`` keys are the Monitor spellings; the tab's classifier
# % display uses the Equalizer spellings so it visually pairs with the
# grower sliders on the right.
CLASSIFIER_LABEL_MAP = {
    "1x1":            "1x1",
    "Twinned (2x1)":  "Tw(2x1)",
    "c(6x2)":         "c(6x2)",
    "rt13xrt13":      "RT13",
    "HTR":            "HTR",
}


class LiveEqualizerTab(QWidget):
    """Live RHEED Equalizer tab. See module docstring for the full design."""

    live_label_save_requested = pyqtSignal(dict)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._basis: dict[str, np.ndarray] = {}
        self._basis_error: Optional[str] = None
        self._current_target: Optional[np.ndarray] = None
        self._current_full_frame: Optional[np.ndarray] = None
        self._current_capture_metadata: dict = {}
        self._sliders: dict[str, QSlider] = {}
        self._slider_value_labels: dict[str, QLabel] = {}
        self._classifier_value_labels: dict[str, QLabel] = {}
        # Reentrancy guard for the reconstruction update — setting slider
        # values programmatically (e.g. Auto-fit) fires valueChanged for
        # each slider, which recomputes reconstruction each time. Guarding
        # keeps the recompute to once per user action.
        self._adjusting = False
        # Pause state (Jul 14 2026). When True, update_camera_frame drops
        # incoming frames on the floor so the 'Selected' pane holds
        # whatever was last displayed. Grower can then Auto-fit / Save
        # against a stable image. Toggled via _pause_btn's checked state.
        self._paused = False
        # Camera alignment calibration (Jul 30 2026).
        self._calibration: Optional[Calibration] = None
        self._shifted_basis: Optional[dict[str, np.ndarray]] = None
        self._basis_landmarks: Optional[np.ndarray] = None   # (3, 2) PROCESS_WH
        self._camera_width: int = 0
        self._camera_height: int = 0
        # Calibration click state.
        self._calibrating: bool = False
        self._calibration_clicks: list[tuple[float, float]] = []  # PROCESS_WH coords
        # Synchronised zoom (Jul 30 2026).
        self._zoom: float = 1.0
        # Display contrast (Jul 30 2026) — display-only, not applied to data.
        self._brightness: float = 0.0
        self._contrast: float = 1.0

        self._build_ui()
        self._load_basis()
        self._reset_to_uniform()

    # ----- Basis loading --------------------------------------------------

    def _load_basis(self):
        try:
            from scripts.equalizer_ui import PROCESS_WH, load_class_means
            self._basis = load_class_means(PROCESS_WH)
            if not self._basis:
                self._basis_error = (
                    "Basis images not found — Constructed pane disabled."
                )
                self._show_scene_text(self._constructed_scene, self._basis_error)
        except Exception as exc:
            self._basis_error = f"Basis load failed: {exc}"
            self._show_scene_text(self._constructed_scene, self._basis_error)
            return

        # Detect 1x1 landmarks OUTSIDE try/except so exceptions are visible.
        if self._basis:
            basis_1x1 = self._basis.get("1x1")
            if basis_1x1 is not None:
                self._basis_landmarks = detect_basis_landmarks(basis_1x1)

    # ----- Zoom ------------------------------------------------------------

    ZOOM_MIN = 0.5
    ZOOM_MAX = 3.0
    ZOOM_STEP = 0.1

    @property
    def _view_scale(self) -> float:
        """Base scale: maps PROCESS_W pixels to DISPLAY_WH at 100 % zoom."""
        from scripts.equalizer_ui import DISPLAY_WH
        return DISPLAY_WH[0] / PROCESS_W

    def _apply_zoom(self) -> None:
        """Sync both views' transforms to the current _zoom level."""
        s = self._view_scale * self._zoom
        t = self._selected_view.transform()
        t.reset()
        t.scale(s, s)
        self._selected_view.setTransform(t)
        self._constructed_view.setTransform(t)
        pct = int(self._zoom * 100)
        self._selected_group.setTitle(f"Selected (live RHEED) — {pct}%")
        self._constructed_group.setTitle(f"Constructed (grower blend) — {pct}%")

    def _set_zoom(self, factor: float) -> None:
        """Set zoom level, re-apply to both views."""
        new = round(max(self.ZOOM_MIN, min(self.ZOOM_MAX, factor)) / self.ZOOM_STEP) * self.ZOOM_STEP
        new = max(self.ZOOM_MIN, min(self.ZOOM_MAX, new))
        if abs(new - self._zoom) < 0.001:
            return
        self._zoom = new
        self._apply_zoom()

    def _render_display(
        self, scene: QGraphicsScene, array: np.ndarray,
    ) -> None:
        """Set the pixmap on a QGraphicsScene from a PROCESS_WH array.

        Applies contrast when rendering for the Selected (left) pane.
        """
        from scripts.equalizer_ui import apply_green_palette
        if scene is self._selected_scene and (self._brightness != 0.0 or self._contrast != 1.0):
            arr = np.clip(array * self._contrast + self._brightness, 0, 255)
        else:
            arr = array
        arr_u8 = np.clip(arr, 0, 255).astype(np.uint8)
        rgb = np.ascontiguousarray(apply_green_palette(arr_u8))
        h, w, _ = rgb.shape
        qimg = QImage(rgb.tobytes(), w, h, w * 3, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        # Replace or add the pixmap item.
        items = scene.items()
        if items:
            item = items[0]
            if isinstance(item, QGraphicsPixmapItem):
                item.setPixmap(pixmap)
                return
        # No existing pixmap item — create one.
        item = QGraphicsPixmapItem(pixmap)
        scene.addItem(item)
        scene.setSceneRect(0, 0, w, h)

    # ----- Contrast controls -----------------------------------------------

    def _on_contrast_plus(self):
        self._contrast = min(4.0, self._contrast + 0.2)
        self._refresh_selected_display()

    def _on_contrast_minus(self):
        self._contrast = max(0.1, self._contrast - 0.2)
        self._refresh_selected_display()

    def _on_brightness_plus(self):
        self._brightness += 10
        self._refresh_selected_display()

    def _on_brightness_minus(self):
        self._brightness -= 10
        self._refresh_selected_display()

    def _on_contrast_reset(self):
        self._brightness = 0.0
        self._contrast = 1.0
        self._refresh_selected_display()

    def _show_scene_text(self, scene: QGraphicsScene, text: str) -> None:
        """Display placeholder text in a graphics scene."""
        scene.clear()
        t = scene.addText(text)
        t.setDefaultTextColor(Qt.GlobalColor.gray)
        r = scene.sceneRect()
        t.setPos(max(0, (r.width() - t.boundingRect().width()) / 2),
                 max(0, (r.height() - t.boundingRect().height()) / 2))

    def _refresh_selected_display(self):
        """Re-render the left pane with current contrast settings.

        Does nothing during calibration so marker overlays are not erased.
        """
        if self._current_target is not None and not self._calibrating:
            self._render_display(self._selected_scene, self._current_target)

    # ----- Layout ---------------------------------------------------------

    def _build_ui(self):
        # Import display size lazily so the module still imports if
        # scripts.equalizer_ui has an error — the tab shows a placeholder
        # instead of failing GUI construction.
        try:
            from scripts.equalizer_ui import DISPLAY_WH
        except Exception:
            DISPLAY_WH = (520, 390)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        title = QLabel(
            "Live RHEED Equalizer — drag sliders until the Constructed "
            "blend matches the Selected live frame, then Save."
        )
        title.setStyleSheet("font-size: 12px; color: #aaa;")
        outer.addWidget(title)

        # === Top row: Selected (live) | Constructed (grower blend) ===
        images_row = QHBoxLayout()
        images_row.setSpacing(12)

        # --- Selected (live RHEED) ---
        self._selected_group = QGroupBox("Selected (live RHEED) — 100%")
        selected_layout = QVBoxLayout(self._selected_group)
        selected_layout.setContentsMargins(8, 12, 8, 8)
        # Contrast controls.
        contrast_row = QHBoxLayout()
        contrast_row.setSpacing(4)
        self._contrast_minus_btn = QPushButton("−")
        self._contrast_minus_btn.setFixedWidth(28)
        self._contrast_minus_btn.setToolTip("Decrease contrast")
        self._contrast_minus_btn.clicked.connect(self._on_contrast_minus)
        contrast_row.addWidget(self._contrast_minus_btn)
        self._contrast_plus_btn = QPushButton("+")
        self._contrast_plus_btn.setFixedWidth(28)
        self._contrast_plus_btn.setToolTip("Increase contrast")
        self._contrast_plus_btn.clicked.connect(self._on_contrast_plus)
        contrast_row.addWidget(self._contrast_plus_btn)
        self._brightness_minus_btn = QPushButton("▼")
        self._brightness_minus_btn.setFixedWidth(28)
        self._brightness_minus_btn.setToolTip("Decrease brightness")
        self._brightness_minus_btn.clicked.connect(self._on_brightness_minus)
        contrast_row.addWidget(self._brightness_minus_btn)
        self._brightness_plus_btn = QPushButton("▲")
        self._brightness_plus_btn.setFixedWidth(28)
        self._brightness_plus_btn.setToolTip("Increase brightness")
        self._brightness_plus_btn.clicked.connect(self._on_brightness_plus)
        contrast_row.addWidget(self._brightness_plus_btn)
        self._contrast_reset_btn = QPushButton("Reset")
        self._contrast_reset_btn.setFixedWidth(42)
        self._contrast_reset_btn.setToolTip("Reset brightness & contrast")
        self._contrast_reset_btn.clicked.connect(self._on_contrast_reset)
        contrast_row.addWidget(self._contrast_reset_btn)
        contrast_row.addStretch(1)
        selected_layout.addLayout(contrast_row)
        # Graphics view for zoom + pan.
        self._selected_scene = QGraphicsScene(self)
        self._selected_view = QGraphicsView(self._selected_scene)
        self._selected_view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._selected_view.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse,
        )
        self._selected_view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._selected_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._selected_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._selected_view.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._selected_view.setBackgroundBrush(
            self._selected_view.palette().brush(self._selected_view.palette().ColorRole.Window)
        )
        self._selected_view.setStyleSheet("QGraphicsView { background-color: #111; border: 1px solid #333; }")
        self._selected_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._selected_view.viewport().installEventFilter(self)
        selected_layout.addWidget(self._selected_view, 1)
        images_row.addWidget(self._selected_group, 1)

        # --- Constructed (grower blend) ---
        self._constructed_group = QGroupBox("Constructed (grower blend) — 100%")
        constructed_layout = QVBoxLayout(self._constructed_group)
        constructed_layout.setContentsMargins(8, 12, 8, 8)
        self._constructed_scene = QGraphicsScene(self)
        self._constructed_view = QGraphicsView(self._constructed_scene)
        self._constructed_view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._constructed_view.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse,
        )
        self._constructed_view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._constructed_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._constructed_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._constructed_view.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._constructed_view.setStyleSheet("QGraphicsView { background-color: #111; border: 1px solid #333; }")
        self._constructed_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._constructed_view.viewport().installEventFilter(self)
        constructed_layout.addWidget(self._constructed_view, 1)
        images_row.addWidget(self._constructed_group, 1)

        outer.addLayout(images_row, 1)

        # === Bottom row: Classifier % | Grower sliders ===
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)

        # --- Classifier % breakdown ---
        classifier_group = QGroupBox("Classifier % breakdown")
        cls_layout = QGridLayout(classifier_group)
        cls_layout.setContentsMargins(12, 16, 12, 12)
        cls_layout.setHorizontalSpacing(12)
        cls_layout.setVerticalSpacing(6)
        for i, label in enumerate(CLASS_LABELS):
            name_label = QLabel(label)
            name_label.setStyleSheet("font-size: 12px;")
            cls_layout.addWidget(name_label, i, 0)
            value_label = QLabel("—%")
            value_label.setStyleSheet(
                "font-size: 12px; color: #0d9488; font-weight: bold;"
            )
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            cls_layout.addWidget(value_label, i, 1)
            self._classifier_value_labels[label] = value_label
        cls_layout.setColumnStretch(0, 1)
        cls_layout.setRowStretch(len(CLASS_LABELS), 1)  # push rows to top
        bottom_row.addWidget(classifier_group, 1)

        # --- Grower % (sliders + buttons) ---
        grower_group = QGroupBox("Grower % breakdown")
        grower_layout = QGridLayout(grower_group)
        grower_layout.setContentsMargins(12, 16, 12, 12)
        grower_layout.setHorizontalSpacing(10)
        grower_layout.setVerticalSpacing(6)
        grower_layout.setColumnStretch(1, 1)  # slider column expands
        for i, label in enumerate(CLASS_LABELS):
            name_label = QLabel(label)
            name_label.setStyleSheet("font-size: 12px;")
            name_label.setMinimumWidth(70)
            grower_layout.addWidget(name_label, i, 0)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            slider.setTickInterval(10)
            slider.valueChanged.connect(self._on_slider_changed)
            grower_layout.addWidget(slider, i, 1)
            self._sliders[label] = slider

            value_label = QLabel("0%")
            value_label.setStyleSheet(
                "font-size: 12px; color: #d97706; font-weight: bold;"
            )
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            value_label.setMinimumWidth(45)
            grower_layout.addWidget(value_label, i, 2)
            self._slider_value_labels[label] = value_label

        # Action button row spans all columns beneath the sliders.
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._auto_fit_btn = QPushButton("Auto-fit")
        self._auto_fit_btn.setToolTip(
            "Least-squares fit the sliders to the current 'Selected' image."
        )
        self._auto_fit_btn.clicked.connect(self._on_auto_fit)
        btn_row.addWidget(self._auto_fit_btn)

        self._normalize_btn = QPushButton("Normalize")
        self._normalize_btn.setToolTip(
            "Rescale the sliders so they sum to 100%."
        )
        self._normalize_btn.clicked.connect(self._on_normalize)
        btn_row.addWidget(self._normalize_btn)

        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setToolTip("Back to uniform 20/20/20/20/20.")
        self._reset_btn.clicked.connect(self._reset_to_uniform)
        btn_row.addWidget(self._reset_btn)

        # Pause button (Jul 14 2026). Checkable so Qt tracks the visual
        # state without us maintaining a duplicate flag. Amber (#d97706)
        # when checked matches the MARK EVENT identity + Manual-events
        # footer counter — a grower looking at any of the three "amber
        # is grower-in-the-loop" surfaces sees the same visual cue.
        # Label flips between "Freeze frame" (pressing will freeze) and
        # "Resume live" (pressing will resume) so the button describes
        # the action, not the current state.
        self._pause_btn = QPushButton("Freeze frame")
        self._pause_btn.setCheckable(True)
        self._pause_btn.setToolTip(
            "Freeze the 'Selected' pane on the current frame so you can "
            "balance sliders without the image changing under you. Auto-"
            "fit + Save still work against the frozen frame."
        )
        self._pause_btn.setStyleSheet(
            "QPushButton { padding: 4px 12px; }"
            "QPushButton:checked { background-color: #d97706; color: white; "
            "font-weight: bold; }"
        )
        self._pause_btn.toggled.connect(self._on_pause_toggled)
        btn_row.addWidget(self._pause_btn)

        # --- Camera alignment controls (Jul 30 2026) ---
        self._calibrate_btn = QPushButton("Calibrate")
        self._calibrate_btn.setToolTip(
            "Click three 1×1 landmarks on the live image to align "
            "camera geometry with the basis images."
        )
        self._calibrate_btn.clicked.connect(self._on_calibrate_clicked)
        btn_row.addWidget(self._calibrate_btn)

        self._clear_cal_btn = QPushButton("Clear Cal")
        self._clear_cal_btn.setToolTip("Discard current camera alignment.")
        self._clear_cal_btn.clicked.connect(self._on_clear_cal_clicked)
        self._clear_cal_btn.hide()
        btn_row.addWidget(self._clear_cal_btn)

        self._cal_status_label = QLabel(
            "○ Not calibrated — Auto-fit and Save disabled"
        )
        self._cal_status_label.setStyleSheet(
            "color: #d97706; font-size: 11px;"
        )
        btn_row.addWidget(self._cal_status_label)

        btn_row.addStretch(1)

        self._save_btn = QPushButton("Save label")
        self._save_btn.setStyleSheet(
            "QPushButton { background-color: #0d9488; color: white; "
            "font-size: 13px; font-weight: bold; padding: 6px 16px; }"
            "QPushButton:disabled { background-color: #222; color: #666; }"
        )
        self._save_btn.setToolTip(
            "Snapshot the current live frame + save the slider weights to "
            "live_labels.csv. Requires an active session."
        )
        self._save_btn.setEnabled(False)  # Enabled by GrowthApp on session start
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn)

        grower_layout.addLayout(
            btn_row, len(CLASS_LABELS), 0, 1, 3,
        )
        bottom_row.addWidget(grower_group, 1)

        outer.addLayout(bottom_row, 1)

        # Initial scene rects for placeholder text centering.
        from scripts.equalizer_ui import PROCESS_WH
        self._selected_scene.setSceneRect(0, 0, PROCESS_WH[0], PROCESS_WH[1])
        self._constructed_scene.setSceneRect(0, 0, PROCESS_WH[0], PROCESS_WH[1])
        self._show_scene_text(self._selected_scene, "Waiting for live camera stream…")
        self._show_scene_text(self._constructed_scene, "Uniform mix — drag sliders below.")

    # ----- Public methods (called by GrowthApp) ---------------------------

    def update_camera_frame(
        self, frame: Optional[np.ndarray], capture_metadata: Optional[dict] = None,
    ):
        """Update the 'Selected' pane with a new live camera frame.

        Frame is expected as ``(H, W, 3)`` uint8 RGB (post-palette-fix
        format from ``drivers/rheed_camera.py``). Handles grayscale
        (2D) input too. Downsamples to PROCESS_WH grayscale for the
        basis-matched processing space and stores the full-res original
        for GrowthApp to snapshot on Save.

        Silently no-ops on decode error (rare — camera may deliver a
        weird frame under stress). The last successful target stays.

        When ``self._paused`` is True (Freeze frame toggled on), the
        incoming frame is dropped on the floor — cache + displayed
        pixmap both preserve whatever was current at pause time. That
        stable state is what Auto-fit / Save see.
        """
        if frame is None:
            return
        if self._paused:
            return
        self._current_full_frame = frame
        self._current_capture_metadata = dict(capture_metadata or {})
        # Track camera dimensions for staleness checks.
        if frame.ndim == 3:
            self._camera_height, self._camera_width = frame.shape[:2]
        else:
            self._camera_height, self._camera_width = frame.shape[:2]

        # Staleness check: if calibration is stale, clear it.
        if self.has_valid_calibration():
            meta = self._current_capture_metadata
            stale, reason = calibration_is_stale(
                self._calibration,
                source_hwnd=int(meta.get("source_hwnd", 0)),
                camera_width=self._camera_width,
                camera_height=self._camera_height,
                camera_mode=str(meta.get("capture_backend", "")),
                session_active=True,
            )
            if stale:
                self.clear_calibration()
        try:
            from PIL import Image
            from scripts.equalizer_ui import PROCESS_WH
            img = Image.fromarray(frame)
            if img.mode != "L":
                img = img.convert("L")
            img = img.resize(PROCESS_WH, Image.LANCZOS)
            self._current_target = np.asarray(img, dtype=np.float32)
            self._render_display(self._selected_scene, self._current_target)
        except Exception:
            # Don't raise from the camera hot path.
            return

    def update_classifier_state(self, state: Optional[ClassifierState]):
        """Populate the 5 classifier % labels from ClassifierState.

        Mirrors the Monitor tab's classifier slider read. Uses
        CLASSIFIER_LABEL_MAP to translate the classifier's smoothed_percent
        keys (Monitor spellings) into this tab's short-form class labels.
        """
        if state is None or not state.smoothed_percent:
            for lbl in self._classifier_value_labels.values():
                lbl.setText("—%")
            return
        smoothed = state.smoothed_percent
        for cls_label, tab_label in CLASSIFIER_LABEL_MAP.items():
            pct = smoothed.get(cls_label, 0)
            if tab_label in self._classifier_value_labels:
                self._classifier_value_labels[tab_label].setText(
                    f"{int(pct)}%"
                )

    def get_current_full_frame(self) -> Optional[np.ndarray]:
        """Return the last-received full-resolution camera frame.

        GrowthApp reads this on Save to snapshot the frame into
        live_label_NNN_*.bmp. Downsampling to PROCESS_WH for the
        Equalizer's visualization is intentional; the on-disk snapshot
        preserves the full 656×492 (or whatever the camera delivers)
        so downstream training pipelines get the native-resolution
        data.
        """
        return self._current_full_frame

    def get_current_capture_metadata(self) -> dict:
        """Return metadata frozen alongside the displayed full frame."""
        return dict(self._current_capture_metadata)

    def clear_camera_frame(
        self, message: str = "Waiting for live camera stream...",
    ) -> None:
        """Clear the selected-frame cache after an upstream capture failure."""
        self._current_target = None
        self._current_full_frame = None
        self._current_capture_metadata = {}
        self._show_scene_text(self._selected_scene, message)

    def set_save_enabled(self, enabled: bool):
        """Toggle the Save button — GrowthApp calls this on session state
        changes (running vs idle/armed). Calibration is an additional
        AND condition.
        """
        self._save_allowed_by_session = enabled
        self._update_button_states()

    def reset_for_new_session(self):
        """Wipe transient state so the next session starts fresh.

        Called by GrowthApp on session reset (disarm). Preserves the
        loaded basis + slider defaults; only the live-camera cache,
        classifier % display, pause state, and calibration are cleared.
        """
        self._current_target = None
        self._current_full_frame = None
        self._show_scene_text(self._selected_scene, "Waiting for live camera stream…")
        for lbl in self._classifier_value_labels.values():
            lbl.setText("—%")
        # Clear calibration — pixel coordinates invalid across sessions.
        self.clear_calibration()
        # Clear pause state so the next armed session starts with a
        # streaming Selected pane, not stuck on the previous session's
        # last frame. setChecked triggers _on_pause_toggled which flips
        # _paused + updates the label, keeping all three in sync.
        if self._pause_btn.isChecked():
            self._pause_btn.setChecked(False)
        # Defensive: even if the button was already unchecked (no
        # toggled emission), force _paused False + label back.
        self._paused = False
        self._pause_btn.setText("Freeze frame")
        self._reset_to_uniform()
        self._update_button_states()

    def _on_pause_toggled(self, checked: bool):
        """React to the Freeze frame / Resume live toggle.

        ``checked=True`` → frozen; button label switches to "Resume live"
        so the next click's action is legible. ``checked=False`` → live;
        label back to "Freeze frame".
        """
        self._paused = checked
        self._pause_btn.setText("Resume live" if checked else "Freeze frame")

    # ----- Slider mechanics -----------------------------------------------

    def _on_slider_changed(self):
        # Guarded so programmatic setValue in _set_weights / _reset_to_uniform
        # only recomputes the reconstruction ONCE at the end, not 5 times.
        if self._adjusting:
            return
        self._refresh_slider_labels()
        self._update_reconstruction()

    def _refresh_slider_labels(self):
        for label, slider in self._sliders.items():
            self._slider_value_labels[label].setText(f"{slider.value()}%")

    def _current_weights(self) -> dict[str, float]:
        """Slider values as fractions in [0, 1]. Sum is arbitrary — the
        grower may or may not have Normalize'd."""
        return {
            label: slider.value() / 100.0
            for label, slider in self._sliders.items()
        }

    def _set_weights(self, weights: dict[str, float]):
        """Set all 5 sliders from a weight dict, refresh labels + recon
        exactly once via the _adjusting guard."""
        self._adjusting = True
        try:
            for label, slider in self._sliders.items():
                w = weights.get(label, 0.0)
                slider.setValue(int(round(w * 100)))
        finally:
            self._adjusting = False
        self._refresh_slider_labels()
        self._update_reconstruction()

    def _reset_to_uniform(self):
        uniform = 1.0 / len(CLASS_LABELS)
        self._set_weights({label: uniform for label in CLASS_LABELS})

    def _effective_basis(self) -> dict[str, np.ndarray]:
        """Return the shifted basis if calibrated or in preview, else raw."""
        if self._shifted_basis is not None:
            return self._shifted_basis
        return self._basis

    def _update_reconstruction(self):
        """Rebuild the Constructed pane from the current slider mixture.

        Uses shifted basis when a calibration is active.
        """
        if not self._basis:
            return

        basis = self._effective_basis()
        weights = self._current_weights()
        shape = next(iter(basis.values())).shape
        recon = np.zeros(shape, dtype=np.float32)
        for label, w in weights.items():
            basis_img = basis.get(label)
            if basis_img is not None:
                recon += float(w) * basis_img
        self._render_display(self._constructed_scene, recon)

    # ----- Button handlers ------------------------------------------------

    def _on_auto_fit(self):
        """Least-squares fit the sliders onto the current 'Selected' image.

        Uses shifted basis when a calibration is active.
        """
        if self._current_target is None or not self._basis:
            return
        try:
            from scripts.equalizer_ui import auto_fit
        except Exception:
            return
        basis = self._effective_basis()
        weights = auto_fit(basis, self._current_target)
        self._set_weights(weights)

    def _on_normalize(self):
        """Rescale sliders so they sum to 1.0 (100%)."""
        weights = self._current_weights()
        s = sum(weights.values())
        if s <= 0:
            return
        self._set_weights({k: v / s for k, v in weights.items()})

    def _on_save(self):
        """Emit the save signal with the current slider weights.

        GrowthApp handles the sensor snapshot + logger call — the tab
        stays independent of the growth-log file lifecycle.
        """
        weights = self._current_weights()
        self.live_label_save_requested.emit(weights)

    # ----- Camera alignment (Jul 30 2026) ----------------------------------

    def _enter_calibration_mode(self):
        """Reset all calibration state and prepare for a fresh calibration."""
        self._calibration = None
        self._shifted_basis = None
        self._pending_calibration = None
        self._calibrating = True
        self._calibration_clicks = []
        # Disable drag-to-pan while calibrating so clicks register as clicks.
        self._selected_view.setDragMode(QGraphicsView.DragMode.NoDrag)

    def _update_button_states(self):
        """Enable or disable buttons based on calibration and session state."""
        cal_ok = (
            self._calibration is not None
            and self._calibration.grower_accepted
        )
        session_ok = getattr(self, "_save_allowed_by_session", False)

        # Active calibration session always wins over a stale accepted one.
        if self._calibrating and len(self._calibration_clicks) == 3:
            self._auto_fit_btn.setEnabled(False)
            self._save_btn.setEnabled(False)
            self._calibrate_btn.setText("Accept")
            self._calibrate_btn.setToolTip("Accept this alignment.")
            self._clear_cal_btn.setText("Retry")
            self._clear_cal_btn.show()
            self._cal_status_label.setText(
                "Review the Constructed pane — aligned?"
            )
            self._cal_status_label.setStyleSheet("color: #d97706; font-size: 11px;")
        elif self._calibrating:
            n = len(self._calibration_clicks)
            self._auto_fit_btn.setEnabled(False)
            self._save_btn.setEnabled(False)
            self._calibrate_btn.setText(f"Clicking ({n}/3)")
            self._calibrate_btn.setToolTip(
                "Click three 1×1 landmarks on the live image."
            )
            self._clear_cal_btn.hide()
            self._cal_status_label.setText(
                "Click left, centre, right 1×1 spots"
            )
            self._cal_status_label.setStyleSheet("color: #d97706; font-size: 11px;")
        elif cal_ok:
            self._auto_fit_btn.setEnabled(True)
            self._save_btn.setEnabled(session_ok)
            self._calibrate_btn.setText("Re-calibrate")
            self._calibrate_btn.setToolTip("Replace current calibration.")
            self._clear_cal_btn.setText("Clear Cal")
            self._clear_cal_btn.show()
            self._cal_status_label.setText("● Calibrated")
            self._cal_status_label.setStyleSheet("color: #22c55e; font-size: 11px;")
        else:
            self._auto_fit_btn.setEnabled(False)
            self._save_btn.setEnabled(False)
            self._calibrate_btn.setText("Calibrate")
            self._calibrate_btn.setToolTip(
                "Click three 1×1 landmarks on the live image."
            )
            self._clear_cal_btn.hide()
            self._cal_status_label.setText(
                "○ Not calibrated — Auto-fit and Save disabled"
            )
            self._cal_status_label.setStyleSheet("color: #d97706; font-size: 11px;")

    def has_valid_calibration(self) -> bool:
        """Return True if a grower-accepted calibration is active."""
        return (
            self._calibration is not None
            and self._calibration.grower_accepted
        )

    def get_calibration(self) -> Optional[Calibration]:
        """Return the current calibration, or None."""
        return self._calibration

    def clear_calibration(self) -> None:
        """Discard the current calibration and revert to raw basis."""
        self._calibration = None
        self._shifted_basis = None
        self._calibrating = False
        self._calibration_clicks = []
        self._selected_view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._selected_view.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        self._update_reconstruction()
        self._update_button_states()

    def _on_calibrate_clicked(self):
        """Start calibration: auto-detect or manual click mode."""
        # Accept path: 3 clicks done, preview showing.
        if self._calibrating and len(self._calibration_clicks) == 3:
            self._accept_calibration()
            return
        if self._current_target is None:
            return

        # Enter fresh calibration mode (clears old calibration, event filter, etc.).
        self._enter_calibration_mode()

        # Try auto-detection first.
        live_pts = detect_live_landmarks(self._current_target)
        if live_pts is not None:
            # Auto-detection succeeded — skip to preview.
            for pt in live_pts:
                self._calibration_clicks.append((float(pt[0]), float(pt[1])))
            self._draw_calibration_markers()
            self._compute_and_preview_calibration()
            return

        # Auto-detection failed — enter manual click mode.
        self._selected_view.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self._update_button_states()

    def _on_clear_cal_clicked(self):
        """Clear Cal or Retry."""
        if self._calibrating and len(self._calibration_clicks) == 3:
            # Retry: restart manual click sequence.
            self._enter_calibration_mode()
            self._selected_view.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self._update_reconstruction()
            self._update_button_states()
        else:
            # Clear calibration.
            self.clear_calibration()

    def eventFilter(self, obj, event):
        """Ctrl+wheel zoom on both panes + calibration clicks on Selected."""
        from PyQt6.QtCore import QEvent

        # Guard: views may not exist yet during construction.
        sv = getattr(self, "_selected_view", None)
        cv = getattr(self, "_constructed_view", None)

        # --- Ctrl+Wheel = zoom (plain wheel scrolls via QGraphicsView) ---
        if sv is not None and cv is not None:
            sel_vp, con_vp = sv.viewport(), cv.viewport()
            if obj in (sel_vp, con_vp):
                if event.type() == QEvent.Type.Wheel:
                    if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                        delta = event.angleDelta().y()
                        if delta > 0:
                            self._set_zoom(self._zoom + self.ZOOM_STEP)
                        elif delta < 0:
                            self._set_zoom(self._zoom - self.ZOOM_STEP)
                        return True

        # --- Calibration clicks on Selected viewport ---
        if sv is not None and obj is sv.viewport() and self._calibrating:
            if event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._handle_calibration_click(event.position().x(),
                                                   event.position().y())
                    return True
        return super().eventFilter(obj, event)

    def _handle_calibration_click(self, viewport_x: float, viewport_y: float):
        """Process one calibration click on the Selected viewport.

        Maps viewport coords → scene coords (= PROCESS_WH directly).
        """
        from PyQt6.QtCore import QPointF
        scene_pt = self._selected_view.mapToScene(int(viewport_x), int(viewport_y))
        px = max(0, min(PROCESS_W - 1, scene_pt.x()))
        py = max(0, min(PROCESS_H - 1, scene_pt.y()))
        self._calibration_clicks.append((px, py))
        n = len(self._calibration_clicks)
        print(f"[CAL] click {n}/3 at PROCESS ({px:.1f}, {py:.1f})")

        # Draw a marker on the Selected label.
        self._draw_calibration_markers()

        if n < 3:
            self._update_button_states()
        else:
            print("[CAL] 3 clicks done, calling _compute_and_preview_calibration")
            self._compute_and_preview_calibration()

    def _draw_calibration_markers(self):
        """Draw crosshair markers at click positions on the Selected label."""
        if self._current_target is None:
            return
        import numpy as np
        # Start from the current target image.
        img = self._current_target.copy()
        colors = [255, 200, 128]  # different brightness for each marker
        for i, (cx, cy) in enumerate(self._calibration_clicks):
            # Convert PROCESS_WH → image pixel coords.
            ix = int(round(cx))
            iy = int(round(cy))
            r = 3
            val = colors[min(i, len(colors) - 1)]
            h, w = img.shape
            # Draw crosshair.
            for dx in range(-r, r + 1):
                xx = ix + dx
                if 0 <= xx < w:
                    img[max(0, iy):min(h, iy + 1), xx] = val
            for dy in range(-r, r + 1):
                yy = iy + dy
                if 0 <= yy < h:
                    img[yy, max(0, ix):min(w, ix + 1)] = val
            # Draw circle (approximate).
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if dx*dx + dy*dy <= r*r:
                        xx, yy = ix + dx, iy + dy
                        if 0 <= xx < w and 0 <= yy < h:
                            img[yy, xx] = val
        self._render_display(self._selected_scene, np.clip(img, 0, 255))

    def _compute_and_preview_calibration(self):
        """Compute similarity transform, warp basis, update Constructed."""
        from PyQt6.QtWidgets import QMessageBox

        if self._basis_landmarks is None:
            QMessageBox.warning(self, "Calibration Failed",
                "Basis landmarks not loaded. The 1×1 basis image may be missing.")
            self._update_button_states()
            return
        if len(self._calibration_clicks) != 3:
            QMessageBox.warning(self, "Calibration Failed",
                f"Need 3 clicks, got {len(self._calibration_clicks)}.")
            self._update_button_states()
            return

        live_points = np.array(self._calibration_clicks, dtype=np.float64)
        try:
            matrix, scale, rot_deg, dx, dy = compute_similarity(
                self._basis_landmarks, live_points,
            )
        except Exception as e:
            QMessageBox.warning(self, "Calibration Failed",
                f"Failed to compute transform:\n{e}")
            self._update_button_states()
            return

        N = self._basis_landmarks.shape[0]
        src_h = np.hstack([self._basis_landmarks, np.ones((N, 1))])
        projected = (matrix @ src_h.T).T
        reproj = float(np.mean(np.linalg.norm(projected - live_points, axis=1)))

        ok, reason = validate_similarity(rot_deg, scale, reproj)
        if not ok:
            QMessageBox.warning(self, "Calibration Failed",
                f"{reason}\n\n"
                f"Rotation: {rot_deg:.1f}°  Scale: {scale:.3f}  Reproj: {reproj:.1f} px\n\n"
                f"Basis landmarks:\n"
                f"  L=({self._basis_landmarks[0,0]:.0f},{self._basis_landmarks[0,1]:.0f}) "
                f"  S=({self._basis_landmarks[1,0]:.0f},{self._basis_landmarks[1,1]:.0f}) "
                f"  R=({self._basis_landmarks[2,0]:.0f},{self._basis_landmarks[2,1]:.0f})\n"
                f"Your clicks:\n"
                f"  L=({live_points[0,0]:.0f},{live_points[0,1]:.0f}) "
                f"  S=({live_points[1,0]:.0f},{live_points[1,1]:.0f}) "
                f"  R=({live_points[2,0]:.0f},{live_points[2,1]:.0f})\n"
                f"Click Calibrate to try again.")
            self._update_button_states()
            return

        self._pending_calibration = Calibration(
            basis_points=self._basis_landmarks.copy(),
            live_points=live_points,
            matrix=matrix,
            rotation_deg=rot_deg,
            scale=scale,
            reprojection_px=reproj,
            source_hwnd=int(self._current_capture_metadata.get("source_hwnd", 0)),
            camera_width=self._camera_width,
            camera_height=self._camera_height,
            camera_mode=str(self._current_capture_metadata.get("capture_backend", "")),
            view_segment_id=0,
            grower_accepted=False,
        )

        try:
            self._shifted_basis = warp_basis_similarity(self._basis, matrix)
        except Exception as e:
            QMessageBox.warning(self, "Calibration Failed",
                f"Failed to warp basis images:\n{e}")
            self._update_button_states()
            return

        self._update_reconstruction()
        self._update_button_states()

    def _accept_calibration(self):
        """Grower accepted the preview — make calibration permanent."""
        cal = self._pending_calibration
        if cal is None:
            return
        cal.grower_accepted = True
        self._calibration = cal
        self._calibrating = False
        self._calibration_clicks = []
        self._selected_view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._selected_view.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        # Restore the live camera image (remove markers).
        self._restore_selected_image()
        self._update_button_states()

    def _restore_selected_image(self):
        """Redisplay the current target image without calibration markers."""
        if self._current_target is None:
            return
        self._render_display(self._selected_scene, self._current_target)
