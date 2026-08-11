"""Offline tests for the Ch-MBE read-only raw Modbus transport."""

import struct
import unittest

from drivers.pyrometer import (
    _RawSerialModbusClient,
    _crc16_modbus,
)


def _reply(device_id: int, registers: list[int]) -> bytes:
    payload = struct.pack(f">{len(registers)}H", *registers)
    body = bytes((device_id, 0x03, len(payload))) + payload
    return body + struct.pack("<H", _crc16_modbus(body))


class RawReplyParsingTests(unittest.TestCase):
    def test_parses_verified_chmbe_temperature_frame(self):
        response = _RawSerialModbusClient._find_reply(
            bytes.fromhex("01 03 04 43 55 D7 2F E1 8B"), 1, 2,
        )
        self.assertIsNotNone(response)
        self.assertFalse(response.isError())
        self.assertEqual(response.registers, [0x4355, 0xD72F])

    def test_skips_echo_and_noise_before_valid_reply(self):
        request = _RawSerialModbusClient._request(1, 0, 2)
        response = _RawSerialModbusClient._find_reply(
            b"\x99" + request + b"\x00" + _reply(1, [0x4355, 0xD72F]),
            1,
            2,
        )
        self.assertIsNotNone(response)
        self.assertEqual(response.registers, [0x4355, 0xD72F])

    def test_rejects_bad_crc(self):
        frame = bytearray(_reply(1, [0x4355, 0xD72F]))
        frame[-1] ^= 0xFF
        self.assertIsNone(
            _RawSerialModbusClient._find_reply(bytes(frame), 1, 2)
        )

    def test_reports_modbus_exception(self):
        body = bytes((1, 0x83, 0x02))
        frame = body + struct.pack("<H", _crc16_modbus(body))
        response = _RawSerialModbusClient._find_reply(frame, 1, 2)
        self.assertIsNotNone(response)
        self.assertTrue(response.isError())
        self.assertIn("0x02", response.error)

    def test_write_is_fail_closed(self):
        client = _RawSerialModbusClient(
            port="COM_TEST", baudrate=115200, parity="N", stopbits=1,
            bytesize=8, timeout=1.0, rts=False,
        )
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            client.write_register(0x1011, 10, device_id=1)


class _FakeSerial:
    def __init__(self, response: bytes):
        self._response = bytearray(response)
        self.written = b""

    @property
    def in_waiting(self):
        return len(self._response)

    def reset_input_buffer(self):
        pass

    def write(self, data: bytes):
        self.written += data

    def flush(self):
        pass

    def read(self, size: int):
        chunk = bytes(self._response[:size])
        del self._response[:size]
        return chunk


class RawClientReadTests(unittest.TestCase):
    def test_read_sends_request_and_returns_registers(self):
        client = _RawSerialModbusClient(
            port="COM_TEST", baudrate=115200, parity="N", stopbits=1,
            bytesize=8, timeout=0.05, rts=False,
        )
        request = client._request(1, 0, 2)
        client.socket = _FakeSerial(request + _reply(1, [0x4355, 0xD72F]))

        response = client.read_holding_registers(0, 2, device_id=1)

        self.assertFalse(response.isError())
        self.assertEqual(response.registers, [0x4355, 0xD72F])
        self.assertEqual(client.socket.written, request)

    def test_timeout_is_an_error_not_a_fresh_empty_sample(self):
        client = _RawSerialModbusClient(
            port="COM_TEST", baudrate=115200, parity="N", stopbits=1,
            bytesize=8, timeout=0.01, rts=False,
        )
        client.socket = _FakeSerial(b"")

        response = client.read_holding_registers(0, 2, device_id=1)

        self.assertTrue(response.isError())
        self.assertEqual(response.registers, [])
        self.assertIn("no CRC-valid", response.error)


if __name__ == "__main__":
    unittest.main()
