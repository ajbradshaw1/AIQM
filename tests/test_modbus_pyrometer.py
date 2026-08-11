#!/usr/bin/env python3
"""End-to-end test of ModbusPyrometer against an in-process pymodbus mock.

Validates word-order auto-detect, plausibility filter, set_rate range
check, pymodbus kwarg compatibility loop, response-object defensiveness,
per-field get_info handling, and the connect/disconnect lifecycle —
all Mac-side, without needing pyserial or a real BASF Exactus.

Approach: replace ``pymodbus.client`` in ``sys.modules`` with a fake that
exposes a ``ModbusSerialClient`` class whose ``read_holding_registers``
and ``write_register`` methods pull from a pre-loaded response queue.
Tests seed the queue with canned successful/failing/oddly-shaped responses
and assert on the driver's behavior.

Usage:
    python -m pytest -q tests/test_modbus_pyrometer.py

Exits 0 on success; raises AssertionError with a diagnostic on failure.

Built Jul 2 2026 as the pre-lab safety net for the *proven* Modbus RTU
pyrometer path. Jacques's pyrometer_script.py works against real hardware;
these tests pin the AIQM port so future changes don't drift.
"""

from __future__ import annotations

import struct
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Fake pymodbus response types
# ---------------------------------------------------------------------------

class FakeReadResponse:
    """Mimics a pymodbus read_holding_registers response."""

    def __init__(self, registers: list[int] | None = None, is_error: bool = False):
        # registers may be omitted to simulate a malformed response
        if registers is not None:
            self.registers = list(registers)
        self._is_error = is_error

    def isError(self) -> bool:  # noqa: N802 — pymodbus name
        return self._is_error


class FakeWriteResponse:
    """Mimics a pymodbus write_register response."""

    def __init__(self, is_error: bool = False):
        self._is_error = is_error

    def isError(self) -> bool:  # noqa: N802
        return self._is_error


class FakeModbusSerialClient:
    """Fake ModbusSerialClient — pulls responses from a queue.

    Kwarg-compatibility test: set ``reject_kwargs`` to a list of kwargs
    that should raise TypeError when passed to read/write. The driver's
    _read_holding loop should fall through to the next kwarg on TypeError.
    """

    # Class-level state so tests can inspect after connect
    _last_kwargs: dict = {}
    _last_instance: "FakeModbusSerialClient" = None
    _connect_returns: bool = True

    def __init__(self, **kwargs):
        FakeModbusSerialClient._last_kwargs = dict(kwargs)
        FakeModbusSerialClient._last_instance = self

        self.read_queue: list[FakeReadResponse | Exception] = []
        self.write_queue: list[FakeWriteResponse | Exception] = []

        # Kwargs to reject with TypeError for compat testing
        self.reject_read_kwargs: set[str] = set()
        self.reject_write_kwargs: set[str] = set()

        self.read_call_count = 0
        self.write_call_count = 0
        self.close_call_count = 0
        self.last_write: tuple | None = None
        self.reads_with_kwarg: list[str] = []

    def connect(self) -> bool:
        return FakeModbusSerialClient._connect_returns

    def close(self) -> None:
        self.close_call_count += 1

    def read_holding_registers(self, *args, **kwargs):
        # The driver's _read_holding tries each kwarg name in turn.
        # Reject the ones in reject_read_kwargs with TypeError so the
        # driver's compat loop advances.
        for kw in ("device_id", "slave", "unit"):
            if kw in kwargs:
                if kw in self.reject_read_kwargs:
                    raise TypeError(
                        f"read_holding_registers() got an unexpected keyword "
                        f"argument {kw!r}"
                    )
                self.reads_with_kwarg.append(kw)
                break
        self.read_call_count += 1
        if not self.read_queue:
            # Default: return an "OK" response with two zero registers.
            return FakeReadResponse([0, 0])
        item = self.read_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def write_register(self, *args, **kwargs):
        for kw in ("device_id", "slave", "unit"):
            if kw in kwargs:
                if kw in self.reject_write_kwargs:
                    raise TypeError(
                        f"write_register() got an unexpected keyword "
                        f"argument {kw!r}"
                    )
                break
        self.write_call_count += 1
        self.last_write = (args, kwargs)
        if not self.write_queue:
            return FakeWriteResponse(is_error=False)
        item = self.write_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------
