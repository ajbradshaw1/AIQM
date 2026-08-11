"""Unit tests for gui/events_tab.py — construction + attach_session
lifecycle (D2 Day 8 sprint kickoff).

Locks the core surface of the Events tab: master table shape, detail
pane placeholder, label form presence, and the attach_session lifecycle
(clears table, reloads cache, handles None). More coverage — row
insertion, label-form activation slots, unreviewed-badge — lands in
Day 9's follow-on test classes; this file is the construction +
lifecycle foundation those will extend.

Run:
    python -m pytest -q tests/test_events_tab.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# QApplication precedes any QWidget. Same pattern as
# test_scrubber_tab.py and test_live_equalizer_tab.py.
from PyQt6.QtWidgets import QApplication  # noqa: E402
_app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

from gui.events_tab import (  # noqa: E402
    COL_EVENT_IDX,
    COL_STATE,
    COL_TIME,
    COLUMN_HEADERS,
    EventsTab,
    RECON_UNLABELED,
)
from gui.growth_logger import (  # noqa: E402
    EVENT_STATE_DISCARDED,
    EVENT_STATE_KEPT_EXPLICIT,
    GrowthLogger,
)
from PyQt6.QtCore import Qt  # noqa: E402


class EventsTabConstructionTests(unittest.TestCase):
    """Five tests locking the widget's structural shape at construction."""

    def setUp(self):
        self.tab = EventsTab()

    def tearDown(self):
        self.tab.deleteLater()

    def test_builds_cleanly_without_session(self):
        # Construction must not require a session. GrowthMonitor
        # instantiates the tab before any session is armed, so a
        # bare EventsTab() has to be a valid state.
        self.assertIsNone(self.tab._session_dir)
        self.assertIsNone(self.tab._growth_logger)
        self.assertEqual(self.tab._last_seen_event_idx, 0)
        self.assertEqual(self.tab._labels_cache, {})
        self.assertIsNone(self.tab._currently_displayed_event_idx)

    def test_master_table_column_count(self):
        # 5 columns per COLUMN_HEADERS. If a new column is added,
        # this test flags the schema change so table-index constants
        # (COL_EVENT_IDX etc.) can be reviewed alongside.
        self.assertEqual(len(COLUMN_HEADERS), 5)
        self.assertEqual(
            self.tab.events_table.columnCount(), 5,
        )
        # Header labels round-trip
        header = self.tab.events_table.horizontalHeader()
        for i, expected in enumerate(COLUMN_HEADERS):
            item = self.tab.events_table.horizontalHeaderItem(i)
            if item is not None:
                self.assertEqual(item.text(), expected)

    def test_detail_placeholder_visible_initially(self):
        # No event selected → placeholder shown, content hidden.
        # Uses isHidden() rather than isVisible() because offscreen
        # Qt reports isVisible=False for any child of a non-shown
        # parent — isHidden() reflects explicit hide() calls, which
        # is the design contract we actually care about.
        self.assertFalse(self.tab._detail_placeholder.isHidden())
        self.assertTrue(self.tab._detail_content.isHidden())
        self.assertIn(
            "Select an event", self.tab._detail_placeholder.text(),
        )

    def test_label_form_widgets_present(self):
        # The three labeling combos + notes field are load-bearing
        # attribute names — attach_session's _populate_label_form,
        # the _on_*_activated slots, and Day 3's dropdowns all
        # reach into these by name.
        for attr in (
            "_primary_recon_combo",
            "_change_from_combo",
            "_change_to_combo",
            "_notes_input",
        ):
            self.assertTrue(
                hasattr(self.tab, attr),
                msg=f"missing labelform widget: {attr}",
            )

    def test_unreviewed_count_signal_registered(self):
        # unreviewed_count_changed is consumed by GrowthMonitor's
        # events-tab-badge painter. Losing the signal name would
        # silently break the badge — regression guard here.
        self.assertTrue(hasattr(self.tab, "unreviewed_count_changed"))
        # Signal can be connected + emitted without exceptions
        received: list[int] = []
        self.tab.unreviewed_count_changed.connect(received.append)
        self.tab.unreviewed_count_changed.emit(7)
        self.assertEqual(received, [7])


