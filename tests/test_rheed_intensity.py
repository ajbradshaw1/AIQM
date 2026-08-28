"""Offline tests for capture-bound RHEED ROI intensity monitoring."""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

from gui.equalizer_alignment import LandmarkDetection  # noqa: E402
from gui.growth_logger import GrowthLogger  # noqa: E402
from gui.rheed_intensity import (  # noqa: E402
    INTENSITY_METRIC,
    NormalizedRoi,
    auto_three_spot_roi_definition,
    frame_luminance_plane,
    manual_roi_definition,
    measure_camera_state,
    measure_regions,
)
from gui.rheed_intensity_window import RheedIntensityWindow  # noqa: E402
from gui.state import CameraState  # noqa: E402


def _state(
    frame: np.ndarray,
    *,
    sequence: int = 1,
    geometry: str = "test-geometry",
    backend: str = "dummy",
    hwnd: int = 0,
) -> CameraState:
    now_ns = time.perf_counter_ns()
    return CameraState(
        frame=np.array(frame, copy=True),
        frame_number=sequence,
        width=frame.shape[1],
        height=frame.shape[0],
        connected=True,
        valid=True,
        mode=backend,
        capture_backend=backend,
        captured_at_utc=f"2026-08-06T12:00:{sequence:02d}.000Z",
        received_at_utc=f"2026-08-06T12:00:{sequence:02d}.000Z",
        capture_sequence=sequence,
        sample_sequence=sequence,
        source_hwnd=hwnd,
        capture_geometry_id=geometry,
        captured_monotonic_ns=now_ns,
        received_monotonic_ns=now_ns,
    )


class RheedIntensityMathTests(unittest.TestCase):
    def test_grayscale_rectangle_exact_sum(self) -> None:
        frame = np.arange(16, dtype=np.uint8).reshape(4, 4)
        total, mean, count = measure_regions(
            frame,
            (NormalizedRoi(0.25, 0.25, 0.75, 0.75),),
        )
        expected = float(frame[1:3, 1:3].sum())
        self.assertEqual(total, expected)
        self.assertEqual(count, 4)
        self.assertEqual(mean, expected / 4)

    def test_rgb_uses_bt601_not_green_only(self) -> None:
        frame = np.array([[[100, 20, 50], [0, 255, 0]]], dtype=np.uint8)
        luminance = frame_luminance_plane(frame)
        np.testing.assert_allclose(
            luminance,
            [[0.299 * 100 + 0.587 * 20 + 0.114 * 50, 0.587 * 255]],
        )
        total, mean, count = measure_regions(
            frame,
            (NormalizedRoi(0.0, 0.0, 1.0, 1.0),),
        )
        self.assertAlmostEqual(total, float(luminance.sum()), places=10)
        self.assertAlmostEqual(mean, float(luminance.mean()), places=10)
        self.assertEqual(count, 2)

    def test_overlapping_three_boxes_use_union(self) -> None:
        frame = np.ones((10, 10), dtype=np.uint8)
        regions = (
            NormalizedRoi(0.1, 0.1, 0.6, 0.6),
            NormalizedRoi(0.4, 0.4, 0.9, 0.9),
            NormalizedRoi(0.2, 0.2, 0.5, 0.5),
        )
        total, mean, count = measure_regions(frame, regions)
        mask = np.zeros((10, 10), dtype=bool)
        for region in regions:
            x0, y0, x1, y1 = region.pixel_bounds(10, 10)
            mask[y0:y1, x0:x1] = True
        self.assertEqual(count, int(mask.sum()))
        self.assertEqual(total, float(mask.sum()))
        self.assertEqual(mean, 1.0)

    def test_auto_definition_maps_three_processed_points(self) -> None:
        frame = np.zeros((192, 256, 3), dtype=np.uint8)
        state = _state(frame)
        detected = LandmarkDetection.detected(
            np.array([[24.0, 48.0], [64.0, 30.0], [104.0, 48.0]]),
            method="test-three-spots",
            confidence=0.8,
            peak_snr=12.0,
        )
        with patch("gui.rheed_intensity.detect_live_landmarks", return_value=detected):
            roi = auto_three_spot_roi_definition(state, frame, box_size_processed_px=7)
        self.assertEqual(roi.mode, "auto_1x1_three_spot")
        self.assertEqual(len(roi.regions), 3)
        self.assertEqual(roi.landmark_points_processed[1], (64.0, 30.0))
        center = roi.regions[1]
        self.assertAlmostEqual((center.left + center.right) / 2, 64.5 / 128)
        self.assertAlmostEqual((center.top + center.bottom) / 2, 30.5 / 96)

    def test_blank_frame_auto_detection_fails_explicitly(self) -> None:
        frame = np.zeros((96, 128), dtype=np.uint8)
        with self.assertRaisesRegex(ValueError, "detection failed"):
            auto_three_spot_roi_definition(_state(frame), frame)

    def test_geometry_change_is_rejected(self) -> None:
        frame = np.full((20, 30), 10, dtype=np.uint8)
        state = _state(frame, geometry="geometry-a")
        roi = manual_roi_definition((0.1, 0.1, 0.5, 0.5), state, frame)
        changed = _state(frame, sequence=2, geometry="geometry-b")
        self.assertEqual(roi.compatibility_error(changed, frame), "capture geometry changed")
        with self.assertRaisesRegex(ValueError, "capture geometry changed"):
            measure_camera_state(changed, roi)


class RheedIntensityWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = RheedIntensityWindow()

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()
        _app.processEvents()

    def test_manual_roi_emits_once_per_capture_and_invalidates_on_resize(self) -> None:
        samples = []
        events = []
        self.window.measurement_ready.connect(samples.append)
        self.window.roi_event.connect(
            lambda event, roi, reason: events.append((event, roi, reason))
        )
        state = _state(np.full((40, 60, 3), 20, dtype=np.uint8))
        self.window.on_camera_state(state)
        self.window._start_manual_selection()
        self.window._on_manual_region((0.1, 0.2, 0.5, 0.7))
        self.assertEqual(len(samples), 1)
        self.assertEqual(events[0][0], "defined")

        self.window.on_camera_state(state)
        self.assertEqual(len(samples), 1, "duplicate capture must not append")

        second = _state(state.frame, sequence=2)
        self.window.on_camera_state(second)
        self.assertEqual(len(samples), 2)

        resized = _state(np.full((41, 60, 3), 20, dtype=np.uint8), sequence=3)
        self.window.on_camera_state(resized)
        self.assertIsNone(self.window.active_roi)
        self.assertEqual(events[-1][0], "invalidated")
        self.assertIn("height", events[-1][2])

    def test_drag_selection_maps_letterboxed_view_to_frame_coordinates(self) -> None:
        self.window.resize(1000, 520)
        self.window.show()
        frame = np.full((100, 200, 3), 40, dtype=np.uint8)
        self.window.on_camera_state(_state(frame))
        _app.processEvents()
        selected = []
        self.window.image_view.region_selected.connect(selected.append)
        self.assertTrue(self.window.image_view.start_selection())

        start = self.window.image_view.mapFromScene(QPointF(20.0, 10.0))
        end = self.window.image_view.mapFromScene(QPointF(100.0, 50.0))
        QTest.mousePress(
            self.window.image_view.viewport(),
            Qt.MouseButton.LeftButton,
            pos=start,
        )
        QTest.mouseMove(self.window.image_view.viewport(), end)
        QTest.mouseRelease(
            self.window.image_view.viewport(),
            Qt.MouseButton.LeftButton,
            pos=end,
        )
        _app.processEvents()
        self.assertEqual(len(selected), 1)
        left, top, right, bottom = selected[0]
        self.assertAlmostEqual(left, 0.10, delta=0.015)
        self.assertAlmostEqual(top, 0.10, delta=0.015)
        self.assertAlmostEqual(right, 0.50, delta=0.015)
        self.assertAlmostEqual(bottom, 0.50, delta=0.015)

    def test_three_spot_candidate_requires_explicit_accept(self) -> None:
        frame = np.zeros((96, 128, 3), dtype=np.uint8)
        state = _state(frame)
        self.window.on_camera_state(state)
        # Construct the correct three-region mode through the public builder.
        detected = LandmarkDetection.detected(
            np.array([[25.0, 50.0], [64.0, 30.0], [103.0, 50.0]]),
            method="test",
            confidence=0.5,
            peak_snr=8.0,
        )
        with patch("gui.rheed_intensity.detect_live_landmarks", return_value=detected):
            self.window._detect_three_spots()
        self.assertIsNone(self.window.active_roi)
        self.assertTrue(self.window.accept_btn.isEnabled())
        self.window._accept_candidate()
        self.assertIsNotNone(self.window.active_roi)
        self.assertEqual(self.window.active_roi.mode, "auto_1x1_three_spot")


