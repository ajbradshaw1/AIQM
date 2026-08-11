"""Tests for scripts/pyrometer_active_exactus_query.py.

Covers the whitelist safety guard (Report Version is the only allowed
write), the response-shape classifier, version-response parsing (complete
/ truncated / extra_bytes), --dry-run semantics, evidence bundle contents,
and observational-label invariants (Codex round-4).

No hardware, no serial — ``pyserial`` is stubbed in ``sys.modules`` at
import time via _fake_pyserial (same idiom as
test_pyrometer_force_modbus.py).

    python -m pytest -q tests/test_pyrometer_active_exactus_query.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pyrometer_active_exactus_query import (  # noqa: E402
    ALLOWED_QUERIES,
    BAUD_RATE,
    DEFAULT_PORT,
    DEFAULT_RESPONSE_WINDOW_S,
    REPORT_VERSION_BYTES,
    VERDICT_DRY_RUN,
    VERDICT_ECHO_OF_SENT,
    VERDICT_ECHO_PLUS_EXTRA,
    VERDICT_EXACTUS_RESPONSIVE,
    VERDICT_NON_ECHO_RESPONSE,
    VERDICT_PARTIAL_ECHO,
    VERDICT_PORT_ERROR,
    VERDICT_QUERY_NO_RESPONSE,
    VERSION_RESPONSE_EXPECTED_LEN,
    VERSION_RESPONSE_HEADER,
    VERSION_SHAPE_COMPLETE,
    VERSION_SHAPE_EXTRA_BYTES,
    VERSION_SHAPE_TRUNCATED,
    classify_response,
    classify_version_shape,
    main,
    parse_version_response,
    write_query_and_capture,
)


# ---------------------------------------------------------------------------
# Fake pyserial (same idiom as test_pyrometer_force_modbus.py)
# ---------------------------------------------------------------------------

class _FakeSerial:
    _instances: list["_FakeSerial"] = []
    _echo_bytes: bytes = b""
    _init_should_raise: bool = False

    def __init__(self, port, baudrate, timeout=0.1, **_kwargs):
        cls = type(self)
        if cls._init_should_raise:
            raise RuntimeError("fake serial: init failed")
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.writes: list[bytes] = []
        self.flush_calls = 0
        self.close_calls = 0
        self._echo_buffer = bytearray(cls._echo_bytes)
        cls._instances.append(self)

    def write(self, payload: bytes) -> int:
        self.writes.append(bytes(payload))
        return len(payload)

    def flush(self) -> None:
        self.flush_calls += 1

    def read(self, size: int = 1) -> bytes:
        if not self._echo_buffer:
            return b""
        chunk = bytes(self._echo_buffer[:size])
        del self._echo_buffer[:size]
        return chunk

    def close(self) -> None:
        self.close_calls += 1


@contextmanager
def _fake_pyserial(
    echo_bytes: bytes = b"",
    init_should_raise: bool = False,
):
    _FakeSerial._instances = []
    _FakeSerial._echo_bytes = echo_bytes
    _FakeSerial._init_should_raise = init_should_raise

    fake_serial_mod = types.ModuleType("serial")
    fake_serial_mod.Serial = _FakeSerial
    with patch.dict(sys.modules, {"serial": fake_serial_mod}):
        yield _FakeSerial


# Canonical example response from BASF manual v5.04 page 51:
#   02 95 44 E2 5F 50 2B 10 10 16 73 FF 03
# The 95 indicates application mode; version byte = 0x44; PROM code =
# E2 5F 50 2B 10 10 16 73 FF; ETX = 03. Length = 13.
BASF_EXAMPLE_VERSION_RESPONSE = bytes([
    0x02, 0x95, 0x44, 0xE2, 0x5F, 0x50, 0x2B,
    0x10, 0x10, 0x16, 0x73, 0xFF, 0x03,
])


# ---------------------------------------------------------------------------
# Whitelist / safety invariants
# ---------------------------------------------------------------------------

class WhitelistSafetyTests(unittest.TestCase):
    """The ALLOWED_QUERIES whitelist is the single choke point that
    prevents future edits from adding state-changing commands via this
    script. Guard the whitelist directly."""

    def test_only_report_version_is_allowed(self):
        """Codex round-4 discipline: any addition needs whitelist +
        docstring + this test updated together."""
        self.assertEqual(len(ALLOWED_QUERIES), 1)
        name, payload = ALLOWED_QUERIES[0]
        self.assertEqual(name, "REPORT_VERSION")
        self.assertEqual(payload, REPORT_VERSION_BYTES)

    def test_report_version_bytes_match_basf_manual(self):
        """BASF manual v5.04 page 51:
             Format: STX 0x56 0x56 ETX
             Command Example: 02 56 56 03"""
        self.assertEqual(REPORT_VERSION_BYTES, bytes([0x02, 0x56, 0x56, 0x03]))

    def test_state_changing_bytes_are_not_in_whitelist(self):
        """Regression guard: mode-switch, start/stop, calibration must
        never sneak into ALLOWED_QUERIES."""
        forbidden = (
            bytes([0x02, 0x4D, 0x4D, 0x03]),  # Switch to Modbus (state-change)
            bytes([0x02, 0x30, 0x30, 0x03]),  # Stop Conversions
            bytes([0x02, 0x31, 0x31, 0x03]),  # Start Conversions
        )
        allowed_bytes = tuple(b for _, b in ALLOWED_QUERIES)
        for payload in forbidden:
            self.assertNotIn(payload, allowed_bytes)

    def test_write_query_refuses_non_whitelisted_payload(self):
        """SAFETY: write_query_and_capture raises before any wire touch
        when payload is not in ALLOWED_QUERIES."""
        forbidden = bytes([0x02, 0x4D, 0x4D, 0x03])  # Switch to Modbus
        with _fake_pyserial() as FakeSerial:
            with self.assertRaisesRegex(RuntimeError, "ALLOWED_QUERIES"):
                write_query_and_capture(
                    "COM_FAKE", forbidden, response_window_s=0.0,
                )
            # And no port was opened.
            self.assertEqual(len(FakeSerial._instances), 0)


# ---------------------------------------------------------------------------
# classify_response — pure function
# ---------------------------------------------------------------------------

class ClassifyResponseTests(unittest.TestCase):

    SENT = REPORT_VERSION_BYTES

    def test_empty_received_is_query_no_response(self):
        """Codex round-4: 'query_no_response' not 'passive_silent' —
        we sent bytes; this is active silence."""
        self.assertEqual(
            classify_response(self.SENT, b""),
            VERDICT_QUERY_NO_RESPONSE,
        )

    def test_exact_echo_is_echo_of_sent(self):
        self.assertEqual(
            classify_response(self.SENT, bytes([0x02, 0x56, 0x56, 0x03])),
            VERDICT_ECHO_OF_SENT,
        )

    def test_basf_example_version_response_is_exactus_responsive(self):
        """The whole point of the script — the canonical response from
        BASF manual page 51 gets recognized as exactus_responsive."""
        self.assertEqual(
            classify_response(self.SENT, BASF_EXAMPLE_VERSION_RESPONSE),
            VERDICT_EXACTUS_RESPONSIVE,
        )

    def test_response_starting_02_95_is_exactus_responsive_before_echo_plus(self):
        """A response that starts with 02 95 should NOT be misclassified
        as echo_plus_extra even if it happens to be longer than the sent
        bytes. Classification order matters."""
        # Short 02 95 ... response — deliberately shorter than a full
        # 13-byte version response, but the header uniquely identifies
        # it as an Exactus version reply.
        short = bytes([0x02, 0x95, 0x44, 0x03])
        self.assertEqual(
            classify_response(self.SENT, short),
            VERDICT_EXACTUS_RESPONSIVE,
        )

    def test_echo_plus_trailing_bytes_is_echo_plus_extra(self):
        received = bytes([0x02, 0x56, 0x56, 0x03, 0x06])
        self.assertEqual(
            classify_response(self.SENT, received),
            VERDICT_ECHO_PLUS_EXTRA,
        )

    def test_prefix_of_sent_is_partial_echo(self):
        self.assertEqual(
            classify_response(self.SENT, bytes([0x02, 0x56])),
            VERDICT_PARTIAL_ECHO,
        )

    def test_completely_different_bytes_is_non_echo(self):
        """NAK (0x15) — the manual documents this as the 'invalid
        response' for Report Version. Non-echo shape."""
        self.assertEqual(
            classify_response(self.SENT, bytes([0x15])),
            VERDICT_NON_ECHO_RESPONSE,
        )


