"""Unittest-compatible coverage for Equalizer camera alignment."""
from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest import mock

import numpy as np

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import gui.equalizer_alignment as alignment
from gui.equalizer_alignment import (
    ACTIVE_SIMULATOR_LABELS,
    AlignmentCandidate,
    BasisAsset,
    BasisBundle,
    CalibrationRecord,
    LandmarkDetection,
    PROCESS_H,
    PROCESS_W,
    RheedFrameSnapshot,
    calibration_is_stale,
    compute_similarity,
    detect_basis_landmarks,
    enumerate_alignment_candidates,
    normalized_correlation,
    project_points,
    validate_alignment,
    validate_manual_landmarks,
    warp_basis_bundle,
    warp_basis_similarity,
    warp_images_once,
)


BASE_POINTS = np.array(
    [[28.0, 48.0], [64.0, 40.0], [100.0, 48.0]], dtype=np.float64
)


def _gaussian_pattern(
    points: np.ndarray = BASE_POINTS,
    *,
    noise: float = 0.0,
    seed: int = 42,
) -> np.ndarray:
    rows, cols = np.indices((PROCESS_H, PROCESS_W), dtype=np.float64)
    image = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float64)
    for x, y in points:
        image += 220.0 * np.exp(-((cols - x) ** 2 + (rows - y) ** 2) / (2.0 * 2.5**2))
    if noise:
        image += np.random.default_rng(seed).normal(0.0, noise, image.shape)
    return np.clip(image, 0.0, 255.0).astype(np.float32)


def _matrix(
    angle_deg: float = 0.0,
    scale: float = 1.0,
    dx: float = 0.0,
    dy: float = 0.0,
    *,
    mirrored: bool = False,
) -> np.ndarray:
    angle = math.radians(angle_deg)
    rotation = scale * np.array(
        [[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]],
        dtype=np.float64,
    )
    local = np.column_stack([rotation, [dx, dy]])
    if not mirrored:
        return local
    parity = np.array(
        [[-1.0, 0.0, PROCESS_W - 1.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    local_h = np.vstack([local, [0.0, 0.0, 1.0]])
    return (local_h @ parity)[:2]


def _bundle(offset: float = 0.0) -> BasisBundle:
    assets = tuple(
        BasisAsset(
            label=label,
            image=_gaussian_pattern() + offset + index,
            active=True,
            source_kind="simulator",
            source_path=f"data/{label}.png",
        )
        for index, label in enumerate(ACTIVE_SIMULATOR_LABELS)
    ) + (
        BasisAsset(
            label="HTR",
            image=None,
            active=False,
            source_kind="unavailable",
            unavailable_reason="canonical simulator basis pending",
        ),
    )
    return BasisBundle(assets)


def _best_candidate(matrix: np.ndarray) -> tuple[AlignmentCandidate, tuple[AlignmentCandidate, ...]]:
    basis = _gaussian_pattern()
    live = warp_basis_similarity({"1x1": basis}, matrix)["1x1"]
    live_points = project_points(matrix, BASE_POINTS)
    live_points = live_points[np.argsort(live_points[:, 0])]
    candidates = enumerate_alignment_candidates(BASE_POINTS, live_points, basis, live)
    return candidates[0], candidates


class LandmarkDetectionTests(unittest.TestCase):
    def test_noise_robust_detection_is_explicit_and_ordered(self) -> None:
        result = detect_basis_landmarks(_gaussian_pattern(noise=2.0))
        self.assertIsInstance(result, LandmarkDetection)
        self.assertTrue(result.success, result.reason)
        np.testing.assert_allclose(result.points, BASE_POINTS, atol=2.0)
        self.assertTrue(np.all(np.diff(result.points[:, 0]) > 0))

    def test_blank_image_is_failure_without_fake_points(self) -> None:
        result = detect_basis_landmarks(np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32))
        self.assertFalse(result.success)
        self.assertEqual(result.points.shape, (0, 2))
        self.assertIn("contrast", result.reason)

    def test_collinear_spots_are_rejected(self) -> None:
        points = np.array([[28.0, 48.0], [64.0, 48.0], [100.0, 48.0]])
        result = detect_basis_landmarks(_gaussian_pattern(points))
        self.assertFalse(result.success)
        self.assertIn("collinear", result.reason)

    def test_non_image_is_failure(self) -> None:
        result = detect_basis_landmarks(np.zeros((3, 4, 2), dtype=np.float32))
        self.assertFalse(result.success)

    def test_manual_points_require_signal_geometry_and_click_order(self) -> None:
        good = validate_manual_landmarks(_gaussian_pattern(), BASE_POINTS)
        self.assertTrue(good.success, good.reason)

        blank = validate_manual_landmarks(
            np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32), BASE_POINTS,
        )
        self.assertFalse(blank.success)
        self.assertIn("SNR", blank.reason)

        wrong_order = validate_manual_landmarks(
            _gaussian_pattern(), BASE_POINTS[[2, 0, 1]],
        )
        self.assertFalse(wrong_order.success)
        self.assertIn("not ordered", wrong_order.reason)


class SimilarityTests(unittest.TestCase):
    def _assert_recovered(self, expected: np.ndarray) -> None:
        destination = project_points(expected, BASE_POINTS)
        actual, scale, _, _, _ = compute_similarity(BASE_POINTS, destination)
        np.testing.assert_allclose(actual, expected, atol=1e-9)
        self.assertGreater(scale, 0.0)

    def test_identity(self) -> None:
        self._assert_recovered(_matrix())

    def test_translation(self) -> None:
        self._assert_recovered(_matrix(dx=7.0, dy=-4.0))

    def test_rotation_and_scale(self) -> None:
        self._assert_recovered(_matrix(angle_deg=17.0, scale=1.25, dx=3.0, dy=-2.0))

    def test_180_degree_rotation(self) -> None:
        matrix, scale, angle, _, _ = compute_similarity(
            BASE_POINTS,
            project_points(_matrix(angle_deg=180.0, dx=127.0, dy=95.0), BASE_POINTS),
        )
        self.assertAlmostEqual(abs(angle), 180.0, places=7)
        self.assertAlmostEqual(scale, 1.0, places=7)
        ok, reason = validate_alignment(
            matrix,
            scale=scale,
            rms_residual_px=0.0,
            max_residual_px=0.0,
            valid_coverage=1.0,
        )
        self.assertTrue(ok, reason)

    def test_degenerate_source_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "degenerate"):
            compute_similarity(np.ones((3, 2)), BASE_POINTS)


