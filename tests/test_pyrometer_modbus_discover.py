"""Tests for scripts/pyrometer_modbus_discover.py.

Covers READ-ONLY safety invariants, sweep structure (open-once-per-baud,
early-stop semantics, no forbidden-register reads), verdict-logic, and
the two-file test convention from test_pyrometer_physical_debug.py.

No hardware, no serial, no pymodbus reads on the wire — every serial
touchpoint is mocked. Runs from Mac dev env:

    python -m pytest -q tests/test_pyrometer_modbus_discover.py
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
from typing import Optional
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pyrometer_modbus_discover import (  # noqa: E402
    CANDIDATE_BAUDS,
    CANDIDATE_DEVICE_IDS,
    CONFIG_REGISTERS_FORBIDDEN,
    DEFAULT_COMBO,
    LIGHTWEIGHT_PROBE_COUNT,
    LIGHTWEIGHT_PROBE_REG,
    READ_ONLY_MODBUS_FUNCTIONS,
    REG_CH1_TEMP,
    REG_NAME0,
    REG_SN0,
    REG_VER,
    _probe_modbus_read,
    build_verdict,
    main,
    sweep_modbus,
    write_evidence_bundle,
)


# ---------------------------------------------------------------------------
# Test doubles — mock ModbusSerialClient without pulling pymodbus
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal pymodbus-like read response."""
    def __init__(self, registers: list[int] | None, error: bool = False):
        self.registers = registers if registers is not None else []
        self._error = error

    def isError(self) -> bool:  # noqa: N802 — mirrors pymodbus API
        return self._error


class _FakeClient:
    """In-memory replacement for ``pymodbus.client.ModbusSerialClient``.

    Behavior controlled by ``responses`` — a dict keyed by
    ``(baudrate, device_id, addr)`` returning the ``list[int]`` that the
    device would answer with. Absence of a key → simulated no-response.
    Records every ``read_holding_registers`` call for assertions.
    """

    # Class-level counter so tests can verify how many client instances
    # were constructed across a sweep. Reset by tests via patching.
    _init_count = 0

    def __init__(self, port, baudrate, timeout, bytesize, parity, stopbits):
        type(self)._init_count += 1
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.connect_returns = True
        self.close_calls = 0
        self.read_calls: list[dict] = []
        # responses is patched by test setup (class attr shared across all
        # FakeClient instances in a given test).
        self.responses = getattr(type(self), "_responses", {})

    def connect(self) -> bool:
        return self.connect_returns

    def close(self) -> None:
        self.close_calls += 1

    def read_holding_registers(
        self, addr, count, device_id=None, slave=None, unit=None,
    ):
        # Normalize the version-drift kwarg into device_id.
        dev = device_id if device_id is not None else (
            slave if slave is not None else unit
        )
        self.read_calls.append({
            "baudrate": self.baudrate, "device_id": dev,
            "addr": addr, "count": count,
        })
        key = (self.baudrate, dev, addr)
        if key in self.responses:
            regs = self.responses[key]
            return _FakeResponse(regs)
        return _FakeResponse(None, error=False)  # empty regs → miss


@contextmanager
def _fake_pymodbus(responses: dict):
    """Install a fake ``pymodbus.client`` module in ``sys.modules``.

    The discover script does ``from pymodbus.client import ModbusSerialClient``
    lazily inside ``sweep_modbus``. On CI (and any Mac env without
    pymodbus installed) that import fails, and ``patch("pymodbus.client...")``
    also fails because the real module doesn't exist to patch. Instead we
    stub the module hierarchy in ``sys.modules`` so the lazy import
    resolves to our ``_FakeClient`` at call time.

    Yields the FakeClient class so tests can reset its instance counter /
    responses table.
    """
    _FakeClient._init_count = 0
    _FakeClient._responses = responses

    fake_root = types.ModuleType("pymodbus")
    fake_client_mod = types.ModuleType("pymodbus.client")
    fake_client_mod.ModbusSerialClient = _FakeClient
    fake_root.client = fake_client_mod

    with patch.dict(
        sys.modules,
        {"pymodbus": fake_root, "pymodbus.client": fake_client_mod},
    ):
        yield _FakeClient


# ---------------------------------------------------------------------------
# READ-ONLY safety invariants
# ---------------------------------------------------------------------------

