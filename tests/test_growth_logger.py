"""Unit tests for GrowthLogger — focused on save_frame's Jul 8 2026 contract
change (LOG ENTRY saves unconditionally, returns quality metadata) plus the
Jul 10 2026 grower-marked events schema (record_manual_event).

Small test surface; no Qt required (GrowthLogger is a plain Python class).
Run: ``python -m pytest -q tests/test_growth_logger.py``.
"""
from __future__ import annotations

import csv
from dataclasses import replace
import hashlib
import json
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

from gui.growth_logger import (  # noqa: E402
    GrowthLogger,
    RHEED_VIEW_EVENT_ALIGNMENT_CONFIRMED,
    RHEED_VIEW_EVENT_CURRENT_ADJUSTED,
    RHEED_VIEW_EVENT_ENERGY_ADJUSTED,
    RHEED_VIEW_EVENT_HISTORY_RESET,
    RHEED_VIEW_EVENT_HISTORY_READY,
    RHEED_VIEW_EVENT_QC_PASS,
    RHEED_VIEW_EVENT_QC_REJECT,
    RHEED_VIEW_EVENT_REALIGN_END,
    RHEED_VIEW_EVENT_REALIGN_START,
    RHEED_VIEW_EVENT_SAMPLE_DIRECTION_ADJUSTED,
    RHEED_VIEW_EVENT_SESSION_START,
    RHEED_VIEW_EVENT_TYPES,
)
from gui.equalizer_alignment import (  # noqa: E402
    ACTIVE_SIMULATOR_LABELS,
    BasisAsset,
    BasisBundle,
    CalibrationRecord,
    RheedFrameSnapshot,
)


_TEST_BASIS_BUNDLE = BasisBundle(tuple([
    *(
        BasisAsset(
            label=label,
            image=np.zeros((96, 128), dtype=np.float32),
            active=True,
            source_kind="simulator",
            source_path=f"data/simulated_basis/{label}.png",
        )
        for label in ACTIVE_SIMULATOR_LABELS
    ),
    BasisAsset(
        label="HTR",
        image=None,
        active=False,
        source_kind="unavailable",
        unavailable_reason="canonical simulator basis pending",
    ),
]))


def _good_frame() -> np.ndarray:
    """A frame with mean intensity comfortably above the quality gate's
    3.0 threshold — passes cleanly."""
    return (np.random.default_rng(42).integers(80, 180, (200, 300, 3))
            .astype(np.uint8))


def _dark_frame() -> np.ndarray:
    """A frame with mean intensity ~1 — well below the 3.0 threshold,
    should be flagged but still saved per Jul 8 decision."""
    return (np.random.default_rng(0).integers(0, 3, (200, 300, 3))
            .astype(np.uint8))


def _classifier_snapshot(event_idx: int = 1, score: float = 2.5) -> dict:
    return {
        "timestamp_iso": "2026-07-31T12:00:01.000000",
        "event_idx": event_idx,
        "event_score": score,
        "elapsed_s": 1.0,
        "capture_sequence": 102,
        "predicted_class": "1x1",
        "quality": 0.8,
    }


def _equalizer_context(
    session_id: str,
    *,
    view_segment_id: int = 4,
    visual_history_generation: int = 2,
    accepted: bool = True,
) -> tuple[CalibrationRecord, RheedFrameSnapshot]:
    """Build a typed, internally compatible calibration/frame pair."""
    rgb = _good_frame()
    now_ns = time.monotonic_ns()
    snapshot = RheedFrameSnapshot.freeze(
        rgb,
        np.mean(rgb, axis=2).astype(np.float32)[::2, ::2][:96, :128],
        captured_at_utc="2026-07-31T12:00:01.000Z",
        received_monotonic_ns=now_ns,
        capture_sequence=102,
        source_hwnd=9001,
        capture_backend="wgc",
        capture_geometry_id="wgc:9001:300x200:roi-full:v1",
        session_id=session_id,
        view_segment_id=view_segment_id,
        visual_history_generation=visual_history_generation,
        gun_aligned=True,
        realignment_active=False,
    )
    calibration = CalibrationRecord(
        calibration_id="cal-test-001",
        basis_bundle_id=_TEST_BASIS_BUNDLE.bundle_id,
        candidate_id="candidate-test",
        matrix=np.array([[1.0, 0.0, 2.0], [0.0, 1.0, -3.0]]),
        parity="normal",
        endpoint_order="forward",
        basis_points=np.array([[20.0, 50.0], [64.0, 35.0], [108.0, 50.0]]),
        live_points=np.array([[22.0, 47.0], [66.0, 32.0], [110.0, 47.0]]),
        residuals_px=np.zeros(3),
        rotation_deg=0.0,
        scale=1.0,
        rms_residual_px=0.0,
        max_residual_px=0.0,
        valid_coverage=0.95,
        correlation=0.9,
        active_classes=ACTIVE_SIMULATOR_LABELS,
        source_hwnd=snapshot.source_hwnd,
        camera_width=snapshot.camera_width,
        camera_height=snapshot.camera_height,
        capture_backend=snapshot.capture_backend,
        capture_geometry_id=snapshot.capture_geometry_id,
        captured_at_utc=snapshot.captured_at_utc,
        capture_sequence=snapshot.capture_sequence,
        received_monotonic_ns=snapshot.received_monotonic_ns,
        session_id=session_id,
        view_segment_id=view_segment_id,
        visual_history_generation=visual_history_generation,
        gun_aligned=True,
        realignment_active=False,
        grower_accepted=accepted,
        accepted_by="test grower" if accepted else "",
        orientation_evidence_kind=("streak/tail" if accepted else ""),
        orientation_evidence_sha256=(
            snapshot.orientation_evidence_sha256() if accepted else ""
        ),
        orientation_evidence_path=(
            "frames/equalizer_calibration_cal-test-001_orientation.png"
            if accepted else ""
        ),
    )
    return calibration, snapshot


