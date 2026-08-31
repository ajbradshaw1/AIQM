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
    AUTO_TRIPLET_LABELS,
    INTENSITY_METRIC,
    MAX_ROI_REGIONS,
    NormalizedRoi,
    RheedRoiDefinition,
    auto_peak_roi_definition,
    auto_three_spot_roi_definition,
    definition_from_mapping,
    frame_luminance_plane,
    manual_roi_definition,
    measure_camera_state,
    measure_region_series,
    measure_regions,
    without_last_region,
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
    exposure_us: float | None = None,
    exposure_generation: int = 0,
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
        exposure_us=exposure_us,
        exposure_generation=exposure_generation,
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


class MultiRegionRoiTests(unittest.TestCase):
    """Per-box tracking: N labelled rectangles, each with its own series."""

    def _frame(self) -> np.ndarray:
        # Two bright patches on a dark field, so each box has a distinct sum.
        frame = np.zeros((40, 60, 3), dtype=np.uint8)
        frame[5:15, 5:15] = 100
        frame[5:15, 30:40] = 200
        return frame

    def test_manual_append_adds_a_box_and_keeps_existing_labels(self) -> None:
        frame = self._frame()
        state = _state(frame)
        first = manual_roi_definition((0.05, 0.10, 0.30, 0.40), state, frame)
        self.assertEqual(first.region_labels, ("box 1",))

        second = manual_roi_definition(
            (0.45, 0.10, 0.70, 0.40), state, frame, append_to=first,
        )
        self.assertEqual(len(second.regions), 2)
        self.assertEqual(second.region_labels, ("box 1", "box 2"))
        self.assertEqual(second.regions[0], first.regions[0])
        self.assertNotEqual(
            second.roi_definition_id, first.roi_definition_id,
            "appending must mint a new ID so old samples stay attributable",
        )

    def test_appending_to_the_auto_triplet_preserves_spot_names(self) -> None:
        frame = np.zeros((96, 128, 3), dtype=np.uint8)
        state = _state(frame)
        detected = LandmarkDetection.detected(
            np.array([[25.0, 50.0], [64.0, 30.0], [103.0, 50.0]]),
            method="test", confidence=0.5, peak_snr=8.0,
        )
        with patch("gui.rheed_intensity.detect_live_landmarks", return_value=detected):
            auto = auto_three_spot_roi_definition(state, frame)
        self.assertEqual(auto.region_labels, AUTO_TRIPLET_LABELS)

        combined = manual_roi_definition(
            (0.80, 0.80, 0.95, 0.95), state, frame, append_to=auto,
        )
        # "specular" must survive: per-region rows are filed under the label,
        # so renaming it would splice two different boxes into one trace.
        self.assertEqual(
            combined.region_labels, AUTO_TRIPLET_LABELS + ("box 1",),
        )

    def test_append_refuses_past_the_region_ceiling(self) -> None:
        frame = self._frame()
        state = _state(frame)
        roi = manual_roi_definition((0.01, 0.01, 0.05, 0.05), state, frame)
        for index in range(1, MAX_ROI_REGIONS):
            left = 0.01 + index * 0.1
            roi = manual_roi_definition(
                (left, 0.01, left + 0.05, 0.05), state, frame, append_to=roi,
            )
        self.assertEqual(len(roi.regions), MAX_ROI_REGIONS)
        with self.assertRaises(ValueError) as ctx:
            manual_roi_definition((0.9, 0.9, 0.95, 0.95), state, frame, append_to=roi)
        self.assertIn("remove one", str(ctx.exception))

    def test_append_refuses_when_the_geometry_moved_under_it(self) -> None:
        frame = self._frame()
        roi = manual_roi_definition((0.05, 0.1, 0.3, 0.4), _state(frame), frame)
        taller = np.zeros((41, 60, 3), dtype=np.uint8)
        with self.assertRaises(ValueError) as ctx:
            manual_roi_definition(
                (0.5, 0.1, 0.7, 0.4), _state(taller, sequence=2), taller,
                append_to=roi,
            )
        self.assertIn("height", str(ctx.exception))

    def test_per_box_series_is_independent_of_the_union(self) -> None:
        frame = self._frame()
        state = _state(frame)
        roi = manual_roi_definition((0.05, 0.10, 0.30, 0.40), state, frame)
        roi = manual_roi_definition(
            (0.45, 0.10, 0.70, 0.40), state, frame, append_to=roi,
        )
        sample = measure_camera_state(state, roi)
        self.assertEqual(len(sample.regions), 2)
        dim, bright = sample.regions
        # The 200-valued patch must read higher than the 100-valued one; a
        # union-only metric could not tell them apart at all.
        self.assertGreater(bright.intensity_mean, dim.intensity_mean)
        # Non-overlapping boxes: the union is exactly their sum.
        self.assertAlmostEqual(
            sample.intensity_sum,
            dim.intensity_sum + bright.intensity_sum,
            places=6,
        )
        self.assertEqual(
            sample.pixel_count, dim.pixel_count + bright.pixel_count,
        )

    def test_overlapping_boxes_count_shared_pixels_once_only_in_the_union(self) -> None:
        frame = np.full((40, 60, 3), 50, dtype=np.uint8)
        state = _state(frame)
        roi = manual_roi_definition((0.1, 0.1, 0.5, 0.5), state, frame)
        roi = manual_roi_definition(
            (0.3, 0.3, 0.7, 0.7), state, frame, append_to=roi,
        )
        sample = measure_camera_state(state, roi)
        first, second = sample.regions
        # Each box owns its overlap — "what is happening inside THIS box" is a
        # different question from "how much unique area is lit".
        self.assertLess(
            sample.pixel_count, first.pixel_count + second.pixel_count,
        )
        self.assertLess(
            sample.intensity_sum, first.intensity_sum + second.intensity_sum,
        )

    def test_remove_last_box_keeps_the_other_labels_stable(self) -> None:
        frame = self._frame()
        state = _state(frame)
        roi = manual_roi_definition((0.05, 0.10, 0.30, 0.40), state, frame)
        roi = manual_roi_definition(
            (0.45, 0.10, 0.70, 0.40), state, frame, append_to=roi,
        )
        roi = manual_roi_definition(
            (0.75, 0.10, 0.90, 0.40), state, frame, append_to=roi,
        )
        trimmed = without_last_region(roi, state, frame)
        self.assertEqual(trimmed.region_labels, ("box 1", "box 2"))
        self.assertEqual(trimmed.regions, roi.regions[:2])

        one_left = without_last_region(trimmed, state, frame)
        with self.assertRaises(ValueError):
            without_last_region(one_left, state, frame)

    def test_removing_from_the_auto_triplet_drops_detector_provenance(self) -> None:
        frame = np.zeros((96, 128, 3), dtype=np.uint8)
        state = _state(frame)
        detected = LandmarkDetection.detected(
            np.array([[25.0, 50.0], [64.0, 30.0], [103.0, 50.0]]),
            method="test", confidence=0.5, peak_snr=8.0,
        )
        with patch("gui.rheed_intensity.detect_live_landmarks", return_value=detected):
            auto = auto_three_spot_roi_definition(state, frame)
        trimmed = without_last_region(auto, state, frame)
        # A curated subset is no longer the detector's proposal, so it must
        # not keep claiming to be one.
        self.assertEqual(trimmed.mode, "manual_rect")
        self.assertEqual(trimmed.detector_method, "")
        self.assertIsNone(trimmed.detector_confidence)
        self.assertEqual(trimmed.region_labels, ("left", "specular"))

    def test_duplicate_region_labels_are_refused(self) -> None:
        region = NormalizedRoi(0.1, 0.1, 0.2, 0.2)
        with self.assertRaises(ValueError) as ctx:
            RheedRoiDefinition(
                roi_definition_id="x", mode="manual_rect",
                regions=(region, region), region_labels=("a", "a"),
                capture_backend="dummy", source_hwnd=0,
                capture_geometry_id="g", frame_width=10, frame_height=10,
                definition_capture_sequence=1, definition_captured_at_utc="",
            )
        self.assertIn("unique", str(ctx.exception))

    def test_schema_v1_payload_without_labels_still_deserializes(self) -> None:
        """Sessions logged before per-box tracking must stay readable."""
        payload = {
            "roi_definition_id": "rheed-roi-legacy",
            "mode": "auto_1x1_three_spot",
            "regions_normalized": [
                {"left": 0.1, "top": 0.1, "right": 0.2, "bottom": 0.2},
                {"left": 0.4, "top": 0.1, "right": 0.5, "bottom": 0.2},
                {"left": 0.7, "top": 0.1, "right": 0.8, "bottom": 0.2},
            ],
            "capture_backend": "wgc",
            "source_hwnd": 12,
            "capture_geometry_id": "g",
            "frame_width": 656,
            "frame_height": 492,
            "definition_capture_sequence": 3,
            "definition_captured_at_utc": "2026-08-06T12:00:00.000Z",
        }
        roi = definition_from_mapping(payload)
        self.assertEqual(len(roi.regions), 3)
        self.assertEqual(roi.region_labels, AUTO_TRIPLET_LABELS)

    def test_measure_region_series_reports_relative_change_per_box(self) -> None:
        frame = self._frame()
        state = _state(frame)
        roi = manual_roi_definition((0.05, 0.10, 0.30, 0.40), state, frame)
        roi = manual_roi_definition(
            (0.45, 0.10, 0.70, 0.40), state, frame, append_to=roi,
        )
        first = measure_region_series(frame, roi)
        baselines = {region.label: region.intensity_sum for region in first}

        brighter = frame.copy()
        brighter[5:15, 30:40] = 220
        second = measure_region_series(brighter, roi, baseline_sums=baselines)
        # Only the box that changed reports a change.
        self.assertAlmostEqual(second[0].relative_change_pct, 0.0, places=6)
        self.assertGreater(second[1].relative_change_pct, 0.0)


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


