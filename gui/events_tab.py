"""
Events tab — master/detail review surface for auto-capture events.

Locked design in `events_tab_design.md` (May 7 2026). Three jobs in one
surface: real-time review during a growth, walk-away catch-up, and
labeling input for Classifier2 training data. Catch-up is the primary
use case — most cognitively expensive — and the master/detail layout is
optimized around it.

Current scope:
  - QSplitter master/detail layout
  - Master list populated from `auto_capture_events.csv` on session
    attach, then live-updated via `AutoCaptureEngine.frame_captured`
  - Detail pane image viewer with metadata header, slider-scrubable
    buffer frames, and a placeholder for empty / missing buffer cases
  - Live row-state updates from `auto_capture_decision`
  - Unreviewed-count badge surfaced via `unreviewed_count_changed`
  - Per-event labeling form: primary reconstruction dropdown + notes
    field, atomic-per-change writes through GrowthLogger, in-memory
    label cache loaded on session attach

Reconstruction-transition labeling (change_from / change_to) shipped
Jul 15 2026 as two dropdowns in the labeling form. The default-hide-
discarded filter remains deferred.

Table of contents (search-friendly section labels — each group below
is bracketed by ``# === <label> ===`` header comments):

  UI SETUP           _build_ui, _build_master_pane, _build_detail_pane,
                     _build_label_form
  SESSION LIFECYCLE  attach_session
  LIVE UPDATES       on_frame_captured (slot),
                     on_decision_made (slot)
  MASTER LIST I/O    _load_csv_rows, _refresh_unreviewed_badge,
                     _add_event_row
  DETAIL PANE        _set_placeholder, _show_detail_content,
                     _on_selection_changed, _load_detail,
                     _format_metadata, _display_frame_at
  LABELING FORM      _populate_label_form, _on_primary_recon_activated,
                     _on_change_from_activated, _on_change_to_activated,
                     _on_notes_text_changed, _flush_notes_to_disk
  CLASSIFIER + EQ    _default_ai_repo_root, _get_classifier,
                     _on_classify_clicked, _on_open_equalizer,
                     _request_retrospective_calibration_acceptance,
                     complete_retrospective_calibration_acceptance,
                     _save_retrospective_equalizer_label,
                     _render_classifier_result

File size discipline: 1100+ lines is at the upper end of what a single
QWidget module should carry. Future additions to the labeling form or
classifier surface should first consider splitting the labeling form
into its own LabelingFormWidget (extracted from _build_label_form +
_populate_label_form + the four _on_*_activated slots), keeping
EventsTab as the master/detail orchestrator.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Optional
import uuid

import numpy as np
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSlider, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from gui.growth_logger import (
    EVENT_STATE_AUTO_SKIPPED,
    EVENT_STATE_DISCARDED,
    EVENT_STATE_KEPT_EXPLICIT,
    GrowthLogger,
)
from gui.widgets import ScalingImageLabel


# Five canonical RHEED reconstruction names + meta-categories for events
# the grower can't or won't tag with one. Spellings match the Monitor tab's
# recon sliders verbatim — see feedback_recon_naming.md for the canonical
# list and the "use ASCII x, lowercase rt13" rule.
RECON_LABEL_OPTIONS = [
    "1x1",
    "Twinned (2x1)",
    "c(6x2)",
    "rt13xrt13",
    "HTR",
    "unknown",
    "artifact",
]
PRIMARY_LABEL_OPTIONS = [
    # Kept as a legacy UI alias so historical rows remain editable.  Exact
    # human labels convert this choice to ``none/weak`` below; ``1x1`` is not
    # part of the new five-output training contract.
    "1x1",
    "none/weak",
    "Twinned (2x1)",
    "c(6x2)",
    "rt13xrt13",
    "HTR",
    "unknown",
    "artifact",
]
# Sentinel data value for "no label assigned" — distinguishable from any
# valid reconstruction name in the dropdown's itemData.
RECON_UNLABELED = ""


# Retrospective Equalizer labels are only trustworthy when the selected frame
# can be bound back to one exact acquisition snapshot.  Older event buffers did
# not carry this lifecycle context and deliberately remain usable only through
# the ordinary dropdown/notes form.
_EQUALIZER_MANIFEST_FIELDS = (
    "frame_path",
    "capture_backend",
    "capture_geometry_id",
    "captured_at_utc",
    "captured_monotonic_ns",
    "capture_sequence",
    "frame_age_ms",
    "source_hwnd",
    "camera_width",
    "camera_height",
    "session_id",
    "view_segment_id",
    "visual_history_generation",
    "gun_aligned",
    "realignment_active",
    "calibration_id",
    "basis_bundle_id",
)
_TRUE_TEXT = frozenset({"1", "true", "yes"})
_FALSE_TEXT = frozenset({"0", "false", "no"})


# An event is "unreviewed" if the grower hasn't made an explicit
# Keep/Discard decision on it. kept_default (banner timed out) counts as
# unreviewed because the timeout means the grower wasn't actually looking
# — exactly the walk-away catch-up case the badge needs to surface.
# auto_skipped events have no buffer to review (quality gate rejected
# all 20 frames), so they don't need grower attention either.
_REVIEWED_STATES = (
    EVENT_STATE_KEPT_EXPLICIT,
    EVENT_STATE_DISCARDED,
    EVENT_STATE_AUTO_SKIPPED,
)


# Master-list column indices. Score-color cell coloring and a label-state
# column come in follow-up commits along with the labeling UI.
COL_EVENT_IDX = 0
COL_TIME = 1
COL_SCORE = 2
COL_TEMP = 3
COL_STATE = 4
COLUMN_HEADERS = ["#", "Time", "Score", "Temp (℃)", "State"]


class EventsTab(QWidget):
    """Master/detail review surface for auto-capture events.

    The master list is the source of truth for which events exist and
    their CSV-recorded state. It is populated two ways:

    1. ``attach_session(session_dir)`` — called by GrowthApp on session
       start to load any pre-existing rows (covers the GUI-restart-mid-
       session case). Clears the table first.
    2. ``on_frame_captured(frame, score)`` — slot for
       ``AutoCaptureEngine.frame_captured``. The engine signal carries no
       event_idx, so we re-read the CSV and append rows past the
       watermark. Connection order matters: GrowthApp's handler must be
       wired first so the CSV row is on disk before this slot reads it.

    Live state changes flow in via ``on_decision_made`` (slot for
    ``GrowthMonitor.auto_capture_decision``). Each transition refreshes
    the unreviewed-count badge through ``unreviewed_count_changed``,
    which GrowthMonitor relays to the tab header so the catch-up case
    (return after walk-away, see "Events (12)") works at a glance.
    """

    # Emitted whenever the count of unreviewed events changes. GrowthMonitor
    # listens and rewrites the tab header text. "Unreviewed" is currently
    # state-based (pending or kept_default) — when labeling lands it will
    # also include events without a primary_reconstruction tag.
    unreviewed_count_changed = pyqtSignal(int)
    # GrowthApp owns acceptance/invalidation and the durable journal.  Events
    # emits an immutable request context and waits for one of the public
    # completion methods below before activating a calibration locally.
    retrospective_calibration_accept_requested = pyqtSignal(object)
    retrospective_calibration_invalidation_requested = pyqtSignal(object)
    retrospective_calibration_resolve_requested = pyqtSignal(object)
    # GrowthMonitor suppresses its live classifier panel and Live Equalizer
    # tab while this auditable labeling-only mode is active.
    blind_labeling_mode_changed = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._session_dir: Optional[Path] = None
        # GrowthLogger reference for label CSV reads/writes. None when no
        # session is attached, set in attach_session.
        self._growth_logger: Optional[GrowthLogger] = None
        # Highest event_idx already in the table — guards repeat appends
        # when the CSV is re-read on every frame_captured.
        self._last_seen_event_idx: int = 0
        # Pre-decoded pixmaps for the currently-selected event's buffer.
        # Cleared on selection change and on attach_session.
        self._cached_pixmaps: list[QPixmap] = []
        self._cached_paths: list[Path] = []
        self._capture_metadata_by_filename: dict[str, dict] = {}
        self._capture_manifest_error: str = "No event buffer is selected."
        self._pending_retrospective_accept_context: Optional[dict] = None
        self._pending_retrospective_invalidation: Optional[dict] = None
        self._pending_retrospective_resolve_context: Optional[dict] = None
        # In-memory label cache — single source of truth for "what label
        # does each event have right now." Loaded from events_labels.csv
        # on session attach, kept in sync as the grower applies labels.
        # Single-writer model (only this widget touches the file), so
        # staleness can't happen.
        self._labels_cache: dict[int, dict] = {}
        # Tracks which event the labeling form is currently bound to, so
        # form-change handlers know which event_idx to write under.
        self._currently_displayed_event_idx: Optional[int] = None
        self._labeler = ""
        self._blind_labeling_mode = False
        self._blind_state_reason = "No labeling session is attached."
        # Debounce notes-text writes — saving on every keystroke would
        # both spam disk and persist garbage like "substrate fla". Save
        # 500ms after the user stops typing, or immediately on selection
        # change (flushed in _on_selection_changed).
        self._notes_save_timer = QTimer(self)
        self._notes_save_timer.setSingleShot(True)
        self._notes_save_timer.setInterval(500)
        self._notes_save_timer.timeout.connect(self._flush_notes_to_disk)
        self._build_ui()
        self.events_table.itemSelectionChanged.connect(
            self._on_selection_changed,
        )

    # =====================================================================
    # === UI SETUP ===
    # =====================================================================

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        splitter.addWidget(self._build_master_pane())
        splitter.addWidget(self._build_detail_pane())

        # 2:3 master:detail. The detail pane will host the image viewer
        # next; growers want pixel real estate for 656x492 RHEED frames.
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([400, 600])

        layout.addWidget(splitter)

    def _build_master_pane(self) -> QWidget:
        master = QWidget()
        master_layout = QVBoxLayout(master)
        master_layout.setContentsMargins(0, 0, 0, 0)
        master_layout.setSpacing(4)

        title = QLabel("Auto-capture Events")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        master_layout.addWidget(title)

        self.events_table = QTableWidget(0, len(COLUMN_HEADERS))
        self.events_table.setHorizontalHeaderLabels(COLUMN_HEADERS)
        header = self.events_table.horizontalHeader()
        header.setSectionResizeMode(
            COL_EVENT_IDX, QHeaderView.ResizeMode.ResizeToContents,
        )
        header.setSectionResizeMode(
            COL_TIME, QHeaderView.ResizeMode.ResizeToContents,
        )
        header.setSectionResizeMode(
            COL_SCORE, QHeaderView.ResizeMode.ResizeToContents,
        )
        header.setSectionResizeMode(
            COL_TEMP, QHeaderView.ResizeMode.ResizeToContents,
        )
        header.setSectionResizeMode(
            COL_STATE, QHeaderView.ResizeMode.Stretch,
        )
        self.events_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers,
        )
        self.events_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows,
        )
        self.events_table.verticalHeader().setVisible(False)
        master_layout.addWidget(self.events_table)

        return master

    def _build_detail_pane(self) -> QWidget:
        """Detail pane: placeholder when nothing is selected, otherwise
        an image viewer with metadata header, scrubable slider, and
        position counter for the buffer frames of the selected event.

        Placeholder + content are siblings in the same QVBoxLayout, both
        with stretch=1; show/hide swaps which one fills the pane.
        """
        detail = QWidget()
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._detail_placeholder = QLabel()
        self._detail_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail_placeholder.setWordWrap(True)
        self._detail_placeholder.setStyleSheet(
            "color: #888; font-style: italic;"
        )
        layout.addWidget(self._detail_placeholder, 1)

        self._detail_content = QWidget()
        content = QVBoxLayout(self._detail_content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(6)

        self._metadata_label = QLabel()
        self._metadata_label.setStyleSheet(
            "font-weight: bold; font-size: 13px; padding: 2px 4px;"
        )
        self._metadata_label.setWordWrap(True)
        content.addWidget(self._metadata_label)

        # Shared widget extracted Jul 13 2026; events tab keeps its historical
        # 320×240 minimum-size hint via the default arg.
        self._image_label = ScalingImageLabel()
        content.addWidget(self._image_label, 1)

        slider_row = QHBoxLayout()
        slider_row.setSpacing(8)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.setStyleSheet(
            "QSlider::groove:horizontal { background: #333; height: 6px; }"
            "QSlider::handle:horizontal { background: #0d9488; width: 14px; "
            "margin: -5px 0; border-radius: 7px; }"
            "QSlider::sub-page:horizontal { background: #0d9488; }"
            "QSlider::add-page:horizontal { background: #555; }"
        )
        self._slider.valueChanged.connect(self._display_frame_at)
        slider_row.addWidget(self._slider, 1)

        self._frame_position_label = QLabel("0 / 0")
        self._frame_position_label.setStyleSheet(
            "color: #aaa; font-size: 11px;"
        )
        self._frame_position_label.setMinimumWidth(60)
        self._frame_position_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        slider_row.addWidget(self._frame_position_label, 0)
        content.addLayout(slider_row)

        content.addWidget(self._build_label_form())

        self._detail_content.hide()
        layout.addWidget(self._detail_content, 1)

        # Default placeholder copy — replaced by _set_placeholder() with
        # context-specific text when an event is selected but its buffer
        # is missing or empty.
        self._set_placeholder("Select an event to view buffer frames.")

        return detail

    def _build_label_form(self) -> QGroupBox:
        """Labeling form — primary reconstruction dropdown + notes field.

        Change_from / change_to dropdowns (Jul 15 2026) let the grower
        label reconstruction *transitions* — i.e. "this event captured
        the moment we went from 1x1 to Twinned(2x1)". Feeds Yuxin's #1
        active-comparisons training signal, which discriminates
        transition frames from steady-state frames. Both default to
        the RECON_UNLABELED sentinel; the "Primary reconstruction"
        dropdown above is for single-class labels (steady state), so
        the two label systems complement without overwriting.

        Uses ``activated`` (not ``currentIndexChanged``) on all three
        combos so programmatic ``setCurrentIndex`` during selection-
        change loading doesn't trigger spurious writes. Notes use a
        debounced timer so per-keystroke writes don't spam disk or
        persist garbage like "substrate fla".
        """
        box = QGroupBox("Label")
        form = QFormLayout(box)
        form.setContentsMargins(10, 16, 10, 8)
        form.setSpacing(6)

        blind_row = QHBoxLayout()
        blind_row.setSpacing(6)
        self._blind_mode_button = QPushButton("Enter blind labeling mode")
        self._blind_mode_button.setCheckable(True)
        self._blind_mode_button.setToolTip(
            "Explicitly hide classifier and Equalizer outputs before assigning "
            "gold labels. The mode and any prior output exposure are persisted "
            "in the growth session."
        )
        self._blind_mode_button.toggled.connect(
            self._on_blind_labeling_toggled,
        )
        blind_row.addWidget(self._blind_mode_button)
        self._blind_mode_status = QLabel("assisted / not auditable")
        self._blind_mode_status.setWordWrap(True)
        self._blind_mode_status.setStyleSheet("font-size: 10px; color: #d97706;")
        blind_row.addWidget(self._blind_mode_status, 1)
        form.addRow("Labeling provenance:", blind_row)

        self._primary_recon_combo = QComboBox()
        self._primary_recon_combo.addItem("(unlabeled)", RECON_UNLABELED)
        for name in PRIMARY_LABEL_OPTIONS:
            display_name = (
                "1x1 (legacy → none/weak)" if name == "1x1" else name
            )
            self._primary_recon_combo.addItem(display_name, name)
        self._primary_recon_combo.activated.connect(
            self._on_primary_recon_activated,
        )
        form.addRow("Primary reconstruction:", self._primary_recon_combo)

        self._primary_confidence_combo = QComboBox()
        self._primary_confidence_combo.setObjectName("primaryLabelConfidence")
        self._primary_confidence_combo.addItem("not rated", "")
        self._primary_confidence_combo.addItem("low", "0.5")
        self._primary_confidence_combo.addItem("medium", "0.75")
        self._primary_confidence_combo.addItem("high", "1.0")
        form.addRow("Primary confidence:", self._primary_confidence_combo)

        # Change from → to (Jul 15 2026). Same RECON_LABEL_OPTIONS list
        # as the primary dropdown; growers get familiar visual + kbd
        # muscle memory. Both default to RECON_UNLABELED so a grower
        # who only cares about the single-class primary label doesn't
        # accidentally write a partial transition.
        self._change_from_combo = QComboBox()
        self._change_from_combo.addItem("(unlabeled)", RECON_UNLABELED)
        for name in RECON_LABEL_OPTIONS:
            self._change_from_combo.addItem(name, name)
        self._change_from_combo.activated.connect(
            self._on_change_from_activated,
        )

        self._change_to_combo = QComboBox()
        self._change_to_combo.addItem("(unlabeled)", RECON_UNLABELED)
        for name in RECON_LABEL_OPTIONS:
            self._change_to_combo.addItem(name, name)
        self._change_to_combo.activated.connect(
            self._on_change_to_activated,
        )

        change_row = QHBoxLayout()
        change_row.setSpacing(6)
        change_row.setContentsMargins(0, 0, 0, 0)
        change_row.addWidget(self._change_from_combo, 1)
        arrow_label = QLabel("→")
        arrow_label.setStyleSheet("font-size: 14px; padding: 0 4px;")
        arrow_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        change_row.addWidget(arrow_label, 0)
        change_row.addWidget(self._change_to_combo, 1)
        form.addRow("Change (from → to):", change_row)

        self._notes_input = QLineEdit()
        self._notes_input.setPlaceholderText("Optional notes…")
        self._notes_input.textChanged.connect(self._on_notes_text_changed)
        self._notes_input.editingFinished.connect(self._flush_notes_to_disk)
        form.addRow("Notes:", self._notes_input)

        # Experimental: classify the currently displayed frame via Classifier2.
        # Lazy-loaded on first click — Bulbasaur needs torch + AI_for_quantum
        # cloned. AI_REPO_ROOT env var overrides the platform default. Result
        # is shown inline; full breakdown via tooltip on hover.
        self._classifier = None  # ClassifierBridge instance, lazy-loaded
        self._classifier_load_error: Optional[str] = None
        classify_row = QHBoxLayout()
        classify_row.setSpacing(6)
        self._classify_button = QPushButton("Classify!")
        self._classify_button.setToolTip(
            "Run Classifier2 on the currently displayed buffer frame "
            "(slider position). Lazy-loads the model on first click."
        )
        self._classify_button.clicked.connect(self._on_classify_clicked)
        classify_row.addWidget(self._classify_button)
        self._classifier_result_label = QLabel("<i>not yet classified</i>")
        self._classifier_result_label.setStyleSheet("color: #888;")
        self._classifier_result_label.setWordWrap(True)
        classify_row.addWidget(self._classifier_result_label, 1)
        form.addRow("Classifier:", classify_row)

        # Launch the Equalizer in a separate top-level window pre-loaded with
        # the currently displayed frame. Per PI direction May 8 + AJ's May 19
        # clarification: separate non-modal window so growers can label
        # without losing sight of the live Growth Monitor.
        self._equalizer_window = None  # held to prevent GC of the popup
        equalizer_row = QHBoxLayout()
        equalizer_row.setSpacing(6)
        self._equalizer_button = QPushButton("Label with Equalizer…")
        self._equalizer_button.setToolTip(
            "Open the currently displayed buffer frame in the RHEED "
            "Equalizer (separate window). A complete capture manifest and "
            "accepted camera calibration are required; HTR is unavailable "
            "until a canonical basis exists."
        )
        self._equalizer_button.clicked.connect(self._on_open_equalizer)
        equalizer_row.addWidget(self._equalizer_button)
        equalizer_row.addStretch(1)
        form.addRow("Equalizer:", equalizer_row)

        return box

    def _set_blind_button_checked(self, checked: bool) -> None:
        self._blind_mode_button.blockSignals(True)
        self._blind_mode_button.setChecked(bool(checked))
        self._blind_mode_button.blockSignals(False)

    def _apply_blind_mode_ui(
        self, active: bool, *, gold_eligible: bool, reason: str,
    ) -> None:
        """Make model-derived answers unavailable while blind mode is active."""
        self._blind_labeling_mode = bool(active)
        self._blind_state_reason = str(reason)
        self._set_blind_button_checked(active)
        self._blind_mode_button.setText(
            "Exit blind labeling mode" if active
            else "Enter blind labeling mode"
        )
        if active and gold_eligible:
            self._blind_mode_status.setText("blind gold eligible")
            self._blind_mode_status.setStyleSheet(
                "font-size: 10px; color: #16a34a; font-weight: bold;"
            )
        elif active:
            self._blind_mode_status.setText("outputs hidden; labels remain assisted")
            self._blind_mode_status.setStyleSheet(
                "font-size: 10px; color: #d97706; font-weight: bold;"
            )
        else:
            self._blind_mode_status.setText("assisted / not blind")
            self._blind_mode_status.setStyleSheet(
                "font-size: 10px; color: #d97706;"
            )
        self._blind_mode_status.setToolTip(reason)
        self._classify_button.setEnabled(not active)
        self._classifier_result_label.setVisible(not active)
        self._equalizer_button.setEnabled(not active)
        if active and self._equalizer_window is not None:
            self._equalizer_window.close()
            self._equalizer_window = None
        self.blind_labeling_mode_changed.emit(bool(active))

    @pyqtSlot(bool)
    def _on_blind_labeling_toggled(self, checked: bool) -> None:
        logger = self._growth_logger
        if logger is None or self._session_dir is None:
            self._set_blind_button_checked(False)
            QMessageBox.warning(
                self, "Blind labeling unavailable",
                "Attach a growth session before entering blind labeling mode.",
            )
            return
        if checked and not self._labeler:
            self._set_blind_button_checked(False)
            QMessageBox.warning(
                self, "Labeler required",
                "Enter the grower/labeler name before blind labeling.",
            )
            return
        try:
            status = logger.set_human_blind_labeling_mode(
                checked, labeler=self._labeler,
            )
        except (OSError, TypeError, ValueError) as exc:
            self._set_blind_button_checked(False)
            self._apply_blind_mode_ui(
                False, gold_eligible=False, reason=str(exc),
            )
            QMessageBox.warning(self, "Blind labeling unavailable", str(exc))
            return
        self._apply_blind_mode_ui(
            bool(status["blind_mode_active"]),
            gold_eligible=bool(status["gold_eligible"]),
            reason=str(status["reason"]),
        )

    def record_classifier_output_visible(
        self, frame_path: Optional[Path] = None,
    ) -> bool:
        """Persist classifier exposure; it can never be undone in-session."""
        logger = self._growth_logger
        if logger is None:
            return True
        record_exposure = getattr(
            logger, "record_human_label_output_exposure", None,
        )
        if not callable(record_exposure):
            return True
        if not record_exposure(
            "classifier", frame_path=frame_path,
        ):
            return False
        self._apply_blind_mode_ui(
            False,
            gold_eligible=False,
            reason="Classifier output was displayed in this session.",
        )
        return True

    def record_equalizer_output_visible(
        self, frame_path: Optional[Path] = None,
    ) -> bool:
        """Persist Equalizer exposure; it can never be undone in-session."""
        logger = self._growth_logger
        if logger is None:
            return True
        record_exposure = getattr(
            logger, "record_human_label_output_exposure", None,
        )
        if not callable(record_exposure):
            return True
        if not record_exposure(
            "equalizer", frame_path=frame_path,
        ):
            return False
        self._apply_blind_mode_ui(
            False,
            gold_eligible=False,
            reason="Equalizer output was displayed in this session.",
        )
        return True

    # =====================================================================
    # === SESSION LIFECYCLE ===
    # === (public API used by GrowthApp) ===
    # =====================================================================

    def attach_session(
        self, growth_logger: Optional[GrowthLogger], *, labeler: str = "",
    ) -> None:
        """Point the tab at the active session's logger and reload the list.

        Call from GrowthApp._on_start after GrowthLogger.start_session.
        Clearing + reloading on attach handles GUI-restart-mid-session and
        keeps the tab consistent with whatever's on disk. Also resets the
        detail pane back to its placeholder so a stale selection from the
        previous session can't render against the new session_dir, and
        repopulates the label cache from events_labels.csv (if any).

        Takes the GrowthLogger rather than a bare session_dir because the
        Events tab now owns label CSV writes, which need the logger's
        update_event_label method.
        """
        # A popup is bound to one logger/session/frame.  Close it before
        # switching sessions so a delayed Save signal cannot write into the
        # newly attached session.
        if self._equalizer_window is not None:
            self._equalizer_window.close()
            self._equalizer_window = None
        self._pending_retrospective_accept_context = None
        self._pending_retrospective_invalidation = None
        self._pending_retrospective_resolve_context = None

        self._growth_logger = growth_logger
        self._labeler = str(labeler or "").strip()
        self._session_dir = (
            growth_logger.session_dir
            if growth_logger is not None and growth_logger.session_dir is not None
            else None
        )
        begin_review = getattr(
            growth_logger, "begin_human_labeling_review", None,
        )
        if (
            growth_logger is not None
            and self._session_dir is not None
            and callable(begin_review)
        ):
            blind_status = begin_review(self._labeler)
        else:
            blind_status = {
                "blind_mode_active": False,
                "gold_eligible": False,
                "reason": "No labeling session is attached.",
            }
        self._apply_blind_mode_ui(
            bool(blind_status["blind_mode_active"]),
            gold_eligible=bool(blind_status["gold_eligible"]),
            reason=str(blind_status["reason"]),
        )
        self._last_seen_event_idx = 0
        self.events_table.setRowCount(0)
        self._cached_pixmaps = []
        self._cached_paths = []
        self._capture_metadata_by_filename = {}
        self._capture_manifest_error = "No event buffer is selected."
        self._image_label.clearImage()
        self._labels_cache = (
            growth_logger.read_event_labels()
            if growth_logger is not None else {}
        )
        self._currently_displayed_event_idx = None
        self._notes_save_timer.stop()
        self._set_placeholder("Select an event to view buffer frames.")
        self._load_csv_rows()

    # =====================================================================
    # === LIVE UPDATES ===
    # === (slots wired by GrowthApp to AutoCaptureEngine + GrowthMonitor) ===
    # =====================================================================

    @pyqtSlot(np.ndarray, float)
    def on_frame_captured(self, frame: np.ndarray, score: float) -> None:  # noqa: ARG002
        """Slot for ``AutoCaptureEngine.frame_captured``.

        The engine emits ``(frame, score)`` only, so we re-read the CSV to
        recover ``event_idx``, ``timestamp``, and ``pyrometer_temp_C`` that
        GrowthApp's earlier-connected handler just wrote. Args are kept
        for signal compatibility but ignored — the CSV row is authoritative.
        """
        self._load_csv_rows()

    @pyqtSlot(int, str, str)
    def on_decision_made(
        self, event_idx: int, buffer_dir: str, state: str,  # noqa: ARG002
    ) -> None:
        """Slot for ``GrowthMonitor.auto_capture_decision``.

        Updates the matching row's state cell + cached UserRole dict and
        — if that row is the currently-selected one — re-renders the
        detail pane's metadata header. Refreshes the unreviewed-count
        badge afterward. ``buffer_dir`` is part of the signal payload
        for GrowthApp's CSV writer; the tab doesn't need it.

        Connection order: GrowthApp's _on_auto_capture_decision is wired
        first and rewrites auto_capture_events.csv via
        update_auto_capture_state. By the time this slot runs, the CSV
        row already reflects the new state — but we don't need to re-read
        it because ``state`` is in the signal payload.
        """
        target = str(event_idx)
        for row_idx in range(self.events_table.rowCount()):
            idx_item = self.events_table.item(row_idx, COL_EVENT_IDX)
            if idx_item is None or idx_item.text() != target:
                continue

            state_item = self.events_table.item(row_idx, COL_STATE)
            if state_item is not None:
                state_item.setText(state)
            else:
                self.events_table.setItem(
                    row_idx, COL_STATE, QTableWidgetItem(state),
                )

            row_data = idx_item.data(Qt.ItemDataRole.UserRole)
            if isinstance(row_data, dict):
                row_data["event_state"] = state
                idx_item.setData(Qt.ItemDataRole.UserRole, row_data)
                if self.events_table.currentRow() == row_idx:
                    self._metadata_label.setText(
                        self._format_metadata(row_data)
                    )
            break

        self._refresh_unreviewed_badge()

    # =====================================================================
    # === MASTER LIST I/O ===
    # === (CSV read → table population, badge recompute) ===
    # =====================================================================

    def _load_csv_rows(self) -> None:
        """Append rows past the watermark from auto_capture_events.csv.

        Wrapped in try/finally so the unreviewed badge always refreshes
        — including the early-return paths (no session, missing CSV)
        and OSError. Without this, attaching to a fresh session whose
        CSV file doesn't exist yet would leave the badge stale at the
        previous session's count.
        """
        try:
            if self._session_dir is None:
                return
            csv_path = self._session_dir / "auto_capture_events.csv"
            if not csv_path.exists():
                return
            try:
                with open(csv_path, "r", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        try:
                            idx = int(row.get("event_idx", "") or 0)
                        except (TypeError, ValueError):
                            continue
                        if idx <= self._last_seen_event_idx:
                            continue
                        self._add_event_row(row)
                        self._last_seen_event_idx = idx
            except OSError:
                return
        finally:
            self._refresh_unreviewed_badge()

    def _refresh_unreviewed_badge(self) -> None:
        """Recompute unreviewed-event count and emit the change signal.

        An event "needs attention" if:
          - state ∈ (pending, kept_default), OR
          - state == kept_explicit AND no primary_reconstruction label

        Discarded events never count — the explicit "no" decision is
        final, and they don't need labeling. The kept_explicit-without-
        label clause makes the badge useful through the labeling phase
        of the workflow, not just the keep/discard phase.

        Walks the table + label cache fresh each time rather than
        maintaining a counter — cheap (≤ ~50 rows per session) and stays
        consistent when other operations (e.g., a future hide-discarded
        filter) change the displayed-vs-existing row set.
        """
        count = 0
        for row_idx in range(self.events_table.rowCount()):
            state_item = self.events_table.item(row_idx, COL_STATE)
            if state_item is None:
                continue
            state = state_item.text()
            if state == EVENT_STATE_DISCARDED:
                continue
            if state not in _REVIEWED_STATES:
                count += 1
                continue
            # state == EVENT_STATE_KEPT_EXPLICIT — also unreviewed if no label
            idx_item = self.events_table.item(row_idx, COL_EVENT_IDX)
            try:
                event_idx = int(idx_item.text()) if idx_item else None
            except (TypeError, ValueError):
                event_idx = None
            label = (
                self._labels_cache.get(event_idx)
                if event_idx is not None else None
            )
            if not label or not label.get("primary_reconstruction"):
                count += 1
        self.unreviewed_count_changed.emit(count)

    def _add_event_row(self, row: dict) -> None:
        """Insert one CSV row at the top of the master list (newest first).

        The full CSV row dict is attached to the event_idx item via
        Qt.UserRole so the selection handler can recover buffer_dir,
        timestamp, etc. without re-reading the CSV.
        """
        timestamp = row.get("timestamp", "")
        try:
            time_str = datetime.fromisoformat(timestamp).strftime("%H:%M:%S")
        except (TypeError, ValueError):
            time_str = timestamp[-8:] if timestamp else "---"

        score_raw = row.get("change_score", "")
        try:
            score_str = f"{float(score_raw):.2f}"
        except (TypeError, ValueError):
            score_str = score_raw or "---"

        temp_raw = row.get("pyrometer_temp_C", "")
        try:
            temp_str = (
                f"{float(temp_raw):.1f}" if temp_raw not in ("", None) else "---"
            )
        except (TypeError, ValueError):
            temp_str = "---"

        state = row.get("event_state", "") or "---"

        # Insert at top so the most-recent event surfaces first; iterating
        # the CSV oldest→newest with insertRow(0) leaves the table in
        # newest-at-top order.
        self.events_table.insertRow(0)
        idx_item = QTableWidgetItem(str(row.get("event_idx", "")))
        idx_item.setData(Qt.ItemDataRole.UserRole, dict(row))
        self.events_table.setItem(0, COL_EVENT_IDX, idx_item)
        self.events_table.setItem(0, COL_TIME, QTableWidgetItem(time_str))
        self.events_table.setItem(0, COL_SCORE, QTableWidgetItem(score_str))
        self.events_table.setItem(0, COL_TEMP, QTableWidgetItem(temp_str))
        self.events_table.setItem(0, COL_STATE, QTableWidgetItem(state))

    # =====================================================================
    # === DETAIL PANE ===
    # === (placeholder ↔ content, selection handling, image display) ===
    # =====================================================================

    def _set_placeholder(self, text: str) -> None:
        """Show the placeholder copy and hide the image viewer."""
        self._detail_placeholder.setText(text)
        self._detail_placeholder.show()
        self._detail_content.hide()

    def _show_detail_content(self) -> None:
        """Show the image viewer and hide the placeholder."""
        self._detail_placeholder.hide()
        self._detail_content.show()

    def _on_selection_changed(self) -> None:
        """Handler for ``events_table.itemSelectionChanged``.

        Resolves the selected row's CSV dict (stored on the event_idx
        item's UserRole) and routes to the detail loader. Empty
        selections — including the cleared-table state right after
        attach_session — fall back to the placeholder.

        Flushes any pending debounced notes write for the *previous*
        event before switching, so unsaved typing doesn't get lost when
        the user clicks another row mid-sentence.
        """
        # Flush before switching — the user may have typed in notes for the
        # previous event without explicitly tabbing out.
        if self._notes_save_timer.isActive():
            self._notes_save_timer.stop()
            self._flush_notes_to_disk()

        row = self.events_table.currentRow()
        if row < 0 or not self.events_table.selectedItems():
            self._set_placeholder("Select an event to view buffer frames.")
            self._cached_pixmaps = []
            self._cached_paths = []
            self._capture_metadata_by_filename = {}
            self._capture_manifest_error = "No event buffer is selected."
            self._image_label.clearImage()
            self._currently_displayed_event_idx = None
            return

        idx_item = self.events_table.item(row, COL_EVENT_IDX)
        if idx_item is None:
            return
        row_data = idx_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row_data, dict):
            return
        self._load_detail(row_data)

    def _load_detail(self, row_data: dict) -> None:
        """Populate the detail pane for the given CSV row.

        Walks through the failure-mode ladder (no buffer dir recorded →
        dir missing on disk → dir empty) and shows a context-specific
        placeholder for each. On success, decodes all PNGs once into the
        pixmap cache so slider scrubbing is instantaneous.
        """
        event_idx = row_data.get("event_idx", "?")
        buffer_dir = row_data.get("buffer_dir", "") or ""
        self._cached_paths = []
        self._cached_pixmaps = []
        self._capture_metadata_by_filename = {}
        self._capture_manifest_error = "Capture manifest was not loaded."

        if self._session_dir is None or not buffer_dir:
            self._set_placeholder(
                f"Event #{event_idx} has no buffer directory recorded."
            )
            return

        full_dir, path_error = self._resolve_event_buffer_dir(buffer_dir)
        if full_dir is None:
            self._capture_manifest_error = path_error
            self._set_placeholder(
                f"Event #{event_idx} — unsafe buffer directory:\n{path_error}"
            )
            return
        if not full_dir.exists() or not full_dir.is_dir():
            self._set_placeholder(
                f"Event #{event_idx} — buffer directory not found:\n{buffer_dir}"
            )
            return

        # Match both .png (legacy sessions before Jul 9 2026) and .bmp
        # (post-switch to match Justin's training data format). Sorted
        # together so within-session interleave is stable.
        discovered_paths = sorted(
            list(full_dir.glob("*.png")) + list(full_dir.glob("*.bmp"))
        )
        frames_root = (self._session_dir / "frames").resolve()
        frame_paths: list[Path] = []
        for candidate in discovered_paths:
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(frames_root)
                resolved.relative_to(full_dir)
            except (OSError, ValueError):
                self._capture_manifest_error = (
                    "A buffered frame resolves outside its event directory or "
                    "session frames/."
                )
                self._set_placeholder(
                    f"Event #{event_idx} — unsafe buffered frame path."
                )
                return
            if not resolved.is_file():
                self._capture_manifest_error = "A buffered frame path is not a file."
                self._set_placeholder(
                    f"Event #{event_idx} — invalid buffered frame path."
                )
                return
            frame_paths.append(resolved)
        if not frame_paths:
            self._set_placeholder(
                f"Event #{event_idx} — buffer directory is empty."
            )
            return

        self._cached_paths = frame_paths
        self._cached_pixmaps = [QPixmap(str(p)) for p in frame_paths]
        (
            self._capture_metadata_by_filename,
            self._capture_manifest_error,
        ) = self._load_capture_manifest(full_dir, frames_root=frames_root)

        self._metadata_label.setText(self._format_metadata(row_data))
        self._show_detail_content()

        # Bind the labeling form to this event before we populate it,
        # so any side-effect signals from setText/setCurrentIndex don't
        # write under a stale event_idx.
        try:
            self._currently_displayed_event_idx = int(event_idx)
        except (TypeError, ValueError):
            self._currently_displayed_event_idx = None
        self._populate_label_form(self._currently_displayed_event_idx)

        # Default to the trigger frame (last one buffered before the
        # signal fired) — most informative single frame for the grower.
        last_idx = len(self._cached_pixmaps) - 1
        self._slider.blockSignals(True)
        self._slider.setRange(0, last_idx)
        self._slider.setValue(last_idx)
        self._slider.blockSignals(False)
        self._display_frame_at(last_idx)

    @staticmethod
    def _parse_manifest_bool(value: object, field: str) -> bool:
        text = str(value or "").strip().lower()
        if text in _TRUE_TEXT:
            return True
        if text in _FALSE_TEXT:
            return False
        raise ValueError(f"{field} must be an explicit true/false value")

    def _resolve_event_buffer_dir(
        self, buffer_dir: object,
    ) -> tuple[Optional[Path], str]:
        """Resolve one event directory strictly beneath ``<session>/frames``."""
        if self._session_dir is None:
            return None, "No growth session is attached."
        text = str(buffer_dir or "").strip()
        if not text:
            return None, "The event buffer path is blank."
        relative = Path(text)
        if relative.is_absolute() or relative.drive or relative.root:
            return None, "Absolute event buffer paths are not accepted."
        if ".." in relative.parts:
            return None, "Parent-directory traversal is not accepted."
        try:
            session_dir = self._session_dir.resolve()
            frames_root = (session_dir / "frames").resolve()
            resolved = (session_dir / relative).resolve()
            remainder = resolved.relative_to(frames_root)
        except (OSError, ValueError):
            return None, "The event buffer path escapes the session frames directory."
        if not remainder.parts:
            return None, "The event buffer path must name a directory below frames/."
        return resolved, ""

    @classmethod
    def _normalize_manifest_row(cls, row: dict) -> dict:
        """Return typed provenance for one historical frame.

        The conversion is intentionally strict: manufacturing a receive time
        or QC state for a legacy row would make an Equalizer label appear more
        synchronized and better traced than the underlying capture actually
        was.
        """
        metadata = dict(row)
        frame_name = str(metadata.get("frame_path", "") or "").strip()
        if not frame_name or Path(frame_name).name != frame_name:
            raise ValueError("frame_path must be one frame filename")

        for field in (
            "capture_backend", "capture_geometry_id", "captured_at_utc",
            "session_id", "basis_bundle_id",
        ):
            if not str(metadata.get(field, "") or "").strip():
                raise ValueError(f"{field} is blank")

        try:
            metadata["capture_sequence"] = int(metadata["capture_sequence"])
            metadata["captured_monotonic_ns"] = int(
                metadata["captured_monotonic_ns"]
            )
            metadata["source_hwnd"] = int(metadata["source_hwnd"])
            metadata["camera_width"] = int(metadata["camera_width"])
            metadata["camera_height"] = int(metadata["camera_height"])
            metadata["view_segment_id"] = int(metadata["view_segment_id"])
            metadata["visual_history_generation"] = int(
                metadata["visual_history_generation"]
            )
            metadata["frame_age_ms"] = float(metadata["frame_age_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("numeric capture provenance is invalid") from exc

        if metadata["capture_sequence"] < 0:
            raise ValueError("capture_sequence cannot be negative")
        if metadata["captured_monotonic_ns"] <= 0:
            raise ValueError("captured_monotonic_ns must be positive")
        if metadata["camera_width"] <= 0 or metadata["camera_height"] <= 0:
            raise ValueError("camera dimensions must be positive")
        if metadata["view_segment_id"] < 0:
            raise ValueError("view_segment_id cannot be negative")
        if metadata["visual_history_generation"] < 0:
            raise ValueError("visual_history_generation cannot be negative")
        if not math.isfinite(metadata["frame_age_ms"]) or metadata["frame_age_ms"] < 0:
            raise ValueError("frame_age_ms must be finite and non-negative")

        metadata["frame_path"] = frame_name
        metadata["capture_backend"] = str(metadata["capture_backend"]).strip()
        metadata["capture_geometry_id"] = str(
            metadata["capture_geometry_id"]
        ).strip()
        captured_at_text = str(metadata["captured_at_utc"]).strip()
        try:
            captured_at = datetime.fromisoformat(captured_at_text)
        except ValueError as exc:
            raise ValueError("captured_at_utc is not ISO-8601") from exc
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at_utc must include a UTC offset")
        if captured_at.utcoffset().total_seconds() != 0:
            raise ValueError("captured_at_utc must be UTC")
        metadata["captured_at_utc"] = captured_at.astimezone(timezone.utc).isoformat()
        metadata["session_id"] = str(metadata["session_id"]).strip()
        metadata["basis_bundle_id"] = str(metadata["basis_bundle_id"]).strip()
        metadata["gun_aligned"] = cls._parse_manifest_bool(
            metadata.get("gun_aligned"), "gun_aligned",
        )
        metadata["realignment_active"] = cls._parse_manifest_bool(
            metadata.get("realignment_active"), "realignment_active",
        )
        metadata["calibration_id"] = str(
            metadata.get("calibration_id", "") or ""
        ).strip()
        return metadata

    @classmethod
    def _load_capture_manifest(
        cls,
        event_dir: Path,
        *,
        frames_root: Optional[Path] = None,
    ) -> tuple[dict[str, dict], str]:
        """Load filename-keyed frame provenance, failing closed on ambiguity."""
        path = event_dir / "capture_manifest.csv"
        if frames_root is not None:
            try:
                resolved_root = frames_root.resolve()
                resolved_event = event_dir.resolve(strict=True)
                remainder = resolved_event.relative_to(resolved_root)
            except (OSError, ValueError):
                return {}, "Capture manifest resolves outside session frames/."
            if not remainder.parts:
                return {}, "Capture manifest must belong to an event below frames/."
        if not path.is_file():
            return {}, "This event has no capture_manifest.csv (legacy buffer)."
        if frames_root is not None:
            try:
                path = path.resolve(strict=True)
                path.relative_to(resolved_root)
                path.relative_to(resolved_event)
            except (OSError, ValueError):
                return {}, "Capture manifest resolves outside session frames/."
        try:
            with open(path, "r", newline="") as stream:
                reader = csv.DictReader(stream)
                fields = set(reader.fieldnames or ())
                missing = [name for name in _EQUALIZER_MANIFEST_FIELDS if name not in fields]
                if missing:
                    return {}, "Capture manifest is missing fields: " + ", ".join(missing)
                result: dict[str, dict] = {}
                for row_number, row in enumerate(reader, start=2):
                    try:
                        metadata = cls._normalize_manifest_row(row)
                    except ValueError as exc:
                        return {}, f"Capture manifest row {row_number} is invalid: {exc}"
                    frame_name = metadata["frame_path"]
                    if frame_name in result:
                        return {}, f"Capture manifest has duplicate frame: {frame_name}"
                    result[frame_name] = metadata
        except OSError as exc:
            return {}, f"Capture manifest could not be read: {exc}"
        if not result:
            return {}, "Capture manifest contains no frame rows."
        return result, ""

    @staticmethod
    def _format_metadata(row_data: dict) -> str:
        """Render the metadata header line for an event."""
        event_idx = row_data.get("event_idx", "?")

        timestamp = row_data.get("timestamp", "")
        try:
            time_str = datetime.fromisoformat(timestamp).strftime("%H:%M:%S")
        except (TypeError, ValueError):
            time_str = "—"

        score_raw = row_data.get("change_score", "")
        try:
            score_str = f"{float(score_raw):.2f}"
        except (TypeError, ValueError):
            score_str = "—"

        temp_raw = row_data.get("pyrometer_temp_C", "")
        try:
            temp_str = (
                f"{float(temp_raw):.1f} ℃" if temp_raw not in ("", None) else "—"
            )
        except (TypeError, ValueError):
            temp_str = "—"

        state = row_data.get("event_state", "?") or "?"

        return (
            f"Event #{event_idx}  ·  {time_str}  ·  "
            f"score {score_str}  ·  {temp_str}  ·  {state}"
        )

    # =====================================================================
    # === LABELING FORM ===
    # === (populate on select + atomic-per-change write slots) ===
    # =====================================================================

    def _populate_label_form(self, event_idx: Optional[int]) -> None:
        """Pre-fill the labeling form from the cache for ``event_idx``.

        Programmatic setCurrentIndex on the QComboBox doesn't fire
        ``activated`` (only user interaction does), so no blockSignals
        is needed for the three combos. QLineEdit.setText DOES fire
        ``textChanged``, which would start the debounce timer
        pointlessly — so we block signals around the notes setText.
        """
        label = (
            self._labels_cache.get(event_idx)
            if event_idx is not None else None
        )
        primary = (label or {}).get("human_primary_reconstruction") or (
            label or {}
        ).get("primary_reconstruction", RECON_UNLABELED)
        change_from = (label or {}).get("change_from", RECON_UNLABELED)
        change_to = (label or {}).get("change_to", RECON_UNLABELED)
        notes = (label or {}).get("notes", "")

        for combo, value in (
            (self._primary_recon_combo, primary),
            (self._change_from_combo, change_from),
            (self._change_to_combo, change_to),
        ):
            target_idx = combo.findData(value)
            if target_idx < 0:
                target_idx = 0  # fall back to "(unlabeled)"
            combo.setCurrentIndex(target_idx)

        self._notes_input.blockSignals(True)
        self._notes_input.setText(notes)
        self._notes_input.blockSignals(False)
        confidence = (label or {}).get("human_confidence", "")
        confidence_idx = self._primary_confidence_combo.findData(confidence)
        self._primary_confidence_combo.setCurrentIndex(
            confidence_idx if confidence_idx >= 0 else 0
        )

    @pyqtSlot(int)
    def _on_primary_recon_activated(self, idx: int) -> None:
        """Persist the new Primary reconstruction selection.

        ``activated`` fires only on user interaction, so getting here
        means the grower changed the dropdown. Writes atomically through
        the logger, keeps the cache in sync, and refreshes the badge —
        the new label may flip a kept_explicit-without-label event into
        the reviewed bucket (or the reverse, if "(unlabeled)" was picked).
        """
        if (
            self._currently_displayed_event_idx is None
            or self._growth_logger is None
        ):
            return
        selected_primary = self._primary_recon_combo.itemData(idx)
        if not selected_primary:
            # Clearing the mutable event summary never erases an earlier
            # append-only annotation. Corrections must be submitted as a new
            # explicit label so the full vote history remains auditable.
            kwargs: dict[str, object] = {
                "primary_reconstruction": "",
                "human_primary_reconstruction": "",
            }
        else:
            if not self._labeler:
                QMessageBox.warning(
                    self, "Labeler required",
                    "A grower/labeler name is required. No human label was saved.",
                )
                self._populate_label_form(self._currently_displayed_event_idx)
                return
            frame_idx = self._slider.value()
            if not (0 <= frame_idx < len(self._cached_paths)):
                QMessageBox.warning(
                    self, "Exact frame required",
                    "Select one captured frame before assigning a primary label.",
                )
                self._populate_label_form(self._currently_displayed_event_idx)
                return
            frame_path = self._cached_paths[frame_idx]
            metadata = self._capture_metadata_by_filename.get(frame_path.name)
            if metadata is None:
                QMessageBox.warning(
                    self, "Exact frame provenance required",
                    "This buffer has no complete capture_manifest.csv record. "
                    "No human label was saved.",
                )
                self._populate_label_form(self._currently_displayed_event_idx)
                return
            # 1x1 is a legacy UI alias for the none/weak presence state, not
            # one of the four conditional superstructure classes.
            primary = (
                "none/weak" if selected_primary == "1x1"
                else selected_primary
            )
            try:
                provenance = self._growth_logger.human_primary_provenance(
                    frame_path=frame_path,
                    labeler=self._labeler,
                )
            except (OSError, TypeError, ValueError) as exc:
                QMessageBox.warning(self, "Primary label blocked", str(exc))
                self._populate_label_form(self._currently_displayed_event_idx)
                return
            kwargs = {
                "human_primary_reconstruction": primary,
                "human_label_source": provenance["human_label_source"],
                "human_labeler": self._labeler,
                "human_confidence": (
                    self._primary_confidence_combo.currentData() or ""
                ),
                "human_blind_to_model": provenance["human_blind_to_model"],
                "human_blind_to_equalizer": provenance[
                    "human_blind_to_equalizer"
                ],
                "human_frame_path": str(frame_path),
                "human_capture_metadata": metadata,
                "human_annotation_id": uuid.uuid4().hex,
            }
        try:
            saved = self._growth_logger.update_event_label(
                self._currently_displayed_event_idx,
                **kwargs,
            )
        except (OSError, TypeError, ValueError) as exc:
            saved = False
            QMessageBox.warning(self, "Primary label blocked", str(exc))
        if not saved:
            self._populate_label_form(self._currently_displayed_event_idx)
            return
        refreshed = self._growth_logger.read_event_labels().get(
            self._currently_displayed_event_idx,
        )
        if refreshed is not None:
            self._labels_cache[self._currently_displayed_event_idx] = refreshed
        self._refresh_unreviewed_badge()

    @pyqtSlot(int)
    def _on_change_from_activated(self, idx: int) -> None:
        """Persist the new Change (from) selection to events_labels.csv.

        Mirrors _on_primary_recon_activated but writes only the
        change_from column. Does NOT refresh the unreviewed-count badge:
        the badge only tracks primary_reconstruction (see
        _refresh_unreviewed_badge), so change_from/to updates don't
        affect the "kept_explicit-without-label" bucket. Downstream
        (Yuxin's #1 pipeline) consumes change_from/to as its own signal
        distinct from the single-class primary label.
        """
        if (
            self._currently_displayed_event_idx is None
            or self._growth_logger is None
        ):
            return
        value = self._change_from_combo.itemData(idx)
        self._growth_logger.update_event_label(
            self._currently_displayed_event_idx,
            change_from=value,
        )
        cached = self._labels_cache.setdefault(
            self._currently_displayed_event_idx,
            {f: "" for f in GrowthLogger.EVENT_LABEL_FIELDS},
        )
        cached["event_idx"] = str(self._currently_displayed_event_idx)
        cached["change_from"] = value
        cached["label_timestamp_iso"] = datetime.now().isoformat()

    @pyqtSlot(int)
    def _on_change_to_activated(self, idx: int) -> None:
        """Persist the new Change (to) selection to events_labels.csv.

        Same pattern + rationale as _on_change_from_activated. Writes
        the change_to column only. See that docstring for badge-refresh
        semantics.
        """
        if (
            self._currently_displayed_event_idx is None
            or self._growth_logger is None
        ):
            return
        value = self._change_to_combo.itemData(idx)
        self._growth_logger.update_event_label(
            self._currently_displayed_event_idx,
            change_to=value,
        )
        cached = self._labels_cache.setdefault(
            self._currently_displayed_event_idx,
            {f: "" for f in GrowthLogger.EVENT_LABEL_FIELDS},
        )
        cached["event_idx"] = str(self._currently_displayed_event_idx)
        cached["change_to"] = value
        cached["label_timestamp_iso"] = datetime.now().isoformat()

    @pyqtSlot()
    def _on_notes_text_changed(self) -> None:
        """Restart the debounced notes-save timer on every keystroke."""
        self._notes_save_timer.start()

    def _flush_notes_to_disk(self) -> None:
        """Write the current notes-field text to events_labels.csv.

        Idempotent — called from the debounce timer, the field's
        editingFinished signal, and the selection-change flush path.
        Notes don't affect the badge (only primary_reconstruction does),
        so no refresh is emitted from here.
        """
        self._notes_save_timer.stop()
        if (
            self._currently_displayed_event_idx is None
            or self._growth_logger is None
        ):
            return
        notes = self._notes_input.text()
        self._growth_logger.update_event_label(
            self._currently_displayed_event_idx,
            notes=notes,
        )
        cached = self._labels_cache.setdefault(
            self._currently_displayed_event_idx,
            {f: "" for f in GrowthLogger.EVENT_LABEL_FIELDS},
        )
        cached["event_idx"] = str(self._currently_displayed_event_idx)
        cached["notes"] = notes
        cached["label_timestamp_iso"] = datetime.now().isoformat()

    @pyqtSlot(int)
    def _display_frame_at(self, idx: int) -> None:
        """Show the frame at slider index ``idx``.

        Bounds-checked because Qt may emit valueChanged transiently
        during setRange/setValue calls; we already block signals around
        those, but defensive guarding is cheap.
        """
        if not (0 <= idx < len(self._cached_pixmaps)):
            return
        self._image_label.setOriginalPixmap(self._cached_pixmaps[idx])
        self._frame_position_label.setText(
            f"{idx + 1} / {len(self._cached_pixmaps)}"
        )
        # Reset the classifier label whenever the displayed frame changes —
        # the displayed result is for the previously-classified frame and
        # would be misleading otherwise. User must click Classify again.
        if hasattr(self, "_classifier_result_label"):
            self._classifier_result_label.setText("<i>not yet classified</i>")
            self._classifier_result_label.setStyleSheet("color: #888;")

    # =====================================================================
    # === CLASSIFIER + EQUALIZER ===
    # === (per-event classify button + Equalizer launcher for labeling) ===
    # =====================================================================

    # Known AI_for_quantum clone locations, in preference order.
    # Extend this list as new lab machines onboard rather than requiring
    # each site to set AI_REPO_ROOT. Same pattern as ElogReader's
    # KNOWN_LOG_DIRS (drivers/evap_control.py).
    _KNOWN_AI_REPO_ROOTS = [
        # Bulbasaur (O-MBE)
        r"C:\Users\Lab10\AI_for_quantum",
        # Ch-MBE (Omicron chalcogenide MBE) — added 2026-07-21
        r"C:\Users\Omicron\AI_for_quantum",
        # AJ's Mac dev clone
        "/Users/aj/ai-for-quantum",
    ]

    @staticmethod
    def _default_ai_repo_root() -> "Path":
        """Resolve the AI_for_quantum repo location for this machine.

        Precedence:
          1. ``AI_REPO_ROOT`` env var — per-machine escape hatch
          2. First existing dir from ``_KNOWN_AI_REPO_ROOTS``
          3. First entry as fallback (error message will point at a
             concrete path we tried)
        """
        import os
        env = os.environ.get("AI_REPO_ROOT")
        if env:
            return Path(env)
        for candidate in EventsTab._KNOWN_AI_REPO_ROOTS:
            if Path(candidate).exists():
                return Path(candidate)
        return Path(EventsTab._KNOWN_AI_REPO_ROOTS[0])

    def _get_classifier(self):
        """Lazy-load ClassifierBridge on first call. Cache for subsequent calls.

        Returns None and surfaces a QMessageBox if loading fails (torch missing,
        repo not cloned, model missing, etc.). Subsequent calls short-circuit
        without retrying — clear cached_error to retry after fixing.
        """
        if self._classifier is not None:
            return self._classifier
        if self._classifier_load_error is not None:
            return None
        try:
            from gui.classifier_bridge import ClassifierBridge
            repo_root = self._default_ai_repo_root()
            if not repo_root.exists():
                raise FileNotFoundError(
                    f"AI_for_quantum repo not found at {repo_root}. "
                    f"Clone it or set AI_REPO_ROOT env var."
                )
            self._classifier = ClassifierBridge(repo_root)
            return self._classifier
        except Exception as e:
            self._classifier_load_error = str(e)
            QMessageBox.critical(
                self, "Classifier load failed",
                f"Failed to load Classifier2.\n\n{e}\n\n"
                f"On Bulbasaur, this typically means torch isn't installed "
                f"or the AI_for_quantum repo isn't cloned. See the lab "
                f"setup notes."
            )
            return None

    def _on_classify_clicked(self) -> None:
        """Classify the currently displayed buffer frame.

        Reads the PNG at the current slider position, hands the raw pixels
        to ClassifierBridge, and renders the breakdown inline. Disables the
        button while inferring (single-threaded; classifier is fast enough
        on CPU that the brief UI freeze is acceptable for an MVP).
        """
        if self._blind_labeling_mode:
            return
        if not self._cached_paths:
            self._classifier_result_label.setText(
                "<span style='color: #c33;'>no frame loaded</span>"
            )
            return
        idx = self._slider.value()
        if not (0 <= idx < len(self._cached_paths)):
            return
        frame_path = self._cached_paths[idx]
        # Exposure is persisted only if a classifier result is actually shown.

        classifier = self._get_classifier()
        if classifier is None:
            self._classifier_result_label.setText(
                "<span style='color: #c33;'>classifier unavailable</span>"
            )
            return

        self._classify_button.setEnabled(False)
        self._classifier_result_label.setText("<i>classifying…</i>")
        try:
            from PIL import Image
            arr = np.asarray(Image.open(frame_path).convert("RGB"))
            result = classifier.classify(arr)
            if not self.record_classifier_output_visible(frame_path):
                raise OSError(
                    "classifier result withheld because exposure provenance "
                    "could not be persisted"
                )
            self._render_classifier_result(result)
        except Exception as e:
            self._classifier_result_label.setText(
                f"<span style='color: #c33;'>error: {e}</span>"
            )
        finally:
            self._classify_button.setEnabled(not self._blind_labeling_mode)

    # --- Experimental: Equalizer integration -------------------------------

    def _label_calibration_id_for_frame(
        self, event_idx: int, frame_path: Path,
    ) -> str:
        """Return the latest label calibration only when it names this frame."""
        row = self._labels_cache.get(event_idx) or {}
        calibration_id = str(row.get("calibration_id", "") or "").strip()
        recorded_path = str(row.get("frame_path", "") or "").strip()
        if not calibration_id or not recorded_path:
            return ""
        candidate = Path(recorded_path)
        if not candidate.is_absolute() and self._session_dir is not None:
            candidate = self._session_dir / candidate
        try:
            same_frame = candidate.resolve() == frame_path.resolve()
        except OSError:
            same_frame = candidate.absolute() == frame_path.absolute()
        return calibration_id if same_frame else ""

    def _on_open_equalizer(self) -> None:
        """Open the selected historical frame in the shared Equalizer panel."""
        if self._blind_labeling_mode:
            return
        if not self._cached_paths:
            return
        idx = self._slider.value()
        if not (0 <= idx < len(self._cached_paths)):
            return
        frame_path = self._cached_paths[idx]
        # Exposure is persisted only after the Equalizer window is ready.
        event_idx = self._currently_displayed_event_idx
        if event_idx is None:
            return
        logger = self._growth_logger
        if logger is None or self._session_dir is None:
            QMessageBox.warning(
                self, "Equalizer unavailable",
                "Attach the event to its growth session before labeling.",
            )
            return

        metadata = self._capture_metadata_by_filename.get(frame_path.name)
        if metadata is None:
            reason = self._capture_manifest_error or (
                f"capture_manifest.csv has no row for {frame_path.name}."
            )
            QMessageBox.warning(
                self, "Equalizer provenance required",
                f"This historical frame cannot be labeled with the Equalizer.\n\n{reason}\n\n"
                "The ordinary reconstruction dropdown and notes remain available.",
            )
            return
        if metadata["gun_aligned"] is not True:
            QMessageBox.warning(
                self, "Equalizer blocked",
                "The selected frame was not recorded with gun_aligned=true.",
            )
            return
        if metadata["realignment_active"] is not False:
            QMessageBox.warning(
                self, "Equalizer blocked",
                "The selected frame was captured during gun realignment.",
            )
            return

        try:
            from PIL import Image
            from gui.live_equalizer_tab import LiveEqualizerTab

            with Image.open(frame_path) as image:
                rgb = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()
        except Exception as exc:
            QMessageBox.critical(
                self, "Equalizer unavailable",
                f"The shared Equalizer or selected frame could not be loaded:\n\n{exc}",
            )
            return
        if (
            rgb.shape[1] != metadata["camera_width"]
            or rgb.shape[0] != metadata["camera_height"]
        ):
            QMessageBox.warning(
                self, "Equalizer provenance mismatch",
                "The saved image dimensions do not match capture_manifest.csv.",
            )
            return

        if self._equalizer_window is not None:
            self._equalizer_window.close()
            self._equalizer_window = None

        window = QWidget(self, Qt.WindowType.Window)
        window.setWindowTitle(f"Event #{event_idx} Equalizer - {frame_path.name}")
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        window.resize(1180, 820)
        layout = QVBoxLayout(window)
        layout.setContentsMargins(8, 8, 8, 8)
        provenance_label = QLabel()
        provenance_label.setWordWrap(True)
        provenance_label.setStyleSheet("padding: 4px; color: #ddd;")
        layout.addWidget(provenance_label)

        try:
            panel = LiveEqualizerTab(window, retrospective=True)
        except Exception as exc:
            window.close()
            QMessageBox.critical(
                self, "Equalizer unavailable",
                f"The shared Equalizer could not be initialized:\n\n{exc}",
            )
            return
        layout.addWidget(panel, 1)
        window._equalizer_panel = panel
        window._equalizer_frame_path = frame_path
        window._equalizer_metadata = dict(metadata)
        window._equalizer_provenance_label = provenance_label

        try:
            panel.set_session_id(metadata["session_id"])
            panel.update_qc_context(
                session_active=True,
                view_segment_id=metadata["view_segment_id"],
                visual_history_generation=metadata["visual_history_generation"],
                gun_aligned=metadata["gun_aligned"],
                realignment_active=metadata["realignment_active"],
            )
            panel.set_save_enabled(True)
            panel.update_camera_frame(rgb, metadata)
            panel_metadata = panel.get_current_capture_metadata()
            if panel_metadata.get("basis_bundle_id") != metadata["basis_bundle_id"]:
                raise ValueError(
                    "the current canonical basis bundle does not match the manifest"
                )
            if (
                panel_metadata.get("capture_geometry_id")
                != metadata["capture_geometry_id"]
            ):
                raise ValueError(
                    "the panel capture geometry does not match the manifest"
                )
        except Exception as exc:
            window.close()
            QMessageBox.critical(
                self, "Equalizer unavailable",
                f"The historical frame context could not be initialized:\n\n{exc}",
            )
            return

        panel.calibration_accept_requested.connect(
            lambda pending: self._request_retrospective_calibration_acceptance(
                panel=panel,
                provenance_label=provenance_label,
                pending=pending,
            )
        )
        panel.calibration_invalidation_requested.connect(
            lambda reason: self._request_retrospective_calibration_invalidation(
                panel=panel,
                provenance_label=provenance_label,
                reason=reason,
            )
        )
        panel.live_label_save_requested.connect(
            lambda payload: self._save_retrospective_equalizer_label(
                panel=panel,
                provenance_label=provenance_label,
                logger=logger,
                event_idx=event_idx,
                frame_path=frame_path,
                payload=payload,
            )
        )

        window._equalizer_active_calibration = None
        self._equalizer_window = window

        def _forget_window(*_args) -> None:
            if self._equalizer_window is window:
                self._equalizer_window = None
            context = self._pending_retrospective_accept_context
            if context is not None and context.get("panel") is panel:
                self._pending_retrospective_accept_context = None
            invalidation = self._pending_retrospective_invalidation
            if invalidation is not None and invalidation.get("panel") is panel:
                self._pending_retrospective_invalidation = None
            resolution = self._pending_retrospective_resolve_context
            if resolution is not None and resolution.get("panel") is panel:
                self._pending_retrospective_resolve_context = None

        window.destroyed.connect(_forget_window)
        if not self.record_equalizer_output_visible(frame_path):
            window.close()
            self._equalizer_window = None
            QMessageBox.warning(
                self, "Equalizer withheld",
                "The Equalizer was not shown because its exposure could not "
                "be persisted in the labeling audit state.",
            )
            return
        window.show()
        window.raise_()
        window.activateWindow()

        label_calibration_id = self._label_calibration_id_for_frame(
            event_idx, frame_path,
        )
        calibration_ids: list[str] = []
        # The exact-frame label wins over the capture manifest.  GrowthApp
        # resolves this ordered list and owns the resulting retrospective map.
        for candidate_id in (label_calibration_id, metadata["calibration_id"]):
            if candidate_id and candidate_id not in calibration_ids:
                calibration_ids.append(candidate_id)
        if not calibration_ids:
            provenance_label.setText(
                "No calibration ID is bound to this frame. Calibrate and accept "
                "this exact frozen frame before saving mixture weights."
            )
            return

        snapshot = panel.get_current_snapshot()
        basis_bundle = panel.get_basis_bundle()
        if snapshot is None or basis_bundle is None:
            provenance_label.setText(
                "Historical calibration reuse is blocked because the frozen "
                "frame snapshot or canonical basis bundle is unavailable."
            )
            return
        request_token = str(uuid.uuid4())
        context = {
            "request_token": request_token,
            "calibration_ids": tuple(calibration_ids),
            "snapshot": snapshot,
            "basis_bundle": basis_bundle,
            "panel": panel,
            "retrospective": True,
        }
        self._pending_retrospective_resolve_context = context
        provenance_label.setText(
            "Waiting for GrowthApp to resolve a compatible journaled "
            "calibration for this historical frame."
        )
        self.retrospective_calibration_resolve_requested.emit({
            "request_token": request_token,
            "calibration_ids": tuple(calibration_ids),
            "snapshot": snapshot,
            "basis_bundle_id": metadata["basis_bundle_id"],
            "basis_bundle": basis_bundle,
            "retrospective": True,
        })

    def complete_retrospective_calibration_resolution(
        self,
        request_token: str,
        calibration,
        reason: str = "",
    ) -> bool:
        """Activate only the record selected by app-owned historical lookup."""
        context = self._pending_retrospective_resolve_context
        if context is None or context.get("request_token") != str(request_token):
            return False
        panel = context["panel"]
        window = self._equalizer_window
        if window is None or getattr(window, "_equalizer_panel", None) is not panel:
            return False
        label = getattr(window, "_equalizer_provenance_label", None)
        if calibration is None:
            self._pending_retrospective_resolve_context = None
            if isinstance(label, QLabel):
                label.setText(
                    str(reason or "No compatible journaled calibration is available. ")
                    + " Calibrate and accept this exact frozen frame before saving."
                )
            return True
        if getattr(calibration, "calibration_id", None) not in context["calibration_ids"]:
            return False
        if not panel.set_accepted_calibration(calibration):
            return False
        self._pending_retrospective_resolve_context = None
        window._equalizer_active_calibration = calibration
        if isinstance(label, QLabel):
            label.setText(
                f"Reusing app-resolved calibration {calibration.calibration_id}. "
                "Save remains bound to this exact historical frame."
            )
        return True

    def fail_retrospective_calibration_resolution(
        self, request_token: str, reason: str,
    ) -> bool:
        """Reject a malformed/untrusted resolution without activating state."""
        context = self._pending_retrospective_resolve_context
        if context is None or context.get("request_token") != str(request_token):
            return False
        self._pending_retrospective_resolve_context = None
        QMessageBox.critical(
            self,
            "Historical calibration unavailable",
            str(reason or "GrowthApp rejected the calibration lookup request."),
        )
        return True

    def _request_retrospective_calibration_acceptance(
        self,
        *,
        panel,
        provenance_label: QLabel,
        pending,
    ) -> None:
        """Send app-owned acceptance all evidence needed for validation."""
        window = self._equalizer_window
        if window is None or getattr(window, "_equalizer_panel", None) is not panel:
            QMessageBox.warning(
                self, "Calibration blocked",
                "The retrospective Equalizer window is no longer active.",
            )
            return
        if self._pending_retrospective_accept_context is not None:
            QMessageBox.warning(
                self, "Calibration pending",
                "A retrospective calibration request is already awaiting the app.",
            )
            return
        try:
            snapshot = panel.get_calibration_snapshot()
            basis_bundle = panel.get_basis_bundle()
            evidence_getter = getattr(
                panel,
                "get_orientation_evidence_kind",
                getattr(panel, "get_handedness_evidence_kind", None),
            )
            if evidence_getter is None:
                raise AttributeError("handedness evidence getter is unavailable")
            evidence_kind = str(evidence_getter() or "")
        except (AttributeError, TypeError, ValueError) as exc:
            QMessageBox.warning(
                self, "Calibration blocked",
                f"Calibration evidence is unavailable: {exc}",
            )
            return
        if snapshot is None or basis_bundle is None or not evidence_kind:
            QMessageBox.warning(
                self, "Calibration blocked",
                "Confirm asymmetric handedness evidence on the frozen frame first.",
            )
            return

        request_token = str(uuid.uuid4())
        context = {
            "request_token": request_token,
            "pending": pending,
            "snapshot": snapshot,
            "basis_bundle": basis_bundle,
            "evidence_kind": evidence_kind,
            "evidence_confirmed": True,
            "panel": panel,
            "retrospective": True,
        }
        self._pending_retrospective_accept_context = context
        provenance_label.setText(
            "Acceptance requested; waiting for GrowthApp to validate evidence "
            "and durably journal the calibration."
        )
        self.retrospective_calibration_accept_requested.emit(dict(context))

    def complete_retrospective_calibration_acceptance(
        self, request_token: str, accepted,
    ) -> bool:
        """Activate a journaled record; False requires app-side invalidation."""
        context = self._pending_retrospective_accept_context
        if context is None or context.get("request_token") != str(request_token):
            return False
        panel = context["panel"]
        pending = context["pending"]
        window = self._equalizer_window
        if (
            window is None
            or getattr(window, "_equalizer_panel", None) is not panel
            or getattr(accepted, "calibration_id", None)
            != getattr(pending, "calibration_id", None)
        ):
            return False
        if not panel.set_accepted_calibration(accepted):
            return False
        window._equalizer_active_calibration = accepted
        self._pending_retrospective_accept_context = None
        label = getattr(window, "_equalizer_provenance_label", None)
        if isinstance(label, QLabel):
            label.setText(
                f"Accepted and journaled calibration {accepted.calibration_id}. "
                "Mixture labels may now be saved for this historical frame."
            )
        return True

    def fail_retrospective_calibration_acceptance(
        self, request_token: str, reason: str,
    ) -> bool:
        """App callback: reject an unjournaled request and clear panel state."""
        context = self._pending_retrospective_accept_context
        if context is None or context.get("request_token") != str(request_token):
            return False
        self._pending_retrospective_accept_context = None
        panel = context["panel"]
        panel.invalidate_calibration(
            str(reason or "retrospective calibration acceptance failed"),
            emit=False,
        )
        QMessageBox.critical(
            self, "Calibration not accepted",
            str(reason or "GrowthApp rejected the calibration request."),
        )
        return True

    def _request_retrospective_calibration_invalidation(
        self,
        *,
        panel,
        provenance_label: QLabel,
        reason: str,
    ) -> None:
        """Ask GrowthApp to journal removal of the popup's active record."""
        window = self._equalizer_window
        if window is None or getattr(window, "_equalizer_panel", None) is not panel:
            return
        calibration = getattr(window, "_equalizer_active_calibration", None)
        if calibration is None:
            return
        if self._pending_retrospective_invalidation is not None:
            return
        request_token = str(uuid.uuid4())
        context = {
            "request_token": request_token,
            "calibration": calibration,
            "reason": str(reason or "retrospective calibration invalidated"),
            "panel": panel,
            "retrospective": True,
        }
        window._equalizer_active_calibration = None
        self._pending_retrospective_invalidation = context
        provenance_label.setText(
            "Calibration cleared locally; waiting for GrowthApp to journal "
            "the invalidation."
        )
        self.retrospective_calibration_invalidation_requested.emit(dict(context))

    def complete_retrospective_calibration_invalidation(
        self, request_token: str,
    ) -> bool:
        context = self._pending_retrospective_invalidation
        if context is None or context.get("request_token") != str(request_token):
            return False
        self._pending_retrospective_invalidation = None
        window = self._equalizer_window
        if window is not None and getattr(window, "_equalizer_panel", None) is context["panel"]:
            label = getattr(window, "_equalizer_provenance_label", None)
            if isinstance(label, QLabel):
                label.setText(
                    "Calibration invalidation journaled. Recalibrate this frozen "
                    "frame before saving."
                )
        return True

    def fail_retrospective_calibration_invalidation(
        self, request_token: str, reason: str,
    ) -> bool:
        context = self._pending_retrospective_invalidation
        if context is None or context.get("request_token") != str(request_token):
            return False
        self._pending_retrospective_invalidation = None
        QMessageBox.critical(
            self, "Calibration invalidation failed",
            str(reason or "GrowthApp could not update the calibration journal."),
        )
        return True

    def _save_retrospective_equalizer_label(
        self,
        *,
        panel,
        provenance_label: QLabel,
        logger: GrowthLogger,
        event_idx: int,
        frame_path: Path,
        payload: dict,
    ) -> None:
        """Save four active weights with the exact frame/calibration DTOs."""
        if logger is not self._growth_logger:
            QMessageBox.warning(
                self, "Equalizer save blocked",
                "The growth session changed while the Equalizer was open.",
            )
            return
        calibration = panel.get_calibration()
        snapshot = panel.get_current_snapshot()
        window = self._equalizer_window
        app_activated = (
            window is not None
            and getattr(window, "_equalizer_panel", None) is panel
            and getattr(window, "_equalizer_active_calibration", None) is calibration
        )
        if calibration is None or snapshot is None or not app_activated:
            QMessageBox.warning(
                self, "Equalizer save blocked",
                "An app-activated calibration and the frozen historical frame "
                "are required.",
            )
            return

        weights = payload.get("final_weights") if isinstance(payload, dict) else None
        if not isinstance(weights, dict):
            QMessageBox.warning(
                self, "Equalizer save blocked", "Invalid Equalizer payload.",
            )
            return
        columns = {
            "1x1": "recon_1x1",
            "Tw(2x1)": "recon_tw",
            "c(6x2)": "recon_c6x2",
            "RT13": "recon_rt13",
        }
        values: dict[str, float] = {}
        try:
            for label, column in columns.items():
                value = float(weights[label])
                if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                    raise ValueError(f"{label} weight is outside [0, 1]")
                values[column] = value
        except (KeyError, TypeError, ValueError) as exc:
            QMessageBox.warning(
                self, "Equalizer save blocked", f"Invalid mixture weights: {exc}",
            )
            return

        try:
            saved = logger.update_event_label(
                event_idx,
                **values,
                recon_HTR=None,
                calibration=calibration,
                snapshot=snapshot,
                frame_path=str(frame_path),
                equalizer_payload=payload,
                equalizer_labeler=self._labeler,
            )
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.critical(
                self, "Equalizer save failed", f"The label was not written:\n\n{exc}",
            )
            return
        if not saved:
            QMessageBox.critical(
                self, "Equalizer save failed",
                "The label writer rejected the retrospective record.",
            )
            return

        refreshed = logger.read_event_labels().get(event_idx)
        if refreshed is not None:
            self._labels_cache[event_idx] = refreshed
        else:
            cached = self._labels_cache.setdefault(
                event_idx,
                {field: "" for field in GrowthLogger.EVENT_LABEL_FIELDS},
            )
            cached.update({key: f"{value:.4f}" for key, value in values.items()})
            cached.update({
                "event_idx": str(event_idx),
                "equalizer_argmax": str(payload.get("argmax", "")),
                "recon_HTR": "",
                "calibration_id": calibration.calibration_id,
                "frame_path": str(frame_path),
            })
        if self._currently_displayed_event_idx == event_idx:
            self._populate_label_form(event_idx)
        self._refresh_unreviewed_badge()
        provenance_label.setText(
            f"Saved event #{event_idx} with calibration "
            f"{calibration.calibration_id}; Equalizer argmax is diagnostic only "
            "and HTR remains unavailable."
        )

    def _render_classifier_result(self, result: dict) -> None:
        """Format classifier output as a compact inline label."""
        scores = result.get("classification_scores", {}) or {}
        predicted = result.get("predicted_class", "?")
        is_bad = result.get("is_bad", False)
        bad_conf = result.get("bad_confidence", 0.0)

        # Sort scores descending; show all with the predicted class bolded.
        items = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        parts = []
        for label, v in items:
            pct = f"{v * 100:.1f}%"
            if label == predicted:
                parts.append(f"<b>{label} {pct}</b>")
            else:
                parts.append(f"{label} {pct}")
        breakdown = " · ".join(parts)

        if is_bad:
            prefix = (
                f"<span style='color: #c33;'>⚠ low-quality "
                f"({bad_conf:.0%})</span>  "
            )
        else:
            prefix = ""
        self._classifier_result_label.setText(prefix + breakdown)
        self._classifier_result_label.setStyleSheet("color: #ddd;")

        # Tooltip shows the raw dict for debugging.
        quality = result.get("quality")
        tip_lines = [f"Predicted: {predicted}"]
        for label, v in items:
            tip_lines.append(f"  {label}: {v:.4f}")
        if quality is not None:
            tip_lines.append(f"Quality: {quality:.3f}")
        if is_bad:
            tip_lines.append(f"Low-quality flag: True ({bad_conf:.1%})")
        self._classifier_result_label.setToolTip("\n".join(tip_lines))