class SaveFrameTests(unittest.TestCase):
    """Tests the Jul 8 2026 contract change: LOG ENTRY saves the frame
    unconditionally when available, quality gate result is captured as
    metadata rather than a save-blocker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("TEST_SAVE_FRAME")

    def tearDown(self):
        self.logger.end_session()
        self.tmp.cleanup()

    def test_good_frame_saves_and_reports_quality_pass_true(self):
        path, quality_pass = self.logger.save_frame(_good_frame(), "160955")
        self.assertNotEqual(path, "")
        self.assertTrue(Path(path).exists())
        self.assertEqual(quality_pass, True)
        # Jul 9 switch: entry frames are .bmp, not .png.
        self.assertTrue(
            path.endswith(".bmp"),
            f"expected .bmp extension, got {path}",
        )

    def test_dark_frame_still_saves_and_reports_quality_pass_false(self):
        # Critical contract test: grower explicit intent > automated
        # quality gate. Frame gets saved despite failing the gate.
        path, quality_pass = self.logger.save_frame(_dark_frame(), "160955")
        self.assertNotEqual(path, "", "dark frame should still be saved")
        self.assertTrue(Path(path).exists())
        self.assertEqual(quality_pass, False)
        self.assertTrue(path.endswith(".bmp"))

    def test_saved_bmp_is_valid_bmp_format(self):
        # PIL should be able to read the file back as a BMP.
        path, _ = self.logger.save_frame(_good_frame(), "160955")
        from PIL import Image
        with Image.open(path) as img:
            self.assertEqual(
                img.format, "BMP",
                f"expected BMP format, PIL reports {img.format}",
            )

    def test_no_session_returns_empty_path_and_none_quality(self):
        logger = GrowthLogger(base_dir=self.tmp.name)
        # No start_session — session_dir is None.
        path, quality_pass = logger.save_frame(_good_frame(), "160955")
        self.assertEqual(path, "")
        self.assertIsNone(quality_pass)

    def test_frame_counter_advances_regardless_of_quality(self):
        # Every LOG ENTRY should get a distinct frame path even when
        # the previous frame was flagged. Confirms that the counter
        # advances on save success, not on quality pass.
        p1, _ = self.logger.save_frame(_dark_frame(), "160955")
        p2, _ = self.logger.save_frame(_good_frame(), "160956")
        p3, _ = self.logger.save_frame(_dark_frame(), "160957")
        self.assertNotEqual(p1, p2)
        self.assertNotEqual(p2, p3)
        # Filenames should be entry_001, entry_002, entry_003 in order.
        self.assertIn("entry_001", p1)
        self.assertIn("entry_002", p2)
        self.assertIn("entry_003", p3)


class CommitFieldsTests(unittest.TestCase):
    """Schema-level tests for the two Jul 8 additions."""

    def test_classifier_status_and_frame_quality_pass_are_present(self):
        # These two columns landed Jul 8 — verify they're in the schema
        # so downstream code can rely on them.
        self.assertIn("classifier_status", GrowthLogger.COMMIT_FIELDS)
        self.assertIn(
            "classifier_prediction_actionable",
            GrowthLogger.COMMIT_FIELDS,
        )
        self.assertIn(
            "classifier_visual_history_generation",
            GrowthLogger.COMMIT_FIELDS,
        )
        self.assertIn("frame_quality_pass", GrowthLogger.COMMIT_FIELDS)
        self.assertIn("psu_source", GrowthLogger.COMMIT_FIELDS)

    def test_field_order_stable(self):
        # frame_quality_pass should follow frame_path (both are per-frame
        # metadata). Grouping matters for CSV readability.
        fields = GrowthLogger.COMMIT_FIELDS
        i_path = fields.index("frame_path")
        i_qual = fields.index("frame_quality_pass")
        self.assertGreater(i_qual, i_path)


class CaptureProvenanceTests(unittest.TestCase):
    """RHEED image rows preserve the capture timestamp and source atomically."""

    def test_capture_geometry_is_in_every_capture_bearing_schema(self):
        schemas = (
            GrowthLogger.COMMIT_FIELDS,
            GrowthLogger.AUTO_CAPTURE_FIELDS,
            GrowthLogger.HEARTBEAT_FIELDS,
            GrowthLogger.MANUAL_EVENT_FIELDS,
            GrowthLogger.RHEED_VIEW_EVENT_FIELDS,
            GrowthLogger.LIVE_LABEL_FIELDS,
            GrowthLogger.EVENT_LABEL_FIELDS,
            GrowthLogger.AUTO_CAPTURE_MANIFEST_FIELDS,
        )
        for schema in schemas:
            self.assertIn("capture_geometry_id", schema)

    def test_heartbeat_writes_capture_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("CAPTURE_META")
            metadata = {
                "capture_backend": "wgc",
                "captured_at_utc": "2026-07-27T12:00:00.123Z",
                "capture_sequence": 42,
                "frame_age_ms": 12.3456,
                "source_hwnd": 9001,
                "capture_geometry_id": "wgc:9001:640x480:roi-full:v1",
            }
            logger.log_heartbeat(
                elapsed_s=1.0,
                frame_path="frames/heartbeat_001.bmp",
                capture_metadata=metadata,
            )
            csv_path = logger.session_dir / "heartbeat_log.csv"
            logger.end_session()
            with open(csv_path, newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["capture_backend"], "wgc")
            self.assertEqual(
                row["captured_at_utc"], "2026-07-27T12:00:00.123Z",
            )
            self.assertEqual(row["capture_sequence"], "42")
            self.assertEqual(row["frame_age_ms"], "12.346")
            self.assertEqual(row["source_hwnd"], "9001")
            self.assertEqual(
                row["capture_geometry_id"],
                "wgc:9001:640x480:roi-full:v1",
            )
    def test_auto_capture_buffer_writes_per_frame_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("BUFFER_META")
            metadata = [
                {
                    "capture_backend": "wgc",
                    "captured_at_utc": f"2026-07-27T12:00:0{i}.000Z",
                    "capture_sequence": 100 + i,
                    "captured_monotonic_ns": 9_000_000_000 + i,
                    "frame_age_ms": 10.0 + i,
                    "source_hwnd": 9001,
                    "capture_geometry_id": "wgc:9001:640x480:roi-full:v1",
                    "camera_width": 640,
                    "camera_height": 480,
                    "session_id": "growth_BUFFER_META_session",
                    "view_segment_id": 4,
                    "visual_history_generation": 2,
                    "gun_aligned": True,
                    "realignment_active": False,
                    "calibration_id": "cal-test-001",
                    "basis_bundle_id": "basis-test-sha256",
                }
                for i in range(2)
            ]
            result = logger.record_auto_capture_event(
                event_idx=1,
                score=2.5,
                elapsed_s=1.0,
                frames=[_good_frame(), _good_frame()],
                frame_capture_metadata=metadata,
                event_capture_metadata=metadata[-1],
                classifier_snapshot=_classifier_snapshot(),
            )
            event_dir = logger.session_dir / result.buffer_dir
            csv_path = logger.session_dir / "auto_capture_events.csv"
            logger.end_session()

            self.assertTrue(result.committed)
            self.assertTrue(result.writer_ready)
            self.assertEqual(result.buffer_count, 2)
            self.assertEqual(
                list((logger.session_dir / "frames").glob("*.pending")),
                [],
            )
            with open(event_dir / "capture_manifest.csv", newline="") as stream:
                rows = list(csv.DictReader(stream))
            with open(csv_path, newline="") as stream:
                event_row = next(csv.DictReader(stream))
            self.assertEqual(event_row["event_idx"], "1")
            self.assertEqual(event_row["buffer_count"], "2")
            self.assertEqual(event_row["buffer_dir"], result.buffer_dir)
            self.assertEqual(
                json.loads((event_dir / "classifier_state.json").read_text()),
                _classifier_snapshot(),
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                [row["capture_sequence"] for row in rows],
                ["100", "101"],
            )
            for row in rows:
                self.assertTrue((event_dir / row["frame_path"]).exists())
                self.assertEqual(row["capture_backend"], "wgc")
                self.assertTrue(int(row["captured_monotonic_ns"]) >= 9_000_000_000)
                self.assertEqual(row["source_hwnd"], "9001")
                self.assertEqual(
                    row["capture_geometry_id"],
                    "wgc:9001:640x480:roi-full:v1",
                )
                self.assertEqual(row["camera_width"], "640")
                self.assertEqual(row["camera_height"], "480")
                self.assertEqual(row["view_segment_id"], "4")
                self.assertEqual(row["visual_history_generation"], "2")
                self.assertEqual(row["gun_aligned"], "True")
                self.assertEqual(row["realignment_active"], "False")
                self.assertEqual(row["calibration_id"], "cal-test-001")
                self.assertEqual(row["basis_bundle_id"], "basis-test-sha256")

    def test_auto_capture_encoder_failure_writes_no_manifest_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("BUFFER_ENCODER_FAIL")
            with patch.object(logger, "_save_rgb_bmp", return_value=False):
                result = logger.record_auto_capture_event(
                    event_idx=1,
                    score=2.5,
                    elapsed_s=1.0,
                    frames=[_good_frame()],
                    frame_capture_metadata=[{"capture_sequence": 1}],
                )
            frames_dir = logger.session_dir / "frames"
            csv_path = logger.session_dir / "auto_capture_events.csv"
            logger.end_session()

            self.assertFalse(result.committed)
            self.assertEqual(result.buffer_count, 0)
            self.assertEqual(result.buffer_dir, "")
            self.assertEqual(list(frames_dir.rglob("*.bmp")), [])
            self.assertFalse(
                (logger.session_dir / GrowthLogger.AUTO_CAPTURE_TRANSACTION_FILE).exists()
            )
            with open(csv_path, newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])

    def test_auto_capture_manifest_failure_removes_new_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("BUFFER_MANIFEST_FAIL")
            original_write = logger._atomic_write_csv

            def fail_manifest(path, *args, **kwargs):
                if Path(path).name == "capture_manifest.csv":
                    return False
                return original_write(path, *args, **kwargs)

            with patch.object(logger, "_atomic_write_csv", side_effect=fail_manifest):
                result = logger.record_auto_capture_event(
                    event_idx=1,
                    score=2.5,
                    elapsed_s=1.0,
                    frames=[_good_frame()],
                )
            frames_dir = logger.session_dir / "frames"
            logger.end_session()

            self.assertEqual(result.buffer_count, 0)
            self.assertEqual(result.buffer_dir, "")
            self.assertFalse(result.committed)
            self.assertEqual(list(frames_dir.rglob("*.bmp")), [])
            self.assertEqual(list(frames_dir.glob("*.pending")), [])

    def test_auto_capture_state_replace_failure_preserves_csv_and_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("AUTO_STATE_ATOMIC")
            logger.log_auto_capture_event(
                event_idx=1,
                score=2.5,
                elapsed_s=1.0,
            )
            csv_path = logger.session_dir / "auto_capture_events.csv"
            original = csv_path.read_bytes()
            with patch.object(logger, "_atomic_write_csv", return_value=False):
                self.assertFalse(
                    logger.update_auto_capture_state(1, "kept_explicit")
                )
            self.assertEqual(csv_path.read_bytes(), original)
            logger.log_auto_capture_event(
                event_idx=2,
                score=3.0,
                elapsed_s=2.0,
            )
            logger.end_session()
            with open(csv_path, newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row["event_idx"] for row in rows], ["1", "2"])


class AutoCaptureTransactionTests(unittest.TestCase):
    def test_committed_event_reports_reopen_failure_and_decision_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("AUTO_REOPEN")
            csv_path = logger.session_dir / "auto_capture_events.csv"

            with patch.object(
                logger, "_open_auto_capture_stream_for_append", return_value=False,
            ):
                result = logger.record_auto_capture_event(
                    event_idx=1,
                    score=2.5,
                    elapsed_s=1.0,
                    frames=[_good_frame()],
                )

            self.assertTrue(result.committed)
            self.assertFalse(result.writer_ready)
            self.assertIsNone(logger._auto_capture_file)
            self.assertIsNone(logger._auto_capture_writer)
            self.assertTrue(
                logger.update_auto_capture_state(1, "discarded")
            )
            self.assertTrue(logger._auto_capture_stream_ready())
            logger.end_session()
            with open(csv_path, newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["event_state"], "discarded")
            self.assertTrue(row["state_changed_at"])

    def test_zero_frame_event_commits_auto_skipped_without_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("AUTO_ZERO")
            result = logger.record_auto_capture_event(
                event_idx=1,
                score=2.5,
                elapsed_s=1.0,
                frames=[_dark_frame()],
            )
            csv_path = logger.session_dir / "auto_capture_events.csv"
            final_dir = logger.session_dir / "frames" / "auto_event_001"
            logger.end_session()

            self.assertEqual(result.buffer_count, 0)
            self.assertEqual(result.buffer_dir, "")
            self.assertTrue(result.committed)
            self.assertFalse(final_dir.exists())
            with open(csv_path, newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["buffer_count"], "0")
            self.assertEqual(row["buffer_dir"], "")
            self.assertEqual(row["event_state"], "auto_skipped")
            self.assertNotEqual(row["state_changed_at"], "")

    def test_power_loss_before_csv_commit_rolls_back_event_directory_on_restart(self):
        class SimulatedPowerLoss(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("AUTO_CRASH_BEFORE_CSV")
            session_dir = logger.session_dir
            marker = logger._auto_capture_transaction_path(session_dir)
            final_dir = session_dir / "frames" / "auto_event_001"
            csv_path = session_dir / "auto_capture_events.csv"
            original_write = logger._atomic_write_csv

            def crash_on_event_csv(path, *args, **kwargs):
                if Path(path).name == "auto_capture_events.csv":
                    raise SimulatedPowerLoss("power lost before event CSV commit")
                return original_write(path, *args, **kwargs)

            with patch.object(logger, "_atomic_write_csv", side_effect=crash_on_event_csv):
                with self.assertRaises(SimulatedPowerLoss):
                    logger.record_auto_capture_event(
                        event_idx=1,
                        score=2.5,
                        elapsed_s=1.0,
                        frames=[_good_frame()],
                        classifier_snapshot=_classifier_snapshot(),
                    )
            self.assertTrue(marker.exists())
            self.assertTrue(final_dir.is_dir())
            with open(csv_path, newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])
            logger.end_session()

            restarted = GrowthLogger(base_dir=tmp)
            self.assertFalse(marker.exists())
            self.assertFalse(final_dir.exists())
            self.assertTrue(restarted.set_base_dir(tmp))

    def test_power_loss_after_csv_commit_finalizes_event_on_restart(self):
        class SimulatedPowerLoss(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("AUTO_CRASH_AFTER_CSV")
            session_dir = logger.session_dir
            marker = logger._auto_capture_transaction_path(session_dir)
            final_dir = session_dir / "frames" / "auto_event_001"
            csv_path = session_dir / "auto_capture_events.csv"
            original_unlink = Path.unlink

            def crash_before_wal_clear(path, *args, **kwargs):
                if Path(path) == marker:
                    raise SimulatedPowerLoss("power lost before auto-capture WAL clear")
                return original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=crash_before_wal_clear):
                with self.assertRaises(SimulatedPowerLoss):
                    logger.record_auto_capture_event(
                        event_idx=1,
                        score=2.5,
                        elapsed_s=1.0,
                        frames=[_good_frame()],
                        classifier_snapshot=_classifier_snapshot(),
                    )
            self.assertTrue(marker.exists())
            self.assertTrue(final_dir.is_dir())
            with open(csv_path, newline="") as stream:
                rows_before = list(csv.DictReader(stream))
            self.assertEqual(len(rows_before), 1)
            logger.end_session()

            restarted = GrowthLogger(base_dir=tmp)
            self.assertFalse(marker.exists())
            self.assertTrue(final_dir.is_dir())
            self.assertEqual(
                json.loads((final_dir / "classifier_state.json").read_text()),
                _classifier_snapshot(),
            )
            with open(csv_path, newline="") as stream:
                rows_after = list(csv.DictReader(stream))
            self.assertEqual(rows_after, rows_before)
            self.assertTrue(restarted.set_base_dir(tmp))

    def test_power_loss_before_directory_promote_cleans_pending_on_restart(self):
        class SimulatedPowerLoss(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("AUTO_CRASH_PENDING")
            session_dir = logger.session_dir
            marker = logger._auto_capture_transaction_path(session_dir)
            final_dir = session_dir / "frames" / "auto_event_001"
            csv_path = session_dir / "auto_capture_events.csv"
            original_replace = __import__("os").replace

            def crash_on_directory_promote(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    source_path.name.startswith(".auto_event_001.")
                    and source_path.name.endswith(".pending")
                    and destination_path == final_dir
                ):
                    raise SimulatedPowerLoss("power lost before event directory promote")
                return original_replace(source, destination)

            with patch("gui.growth_logger.os.replace", side_effect=crash_on_directory_promote):
                with self.assertRaises(SimulatedPowerLoss):
                    logger.record_auto_capture_event(
                        event_idx=1,
                        score=2.5,
                        elapsed_s=1.0,
                        frames=[_good_frame()],
                    )
            payload = json.loads(marker.read_text(encoding="utf-8"))
            staging_dir = session_dir / payload["staging_dir"]
            self.assertTrue(staging_dir.is_dir())
            self.assertFalse(final_dir.exists())
            with open(csv_path, newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])
            logger.end_session()

            restarted = GrowthLogger(base_dir=tmp)
            self.assertFalse(marker.exists())
            self.assertFalse(staging_dir.exists())
            self.assertFalse(final_dir.exists())
            self.assertTrue(restarted.set_base_dir(tmp))

    def test_wal_is_durable_and_exact_before_first_image_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("AUTO_WAL_ORDER")
            marker = logger._auto_capture_transaction_path(logger.session_dir)
            original_save = logger._save_rgb_bmp
            observed = []

            def inspect_wal_before_save(frame, path):
                self.assertTrue(marker.is_file())
                payload = json.loads(marker.read_text(encoding="utf-8"))
                self.assertEqual(payload["event_idx"], 1)
                self.assertEqual(payload["row"]["event_idx"], "1")
                self.assertEqual(payload["row"]["buffer_count"], "1")
                self.assertEqual(
                    payload["row"]["buffer_dir"], "frames/auto_event_001",
                )
                self.assertEqual(len(payload["manifest_rows"]), 1)
                self.assertEqual(
                    payload["manifest_rows"][0]["frame_path"], Path(path).name,
                )
                self.assertEqual(
                    set(payload["row"]), set(GrowthLogger.AUTO_CAPTURE_FIELDS),
                )
                self.assertEqual(
                    set(payload["manifest_rows"][0]),
                    set(GrowthLogger.AUTO_CAPTURE_MANIFEST_FIELDS),
                )
                snapshot_text = payload["classifier_snapshot_json"]
                self.assertEqual(json.loads(snapshot_text), _classifier_snapshot())
                self.assertEqual(
                    payload["classifier_snapshot_sha256"],
                    hashlib.sha256(snapshot_text.encode("utf-8")).hexdigest(),
                )
                observed.append(True)
                return original_save(frame, path)

            with patch.object(logger, "_save_rgb_bmp", side_effect=inspect_wal_before_save):
                result = logger.record_auto_capture_event(
                    event_idx=1,
                    score=2.5,
                    elapsed_s=1.0,
                    frames=[_good_frame()],
                    classifier_snapshot=_classifier_snapshot(),
                )
            logger.end_session()

            self.assertEqual(result.buffer_count, 1)
            self.assertTrue(result.committed)
            self.assertEqual(observed, [True])

    def test_malformed_auto_capture_wal_is_retained_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("AUTO_BAD_WAL")
            marker = logger._auto_capture_transaction_path(logger.session_dir)
            marker.write_text('{"schema_version":1', encoding="utf-8")
            logger.end_session()

            restarted = GrowthLogger(base_dir=tmp)
            self.assertTrue(marker.exists())
            self.assertFalse(restarted.set_base_dir(tmp))

    def test_conflicting_same_index_wal_is_retained_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("AUTO_CONFLICT_WAL")
            result = logger.record_auto_capture_event(
                event_idx=1,
                score=2.5,
                elapsed_s=1.0,
                frames=[_dark_frame()],
            )
            self.assertEqual(result.buffer_count, 0)
            self.assertEqual(result.buffer_dir, "")
            self.assertTrue(result.committed)
            marker = logger._auto_capture_transaction_path(logger.session_dir)
            conflicting_row = logger._build_auto_capture_row(
                event_idx=1,
                score=9.0,
                elapsed_s=1.0,
                buffer_count=0,
                event_state="auto_skipped",
            )
            marker.write_text(json.dumps({
                "schema_version": logger.AUTO_CAPTURE_TRANSACTION_SCHEMA_VERSION,
                "event_idx": 1,
                "staging_dir": "",
                "final_dir": "",
                "row": conflicting_row,
                "manifest_rows": [],
            }), encoding="utf-8")
            logger.end_session()

            restarted = GrowthLogger(base_dir=tmp)
            self.assertTrue(marker.exists())
            self.assertFalse(restarted.set_base_dir(tmp))

    def test_zero_frame_event_refuses_preexisting_same_index_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("AUTO_ZERO_CONFLICT")
            final_dir = logger.session_dir / "frames" / "auto_event_001"
            final_dir.mkdir()
            result = logger.record_auto_capture_event(
                event_idx=1,
                score=2.5,
                elapsed_s=1.0,
                frames=[_dark_frame()],
            )
            marker = logger._auto_capture_transaction_path(logger.session_dir)
            csv_path = logger.session_dir / "auto_capture_events.csv"
            logger.end_session()

            self.assertEqual(result.buffer_count, 0)
            self.assertEqual(result.buffer_dir, "")
            self.assertFalse(result.committed)
            self.assertTrue(final_dir.is_dir())
            self.assertFalse(marker.exists())
            with open(csv_path, newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])


class EqualizerCalibrationJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("CAL_JOURNAL")
        self.path = self.logger.session_dir / "equalizer_calibrations.jsonl"
        self.calibration, self.snapshot = _equalizer_context(
            self.logger.session_dir.name,
        )

    def tearDown(self):
        self.logger.end_session()
        self.tmp.cleanup()

    def test_accept_and_invalidate_are_append_only_and_replay_fail_closed(self):
        self.assertTrue(self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        ))
        self.assertIs(
            self.logger.get_calibration(self.calibration.calibration_id).__class__,
            CalibrationRecord,
        )
        self.assertTrue(self.logger.record_calibration_invalidation(
            self.calibration, "camera disconnected",
        ))
        self.assertIsNone(
            self.logger.get_calibration(self.calibration.calibration_id),
        )
        self.assertEqual(
            self.logger.get_historical_calibration(
                self.calibration.calibration_id,
            ).calibration_id,
            self.calibration.calibration_id,
        )
        with open(self.path, encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream]
        self.assertEqual([event["event"] for event in events], [
            "accepted", "invalidated",
        ])
        accepted = events[0]["calibration"]
        self.assertEqual(accepted["matrix"], [[1.0, 0.0, 2.0], [0.0, 1.0, -3.0]])
        self.assertEqual(accepted["active_classes"], list(ACTIVE_SIMULATOR_LABELS))
        self.assertEqual(
            events[1]["calibration"]["invalidated_reason"],
            "camera disconnected",
        )
        evidence_path = (
            self.logger.session_dir / self.calibration.orientation_evidence_path
        )
        self.assertTrue(evidence_path.exists())
        self.assertEqual(
            hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            self.calibration.orientation_evidence_sha256,
        )
        self.assertEqual(
            events[0]["orientation_evidence_path"],
            self.calibration.orientation_evidence_path,
        )
        manifest = events[0]["basis_bundle_manifest"]
        self.assertEqual(manifest["bundle_id"], self.calibration.basis_bundle_id)
        self.assertEqual(manifest["coordinate_frame"], "simulator-128x96-v2")
        self.assertEqual(
            tuple(asset["label"] for asset in manifest["assets"]),
            (*ACTIVE_SIMULATOR_LABELS, "HTR"),
        )

    def test_basis_bundle_manifest_is_written_once_per_session(self):
        self.assertTrue(self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        ))
        second = replace(
            self.calibration,
            calibration_id="cal-test-002",
            orientation_evidence_path=(
                "frames/equalizer_calibration_cal-test-002_orientation.png"
            ),
        )
        self.assertTrue(self.logger.record_calibration(
            second,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        ))
        events = [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(events), 2)
        self.assertIn("basis_bundle_manifest", events[0])
        self.assertNotIn("basis_bundle_manifest", events[1])

    def test_invalid_capture_timestamp_is_rejected_before_journaling(self):
        object.__setattr__(self.calibration, "captured_at_utc", "not-a-time")
        with self.assertRaisesRegex(ValueError, "ISO-8601"):
            self.logger.record_calibration(
                self.calibration,
                evidence_snapshot=self.snapshot,
                basis_bundle=_TEST_BASIS_BUNDLE,
            )
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")

    def test_mismatched_basis_manifest_is_rejected_before_journaling(self):
        different_bundle = replace(
            _TEST_BASIS_BUNDLE,
            version="simulator-test-other",
            bundle_id="",
        )
        with self.assertRaisesRegex(ValueError, "basis bundle"):
            self.logger.record_calibration(
                self.calibration,
                evidence_snapshot=self.snapshot,
                basis_bundle=different_bundle,
            )
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")

    def test_lookup_requires_exact_lifecycle_and_bundle(self):
        self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        )
        found = self.logger.find_compatible_calibration(
            session_id=self.calibration.session_id,
            view_segment_id=4,
            visual_history_generation=2,
            basis_bundle_id=self.calibration.basis_bundle_id,
            capture_backend="wgc",
            source_hwnd=9001,
            capture_geometry_id=self.calibration.capture_geometry_id,
        )
        self.assertEqual(found.calibration_id, self.calibration.calibration_id)
        self.assertIsNone(self.logger.find_compatible_calibration(
            session_id=self.calibration.session_id,
            view_segment_id=5,
            visual_history_generation=2,
            basis_bundle_id=self.calibration.basis_bundle_id,
        ))
        self.assertIsNone(self.logger.find_compatible_calibration(
            session_id=self.calibration.session_id,
            view_segment_id=4,
            visual_history_generation=2,
            basis_bundle_id=self.calibration.basis_bundle_id,
            capture_geometry_id="different-geometry",
        ))

    def test_unaccepted_record_is_rejected_without_a_line(self):
        pending, _ = _equalizer_context(
            self.logger.session_dir.name, accepted=False,
        )
        with self.assertRaisesRegex(ValueError, "accepted"):
            self.logger.record_calibration(pending)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")

    def test_incomplete_capture_identity_is_rejected_before_journaling(self):
        # Bypass the DTO's own invariant to exercise the logger boundary too.
        calibration = self.calibration
        object.__setattr__(calibration, "capture_sequence", 0)

        with self.assertRaisesRegex(ValueError, "capture_sequence"):
            self.logger.record_calibration(
                calibration,
                evidence_snapshot=self.snapshot,
                basis_bundle=_TEST_BASIS_BUNDLE,
            )

        self.assertEqual(self.path.read_text(encoding="utf-8"), "")

    def test_journal_handle_is_closed_at_session_end(self):
        handle = self.logger._equalizer_calibration_file
        self.logger.end_session()
        self.assertTrue(handle.closed)
        self.assertIsNone(self.logger._equalizer_calibration_file)

    def test_retrospective_accept_can_append_after_session_end(self):
        self.logger.end_session()

        self.assertTrue(self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        ))

        restored = self.logger.get_historical_calibration(
            self.calibration.calibration_id,
        )
        self.assertIsNotNone(restored)

    def test_orientation_evidence_hash_mismatch_does_not_append_or_activate(self):
        mismatched = replace(
            self.calibration,
            orientation_evidence_sha256="0" * 64,
        )

        self.assertFalse(self.logger.record_calibration(
            mismatched,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        ))

        self.assertEqual(self.path.read_text(encoding="utf-8"), "")
        self.assertIsNone(self.logger.get_calibration(mismatched.calibration_id))
        self.assertFalse(
            (self.logger.session_dir / mismatched.orientation_evidence_path).exists()
        )

    def test_evidence_write_failure_does_not_append_or_activate(self):
        with patch.object(
            self.logger,
            "_atomic_write_bytes",
            return_value=False,
        ):
            self.assertFalse(self.logger.record_calibration(
                self.calibration,
                evidence_snapshot=self.snapshot,
                basis_bundle=_TEST_BASIS_BUNDLE,
            ))

        self.assertEqual(self.path.read_text(encoding="utf-8"), "")
        self.assertIsNone(
            self.logger.get_calibration(self.calibration.calibration_id)
        )

    def test_append_failure_keeps_untrusted_marker_and_removes_new_evidence(self):
        self.logger._equalizer_calibration_file.close()

        class BrokenJournal:
            closed = False

            @staticmethod
            def write(_line):
                raise OSError("simulated append failure")

            @staticmethod
            def flush():
                pass

            @staticmethod
            def close():
                pass

        self.logger._equalizer_calibration_file = BrokenJournal()
        self.assertFalse(self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        ))

        marker = self.path.with_suffix(".untrusted")
        self.assertTrue(marker.exists())
        self.assertFalse(
            (self.logger.session_dir / self.calibration.orientation_evidence_path).exists()
        )
        self.assertFalse(
            self.logger._calibration_transaction_path(
                self.logger.session_dir,
            ).exists()
        )
        self.assertEqual(self.logger.read_calibrations(), {})

    def test_power_loss_before_calibration_journal_rolls_back_evidence(self):
        class SimulatedPowerLoss(BaseException):
            pass

        marker = self.logger._calibration_transaction_path(
            self.logger.session_dir,
        )
        evidence_path = (
            self.logger.session_dir / self.calibration.orientation_evidence_path
        )
        with patch.object(
            self.logger,
            "_append_calibration_event",
            side_effect=SimulatedPowerLoss("power lost before journal append"),
        ):
            with self.assertRaises(SimulatedPowerLoss):
                self.logger.record_calibration(
                    self.calibration,
                    evidence_snapshot=self.snapshot,
                    basis_bundle=_TEST_BASIS_BUNDLE,
                )

        self.assertTrue(marker.exists())
        self.assertTrue(evidence_path.exists())
        GrowthLogger(base_dir=self.tmp.name)
        self.assertFalse(marker.exists())
        self.assertFalse(evidence_path.exists())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")

    def test_power_loss_after_calibration_journal_completes_on_restart(self):
        class SimulatedPowerLoss(BaseException):
            pass

        marker = self.logger._calibration_transaction_path(
            self.logger.session_dir,
        )
        evidence_path = (
            self.logger.session_dir / self.calibration.orientation_evidence_path
        )
        real_unlink = Path.unlink

        def crash_before_wal_clear(path, *args, **kwargs):
            if Path(path) == marker:
                raise SimulatedPowerLoss("power lost before calibration WAL clear")
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=crash_before_wal_clear):
            with self.assertRaises(SimulatedPowerLoss):
                self.logger.record_calibration(
                    self.calibration,
                    evidence_snapshot=self.snapshot,
                    basis_bundle=_TEST_BASIS_BUNDLE,
                )

        self.assertTrue(marker.exists())
        self.assertTrue(evidence_path.exists())
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)
        GrowthLogger(base_dir=self.tmp.name)
        self.assertFalse(marker.exists())
        self.assertTrue(evidence_path.exists())
        self.assertIsNotNone(self.logger.get_historical_calibration(
            self.calibration.calibration_id,
        ))

    def test_malformed_calibration_wal_is_retained_fail_closed(self):
        marker = self.logger._calibration_transaction_path(
            self.logger.session_dir,
        )
        marker.write_text('{"schema_version":1', encoding="utf-8")

        restarted = GrowthLogger(base_dir=self.tmp.name)

        self.assertTrue(marker.exists())
        self.assertFalse(restarted.set_base_dir(self.tmp.name))

    def test_malformed_journal_persists_untrusted_marker(self):
        self.logger._equalizer_calibration_file.close()
        self.logger._equalizer_calibration_file = None
        self.path.write_text('{"journal_schema_version":1', encoding="utf-8")

        self.assertEqual(self.logger.read_calibrations(), {})
        self.assertTrue(self.path.with_suffix(".untrusted").exists())
        self.assertFalse(self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        ))

    def test_tampered_evidence_makes_replay_untrusted(self):
        self.assertTrue(self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        ))
        evidence_path = (
            self.logger.session_dir / self.calibration.orientation_evidence_path
        )
        evidence_path.write_bytes(b"tampered")

        self.assertEqual(self.logger.read_calibrations(), {})
        self.assertEqual(self.logger.read_historical_calibrations(), {})
        self.assertTrue(self.path.with_suffix(".untrusted").exists())

    def test_repeated_post_stop_invalidation_stays_historical_not_active(self):
        self.assertTrue(self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        ))
        self.logger.end_session()

        self.assertTrue(self.logger.record_calibration_invalidation(
            self.calibration,
            "session ended",
        ))
        after_first_invalidation = self.path.read_bytes()
        self.assertTrue(self.logger.record_calibration_invalidation(
            self.calibration,
            "retrospective popup closed",
        ))
        self.assertEqual(self.path.read_bytes(), after_first_invalidation)

        self.assertIsNone(
            self.logger.get_calibration(self.calibration.calibration_id)
        )
        self.assertIsNotNone(self.logger.get_historical_calibration(
            self.calibration.calibration_id,
        ))
        events = [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [event["event"] for event in events],
            ["accepted", "invalidated"],
        )

    def test_repeated_invalidation_still_validates_historical_identity(self):
        self.assertTrue(self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        ))
        self.assertTrue(self.logger.record_calibration_invalidation(
            self.calibration,
            "session ended",
        ))
        mismatched = replace(self.calibration, accepted_by="different grower")

        with self.assertRaisesRegex(ValueError, "does not match journal"):
            self.logger.record_calibration_invalidation(
                mismatched,
                "duplicate lifecycle signal",
            )


class EqualizerLiveLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("LIVE_TYPED")
        self.csv_path = self.logger.session_dir / "live_labels.csv"
        self.calibration, self.snapshot = _equalizer_context(
            self.logger.session_dir.name,
        )
        self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        )

    def tearDown(self):
        self.logger.end_session()
        self.tmp.cleanup()

    def _rows(self) -> list[dict]:
        with open(self.csv_path, newline="") as stream:
            return list(csv.DictReader(stream))

    def test_typed_snapshot_is_saved_with_current_provenance_and_htr_blank(self):
        idx = self.logger.record_live_label(
            12.5,
            {"1x1": 0.6, "Tw(2x1)": 0.2, "c(6x2)": 0.1,
             "RT13": 0.1, "HTR": 0.9},
            calibration=self.calibration,
            snapshot=self.snapshot,
            pyro_temp=650.0,
        )
        self.assertEqual(idx, 1)
        row = self._rows()[0]
        self.assertEqual(row["recon_HTR"], "")
        self.assertEqual(row["calibration_id"], self.calibration.calibration_id)
        self.assertEqual(row["basis_bundle_id"], self.calibration.basis_bundle_id)
        self.assertEqual(
            json.loads(row["equalizer_active_classes"]),
            list(ACTIVE_SIMULATOR_LABELS),
        )
        self.assertEqual(row["capture_sequence"], "102")
        self.assertEqual(
            row["capture_geometry_id"],
            self.snapshot.capture_geometry_id,
        )
        self.assertGreaterEqual(float(row["frame_age_ms"]), 0.0)
        self.assertTrue(Path(row["frame_path"]).exists())

    def test_transactional_writes_reopen_stream_and_keep_monotonic_indices(self):
        first = self.logger.record_live_label(
            1.0,
            {"1x1": 1.0},
            calibration=self.calibration,
            snapshot=self.snapshot,
        )
        second = self.logger.record_live_label(
            2.0,
            {"Tw(2x1)": 1.0},
            calibration=self.calibration,
            snapshot=self.snapshot,
        )

        self.assertEqual((first, second), (1, 2))
        self.assertEqual(
            [row["label_idx"] for row in self._rows()],
            ["1", "2"],
        )
        self.assertFalse(self.logger._live_label_transaction_path(
            self.logger.session_dir,
        ).exists())

    def test_missing_or_stale_context_does_not_mutate_counter_or_frames(self):
        with self.assertRaisesRegex(TypeError, "accepted calibration"):
            self.logger.record_live_label(1.0, {"1x1": 1.0})
        stale_snapshot = RheedFrameSnapshot.freeze(
            self.snapshot.rgb,
            self.snapshot.grayscale,
            captured_at_utc=self.snapshot.captured_at_utc,
            received_monotonic_ns=time.monotonic_ns(),
            capture_sequence=self.snapshot.capture_sequence,
            source_hwnd=self.snapshot.source_hwnd,
            capture_backend=self.snapshot.capture_backend,
            capture_geometry_id=self.snapshot.capture_geometry_id,
            session_id=self.snapshot.session_id,
            view_segment_id=99,
            visual_history_generation=self.snapshot.visual_history_generation,
            gun_aligned=True,
        )
        with self.assertRaisesRegex(ValueError, "view segment"):
            self.logger.record_live_label(
                1.0, {"1x1": 1.0},
                calibration=self.calibration, snapshot=stale_snapshot,
            )
        self.assertEqual(self.logger._live_label_counter, 0)
        self.assertEqual(self._rows(), [])
        self.assertFalse(any(
            path.name.startswith("live_label_")
            for path in (self.logger.session_dir / "frames").iterdir()
        ))

    def test_invalid_weight_does_not_create_an_orphan(self):
        with self.assertRaisesRegex(ValueError, "must be in"):
            self.logger.record_live_label(
                1.0, {"1x1": 1.5},
                calibration=self.calibration, snapshot=self.snapshot,
            )
        self.assertEqual(self.logger._live_label_counter, 0)
        self.assertFalse(any(
            path.name.startswith("live_label_")
            for path in (self.logger.session_dir / "frames").iterdir()
        ))

    def test_image_encoder_failure_does_not_write_live_row_or_counter(self):
        with patch.object(self.logger, "_save_rgb_bmp", return_value=False):
            self.assertEqual(self.logger.record_live_label(
                1.0,
                {"1x1": 1.0},
                calibration=self.calibration,
                snapshot=self.snapshot,
            ), 0)

        self.assertEqual(self.logger._live_label_counter, 0)
        self.assertEqual(self._rows(), [])

    def test_csv_replace_failure_rolls_back_wal_and_image(self):
        original = self.csv_path.read_bytes()
        marker = self.logger._live_label_transaction_path(
            self.logger.session_dir,
        )
        with patch.object(self.logger, "_atomic_write_csv", return_value=False):
            self.assertEqual(self.logger.record_live_label(
                1.0,
                {"1x1": 1.0},
                calibration=self.calibration,
                snapshot=self.snapshot,
            ), 0)

        self.assertEqual(self.csv_path.read_bytes(), original)
        self.assertFalse(marker.exists())
        self.assertEqual(self._rows(), [])
        self.assertEqual(list((self.logger.session_dir / "frames").glob(
            "*live_label*",
        )), [])

    def test_power_loss_before_csv_commit_is_rolled_back_on_restart(self):
        class SimulatedPowerLoss(BaseException):
            pass

        marker = self.logger._live_label_transaction_path(
            self.logger.session_dir,
        )
        with patch.object(
            self.logger,
            "_atomic_write_csv",
            side_effect=SimulatedPowerLoss("power lost before CSV replace"),
        ):
            with self.assertRaises(SimulatedPowerLoss):
                self.logger.record_live_label(
                    1.0,
                    {"1x1": 1.0},
                    calibration=self.calibration,
                    snapshot=self.snapshot,
                )

        transaction = json.loads(marker.read_text(encoding="utf-8"))
        final_path = (
            self.logger.session_dir / transaction["final_frame_path"]
        )
        self.assertTrue(final_path.exists())
        self.assertEqual(self._rows(), [])

        restarted = GrowthLogger(
            base_dir=Path(self.tmp.name) / "constructor-default",
        )
        self.assertTrue(marker.exists())
        self.assertTrue(restarted.set_base_dir(self.tmp.name))

        self.assertFalse(marker.exists())
        self.assertFalse(final_path.exists())
        self.assertEqual(self._rows(), [])

    def test_power_loss_during_image_encode_removes_private_temp_on_restart(self):
        class SimulatedPowerLoss(BaseException):
            pass

        marker = self.logger._live_label_transaction_path(
            self.logger.session_dir,
        )
        interrupted_temp = None

        def interrupt_encoder(_frame, pending_path):
            nonlocal interrupted_temp
            interrupted_temp = (
                pending_path.parent / f".{pending_path.stem}.crash.tmp.bmp"
            )
            interrupted_temp.write_bytes(b"partial BMP evidence")
            raise SimulatedPowerLoss("power lost during image encode")

        with patch.object(
            self.logger,
            "_save_rgb_bmp",
            side_effect=interrupt_encoder,
        ):
            with self.assertRaises(SimulatedPowerLoss):
                self.logger.record_live_label(
                    1.0,
                    {"1x1": 1.0},
                    calibration=self.calibration,
                    snapshot=self.snapshot,
                )

        self.assertTrue(marker.exists())
        self.assertTrue(interrupted_temp.exists())

        GrowthLogger(base_dir=self.tmp.name)

        self.assertFalse(marker.exists())
        self.assertFalse(interrupted_temp.exists())
        self.assertEqual(self._rows(), [])

    def test_power_loss_after_csv_commit_is_completed_on_restart(self):
        class SimulatedPowerLoss(BaseException):
            pass

        marker = self.logger._live_label_transaction_path(
            self.logger.session_dir,
        )
        original_unlink = Path.unlink

        def crash_before_wal_clear(path, *args, **kwargs):
            if path == marker:
                raise SimulatedPowerLoss("power lost before WAL clear")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=crash_before_wal_clear):
            with self.assertRaises(SimulatedPowerLoss):
                self.logger.record_live_label(
                    1.0,
                    {"1x1": 1.0},
                    calibration=self.calibration,
                    snapshot=self.snapshot,
                )

        row = self._rows()[0]
        final_path = Path(row["frame_path"])
        self.assertTrue(marker.exists())
        self.assertTrue(final_path.exists())

        GrowthLogger(base_dir=self.tmp.name)

        self.assertFalse(marker.exists())
        self.assertTrue(final_path.exists())
        self.assertEqual(len(self._rows()), 1)


class EqualizerEventLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("EVENT_TYPED")
        self.calibration, self.snapshot = _equalizer_context(
            self.logger.session_dir.name,
        )
        self.logger.record_calibration(
            self.calibration,
            evidence_snapshot=self.snapshot,
            basis_bundle=_TEST_BASIS_BUNDLE,
        )
        self.selected_frame = self.logger.session_dir / "frames" / "selected.bmp"
        self.selected_frame.write_bytes(b"existing event evidence")

    def tearDown(self):
        self.logger.end_session()
        self.tmp.cleanup()

    def test_typed_event_label_reuses_selected_frame_and_blanks_htr(self):
        self.assertTrue(self.logger.update_event_label(
            7,
            recon_1x1=0.7,
            recon_tw=0.1,
            recon_c6x2=0.1,
            recon_rt13=0.1,
            recon_HTR=1.0,
            calibration=self.calibration,
            snapshot=self.snapshot,
            frame_path=str(self.selected_frame),
        ))
        row = self.logger.read_event_labels()[7]
        self.assertEqual(row["recon_HTR"], "")
        self.assertEqual(row["calibration_id"], self.calibration.calibration_id)
        self.assertEqual(row["capture_sequence"], "102")
        self.assertEqual(
            row["capture_geometry_id"],
            self.snapshot.capture_geometry_id,
        )
        self.assertEqual(Path(row["frame_path"]), self.selected_frame.resolve())

    def test_explicit_context_resolves_active_journal_record(self):
        self.assertTrue(self.logger.update_event_label(
            8,
            recon_1x1=1.0,
            recon_HTR=0.5,
            calibration_id=self.calibration.calibration_id,
            basis_bundle_id=self.calibration.basis_bundle_id,
            equalizer_active_classes=self.calibration.active_classes,
            view_segment_id=self.calibration.view_segment_id,
            visual_history_generation=self.calibration.visual_history_generation,
            frame_path=str(self.selected_frame),
            capture_backend="wgc",
            captured_at_utc="2026-07-31T12:00:01.000Z",
            capture_sequence=102,
            frame_age_ms=4.0,
            source_hwnd=9001,
            capture_geometry_id=self.calibration.capture_geometry_id,
        ))
        row = self.logger.read_event_labels()[8]
        self.assertEqual(row["frame_path"], str(self.selected_frame.resolve()))
        self.assertEqual(row["recon_HTR"], "")

    def test_legacy_csv_read_fills_new_columns(self):
        path = self.logger.session_dir / "events_labels.csv"
        legacy_fields = GrowthLogger.EVENT_LABEL_FIELDS[:11]
        with open(path, "w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=legacy_fields)
            writer.writeheader()
            writer.writerow({
                field: ("3" if field == "event_idx" else "")
                for field in legacy_fields
            })
        row = self.logger.read_event_labels()[3]
        self.assertEqual(set(row), set(GrowthLogger.EVENT_LABEL_FIELDS))
        self.assertEqual(row["calibration_id"], "")
        self.assertEqual(row["capture_backend"], "")
        self.assertEqual(row["capture_geometry_id"], "")

    def test_missing_existing_frame_is_rejected_without_event_row(self):
        csv_path = self.logger.session_dir / "events_labels.csv"
        with self.assertRaisesRegex(ValueError, "must exist"):
            self.logger.update_event_label(
                9,
                recon_1x1=1.0,
                calibration=self.calibration,
                snapshot=self.snapshot,
                frame_path=str(
                    self.logger.session_dir / "frames" / "missing.bmp"
                ),
            )

        self.assertFalse(csv_path.exists())

    def test_csv_replace_failure_preserves_old_file(self):
        self.assertTrue(self.logger.update_event_label(10, notes="before"))
        csv_path = self.logger.session_dir / "events_labels.csv"
        before = csv_path.read_bytes()

        with patch(
            "gui.growth_logger.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            self.assertFalse(self.logger.update_event_label(
                10,
                notes="after",
            ))

        self.assertEqual(csv_path.read_bytes(), before)
        self.assertEqual(list(csv_path.parent.glob(f".{csv_path.name}.*.tmp")), [])

    def test_csv_write_failure_preserves_existing_event_image(self):
        with patch.object(self.logger, "_atomic_write_csv", return_value=False):
            self.assertFalse(self.logger.update_event_label(
                11,
                recon_1x1=1.0,
                calibration=self.calibration,
                snapshot=self.snapshot,
                frame_path=str(self.selected_frame),
            ))

        self.assertTrue(self.selected_frame.exists())
        self.assertNotIn(11, self.logger.read_event_labels())


class UpdateEventLabelPartialUpdateTests(unittest.TestCase):
    """Jul 15 2026 additions: change_from and change_to are UI-populated
    via events_tab dropdowns. update_event_label's None-preserving
    contract must not clobber sibling columns when only one of these
    kwargs is passed. Guards against a subtle bug where the two new
    columns' UI-populated status accidentally makes them "always
    written" rather than "written when explicitly passed".
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("TEST_UPDATE_LABEL")

    def tearDown(self):
        try:
            self.logger.end_session()
        except Exception:
            pass
        self.tmp.cleanup()

    def _read_row_for_event(self, event_idx: int) -> dict:
        csv_path = self.logger.session_dir / "events_labels.csv"
        with open(csv_path) as f:
            for row in csv.DictReader(f):
                if row.get("event_idx") == str(event_idx):
                    return row
        return {}

    def test_update_event_label_change_from_only_preserves_other_fields(self):
        # Set primary + notes, then update change_from alone. Expect the
        # earlier fields to survive the partial update.
        self.logger.update_event_label(
            event_idx=1,
            primary_reconstruction="c(6x2)",
            notes="test note",
        )
        self.logger.update_event_label(event_idx=1, change_from="1x1")

        row = self._read_row_for_event(1)
        self.assertEqual(row["primary_reconstruction"], "c(6x2)")
        self.assertEqual(row["notes"], "test note")
        self.assertEqual(row["change_from"], "1x1")
        # change_to still blank — not touched by either call.
        self.assertEqual(row["change_to"], "")

    def test_update_event_label_change_to_only_preserves_other_fields(self):
        # Mirror of above for change_to.
        self.logger.update_event_label(
            event_idx=2,
            primary_reconstruction="rt13xrt13",
            notes="another note",
        )
        self.logger.update_event_label(event_idx=2, change_to="HTR")

        row = self._read_row_for_event(2)
        self.assertEqual(row["primary_reconstruction"], "rt13xrt13")
        self.assertEqual(row["notes"], "another note")
        self.assertEqual(row["change_to"], "HTR")
        self.assertEqual(row["change_from"], "")

    def test_update_change_from_and_to_together(self):
        # Common case: grower sets both dropdowns as consecutive
        # dropdown activations. Two separate update calls, one per
        # dropdown, must both land.
        self.logger.update_event_label(event_idx=3, change_from="1x1")
        self.logger.update_event_label(event_idx=3, change_to="Twinned (2x1)")

        row = self._read_row_for_event(3)
        self.assertEqual(row["change_from"], "1x1")
        self.assertEqual(row["change_to"], "Twinned (2x1)")


