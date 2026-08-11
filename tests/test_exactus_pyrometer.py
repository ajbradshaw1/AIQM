#!/usr/bin/env python3
"""End-to-end test of ExactusSerialPyrometer against an in-process serial mock.

Validates the packet-framing logic, header re-sync after garbage,
timeout handling, plausibility filtering, bounded sync-hunt, and the
consecutive-failure disconnect marker — all without touching a real
serial port or a real Exactus.

Approach: replace ``serial`` in ``sys.modules`` with a fake module whose
``Serial`` class pulls bytes from a pre-loaded FIFO buffer. Each test
seeds the buffer, exercises ``connect()`` + ``read_temperature()``,
and asserts on the return value or the raised exception.

Usage:
    python -m pytest -q tests/test_exactus_pyrometer.py

Exits 0 on success; raises AssertionError with a diagnostic on failure.

Built Jul 2 2026 as the pre-lab safety net for Aarav's cherry-picked
binary-protocol code. If this passes on Mac, the driver's framing
logic is correct against a synthetic stream; anything that fails at
Bulbasaur is then a real-device problem (wrong endianness, wrong baud,
wrong packet shape), not a driver-logic problem.
"""

from __future__ import annotations

import struct
import sys
import types
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Fake serial.Serial
# ---------------------------------------------------------------------------

class FakeSerial:
    """Minimal drop-in for pyserial's Serial that reads from a byte buffer.

    Records the constructor kwargs so tests can assert framing params
    were passed through. Exposes ``reset_input_buffer`` so we can verify
    the driver flushes stale bytes on connect.
    """

    _last_kwargs: dict = {}      # class-level: last constructor kwargs
    _last_instance: "FakeSerial" = None  # class-level: last constructed instance

    def __init__(self, **kwargs):
        FakeSerial._last_kwargs = dict(kwargs)
        FakeSerial._last_instance = self
        self._buffer: deque[int] = deque()
        self.is_open = True
        self.closed = False
        self.reset_input_called = False

    # -- Test helpers ---------------------------------------------------

    def load(self, data: bytes) -> None:
        """Push bytes onto the read buffer (FIFO)."""
        self._buffer.extend(data)

    def load_packet(self, temp_c: float, fmt: str = ">f", header: int = 0x81) -> None:
        """Seed one complete Exactus packet with the given temperature."""
        self.load(bytes([header]) + struct.pack(fmt, temp_c))

    # -- pyserial-facing API ------------------------------------------

    def read(self, n: int = 1) -> bytes:
        """Return up to ``n`` bytes; empty bytes on empty buffer (=timeout)."""
        if not self._buffer:
            return b""  # pyserial returns b"" on read timeout
        out = bytearray()
        for _ in range(n):
            if not self._buffer:
                break
            out.append(self._buffer.popleft())
        return bytes(out)

    def close(self) -> None:
        self.closed = True
        self.is_open = False

    def reset_input_buffer(self) -> None:
        self.reset_input_called = True
        self._buffer.clear()


def install_fake_serial() -> types.SimpleNamespace:
    """Replace ``serial`` in ``sys.modules`` with a fake exposing ``Serial``.

    Returns the fake module so the caller can construct instances directly
    (though tests usually operate on FakeSerial._last_instance after connect).
    """
    fake = types.SimpleNamespace(Serial=FakeSerial)
    sys.modules["serial"] = fake
    return fake


def uninstall_fake_serial() -> None:
    sys.modules.pop("serial", None)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _connect_with_stream(*data_blobs: bytes):
    """Install fake serial, connect a driver, seed the read buffer.

    Returns ``(driver, fake_serial_instance)``. Loads happen AFTER
    connect() so the reset_input_buffer call doesn't drain them.
    """
    install_fake_serial()
    from drivers.pyrometer import ExactusSerialPyrometer

    p = ExactusSerialPyrometer(port="COM_TEST", baudrate=115200, timeout=0.1)
    p.connect()
    for blob in data_blobs:
        FakeSerial._last_instance.load(blob)
    return p, FakeSerial._last_instance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_valid_packet_roundtrip() -> None:
    """One 5-byte packet → read_temperature() returns the correct float."""
    try:
        p, _ = _connect_with_stream()
        FakeSerial._last_instance.load_packet(temp_c=752.5)
        val = p.read_temperature()
        assert abs(val - 752.5) < 1e-3, f"expected 752.5, got {val}"
        p.disconnect()
    finally:
        uninstall_fake_serial()