class CandidateTests(unittest.TestCase):
    def test_normal_candidate_ranks_first(self) -> None:
        expected = _matrix(angle_deg=6.0, scale=0.95, dx=7.0, dy=-3.0)
        best, candidates = _best_candidate(expected)
        self.assertTrue(best.valid, best.validation_reason)
        self.assertEqual(best.parity, "normal")
        np.testing.assert_allclose(best.matrix, expected, atol=1e-8)
        self.assertGreater(best.correlation, 0.999)
        self.assertEqual(len(candidates), 4)
        self.assertEqual({c.parity for c in candidates}, {"normal", "mirrored"})
        self.assertEqual(
            {c.endpoint_order for c in candidates}, {"forward", "reversed"},
        )

    def test_mirrored_candidate_ranks_first(self) -> None:
        expected = _matrix(angle_deg=-4.0, scale=0.9, dx=5.0, dy=2.0, mirrored=True)
        best, candidates = _best_candidate(expected)
        self.assertTrue(best.valid, best.validation_reason)
        self.assertEqual(best.parity, "mirrored")
        np.testing.assert_allclose(best.matrix, expected, atol=1e-8)
        self.assertGreater(best.correlation, 0.999)
        self.assertTrue(any(c.parity == "normal" for c in candidates))
        self.assertTrue(any(c.parity == "mirrored" for c in candidates))

    def test_input_point_order_is_not_silently_accepted(self) -> None:
        basis = _gaussian_pattern()
        expected = _matrix(dx=3.0, dy=-2.0)
        live = warp_basis_similarity({"1x1": basis}, expected)["1x1"]
        scrambled = project_points(expected, BASE_POINTS)[[2, 0, 1]]
        with self.assertRaisesRegex(ValueError, "not ordered"):
            enumerate_alignment_candidates(BASE_POINTS, scrambled, basis, live)

    def test_correlation_ranking_uses_only_valid_pixels(self) -> None:
        a = np.arange(16, dtype=np.float64).reshape(4, 4)
        b = a.copy()
        b[0, :] = -1000.0
        mask = np.ones_like(a, dtype=bool)
        mask[0, :] = False
        self.assertAlmostEqual(normalized_correlation(a, b, mask), 1.0)