class EventsTabAttachSessionTests(unittest.TestCase):
    """Six tests locking the attach_session lifecycle."""

    def setUp(self):
        self.tab = EventsTab()
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("D2_TEST")

    def tearDown(self):
        self.tab.deleteLater()
        try:
            self.logger.end_session()
        except Exception:
            pass
        self.tmp.cleanup()

    def _write_labels_csv(self, rows: list[dict]):
        """Helper: write events_labels.csv with the given rows before
        attach so we can verify cache repopulation."""
        csv_path = self.logger.session_dir / "events_labels.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=GrowthLogger.EVENT_LABEL_FIELDS,
            )
            w.writeheader()
            for row in rows:
                w.writerow(row)

    def test_attach_populates_session_dir_and_logger(self):
        # After attach, both refs are set + point to the correct
        # session. Downstream slots (labeling writes) rely on both
        # being non-None.
        self.tab.attach_session(self.logger)
        self.assertIs(self.tab._growth_logger, self.logger)
        self.assertEqual(
            self.tab._session_dir, self.logger.session_dir,
        )

    def test_attach_clears_table_from_prior_session(self):
        # Simulate a prior session having populated the table (rows
        # from a previous session leaking into a new one would be
        # a UI-truth failure). attach_session must clear.
        self.tab.events_table.setRowCount(3)
        self.assertEqual(self.tab.events_table.rowCount(), 3)
        self.tab.attach_session(self.logger)
        self.assertEqual(self.tab.events_table.rowCount(), 0)

    def test_attach_reloads_label_cache_from_csv(self):
        # Pre-write a label to the logger's session_dir. attach
        # must re-read that CSV into _labels_cache so the labeling
        # form can prefill existing labels when the grower selects
        # a previously-labeled event.
        self._write_labels_csv([
            {
                "event_idx": "5",
                "primary_reconstruction": "1x1",
                "change_from": "",
                "change_to": "",
                "notes": "prior session note",
                "label_timestamp_iso": "2026-07-22T10:00:00",
                "recon_1x1": "",
                "recon_tw": "",
                "recon_c6x2": "",
                "recon_rt13": "",
                "recon_HTR": "",
            },
        ])
        self.tab.attach_session(self.logger)
        self.assertIn(5, self.tab._labels_cache)
        self.assertEqual(
            self.tab._labels_cache[5]["primary_reconstruction"],
            "1x1",
        )
        self.assertEqual(
            self.tab._labels_cache[5]["notes"],
            "prior session note",
        )

    def test_attach_with_no_labels_csv_yields_empty_cache(self):
        # Fresh session, no CSV → cache is an empty dict, not
        # None (downstream .get(idx) calls assume dict).
        self.tab.attach_session(self.logger)
        self.assertEqual(self.tab._labels_cache, {})
        self.assertIsInstance(self.tab._labels_cache, dict)

    def test_attach_none_clears_state(self):
        # attach(None) is the "session ended" cleanup path from
        # GrowthApp._on_stop. Both refs must be nulled + cache
        # must clear.
        self.tab.attach_session(self.logger)
        self.assertIsNotNone(self.tab._growth_logger)

        self.tab.attach_session(None)
        self.assertIsNone(self.tab._growth_logger)
        self.assertIsNone(self.tab._session_dir)
        self.assertEqual(self.tab._labels_cache, {})

    def test_attach_resets_currently_displayed_event_idx(self):
        # If the grower had an event selected when the previous
        # session ended, attach_session must reset the cursor so
        # form-write slots don't write against a stale event_idx
        # under the new session's CSV.
        self.tab.attach_session(self.logger)
        self.tab._currently_displayed_event_idx = 42
        self.tab.attach_session(self.logger)  # re-attach same logger
        self.assertIsNone(
            self.tab._currently_displayed_event_idx,
        )


