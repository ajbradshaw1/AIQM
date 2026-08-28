"""Interaction tests for editable v2 point-event labels and Anchors."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QMessageBox  # noqa: E402
from gui.rheed_point_events import make_review_anchor, sha256_file  # noqa: E402
from tests.test_events_tab import EventFixture, _app  # noqa: E402


class EditableEventLabelTests(EventFixture):
    def setUp(self) -> None:
        super().setUp()
        self.attach_and_select()

    def test_reconstruction_clarity_and_quality_autosave_independently(self) -> None:
        with patch.object(QMessageBox, "warning") as warning:
            rt13 = self.tab._reconstruction_change_combos["rt13"]
            rt13.setCurrentIndex(rt13.findData("appeared"))
            self.tab._clarity_combo.setCurrentIndex(
                self.tab._clarity_combo.findData("good")
            )
            self.tab._quality_combo.setCurrentIndex(
                self.tab._quality_combo.findData("bad")
            )
        self.assertFalse(warning.called, warning.call_args)

        state = self.store.get(self.event_id)
        labels = state["review"]["labels"]
        self.assertEqual(len(labels), 3)
        reconstruction = next(item for item in labels if item["kind"] == "reconstruction")
        clarity = next(item for item in labels if item["kind"] == "pattern_clarity")
        quality = next(item for item in labels if item["kind"] == "surface_quality")
        self.assertEqual((reconstruction["change"], reconstruction["value"]), ("appeared", "rt13"))
        self.assertEqual((clarity["change"], clarity["value"]), ("became", "good"))
        self.assertEqual((quality["change"], quality["value"]), ("became", "bad"))

        rt13.setCurrentIndex(rt13.findData(""))
        edited = self.store.get(self.event_id)["review"]["labels"]
        self.assertFalse(any(item["kind"] == "reconstruction" for item in edited))

        one_by_one = self.tab._reconstruction_change_combos["one_by_one"]
        one_by_one.setCurrentIndex(one_by_one.findData("disappeared"))
        edited = self.store.get(self.event_id)["review"]["labels"]
        edited_recon = next(item for item in edited if item["kind"] == "reconstruction")
        self.assertEqual(
            (edited_recon["value"], edited_recon["change"]),
            ("one_by_one", "disappeared"),
        )

        self.tab._clarity_combo.setCurrentIndex(self.tab._clarity_combo.findData(""))
        remaining = self.store.get(self.event_id)["review"]["labels"]
        self.assertEqual(len(remaining), 2)
        self.assertFalse(any(item["kind"] == "pattern_clarity" for item in remaining))

    def test_labeler_identity_is_entered_once_for_session(self) -> None:
        self.assertEqual(self.tab._labeler_input.text(), "grower-a")
        self.assertFalse(hasattr(self.tab, "_reviewer_input"))
        self.assertFalse(hasattr(self.tab, "_confidence_combo"))

    def test_slider_frame_moves_only_review_point(self) -> None:
        original = self.store.get(self.event_id)["source"]
        self.tab._slider.setValue(1)
        self.tab._move_review_to_current_frame()
        state = self.store.get(self.event_id)
        self.assertEqual(state["source"], original)
        self.assertEqual(state["review"]["anchor"]["capture_sequence"], "101")
        self.assertEqual(
            state["review"]["anchor"]["image_sha256"], sha256_file(self.frames[1]),
        )

    def test_representative_anchor_belongs_to_derived_interval(self) -> None:
        htr = self.tab._reconstruction_change_combos["htr"]
        htr.setCurrentIndex(htr.findData("appeared"))
        self.tab._slider.setValue(1)
        self.tab._set_representative_anchor()
        state = self.store.get(self.event_id)
        self.assertEqual(state["review"]["representative_anchor"]["capture_sequence"], "101")
        self.assertIn("derived stable-state interval", self.tab._interval_explanation.text())

    def test_posthoc_event_is_created_at_exact_shown_frame(self) -> None:
        self.tab._slider.setValue(1)
        before_ids = set(self.store.states)
        self.tab._add_posthoc_event()
        added_ids = set(self.store.states) - before_ids
        self.assertEqual(len(added_ids), 1)
        state = self.store.get(added_ids.pop())
        self.assertEqual(state["source"]["kind"], "posthoc")
        self.assertEqual(state["review"]["candidate_decision"], "confirmed")
        self.assertEqual(state["review"]["anchor"]["capture_sequence"], "101")


class InitialAssumptionUiTests(EventFixture):
    def test_initial_one_by_one_is_a_state_not_an_appearance_event(self) -> None:
        first_anchor = make_review_anchor(
            frame_path=self.frames[0], image_sha256=sha256_file(self.frames[0]),
            capture_sequence=100, captured_at_utc="2026-08-20T12:00:10+00:00",
            elapsed_s=0.0, view_segment_id=1, capture_geometry_id="geometry-a",
        )
        initial = self.store.ensure_initial_assumption(
            actor="session", session_identity=self.session_dir.name,
            first_frame_anchor=first_anchor,
            original_at_utc="2026-08-20T12:00:00+00:00",
        )
        self.tab.attach_session(self.logger, labeler="grower-a")
        for row in range(self.tab.events_table.rowCount()):
            record = self.tab.events_table.item(row, 0).data(0x0100)
            if isinstance(record, dict) and record.get("event_id") == initial["event_id"]:
                self.tab.events_table.selectRow(row)
                _app.processEvents()
                break
        self.assertEqual(initial["review"]["candidate_decision"], "confirmed")
        self.assertEqual(initial["review"]["labels"], [])
        self.assertIn("Initial state is not an appearance event", self.tab._completion_help.text())
        self.assertFalse(self.tab._clarity_combo.isEnabled())
        self.assertEqual(self.tab._clarity_combo.currentData(), "bad")
        self.tab._quality_combo.setCurrentIndex(
            self.tab._quality_combo.findData("good")
        )
        labels = self.store.get(initial["event_id"])["review"]["labels"]
        self.assertEqual(
            [(item["kind"], item["value"]) for item in labels],
            [("surface_quality", "good")],
        )


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