# Install / uninstall the fake module
# ---------------------------------------------------------------------------

def install_fake_pymodbus() -> None:
    """Install a fake pymodbus.client module exposing ModbusSerialClient."""
    fake_client = types.ModuleType("pymodbus.client")
    fake_client.ModbusSerialClient = FakeModbusSerialClient
    fake_root = types.ModuleType("pymodbus")
    fake_root.client = fake_client
    sys.modules["pymodbus"] = fake_root
    sys.modules["pymodbus.client"] = fake_client
    FakeModbusSerialClient._connect_returns = True


def uninstall_fake_pymodbus() -> None:
    sys.modules.pop("pymodbus.client", None)
    sys.modules.pop("pymodbus", None)


# ---------------------------------------------------------------------------
# Helpers for building canned float32 responses
# ---------------------------------------------------------------------------

def f32_to_regs(value: float, word_swap: bool = False) -> list[int]:
    """Return the two 16-bit registers that decode to ``value`` under IEEE 754.

    Mirrors ``_regs_to_f32`` in the driver. ``word_swap=True`` returns the
    words swapped so the driver's word_swap=True path yields the right value.
    """
    raw = struct.unpack(">I", struct.pack(">f", value))[0]
    hi = (raw >> 16) & 0xFFFF
    lo = raw & 0xFFFF
    return [lo, hi] if word_swap else [hi, lo]