class RheedIntensityLoggerTests(unittest.TestCase):
    def test_session_writes_definition_and_unique_capture_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = GrowthLogger(directory)
            logger.start_session("ROI")
            frame = np.full((20, 30, 3), 25, dtype=np.uint8)
            state = _state(frame, sequence=7)
            roi = manual_roi_definition((0.1, 0.1, 0.8, 0.8), state, frame)
            sample = measure_camera_state(state, roi)
            self.assertTrue(logger.record_rheed_roi_definition("defined", roi))
            self.assertTrue(logger.record_rheed_roi_intensity(
                sample,
                elapsed_s=1.25,
                view_segment_id=2,
                visual_history_generation=3,
            ))
            self.assertFalse(logger.record_rheed_roi_intensity(
                sample,
                elapsed_s=1.25,
                view_segment_id=2,
                visual_history_generation=3,
            ))
            session_dir = logger.session_dir
            logger.end_session()

            with (session_dir / "rheed_roi_intensity.csv").open(
                newline="", encoding="utf-8",
            ) as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["capture_sequence"], "7")
            self.assertEqual(rows[0]["intensity_metric"], INTENSITY_METRIC)
            self.assertEqual(rows[0]["view_segment_id"], "2")
            self.assertEqual(len(json.loads(rows[0]["regions_normalized_json"])), 1)

            definitions = [
                json.loads(line)
                for line in (session_dir / "rheed_roi_definitions.jsonl")
                .read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(definitions), 1)
            self.assertEqual(definitions[0]["event"], "defined")
            self.assertEqual(
                definitions[0]["roi"]["roi_definition_id"],
                roi.roi_definition_id,
            )

    def test_roi_and_point_event_share_one_session_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = GrowthLogger(directory)
            logger.start_session("ROI_POINT_EVENT")
            frame = np.full((20, 30, 3), 25, dtype=np.uint8)
            state = _state(frame, sequence=8)
            roi = manual_roi_definition((0.1, 0.1, 0.8, 0.8), state, frame)
            sample = measure_camera_state(state, roi)
            self.assertTrue(logger.record_rheed_roi_definition("defined", roi))
            self.assertTrue(logger.record_rheed_roi_intensity(
                sample,
                elapsed_s=1.25,
                view_segment_id=2,
                visual_history_generation=3,
            ))
            capture_metadata = {
                "capture_backend": state.capture_backend,
                "captured_at_utc": state.captured_at_utc,
                "capture_sequence": state.capture_sequence,
                "frame_age_ms": state.frame_age_ms,
                "source_hwnd": state.source_hwnd,
                "captured_monotonic_ns": state.captured_monotonic_ns,
                "capture_geometry_id": state.capture_geometry_id,
            }
            self.assertEqual(logger.record_manual_event(
                elapsed_s=1.5,
                frame=frame,
                note="combined lifecycle",
                capture_metadata=capture_metadata,
                event_at_utc=state.captured_at_utc,
            ), 1)
            event_id = logger.last_point_event_id
            self.assertTrue(event_id)
            session_dir = logger.session_dir

            logger.end_session()

            with (session_dir / "rheed_roi_intensity.csv").open(
                newline="", encoding="utf-8",
            ) as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 1)
            self.assertTrue(
                (session_dir / "rheed_roi_definitions.jsonl").read_text(
                    encoding="utf-8",
                ).strip()
            )
            summary = json.loads(
                (session_dir / "rheed_point_events.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual(
                [event["event_id"] for event in summary["events"]],
                [event_id],
            )
            self.assertTrue(
                (session_dir / "rheed_event_revisions.jsonl").read_text(
                    encoding="utf-8",
                ).strip()
            )


if __name__ == "__main__":
    unittest.main()