def test_framing_params_passed_through() -> None:
    """connect() sends parity/stopbits/bytesize to pyserial's Serial()."""
    try:
        p, _ = _connect_with_stream()
        kw = FakeSerial._last_kwargs
        assert kw["port"] == "COM_TEST", kw
        assert kw["baudrate"] == 115200, kw
        assert kw["parity"] == "N", kw
        assert kw["stopbits"] == 1, kw
        assert kw["bytesize"] == 8, kw
        assert kw["timeout"] == 0.1, kw
        p.disconnect()
    finally:
        uninstall_fake_serial()


def test_reset_input_buffer_called() -> None:
    """connect() flushes stale bytes via reset_input_buffer()."""
    try:
        p, ser = _connect_with_stream()
        assert ser.reset_input_called, "expected reset_input_buffer() call in connect()"
        p.disconnect()
    finally:
        uninstall_fake_serial()


def test_header_resync_after_garbage() -> None:
    """Garbage bytes before the 0x81 header get skipped; read succeeds."""
    try:
        p, _ = _connect_with_stream(bytes([0x00, 0x42, 0xFF]))
        # Then append the real packet
        FakeSerial._last_instance.load_packet(temp_c=451.75)
        val = p.read_temperature()
        assert abs(val - 451.75) < 1e-3, f"expected 451.75, got {val}"
        p.disconnect()
    finally:
        uninstall_fake_serial()


def test_timeout_on_empty_buffer_raises() -> None:
    """Empty buffer → read() returns b"" → RuntimeError."""
    try:
        p, _ = _connect_with_stream()  # no data
        try:
            p.read_temperature()
        except RuntimeError as exc:
            assert "timeout" in str(exc).lower(), f"unexpected message: {exc}"
            p.disconnect()
            return
        raise AssertionError("expected RuntimeError, none raised")
    finally:
        uninstall_fake_serial()


def test_short_packet_raises() -> None:
    """Header + <4 body bytes → RuntimeError 'short packet'."""
    try:
        p, _ = _connect_with_stream(bytes([0x81, 0x00, 0x00]))  # only 2 body bytes
        try:
            p.read_temperature()
        except RuntimeError as exc:
            assert "short packet" in str(exc).lower(), f"unexpected message: {exc}"
            p.disconnect()
            return
        raise AssertionError("expected RuntimeError, none raised")
    finally:
        uninstall_fake_serial()


def test_plausibility_filter_rejects_garbage() -> None:
    """0x81 + a float far outside [-100, 3000] → RuntimeError 'implausible'."""
    try:
        p, _ = _connect_with_stream()
        # Ship a header + a float of 1e30 (way outside range)
        FakeSerial._last_instance.load(
            bytes([0x81]) + struct.pack(">f", 1e30)
        )
        try:
            p.read_temperature()
        except RuntimeError as exc:
            assert "implausible" in str(exc).lower(), f"unexpected message: {exc}"
            p.disconnect()
            return
        raise AssertionError("expected RuntimeError, none raised")
    finally:
        uninstall_fake_serial()


def test_bounded_sync_hunt() -> None:
    """No 0x81 in >MAX_SYNC_BYTES bytes → RuntimeError with clear diagnostic."""
    try:
        p, _ = _connect_with_stream()
        # Stream 300 non-0x81 bytes — MAX_SYNC_BYTES is 256
        FakeSerial._last_instance.load(bytes([0x00] * 300))
        try:
            p.read_temperature()
        except RuntimeError as exc:
            assert "256 bytes" in str(exc), f"expected max-sync diagnostic, got: {exc}"
            p.disconnect()
            return
        raise AssertionError("expected RuntimeError, none raised")
    finally:
        uninstall_fake_serial()