class AutoPeakRoiTests(unittest.TestCase):
    """The general N-spot proposer, beside the 1x1 triplet detector."""

    @staticmethod
    def _spots(centres, *, shape=(96, 128)) -> np.ndarray:
        h, w = shape
        yy, xx = np.mgrid[0:h, 0:w]
        img = np.full((h, w), 8.0)
        for cx, cy, amp in centres:
            img += amp * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 3.0 ** 2)))
        return np.clip(img, 0, 255).astype(np.uint8)

    def test_proposes_the_requested_number_of_boxes_brightest_first(self) -> None:
        frame = self._spots([(30, 48, 200), (64, 40, 240), (98, 48, 160)])
        roi = auto_peak_roi_definition(_state(frame), frame, count=3)
        self.assertEqual(roi.mode, "auto_peaks")
        self.assertEqual(len(roi.regions), 3)
        self.assertEqual(roi.region_labels, ("spot 1", "spot 2", "spot 3"))
        self.assertEqual(roi.detector_method, "auto-peaks-2d")
        # spot 1 is the brightest peak, which here is the centre one.
        centre_x = (roi.regions[0].left + roi.regions[0].right) / 2.0
        self.assertAlmostEqual(centre_x, 64.5 / 128, delta=0.05)

    def test_accepts_vertically_stacked_spots(self) -> None:
        """The x-separation rule belongs to the 1x1 row detector, not here.

        Two spots in the same column are a legitimate pair of regions; the
        triplet detector rejects them because a 1x1 pattern is a horizontal
        row, and applying that rule generally would refuse valid ROIs.
        """
        frame = self._spots([(64, 24, 220), (64, 68, 200)])
        roi = auto_peak_roi_definition(_state(frame), frame, count=2)
        self.assertEqual(len(roi.regions), 2)
        xs = [(r.left + r.right) / 2.0 for r in roi.regions]
        self.assertAlmostEqual(xs[0], xs[1], delta=0.03)

    def test_asking_for_more_spots_than_exist_says_so(self) -> None:
        frame = self._spots([(30, 48, 220), (98, 48, 200)])
        with self.assertRaises(ValueError) as ctx:
            auto_peak_roi_definition(_state(frame), frame, count=5)
        self.assertIn("Found only", str(ctx.exception))
        self.assertIn("by hand", str(ctx.exception))

    def test_a_flat_frame_is_refused_on_contrast(self) -> None:
        frame = np.full((96, 128), 30, dtype=np.uint8)
        with self.assertRaises(ValueError) as ctx:
            auto_peak_roi_definition(_state(frame), frame, count=2)
        self.assertIn("contrast", str(ctx.exception))

    def test_count_is_bounded_by_the_region_ceiling(self) -> None:
        frame = self._spots([(30, 48, 220)])
        for bad in (0, MAX_ROI_REGIONS + 1):
            with self.assertRaises(ValueError) as ctx:
                auto_peak_roi_definition(_state(frame), frame, count=bad)
            self.assertIn("between 1 and", str(ctx.exception))

    def test_the_triplet_detector_still_owns_the_named_geometry(self) -> None:
        """auto_peaks must not quietly become the 1x1 detector.

        The triplet path validates left/specular/right ordering and rejects
        collinear triples; auto_peaks makes no such claim, and its labels say
        so.
        """
        frame = self._spots([(30, 48, 200), (64, 40, 240), (98, 48, 160)])
        state = _state(frame)
        triplet = auto_three_spot_roi_definition(state, frame)
        peaks = auto_peak_roi_definition(state, frame, count=3)
        self.assertEqual(triplet.region_labels, AUTO_TRIPLET_LABELS)
        self.assertEqual(peaks.region_labels, ("spot 1", "spot 2", "spot 3"))
        self.assertEqual(len(triplet.landmark_points_processed), 3)
        self.assertEqual(peaks.landmark_points_processed, ())


