"""Platform-independent tests for the O-MBE interface cross-check runner."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ombe_interface_crosscheck import (  # noqa: E402
    RunArtifacts,
    _SerialPortMutex,
    _exit_code_for_summary,
    acquire_evap_pair_sample,
    acquire_mistral_sample,
    audit_modbus_configuration,
    collect_timed,
    diagnose_jsonrpc,
    parse_evap_pressure_text,
    parse_mistral_text,
    probe_pyrometer,
)


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds

    def advance(self, seconds):
        self.value += seconds


class FakeImage:
    """Minimal ndarray-like RGB image for platform-neutral crop tests."""

    def __init__(self, height, width, channels=3):
        self.shape = (height, width, channels)
        self.size = height * width * channels

    def __getitem__(self, key):
        y_slice, x_slice = key
        height = max(0, min(self.shape[0], y_slice.stop) - max(0, y_slice.start))
        width = max(0, min(self.shape[1], x_slice.stop) - max(0, x_slice.start))
        return FakeImage(height, width, self.shape[2])


class ParserTests(unittest.TestCase):
    def test_evap_parser_accepts_scientific_notation(self):
        self.assertEqual(
            parse_evap_pressure_text("Pressure 2.50 e-09 mbar"), 2.5e-9
        )

    def test_evap_parser_rejects_out_of_range_and_missing(self):
        self.assertIsNone(parse_evap_pressure_text("Pressure unavailable"))
        self.assertIsNone(parse_evap_pressure_text("Pressure 2.0e+01 mbar"))

    def test_mistral_parser_extracts_all_four_fields(self):
        text = (
            "Set Voltage: 10.0 V Set Current: 2.0 A\n"
            "Actual Voltage: 9.9 V Actual Current: 1.9 A"
        )
        self.assertEqual(
            parse_mistral_text(text),
            {
                "v_set": 10.0,
                "v_actual": 9.9,
                "i_set": 2.0,
                "i_actual": 1.9,
            },
        )

    def test_mistral_parser_keeps_out_of_range_field_none(self):
        values = parse_mistral_text(
            "Set Voltage: 999 V Actual Voltage: 10 V "
            "Set Current: 2 A Actual Current: 1 A"
        )
        self.assertIsNone(values["v_set"])
        self.assertEqual(values["v_actual"], 10.0)


class CollectionTests(unittest.TestCase):
    def test_monotonic_schedule_and_read_duration(self):
        clock = FakeClock()
        emitted = []

        def read_one(sequence):
            clock.advance(0.025)
            return {"value": sequence}

        result = collect_timed(
            read_one,
            duration_s=2.1,
            interval_s=1.0,
            emit=emitted.append,
            clock=clock,
            sleep=clock.sleep,
            utc_now=lambda: "2026-07-27T00:00:00+00:00",
        )
        self.assertEqual([row["sequence"] for row in emitted], [0, 1, 2])
        self.assertTrue(all(row["read_ok"] for row in emitted))
        self.assertTrue(
            all(abs(row["read_duration_ms"] - 25.0) < 1e-6 for row in emitted)
        )
        self.assertFalse(result.interrupted)

    def test_read_exception_is_preserved_as_sample(self):
        clock = FakeClock()
        emitted = []

        def read_one(_sequence):
            raise RuntimeError("fake read failure")

        collect_timed(
            read_one,
            duration_s=0.5,
            interval_s=1.0,
            emit=emitted.append,
            clock=clock,
            sleep=clock.sleep,
            utc_now=lambda: "now",
        )
        self.assertEqual(len(emitted), 1)
        self.assertFalse(emitted[0]["read_ok"])
        self.assertIn("fake read failure", emitted[0]["read_error"])


class FakePyrometer:
    def __init__(self):
        self.connected = False
        self.disconnected = False
        self.read_count = 0

    def connect(self):
        self.connected = True

    def get_info(self):
        return {"name": "fake", "serial": "TEST-1"}

    def read_temperature(self):
        self.read_count += 1
        return 400.0 + self.read_count

    def read_emissivity(self):
        return 0.82

    def set_rate(self, *_args, **_kwargs):
        raise AssertionError("probe must never call an instrument setter")

    def disconnect(self):
        self.disconnected = True


class PyrometerProbeTests(unittest.TestCase):
    def test_fake_sensor_connect_read_disconnect_without_setter(self):
        clock = FakeClock()
        sensor = FakePyrometer()
        emitted = []
        summary = probe_pyrometer(
            sensor,
            mode="modbus",
            duration_s=1.1,
            interval_s=1.0,
            samples_per_poll=2,
            emit=emitted.append,
            clock=clock,
            sleep=clock.sleep,
            utc_now=lambda: "now",
        )
        self.assertTrue(sensor.connected)
        self.assertTrue(sensor.disconnected)
        self.assertEqual(sensor.read_count, 4)
        self.assertEqual(len(emitted), 2)
        self.assertEqual(emitted[0]["temperature_n"], 2)
        self.assertEqual(summary["device_info"]["serial"], "TEST-1")
        self.assertEqual(summary["successful_sample_calls"], 2)
        self.assertEqual(summary["valid_numeric_samples"], 2)
        self.assertEqual(summary["valid_temperature_readings"], 4)
        self.assertEqual(summary["temperature_C"]["min"], 401.0)
        self.assertEqual(summary["temperature_C"]["max"], 404.0)

    @unittest.skipUnless(
        sys.platform == "win32", "named mutex is a Windows safety gate"
    )
    def test_serial_runner_mutex_rejects_same_port_concurrently(self):
        port = "COM-CODEX-INTERFACE-MUTEX-TEST"
        with _SerialPortMutex(port):
            with self.assertRaisesRegex(RuntimeError, "diagnostic lock"):
                with _SerialPortMutex(port):
                    self.fail("second lock must not be acquired")
        with _SerialPortMutex(port):
            pass


class OcrAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.frame = FakeImage(100, 200)
        self.saved = []

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self, _crop, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-png")
        self.saved.append(path)

    def test_evap_pair_records_crop_text_values_and_delta(self):
        monotonic_values = iter(
            [1_000_000_000, 1_100_000_000, 1_200_000_000,
             1_350_000_000, 1_500_000_000]
        )
        utc_values = iter(
            [
                "2026-07-27T00:00:00.000+00:00",
                "2026-07-27T00:00:00.100+00:00",
                "2026-07-27T00:00:00.200+00:00",
                "2026-07-27T00:00:00.350+00:00",
                "2026-07-27T00:00:00.500+00:00",
            ]
        )
        row = acquire_evap_pair_sample(
            3,
            hwnd=123,
            crop_dir=self.root / "crops",
            capture_fn=lambda _hwnd: self.frame,
            window_metadata_fn=lambda _hwnd: {
                "window_width_px": 200,
                "window_height_px": 100,
                "dpi": 144,
            },
            bbox_fn=lambda _frame: (5, 6, 20, 10),
            ocr_fn=lambda _crop: "Pressure 2.00e-09 mbar",
            save_crop_fn=self._save,
            elog_fn=lambda: {
                "elog_source_at_utc": "2026-07-27T00:00:00+00:00",
                "elog_pressure_mbar": 1.5e-9,
            },
            utc_now=lambda: next(utc_values),
            monotonic_ns=lambda: next(monotonic_values),
        )
        self.assertTrue(row["elog_ok"])
        self.assertTrue(row["ocr_ok"])
        self.assertAlmostEqual(row["ocr_pressure_mbar"], 2e-9)
        self.assertAlmostEqual(row["pressure_absolute_difference_mbar"], 0.5e-9)
        self.assertEqual(row["window"]["dpi"], 144)
        self.assertTrue(self.saved[0].exists())
        self.assertEqual(row["pair_receive_offset_ms"], 250.0)
        self.assertEqual(row["elog_source_age_at_ocr_ms"], 350.0)
        self.assertEqual(row["ocr_frame_received_at_utc"],
                         "2026-07-27T00:00:00.350+00:00")

    def test_evap_ocr_survives_independent_elog_failure(self):
        def fail_elog():
            raise OSError("fake elog unavailable")

        row = acquire_evap_pair_sample(
            0,
            hwnd=1,
            crop_dir=self.root / "crops",
            capture_fn=lambda _hwnd: self.frame,
            window_metadata_fn=lambda _hwnd: {},
            bbox_fn=lambda _frame: (0, 0, 10, 10),
            ocr_fn=lambda _crop: "Pressure 1.0e-08 mbar",
            save_crop_fn=self._save,
            elog_fn=fail_elog,
        )
        self.assertFalse(row["elog_ok"])
        self.assertIn("fake elog unavailable", row["elog_error"])
        self.assertTrue(row["ocr_ok"])
        self.assertEqual(row["ocr_pressure_mbar"], 1e-8)

    def test_evap_invalid_elog_is_excluded_from_pair_statistics(self):
        row = acquire_evap_pair_sample(
            1,
            hwnd=1,
            crop_dir=self.root / "crops",
            capture_fn=lambda _hwnd: self.frame,
            window_metadata_fn=lambda _hwnd: {},
            bbox_fn=lambda _frame: (0, 0, 10, 10),
            ocr_fn=lambda _crop: "Pressure 1.0e-08 mbar",
            save_crop_fn=self._save,
            elog_fn=lambda: {
                "elog_source_at_utc": "2026-07-27T00:00:00+00:00",
                "elog_pressure_mbar": float("nan"),
            },
        )
        self.assertFalse(row["elog_pressure_valid"])
        self.assertFalse(row["paired_pressure_valid"])
        self.assertIsNone(row["pressure_absolute_difference_mbar"])

    def test_evap_ocr_exception_keeps_saved_crop_provenance(self):
        def fail_ocr(_crop):
            raise RuntimeError("fake OCR failure")

        row = acquire_evap_pair_sample(
            4,
            hwnd=1,
            crop_dir=self.root / "crops",
            capture_fn=lambda _hwnd: self.frame,
            window_metadata_fn=lambda _hwnd: {"dpi": 96},
            bbox_fn=lambda _frame: (0, 0, 10, 10),
            ocr_fn=fail_ocr,
            save_crop_fn=self._save,
            elog_fn=lambda: {
                "elog_source_at_utc": "2026-07-27T00:00:00+00:00",
                "elog_pressure_mbar": 1e-8,
            },
        )
        self.assertTrue(row["ocr_crop_saved"])
        self.assertTrue(Path(row["ocr_crop_path"]).exists())
        self.assertIn("fake OCR failure", row["ocr_error"])

    def test_mistral_records_window_dpi_crop_text_and_values(self):
        monotonic_values = iter(
            [2_000_000_000, 2_040_000_000, 2_100_000_000]
        )
        utc_values = iter(
            [
                "2026-07-27T00:00:00.000+00:00",
                "2026-07-27T00:00:00.040+00:00",
                "2026-07-27T00:00:00.100+00:00",
            ]
        )
        row = acquire_mistral_sample(
            2,
            hwnd=55,
            crop_dir=self.root / "crops",
            capture_fn=lambda _hwnd: self.frame,
            window_metadata_fn=lambda _hwnd: {
                "dpi": 120,
                "window_width_px": 200,
                "window_height_px": 100,
            },
            bbox_fn=lambda _frame: (0, 0, 40, 20),
            ocr_fn=lambda _crop: (
                "Set Voltage: 5 V Set Current: 2 A "
                "Actual Voltage: 4.9 V Actual Current: 1.9 A"
            ),
            save_crop_fn=self._save,
            utc_now=lambda: next(utc_values),
            monotonic_ns=lambda: next(monotonic_values),
        )
        self.assertEqual(row["parsed_field_count"], 4)
        self.assertFalse(row["all_fields_none"])
        self.assertEqual(row["window"]["dpi"], 120)
        self.assertTrue(Path(row["ocr_crop_path"]).exists())
        self.assertEqual(row["ocr_capture_duration_ms"], 40.0)
        self.assertEqual(row["ocr_pipeline_duration_ms"], 100.0)

    def test_mistral_ocr_exception_keeps_saved_crop_provenance(self):
        def fail_ocr(_crop):
            raise RuntimeError("fake MISTRAL OCR failure")

        row = acquire_mistral_sample(
            5,
            hwnd=55,
            crop_dir=self.root / "crops",
            capture_fn=lambda _hwnd: self.frame,
            window_metadata_fn=lambda _hwnd: {"dpi": 96},
            bbox_fn=lambda _frame: (0, 0, 20, 10),
            ocr_fn=fail_ocr,
            save_crop_fn=self._save,
        )
        self.assertTrue(row["ocr_crop_saved"])
        self.assertTrue(Path(row["ocr_crop_path"]).exists())
        self.assertFalse(row["ocr_ok"])
        self.assertIn("fake MISTRAL OCR failure", row["ocr_error"])
        self.assertTrue(row["all_fields_none"])


class FakeJsonRpc:
    def __init__(self, *, fail_connect=False, configured=False):
        self.fail_connect = fail_connect
        self._connected = False
        self.configured = configured
        self.disconnected = False

    def connect(self):
        if self.fail_connect:
            raise RuntimeError("fake unreachable")
        self._connected = True

    @property
    def connected(self):
        return self._connected

    def get_read_config(self):
        return {"v_set": ("readV", None)} if self.configured else {}

    def read(self):
        return {
            "v_set": None,
            "v_actual": None,
            "i_set": None,
            "i_actual": None,
        }

    def try_standard_discovery(self):
        return {"rpc.discover": {"ok": False, "error": "not found"}}

    def disconnect(self):
        self._connected = False
        self.disconnected = True


class JsonRpcDiagnosisTests(unittest.TestCase):
    def test_connected_empty_config_all_none_is_explicit(self):
        client = FakeJsonRpc()
        result = diagnose_jsonrpc(client)
        self.assertTrue(result["connected"])
        self.assertTrue(result["all_values_none"])
        self.assertEqual(
            result["diagnosis"],
            "connected_with_empty_read_config_and_all_none",
        )
        self.assertTrue(client.disconnected)

    def test_connect_failure_is_not_reported_as_all_none(self):
        result = diagnose_jsonrpc(FakeJsonRpc(fail_connect=True))
        self.assertFalse(result["connected"])
        self.assertIsNone(result["all_values_none"])
        self.assertEqual(result["diagnosis"], "unreachable_or_connect_failed")


class ConfigurationAuditTests(unittest.TestCase):
    def _sources(self, modbus_call):
        return {
            "growth_app": (
                "def arm(port, baud):\n"
                "    return PyrometerWorker(port=port, baudrate=baud)\n"
            ),
            "workers": (
                "class PyrometerWorker:\n"
                "    def _create_sensor(self):\n"
                f"        x = {modbus_call}\n"
                "        y = ExactusSerialPyrometer("
                "port=self.port, baudrate=self.baudrate)\n"
                "        return x\n"
            ),
            "pyrometer": (
                "class ModbusPyrometer:\n"
                "    def __init__(self, port='COM4', baudrate=115200, "
                "device_id=1):\n"
                "        pass\n"
            ),
        }

    def test_audit_detects_current_unforwarded_route(self):
        result = audit_modbus_configuration(
            REPO_ROOT, sources=self._sources("ModbusPyrometer()")
        )
        self.assertEqual(
            result["finding"], "gui_values_not_forwarded_to_modbus_driver"
        )
        self.assertFalse(result["worker_to_modbus"]["passes_port"])
        self.assertEqual(
            result["modbus_driver_constructor_defaults"]["port"], "COM4"
        )

    def test_audit_recognizes_forwarded_route(self):
        result = audit_modbus_configuration(
            REPO_ROOT,
            sources=self._sources(
                "ModbusPyrometer(port=self.port, baudrate=self.baudrate)"
            ),
        )
        self.assertEqual(
            result["finding"], "gui_values_forwarded_to_modbus_driver"
        )

    def test_hardcoded_modbus_keywords_are_not_forwarding(self):
        result = audit_modbus_configuration(
            REPO_ROOT,
            sources=self._sources(
                "ModbusPyrometer(port='COM9', baudrate=9600)"
            ),
        )
        self.assertEqual(
            result["finding"], "gui_values_not_forwarded_to_modbus_driver"
        )
        self.assertFalse(result["worker_to_modbus"]["route_complete"])

    def test_current_checkout_reports_observed_modbus_mismatch(self):
        result = audit_modbus_configuration(REPO_ROOT)
        self.assertEqual(
            result["finding"], "gui_values_not_forwarded_to_modbus_driver"
        )


class ArtifactTests(unittest.TestCase):
    def test_run_artifacts_have_summary_and_valid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = RunArtifacts(
                Path(tmp), "fake", {"duration_s": 1}, run_id="unit-test"
            )
            artifacts.append_sample({"sequence": 0, "value": 1.25})
            artifacts.finish({"run_status": "completed", "sample_attempts": 1})

            self.assertTrue((artifacts.run_dir / "summary.json").exists())
            self.assertTrue((artifacts.run_dir / "summary.csv").exists())
            metadata = json.loads(
                (artifacts.run_dir / "run_metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                metadata["raw_artifact_path"], str(artifacts.run_dir)
            )
            manifest = (
                artifacts.run_dir / "SHA256SUMS.txt"
            ).read_text(encoding="utf-8")
            for line in manifest.splitlines():
                digest, relative = line.split("  ", 1)
                data = (artifacts.run_dir / relative).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)

    def test_existing_run_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            RunArtifacts(Path(tmp), "fake", {}, run_id="same")
            with self.assertRaises(FileExistsError):
                RunArtifacts(Path(tmp), "fake", {}, run_id="same")

    def test_summary_exit_codes_distinguish_invalid_and_setup_runs(self):
        self.assertEqual(_exit_code_for_summary({"run_status": "completed"}), 0)
        self.assertEqual(
            _exit_code_for_summary({"run_status": "setup_error"}), 1
        )
        self.assertEqual(
            _exit_code_for_summary(
                {"run_status": "completed_no_valid_data"}
            ),
            3,
        )
        self.assertEqual(
            _exit_code_for_summary({"run_status": "interrupted"}), 130
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