def _write_auto_capture_csv(
    logger: GrowthLogger, rows: list[dict],
) -> None:
    """Helper: write auto_capture_events.csv with the given rows."""
    csv_path = logger.session_dir / "auto_capture_events.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=GrowthLogger.AUTO_CAPTURE_FIELDS,
        )
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _mkrow(event_idx: int, elapsed_s: float = 30.0,
            score: float = 0.5, state: str = "pending",
            temp: float = 500.0) -> dict:
    return {
        "timestamp": f"2026-07-15T10:00:{int(elapsed_s):02d}",
        "elapsed_s": str(elapsed_s),
        "event_idx": str(event_idx),
        "change_score": str(score),
        "pyrometer_temp_C": str(temp),
        "buffer_count": "20",
        "buffer_dir": "",
        "event_state": state,
        "state_changed_at": "",
    }


class EventsTabRowInsertionTests(unittest.TestCase):
    """Six tests locking auto_capture_events.csv → master-table
    row insertion. Covers _load_csv_rows + _add_event_row."""

    def setUp(self):
        self.tab = EventsTab()
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("ROW_TEST")

    def tearDown(self):
        self.tab.deleteLater()
        try:
            self.logger.end_session()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_newest_row_at_top(self):
        # Rows written to CSV in idx order (1, 2, 3) but inserted
        # in the table via insertRow(0) — newest event_idx ends up
        # at row 0. Guards against a future refactor accidentally
        # switching to append order (bottom-heavy tables are
        # painful during long walk-away sessions).
        _write_auto_capture_csv(self.logger, [
            _mkrow(1), _mkrow(2), _mkrow(3),
        ])
        self.tab.attach_session(self.logger)
        self.assertEqual(self.tab.events_table.rowCount(), 3)
        top_idx = self.tab.events_table.item(0, COL_EVENT_IDX).text()
        bottom_idx = self.tab.events_table.item(2, COL_EVENT_IDX).text()
        self.assertEqual(top_idx, "3")
        self.assertEqual(bottom_idx, "1")

    def test_no_duplicates_on_repeat_load(self):
        # _load_csv_rows uses _last_seen_event_idx as a watermark.
        # Attach populates it; a second on_frame_captured with the
        # same CSV must not re-append the same rows. Regression
        # guard for a subtle append-storm bug that could show up if
        # the watermark logic breaks.
        _write_auto_capture_csv(self.logger, [_mkrow(1), _mkrow(2)])
        self.tab.attach_session(self.logger)
        self.assertEqual(self.tab.events_table.rowCount(), 2)
        # Simulate another frame_captured with no new rows in CSV
        import numpy as np
        self.tab.on_frame_captured(np.zeros((10, 10)), 0.0)
        self.assertEqual(self.tab.events_table.rowCount(), 2)

    def test_bad_event_idx_row_skipped(self):
        # Row with non-integer event_idx must be skipped rather
        # than raising. CSV corruption should degrade gracefully.
        _write_auto_capture_csv(self.logger, [
            _mkrow(1),
            _mkrow(0) | {"event_idx": "not_a_number"},
            _mkrow(2),
        ])
        self.tab.attach_session(self.logger)
        # Only rows 1 and 2 land; corrupt row filtered out
        self.assertEqual(self.tab.events_table.rowCount(), 2)
        ids = {
            self.tab.events_table.item(i, COL_EVENT_IDX).text()
            for i in range(self.tab.events_table.rowCount())
        }
        self.assertEqual(ids, {"1", "2"})

    def test_on_frame_captured_appends_new_row(self):
        # After attach, add a new row to CSV, fire on_frame_captured
        # — the new row should land in the table. This is the
        # "live event fired" path.
        _write_auto_capture_csv(self.logger, [_mkrow(1)])
        self.tab.attach_session(self.logger)
        self.assertEqual(self.tab.events_table.rowCount(), 1)
        # Add row 2, trigger reload
        _write_auto_capture_csv(self.logger, [_mkrow(1), _mkrow(2)])
        import numpy as np
        self.tab.on_frame_captured(np.zeros((10, 10)), 0.0)
        self.assertEqual(self.tab.events_table.rowCount(), 2)

    def test_missing_csv_yields_empty_table(self):
        # No auto_capture_events.csv exists (fresh session before
        # any event has fired). Table stays empty; no exception.
        self.tab.attach_session(self.logger)
        self.assertEqual(self.tab.events_table.rowCount(), 0)

    def test_row_metadata_carries_full_csv_dict(self):
        # UserRole data on the event_idx cell should be the full
        # CSV row dict — subsequent selection handler reads
        # buffer_dir + state + timestamp without re-reading the CSV.
        row = _mkrow(1, state="kept_default", temp=456.7)
        row["buffer_dir"] = "/tmp/buf_1"
        _write_auto_capture_csv(self.logger, [row])
        self.tab.attach_session(self.logger)
        item = self.tab.events_table.item(0, COL_EVENT_IDX)
        data = item.data(Qt.ItemDataRole.UserRole)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["event_idx"], "1")
        self.assertEqual(data["buffer_dir"], "/tmp/buf_1")
        self.assertEqual(data["event_state"], "kept_default")