def test_consecutive_failures_mark_disconnected() -> None:
    """MAX_CONSECUTIVE_FAILS timeouts in a row → self._connected flips to False."""
    try:
        p, _ = _connect_with_stream()
        from drivers.pyrometer import ExactusSerialPyrometer
        for i in range(ExactusSerialPyrometer.MAX_CONSECUTIVE_FAILS):
            assert p.connected, f"unexpectedly disconnected at iteration {i}"
            try:
                p.read_temperature()
            except RuntimeError:
                pass
        # After MAX_CONSECUTIVE_FAILS raises, connected should be False
        assert not p.connected, (
            f"expected disconnected after "
            f"{ExactusSerialPyrometer.MAX_CONSECUTIVE_FAILS} failures"
        )
        p.disconnect()
    finally:
        uninstall_fake_serial()


def test_successful_read_resets_fail_counter() -> None:
    """A good read after N-1 failures resets the counter, staying connected."""
    try:
        p, _ = _connect_with_stream()
        from drivers.pyrometer import ExactusSerialPyrometer
        # N-1 failures
        for _ in range(ExactusSerialPyrometer.MAX_CONSECUTIVE_FAILS - 1):
            try:
                p.read_temperature()
            except RuntimeError:
                pass
        assert p.connected, "should still be connected before the last fail"
        # Now succeed
        FakeSerial._last_instance.load_packet(temp_c=200.0)
        val = p.read_temperature()
        assert abs(val - 200.0) < 1e-3
        # Fail again a bunch of times — should NOT flip disconnected on the
        # first one because the counter reset to 0
        for i in range(ExactusSerialPyrometer.MAX_CONSECUTIVE_FAILS - 1):
            try:
                p.read_temperature()
            except RuntimeError:
                pass
            assert p.connected, f"disconnected too early at iter {i}"
        p.disconnect()
    finally:
        uninstall_fake_serial()


def test_get_info_shape() -> None:
    """get_info() returns the expected dict shape for state.device_info."""
    try:
        p, _ = _connect_with_stream()
        info = p.get_info()
        assert set(info) == {"name", "port", "baud", "framing"}, info
        assert info["port"] == "COM_TEST"
        assert info["baud"] == 115200
        assert info["framing"] == "8N1"
        assert "Exactus" in info["name"]
        p.disconnect()
    finally:
        uninstall_fake_serial()


def test_read_before_connect_raises() -> None:
    """read_temperature() before connect() → RuntimeError 'not connected'."""
    try:
        install_fake_serial()
        from drivers.pyrometer import ExactusSerialPyrometer
        p = ExactusSerialPyrometer(port="COM_TEST")
        try:
            p.read_temperature()
        except RuntimeError as exc:
            assert "not connected" in str(exc).lower(), f"unexpected message: {exc}"
            return
        raise AssertionError("expected RuntimeError")
    finally:
        uninstall_fake_serial()


def test_endianness_switch() -> None:
    """Setting FLOAT_STRUCT to '<f' interprets the same bytes as little-endian."""
    try:
        install_fake_serial()
        from drivers.pyrometer import ExactusSerialPyrometer

        # Manually toggle endianness for this test only.
        original = ExactusSerialPyrometer.FLOAT_STRUCT
        try:
            ExactusSerialPyrometer.FLOAT_STRUCT = "<f"
            p = ExactusSerialPyrometer(port="COM_TEST", timeout=0.1)
            p.connect()
            # Load a header + a little-endian-encoded 123.5
            FakeSerial._last_instance.load(
                bytes([0x81]) + struct.pack("<f", 123.5)
            )
            val = p.read_temperature()
            assert abs(val - 123.5) < 1e-3, f"expected 123.5, got {val}"
            p.disconnect()
        finally:
            ExactusSerialPyrometer.FLOAT_STRUCT = original
    finally:
        uninstall_fake_serial()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_valid_packet_roundtrip,
    test_framing_params_passed_through,
    test_reset_input_buffer_called,
    test_header_resync_after_garbage,
    test_timeout_on_empty_buffer_raises,
    test_short_packet_raises,
    test_plausibility_filter_rejects_garbage,
    test_bounded_sync_hunt,
    test_consecutive_failures_mark_disconnected,
    test_successful_read_resets_fail_counter,
    test_get_info_shape,
    test_read_before_connect_raises,
    test_endianness_switch,
]


def main() -> int:
    print(f"ExactusSerialPyrometer smoke test — {len(TESTS)} cases")
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
