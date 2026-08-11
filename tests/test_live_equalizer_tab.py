"""Focused offscreen tests for the provenance-bound Live Equalizer tab.

Run with the Bulbasaur GUI environment::

    python -m pytest -q tests/test_live_equalizer_tab.py
"""
from __future__ import annotations

from contextlib import redirect_stderr
import io
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

from gui.equalizer_alignment import (  # noqa: E402
    ACTIVE_SIMULATOR_LABELS,
    AlignmentCandidate,
    BasisAsset,
    BasisBundle,
    CalibrationRecord,
    LandmarkDetection,
    PROCESS_H,
    PROCESS_W,
)
from gui.live_equalizer_tab import CLASS_LABELS, LiveEqualizerTab  # noqa: E402
from gui.state import ClassifierState  # noqa: E402


LANDMARKS = np.array(
    [[30.0, 55.0], [64.0, 35.0], [98.0, 55.0]], dtype=np.float64,
)


def _spot_image(offset: float = 0.0) -> np.ndarray:
    yy, xx = np.mgrid[:PROCESS_H, :PROCESS_W]
    image = np.full((PROCESS_H, PROCESS_W), offset, dtype=np.float32)
    for index, (x, y) in enumerate(LANDMARKS):
        image += (180.0 + 20.0 * index) * np.exp(
            -((xx - x) ** 2 + (yy - y) ** 2) / (2.0 * 2.5**2),
        )
    return np.clip(image, 0, 255).astype(np.float32)


def _bundle() -> BasisBundle:
    base = _spot_image()
    assets = []
    for index, label in enumerate(ACTIVE_SIMULATOR_LABELS):
        assets.append(BasisAsset(
            label=label,
            image=np.clip(base + index * 3, 0, 255),
            active=True,
            source_kind="simulator",
            source_path=f"test/{label}.png",
        ))
    assets.append(BasisAsset(
        label="HTR",
        image=None,
        active=False,
        source_kind="unavailable",
        unavailable_reason="canonical simulator basis pending",
    ))
    return BasisBundle(tuple(assets), version="test-v1")


def _detection() -> LandmarkDetection:
    return LandmarkDetection.detected(
        LANDMARKS,
        method="test",
        confidence=1.0,
        peak_snr=20.0,
    )


def _rgb(gray: np.ndarray | None = None) -> np.ndarray:
    if gray is None:
        gray = _spot_image()
    u8 = np.clip(gray, 0, 255).astype(np.uint8)
    return np.repeat(u8[..., None], 3, axis=2)


def _metadata(
    sequence: int = 1,
    *,
    hwnd: int = 1234,
    monotonic_ns: int | None = None,
    backend: str = "wgc",
    geometry_id: str = "test-wgc-roi-v1:dpi-getdpiforwindow-96",
    width: int = PROCESS_W,
    height: int = PROCESS_H,
) -> dict:
    return {
        "capture_backend": backend,
        "captured_at_utc": "2026-07-31T12:00:00+00:00",
        "captured_monotonic_ns": (
            time.monotonic_ns() if monotonic_ns is None else monotonic_ns
        ),
        "capture_sequence": sequence,
        "source_hwnd": hwnd,
        "capture_geometry_id": geometry_id,
        "camera_width": width,
        "camera_height": height,
        "view_segment_id": 4,
        "visual_history_generation": 2,
    }


def _identity_candidate() -> AlignmentCandidate:
    matrix = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    return AlignmentCandidate(
        matrix=matrix,
        parity="normal",
        endpoint_order="forward",
        basis_points=LANDMARKS,
        live_points=LANDMARKS,
        projected_points=LANDMARKS,
        residuals_px=np.zeros(3),
        scale=1.0,
        rotation_deg=0.0,
        translation_xy=(0.0, 0.0),
        rms_residual_px=0.0,
        max_residual_px=0.0,
        valid_coverage=1.0,
        correlation=1.0,
        valid=True,
    )