# ---------------------------------------------------------------------------
# Version-response parsing — complete / truncated / extra_bytes
# ---------------------------------------------------------------------------

class ParseVersionResponseTests(unittest.TestCase):

    def test_shape_complete_for_exactly_13_bytes(self):
        self.assertEqual(
            classify_version_shape(BASF_EXAMPLE_VERSION_RESPONSE),
            VERSION_SHAPE_COMPLETE,
        )

    def test_shape_truncated_for_shorter_response(self):
        self.assertEqual(
            classify_version_shape(bytes([0x02, 0x95, 0x44, 0x03])),
            VERSION_SHAPE_TRUNCATED,
        )

    def test_shape_extra_bytes_for_longer_response(self):
        padded = BASF_EXAMPLE_VERSION_RESPONSE + bytes([0x00, 0xFF])
        self.assertEqual(
            classify_version_shape(padded),
            VERSION_SHAPE_EXTRA_BYTES,
        )

    def test_parse_extracts_version_and_prom_from_canonical_example(self):
        parsed = parse_version_response(BASF_EXAMPLE_VERSION_RESPONSE)
        self.assertEqual(parsed["shape"], VERSION_SHAPE_COMPLETE)
        # Manual says version byte = 0x44 in this example.
        self.assertEqual(parsed["version_byte"], 0x44)
        # PROM code = 9 bytes after the version byte, before ETX.
        # Manual note: PROM = E25F502B10101673FF (factory use only).
        self.assertEqual(parsed["prom_hex"], "E2 5F 50 2B 10 10 16 73 FF")

    def test_parse_does_not_raise_on_short_response(self):
        """Codex round-4: don't hard-fail on length differences."""
        parsed = parse_version_response(bytes([0x02, 0x95, 0x44]))
        self.assertEqual(parsed["shape"], VERSION_SHAPE_TRUNCATED)
        self.assertEqual(parsed["version_byte"], 0x44)
        # PROM slice is empty because we don't have 9 bytes past the header.
        self.assertEqual(parsed["prom_hex"], "")

    def test_parse_does_not_raise_on_wrong_header(self):
        """Response that doesn't start with 02 95 — parser returns None
        fields without exploding."""
        parsed = parse_version_response(bytes([0x02, 0x56, 0x56, 0x03]))
        self.assertIsNone(parsed["version_byte"])
        self.assertEqual(parsed["prom_hex"], "")