class RheedIntensityWindowMultiRegionTests(unittest.TestCase):
    """The trend window's side of per-box tracking and exposure breaks."""

    def setUp(self) -> None:
        self.window = RheedIntensityWindow()

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()
        _app.processEvents()

    def _frame(self) -> np.ndarray:
        frame = np.zeros((40, 60, 3), dtype=np.uint8)
        frame[5:15, 5:15] = 100
        frame[5:15, 30:40] = 200
        return frame

    def _define_two_boxes(self) -> None:
        state = _state(self._frame(), exposure_us=300_000.0)
        self.window.on_camera_state(state)
        self.window._start_manual_selection()
        self.window._on_manual_region((0.05, 0.10, 0.30, 0.40))
        self.window._start_append_selection()
        self.window._on_manual_region((0.45, 0.10, 0.70, 0.40))

    def test_adding_a_box_creates_a_second_curve(self) -> None:
        self._define_two_boxes()
        roi = self.window.active_roi
        self.assertEqual(len(roi.regions), 2)
        self.assertEqual(
            set(self.window._region_curves), {"box 1", "box 2"},
        )
        self.assertTrue(self.window.remove_btn.isEnabled())

    def test_a_single_box_draws_no_duplicate_curve(self) -> None:
        """One box would plot the same numbers as the union — don't."""
        state = _state(self._frame())
        self.window.on_camera_state(state)
        self.window._start_manual_selection()
        self.window._on_manual_region((0.05, 0.10, 0.30, 0.40))
        self.assertEqual(self.window._region_curves, {})
        self.assertFalse(self.window.remove_btn.isEnabled())

    def test_add_is_unavailable_until_an_roi_exists(self) -> None:
        self.window.on_camera_state(_state(self._frame()))
        self.assertFalse(self.window.add_btn.isEnabled())
        self.window._start_append_selection()
        self.assertIn("define an ROI first", self.window.status_label.text())

    def test_per_box_samples_reach_the_measurement_signal(self) -> None:
        samples: list = []
        self.window.measurement_ready.connect(samples.append)
        self._define_two_boxes()
        self.window.on_camera_state(
            _state(self._frame(), sequence=2, exposure_us=300_000.0),
        )
        self.assertTrue(samples)
        latest = samples[-1]
        self.assertEqual(
            [region.label for region in latest.regions], ["box 1", "box 2"],
        )
        self.assertEqual(latest.exposure_us, 300_000.0)

    def test_exposure_change_breaks_the_trend_and_rebaselines(self) -> None:
        events: list = []
        self.window.roi_event.connect(
            lambda event, roi, reason: events.append((event, reason))
        )
        self._define_two_boxes()
        self.window.on_camera_state(
            _state(self._frame(), sequence=2, exposure_us=300_000.0),
        )
        points_before = len(self.window._times)

        self.window.on_camera_state(_state(
            self._frame(), sequence=3,
            exposure_us=500_000.0, exposure_generation=1,
        ))

        # A NaN was inserted so pyqtgraph lifts the pen across the step, and
        # each per-box series got the same gap.
        times = list(self.window._times)
        self.assertGreater(len(times), points_before)
        self.assertTrue(any(np.isnan(value) for value in times))
        for series in self.window._region_series.values():
            self.assertTrue(any(np.isnan(value) for value in series))
        self.assertEqual(len(self.window._exposure_markers), 1)

        exposure_events = [e for e in events if e[0] == "exposure_changed"]
        self.assertEqual(len(exposure_events), 1)
        self.assertIn("300 ms", exposure_events[0][1])
        self.assertIn("500 ms", exposure_events[0][1])

        # Δ% is measured against the new exposure, not across the step.
        self.assertAlmostEqual(
            self.window.latest_sample.relative_change_pct, 0.0, places=6,
        )
        for region in self.window.latest_sample.regions:
            self.assertAlmostEqual(region.relative_change_pct, 0.0, places=6)

    def test_unchanged_exposure_generation_leaves_the_trend_continuous(self) -> None:
        self._define_two_boxes()
        for sequence in (2, 3, 4):
            self.window.on_camera_state(_state(
                self._frame(), sequence=sequence,
                exposure_us=300_000.0, exposure_generation=0,
            ))
        self.assertFalse(
            any(np.isnan(value) for value in self.window._times)
        )
        self.assertEqual(self.window._exposure_markers, [])

    def test_every_box_is_drawn_and_named_on_the_frame(self) -> None:
        """Regression: a stuck selection used to silently stop the overlay.

        `_refresh_image` early-returns while the view is in selection mode.
        The mouse-release path cancels first, but a programmatically driven
        selection did not — leaving an ROI that was measured and plotted while
        never being drawn, which is exactly the state a grower cannot debug.
        """
        self._define_two_boxes()
        self.assertFalse(
            self.window.image_view.is_selecting,
            "selection must be cancelled once the region is handled",
        )
        overlay = self.window.image_view._overlay_items
        # One rectangle plus one label per box.
        self.assertEqual(len(overlay), 4)
        drawn_labels = sorted(
            item.text() for item in overlay if hasattr(item, "text")
        )
        self.assertEqual(drawn_labels, ["box 1", "box 2"])

    def test_normalise_toggle_switches_units_without_losing_history(self) -> None:
        """Δ% is recorded at measurement time, not derived on toggle.

        Deriving it from stored sums with the CURRENT baseline would
        misrepresent every point taken before an exposure change re-seeded
        that baseline.
        """
        self._define_two_boxes()
        for sequence in (2, 3, 4):
            self.window.on_camera_state(_state(
                self._frame(), sequence=sequence, exposure_us=300_000.0,
            ))
        points = len(self.window._times)
        self.assertEqual(len(self.window._intensities_pct), points)
        for series in self.window._region_series_pct.values():
            self.assertEqual(len(series), points)

        absolute = self.window._curve.getData()[1]
        self.window.normalize_check.setChecked(True)
        normalised = self.window._curve.getData()[1]
        self.assertEqual(len(absolute), len(normalised))
        self.assertIn("%", self.window._plot.getPlotItem().getAxis("left").labelUnits)
        # Sums are a.u. in the thousands; Δ% sits near zero. Different data.
        self.assertNotEqual(list(absolute), list(normalised))

        self.window.normalize_check.setChecked(False)
        self.assertEqual(list(self.window._curve.getData()[1]), list(absolute))

    def test_normalised_series_also_break_at_an_exposure_change(self) -> None:
        self._define_two_boxes()
        self.window.normalize_check.setChecked(True)
        self.window.on_camera_state(
            _state(self._frame(), sequence=2, exposure_us=300_000.0),
        )
        self.window.on_camera_state(_state(
            self._frame(), sequence=3,
            exposure_us=500_000.0, exposure_generation=1,
        ))
        self.assertTrue(
            any(np.isnan(v) for v in self.window._intensities_pct)
        )
        for series in self.window._region_series_pct.values():
            self.assertTrue(any(np.isnan(v) for v in series))
        # Both histories stay the same length as the shared time axis.
        points = len(self.window._times)
        self.assertEqual(len(self.window._intensities), points)
        self.assertEqual(len(self.window._intensities_pct), points)

    def test_detect_spots_requires_explicit_accept_like_the_triplet(self) -> None:
        h, w = 96, 128
        yy, xx = np.mgrid[0:h, 0:w]
        img = np.full((h, w), 8.0)
        for cx, cy, amp in [(30, 48, 200), (64, 40, 240), (98, 48, 160)]:
            img += amp * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 3.0 ** 2)))
        frame = np.clip(img, 0, 255).astype(np.uint8)
        self.window.on_camera_state(_state(frame))
        self.window.spot_count.setValue(3)
        self.window._detect_peaks()
        self.assertIsNone(self.window.active_roi)
        self.assertTrue(self.window.accept_btn.isEnabled())
        self.window._accept_candidate()
        roi = self.window.active_roi
        self.assertEqual(roi.mode, "auto_peaks")
        self.assertEqual(len(roi.regions), 3)
        self.assertEqual(
            sorted(self.window._region_curves), sorted(roi.region_labels),
        )

    def test_clearing_the_roi_removes_every_per_box_curve(self) -> None:
        self._define_two_boxes()
        self.assertTrue(self.window._region_curves)
        self.window.clear_roi()
        self.assertEqual(self.window._region_curves, {})
        self.assertFalse(self.window.add_btn.isEnabled())
        self.assertFalse(self.window.remove_btn.isEnabled())


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


