"""Point-event review UI for live and completed RHEED sessions.

The acquisition CSV files and saved frames are immutable evidence.  This tab
edits only the append-only ``rheed-point-events-v3`` review journal.  A point
event can contain several small, explicit human labels:

* a reconstruction appeared or disappeared; and
* pattern clarity became Good or Bad.

The point controls save immediately.  Anchors are selected separately for the
derived stable state interval after a point, rather than being treated as an
event label.

Automatic image-change detections are proposals.  A grower must explicitly
confirm or reject each proposal.  The source time never moves; moving the
review point only selects another real saved frame.  Model-derived visual
fitting is deliberately absent from this labeling surface.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional
import uuid

import numpy as np
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.growth_logger import GrowthLogger
from gui.rheed_point_events import (
    PointEventCompletionError,
    PointEventError,
    V2_SCHEMA_ID,
    make_review_anchor,
)
from gui.widgets import ScalingImageLabel


RECONSTRUCTION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("1x1", "one_by_one"),
    ("Twinned (2x1)", "twinned_two_by_one"),
    ("c(6x2)", "c_six_by_two"),
    ("RT13", "rt13"),
    ("HTR", "htr"),
)
RECONSTRUCTION_DISPLAY = dict((value, label) for label, value in RECONSTRUCTION_OPTIONS)
RECON_LABEL_OPTIONS = [label for label, _value in RECONSTRUCTION_OPTIONS]
RECON_UNLABELED = ""

CLARITY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Good", "good"),
    ("Bad", "bad"),
)
CLARITY_DISPLAY = dict((value, label) for label, value in CLARITY_OPTIONS)

COL_EVENT_IDX = 0  # Historical public name; the cell now holds a stable event ID.
COL_EVENT_ID = COL_EVENT_IDX
COL_TIME = 1
COL_SOURCE = 2
COL_LABELS = 3
COL_DECISION = 4
COL_STATE = 5
COLUMN_HEADERS = ["Event", "Time", "Source", "Labels", "Candidate", "Review"]

_REFERENCE_EVENT_NAMES = {
    "session_start": "Session start",
    "alignment_confirmed": "RHEED alignment confirmed",
    "realign_start": "RHEED realignment started",
    "realign_end": "RHEED realignment finished",
    "history_reset": "Image history reset",
    "history_ready": "Image history ready",
    "qc_reject": "Image not analyzable",
    "qc_pass": "Image analyzable again",
    "sample_direction_adjusted": "Sample direction adjusted",
    "rheed_current_adjusted": "RHEED current adjusted",
    "rheed_energy_adjusted": "RHEED energy adjusted",
}


def _format_time(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return text[-8:] if len(text) >= 8 else text


def _as_optional_float(value: object) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _frame_elapsed_from_capture(
    metadata: Mapping[str, Any], source: Mapping[str, Any],
) -> Optional[float]:
    """Resolve one saved frame's session time without interpolation.

    Current manifests carry exact UTC capture times but older ones do not
    carry ``elapsed_s``.  Their elapsed position is still recoverable from
    the immutable event UTC/elapsed pair and the frame's own UTC timestamp.
    """
    explicit = _as_optional_float(metadata.get("elapsed_s"))
    if explicit is not None:
        return explicit
    base_elapsed = _as_optional_float(source.get("original_elapsed_s"))
    captured_text = str(metadata.get("captured_at_utc") or "").strip()
    source_text = str(source.get("original_at_utc") or "").strip()
    if base_elapsed is None or not captured_text or not source_text:
        return base_elapsed
    try:
        captured = datetime.fromisoformat(captured_text.replace("Z", "+00:00"))
        source_at = datetime.fromisoformat(source_text.replace("Z", "+00:00"))
        if captured.tzinfo is None or source_at.tzinfo is None:
            return base_elapsed
        elapsed = base_elapsed + (captured - source_at).total_seconds()
    except ValueError:
        return base_elapsed
    return elapsed if elapsed >= 0 else base_elapsed


class EventsTab(QWidget):
    """Master/detail editor for append-only RHEED point-event reviews."""

    unreviewed_count_changed = pyqtSignal(int)
    blind_labeling_mode_changed = pyqtSignal(bool)
    review_anchor_moved = pyqtSignal(object)
    representative_anchor_moved = pyqtSignal(object)
    point_event_changed = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._growth_logger: Optional[GrowthLogger] = None
        self._point_store = None
        self._session_dir: Optional[Path] = None
        self._labeler = ""
        self._blind_labeling_mode = False
        self._blind_state_reason = "No labeling session is attached."

        self._event_states: dict[str, dict[str, Any]] = {}
        self._source_rows: dict[tuple[str, str], dict[str, str]] = {}
        self._reference_rows: dict[str, dict[str, str]] = {}
        self._currently_displayed_event_id: Optional[str] = None
        # Kept during the transition so external diagnostics do not confuse a
        # source-local index with the stable UUID.
        self._currently_displayed_event_idx: Optional[int] = None
        self._last_seen_event_idx = 0
        self._labels_cache: dict[str, dict[str, Any]] = {}

        self._cached_paths: list[Path] = []
        self._cached_pixmaps: list[QPixmap] = []
        self._capture_metadata_by_path: dict[str, dict[str, Any]] = {}
        self._capture_metadata_by_filename: dict[str, dict[str, Any]] = {}
        self._capture_manifest_error = "No event buffer is selected."
        self._populating_form = False

        self._review_save_timer = QTimer(self)
        self._review_save_timer.setSingleShot(True)
        self._review_save_timer.setInterval(350)
        self._review_save_timer.timeout.connect(self._flush_review_fields)

        self._reload_timer = QTimer(self)
        self._reload_timer.setInterval(1000)
        self._reload_timer.timeout.connect(self._reload_events)

        self._build_ui()
        self.events_table.itemSelectionChanged.connect(self._on_selection_changed)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_master_pane())
        splitter.addWidget(self._build_detail_pane())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([440, 680])
        layout.addWidget(splitter)

    def _build_master_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header_row = QHBoxLayout()
        title = QLabel("RHEED point events")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        header_row.addWidget(title)
        header_row.addStretch(1)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self._reload_events)
        header_row.addWidget(refresh_button)
        layout.addLayout(header_row)

        identity_row = QHBoxLayout()
        identity_row.addWidget(QLabel("Grower:"))
        self._labeler_input = QLineEdit()
        self._labeler_input.setPlaceholderText("Enter once for this session")
        self._labeler_input.editingFinished.connect(self._apply_session_labeler)
        identity_row.addWidget(self._labeler_input, 1)
        self._autosave_status = QLabel("Changes save automatically")
        self._autosave_status.setStyleSheet("color: #16a34a; font-size: 10px;")
        identity_row.addWidget(self._autosave_status)
        layout.addLayout(identity_row)

        hint = QLabel(
            "Add or move point changes at exact saved frames. The complete state "
            "between points is derived automatically; hardware-log rows are read-only."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(hint)

        self.events_table = QTableWidget(0, len(COLUMN_HEADERS))
        self.events_table.setHorizontalHeaderLabels(COLUMN_HEADERS)
        self.events_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.events_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.events_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.events_table.verticalHeader().setVisible(False)
        # Stable IDs remain attached to the hidden first cell for selection and
        # audit.  Growers do not have to name events or interpret UUIDs.
        self.events_table.setColumnHidden(COL_EVENT_ID, True)
        header = self.events_table.horizontalHeader()
        for column in (COL_EVENT_ID, COL_TIME, COL_SOURCE, COL_DECISION, COL_STATE):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LABELS, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.events_table)
        return pane

    def _build_detail_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._detail_placeholder = QLabel("Select an event to review it.")
        self._detail_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail_placeholder.setWordWrap(True)
        self._detail_placeholder.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(self._detail_placeholder, 1)

        self._detail_content = QWidget()
        content = QVBoxLayout(self._detail_content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(6)

        self._metadata_label = QLabel()
        self._metadata_label.setWordWrap(True)
        self._metadata_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        content.addWidget(self._metadata_label)

        self._image_label = ScalingImageLabel(minimum_size=(320, 210))
        content.addWidget(self._image_label, 1)

        slider_row = QHBoxLayout()
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.valueChanged.connect(self._display_frame_at)
        slider_row.addWidget(self._slider, 1)
        self._frame_position_label = QLabel("0 / 0")
        self._frame_position_label.setMinimumWidth(70)
        slider_row.addWidget(self._frame_position_label)
        content.addLayout(slider_row)

        anchor_row = QHBoxLayout()
        self._move_review_button = QPushButton("Move review point to shown frame")
        self._move_review_button.setToolTip(
            "The immutable source time stays unchanged. This moves only the review point "
            "to the real saved frame shown above."
        )
        self._move_review_button.clicked.connect(self._move_review_to_current_frame)
        anchor_row.addWidget(self._move_review_button)
        self._representative_button = QPushButton(
            "Use shown frame as representative of this interval"
        )
        self._representative_button.setToolTip(
            "This does not label the event. It chooses the clearest characteristic "
            "image inside the stable state interval after the selected change point."
        )
        self._representative_button.clicked.connect(self._set_representative_anchor)
        anchor_row.addStretch(1)
        content.addLayout(anchor_row)

        self._reference_notice = QLabel()
        self._reference_notice.setWordWrap(True)
        self._reference_notice.setStyleSheet(
            "padding: 8px; background: #263238; color: #ddd; border-radius: 4px;"
        )
        self._reference_notice.hide()
        content.addWidget(self._reference_notice)

        self._label_box = self._build_label_form()
        content.addWidget(self._label_box)

        self._interval_box = QGroupBox("Representative image for derived interval")
        interval_layout = QVBoxLayout(self._interval_box)
        self._interval_explanation = QLabel()
        self._interval_explanation.setWordWrap(True)
        interval_layout.addWidget(self._interval_explanation)
        self._representative_position_label = QLabel("No representative selected yet")
        self._representative_position_label.setWordWrap(True)
        interval_layout.addWidget(self._representative_position_label)
        interval_layout.addWidget(self._representative_button)
        content.addWidget(self._interval_box)

        self._detail_content.hide()
        layout.addWidget(self._detail_content, 1)
        return pane

    def _build_label_form(self) -> QGroupBox:
        box = QGroupBox("Changes at this exact frame")
        form = QFormLayout(box)
        form.setContentsMargins(10, 16, 10, 8)
        form.setSpacing(6)

        self._source_position_label = QLabel("—")
        self._source_position_label.setWordWrap(True)
        form.addRow("Original event (fixed):", self._source_position_label)
        self._review_position_label = QLabel("—")
        self._review_position_label.setWordWrap(True)
        form.addRow("Review point:", self._review_position_label)
        blind_row = QHBoxLayout()
        self._blind_mode_button = QPushButton("Enter blind human review")
        self._blind_mode_button.setCheckable(True)
        self._blind_mode_button.setToolTip(
            "Hide model-derived answers while assigning human labels. Exposure "
            "provenance remains append-only."
        )
        self._blind_mode_button.toggled.connect(self._on_blind_labeling_toggled)
        blind_row.addWidget(self._blind_mode_button)
        self._blind_mode_status = QLabel("assisted / not blind")
        self._blind_mode_status.setWordWrap(True)
        blind_row.addWidget(self._blind_mode_status, 1)
        form.addRow("Review provenance:", blind_row)

        initial_state = QLabel(
            "Start of session is fixed as 1x1 with unknown pattern clarity. "
            "This is an auditable baseline, not an appearance event."
        )
        initial_state.setWordWrap(True)
        initial_state.setStyleSheet("color: #888; font-size: 10px;")
        self._initial_state_notice = initial_state
        form.addRow("Starting state:", initial_state)

        reconstruction_widget = QWidget()
        reconstruction_layout = QFormLayout(reconstruction_widget)
        reconstruction_layout.setContentsMargins(0, 0, 0, 0)
        reconstruction_layout.setSpacing(3)
        self._reconstruction_change_combos: dict[str, QComboBox] = {}
        for display, value in RECONSTRUCTION_OPTIONS:
            combo = QComboBox()
            combo.addItem("No change", "")
            combo.addItem("Appeared", "appeared")
            combo.addItem("Disappeared", "disappeared")
            combo.currentIndexChanged.connect(
                lambda _index, recon=value: self._on_reconstruction_change(recon)
            )
            self._reconstruction_change_combos[value] = combo
            reconstruction_layout.addRow(f"{display}:", combo)
        form.addRow("Reconstruction changes:", reconstruction_widget)

        self._clarity_combo = QComboBox()
        self._clarity_combo.addItem("No change", "")
        self._clarity_combo.addItem("Became Good (clear features)", "good")
        self._clarity_combo.addItem("Became Bad (unclear features)", "bad")
        self._clarity_combo.currentIndexChanged.connect(self._on_clarity_change)
        form.addRow("Pattern clarity:", self._clarity_combo)

        self._notes_input = QLineEdit()
        self._notes_input.setPlaceholderText("Optional note; not an event name")
        self._notes_input.textChanged.connect(self._queue_review_field_save)
        self._notes_input.editingFinished.connect(self._flush_review_fields)
        form.addRow("Comment:", self._notes_input)

        finish_row = QHBoxLayout()
        self._add_event_button = QPushButton("Add event at shown frame")
        self._add_event_button.clicked.connect(self._add_posthoc_event)
        finish_row.addWidget(self._add_event_button)
        finish_row.addStretch(1)
        form.addRow("Point:", finish_row)

        self._completion_help = QLabel("Every selection and note is saved automatically.")
        self._completion_help.setWordWrap(True)
        self._completion_help.setStyleSheet("color: #16a34a; font-size: 10px;")
        form.addRow("", self._completion_help)
        return box

    # ---------------------------------------------------------- provenance

    def _set_blind_button_checked(self, checked: bool) -> None:
        self._blind_mode_button.blockSignals(True)
        self._blind_mode_button.setChecked(bool(checked))
        self._blind_mode_button.blockSignals(False)

    def _apply_blind_mode_ui(
        self, active: bool, *, gold_eligible: bool, reason: str,
    ) -> None:
        self._blind_labeling_mode = bool(active)
        self._blind_state_reason = str(reason)
        self._set_blind_button_checked(active)
        self._blind_mode_button.setText(
            "Exit blind human review" if active else "Enter blind human review"
        )
        if active and gold_eligible:
            text, color = "blind gold eligible", "#16a34a"
        elif active:
            text, color = "outputs hidden; assisted provenance", "#d97706"
        else:
            text, color = "assisted / not blind", "#d97706"
        self._blind_mode_status.setText(text)
        self._blind_mode_status.setStyleSheet(f"font-size: 10px; color: {color};")
        self._blind_mode_status.setToolTip(reason)
        self.blind_labeling_mode_changed.emit(bool(active))

    @pyqtSlot(bool)
    def _on_blind_labeling_toggled(self, checked: bool) -> None:
        logger = self._growth_logger
        if logger is None or self._session_dir is None:
            self._set_blind_button_checked(False)
            QMessageBox.warning(
                self, "Blind review unavailable", "Attach a growth session first."
            )
            return
        reviewer = self._labeler_input.text().strip() or self._labeler
        if checked and not reviewer:
            self._set_blind_button_checked(False)
            QMessageBox.warning(
                self, "Reviewer required", "Enter the grower or reviewer name first."
            )
            return
        setter = getattr(logger, "set_human_blind_labeling_mode", None)
        try:
            status = setter(checked, labeler=reviewer) if callable(setter) else {
                "blind_mode_active": checked,
                "gold_eligible": False,
                "reason": "Exposure audit is unavailable for this legacy session.",
            }
        except (OSError, TypeError, ValueError) as exc:
            self._set_blind_button_checked(False)
            QMessageBox.warning(self, "Blind review unavailable", str(exc))
            return
        self._apply_blind_mode_ui(
            bool(status["blind_mode_active"]),
            gold_eligible=bool(status["gold_eligible"]),
            reason=str(status["reason"]),
        )

    def record_classifier_output_visible(self, frame_path: Optional[Path] = None) -> bool:
        """Persist model-output exposure from the separate live monitor."""
        return self._record_output_exposure("classifier", frame_path)

    def record_equalizer_output_visible(self, frame_path: Optional[Path] = None) -> bool:
        """Compatibility provenance hook for the separate legacy live tab.

        This method intentionally creates no widget, button, popup, or label
        dependency in the point-event editor.
        """
        return self._record_output_exposure("equalizer", frame_path)

    def _record_output_exposure(self, kind: str, frame_path: Optional[Path]) -> bool:
        logger = self._growth_logger
        if logger is None:
            return True
        writer = getattr(logger, "record_human_label_output_exposure", None)
        if not callable(writer):
            return True
        if not writer(kind, frame_path=frame_path):
            return False
        self._apply_blind_mode_ui(
            False,
            gold_eligible=False,
            reason=f"A {kind} output was displayed in this session.",
        )
        return True

    # ------------------------------------------------------------ lifecycle

    def attach_session(
        self, growth_logger: Optional[GrowthLogger], *, labeler: str = "",
    ) -> None:
        """Attach the active journal, preserving immutable acquisition logs."""
        self._flush_review_fields()
        self._reload_timer.stop()
        self._growth_logger = growth_logger
        self._point_store = (
            growth_logger.point_event_store if growth_logger is not None else None
        )
        self._session_dir = (
            growth_logger.session_dir
            if growth_logger is not None and growth_logger.session_dir is not None
            else None
        )
        self._labeler = str(labeler or "").strip()
        self._labeler_input.blockSignals(True)
        self._labeler_input.setText(self._labeler)
        self._labeler_input.blockSignals(False)
        self._event_states = {}
        self._labels_cache = {}
        self._source_rows = {}
        self._reference_rows = {}
        self._currently_displayed_event_id = None
        self._currently_displayed_event_idx = None
        self._last_seen_event_idx = 0
        self._clear_frame_cache()
        self.events_table.setRowCount(0)
        self._show_placeholder("Select an event to review it.")

        begin = getattr(growth_logger, "begin_human_labeling_review", None)
        if growth_logger is not None and self._session_dir is not None and callable(begin):
            status = begin(self._labeler)
        else:
            status = {
                "blind_mode_active": False,
                "gold_eligible": False,
                "reason": "No labeling session is attached.",
            }
        self._apply_blind_mode_ui(
            bool(status["blind_mode_active"]),
            gold_eligible=bool(status["gold_eligible"]),
            reason=str(status["reason"]),
        )
        self._reload_events()
        if self._session_dir is not None:
            self._reload_timer.start()

    def _apply_session_labeler(self) -> None:
        """Apply one grower identity to the whole attached review session."""
        value = self._labeler_input.text().strip()
        if not value:
            self._labeler_input.setText(self._labeler)
            return
        self._labeler = value
        begin = getattr(self._growth_logger, "begin_human_labeling_review", None)
        if callable(begin):
            try:
                status = begin(value)
            except (OSError, TypeError, ValueError) as exc:
                QMessageBox.warning(self, "Grower name could not be saved", str(exc))
                return
            self._apply_blind_mode_ui(
                bool(status["blind_mode_active"]),
                gold_eligible=bool(status["gold_eligible"]),
                reason=str(status["reason"]),
            )
        self._autosave_status.setText(f"Auto-saving as {value}")

    @pyqtSlot(np.ndarray, float)
    def on_frame_captured(self, frame: np.ndarray, score: float) -> None:  # noqa: ARG002
        self._reload_events()

    @pyqtSlot(int, str, str)
    def on_decision_made(
        self, event_idx: int, buffer_dir: str, state: str,  # noqa: ARG002
    ) -> None:
        # GrowthApp has already persisted both legacy storage evidence and any
        # explicit Confirm/Reject revision for this stable event ID.
        self._reload_events()

    # -------------------------------------------------------------- loading

    def _read_csv(self, name: str) -> list[dict[str, str]]:
        if self._session_dir is None:
            return []
        path = self._session_dir / name
        if not path.is_file():
            return []
        try:
            with open(path, "r", newline="", encoding="utf-8-sig") as stream:
                return [dict(row) for row in csv.DictReader(stream)]
        except OSError:
            return []

    def _load_source_rows(self) -> None:
        rows: dict[tuple[str, str], dict[str, str]] = {}
        for name in ("auto_capture_events.csv", "manual_events.csv"):
            for row in self._read_csv(name):
                index = str(row.get("event_idx") or "")
                if index:
                    rows[(name, index)] = row
        self._source_rows = rows

    def _load_reference_rows(self) -> None:
        result: dict[str, dict[str, str]] = {}
        for ordinal, row in enumerate(self._read_csv("rheed_view_events.csv"), start=1):
            event_type = str(row.get("event_type") or "").strip()
            if not event_type:
                continue
            source_index = str(row.get("event_idx") or ordinal)
            key = f"reference:{source_index}:{event_type}"
            result[key] = row
        self._reference_rows = result

    def _reload_events(self) -> None:
        # Do not rebuild the detail form underneath continuous typing. The
        # 500 ms idle save will finish first and the next 1 Hz poll catches
        # any newly arrived live event without moving the text cursor.
        if (
            self._review_save_timer.isActive()
            and self._notes_input.hasFocus()
        ):
            return
        selected = self._selected_record_key()
        self._load_source_rows()
        self._load_reference_rows()
        self._event_states = (
            self._point_store.states if self._point_store is not None else {}
        )
        self._labels_cache = {
            event_id: dict(state.get("review") or {})
            for event_id, state in self._event_states.items()
        }
        auto_indices = [
            int(key[1]) for key in self._source_rows
            if key[0] == "auto_capture_events.csv" and key[1].isdigit()
        ]
        self._last_seen_event_idx = max(auto_indices, default=0)
        self._refresh_table(selected_key=selected)

    def _source_row_for_state(self, state: Mapping[str, Any]) -> dict[str, str]:
        source = state.get("source") or {}
        name = Path(str(source.get("source_file") or "")).name
        index = str(source.get("source_index") or "")
        return dict(self._source_rows.get((name, index), {}))

    def _selected_record_key(self) -> Optional[str]:
        row = self.events_table.currentRow()
        if row < 0:
            return None
        item = self.events_table.item(row, COL_EVENT_ID)
        payload = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return str(payload.get("key")) if isinstance(payload, Mapping) else None

    def _refresh_table(
        self, _checked: bool = False, *, selected_key: Optional[str] = None,
    ) -> None:
        if selected_key is None:
            selected_key = self._selected_record_key()
        records: list[dict[str, Any]] = []
        for event_id, state in self._event_states.items():
            review = state.get("review") or {}
            if review.get("disposition") != "active":
                continue
            source = state.get("source") or {}
            # Automatic image-change proposals are intentionally absent from
            # this temporary manual-only workflow. Acquisition evidence is
            # preserved on disk; it simply does not create a labeling task.
            if source.get("kind") == "auto_capture":
                continue
            elapsed = _as_optional_float(source.get("original_elapsed_s"))
            records.append({
                "key": event_id,
                "kind": "point",
                "event_id": event_id,
                "state": state,
                "sort_elapsed": -1.0 if elapsed is None else elapsed,
                "sort_time": str(source.get("original_at_utc") or ""),
            })
        for key, row in self._reference_rows.items():
            elapsed = _as_optional_float(row.get("elapsed_s"))
            records.append({
                "key": key,
                "kind": "reference",
                "row": row,
                "sort_elapsed": -1.0 if elapsed is None else elapsed,
                "sort_time": str(row.get("timestamp") or ""),
            })
        records.sort(
            key=lambda item: (item["sort_elapsed"], item["sort_time"], item["key"]),
            reverse=True,
        )

        self.events_table.blockSignals(True)
        self.events_table.setRowCount(0)
        selected_row = -1
        for row_index, record in enumerate(records):
            self.events_table.insertRow(row_index)
            values = self._table_values(record)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == COL_EVENT_ID:
                    item.setData(Qt.ItemDataRole.UserRole, record)
                    item.setToolTip(record["key"])
                self.events_table.setItem(row_index, column, item)
            if record["key"] == selected_key:
                selected_row = row_index
        self.events_table.blockSignals(False)
        if selected_row >= 0:
            self.events_table.selectRow(selected_row)
        elif selected_key is not None:
            self._show_placeholder("Select an event to review it.")
            self._currently_displayed_event_id = None
        self._refresh_unreviewed_badge()

    def _table_values(self, record: Mapping[str, Any]) -> list[str]:
        if record["kind"] == "reference":
            row = record["row"]
            event_type = str(row.get("event_type") or "reference")
            return [
                f"ref #{row.get('event_idx') or '?'}",
                _format_time(row.get("timestamp")),
                "reference",
                _REFERENCE_EVENT_NAMES.get(event_type, event_type.replace("_", " ")),
                "read-only",
                "Evidence",
            ]
        state = record["state"]
        source = state.get("source") or {}
        review = state.get("review") or {}
        kind = str(source.get("kind") or "event")
        index = str(source.get("source_index") or "")
        event_name = "initial" if kind == "initial_assumption" else f"#{index or str(state.get('event_id'))[:8]}"
        return [
            event_name,
            _format_time(source.get("original_at_utc")),
            kind.replace("_", " "),
            self._labels_summary(review.get("labels") or []),
            str(review.get("candidate_decision") or "—"),
            "Unfinished" if state.get("status") == "Draft" else "Complete",
        ]

    @staticmethod
    def _labels_summary(labels: object) -> str:
        if not isinstance(labels, list) or not labels:
            return "—"
        parts: list[str] = []
        for label in labels:
            if not isinstance(label, Mapping):
                continue
            if label.get("kind") == "reconstruction":
                sign = "+" if label.get("change") == "appeared" else "−"
                parts.append(sign + RECONSTRUCTION_DISPLAY.get(
                    str(label.get("value")), str(label.get("value") or "?"),
                ))
            elif label.get("kind") == "pattern_clarity":
                parts.append("Clarity→" + CLARITY_DISPLAY.get(
                    str(label.get("value")), str(label.get("value") or "?"),
                ))
            elif label.get("kind") == "surface_quality":
                value = str(label.get("value") or "?")
                parts.append("Legacy quality→" + value.capitalize())
        return ", ".join(parts) if parts else "—"

    def _refresh_unreviewed_badge(self) -> None:
        # Autosave has no per-event Complete/Apply ceremony, therefore an
        # "unfinished" badge would be misleading. Missing interval
        # representatives are shown in the interval UI itself.
        self.unreviewed_count_changed.emit(0)

    # ------------------------------------------------------------ selection

    def _show_placeholder(self, text: str) -> None:
        self._detail_placeholder.setText(text)
        self._detail_placeholder.show()
        self._detail_content.hide()

    def _show_detail(self) -> None:
        self._detail_placeholder.hide()
        self._detail_content.show()

    def _clear_frame_cache(self) -> None:
        self._cached_paths = []
        self._cached_pixmaps = []
        self._capture_metadata_by_path = {}
        self._capture_metadata_by_filename = {}
        self._capture_manifest_error = "No event buffer is selected."
        if hasattr(self, "_image_label"):
            self._image_label.clearImage()

    def _on_selection_changed(self) -> None:
        self._flush_review_fields()
        row = self.events_table.currentRow()
        item = self.events_table.item(row, COL_EVENT_ID) if row >= 0 else None
        record = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(record, Mapping):
            self._currently_displayed_event_id = None
            self._currently_displayed_event_idx = None
            self._clear_frame_cache()
            self._show_placeholder("Select an event to review it.")
            return
        if record["kind"] == "reference":
            self._load_reference_detail(record["row"])
        else:
            self._load_point_detail(str(record["event_id"]))

    def _load_reference_detail(self, row: Mapping[str, Any]) -> None:
        self._currently_displayed_event_id = None
        self._currently_displayed_event_idx = None
        event_type = str(row.get("event_type") or "reference")
        title = _REFERENCE_EVENT_NAMES.get(event_type, event_type.replace("_", " "))
        self._metadata_label.setText(
            f"{title} · {_format_time(row.get('timestamp'))} · read-only acquisition evidence"
        )
        self._reference_notice.setText(
            "This row records an instrument/view event. It can be used as context but "
            "cannot be edited as a reconstruction or clarity label."
        )
        self._reference_notice.show()
        self._label_box.hide()
        self._interval_box.hide()
        self._move_review_button.hide()
        self._representative_button.hide()
        paths = self._safe_individual_paths([row.get("frame_path")])
        self._set_frame_paths(paths, preferred_path=paths[0] if paths else None)
        self._show_detail()

    def _load_point_detail(self, event_id: str) -> None:
        state = self._event_states.get(event_id)
        if state is None:
            self._show_placeholder("This event changed on disk. Press Refresh.")
            return
        self._currently_displayed_event_id = event_id
        source = state.get("source") or {}
        try:
            self._currently_displayed_event_idx = int(source.get("source_index"))
        except (TypeError, ValueError):
            self._currently_displayed_event_idx = None
        row = self._source_row_for_state(state)
        self._metadata_label.setText(self._format_point_metadata(state, row))
        self._reference_notice.hide()
        self._label_box.show()
        self._interval_box.show()
        self._move_review_button.show()
        self._representative_button.show()

        paths, event_dir = self._frame_paths_for_event(state, row)
        self._capture_metadata_by_path = self._load_capture_metadata(event_dir)
        self._capture_metadata_by_filename = {
            Path(path).name: metadata
            for path, metadata in self._capture_metadata_by_path.items()
        }
        preferred = (state.get("review") or {}).get("anchor") or {}
        preferred_path = Path(str(preferred.get("frame_path") or "")) if preferred else None
        self._set_frame_paths(paths, preferred_path=preferred_path)
        self._populate_label_form(state)
        self._show_detail()

    def _format_point_metadata(
        self, state: Mapping[str, Any], row: Mapping[str, Any],
    ) -> str:
        source = state.get("source") or {}
        score = _as_optional_float(row.get("change_score"))
        temperature = _as_optional_float(row.get("pyrometer_temp_C"))
        parts = [
            f"{str(source.get('kind') or 'event').replace('_', ' ').title()}",
            _format_time(source.get("original_at_utc")),
        ]
        if score is not None:
            parts.append(f"registered change score {score:.3f}")
        if temperature is not None:
            parts.append(f"{temperature:.1f} ℃")
        return " · ".join(parts)

    def _safe_path(self, value: object) -> Optional[Path]:
        if self._session_dir is None or not str(value or "").strip():
            return None
        raw = Path(str(value))
        candidates = [raw] if raw.is_absolute() else [self._session_dir / raw]
        # Relocated archives sometimes retain the old absolute prefix. Only
        # the basename fallback below is accepted, never the old parent path.
        candidates.append(self._session_dir / "frames" / raw.name)
        frames_root = (self._session_dir / "frames").resolve()
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(frames_root)
            except (OSError, ValueError):
                continue
            if resolved.is_file() and resolved.suffix.lower() in {".bmp", ".png"}:
                return resolved
        return None

    def _safe_individual_paths(self, values: list[object]) -> list[Path]:
        result: list[Path] = []
        for value in values:
            path = self._safe_path(value)
            if path is not None and path not in result:
                result.append(path)
        return result

    def _resolve_event_dir(self, value: object, source_index: str) -> Optional[Path]:
        if self._session_dir is None:
            return None
        frames_root = (self._session_dir / "frames").resolve()
        candidates: list[Path] = []
        text = str(value or "").strip()
        if text:
            raw = Path(text)
            if not raw.is_absolute() and ".." not in raw.parts:
                candidates.append(self._session_dir / raw)
        if source_index.isdigit():
            candidates.append(self._session_dir / "frames" / f"auto_event_{int(source_index):03d}")
            candidates.append(self._session_dir / "frames" / f"manual_event_{int(source_index):03d}")
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                relative = resolved.relative_to(frames_root)
            except (OSError, ValueError):
                continue
            if relative.parts and resolved.is_dir():
                return resolved
        return None

    def _frame_paths_for_event(
        self, state: Mapping[str, Any], row: Mapping[str, Any],
    ) -> tuple[list[Path], Optional[Path]]:
        source = state.get("source") or {}
        source_index = str(source.get("source_index") or "")
        event_dir = self._resolve_event_dir(row.get("buffer_dir"), source_index)
        paths: list[Path] = []
        if event_dir is not None:
            frames_root = (self._session_dir / "frames").resolve()  # type: ignore[union-attr]
            for candidate in sorted(event_dir.iterdir()):
                if candidate.suffix.lower() not in {".bmp", ".png"}:
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(frames_root)
                    resolved.relative_to(event_dir)
                except (OSError, ValueError):
                    continue
                paths.append(resolved)
        review = state.get("review") or {}
        paths.extend(self._safe_individual_paths([
            source.get("original_frame_path"),
            row.get("frame_path"),
            (review.get("anchor") or {}).get("frame_path"),
            (review.get("representative_anchor") or {}).get("frame_path"),
        ]))
        deduplicated: list[Path] = []
        for path in paths:
            if path not in deduplicated:
                deduplicated.append(path)
        return deduplicated, event_dir

    def _load_capture_metadata(self, event_dir: Optional[Path]) -> dict[str, dict[str, Any]]:
        if event_dir is None:
            return {}
        manifest = event_dir / "capture_manifest.csv"
        if not manifest.is_file():
            self._capture_manifest_error = "No capture manifest; frame hash remains exact."
            return {}
        result: dict[str, dict[str, Any]] = {}
        try:
            with open(manifest, "r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    name = Path(str(row.get("frame_path") or "")).name
                    if not name:
                        continue
                    path = event_dir / name
                    try:
                        resolved = path.resolve(strict=True)
                        resolved.relative_to(event_dir)
                    except (OSError, ValueError):
                        continue
                    result[str(resolved)] = dict(row)
        except OSError as exc:
            self._capture_manifest_error = f"Capture manifest could not be read: {exc}"
            return {}
        self._capture_manifest_error = "" if result else "Capture manifest contains no usable rows."
        return result

    def _set_frame_paths(
        self, paths: list[Path], *, preferred_path: Optional[Path],
    ) -> None:
        self._cached_paths = list(paths)
        self._cached_pixmaps = [QPixmap(str(path)) for path in paths]
        if not paths:
            self._slider.blockSignals(True)
            self._slider.setRange(0, 0)
            self._slider.setValue(0)
            self._slider.blockSignals(False)
            self._image_label.clearImage()
            self._image_label.setText("No saved RHEED frame is attached to this event.")
            self._frame_position_label.setText("0 / 0")
            self._move_review_button.setEnabled(False)
            self._representative_button.setEnabled(False)
            return
        selected = len(paths) - 1
        if preferred_path is not None:
            try:
                preferred = preferred_path.resolve()
                selected = [path.resolve() for path in paths].index(preferred)
            except (OSError, ValueError):
                pass
        self._slider.blockSignals(True)
        self._slider.setRange(0, len(paths) - 1)
        self._slider.setValue(selected)
        self._slider.blockSignals(False)
        self._move_review_button.setEnabled(True)
        self._representative_button.setEnabled(True)
        self._display_frame_at(selected)

    @pyqtSlot(int)
    def _display_frame_at(self, index: int) -> None:
        if not self._cached_pixmaps or not 0 <= index < len(self._cached_pixmaps):
            self._image_label.clearImage()
            self._frame_position_label.setText("0 / 0")
            return
        self._image_label.setOriginalPixmap(self._cached_pixmaps[index])
        self._frame_position_label.setText(f"{index + 1} / {len(self._cached_pixmaps)}")

    # --------------------------------------------------------------- labels

    def _current_state(self) -> Optional[dict[str, Any]]:
        event_id = self._currently_displayed_event_id
        if event_id is None or self._point_store is None:
            return None
        state = self._point_store.get(event_id)
        if state is not None:
            self._event_states[event_id] = state
        return state

    def _actor(self, *, warn: bool = True) -> Optional[str]:
        actor = self._labeler_input.text().strip() or self._labeler
        if not actor and warn:
            QMessageBox.warning(
                self,
                "Grower name required",
                "Enter the grower name once at the top of the Events tab.",
            )
        return actor or None

    @staticmethod
    def _anchor_text(anchor: object) -> str:
        if not isinstance(anchor, Mapping):
            return "Not selected"
        sequence = anchor.get("capture_sequence")
        elapsed = _as_optional_float(anchor.get("elapsed_s"))
        name = Path(str(anchor.get("frame_path") or "")).name or "saved frame"
        details = [name]
        if sequence not in (None, ""):
            details.append(f"capture {sequence}")
        if elapsed is not None:
            details.append(f"t={elapsed:.2f} s")
        return " · ".join(details)

    def _populate_label_form(self, state: Mapping[str, Any]) -> None:
        self._populating_form = True
        try:
            source = state.get("source") or {}
            review = state.get("review") or {}
            original_elapsed = _as_optional_float(source.get("original_elapsed_s"))
            source_text = _format_time(source.get("original_at_utc"))
            if original_elapsed is not None:
                source_text += f" · t={original_elapsed:.2f} s"
            source_text += " · immutable"
            self._source_position_label.setText(source_text)
            self._review_position_label.setText(self._anchor_text(review.get("anchor")))
            self._representative_position_label.setText(
                self._anchor_text(review.get("representative_anchor"))
            )
            self._notes_input.setText(str(review.get("comment") or ""))

            source_kind = str(source.get("kind") or "")
            initial_state = source_kind == "initial_assumption"
            legacy_v2 = str(state.get("schema") or "") == V2_SCHEMA_ID
            labels = [
                item for item in review.get("labels") or []
                if isinstance(item, Mapping)
            ]
            by_meaning = {
                (str(item.get("kind")), str(item.get("value"))): item
                for item in labels
            }
            for value, combo in self._reconstruction_change_combos.items():
                combo.blockSignals(True)
                label = by_meaning.get(("reconstruction", value))
                index = combo.findData(str(label.get("change"))) if label else 0
                combo.setCurrentIndex(max(index, 0))
                combo.blockSignals(False)

            clarity = next((
                str(item.get("value")) for item in labels
                if item.get("kind") == "pattern_clarity"
            ), "")
            self._clarity_combo.blockSignals(True)
            self._clarity_combo.setCurrentIndex(max(
                self._clarity_combo.findData(
                    "bad" if initial_state and legacy_v2
                    else "" if initial_state else clarity
                ),
                0,
            ))
            self._clarity_combo.blockSignals(False)

            self._set_edit_controls_enabled(True)
            self._initial_state_notice.setVisible(initial_state)
            if initial_state:
                if legacy_v2:
                    quality = next((
                        str(item.get("value")) for item in labels
                        if item.get("kind") == "surface_quality"
                    ), "unknown")
                    self._initial_state_notice.setText(
                        "Frozen v2 starting state: 1x1 with Bad pattern clarity; "
                        f"recorded surface quality is {quality.capitalize()}."
                    )
                else:
                    self._initial_state_notice.setText(
                        "Start of session is fixed as 1x1 with unknown pattern "
                        "clarity. This is an auditable baseline, not an "
                        "appearance event."
                    )
                for combo in self._reconstruction_change_combos.values():
                    combo.setEnabled(False)
                self._clarity_combo.setEnabled(False)
                self._move_review_button.setEnabled(False)
            else:
                self._interval_explanation.setText(
                    "The labeled changes at this point start a derived stable-state "
                    "interval. Select its clearest characteristic frame here; the "
                    "next labeled point ends this interval automatically."
                )
            if initial_state:
                if legacy_v2:
                    self._interval_explanation.setText(
                        "This frozen v2 interval starts with 1x1 present and Bad "
                        "clarity. Its original surface-quality label is shown "
                        "without reinterpretation."
                    )
                else:
                    self._interval_explanation.setText(
                        "This is the first derived interval: 1x1 is present and "
                        "clarity is unknown. Select the clearest characteristic "
                        "frame in this interval without inventing a change at the "
                        "first frame."
                    )
            legacy_read_only = bool(
                self._point_store is not None
                and getattr(self._point_store, "read_only_legacy", False)
            )
            if legacy_read_only:
                self._set_edit_controls_enabled(False)
                self._completion_help.setText(
                    "Legacy event journal: integrity-checked and shown read-only. "
                    "Its labels are not guessed or silently converted into v3 "
                    "point-label meanings."
                )
            elif initial_state:
                self._completion_help.setText(
                    "Initial state is not an appearance event and has no semantic "
                    "change label. Its interval representative saves immediately."
                )
            else:
                self._completion_help.setText(
                    "Every reconstruction, clarity, and note edit is saved "
                    "immediately. There is no separate Save or Apply step."
                )
        finally:
            self._populating_form = False

    def _set_edit_controls_enabled(self, enabled: bool) -> None:
        widgets = [
            *self._reconstruction_change_combos.values(),
            self._clarity_combo,
            self._notes_input,
            self._move_review_button,
            self._representative_button,
        ]
        for widget in widgets:
            widget.setEnabled(enabled)
        self._add_event_button.setEnabled(bool(self._cached_paths))

    def _label_for_kind_value(
        self, state: Mapping[str, Any], *, kind: str, value: str | None = None,
    ) -> Optional[dict[str, Any]]:
        for raw in (state.get("review") or {}).get("labels") or []:
            if not isinstance(raw, Mapping) or raw.get("kind") != kind:
                continue
            if value is None or raw.get("value") == value:
                return dict(raw)
        return None

    def _store_call(self, method_name: str, **kwargs: Any) -> Optional[dict[str, Any]]:
        # A prefilled grower name must become durable on the first explicit
        # event action; otherwise the UI could appear reviewed while the
        # append-only state still had a blank reviewer.
        self._flush_review_fields()
        state = self._current_state()
        actor = self._actor()
        if state is None or actor is None or self._point_store is None:
            return None
        method = getattr(self._point_store, method_name, None)
        if not callable(method):
            QMessageBox.warning(
                self, "Review update unavailable", f"The journal does not support {method_name}."
            )
            return None
        try:
            updated = method(
                str(state["event_id"]),
                actor=actor,
                base_revision_id=state.get("revision_id"),
                **kwargs,
            )
        except PointEventError as exc:
            # Autosave must never interrupt the grower with a modal dialog.
            # Keep the validated journal unchanged, restore the visible
            # controls from that state, and show the reason inline.
            self._autosave_status.setStyleSheet(
                "color: #dc2626; font-size: 10px;"
            )
            self._autosave_status.setText(f"Not saved: {exc}")
            self._populate_label_form(state)
            return None
        self._autosave_status.setStyleSheet("color: #16a34a; font-size: 10px;")
        self._autosave_status.setText("Saved automatically")
        self._after_store_change(updated)
        return updated

    def _after_store_change(self, state: Mapping[str, Any]) -> None:
        event_id = str(state["event_id"])
        self._event_states[event_id] = dict(state)
        self._labels_cache[event_id] = dict(state.get("review") or {})
        self.point_event_changed.emit(dict(state))
        self._refresh_table(selected_key=event_id)
        if self._currently_displayed_event_id == event_id:
            self._populate_label_form(state)

    def _set_semantic_choice(
        self, *, kind: str, change: str, value: str,
        identity_value: str | None = None,
    ) -> None:
        if self._populating_form:
            return
        state = self._current_state()
        if state is None:
            return
        existing = self._label_for_kind_value(
            state, kind=kind, value=identity_value,
        )
        if not value:
            if existing is not None:
                self._store_call("remove_label", label_id=existing["label_id"])
            return
        label = {
            "label_id": str(existing.get("label_id") if existing else uuid.uuid4()),
            "kind": kind,
            "change": change,
            "value": value,
        }
        if existing is None:
            self._store_call("add_label", label=label)
        elif (
            existing.get("change") != change or existing.get("value") != value
        ):
            self._store_call(
                "edit_label", label_id=existing["label_id"], label=label,
            )

    def _on_reconstruction_change(self, reconstruction: str) -> None:
        change = str(self._reconstruction_change_combos[reconstruction].currentData())
        self._set_semantic_choice(
            kind="reconstruction", change=change or "appeared",
            value=reconstruction if change else "", identity_value=reconstruction,
        )

    def _on_clarity_change(self, _index: int = 0) -> None:
        value = str(self._clarity_combo.currentData())
        self._set_semantic_choice(
            kind="pattern_clarity", change="became", value=value,
        )

    def _queue_review_field_save(self) -> None:
        if not self._populating_form:
            self._review_save_timer.start()

    def _flush_review_fields(self) -> None:
        self._review_save_timer.stop()
        if self._populating_form:
            return
        state = self._current_state()
        if state is None or self._point_store is None:
            return
        review = state.get("review") or {}
        changes: dict[str, Any] = {}
        comment = self._notes_input.text()
        if comment != str(review.get("comment") or ""):
            changes["comment"] = comment
        if not changes:
            return
        actor = self._actor(warn=False)
        if not actor:
            return
        try:
            updated = self._point_store.edit_review(
                str(state["event_id"]),
                actor=actor,
                base_revision_id=state.get("revision_id"),
                **changes,
            )
        except PointEventError as exc:
            self._autosave_status.setStyleSheet(
                "color: #dc2626; font-size: 10px;"
            )
            self._autosave_status.setText(f"Not saved: {exc}")
            self._populate_label_form(state)
            return
        self._autosave_status.setStyleSheet("color: #16a34a; font-size: 10px;")
        self._autosave_status.setText("Saved automatically")
        self._after_store_change(updated)

    # --------------------------------------------------------------- anchors

    def _current_frame_anchor(self) -> Optional[dict[str, Any]]:
        state = self._current_state()
        index = self._slider.value()
        if state is None or not 0 <= index < len(self._cached_paths):
            return None
        path = self._cached_paths[index]
        metadata = dict(self._capture_metadata_by_path.get(str(path), {}))
        source = state.get("source") or {}
        review = state.get("review") or {}
        for existing in (review.get("anchor"), review.get("representative_anchor")):
            if not isinstance(existing, Mapping):
                continue
            try:
                same_path = Path(str(existing.get("frame_path") or "")).resolve() == path.resolve()
            except OSError:
                same_path = False
            if same_path:
                metadata = {**dict(existing), **metadata}
                break
        captured_at = str(metadata.get("captured_at_utc") or source.get("original_at_utc") or "")
        elapsed = _frame_elapsed_from_capture(metadata, source)
        try:
            archive_member = path.relative_to(self._session_dir).as_posix()
        except (OSError, TypeError, ValueError):
            archive_member = str(metadata.get("archive_member") or "")
        image_sha256 = str(
            metadata.get("image_sha256") or metadata.get("frame_sha256") or ""
        )
        image_sha256_algorithm = str(
            metadata.get("image_sha256_algorithm")
            or metadata.get("frame_sha256_algorithm")
            or ("raw-file-bytes-v1" if image_sha256 else "")
        )
        try:
            return make_review_anchor(
                frame_path=path,
                capture_sequence=metadata.get("capture_sequence", source.get("capture_sequence")),
                image_sha256=image_sha256,
                image_sha256_algorithm=image_sha256_algorithm,
                captured_at_utc=captured_at,
                elapsed_s=elapsed,
                view_segment_id=metadata.get("view_segment_id"),
                capture_geometry_id=str(metadata.get("capture_geometry_id") or ""),
                session_identity=str(
                    source.get("session_identity")
                    or (self._session_dir.name if self._session_dir else "")
                ),
                frame_index=metadata.get("frame_index"),
                archive_member=archive_member,
            )
        except (OSError, PointEventError) as exc:
            QMessageBox.warning(self, "Frame cannot be selected", str(exc))
            return None

    def _move_review_to_current_frame(self) -> None:
        anchor = self._current_frame_anchor()
        if anchor is None:
            return
        updated = self._store_call("move_review_anchor", anchor=anchor)
        if updated is not None:
            self.review_anchor_moved.emit({
                "event_id": updated["event_id"], "anchor": anchor,
            })

    def _set_representative_anchor(self) -> None:
        anchor = self._current_frame_anchor()
        if anchor is None:
            return
        updated = self._store_call("move_representative_anchor", anchor=anchor)
        if updated is not None:
            self.representative_anchor_moved.emit({
                "event_id": updated["event_id"], "anchor": anchor,
            })

    # ---------------------------------------------------------- finish/add

    def _complete_event(self) -> None:
        self._flush_review_fields()
        state = self._current_state()
        actor = self._actor()
        if state is None or actor is None or self._point_store is None:
            return
        try:
            updated = self._point_store.complete(
                str(state["event_id"]),
                actor=actor,
                base_revision_id=state.get("revision_id"),
            )
        except PointEventCompletionError as exc:
            QMessageBox.warning(
                self,
                "Event is still unfinished",
                "Complete these items first:\n\n• " + "\n• ".join(exc.errors),
            )
            return
        except PointEventError as exc:
            QMessageBox.warning(self, "Complete blocked", str(exc))
            return
        self._after_store_change(updated)

    def _reopen_event(self) -> None:
        state = self._current_state()
        actor = self._actor()
        if state is None or actor is None or self._point_store is None:
            return
        try:
            updated = self._point_store.reopen(
                str(state["event_id"]), actor=actor,
            )
        except PointEventError as exc:
            QMessageBox.warning(self, "Reopen blocked", str(exc))
            return
        self._after_store_change(updated)

    def _add_posthoc_event(self) -> None:
        if self._point_store is None or self._session_dir is None:
            return
        actor = self._actor()
        anchor = self._current_frame_anchor()
        if actor is None or anchor is None:
            return
        source_index = str(uuid.uuid4())
        source_row = {
            "source": "events_tab",
            "source_index": source_index,
            "parent_event_id": self._currently_displayed_event_id or "",
            "frame_path": anchor["frame_path"],
            "capture_sequence": anchor.get("capture_sequence"),
            "captured_at_utc": anchor.get("captured_at_utc"),
            "elapsed_s": anchor.get("elapsed_s"),
            "archive_member": anchor.get("archive_member", ""),
            "frame_index": anchor.get("frame_index"),
        }
        if anchor.get("image_sha256"):
            source_row["image_sha256"] = anchor["image_sha256"]
        try:
            state = self._point_store.create_event(
                source_kind="posthoc",
                actor=actor,
                session_identity=self._session_dir.name,
                source_file="events_tab_posthoc",
                source_index=source_index,
                source_row=source_row,
                original_at_utc=str(anchor.get("captured_at_utc") or ""),
                original_elapsed_s=anchor.get("elapsed_s"),
                capture_sequence=anchor.get("capture_sequence"),
                original_frame_path=str(anchor["frame_path"]),
                original_image_sha256=str(anchor.get("image_sha256") or ""),
                review_anchor=anchor,
                current_software=True,
            )
        except (OSError, PointEventError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Event could not be added", str(exc))
            return
        self._event_states[str(state["event_id"])] = state
        self._refresh_table(selected_key=str(state["event_id"]))
        self.point_event_changed.emit(dict(state))


__all__ = [
    "CLARITY_OPTIONS",
    "COL_DECISION",
    "COL_EVENT_ID",
    "COL_EVENT_IDX",
    "COL_LABELS",
    "COL_SOURCE",
    "COL_STATE",
    "COL_TIME",
    "COLUMN_HEADERS",
    "EventsTab",
    "RECON_LABEL_OPTIONS",
    "RECON_UNLABELED",
    "RECONSTRUCTION_OPTIONS",
]
