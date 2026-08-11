"""Unit tests for the events_tab labeling form's change_from / change_to
dropdowns (Jul 15 2026 addition).

Locks the shape + round-trip behavior of the two new combos:
  - present in the form after tab construction
  - default to RECON_UNLABELED sentinel
  - selection writes atomically to events_labels.csv
  - selection persists across attach_session reload
  - independent from primary_reconstruction (partial-update contract)

Run:
    python -m pytest -q tests/test_events_tab_labeling.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# QApplication precedes any QWidget. Same pattern as test_live_equalizer_tab.py.
from PyQt6.QtWidgets import QApplication  # noqa: E402
_app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

from gui.events_tab import (  # noqa: E402
    RECON_LABEL_OPTIONS,
    RECON_UNLABELED,
    EventsTab,
)
from gui.growth_logger import GrowthLogger  # noqa: E402


def _make_logger_with_session(tmp: str) -> GrowthLogger:
    logger = GrowthLogger(base_dir=tmp)
    logger.start_session("TEST_LABELING")
    return logger


class ChangeFromToDropdownTests(unittest.TestCase):
    """Six locked behaviors for the change_from / change_to combos."""

    def setUp(self):
        self.tab = EventsTab()
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = _make_logger_with_session(self.tmp.name)
        self.tab.attach_session(self.logger)
        # Simulate an event being "currently displayed" so the slots
        # don't early-return on the None guard. Downstream tests set
        # index + fire activated to trigger the write path.
        self.tab._currently_displayed_event_idx = 1

    def tearDown(self):
        self.tab.deleteLater()
        try:
            self.logger.end_session()
        except Exception:
            pass
        self.tmp.cleanup()

    def _read_labels_csv(self) -> list[dict]:
        csv_path = self.logger.session_dir / "events_labels.csv"
        if not csv_path.exists():
            return []
        with open(csv_path) as f:
            return list(csv.DictReader(f))

    def test_dropdowns_present_after_form_build(self):
        # Both combos are exposed as public-ish attributes for slot wiring.
        # Loss of these attribute names would break EventsTab.attach_session
        # + the tests below, so lock the surface.
        self.assertTrue(hasattr(self.tab, "_change_from_combo"))
        self.assertTrue(hasattr(self.tab, "_change_to_combo"))
        # Item count = 1 (unlabeled sentinel) + full RECON_LABEL_OPTIONS.
        expected_count = 1 + len(RECON_LABEL_OPTIONS)
        self.assertEqual(self.tab._change_from_combo.count(), expected_count)
        self.assertEqual(self.tab._change_to_combo.count(), expected_count)

    def test_dropdowns_default_to_unlabeled_sentinel(self):
        # Fresh construction: both combos at index 0 = RECON_UNLABELED.
        # Guard against a future accidental default-swap that would
        # silently label every unfixed event as "1x1".
        self.assertEqual(
            self.tab._change_from_combo.currentData(), RECON_UNLABELED,
        )
        self.assertEqual(
            self.tab._change_to_combo.currentData(), RECON_UNLABELED,
        )

    def test_change_from_selection_writes_to_csv(self):
        # Pick "1x1" (index 1 = first real class after (unlabeled)) and
        # fire the activation slot directly. QComboBox.activated is
        # user-only in normal Qt; tests trigger it explicitly.
        one_x_one_idx = self.tab._change_from_combo.findData("1x1")
        self.tab._change_from_combo.setCurrentIndex(one_x_one_idx)
        self.tab._on_change_from_activated(one_x_one_idx)

        rows = self._read_labels_csv()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_idx"], "1")
        self.assertEqual(rows[0]["change_from"], "1x1")
        # change_to unchanged, still empty
        self.assertEqual(rows[0].get("change_to", ""), "")

    def test_change_to_selection_writes_to_csv(self):
        tw_idx = self.tab._change_to_combo.findData("Twinned (2x1)")
        self.tab._change_to_combo.setCurrentIndex(tw_idx)
        self.tab._on_change_to_activated(tw_idx)

        rows = self._read_labels_csv()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["change_to"], "Twinned (2x1)")
        # change_from unchanged, still empty
        self.assertEqual(rows[0].get("change_from", ""), "")

    def test_change_from_and_to_persist_across_reload(self):
        # Set both, close the tab, reopen, verify populate_label_form
        # restores the same selections. Full round-trip through disk.
        from_idx = self.tab._change_from_combo.findData("1x1")
        to_idx = self.tab._change_to_combo.findData("Twinned (2x1)")
        self.tab._change_from_combo.setCurrentIndex(from_idx)
        self.tab._on_change_from_activated(from_idx)
        self.tab._change_to_combo.setCurrentIndex(to_idx)
        self.tab._on_change_to_activated(to_idx)

        # New tab against the same logger — re-reads the CSV into cache.
        tab2 = EventsTab()
        tab2.attach_session(self.logger)
        tab2._currently_displayed_event_idx = 1
        tab2._populate_label_form(1)

        self.assertEqual(
            tab2._change_from_combo.currentData(), "1x1",
        )
        self.assertEqual(
            tab2._change_to_combo.currentData(), "Twinned (2x1)",
        )
        tab2.deleteLater()

    def test_change_dropdowns_do_not_affect_primary_reconstruction(self):
        # Set a primary first, then change_from — the row should have
        # BOTH values, not just the last-touched one. Guard against a
        # subtle bug where update_event_label's None-preserving
        # semantics get accidentally overridden.
        # Seed the mutable legacy summary directly. The primary dropdown now
        # represents an exact-frame human submission and correctly refuses a
        # labeler-less, manifest-less test fixture.
        self.logger.update_event_label(
            1, primary_reconstruction="c(6x2)",
        )

        from_idx = self.tab._change_from_combo.findData("1x1")
        self.tab._change_from_combo.setCurrentIndex(from_idx)
        self.tab._on_change_from_activated(from_idx)

        rows = self._read_labels_csv()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["primary_reconstruction"], "c(6x2)")
        self.assertEqual(rows[0]["change_from"], "1x1")


class ChangeFromToNoSessionGuardTests(unittest.TestCase):
    """Slots must silently no-op when session isn't attached / no event
    displayed — same guard pattern as _on_primary_recon_activated."""

    def setUp(self):
        self.tab = EventsTab()

    def tearDown(self):
        self.tab.deleteLater()

    def test_no_session_no_op(self):
        # No logger attached + no event displayed → both slots return
        # without raising. Also no CSV should be created since no
        # session_dir exists.
        self.tab._currently_displayed_event_idx = None
        self.tab._growth_logger = None
        self.tab._on_change_from_activated(1)
        self.tab._on_change_to_activated(1)
        # No exception = pass.

    def test_no_event_displayed_no_op(self):
        # Logger attached but no currently-displayed event → slots
        # short-circuit rather than writing to a mysterious "event 0".
        tmp = tempfile.TemporaryDirectory()
        logger = _make_logger_with_session(tmp.name)
        self.tab.attach_session(logger)
        self.tab._currently_displayed_event_idx = None
        self.tab._on_change_from_activated(1)
        csv_path = logger.session_dir / "events_labels.csv"
        # No writes happened → file was never created.
        self.assertFalse(csv_path.exists())
        logger.end_session()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