class WarpAndValidationTests(unittest.TestCase):
    def test_valid_mask_and_translation(self) -> None:
        image = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32)
        image[40, 50] = 255.0
        warped, mask = warp_images_once({"1x1": image}, _matrix(dx=3.0))
        self.assertAlmostEqual(float(warped["1x1"][40, 53]), 255.0)
        self.assertFalse(mask[:, :3].any())
        self.assertTrue(mask[:, 3:].all())

    def test_each_asset_is_interpolated_once(self) -> None:
        images = {label: _gaussian_pattern() for label in ACTIVE_SIMULATOR_LABELS}
        original = alignment._bilinear_sample
        with mock.patch.object(alignment, "_bilinear_sample", wraps=original) as sampler:
            warped, _ = warp_images_once(images, _matrix(angle_deg=2.0))
        self.assertEqual(set(warped), set(ACTIVE_SIMULATOR_LABELS))
        self.assertEqual(sampler.call_count, len(ACTIVE_SIMULATOR_LABELS))

    def test_bundle_warp_excludes_htr(self) -> None:
        warped, mask = warp_basis_bundle(_bundle(), _matrix())
        self.assertEqual(tuple(warped), ACTIVE_SIMULATOR_LABELS)
        self.assertTrue(mask.all())

    def test_all_fail_closed_thresholds(self) -> None:
        cases = (
            dict(scale=0.49, rms_residual_px=0.0, max_residual_px=0.0, valid_coverage=1.0),
            dict(scale=1.0, rms_residual_px=3.01, max_residual_px=4.0, valid_coverage=1.0),
            dict(scale=1.0, rms_residual_px=2.0, max_residual_px=5.01, valid_coverage=1.0),
            dict(scale=1.0, rms_residual_px=0.0, max_residual_px=0.0, valid_coverage=0.49),
        )
        for values in cases:
            with self.subTest(values=values):
                ok, reason = validate_alignment(_matrix(), **values)
                self.assertFalse(ok)
                self.assertTrue(reason)
        ok, reason = validate_alignment(
            _matrix(),
            scale=1.0,
            rms_residual_px=3.0,
            max_residual_px=5.0,
            valid_coverage=0.5,
        )
        self.assertTrue(ok, reason)

    def test_nonfinite_and_singular_matrices_fail(self) -> None:
        for matrix in (
            np.array([[np.nan, 0.0, 0.0], [0.0, 1.0, 0.0]]),
            np.zeros((2, 3)),
        ):
            ok, _ = validate_alignment(
                matrix,
                scale=1.0,
                rms_residual_px=0.0,
                max_residual_px=0.0,
                valid_coverage=1.0,
            )
            self.assertFalse(ok)


