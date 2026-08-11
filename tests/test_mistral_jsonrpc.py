#!/usr/bin/env python3
"""End-to-end test of MistralJsonRpcClient against an in-process JSON-RPC 2.0 mock.

Validates request formation, response parsing, error handling, and the
drop-in MistralGui interface (``connect/read/disconnect`` + read-config
population) — all Mac-side, without needing Bulbasaur / the real MISTRAL
backend.

Runs an ephemeral HTTP server on 127.0.0.1 that implements a small
JSON-RPC 2.0 method catalog and the specific error paths we need to
exercise: parse error, method-not-found, transport error.

Usage:
    python -m pytest -q tests/test_mistral_jsonrpc.py

Exits 0 on success; raises AssertionError with a diagnostic on failure.

Built Jul 2 2026 as a pre-lab safety net for the Jun 23 discovery. If
this passes on Mac, the client is safe to run on Bulbasaur; anything
that fails at the lab is then a real-server problem, not a wire-format
one.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from drivers.mistral_jsonrpc import (  # noqa: E402
    ERR_METHOD_NOT_FOUND,
    ERR_PARSE,
    JsonRpcError,
    JsonRpcTransportError,
    MistralJsonRpcClient,
)


# ---------------------------------------------------------------------------
# Mock JSON-RPC 2.0 server
# ---------------------------------------------------------------------------

MOCK_METHODS: dict[str, object] = {
    # Plain float returns — most common vendor shape
    "getVoltage":         12.34,
    "getCurrent":         5.67,
    "getVoltageSetpoint": 12.50,
    "getCurrentSetpoint": 6.00,
    # Dict-envelope returns — some .NET / OPC-UA-flavored APIs
    "getVoltageEnveloped": {"value": 42.42, "unit": "V", "quality": "GOOD"},
    # Discovery-style
    "system.listMethods": [
        "getVoltage", "getCurrent",
        "getVoltageSetpoint", "getCurrentSetpoint",
        "getVoltageEnveloped", "system.listMethods",
    ],
}


class MockJsonRpcHandler(BaseHTTPRequestHandler):
    """Minimal JSON-RPC 2.0 responder with the shapes we need to validate."""

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003, ARG002
        # Silence the default per-request log spam; tests print their own status.
        pass

    def do_GET(self) -> None:  # noqa: N802
        # Mirrors Kestrel's 137-byte "Scandes NoName" placeholder — anything
        # is fine here so long as it's a 200. The client's connect() only
        # cares about the status code.
        body = b"<html><body>Mock JSON-RPC server root</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""

        # Try to parse the envelope. Any parse failure → -32700 with a null id.
        try:
            req = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reply_error(None, ERR_PARSE, "Parse error")
            return

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params")

        if method not in MOCK_METHODS:
            self._reply_error(req_id, ERR_METHOD_NOT_FOUND, f"Method not found: {method}")
            return

        # Params are unused in the mock — we just echo the catalog entry.
        # Real server would validate params here.
        _ = params
        self._reply_result(req_id, MOCK_METHODS[method])

    def _reply_result(self, req_id, result) -> None:
        self._reply({"jsonrpc": "2.0", "result": result, "id": req_id})

    def _reply_error(self, req_id, code: int, message: str) -> None:
        self._reply({
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": req_id,
        })

    def _reply(self, envelope: dict) -> None:
        body = json.dumps(envelope).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MockServer:
    """Context manager around a threaded HTTP server on an ephemeral port."""

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def __enter__(self) -> "MockServer":
        # Port 0 → OS picks an ephemeral port; server.server_address[1] tells us which.
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), MockJsonRpcHandler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="mock-jsonrpc-server",
            daemon=True,
        )
        self._thread.start()
        # Wait until the socket actually accepts — server_close before serve
        # starts race-conditions have caused flaky tests in the past.
        _wait_for_port("127.0.0.1", self.port, timeout=2.0)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def _wait_for_port(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"mock server never came up on {host}:{port}")


def _find_free_port() -> int:
    """Return an OS-assigned free port for tests that need a *dead* address."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_connect_reachable() -> None:
    """connect() succeeds against a running server; connected → True."""
    with MockServer() as srv:
        c = MistralJsonRpcClient(host="127.0.0.1", port=srv.port, timeout=1.0)
        assert not c.connected
        c.connect()
        assert c.connected
        c.disconnect()
        assert not c.connected


def test_connect_unreachable() -> None:
    """connect() raises RuntimeError against a dead port (no server)."""
    dead_port = _find_free_port()
    c = MistralJsonRpcClient(host="127.0.0.1", port=dead_port, timeout=0.5)
    try:
        c.connect()
    except RuntimeError as exc:
        assert "unreachable" in str(exc), f"unexpected message: {exc}"
        return
    raise AssertionError("expected RuntimeError, none raised")


def test_call_roundtrip() -> None:
    """call('getVoltage') returns the mock's 12.34 float."""
    with MockServer() as srv:
        c = MistralJsonRpcClient(host="127.0.0.1", port=srv.port, timeout=1.0)
        c.connect()
        result = c.call("getVoltage")
        assert result == 12.34, f"expected 12.34, got {result!r}"