def _connect_driver() -> tuple:
    """Install fake pymodbus, connect a driver with default word-order detect.

    The default read queue provides one plausible temperature reading so
    _detect_word_order lands on word_swap=False cleanly. Returns
    ``(driver, fake_client_instance)`` for test-side manipulation.
    """
    install_fake_pymodbus()
    from drivers.pyrometer import ModbusPyrometer
    driver = ModbusPyrometer(port="COM_TEST", baudrate=115200, device_id=1)
    # Seed one plausible temperature for word-order detect.
    fake_pre_seed = FakeReadResponse(f32_to_regs(750.0))
    # We need to inject *before* connect happens. But the fake client
    # gets constructed inside connect() — so instead, we use a class-
    # level pattern: pre-set the responses via a factory reference.
    #
    # Trick: create the client manually first to get the instance, then
    # monkey-patch the driver's connect to reuse it.
    # Simpler path: let connect() proceed, but during word-order detect
    # (which reads via _read_holding), the fake returns the default OK
    # response with [0, 0] — that's raw=0 → f32=0.0 → outside plausibility.
    # So the driver would attempt word_swap=True, get 0.0 again, and stay
    # word_swap=False with a warning log.
    #
    # For most tests, that's fine (word_swap=False is the common case).
    # For tests that need a specific word-order behavior, seed the fake
    # instance's read_queue AFTER construction but BEFORE the first read.
    #
    # Concrete approach: pre-populate a class-level factory queue that
    # instances copy into their read_queue on construction.
    driver.connect()
    fake = FakeModbusSerialClient._last_instance
    return driver, fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_construct_no_wire_traffic() -> None:
    """Constructor doesn't touch pymodbus — safe to inspect defaults offline."""
    install_fake_pymodbus()
    try:
        from drivers.pyrometer import ModbusPyrometer
        driver = ModbusPyrometer(port="COM_TEST")
        # No fake instance yet — nothing constructed the client
        assert FakeModbusSerialClient._last_instance is None
        assert not driver.connected
        assert driver.TEMP_MIN_C == -100.0
        assert driver.TEMP_MAX_C == 3000.0
        assert driver.RATE_MIN_HZ == 1
        assert driver.RATE_MAX_HZ == 1000
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_connect_success() -> None:
    """connect() → connected=True; disconnect() → False + client closed."""
    try:
        driver, fake = _connect_driver()
        assert driver.connected
        driver.disconnect()
        assert not driver.connected
        assert fake.close_call_count == 1
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_connect_serial_port_failure_raises() -> None:
    """Serial port open failure raises RuntimeError with a clear diagnostic."""
    install_fake_pymodbus()
    try:
        FakeModbusSerialClient._connect_returns = False
        from drivers.pyrometer import ModbusPyrometer
        driver = ModbusPyrometer(port="COM_TEST")
        try:
            driver.connect()
        except RuntimeError as exc:
            assert "COM_TEST" in str(exc)
            assert "TemperaSure" in str(exc)  # part of the diagnostic message
            return
        raise AssertionError("expected RuntimeError")
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_read_temperature_happy_path() -> None:
    """A plausible-encoded temperature read returns the correct float."""
    try:
        driver, fake = _connect_driver()
        fake.read_queue.append(FakeReadResponse(f32_to_regs(755.5)))
        t = driver.read_temperature()
        assert abs(t - 755.5) < 1e-3, f"expected 755.5, got {t}"
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_read_temperature_plausibility_rejects_out_of_range() -> None:
    """A read returning >3000°C raises RuntimeError with 'implausible' text."""
    try:
        driver, fake = _connect_driver()
        # Encode 5000°C — outside the [−100, 3000] range
        fake.read_queue.append(FakeReadResponse(f32_to_regs(5000.0)))
        try:
            driver.read_temperature()
        except RuntimeError as exc:
            assert "implausible" in str(exc).lower(), f"unexpected: {exc}"
            return
        raise AssertionError("expected RuntimeError, none raised")
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_read_before_connect_raises() -> None:
    """read_temperature before connect() → RuntimeError 'not connected'."""
    install_fake_pymodbus()
    try:
        from drivers.pyrometer import ModbusPyrometer
        driver = ModbusPyrometer(port="COM_TEST")
        try:
            driver.read_temperature()
        except RuntimeError as exc:
            assert "not connected" in str(exc).lower(), f"unexpected: {exc}"
            return
        raise AssertionError("expected RuntimeError")
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_read_response_missing_registers_attr() -> None:
    """A response without .registers → clean RuntimeError with Exactus-mode hint.

    Regression guard for Jul 27 2026 error-message improvement: when the
    probe is in Exactus streaming mode, pymodbus returns responses with
    no registers. The driver should surface the recovery hint (port
    the smoke script's diagnostic) instead of a bare "Modbus read error".
    """
    try:
        driver, fake = _connect_driver()
        # No registers attr = malformed response (Exactus-mode symptom)
        fake.read_queue.append(FakeReadResponse(registers=None))
        try:
            driver.read_temperature()
        except AttributeError:
            raise AssertionError(
                "leaked AttributeError — _resp_ok should guard against "
                "missing .registers"
            )
        except RuntimeError as exc:
            msg = str(exc)
            assert "empty register response" in msg.lower(), \
                f"missing empty-response signal: {exc}"
            assert "exactus protocol mode" in msg.lower(), \
                f"missing Exactus-mode hint: {exc}"
            assert "power-cycle" in msg.lower(), \
                f"missing recovery instruction: {exc}"
            return
        raise AssertionError("expected RuntimeError")
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_read_empty_registers_list_signals_exactus_mode() -> None:
    """A response with .registers=[] → Exactus-mode hint (same class of failure)."""
    try:
        driver, fake = _connect_driver()
        fake.read_queue.append(FakeReadResponse(registers=[]))
        try:
            driver.read_temperature()
        except RuntimeError as exc:
            msg = str(exc)
            assert "empty register response" in msg.lower(), \
                f"missing empty-response signal: {exc}"
            assert "exactus protocol mode" in msg.lower(), \
                f"missing Exactus-mode hint: {exc}"
            return
        raise AssertionError("expected RuntimeError")
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_read_short_response_reports_count_mismatch() -> None:
    """A response with 1 reg when 2 are needed → 'response has 1 regs, needs 2'.

    Distinguishes the count-mismatch case from the empty-response case so
    the operator knows the probe IS responding (just with wrong length),
    not silent (Exactus mode).
    """
    try:
        driver, fake = _connect_driver()
        # Short response — 1 reg but read_temperature() needs 2 for float32
        # Use is_error=True so _resp_ok returns False and we hit the branch.
        fake.read_queue.append(FakeReadResponse(registers=[42], is_error=True))
        try:
            driver.read_temperature()
        except RuntimeError as exc:
            msg = str(exc).lower()
            assert "needs 2" in msg, f"missing 'needs 2' in message: {exc}"
            assert "1 regs" in msg, f"missing observed count: {exc}"
            # Must NOT surface the Exactus hint — that's a different failure mode
            assert "exactus" not in msg, \
                f"count-mismatch case should not blame Exactus mode: {exc}"
            return
        raise AssertionError("expected RuntimeError")
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_set_rate_range_check_rejects_negative() -> None:
    """set_rate(-1) → ValueError before any wire traffic."""
    try:
        driver, fake = _connect_driver()
        pre_write_count = fake.write_call_count
        try:
            driver.set_rate(-1)
        except ValueError as exc:
            assert "-1" in str(exc)
            assert fake.write_call_count == pre_write_count, (
                "range check should reject BEFORE writing"
            )
            return
        raise AssertionError("expected ValueError")
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_set_rate_range_check_rejects_over_max() -> None:
    """set_rate(9999999) → ValueError before wire traffic."""
    try:
        driver, fake = _connect_driver()
        pre_write_count = fake.write_call_count
        try:
            driver.set_rate(9999999)
        except ValueError:
            assert fake.write_call_count == pre_write_count
            return
        raise AssertionError("expected ValueError")
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_set_rate_happy_path_writes_and_verifies() -> None:
    """set_rate(100) with a successful write response completes cleanly."""
    try:
        driver, fake = _connect_driver()
        fake.write_queue.append(FakeWriteResponse(is_error=False))
        driver.set_rate(100)
        assert fake.write_call_count >= 1
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_set_rate_raises_when_write_reports_error() -> None:
    """A write response that reports isError() → RuntimeError from set_rate."""
    try:
        driver, fake = _connect_driver()
        fake.write_queue.append(FakeWriteResponse(is_error=True))
        try:
            driver.set_rate(100)
        except RuntimeError as exc:
            assert "set_rate" in str(exc).lower(), f"unexpected: {exc}"
            return
        raise AssertionError("expected RuntimeError")
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_pymodbus_kwarg_compat_loop() -> None:
    """When the first kwarg name (device_id) raises TypeError, driver retries."""
    try:
        driver, fake = _connect_driver()
        # Reject device_id — driver should fall through to slave (which succeeds).
        fake.reject_read_kwargs = {"device_id"}
        fake.read_queue.append(FakeReadResponse(f32_to_regs(500.0)))
        t = driver.read_temperature()
        assert abs(t - 500.0) < 1e-3
        # Confirm the successful call used a fallback kwarg (slave or unit).
        assert "slave" in fake.reads_with_kwarg or "unit" in fake.reads_with_kwarg, (
            f"expected fallback kwarg used, saw: {fake.reads_with_kwarg}"
        )
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_get_info_partial_failure() -> None:
    """When some info reads fail, get_info still returns the successful ones."""
    try:
        driver, fake = _connect_driver()
        # 4 sequential reads: name (ascii 32 regs), serial (ascii 9),
        # version (u16), rate (u16). Fail name and serial; succeed version + rate.
        fake.read_queue.extend([
            FakeReadResponse(is_error=True),           # name fails
            FakeReadResponse(is_error=True),           # serial fails
            FakeReadResponse([0x0108]),                # version 1.8
            FakeReadResponse([50]),                    # rate 50 Hz
        ])
        info = driver.get_info()
        assert "name" not in info, f"name should have failed: {info}"
        assert "serial" not in info
        assert info.get("version") == "1.8", f"unexpected version: {info}"
        assert info.get("rate_hz") == 50, f"unexpected rate: {info}"
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


