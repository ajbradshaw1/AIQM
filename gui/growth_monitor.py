"""
MBE Growth Monitor — OMBE Growth Log Assistant.

Provides live RHEED + pyrometer display, timestamped operations logging,
and growth log export for OMBE growths.

Tab layout:
  Monitor  — value displays, live RHEED image, note entry, LOG ENTRY button
  Session  — config (upper-left), sensor log (upper-right),
             growth notes (bottom half, full width), export
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from drivers.config import MBESystemConfig, get_active_config

import numpy as np
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QTextEdit, QSizePolicy, QFrame,
    QDoubleSpinBox, QComboBox, QFormLayout, QFileDialog,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QGroupBox, QSlider, QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QFont, QShortcut, QKeySequence


def _default_save_path(config: Optional[MBESystemConfig] = None) -> str:
    """Pick the default growth-log save path.

    On Windows with the T9 SSD mounted at E:, use a chamber-specific root:
    ``E:\\OMBE\\GrowthMonitor`` or ``E:\\ChMBE\\GrowthMonitor``.  Checks the
    DRIVE (``E:\\``), not a chamber folder — the folder is created on first
    save.  This keeps a Ch-MBE launch from silently inheriting an O-MBE path.

    Fallbacks:
      - Windows without the T9 mounted → the corresponding chamber folder
        under ``%USERPROFILE%\\Documents``.
        Better than the repo path because it's still off C:'s bloat
        (whichever partition Documents lives on, typically not the
        overflowing one).
      - Non-Windows (Mac dev) → ``logs/growths`` relative to the repo.
        Backwards-compatible for local dev / CI test paths.
    """
    installed_session_root = os.environ.get("AIQM_SESSION_ROOT", "").strip()
    if installed_session_root:
        return installed_session_root
    cfg = config or get_active_config()
    chamber_folder = "ChMBE" if cfg.chamber_id == "chmbe" else "OMBE"
    if sys.platform == "win32":
        # Check the drive itself, not the OMBE folder — folder gets
        # created on first save if missing.
        if Path("E:\\").exists():
            return str(Path("E:\\") / chamber_folder / "GrowthMonitor")
        # Windows without T9: prefer Documents over C: root; keeps growth
        # sessions per-user and off any C:-bloat path.
        return str(Path.home() / "Documents" / chamber_folder)
    # Non-Windows dev: repo-relative default preserves test / CI paths.
    return "logs/growths"

from gui.state import (
    CameraState, ClassifierState, EvapControlState, MistralState,
    PowerSupplyState, PyrometerState, RheedQcState, WeakPrimaryShadowState,
)
from gui.widgets import ValueDisplay
from gui.growth_logger import (
    EVENT_STATE_DISCARDED,
    EVENT_STATE_KEPT_DEFAULT,
    EVENT_STATE_KEPT_EXPLICIT,
)
from gui.events_tab import EventsTab
from gui.live_equalizer_tab import LiveEqualizerTab
from gui.scrubber_tab import ScrubberTab


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

DARK_STYLESHEET = """
QWidget {
    background-color: #111;
    color: #ddd;
    font-size: 13px;
}
QLineEdit, QTextEdit {
    background-color: #fff;
    color: #111;
    border: 1px solid #555;
    padding: 4px;
}
QDoubleSpinBox, QComboBox {
    border: 1px solid #555;
    border-radius: 0px;
    padding: 4px;
    background-color: #fff;
    color: #111;
}
QComboBox QAbstractItemView {
    background-color: #fff;
    color: #111;
    selection-background-color: #2563eb;
    selection-color: #fff;
    border: 1px solid #555;
}
QComboBox::drop-down {
    border: none;
}
QLabel {
    background-color: transparent;
}
QPushButton {
    border: 1px solid #555;
    padding: 8px 16px;
    font-weight: bold;
    background-color: #333;
    color: #ddd;
}
QPushButton:disabled {
    background-color: #222;
    color: #666;
    border-color: #333;
}
QTabWidget::pane {
    border: 1px solid #555;
    background-color: #111;
}
QTabBar::tab {
    background: #222;
    color: #aaa;
    padding: 8px 16px;
    border: 1px solid #555;
    border-bottom: none;
}
QTabBar::tab:selected {
    background: #333;
    color: #fff;
}
QTableWidget {
    background-color: #1a1a1a;
    color: #ddd;
    gridline-color: #333;
    border: 1px solid #555;
}
QTableWidget::item {
    padding: 4px;
}
QHeaderView::section {
    background-color: #222;
    color: #ddd;
    padding: 6px;
    border: 1px solid #333;
    font-weight: bold;
}
QGroupBox {
    border: 1px solid #555;
    margin-top: 8px;
    padding-top: 16px;
    font-weight: bold;
}
QGroupBox::title {
    color: #ddd;
    subcontrol-position: top left;
    padding: 2px 8px;
}
"""

BTN_ARM = "QPushButton { background-color: #2563eb; color: white; }"
BTN_DISARM = "QPushButton { background-color: #7c3aed; color: white; }"
BTN_START = "QPushButton { background-color: #16a34a; color: white; }"
BTN_STOP = "QPushButton { background-color: #dc2626; color: white; }"

MAX_SENSOR_DISPLAY_ROWS = 500


# ---------------------------------------------------------------------------
# GrowthMonitor
# ---------------------------------------------------------------------------

class AutoCaptureBanner(QFrame):
    """Non-modal interrupt banner shown when auto-capture flags an event.

    The immutable context buffer is always kept. Explicit buttons Confirm or
    Reject the same v2 candidate shown by the Events tab; timeout leaves that
    candidate Pending for later review. Legacy keep/discard CSV states remain
    storage-audit evidence and never delete captured frames.

    Apr 17 design — the "progress bar" interrupt mechanism that builds
    grower trust in the detector's prompt cadence before the
    classifier-driven version takes over.
    """

    decision_made = pyqtSignal(int, str, str)  # emits (event_idx, buffer_dir, state)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "AutoCaptureBanner { background-color: #fff5e6; "
            "border-left: 4px solid #d97706; border-radius: 2px; }"
            "QLabel { color: #333; background: transparent; }"
            "QPushButton { padding: 4px 14px; font-size: 12px; }"
        )
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(10)

        self._icon_label = QLabel("⚡")  # lightning bolt
        self._icon_label.setStyleSheet("font-size: 18px; color: #d97706;")
        layout.addWidget(self._icon_label)

        self._message_label = QLabel("")
        self._message_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        layout.addWidget(self._message_label)

        layout.addStretch(1)

        self._countdown_label = QLabel("")
        self._countdown_label.setStyleSheet("font-size: 12px; color: #666;")
        layout.addWidget(self._countdown_label)

        self._discard_btn = QPushButton("Reject candidate")
        self._discard_btn.setStyleSheet(
            "QPushButton { background-color: #fff; color: #b91c1c; "
            "border: 1px solid #b91c1c; border-radius: 3px; }"
            "QPushButton:hover { background-color: #fee2e2; }"
        )
        self._discard_btn.clicked.connect(self._on_discard_clicked)
        layout.addWidget(self._discard_btn)

        self._keep_btn = QPushButton("Confirm candidate")
        self._keep_btn.setStyleSheet(
            "QPushButton { background-color: #fff; color: #15803d; "
            "border: 1px solid #15803d; border-radius: 3px; }"
            "QPushButton:hover { background-color: #dcfce7; }"
        )
        self._keep_btn.clicked.connect(self._on_keep_clicked)
        layout.addWidget(self._keep_btn)

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick)
        self._countdown_remaining = 0
        self._current_event_idx = 0
        self._current_buffer_dir = ""
        self.hide()

    def show_event(
        self,
        event_idx: int,
        score: float,
        buffer_dir: str,
        countdown_s: int = 10,
    ) -> None:
        """Display the banner for one flagged event."""
        self._current_event_idx = event_idx
        self._current_buffer_dir = buffer_dir
        self._countdown_remaining = max(1, int(countdown_s))
        self._message_label.setText(
            f"Auto-capture event #{event_idx} flagged (score {score:.2f})"
        )
        self._update_countdown_label()
        self.show()
        self._countdown_timer.start()

    def _tick(self) -> None:
        self._countdown_remaining -= 1
        if self._countdown_remaining <= 0:
            self._on_keep_default_timeout()
        else:
            self._update_countdown_label()

    def _update_countdown_label(self) -> None:
        self._countdown_label.setText(
            f"Stays Pending; closes in {self._countdown_remaining}s"
        )

    def _on_discard_clicked(self) -> None:
        self._emit_decision(EVENT_STATE_DISCARDED)

    def _on_keep_clicked(self) -> None:
        self._emit_decision(EVENT_STATE_KEPT_EXPLICIT)

    def _on_keep_default_timeout(self) -> None:
        self._emit_decision(EVENT_STATE_KEPT_DEFAULT)

    def _emit_decision(self, state: str) -> None:
        """Stop countdown, emit decision_made, hide the banner."""
        self._countdown_timer.stop()
        self.decision_made.emit(
            self._current_event_idx,
            self._current_buffer_dir,
            state,
        )
        self.hide()


class GrowthMonitor(QWidget):
    """Main growth monitoring widget — OMBE growth log assistant."""

    arm_requested = pyqtSignal()
    disarm_requested = pyqtSignal()
    reconnect_rheed_requested = pyqtSignal()
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    open_rheed_trend_requested = pyqtSignal()
    # Microseconds. Emitted by the "Apply" button beside the exposure spinbox,
    # never by the spinbox's own valueChanged — a stray scroll wheel must not
    # write to hardware.
    apply_exposure_requested = pyqtSignal(float)
    open_temp_plot_requested = pyqtSignal()
    commit_requested = pyqtSignal(dict)
    # Grower-marked event (Jul 10 2026 group-meeting design shift). Payload:
    #   pyro_temp:  Optional[float]   — °C from _latest_pyro
    #   voltage_V:  Optional[float]   — from _latest_mistral/_latest_psu
    #   current_A:  Optional[float]   — from _latest_mistral/_latest_psu
    #   psu_source: str               — "mistral" | "direct" | "none"
    #   elapsed_s:  float             — seconds since start
    #   note:       str               — from log_note_input if non-empty
    # GrowthApp adds the frame from monitor.get_current_frame() and calls
    # logger.record_manual_event. See meeting_jul10_2026.md for the
    # three-concern architecture (capture / mark / label) that this signal
    # sits inside.
    manual_event_requested = pyqtSignal(dict)
    # Structured acquisition-side RHEED events. Payload contains
    # ``event_type``, ``elapsed_s``, and the optional queued note. GrowthApp
    # owns the state machine and attaches the current frame/sensor snapshot.
    rheed_view_event_requested = pyqtSignal(dict)
    export_requested = pyqtSignal()
    # Grower asks for an mp4 time-lapse of the session's heartbeat frames
    # (Jul 10 2026 workstream #5). GrowthApp handles the file dialog +
    # threading; the widget just signals intent. Emitted without args —
    # the app-side handler picks the output path via QFileDialog.
    movie_export_requested = pyqtSignal()
    # True = user wants auto-capture paused; False = wants it resumed.
    auto_capture_pause_toggled = pyqtSignal(bool)
    # Forwarded from the banner — emits (event_idx, buffer_dir, state).
    # State is one of EVENT_STATE_KEPT_EXPLICIT / EVENT_STATE_KEPT_DEFAULT /
    # EVENT_STATE_DISCARDED. GrowthApp connects this to update the row in
    # auto_capture_events.csv via GrowthLogger.update_auto_capture_state.
    auto_capture_decision = pyqtSignal(int, str, str)

    def __init__(self, parent=None, config: Optional[MBESystemConfig] = None):
        super().__init__(parent)
        self._cfg: MBESystemConfig = config if config is not None else get_active_config()
        self.setStyleSheet(DARK_STYLESHEET)

        self._state = "idle"  # idle | armed | running
        self._start_time: Optional[datetime] = None
        self._latest_psu: Optional[PowerSupplyState] = None
        self._latest_pyro: Optional[PyrometerState] = None
        self._latest_camera: Optional[CameraState] = None
        self._latest_mistral: Optional[MistralState] = None
        self._latest_evap: Optional[EvapControlState] = None
        # Classifier state — cached by update_classifier_state so _on_commit
        # can pair the classifier's smoothed_percent with the grower's slider
        # values in every log entry (for Yuxin's #1 active-comparisons signal).
        self._latest_classifier: Optional[ClassifierState] = None
        self._blind_labeling_mode = False
        self._live_classifier_widgets: list[QWidget] = []
        self._classifier_output_exposure_recorded = False
        # Operator-known acquisition state is independent of classifier
        # Bad/OOD predictions and remains available when live classification
        # is disabled.
        self._rheed_qc_state = RheedQcState()
        # Grower-correction UI state (Jul 6 2026 — deliverable #5):
        #   _correction_active — True while the ✎ Correct toggle is on.
        #   _adjusting — reentrancy guard for Pattern A proportional
        #     adjustment; without it, the valueChanged signal cascade from
        #     one slider recursively re-triggers via the others we
        #     programmatically set, and Qt happily blows the stack.
        self._correction_active: bool = False
        self._adjusting: bool = False
        self._current_frame: Optional[np.ndarray] = None
        # Grower-marked-event count for this session. Increments locally on
        # every _on_mark_event and drives the footer counter label. Stays in
        # sync with growth_logger's _manual_event_counter because both
        # increment on the same click; the button is disabled when no
        # session is armed so there's no drift path.
        self._manual_event_count: int = 0
        # Continuous-capture ("movie") state for the Monitor footer indicator.
        # Interval is set once at session start (mirrors the timer setup in
        # growth_app._on_start); count increments on each heartbeat tick via
        # set_continuous_capture_count. Both reset at session end.
        self._continuous_capture_interval_s: Optional[float] = None
        self._continuous_capture_count: int = 0

        self._build_ui()
        self._apply_state()

        # Elapsed timer — 10 ms tick
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(10)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

    # ----- Layout ----------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(8)

        # === Top bar: Grower, Sample ID, buttons (always visible) ===
        top = QHBoxLayout()

        top.addWidget(QLabel("Grower:"))
        self.grower_input = QLineEdit()
        self.grower_input.setPlaceholderText("Enter grower name...")
        self.grower_input.setFixedWidth(180)
        top.addWidget(self.grower_input)

        top.addWidget(QLabel("  Sample ID:"))
        self.sample_id_input = QLineEdit()
        self.sample_id_input.setPlaceholderText("e.g. STO15_SY250702B")
        self.sample_id_input.setFixedWidth(220)
        top.addWidget(self.sample_id_input)

        top.addStretch()

        self.arm_btn = QPushButton("ARM")
        self.start_btn = QPushButton("START")
        self.stop_btn = QPushButton("STOP")
        for btn in (self.arm_btn, self.start_btn, self.stop_btn):
            btn.setFixedWidth(100)
        top.addWidget(self.arm_btn)
        top.addWidget(self.start_btn)
        top.addWidget(self.stop_btn)
        root.addLayout(top)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #555;")
        root.addWidget(sep)

        # === Tab widget ===
        self._tabs = QTabWidget()
        self._build_monitor_tab()
        self._build_direct_read_tab()
        self._build_events_tab()
        self._build_scrubber_tab()
        self._build_hidden_live_equalizer_compatibility_widget()
        self._build_session_tab()
        root.addWidget(self._tabs, 1)

        # === Connect button signals ===
        self.arm_btn.clicked.connect(self._on_arm_clicked)
        self.start_btn.clicked.connect(self._on_start_clicked)
        self.stop_btn.clicked.connect(self._on_stop_clicked)

    # ----- Monitor Tab -----------------------------------------------------

    def _build_monitor_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Auto-capture event banner — hidden by default, surfaces when the
        # detector flags an event and disappears on Confirm / Reject / timeout.
        self.auto_capture_banner = AutoCaptureBanner()
        self.auto_capture_banner.decision_made.connect(
            self._on_auto_capture_decision_internal,
        )
        layout.addWidget(self.auto_capture_banner)

        # Value displays at top of monitor tab
        vals = QHBoxLayout()
        self.elapsed_display = ValueDisplay("Elapsed Time", "", 0)
        self.elapsed_display.value.setText("00:00:00.00")
        self.temp_display = ValueDisplay("Temperature", "\u2103", 0)
        self.voltage_display = ValueDisplay("Voltage", "V", 2)
        self.current_display = ValueDisplay("Current", "A", 3)
        self.pressure_display = ValueDisplay("Pressure", "mbar", 2)
        for d in (self.elapsed_display, self.temp_display,
                  self.voltage_display, self.current_display,
                  self.pressure_display):
            d.setStyleSheet(
                "QFrame { border: 1px solid #555; } "
                "QLabel { background: transparent; }"
            )
            vals.addWidget(d)
        layout.addLayout(vals)

        # Main content: RHEED (left) | Note + Button (right)
        content = QHBoxLayout()
        content.setSpacing(12)

        # --- Left: RHEED image ---
        self.rheed_image_label = QLabel()
        self.rheed_image_label.setMinimumSize(400, 300)
        self.rheed_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rheed_image_label.setStyleSheet(
            "background-color: #000; border: 1px solid #555;"
        )
        self.rheed_image_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        content.addWidget(self.rheed_image_label, 2)

        # --- Right: Recon sliders + Note input + LOG ENTRY ---
        right = QVBoxLayout()
        right.setSpacing(8)

        # Live classification sliders — driven by the classifier worker
        # (see gui/workers.py::ClassifierWorker). Read-only for MVP; the
        # grower-override flow is a follow-up. Section header, canonical
        # label list from gui.recon_labels, and a per-cycle status line
        # below the sliders showing the raw-score sum + inference time.
        from gui.recon_labels import RECON_LABELS

        # Header row: title on the left, ✎ Correct toggle on the right.
        # Toggle unlocks the sliders for grower correction (Pattern A
        # proportional adjustment; sum always sits at 100). Auto-locks
        # after LOG ENTRY so every correction is a per-entry decision,
        # not a session-wide setting. See _on_correction_toggled +
        # _on_grower_slider_changed for the interaction logic.
        recon_header = QHBoxLayout()
        recon_header.setContentsMargins(0, 0, 0, 0)
        recon_label = QLabel("Live Classification (%)")
        recon_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        self._live_classifier_widgets.append(recon_label)
        recon_header.addWidget(recon_label, 1)

        self.correction_btn = QPushButton("✎ Correct")
        self.correction_btn.setCheckable(True)
        self.correction_btn.setFixedHeight(24)
        self.correction_btn.setStyleSheet(
            "QPushButton { background-color: #1a1a1a; color: #aaa; "
            "border: 1px solid #444; padding: 2px 10px; font-size: 11px; }"
            "QPushButton:checked { background-color: #7f1d3d; color: #fff; "
            "border: 1px solid #e11d48; }"
            "QPushButton:disabled { color: #444; border-color: #2a2a2a; }"
        )
        self.correction_btn.setToolTip(
            "Toggle to override the classifier's live prediction. Sliders "
            "become draggable and stay summed to 100% (proportional "
            "adjustment). The next LOG ENTRY captures both your belief and "
            "the classifier's — the pair feeds Yuxin's active-comparisons "
            "training signal. Auto-locks after each LOG ENTRY so every "
            "correction is a fresh decision."
        )
        self.correction_btn.clicked.connect(self._on_correction_toggled)
        self._live_classifier_widgets.append(self.correction_btn)
        recon_header.addWidget(self.correction_btn, 0)
        right.addLayout(recon_header)

        self._recon_sliders: dict[str, QSlider] = {}
        self._recon_value_labels: dict[str, QLabel] = {}
        recon_grid = QHBoxLayout()
        recon_grid.setSpacing(6)
        for name in RECON_LABELS:
            col = QVBoxLayout()
            col.setSpacing(2)
            lbl = QLabel(name)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("font-size: 11px;")
            self._live_classifier_widgets.append(lbl)
            col.addWidget(lbl)
            slider = QSlider(Qt.Orientation.Vertical)
            slider.setRange(0, 100)
            slider.setValue(0)
            slider.setEnabled(False)  # Option A: read-only display
            slider.setTickPosition(QSlider.TickPosition.TicksRight)
            slider.setTickInterval(25)
            slider.setFixedHeight(90)
            slider.setStyleSheet(self._SLIDER_STYLE_LIVE)
            self._live_classifier_widgets.append(slider)
            col.addWidget(slider, alignment=Qt.AlignmentFlag.AlignHCenter)
            val_lbl = QLabel("0%")
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_lbl.setStyleSheet("font-size: 11px; color: #0d9488;")
            self._live_classifier_widgets.append(val_lbl)
            col.addWidget(val_lbl)
            recon_grid.addLayout(col)
            self._recon_sliders[name] = slider
            self._recon_value_labels[name] = val_lbl
        right.addLayout(recon_grid)

        # Grower-correction Pattern A signal wiring. The handler no-ops
        # unless self._correction_active is True, so classifier-driven
        # slider updates (which use blockSignals for their own reasons)
        # never trigger it. Default-arg capture on ``n`` is required to
        # dodge Python's late-binding closure-over-loop-variable trap.
        for name, slider in self._recon_sliders.items():
            slider.valueChanged.connect(
                lambda v, n=name: self._on_grower_slider_changed(n, v)
            )

        # Transparency label: shows raw-score sum, inference time, OOD flag,
        # or error text depending on classifier state. Same slot updates
        # both this label and the slider values (see update_classifier_state).
        self._recon_status_label = QLabel("Classifier idle")
        self._recon_status_label.setStyleSheet("font-size: 10px; color: #888;")
        self._recon_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._live_classifier_widgets.append(self._recon_status_label)
        right.addWidget(self._recon_status_label)

        self._weak_primary_shadow_label = QLabel(
            "Brightness-robust 4-output shadow: disabled"
        )
        self._weak_primary_shadow_label.setWordWrap(True)
        self._weak_primary_shadow_label.setStyleSheet(
            "font-size: 10px; color: #a78bfa; border: 1px solid #4c1d95; "
            "padding: 4px;"
        )
        self._weak_primary_shadow_label.setToolTip(
            "Diagnostic only: all_extreme brightness-robust conditional "
            "probabilities for Twinned, c(6x2), rt13 and HTR. The shadow "
            "panel has no 1x1 column and is never used for advice, control, "
            "or automatic capture."
        )
        self._live_classifier_widgets.append(self._weak_primary_shadow_label)
        right.addWidget(self._weak_primary_shadow_label)

        # Note input
        note_label = QLabel("Log Entry")
        note_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        right.addWidget(note_label)

        self.log_note_input = QTextEdit()
        self.log_note_input.setPlaceholderText("What's happening now?")
        self.log_note_input.setStyleSheet(
            "QTextEdit { font-size: 15px; padding: 8px; font-family: Arial; }"
        )
        right.addWidget(self.log_note_input, 1)  # stretch to fill

        # Two-button action row: MARK EVENT (fast, single-tap) | LOG ENTRY
        # (deliberate, sliders + note). Landed Jul 10 2026 to resolve the
        # sub-second reconstruction problem — grower can flag "something
        # happened NOW" without opening the labeling flow. Same height so
        # neither dominates; distinct colors (amber vs teal) so the eye
        # doesn't confuse them under mid-growth attention pressure.
        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        action_row.setContentsMargins(0, 0, 0, 0)

        # MARK EVENT — placed LEFT so the grower's dominant-hand click path
        # hits the fast button first. Amber (#d97706) distinguishes it from
        # the teal LOG ENTRY. Keyboard shortcut "E" — avoids Space (used by
        # sliders) and Enter (note editor).
        self.mark_event_btn = QPushButton("⚡ MARK EVENT  (E)")
        self.mark_event_btn.setStyleSheet(
            "QPushButton { background-color: #d97706; color: white; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:disabled { background-color: #222; color: #666; }"
        )
        self.mark_event_btn.setFixedHeight(54)
        self.mark_event_btn.setToolTip(
            "Log a grower-marked event NOW. Single-tap — no note required. "
            "If the note field has text, it's captured with the event. "
            "Snapshots the current frame + sensor state. Multi-click OK for "
            "sub-second reconstructions. Shortcut: E."
        )
        self.mark_event_btn.clicked.connect(self._on_mark_event)
        action_row.addWidget(self.mark_event_btn, 1)

        # LOG ENTRY — the deliberate labeled path. Unchanged behavior.
        self.commit_btn = QPushButton("LOG ENTRY  (Ctrl+S)")
        self.commit_btn.setStyleSheet(
            "QPushButton { background-color: #0d9488; color: white; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:disabled { background-color: #222; color: #666; }"
        )
        self.commit_btn.setFixedHeight(54)
        self.commit_btn.clicked.connect(self._on_commit)
        action_row.addWidget(self.commit_btn, 1)

        right.addLayout(action_row)

        # Operator RHEED adjustments are lightweight timestamped annotations.
        # They never pause acquisition or change classifier/Equalizer state.
        # Explicit QC PASS/REJECT remains a separate one-frame label.
        rheed_qc_row = QHBoxLayout()
        rheed_qc_row.setSpacing(6)
        self.rheed_qc_status_label = QLabel(
            "RHEED: adjustment events ready"
        )
        self.rheed_qc_status_label.setWordWrap(True)
        self.rheed_qc_status_label.setStyleSheet(
            "color: #22c55e; font-size: 11px; padding: 3px 6px;"
        )
        rheed_qc_row.addWidget(self.rheed_qc_status_label, 1)

        self.rheed_direction_btn = QPushButton("Sample direction")
        self.rheed_direction_btn.setToolTip(
            "Record that the sample direction was adjusted. This does not "
            "change the view segment or pause any acquisition."
        )
        self.rheed_direction_btn.clicked.connect(
            lambda: self._emit_rheed_adjustment_event(
                "sample_direction_adjusted"
            ),
        )
        rheed_qc_row.addWidget(self.rheed_direction_btn, 0)

        self.rheed_current_btn = QPushButton("RHEED current")
        self.rheed_current_btn.setToolTip(
            "Record a RHEED beam-current adjustment without changing state."
        )
        self.rheed_current_btn.clicked.connect(
            lambda: self._emit_rheed_adjustment_event(
                "rheed_current_adjusted"
            ),
        )
        rheed_qc_row.addWidget(self.rheed_current_btn, 0)

        self.rheed_energy_btn = QPushButton("RHEED energy")
        self.rheed_energy_btn.setToolTip(
            "Record a RHEED beam-energy adjustment without changing state."
        )
        self.rheed_energy_btn.clicked.connect(
            lambda: self._emit_rheed_adjustment_event(
                "rheed_energy_adjusted"
            ),
        )
        rheed_qc_row.addWidget(self.rheed_energy_btn, 0)

        self.rheed_qc_pass_btn = QPushButton("IMAGE USABLE")
        self.rheed_qc_pass_btn.setToolTip(
            "Mark the current full RHEED acquisition image as usable for "
            "analysis. This describes image acquisition quality only; it is "
            "not a surface reconstruction or film-quality label."
        )
        self.rheed_qc_pass_btn.clicked.connect(
            lambda: self._emit_rheed_qc_label("qc_pass"),
        )
        rheed_qc_row.addWidget(self.rheed_qc_pass_btn, 0)

        self.rheed_qc_reject_btn = QPushButton("IMAGE UNUSABLE")
        self.rheed_qc_reject_btn.setToolTip(
            "Mark the current full RHEED acquisition image as unusable for "
            "analysis. This describes acquisition failure or obstruction, "
            "not surface reconstruction or film quality. Add a reason in "
            "the note box when possible."
        )
        self.rheed_qc_reject_btn.setStyleSheet(
            "QPushButton { background-color: #7f1d1d; color: white; }"
            "QPushButton:disabled { background-color: #222; color: #666; }"
        )
        self.rheed_qc_reject_btn.clicked.connect(
            lambda: self._emit_rheed_qc_label("qc_reject"),
        )
        rheed_qc_row.addWidget(self.rheed_qc_reject_btn, 0)
        right.addLayout(rheed_qc_row)

        # Keyboard shortcuts
        self._save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self._save_shortcut.activated.connect(self._on_commit)
        self._mark_shortcut = QShortcut(QKeySequence("E"), self)
        self._mark_shortcut.activated.connect(self._on_mark_event)

        content.addLayout(right, 1)
        layout.addLayout(content, 1)

        # Thin diagnostic footer for the auto-capture engine. Stays out of
        # the way during normal use; growers can ignore it unless they want
        # to see the live change-score / event count.
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        self.auto_capture_label = QLabel("Auto-capture: idle")
        self.auto_capture_label.setStyleSheet(
            "color: #888; font-size: 11px; padding: 4px 8px; "
            "background-color: #1a1a1a; border-top: 1px solid #333;"
        )
        self.auto_capture_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        footer.addWidget(self.auto_capture_label, 1)

        # Continuous-capture indicator (Jul 10 2026). Surfaces the "movie"
        # cadence the growers asked for at the group meeting — otherwise
        # the interval is buried on the Config tab and the grower can't
        # confirm at a glance that frames are actually landing. Text
        # format: "Capture: every 5s · 42 frames". Cyan (#0891b2) to
        # separate visually from auto-capture (grey) and manual events
        # (amber).
        self.continuous_capture_label = QLabel("Capture: idle")
        self.continuous_capture_label.setStyleSheet(
            "color: #0891b2; font-size: 11px; padding: 4px 8px; "
            "background-color: #1a1a1a; border-top: 1px solid #333;"
        )
        self.continuous_capture_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        footer.addWidget(self.continuous_capture_label, 0)

        # Manual-event counter — sits next to the auto-capture status so
        # both event streams are visible in one glance. Same footer
        # styling. Text stays "Manual events: N" as the counter climbs;
        # 0 case still shows the label so growers know the surface exists.
        self.manual_event_label = QLabel("Manual events: 0")
        self.manual_event_label.setStyleSheet(
            "color: #d97706; font-size: 11px; padding: 4px 8px; "
            "background-color: #1a1a1a; border-top: 1px solid #333;"
        )
        self.manual_event_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        footer.addWidget(self.manual_event_label, 0)

        # Pause toggle — soft halt of auto-capture only. Sensor logging,
        # heartbeat, and frame display all keep running. Emergency stop
        # (full session halt) remains on the top-bar STOP button.
        self.pause_auto_capture_btn = QPushButton("Pause Auto-Capture")
        self.pause_auto_capture_btn.setCheckable(True)
        self.pause_auto_capture_btn.setEnabled(False)
        self.pause_auto_capture_btn.setStyleSheet(
            "QPushButton { background-color: #1a1a1a; color: #aaa; "
            "border: 1px solid #444; padding: 2px 10px; font-size: 11px; }"
            "QPushButton:checked { background-color: #7c2d12; color: #fff; "
            "border: 1px solid #c2410c; }"
            "QPushButton:disabled { color: #555; border-color: #2a2a2a; }"
        )
        self.pause_auto_capture_btn.clicked.connect(
            self._on_pause_auto_capture_clicked,
        )
        footer.addWidget(self.pause_auto_capture_btn, 0)

        self.reconnect_rheed_btn = QPushButton("Reconnect RHEED")
        self.reconnect_rheed_btn.setToolTip(
            "Retry the detached kSA Live Video WGC connection without "
            "ending the current growth session."
        )
        self.reconnect_rheed_btn.setStyleSheet(
            "QPushButton { background-color: #7f1d1d; color: white; "
            "border: 1px solid #ef4444; padding: 2px 10px; "
            "font-size: 11px; font-weight: bold; }"
            "QPushButton:disabled { background-color: #3f1d1d; color: #888; }"
        )
        self.reconnect_rheed_btn.clicked.connect(
            self.reconnect_rheed_requested,
        )
        self.reconnect_rheed_btn.hide()
        footer.addWidget(self.reconnect_rheed_btn, 0)

        _btn_style = (
            "QPushButton { background-color: #1a1a1a; color: #94a3b8; "
            "border: 1px solid #334155; padding: 2px 8px; font-size: 11px; }"
            "QPushButton:hover { color: #e2e8f0; border-color: #475569; }"
        )
        self.btn_rheed_trend = QPushButton("RHEED Trend ↗")
        self.btn_rheed_trend.setFixedWidth(120)
        self.btn_rheed_trend.setStyleSheet(_btn_style)
        self.btn_rheed_trend.clicked.connect(self.open_rheed_trend_requested)
        footer.addWidget(self.btn_rheed_trend, 0)

        self.btn_temp_plot = QPushButton("Temp Plot ↗")
        self.btn_temp_plot.setFixedWidth(100)
        self.btn_temp_plot.setStyleSheet(_btn_style)
        self.btn_temp_plot.clicked.connect(self.open_temp_plot_requested)
        footer.addWidget(self.btn_temp_plot, 0)

        layout.addLayout(footer)

        self._tabs.addTab(tab, "Monitor")

    # ----- Direct-read Tab -------------------------------------------------

    def _build_direct_read_tab(self):
        """Mount the Direct-read tab — live growth-state fields sourced
        from EvapControl's own ``.elo`` binary log via
        ``drivers.evap_control.ElogReader``.

        Populated by ``EvapControlWorker`` when configured with
        ``mode="elog"`` in the Config tab. In "screengrab" or "dummy"
        mode the fields stay at their default ``"---"`` (the worker
        can't source them). Growers seeing all-dashes here should
        check the Evap Control mode selector in the Config tab.

        Layout (Design C, locked 2026-07-09): three ``QGroupBox``
        sections — Substrate (2 fields), Effusion cells (5), Plasma
        (3). The Plasma section is hidden by default and revealed
        only when at least one plasma field carries a value —
        ``ElogReader`` filters NaN → None, so an off plasma source
        presents as all-None from our side, matching the "hide when
        unused" affordance from the design mockup.

        See ``ElogReader.DEFAULT_VAR_MAP`` in
        ``drivers/evap_control.py`` for the elog-variable →
        EvapControlState field mapping. Adding a new cell requires:
        extend that map + the ``EvapControlState`` dataclass + the
        ``log_sensors`` schema in ``gui/growth_logger.py`` + this
        tab.
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Header names the source so all-dashes isn't mysterious.
        header = QLabel(
            "Live growth state from EvapControl's <b>.elo</b> log. "
            "Requires EvapControl mode = <b>elog</b> in the Config "
            "tab; otherwise fields stay at ---."
        )
        header.setStyleSheet("font-size: 11px; color: #888;")
        header.setWordWrap(True)
        layout.addWidget(header)
        self._evap_source_status_label = QLabel(
            "EvapControl source: unavailable"
        )
        self._evap_source_status_label.setStyleSheet(
            "font-size: 11px; color: #888;"
        )
        self._evap_source_status_label.setWordWrap(True)
        layout.addWidget(self._evap_source_status_label)

        _field_style = (
            "QFrame { border: 1px solid #555; } "
            "QLabel { background: transparent; }"
        )

        # --- Substrate section: Manipulator PV + setpoint ---
        # Pair rationale: the pyrometer on Monitor tab gives an
        # emissivity-corrected substrate temperature; Mani PV here is
        # a thermocouple reading from the manipulator stage. Both
        # visible = cross-check for thermocouple drift or pyrometer
        # emissivity errors.
        substrate_group = QGroupBox("Substrate (Manipulator)")
        substrate_layout = QHBoxLayout(substrate_group)
        self.substrate_pv_display = ValueDisplay(
            "Mani PV", "°C", 1,
        )
        self.substrate_sp_display = ValueDisplay(
            "Mani setpoint", "°C", 1,
        )
        for d in (self.substrate_pv_display, self.substrate_sp_display):
            d.setStyleSheet(_field_style)
            substrate_layout.addWidget(d)
        layout.addWidget(substrate_group)

        # --- Effusion cells section (count and labels from chamber config) ---
        cells_group = QGroupBox("Effusion cells")
        cells_layout = QHBoxLayout(cells_group)
        # self._cell_displays[i] corresponds to self._cfg.cell_display[i].
        # O-MBE: 5 elog-sourced cells; Ch-MBE: 7 ADS-sourced cells.
        self._cell_displays: list = []
        for cell_def in self._cfg.cell_display:
            d = ValueDisplay(cell_def["label"], "°C", 1)
            d.setStyleSheet(_field_style)
            cells_layout.addWidget(d)
            self._cell_displays.append(d)
        layout.addWidget(cells_group)

        # Ch-MBE direct-log sources are separate from the numbered ADS cell
        # widgets above. This preserves the verified elog source names without
        # presenting an unverified ADS CellN -> material assignment as fact.
        self._elog_source_displays: list = []
        if self._cfg.elog_source_display:
            elog_sources_group = QGroupBox("EvapControl log sources")
            elog_sources_layout = QHBoxLayout(elog_sources_group)
            for source_def in self._cfg.elog_source_display:
                d = ValueDisplay(source_def["label"], "°C", 1)
                d.setStyleSheet(_field_style)
                elog_sources_layout.addWidget(d)
                self._elog_source_displays.append(d)
            layout.addWidget(elog_sources_group)

        # --- Plasma section: hidden by default; revealed when any
        #     plasma field is populated. ElogReader treats NaN as
        #     "unavailable" (returns None), so all-None ⇒ plasma off.
        self.plasma_group = QGroupBox("Plasma source")
        plasma_layout = QHBoxLayout(self.plasma_group)
        self.plasma_dc_display = ValueDisplay("DC bias", "V", 1)
        self.plasma_fwd_display = ValueDisplay("Forward", "W", 1)
        self.plasma_rfl_display = ValueDisplay("Reflected", "W", 1)
        for d in (
            self.plasma_dc_display, self.plasma_fwd_display,
            self.plasma_rfl_display,
        ):
            d.setStyleSheet(_field_style)
            plasma_layout.addWidget(d)
        self.plasma_group.setVisible(False)
        layout.addWidget(self.plasma_group)

        layout.addStretch(1)  # push sections to top

        self._tabs.addTab(tab, "Direct-read")

    # ----- Events Tab ------------------------------------------------------

    def _build_events_tab(self):
        """Mount the EventsTab — review surface for auto-capture events.

        Lives between Monitor (live) and Session (config + notes + export)
        in the tab order so the grower's natural left-to-right scan is
        now → just-fired events → session admin.

        The tab's text gets a "(N)" badge whenever there are unreviewed
        events — pending or kept_default — so the catch-up case after a
        walk-away is legible at a glance from any other tab.
        """
        self.events_tab = EventsTab()
        self._events_tab_index = self._tabs.addTab(self.events_tab, "Events")
        self.events_tab.unreviewed_count_changed.connect(
            self._on_unreviewed_count_changed,
        )
        self.events_tab.blind_labeling_mode_changed.connect(
            self._on_blind_labeling_mode_changed,
        )

    def _on_unreviewed_count_changed(self, count: int):
        """Repaint the Events tab header with the unreviewed-event count.

        Empty badge when count == 0 to keep the header quiet during a
        fully-attended growth; non-zero counts surface explicitly.
        """
        label = "Events" if count == 0 else f"Events ({count})"
        self._tabs.setTabText(self._events_tab_index, label)

    # ----- Hidden legacy Equalizer compatibility ---------------------------

    def _build_hidden_live_equalizer_compatibility_widget(self) -> None:
        """Keep legacy Equalizer APIs available without exposing their UI.

        GrowthApp still calls this object's provenance and lifecycle methods
        when it reads older sessions containing calibration or ``live_labels``
        records. Keeping the object avoids changing that compatibility path,
        but deliberately never adding it to ``self._tabs`` ensures neither
        chamber presents Live Equalizer as an operator-facing labeling tool.
        """
        self.live_equalizer_tab = LiveEqualizerTab(parent=self)
        self.live_equalizer_tab.setObjectName(
            "legacyLiveEqualizerCompatibilityWidget"
        )
        self.live_equalizer_tab.hide()

    def _on_blind_labeling_mode_changed(self, active: bool) -> None:
        """Suppress every in-app model/Equalizer answer during blind review."""
        self._blind_labeling_mode = bool(active)
        for widget in self._live_classifier_widgets:
            widget.setVisible(not active)
            if active:
                widget.setEnabled(False)
        if active and hasattr(self, "live_equalizer_tab"):
            self.live_equalizer_tab.update_classifier_state(None)
        elif not active:
            self._apply_state()

    # ----- Scrubber Tab ----------------------------------------------------

    def _build_scrubber_tab(self):
        """Mount the ScrubberTab — retrospective timeline playback.

        Ships workstream #3 from the Jul 10 2026 group-meeting design
        shift. The scrubber pairs with MARK EVENT (Monitor tab) and
        continuous capture (heartbeat system) to complete the three-
        concern architecture (capture / mark / label). Its one job is
        letting the grower flip through the growth 'movie' after the
        fact; event labeling stays on the Events tab.

        Placed after Events in the tab order so the retrospective
        surfaces sit together — grower's mental model is now → auto-
        events → scrub-through-everything → session admin.
        """
        self.scrubber_tab = ScrubberTab()
        self._tabs.addTab(self.scrubber_tab, "Scrubber")

    # ----- Session Tab -----------------------------------------------------

    def _build_session_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # === Top half: Config (left) + Sensor Log (right) ===
        top_half = QHBoxLayout()
        top_half.setSpacing(12)

        # --- Config (upper-left) ---
        config_group = QGroupBox("Config")
        config_group.setMaximumWidth(420)
        config_form = QFormLayout(config_group)
        config_form.setContentsMargins(10, 16, 10, 8)
        config_form.setSpacing(6)
        config_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        self.config_interval_spin = QDoubleSpinBox()
        self.config_interval_spin.setRange(1.0, 600.0)
        self.config_interval_spin.setValue(1.0)
        self.config_interval_spin.setSuffix(" s")
        self.config_interval_spin.setDecimals(0)
        self.config_interval_spin.setSingleStep(1.0)
        config_form.addRow("Recording interval:", self.config_interval_spin)

        # Continuous-capture interval — how often we save a RHEED frame
        # regardless of detector flags. Historically named "heartbeat"
        # (internal identifier still `_heartbeat_*` for compatibility);
        # user-facing labels use the Jul 10 2026 grower vocabulary of
        # "continuous capture" / "movie of the growth" instead.
        #
        # Default 5 s per the May 21 joint decision (was env-var-only
        # before Jun 23). Range 1 s (dense) to 600 s (10 min, the old
        # default). Applied at session START — changing mid-session has
        # no effect. This is the primary feed for the scrubber timeline
        # (workstream #3), so denser intervals give the scrubber a
        # smoother movie at the cost of more disk.
        self.config_heartbeat_interval_spin = QDoubleSpinBox()
        self.config_heartbeat_interval_spin.setRange(1.0, 600.0)
        self.config_heartbeat_interval_spin.setValue(5.0)
        self.config_heartbeat_interval_spin.setSuffix(" s")
        self.config_heartbeat_interval_spin.setDecimals(0)
        self.config_heartbeat_interval_spin.setSingleStep(1.0)
        self.config_heartbeat_interval_spin.setToolTip(
            "How often to save a RHEED frame during a session — the "
            "'movie' that the scrubber will play back. Lower = denser "
            "data + smoother scrub (more disk + memory); 5 s default "
            "per May 21 joint group meeting. At 5 s, a 4-hour growth "
            "produces ~2,880 frames (~1.4 GB at ~500 KB/frame BMP)."
        )
        config_form.addRow(
            "Continuous capture interval:",
            self.config_heartbeat_interval_spin,
        )

        save_row = QHBoxLayout()
        self.config_save_path = QLineEdit()
        default_save = _default_save_path(self._cfg)
        self.config_save_path.setPlaceholderText(default_save)
        self.config_save_path.setText(default_save)
        save_row.addWidget(self.config_save_path)
        # Kept as an attribute so _set_config_widgets_enabled can lock it
        # during a running session — otherwise the grower could re-browse
        # to a different directory mid-session and silently split their
        # log into two locations.
        self.config_browse_btn = QPushButton("Browse")
        self.config_browse_btn.setFixedWidth(70)
        self.config_browse_btn.setStyleSheet(
            "QPushButton { font-size: 11px; padding: 4px 8px; }"
        )
        self.config_browse_btn.clicked.connect(self._on_config_browse)
        save_row.addWidget(self.config_browse_btn)
        config_form.addRow("Save folder:", save_row)

        self.config_prefix = QLineEdit("growth")
        config_form.addRow("Filename prefix:", self.config_prefix)

        self.config_camera_mode = QComboBox()
        self.config_camera_mode.addItems(
            [
                "dummy",
                "dummy_c6x2",
                "dummy_tw",
                "dummy_rt13_tilted",
                "screengrab",
                "screengrab_mss",
                "vimba",
            ]
        )
        self.config_camera_mode.setItemData(
            0,
            "Stable experimental STO 1x1 frame for alignment testing.",
        )
        self.config_camera_mode.setItemData(
            1,
            "Stable experimental STO c(6x2) frame for alignment testing.",
        )
        self.config_camera_mode.setItemData(
            2,
            "Stable experimental STO Twinned (2x1) frame for alignment.",
        )
        self.config_camera_mode.setItemData(
            3,
            "RT13_20 experimental frame rotated 15 degrees for alignment demo.",
        )
        self.config_camera_mode.setItemData(
            4,
            "Windows Graphics Capture of the detached kSA Live Video window.",
        )
        self.config_camera_mode.setItemData(
            5,
            "Legacy monitor-pixel capture; overlays can contaminate frames.",
        )
        self.config_camera_mode.setItemData(
            6,
            "Direct read from the AVT camera through the Vimba SDK.",
        )
        self.config_camera_mode.setCurrentText(self._cfg.camera_mode_default)
        config_form.addRow("Camera mode:", self.config_camera_mode)

        self.config_camera_exposure_ms = QDoubleSpinBox()
        safe_exposure_max_ms = (
            900.0 / self._cfg.camera_fps
            if self._cfg.camera_fps > 0 else 900.0
        )
        self.config_camera_exposure_ms.setRange(0.0, safe_exposure_max_ms)
        self.config_camera_exposure_ms.setDecimals(0)
        self.config_camera_exposure_ms.setSingleStep(10.0)
        self.config_camera_exposure_ms.setSuffix(" ms")
        self.config_camera_exposure_ms.setSpecialValueText("Keep current")
        configured_exposure = self._cfg.camera_exposure_us
        self.config_camera_exposure_ms.setValue(
            configured_exposure / 1000.0 if configured_exposure else 0.0
        )
        self.config_camera_exposure_ms.setToolTip(
            "Manual exposure for direct Vimba mode. 'Keep current' performs "
            f"no camera write. The {safe_exposure_max_ms:.0f} ms ceiling "
            f"preserves headroom for the {self._cfg.camera_fps:g} Hz "
            "acquisition loop. Applied at ARM, and changeable live with "
            "Apply; every change is recorded. It does not modify the camera "
            "user set, so it reverts on power-cycle."
        )
        # Unlike every other config widget, this one stays live while armed.
        # It is not a setting the worker committed to at construction — it is
        # a hardware value the camera reads back and confirms, so a mid-session
        # change is verifiable rather than silently divergent.
        self.btn_apply_exposure = QPushButton("Apply")
        self.btn_apply_exposure.setFixedWidth(64)
        self.btn_apply_exposure.clicked.connect(self._on_apply_exposure_clicked)
        exposure_row = QHBoxLayout()
        exposure_row.setContentsMargins(0, 0, 0, 0)
        exposure_row.setSpacing(4)
        exposure_row.addWidget(self.config_camera_exposure_ms, 1)
        exposure_row.addWidget(self.btn_apply_exposure, 0)
        exposure_holder = QWidget()
        exposure_holder.setLayout(exposure_row)
        config_form.addRow("Direct exposure:", exposure_holder)
        self.config_camera_mode.currentTextChanged.connect(
            self._update_camera_exposure_enabled,
        )
        self.config_camera_exposure_ms.valueChanged.connect(
            lambda _value: self._update_camera_exposure_enabled(),
        )
        self._update_camera_exposure_enabled()

        self.config_pyrometer_mode = QComboBox()
        self.config_pyrometer_mode.addItems(["dummy", "exactus", "modbus", "screengrab"])
        self.config_pyrometer_mode.setCurrentText(self._cfg.pyrometer_mode_default)
        config_form.addRow("Pyrometer mode:", self.config_pyrometer_mode)

        # Seeded from the active chamber config so each chamber opens on its
        # own port instead of a hardcoded COM4. Ch-MBE's probe is on COM3
        # (verified 2026-08-05); typing it by hand every session was the
        # failure mode this replaces. The grower can still override here.
        self.config_exactus_port = QLineEdit(self._cfg.pyrometer_port)
        config_form.addRow("Exactus port:", self.config_exactus_port)

        self.config_exactus_baud = QComboBox()
        self.config_exactus_baud.addItems(
            ["9600", "19200", "38400", "57600", "115200"]
        )
        self.config_exactus_baud.setCurrentText(str(self._cfg.pyrometer_baudrate))
        config_form.addRow("Exactus baud:", self.config_exactus_baud)

        self.config_mistral_mode = QComboBox()
        # "jsonrpc" — direct-read of the Scandes JSON-RPC 2.0 backend at
        # http://10.0.42.231:9000/api (discovered Jun 23 2026). Multi-client
        # safe alongside a running MistralGui. See drivers/mistral_jsonrpc.py
        # and scripts/test_mistral_jsonrpc_discovery.py. Selecting this mode
        # before running the discovery probe on Bulbasaur produces a
        # connected driver with all-None V/I readings (read_config empty)
        # — no crashes, just no data until set_read_config populates methods.
        self.config_mistral_mode.addItems(["dummy", "screengrab", "jsonrpc", "ads"])
        self.config_mistral_mode.setCurrentText(self._cfg.mistral_mode_default)
        config_form.addRow("MISTRAL mode:", self.config_mistral_mode)

        self.config_evap_mode = QComboBox()
        # "elog" — direct-read of EvapControl's own .elo binary log
        # (drivers/elog.py). No OCR, no window positioning. See
        # drivers/evap_control.py:ElogReader.
        #
        # Default flipped from "screengrab" to "elog" 2026-07-09
        # (per P3 decision): direct-read is strictly better when
        # EvapControl is running. Bulbasaur end-to-end validation
        # pending — session must arm cleanly with the new default,
        # all 10 Direct-read fields must populate with live values,
        # values must cross-check against the EvapControl window's
        # own readouts. See elog_direct_read_may15.md Jul 9 update.
        #
        # Failure mode: if EvapControl isn't running, ElogReader
        # .connect() raises → worker emits connected=False + error
        # → Direct-read shows "---" everywhere. Grower flips the
        # dropdown back to "screengrab" for OCR fallback. Auto-
        # fallback deferred (memory Jul 9 open question #1).
        self.config_evap_mode.addItems(["dummy", "elog", "screengrab"])
        self.config_evap_mode.setCurrentText(self._cfg.evap_mode_default)
        config_form.addRow("Evap Control mode:", self.config_evap_mode)

        # Enable/disable the live classifier. Off = classifier worker
        # never starts, main-tab sliders stay at zero and status shows
        # "Classifier disabled". Useful when the classifier is misbehaving
        # (e.g. Bulbasaur without torch) or when a grower explicitly
        # doesn't want it — the rest of the app runs unchanged.
        self.config_classifier_enabled = QCheckBox()
        self.config_classifier_enabled.setChecked(True)
        config_form.addRow("Live classifier:", self.config_classifier_enabled)

        self.config_weak_primary_shadow_enabled = QCheckBox()
        self.config_weak_primary_shadow_enabled.setChecked(False)
        self.config_weak_primary_shadow_enabled.setToolTip(
            "Run the complete 36-head all_extreme four-output ensemble as a "
            "non-actionable diagnostic. It has no 1x1 output, never replaces "
            "the deployed classifier, and does not drive automatic capture."
        )
        config_form.addRow(
            "4-output shadow:", self.config_weak_primary_shadow_enabled,
        )

        # Snapshot of the tooltips each config widget carries in its
        # "unlocked" (idle) state. When the session transitions to armed
        # or running, _set_config_widgets_enabled overwrites the tooltip
        # with a "🔒 Disarm to change" reminder; on disarm it restores
        # from this dict. Prevents the Jul 8 Path Y bug where unchecking
        # "Live classifier" mid-session silently diverged the checkbox
        # from the running worker (see bulbasaur_lab_day_jul07 → follow-ups).
        self._config_widget_original_tooltips: dict = {
            w: w.toolTip() for w in (
                self.config_interval_spin,
                self.config_heartbeat_interval_spin,
                self.config_save_path,
                self.config_browse_btn,
                self.config_prefix,
                self.config_camera_mode,
                # config_camera_exposure_ms is deliberately ABSENT: it is the
                # one hardware control that stays usable while armed, and
                # _update_camera_exposure_enabled owns its enable state and
                # tooltip. Adding it back here would re-lock it on ARM and
                # undo the live-exposure feature.
                self.config_pyrometer_mode,
                self.config_exactus_port,
                self.config_exactus_baud,
                self.config_mistral_mode,
                self.config_evap_mode,
                self.config_classifier_enabled,
                self.config_weak_primary_shadow_enabled,
            )
        }

        top_half.addWidget(config_group)

        # --- Sensor Log (upper-right) ---
        sensor_container = QVBoxLayout()
        sensor_container.setSpacing(4)
        sensor_label = QLabel("Sensor Log \u2014 Interval Reads")
        sensor_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        sensor_container.addWidget(sensor_label)

        self.sensor_log_table = QTableWidget(0, 5)
        self.sensor_log_table.setHorizontalHeaderLabels(
            ["Time", "Temp (\u2103)", "V (V)", "I (A)", "P (mbar)"]
        )
        s_header = self.sensor_log_table.horizontalHeader()
        s_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.sensor_log_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.sensor_log_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.sensor_log_table.verticalHeader().setVisible(False)
        sensor_container.addWidget(self.sensor_log_table)

        top_half.addLayout(sensor_container, 1)

        layout.addLayout(top_half)

        # === Bottom half: Growth Notes (full width) ===
        notes_label = QLabel("Growth Notes \u2014 Grower Commits")
        notes_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(notes_label)

        self.growth_notes_table = QTableWidget(0, 5)
        self.growth_notes_table.setHorizontalHeaderLabels(
            ["Time", "Temp (\u2103)", "V (V)", "I (A)", "Note"]
        )
        n_header = self.growth_notes_table.horizontalHeader()
        # Columns 0-3: fixed width, evenly sharing ~50% of space
        for col in range(4):
            n_header.setSectionResizeMode(
                col, QHeaderView.ResizeMode.Interactive
            )
            self.growth_notes_table.setColumnWidth(col, 100)
        # Column 4 (Note): stretches to fill remaining ~50%
        n_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.growth_notes_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.growth_notes_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.growth_notes_table.verticalHeader().setVisible(False)
        layout.addWidget(self.growth_notes_table, 1)

        # Export button row — CSV growth log + optional mp4 time-lapse.
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        # Export Movie — Jul 10 2026 addition. Stitches heartbeat_log.csv's
        # BMP frames into a growth_movie.mp4 at 15 fps default (75× speedup
        # at 5-second capture; 4-hour growth plays in ~3.2 min). Runs on a
        # QThread so the GUI stays responsive during the 30-90s encode.
        # Positioned LEFT of the Growth Log export so the eye reads
        # "movie → log" in scan order; both blue but movie has a lighter
        # shade to signal secondary export.
        self.export_movie_btn = QPushButton("Export Movie")
        self.export_movie_btn.setStyleSheet(
            "QPushButton { background-color: #0284c7; color: white; "
            "font-size: 14px; padding: 10px 20px; }"
        )
        self.export_movie_btn.setToolTip(
            "Encode this session's continuous-capture frames into an mp4 "
            "time-lapse at 15 fps (75× speedup at 5-second capture). "
            "Runs in the background; status bar shows progress."
        )
        self.export_movie_btn.clicked.connect(
            lambda: self.movie_export_requested.emit()
        )
        btn_row.addWidget(self.export_movie_btn)

        self.export_btn = QPushButton("Export Growth Log")
        self.export_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-size: 14px; padding: 10px 20px; }"
        )
        self.export_btn.clicked.connect(lambda: self.export_requested.emit())
        btn_row.addWidget(self.export_btn)
        layout.addLayout(btn_row)

        self._tabs.addTab(tab, "Session")

    # ----- State machine ---------------------------------------------------

    _CONFIG_LOCKED_TOOLTIP = (
        "🔒 Disarm to change hardware config — arming reads these once "
        "and commits to them for the session. See Jul 8 2026 lab findings "
        "for context (unchecking mid-session used to silently diverge "
        "from the running worker)."
    )

    def _update_camera_exposure_enabled(self, mode: str = "") -> None:
        """Own the enable state of the exposure spinbox and its Apply button.

        The spinbox is usable in every session state for direct modes: in idle
        it seeds the ARM-time write, and while armed or running it stages a
        live change. Apply is the thing that actually reaches hardware, so it
        is gated further — a camera must be running, and "Keep current" (0) is
        not a value that can be applied.
        """
        selected_mode = str(mode or self.config_camera_mode.currentText())
        direct = selected_mode in ("vimba", "direct")
        self.config_camera_exposure_ms.setEnabled(direct)
        live = direct and self._state in ("armed", "running")
        self.btn_apply_exposure.setEnabled(
            live and self.config_camera_exposure_ms.value() > 0
        )
        if not direct:
            self.btn_apply_exposure.setToolTip(
                "Exposure is a direct-Vimba control. Screengrab and dummy "
                "modes have no camera exposure to set."
            )
        elif not live:
            self.btn_apply_exposure.setToolTip(
                "ARM first — a live exposure change needs a running camera."
            )
        elif self.config_camera_exposure_ms.value() <= 0:
            self.btn_apply_exposure.setToolTip(
                "'Keep current' is not a value to apply. Dial in a target "
                "exposure first."
            )
        else:
            self.btn_apply_exposure.setToolTip(
                "Write this exposure to the camera now, without disarming. "
                "The confirmed readback is announced and logged, and the "
                "RHEED trend marks the discontinuity."
            )

    def _on_apply_exposure_clicked(self) -> None:
        """Translate the spinbox's milliseconds into the driver's µs."""
        exposure_ms = float(self.config_camera_exposure_ms.value())
        if exposure_ms <= 0:
            return
        self.apply_exposure_requested.emit(exposure_ms * 1000.0)

    def _set_config_widgets_enabled(self, enabled: bool) -> None:
        """Lock/unlock the config panel widgets in bulk.

        When ``enabled`` is False, every config widget is disabled AND its
        tooltip is replaced with a "🔒 Disarm to change" reminder so the
        grower has an obvious explanation for why they can't click. When
        ``enabled`` is True, the original tooltips are restored from the
        snapshot dict built in ``_build_ui``.

        Called from ``_apply_state`` on every state transition. Idle →
        unlocked; armed/running → locked. Prevents the Path Y bug where
        unchecking "Live classifier" during a session had no effect on
        the running worker but silently corrupted paired data.
        """
        for widget, orig_tooltip in self._config_widget_original_tooltips.items():
            widget.setEnabled(enabled)
            if enabled:
                widget.setToolTip(orig_tooltip)
            else:
                widget.setToolTip(self._CONFIG_LOCKED_TOOLTIP)
        # Unconditional: the exposure control is outside the bulk lock, and
        # ARM is exactly when its Apply button becomes usable. Running this
        # only on unlock would leave Apply dead for the whole session.
        self._update_camera_exposure_enabled()

    def _apply_state(self):
        s = self._state
        # The hidden compatibility widget may not exist yet during the first
        # _apply_state from __init__. Guard the legacy lifecycle calls so an
        # early state application does not raise AttributeError.
        _le_tab = getattr(self, "live_equalizer_tab", None)
        if s == "idle":
            self.arm_btn.setText("ARM")
            self.arm_btn.setStyleSheet(BTN_ARM)
            self.arm_btn.setEnabled(True)
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.commit_btn.setEnabled(False)
            self.mark_event_btn.setEnabled(False)
            if _le_tab is not None:
                _le_tab.set_save_enabled(False)
            self.sample_id_input.setEnabled(True)
            self.grower_input.setEnabled(True)
            # Config panel: fully editable in idle.
            self._set_config_widgets_enabled(True)
        elif s == "armed":
            self.arm_btn.setText("DISARM")
            self.arm_btn.setStyleSheet(BTN_DISARM)
            self.arm_btn.setEnabled(True)
            # NOT unconditionally enabled: ARM means the worker started, not
            # that the camera ever delivered. _refresh_start_enabled turns it
            # on when the first qualifying frame arrives, and off again if
            # delivery stops.
            self.start_btn.setEnabled(self.has_live_camera_frame())
            self.start_btn.setStyleSheet(BTN_START)
            self.stop_btn.setEnabled(False)
            self.commit_btn.setEnabled(False)
            self.mark_event_btn.setEnabled(False)
            if _le_tab is not None:
                _le_tab.set_save_enabled(False)
            self.sample_id_input.setEnabled(True)
            self.grower_input.setEnabled(True)
            # Config panel: locked — worker states already committed to
            # the values that were current at arm time.
            self._set_config_widgets_enabled(False)
        elif s == "running":
            # The session clock starts HERE, not on the button click: this
            # branch is reached only after GrowthApp accepted the request and
            # opened the session.
            #
            # Guarded on the TIMER, not on _start_time. A redundant
            # set_state("running") mid-session must not restart the clock and
            # reset elapsed time — but _start_time survives STOP (it is only
            # cleared by reset_displays on disarm) because the export path
            # still reads it, so guarding on that would leave a second session
            # in the same arm cycle with a dead clock.
            if not self._elapsed_timer.isActive():
                self._start_time = datetime.now()
                self._elapsed_timer.start()
            self.arm_btn.setEnabled(False)
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.stop_btn.setStyleSheet(BTN_STOP)
            self.commit_btn.setEnabled(True)
            # MARK EVENT mirrors commit_btn's gating — enabled once the
            # session is actively logging so every mark lands in a real
            # session dir. Idle/armed clicks would go nowhere since the
            # logger hasn't opened manual_events.csv yet.
            self.mark_event_btn.setEnabled(True)
            # Preserve the legacy save gate for historical integrations even
            # though no Equalizer control is exposed in the tab bar.
            if _le_tab is not None:
                _le_tab.set_save_enabled(True)
            self.sample_id_input.setEnabled(False)
            self.grower_input.setEnabled(False)
            # Config panel: stays locked — session in progress.
            self._set_config_widgets_enabled(False)

        self._update_rheed_qc_controls()

    @property
    def state(self) -> str:
        """Current top-bar state: "idle", "armed", or "running".

        Read-only accessor so GrowthApp can tell a pre-session ARM from a
        live session without reaching into ``_state``. The distinction
        matters for camera failures: refusing to arm should return the GUI
        to idle, while losing the camera mid-growth must leave the session
        and its sensor logging alone.
        """
        return self._state

    def set_state(self, new_state: str):
        self._state = new_state
        self._apply_state()

    def _on_arm_clicked(self):
        if self._state == "idle":
            self.arm_requested.emit()
        elif self._state == "armed":
            self.disarm_requested.emit()

    def _on_start_clicked(self):
        # Deliberately does NOT start the clock. GrowthApp can still refuse
        # this request — no live frame, an unrecovered log transaction — and
        # it returns without unwinding anything the click did. Starting the
        # elapsed timer here meant a refused START left a running clock and a
        # populated _start_time behind an "armed" UI, so the next successful
        # start inherited a session age that predated the session.
        #
        # The clock now starts in _apply_state("running"), which only happens
        # after every precondition and the session initialisation succeeded.
        if self._state != "armed":
            return
        if not self.has_live_camera_frame():
            # Defense in depth: the button should already be disabled.
            return
        self.start_requested.emit()

    def _on_stop_clicked(self):
        if self._state == "running":
            self._elapsed_timer.stop()
            self.stop_requested.emit()

    # ----- Elapsed timer ---------------------------------------------------

    def _tick_elapsed(self):
        if self._start_time is None:
            return
        delta = datetime.now() - self._start_time
        total_s = delta.total_seconds()
        h = int(total_s // 3600)
        m = int((total_s % 3600) // 60)
        s = total_s % 60
        self.elapsed_display.value.setText(f"{h:02d}:{m:02d}:{s:05.2f}")

    def get_elapsed_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return (datetime.now() - self._start_time).total_seconds()

    # ----- State update methods (called by GrowthApp fan-out) -------------

    def update_psu_state(self, state: PowerSupplyState):
        self._latest_psu = state
        if state.connected:
            self.voltage_display.set_value(state.voltage_measured)
            self.current_display.set_value(state.current_measured)
        else:
            self.voltage_display.value.setText("---")
            self.current_display.value.setText("---")

    def update_pyrometer_state(self, state: PyrometerState):
        self._latest_pyro = state
        if state.has_valid_reading:
            self.temp_display.value.setText(f"{state.temperature:.0f} \u2103")
        else:
            # Covers both disconnected and connected-but-no-reading-yet
            # (post-connect / post-failed-batch). Rendering "---" instead
            # of "0 \u2103" prevents the header label from advertising a
            # spurious reading when the sensor is quiet.
            self.temp_display.value.setText("---")

    def update_mistral_state(self, state: MistralState):
        self._latest_mistral = state
        if state.connected and state.valid and state.v_actual is not None:
            self.voltage_display.set_value(state.v_actual)
        else:
            self.voltage_display.value.setText("---")
        if state.connected and state.valid and state.i_actual is not None:
            self.current_display.set_value(state.i_actual)
        else:
            self.current_display.value.setText("---")
        # ADS extended cell temps (mode="ads").
        # Populates _cell_displays widgets only when the chamber's
        # `ads_display_confirmed` flag is True (Ch-MBE — cell_display
        # uses numeric labels aligned with ADS Cell{N}). On O-MBE the
        # flag is False because cell_display uses material labels
        # (Sr, Eu, Er, etc.) whose Cell{N} → material mapping is
        # still unconfirmed — writing ADS Cell{N} into a widget labeled
        # "Sr" would silently mis-display. ADS data still flows to CSV
        # via update_mistral_state → sensor_log.
        # A follow-up will add a dedicated MISTRAL PSU-state panel that
        # shows all `ads_cell_count` cells by number, decoupled from
        # _cell_displays.
        if (
            state.valid
            and state.ads_cells
            and self._cfg.ads_display_confirmed
        ):
            for i, display in enumerate(self._cell_displays, start=1):
                if i > self._cfg.ads_cell_count:
                    break   # skip cells that don't exist on this PLC
                t = state.ads_cells.get(f"cell{i}_T")
                if t is None:
                    display.value.setText("---")
                else:
                    display.set_value(t)

    def update_evap_state(self, state: EvapControlState):
        self._latest_evap = state
        source_status = str(getattr(state, "source_status", "unavailable") or "unavailable")
        source_age_ms = getattr(state, "source_age_ms", None)
        source_age = (
            f"{source_age_ms / 1000.0:.1f} s old"
            if source_age_ms is not None else "age unavailable"
        )
        attempt_source = str(
            getattr(state, "attempt_source_at_utc", None) or "unknown source time"
        )
        if state.mode != "elog":
            source_text = (
                f"EvapControl source freshness: not available in {state.mode or 'unknown'} mode"
            )
            source_color = "#888"
        elif getattr(state, "source_stale", False):
            source_text = (
                f"EvapControl source: STALE · {attempt_source} · {source_age}"
            )
            source_color = "#ff6b6b"
        elif source_status in {"unchanged", "regressed"}:
            source_text = (
                "EvapControl source: waiting for a new record · "
                f"{attempt_source} · {source_age} · {source_status}"
            )
            source_color = "#f0ad4e"
        elif state.valid and source_status == "advanced":
            source_text = (
                f"EvapControl source: fresh · {attempt_source} · {source_age}"
            )
            source_color = "#4caf50"
        elif state.error:
            source_text = f"EvapControl source: unavailable · {state.error}"
            source_color = "#ff6b6b"
        else:
            # Compatibility states from older/custom drivers have no source
            # record contract. Never label them fresh based on values alone.
            source_text = "EvapControl source freshness: unavailable"
            source_color = "#888"
        self._evap_source_status_label.setText(source_text)
        self._evap_source_status_label.setStyleSheet(
            f"font-size: 11px; color: {source_color};"
        )
        # Monitor-tab pressure display (kept as-is; both screengrab
        # and elog modes source it).
        p = state.chamber_pressure_mbar
        if state.connected and state.valid and p is not None:
            self.pressure_display.value.setText(f"{p:.2e} mbar")
        else:
            self.pressure_display.value.setText("---")

        # Direct-read tab: substrate + cells + plasma. When the worker
        # is disconnected (never armed, or ElogReader.connect() raised
        # because no live .elo file exists), every field returns to
        # "---" regardless of last populated value — matches the
        # pressure display's connection-conditional behavior above.
        # When the worker is connected but the field is None (e.g.
        # "screengrab" mode which only sources pressure, or "elog"
        # mode with the variable absent from the elog schema), same
        # "---".
        connected = state.connected and state.valid
        # Cell displays: driven by EvapControlState fields when state_field
        # is set (O-MBE elog path). Ch-MBE cells have state_field=None and
        # are updated from ads_cells in update_mistral_state() instead, so an
        # independent EvapControl tick must not clear those ADS-owned widgets.
        for display, cell_def in zip(self._cell_displays, self._cfg.cell_display):
            sf = cell_def.get("state_field")
            if not sf:
                continue
            val = getattr(state, sf, None) if connected else None
            if val is None:
                display.value.setText("---")
            else:
                display.set_value(val)
        for display, source_def in zip(
            self._elog_source_displays, self._cfg.elog_source_display,
        ):
            value = (
                getattr(state, source_def["state_field"], None)
                if connected else None
            )
            if value is None:
                display.value.setText("---")
            else:
                display.set_value(value)
        for display, value in (
            (self.substrate_pv_display,
             state.substrate_temp_pv_C if connected else None),
            (self.substrate_sp_display,
             state.substrate_temp_setpoint_C if connected else None),
            (self.plasma_dc_display,
             state.plasma_dc_bias_V if connected else None),
            (self.plasma_fwd_display,
             state.plasma_forward_W if connected else None),
            (self.plasma_rfl_display,
             state.plasma_reflected_W if connected else None),
        ):
            if value is None:
                display.value.setText("---")
            else:
                display.set_value(value)

        # Plasma section visibility. Only when at least one plasma
        # field carries a value — ElogReader NaN-filters unavailable
        # variables to None, so a shut-off plasma source presents as
        # all-None here and the section stays hidden.
        plasma_on = connected and any(
            v is not None for v in (
                state.plasma_dc_bias_V,
                state.plasma_forward_W,
                state.plasma_reflected_W,
            )
        )
        self.plasma_group.setVisible(plasma_on)

    def has_live_camera_frame(self) -> bool:
        """Whether the camera has delivered a usable frame this arm cycle.

        Reads the LAST state rather than a "did we ever see one" flag: a camera
        that delivered frames and then died must not keep permitting a session
        start. The worker's starvation deadline clears ``connected`` once
        delivery stops, so both never-started and went-quiet fail this.
        """
        state = self._latest_camera
        if state is None:
            return False
        return bool(
            getattr(state, "connected", False)
            and getattr(state, "valid", False)
            and getattr(state, "frame", None) is not None
        )

    def _refresh_start_enabled(self) -> None:
        """START is available only while armed AND holding a live frame.

        ARM only proves the worker started; direct Vimba reads are
        edge-triggered, so a camera can connect and never fire a callback.
        Leaving START clickable in that window let a grower begin a growth
        with no RHEED at all — and the slot-side rejection came too late,
        after the click had already started the session clock.
        """
        if self._state != "armed":
            return
        enabled = self.has_live_camera_frame()
        self.start_btn.setEnabled(enabled)
        self.start_btn.setToolTip(
            "" if enabled else
            "Waiting for the first live RHEED frame. If this does not clear, "
            "DISARM and check camera access (kSA / Vimba X Viewer)."
        )

    def update_camera_state(self, state: CameraState):
        self._latest_camera = state
        self._refresh_start_enabled()
        if state.frame is not None:
            self._current_frame = state.frame
            self._display_frame(state.frame)
            # Feed the hidden compatibility widget too. Attribute-guarded
            # because early camera state emission can race GUI construction.
            if hasattr(self, "live_equalizer_tab"):
                self.live_equalizer_tab.update_camera_frame(
                    state.frame, self.get_current_capture_metadata(),
                )
        else:
            # Never retain a previously valid frame after a capture error.
            # Heartbeat/manual/classifier paths all read this cache, so
            # clearing it is the fail-closed boundary for image logging.
            self._current_frame = None
            self.rheed_image_label.clear()
            self.rheed_image_label.setText("RHEED unavailable")
            if hasattr(self, "live_equalizer_tab"):
                self.live_equalizer_tab.clear_camera_frame(
                    "RHEED unavailable",
                )
        self._update_rheed_qc_controls()

    def clear_camera_provenance(self) -> None:
        """Drop the prior arm cycle's camera readback before reconnecting."""
        self._latest_camera = None

    # Value-label style presets. Kept as constants so update_classifier_state
    # doesn't allocate style strings per emission (5-slider hot path at 2 Hz).
    _RECON_VAL_STYLE_NORMAL = "font-size: 11px; color: #0d9488;"
    _RECON_VAL_STYLE_ARGMAX = (
        "font-size: 13px; color: #14b8a6; font-weight: bold;"
    )
    _RECON_VAL_STYLE_PLACEHOLDER = "font-size: 11px; color: #666;"
    _RECON_STATUS_STYLE_INFO = "font-size: 10px; color: #888;"
    _RECON_STATUS_STYLE_WARN = "font-size: 10px; color: #d97706;"  # amber
    _RECON_STATUS_STYLE_ERROR = "font-size: 10px; color: #c33;"    # red
    # Correction-mode styles: rose (#e11d48) to distinguish grower-driven
    # sliders from classifier-driven at a glance. Same color also on the
    # ✎ Correct button checked state so the whole section reads "in
    # correction" instantly.
    _RECON_STATUS_STYLE_CORRECTION = (
        "font-size: 10px; color: #e11d48; font-weight: bold;"
    )
    _RECON_VAL_STYLE_CORRECTION = (
        "font-size: 11px; color: #e11d48; font-weight: bold;"
    )
    _SLIDER_STYLE_LIVE = (
        "QSlider::groove:vertical { background: #333; width: 6px; }"
        "QSlider::handle:vertical { background: #0d9488; height: 12px; "
        "margin: 0 -4px; border-radius: 6px; }"
        "QSlider::sub-page:vertical { background: #555; }"
        "QSlider::add-page:vertical { background: #0d9488; }"
    )
    _SLIDER_STYLE_CORRECTION = (
        "QSlider::groove:vertical { background: #3f0a1a; width: 6px; }"
        "QSlider::handle:vertical { background: #e11d48; height: 14px; "
        "margin: 0 -5px; border-radius: 7px; }"
        "QSlider::sub-page:vertical { background: #555; }"
        "QSlider::add-page:vertical { background: #e11d48; }"
    )
    _ARGMAX_HIGHLIGHT_THRESHOLD = 30  # only highlight when clearly above uniform-20

    def update_classifier_state(self, state: ClassifierState):
        """Route ClassifierState to the recon sliders + status label.

        Five display modes, chosen in this order:

        1. **loading** — bridge still initializing. Status: "Loading
           classifier…"; sliders left at whatever they were.
        2. **error** — bridge load or classify failed. Status: red
           "⚠ <error>"; sliders left unchanged.
        3. **warming up** — worker is ready but no non-OOD frame has
           arrived yet, so ``smoothed_percent`` is just the ready-time
           uniform-20 placeholder. Value labels show "—" (not "20%")
           so growers don't misread the placeholder as a real
           prediction. Status: "Waiting for frames…" or
           "Warming up — quality X.XX; no confident data yet".
        4. **OOD** — at least one confident classification has occurred,
           but the current frame is out-of-distribution. Sliders remain
           frozen at the last confident smoothed value (the worker
           enforces the freeze). Status: amber
           "OOD — quality X.XX | Nms | showing last confident".
        5. **normal** — confident classification, sliders reflect the
           freshly-EMA'd smoothed_percent. Status includes raw_sum,
           quality, and inference time for transparency. If any class
           clearly leads (> 30%), its value label is highlighted in
           bold + brighter teal for at-a-glance readability.

        Every non-error state also sets a hover-tooltip on the status
        label that includes the model version and explains what the
        current status means — useful for growers who don't speak ML
        fluently and for anyone debugging on Bulbasaur.

        Sliders are read-only (Option A) so ``blockSignals`` isn't
        strictly required, but it's cheap insurance if we ever re-enable
        interactivity for a grower-override mode.

        Correction-mode short-circuit: when self._correction_active is
        True, the grower is currently driving the sliders and the
        classifier's live output must NOT overwrite them. We still cache
        the state (so _on_commit can pair grower vs classifier in the
        log entry) and keep the status label showing the correction-mode
        message set by _on_correction_toggled. All slider/value-label
        rendering paths below are skipped.
        """
        # Cache first — needed for _on_commit's classifier_recon_* fields
        # even when correction mode has suppressed slider updates.
        self._latest_classifier = state

        if self._blind_labeling_mode:
            return
        if (
            state.has_confident_data
            and not state.loading
            and not state.error
            and not self._classifier_output_exposure_recorded
        ):
            recorded = self.events_tab.record_classifier_output_visible()
            self._classifier_output_exposure_recorded = bool(recorded)
            if not recorded:
                self._recon_status_label.setText(
                    "Classifier result withheld: audit state write failed"
                )
                self._recon_status_label.setStyleSheet(
                    self._RECON_STATUS_STYLE_ERROR
                )
                return

        if self._correction_active:
            return

        model_line = f"Model: {state.model_version}\n" if state.model_version else ""

        if state.loading:
            self._recon_status_label.setText("Loading classifier…")
            self._recon_status_label.setStyleSheet(self._RECON_STATUS_STYLE_INFO)
            self._recon_status_label.setToolTip(
                "Loading the classifier model. First arm of a session "
                "takes 1-2 seconds; subsequent arms in the same session "
                "reuse the loaded bridge."
            )
            return
        if state.error:
            self._recon_status_label.setText(f"⚠ {state.error}")
            self._recon_status_label.setStyleSheet(self._RECON_STATUS_STYLE_ERROR)
            self._recon_status_label.setToolTip(
                f"{state.error}\n\n"
                "Common causes: torch not installed on this machine, "
                "best_model.pth missing, AI_for_quantum repo not cloned. "
                "Uncheck 'Live classifier' in the config panel to disarm "
                "and re-arm without the classifier."
            )
            return

        smoothed = state.smoothed_percent

        # Warming-up state: no confident classification has ever arrived,
        # so the smoothed_percent values are placeholders — mark them "—"
        # in the value labels so growers don't misread them.
        if not state.has_confident_data:
            if smoothed:
                for name, slider in self._recon_sliders.items():
                    slider.blockSignals(True)
                    slider.setValue(int(smoothed.get(name, 0)))
                    slider.blockSignals(False)
                    self._recon_value_labels[name].setText("—")
                    self._recon_value_labels[name].setStyleSheet(
                        self._RECON_VAL_STYLE_PLACEHOLDER,
                    )
            if state.last_frame_number < 0:
                self._recon_status_label.setText("Waiting for frames…")
            else:
                self._recon_status_label.setText(
                    f"Warming up — quality {state.quality:.2f}; "
                    f"no confident data yet"
                )
            self._recon_status_label.setStyleSheet(self._RECON_STATUS_STYLE_INFO)
            self._recon_status_label.setToolTip(
                f"{model_line}"
                "Waiting for a frame the model recognizes with "
                f"confidence (quality ≥ {self._OOD_TOOLTIP_THRESHOLD}). "
                "Sliders show a neutral placeholder until then — the "
                "'—' means 'no data yet', not 'model predicts 20%'."
            )
            return

        # Confident-data state: sliders reflect real (EMA-smoothed) values.
        # Compute argmax to decide whether to highlight a leading class.
        argmax_label = (
            max(smoothed.items(), key=lambda kv: kv[1])[0] if smoothed else None
        )
        max_v = smoothed.get(argmax_label, 0) if argmax_label else 0
        highlight_winner = max_v > self._ARGMAX_HIGHLIGHT_THRESHOLD

        for name, slider in self._recon_sliders.items():
            v = int(smoothed.get(name, 0))
            slider.blockSignals(True)
            slider.setValue(v)
            slider.blockSignals(False)
            val_lbl = self._recon_value_labels[name]
            val_lbl.setText(f"{v}%")
            if highlight_winner and name == argmax_label:
                val_lbl.setStyleSheet(self._RECON_VAL_STYLE_ARGMAX)
            else:
                val_lbl.setStyleSheet(self._RECON_VAL_STYLE_NORMAL)

        # Enrich status line with quality + inference time for transparency
        if state.is_ood:
            self._recon_status_label.setText(
                f"OOD — quality {state.quality:.2f} | "
                f"{state.inference_ms:.0f} ms | showing last confident"
            )
            self._recon_status_label.setStyleSheet(self._RECON_STATUS_STYLE_WARN)
            self._recon_status_label.setToolTip(
                f"{model_line}"
                "Out-of-distribution: the model saw a frame unlike "
                "anything in its training set (quality below the "
                f"{self._OOD_TOOLTIP_THRESHOLD} threshold). Sliders "
                "are frozen at the last confident prediction until a "
                "recognizable frame arrives. This is expected during "
                "reconstruction transitions and beam-block events."
            )
        else:
            self._recon_status_label.setText(
                f"Sum: {state.raw_sum:.2f} | quality {state.quality:.2f} | "
                f"{state.inference_ms:.0f} ms"
            )
            self._recon_status_label.setStyleSheet(self._RECON_STATUS_STYLE_INFO)
            self._recon_status_label.setToolTip(
                f"{model_line}"
                "Sum: total of raw model scores before normalization "
                "(around 1.0 means the model is behaving; far from 1.0 "
                "means the Equalizer recipe is doing heavy scaling).\n"
                "Quality: the model's confidence in this specific frame.\n"
                "Ms: time to classify this frame."
            )

        # Preserve legacy classifier-state routing to the hidden compatibility
        # widget. Attribute-guarded for early worker emissions during build.
        if hasattr(self, "live_equalizer_tab"):
            self.live_equalizer_tab.update_classifier_state(state)

    def update_weak_primary_shadow_state(
        self, state: WeakPrimaryShadowState,
    ) -> None:
        """Render four outputs without feeding the five-class UI."""
        if self._blind_labeling_mode:
            return
        if state.loading:
            text = "Brightness-robust 4-output shadow: loading 36 heads…"
        elif state.error:
            text = f"Brightness-robust 4-output shadow unavailable: {state.error}"
        elif state.last_frame_number < 0:
            text = (
                "Brightness-robust 4-output shadow "
                f"({state.brightness_policy}, {state.checkpoint_count}/36); "
                "waiting for frame"
            )
        else:
            probability = state.conditional_probabilities
            breakdown = " · ".join(
                f"{name} {100.0 * float(probability.get(name, 0.0)):.1f}%"
                for name in ("Twinned (2x1)", "c(6x2)", "rt13xrt13", "HTR")
            )
            text = (
                "SHADOW ONLY · all_extreme · no 1x1 · " + breakdown
                + f" · disagreement {state.checkpoint_disagreement:.3f}"
                + f" · {state.inference_ms:.0f} ms"
            )
            if not self._classifier_output_exposure_recorded:
                recorded = self.events_tab.record_classifier_output_visible()
                self._classifier_output_exposure_recorded = bool(recorded)
        self._weak_primary_shadow_label.setText(text)

    def set_weak_primary_shadow_disabled(self) -> None:
        self._weak_primary_shadow_label.setText(
            "Brightness-robust 4-output shadow: disabled"
        )

    # Documented for the tooltip messages so they stay in sync with
    # ClassifierWorker.OOD_QUALITY_THRESHOLD; hard-coded because the
    # worker class-level constant isn't cheap to reach from a Qt slot
    # hot-path. Update both if the threshold moves.
    _OOD_TOOLTIP_THRESHOLD = 0.3

    def set_classifier_capture_unavailable(self, message: str) -> None:
        """Invalidate classifier output when its RHEED source is lost."""
        state = ClassifierState(
            loading=False,
            ready=False,
            error=message,
        )
        self.update_classifier_state(state)
        if hasattr(self, "live_equalizer_tab"):
            self.live_equalizer_tab.update_classifier_state(None)

    def reset_classifier_session_state(
        self,
        message: str = "Waiting for current-session classifier data…",
    ) -> None:
        """Clear cached scores at a START/STOP generation boundary."""
        self._latest_classifier = None
        self._classifier_output_exposure_recorded = False
        if self._correction_active:
            self.correction_btn.setChecked(False)
            self._on_correction_toggled(False)
        for name, slider in self._recon_sliders.items():
            slider.blockSignals(True)
            slider.setValue(0)
            slider.blockSignals(False)
            self._recon_value_labels[name].setText("—")
            self._recon_value_labels[name].setStyleSheet(
                self._RECON_VAL_STYLE_PLACEHOLDER
            )
        self._recon_status_label.setText(message)
        self._recon_status_label.setStyleSheet(
            self._RECON_STATUS_STYLE_INFO
        )
        if hasattr(self, "live_equalizer_tab"):
            self.live_equalizer_tab.update_classifier_state(None)

    def set_classifier_disabled(self):
        """Show explicit "disabled" state — invoked from GrowthApp._on_arm
        when the user unchecks "Live classifier" in the config panel.

        Sliders zero out, value labels reset, status label makes the
        disabled state explicit so growers don't wonder why the
        classifier isn't reporting.

        Also disables the ✎ Correct button: correcting a classifier that
        isn't running would produce a "grower belief with no classifier
        pair" log entry, which pollutes Yuxin's #1 active-comparisons
        signal (the whole point of the pair is having both sides).

        Defensive: clears ``_latest_classifier`` to None so ``_on_commit``
        writes ``classifier_status = DISABLED`` on the next LOG ENTRY
        instead of stale values from before the disable. Belt-and-
        suspenders against any code path that reaches here with a
        lingering state cache — see Jul 8 2026 Path Y investigation.
        """
        # Force correction off first — otherwise the setEnabled(False)
        # loop below re-locks sliders that the correction toggle would
        # otherwise have left enabled.
        if self._correction_active:
            self.correction_btn.setChecked(False)
            self._on_correction_toggled(False)
        self.correction_btn.setEnabled(False)
        # Wipe the stale classifier state cache so LOG ENTRY produces
        # correct DISABLED rows even if the caller didn't disarm first.
        self._latest_classifier = None
        for name, slider in self._recon_sliders.items():
            slider.blockSignals(True)
            slider.setValue(0)
            slider.blockSignals(False)
            self._recon_value_labels[name].setText("0%")
            self._recon_value_labels[name].setStyleSheet(
                self._RECON_VAL_STYLE_PLACEHOLDER,
            )
        self._recon_status_label.setText(
            "Classifier disabled — re-arm with the config toggle checked"
        )
        self._recon_status_label.setStyleSheet(self._RECON_STATUS_STYLE_INFO)
        self._recon_status_label.setToolTip(
            "The 'Live classifier' checkbox in the config panel is "
            "unchecked, so the classifier worker never started for "
            "this session. Everything else (camera, pyrometer, MISTRAL, "
            "auto-capture) runs as normal."
        )

    # ----- RHEED frame display --------------------------------------------

    def _display_frame(self, frame: np.ndarray):
        h, w = frame.shape[:2]
        # Ensure contiguous memory layout and convert to bytes for PyQt6
        frame = np.ascontiguousarray(frame)
        data = frame.tobytes()
        if frame.ndim == 2:
            qimg = QImage(data, w, h, w, QImage.Format.Format_Grayscale8)
        else:
            qimg = QImage(data, w, h, 3 * w, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg).scaled(
            self.rheed_image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.rheed_image_label.setPixmap(pixmap)

    # ----- Grower correction (deliverable #5) -----------------------------

    def _on_correction_toggled(self, checked: bool):
        """Handler for the ✎ Correct toggle.

        On (checked=True):
            - Unlock sliders (setEnabled True) so the grower can drag.
            - Immediately normalize existing slider values to sum-100 so
              the correction state starts from a clean total, even when
              the classifier's smoothed_percent rounded to 99 or 101.
            - Swap slider handle color to rose (#e11d48) so it's obvious
              at a glance which mode is active.
            - Replace the status label with the correction-mode message.

        Off (checked=False):
            - Lock sliders (setEnabled False), restore teal handle color.
            - Re-render the last classifier state (if any) so the sliders
              resume tracking the live model. Idle fallback if the
              classifier never emitted.

        Sync the button state defensively so callers who invoke this
        directly (tests, reset paths, auto-lock in _on_commit) leave the
        UI in a self-consistent state — clicked(bool) from a real button
        click already matches, so setChecked here is a cheap no-op for
        that path.
        """
        self._correction_active = checked
        if self.correction_btn.isChecked() != checked:
            self.correction_btn.setChecked(checked)
        for slider in self._recon_sliders.values():
            slider.setEnabled(checked)

        if checked:
            self._normalize_sliders_to_100()
            for slider in self._recon_sliders.values():
                slider.setStyleSheet(self._SLIDER_STYLE_CORRECTION)
            for name, slider in self._recon_sliders.items():
                self._recon_value_labels[name].setText(f"{slider.value()}%")
                self._recon_value_labels[name].setStyleSheet(
                    self._RECON_VAL_STYLE_CORRECTION,
                )
            self._recon_status_label.setText(
                "✎ Correction mode — drag sliders (sum stays at 100%)"
            )
            self._recon_status_label.setStyleSheet(
                self._RECON_STATUS_STYLE_CORRECTION,
            )
            self._recon_status_label.setToolTip(
                "Grower correction is active. Drag any slider — the other "
                "four adjust proportionally to keep the total at 100%. The "
                "next LOG ENTRY captures both your belief and the "
                "classifier's, then auto-locks this toggle so the next "
                "entry is a fresh decision.\n\n"
                "Uncheck to resume classifier-driven display without "
                "logging a correction."
            )
        else:
            for slider in self._recon_sliders.values():
                slider.setStyleSheet(self._SLIDER_STYLE_LIVE)
            if self._latest_classifier is not None:
                self.update_classifier_state(self._latest_classifier)
            else:
                for name, slider in self._recon_sliders.items():
                    slider.blockSignals(True)
                    slider.setValue(0)
                    slider.blockSignals(False)
                    self._recon_value_labels[name].setText("0%")
                    self._recon_value_labels[name].setStyleSheet(
                        self._RECON_VAL_STYLE_NORMAL,
                    )
                self._recon_status_label.setText("Classifier idle")
                self._recon_status_label.setStyleSheet(
                    self._RECON_STATUS_STYLE_INFO,
                )
                self._recon_status_label.setToolTip("")

    def _normalize_sliders_to_100(self):
        """Force the 5 sliders to sum to 100 via proportional scaling.

        Called on correction-mode entry so the first draggable state
        starts from a clean total, even when the classifier's
        smoothed_percent sums to 99 or 101 due to integer rounding, or
        when the sliders are all at 0 (fresh session, classifier idle).

        Edge cases:
            - Total already 100 → no-op.
            - Total is 0 → uniform distribution (20 each with residual on
              the first slider — this is what "no signal, all classes
              equally likely" means).
            - Non-integer proportional result → assign residual to the
              largest slider so the integer sum lands exactly on 100.

        Uses blockSignals so this normalization pass doesn't trigger
        the grower-slider handler (which would try to re-normalize
        recursively).
        """
        values = {n: s.value() for n, s in self._recon_sliders.items()}
        total = sum(values.values())
        if total == 100:
            return

        n_sliders = len(values)
        if total == 0:
            base = 100 // n_sliders
            residual = 100 - base * n_sliders
            new_values = {
                name: base + (1 if i < residual else 0)
                for i, name in enumerate(values)
            }
        else:
            new_values = {
                n: int(round(v * 100 / total)) for n, v in values.items()
            }
            diff = 100 - sum(new_values.values())
            if diff != 0:
                largest = max(new_values, key=new_values.get)
                new_values[largest] += diff

        for name, v in new_values.items():
            slider = self._recon_sliders[name]
            slider.blockSignals(True)
            slider.setValue(max(0, min(100, v)))
            slider.blockSignals(False)

    def _on_grower_slider_changed(self, changed_name: str, new_value: int):
        """Grower dragged a slider — proportionally adjust the others so
        the total returns to 100 (Pattern A). No-op when correction is
        off, no-op when we're already inside a programmatic adjustment
        (recursion guard).

        Algorithm:
            1. The 4 other sliders' new sum must equal 100 - new_value.
            2. If those 4 currently sum to 0, distribute the target sum
               uniformly (with integer residual on the first sliders).
            3. Otherwise, scale each by (its_current / current_sum) *
               target — integer-rounded — then push any rounding
               residual (±1 or ±2) onto the currently-largest one so
               the integer total lands exactly on 100.

        The _adjusting guard is the primary recursion defense: it
        rejects the re-entry that happens when our own setValue()
        below fires *this* handler again for a sibling slider. The
        blockSignals() calls are belt-and-suspenders — cheap enough
        and make the intent explicit at the call site.
        """
        if not self._correction_active or self._adjusting:
            return

        self._adjusting = True
        try:
            others = [
                (n, s) for n, s in self._recon_sliders.items()
                if n != changed_name
            ]
            others_target_sum = 100 - new_value
            current_others_sum = sum(s.value() for _, s in others)

            if current_others_sum == 0:
                # No signal to preserve → uniform distribute.
                n_others = len(others)
                base = others_target_sum // n_others
                residual = others_target_sum - base * n_others
                new_values = {
                    name: base + (1 if i < residual else 0)
                    for i, (name, _) in enumerate(others)
                }
            else:
                # Preserve relative shape, scale to hit target sum.
                new_values = {
                    name: int(round(s.value() * others_target_sum
                                    / current_others_sum))
                    for name, s in others
                }
                diff = others_target_sum - sum(new_values.values())
                if diff != 0:
                    largest = max(new_values, key=new_values.get)
                    new_values[largest] += diff

            for name, v in new_values.items():
                slider = self._recon_sliders[name]
                slider.blockSignals(True)
                slider.setValue(max(0, min(100, v)))
                slider.blockSignals(False)

            # Refresh all value labels so what the grower sees matches
            # what will land in the CSV. Uses the correction style so
            # the row keeps its "grower is driving" visual state.
            for name, slider in self._recon_sliders.items():
                self._recon_value_labels[name].setText(f"{slider.value()}%")
                self._recon_value_labels[name].setStyleSheet(
                    self._RECON_VAL_STYLE_CORRECTION,
                )
        finally:
            self._adjusting = False

    # ----- RHEED acquisition QC -------------------------------------------

    def _emit_rheed_adjustment_event(self, event_type: str):
        """Record an operator adjustment without changing acquisition state."""
        buttons = {
            "sample_direction_adjusted": self.rheed_direction_btn,
            "rheed_current_adjusted": self.rheed_current_btn,
            "rheed_energy_adjusted": self.rheed_energy_btn,
        }
        button = buttons.get(event_type)
        if button is None or not button.isEnabled():
            return
        self.rheed_view_event_requested.emit({
            "event_type": event_type,
            "elapsed_s": self.get_elapsed_seconds(),
            "note": self.log_note_input.toPlainText().strip(),
        })

    def _emit_rheed_qc_label(self, event_type: str):
        """Request a one-frame explicit global QC PASS/REJECT label."""
        button = (
            self.rheed_qc_pass_btn
            if event_type == "qc_pass"
            else self.rheed_qc_reject_btn
        )
        if not button.isEnabled():
            return
        self.rheed_view_event_requested.emit({
            "event_type": event_type,
            "elapsed_s": self.get_elapsed_seconds(),
            "note": self.log_note_input.toPlainText().strip(),
        })

    def update_rheed_qc_state(self, state: RheedQcState) -> None:
        """Render the app-owned acquisition state without inferring labels."""
        self._rheed_qc_state = state
        if hasattr(self, "live_equalizer_tab"):
            self.live_equalizer_tab.update_qc_context(
                session_active=state.session_active,
                view_segment_id=state.view_segment_id,
                visual_history_generation=state.visual_history_generation,
                gun_aligned=state.gun_aligned,
                realignment_active=state.realignment_active,
            )
        self._update_rheed_qc_controls()

    def _update_rheed_qc_controls(self) -> None:
        """Refresh status text, transition label, and state-dependent gates."""
        if not hasattr(self, "rheed_direction_btn"):
            return

        state = self._rheed_qc_state
        running = self._state == "running" and state.session_active
        fresh_frame = self._has_fresh_rheed_frame()

        if state.history_required <= 0:
            status = (
                f"RHEED: segment {state.view_segment_id}; "
                "single-frame classifier only, temporal advice not deployed"
            )
            color = "#d97706"
        elif state.history_ready:
            status = (
                f"RHEED: segment {state.view_segment_id} ready "
                f"({state.history_frame_count}/{state.history_required})"
            )
            color = "#22c55e"
        else:
            status = (
                f"RHEED: segment {state.view_segment_id} warming "
                f"({state.history_frame_count}/{state.history_required}); "
                "temporal advice not actionable"
            )
            color = "#d97706"

        self.rheed_qc_status_label.setText(status)
        self.rheed_qc_status_label.setStyleSheet(
            f"color: {color}; font-size: 11px; padding: 3px 6px;"
        )
        self.rheed_direction_btn.setEnabled(running)
        self.rheed_current_btn.setEnabled(running)
        self.rheed_energy_btn.setEnabled(running)
        self.rheed_qc_reject_btn.setEnabled(running and fresh_frame)
        self.rheed_qc_pass_btn.setEnabled(
            running
            and fresh_frame
        )

    def _has_fresh_rheed_frame(self) -> bool:
        """Return whether operator labels can bind to a current capture."""
        metadata = self.get_current_capture_metadata()
        if self._current_frame is None or not metadata:
            return False
        try:
            age_ms = float(metadata.get("frame_age_ms", float("inf")))
        except (TypeError, ValueError):
            return False
        if not np.isfinite(age_ms) or age_ms > 3000.0:
            return False
        return bool(
            int(metadata.get("capture_sequence") or 0) > 0
            or metadata.get("captured_at_utc")
        )

    # ----- MARK EVENT handler ---------------------------------------------

    def _on_mark_event(self):
        """Fire a manual event with the current sensor snapshot.

        Design contract (Jul 10 2026): fast, low-friction, no dialogs, no
        confirmations. Multi-click OK — every click is one event. Silently
        no-ops if the button is disabled (no session armed), matching
        _on_commit's guard so the E-key shortcut can't fire outside a
        session either.

        Payload assembly mirrors _on_commit but skips the classifier +
        slider snapshots — those live on LOG ENTRY where the grower is
        opting in to the fuller decision. Keeps this handler under the
        sub-second budget the growers surfaced at the Jul 10 meeting.
        """
        if not self.mark_event_btn.isEnabled():
            return
        event_at_utc = datetime.now(timezone.utc).isoformat()

        # PSU snapshot — same priority as _on_commit (mistral > direct >
        # none). Keeps psu_source semantically identical between the two
        # event families so downstream analysis can compare LOG ENTRY and
        # MARK EVENT rows without branching on file source.
        voltage_v: Optional[float] = None
        current_a: Optional[float] = None
        psu_source = "none"
        if self._latest_mistral and self._latest_mistral.connected:
            voltage_v = self._latest_mistral.v_actual
            current_a = self._latest_mistral.i_actual
            psu_source = "mistral"
        elif self._latest_psu and self._latest_psu.connected:
            voltage_v = self._latest_psu.voltage_measured
            current_a = self._latest_psu.current_measured
            psu_source = "direct"

        pyro_temp: Optional[float] = None
        if self._latest_pyro and self._latest_pyro.has_valid_reading:
            pyro_temp = self._latest_pyro.temperature

        # Grab any queued note text WITHOUT clearing the input — the
        # grower may want the same note attached to a following LOG
        # ENTRY. If they don't, they can clear it manually. This is the
        # low-cost, low-surprise choice for the primary "just mark it"
        # flow.
        note = self.log_note_input.toPlainText().strip()

        payload = {
            "event_at_utc": event_at_utc,
            "elapsed_s": self.get_elapsed_seconds(),
            "pyro_temp": pyro_temp,
            "voltage_V": voltage_v,
            "current_A": current_a,
            "psu_source": psu_source,
            "note": note,
        }

        # Bump the local counter + update the footer BEFORE emitting so
        # the UI updates instantly even if the app-side handler takes a
        # moment on a slow disk. GrowthApp is responsible for the CSV
        # write; it uses its own counter which stays in sync because the
        # button is disabled outside armed sessions.
        self._manual_event_count += 1
        self._update_manual_event_label()

        self.manual_event_requested.emit(payload)

    def _update_manual_event_label(self):
        """Refresh the footer's manual-event counter text."""
        if self._manual_event_count == 0:
            self.manual_event_label.setText("Manual events: 0")
        else:
            self.manual_event_label.setText(
                f"Manual events: {self._manual_event_count}"
            )

    # ----- Continuous-capture indicator -----------------------------------

    def set_continuous_capture_interval(self, interval_s: Optional[float]):
        """Set (or clear) the continuous-capture interval shown in the footer.

        Called by GrowthApp at session start with the interval that was
        actually applied to _heartbeat_timer. Passing ``None`` clears the
        indicator (back to "Capture: idle") — used at session end.
        """
        self._continuous_capture_interval_s = interval_s
        self._update_continuous_capture_label()

    def increment_continuous_capture_count(self):
        """Bump the frame counter by 1 and refresh the footer label.

        Called by GrowthApp from _on_heartbeat once the save actually
        landed (after quality gate). Keeps the display in sync with the
        real number of frames on disk — a quality-gated skip won't
        increment.
        """
        self._continuous_capture_count += 1
        self._update_continuous_capture_label()

    def _update_continuous_capture_label(self):
        """Refresh the footer's continuous-capture text.

        Format: "Capture: idle" (no session) or "Capture: every 5s · 42 frames".
        Interval float rendered as int seconds since the spinbox uses
        decimals=0 anyway; keeps the label narrow enough that it doesn't
        crowd the manual-event counter to its right.
        """
        interval = self._continuous_capture_interval_s
        if interval is None:
            self.continuous_capture_label.setText("Capture: idle")
            return
        # Interval as int when it's a whole number, otherwise 1 dp — keeps
        # "every 5s" clean while still supporting sub-second intervals if
        # the range ever expands.
        if float(interval).is_integer():
            interval_str = f"{int(interval)}s"
        else:
            interval_str = f"{interval:.1f}s"
        self.continuous_capture_label.setText(
            f"Capture: every {interval_str} · {self._continuous_capture_count} "
            f"frame{'' if self._continuous_capture_count == 1 else 's'}"
        )

    # ----- LOG ENTRY handler ----------------------------------------------

    def _on_commit(self):
        if not self.commit_btn.isEnabled():
            return

        now = datetime.now()
        temp_str = (
            f"{self._latest_pyro.temperature:.1f}"
            if self._latest_pyro and self._latest_pyro.has_valid_reading else ""
        )

        # PSU value snapshot — prefer MISTRAL screengrab OCR since that's
        # the current O-MBE path (TDK-Lambda hardware → MISTRAL → OCR),
        # fall back to direct-read PSU when we eventually wire one up.
        # psu_source tells downstream which path produced the number so
        # they can weight OCR-derived values differently from direct-read.
        # See growth_logger.py COMMIT_FIELDS comment for the full taxonomy.
        voltage_str = ""
        current_str = ""
        psu_source = "none"
        if self._latest_mistral and self._latest_mistral.connected:
            if self._latest_mistral.v_actual is not None:
                voltage_str = f"{self._latest_mistral.v_actual:.3f}"
            if self._latest_mistral.i_actual is not None:
                current_str = f"{self._latest_mistral.i_actual:.3f}"
            # Source is "mistral" whenever the MISTRAL worker is connected,
            # even if a specific field happens to be None — captures the
            # topology ("mistral was the intended source") more accurately
            # than treating a partial reading as "no source at all".
            psu_source = "mistral"
        elif self._latest_psu and self._latest_psu.connected:
            voltage_str = f"{self._latest_psu.voltage_measured:.3f}"
            current_str = f"{self._latest_psu.current_measured:.3f}"
            psu_source = "direct"

        entry = {
            "timestamp": now.isoformat(),
            "time_display": now.strftime("%H:%M"),
            "sample_id": self.sample_id_input.text(),
            "grower": self.grower_input.text(),
            "elapsed_s": f"{self.get_elapsed_seconds():.2f}",
            "pyrometer_temp_C": temp_str,
            "voltage_V": voltage_str,
            "current_A": current_str,
            "psu_source": psu_source,
            "note": self.log_note_input.toPlainText().strip(),
        }

        # Capture reconstruction estimates from sliders. When correction
        # is off, these mirror the classifier's smoothed_percent (sliders
        # are read-only and driven by update_classifier_state). When
        # correction is on, these are the grower's belief — Pattern A
        # keeps their sum at 100.
        for name, slider in self._recon_sliders.items():
            entry[f"recon_{name}"] = (
                str(slider.value()) if self._correction_active else ""
            )
        entry["recon_label_source"] = (
            "human_corrected" if self._correction_active else ""
        )
        entry["recon_labeler"] = (
            self.grower_input.text().strip() if self._correction_active else ""
        )
        entry["recon_confidence"] = ""

        # Snapshot the classifier's live prediction alongside the
        # grower's slider values so every log entry becomes a paired
        # (grower, classifier) datum for Yuxin's #1 active-comparisons
        # signal (see yuxin_deliverables_jul06.md). Blank when the
        # classifier never emitted (disabled for this session, or not
        # yet loaded).
        #
        # classifier_status column disambiguates why classifier_recon_*
        # might be blank or 0. Set even when _latest_classifier is None
        # so downstream analysis can distinguish DISABLED from OK-with-
        # 0-confidence (see growth_logger.py COMMIT_FIELDS comment).
        if self._latest_classifier is not None:
            smoothed = self._latest_classifier.smoothed_percent
            for name in self._recon_sliders:
                entry[f"classifier_recon_{name}"] = str(
                    smoothed.get(name, 0)
                )
            entry["grower_corrected"] = (
                "True" if self._correction_active else "False"
            )
            # Lifecycle branch — errors and loading are checked before
            # "OK" so a state with e.g. both loading=True and error=""
            # doesn't get called OK prematurely. Order matches
            # ClassifierState's documented lifecycle transitions.
            if self._latest_classifier.error:
                entry["classifier_status"] = "ERROR"
            elif self._latest_classifier.loading:
                entry["classifier_status"] = "LOADING"
            else:
                entry["classifier_status"] = "OK"
            entry["classifier_input_mode"] = (
                self._latest_classifier.model_input_mode
            )
            entry["classifier_prediction_actionable"] = str(
                bool(self._latest_classifier.prediction_actionable)
            )
            entry["classifier_view_segment_id"] = (
                ""
                if self._latest_classifier.view_segment_id is None
                else str(self._latest_classifier.view_segment_id)
            )
            entry["classifier_visual_history_generation"] = str(
                self._latest_classifier.visual_history_generation
            )
            entry["classifier_history_ready"] = str(
                bool(self._latest_classifier.history_ready)
            )
        else:
            for name in self._recon_sliders:
                entry[f"classifier_recon_{name}"] = ""
            entry["grower_corrected"] = ""
            entry["classifier_status"] = "DISABLED"
            entry["classifier_input_mode"] = ""
            entry["classifier_prediction_actionable"] = ""
            entry["classifier_view_segment_id"] = ""
            entry["classifier_visual_history_generation"] = ""
            entry["classifier_history_ready"] = ""

        # Add row to Growth Notes table
        self._add_growth_note_row(entry)

        # Clear the note input for next entry
        self.log_note_input.clear()

        # Auto-lock correction after every LOG ENTRY so the next entry
        # is a fresh decision — prevents stale grower values from
        # bleeding across log entries when the growth has moved on. If
        # correction was off, this is a no-op (setChecked(False) on an
        # unchecked button doesn't fire clicked).
        if self._correction_active:
            self.correction_btn.setChecked(False)
            self._on_correction_toggled(False)

        # Sliders are classifier-driven (read-only) — no reset needed.
        # The next classifier emission will keep them in sync with the
        # live model output.

        self.commit_requested.emit(entry)

    def _add_growth_note_row(self, entry: dict):
        """Add a row to the Growth Notes table."""
        row = self.growth_notes_table.rowCount()
        self.growth_notes_table.insertRow(row)
        self.growth_notes_table.setItem(
            row, 0, QTableWidgetItem(entry.get("time_display", ""))
        )
        self.growth_notes_table.setItem(
            row, 1, QTableWidgetItem(
                entry.get("pyrometer_temp_C", "") or "---"
            )
        )
        self.growth_notes_table.setItem(
            row, 2, QTableWidgetItem(
                entry.get("voltage_V", "") or "---"
            )
        )
        self.growth_notes_table.setItem(
            row, 3, QTableWidgetItem(
                entry.get("current_A", "") or "---"
            )
        )
        self.growth_notes_table.setItem(
            row, 4, QTableWidgetItem(entry.get("note", ""))
        )
        self.growth_notes_table.scrollToBottom()

    def clear_session_tables(self):
        """Reset Sensor Log + Growth Notes table UIs for a new session.

        The CSV files are correctly per-session (start_session opens fresh
        ones), but these QTableWidgets accumulate rows visually across
        sessions because nothing in the original wiring resets them.
        Mirrors the setRowCount(0) call EventsTab.attach_session uses.
        """
        self.sensor_log_table.setRowCount(0)
        self.growth_notes_table.setRowCount(0)

    # ----- Sensor Log display ---------------------------------------------

    def add_sensor_log_row(self, time_str: str, temp: Optional[float],
                           voltage: Optional[float] = None,
                           current: Optional[float] = None,
                           pressure: Optional[float] = None):
        """Add a row to the Sensor Log table (newest at top)."""
        table = self.sensor_log_table

        if table.rowCount() >= MAX_SENSOR_DISPLAY_ROWS:
            table.removeRow(table.rowCount() - 1)

        table.insertRow(0)
        table.setItem(0, 0, QTableWidgetItem(time_str))
        table.setItem(
            0, 1, QTableWidgetItem(
                f"{temp:.1f}" if temp is not None else "---"
            )
        )
        table.setItem(
            0, 2, QTableWidgetItem(
                f"{voltage:.3f}" if voltage is not None else "---"
            )
        )
        table.setItem(
            0, 3, QTableWidgetItem(
                f"{current:.3f}" if current is not None else "---"
            )
        )
        table.setItem(
            0, 4, QTableWidgetItem(
                f"{pressure:.2e}" if pressure is not None else "---"
            )
        )

    # ----- Config helpers -------------------------------------------------

    def _on_config_browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Save Folder")
        if folder:
            self.config_save_path.setText(folder)

    # ----- Accessors for GrowthApp ----------------------------------------

    def get_current_frame(self) -> Optional[np.ndarray]:
        return self._current_frame

    def get_current_capture_metadata(self) -> dict:
        """Return provenance paired with the currently cached camera frame."""
        import time

        state = self._latest_camera
        if (
            state is None or state.frame is None
            or not state.connected or not state.valid
        ):
            return {}
        frame_age_ms = state.frame_age_ms
        if state.captured_monotonic_ns:
            frame_age_ms = max(
                0.0,
                (time.perf_counter_ns() - state.captured_monotonic_ns)
                / 1_000_000,
            )
        return {
            "capture_backend": state.capture_backend,
            "capture_geometry_id": state.capture_geometry_id,
            "captured_at_utc": state.captured_at_utc,
            # Internal-only source clock. CSV writers intentionally ignore it;
            # Equalizer snapshots use it to recompute age at Save time.
            "captured_monotonic_ns": state.captured_monotonic_ns,
            "capture_sequence": state.capture_sequence,
            "frame_age_ms": frame_age_ms,
            "source_hwnd": state.source_hwnd,
            "camera_width": state.width,
            "camera_height": state.height,
            "session_active": self._rheed_qc_state.session_active,
            "view_segment_id": self._rheed_qc_state.view_segment_id,
            "visual_history_generation": (
                self._rheed_qc_state.visual_history_generation
            ),
            "gun_aligned": self._rheed_qc_state.gun_aligned,
            "realignment_active": self._rheed_qc_state.realignment_active,
        }

    def set_auto_capture_status(self, text: str):
        """Update the auto-capture footer label. Called by GrowthApp on
        each evaluated frame and on session start/stop."""
        self.auto_capture_label.setText(text)

    def show_auto_capture_event(
        self,
        event_idx: int,
        score: float,
        buffer_dir: str,
        countdown_s: int = 10,
    ):
        """Display the non-modal auto-capture banner for a flagged event."""
        self.auto_capture_banner.show_event(
            event_idx, score, buffer_dir, countdown_s,
        )

    def _on_auto_capture_decision_internal(
        self, event_idx: int, buffer_dir: str, state: str,
    ):
        """Re-emit the banner's decision signal at the GrowthMonitor level
        so GrowthApp can route the state update without knowing about the
        banner internals."""
        self.auto_capture_decision.emit(event_idx, buffer_dir, state)

    def set_auto_capture_pause_enabled(self, enabled: bool):
        """Enable/disable the pause toggle. Called by GrowthApp at session
        start (enable) and stop (disable + reset to unpaused)."""
        self.pause_auto_capture_btn.setEnabled(enabled)
        if not enabled and self.pause_auto_capture_btn.isChecked():
            # Force the toggle back to "Pause" so a re-armed session starts
            # in the running state without the button stuck in "Resume".
            self.pause_auto_capture_btn.setChecked(False)
            self.pause_auto_capture_btn.setText("Pause Auto-Capture")

    def is_auto_capture_paused(self) -> bool:
        """Return the grower's explicit auto-capture pause selection."""
        return self.pause_auto_capture_btn.isChecked()

    def set_rheed_reconnect_required(
        self, required: bool, *, in_progress: bool = False,
    ) -> None:
        """Show the in-session manual WGC reconnect control when needed."""
        self.reconnect_rheed_btn.setVisible(required or in_progress)
        self.reconnect_rheed_btn.setEnabled(required and not in_progress)
        self.reconnect_rheed_btn.setText(
            "Reconnecting RHEED\u2026" if in_progress else "Reconnect RHEED"
        )

    def _on_pause_auto_capture_clicked(self):
        """Toggle handler — flips label text and emits the request signal."""
        paused = self.pause_auto_capture_btn.isChecked()
        self.pause_auto_capture_btn.setText(
            "Resume Auto-Capture" if paused else "Pause Auto-Capture"
        )
        self.auto_capture_pause_toggled.emit(paused)

    def get_session_metadata(self) -> dict:
        """Return session metadata for growth log export."""
        mistral_mode = self.config_mistral_mode.currentText()
        direct_camera = self.config_camera_mode.currentText() in (
            "vimba", "direct",
        )
        metadata = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "grower": self.grower_input.text(),
            "sample_id": self.sample_id_input.text(),
            "chamber_id": self._cfg.chamber_id,
            "camera_mode": self.config_camera_mode.currentText(),
            "camera_exposure_requested_ms": (
                self.config_camera_exposure_ms.value()
                if direct_camera
                and self.config_camera_exposure_ms.value() > 0
                else None
            ),
            "camera_exposure_readback_ms": (
                self._latest_camera.exposure_us / 1000.0
                if direct_camera
                and self._latest_camera is not None
                and self._latest_camera.exposure_us is not None
                else None
            ),
            "mistral_mode": mistral_mode,
            "evap_mode": self.config_evap_mode.currentText(),
            "pyrometer_mode": self.config_pyrometer_mode.currentText(),
            "sensor_log_interval_s": float(
                self.config_interval_spin.value()
            ),
            "heartbeat_interval_s": float(
                self.config_heartbeat_interval_spin.value()
            ),
            "camera_timestamp_kind": "software_receive_not_exposure",
            "rheed_gun_telemetry_available": False,
            "rheed_gun_telemetry_fields": [],
            "mistral_vi_semantics": (
                "substrate_manipulator_heater_psu_not_rheed_gun"
            ),
            # Record the effective serial settings, including user-editable
            # COM/baud values and chamber-bound transport fields.  These are
            # provenance only and do not authorize a setpoint change.
            "pyrometer_port": (
                self.config_exactus_port.text().strip()
                or self._cfg.pyrometer_port
            ),
            "pyrometer_baudrate": int(
                self.config_exactus_baud.currentText()
            ),
            "pyrometer_device_id": self._cfg.pyrometer_device_id,
            "pyrometer_rts": self._cfg.pyrometer_rts,
            "pyrometer_modbus_backend": (
                self._cfg.pyrometer_modbus_backend
            ),
            "weak_primary_shadow_enabled": (
                self.config_weak_primary_shadow_enabled.isChecked()
            ),
        }
        # ADS profile provenance — record the exact PLC endpoint + cell
        # count that produced this session's sensor log. Makes old CSVs
        # auditable: a reader can look at session_metadata.json and know
        # which chamber/PLC/schema the cell{N}_* columns came from.
        if mistral_mode == "ads":
            metadata["mistral_ads_netid"]      = self._cfg.ads_netid
            metadata["mistral_ads_cell_count"] = self._cfg.ads_cell_count
            metadata["mistral_ads_port_main"]  = self._cfg.ads_port_main
            metadata["mistral_ads_port_pid"]   = self._cfg.ads_port_pid
        if metadata["evap_mode"] == "elog":
            metadata["evap_log_dir"] = self._cfg.evap_log_dir
            latest_evap = self._latest_evap
            source_path = (
                latest_evap.source_path
                if latest_evap is not None
                else ""
            )
            metadata["evap_source_path"] = source_path
            metadata["evap_source_resolved"] = bool(source_path)
        return metadata

    # ----- Reset -----------------------------------------------------------

    def reset_displays(self):
        """Clear live data displays back to defaults (preserves tables)."""
        self.elapsed_display.value.setText("00:00:00.00")
        self.temp_display.value.setText("---")
        self.voltage_display.value.setText("---")
        self.current_display.value.setText("---")
        self.pressure_display.value.setText("---")
        # Direct-read tab: all fields back to "---", plasma section hidden.
        for d in (
            self.substrate_pv_display, self.substrate_sp_display,
            self.plasma_dc_display, self.plasma_fwd_display,
            self.plasma_rfl_display,
        ):
            d.value.setText("---")
        for d in self._cell_displays:
            d.value.setText("---")
        for d in self._elog_source_displays:
            d.value.setText("---")
        self._evap_source_status_label.setText(
            "EvapControl source: unavailable"
        )
        self._evap_source_status_label.setStyleSheet(
            "font-size: 11px; color: #888;"
        )
        self.plasma_group.setVisible(False)
        self.rheed_image_label.clear()
        self.reconnect_rheed_btn.hide()
        self.auto_capture_label.setText("Auto-capture: idle")
        # Force grower correction off before wiping slider state so the
        # next session starts on a clean classifier-driven display, and
        # so the button visual state matches the internal state (a stuck
        # ✓ ✎ Correct button on a fresh session would be misleading).
        if self._correction_active:
            self.correction_btn.setChecked(False)
            self._on_correction_toggled(False)
        self.correction_btn.setEnabled(True)
        # Classifier sliders back to zero + idle status; the next arm will
        # trigger a fresh "Loading classifier…" cycle. Also reset the
        # value-label styles in case the previous session left an argmax
        # highlight or placeholder style behind.
        for name, slider in self._recon_sliders.items():
            slider.blockSignals(True)
            slider.setValue(0)
            slider.blockSignals(False)
            slider.setStyleSheet(self._SLIDER_STYLE_LIVE)
            self._recon_value_labels[name].setText("0%")
            self._recon_value_labels[name].setStyleSheet(
                self._RECON_VAL_STYLE_NORMAL,
            )
        self._recon_status_label.setText("Classifier idle")
        self._recon_status_label.setStyleSheet(self._RECON_STATUS_STYLE_INFO)
        self.set_weak_primary_shadow_disabled()
        self._start_time = None
        self._current_frame = None
        self._latest_psu = None
        self._latest_pyro = None
        self._latest_camera = None
        self._latest_mistral = None
        self._latest_evap = None
        self._latest_classifier = None
        self._rheed_qc_state = RheedQcState()
        self._update_rheed_qc_controls()
        # Manual-event counter belongs to the session — a fresh session
        # gets a fresh count. Footer label re-renders to "Manual events: 0"
        # so the next armed session starts on a clean slate.
        self._manual_event_count = 0
        self._update_manual_event_label()
        # Continuous-capture indicator resets to "Capture: idle" — the
        # next session's _on_start will re-populate the interval.
        self._continuous_capture_interval_s = None
        self._continuous_capture_count = 0
        self._update_continuous_capture_label()
        # The hidden legacy widget wipes transient state while preserving its
        # basis defaults. Keep this for older session/calibration readers.
        if hasattr(self, "live_equalizer_tab"):
            self.live_equalizer_tab.reset_for_new_session()