class RheedRoiRegionLoggerTests(unittest.TestCase):
    """The per-region sibling file and the exposure timeline."""

    def _two_box_sample(self, logger, *, sequence=7, exposure_us=300_000.0,
                        exposure_generation=0):
        frame = np.zeros((20, 30, 3), dtype=np.uint8)
        frame[2:8, 2:8] = 90
        frame[2:8, 18:26] = 210
        state = _state(
            frame, sequence=sequence,
            exposure_us=exposure_us, exposure_generation=exposure_generation,
        )
        roi = manual_roi_definition((0.05, 0.05, 0.35, 0.45), state, frame)
        roi = manual_roi_definition(
            (0.55, 0.05, 0.90, 0.45), state, frame, append_to=roi,
        )
        return roi, measure_camera_state(state, roi)

    def test_one_row_per_box_per_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = GrowthLogger(directory)
            logger.start_session("ROI_REGIONS")
            roi, sample = self._two_box_sample(logger)
            self.assertTrue(logger.record_rheed_roi_definition("defined", roi))
            self.assertTrue(logger.record_rheed_roi_intensity(
                sample, elapsed_s=2.0, view_segment_id=1,
                visual_history_generation=0,
            ))
            session_dir = logger.session_dir
            logger.end_session()

            with (session_dir / "rheed_roi_region_intensity.csv").open(
                newline="", encoding="utf-8",
            ) as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                [row["region_label"] for row in rows], ["box 1", "box 2"],
            )
            self.assertEqual([row["region_index"] for row in rows], ["0", "1"])
            self.assertTrue(all(
                row["roi_definition_id"] == roi.roi_definition_id
                for row in rows
            ))
            # The brighter patch reads higher — the whole point of splitting.
            self.assertGreater(
                float(rows[1]["intensity_mean"]),
                float(rows[0]["intensity_mean"]),
            )
            # Every row records the exposure that produced it.
            self.assertTrue(all(
                float(row["exposure_us"]) == 300_000.0 for row in rows
            ))
            self.assertEqual(
                json.loads(rows[0]["region_normalized_json"]),
                roi.regions[0].to_dict(),
            )

    def test_duplicate_capture_writes_no_region_rows(self) -> None:
        """The union file's dedupe key governs both files, so they agree."""
        with tempfile.TemporaryDirectory() as directory:
            logger = GrowthLogger(directory)
            logger.start_session("ROI_REGION_DEDUPE")
            roi, sample = self._two_box_sample(logger)
            self.assertTrue(logger.record_rheed_roi_intensity(
                sample, elapsed_s=2.0,
            ))
            self.assertFalse(logger.record_rheed_roi_intensity(
                sample, elapsed_s=2.0,
            ))
            session_dir = logger.session_dir
            logger.end_session()

            with (session_dir / "rheed_roi_region_intensity.csv").open(
                newline="", encoding="utf-8",
            ) as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)

    def test_union_file_records_exposure_and_stays_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = GrowthLogger(directory)
            logger.start_session("ROI_UNION_EXPOSURE")
            _roi, sample = self._two_box_sample(
                logger, exposure_us=500_000.0, exposure_generation=2,
            )
            self.assertTrue(logger.record_rheed_roi_intensity(
                sample, elapsed_s=2.0,
            ))
            session_dir = logger.session_dir
            logger.end_session()

            with (session_dir / "rheed_roi_intensity.csv").open(
                newline="", encoding="utf-8",
            ) as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["region_count"], "2")
            self.assertEqual(float(rows[0]["exposure_us"]), 500_000.0)
            self.assertEqual(rows[0]["exposure_generation"], "2")
            self.assertEqual(rows[0]["schema_version"], "2")

    def test_exposure_changes_are_journalled_with_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = GrowthLogger(directory)
            logger.start_session("EXPOSURE_TIMELINE")
            self.assertTrue(logger.record_camera_exposure_change(
                generation=1,
                confirmed_exposure_us=500_000.0,
                previous_exposure_us=300_000.0,
                elapsed_s=12.5,
                capture_sequence=42,
                captured_at_utc="2026-08-31T12:00:00.000Z",
                capture_backend="vimba",
                outcome="confirmed",
            ))
            self.assertTrue(logger.record_camera_exposure_change(
                generation=1,
                confirmed_exposure_us=500_000.0,
                previous_exposure_us=500_000.0,
                elapsed_s=20.0,
                outcome="refused",
                reason="outside the camera range",
            ))
            # An unknown outcome is rejected rather than written as data.
            self.assertFalse(logger.record_camera_exposure_change(
                generation=2,
                confirmed_exposure_us=1.0,
                previous_exposure_us=1.0,
                elapsed_s=1.0,
                outcome="maybe",
            ))
            session_dir = logger.session_dir
            logger.end_session()

            entries = [
                json.loads(line)
                for line in (session_dir / "camera_exposure_changes.jsonl")
                .read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["outcome"], "confirmed")
            self.assertEqual(entries[0]["confirmed_exposure_us"], 500_000.0)
            self.assertEqual(entries[0]["previous_exposure_us"], 300_000.0)
            self.assertEqual(entries[0]["capture_sequence"], 42)
            self.assertEqual(entries[1]["outcome"], "refused")
            self.assertIn("outside the camera range", entries[1]["reason"])

    def test_exposure_change_is_accepted_as_an_roi_lifecycle_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = GrowthLogger(directory)
            logger.start_session("ROI_EXPOSURE_EVENT")
            roi, _sample = self._two_box_sample(logger)
            self.assertTrue(logger.record_rheed_roi_definition(
                "exposure_changed", roi, reason="exposure 300 ms -> 500 ms",
            ))
            session_dir = logger.session_dir
            logger.end_session()

            events = [
                json.loads(line)
                for line in (session_dir / "rheed_roi_definitions.jsonl")
                .read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["event"], "exposure_changed")
            self.assertIn("500 ms", events[-1]["reason"])


if __name__ == "__main__":
    unittest.main()