# ---------------------------------------------------------------------------
# Observational-label invariants (Codex round-4)
# ---------------------------------------------------------------------------

class ObservationalLabelTests(unittest.TestCase):

    def test_verdict_labels_do_not_contain_assertive_terms(self):
        """Labels stay descriptive, never causal."""
        assertive_forbidden = (
            "local_echo_detected",
            "probe_ack_confirmed",
            "success",
            "passive_silent",   # Codex: it's not passive; it sent a query
        )
        for label in (VERDICT_EXACTUS_RESPONSIVE, VERDICT_ECHO_OF_SENT,
                      VERDICT_ECHO_PLUS_EXTRA, VERDICT_PARTIAL_ECHO,
                      VERDICT_NON_ECHO_RESPONSE, VERDICT_QUERY_NO_RESPONSE):
            for forbidden in assertive_forbidden:
                self.assertNotIn(
                    forbidden, label,
                    f"Label {label!r} contains forbidden term {forbidden!r} "
                    "— Codex round-4 requires observational labels only.",
                )

    def test_query_no_response_label_is_the_silence_verdict(self):
        """Locking in the specific 'active silence' label per Codex."""
        self.assertEqual(VERDICT_QUERY_NO_RESPONSE, "query_no_response")


# ---------------------------------------------------------------------------
# CLI + main() lifecycle
# ---------------------------------------------------------------------------