class BundleAndSnapshotTests(unittest.TestCase):
    def test_bundle_hash_is_content_addressed_and_htr_inactive(self) -> None:
        first = _bundle()
        second = _bundle()
        changed = _bundle(offset=1.0)
        self.assertEqual(first.bundle_id, second.bundle_id)
        self.assertNotEqual(first.bundle_id, changed.bundle_id)
        self.assertEqual(first.active_labels, ACTIVE_SIMULATOR_LABELS)
        self.assertEqual(first.unavailable_labels, ("HTR",))
        self.assertFalse(first.asset("HTR").active)
        manifest = first.to_manifest_dict()
        self.assertEqual(manifest["bundle_id"], first.bundle_id)
        self.assertEqual(manifest["version"], "simulator-v2")
        self.assertEqual(
            tuple(asset["label"] for asset in manifest["assets"]),
            (*ACTIVE_SIMULATOR_LABELS, "HTR"),
        )
        for asset in manifest["assets"][:4]:
            self.assertEqual(len(asset["image_sha256"]), 64)
            self.assertEqual(
                asset["source_transform"],
                np.eye(3, dtype=np.float64).tolist(),
            )
        self.assertEqual(
            BasisBundle.validate_manifest_dict(manifest),
            manifest,
        )
        tampered = json.loads(json.dumps(manifest))
        tampered["assets"][0]["image_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "identity"):
            BasisBundle.validate_manifest_dict(tampered)

    def test_committed_canonical_assets_exclude_white_export_frame(self) -> None:
        from scripts.equalizer_ui import load_basis_bundle

        bundle = load_basis_bundle()
        self.assertEqual(bundle.version, "simulator-v2")
        for label, image in bundle.active_images().items():
            with self.subTest(label=label):
                border_means = (
                    image[0].mean(), image[-1].mean(),
                    image[:, 0].mean(), image[:, -1].mean(),
                )
                self.assertLess(max(border_means), 25.0)

    def test_snapshot_copies_arrays_and_recomputes_age(self) -> None:
        rgb = np.zeros((24, 32, 3), dtype=np.uint8)
        gray = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32)
        snapshot = RheedFrameSnapshot.freeze(
            rgb,
            gray,
            received_monotonic_ns=1_000_000_000,
            view_segment_id=None,
        )
        rgb[...] = 99
        gray[...] = 99
        self.assertFalse(snapshot.rgb.any())
        self.assertFalse(snapshot.grayscale.any())
        self.assertFalse(snapshot.rgb.flags.writeable)
        self.assertAlmostEqual(snapshot.age_ms(1_125_000_000), 125.0)

        # The ultimate bytes backing store is immutable; callers cannot undo
        # the read-only flag after the grower reviewed this frame.
        with self.assertRaises(ValueError):
            snapshot.rgb.setflags(write=True)
        with self.assertRaises(ValueError):
            snapshot.grayscale.setflags(write=True)

    def test_snapshot_normalizes_only_explicit_utc_timestamps(self) -> None:
        rgb = np.zeros((24, 32, 3), dtype=np.uint8)
        gray = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32)
        snapshot = RheedFrameSnapshot.freeze(
            rgb,
            gray,
            captured_at_utc="2026-07-31T12:00:00Z",
        )
        self.assertEqual(
            snapshot.captured_at_utc,
            "2026-07-31T12:00:00+00:00",
        )
        for invalid in (
            "not-a-time",
            "2026-07-31T12:00:00",
            "2026-07-31T07:00:00-05:00",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                RheedFrameSnapshot.freeze(
                    rgb,
                    gray,
                    captured_at_utc=invalid,
                )

    def test_snapshot_accepts_captured_monotonic_alias(self) -> None:
        snapshot = RheedFrameSnapshot.from_capture(
            np.zeros((24, 32, 3), dtype=np.uint8),
            np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32),
            {
                "captured_monotonic_ns": 123,
                "capture_sequence": 9,
                "source_hwnd": 77,
                "capture_backend": "wgc",
            },
            gun_aligned=True,
        )
        self.assertEqual(snapshot.received_monotonic_ns, 123)
        self.assertEqual(snapshot.capture_sequence, 9)
        self.assertTrue(snapshot.gun_aligned)

    def test_snapshot_rejects_dimensions_that_do_not_match_rgb(self) -> None:
        with self.assertRaisesRegex(ValueError, "dimensions"):
            RheedFrameSnapshot.freeze(
                np.zeros((96, 128, 3), dtype=np.uint8),
                np.zeros((96, 128), dtype=np.float32),
                camera_width=640,
                camera_height=480,
            )

    def test_orientation_evidence_hash_matches_exact_png_bytes(self) -> None:
        snapshot = RheedFrameSnapshot.freeze(
            np.arange(96 * 128 * 3, dtype=np.uint8).reshape(96, 128, 3),
            np.zeros((96, 128), dtype=np.float32),
        )
        import hashlib

        payload = snapshot.orientation_evidence_png()
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(
            snapshot.orientation_evidence_sha256(),
            hashlib.sha256(payload).hexdigest(),
        )

    def test_retrospective_logging_uses_persisted_age_not_new_boot_clock(self) -> None:
        snapshot = RheedFrameSnapshot.freeze(
            np.zeros((96, 128, 3), dtype=np.uint8),
            np.zeros((96, 128), dtype=np.float32),
            received_monotonic_ns=9_000_000_000_000,
            source_frame_age_ms=12.5,
            retrospective=True,
        )
        self.assertEqual(snapshot.age_ms(now_monotonic_ns=1), 0.0)
        self.assertEqual(snapshot.logging_age_ms(), 12.5)


