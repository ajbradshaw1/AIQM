#!/usr/bin/env python3
"""Tests for scripts/pyrometer_physical_debug.py.

Covers the READ-ONLY safety invariants + the verdict-logic decision tree.
Does NOT touch pyserial / pymodbus — that's on-hardware territory.

Runs on Mac dev env with no lab dependencies:
    python -m pytest -q tests/test_pyrometer_physical_debug.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pyrometer_physical_debug import (  # noqa: E402
    CANDIDATE_MODBUS_BAUDS,
    CONFIG_REGISTERS_FORBIDDEN,
    EXACTUS_HEADER,
    READ_ONLY_MODBUS_FUNCTIONS,
    _parse_exactus_samples,
    _probe_modbus_read,
    build_verdict,
    describe_conflicting_processes,
    list_available_ports,
    main,
    write_evidence_bundle,
)


# ---------------------------------------------------------------------------
# READ-ONLY safety — these constraints must hold as long as the script's
# module docstring claims "READ-ONLY."
# ---------------------------------------------------------------------------

class ReadOnlySafetyTests(unittest.TestCase):

    def test_allowlist_matches_what_script_actually_uses(self):
        """READ_ONLY_MODBUS_FUNCTIONS reflects only what the script actually calls.

        Codex Jul 28 review: the previous version listed 0x03 and 0x04
        but only ever called 0x03. Keep the allowlist tight so a future
        editor who wants to use 0x04 (Read Input Registers) has to think
        about it — or add 0x04 back when they add a real probe for it.
        """
        self.assertEqual(READ_ONLY_MODBUS_FUNCTIONS, (0x03,))

    def test_no_write_function_codes(self):
        """Modbus RTU §7 write functions must never be in the allowlist."""
        for fc in (0x05, 0x06, 0x0F, 0x10):
            self.assertNotIn(
                fc, READ_ONLY_MODBUS_FUNCTIONS,
                f"Write function 0x{fc:02X} must not be in the allowlist",
            )

    def test_config_registers_forbidden_covers_bricking_targets(self):
        """The forbidden list includes every config register we can identify.

        REG_ADDR (0x1007), REG_BAUD (0x1008), REG_RATE (0x1011), and
        REG_CMD (0x8000) are the four addresses where a mistaken write
        could permanently misconfigure or brick the device. Keeping them
        out of the probe path prevents an accidental future edit from
        turning a diagnostic into a device-reprogrammer.
        """
        for reg in (0x1007, 0x1008, 0x1011, 0x8000):
            self.assertIn(
                reg, CONFIG_REGISTERS_FORBIDDEN,
                f"Register 0x{reg:04X} must stay in CONFIG_REGISTERS_FORBIDDEN",
            )

    def test_probe_refuses_forbidden_register(self):
        """_probe_modbus_read raises before hitting the wire for banned addrs."""
        class ExplodingClient:
            def read_holding_registers(self, *_args, **_kwargs):
                raise AssertionError(
                    "SAFETY VIOLATION: probe reached the wire for a "
                    "forbidden address"
                )
        for reg in CONFIG_REGISTERS_FORBIDDEN:
            with self.assertRaises(RuntimeError) as ctx:
                _probe_modbus_read(ExplodingClient(), reg, 1, device_id=1)
            self.assertIn(
                "SAFETY", str(ctx.exception),
                f"Expected SAFETY in error for 0x{reg:04X}, got: {ctx.exception}",
            )

    def test_probe_captures_pymodbus_exception_gracefully(self):
        """pymodbus runtime exceptions (e.g. ModbusIOException on timeout)
        must be captured into result['error'] and NOT propagate up.

        Regression: Bulbasaur run 2026-07-28 crashed with an unhandled
        ModbusIOException on a silent device, aborting phase_modbus_probe
        before the operator saw the verdict block.
        """
        class TimingOutClient:
            """Mimics pymodbus raising ModbusIOException on a silent device."""
            def read_holding_registers(self, *_args, **_kwargs):
                raise RuntimeError(
                    "Modbus Error: [Input/Output] No response received "
                    "after 3 retries"
                )
        # Use a non-forbidden address (REG_VER 0x1300) so we exercise the
        # actual read path, not the SAFETY short-circuit.
        result = _probe_modbus_read(TimingOutClient(), 0x1300, 1, device_id=1)
        self.assertFalse(result["response"])
        self.assertIn("RuntimeError", result["error"])
        self.assertIn("No response received", result["error"])
        # And the caller receives a dict, not an exception
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# Verdict logic — given the raw phase outputs, does the state classifier
# pick the right label and produce an actionable recovery hint?
# ---------------------------------------------------------------------------

class VerdictLogicTests(unittest.TestCase):

    def test_port_down_verdict(self):
        state, hint = build_verdict(
            port_open=False, exactus={}, modbus_results=[],
        )
        self.assertEqual(state, "port_down")
        self.assertIn("IFD-5", hint)

    def test_exactus_streaming_verdict_be_only(self):
        """Exactus streaming with only BE decodes plausible → hint mentions BE values."""
        exactus = {
            "bytes_received": 100,
            "exactus_headers": 15,
            "sample_temperatures": [
                {"offset": 5, "be": 450.10, "le": 1.2e-38,
                 "be_plausible": True, "le_plausible": False},
            ],
        }
        state, hint = build_verdict(True, exactus, [])
        self.assertEqual(state, "exactus_streaming")
        self.assertIn("Exactus", hint)
        self.assertIn("450.1", hint)
        # LE should NOT be highlighted when BE alone is plausible
        self.assertNotIn("little-endian", hint.lower())

    def test_exactus_streaming_verdict_le_only_flags_endianness(self):
        """LE-only plausible → hint should flag that the driver may be wrong."""
        exactus = {
            "bytes_received": 100,
            "exactus_headers": 15,
            "sample_temperatures": [
                {"offset": 5, "be": 1e30, "le": 450.10,
                 "be_plausible": False, "le_plausible": True},
            ],
        }
        state, hint = build_verdict(True, exactus, [])
        self.assertEqual(state, "exactus_streaming")
        self.assertIn("450.1", hint)
        self.assertIn("little-endian", hint.lower())

    def test_exactus_streaming_verdict_both_plausible_flags_ambiguity(self):
        """Both endians plausible → hint flags need for cross-check."""
        exactus = {
            "bytes_received": 100,
            "exactus_headers": 15,
            "sample_temperatures": [
                {"offset": 5, "be": 20.5, "le": 100.2,
                 "be_plausible": True, "le_plausible": True},
            ],
        }
        state, hint = build_verdict(True, exactus, [])
        self.assertEqual(state, "exactus_streaming")
        self.assertIn("BOTH endians", hint)
        self.assertIn("cross-check", hint.lower())

    def test_modbus_responsive_verdict(self):
        modbus_results = [
            {"baud": 115200, "response": True, "register_name": "REG_VER"},
            {"baud": 115200, "response": True, "register_name": "REG_NAME0"},
        ]
        exactus = {"bytes_received": 0, "exactus_headers": 0}
        state, hint = build_verdict(True, exactus, modbus_results)
        self.assertEqual(state, "modbus_responsive")
        self.assertIn("115200", hint)

    def test_silent_verdict(self):
        state, hint = build_verdict(
            True,
            {"bytes_received": 0, "exactus_headers": 0},
            [],
        )
        self.assertEqual(state, "silent")
        # Hint must guide toward physical checks
        for keyword in ("power", "IFD-5", "TemperaSure"):
            self.assertIn(keyword, hint,
                          f"Silent-state hint should mention {keyword!r}")

    def test_transmitting_unknown_verdict(self):
        # Bytes seen but no Exactus headers and no Modbus response.
        exactus = {
            "bytes_received": 50, "exactus_headers": 0,
            "sample_temperatures": [],
        }
        state, hint = build_verdict(True, exactus, [])
        self.assertEqual(state, "transmitting_unknown")
        self.assertIn("baud", hint.lower())

    def test_both_active_flagged_unusual(self):
        exactus = {
            "bytes_received": 100, "exactus_headers": 15,
            "sample_temperatures": [],
        }
        modbus = [{
            "baud": 115200, "response": True, "register_name": "REG_VER",
        }]
        state, hint = build_verdict(True, exactus, modbus)
        self.assertEqual(state, "both_active_unexpected")
        self.assertIn("unusual", hint.lower())

    def test_modbus_reports_only_bauds_that_actually_responded(self):
        """The recovery hint must not falsely list a baud that had no response."""
        modbus_results = [
            {"baud": 115200, "response": False, "register_name": "REG_VER"},
            {"baud": 19200, "response": True, "register_name": "REG_VER"},
            {"baud": 19200, "response": True, "register_name": "REG_NAME0"},
        ]
        state, hint = build_verdict(
            True, {"bytes_received": 0, "exactus_headers": 0}, modbus_results,
        )
        self.assertEqual(state, "modbus_responsive")
        self.assertIn("19200", hint)
        self.assertNotIn("115200", hint)


# ---------------------------------------------------------------------------
# CLI argparse behavior
# ---------------------------------------------------------------------------

class CLITests(unittest.TestCase):

    def test_help_does_not_crash(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_non_numeric_listen_time_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["--exactus-listen-s", "not-a-number"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_non_integer_device_id_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["--device-id", "banana"])
        self.assertNotEqual(ctx.exception.code, 0)


# ---------------------------------------------------------------------------
# Baud sweep configuration — verifies our candidate list is sensible.
# ---------------------------------------------------------------------------

class BaudCandidatesTests(unittest.TestCase):

    def test_115200_is_first(self):
        """Bulbasaur default matches the driver default; sweep it first."""
        self.assertEqual(CANDIDATE_MODBUS_BAUDS[0], 115200)

    def test_candidates_are_all_standard_uart_rates(self):
        for baud in CANDIDATE_MODBUS_BAUDS:
            self.assertIn(baud, (300, 1200, 2400, 4800, 9600, 19200,
                                 38400, 57600, 115200))


# ---------------------------------------------------------------------------
# Dual-endian Exactus parsing (Codex Jul 28 review addition)
# ---------------------------------------------------------------------------

class ExactusSamplesParsingTests(unittest.TestCase):

    def _make_be_packet(self, temp_c: float) -> bytes:
        import struct
        return bytes([EXACTUS_HEADER]) + struct.pack(">f", temp_c)

    def _make_le_packet(self, temp_c: float) -> bytes:
        import struct
        return bytes([EXACTUS_HEADER]) + struct.pack("<f", temp_c)

    def test_parses_big_endian_sample(self):
        buffer = self._make_be_packet(450.5)
        samples = _parse_exactus_samples(buffer)
        self.assertEqual(len(samples), 1)
        self.assertAlmostEqual(samples[0]["be"], 450.5, places=2)
        self.assertTrue(samples[0]["be_plausible"])
        # The LE decode of BE bytes is almost always garbage — verify
        # the flag correctly reports it as not plausible.
        self.assertFalse(samples[0]["le_plausible"])

    def test_parses_little_endian_sample(self):
        buffer = self._make_le_packet(450.5)
        samples = _parse_exactus_samples(buffer)
        self.assertEqual(len(samples), 1)
        # The BE decode of LE bytes is garbage; LE decode is the real value.
        self.assertFalse(samples[0]["be_plausible"])
        self.assertTrue(samples[0]["le_plausible"])
        self.assertAlmostEqual(samples[0]["le"], 450.5, places=2)

    def test_stops_at_max_samples(self):
        # 10 valid BE packets, cap = 5
        buffer = b"".join(self._make_be_packet(400.0 + i) for i in range(10))
        samples = _parse_exactus_samples(buffer, max_samples=5)
        self.assertEqual(len(samples), 5)

    def test_skips_implausible_samples(self):
        # 0x81 followed by all-zeros → BE decode = 0.0 which is plausible
        # so this test uses bytes that give implausible values for both.
        # 0x81 followed by 0x7F 0x80 0x00 0x00 → BE decode = +infinity,
        # LE decode = 4.6e-41. Neither plausible.
        buffer = bytes([EXACTUS_HEADER, 0x7F, 0x80, 0x00, 0x00])
        samples = _parse_exactus_samples(buffer)
        self.assertEqual(len(samples), 0)

    def test_empty_buffer(self):
        self.assertEqual(_parse_exactus_samples(b""), [])


# ---------------------------------------------------------------------------
# Evidence bundle writer (Codex Jul 28 review addition)
# ---------------------------------------------------------------------------

class EvidenceBundleTests(unittest.TestCase):

    def test_writes_result_json_always(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence"
            written = write_evidence_bundle(
                out,
                args_summary={"port": "COM4"},
                port_open=True,
                port_detail="opened OK",
                environment={"available_ports": []},
                exactus={"bytes_received": 0, "exactus_headers": 0,
                         "sample_temperatures": []},
                modbus_results=[],
                state="silent",
                hint="check power",
            )
            self.assertIn("result.json", written)
            self.assertTrue(written["result.json"].exists())
            data = json.loads(written["result.json"].read_text())
            self.assertEqual(data["verdict"]["state"], "silent")
            self.assertEqual(data["args"]["port"], "COM4")

    def test_writes_raw_sniff_only_when_bytes_captured(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            # No _raw_bytes → no raw_sniff.bin written
            written = write_evidence_bundle(
                out, {}, True, "ok", {},
                {"bytes_received": 0, "exactus_headers": 0,
                 "sample_temperatures": []},
                [], "silent", "hint",
            )
            self.assertNotIn("raw_sniff.bin", written)
            self.assertFalse((out / "raw_sniff.bin").exists())

    def test_writes_raw_sniff_when_bytes_present(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            raw = b"\x81\x00\x00\xE1\x00\x81\x00\x00\xE1\x01"
            written = write_evidence_bundle(
                out, {}, True, "ok", {},
                {"bytes_received": len(raw), "exactus_headers": 2,
                 "sample_temperatures": [], "_raw_bytes": raw},
                [], "exactus_streaming", "hint",
            )
            self.assertIn("raw_sniff.bin", written)
            self.assertEqual(written["raw_sniff.bin"].read_bytes(), raw)

    def test_raw_bytes_key_not_in_json(self):
        """bytes aren't JSON-safe; the writer must pop _raw_bytes before serialization."""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            raw = b"\x81\x00\x00\xE1\x00"
            written = write_evidence_bundle(
                out, {}, True, "ok", {},
                {"bytes_received": 5, "exactus_headers": 1,
                 "sample_temperatures": [], "_raw_bytes": raw},
                [], "exactus_streaming", "hint",
            )
            data = json.loads(written["result.json"].read_text())
            self.assertNotIn("_raw_bytes", data["phase_2_exactus"])


