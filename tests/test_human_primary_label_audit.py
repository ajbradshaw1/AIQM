"""Focused tests for append-only human-primary labels and blind provenance."""
from __future__ import annotations

import csv
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

from gui.equalizer_label_contract import frame_rgb_sha256  # noqa: E402
from gui.events_tab import EventsTab  # noqa: E402
from gui.growth_logger import GrowthLogger  # noqa: E402


def _exact_frame(logger: GrowthLogger) -> tuple[Path, dict[str, object], np.ndarray]:
    assert logger.session_dir is not None
    frame_dir = logger.session_dir / "frames" / "auto_event_001"
    frame_dir.mkdir(parents=True, exist_ok=True)
    rgb = np.zeros((12, 16, 3), dtype=np.uint8)
    rgb[2:8, 4:11] = (80, 190, 120)
    path = frame_dir / "frame_000.bmp"
    Image.fromarray(rgb).save(path, format="BMP")
    metadata: dict[str, object] = {
        "frame_path": path.name,
        "capture_backend": "wgc",
        "captured_at_utc": "2026-08-02T12:00:00.000Z",
        "capture_sequence": 41,
        "view_segment_id": 3,
        "visual_history_generation": 2,
        "session_id": logger.session_dir.name,
        "gun_aligned": True,
        "realignment_active": False,
    }
    return path, metadata, rgb


def _append_label(
    logger: GrowthLogger,
    *,
    event_idx: int,
    frame_path: Path,
    metadata: dict[str, object],
    labeler: str,
    reconstruction: str,
) -> bool:
    provenance = logger.human_primary_provenance(
        frame_path=frame_path, labeler=labeler,
    )
    return logger.update_event_label(
        event_idx,
        human_primary_reconstruction=reconstruction,
        human_label_source=str(provenance["human_label_source"]),
        human_labeler=labeler,
        human_confidence="0.75",
        human_blind_to_model=bool(provenance["human_blind_to_model"]),
        human_blind_to_equalizer=bool(
            provenance["human_blind_to_equalizer"]
        ),
        human_frame_path=str(frame_path),
        human_capture_metadata=metadata,
    )


class HumanPrimaryAuditLoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("PRIMARY_AUDIT")
        self.frame_path, self.metadata, self.rgb = _exact_frame(self.logger)

    def tearDown(self) -> None:
        self.logger.end_session()
        self.tmp.cleanup()

    def _enter_blind(self, labeler: str) -> None:
        attached = self.logger.begin_human_labeling_review(labeler)
        self.assertTrue(attached["audit_trusted"])
        status = self.logger.set_human_blind_labeling_mode(
            True, labeler=labeler,
        )
        self.assertTrue(status["gold_eligible"])

    def test_multiple_judgments_append_and_summary_points_to_latest(self) -> None:
        self._enter_blind("expert-a")
        self.assertTrue(_append_label(
            self.logger,
            event_idx=1,
            frame_path=self.frame_path,
            metadata=self.metadata,
            labeler="expert-a",
            reconstruction="c(6x2)",
        ))
        self._enter_blind("expert-b")
        self.assertTrue(_append_label(
            self.logger,
            event_idx=1,
            frame_path=self.frame_path,
            metadata=self.metadata,
            labeler="expert-b",
            reconstruction="HTR",
        ))

        rows = self.logger.read_human_primary_labels()
        self.assertEqual([row["label_idx"] for row in rows], ["1", "2"])
        self.assertEqual(len({row["annotation_id"] for row in rows}), 2)
        self.assertEqual([row["human_labeler"] for row in rows], [
            "expert-a", "expert-b",
        ])
        self.assertTrue(all(
            row["human_label_source"] == "human_blind_primary"
            for row in rows
        ))
        self.assertEqual(rows[0]["frame_sha256"], frame_rgb_sha256(self.rgb))
        self.assertEqual(rows[0]["capture_sequence"], "41")
        self.assertEqual(rows[0]["view_segment_id"], "3")
        self.assertEqual(rows[0]["capture_backend"], "wgc")
        self.assertEqual(
            rows[0]["captured_at_utc"], "2026-08-02T12:00:00.000Z",
        )
        self.assertEqual(rows[0]["gun_aligned"], "True")
        self.assertEqual(rows[0]["realignment_active"], "False")
        self.assertEqual(rows[0]["acquisition_run"], self.metadata["session_id"])

        summary = self.logger.read_event_labels()[1]
        self.assertEqual(summary["human_primary_reconstruction"], "HTR")
        self.assertEqual(summary["human_primary_annotation_id"], rows[1][
            "annotation_id"
        ])
        self.assertEqual(summary["human_primary_label_idx"], "2")

    def test_blank_labeler_is_rejected_before_audit_file_creation(self) -> None:
        with self.assertRaisesRegex(ValueError, "labeler is required"):
            self.logger.update_event_label(
                2,
                human_primary_reconstruction="none/weak",
                human_label_source="human_assisted_primary",
                human_labeler="",
                human_blind_to_model=False,
                human_blind_to_equalizer=False,
                human_frame_path=str(self.frame_path),
                human_capture_metadata=self.metadata,
            )
        self.assertFalse(
            (self.logger.session_dir / "human_primary_labels.csv").exists()
        )

    def test_exposure_and_restart_fail_closed_to_assisted(self) -> None:
        self._enter_blind("expert-a")
        before = self.logger.human_primary_provenance(
            frame_path=self.frame_path, labeler="expert-a",
        )
        self.assertEqual(before["human_label_source"], "human_blind_primary")

        self.assertTrue(self.logger.record_human_label_output_exposure(
            "classifier", frame_path=self.frame_path,
        ))
        exposed = self.logger.human_primary_provenance(
            frame_path=self.frame_path, labeler="expert-a",
        )
        self.assertEqual(exposed["human_label_source"], "human_assisted_primary")
        self.assertFalse(exposed["human_blind_to_model"])

        # A new logger has a new runtime ID. Persisted active=True from a
        # previous process would still not prove blindness in this runtime.
        second_tmp = tempfile.TemporaryDirectory()
        try:
            second = GrowthLogger(base_dir=second_tmp.name)
            second._session_dir = self.logger.session_dir
            restarted = second.human_primary_provenance(
                frame_path=self.frame_path, labeler="expert-a",
            )
            self.assertEqual(
                restarted["human_label_source"], "human_assisted_primary",
            )
        finally:
            second_tmp.cleanup()


class HumanPrimaryAuditEventsTabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("PRIMARY_UI")
        self.frame_path, self.metadata, _rgb = _exact_frame(self.logger)
        self.tab = EventsTab()

    def tearDown(self) -> None:
        self.tab.deleteLater()
        self.logger.end_session()
        self.tmp.cleanup()

    def _bind_exact_frame(self) -> None:
        self.tab._currently_displayed_event_idx = 1
        self.tab._cached_paths = [self.frame_path]
        self.tab._capture_metadata_by_filename = {
            self.frame_path.name: self.metadata,
        }
        self.tab._slider.setRange(0, 0)

    def test_blank_labeler_shows_warning_and_never_saves(self) -> None:
        self.tab.attach_session(self.logger, labeler="")
        self._bind_exact_frame()
        label_idx = self.tab._primary_recon_combo.findData("c(6x2)")
        with patch.object(QMessageBox, "warning") as warning:
            self.tab._on_primary_recon_activated(label_idx)
        self.assertTrue(warning.called)
        self.assertIn("Labeler required", warning.call_args.args[1])
        self.assertEqual(self.logger.read_human_primary_labels(), [])

    def test_explicit_blind_mode_hides_outputs_and_saves_gold(self) -> None:
        self.tab.attach_session(self.logger, labeler="expert-a")
        self._bind_exact_frame()
        self.tab._on_blind_labeling_toggled(True)
        self.assertTrue(self.tab._blind_labeling_mode)
        self.assertFalse(self.tab._classify_button.isEnabled())
        self.assertFalse(self.tab._equalizer_button.isEnabled())
        self.assertFalse(self.tab._classifier_result_label.isVisible())

        label_idx = self.tab._primary_recon_combo.findData("c(6x2)")
        self.tab._on_primary_recon_activated(label_idx)
        rows = self.logger.read_human_primary_labels()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["human_label_source"], "human_blind_primary")

    def test_persisted_exposure_forces_assisted_annotation(self) -> None:
        self.tab.attach_session(self.logger, labeler="expert-a")
        self._bind_exact_frame()
        self.tab.record_equalizer_output_visible(self.frame_path)
        self.tab._on_blind_labeling_toggled(True)
        self.assertTrue(self.tab._blind_labeling_mode)
        self.assertIn("assisted", self.tab._blind_mode_status.text())

        label_idx = self.tab._primary_recon_combo.findData("HTR")
        self.tab._on_primary_recon_activated(label_idx)
        rows = self.logger.read_human_primary_labels()
        self.assertEqual(rows[0]["human_label_source"], "human_assisted_primary")
        self.assertEqual(rows[0]["human_blind_to_equalizer"], "False")


if __name__ == "__main__":
    unittest.main(verbosity=2)