class ReadOnlySafetyTests(unittest.TestCase):

    def test_allowlist_matches_what_script_uses(self):
        """READ_ONLY_MODBUS_FUNCTIONS reflects only function 0x03."""
        self.assertEqual(READ_ONLY_MODBUS_FUNCTIONS, (0x03,))

    def test_no_write_function_codes(self):
        """Modbus §7 write functions must never be in the allowlist."""
        for fc in (0x05, 0x06, 0x0F, 0x10):
            self.assertNotIn(fc, READ_ONLY_MODBUS_FUNCTIONS)

    def test_config_registers_forbidden_covers_bricking_targets(self):
        """REG_ADDR, REG_BAUD, REG_RATE, REG_CMD are all forbidden."""
        for reg in (0x1007, 0x1008, 0x1011, 0x8000):
            self.assertIn(reg, CONFIG_REGISTERS_FORBIDDEN)

    def test_probe_refuses_forbidden_register(self):
        """_probe_modbus_read raises before hitting the wire for banned addrs."""
        class ExplodingClient:
            def read_holding_registers(self, *_a, **_k):
                raise AssertionError(
                    "SAFETY VIOLATION: probe reached the wire for a "
                    "forbidden config register"
                )

        for addr in CONFIG_REGISTERS_FORBIDDEN:
            with self.assertRaisesRegex(RuntimeError, "CONFIG_REGISTERS_FORBIDDEN"):
                _probe_modbus_read(ExplodingClient(), addr, 1, device_id=1)


# ---------------------------------------------------------------------------
# Local register constant match against known BASF manual addresses
# ---------------------------------------------------------------------------

class LocalRegisterConstantsTests(unittest.TestCase):
    """Codex round 4: assert against KNOWN LITERAL HEX from the BASF manual.

    Do NOT compare against drivers.pyrometer's constants — that defeats
    the CI-safe local-constant decision (importing drivers.pyrometer at
    test time drags in pyvisa). Do NOT compare against themselves —
    tautology. The source of truth is the BASF manual register map,
    cited in the module docstring.
    """

    def test_reg_ver_address_matches_manual(self):
        self.assertEqual(REG_VER, 0x1300)

    def test_reg_name0_address_matches_manual(self):
        self.assertEqual(REG_NAME0, 0x1100)

    def test_reg_sn0_address_matches_manual(self):
        self.assertEqual(REG_SN0, 0x1305)

    def test_reg_ch1_temp_address_matches_manual(self):
        self.assertEqual(REG_CH1_TEMP, 0x0000)


# ---------------------------------------------------------------------------
# Default sweep set
# ---------------------------------------------------------------------------

class SweepDefaultsTests(unittest.TestCase):

    def test_default_device_id_sweep_starts_at_1(self):
        """Regression guard — device_id=1 is the driver's default and must
        be probed first so the common case doesn't wait behind others."""
        self.assertEqual(CANDIDATE_DEVICE_IDS[0], 1)

    def test_default_device_id_sweep_excludes_broadcast_zero(self):
        """Codex round 2: Modbus broadcast (address 0) is write-only.

        Issuing function 0x03 to broadcast is spec-undefined. The default
        sweep MUST exclude 0 so we don't send garbage to every device on
        the bus.
        """
        self.assertNotIn(0, CANDIDATE_DEVICE_IDS)

    def test_default_bauds_fast_to_slow(self):
        """115200 (the current default) probes first for fastest hit."""
        self.assertEqual(CANDIDATE_BAUDS[0], 115200)

    def test_default_combo_matches_driver_config(self):
        """The DEFAULT_COMBO constant reflects ModbusPyrometer's ctor."""
        self.assertEqual(DEFAULT_COMBO, (115200, 1))


# ---------------------------------------------------------------------------
# Sweep semantics — the workhorse tests
# ---------------------------------------------------------------------------

