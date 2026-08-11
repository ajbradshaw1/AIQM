"""Focused tests for app-owned Equalizer calibration and Save gating.

The tests call ``GrowthApp`` handlers on a synchronous harness.  No real
``QApplication``, camera, worker thread, or filesystem logger is started.

Run from the repository root with::

    python -m pytest -q tests/test_equalizer_app_integration.py
"""
from __future__ import annotations

import sys
import time
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ``growth_app`` imports plotting windows even though this focused suite does
# not construct them.  Keep the tests runnable when pyqtgraph is optional.
try:
    import pyqtgraph  # noqa: F401
except ModuleNotFoundError:
    sys.modules["pyqtgraph"] = types.ModuleType("pyqtgraph")

from gui.equalizer_alignment import (  # noqa: E402
    BasisBundle,
    CalibrationRecord,
    RheedFrameSnapshot,
)
from gui.growth_app import GrowthApp  # noqa: E402
from gui.state import RheedQcState  # noqa: E402
from gui.equalizer_label_contract import build_equalizer_payload  # noqa: E402
from scripts.equalizer_ui import load_basis_bundle  # noqa: E402


SESSION_ID = "20260731_120000_equalizer-test"
SOURCE_HWND = 4242
VIEW_SEGMENT_ID = 3
VISUAL_HISTORY_GENERATION = 7
_TEST_BASIS_BUNDLE = load_basis_bundle()
BASIS_BUNDLE_ID = _TEST_BASIS_BUNDLE.bundle_id
CAPTURE_GEOMETRY_ID = "ksa-chrome-v1:1:75:30"


def _equalizer_payload(
    weights: dict[str, float] | None = None,
) -> dict[str, object]:
    final = {
        "1x1": 1.0,
        "Tw(2x1)": 0.0,
        "c(6x2)": 0.0,
        "RT13": 0.0,
        "HTR": None,
    }
    if weights:
        final.update(weights)
    return build_equalizer_payload(
        raw_weights=None,
        final_weights=final,
        fit_mode="manual",
        normalization_applied=False,
        residual_rms=0.0,
        valid_coverage=1.0,
    )


class _StatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, message: str, *_args: object) -> None:
        self.messages.append(message)


class _TextInput:
    def __init__(self, value: str) -> None:
        self.value = value

    def text(self) -> str:
        return self.value


class _LiveTab:
    def __init__(self, snapshot: RheedFrameSnapshot | None) -> None:
        self.snapshot = snapshot
        self.accepted: list[CalibrationRecord] = []
        self.invalidations: list[tuple[str, bool]] = []
        self.accept_result = True
        self.basis_bundle: BasisBundle = _TEST_BASIS_BUNDLE

    def get_current_snapshot(self) -> RheedFrameSnapshot | None:
        return self.snapshot

    def get_current_capture_metadata(self) -> dict[str, object]:
        return {"basis_bundle_id": BASIS_BUNDLE_ID}

    def get_basis_bundle_id(self) -> str:
        return self.basis_bundle.bundle_id

    def get_basis_bundle(self) -> BasisBundle:
        return self.basis_bundle

    def get_calibration_snapshot(self) -> RheedFrameSnapshot | None:
        return self.snapshot

    def get_handedness_evidence_kind(self) -> str:
        return "Tw(2x1)"

    def set_accepted_calibration(self, record: CalibrationRecord) -> bool:
        self.accepted.append(record)
        return self.accept_result

    def invalidate_calibration(self, reason: str, *, emit: bool = False) -> None:
        self.invalidations.append((reason, emit))