class DryRunTests(unittest.TestCase):

    def test_dry_run_opens_no_port(self):
        with _fake_pyserial() as FakeSerial, redirect_stdout(io.StringIO()):
            rc = main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            len(FakeSerial._instances), 0,
            "--dry-run opened a serial port; it must not touch hardware",
        )

    def test_dry_run_prints_the_bytes(self):
        buf = io.StringIO()
        with _fake_pyserial(), redirect_stdout(buf):
            main(["--dry-run"])
        self.assertIn("02 56 56 03", buf.getvalue())

    def test_dry_run_evidence_bundle_records_dry_run_verdict(self):
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()):
            main(["--dry-run", "--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["verdict"]["state"], VERDICT_DRY_RUN)
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["write_error"], "")


class QueryLifecycleTests(unittest.TestCase):

    def test_query_writes_exactly_report_version_bytes(self):
        with _fake_pyserial() as FakeSerial, redirect_stdout(io.StringIO()):
            rc = main([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(FakeSerial._instances), 1)
        inst = FakeSerial._instances[0]
        self.assertEqual(inst.writes, [REPORT_VERSION_BYTES])
        self.assertEqual(inst.port, DEFAULT_PORT)
        self.assertEqual(inst.baudrate, BAUD_RATE)

    def test_port_closed_when_init_raises(self):
        """serial.Serial() constructor raise → verdict=port_error, exit 1."""
        with tempfile.TemporaryDirectory() as tmp, \
                _fake_pyserial(init_should_raise=True), \
                redirect_stdout(io.StringIO()):
            rc = main(["--output-dir", tmp])
            self.assertEqual(rc, 1)
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["verdict"]["state"], VERDICT_PORT_ERROR)
        self.assertIn("fake serial: init failed", data["write_error"])

    def test_exactus_responsive_verdict_and_version_info_when_valid_response(self):
        with tempfile.TemporaryDirectory() as tmp, \
                _fake_pyserial(echo_bytes=BASF_EXAMPLE_VERSION_RESPONSE), \
                redirect_stdout(io.StringIO()):
            main(["--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(
            data["verdict"]["state"], VERDICT_EXACTUS_RESPONSIVE,
        )
        # version_info block is present with parsed fields.
        self.assertIn("version_info", data)
        self.assertEqual(data["version_info"]["shape"], VERSION_SHAPE_COMPLETE)
        self.assertEqual(data["version_info"]["version_byte"], 0x44)
        self.assertEqual(
            data["version_info"]["prom_hex"], "E2 5F 50 2B 10 10 16 73 FF",
        )

    def test_echo_of_sent_verdict_when_response_matches_sent(self):
        with tempfile.TemporaryDirectory() as tmp, \
                _fake_pyserial(echo_bytes=REPORT_VERSION_BYTES), \
                redirect_stdout(io.StringIO()):
            main(["--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["verdict"]["state"], VERDICT_ECHO_OF_SENT)
        # Version info should NOT be present when verdict is echo (not
        # exactus_responsive) — the shape-parser only runs for the
        # 02 95 header case.
        self.assertNotIn("version_info", data)

    def test_query_no_response_verdict_when_silent(self):
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()):
            main(["--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(
            data["verdict"]["state"], VERDICT_QUERY_NO_RESPONSE,
        )
        self.assertEqual(data["response_hex"], "")
        self.assertEqual(data["response_bytes_len"], 0)

    def test_non_state_changing_default_no_gate_required(self):
        """Codex round-4: unlike force_modbus, this script does NOT
        require --i-am-doing-a-write. Bare invocation is legitimate."""
        with _fake_pyserial(), redirect_stdout(io.StringIO()):
            rc = main([])
        self.assertEqual(rc, 0)


class CliFlagTests(unittest.TestCase):

    def test_no_i_am_doing_a_write_flag_exists(self):
        """Regression guard against a future edit adding the state-
        changing script's safety gate to this non-state-changing script."""
        buf = io.StringIO()
        with self.assertRaises(SystemExit), _fake_pyserial(), \
                redirect_stdout(buf):
            main(["--help"])
        out = buf.getvalue()
        self.assertNotIn("--i-am-doing-a-write", out)

    def test_help_mentions_dry_run_and_response_window(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, _fake_pyserial(), \
                redirect_stdout(buf):
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        out = buf.getvalue()
        self.assertIn("--dry-run", out)
        self.assertIn("--response-window-s", out)
        self.assertIn("--output-dir", out)
        # And the description makes the non-state-changing contract clear.
        self.assertIn("Non-state-changing", out)


# ---------------------------------------------------------------------------
# Evidence bundle schema — its own shape (Codex round-3 pattern)
# ---------------------------------------------------------------------------

class EvidenceBundleShapeTests(unittest.TestCase):

    def test_bundle_records_bytes_written_and_verdict(self):
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()):
            main(["--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["bytes_written_hex"], "02 56 56 03")
        self.assertEqual(data["bytes_written_len"], 4)
        self.assertIn("verdict", data)

    def test_bundle_schema_differs_from_other_pyrometer_scripts(self):
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()):
            main(["--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        # Our keys
        self.assertIn("bytes_written_hex", data)
        self.assertIn("response_hex", data)
        self.assertIn("verdict", data)
        # NOT physical_debug's / discover's / force_modbus's specific keys
        self.assertNotIn("phase_1_port", data)
        self.assertNotIn("phase_1_sweep_hits", data)
        self.assertNotIn("response_classification", data)  # force_modbus's field


if __name__ == "__main__":
    unittest.main(verbosity=2)