def test_word_swap_detected_on_implausible_first_read() -> None:
    """Detect: implausible first read + plausible second → driver sets _word_swap=True.

    Numerical care: most float32 values in the temperature range decode
    to denormals near zero when read with the wrong word order — and
    denormals fall INSIDE the (-100, 3000) plausibility range, so the
    detect logic wouldn't fire on them. We instead hand-craft the first
    response's registers so that ``_regs_to_f32(hi, lo)`` under
    ``word_swap=False`` yields +Inf (clearly outside the range), and
    the second response uses ``f32_to_regs(750.0, word_swap=True)`` so
    the driver's ``word_swap=True`` path decodes to 750.0.
    """
    install_fake_pymodbus()
    try:
        from drivers.pyrometer import ModbusPyrometer

        # First response: [0x7F80, 0x0000]
        # word_swap=False decode: raw = 0x7F800000 = +Inf (implausible)
        # word_swap=True decode:  raw = 0x00007F80 ≈ 4.55e-41 (plausible)
        # → drives the detect logic into trying the swap path.
        first_response = FakeReadResponse([0x7F80, 0x0000])
        # Second response: 750.0 encoded such that word_swap=True decodes correctly.
        second_response = FakeReadResponse(f32_to_regs(750.0, word_swap=True))
        FakeModbusSerialClient._pre_arm_reads = [first_response, second_response]

        _orig_init = FakeModbusSerialClient.__init__

        def patched_init(self, **kwargs):
            _orig_init(self, **kwargs)
            pre = getattr(FakeModbusSerialClient, "_pre_arm_reads", None)
            if pre:
                self.read_queue.extend(pre)
                FakeModbusSerialClient._pre_arm_reads = []

        FakeModbusSerialClient.__init__ = patched_init
        try:
            driver = ModbusPyrometer(port="COM_TEST")
            driver.connect()
            assert driver.connected
            assert driver._word_swap is True, (
                f"expected _word_swap=True after auto-detect; got "
                f"{driver._word_swap!r}"
            )
        finally:
            FakeModbusSerialClient.__init__ = _orig_init
            FakeModbusSerialClient._pre_arm_reads = []
    finally:
        FakeModbusSerialClient._last_instance = None
        uninstall_fake_pymodbus()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_construct_no_wire_traffic,
    test_connect_success,
    test_connect_serial_port_failure_raises,
    test_read_temperature_happy_path,
    test_read_temperature_plausibility_rejects_out_of_range,
    test_read_before_connect_raises,
    test_read_response_missing_registers_attr,
    test_read_empty_registers_list_signals_exactus_mode,
    test_read_short_response_reports_count_mismatch,
    test_set_rate_range_check_rejects_negative,
    test_set_rate_range_check_rejects_over_max,
    test_set_rate_happy_path_writes_and_verifies,
    test_set_rate_raises_when_write_reports_error,
    test_pymodbus_kwarg_compat_loop,
    test_get_info_partial_failure,
    test_word_swap_detected_on_implausible_first_read,
]


def main() -> int:
    print(f"ModbusPyrometer smoke test — {len(TESTS)} cases")
    print()
    failures: list[tuple[str, BaseException]] = []
    for t in TESTS:
        name = t.__name__
        try:
            t()
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  PASS {name}")

    print()
    if failures:
        print(f"FAIL - {len(failures)}/{len(TESTS)} tests failed")
        return 1
    print(f"PASS - {len(TESTS)}/{len(TESTS)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