class _EventsTab:
    def __init__(self) -> None:
        self.completed_resolutions: list[
            tuple[str, CalibrationRecord | None, str]
        ] = []
        self.failed_resolutions: list[tuple[str, str]] = []
        self.completed_accepts: list[tuple[str, CalibrationRecord]] = []
        self.failed_accepts: list[tuple[str, str]] = []
        self.completed_invalidations: list[str] = []
        self.failed_invalidations: list[tuple[str, str]] = []
        self.accept_completion_result = True
        self.resolve_completion_result = True

    def complete_retrospective_calibration_resolution(
        self,
        token: str,
        calibration: CalibrationRecord | None,
        reason: str = "",
    ) -> bool:
        self.completed_resolutions.append((token, calibration, reason))
        return self.resolve_completion_result

    def fail_retrospective_calibration_resolution(
        self,
        token: str,
        reason: str,
    ) -> bool:
        self.failed_resolutions.append((token, reason))
        return True

    def complete_retrospective_calibration_acceptance(
        self,
        token: str,
        calibration: CalibrationRecord,
    ) -> bool:
        self.completed_accepts.append((token, calibration))
        return self.accept_completion_result

    def fail_retrospective_calibration_acceptance(
        self,
        token: str,
        reason: str,
    ) -> bool:
        self.failed_accepts.append((token, reason))
        return True

    def complete_retrospective_calibration_invalidation(
        self,
        token: str,
    ) -> bool:
        self.completed_invalidations.append(token)
        return True

    def fail_retrospective_calibration_invalidation(
        self,
        token: str,
        reason: str,
    ) -> bool:
        self.failed_invalidations.append((token, reason))
        return True


class _Monitor:
    def __init__(self, snapshot: RheedFrameSnapshot | None) -> None:
        self.live_equalizer_tab = _LiveTab(snapshot)
        self.events_tab = _EventsTab()
        self.grower_input = _TextInput("GROWER_A")
        self._latest_mistral = None
        self._latest_psu = None
        self._latest_pyro = None
        self.updated_qc_states: list[RheedQcState] = []

    def get_current_capture_metadata(self) -> dict[str, object]:
        if self.live_equalizer_tab.snapshot is None:
            return {}
        snapshot = self.live_equalizer_tab.snapshot
        return {
            "source_hwnd": snapshot.source_hwnd,
            "camera_width": snapshot.camera_width,
            "camera_height": snapshot.camera_height,
            "capture_backend": snapshot.capture_backend,
            "capture_geometry_id": snapshot.capture_geometry_id,
        }

    def get_elapsed_seconds(self) -> float:
        return 12.5

    def update_rheed_qc_state(self, state: RheedQcState) -> None:
        self.updated_qc_states.append(state)


class _GrowthLog:
    def __init__(self) -> None:
        self.active = True
        self.session_dir = Path("test-output") / SESSION_ID
        self.record_calibration = MagicMock(return_value=True)
        self.record_calibration_invalidation = MagicMock(return_value=True)
        self.record_live_label = MagicMock(return_value=1)
        self.historical_calibrations: dict[str, CalibrationRecord] = {}
        self.get_historical_calibration = MagicMock(
            side_effect=self.historical_calibrations.get,
        )


class _AppHarness:
    """Only the attributes used by the Equalizer integration handlers."""

    def __init__(
        self,
        snapshot: RheedFrameSnapshot | None,
        calibration: CalibrationRecord | None,
        *,
        qc_state: RheedQcState | None = None,
    ) -> None:
        self.monitor = _Monitor(snapshot)
        self.growth_log = _GrowthLog()
        self._equalizer_calibration = calibration
        self._retrospective_equalizer_calibrations = {}
        self._rheed_qc_state = qc_state or _stable_qc_state()
        self.classifier_worker = None
        self._status_bar = _StatusBar()

        # The production handlers call these through ``self``.  Bind the real
        # helper methods so this harness also exercises invalidation logging.
        self._equalizer_session_id = types.MethodType(
            GrowthApp._equalizer_session_id,
            self,
        )
        self._invalidate_equalizer_calibration = types.MethodType(
            GrowthApp._invalidate_equalizer_calibration,
            self,
        )
        self._fail_retrospective_calibration_acceptance = types.MethodType(
            GrowthApp._fail_retrospective_calibration_acceptance,
            self,
        )
        self._fail_retrospective_calibration_resolution = types.MethodType(
            GrowthApp._fail_retrospective_calibration_resolution,
            self,
        )

    def statusBar(self) -> _StatusBar:
        return self._status_bar


def _stable_qc_state(
    *,
    session_active: bool = True,
    view_segment_id: int = VIEW_SEGMENT_ID,
    visual_history_generation: int = VISUAL_HISTORY_GENERATION,
    gun_aligned: bool | None = True,
    realignment_active: bool = False,
) -> RheedQcState:
    return RheedQcState(
        session_active=session_active,
        view_segment_id=view_segment_id,
        visual_history_generation=visual_history_generation,
        gun_aligned=gun_aligned,
        realignment_active=realignment_active,
    )