class SweepSemanticsTests(unittest.TestCase):

    def _run_sweep(
        self,
        responses: dict,
        bauds: tuple = (115200, 19200),
        device_ids: tuple = (1, 2, 3),
        exhaustive: bool = False,
    ) -> tuple[list, list, int]:
        """Patch ModbusSerialClient with FakeClient and run sweep.

        Returns (hits, misses, client_init_count).
        """
        with _fake_pymodbus(responses):
            hits, misses = sweep_modbus(
                "COM4", bauds, device_ids,
                per_combo_timeout_s=0.01, exhaustive=exhaustive,
            )
        return hits, misses, _FakeClient._init_count

    def test_port_opens_at_most_once_per_baud(self):
        """Codex round 3 perf invariant — this is the whole reason to
        restructure the sweep as outer-baud / inner-device_id."""
        # No responses configured → every combo silent, but the sweep
        # still walks the full baud × id matrix.
        _, _, init_count = self._run_sweep(
            {},
            bauds=(115200, 57600, 38400),
            device_ids=(1, 2, 3, 4, 5),
        )
        # 3 bauds × 5 ids = 15 combos would be 15 client inits if we
        # open/close per combo. The correct behavior is 3 (one per baud).
        self.assertEqual(
            init_count, 3,
            f"Expected 3 client inits (one per baud); got {init_count}. "
            "Sweep is doing open/close per (baud, id) combo, which burns "
            "~100ms Windows serial-open overhead per combo.",
        )

    def test_early_stop_on_first_hit_unless_exhaustive(self):
        """Default behavior: hit → break out of inner device_id loop.

        Device_id=2 answers at baud 115200. Under non-exhaustive mode,
        the sweep should NOT probe device_id=3+.
        """
        responses = {
            (115200, 2, REG_VER): [0x0903],  # ver 9.3
            (115200, 2, REG_NAME0): [0x45, 0x58, 0x49, 0x34, 0x37, 0x36, 0x35]
                                     + [0x00] * 25,  # "EXI4765" padded
            (115200, 2, REG_SN0): [0x00] * 9,
            (115200, 2, REG_CH1_TEMP): [0x437B, 0x3333],  # ~251.4°C big-endian
        }
        hits, _, _ = self._run_sweep(
            responses, bauds=(115200,), device_ids=(1, 2, 3, 4, 5),
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["baud"], 115200)
        self.assertEqual(hits[0]["device_id"], 2)
        self.assertEqual(hits[0]["identity"]["version"], "9.3")
        self.assertEqual(hits[0]["identity"]["name"], "EXI4765")

    def test_exhaustive_flag_walks_full_matrix(self):
        """--exhaustive: probe every device_id on every baud even after hit."""
        responses = {
            (115200, 2, REG_VER): [0x0903],
            (115200, 2, REG_NAME0): [0x00] * 32,
            (115200, 2, REG_SN0): [0x00] * 9,
            (115200, 2, REG_CH1_TEMP): [0x0000, 0x0000],
        }
        _, misses, _ = self._run_sweep(
            responses, bauds=(115200,), device_ids=(1, 2, 3, 4, 5),
            exhaustive=True,
        )
        # Under exhaustive, we should get misses for 1, 3, 4, 5 on 115200
        # (device_id=2 is the hit). Under non-exhaustive, we'd get miss for
        # 1 only (then break on 2's hit).
        miss_device_ids = sorted({m["device_id"] for m in misses})
        self.assertEqual(miss_device_ids, [1, 3, 4, 5])

    def test_probe_never_touches_config_registers_under_exhaustive(self):
        """Data-only invariant, even in the worst case (exhaustive sweep).

        Codex round 2: guard that no addr in CONFIG_REGISTERS_FORBIDDEN
        ever appears in a mocked wire call. If it does, either the
        discover-script code path or a future extension broke the
        invariant.
        """
        seen_addrs: list[int] = []
        original_read = _FakeClient.read_holding_registers

        def spy_read(self, addr, *args, **kwargs):
            seen_addrs.append(addr)
            return original_read(self, addr, *args, **kwargs)

        with _fake_pymodbus({}), \
                patch.object(_FakeClient, "read_holding_registers", spy_read):
            sweep_modbus(
                "COM4", (115200, 19200), (1, 2, 3),
                per_combo_timeout_s=0.01, exhaustive=True,
            )
        # Compute the set of addresses that reached the wire.
        wire_addrs = set(seen_addrs)
        forbidden_hits = wire_addrs & set(CONFIG_REGISTERS_FORBIDDEN)
        self.assertEqual(
            forbidden_hits, set(),
            f"Forbidden config registers reached the wire: "
            f"{[hex(a) for a in forbidden_hits]}. Data-only invariant "
            "broken — see plan P2 'Register-address constants' section.",
        )

    def test_probe_only_uses_data_registers(self):
        """The full set of allowed addresses is exactly the four data ones."""
        seen_addrs: list[int] = []
        original_read = _FakeClient.read_holding_registers

        def spy_read(self, addr, *args, **kwargs):
            seen_addrs.append(addr)
            return original_read(self, addr, *args, **kwargs)

        with _fake_pymodbus({}), \
                patch.object(_FakeClient, "read_holding_registers", spy_read):
            sweep_modbus(
                "COM4", (115200,), (1,),
                per_combo_timeout_s=0.01, exhaustive=False,
            )
        # With no responses configured, the sweep only ever fires the
        # lightweight probe (REG_VER). The fuller identity reads only
        # happen on a HIT, which won't occur here. So we expect just
        # REG_VER on the wire.
        self.assertEqual(set(seen_addrs), {REG_VER})

    def test_probe_reads_full_identity_on_hit(self):
        """On a hit, the sweep reads REG_VER + REG_NAME0 + REG_SN0 + REG_CH1_TEMP.

        Two REG_VER calls happen: one lightweight probe + one identity
        version fetch. Both are allowed reads.
        """
        responses = {
            (115200, 1, REG_VER): [0x0903],
            (115200, 1, REG_NAME0): [0x00] * 32,
            (115200, 1, REG_SN0): [0x00] * 9,
            (115200, 1, REG_CH1_TEMP): [0x0000, 0x0000],
        }
        seen_addrs: list[int] = []
        original_read = _FakeClient.read_holding_registers

        def spy_read(self, addr, *args, **kwargs):
            seen_addrs.append(addr)
            return original_read(self, addr, *args, **kwargs)

        with _fake_pymodbus(responses), \
                patch.object(_FakeClient, "read_holding_registers", spy_read):
            sweep_modbus(
                "COM4", (115200,), (1,),
                per_combo_timeout_s=0.01, exhaustive=False,
            )
        # Set of addresses touched — all four data regs should appear.
        self.assertIn(REG_VER, seen_addrs)
        self.assertIn(REG_NAME0, seen_addrs)
        self.assertIn(REG_SN0, seen_addrs)
        self.assertIn(REG_CH1_TEMP, seen_addrs)