class ManualEventSchemaTests(unittest.TestCase):
    """Schema-level tests for MANUAL_EVENT_FIELDS (Jul 10 2026 addition).

    The scrubber timeline (workstream #3) will read manual_events.csv, so
    every field name is load-bearing. These tests lock the contract.
    """

    def test_schema_contains_expected_columns(self):
        expected = {
            "timestamp", "elapsed_s", "event_idx",
            "pyrometer_temp_C",
            "voltage_V", "current_A", "psu_source",
            "frame_path", "note",
            "capture_backend", "captured_at_utc", "capture_sequence",
            "frame_age_ms", "source_hwnd", "capture_geometry_id",
        }
        self.assertEqual(set(GrowthLogger.MANUAL_EVENT_FIELDS), expected)

    def test_timestamp_first_event_idx_third(self):
        # Ordering convention: timestamp is column 1 (mirrors other logs),
        # event_idx is column 3 (after elapsed_s). Downstream sort/plot
        # code relies on this ordering.
        self.assertEqual(GrowthLogger.MANUAL_EVENT_FIELDS[0], "timestamp")
        self.assertEqual(GrowthLogger.MANUAL_EVENT_FIELDS[1], "elapsed_s")
        self.assertEqual(GrowthLogger.MANUAL_EVENT_FIELDS[2], "event_idx")