class EqualizerTabCase(unittest.TestCase):
    retrospective = False

    def setUp(self) -> None:
        self.bundle = _bundle()
        self._loader = patch(
            "scripts.equalizer_ui.load_basis_bundle", return_value=self.bundle,
        )
        self._basis_detector = patch(
            "gui.live_equalizer_tab.detect_basis_landmarks",
            return_value=_detection(),
        )
        self._loader.start()
        self._basis_detector.start()
        self.tab = LiveEqualizerTab(retrospective=self.retrospective)

    def tearDown(self) -> None:
        self.tab.deleteLater()
        self._basis_detector.stop()
        self._loader.stop()

    def make_live(self, *, sequence: int = 1, hwnd: int = 1234) -> None:
        self.tab.set_session_id("SESSION-A")
        self.tab.set_save_enabled(True)
        self.tab.update_qc_context(
            session_active=True,
            view_segment_id=4,
            visual_history_generation=2,
            gun_aligned=True,
            realignment_active=False,
        )
        self.tab.update_camera_frame(_rgb(), _metadata(sequence, hwnd=hwnd))

    def accepted_record(self) -> CalibrationRecord:
        snapshot = self.tab.get_current_snapshot()
        assert snapshot is not None
        return CalibrationRecord.from_candidate(
            _identity_candidate(), self.bundle, snapshot,
        ).accepted(
            "unit-test",
            snapshot.orientation_evidence_sha256(),
            orientation_evidence_kind="RT13",
        )

    def confirm_handedness(self, kind: str = "RT13") -> None:
        index = self.tab._handedness_evidence_combo.findData(kind)
        self.assertGreaterEqual(index, 0)
        self.tab._handedness_evidence_combo.setCurrentIndex(index)
        self.tab._handedness_confirm.setChecked(True)
        self.assertEqual(self.tab.get_handedness_evidence_kind(), kind)

    def activate_calibration(self) -> CalibrationRecord:
        record = self.accepted_record()
        self.assertTrue(self.tab.set_accepted_calibration(record))
        return record


class ConstructionAndHtrTests(EqualizerTabCase):
    def test_htr_is_visible_but_unavailable(self) -> None:
        self.assertEqual(set(self.tab._sliders), set(CLASS_LABELS))
        self.assertFalse(self.tab._sliders["HTR"].isEnabled())
        self.assertEqual(self.tab._sliders["HTR"].value(), 0)
        self.assertEqual(self.tab._slider_value_labels["HTR"].text(), "N/A")
        self.assertIn("canonical", self.tab._sliders["HTR"].toolTip())

    def test_reset_normalizes_only_four_active_classes(self) -> None:
        weights = self.tab._current_weights()
        self.assertEqual(weights["HTR"], None)
        self.assertAlmostEqual(
            sum(float(weights[label]) for label in ACTIVE_SIMULATOR_LABELS),
            1.0,
        )
        for label in ACTIVE_SIMULATOR_LABELS:
            self.assertEqual(self.tab._sliders[label].value(), 25)

    def test_stable_widget_names(self) -> None:
        self.assertEqual(self.tab._selected_view.objectName(), "selectedRheedView")
        self.assertEqual(
            self.tab._constructed_view.objectName(), "constructedRheedView",
        )
        self.assertEqual(
            self.tab._candidate_combo.objectName(), "alignmentCandidateSelector",
        )
        self.assertEqual(self.tab._save_btn.objectName(), "saveLiveLabelButton")
        self.assertEqual(
            self.tab._preview_basis_combo.objectName(),
            "alignmentPreviewBasisSelector",
        )
        self.assertEqual(
            self.tab._handedness_evidence_combo.objectName(),
            "handednessEvidenceSelector",
        )


