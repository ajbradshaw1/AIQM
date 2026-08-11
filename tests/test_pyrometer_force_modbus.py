"""Tests for scripts/pyrometer_force_modbus.py.

Covers the safety gates, the write path, the round-3 echo-capture
addition, evidence bundles, and CLI plumbing. No hardware, no serial —
``pyserial`` is stubbed in ``sys.modules`` at import time so the
discover-script pattern applies (see test_pyrometer_modbus_discover.py
for the same ``_fake_pyserial`` idiom).

    python -m pytest -q tests/test_pyrometer_force_modbus.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pyrometer_force_modbus import (  # noqa: E402
    BAUD_RATE,
    BYTES_EXPECTED_LEN,
    EXACTUS_TO_MODBUS_BYTES,
    NEXT_STEP_COMMAND_TEMPLATE,
    POST_WRITE_ECHO_WINDOW_S,
    POST_WRITE_WAIT_S,
    RESPONSE_ECHO_OF_SENT,
    RESPONSE_ECHO_PLUS_EXTRA,
    RESPONSE_NON_ECHO,
    RESPONSE_NONE,
    RESPONSE_PARTIAL_ECHO,
    VERDICT_DRY_RUN,
    VERDICT_PORT_ERROR,
    VERDICT_SAFETY_DENIED,
    VERDICT_WROTE_NO_RESPONSE,
    VERDICT_WROTE_WITH_RESPONSE,
    classify_response,
    main,
    write_and_capture_echo,
    write_evidence_bundle,
)


# ---------------------------------------------------------------------------
# Fake pyserial installed in sys.modules — same trick as
# test_pyrometer_modbus_discover.py's _fake_pymodbus
# ---------------------------------------------------------------------------

class _FakeSerial:
    """Minimal pyserial.Serial replacement for tests.

    Class-level knobs the test sets BEFORE instantiation:

      * ``_write_should_raise`` — write() raises RuntimeError
      * ``_echo_bytes`` — bytes to return from successive read() calls
        (drains 64 bytes per call, matching the script's read(64) loop)
      * ``_init_should_raise`` — Serial(...) constructor itself raises

    Instances record every operation so tests can assert on them.
    """

    _instances: list["_FakeSerial"] = []
    _write_should_raise: bool = False
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
        # Copy the class-level echo buffer so per-test bytes are per-instance.
        self._echo_buffer = bytearray(cls._echo_bytes)
        cls._instances.append(self)

    def write(self, payload: bytes) -> int:
        if type(self)._write_should_raise:
            raise RuntimeError("fake serial: write failed")
        self.writes.append(bytes(payload))
        return len(payload)

    def flush(self) -> None:
        self.flush_calls += 1

    def read(self, size: int = 1) -> bytes:
        # Drain up to ``size`` bytes from the echo buffer. If empty,
        # return b"" (matches pyserial's behavior when the timeout
        # elapses with no bytes).
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
    write_should_raise: bool = False,
    init_should_raise: bool = False,
):
    """Stub ``sys.modules['serial']`` with a fake module exposing _FakeSerial.

    The force-modbus script does ``import serial`` lazily inside
    ``write_and_capture_echo``. Stubbing here means the lazy import
    resolves to our fake at call time without pyserial being installed
    on CI. Same pattern as _fake_pymodbus.
    """
    _FakeSerial._instances = []
    _FakeSerial._echo_bytes = echo_bytes
    _FakeSerial._write_should_raise = write_should_raise
    _FakeSerial._init_should_raise = init_should_raise

    fake_serial_mod = types.ModuleType("serial")
    fake_serial_mod.Serial = _FakeSerial
    with patch.dict(sys.modules, {"serial": fake_serial_mod}):
        yield _FakeSerial


# ---------------------------------------------------------------------------
# Test 1-2 — Byte constants are the exact BASF/Jacques Exactus ASCII
# ---------------------------------------------------------------------------

class ConstantBytesTests(unittest.TestCase):

    def test_constant_bytes_match_expected(self):
        """Golden-value regression guard against typos (e.g. 0x4E vs 0x4D).

        Source of truth: Jacques's JacquesPyrometerTesting.ipynb cell 3.
          0x02 = STX
          0x4D = 'M'
          0x4D = 'M'  (mnemonic: 'Modbus Mode')
          0x03 = ETX
        """
        self.assertEqual(
            EXACTUS_TO_MODBUS_BYTES,
            bytes([0x02, 0x4D, 0x4D, 0x03]),
        )

    def test_bytes_length_matches_expected(self):
        self.assertEqual(len(EXACTUS_TO_MODBUS_BYTES), BYTES_EXPECTED_LEN)
        self.assertEqual(BYTES_EXPECTED_LEN, 4)


# ---------------------------------------------------------------------------
# Test 3, 10 — Safety gate
# ---------------------------------------------------------------------------

class SafetyGateTests(unittest.TestCase):

    def test_no_flags_exits_with_safety_denied(self):
        """Neither --dry-run nor --i-am-doing-a-write → exit 2, no port open."""
        with _fake_pyserial() as FakeSerial, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = main([])
        self.assertEqual(rc, 2)
        # And CRITICALLY no serial port was opened.
        self.assertEqual(
            len(FakeSerial._instances), 0,
            "Serial port was opened despite safety-denied — the gate leaks!",
        )

    def test_safety_gate_message_names_both_accepted_flags(self):
        """Error message MUST explicitly mention both --dry-run and
        --i-am-doing-a-write so the operator knows their options."""
        stderr = io.StringIO()
        with _fake_pyserial(), redirect_stdout(io.StringIO()), \
                redirect_stderr(stderr):
            main([])
        err = stderr.getvalue()
        self.assertIn("--dry-run", err)
        self.assertIn("--i-am-doing-a-write", err)
        self.assertIn("SAFETY DENIED", err)

    def test_safety_gate_writes_evidence_bundle_when_output_dir_set(self):
        """Even a denied run leaves a paste-back trail if --output-dir is set.

        Useful for auditing: "did the operator try to run this without
        the ack?" The bundle records verdict=safety_denied.
        """
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(["--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
            self.assertEqual(data["verdict"]["state"], VERDICT_SAFETY_DENIED)


# ---------------------------------------------------------------------------
# Test 4 — Dry-run
# ---------------------------------------------------------------------------

class DryRunTests(unittest.TestCase):

    def test_dry_run_opens_no_port(self):
        """--dry-run alone → exit 0, no serial port opened."""
        with _fake_pyserial() as FakeSerial, redirect_stdout(io.StringIO()):
            rc = main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            len(FakeSerial._instances), 0,
            "--dry-run opened a serial port; it must not touch hardware",
        )

    def test_dry_run_prints_the_bytes(self):
        """--dry-run stdout includes '02 4D 4D 03' so the operator sees
        exactly what would be sent."""
        buf = io.StringIO()
        with _fake_pyserial(), redirect_stdout(buf):
            main(["--dry-run"])
        self.assertIn("02 4D 4D 03", buf.getvalue())


# ---------------------------------------------------------------------------
# Test 5 — Write path sends EXACTLY the expected bytes
# ---------------------------------------------------------------------------

class WritePathTests(unittest.TestCase):

    def test_write_path_sends_exactly_expected_bytes(self):
        """--i-am-doing-a-write path writes 02 4D 4D 03, one call, no more."""
        with _fake_pyserial() as FakeSerial, redirect_stdout(io.StringIO()):
            rc = main(["--i-am-doing-a-write", "--port", "COM_FAKE"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(FakeSerial._instances), 1)
        inst = FakeSerial._instances[0]
        self.assertEqual(inst.writes, [EXACTUS_TO_MODBUS_BYTES])
        self.assertEqual(inst.port, "COM_FAKE")
        self.assertEqual(inst.baudrate, BAUD_RATE)


# ---------------------------------------------------------------------------
# Test 6 — Port is closed even if write raises
# ---------------------------------------------------------------------------

class PortCloseOnExceptionTests(unittest.TestCase):

    def test_port_closed_when_write_raises(self):
        """A wedged port after a crash means discover can't run afterward.

        The try/finally in write_and_capture_echo guards this; the test
        locks it in.
        """
        with _fake_pyserial(write_should_raise=True) as FakeSerial, \
                redirect_stdout(io.StringIO()):
            rc = main(["--i-am-doing-a-write"])
        # Write raised, so exit is 1 (port_error), not 0.
        self.assertEqual(rc, 1)
        self.assertEqual(len(FakeSerial._instances), 1)
        # But close was called — port is not wedged.
        self.assertEqual(FakeSerial._instances[0].close_calls, 1)

    def test_port_error_verdict_recorded_when_init_raises(self):
        """serial.Serial() constructor raise → verdict=port_error."""
        with tempfile.TemporaryDirectory() as tmp, \
                _fake_pyserial(init_should_raise=True), \
                redirect_stdout(io.StringIO()):
            rc = main([
                "--i-am-doing-a-write",
                "--output-dir", tmp,
            ])
            self.assertEqual(rc, 1)
            data = json.loads((Path(tmp) / "result.json").read_text())
            self.assertEqual(data["verdict"]["state"], VERDICT_PORT_ERROR)
            self.assertIn("fake serial: init failed", data["write_error"])


# ---------------------------------------------------------------------------
# Test 7-8 — Evidence bundle contents
# ---------------------------------------------------------------------------

class EvidenceBundleTests(unittest.TestCase):

    def test_evidence_bundle_records_bytes_hex(self):
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()):
            main(["--i-am-doing-a-write", "--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
            self.assertEqual(data["bytes_written_hex"], "02 4D 4D 03")
            self.assertEqual(data["bytes_written_len"], BYTES_EXPECTED_LEN)

    def test_evidence_bundle_records_dry_run_verdict(self):
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()):
            main(["--dry-run", "--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
            self.assertEqual(data["verdict"]["state"], VERDICT_DRY_RUN)
            self.assertTrue(data["dry_run"])
            # And no write_error since we didn't touch the port.
            self.assertEqual(data["write_error"], "")

    def test_bundle_schema_differs_from_other_pyrometer_scripts(self):
        """This bundle has its own shape: bytes_written_hex / response_hex /
        dry_run / verdict. Not physical_debug's phase_N_* or discover's
        sweep_hits/misses. Assert we designed for it explicitly."""
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()):
            main(["--i-am-doing-a-write", "--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertIn("bytes_written_hex", data)
        self.assertIn("response_hex", data)
        self.assertIn("dry_run", data)
        self.assertIn("verdict", data)
        self.assertNotIn("phase_1_port", data)
        self.assertNotIn("phase_1_sweep_hits", data)


# ---------------------------------------------------------------------------
# Test 9 — --verify prints the next-step command
# ---------------------------------------------------------------------------

class VerifyFlagTests(unittest.TestCase):

    def test_verify_flag_prints_next_command(self):
        """--verify → next-step invocation printed verbatim, do NOT run it."""
        buf = io.StringIO()
        with _fake_pyserial(), redirect_stdout(buf):
            main(["--i-am-doing-a-write", "--verify"])
        out = buf.getvalue()
        self.assertIn("pyrometer_modbus_discover.py", out)
        self.assertIn(NEXT_STEP_COMMAND_TEMPLATE, out)

    def test_verify_flag_works_with_dry_run(self):
        """--verify + --dry-run → both are OK; hint appears; no port open."""
        buf = io.StringIO()
        with _fake_pyserial() as FakeSerial, redirect_stdout(buf):
            rc = main(["--dry-run", "--verify"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(FakeSerial._instances), 0)
        self.assertIn("pyrometer_modbus_discover.py", buf.getvalue())


# ---------------------------------------------------------------------------
# Round-3 additions: Echo capture (Codex round 3 — diagnostic-only, NOT success)
# ---------------------------------------------------------------------------

class EchoCaptureTests(unittest.TestCase):

    def test_echo_capture_records_response_hex_when_bytes_received(self):
        """Simulated echo → response_hex populated, verdict=wrote_with_response."""
        # 4 bytes echoed back — matches the pattern Jacques observed.
        with tempfile.TemporaryDirectory() as tmp, \
                _fake_pyserial(echo_bytes=bytes([0x02, 0x4D, 0x4D, 0x03])), \
                redirect_stdout(io.StringIO()):
            main([
                "--i-am-doing-a-write",
                "--output-dir", tmp,
            ])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["response_hex"], "02 4D 4D 03")
        self.assertEqual(data["response_bytes_len"], 4)
        self.assertEqual(data["verdict"]["state"], VERDICT_WROTE_WITH_RESPONSE)

    def test_echo_capture_records_empty_response_hex_when_silent(self):
        """No echo (silence) → response_hex='', verdict=wrote_no_response.

        Matches BASF Exactus 'no response expected' documentation for
        the mode-switch commands."""
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()):
            main([
                "--i-am-doing-a-write",
                "--output-dir", tmp,
            ])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["response_hex"], "")
        self.assertEqual(data["response_bytes_len"], 0)
        self.assertEqual(data["verdict"]["state"], VERDICT_WROTE_NO_RESPONSE)

    def test_echo_capture_never_flags_success(self):
        """Regardless of echo bytes, exit code is 0 for 'write completed'.

        Success is discover's job — this script's exit code just says
        'the write phase ran without exploding.' A future edit that
        makes exit code depend on response_bytes_len WILL break this
        test — which is the point.
        """
        # With echo:
        with _fake_pyserial(echo_bytes=b"\x02MM\x03"), \
                redirect_stdout(io.StringIO()):
            rc_with_echo = main(["--i-am-doing-a-write"])
        # Without echo:
        with _fake_pyserial(), redirect_stdout(io.StringIO()):
            rc_no_echo = main(["--i-am-doing-a-write"])
        self.assertEqual(rc_with_echo, 0)
        self.assertEqual(rc_no_echo, 0)


# ---------------------------------------------------------------------------
# Codex round-4: response_classification labels (observational, not causal)
# ---------------------------------------------------------------------------

class ClassifyResponsePureTests(unittest.TestCase):
    """Pure-function tests for ``classify_response``.

    The labels describe the SHAPE of what came back on the wire — no
    causation, no interpretation. Downstream (SOP) does the interpreting.
    """

    SENT = bytes([0x02, 0x4D, 0x4D, 0x03])

    def test_empty_received_is_no_response(self):
        self.assertEqual(classify_response(self.SENT, b""), RESPONSE_NONE)

    def test_exact_echo_is_echo_of_sent(self):
        self.assertEqual(
            classify_response(self.SENT, bytes([0x02, 0x4D, 0x4D, 0x03])),
            RESPONSE_ECHO_OF_SENT,
        )

    def test_echo_plus_trailing_bytes_is_echo_plus_extra(self):
        """Echo followed by extra bytes — could be echo + probe ACK or
        echo + noise. Label is observational only."""
        received = bytes([0x02, 0x4D, 0x4D, 0x03, 0x06])  # echo + ACK-shaped
        self.assertEqual(
            classify_response(self.SENT, received),
            RESPONSE_ECHO_PLUS_EXTRA,
        )

    def test_partial_prefix_of_sent_is_partial_echo(self):
        """Shorter response that matches a prefix of the sent bytes."""
        self.assertEqual(
            classify_response(self.SENT, bytes([0x02, 0x4D])),
            RESPONSE_PARTIAL_ECHO,
        )

    def test_completely_different_bytes_is_non_echo(self):
        """Real probe response for Report Version (BASF manual page 51)
        would start with `02 95 ...` — a totally different shape."""
        received = bytes([0x02, 0x95, 0x44, 0xE2, 0x5F, 0x50, 0x2B,
                          0x10, 0x10, 0x16, 0x73, 0xFF, 0x03])
        self.assertEqual(
            classify_response(self.SENT, received),
            RESPONSE_NON_ECHO,
        )

    def test_single_ack_byte_is_non_echo(self):
        """ACK (0x06) is a shape mismatch, not a prefix of what we sent."""
        self.assertEqual(
            classify_response(self.SENT, bytes([0x06])),
            RESPONSE_NON_ECHO,
        )

    def test_labels_are_observational_not_assertive(self):
        """Guard against future edits that rename to causal labels
        (e.g. `local_echo_detected`, `probe_ack_confirmed`). Codex
        round-4: labels stay descriptive."""
        assertive_forbidden = (
            "local_echo_detected",
            "probe_ack_confirmed",
            "probe_response",
            "success",
        )
        for label in (RESPONSE_NONE, RESPONSE_ECHO_OF_SENT,
                      RESPONSE_ECHO_PLUS_EXTRA, RESPONSE_PARTIAL_ECHO,
                      RESPONSE_NON_ECHO):
            for forbidden in assertive_forbidden:
                self.assertNotIn(
                    forbidden, label,
                    f"Label {label!r} contains assertive term "
                    f"{forbidden!r} — Codex round-4 requires "
                    "observational labels only.",
                )


class ResponseClassificationEvidenceBundleTests(unittest.TestCase):
    """The evidence bundle MUST record the classification alongside the
    raw hex so paste-back reviewers see both the observation and the
    shape-label without having to re-parse the hex."""

    def test_bundle_records_echo_of_sent_when_response_equals_sent(self):
        with tempfile.TemporaryDirectory() as tmp, \
                _fake_pyserial(echo_bytes=bytes([0x02, 0x4D, 0x4D, 0x03])), \
                redirect_stdout(io.StringIO()):
            main(["--i-am-doing-a-write", "--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["response_classification"], RESPONSE_ECHO_OF_SENT)

    def test_bundle_records_no_response_when_silent(self):
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()):
            main(["--i-am-doing-a-write", "--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["response_classification"], RESPONSE_NONE)

    def test_bundle_records_echo_plus_extra(self):
        """Received bytes start with sent bytes but contain more after."""
        with tempfile.TemporaryDirectory() as tmp, \
                _fake_pyserial(echo_bytes=bytes([0x02, 0x4D, 0x4D, 0x03, 0x06])), \
                redirect_stdout(io.StringIO()):
            main(["--i-am-doing-a-write", "--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(
            data["response_classification"], RESPONSE_ECHO_PLUS_EXTRA,
        )

    def test_bundle_records_non_echo_when_report_version_response_shape(self):
        """If the probe were in Exactus mode and the write happened to
        elicit a version-string-shaped response (hypothetically), the
        classifier should call it non_echo — not conflate it with echo."""
        version_shaped = bytes([
            0x02, 0x95, 0x44, 0xE2, 0x5F, 0x50, 0x2B,
            0x10, 0x10, 0x16, 0x73, 0xFF, 0x03,
        ])
        with tempfile.TemporaryDirectory() as tmp, \
                _fake_pyserial(echo_bytes=version_shaped), \
                redirect_stdout(io.StringIO()):
            main(["--i-am-doing-a-write", "--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["response_classification"], RESPONSE_NON_ECHO)

    def test_bundle_omits_classification_on_dry_run(self):
        """Dry-run never touches the port → classification is empty string."""
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()):
            main(["--dry-run", "--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["response_classification"], "")

    def test_bundle_omits_classification_on_safety_denied(self):
        """Safety-denied run writes the bundle but no write happened."""
        with tempfile.TemporaryDirectory() as tmp, _fake_pyserial(), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(["--output-dir", tmp])
            data = json.loads((Path(tmp) / "result.json").read_text())
        self.assertEqual(data["response_classification"], "")


# ---------------------------------------------------------------------------
# write_and_capture_echo — direct unit test (bypasses argparse / CLI)
# ---------------------------------------------------------------------------

class WriteAndCaptureEchoUnitTests(unittest.TestCase):

    def test_write_returns_success_dict_when_silent(self):
        with _fake_pyserial():
            r = write_and_capture_echo(
                "COM_FAKE", EXACTUS_TO_MODBUS_BYTES,
                post_write_wait_s=0.0, echo_window_s=0.0,
            )
        self.assertTrue(r["opened"])
        self.assertEqual(r["bytes_written"], BYTES_EXPECTED_LEN)
        self.assertEqual(r["response_hex"], "")
        self.assertEqual(r["error"], "")

    def test_write_returns_error_dict_when_pyserial_missing(self):
        """If pyserial isn't installed, error is populated (no crash).

        On CI pyserial IS missing; on Mac dev it's installed. Either way
        we simulate the missing-pyserial condition by patching
        ``builtins.__import__`` to raise ImportError for ``serial``. This
        makes the test env-independent — the ImportError branch in
        ``write_and_capture_echo`` gets exercised regardless of what's
        actually installed.
        """
        import builtins
        real_import = builtins.__import__

        def blocking_import(name, *args, **kwargs):
            if name == "serial" or name.startswith("serial."):
                raise ImportError("simulated: pyserial not installed")
            return real_import(name, *args, **kwargs)

        # Ensure any cached 'serial' module is popped so the import
        # actually re-executes and hits our blocker.
        original = sys.modules.pop("serial", None)
        try:
            with patch.object(builtins, "__import__", blocking_import):
                r = write_and_capture_echo(
                    "COM_FAKE", EXACTUS_TO_MODBUS_BYTES,
                    post_write_wait_s=0.0, echo_window_s=0.0,
                )
        finally:
            if original is not None:
                sys.modules["serial"] = original
        self.assertFalse(r["opened"])
        self.assertIn("pyserial not installed", r["error"])

    def test_wrong_payload_length_raises_before_opening_port(self):
        """assert-len guard fires BEFORE the port is opened."""
        with self.assertRaises(AssertionError), _fake_pyserial() as FakeSerial:
            write_and_capture_echo(
                "COM_FAKE", bytes([0x02, 0x4D]),  # wrong length
                post_write_wait_s=0.0, echo_window_s=0.0,
            )
        # And crucially, no port was opened.
        self.assertEqual(len(FakeSerial._instances), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