# ---------------------------------------------------------------------------
# Verdict logic
# ---------------------------------------------------------------------------

class VerdictTests(unittest.TestCase):

    def test_silent_all_combos_when_no_hits(self):
        state, hint = build_verdict(hits=[], misses=[
            {"baud": 115200, "device_id": 1, "error": "no response"},
        ])
        self.assertEqual(state, "silent_all_combos")
        self.assertIn("non-Modbus mode", hint)
        self.assertIn("force_modbus", hint)

    def test_identity_found_at_default_verdict(self):
        state, hint = build_verdict(
            hits=[{
                "baud": 115200, "device_id": 1,
                "identity": {"version": "9.3", "name": "EXI4765",
                             "serial": "S123", "temp_no_swap_C": 200.0,
                             "temp_swap_C": -1e30},
            }],
            misses=[],
        )
        self.assertEqual(state, "identity_found_at_default")
        self.assertIn("9.3", hint)
        self.assertIn("EXI4765", hint)

    def test_verdict_names_the_baud_id_combo_on_non_default_hit(self):
        """Non-default hit → verdict text must include the actual combo."""
        state, hint = build_verdict(
            hits=[{
                "baud": 19200, "device_id": 5,
                "identity": {"version": "9.3", "name": "EXI4765",
                             "serial": "", "temp_no_swap_C": None,
                             "temp_swap_C": None},
            }],
            misses=[],
        )
        self.assertEqual(state, "identity_found_at_non_default")
        self.assertIn("(19200, 5)", hint)
        # Recovery hint MUST tell operator not to expect this script to
        # write config registers — reconfiguration is manual.
        self.assertIn("does NOT write", hint)

    def test_pymodbus_missing_verdict(self):
        """The sentinel miss (baud=0) triggers a distinct verdict."""
        state, hint = build_verdict(
            hits=[],
            misses=[{"baud": 0, "device_id": 0,
                     "error": "pymodbus not installed"}],
        )
        self.assertEqual(state, "pymodbus_missing")
        self.assertIn("pip install pymodbus", hint)


# ---------------------------------------------------------------------------
# Evidence bundle
# ---------------------------------------------------------------------------

