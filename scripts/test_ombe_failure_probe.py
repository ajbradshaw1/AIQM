"""Platform-independent tests for the O-MBE failure recorder analysis."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.ombe_failure_probe as probe_module
from scripts.ombe_failure_probe import (
    ObservationTracker,
    _SerialPortMutex,
    annotate_observation,
    data_age,
    driver_identity,
    finalize_interrupted_run,
    frame_fingerprint,
    mark_event,
    marker_pairing_errors,
    measurement_signature,
    measurement_status,
    passive_source_probe,
    passive_network_interface_probe,
    prepare_output_dir,
    run_probe,
    state_payload,
    summarize,
    validate_fault_plan,
    validate_source_mode,
)


@dataclass
class _State:
    temperature: float = 500.0
    temperature_std: float = 1.0
    temperature_n: int = 5
    emissivity: float = 0.9
    connected: bool = True
    error: str = ""


def _pyrometer_row(index: int, t: float, *, error: str = ""):
    return {
        "emission_index": index,
        "observed_at_utc": f"2026-07-27T00:00:{int(t):02d}.000Z",
        "observed_monotonic_s": t,
        "observed_monotonic_ns": round(t * 1_000_000_000),
        "elapsed_s": t,
        "state": {
            "temperature": 500.0,
            "temperature_std": 1.0,
            "temperature_n": 5,
            "emissivity": 0.9,
            "connected": True,
            "error": error,
            "sample_sequence": index + 1,
            "received_at_utc": f"2026-07-27T00:00:{int(t):02d}.000Z",
        },
    }


def _evap_row(index: int, t: float):
    return {
        "emission_index": index,
        "observed_at_utc": f"2026-07-27T00:00:{int(t):02d}.000Z",
        "observed_monotonic_s": t,
        "observed_monotonic_ns": round(t * 1_000_000_000),
        "elapsed_s": t,
        "state": {
            "chamber_pressure_mbar": 1e-10,
            "substrate_temp_pv_C": 500.0,
            "substrate_temp_setpoint_C": 500.0,
            "cell_HTEC2_pv_C": 300.0,
            "cell_Y_pv_C": 300.0,
            "cell_Sr_pv_C": 300.0,
            "cell_Eu_pv_C": 300.0,
            "cell_Er_pv_C": 300.0,
            "plasma_dc_bias_V": 0.0,
            "plasma_forward_W": 0.0,
            "plasma_reflected_W": 0.0,
            "connected": True,
            "error": "",
        },
    }


class SerializationTests(unittest.TestCase):
    def test_dataclass_serialization(self):
        payload = state_payload(_State())
        self.assertEqual(payload["temperature"], 500.0)
        self.assertTrue(payload["connected"])

    def test_signature_uses_all_source_measurements(self):
        payload = state_payload(_State())
        self.assertEqual(
            measurement_signature("pyrometer", payload),
            (500.0, 1.0, 5, 0.9),
        )

    def test_camera_signature_prefers_capture_sequence(self):
        first = {
            "frame_number": 100,
            "capture_sequence": 42,
        }
        second = {
            "frame_number": 101,
            "capture_sequence": 42,
        }
        self.assertEqual(
            measurement_signature("camera", first),
            measurement_signature("camera", second),
        )

    def test_capture_timestamp_produces_age(self):
        age_ms, source = data_age(
            {"captured_at_utc": "2026-07-27T00:00:00.000Z"},
            "2026-07-27T00:00:00.500Z",
            0,
        )
        self.assertEqual(age_ms, 500.0)
        self.assertEqual(source, "captured_at_utc")

    def test_monotonic_receipt_timestamp_produces_age(self):
        age_ms, source = data_age(
            {"received_monotonic_ns": 1_000_000_000},
            "",
            1_500_000_000,
        )
        self.assertEqual(age_ms, 500.0)
        self.assertEqual(source, "received_monotonic_ns")

    def test_frame_fingerprint_detects_pixel_change(self):
        first = SimpleNamespace(frame=np.zeros((64, 64, 3), dtype=np.uint8))
        changed_frame = np.zeros((64, 64, 3), dtype=np.uint8)
        changed_frame[0, 0] = 255
        second = SimpleNamespace(frame=changed_frame)
        self.assertNotEqual(
            frame_fingerprint(first)["sha256"],
            frame_fingerprint(second)["sha256"],
        )


class ModeValidationTests(unittest.TestCase):
    def test_valid_source_mode_is_accepted(self):
        validate_source_mode("evap", "elog")
        validate_source_mode("camera", "screengrab")

    def test_typo_and_dummy_modes_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_source_mode("mistral", "screenrab")
        with self.assertRaises(ValueError):
            validate_source_mode("camera", "dummy")

    def test_driver_identity_reports_actual_class_and_backend(self):
        class ScreenGrabCamera:
            _backend = "wgc"
            _capture_session = None

        class Worker:
            _camera = ScreenGrabCamera()

        identity = driver_identity(Worker(), "camera")
        self.assertEqual(identity["driver_class"], "ScreenGrabCamera")
        self.assertEqual(identity["driver_backend"], "wgc")


class FaultPlanTests(unittest.TestCase):
    @staticmethod
    def _args(**overrides):
        values = {
            "source": "pyrometer",
            "mode": "exactus",
            "port": "COM4",
            "baudrate": 115200,
            "device_id": 1,
            "fault_class": "transport-loss",
            "scenario_id": "serial-link-1",
            "target": "read-only pyrometer adapter",
            "boundary": "dedicated COM4 USB/RS-422 adapter",
            "operator": "operator",
            "observer": "observer",
            "approver": "instrument owner",
            "authorization_ref": "maintenance-ticket-1",
            "sop_ref": "SOP-EXACTUS-RECOVERY",
            "safe_state_note": "idle; no growth; output not used for control",
            "abort_criteria": "any interlock or unexpected output",
            "confirm_safe_state": True,
            "link_kind": "serial",
            "independent_observer_ref": "",
            "network_interface": None,
            "network_interface_index": None,
            "network_probe_interval": 2.0,
        }
        values.update(overrides)
        return Namespace(**values)

    def test_physical_fault_requires_full_safety_metadata(self):
        with self.assertRaisesRegex(ValueError, "approver"):
            validate_fault_plan(self._args(approver=""))
        with self.assertRaisesRegex(ValueError, "distinct operator"):
            validate_fault_plan(self._args(observer="operator"))

    def test_complete_transport_plan_is_accepted(self):
        spec = validate_fault_plan(self._args())
        self.assertEqual(spec["safety_tier"], "S2")
        self.assertTrue(spec["physical"])

    def test_live_run_rejects_synthetic_label_and_unbound_network(self):
        with self.assertRaisesRegex(ValueError, "synthetic unit-test"):
            validate_fault_plan(
                self._args(
                    fault_class="offline-injection",
                    link_kind="none",
                )
            )
        with self.assertRaisesRegex(ValueError, "not yet source-bound"):
            validate_fault_plan(
                self._args(
                    link_kind="network",
                    network_interface_index=7,
                )
            )

    def test_transport_plan_rejects_unobservable_boundaries(self):
        with self.assertRaisesRegex(ValueError, "supports only observable"):
            validate_fault_plan(self._args(link_kind="file"))
        with self.assertRaisesRegex(ValueError, "currently supported only"):
            validate_fault_plan(
                self._args(
                    source="camera",
                    mode="screengrab",
                    link_kind="usb",
                )
            )

    def test_instrument_power_loss_is_bound_to_direct_pyrometer(self):
        spec = validate_fault_plan(
            self._args(
                fault_class="instrument-power-loss",
                independent_observer_ref="observer-video-1",
            )
        )
        self.assertEqual(spec["safety_tier"], "S3")
        with self.assertRaisesRegex(ValueError, "currently supported only"):
            validate_fault_plan(
                self._args(
                    fault_class="instrument-power-loss",
                    source="camera",
                    mode="screengrab",
                    link_kind="video",
                    independent_observer_ref="observer-video-1",
                )
            )

    def test_file_feed_stop_requires_evap_elog(self):
        with self.assertRaisesRegex(ValueError, "evap --mode elog"):
            validate_fault_plan(
                self._args(
                    fault_class="file-feed-stop",
                    link_kind="none",
                    source="evap",
                    mode="ocr",
                )
            )

    def test_window_source_loss_requires_window_capture_mode(self):
        with self.assertRaisesRegex(ValueError, "screengrab/UIA"):
            validate_fault_plan(
                self._args(
                    fault_class="window-source-loss",
                    link_kind="none",
                    source="pyrometer",
                    mode="exactus",
                )
            )

    def test_s1_manual_source_loss_requires_safety_metadata(self):
        with self.assertRaisesRegex(ValueError, "operator"):
            validate_fault_plan(
                self._args(
                    fault_class="window-source-loss",
                    source="mistral",
                    mode="screengrab",
                    operator="",
                    link_kind="video",
                )
            )

    def test_nondefault_modbus_is_rejected_before_worker_creation(self):
        with self.assertRaisesRegex(ValueError, "does not forward non-default"):
            validate_fault_plan(
                self._args(
                    source="pyrometer",
                    mode="modbus",
                    port="COM9",
                )
            )

    def test_workstation_observation_requires_independent_evidence(self):
        with self.assertRaisesRegex(ValueError, "independent-observer"):
            validate_fault_plan(
                self._args(
                    fault_class="workstation-power-observation",
                    link_kind="none",
                )
            )

    @unittest.skipUnless(
        sys.platform == "win32", "named serial mutex is Windows-only"
    )
    def test_same_serial_port_cannot_be_probed_twice(self):
        port = "COM-CODEX-FAILURE-MUTEX-TEST"
        with _SerialPortMutex(port):
            with self.assertRaisesRegex(RuntimeError, "diagnostic lock"):
                with _SerialPortMutex(port):
                    self.fail("second serial probe must fail closed")


class ValidityTests(unittest.TestCase):
    def test_connected_all_none_ocr_state_is_invalid(self):
        payload = {
            "v_set": None,
            "v_actual": None,
            "i_set": None,
            "i_actual": None,
            "connected": True,
            "error": "",
        }
        valid, reason, all_none = measurement_status("mistral", payload)
        self.assertFalse(valid)
        self.assertTrue(all_none)
        self.assertEqual(reason, "all_measurements_none")

    def test_annotation_records_value_change_and_unchanged_age(self):
        tracker = ObservationTracker()
        first = annotate_observation(_pyrometer_row(0, 0.0), "pyrometer", tracker)
        second = annotate_observation(_pyrometer_row(1, 4.0), "pyrometer", tracker)
        self.assertIsNone(first["value_changed"])
        self.assertFalse(second["value_changed"])
        self.assertEqual(second["unchanged_for_s"], 4.0)
        self.assertTrue(second["measurement_valid"])


class SummaryTests(unittest.TestCase):
    def test_stable_value_without_failure_marker_is_not_stale(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(8)]
        summary = summarize(rows, "pyrometer", baseline_duration_s=4.0)
        self.assertEqual(summary["stale_candidate_count"], 0)
        self.assertEqual(summary["stale_threshold_s"], 3.0)
        self.assertEqual(
            summary["baseline_interval_s"]["source"],
            "healthy_pre_failure_window",
        )

    def test_marker_aligned_retained_valid_value_is_flagged(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(7)]
        markers = [{
            "kind": "failure-start",
            "label": "stop-update",
            "marked_monotonic_s": 2.1,
            "marked_at_utc": "2026-07-27T00:00:02.100Z",
        }]
        summary = summarize(
            rows,
            "pyrometer",
            markers=markers,
            baseline_duration_s=10.0,
        )
        self.assertEqual(summary["baseline_interval_s"]["p95"], 1.0)
        self.assertEqual(summary["stale_candidate_emission_indices"], [6])
        self.assertEqual(summary["failure_marker_count"], 1)
        failures = probe_module.summary_validation_failure_reasons(summary)
        self.assertTrue(any("stale valid data" in item for item in failures))

    def test_failure_intervals_do_not_inflate_baseline_p95(self):
        rows = [
            _pyrometer_row(0, 0.0),
            _pyrometer_row(1, 1.0),
            _pyrometer_row(2, 10.0),
            _pyrometer_row(3, 20.0),
        ]
        markers = [{
            "kind": "failure-start",
            "label": "close-window",
            "marked_monotonic_s": 1.5,
        }]
        summary = summarize(
            rows,
            "pyrometer",
            markers=markers,
            baseline_duration_s=30.0,
        )
        self.assertEqual(summary["baseline_interval_s"]["samples"], 1)
        self.assertEqual(summary["baseline_interval_s"]["p95"], 1.0)
        self.assertEqual(summary["stale_threshold_s"], 3.0)

    def test_supplied_baseline_p95_controls_threshold(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(3)]
        summary = summarize(rows, "pyrometer", baseline_p95_s=4.0)
        self.assertEqual(summary["baseline_interval_s"]["source"], "supplied")
        self.assertEqual(summary["stale_threshold_s"], 8.0)

    def test_short_baseline_is_not_healthy_for_long_requested_window(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(3)]
        markers = [{
            "kind": "failure-start",
            "episode_id": "too-early",
            "marked_monotonic_s": 2.1,
        }]
        summary = summarize(
            rows,
            "pyrometer",
            markers=markers,
            baseline_duration_s=30.0,
        )
        self.assertFalse(summary["baseline_health"]["healthy"])
        self.assertLess(
            summary["baseline_health"]["observed_span_s"],
            summary["baseline_health"]["required_span_s"],
        )

    def test_all_none_failure_has_detection_latency_without_worker_error(self):
        rows = [{
            "emission_index": 0,
            "observed_at_utc": "2026-07-27T00:00:00.000Z",
            "observed_monotonic_s": 0.0,
            "elapsed_s": 0.0,
            "state": {
                "v_set": 1.0,
                "v_actual": 1.0,
                "i_set": 1.0,
                "i_actual": 1.0,
                "connected": True,
                "error": "",
            },
        }, {
            "emission_index": 1,
            "observed_at_utc": "2026-07-27T00:00:01.000Z",
            "observed_monotonic_s": 1.0,
            "elapsed_s": 1.0,
            "state": {
                "v_set": None,
                "v_actual": None,
                "i_set": None,
                "i_actual": None,
                "connected": True,
                "error": "",
            },
        }]
        markers = [{
            "kind": "failure-start",
            "label": "cover",
            "marked_monotonic_s": 0.5,
        }]
        summary = summarize(rows, "mistral", markers=markers)
        self.assertEqual(summary["connected_but_invalid_emissions"], 1)
        self.assertEqual(summary["all_measurements_none_emissions"], 1)
        self.assertEqual(
            summary["failure_episodes"][0]["invalid_detection_latency_s"],
            0.5,
        )
        self.assertEqual(
            summary["failure_episodes"][0]["assessment"],
            "not_evaluable_no_healthy_baseline",
        )

    def test_watchdog_detects_worker_emission_timeout(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(4)]
        markers = [{
            "kind": "failure-start",
            "episode_id": "blocked-read",
            "marked_monotonic_s": 3.1,
            "marked_at_utc": "2026-07-27T00:00:03.100Z",
        }]
        watchdog = [
            {
                "watchdog_index": index,
                "observed_monotonic_s": t,
                "observed_at_utc": (
                    f"2026-07-27T00:00:{int(t):02d}.000Z"
                ),
                "elapsed_s": t,
                "last_emission_age_s": 0.2,
                "emission_timeout": False,
                "network_interface": None,
                "serial_link": None,
            }
            for index, t in enumerate((0.0, 1.0, 2.0, 3.0))
        ]
        watchdog.append({
            "watchdog_index": 4,
            "observed_monotonic_s": 6.2,
            "observed_at_utc": "2026-07-27T00:00:06.200Z",
            "elapsed_s": 6.2,
            "last_emission_age_s": 3.2,
            "emission_timeout": True,
            "network_interface": None,
            "serial_link": None,
        })
        summary = summarize(
            rows,
            "pyrometer",
            markers=markers,
            watchdog_rows=watchdog,
            baseline_duration_s=3.0,
        )
        episode = summary["failure_episodes"][0]
        self.assertEqual(episode["assessment"], "not_detected")
        self.assertIsNone(episode["first_detection_channel"])
        self.assertEqual(
            episode["first_external_fault_evidence_channel"],
            "emission_timeout",
        )
        self.assertAlmostEqual(
            episode["emission_timeout_detection_latency_s"], 3.1, places=6
        )

    def test_recovery_requires_three_advancing_fresh_samples(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(12)]
        for index in range(4, 8):
            rows[index]["state"]["error"] = "link lost"
        markers = [{
            "kind": "failure-start",
            "episode_id": "serial-loss-1",
            "fault_class": "transport-loss",
            "safety_tier": "S2",
            "marked_monotonic_s": 3.1,
            "marked_at_utc": "2026-07-27T00:00:03.100Z",
            "boot_id": "boot-a",
        }, {
            "kind": "recovery",
            "episode_id": "serial-loss-1",
            "marked_monotonic_s": 7.1,
            "marked_at_utc": "2026-07-27T00:00:07.100Z",
            "boot_id": "boot-a",
        }]
        summary = summarize(
            rows,
            "pyrometer",
            mode="exactus",
            markers=markers,
            baseline_duration_s=3.0,
            required_fresh_samples=3,
        )
        episode = summary["failure_episodes"][0]
        self.assertTrue(episode["baseline_healthy"])
        self.assertTrue(episode["recovery_confirmed"])
        self.assertAlmostEqual(
            episode["first_valid_after_restore_latency_s"], 0.9, places=6
        )
        self.assertAlmostEqual(
            episode["fresh_recovery_latency_s"], 2.9, places=6
        )

    def test_changing_valid_values_then_late_error_is_a_failure(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(12)]
        for index in range(4, 8):
            rows[index]["state"]["temperature"] = 500.0 + index
        rows[8]["state"]["error"] = "late link-loss detection"
        markers = [{
            "kind": "failure-start",
            "episode_id": "late-detection",
            "fault_class": "transport-loss",
            "marked_monotonic_s": 3.1,
        }, {
            "kind": "recovery",
            "episode_id": "late-detection",
            "marked_monotonic_s": 8.1,
        }]
        summary = summarize(
            rows,
            "pyrometer",
            mode="exactus",
            markers=markers,
            baseline_duration_s=3.0,
            required_fresh_samples=3,
        )
        episode = summary["failure_episodes"][0]
        self.assertEqual(episode["assessment"], "late_detection")
        self.assertTrue(episode["late_production_detection"])
        self.assertIsNone(episode["first_detection_channel"])
        self.assertAlmostEqual(
            episode["invalid_detection_latency_s"],
            4.9,
            places=6,
        )
        self.assertEqual(
            episode["valid_after_deadline_emission_indices"],
            [7],
        )
        self.assertEqual(episode["stale_valid_emission_indices"], [])
        failures = probe_module.summary_validation_failure_reasons(summary)
        self.assertTrue(any("exceeded" in reason for reason in failures))
        self.assertTrue(any("remained valid" in reason for reason in failures))

    def test_elog_source_freeze_is_separate_detection_channel(self):
        rows = [_evap_row(i, float(i)) for i in range(4)]
        markers = [{
            "kind": "failure-start",
            "episode_id": "elog-freeze",
            "marked_monotonic_s": 3.1,
            "marked_at_utc": "2026-07-27T00:00:03.100Z",
        }]
        watchdog = [
            {
                "watchdog_index": index,
                "observed_monotonic_s": t,
                "observed_at_utc": (
                    f"2026-07-27T00:00:{int(t):02d}.000Z"
                ),
                "elapsed_s": t,
                "last_emission_age_s": 0.2,
                "emission_timeout": False,
                "source_unchanged_for_s": 0.0,
                "source_probe": {
                    "elog_source_at_utc": (
                        f"2026-07-27T00:00:{int(t):02d}.000Z"
                    )
                },
            }
            for index, t in enumerate((0.0, 1.0, 2.0, 3.0))
        ]
        watchdog.append({
            "watchdog_index": 4,
            "observed_monotonic_s": 7.0,
            "observed_at_utc": "2026-07-27T00:00:07.000Z",
            "elapsed_s": 7.0,
            "last_emission_age_s": 0.2,
            "emission_timeout": False,
            "source_unchanged_for_s": 4.0,
            "source_probe": {
                "elog_source_at_utc": "2026-07-27T00:00:03.000Z"
            },
        })
        summary = summarize(
            rows,
            "evap",
            mode="elog",
            markers=markers,
            watchdog_rows=watchdog,
            baseline_duration_s=3.0,
        )
        episode = summary["failure_episodes"][0]
        self.assertEqual(episode["assessment"], "not_detected")
        self.assertIsNone(episode["first_detection_channel"])
        self.assertEqual(
            episode["first_external_fault_evidence_channel"],
            "source_feed_stale",
        )

    def test_receipt_timestamps_do_not_confirm_ocr_hardware_recovery(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(9)]
        rows[4]["state"]["error"] = "display unavailable"
        rows[5]["state"]["error"] = "display unavailable"
        markers = [{
            "kind": "failure-start",
            "episode_id": "transport-via-display",
            "fault_class": "transport-loss",
            "marked_monotonic_s": 3.1,
        }, {
            "kind": "recovery",
            "episode_id": "transport-via-display",
            "marked_monotonic_s": 5.1,
        }]
        summary = summarize(
            rows,
            "pyrometer",
            mode="screengrab",
            markers=markers,
            baseline_duration_s=3.0,
        )
        episode = summary["failure_episodes"][0]
        self.assertFalse(episode["recovery_confirmed"])
        self.assertFalse(episode["fault_window_sufficient"])
        self.assertEqual(
            episode["assessment"],
            "not_evaluable_short_fault_window",
        )
        self.assertEqual(
            episode["recovery_evidence_kind"],
            "unavailable_for_ocr_or_display_source",
        )

    def test_elog_recovery_requires_advancing_source_timestamps(self):
        rows = [_evap_row(i, float(i)) for i in range(12)]
        markers = [{
            "kind": "failure-start",
            "episode_id": "elog-stop",
            "fault_class": "file-feed-stop",
            "marked_monotonic_s": 3.1,
        }, {
            "kind": "recovery",
            "episode_id": "elog-stop",
            "marked_monotonic_s": 7.1,
        }]
        watchdog = []
        source_seconds = (0, 1, 2, 2, 2, 2, 2, 2, 8, 9, 10, 11)
        for index, (t, source_second) in enumerate(
            zip(range(12), source_seconds)
        ):
            watchdog.append({
                "watchdog_index": index,
                "observed_monotonic_s": float(t),
                "observed_at_utc": f"2026-07-27T00:00:{t:02d}.000Z",
                "elapsed_s": float(t),
                "last_emission_age_s": 0.2,
                "emission_timeout": False,
                "source_unchanged_for_s": (
                    0.0 if t < 3 or t > 7 else float(t - 2)
                ),
                "source_probe": {
                    "elog_source_at_utc": (
                        f"2026-07-27T00:00:{source_second:02d}.000Z"
                    ),
                    "elog_mtime_ns": t * 1000,
                },
            })
        summary = summarize(
            rows,
            "evap",
            mode="elog",
            markers=markers,
            watchdog_rows=watchdog,
            baseline_duration_s=3.0,
            required_fresh_samples=3,
        )
        episode = summary["failure_episodes"][0]
        self.assertTrue(episode["recovery_confirmed"])
        self.assertEqual(
            episode["recovery_evidence_kind"],
            "advancing_elog_source_timestamps",
        )
        self.assertAlmostEqual(
            episode["fresh_recovery_latency_s"], 2.9, places=6
        )

    def test_touch_only_elog_metadata_is_not_source_freshness(self):
        first = {
            "source_probe": {
                "elog_source_at_utc": "2026-07-27T00:00:01.000Z",
                "elog_mtime_ns": 1,
            }
        }
        touched = {
            "source_probe": {
                "elog_source_at_utc": "2026-07-27T00:00:01.000Z",
                "elog_mtime_ns": 999,
            }
        }
        self.assertEqual(
            probe_module._elog_source_token(first),
            probe_module._elog_source_token(touched),
        )

    def test_wgc_sequence_does_not_confirm_camera_power_recovery(self):
        rows = []
        for index in range(9):
            rows.append({
                "emission_index": index,
                "observed_at_utc": (
                    f"2026-07-27T00:00:{index:02d}.000Z"
                ),
                "observed_monotonic_s": float(index),
                "observed_monotonic_ns": index * 1_000_000_000,
                "elapsed_s": float(index),
                "state": {
                    "frame_number": index,
                    "capture_sequence": index + 1,
                    "frame": np.zeros((2, 2, 3), dtype=np.uint8),
                    "connected": index not in {4, 5},
                    "error": "camera lost" if index in {4, 5} else "",
                },
            })
        markers = [{
            "kind": "failure-start",
            "episode_id": "camera-power",
            "fault_class": "instrument-power-loss",
            "marked_monotonic_s": 3.1,
        }, {
            "kind": "recovery",
            "episode_id": "camera-power",
            "marked_monotonic_s": 5.1,
        }]
        summary = summarize(
            rows,
            "camera",
            mode="screengrab",
            markers=markers,
            baseline_duration_s=3.0,
        )
        episode = summary["failure_episodes"][0]
        self.assertFalse(episode["recovery_confirmed"])
        self.assertEqual(
            episode["recovery_evidence_kind"],
            "unavailable_no_exposure_provenance",
        )

    def test_boot_change_uses_utc_timeline(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(7)]
        for row in rows[:4]:
            row["boot_id"] = "boot-a"
            row["boot_id_source"] = "windows_registry_prefetch_boot_id"
        for row in rows[4:]:
            row["boot_id"] = "boot-b"
            row["boot_id_source"] = "windows_registry_prefetch_boot_id"
        markers = [{
            "kind": "failure-start",
            "episode_id": "pc-loss",
            "marked_monotonic_s": 3.1,
            "marked_at_utc": "2026-07-27T00:00:03.100Z",
            "boot_id": "boot-a",
            "boot_id_source": "windows_registry_prefetch_boot_id",
        }, {
            "kind": "recovery",
            "episode_id": "pc-loss",
            "marked_monotonic_s": 0.1,
            "marked_at_utc": "2026-07-27T00:00:03.900Z",
            "boot_id": "boot-b",
            "boot_id_source": "windows_registry_prefetch_boot_id",
        }]
        summary = summarize(
            rows,
            "pyrometer",
            markers=markers,
            baseline_duration_s=10.0,
        )
        episode = summary["failure_episodes"][0]
        self.assertEqual(episode["timeline"], "utc")
        self.assertTrue(episode["clock_or_boot_discontinuity"])
        self.assertTrue(episode["timeline_valid"])
        self.assertTrue(episode["cross_boot_order_valid"])

    def test_old_boot_rows_cannot_confirm_post_reboot_recovery(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(7)]
        for row in rows:
            row["boot_id"] = "boot-a"
            row["boot_id_source"] = "windows_registry_prefetch_boot_id"
        markers = [{
            "kind": "failure-start",
            "episode_id": "pc-loss",
            "marked_monotonic_s": 3.1,
            "marked_at_utc": "2026-07-27T00:00:03.100Z",
            "boot_id": "boot-a",
            "boot_id_source": "windows_registry_prefetch_boot_id",
        }, {
            "kind": "recovery",
            "episode_id": "pc-loss",
            "marked_monotonic_s": 0.1,
            "marked_at_utc": "2026-07-27T00:00:04.100Z",
            "boot_id": "boot-b",
            "boot_id_source": "windows_registry_prefetch_boot_id",
        }]
        summary = summarize(
            rows,
            "pyrometer",
            mode="exactus",
            markers=markers,
            baseline_duration_s=3.0,
        )
        episode = summary["failure_episodes"][0]
        self.assertFalse(episode["cross_boot_order_valid"])
        self.assertFalse(episode["timeline_valid"])
        self.assertFalse(episode["recovery_confirmed"])

    def test_backward_utc_across_reboot_is_not_evaluable(self):
        rows = [_pyrometer_row(i, float(i)) for i in range(7)]
        for row in rows[:4]:
            row["boot_id"] = "boot-a"
            row["boot_id_source"] = "windows_registry_prefetch_boot_id"
        for offset, row in enumerate(rows[4:], start=2):
            row["boot_id"] = "boot-b"
            row["boot_id_source"] = "windows_registry_prefetch_boot_id"
            row["observed_at_utc"] = (
                f"2026-07-27T00:00:{offset:02d}.000Z"
            )
            row["observed_monotonic_s"] = float(offset - 2)
        markers = [{
            "kind": "failure-start",
            "episode_id": "clock-jump",
            "marked_monotonic_s": 3.1,
            "marked_at_utc": "2026-07-27T00:00:03.100Z",
            "boot_id": "boot-a",
            "boot_id_source": "windows_registry_prefetch_boot_id",
        }, {
            "kind": "recovery",
            "episode_id": "clock-jump",
            "marked_monotonic_s": 0.1,
            "marked_at_utc": "2026-07-27T00:00:02.100Z",
            "boot_id": "boot-b",
            "boot_id_source": "windows_registry_prefetch_boot_id",
        }]
        summary = summarize(
            rows,
            "pyrometer",
            mode="exactus",
            markers=markers,
            baseline_duration_s=3.0,
        )
        episode = summary["failure_episodes"][0]
        self.assertFalse(episode["timeline_valid"])
        self.assertEqual(
            episode["assessment"],
            "not_evaluable_clock_or_boot_timeline",
        )


class EvidenceTests(unittest.TestCase):
    def test_output_directory_is_collision_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            prepare_output_dir(target)
            with self.assertRaises(FileExistsError):
                prepare_output_dir(target)

    def test_marker_is_hashed_after_completed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run_info = {
                "source": "pyrometer",
                "mode": "exactus",
                "baseline_duration_s": 1.0,
                "baseline_p95_s_supplied": None,
                "required_fresh_samples": 3,
                "fault_class": "offline-injection",
                "link_kind": "none",
            }
            (directory / "run_info.json").write_text(
                json.dumps(run_info), encoding="utf-8"
            )
            with (directory / "states.jsonl").open(
                "w", encoding="utf-8"
            ) as stream:
                for row in [_pyrometer_row(i, float(i)) for i in range(3)]:
                    stream.write(json.dumps(row) + "\n")
            (directory / "watchdog.jsonl").write_text("", encoding="utf-8")
            (directory / "summary.json").write_text(
                json.dumps({
                    "evidence_schema_version": 2,
                    "lifecycle_fatal_errors": ["preserve-me"],
                    "fatal_errors": ["preserve-me"],
                }),
                encoding="utf-8",
            )

            def args(kind):
                return SimpleNamespace(
                    output_dir=directory,
                    kind=kind,
                    episode_id="episode-1",
                    label=kind,
                    note="manual action",
                    action_side="operator",
                    marker_uncertainty_ms=0.0,
                )

            self.assertEqual(mark_event(args("failure-start")), 0)
            marker_path = directory / "markers.jsonl"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["kind"], "failure-start")
            manifest = json.loads(
                (directory / "sha256_manifest.json").read_text(
                    encoding="utf-8",
                )
            )
            expected = hashlib.sha256(marker_path.read_bytes()).hexdigest()
            self.assertEqual(manifest["markers.jsonl"], expected)
            refreshed = json.loads(
                (directory / "summary.json").read_text(encoding="utf-8")
            )
            self.assertIn("preserve-me", refreshed["fatal_errors"])
            self.assertTrue(
                any(
                    "without terminal marker" in error
                    for error in refreshed["marker_evidence_errors"]
                )
            )
            self.assertEqual(refreshed["failure_marker_count"], 1)
            self.assertEqual(mark_event(args("recovery")), 0)
            recovered = json.loads(
                (directory / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                recovered["lifecycle_fatal_errors"],
                ["preserve-me"],
            )
            self.assertFalse(
                any(
                    "without terminal marker" in error
                    for error in recovered["marker_evidence_errors"]
                )
            )

    def test_marker_rejects_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = type("Args", (), {
                "output_dir": Path(tmp),
                "kind": "action",
                "label": "cover",
                "note": "",
            })()
            with self.assertRaises(FileNotFoundError):
                mark_event(args)

    def test_marker_pairing_rejects_duplicate_and_unmatched_events(self):
        errors = marker_pairing_errors([
            {
                "kind": "failure-start",
                "episode_id": "episode-1",
                "marked_monotonic_s": 1.0,
            },
            {
                "kind": "failure-start",
                "episode_id": "episode-1",
                "marked_monotonic_s": 2.0,
            },
            {
                "kind": "recovery",
                "episode_id": "episode-2",
                "marked_monotonic_s": 3.0,
            },
        ])
        self.assertTrue(any("duplicate failure-start" in error for error in errors))
        self.assertTrue(any("without failure-start" in error for error in errors))

    def test_manual_failure_marker_requires_precheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run_info = {
                "fault_class": "window-source-loss",
                "safe_state_confirmed": True,
                "link_kind": "video",
            }
            (directory / "run_info.json").write_text(
                json.dumps(run_info), encoding="utf-8"
            )

            def args(kind):
                return SimpleNamespace(
                    output_dir=directory,
                    kind=kind,
                    episode_id="window-1",
                    label=kind,
                    note="",
                    action_side="operator",
                    marker_uncertainty_ms=0.0,
                )

            with self.assertRaisesRegex(ValueError, "precheck-complete"):
                mark_event(args("failure-start"))
            self.assertEqual(mark_event(args("precheck-complete")), 0)
            with self.assertRaisesRegex(ValueError, "active recorder process"):
                mark_event(args("failure-start"))

    def test_failure_start_requires_live_healthy_baseline_and_one_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            boot_id, boot_source = probe_module.current_boot_identity()
            now = datetime.now(timezone.utc)
            first_at = now - timedelta(seconds=3)
            first_monotonic = time.monotonic() - 3.0
            rows = []
            watchdog_rows = []
            for index in range(7):
                offset_s = index * 0.5
                observed = first_at + timedelta(seconds=offset_s)
                observed_text = observed.isoformat().replace("+00:00", "Z")
                row = _pyrometer_row(index, offset_s)
                row.update({
                    "observed_at_utc": observed_text,
                    "observed_monotonic_s": first_monotonic + offset_s,
                    "observed_monotonic_ns": round(
                        (first_monotonic + offset_s) * 1_000_000_000
                    ),
                    "elapsed_s": offset_s,
                    "boot_id": boot_id,
                    "boot_id_source": boot_source,
                })
                row["state"]["received_at_utc"] = observed_text
                rows.append(row)
                watchdog_rows.append({
                    "watchdog_index": index,
                    "observed_at_utc": observed_text,
                    "observed_monotonic_s": first_monotonic + offset_s,
                    "observed_monotonic_ns": round(
                        (first_monotonic + offset_s) * 1_000_000_000
                    ),
                    "elapsed_s": offset_s,
                    "boot_id": boot_id,
                    "boot_id_source": boot_source,
                    "last_emission_age_s": 0.1,
                    "worker_running": True,
                    "network_interface": None,
                    "serial_link": None,
                })
            run_info = {
                "source": "pyrometer",
                "mode": "screengrab",
                "fault_class": "window-source-loss",
                "safe_state_confirmed": True,
                "link_kind": "video",
                "boot_id": boot_id,
                "boot_id_source": boot_source,
                "poll_interval_s": 0.5,
                "watchdog_interval_s": 0.5,
                "baseline_duration_s": 3.0,
                "baseline_p95_s_supplied": 0.5,
                "required_fresh_samples": 3,
            }
            (directory / "run_info.json").write_text(
                json.dumps(run_info), encoding="utf-8"
            )
            for name, records in (
                ("states.jsonl", rows),
                ("watchdog.jsonl", watchdog_rows),
            ):
                (directory / name).write_text(
                    "".join(json.dumps(row) + "\n" for row in records),
                    encoding="utf-8",
                )

            def args(kind, episode_id="window-1"):
                return SimpleNamespace(
                    output_dir=directory,
                    kind=kind,
                    episode_id=episode_id,
                    label=kind,
                    note="",
                    action_side="operator",
                    marker_uncertainty_ms=0.0,
                )

            with probe_module._RecorderLivenessLease(directory):
                self.assertEqual(mark_event(args("precheck-complete")), 0)
                self.assertEqual(mark_event(args("failure-start")), 0)
                self.assertEqual(mark_event(args("failure-observed")), 0)
                with self.assertRaisesRegex(ValueError, "prior recovery"):
                    mark_event(args("vendor-ready"))
                self.assertEqual(mark_event(args("recovery")), 0)
                with self.assertRaisesRegex(ValueError, "after terminal"):
                    mark_event(args("failure-observed"))
                self.assertEqual(mark_event(args("vendor-ready")), 0)
                self.assertEqual(mark_event(args("gui-reconnected")), 0)
                self.assertEqual(mark_event(args("rearmed")), 0)
                self.assertEqual(
                    mark_event(args("precheck-complete", "window-2")),
                    0,
                )
                with self.assertRaisesRegex(ValueError, "one fault episode"):
                    mark_event(args("failure-start", "window-2"))

    def test_failure_start_rejects_stale_trailing_elog_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            boot_id, boot_source = probe_module.current_boot_identity()
            now = datetime.now(timezone.utc)
            first_at = now - timedelta(seconds=8)
            first_monotonic = time.monotonic() - 8.0
            rows = []
            watchdog_rows = []
            for index in range(9):
                observed = first_at + timedelta(seconds=index)
                observed_text = observed.isoformat().replace("+00:00", "Z")
                row = _evap_row(index, float(index))
                row.update({
                    "observed_at_utc": observed_text,
                    "observed_monotonic_s": first_monotonic + index,
                    "observed_monotonic_ns": round(
                        (first_monotonic + index) * 1_000_000_000
                    ),
                    "elapsed_s": float(index),
                    "boot_id": boot_id,
                    "boot_id_source": boot_source,
                })
                rows.append(row)
                source_second = min(index, 4)
                source_at = first_at + timedelta(seconds=source_second)
                watchdog_rows.append({
                    "watchdog_index": index,
                    "observed_at_utc": observed_text,
                    "observed_monotonic_s": first_monotonic + index,
                    "observed_monotonic_ns": round(
                        (first_monotonic + index) * 1_000_000_000
                    ),
                    "elapsed_s": float(index),
                    "boot_id": boot_id,
                    "boot_id_source": boot_source,
                    "last_emission_age_s": 0.1,
                    "worker_running": True,
                    "source_unchanged_for_s": float(max(0, index - 4)),
                    "source_probe": {
                        "elog_source_at_utc": (
                            source_at.isoformat().replace("+00:00", "Z")
                        ),
                    },
                    "network_interface": None,
                    "serial_link": None,
                })
            run_info = {
                "source": "evap",
                "mode": "elog",
                "fault_class": "file-feed-stop",
                "safe_state_confirmed": True,
                "link_kind": "file",
                "boot_id": boot_id,
                "poll_interval_s": 1.0,
                "watchdog_interval_s": 1.0,
                "baseline_duration_s": 6.0,
                "baseline_p95_s_supplied": 1.0,
                "required_fresh_samples": 3,
            }
            for name, value in (
                ("run_info.json", run_info),
                ("states.jsonl", rows),
                ("watchdog.jsonl", watchdog_rows),
            ):
                path = directory / name
                if name.endswith(".jsonl"):
                    path.write_text(
                        "".join(json.dumps(row) + "\n" for row in value),
                        encoding="utf-8",
                    )
                else:
                    path.write_text(json.dumps(value), encoding="utf-8")
            with probe_module._RecorderLivenessLease(directory):
                with self.assertRaisesRegex(
                    ValueError,
                    "source_feed_stale",
                ):
                    probe_module._validate_live_baseline_before_failure(
                        run_info,
                        directory,
                        [],
                    )

    def test_serial_failure_marker_rejects_unknown_driver_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            watchdog = {
                "observed_at_utc": probe_module.utc_now(),
                "serial_link": {
                    "present": True,
                    "hwid": "USB VID:PID=1234:5678",
                },
                "driver_identity": {
                    "port": None,
                    "baudrate": None,
                },
            }
            (directory / "watchdog.jsonl").write_text(
                json.dumps(watchdog) + "\n",
                encoding="utf-8",
            )
            run_info = {
                "link_kind": "serial",
                "source": "pyrometer",
                "mode": "exactus",
                "watchdog_interval_s": 0.5,
                "requested_interface": {
                    "port": "COM4",
                    "baudrate": 115200,
                },
            }
            with self.assertRaisesRegex(ValueError, "unknown or mismatched"):
                probe_module._validate_interface_before_failure(
                    run_info,
                    directory,
                )

    def test_network_failure_marker_rejects_cached_link_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            watchdog = {
                "observed_at_utc": probe_module.utc_now(),
                "network_interface": {
                    "sampled_at_utc": "2020-01-01T00:00:00.000Z",
                    "present": True,
                    "is_up": True,
                    "interface_index": 7,
                    "interface": "Lab NIC",
                    "name_matches": True,
                },
            }
            (directory / "watchdog.jsonl").write_text(
                json.dumps(watchdog) + "\n",
                encoding="utf-8",
            )
            run_info = {
                "link_kind": "network",
                "watchdog_interval_s": 0.5,
                "requested_interface": {
                    "network_interface_index": 7,
                    "network_interface": "Lab NIC",
                },
            }
            with self.assertRaisesRegex(ValueError, "exact declared local"):
                probe_module._validate_interface_before_failure(
                    run_info,
                    directory,
                )

    def test_passive_elog_probe_records_file_freshness_without_writes(self):
        class ElogReader:
            connected = True

            def __init__(self, path):
                self._last_log_path = path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.elo"
            path.write_bytes(b"not-a-real-elog")
            worker = SimpleNamespace(_driver=ElogReader(path))
            result = passive_source_probe(worker, "evap")
            self.assertEqual(result["elog_path"], str(path))
            self.assertEqual(result["elog_size_bytes"], len(b"not-a-real-elog"))
            self.assertIn("elog_tail_sha256", result)
            self.assertIn("error", result)

    def test_network_probe_reads_adapter_state_without_socket_connection(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                b"Idx Met MTU State Name\r\n"
                b"7 25 1500 connected Lab NIC\r\n"
            ),
            stderr=b"",
        )
        with patch.object(
            probe_module.subprocess,
            "run",
            return_value=completed,
        ) as run:
            result = passive_network_interface_probe(7, "Lab NIC")
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["netsh", "interface", "ipv4", "show"])
        self.assertTrue(result["is_up"])
        self.assertEqual(result["interface"], "Lab NIC")
        self.assertEqual(result["interface_index"], 7)

    def test_finalize_salvages_fsynced_partial_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "partial"
            directory.mkdir()
            run_info = {
                "source": "pyrometer",
                "mode": "exactus",
                "baseline_duration_s": 10.0,
                "baseline_p95_s_supplied": None,
                "required_fresh_samples": 3,
                "boot_id": "old-boot",
            }
            (directory / "run_info.json").write_text(
                json.dumps(run_info), encoding="utf-8"
            )
            with (directory / "states.jsonl").open(
                "w", encoding="utf-8"
            ) as stream:
                for row in [_pyrometer_row(i, float(i)) for i in range(4)]:
                    stream.write(json.dumps(row) + "\n")
            (directory / "watchdog.jsonl").write_text("", encoding="utf-8")
            result = finalize_interrupted_run(
                Namespace(output_dir=directory, force=False)
            )
            self.assertEqual(result, 2)
            summary = json.loads(
                (directory / "summary.json").read_text(encoding="utf-8")
            )
            self.assertTrue(summary["finalized_after_interruption"])
            self.assertEqual(summary["recovered_state_rows"], 4)
            self.assertIn(
                "no failure-start episode was recorded",
                summary["incomplete_reasons"],
            )
            self.assertIn(
                "recorder terminated before normal lifecycle finalization",
                summary["lifecycle_fatal_errors"],
            )
            self.assertTrue((directory / "sha256_manifest.json").is_file())

    def test_finalize_never_promotes_interrupted_complete_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run_info = {
                "source": "pyrometer",
                "mode": "exactus",
                "baseline_duration_s": 3.0,
                "baseline_p95_s_supplied": 1.0,
                "required_fresh_samples": 3,
                "fault_class": "offline-injection",
                "link_kind": "none",
            }
            (directory / "run_info.json").write_text(
                json.dumps(run_info), encoding="utf-8"
            )
            rows = [_pyrometer_row(index, float(index)) for index in range(9)]
            for index in (4, 5):
                rows[index]["state"].update({
                    "temperature": None,
                    "temperature_std": None,
                    "temperature_n": 0,
                    "emissivity": None,
                    "connected": False,
                    "error": "synthetic outage",
                })
            (directory / "states.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            (directory / "watchdog.jsonl").write_text("", encoding="utf-8")
            markers = [{
                "kind": "failure-start",
                "episode_id": "interrupted-complete",
                "fault_class": "offline-injection",
                "marked_at_utc": "2026-07-27T00:00:03.100Z",
                "marked_monotonic_s": 3.1,
            }, {
                "kind": "recovery",
                "episode_id": "interrupted-complete",
                "marked_at_utc": "2026-07-27T00:00:05.100Z",
                "marked_monotonic_s": 5.1,
            }]
            (directory / "markers.jsonl").write_text(
                "".join(json.dumps(marker) + "\n" for marker in markers),
                encoding="utf-8",
            )
            result = finalize_interrupted_run(
                Namespace(output_dir=directory, force=False)
            )
            self.assertEqual(result, 2)
            summary = json.loads(
                (directory / "summary.json").read_text(encoding="utf-8")
            )
            self.assertFalse(summary["run_complete"])
            self.assertTrue(summary["finalized_after_interruption"])
            self.assertIn(
                "recorder terminated before normal lifecycle finalization",
                summary["fatal_errors"],
            )

    def test_finalize_refuses_active_recorder_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "run_info.json").write_text(
                json.dumps({"source": "pyrometer"}),
                encoding="utf-8",
            )
            with probe_module._RecorderLivenessLease(directory):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "liveness lease is active",
                ):
                    finalize_interrupted_run(
                        Namespace(output_dir=directory, force=False)
                    )


class RunLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtCore import QCoreApplication

        cls._app = QCoreApplication.instance() or QCoreApplication([])

    @staticmethod
    def _args(output_dir: Path) -> Namespace:
        return Namespace(
            source="mistral",
            mode="screengrab",
            duration_s=0.01,
            poll_interval=1.0,
            baseline_duration_s=1.0,
            baseline_p95_s=None,
            software_root=Path.cwd(),
            expected_capture_backend=None,
            port="COM4",
            baudrate=115200,
            device_id=1,
            network_interface=None,
            network_interface_index=None,
            network_probe_interval=2.0,
            watchdog_interval=0.05,
            emission_timeout_s=0.1,
            required_fresh_samples=3,
            campaign_id="unit-test",
            scenario_id="unit-test-scenario",
            fault_class="window-source-loss",
            link_kind="video",
            target="fake",
            boundary="synthetic window fixture",
            operator="unit-test",
            observer="unit-test",
            approver="unit-test",
            authorization_ref="unit-test",
            sop_ref="unit-test",
            safe_state_note="synthetic worker only",
            abort_criteria="test assertion",
            independent_observer_ref="",
            confirm_safe_state=True,
            output_dir=output_dir,
        )

    def test_stop_timeout_is_nonzero_and_evidence_is_finalized(self):
        from PyQt6.QtCore import QObject, QTimer, pyqtSignal

        class MistralGui:
            pass

        class FakeWorker(QObject):
            state_updated = pyqtSignal(object)
            finished = pyqtSignal()

            def __init__(self):
                super().__init__()
                self._driver = MistralGui()

            def start(self):
                state = SimpleNamespace(
                    v_set=1.0,
                    v_actual=1.0,
                    i_set=1.0,
                    i_actual=1.0,
                    connected=True,
                    error="",
                    mode="screengrab",
                )
                QTimer.singleShot(0, lambda: self.state_updated.emit(state))

            def stop(self):
                pass

            def wait(self, _timeout_ms):
                return False

            def isRunning(self):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            with (
                patch.object(
                    probe_module,
                    "_worker_factory",
                    return_value=FakeWorker(),
                ),
                patch.object(
                    _SerialPortMutex,
                    "retain_until_process_exit",
                ) as retain_lock,
            ):
                result = run_probe(self._args(output_dir))
            retain_lock.assert_called_once()
            self.assertEqual(result, 2)
            summary = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )
            self.assertTrue(summary["stop_timed_out"])
            self.assertIn(
                "worker did not stop within 5 seconds",
                summary["fatal_errors"],
            )
            self.assertEqual(
                len(
                    (output_dir / "states.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
                1,
            )
            self.assertTrue((output_dir / "sha256_manifest.json").is_file())

    def test_watchdog_join_timeout_never_writes_manifest(self):
        from PyQt6.QtCore import QObject, QTimer, pyqtSignal

        class MistralGui:
            pass

        class FakeWorker(QObject):
            state_updated = pyqtSignal(object)
            finished = pyqtSignal()

            def __init__(self):
                super().__init__()
                self._driver = MistralGui()
                self._running = True

            def start(self):
                state = SimpleNamespace(
                    v_set=1.0,
                    v_actual=1.0,
                    i_set=1.0,
                    i_actual=1.0,
                    connected=True,
                    error="",
                    mode="screengrab",
                )
                QTimer.singleShot(0, lambda: self.state_updated.emit(state))

            def stop(self):
                self._running = False

            def wait(self, _timeout_ms):
                return True

            def isRunning(self):
                return self._running

        class StuckWatchdogThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def join(self, timeout=None):
                pass

            def is_alive(self):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            with (
                patch.object(
                    probe_module,
                    "_worker_factory",
                    return_value=FakeWorker(),
                ),
                patch.object(
                    probe_module.threading,
                    "Thread",
                    StuckWatchdogThread,
                ),
            ):
                result = run_probe(self._args(output_dir))
            self.assertEqual(result, 2)
            summary = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )
            self.assertTrue(summary["watchdog_stop_timed_out"])
            self.assertFalse(summary["manifest_written"])
            self.assertFalse(
                (output_dir / "sha256_manifest.json").exists()
            )

    def test_unexpected_driver_fallback_fails_closed(self):
        from PyQt6.QtCore import QObject, QTimer, pyqtSignal

        class DummyMistralGui:
            pass

        class FakeWorker(QObject):
            state_updated = pyqtSignal(object)
            finished = pyqtSignal()

            def __init__(self):
                super().__init__()
                self._driver = DummyMistralGui()
                self._running = True

            def start(self):
                state = SimpleNamespace(
                    v_set=1.0,
                    v_actual=1.0,
                    i_set=1.0,
                    i_actual=1.0,
                    connected=True,
                    error="",
                    mode="screengrab",
                )
                QTimer.singleShot(0, lambda: self.state_updated.emit(state))

            def stop(self):
                self._running = False

            def wait(self, _timeout_ms):
                return True

            def isRunning(self):
                return self._running

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            with patch.object(
                probe_module,
                "_worker_factory",
                return_value=FakeWorker(),
            ):
                result = run_probe(self._args(output_dir))
            self.assertEqual(result, 2)
            summary = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                any(
                    "refusing dummy/fallback validation" in message
                    for message in summary["fatal_errors"]
                )
            )

    def test_watchdog_records_timeout_when_worker_emits_nothing(self):
        from PyQt6.QtCore import QObject, pyqtSignal

        class MistralGui:
            connected = True

        class SilentWorker(QObject):
            state_updated = pyqtSignal(object)
            finished = pyqtSignal()

            def __init__(self):
                super().__init__()
                self._driver = MistralGui()
                self._running = True

            def start(self):
                pass

            def stop(self):
                self._running = False

            def wait(self, _timeout_ms):
                return True

            def isRunning(self):
                return self._running

        with tempfile.TemporaryDirectory() as tmp:
            args = self._args(Path(tmp) / "run")
            args.duration_s = 0.15
            args.watchdog_interval = 0.02
            args.emission_timeout_s = 0.04
            with patch.object(
                probe_module,
                "_worker_factory",
                return_value=SilentWorker(),
            ):
                result = run_probe(args)
            self.assertEqual(result, 2)
            summary = json.loads(
                (args.output_dir / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["emissions"], 0)
            self.assertGreater(summary["watchdog_samples"], 0)
            self.assertGreater(
                summary["watchdog_emission_timeout_samples"], 0
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