# ---------------------------------------------------------------------------
# Environment helpers — smoke-level (they use external subprocess/pyserial)
# ---------------------------------------------------------------------------

class EnvironmentHelpersTests(unittest.TestCase):

    def test_list_available_ports_returns_list_no_crash(self):
        ports = list_available_ports()
        self.assertIsInstance(ports, list)
        for p in ports:
            # Each entry has the three expected keys
            self.assertIn("device", p)
            self.assertIn("description", p)
            self.assertIn("hwid", p)

    def test_describe_conflicting_processes_returns_list_no_crash(self):
        # On Mac dev env this returns [] (non-Windows shortcut).
        # On Windows dev env it returns whatever tasklist finds; empty list
        # is a valid result. Either way, no exception.
        result = describe_conflicting_processes()
        self.assertIsInstance(result, list)


# ---------------------------------------------------------------------------
# Extended CLI test — --output-dir argument
# ---------------------------------------------------------------------------

class OutputDirCLITests(unittest.TestCase):

    def test_output_dir_accepted(self):
        """--output-dir should be a valid argparse choice (parse-only test)."""
        # We don't want to invoke a real hardware run here; test only the
        # argparse layer by using --help which exits before phase 1.
        with self.assertRaises(SystemExit) as ctx:
            main(["--output-dir", "/tmp/evidence", "--help"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    print("Running pyrometer_physical_debug tests...\n")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().discover(
        start_dir=str(Path(__file__).parent),
        pattern="test_pyrometer_physical_debug.py",
    ))
    sys.exit(0 if result.wasSuccessful() else 1)