class CalibrationRecordTests(unittest.TestCase):
    def _record(self) -> CalibrationRecord:
        bundle = _bundle()
        best, _ = _best_candidate(_matrix(dx=2.0, dy=-1.0))
        snapshot = RheedFrameSnapshot.freeze(
            np.zeros((96, 128, 3), dtype=np.uint8),
            _gaussian_pattern(),
            captured_at_utc="2026-07-31T12:00:00+00:00",
            received_monotonic_ns=123,
            capture_sequence=4,
            source_hwnd=99,
            capture_backend="wgc",
            capture_geometry_id="ksa-chrome-v1:1:75:30",
            session_id="session-a",
            view_segment_id=2,
            visual_history_generation=7,
            gun_aligned=True,
            realignment_active=False,
        )
        return CalibrationRecord.from_candidate(best, bundle, snapshot)

    @staticmethod
    def _accepted(
        record: CalibrationRecord,
        accepted_by: str = "grower-a",
    ) -> CalibrationRecord:
        return record.accepted(
            accepted_by,
            "a" * 64,
            orientation_evidence_kind="Tw(2x1)",
        )

    def test_accept_is_immutable_and_records_provenance(self) -> None:
        pending = self._record()
        accepted = self._accepted(pending)
        self.assertFalse(pending.grower_accepted)
        self.assertTrue(accepted.grower_accepted)
        self.assertEqual(accepted.accepted_by, "grower-a")
        self.assertEqual(accepted.orientation_evidence_kind, "Tw(2x1)")
        self.assertEqual(accepted.orientation_evidence_sha256, "a" * 64)
        self.assertEqual(
            accepted.orientation_evidence_path,
            f"frames/equalizer_calibration_{accepted.calibration_id}_orientation.png",
        )

    def test_dummy_record_can_be_accepted_without_session_qc_provenance(self) -> None:
        pending = replace(
            self._record(),
            capture_backend="dummy_tw",
            session_id="",
            view_segment_id=0,
            gun_aligned=False,
        )
        accepted = self._accepted(pending)
        self.assertTrue(accepted.grower_accepted)
        self.assertEqual(accepted.capture_backend, "dummy_tw")

    def test_real_record_still_rejects_missing_session_qc_provenance(self) -> None:
        pending = replace(
            self._record(),
            session_id="",
            gun_aligned=False,
        )
        with self.assertRaisesRegex(ValueError, "stable session/QC"):
            self._accepted(pending)

    def test_invalidated_record_cannot_be_revived(self) -> None:
        invalidated = self._accepted(self._record()).invalidated(
            "camera disconnected",
        )
        with self.assertRaisesRegex(ValueError, "cannot be revived"):
            self._accepted(invalidated)

    def test_json_round_trip(self) -> None:
        original = self._accepted(self._record())
        payload = original.to_json_dict()
        json.dumps(payload)
        restored = CalibrationRecord.from_json_dict(payload)
        self.assertEqual(restored.calibration_id, original.calibration_id)
        self.assertEqual(restored.basis_bundle_id, original.basis_bundle_id)
        np.testing.assert_array_equal(restored.matrix, original.matrix)
        self.assertEqual(restored.view_segment_id, 2)
        self.assertTrue(restored.gun_aligned)

    def test_tampered_accepted_json_is_rejected(self) -> None:
        payload = self._accepted(self._record()).to_json_dict()
        tampering = (
            ("matrix", [[1.0, 0.5, 2.0], [0.0, 1.0, -1.0]]),
            ("active_classes", [*ACTIVE_SIMULATOR_LABELS, "HTR"]),
            ("parity", "unknown"),
        )
        for field, value in tampering:
            with self.subTest(field=field):
                modified = dict(payload)
                modified[field] = value
                with self.assertRaises(ValueError):
                    CalibrationRecord.from_json_dict(modified)

    def test_staleness_covers_qc_and_capture_context(self) -> None:
        record = self._accepted(self._record())
        fresh, reason = calibration_is_stale(
            record,
            source_hwnd=99,
            camera_width=128,
            camera_height=96,
            capture_backend="wgc",
            capture_geometry_id="ksa-chrome-v1:1:75:30",
            session_id="session-a",
            view_segment_id=2,
            visual_history_generation=7,
            basis_bundle_id=record.basis_bundle_id,
            gun_aligned=True,
            realignment_active=False,
        )
        self.assertFalse(fresh, reason)
        stale, reason = calibration_is_stale(record, view_segment_id=3)
        self.assertTrue(stale)
        self.assertIn("segment", reason)
        stale, reason = calibration_is_stale(record, realignment_active=True)
        self.assertTrue(stale)
        self.assertIn("realignment", reason)
        stale, reason = calibration_is_stale(
            record,
            capture_geometry_id="ksa-chrome-v1:0:0:0",
        )
        self.assertTrue(stale)
        self.assertIn("geometry", reason)


if __name__ == "__main__":
    unittest.main()