def test_call_method_not_found() -> None:
    """call() on an unknown method raises JsonRpcError with code -32601."""
    with MockServer() as srv:
        c = MistralJsonRpcClient(host="127.0.0.1", port=srv.port, timeout=1.0)
        c.connect()
        try:
            c.call("does_not_exist_anywhere")
        except JsonRpcError as exc:
            assert exc.code == ERR_METHOD_NOT_FOUND, f"got code {exc.code}"
            return
        raise AssertionError("expected JsonRpcError, none raised")


def test_standard_discovery_partial() -> None:
    """try_standard_discovery: system.listMethods works, the other two 404."""
    with MockServer() as srv:
        c = MistralJsonRpcClient(host="127.0.0.1", port=srv.port, timeout=1.0)
        c.connect()
        out = c.try_standard_discovery()
        assert set(out) == {"rpc.discover", "system.listMethods", "system.describe"}
        assert not out["rpc.discover"]["ok"]
        assert out["rpc.discover"]["error_code"] == ERR_METHOD_NOT_FOUND
        assert out["system.listMethods"]["ok"]
        assert "getVoltage" in out["system.listMethods"]["result"]
        assert not out["system.describe"]["ok"]


def test_read_with_config() -> None:
    """read() with a valid config returns a populated dict of the right shape."""
    with MockServer() as srv:
        c = MistralJsonRpcClient(host="127.0.0.1", port=srv.port, timeout=1.0)
        c.connect()
        c.set_read_config({
            "v_set":    ("getVoltageSetpoint", None),
            "v_actual": ("getVoltage",         None),
            "i_set":    ("getCurrentSetpoint", None),
            "i_actual": ("getCurrent",         None),
        })
        vals = c.read()
        assert vals == {
            "v_set":    12.50,
            "v_actual": 12.34,
            "i_set":    6.00,
            "i_actual": 5.67,
        }, f"unexpected read() dict: {vals}"


def test_read_empty_config() -> None:
    """read() with no config returns the 4-key all-None shape, no error."""
    with MockServer() as srv:
        c = MistralJsonRpcClient(host="127.0.0.1", port=srv.port, timeout=1.0)
        c.connect()
        vals = c.read()
        assert vals == {
            "v_set": None, "v_actual": None,
            "i_set": None, "i_actual": None,
        }, f"unexpected empty-config dict: {vals}"


def test_read_before_connect() -> None:
    """read() before connect() returns all-None (silent, no exception)."""
    c = MistralJsonRpcClient(host="127.0.0.1", port=1, timeout=0.5)
    vals = c.read()  # no server, no connect — should not raise
    assert vals == {
        "v_set": None, "v_actual": None,
        "i_set": None, "i_actual": None,
    }


def test_read_envelope_unwrap() -> None:
    """A method returning {'value': N} → read() gets N."""
    with MockServer() as srv:
        c = MistralJsonRpcClient(host="127.0.0.1", port=srv.port, timeout=1.0)
        c.connect()
        c.set_read_config({"v_actual": ("getVoltageEnveloped", None)})
        vals = c.read()
        assert vals["v_actual"] == 42.42, (
            f"expected 42.42 unwrapped from envelope, got {vals['v_actual']!r}"
        )
        # Other keys stayed None because we only configured v_actual.
        assert vals["v_set"] is None and vals["i_set"] is None


def test_read_transport_error_returns_none() -> None:
    """After the server dies mid-session, read() returns all-None (not raise)."""
    srv = MockServer()
    srv.__enter__()
    try:
        c = MistralJsonRpcClient(host="127.0.0.1", port=srv.port, timeout=0.5)
        c.connect()
        c.set_read_config({"v_actual": ("getVoltage", None)})
        vals = c.read()
        assert vals["v_actual"] == 12.34
    finally:
        srv.__exit__(None, None, None)

    # Server is now dead. Read again — should silently return None, not crash.
    vals = c.read()
    assert vals == {
        "v_set": None, "v_actual": None,
        "i_set": None, "i_actual": None,
    }, f"expected all-None after transport failure, got {vals}"


def test_set_read_config_rejects_bogus_key() -> None:
    """set_read_config() with an unknown key raises ValueError before any I/O."""
    c = MistralJsonRpcClient(host="127.0.0.1", port=1)
    try:
        c.set_read_config({"bogus": ("m", None)})
    except ValueError as exc:
        assert "bogus" in str(exc)
        return
    raise AssertionError("expected ValueError for unknown key")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_connect_reachable,
    test_connect_unreachable,
    test_call_roundtrip,
    test_call_method_not_found,
    test_standard_discovery_partial,
    test_read_with_config,
    test_read_empty_config,
    test_read_before_connect,
    test_read_envelope_unwrap,
    test_read_transport_error_returns_none,
    test_set_read_config_rejects_bogus_key,
]


def main() -> int:
    print(f"MistralJsonRpcClient smoke test — {len(TESTS)} cases")
    print()
    failures: list[tuple[str, BaseException]] = []
    for t in TESTS:
        name = t.__name__
        try:
            t()
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"  ✗ {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ✓ {name}")

    print()
    if failures:
        print(f"FAIL — {len(failures)}/{len(TESTS)} tests failed")
        return 1
    print(f"PASS — {len(TESTS)}/{len(TESTS)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