class AdsSensorSchemaTests(unittest.TestCase):
    """Tests for the Jul 27 2026 ADS union schema in SENSOR_FIELDS.

    Verifies that the 8-column-per-cell union schema (T/T_set/active_setpoint/
    V/I/prog_V/prog_A/power) is present for cells 1-7, that Bulbasaur-shape
    ads_cells (only cells 1-6 keyed) populates cell1-6 and leaves cell7 blank,
    and that non-ADS mode leaves all cell columns blank. Also confirms the
    historical `cell{i}_T_C` column name is preserved verbatim.
    """

    ADS_CELL_SUFFIXES = (
        "T_C", "T_set_C", "active_setpoint_C",
        "V", "I", "prog_V", "prog_A", "power_W",
    )

    def test_schema_has_all_56_cell_columns(self):
        for i in range(1, 8):
            for sfx in self.ADS_CELL_SUFFIXES:
                col = f"cell{i}_{sfx}"
                self.assertIn(col, GrowthLogger.SENSOR_FIELDS,
                              f"missing column: {col}")

    def test_historical_cell_T_C_columns_preserved(self):
        # Regression guard: downstream analysis may depend on these names.
        for i in range(1, 8):
            self.assertIn(f"cell{i}_T_C", GrowthLogger.SENSOR_FIELDS)

    def test_bulbasaur_shape_populates_cells_1_to_6_blanks_cell7(self):
        # Simulate a Bulbasaur MistralAdsClient(cell_count=6).read() dict:
        # 8 keys per cell for cells 1-6, no cell7 keys.
        ads_cells = {}
        for i in range(1, 7):
            ads_cells[f"cell{i}_T"] = 100.0 + i
            ads_cells[f"cell{i}_T_set"] = 105.0 + i
            ads_cells[f"cell{i}_active_setpoint"] = 103.0 + i
            ads_cells[f"cell{i}_V"] = 1.0 + i * 0.1
            ads_cells[f"cell{i}_I"] = 2.0 + i * 0.01
            ads_cells[f"cell{i}_prog_V"] = 60.0 + i
            ads_cells[f"cell{i}_prog_A"] = 5.0 + i * 0.5
            ads_cells[f"cell{i}_power"] = 9.0 + i

        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("TEST_ADS_BULBASAUR")
            logger.log_sensors(
                pyro_temp=None, elapsed_s=1.0,
                ads_cells=ads_cells,
            )
            logger.end_session()

            csv_path = logger.session_dir / "sensor_log.csv"
            with open(csv_path, newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Cells 1-6 populated (non-empty strings)
        for i in range(1, 7):
            for sfx in self.ADS_CELL_SUFFIXES:
                col = f"cell{i}_{sfx}"
                self.assertNotEqual(
                    row[col], "",
                    f"cell{i} column {col} should be populated",
                )
        # Cell 7 blank across all 8 columns
        for sfx in self.ADS_CELL_SUFFIXES:
            col = f"cell7_{sfx}"
            self.assertEqual(row[col], "",
                             f"cell7 column {col} should be blank on Bulbasaur")

    def test_non_ads_mode_leaves_all_cell_columns_blank(self):
        # Non-ADS mode passes ads_cells=None; every cell column should be
        # blank in the CSV.
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("TEST_NO_ADS")
            logger.log_sensors(
                pyro_temp=None, elapsed_s=1.0,
                ads_cells=None,
            )
            logger.end_session()

            csv_path = logger.session_dir / "sensor_log.csv"
            with open(csv_path, newline="") as f:
                rows = list(csv.DictReader(f))

        row = rows[0]
        for i in range(1, 8):
            for sfx in self.ADS_CELL_SUFFIXES:
                col = f"cell{i}_{sfx}"
                self.assertEqual(row[col], "",
                                 f"column {col} should be blank when ads_cells=None")


class ManualEventLifecycleTests(unittest.TestCase):
    """File-lifecycle tests: manual_events.csv opens on start_session,
    closes on end_session, and has the correct header."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)

    def tearDown(self):
        try:
            self.logger.end_session()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_manual_events_csv_created_on_start_session(self):
        self.logger.start_session("TEST_MANUAL_EVENTS")
        csv_path = self.logger.session_dir / "manual_events.csv"
        self.assertTrue(csv_path.exists())

    def test_manual_events_csv_has_correct_header(self):
        self.logger.start_session("TEST_HEADER")
        csv_path = self.logger.session_dir / "manual_events.csv"
        with open(csv_path, "r", newline="") as f:
            header = next(csv.reader(f))
        self.assertEqual(header, GrowthLogger.MANUAL_EVENT_FIELDS)

    def test_counter_resets_on_new_session(self):
        # Log an event, end the session, start a new one — counter should
        # be 0 again so the next event is idx=1.
        self.logger.start_session("TEST_A")
        idx_a = self.logger.record_manual_event(elapsed_s=1.0)
        self.assertEqual(idx_a, 1)
        self.logger.end_session()

        self.logger.start_session("TEST_B")
        idx_b = self.logger.record_manual_event(elapsed_s=2.0)
        # Fresh session → fresh counter starting at 1, not 2.
        self.assertEqual(idx_b, 1)

    def test_file_closed_on_end_session(self):
        self.logger.start_session("TEST_CLOSE")
        self.logger.end_session()
        # Writer refs cleared so a stray record_manual_event no-ops safely.
        self.assertIsNone(self.logger._manual_event_writer)

    def test_no_session_returns_zero(self):
        # No start_session — record must silently no-op with idx 0.
        idx = self.logger.record_manual_event(elapsed_s=10.0)
        self.assertEqual(idx, 0)


def _dummy_frame() -> np.ndarray:
    """A modest-intensity 100x100x3 frame that PIL / cv2 can encode as BMP."""
    return (np.random.default_rng(7).integers(40, 200, (100, 100, 3))
            .astype(np.uint8))


class ManualEventRecordTests(unittest.TestCase):
    """End-to-end tests for record_manual_event's row + frame behavior."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("TEST_RECORD")
        self.csv_path = self.logger.session_dir / "manual_events.csv"

    def tearDown(self):
        try:
            self.logger.end_session()
        except Exception:
            pass
        self.tmp.cleanup()

    def _read_rows(self) -> list[dict]:
        with open(self.csv_path, "r", newline="") as f:
            return list(csv.DictReader(f))

    def test_bare_click_writes_one_row_with_index_1(self):
        # Simulates a grower click with no note, no frame, no PSU state.
        idx = self.logger.record_manual_event(elapsed_s=42.0)
        self.assertEqual(idx, 1)

        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_idx"], "1")
        self.assertEqual(rows[0]["elapsed_s"], "42.00")
        # Optional fields blank when None is passed.
        self.assertEqual(rows[0]["pyrometer_temp_C"], "")
        self.assertEqual(rows[0]["voltage_V"], "")
        self.assertEqual(rows[0]["current_A"], "")
        self.assertEqual(rows[0]["psu_source"], "none")
        self.assertEqual(rows[0]["frame_path"], "")
        self.assertEqual(rows[0]["note"], "")

    def test_full_payload_populates_all_columns(self):
        idx = self.logger.record_manual_event(
            elapsed_s=123.45,
            pyro_temp=650.7,
            voltage_V=12.345,
            current_A=0.678,
            psu_source="mistral",
            note="rt13 blooming",
        )
        self.assertEqual(idx, 1)
        rows = self._read_rows()
        self.assertEqual(rows[0]["elapsed_s"], "123.45")
        self.assertEqual(rows[0]["pyrometer_temp_C"], "650.7")
        self.assertEqual(rows[0]["voltage_V"], "12.345")
        self.assertEqual(rows[0]["current_A"], "0.678")
        self.assertEqual(rows[0]["psu_source"], "mistral")
        self.assertEqual(rows[0]["note"], "rt13 blooming")

    def test_counter_increments_monotonically_across_calls(self):
        # Multi-click safety: consecutive fast clicks all land as
        # distinct events with monotonically increasing indices.
        idxs = [
            self.logger.record_manual_event(elapsed_s=float(i))
            for i in range(5)
        ]
        self.assertEqual(idxs, [1, 2, 3, 4, 5])
        rows = self._read_rows()
        self.assertEqual(len(rows), 5)
        # Row order matches call order (append semantics).
        self.assertEqual(
            [r["event_idx"] for r in rows],
            ["1", "2", "3", "4", "5"],
        )

    def test_frame_is_saved_when_provided(self):
        # A supplied frame should be written to frames/ as
        # manual_event_NNN_HHMMSS.bmp; the row's frame_path points to it.
        frame = _dummy_frame()
        idx = self.logger.record_manual_event(elapsed_s=5.0, frame=frame)
        self.assertEqual(idx, 1)

        rows = self._read_rows()
        frame_path = rows[0]["frame_path"]
        self.assertNotEqual(frame_path, "")
        self.assertTrue(Path(frame_path).exists())
        self.assertTrue(
            Path(frame_path).name.startswith("manual_event_001_"),
            f"expected manual_event_001_ prefix, got {Path(frame_path).name}",
        )
        self.assertTrue(frame_path.endswith(".bmp"))

    def test_frame_path_empty_when_no_frame_passed(self):
        # A bare click still records the event but leaves frame_path
        # blank so the scrubber can render "mark-only" tick marks.
        idx = self.logger.record_manual_event(elapsed_s=5.0, frame=None)
        self.assertEqual(idx, 1)
        rows = self._read_rows()
        self.assertEqual(rows[0]["frame_path"], "")

    def test_frame_files_are_distinct_across_events(self):
        # Frame filename includes the event_idx so consecutive marks
        # don't collide even at the same HH:MM:SS second.
        frame = _dummy_frame()
        self.logger.record_manual_event(elapsed_s=1.0, frame=frame)
        self.logger.record_manual_event(elapsed_s=1.1, frame=frame)
        rows = self._read_rows()
        p1 = Path(rows[0]["frame_path"]).name
        p2 = Path(rows[1]["frame_path"]).name
        self.assertNotEqual(p1, p2)
        self.assertIn("manual_event_001_", p1)
        self.assertIn("manual_event_002_", p2)

    def test_saved_bmp_is_valid_bmp_format(self):
        frame = _dummy_frame()
        self.logger.record_manual_event(elapsed_s=1.0, frame=frame)
        rows = self._read_rows()
        from PIL import Image
        with Image.open(rows[0]["frame_path"]) as img:
            self.assertEqual(img.format, "BMP")


class RheedViewEventSchemaTests(unittest.TestCase):
    """The structured acquisition/QC log has a stable, separate schema."""

    def test_schema_is_exact_and_manual_schema_is_unchanged(self):
        expected = [
            "timestamp", "elapsed_s", "event_idx", "event_type",
            "realignment_id",
            "previous_view_segment_id", "view_segment_id",
            "visual_history_generation",
            "gun_aligned",
            "history_frame_count", "history_required", "history_ready",
            "qc_reject", "qc_reason", "labeler", "confidence",
            "prediction_actionable",
            "frame_role", "frame_path", "note",
            "capture_backend", "captured_at_utc", "capture_sequence",
            "frame_age_ms", "source_hwnd", "capture_geometry_id",
        ]
        self.assertEqual(GrowthLogger.RHEED_VIEW_EVENT_FIELDS, expected)
        self.assertNotIn("event_type", GrowthLogger.MANUAL_EVENT_FIELDS)
        self.assertNotIn("qc_reject", GrowthLogger.MANUAL_EVENT_FIELDS)

    def test_event_type_vocabulary_is_explicit(self):
        self.assertEqual(
            RHEED_VIEW_EVENT_TYPES,
            {
                RHEED_VIEW_EVENT_SESSION_START,
                RHEED_VIEW_EVENT_ALIGNMENT_CONFIRMED,
                RHEED_VIEW_EVENT_REALIGN_START,
                RHEED_VIEW_EVENT_REALIGN_END,
                RHEED_VIEW_EVENT_HISTORY_RESET,
                RHEED_VIEW_EVENT_HISTORY_READY,
                RHEED_VIEW_EVENT_QC_REJECT,
                RHEED_VIEW_EVENT_QC_PASS,
                RHEED_VIEW_EVENT_SAMPLE_DIRECTION_ADJUSTED,
                RHEED_VIEW_EVENT_CURRENT_ADJUSTED,
                RHEED_VIEW_EVENT_ENERGY_ADJUSTED,
            },
        )


class RheedViewEventLifecycleTests(unittest.TestCase):
    """File creation, counter reset, metadata count, and close behavior."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)

    def tearDown(self):
        try:
            self.logger.end_session()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_file_is_created_with_flushed_header(self):
        self.logger.start_session("RHEED_VIEW_HEADER")
        path = self.logger.session_dir / "rheed_view_events.csv"
        self.assertTrue(path.exists())
        with open(path, newline="") as stream:
            self.assertEqual(
                next(csv.reader(stream)),
                GrowthLogger.RHEED_VIEW_EVENT_FIELDS,
            )

    def test_counter_is_monotonic_and_resets_for_new_session(self):
        self.logger.start_session("RHEED_VIEW_A")
        self.assertEqual(
            self.logger.record_rheed_view_event(
                RHEED_VIEW_EVENT_SESSION_START, 0.0,
            ),
            1,
        )
        self.assertEqual(
            self.logger.record_rheed_view_event(
                RHEED_VIEW_EVENT_REALIGN_START, 1.0,
            ),
            2,
        )
        self.logger.end_session()

        self.logger.start_session("RHEED_VIEW_B")
        self.assertEqual(
            self.logger.record_rheed_view_event(
                RHEED_VIEW_EVENT_SESSION_START, 0.0,
            ),
            1,
        )

    def test_valid_event_without_session_returns_zero(self):
        self.assertEqual(
            self.logger.record_rheed_view_event(
                RHEED_VIEW_EVENT_QC_REJECT, 1.0,
            ),
            0,
        )

    def test_end_session_closes_and_clears_writer(self):
        self.logger.start_session("RHEED_VIEW_CLOSE")
        event_file = self.logger._rheed_view_event_file
        self.logger.end_session()
        self.assertTrue(event_file.closed)
        self.assertIsNone(self.logger._rheed_view_event_file)
        self.assertIsNone(self.logger._rheed_view_event_writer)

    def test_session_metadata_contains_event_count(self):
        self.logger.start_session("RHEED_VIEW_METADATA")
        for event_type in (
            RHEED_VIEW_EVENT_SESSION_START,
            RHEED_VIEW_EVENT_REALIGN_START,
            RHEED_VIEW_EVENT_REALIGN_END,
        ):
            self.logger.record_rheed_view_event(event_type, 1.0)
        self.logger.save_session_metadata({})
        metadata_path = self.logger.session_dir / "session_metadata.json"
        with open(metadata_path) as stream:
            metadata = json.load(stream)
        self.assertEqual(metadata["rheed_view_event_count"], 3)


class RheedViewEventRecordTests(unittest.TestCase):
    """Rows preserve state, frame evidence, and camera provenance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = GrowthLogger(base_dir=self.tmp.name)
        self.logger.start_session("RHEED_VIEW_RECORD")
        self.csv_path = self.logger.session_dir / "rheed_view_events.csv"

    def tearDown(self):
        try:
            self.logger.end_session()
        except Exception:
            pass
        self.tmp.cleanup()

    def _read_rows(self) -> list[dict]:
        with open(self.csv_path, newline="") as stream:
            return list(csv.DictReader(stream))

    def test_full_state_snapshot_and_capture_provenance_are_written(self):
        state = {
            "view_segment_id": 4,
            "gun_aligned": True,
            "history_frame_count": 0,
            "history_required": 32,
            "history_ready": False,
            "qc_reject": False,
            "qc_reason": "",
            "prediction_actionable": False,
        }
        idx = self.logger.record_rheed_view_event(
            RHEED_VIEW_EVENT_REALIGN_END,
            123.456,
            state_snapshot=state,
            realignment_id=7,
            previous_view_segment_id=3,
            frame_role="post_realign",
            note="new gun direction locked",
            capture_metadata={
                "capture_backend": "wgc",
                "captured_at_utc": "2026-07-29T20:00:00.123Z",
                "capture_sequence": 99,
                "frame_age_ms": 4.5678,
                "source_hwnd": 4321,
            },
        )
        self.assertEqual(idx, 1)
        row = self._read_rows()[0]
        self.assertEqual(row["elapsed_s"], "123.46")
        self.assertEqual(row["event_type"], RHEED_VIEW_EVENT_REALIGN_END)
        self.assertEqual(row["realignment_id"], "7")
        self.assertEqual(row["previous_view_segment_id"], "3")
        self.assertEqual(row["view_segment_id"], "4")
        self.assertEqual(row["gun_aligned"], "True")
        self.assertEqual(row["history_frame_count"], "0")
        self.assertEqual(row["history_required"], "32")
        self.assertEqual(row["history_ready"], "False")
        self.assertEqual(row["qc_reject"], "False")
        self.assertEqual(row["prediction_actionable"], "False")
        self.assertEqual(row["frame_role"], "post_realign")
        self.assertEqual(row["note"], "new gun direction locked")
        self.assertEqual(row["capture_backend"], "wgc")
        self.assertEqual(
            row["captured_at_utc"], "2026-07-29T20:00:00.123Z",
        )
        self.assertEqual(row["capture_sequence"], "99")
        self.assertEqual(row["frame_age_ms"], "4.568")
        self.assertEqual(row["source_hwnd"], "4321")

    def test_ids_may_come_from_state_snapshot(self):
        self.logger.record_rheed_view_event(
            RHEED_VIEW_EVENT_REALIGN_START,
            5.0,
            state_snapshot={
                "realignment_id": 8,
                "previous_view_segment_id": 4,
                "view_segment_id": None,
                "gun_aligned": False,
                "qc_reject": True,
                "qc_reason": "gun_realigning",
            },
        )
        row = self._read_rows()[0]
        self.assertEqual(row["realignment_id"], "8")
        self.assertEqual(row["previous_view_segment_id"], "4")
        self.assertEqual(row["view_segment_id"], "")
        self.assertEqual(row["gun_aligned"], "False")
        self.assertEqual(row["qc_reject"], "True")
        self.assertEqual(row["qc_reason"], "gun_realigning")

    def test_event_frame_is_saved_and_bound_to_row(self):
        idx = self.logger.record_rheed_view_event(
            RHEED_VIEW_EVENT_QC_REJECT,
            3.0,
            frame=_dummy_frame(),
            frame_role="qc_reject",
        )
        self.assertEqual(idx, 1)
        row = self._read_rows()[0]
        path = Path(row["frame_path"])
        self.assertTrue(path.exists())
        self.assertIn(
            "rheed_view_event_001_qc_reject_", path.name,
        )
        self.assertEqual(path.suffix.lower(), ".bmp")
        from PIL import Image
        with Image.open(path) as image:
            self.assertEqual(image.format, "BMP")

    def test_invalid_event_type_raises_without_mutating_counter_or_file(self):
        with self.assertRaisesRegex(ValueError, "Unsupported RHEED view"):
            self.logger.record_rheed_view_event("free_form_typo", 1.0)
        self.assertEqual(self.logger._rheed_view_event_counter, 0)
        self.assertEqual(self._read_rows(), [])

    def test_non_mapping_state_snapshot_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "must be a mapping"):
            self.logger.record_rheed_view_event(
                RHEED_VIEW_EVENT_HISTORY_READY,
                1.0,
                state_snapshot=["not", "a", "mapping"],
            )
        self.assertEqual(self.logger._rheed_view_event_counter, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