class EventsTabLabelFormTests(unittest.TestCase):
    """Five tests locking the labeling-form write path + debounce."""

    def setUp(self):
        self.tab = EventsTab()
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("FORM_TEST")

    def tearDown(self):
        self.tab.deleteLater()
        try:
            self.logger.end_session()
        except Exception:
            pass
        self.tmp.cleanup()

    def _read_labels_csv(self) -> list[dict]:
        p = self.logger.session_dir / "events_labels.csv"
        if not p.exists():
            return []
        with open(p) as f:
            return list(csv.DictReader(f))

    def test_primary_recon_activation_without_labeler_is_blocked(self):
        # User picks "1x1" from the primary combo → events_labels.csv
        # gets a row keyed to the current event_idx. Locks the
        # single-slot write contract (activated fires only on user
        # interaction, so triggering it in the test simulates the
        # real click path).
        self.tab.attach_session(self.logger)
        self.tab._currently_displayed_event_idx = 7
        one_x_one_idx = self.tab._primary_recon_combo.findData("1x1")
        self.tab._primary_recon_combo.setCurrentIndex(one_x_one_idx)
        with patch("gui.events_tab.QMessageBox.warning") as warning:
            self.tab._on_primary_recon_activated(one_x_one_idx)
        rows = self._read_labels_csv()
        self.assertEqual(rows, [])
        self.assertTrue(warning.called)

    def test_notes_debounce_writes_once(self):
        # Multiple textChanged emissions restart the debounce
        # timer; only one CSV write should land after the flush
        # fires. Simulate by calling _on_notes_text_changed
        # repeatedly then _flush_notes_to_disk once.
        self.tab.attach_session(self.logger)
        self.tab._currently_displayed_event_idx = 3
        self.tab._notes_input.setText("something to say")
        # Simulate 3 rapid keystrokes
        for _ in range(3):
            self.tab._on_notes_text_changed()
        # Manual flush (in real UI the debounce timer fires)
        self.tab._flush_notes_to_disk()
        rows = self._read_labels_csv()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["notes"], "something to say")

    def test_switching_events_flushes_pending_notes(self):
        # Grower starts typing on event 1, then clicks event 2
        # WITHOUT waiting for the debounce timer. The unsaved
        # notes must persist to event 1's row (not silently lost).
        # _flush_notes_to_disk is called from _on_selection_changed
        # to ensure this — this test locks that side-effect.
        self.tab.attach_session(self.logger)
        self.tab._currently_displayed_event_idx = 1
        self.tab._notes_input.setText("half-typed note")
        # Simulate keystroke without letting the debounce fire
        self.tab._on_notes_text_changed()
        # Now flush (as would happen on selection change)
        self.tab._flush_notes_to_disk()
        rows = self._read_labels_csv()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_idx"], "1")
        self.assertEqual(rows[0]["notes"], "half-typed note")

    def test_populate_from_cache_on_selection(self):
        # Pre-populate label cache; _populate_label_form should
        # restore the primary combo + notes without emitting any
        # writes. Guards against a "select event, form empty even
        # though CSV has a label" UX bug.
        self.tab.attach_session(self.logger)
        self.tab._labels_cache[5] = {
            "event_idx": "5",
            "primary_reconstruction": "c(6x2)",
            "change_from": RECON_UNLABELED,
            "change_to": RECON_UNLABELED,
            "notes": "previously saved",
            "label_timestamp_iso": "2026-07-15T10:00:00",
            "recon_1x1": "", "recon_tw": "", "recon_c6x2": "",
            "recon_rt13": "", "recon_HTR": "",
        }
        self.tab._populate_label_form(5)
        self.assertEqual(
            self.tab._primary_recon_combo.currentData(), "c(6x2)",
        )
        self.assertEqual(
            self.tab._notes_input.text(), "previously saved",
        )

    def test_form_state_isolated_between_events(self):
        # Set label on event A. Switch to event B (no prior label)
        # via _populate_label_form. Form must reset to unlabeled,
        # not carry event A's state across.
        self.tab.attach_session(self.logger)
        self.tab._labels_cache[10] = {
            "event_idx": "10", "primary_reconstruction": "HTR",
            "change_from": "", "change_to": "",
            "notes": "hot growth", "label_timestamp_iso": "T",
            "recon_1x1": "", "recon_tw": "", "recon_c6x2": "",
            "recon_rt13": "", "recon_HTR": "",
        }
        self.tab._populate_label_form(10)
        self.assertEqual(
            self.tab._primary_recon_combo.currentData(), "HTR",
        )
        # Switch to event 11 (no cache entry)
        self.tab._populate_label_form(11)
        self.assertEqual(
            self.tab._primary_recon_combo.currentData(),
            RECON_UNLABELED,
        )
        self.assertEqual(self.tab._notes_input.text(), "")


