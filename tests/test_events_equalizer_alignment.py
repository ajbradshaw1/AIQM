"""Headless tests for provenance-safe retrospective Equalizer labeling.

Run with::

    python -m pytest -q tests/test_events_equalizer_alignment.py
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

from gui.events_tab import (  # noqa: E402
    EventsTab,
    _EQUALIZER_MANIFEST_FIELDS,
)
from gui.growth_logger import GrowthLogger  # noqa: E402
from gui.equalizer_label_contract import build_equalizer_payload  # noqa: E402


class _Calibration:
    def __init__(
        self,
        calibration_id: str,
        basis_bundle_id: str = "bundle-v1",
        capture_geometry_id: str = "geometry-v1",
    ):
        self.calibration_id = calibration_id
        self.basis_bundle_id = basis_bundle_id
        self.capture_geometry_id = capture_geometry_id


class _PendingCalibration:
    def __init__(self, calibration_id: str = "new-cal"):
        self.calibration_id = calibration_id
        self.accepted_by = ""

    def accepted(self, accepted_by: str = "") -> _Calibration:
        self.accepted_by = accepted_by
        return _Calibration(self.calibration_id)


class _FakePanel(QWidget):
    calibration_accept_requested = pyqtSignal(object)
    calibration_invalidation_requested = pyqtSignal(str)
    live_label_save_requested = pyqtSignal(dict)
    instances: list["_FakePanel"] = []

    def __init__(self, parent=None, *, retrospective: bool = False):
        super().__init__(parent)
        self.retrospective = retrospective
        self.session_id = ""
        self.qc_context: dict = {}
        self.save_enabled = False
        self.frame = None
        self.metadata: dict = {}
        self.calibration = None
        self.snapshot = None
        self.evidence_kind = "Tw(2x1)"
        self.invalidations: list[tuple[str, bool]] = []
        self.set_records: list[object] = []
        type(self).instances.append(self)

    def set_session_id(self, session_id: str) -> None:
        self.session_id = session_id

    def update_qc_context(self, **context) -> None:
        self.qc_context = context

    def set_save_enabled(self, enabled: bool) -> None:
        self.save_enabled = enabled

    def update_camera_frame(self, frame: np.ndarray, metadata: dict) -> None:
        self.frame = np.array(frame, copy=True)
        self.metadata = dict(metadata)
        self.snapshot = types.SimpleNamespace(frame_token="frozen-snapshot")

    def get_current_capture_metadata(self) -> dict:
        return {
            "basis_bundle_id": self.metadata.get("basis_bundle_id", ""),
            "capture_geometry_id": self.metadata.get("capture_geometry_id", ""),
        }

    def get_basis_bundle(self):
        return types.SimpleNamespace(
            bundle_id=self.metadata.get("basis_bundle_id", ""),
        )

    def get_calibration_snapshot(self):
        return self.snapshot

    def get_handedness_evidence_kind(self) -> str:
        return self.evidence_kind

    def set_accepted_calibration(self, record) -> bool:
        self.calibration = record
        self.set_records.append(record)
        return True

    def get_calibration(self):
        return self.calibration

    def get_current_snapshot(self):
        return self.snapshot

    def invalidate_calibration(self, reason: str, *, emit: bool = False) -> None:
        self.calibration = None
        self.invalidations.append((reason, emit))


class _FakeLogger:
    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        self.active_calibrations: dict[str, object] = {}
        self.historical_calibrations: dict[str, object] = {}
        self.historical_lookups: list[str] = []
        self.recorded: list[object] = []
        self.invalidated: list[tuple[object, str]] = []
        self.update_calls: list[tuple[int, dict]] = []
        self.rows: dict[int, dict] = {}

    def read_event_labels(self) -> dict[int, dict]:
        return dict(self.rows)

    def get_calibration(self, calibration_id: str):
        return self.active_calibrations.get(calibration_id)

    def get_historical_calibration(self, calibration_id: str):
        self.historical_lookups.append(calibration_id)
        return self.historical_calibrations.get(calibration_id)

    def record_calibration(self, calibration) -> bool:
        self.recorded.append(calibration)
        self.active_calibrations[calibration.calibration_id] = calibration
        self.historical_calibrations[calibration.calibration_id] = calibration
        return True

    def record_calibration_invalidation(self, calibration, reason: str) -> bool:
        self.invalidated.append((calibration, reason))
        return True

    def update_event_label(self, event_idx: int, **kwargs) -> bool:
        self.update_calls.append((event_idx, dict(kwargs)))
        row = {field: "" for field in GrowthLogger.EVENT_LABEL_FIELDS}
        row["event_idx"] = str(event_idx)
        row["equalizer_argmax"] = kwargs["equalizer_payload"]["argmax"]
        for key in ("recon_1x1", "recon_tw", "recon_c6x2", "recon_rt13"):
            row[key] = f"{kwargs[key]:.4f}"
        row["recon_HTR"] = ""
        row["calibration_id"] = kwargs["calibration"].calibration_id
        row["frame_path"] = kwargs["frame_path"]
        self.rows[event_idx] = row
        return True


def _manifest_row(
    frame_name: str,
    *,
    calibration_id: str = "",
    gun_aligned: str = "True",
    basis_bundle_id: str = "bundle-v1",
) -> dict[str, str]:
    return {
        "frame_path": frame_name,
        "capture_backend": "windows_graphics_capture",
        "capture_geometry_id": "geometry-v1",
        "captured_at_utc": "2026-07-31T12:00:00+00:00",
        "captured_monotonic_ns": str(time.monotonic_ns() - 1_000_000),
        "capture_sequence": "42",
        "frame_age_ms": "1.25",
        "source_hwnd": "12345",
        "camera_width": "8",
        "camera_height": "6",
        "session_id": "growth_TEST_20260731_120000",
        "view_segment_id": "3",
        "visual_history_generation": "2",
        "gun_aligned": gun_aligned,
        "realignment_active": "False",
        "calibration_id": calibration_id,
        "basis_bundle_id": basis_bundle_id,
    }


class EventsEqualizerAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakePanel.instances.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.tmp.name) / "growth_TEST_20260731_120000"
        self.event_dir = self.session_dir / "frames" / "auto_event_001"
        self.event_dir.mkdir(parents=True)
        self.frame_path = self.event_dir / "buf_00.bmp"
        from PIL import Image

        Image.fromarray(np.full((6, 8, 3), 80, dtype=np.uint8)).save(self.frame_path)
        self.logger = _FakeLogger(self.session_dir)
        self.tab = EventsTab()
        self.tab.attach_session(self.logger)
        self.tab._cached_paths = [self.frame_path]
        self.tab._slider.setRange(0, 0)
        self.tab._slider.setValue(0)
        self.tab._currently_displayed_event_idx = 1

    def tearDown(self) -> None:
        if self.tab._equalizer_window is not None:
            self.tab._equalizer_window.close()
        self.tab.deleteLater()
        _app.processEvents()
        self.tmp.cleanup()

    def _write_manifest(self, row: dict, *, omit: str = "") -> None:
        fields = [name for name in _EQUALIZER_MANIFEST_FIELDS if name != omit]
        with open(self.event_dir / "capture_manifest.csv", "w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerow({key: row[key] for key in fields})
        (
            self.tab._capture_metadata_by_filename,
            self.tab._capture_manifest_error,
        ) = self.tab._load_capture_manifest(self.event_dir)

    def _open_with_fake_panel(self) -> _FakePanel:
        with patch("gui.live_equalizer_tab.LiveEqualizerTab", _FakePanel):
            self.tab._on_open_equalizer()
        self.assertTrue(_FakePanel.instances)
        return _FakePanel.instances[-1]

    def test_manifest_routes_exact_frame_metadata_to_shared_panel(self) -> None:
        row = _manifest_row(self.frame_path.name)
        self._write_manifest(row)
        self.assertEqual(
            set(GrowthLogger.AUTO_CAPTURE_MANIFEST_FIELDS),
            set(_EQUALIZER_MANIFEST_FIELDS),
        )

        panel = self._open_with_fake_panel()

        self.assertTrue(panel.retrospective)
        self.assertEqual(panel.session_id, row["session_id"])
        self.assertEqual(panel.metadata["capture_sequence"], 42)
        self.assertEqual(
            panel.metadata["captured_monotonic_ns"],
            int(row["captured_monotonic_ns"]),
        )
        self.assertEqual(panel.qc_context["view_segment_id"], 3)
        self.assertEqual(panel.qc_context["visual_history_generation"], 2)
        self.assertTrue(panel.qc_context["gun_aligned"])
        self.assertFalse(panel.qc_context["realignment_active"])
        self.assertTrue(panel.save_enabled)
        self.assertTrue(np.array_equal(panel.frame, np.full((6, 8, 3), 80)))
        self.assertEqual(
            self.tab._equalizer_window._equalizer_frame_path,
            self.frame_path,
        )
        self.assertIs(self.tab._equalizer_window.parent(), self.tab)
        self.assertTrue(self.tab._equalizer_window.isWindow())

    def test_closing_owned_window_clears_reference(self) -> None:
        self._write_manifest(_manifest_row(self.frame_path.name))
        self._open_with_fake_panel()
        window = self.tab._equalizer_window

        window.close()
        _app.processEvents()

        self.assertIsNone(self.tab._equalizer_window)

    def test_missing_or_legacy_manifest_fails_closed_with_dialog(self) -> None:
        self._write_manifest(
            _manifest_row(self.frame_path.name), omit="realignment_active",
        )
        self.assertEqual(self.tab._capture_metadata_by_filename, {})

        with patch.object(QMessageBox, "warning") as warning:
            self.tab._on_open_equalizer()

        warning.assert_called_once()
        self.assertIn("missing fields", warning.call_args.args[2])
        self.assertIsNone(self.tab._equalizer_window)
        self.assertEqual(_FakePanel.instances, [])

    def test_malformed_manifest_row_fails_closed(self) -> None:
        row = _manifest_row(self.frame_path.name)
        row["view_segment_id"] = "not-an-integer"
        self._write_manifest(row)

        self.assertEqual(self.tab._capture_metadata_by_filename, {})
        self.assertIn("row 2 is invalid", self.tab._capture_manifest_error)

    def test_blank_basis_bundle_fails_closed(self) -> None:
        row = _manifest_row(self.frame_path.name)
        row["basis_bundle_id"] = ""
        self._write_manifest(row)

        self.assertEqual(self.tab._capture_metadata_by_filename, {})
        self.assertIn("basis_bundle_id is blank", self.tab._capture_manifest_error)

    def test_blank_capture_geometry_fails_closed(self) -> None:
        row = _manifest_row(self.frame_path.name)
        row["capture_geometry_id"] = ""
        self._write_manifest(row)

        self.assertEqual(self.tab._capture_metadata_by_filename, {})
        self.assertIn("capture_geometry_id is blank", self.tab._capture_manifest_error)

    def test_manifest_timestamp_must_be_iso8601_utc(self) -> None:
        invalid_values = (
            "not-a-timestamp",
            "2026-07-31T12:00:00",
            "2026-07-31T13:00:00+01:00",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                row = _manifest_row(self.frame_path.name)
                row["captured_at_utc"] = value
                self._write_manifest(row)
                self.assertEqual(self.tab._capture_metadata_by_filename, {})
                self.assertIn("captured_at_utc", self.tab._capture_manifest_error)

    def test_event_buffer_path_must_stay_below_session_frames(self) -> None:
        for value in (str(self.event_dir.resolve()), "frames/../outside"):
            with self.subTest(buffer_dir=value):
                with patch.object(self.tab, "_load_capture_manifest") as loader:
                    self.tab._load_detail({"event_idx": "1", "buffer_dir": value})
                self.assertEqual(self.tab._cached_paths, [])
                self.assertIn("not accepted", self.tab._capture_manifest_error)
                loader.assert_not_called()

    def test_duplicate_manifest_frame_fails_closed(self) -> None:
        row = _manifest_row(self.frame_path.name)
        with open(
            self.event_dir / "capture_manifest.csv", "w", newline="",
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=_EQUALIZER_MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerow(row)
            writer.writerow(row)

        metadata, error = self.tab._load_capture_manifest(self.event_dir)

        self.assertEqual(metadata, {})
        self.assertIn("duplicate frame", error)

    def test_gun_not_aligned_fails_closed(self) -> None:
        self._write_manifest(
            _manifest_row(self.frame_path.name, gun_aligned="False"),
        )

        with patch.object(QMessageBox, "warning") as warning:
            self.tab._on_open_equalizer()

        warning.assert_called_once()
        self.assertIn("gun_aligned=true", warning.call_args.args[2])
        self.assertIsNone(self.tab._equalizer_window)

    def test_manifest_calibration_id_reuses_historical_record_after_stop(self) -> None:
        calibration = _Calibration("existing-cal")
        self._write_manifest(
            _manifest_row(
                self.frame_path.name,
                calibration_id=calibration.calibration_id,
            )
        )
        requests: list[dict] = []

        def _resolve(payload: dict) -> None:
            requests.append(payload)
            self.assertTrue(
                self.tab.complete_retrospective_calibration_resolution(
                    payload["request_token"], calibration,
                )
            )

        self.tab.retrospective_calibration_resolve_requested.connect(_resolve)

        panel = self._open_with_fake_panel()

        self.assertIs(panel.calibration, calibration)
        self.assertEqual(panel.set_records, [calibration])
        self.assertEqual(requests[0]["calibration_ids"], (calibration.calibration_id,))
        self.assertEqual(self.logger.historical_lookups, [])

    def test_events_label_calibration_id_has_priority_for_selected_frame(self) -> None:
        label_calibration = _Calibration("label-cal")
        manifest_calibration = _Calibration("manifest-cal")
        self.tab._labels_cache[1] = {
            "event_idx": "1",
            "calibration_id": label_calibration.calibration_id,
            "frame_path": str(self.frame_path),
        }
        self._write_manifest(
            _manifest_row(
                self.frame_path.name,
                calibration_id=manifest_calibration.calibration_id,
            )
        )
        requests: list[dict] = []

        def _resolve(payload: dict) -> None:
            requests.append(payload)
            self.tab.complete_retrospective_calibration_resolution(
                payload["request_token"], label_calibration,
            )

        self.tab.retrospective_calibration_resolve_requested.connect(_resolve)

        panel = self._open_with_fake_panel()

        self.assertIs(panel.calibration, label_calibration)
        self.assertEqual(
            requests[0]["calibration_ids"],
            ("label-cal", "manifest-cal"),
        )
        self.assertEqual(self.logger.historical_lookups, [])

    def test_accept_request_waits_for_app_completion(self) -> None:
        self._write_manifest(_manifest_row(self.frame_path.name))
        panel = self._open_with_fake_panel()
        pending = _PendingCalibration()
        requests: list[dict] = []
        self.tab.retrospective_calibration_accept_requested.connect(
            requests.append,
        )

        panel.calibration_accept_requested.emit(pending)

        self.assertEqual(self.logger.recorded, [])
        self.assertIsNone(panel.calibration)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertIs(request["pending"], pending)
        self.assertIs(request["snapshot"], panel.snapshot)
        self.assertEqual(request["evidence_kind"], "Tw(2x1)")
        self.assertTrue(request["evidence_confirmed"])
        self.assertIs(request["panel"], panel)

        # Simulate GrowthApp accepting + journaling, then handing the typed
        # accepted record back to Events for local activation.
        accepted = pending.accepted(accepted_by="events-retrospective")
        completed = self.tab.complete_retrospective_calibration_acceptance(
            request["request_token"], accepted,
        )

        self.assertTrue(completed)
        self.assertIs(panel.calibration, accepted)
        self.assertIsNone(self.tab._pending_retrospective_accept_context)

    def test_failed_accept_request_clears_panel_fail_closed(self) -> None:
        self._write_manifest(_manifest_row(self.frame_path.name))
        panel = self._open_with_fake_panel()
        requests: list[dict] = []
        self.tab.retrospective_calibration_accept_requested.connect(requests.append)
        panel.calibration_accept_requested.emit(_PendingCalibration())

        with patch.object(QMessageBox, "critical"):
            failed = self.tab.fail_retrospective_calibration_acceptance(
                requests[0]["request_token"], "journal unavailable",
            )

        self.assertTrue(failed)
        self.assertEqual(panel.invalidations, [("journal unavailable", False)])
        self.assertIsNone(panel.calibration)

    def test_invalidation_is_requested_then_completed_by_app(self) -> None:
        calibration = _Calibration("existing-cal")
        self._write_manifest(
            _manifest_row(
                self.frame_path.name,
                calibration_id=calibration.calibration_id,
            )
        )
        self.tab.retrospective_calibration_resolve_requested.connect(
            lambda payload: self.tab.complete_retrospective_calibration_resolution(
                payload["request_token"], calibration,
            )
        )
        panel = self._open_with_fake_panel()
        requests: list[dict] = []
        self.tab.retrospective_calibration_invalidation_requested.connect(
            requests.append,
        )

        panel.calibration = None  # Live panel clears itself before emitting.
        panel.calibration_invalidation_requested.emit("grower recalibrated")

        self.assertEqual(self.logger.invalidated, [])
        self.assertEqual(len(requests), 1)
        self.assertIs(requests[0]["calibration"], calibration)
        self.assertEqual(requests[0]["reason"], "grower recalibrated")
        self.assertTrue(
            self.tab.complete_retrospective_calibration_invalidation(
                requests[0]["request_token"],
            )
        )
        self.assertIsNone(self.tab._pending_retrospective_invalidation)

    def test_save_writes_calibration_snapshot_frame_and_blank_htr(self) -> None:
        self._write_manifest(_manifest_row(self.frame_path.name))
        panel = self._open_with_fake_panel()
        calibration = _Calibration("accepted-cal")
        panel.calibration = calibration
        self.tab._equalizer_window._equalizer_active_calibration = calibration

        weights = {
            "1x1": 0.1, "Tw(2x1)": 0.2,
            "c(6x2)": 0.6, "RT13": 0.1, "HTR": None,
        }
        panel.live_label_save_requested.emit(build_equalizer_payload(
            raw_weights=None,
            final_weights=weights,
            fit_mode="manual",
            normalization_applied=False,
            residual_rms=3.0,
            valid_coverage=0.9,
        ))

        self.assertEqual(len(self.logger.update_calls), 1)
        event_idx, kwargs = self.logger.update_calls[0]
        self.assertEqual(event_idx, 1)
        self.assertIs(kwargs["calibration"], calibration)
        self.assertIs(kwargs["snapshot"], panel.snapshot)
        self.assertEqual(kwargs["frame_path"], str(self.frame_path))
        self.assertIsNone(kwargs["recon_HTR"])
        self.assertEqual(kwargs["recon_c6x2"], 0.6)
        self.assertNotIn("primary_reconstruction", kwargs)
        self.assertEqual(kwargs["equalizer_payload"]["argmax"], "c(6x2)")
        self.assertEqual(self.tab._labels_cache[1]["primary_reconstruction"], "")
        self.assertEqual(self.tab._labels_cache[1]["recon_HTR"], "")

    def test_real_panel_record_reuse_and_save_round_trip(self) -> None:
        from gui.equalizer_alignment import CalibrationRecord
        from gui.live_equalizer_tab import LiveEqualizerTab
        from scripts.equalizer_ui import load_basis_bundle

        bundle = load_basis_bundle()
        calibration_id = "real-events-calibration"
        self._write_manifest(_manifest_row(
            self.frame_path.name,
            calibration_id=calibration_id,
            basis_bundle_id=bundle.bundle_id,
        ))
        resolved: list[CalibrationRecord] = []

        def _resolve(payload: dict) -> None:
            snapshot = payload["snapshot"]
            points = np.array(
                [[28.0, 45.0], [64.0, 38.0], [100.0, 45.0]],
                dtype=np.float64,
            )
            pending = CalibrationRecord(
                calibration_id=calibration_id,
                basis_bundle_id=bundle.bundle_id,
                candidate_id="real-events-candidate",
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
            )
            accepted = pending.accepted(
                "TEST_GROWER",
                snapshot.orientation_evidence_sha256(),
                orientation_evidence_kind="streak/tail",
            )
            resolved.append(accepted)
            self.assertTrue(
                self.tab.complete_retrospective_calibration_resolution(
                    payload["request_token"], accepted,
                )
            )

        self.tab.retrospective_calibration_resolve_requested.connect(_resolve)
        self.tab._on_open_equalizer()

        window = self.tab._equalizer_window
        self.assertIsNotNone(window)
        panel = window._equalizer_panel
        self.assertIsInstance(panel, LiveEqualizerTab)
        self.assertIs(panel.get_calibration(), resolved[0])

        panel.live_label_save_requested.emit(build_equalizer_payload(
            raw_weights=None,
            final_weights={
                "1x1": 0.7, "Tw(2x1)": 0.1,
                "c(6x2)": 0.1, "RT13": 0.1, "HTR": None,
            },
            fit_mode="manual",
            normalization_applied=False,
            residual_rms=2.0,
            valid_coverage=1.0,
        ))

        self.assertEqual(len(self.logger.update_calls), 1)
        _, kwargs = self.logger.update_calls[0]
        self.assertIs(kwargs["calibration"], resolved[0])
        self.assertIs(kwargs["snapshot"], panel.get_current_snapshot())


if __name__ == "__main__":
    unittest.main(verbosity=2)