class LocalDemoGateTests(EqualizerTabCase):
    def setUp(self) -> None:
        super().setUp()
        self.tab.set_session_id("")
        self.tab.set_save_enabled(False)
        self.tab.update_qc_context(
            session_active=False,
            view_segment_id=0,
            visual_history_generation=0,
            gun_aligned=False,
            realignment_active=False,
        )

    def test_dummy_frame_allows_calibration_without_session_or_alignment(self) -> None:
        self.tab.update_camera_frame(
            _rgb(),
            _metadata(
                backend="dummy_c6x2",
                hwnd=0,
                monotonic_ns=1,
                geometry_id="dummy:full",
            ),
        )
        self.assertTrue(self.tab._calibrate_btn.isEnabled())

        record = self.accepted_record()
        self.assertTrue(self.tab.set_accepted_calibration(record))
        self.assertTrue(self.tab._auto_fit_btn.isEnabled())
        self.assertFalse(self.tab._save_btn.isEnabled())

    def test_real_camera_still_requires_session_and_alignment(self) -> None:
        self.tab.update_camera_frame(_rgb(), _metadata(backend="wgc"))
        self.assertFalse(self.tab._calibrate_btn.isEnabled())
        self.assertIn("session", self.tab._cal_status_label.text())


class SnapshotAtomicityTests(EqualizerTabCase):
    def test_camera_update_copies_source_arrays(self) -> None:
        self.make_live()
        source = _rgb(np.full((PROCESS_H, PROCESS_W), 77, dtype=np.float32))
        self.tab.update_camera_frame(source, _metadata(2))
        source[:] = 0
        snapshot = self.tab.get_current_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(int(snapshot.rgb[0, 0, 0]), 77)
        self.assertFalse(snapshot.rgb.flags.writeable)

    def test_calibration_snapshot_does_not_follow_new_frames(self) -> None:
        self.make_live(sequence=10)
        failure = LandmarkDetection.failure("no peaks")
        with patch(
            "gui.live_equalizer_tab.detect_live_landmarks", return_value=failure,
        ):
            self.tab._on_calibrate_clicked()
        frozen = self.tab._calibration_snapshot
        self.assertIsNotNone(frozen)
        self.assertIs(self.tab.get_calibration_snapshot(), frozen)
        frozen_pixels = np.array(frozen.rgb, copy=True)

        self.tab.update_camera_frame(
            _rgb(np.full((PROCESS_H, PROCESS_W), 200, dtype=np.float32)),
            _metadata(11),
        )
        self.assertEqual(self.tab.get_current_snapshot().capture_sequence, 11)
        self.assertEqual(self.tab._calibration_snapshot.capture_sequence, 10)
        self.assertTrue(np.array_equal(self.tab._calibration_snapshot.rgb, frozen_pixels))

    def test_clear_camera_fails_closed(self) -> None:
        self.make_live()
        self.activate_calibration()
        reasons: list[str] = []
        self.tab.calibration_invalidation_requested.connect(reasons.append)
        self.tab.clear_camera_frame("RHEED unavailable")
        self.assertIsNone(self.tab.get_current_snapshot())
        self.assertIsNone(self.tab.get_calibration())
        self.assertEqual(reasons, ["camera disconnected"])


