"""Tests for drivers.ksa_comm — client class + wire protocol + error taxonomy.

Runs offline via a MockSocket; no kSA server required. Locks the wire
format that's been shipping since May 20 2026 (u32 TEXT_CMD encoding,
u32-prefixed text replies, 5-category error taxonomy per Jul 7 2026).

Run: ``python -m pytest -q tests/test_ksa_comm_client.py``.
"""
from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from drivers.ksa_comm import (  # noqa: E402
    CMD_GET_APP_VERSION, CMD_GET_STATUS, CMD_TEXT_CMD,
    ERR_GENERAL, ERR_OK, ERR_UNKNOWN,
    HANDSHAKE_SERVER_STR,
    KsaCommClient, KsaErrorCategory,
    classify_error, decode_text_reply, encode_text_body, err_name,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class MockSocket:
    """Deterministic socket replacement for offline tests.

    Backed by a pre-programmed byte buffer that ``recv`` chunks from. All
    ``sendall`` bytes accumulate in ``.sent`` for post-hoc assertion.
    Raises ``ConnectionError`` if a test tries to ``recv`` past the end
    of the buffer — mirrors the real socket behavior at connection loss.
    """

    def __init__(self, recv_data: bytes = b""):
        self._recv_buf = recv_data
        self.sent = b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        if not self._recv_buf:
            raise ConnectionError("MockSocket recv buffer exhausted")
        chunk = self._recv_buf[:n]
        self._recv_buf = self._recv_buf[n:]
        return chunk

    def close(self) -> None:
        pass


def _short_string_frame(s: str) -> bytes:
    """Wire-format encoding of a short-string frame."""
    payload = s.encode("ascii") + b"\x00"
    return bytes([len(payload)]) + payload


def _handshake_server_response(version: int = 6) -> bytes:
    """Bytes the server sends during the handshake: server-name
    short-string + u16 LE protocol version."""
    return _short_string_frame(HANDSHAKE_SERVER_STR) + struct.pack("<H", version)


def _binary_reply(cmd_code: int, err_code: int, reply_data: bytes) -> bytes:
    """Wrap ``reply_data`` in a binary reply frame."""
    return (
        struct.pack("<HhH", cmd_code, err_code, len(reply_data))
        + reply_data
    )


def _text_reply_payload(text: str) -> bytes:
    """u32-prefixed text payload (as kSA emits for both OK + error replies)."""
    body = text.encode("ascii") + b"\x00"
    return struct.pack("<I", len(body)) + body


# ---------------------------------------------------------------------------
# encode_text_body — the 4 candidate encodings, u32 is the working one on v6
# ---------------------------------------------------------------------------

class EncodingTests(unittest.TestCase):

    def test_u32_encoding_length_includes_null(self):
        # Body: [u32 LE length-including-null][chars][null]
        # For "?": length=2 (1 char + 1 null); expected 6 bytes total.
        body = encode_text_body("?", "u32")
        self.assertEqual(body, b"\x02\x00\x00\x00?\x00")

    def test_raw_encoding_is_bare_ascii(self):
        self.assertEqual(encode_text_body("hi", "raw"), b"hi")

    def test_short_encoding_is_short_string(self):
        # [u8 length-including-null][chars][null]. "hi" → length=3.
        self.assertEqual(encode_text_body("hi", "short"), b"\x03hi\x00")

    def test_u16_encoding_chars_only_length_no_null(self):
        # [u16 LE chars-only length][chars]
        self.assertEqual(encode_text_body("hi", "u16"), b"\x02\x00hi")

    def test_unknown_encoding_raises(self):
        with self.assertRaises(ValueError):
            encode_text_body("hi", "utf32")


# ---------------------------------------------------------------------------
# decode_text_reply — server always u32-prefixes text; same shape err vs OK
# ---------------------------------------------------------------------------

class DecodingTests(unittest.TestCase):

    def test_u32_prefixed_text_stripped(self):
        payload = b"OK\x00"
        data = struct.pack("<I", len(payload)) + payload
        self.assertEqual(decode_text_reply(data), "OK")

    def test_multiline_reply_preserves_newlines(self):
        payload = b"line1\nline2\x00"
        data = struct.pack("<I", len(payload)) + payload
        self.assertEqual(decode_text_reply(data), "line1\nline2")

    def test_reply_too_short_returns_empty(self):
        self.assertEqual(decode_text_reply(b""), "")
        self.assertEqual(decode_text_reply(b"\x00\x00"), "")


# ---------------------------------------------------------------------------
# err_name — human-readable wire error codes
# ---------------------------------------------------------------------------

class ErrorNameTests(unittest.TestCase):

    def test_known_codes(self):
        self.assertEqual(err_name(ERR_OK), "Success")
        self.assertEqual(err_name(ERR_UNKNOWN), "Unknown command")
        self.assertEqual(err_name(ERR_GENERAL), "General error")

    def test_unknown_code_falls_back(self):
        self.assertEqual(err_name(-99), "unknown(-99)")


# ---------------------------------------------------------------------------
# classify_error — Jul 7 taxonomy; 5 categories + unclassified
# ---------------------------------------------------------------------------

class ErrorClassificationTests(unittest.TestCase):

    def test_unsupported_root_command_is_wrong_vocabulary(self):
        self.assertEqual(
            classify_error("unsupported root command!"),
            KsaErrorCategory.WRONG_VOCABULARY,
        )

    def test_iappcmdprocessor_unhandled_is_wrong_vocabulary(self):
        self.assertEqual(
            classify_error("IAppCmdProcessor - Unhandled Command"),
            KsaErrorCategory.WRONG_VOCABULARY,
        )

    def test_manager_not_available_is_not_instantiated(self):
        self.assertEqual(
            classify_error("alarm manager not available!"),
            KsaErrorCategory.NOT_INSTANTIATED,
        )

    def test_controller_not_found_is_not_instantiated(self):
        self.assertEqual(
            classify_error("acquire controller not found!"),
            KsaErrorCategory.NOT_INSTANTIATED,
        )

    def test_dialog_not_available_is_dialog_closed(self):
        self.assertEqual(
            classify_error("gun control dialog not available!"),
            KsaErrorCategory.DIALOG_CLOSED,
        )

    def test_bare_not_available_is_not_in_build(self):
        # "mfc not available!" — no "manager", no "dialog" prefix.
        # Documents the feature-absent case (mfc doesn't ship with the
        # kSA 400 install per Jul 7 findings).
        self.assertEqual(
            classify_error("mfc not available!"),
            KsaErrorCategory.NOT_IN_BUILD,
        )

    def test_cannot_find_file_is_session_required(self):
        self.assertEqual(
            classify_error("The system cannot find the file specified."),
            KsaErrorCategory.SESSION_REQUIRED,
        )

    def test_unknown_phrase_is_unclassified(self):
        self.assertEqual(
            classify_error("something we've never seen before"),
            KsaErrorCategory.UNCLASSIFIED,
        )

    def test_classification_is_case_insensitive(self):
        # Jul 7 memory records phrasings in mixed case (e.g.
        # "IAppCmdProcessor"). classify_error lowercases both sides
        # so a variant capitalization doesn't fall through.
        self.assertEqual(
            classify_error("Unsupported Root Command!"),
            KsaErrorCategory.WRONG_VOCABULARY,
        )


# ---------------------------------------------------------------------------
# KsaCommClient lifecycle
# ---------------------------------------------------------------------------

class ClientLifecycleTests(unittest.TestCase):

    def test_constructor_defaults(self):
        client = KsaCommClient()
        self.assertEqual(client.host, "127.0.0.1")
        self.assertEqual(client.port, 1800)
        self.assertFalse(client.connected)
        self.assertIsNone(client.protocol_version)

    def test_constructor_custom_endpoint(self):
        client = KsaCommClient(host="10.0.42.99", port=1234, timeout=1.5)
        self.assertEqual(client.host, "10.0.42.99")
        self.assertEqual(client.port, 1234)
        self.assertEqual(client.timeout, 1.5)

    def test_connect_does_handshake_and_records_protocol_version(self):
        mock_sock = MockSocket(_handshake_server_response(version=6))
        client = KsaCommClient()
        with patch(
            "drivers.ksa_comm.socket.create_connection",
            return_value=mock_sock,
        ):
            client.connect()
        self.assertTrue(client.connected)
        self.assertEqual(client.protocol_version, 6)
        # Client should have sent exactly the "ksacomm_client" short-string.
        self.assertEqual(mock_sock.sent, _short_string_frame("ksacomm_client"))

    def test_connect_wrong_server_string_raises_and_stays_disconnected(self):
        wrong_response = (
            _short_string_frame("bogus_server") + struct.pack("<H", 6)
        )
        mock_sock = MockSocket(wrong_response)
        client = KsaCommClient()
        with patch(
            "drivers.ksa_comm.socket.create_connection",
            return_value=mock_sock,
        ):
            with self.assertRaises(ConnectionError):
                client.connect()
        self.assertFalse(client.connected)
        self.assertIsNone(client.protocol_version)

    def test_disconnect_when_not_connected_is_safe(self):
        # Never connected; disconnect must not raise.
        client = KsaCommClient()
        client.disconnect()
        self.assertFalse(client.connected)

    def test_send_binary_when_not_connected_raises(self):
        client = KsaCommClient()
        with self.assertRaises(ConnectionError):
            client.send_binary(CMD_GET_STATUS)

    def test_send_text_when_not_connected_raises(self):
        client = KsaCommClient()
        with self.assertRaises(ConnectionError):
            client.send_text("version")

    def test_context_manager_connects_and_disconnects(self):
        mock_sock = MockSocket(_handshake_server_response())
        with patch(
            "drivers.ksa_comm.socket.create_connection",
            return_value=mock_sock,
        ):
            with KsaCommClient() as client:
                self.assertTrue(client.connected)
            self.assertFalse(client.connected)


# ---------------------------------------------------------------------------
# send_text end-to-end (encoding + wire + decoding)
# ---------------------------------------------------------------------------

class SendTextTests(unittest.TestCase):

    def _connected_client_with_text_reply(
        self, err_code: int, reply_text: str,
    ) -> tuple[KsaCommClient, MockSocket]:
        """Connected client whose next TEXT_CMD receives a u32-prefixed
        text reply."""
        reply_frame = _binary_reply(
            CMD_TEXT_CMD, err_code, _text_reply_payload(reply_text),
        )
        mock_sock = MockSocket(_handshake_server_response() + reply_frame)
        client = KsaCommClient()
        with patch(
            "drivers.ksa_comm.socket.create_connection",
            return_value=mock_sock,
        ):
            client.connect()
        return client, mock_sock

    def test_send_text_success_returns_ok_and_reply(self):
        client, _ = self._connected_client_with_text_reply(
            ERR_OK, "version 5.85",
        )
        err, reply = client.send_text("version")
        self.assertEqual(err, ERR_OK)
        self.assertEqual(reply, "version 5.85")

    def test_send_text_error_still_returns_reply_text(self):
        # err=-1 (General error) with descriptive text — same
        # u32-prefixed reply shape as a success reply.
        client, _ = self._connected_client_with_text_reply(
            ERR_GENERAL, "unsupported root command!",
        )
        err, reply = client.send_text("nonsense")
        self.assertEqual(err, ERR_GENERAL)
        self.assertEqual(reply, "unsupported root command!")

    def test_send_text_error_reply_feeds_classify_error(self):
        # Full loop: send_text → err + reply → classify_error → category.
        client, _ = self._connected_client_with_text_reply(
            ERR_GENERAL, "The system cannot find the file specified.",
        )
        err, reply = client.send_text("app query SQL 'SELECT 1'")
        self.assertNotEqual(err, ERR_OK)
        self.assertEqual(
            classify_error(reply),
            KsaErrorCategory.SESSION_REQUIRED,
        )

    def test_send_text_default_encoding_is_u32_on_wire(self):
        # Verify the actual bytes hitting the socket match the u32
        # encoding — the v6-working format. A regression here would
        # silently downgrade every TEXT_CMD to err=-2.
        client, mock_sock = self._connected_client_with_text_reply(ERR_OK, "")
        baseline_sent_len = len(mock_sock.sent)
        client.send_text("?")
        new_bytes = mock_sock.sent[baseline_sent_len:]
        # Wire: [CmdCode:2 LE = 1011][CmdLen:2 LE = 6][u32 body-len:4 LE][?][null]
        expected = (
            struct.pack("<HH", CMD_TEXT_CMD, 6)  # command header
            + b"\x02\x00\x00\x00?\x00"           # u32-encoded body
        )
        self.assertEqual(new_bytes, expected)


# ---------------------------------------------------------------------------
# Typed high-level commands
# ---------------------------------------------------------------------------

class TypedCommandTests(unittest.TestCase):

    def _connected_client_with_binary_reply(
        self, cmd_code: int, err_code: int, reply_data: bytes,
    ) -> KsaCommClient:
        mock_sock = MockSocket(
            _handshake_server_response()
            + _binary_reply(cmd_code, err_code, reply_data)
        )
        client = KsaCommClient()
        with patch(
            "drivers.ksa_comm.socket.create_connection",
            return_value=mock_sock,
        ):
            client.connect()
        return client

    def test_get_app_version_parses_length_prefixed_string(self):
        version_str = "5.85.20221128"
        # Reply format: [len:1][chars:len]
        reply_data = bytes([len(version_str)]) + version_str.encode("ascii")
        client = self._connected_client_with_binary_reply(
            CMD_GET_APP_VERSION, ERR_OK, reply_data,
        )
        self.assertEqual(client.get_app_version(), version_str)

    def test_get_app_version_error_returns_none(self):
        client = self._connected_client_with_binary_reply(
            CMD_GET_APP_VERSION, ERR_UNKNOWN, b"",
        )
        self.assertIsNone(client.get_app_version())

    def test_get_status_parses_full_dict(self):
        # 24-byte status payload per protocol section 5.10:
        # StatusSize=24, StatusVersion=1, OpStatus=1 (Idle),
        # LastHome=0.0, RpmStatus=1 (Stable), Rpm=15.5.
        status_data = (
            struct.pack("<HHH", 24, 1, 1)
            + struct.pack("<d", 0.0)
            + struct.pack("<H", 1)
            + struct.pack("<d", 15.5)
        )
        client = self._connected_client_with_binary_reply(
            CMD_GET_STATUS, ERR_OK, status_data,
        )
        status = client.get_status()
        self.assertIsNotNone(status)
        self.assertEqual(status["op_status"], 1)
        self.assertEqual(status["op_status_name"], "Idle")
        self.assertEqual(status["rpm"], 15.5)
        self.assertEqual(status["rpm_status_name"], "Stable")

    def test_get_status_reply_too_short_returns_none(self):
        client = self._connected_client_with_binary_reply(
            CMD_GET_STATUS, ERR_OK, b"\x00\x00",  # 2 bytes; need 24
        )
        self.assertIsNone(client.get_status())

    def test_get_status_error_returns_none(self):
        client = self._connected_client_with_binary_reply(
            CMD_GET_STATUS, ERR_UNKNOWN, b"",
        )
        self.assertIsNone(client.get_status())


if __name__ == "__main__":
    unittest.main(verbosity=2)