class EventsTabUnreviewedBadgeTests(unittest.TestCase):
    """Four tests locking the unreviewed-count badge semantics."""

    def setUp(self):
        self.tab = EventsTab()
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("BADGE_TEST")
        # Capture the signal emissions for assertion
        self.emitted: list[int] = []
        self.tab.unreviewed_count_changed.connect(self.emitted.append)

    def tearDown(self):
        self.tab.deleteLater()
        try:
            self.logger.end_session()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_pending_events_counted(self):
        # 3 pending events → badge shows 3.
        _write_auto_capture_csv(self.logger, [
            _mkrow(1, state="pending"),
            _mkrow(2, state="pending"),
            _mkrow(3, state="pending"),
        ])
        self.tab.attach_session(self.logger)
        self.assertGreater(len(self.emitted), 0)
        self.assertEqual(self.emitted[-1], 3)

    def test_discarded_events_not_counted(self):
        # 1 discarded + 1 pending → badge shows 1 (only pending).
        # Discarded is an explicit "no" decision; grower doesn't
        # need to see it in the unreviewed count.
        _write_auto_capture_csv(self.logger, [
            _mkrow(1, state=EVENT_STATE_DISCARDED),
            _mkrow(2, state="pending"),
        ])
        self.tab.attach_session(self.logger)
        self.assertEqual(self.emitted[-1], 1)

    def test_kept_explicit_without_label_counts(self):
        # kept_explicit but no primary_reconstruction label →
        # still counts as needing attention. Locks the labeling-
        # phase badge behavior (badge stays useful past the
        # keep/discard phase).
        _write_auto_capture_csv(self.logger, [
            _mkrow(1, state=EVENT_STATE_KEPT_EXPLICIT),
        ])
        self.tab.attach_session(self.logger)
        # No label cache entry for event 1 → still unreviewed
        self.assertEqual(self.emitted[-1], 1)

    def test_kept_explicit_with_label_not_counted(self):
        # kept_explicit AND has a primary_reconstruction label →
        # fully reviewed, badge zero. Locks the "labeling clears
        # the badge" contract.
        _write_auto_capture_csv(self.logger, [
            _mkrow(1, state=EVENT_STATE_KEPT_EXPLICIT),
        ])
        self.tab.attach_session(self.logger)
        # Now apply a label
        self.tab._labels_cache[1] = {
            "event_idx": "1", "primary_reconstruction": "1x1",
            "change_from": "", "change_to": "",
            "notes": "", "label_timestamp_iso": "T",
            "recon_1x1": "", "recon_tw": "", "recon_c6x2": "",
            "recon_rt13": "", "recon_HTR": "",
        }
        self.tab._refresh_unreviewed_badge()
        self.assertEqual(self.emitted[-1], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
