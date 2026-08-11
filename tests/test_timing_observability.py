"""Non-hardware tests for O-MBE timing provenance and reporting."""
from __future__ import annotations

import csv
import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from drivers.evap_control import ElogReader  # noqa: E402
from gui.growth_app import _sample_timing_snapshot  # noqa: E402
from gui.growth_logger import GrowthLogger  # noqa: E402
from gui.state import (  # noqa: E402
    EvapControlState,
    MistralState,
    PyrometerState,
)
from gui.workers import _mark_sample_received  # noqa: E402
from scripts.ombe_timing_probe import (  # noqa: E402
    render_markdown,
    summarize_instrument,
)


class StateTimingTests(unittest.TestCase):
    def test_all_observed_states_start_without_a_sample(self):
        for state_type in (PyrometerState, MistralState, EvapControlState):
            state = state_type()
            self.assertIsNone(state.source_at_utc)
            self.assertIsNone(state.received_at_utc)
            self.assertEqual(state.sample_sequence, 0)
            self.assertIsNone(state.read_duration_ms)
            self.assertIsNone(state.received_monotonic_ns)

    def test_mark_sample_received_increments_and_measures_duration(self):
        state = PyrometerState()
        with (
            patch("gui.workers.time.perf_counter_ns", return_value=2_500_000),
            patch(
                "gui.workers._utc_iso_now",
                return_value="2026-07-27T12:00:00.000+00:00",
            ),
        ):
            _mark_sample_received(
                state,
                read_started_ns=1_000_000,
                source_at_utc="2026-07-27T11:59:59+00:00",
            )
        self.assertEqual(state.sample_sequence, 1)
        self.assertEqual(state.read_duration_ms, 1.5)
        self.assertEqual(state.received_monotonic_ns, 2_500_000)
        self.assertEqual(
            state.received_at_utc, "2026-07-27T12:00:00.000+00:00",
        )
        self.assertEqual(
            state.source_at_utc, "2026-07-27T11:59:59+00:00",
        )

    def test_snapshot_uses_monotonic_age(self):
        state = MistralState(
            source_at_utc=None,
            received_at_utc="2026-07-27T12:00:00.000+00:00",
            sample_sequence=7,
            read_duration_ms=12.5,
            received_monotonic_ns=2_000_000,
        )
        snapshot = _sample_timing_snapshot(
            state, logged_monotonic_ns=5_500_000,
        )
        self.assertEqual(snapshot["sequence"], 7)
        self.assertEqual(snapshot["age_ms"], 3.5)
        self.assertEqual(snapshot["read_duration_ms"], 12.5)


class ElogTimestampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "live.elo"
        self.path.touch()
        self.source_ts = dt.datetime(
            2026, 7, 27, 12, 34, 56, 789000, tzinfo=dt.timezone.utc,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_reader_dependencies(self):
        return (
            patch("drivers.elog.find_current_log", return_value=self.path),
            patch(
                "drivers.elog.parse_schema",
                return_value=(["MBE.Pressure"], ["%.2e mbar"], 0),
            ),
            patch(
                "drivers.elog.latest_record",
                return_value=(
                    self.source_ts,
                    {"MBE.Pressure": (2.5e-9, "%.2e mbar")},
                ),
            ),
        )

    def test_reader_preserves_latest_record_timestamp(self):
        find_patch, schema_patch, latest_patch = self._patch_reader_dependencies()
        with find_patch, schema_patch, latest_patch:
            reader = ElogReader(
                log_dir=self.tmp.name,
                var_map={"MBE.Pressure": "chamber_pressure_mbar"},
            )
            reader.connect()
            values = reader.read()
        self.assertAlmostEqual(values["chamber_pressure_mbar"], 2.5e-9)
        self.assertEqual(
            reader.last_source_at_utc,
            "2026-07-27T12:34:56.789000+00:00",
        )

    def test_failed_tail_read_does_not_reuse_source_timestamp(self):
        find_patch, schema_patch, latest_patch = self._patch_reader_dependencies()
        with find_patch, schema_patch, latest_patch:
            reader = ElogReader(
                log_dir=self.tmp.name,
                var_map={"MBE.Pressure": "chamber_pressure_mbar"},
            )
            reader.connect()
            reader.read()
        self.assertIsNotNone(reader.last_source_at_utc)

        with (
            patch("drivers.elog.find_current_log", return_value=self.path),
            patch("drivers.elog.latest_record", side_effect=OSError("busy")),
        ):
            values = reader.read()
        self.assertIsNone(values["chamber_pressure_mbar"])
        self.assertIsNone(reader.last_source_at_utc)


class SensorLogSchemaTests(unittest.TestCase):
    def test_timing_columns_are_appended_and_optional(self):
        first_new = GrowthLogger.SENSOR_FIELDS.index(
            "pyrometer_source_at_utc",
        )
        self.assertEqual(
            GrowthLogger.SENSOR_FIELDS[first_new - 1], "cell7_power_W",
        )

        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("TIMING")
            session_dir = logger.session_dir
            logger.log_sensors(450.0, 0.0)
            logger.log_sensors(
                451.0,
                1.0,
                pyrometer_received_at_utc=(
                    "2026-07-27T12:00:00.000+00:00"
                ),
                pyrometer_sample_sequence=4,
                pyrometer_age_ms=123.4567,
                pyrometer_read_duration_ms=8.7654,
                evap_source_at_utc="2026-07-27T11:59:59+00:00",
            )
            logger.end_session()

            with (session_dir / "sensor_log.csv").open(newline="") as stream:
                rows = list(csv.DictReader(stream))

        self.assertEqual(rows[0]["pyrometer_received_at_utc"], "")
        self.assertEqual(rows[0]["pyrometer_sample_sequence"], "")
        self.assertEqual(
            rows[1]["pyrometer_received_at_utc"],
            "2026-07-27T12:00:00.000+00:00",
        )
        self.assertEqual(rows[1]["pyrometer_sample_sequence"], "4")
        self.assertEqual(rows[1]["pyrometer_age_ms"], "123.457")
        self.assertEqual(rows[1]["pyrometer_read_duration_ms"], "8.765")
        self.assertEqual(
            rows[1]["evap_source_at_utc"],
            "2026-07-27T11:59:59+00:00",
        )


class ProbeSummaryTests(unittest.TestCase):
    def test_summary_tracks_gaps_reuse_missingness_and_elog_offset(self):
        rows = [
            {
                "evap_sample_sequence": "1",
                "evap_received_at_utc": "2026-07-27T12:00:00+00:00",
                "evap_source_at_utc": "2026-07-27T11:59:59.500+00:00",
                "evap_age_ms": "100",
                "evap_read_duration_ms": "10",
                "chamber_pressure_mbar": "2e-9",
            },
            {
                "evap_sample_sequence": "1",
                "evap_received_at_utc": "2026-07-27T12:00:00+00:00",
                "evap_source_at_utc": "2026-07-27T11:59:59.500+00:00",
                "evap_age_ms": "1100",
                "evap_read_duration_ms": "10",
                "chamber_pressure_mbar": "2e-9",
            },
            {
                "evap_sample_sequence": "3",
                "evap_received_at_utc": "2026-07-27T12:00:02+00:00",
                "evap_source_at_utc": "2026-07-27T12:00:01.250+00:00",
                "evap_age_ms": "100",
                "evap_read_duration_ms": "14",
                "chamber_pressure_mbar": "3e-9",
            },
            {
                "evap_sample_sequence": "",
                "evap_received_at_utc": "",
                "evap_source_at_utc": "",
                "evap_age_ms": "",
                "evap_read_duration_ms": "",
                "chamber_pressure_mbar": "",
            },
        ]
        result = summarize_instrument(
            rows, "evap", ["chamber_pressure_mbar"],
        )
        self.assertEqual(result["unique_observed_samples"], 2)
        self.assertEqual(result["reused_sequence_rows"], 1)
        self.assertEqual(result["unobserved_intermediate_samples"], 1)
        self.assertEqual(result["missing_provenance_rows"], 1)
        self.assertEqual(result["value_missing_rows"], 1)
        self.assertEqual(
            result["sequence_normalized_interval_s"]["p50"], 1.0,
        )
        self.assertEqual(result["read_duration_ms"]["p50"], 12.0)
        self.assertEqual(
            result["source_to_receive_offset_ms"]["p50"], 625.0,
        )
        self.assertEqual(result["source_update_interval_s"]["p50"], 1.75)
        # The repeated second row is the same logger sample (same sequence),
        # so it is not a second source read.
        self.assertEqual(result["reused_source_timestamp_rows"], 0)

    def test_distinct_reads_with_same_elog_timestamp_are_counted(self):
        rows = [
            {
                "evap_sample_sequence": str(sequence),
                "evap_received_at_utc": (
                    f"2026-07-27T12:00:0{sequence}+00:00"
                ),
                "evap_source_at_utc": "2026-07-27T12:00:00+00:00",
                "chamber_pressure_mbar": "2e-9",
            }
            for sequence in (1, 2)
        ]
        result = summarize_instrument(
            rows, "evap", ["chamber_pressure_mbar"],
        )
        self.assertEqual(result["reused_source_timestamp_rows"], 1)

    def test_stale_valid_row_uses_measured_interval_audit_rule(self):
        rows = [
            {
                "mistral_sample_sequence": str(index),
                "mistral_received_at_utc": (
                    f"2026-07-27T12:00:0{index}+00:00"
                ),
                "mistral_age_ms": "3500" if index == 3 else "100",
                "mistral_valid": "True",
                "mistral_v_actual_V": "1.0",
            }
            for index in (1, 2, 3)
        ]
        result = summarize_instrument(
            rows, "mistral", ["mistral_v_actual_V"],
        )
        self.assertEqual(result["stale_audit_threshold_ms"], 3000.0)
        self.assertEqual(result["stale_valid_rows"], 1)

    def test_report_states_that_no_threshold_was_applied(self):
        empty_stats = {
            "p50": None,
            "p95": None,
            "p99": None,
        }
        instrument = {
            "unique_observed_samples": 0,
            "missing_provenance_rows": 0,
            "missing_provenance_percent": None,
            "value_missing_rows": 0,
            "value_missing_percent": None,
            "reused_sequence_rows": 0,
            "unobserved_intermediate_samples": 0,
            "sequence_normalized_interval_s": empty_stats,
            "read_duration_ms": empty_stats,
            "logged_age_ms": empty_stats,
            "source_to_receive_offset_ms": empty_stats,
            "source_update_interval_s": empty_stats,
            "reused_source_timestamp_rows": 0,
            "stale_audit_threshold_ms": 3000.0,
            "stale_valid_rows": 0,
        }
        summary = {
            "source": {
                "sensor_log": "sensor_log.csv",
                "sha256": "abc",
                "analyzed_row_count": 0,
                "timing_columns_present": True,
                "missing_timing_columns": [],
            },
            "capture": {
                "started_at_utc": "start",
                "ended_at_utc": "end",
                "requested_duration_s": 3600,
            },
            "metadata": {},
            "software": {"git_commit": "commit"},
            "instruments": {"evap": instrument},
            "cross_source": {
                "sync_span_ms": empty_stats,
                "sync_valid_rows": 0,
                "row_count": 0,
            },
            "temporal_trace": {
                "parse_errors": 0,
                "classifier_capture_to_complete_ms": empty_stats,
                "csv_flush_ms": empty_stats,
                "event_loop_turn_ms": empty_stats,
            },
            "operator_actions": {"action_count": 0},
        }
        report = render_markdown(summary)
        self.assertIn("No synchronization or pass/fail threshold", report)
        self.assertIn("N/A", report)


if __name__ == "__main__":
    unittest.main()
