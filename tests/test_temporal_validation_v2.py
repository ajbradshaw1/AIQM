"""Deterministic tests for GUI temporal validation v2."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from drivers.evap_control import EvapControl  # noqa: E402
from drivers.mistral import MistralGui  # noqa: E402
from gui.growth_logger import GrowthLogger  # noqa: E402
from gui.state import CameraState, MistralState, PyrometerState  # noqa: E402
from gui.temporal_observability import (  # noqa: E402
    snapshot_state,
    synchronization_summary,
)
from gui.workers import (  # noqa: E402
    _emission_snapshot,
    _mark_attempt_completed,
    _mark_read_failed,
    _mark_sample_received,
)
from scripts.ombe_timing_probe import (  # noqa: E402
    summarize_operator_actions,
    summarize_trace,
)
from scripts.validate_temporal_session import validate  # noqa: E402


class TimingMathTests(unittest.TestCase):
    def test_known_delays_use_only_monotonic_clock(self):
        for delay_ms in (0, 100, 500, 1500):
            state = MistralState(
                connected=True,
                valid=True,
                sample_sequence=4,
                received_at_utc="2099-01-01T00:00:00+00:00",
                received_monotonic_ns=10_000_000_000,
            )
            snapshot = snapshot_state(
                "mistral", state,
                10_000_000_000 + delay_ms * 1_000_000,
            )
            self.assertEqual(snapshot.age_ms, float(delay_ms))

    def test_sync_span_and_structural_validity(self):
        states = {}
        for index, source in enumerate((
            "rheed", "pyrometer", "mistral", "evap",
        )):
            state = MistralState(
                connected=True, valid=True, sample_sequence=1,
                received_at_utc="2026-08-02T12:00:00+00:00",
                received_monotonic_ns=1_000_000_000 + index * 500_000,
            )
            states[source] = snapshot_state(
                source, state, 2_000_000_000,
            )
        summary = synchronization_summary(states)
        self.assertTrue(summary["sync_complete"])
        self.assertTrue(summary["sync_valid"])
        self.assertEqual(summary["sync_span_ms"], 1.5)

        states["evap"] = snapshot_state("evap", MistralState())
        summary = synchronization_summary(states)
        self.assertFalse(summary["sync_complete"])
        self.assertFalse(summary["sync_valid"])

    def test_failure_does_not_advance_or_refresh_sample(self):
        state = PyrometerState(sample_sequence=7, valid=True)
        with (
            patch("gui.workers.time.perf_counter_ns", return_value=5_000_000),
            patch("gui.workers._utc_iso_now", return_value="t"),
        ):
            _mark_sample_received(state, 4_000_000)
        previous_received = state.received_monotonic_ns
        self.assertEqual(state.sample_sequence, 8)
        _mark_read_failed(state, "cable disconnected")
        self.assertEqual(state.sample_sequence, 8)
        self.assertEqual(state.received_monotonic_ns, previous_received)
        self.assertFalse(state.valid)

    def test_emission_returns_an_independent_state(self):
        state = MistralState(sample_sequence=2)
        with patch("gui.workers.time.perf_counter_ns", return_value=99):
            emitted = _emission_snapshot(state)
        state.sample_sequence = 3
        self.assertEqual(emitted.sample_sequence, 2)
        self.assertEqual(emitted.worker_emitted_monotonic_ns, 99)

    def test_failed_ocr_attempt_does_not_replace_sample_capture(self):
        state = MistralState(
            sample_sequence=2,
            valid=False,
            received_at_utc="sample-received",
            received_monotonic_ns=2_000_000,
            capture_completed_at_utc="sample-capture",
            capture_completed_monotonic_ns=1_500_000,
            processing_duration_ms=0.5,
        )
        with patch("gui.workers._utc_iso_now", return_value="attempt-done"):
            _mark_attempt_completed(
                state,
                read_started_ns=3_000_000,
                completed_ns=5_000_000,
                capture_at_utc="attempt-capture",
                capture_monotonic_ns=4_000_000,
                succeeded=False,
            )

        self.assertEqual(state.sample_sequence, 2)
        self.assertEqual(state.received_at_utc, "sample-received")
        self.assertEqual(state.capture_completed_at_utc, "sample-capture")
        self.assertEqual(state.processing_duration_ms, 0.5)
        self.assertEqual(
            state.attempt_capture_completed_at_utc, "attempt-capture",
        )
        self.assertEqual(state.attempt_completed_at_utc, "attempt-done")
        self.assertEqual(state.attempt_duration_ms, 2.0)

        timing = snapshot_state("mistral", state, 6_000_000)
        self.assertEqual(
            timing.attempt_capture_completed_monotonic_ns, 4_000_000,
        )
        self.assertEqual(timing.attempt_completed_monotonic_ns, 5_000_000)


class OcrCaptureTimingTests(unittest.TestCase):
    def test_audited_bulbasaur_window_titles_match_driver_substrings(self):
        self.assertIn(MistralGui()._substring, "MistralGui")
        self.assertIn(
            EvapControl()._substring,
            "Evaporation control (Logged in: master)",
        )

    def test_mistral_capture_time_is_before_ocr_completion(self):
        driver = MistralGui()
        driver._connected = True
        driver._hwnd = 1
        frame = np.zeros((1039, 1793, 3), dtype=np.uint8)
        with (
            patch("drivers.mistral.capture_window", return_value=frame),
            patch("drivers.mistral.ocr_crop", return_value="Set 1 Actual 2"),
            patch("drivers.mistral.time.perf_counter_ns", return_value=123),
        ):
            driver.read()
        self.assertEqual(driver.last_capture_monotonic_ns, 123)
        self.assertIsNotNone(driver.last_capture_at_utc)

    def test_evap_capture_time_survives_ocr_parse_failure(self):
        driver = EvapControl()
        driver._connected = True
        driver._hwnd = 1
        frame = np.zeros((567, 1497, 3), dtype=np.uint8)
        with (
            patch("drivers.evap_control.capture_window", return_value=frame),
            patch("drivers.evap_control.ocr_crop", return_value="garbage"),
            patch("drivers.evap_control.time.perf_counter_ns", return_value=456),
        ):
            values = driver.read()
        self.assertIsNone(values["chamber_pressure_mbar"])
        self.assertEqual(driver.last_capture_monotonic_ns, 456)

    def test_failed_window_capture_clears_previous_ocr_capture_time(self):
        for module, driver, shape in (
            ("drivers.mistral", MistralGui(), (1039, 1793, 3)),
            ("drivers.evap_control", EvapControl(), (567, 1497, 3)),
        ):
            driver._connected = True
            driver._hwnd = 1
            with (
                patch(
                    f"{module}.capture_window",
                    return_value=np.zeros(shape, dtype=np.uint8),
                ),
                patch(f"{module}.ocr_crop", return_value=""),
                patch(f"{module}.time.perf_counter_ns", return_value=100),
            ):
                driver.read()
            self.assertEqual(driver.last_capture_monotonic_ns, 100)
            with patch(f"{module}.capture_window", side_effect=OSError("closed")):
                driver.read()
            self.assertIsNone(driver.last_capture_monotonic_ns)
            self.assertIsNone(driver.last_capture_at_utc)


class TraceAndDurabilityTests(unittest.TestCase):
    @staticmethod
    def _write_frame_session(root: Path, *, trace_sequence: int = 7) -> Path:
        session = root
        frames = session / "frames"
        frames.mkdir()
        frame = frames / "manual_event_001_120000.bmp"
        frame.write_bytes(b"durable-rheed-evidence")
        (session / "sensor_log.csv").write_text(
            "timestamp,rheed_valid\n", encoding="utf-8",
        )
        (session / "heartbeat_log.csv").write_text(
            "frame_path,capture_sequence,capture_backend,source_hwnd,"
            "captured_at_utc\n",
            encoding="utf-8",
        )
        (session / "manual_events.csv").write_text(
            "frame_path,capture_sequence,capture_backend,source_hwnd,"
            "captured_at_utc\n"
            f"frames/{frame.name},7,wgc,123,2026-08-03T12:00:00+00:00\n",
            encoding="utf-8",
        )
        trace = {
            "event": "frame_saved",
            "source": "manual_event",
            "details": {
                "frame_path": str(frame),
                "capture_sequence": trace_sequence,
            },
        }
        (session / "temporal_trace.jsonl").write_text(
            json.dumps(trace) + "\n", encoding="utf-8",
        )
        return frame

    def test_logger_writes_parseable_trace_and_sync_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("TEMPORAL")
            session = logger.session_dir
            logger.log_temporal_event(
                "state_received", "rheed",
                timing={"sequence": 5, "valid": True},
            )
            logger.log_sensors(
                500.0, 1.0,
                snapshot_at_utc="2026-08-02T12:00:00+00:00",
                snapshot_monotonic_ns=123,
                sync_span_ms=25.0,
                sync_complete=True,
                sync_valid=True,
                rheed_timing={
                    "sequence": 8,
                    "received_at_utc": "2026-08-02T12:00:00+00:00",
                    "valid": True,
                    "mode": "screengrab",
                },
            )
            logger.end_session()
            trace = [
                json.loads(line)
                for line in (session / "temporal_trace.jsonl").read_text().splitlines()
            ]
            self.assertEqual(trace[0]["event"], "session_start")
            self.assertEqual(trace[-1]["event"], "session_end")
            with (session / "sensor_log.csv").open(newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["sync_span_ms"], "25.000")
            self.assertEqual(row["rheed_sample_sequence"], "8")
            self.assertEqual(row["rheed_valid"], "True")

    def test_trace_nearest_neighbor_and_fault_latency(self):
        records = [
            {
                "event": "state_received", "source": source,
                "recorded_monotonic_ns": recorded,
                "timing": {
                    "received_monotonic_ns": received,
                    "valid": valid,
                    "error": error,
                },
            }
            for source, recorded, received, valid, error in (
                ("rheed", 1_000, 1_000, True, ""),
                ("mistral", 1_100, 1_100, True, ""),
                ("mistral", 2_500, 1_100, False, "closed"),
                ("mistral", 4_000, 4_000, True, ""),
            )
        ]
        records.extend([
            {
                "event": "manual_event_saved", "source": "manual_event",
                "details": {
                    "write_duration_ms": 12.5,
                    "source_age_at_save_ms": 40.0,
                },
            },
            {
                "event": "equalizer_label_saved", "source": "equalizer",
                "details": {
                    "write_duration_ms": 20.0,
                    "source_age_at_save_ms": 55.0,
                },
            },
        ])
        trace = summarize_trace(records, 0)
        self.assertEqual(
            trace["nearest_neighbor_to_rheed"]["mistral"]
            ["absolute_delta_ms"]["p50"],
            0.0001,
        )
        self.assertEqual(trace["save_operations"]["manual_event"]["count"], 1)
        self.assertEqual(
            trace["save_operations"]["equalizer"]
            ["source_age_at_save_ms"]["p50"],
            55.0,
        )
        actions = [
            {"label": "close", "source": "mistral", "phase": "before",
             "recorded_monotonic_ns": 2_000},
            {"label": "reopen", "source": "mistral", "phase": "after",
             "recorded_monotonic_ns": 3_000},
        ]
        result = summarize_operator_actions(records, actions, 0)
        self.assertEqual(result["transitions"][0]["transition"]["latency_ms"], 0.0005)
        self.assertEqual(result["transitions"][1]["transition"]["latency_ms"], 0.001)

    def test_validator_rejects_partial_jsonl_after_forced_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "frames").mkdir()
            (session / "sensor_log.csv").write_text(
                "timestamp,rheed_valid\n", encoding="utf-8",
            )
            (session / "heartbeat_log.csv").write_text(
                "frame_path,capture_sequence,capture_backend,source_hwnd,captured_at_utc\n",
                encoding="utf-8",
            )
            (session / "temporal_trace.jsonl").write_text(
                '{"event":"session_start"}\n{"event":', encoding="utf-8",
            )
            result = validate(session)
        self.assertFalse(result["passed"])
        self.assertTrue(any("temporal_trace" in error for error in result["errors"]))

    def test_validator_accepts_nonheartbeat_frame_with_unique_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_frame_session(Path(tmp))
            result = validate(Path(tmp))
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["stored_images"], 1)
        self.assertEqual(result["traced_frames"], 1)

    def test_validator_rejects_orphan_nonheartbeat_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            self._write_frame_session(session)
            (session / "frames" / "entry_002_120001.bmp").write_bytes(b"orphan")
            result = validate(session)
        self.assertFalse(result["passed"])
        self.assertTrue(any("orphan RHEED image" in error for error in result["errors"]))

    def test_validator_rejects_trace_sequence_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            self._write_frame_session(session, trace_sequence=8)
            result = validate(session)
        self.assertFalse(result["passed"])
        self.assertTrue(any("does not match CSV" in error for error in result["errors"]))

    def test_validator_rejects_missing_referenced_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            frame = self._write_frame_session(session)
            frame.unlink()
            result = validate(session)
        self.assertFalse(result["passed"])
        self.assertTrue(any("missing frame_path" in error for error in result["errors"]))

    def test_validator_accepts_hashed_equalizer_orientation_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            self._write_frame_session(session)
            evidence = session / "frames" / "equalizer_orientation.png"
            evidence.write_bytes(b"immutable-calibration-frame")
            digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
            event = {
                "event": "accepted",
                "orientation_evidence_path": "frames/equalizer_orientation.png",
                "calibration": {
                    "capture_sequence": 9,
                    "capture_backend": "wgc",
                    "captured_at_utc": "2026-08-03T12:00:01+00:00",
                    "source_hwnd": 123,
                    "orientation_evidence_sha256": digest,
                },
            }
            (session / "equalizer_calibrations.jsonl").write_text(
                json.dumps(event) + "\n", encoding="utf-8",
            )
            result = validate(session)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["stored_images"], 2)

    def test_validator_rejects_equalizer_evidence_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            self._write_frame_session(session)
            evidence = session / "frames" / "equalizer_orientation.png"
            evidence.write_bytes(b"immutable-calibration-frame")
            event = {
                "event": "accepted",
                "orientation_evidence_path": "frames/equalizer_orientation.png",
                "calibration": {
                    "capture_sequence": 9,
                    "capture_backend": "wgc",
                    "captured_at_utc": "2026-08-03T12:00:01+00:00",
                    "source_hwnd": 123,
                    "orientation_evidence_sha256": "0" * 64,
                },
            }
            (session / "equalizer_calibrations.jsonl").write_text(
                json.dumps(event) + "\n", encoding="utf-8",
            )
            result = validate(session)
        self.assertFalse(result["passed"])
        self.assertTrue(any("SHA-256 mismatch" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