def _snapshot(
    *,
    source_hwnd: int = SOURCE_HWND,
    received_monotonic_ns: int | None = None,
    retrospective: bool = False,
    capture_backend: str = "wgc",
    session_id: str = SESSION_ID,
    gun_aligned: bool = True,
) -> RheedFrameSnapshot:
    if received_monotonic_ns is None:
        received_monotonic_ns = time.monotonic_ns()
    return RheedFrameSnapshot.freeze(
        np.full((96, 128, 3), 70, dtype=np.uint8),
        np.full((96, 128), 0.25, dtype=np.float32),
        captured_at_utc="2026-07-31T17:00:00+00:00",
        received_monotonic_ns=received_monotonic_ns,
        capture_sequence=91,
        source_hwnd=source_hwnd,
        capture_backend=capture_backend,
        capture_geometry_id=CAPTURE_GEOMETRY_ID,
        camera_width=128,
        camera_height=96,
        session_id=session_id,
        view_segment_id=VIEW_SEGMENT_ID,
        visual_history_generation=VISUAL_HISTORY_GENERATION,
        gun_aligned=gun_aligned,
        realignment_active=False,
        source_frame_age_ms=5.0 if retrospective else None,
        retrospective=retrospective,
    )


def _calibration(
    snapshot: RheedFrameSnapshot,
    *,
    grower_accepted: bool,
    calibration_id: str = "calibration-test",
) -> CalibrationRecord:
    points = np.array(
        [[30.0, 48.0], [64.0, 42.0], [98.0, 48.0]],
        dtype=np.float64,
    )
    record = CalibrationRecord(
        calibration_id=calibration_id,
        basis_bundle_id=BASIS_BUNDLE_ID,
        candidate_id="candidate-test",
        matrix=np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]),
        parity="normal",
        endpoint_order="forward",
        basis_points=points,
        live_points=points,
        residuals_px=np.zeros(3),
        rotation_deg=0.0,
        scale=1.0,
        rms_residual_px=0.0,
        max_residual_px=0.0,
        valid_coverage=1.0,
        correlation=1.0,
        source_hwnd=snapshot.source_hwnd,
        camera_width=snapshot.camera_width,
        camera_height=snapshot.camera_height,
        capture_backend=snapshot.capture_backend,
        capture_geometry_id=snapshot.capture_geometry_id,
        captured_at_utc=snapshot.captured_at_utc,
        capture_sequence=snapshot.capture_sequence,
        received_monotonic_ns=snapshot.received_monotonic_ns,
        session_id=snapshot.session_id,
        view_segment_id=snapshot.view_segment_id,
        visual_history_generation=snapshot.visual_history_generation,
        gun_aligned=snapshot.gun_aligned,
        realignment_active=snapshot.realignment_active,
        grower_accepted=False,
    )
    if not grower_accepted:
        return record
    return record.accepted(
        "GROWER_A",
        snapshot.orientation_evidence_sha256(),
        orientation_evidence_kind="Tw(2x1)",
    )