class CandidateReviewTests(EqualizerTabCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_live()

    def start_detected_calibration(self) -> None:
        with patch(
            "gui.live_equalizer_tab.detect_live_landmarks",
            return_value=_detection(),
        ):
            self.tab._on_calibrate_clicked()

    def test_detection_generates_reviewable_normal_and_mirror_hypotheses(self) -> None:
        self.start_detected_calibration()
        aliases = {
            alias
            for candidate in self.tab._candidates
            for alias in candidate.equivalent_hypotheses
        }
        self.assertTrue(any(alias.startswith("normal:") for alias in aliases))
        self.assertTrue(any(alias.startswith("mirrored:") for alias in aliases))
        self.assertGreater(self.tab._candidate_combo.count(), 0)
        self.assertIsNone(self.tab.get_calibration())
        self.assertIn("Request acceptance", self.tab._calibrate_btn.text())

    def test_accept_button_emits_pending_record_only(self) -> None:
        self.start_detected_calibration()
        self.confirm_handedness()
        emitted: list[CalibrationRecord] = []
        self.tab.calibration_accept_requested.connect(emitted.append)
        self.tab._request_candidate_acceptance()
        self.assertEqual(len(emitted), 1)
        self.assertFalse(emitted[0].grower_accepted)
        self.assertIsNone(self.tab.get_calibration())
        self.assertFalse(self.tab._save_btn.isEnabled())

    def test_app_confirmation_activates_calibration(self) -> None:
        self.start_detected_calibration()
        self.confirm_handedness("c(6x2)")
        emitted: list[CalibrationRecord] = []
        self.tab.calibration_accept_requested.connect(emitted.append)
        self.tab._request_candidate_acceptance()
        snapshot = self.tab.get_calibration_snapshot()
        self.assertIsNotNone(snapshot)
        accepted = emitted[0].accepted(
            "grower",
            snapshot.orientation_evidence_sha256(),
            orientation_evidence_kind=self.tab.get_handedness_evidence_kind(),
        )
        self.assertTrue(self.tab.set_accepted_calibration(accepted))
        self.assertEqual(self.tab.get_calibration().calibration_id, accepted.calibration_id)
        self.assertTrue(self.tab._auto_fit_btn.isEnabled())
        self.assertTrue(self.tab._save_btn.isEnabled())

    def test_acceptance_requires_explicit_asymmetric_evidence(self) -> None:
        self.start_detected_calibration()
        emitted: list[CalibrationRecord] = []
        self.tab.calibration_accept_requested.connect(emitted.append)

        self.assertFalse(self.tab._calibrate_btn.isEnabled())
        self.tab._request_candidate_acceptance()
        self.assertEqual(emitted, [])
        self.assertIn("asymmetric", self.tab._cal_status_label.text())

        evidence_index = self.tab._handedness_evidence_combo.findData("RT13")
        self.tab._handedness_evidence_combo.setCurrentIndex(evidence_index)
        self.assertFalse(self.tab._calibrate_btn.isEnabled())
        self.tab._handedness_confirm.setChecked(True)
        self.assertTrue(self.tab._calibrate_btn.isEnabled())
        self.tab._request_candidate_acceptance()
        self.assertEqual(len(emitted), 1)

    def test_preview_switches_all_four_warped_bases(self) -> None:
        self.start_detected_calibration()
        self.assertEqual(
            tuple(
                self.tab._preview_basis_combo.itemData(index)
                for index in range(self.tab._preview_basis_combo.count())
            ),
            ACTIVE_SIMULATOR_LABELS,
        )
        for label in ACTIVE_SIMULATOR_LABELS:
            with self.subTest(label=label):
                index = self.tab._preview_basis_combo.findData(label)
                with patch.object(self.tab, "_set_scene_rgb") as render:
                    self.tab._preview_basis_combo.setCurrentIndex(index)
                    self.tab._render_candidate_preview(
                        self.tab._selected_candidate,
                    )
                self.assertEqual(self.tab._preview_basis_label, label)
                overlay = render.call_args.args[1]
                expected = int(np.clip(
                    self.tab._warped_basis[label][0, 0], 0, 255,
                ))
                self.assertEqual(int(overlay[0, 0, 0]), expected)
                self.assertEqual(int(overlay[0, 0, 2]), expected)

    def test_candidate_change_resets_handedness_confirmation(self) -> None:
        self.start_detected_calibration()
        self.confirm_handedness("streak/tail")
        first = self.tab.get_handedness_evidence()
        self.assertTrue(first["confirmed"])
        self.assertEqual(first["kind"], "streak/tail")
        self.tab._candidate_combo.setCurrentIndex(1)
        self.assertEqual(self.tab.get_handedness_evidence_kind(), "")
        self.assertFalse(self.tab._handedness_confirm.isChecked())

    def test_reconstruction_evidence_must_match_previewed_basis(self) -> None:
        self.start_detected_calibration()
        self.confirm_handedness("c(6x2)")
        self.assertEqual(self.tab._preview_basis_label, "c(6x2)")

        preview_index = self.tab._preview_basis_combo.findData("1x1")
        self.assertGreaterEqual(preview_index, 0)
        self.tab._preview_basis_combo.setCurrentIndex(preview_index)
        self.tab._handedness_confirm.setChecked(True)

        self.assertEqual(self.tab.get_handedness_evidence_kind(), "")
        self.assertFalse(self.tab._calibrate_btn.isEnabled())

        # Streak/tail evidence is independent of the reconstruction preview.
        evidence_index = self.tab._handedness_evidence_combo.findData(
            "streak/tail",
        )
        self.tab._handedness_evidence_combo.setCurrentIndex(evidence_index)
        self.tab._handedness_confirm.setChecked(True)
        self.assertEqual(
            self.tab.get_handedness_evidence_kind(),
            "streak/tail",
        )

    def test_failed_auto_detection_enters_manual_mode(self) -> None:
        with patch(
            "gui.live_equalizer_tab.detect_live_landmarks",
            return_value=LandmarkDetection.failure("low SNR"),
        ):
            self.tab._on_calibrate_clicked()
        self.assertTrue(self.tab._calibrating)
        self.assertEqual(self.tab._manual_clicks, [])
        self.assertIn("Automatic detection failed", self.tab._cal_status_label.text())

    def test_retry_keeps_original_frozen_snapshot(self) -> None:
        self.start_detected_calibration()
        original_sequence = self.tab._calibration_snapshot.capture_sequence
        self.tab.update_camera_frame(_rgb(), _metadata(sequence=99))
        self.tab._on_clear_cal_clicked()
        self.assertEqual(
            self.tab._calibration_snapshot.capture_sequence,
            original_sequence,
        )
        self.assertEqual(self.tab.get_current_snapshot().capture_sequence, 99)

    def test_review_invalidates_on_every_capture_geometry_discontinuity(self) -> None:
        reasons: list[str] = []
        self.tab.calibration_invalidation_requested.connect(reasons.append)
        discontinuities = (
            ("backend", {"backend": "mss"}, _rgb()),
            ("HWND", {"hwnd": 5678}, _rgb()),
            (
                "dimensions",
                {"width": PROCESS_W + 1},
                np.pad(_rgb(), ((0, 0), (0, 1), (0, 0))),
            ),
            (
                "geometry/DPI",
                {
                    "geometry_id": (
                        "test-wgc-roi-v1:dpi-getdpiforwindow-144"
                    ),
                },
                _rgb(),
            ),
        )
        for sequence, (name, overrides, frame) in enumerate(
            discontinuities,
            start=20,
        ):
            with self.subTest(discontinuity=name):
                self.tab.update_camera_frame(_rgb(), _metadata(sequence))
                self.start_detected_calibration()
                self.assertTrue(self.tab._calibrating)
                before = len(reasons)
                self.tab.update_camera_frame(
                    frame,
                    _metadata(sequence + 100, **overrides),
                )
                self.assertEqual(len(reasons), before + 1)
                self.assertFalse(self.tab._calibrating)
                self.assertIsNone(self.tab._calibration_snapshot)
                self.assertIsNone(self.tab._pending_calibration)
                self.assertIsNone(self.tab._selected_candidate)

    def test_pending_review_observes_discontinuity_while_paused(self) -> None:
        self.start_detected_calibration()
        self.confirm_handedness("RT13")
        emitted: list[CalibrationRecord] = []
        self.tab.calibration_accept_requested.connect(emitted.append)
        self.tab._request_candidate_acceptance()
        self.assertEqual(len(emitted), 1)
        self.assertIsNotNone(self.tab._pending_calibration)
        frozen_sequence = self.tab.get_current_snapshot().capture_sequence

        self.tab._pause_btn.setChecked(True)
        self.tab.update_camera_frame(
            _rgb(),
            _metadata(
                44,
                geometry_id="test-wgc-roi-v1:dpi-getdpiforwindow-144",
            ),
        )
        self.assertEqual(
            self.tab.get_current_snapshot().capture_sequence,
            frozen_sequence,
        )
        self.assertFalse(self.tab._calibrating)
        self.assertIsNone(self.tab._calibration_snapshot)
        self.assertIsNone(self.tab._pending_calibration)

    def test_geometry_change_away_then_back_does_not_restore_review(self) -> None:
        self.start_detected_calibration()
        self.tab.update_camera_frame(
            _rgb(),
            _metadata(
                50,
                geometry_id="test-wgc-roi-v1:dpi-getdpiforwindow-144",
            ),
        )
        self.assertFalse(self.tab._calibrating)
        self.assertIsNone(self.tab._calibration_snapshot)

        self.tab.update_camera_frame(_rgb(), _metadata(51))
        self.assertFalse(self.tab._calibrating)
        self.assertIsNone(self.tab._calibration_snapshot)
        self.assertIsNone(self.tab._pending_calibration)


class SaveAndQcGateTests(EqualizerTabCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_live()

    def test_save_requires_accepted_calibration_and_emits_htr_none(self) -> None:
        self.assertFalse(self.tab._save_btn.isEnabled())
        self.activate_calibration()
        emitted: list[dict] = []
        self.tab.live_label_save_requested.connect(emitted.append)
        self.tab._save_btn.click()
        self.assertEqual(len(emitted), 1)
        self.assertIsNone(emitted[0]["final_weights"]["HTR"])
        self.assertIsNone(emitted[0]["normalized_weights"]["HTR"])
        self.assertEqual(emitted[0]["schema_version"], 1)
        self.assertEqual(emitted[0]["label_source"], "equalizer_manual")
        self.assertGreaterEqual(emitted[0]["residual_rms"], 0.0)

    def test_qc_view_segment_change_invalidates_and_emits(self) -> None:
        self.activate_calibration()
        emitted: list[str] = []
        self.tab.calibration_invalidation_requested.connect(emitted.append)
        self.tab.update_qc_context(
            session_active=True,
            view_segment_id=5,
            visual_history_generation=3,
            gun_aligned=True,
            realignment_active=False,
        )
        self.assertIsNone(self.tab.get_calibration())
        self.assertFalse(self.tab._save_btn.isEnabled())
        self.assertEqual(emitted, ["view segment changed"])

    def test_realign_start_invalidates(self) -> None:
        self.activate_calibration()
        self.tab.update_qc_context(
            session_active=True,
            view_segment_id=4,
            visual_history_generation=2,
            gun_aligned=True,
            realignment_active=True,
        )
        self.assertIsNone(self.tab.get_calibration())

    def test_camera_hwnd_change_invalidates(self) -> None:
        self.activate_calibration()
        self.tab.update_camera_frame(_rgb(), _metadata(2, hwnd=5678))
        self.assertIsNone(self.tab.get_calibration())
        self.assertFalse(self.tab._save_btn.isEnabled())

    def test_wgc_dpi_change_invalidates_accepted_calibration(self) -> None:
        self.activate_calibration()
        self.tab.update_camera_frame(
            _rgb(),
            _metadata(
                2,
                geometry_id="test-wgc-roi-v1:dpi-getdpiforwindow-144",
            ),
        )
        self.assertIsNone(self.tab.get_calibration())
        self.assertFalse(self.tab._save_btn.isEnabled())
        self.assertIn("geometry changed", self.tab._cal_status_label.text())

    def test_programmatic_invalidation_does_not_emit(self) -> None:
        self.activate_calibration()
        emitted: list[str] = []
        self.tab.calibration_invalidation_requested.connect(emitted.append)
        self.tab.invalidate_calibration("app-owned invalidation", emit=False)
        self.assertEqual(emitted, [])


class PauseAndClassifierTests(EqualizerTabCase):
    def test_pause_preserves_displayed_snapshot(self) -> None:
        self.make_live(sequence=1)
        self.tab._pause_btn.setChecked(True)
        self.tab.update_camera_frame(_rgb(), _metadata(2))
        self.assertEqual(self.tab.get_current_snapshot().capture_sequence, 1)
        self.tab._pause_btn.setChecked(False)
        self.tab.update_camera_frame(_rgb(), _metadata(3))
        self.assertEqual(self.tab.get_current_snapshot().capture_sequence, 3)

    def test_classifier_mapping_is_preserved(self) -> None:
        self.tab.update_classifier_state(ClassifierState(
            ready=True,
            smoothed_percent={
                "1x1": 50,
                "Twinned (2x1)": 20,
                "c(6x2)": 10,
                "rt13xrt13": 15,
                "HTR": 5,
            },
        ))
        self.assertEqual(self.tab._classifier_value_labels["Tw(2x1)"].text(), "20%")
        self.assertEqual(self.tab._classifier_value_labels["RT13"].text(), "15%")


class RetrospectiveGateTests(EqualizerTabCase):
    retrospective = True

    def test_historical_snapshot_ignores_age_and_current_session_gate(self) -> None:
        self.tab.set_session_id("HISTORICAL-SESSION")
        self.tab.update_qc_context(
            session_active=False,
            view_segment_id=4,
            visual_history_generation=2,
            gun_aligned=True,
            realignment_active=False,
        )
        metadata = _metadata(monotonic_ns=1)
        metadata["frame_age_ms"] = 27.5
        self.tab.update_camera_frame(_rgb(), metadata)
        record = self.accepted_record()
        self.assertTrue(self.tab.set_accepted_calibration(record))
        self.assertTrue(self.tab._save_btn.isEnabled())
        self.assertEqual(
            self.tab.get_current_capture_metadata()["frame_age_ms"],
            27.5,
        )

    def test_historical_mode_still_requires_session_id(self) -> None:
        self.tab.update_qc_context(
            session_active=False,
            view_segment_id=4,
            visual_history_generation=2,
            gun_aligned=True,
            realignment_active=False,
        )
        self.tab.update_camera_frame(_rgb(), _metadata(monotonic_ns=1))
        self.assertFalse(self.tab._calibrate_btn.isEnabled())


class StandaloneDisabledTests(unittest.TestCase):
    def test_direct_entry_returns_nonzero_with_growth_monitor_guidance(self) -> None:
        from scripts import equalizer_ui

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = equalizer_ui.main()
        self.assertNotEqual(result, 0)
        self.assertIn("Growth Monitor", stderr.getvalue())

    def test_legacy_window_cannot_save_or_call_callback(self) -> None:
        from scripts.equalizer_ui import EqualizerWindow

        callbacks: list[dict] = []
        with patch(
            "scripts.equalizer_ui.load_class_means",
            return_value=self._basis_images(),
        ):
            window = EqualizerWindow(on_save_callback=callbacks.append)
        try:
            self.assertFalse(window._standalone_save_btn.isEnabled())
            window.action_save()
            self.assertEqual(callbacks, [])
            self.assertIn("Growth Monitor", window.statusbar.currentMessage())
        finally:
            window.deleteLater()

    @staticmethod
    def _basis_images() -> dict[str, np.ndarray]:
        base = _spot_image()
        return {
            label: np.array(base, copy=True)
            for label in ACTIVE_SIMULATOR_LABELS
        }


if __name__ == "__main__":
    unittest.main(verbosity=2)