class EvidenceBundleTests(unittest.TestCase):

    def test_bundle_records_per_combo_error_when_all_silent(self):
        """Codex round 2: diagnostic completeness — each miss must appear
        in the JSON with its (baud, device_id, error) triple."""
        misses = [
            {"baud": 115200, "device_id": 1, "error": "no response"},
            {"baud": 115200, "device_id": 2, "error": "no response"},
            {"baud": 19200, "device_id": 1, "error": "no response"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bundle"
            written = write_evidence_bundle(
                out, args_summary={"port": "COM4"},
                environment={"available_ports": []},
                hits=[], misses=misses,
                state="silent_all_combos", hint="hint text",
            )
            self.assertIn("result.json", written)
            data = json.loads(written["result.json"].read_text())
            self.assertEqual(data["phase_2_sweep_misses"], misses)
            self.assertEqual(data["phase_1_sweep_hits"], [])
            self.assertEqual(data["verdict"]["state"], "silent_all_combos")

    def test_bundle_schema_differs_from_physical_debug(self):
        """Codex round 3: this bundle's shape is discover-specific.

        Reusing pyrometer_physical_debug's write_evidence_bundle would
        force a phase_1_port / phase_2_exactus / phase_3_modbus schema
        that doesn't map onto the (baud, id) sweep. Assert our shape
        is what we designed for.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bundle"
            written = write_evidence_bundle(
                out, args_summary={},
                environment={"available_ports": []},
                hits=[], misses=[],
                state="silent_all_combos", hint="",
            )
            data = json.loads(written["result.json"].read_text())
            # Our keys
            self.assertIn("phase_0_env", data)
            self.assertIn("phase_1_sweep_hits", data)
            self.assertIn("phase_2_sweep_misses", data)
            self.assertIn("verdict", data)
            # NOT physical_debug's keys
            self.assertNotIn("phase_1_port", data)
            self.assertNotIn("phase_2_exactus", data)
            self.assertNotIn("phase_3_modbus", data)


# ---------------------------------------------------------------------------
# --initial-wait-s: applied once, not per-combo
# ---------------------------------------------------------------------------

class InitialWaitTests(unittest.TestCase):

    def test_initial_wait_applied_once_not_per_combo(self):
        """Codex round 2 perf invariant.

        The wait must fire ONCE (Phase 0.5) before the sweep. NOT once
        per baud, NOT once per (baud, id) combo. A regression where the
        wait leaked into the sweep loop would make a 55-combo sweep at
        --initial-wait-s=8 take 7+ minutes.
        """
        # Capture time.sleep calls in the discover module. We patch it
        # at the module namespace to catch our uses (Phase 0.5 explicitly
        # calls time.sleep). Any use inside pymodbus itself is caught
        # too, but our mocked FakeClient never enters pymodbus.
        sleep_calls: list[float] = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)

        argv = [
            "--port", "COM_FAKE",
            "--bauds", "115200,19200,9600",
            "--device-ids", "1,2,3,4,5",
            "--initial-wait-s", "8.0",
            "--per-combo-timeout-s", "0.001",
        ]
        with _fake_pymodbus({}), \
                patch(
                    "scripts.pyrometer_modbus_discover.time.sleep", fake_sleep,
                ), redirect_stdout(io.StringIO()):
            rc = main(argv)
        # No hits → exit 1
        self.assertEqual(rc, 1)
        # Exactly one call, exactly 8.0 seconds. Regression guard: if the
        # wait leaks into the sweep loop, sleep_calls will have many
        # entries or a sum way larger than 8.0.
        self.assertEqual(
            sleep_calls, [8.0],
            f"time.sleep was called {len(sleep_calls)} time(s) with "
            f"{sleep_calls}. --initial-wait-s must fire exactly ONCE.",
        )


# ---------------------------------------------------------------------------
# CLI edge cases
# ---------------------------------------------------------------------------

class CliTests(unittest.TestCase):

    def test_device_id_zero_rejected_via_override(self):
        """Even under --device-ids, broadcast (0) must be refused up-front."""
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            main([
                "--port", "COM_FAKE",
                "--device-ids", "0,1,2",
            ])

    def test_help_lists_all_new_flags(self):
        """Smoke test — --help works and mentions the round-2/3 flags."""
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(buf):
            main(["--help"])
        # argparse exits 0 for --help
        self.assertEqual(ctx.exception.code, 0)
        out = buf.getvalue()
        self.assertIn("--initial-wait-s", out)
        self.assertIn("--per-combo-timeout-s", out)
        self.assertIn("--exhaustive", out)
        self.assertIn("--output-dir", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