class EqualizerAppIntegrationTests(unittest.TestCase):
    def test_auto_capture_metadata_binds_session_basis_and_calibration(self) -> None:
        snapshot = _snapshot()
        calibration = _calibration(snapshot, grower_accepted=True)
        app = _AppHarness(snapshot, calibration)

        metadata = GrowthApp._current_auto_capture_metadata(app)

        self.assertEqual(metadata["session_id"], SESSION_ID)
        self.assertEqual(metadata["basis_bundle_id"], BASIS_BUNDLE_ID)
        self.assertEqual(
            metadata["calibration_id"], calibration.calibration_id,
        )
        self.assertEqual(metadata["source_hwnd"], SOURCE_HWND)

    def test_accept_journals_before_activating_app_owned_record(self) -> None:
        snapshot = _snapshot()
        pending = _calibration(snapshot, grower_accepted=False)
        app = _AppHarness(snapshot, None)

        GrowthApp._on_equalizer_calibration_accept(app, pending)

        app.growth_log.record_calibration.assert_called_once()
        journaled = app.growth_log.record_calibration.call_args.args[0]
        self.assertIsInstance(journaled, CalibrationRecord)
        self.assertTrue(journaled.grower_accepted)
        self.assertEqual(journaled.accepted_by, "GROWER_A")
        self.assertEqual(journaled.orientation_evidence_kind, "Tw(2x1)")
        self.assertEqual(
            journaled.orientation_evidence_sha256,
            snapshot.orientation_evidence_sha256(),
        )
        self.assertIs(
            app.growth_log.record_calibration.call_args.kwargs[
                "evidence_snapshot"
            ],
            snapshot,
        )
        self.assertIs(
            app.growth_log.record_calibration.call_args.kwargs["basis_bundle"],
            _TEST_BASIS_BUNDLE,
        )
        self.assertIs(app._equalizer_calibration, journaled)
        self.assertEqual(app.monitor.live_equalizer_tab.accepted, [journaled])
        self.assertIn("Equalizer calibrated", app._status_bar.messages[-1])

    def test_dummy_accepts_in_memory_without_active_session_or_journal(self) -> None:
        snapshot = _snapshot(
            source_hwnd=0,
            capture_backend="dummy_tw",
            session_id="",
            gun_aligned=False,
        )
        pending = _calibration(snapshot, grower_accepted=False)
        app = _AppHarness(
            snapshot,
            None,
            qc_state=_stable_qc_state(
                session_active=False,
                gun_aligned=False,
            ),
        )
        app.growth_log.active = False
        app.growth_log.session_dir = None

        GrowthApp._on_equalizer_calibration_accept(app, pending)

        app.growth_log.record_calibration.assert_not_called()
        self.assertIsNotNone(app._equalizer_calibration)
        self.assertEqual(len(app.monitor.live_equalizer_tab.accepted), 1)
        self.assertIn("Local alignment demo active", app._status_bar.messages[-1])

    def test_real_camera_accept_still_rejects_inactive_session(self) -> None:
        snapshot = _snapshot()
        pending = _calibration(snapshot, grower_accepted=False)
        app = _AppHarness(
            snapshot,
            None,
            qc_state=_stable_qc_state(
                session_active=False,
                gun_aligned=False,
            ),
        )
        app.growth_log.active = False

        GrowthApp._on_equalizer_calibration_accept(app, pending)

        app.growth_log.record_calibration.assert_not_called()
        self.assertIsNone(app._equalizer_calibration)
        self.assertIn(
            "running session",
            app.monitor.live_equalizer_tab.invalidations[-1][0],
        )

    def test_live_save_passes_exact_app_calibration_and_tab_snapshot(self) -> None:
        snapshot = _snapshot()
        calibration = _calibration(snapshot, grower_accepted=True)
        app = _AppHarness(snapshot, calibration)
        payload = _equalizer_payload({
            "1x1": 0.8, "Tw(2x1)": 0.1, "c(6x2)": 0.1, "RT13": 0.0,
        })
        weights = payload["final_weights"]

        GrowthApp._on_live_label_save(app, payload)

        app.growth_log.record_live_label.assert_called_once()
        kwargs = app.growth_log.record_live_label.call_args.kwargs
        self.assertIs(kwargs["calibration"], calibration)
        self.assertIs(kwargs["snapshot"], snapshot)
        self.assertIs(kwargs["weights"], weights)
        self.assertEqual(kwargs["elapsed_s"], 12.5)

    def test_accept_rejects_basis_bundle_identity_mismatch(self) -> None:
        snapshot = _snapshot()
        pending = _calibration(snapshot, grower_accepted=False)
        app = _AppHarness(snapshot, None)
        app.monitor.live_equalizer_tab.basis_bundle = replace(
            _TEST_BASIS_BUNDLE,
            version="mismatched-test-bundle",
            bundle_id="",
        )

        GrowthApp._on_equalizer_calibration_accept(app, pending)

        app.growth_log.record_calibration.assert_not_called()
        self.assertIn(
            "mismatched",
            app.monitor.live_equalizer_tab.invalidations[-1][0],
        )

    def test_live_save_rejects_missing_equalizer_context_without_write(self) -> None:
        snapshot = _snapshot()
        calibration = _calibration(snapshot, grower_accepted=True)
        cases = (
            ("missing calibration", snapshot, None),
            ("missing snapshot", None, calibration),
        )
        for label, current_snapshot, current_calibration in cases:
            with self.subTest(label=label):
                app = _AppHarness(current_snapshot, current_calibration)
                GrowthApp._on_live_label_save(app, _equalizer_payload())
                app.growth_log.record_live_label.assert_not_called()

    def test_live_save_rejects_stale_frame_without_write(self) -> None:
        stale_snapshot = _snapshot(
            received_monotonic_ns=time.monotonic_ns() - 10_000_000_000,
        )
        calibration = _calibration(stale_snapshot, grower_accepted=True)
        app = _AppHarness(stale_snapshot, calibration)

        GrowthApp._on_live_label_save(app, _equalizer_payload())

        app.growth_log.record_live_label.assert_not_called()
        self.assertIn("frame is stale", app._status_bar.messages[-1])

    def test_live_save_rejects_incompatible_capture_and_invalidates(self) -> None:
        calibration_snapshot = _snapshot()
        incompatible_snapshot = _snapshot(source_hwnd=SOURCE_HWND + 1)
        calibration = _calibration(calibration_snapshot, grower_accepted=True)
        app = _AppHarness(incompatible_snapshot, calibration)

        GrowthApp._on_live_label_save(app, _equalizer_payload())

        app.growth_log.record_live_label.assert_not_called()
        app.growth_log.record_calibration_invalidation.assert_called_once()
        self.assertIsNone(app._equalizer_calibration)
        self.assertIn("camera HWND changed", app._status_bar.messages[-1])

    def test_live_save_rejects_unstable_qc_without_write(self) -> None:
        snapshot = _snapshot()
        calibration = _calibration(snapshot, grower_accepted=True)
        unstable_states = (
            _stable_qc_state(gun_aligned=False),
            _stable_qc_state(realignment_active=True),
        )
        for state in unstable_states:
            with self.subTest(state=state):
                app = _AppHarness(snapshot, calibration, qc_state=state)
                GrowthApp._on_live_label_save(app, _equalizer_payload())
                app.growth_log.record_live_label.assert_not_called()
                app.growth_log.record_calibration_invalidation.assert_called_once()
                self.assertIsNone(app._equalizer_calibration)

    def test_qc_view_or_generation_change_invalidates_app_calibration(self) -> None:
        cases = (
            (
                "view segment",
                _stable_qc_state(view_segment_id=VIEW_SEGMENT_ID + 1),
                False,
                "view segment changed",
            ),
            (
                "visual generation reset",
                _stable_qc_state(),
                True,
                "visual-history generation changed",
            ),
        )
        for label, next_state, reset_visual_history, expected_reason in cases:
            with self.subTest(label=label):
                snapshot = _snapshot()
                calibration = _calibration(snapshot, grower_accepted=True)
                app = _AppHarness(snapshot, calibration)

                GrowthApp._apply_rheed_qc_state(
                    app,
                    next_state,
                    reset_visual_history=reset_visual_history,
                )

                app.growth_log.record_calibration_invalidation.assert_called_once()
                args = app.growth_log.record_calibration_invalidation.call_args.args
                self.assertIs(args[0], calibration)
                self.assertEqual(args[1], expected_reason)
                self.assertIsNone(app._equalizer_calibration)
                self.assertEqual(
                    app.monitor.live_equalizer_tab.invalidations,
                    [(expected_reason, False)],
                )
                self.assertIs(app.monitor.updated_qc_states[-1], app._rheed_qc_state)

    def test_events_acceptance_is_app_owned_and_journaled_before_callback(self) -> None:
        snapshot = _snapshot(retrospective=True)
        pending = _calibration(snapshot, grower_accepted=False)
        app = _AppHarness(snapshot, None)
        payload = {
            "request_token": "request-1",
            "pending": pending,
            "snapshot": snapshot,
            "basis_bundle": _TEST_BASIS_BUNDLE,
            "evidence_kind": "c(6x2)",
            "evidence_confirmed": True,
            "retrospective": True,
        }

        GrowthApp._on_retrospective_calibration_accept(app, payload)

        app.growth_log.record_calibration.assert_called_once()
        accepted = app.growth_log.record_calibration.call_args.args[0]
        self.assertTrue(accepted.grower_accepted)
        self.assertEqual(accepted.orientation_evidence_kind, "c(6x2)")
        self.assertIs(
            app.growth_log.record_calibration.call_args.kwargs[
                "evidence_snapshot"
            ],
            snapshot,
        )
        self.assertIs(
            app.growth_log.record_calibration.call_args.kwargs["basis_bundle"],
            _TEST_BASIS_BUNDLE,
        )
        self.assertEqual(
            app.monitor.events_tab.completed_accepts,
            [("request-1", accepted)],
        )
        self.assertIs(
            app._retrospective_equalizer_calibrations[
                accepted.calibration_id
            ],
            accepted,
        )

    def test_events_historical_resolution_is_app_owned_and_preserves_priority(self) -> None:
        snapshot = _snapshot(retrospective=True)
        label_calibration = _calibration(snapshot, grower_accepted=True)
        manifest_calibration = _calibration(
            snapshot,
            grower_accepted=True,
            calibration_id="manifest-calibration",
        )
        app = _AppHarness(snapshot, None)
        app.growth_log.historical_calibrations.update({
            label_calibration.calibration_id: label_calibration,
            manifest_calibration.calibration_id: manifest_calibration,
        })

        GrowthApp._on_retrospective_calibration_resolve(app, {
            "request_token": "resolve-1",
            "calibration_ids": (
                label_calibration.calibration_id,
                manifest_calibration.calibration_id,
            ),
            "snapshot": snapshot,
            "basis_bundle_id": BASIS_BUNDLE_ID,
            "basis_bundle": _TEST_BASIS_BUNDLE,
            "retrospective": True,
        })

        self.assertEqual(
            app.growth_log.get_historical_calibration.call_args_list[0].args,
            (label_calibration.calibration_id,),
        )
        self.assertEqual(len(app.growth_log.get_historical_calibration.call_args_list), 1)
        self.assertEqual(
            app.monitor.events_tab.completed_resolutions,
            [("resolve-1", label_calibration, "")],
        )
        self.assertIs(
            app._retrospective_equalizer_calibrations[
                label_calibration.calibration_id
            ],
            label_calibration,
        )

    def test_events_historical_resolution_fails_closed_on_bad_request(self) -> None:
        app = _AppHarness(_snapshot(retrospective=True), None)

        GrowthApp._on_retrospective_calibration_resolve(app, {
            "request_token": "resolve-bad",
            "calibration_ids": ("calibration-test",),
            "snapshot": _snapshot(retrospective=False),
            "basis_bundle_id": BASIS_BUNDLE_ID,
            "basis_bundle": _TEST_BASIS_BUNDLE,
            "retrospective": True,
        })

        self.assertEqual(
            app.monitor.events_tab.failed_resolutions[0][0],
            "resolve-bad",
        )
        app.growth_log.get_historical_calibration.assert_not_called()

    def test_events_acceptance_fails_closed_when_journal_write_fails(self) -> None:
        snapshot = _snapshot(retrospective=True)
        pending = _calibration(snapshot, grower_accepted=False)
        app = _AppHarness(snapshot, None)
        app.growth_log.record_calibration.return_value = False

        GrowthApp._on_retrospective_calibration_accept(app, {
            "request_token": "request-fail",
            "pending": pending,
            "snapshot": snapshot,
            "basis_bundle": _TEST_BASIS_BUNDLE,
            "evidence_kind": "RT13",
            "evidence_confirmed": True,
            "retrospective": True,
        })

        self.assertFalse(app.monitor.events_tab.completed_accepts)
        self.assertEqual(
            app.monitor.events_tab.failed_accepts[0][0],
            "request-fail",
        )
        self.assertFalse(app._retrospective_equalizer_calibrations)

    def test_events_invalidation_is_journaled_by_app(self) -> None:
        snapshot = _snapshot(retrospective=True)
        accepted = _calibration(snapshot, grower_accepted=True)
        app = _AppHarness(snapshot, None)
        app._retrospective_equalizer_calibrations[accepted.calibration_id] = accepted

        GrowthApp._on_retrospective_calibration_invalidation(app, {
            "request_token": "invalidate-1",
            "calibration": accepted,
            "reason": "grower requested recalibration",
            "retrospective": True,
        })

        app.growth_log.record_calibration_invalidation.assert_called_once_with(
            accepted,
            "grower requested recalibration",
        )
        self.assertEqual(
            app.monitor.events_tab.completed_invalidations,
            ["invalidate-1"],
        )
        self.assertNotIn(
            accepted.calibration_id,
            app._retrospective_equalizer_calibrations,
        )


if __name__ == "__main__":
    unittest.main()
