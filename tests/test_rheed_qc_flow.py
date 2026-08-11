"""Tests for lightweight RHEED adjustment events and GUI wiring.

Run with the GUI environment:

    python -m pytest -q tests/test_rheed_qc_flow.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gui.growth_app import GrowthApp  # noqa: E402
from gui.growth_logger import GrowthLogger  # noqa: E402
from gui.state import CameraState, ClassifierState, RheedQcState  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)


class RheedQcFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.window = GrowthApp()
        self.window.growth_log = GrowthLogger(base_dir=self.tmp.name)
        self.window.growth_log.start_session("QC_FLOW")
        self.window.monitor.set_state("running")
        self.window.monitor.grower_input.setText("QC_TESTER")
        initial = RheedQcState(
            session_active=True,
            view_segment_id=0,
            gun_aligned=True,
            history_required=3,
        )
        self.window._rheed_qc_state = initial
        self.window.monitor.update_rheed_qc_state(initial)
        self.window.monitor.update_camera_state(CameraState(
            frame=np.full((20, 30, 3), 80, dtype=np.uint8),
            frame_number=1,
            connected=True,
            valid=True,
            capture_backend="wgc",
            captured_at_utc="2026-07-29T12:00:00.000Z",
            capture_sequence=10,
            frame_age_ms=4.0,
            source_hwnd=123,
        ))

    def tearDown(self):
        self.window.growth_log.end_session()
        self.window.close()
        self.tmp.cleanup()

    def _rows(self) -> list[dict]:
        path = self.window.growth_log.session_dir / "rheed_view_events.csv"
        with open(path, newline="") as stream:
            return list(csv.DictReader(stream))

    def _event(self, event_type: str, note: str = "") -> None:
        self.window._on_rheed_view_event({
            "event_type": event_type,
            "elapsed_s": 1.25,
            "note": note,
        })

    def test_adjustments_are_events_only_and_qc_remains_explicit(self):
        original = self.window._rheed_qc_state
        self.window.auto_capture_engine.enabled = True
        self._event("sample_direction_adjusted", "rotated azimuth")
        self._event("rheed_current_adjusted")
        self._event("rheed_energy_adjusted")
        self._event("qc_pass")
        self._event("qc_reject", "operator rejected frame")

        state = self.window._rheed_qc_state
        self.assertEqual(state, original)
        self.assertEqual(state.view_segment_id, 0)
        self.assertTrue(state.gun_aligned)
        self.assertTrue(self.window.auto_capture_engine.enabled)

        rows = self._rows()
        self.assertEqual(
            [row["event_type"] for row in rows],
            [
                "sample_direction_adjusted",
                "rheed_current_adjusted",
                "rheed_energy_adjusted",
                "qc_pass",
                "qc_reject",
            ],
        )
        self.assertEqual(rows[0]["frame_role"], "sample_direction_adjusted")
        self.assertEqual(rows[0]["previous_view_segment_id"], "0")
        self.assertEqual(rows[0]["view_segment_id"], "0")
        self.assertEqual(rows[0]["note"], "rotated azimuth")
        self.assertEqual(rows[4]["qc_reject"], "True")
        self.assertEqual(rows[4]["qc_reason"], "operator rejected frame")
        self.assertEqual(rows[4]["labeler"], "QC_TESTER")
        self.assertTrue(Path(rows[0]["frame_path"]).exists())

    def test_adjustment_is_logged_without_a_fresh_frame(self):
        self.window.monitor.update_camera_state(CameraState(
            frame=None,
            connected=False,
            valid=False,
            error="capture unavailable",
        ))
        self._event("rheed_current_adjusted", "operator changed current")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "rheed_current_adjusted")
        self.assertEqual(rows[0]["frame_path"], "")
        self.assertEqual(rows[0]["note"], "operator changed current")

    def test_history_ready_is_logged_once_for_current_segment(self):
        segment = self.window._rheed_qc_state.view_segment_id
        ready = ClassifierState(
            loading=False,
            ready=True,
            view_segment_id=segment,
            visual_history_generation=(
                self.window._rheed_qc_state.visual_history_generation
            ),
            gun_aligned=True,
            history_frame_count=3,
            history_required=3,
            history_ready=True,
            prediction_actionable=True,
        )
        self.window._on_classifier_state(ready)
        self.window._on_classifier_state(ready)
        rows = self._rows()
        self.assertEqual(
            [row["event_type"] for row in rows].count("history_ready"),
            1,
        )
        self.assertTrue(self.window._rheed_qc_state.prediction_actionable)

    def test_heartbeat_writes_each_capture_token_only_once(self):
        frame = np.random.default_rng(7).integers(
            40, 180, size=(120, 140, 3), dtype=np.uint8,
        )
        self.window.monitor.update_camera_state(CameraState(
            frame=frame,
            frame_number=3,
            connected=True,
            valid=True,
            capture_backend="wgc",
            captured_at_utc="2026-07-29T12:00:02.000Z",
            capture_sequence=12,
            frame_age_ms=1.0,
            source_hwnd=123,
            captured_monotonic_ns=0,
            capture_geometry_id="wgc:123:140x120:roi-full:v1",
        ))
        self.window._on_heartbeat()
        self.window._on_heartbeat()

        path = self.window.growth_log.session_dir / "heartbeat_log.csv"
        with open(path, newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["capture_sequence"], "12")
        self.assertEqual(self.window.growth_log._heartbeat_counter, 1)

    def test_stale_same_segment_classifier_generation_is_ignored(self):
        current = self.window._rheed_qc_state
        stale = ClassifierState(
            loading=False,
            ready=True,
            view_segment_id=current.view_segment_id,
            visual_history_generation=(
                current.visual_history_generation - 1
            ),
            gun_aligned=True,
            history_frame_count=32,
            history_required=32,
            history_ready=True,
            prediction_actionable=True,
        )
        self.window._on_classifier_state(stale)
        self.assertFalse(self.window._rheed_qc_state.history_ready)
        self.assertIsNone(self.window._latest_classifier)

    def test_non_wgc_capture_loss_resets_history_once(self):
        generation = (
            self.window._rheed_qc_state.visual_history_generation
        )
        failed = CameraState(
            frame=None,
            connected=False,
            error="Vimba stream stopped",
            mode="vimba",
            capture_backend="vimba",
        )
        self.window._on_camera_state(failed)
        self.window._on_camera_state(failed)
        self.assertEqual(
            self.window._rheed_qc_state.visual_history_generation,
            generation + 1,
        )
        rows = self._rows()
        self.assertEqual(
            [row["event_type"] for row in rows].count("history_reset"),
            1,
        )
        self.assertFalse(self.window.auto_capture_engine.enabled)

    def test_auto_capture_cannot_resume_without_fresh_frame(self):
        self.window._on_camera_state(CameraState(
            frame=None,
            connected=False,
            error="capture unavailable",
            mode="vimba",
        ))
        self.window._on_auto_capture_pause_toggled(False)
        self.assertFalse(self.window.auto_capture_engine.enabled)
        self.assertIn(
            "fresh RHEED frame required",
            self.window.monitor.auto_capture_label.text(),
        )

    def test_stop_and_restart_clear_cached_classifier_scores(self):
        stale = ClassifierState(
            loading=False,
            ready=True,
            smoothed_percent={"1x1": 100},
            has_confident_data=True,
            model_input_mode="single_frame",
        )
        self.window._latest_classifier = stale
        self.window.monitor.update_classifier_state(stale)
        self.window._on_stop()
        self.assertIsNone(self.window._latest_classifier)
        self.assertIsNone(self.window.monitor._latest_classifier)

        self.window._latest_classifier = stale
        self.window.monitor.update_classifier_state(stale)
        self.window.monitor.config_save_path.setText(self.tmp.name)
        self.window.monitor.sample_id_input.setText("QC_RESTART")
        self.window._on_start()
        self.assertIsNone(self.window._latest_classifier)
        self.assertIsNone(self.window.monitor._latest_classifier)

    def test_monitor_emits_three_lightweight_adjustment_events(self):
        payloads: list[dict] = []
        self.window.monitor.rheed_view_event_requested.connect(payloads.append)

        self.window.monitor.rheed_direction_btn.click()
        self.window.monitor.rheed_current_btn.click()
        self.window.monitor.rheed_energy_btn.click()
        self.assertEqual(
            [payload["event_type"] for payload in payloads],
            [
                "sample_direction_adjusted",
                "rheed_current_adjusted",
                "rheed_energy_adjusted",
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
