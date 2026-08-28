"""Focused Qt tests for the v2 point-event review surface."""

from __future__ import annotations

import csv
import os
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

from gui.events_tab import COL_EVENT_ID, COLUMN_HEADERS, EventsTab  # noqa: E402
from gui.growth_logger import GrowthLogger  # noqa: E402
from gui.rheed_point_events import make_review_anchor, sha256_file  # noqa: E402


class EventFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("EVENT_UI")
        assert self.logger.session_dir is not None
        assert self.logger.point_event_store is not None
        self.store = self.logger.point_event_store
        self.session_dir = self.logger.session_dir
        self.event_dir = self.session_dir / "frames" / "manual_event_001"
        self.event_dir.mkdir(parents=True)
        self.frames: list[Path] = []
        for index, value in enumerate((40, 160)):
            frame = np.full((12, 16, 3), value, dtype=np.uint8)
            path = self.event_dir / f"buf_{index:02d}.bmp"
            Image.fromarray(frame).save(path, format="BMP")
            self.frames.append(path)
        with open(self.event_dir / "capture_manifest.csv", "w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=[
                "frame_path", "capture_sequence", "captured_at_utc",
                "elapsed_s", "view_segment_id", "capture_geometry_id",
            ])
            writer.writeheader()
            writer.writerow({
                "frame_path": self.frames[0].name,
                "capture_sequence": "100",
                "captured_at_utc": "2026-08-20T12:00:10+00:00",
                "elapsed_s": "10.0",
                "view_segment_id": "1",
                "capture_geometry_id": "geometry-a",
            })
            writer.writerow({
                "frame_path": self.frames[1].name,
                "capture_sequence": "101",
                "captured_at_utc": "2026-08-20T12:00:11+00:00",
                "elapsed_s": "11.0",
                "view_segment_id": "1",
                "capture_geometry_id": "geometry-a",
            })
        self.source_row = {
            "timestamp": "2026-08-20T12:00:10+00:00",
            "elapsed_s": "10.0",
            "event_idx": "1",
            "change_score": "0.83",
            "pyrometer_temp_C": "715.0",
            "buffer_dir": "frames/auto_event_001",
            "event_state": "pending",
        }
        with open(self.session_dir / "manual_events.csv", "w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(self.source_row))
            writer.writeheader()
            writer.writerow(self.source_row)
        anchor = make_review_anchor(
            frame_path=self.frames[0], image_sha256=sha256_file(self.frames[0]),
            capture_sequence=100, captured_at_utc="2026-08-20T12:00:10+00:00",
            elapsed_s=10.0, view_segment_id=1, capture_geometry_id="geometry-a",
        )
        state = self.store.create_event(
            source_kind="manual", actor="grower-a",
            session_identity=self.session_dir.name,
            source_file="manual_events.csv", source_index=1,
            source_row=self.source_row,
            original_at_utc=self.source_row["timestamp"], original_elapsed_s=10.0,
            capture_sequence=100, original_frame_path=str(self.frames[0]),
            original_image_sha256=sha256_file(self.frames[0]), review_anchor=anchor,
        )
        self.event_id = str(state["event_id"])
        self.tab = EventsTab()

    def tearDown(self) -> None:
        self.tab.deleteLater()
        self.logger.end_session()
        self.tmp.cleanup()

    def attach_and_select(self) -> None:
        self.tab.attach_session(self.logger, labeler="grower-a")
        for row in range(self.tab.events_table.rowCount()):
            item = self.tab.events_table.item(row, COL_EVENT_ID)
            record = item.data(Qt.ItemDataRole.UserRole)
            if record.get("event_id") == self.event_id:
                self.tab.events_table.selectRow(row)
                _app.processEvents()
                return
        self.fail("point event was not rendered")


class EventsTabConstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tab = EventsTab()

    def tearDown(self) -> None:
        self.tab.deleteLater()

    def test_simple_point_event_controls_replace_legacy_form(self) -> None:
        self.assertEqual(self.tab.events_table.columnCount(), len(COLUMN_HEADERS))
        for name in (
            "_labeler_input", "_reconstruction_change_combos", "_clarity_combo",
            "_quality_combo", "_representative_button",
        ):
            self.assertTrue(hasattr(self.tab, name), name)
        for removed in (
            "_primary_recon_combo", "_change_from_combo", "_change_to_combo",
            "_classifier_result_label", "_equalizer_button", "_equalizer_window",
            "_reviewer_input", "_confidence_combo", "_add_label_button",
            "_complete_button", "_reopen_button",
        ):
            self.assertFalse(hasattr(self.tab, removed), removed)
        self.assertTrue(self.tab.events_table.isColumnHidden(COL_EVENT_ID))

    def test_constructs_without_session_and_emits_badge(self) -> None:
        self.assertIsNone(self.tab._session_dir)
        received: list[int] = []
        self.tab.unreviewed_count_changed.connect(received.append)
        self.tab._refresh_unreviewed_badge()
        self.assertEqual(received[-1], 0)


class EventsTabLifecycleTests(EventFixture):
    def test_attach_reads_point_journal_and_saved_frames(self) -> None:
        self.attach_and_select()
        self.assertEqual(self.tab._currently_displayed_event_id, self.event_id)
        self.assertEqual(self.tab._currently_displayed_event_idx, 1)
        self.assertEqual(self.tab._cached_paths, self.frames)
        self.assertIn("registered change score 0.830", self.tab._metadata_label.text())
        self.assertIn("immutable", self.tab._source_position_label.text())

    def test_reload_preserves_stable_event_selection(self) -> None:
        self.attach_and_select()
        self.tab._reload_events()
        self.assertEqual(self.tab._currently_displayed_event_id, self.event_id)
        selected = self.tab.events_table.item(
            self.tab.events_table.currentRow(), COL_EVENT_ID,
        ).data(Qt.ItemDataRole.UserRole)
        self.assertEqual(selected["event_id"], self.event_id)

    def test_autosave_workflow_does_not_show_unfinished_badge(self) -> None:
        received: list[int] = []
        self.tab.unreviewed_count_changed.connect(received.append)
        self.tab.attach_session(self.logger, labeler="grower-a")
        self.assertEqual(received[-1], 0)
        self.assertEqual(self.tab.events_table.rowCount(), 1)

    def test_automatic_proposals_are_not_labeling_tasks(self) -> None:
        state = self.store.get(self.event_id)
        state["source"]["kind"] = "auto_capture"
        self.tab._event_states = {self.event_id: state}
        self.tab._refresh_table()
        self.assertEqual(self.tab.events_table.rowCount(), 0)

    def test_reference_hardware_event_is_visible_and_read_only(self) -> None:
        row = {
            "timestamp": "2026-08-20T12:00:12+00:00", "elapsed_s": "12.0",
            "event_idx": "1", "event_type": "rheed_energy_adjusted",
            "frame_path": "",
        }
        with open(self.session_dir / "rheed_view_events.csv", "w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        self.tab.attach_session(self.logger, labeler="grower-a")
        for table_row in range(self.tab.events_table.rowCount()):
            record = self.tab.events_table.item(
                table_row, COL_EVENT_ID,
            ).data(Qt.ItemDataRole.UserRole)
            if record["kind"] == "reference":
                self.tab.events_table.selectRow(table_row)
                _app.processEvents()
                break
        self.assertTrue(self.tab._label_box.isHidden())
        self.assertIn("read-only acquisition evidence", self.tab._metadata_label.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
