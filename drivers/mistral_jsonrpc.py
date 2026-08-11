"""
MISTRAL PSU JSON-RPC 2.0 client — direct-read alternative to MistralGui screengrab.

Discovered June 23 2026 (see docs/mistral_jsonrpc_discovery.md +
mistral_psu_backend_discovery_jun23.md memory): MistralGui is not a native
app talking to a PSU — it's a client of a JSON-RPC 2.0 backend service
at ``http://10.0.42.231:9000/api`` on the Omicron Lab10 instrument LAN.
Server: Microsoft Kestrel (ASP.NET Core). Brand: "Scandes NoName".
The service is provably multi-client at the HTTP layer — three
independent probes on Jun 23 never disrupted MistralGui's own session.

This module provides:

  1. ``MistralJsonRpcClient`` — a driver matching ``drivers.mistral.MistralGui``'s
     interface (``connect/read/disconnect`` + ``connected/hwnd`` properties,
     ``read()`` returns the same 4-key ``{v_set, v_actual, i_set, i_actual}``
     dict). Drop-in for ``MistralWorker`` via ``mode="jsonrpc"``.

  2. Generic JSON-RPC 2.0 call primitive with proper error handling
     (spec error codes -32700 through -32000 raise :class:`JsonRpcError`;
     transport failures raise :class:`JsonRpcTransportError`).

  3. Discovery helpers — ``try_standard_discovery`` runs the three common
     conventions (``rpc.discover``, ``system.listMethods``, ``system.describe``)
     and returns per-attempt results so the caller can pick whichever
     produced a usable catalog.

**Read-config is not populated until discovery identifies the actual
method names.** Until then ``read()`` returns all-None (safe default).
Once methods are known, populate via :meth:`set_read_config` from a
config file, GUI setting, or the probe script's discovery output.

See ``scripts/test_mistral_jsonrpc_discovery.py`` for the Bulbasaur
probe runner and ``tests/test_mistral_jsonrpc.py`` for the offline
smoke test against an in-process mock server.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)


# --- JSON-RPC 2.0 spec constants ------------------------------------------

# Standard error codes from the JSON-RPC 2.0 spec. Reproduced here so we
# don't take a dep on a jsonrpc client library just to get named codes.
ERR_PARSE = -32700          # Invalid JSON was received
ERR_INVALID_REQUEST = -32600  # The JSON sent is not a valid Request object
ERR_METHOD_NOT_FOUND = -32601  # The method does not exist
ERR_INVALID_PARAMS = -32602    # Invalid method parameters
ERR_INTERNAL = -32603          # Internal JSON-RPC error
# -32000 to -32099: reserved for implementation-defined server errors


# --- Exceptions -----------------------------------------------------------

class JsonRpcError(Exception):
    """A JSON-RPC 2.0 spec-level error from the server.

    ``code`` is the spec-defined error code (e.g. ``-32601`` = method not
    found); ``message`` is the server-supplied description; ``data`` is
    any implementation-specific extra payload.
    """

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class JsonRpcTransportError(Exception):
    """HTTP or network layer failed — request never got a valid JSON-RPC response.

    Covers connection refused / timeout / non-JSON body / server returning
    a non-200 without a well-formed JSON-RPC error envelope. Distinct from
    :class:`JsonRpcError` so callers can retry transport-level failures but
    let spec-level failures propagate (they're usually configuration bugs).
    """


# --- Generic JSON-RPC 2.0 client ------------------------------------------

class MistralJsonRpcClient:
    """Direct-read MISTRAL PSU driver via JSON-RPC 2.0 over HTTP.

    Drop-in for :class:`drivers.mistral.MistralGui`:

    * ``connect()`` — probes HTTP reachability (GET ``/``); doesn't call
      any JSON-RPC method itself, so it's safe even before the read-config
      is populated.
    * ``read()`` — returns the same 4-key dict ``MistralGui.read()``
      returns. Values come from JSON-RPC calls whose method names live in
      ``_read_config``. Empty read-config → all values None.
    * ``disconnect()`` — clears the connected flag; there is no persistent
      HTTP connection to close (urllib manages that per request).

    Plus, JSON-RPC-specific:

    * :meth:`call` — send a request and return the ``result`` field or raise.
    * :meth:`try_standard_discovery` — try ``rpc.discover``,
      ``system.listMethods``, ``system.describe`` and return per-method results.
    * :meth:`set_read_config` — configure which methods populate which
      ``read()`` output keys, after discovery identifies them.

    Multi-PSU note: the current ``read()`` shape mirrors ``MistralGui``'s
    single-active-PSU model. Once we know how the API addresses PSUs
    (per-method params, per-endpoint URLs, subnet-per-PSU IPs), we can add
    a per-PSU read shape as an extension without breaking the 4-key dict.
    """

    DEFAULT_HOST = "10.0.42.231"
    DEFAULT_PORT = 9000
    DEFAULT_PATH = "/api"
    DEFAULT_TIMEOUT_S = 2.0

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        path: str = DEFAULT_PATH,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        self._host = host
        self._port = port
        self._path = path
        self._timeout = timeout
        self._url = f"http://{host}:{port}{path}"
        self._root_url = f"http://{host}:{port}/"
        self._connected = False
        self._request_id = 0
        # ``read()`` mapping: {output_dict_key: (method_name, params_or_None)}.
        # Populated after discovery via :meth:`set_read_config`. Until then
        # ``read()`` returns all-None so the worker sees a working driver
        # with no readings (rather than crashing).
        self._read_config: dict[str, tuple[str, Optional[dict]]] = {}

    # -- connect/read/disconnect (MistralGui interface) --------------------

    def connect(self) -> None:
        """Probe reachability with a GET on the server root.

        Jun 23 discovery showed the Kestrel server returns 200 + a 137-byte
        "Scandes NoName" placeholder for any unmapped path, including ``/``.
        That's a cheap reachability check that doesn't touch the JSON-RPC
        layer, so it works even when ``_read_config`` is empty.
        """
        try:
            req = urllib.request.Request(self._root_url, method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"MistralJsonRpc server at {self._host}:{self._port} "
                        f"returned status {resp.status} on GET / — expected 200"
                    )
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"MistralJsonRpc server unreachable at "
                f"{self._host}:{self._port}: {exc.reason}"
            ) from exc
        self._connected = True
        log.info("MistralJsonRpcClient connected: %s", self._url)

    def read(self) -> dict[str, Optional[float]]:
        """Return the 4-key ``{v_set, v_actual, i_set, i_actual}`` dict.

        Each value is populated from the JSON-RPC method configured in
        ``_read_config[key]``. Missing keys or failing calls return None
        for that key — the shape stays constant. This mirrors
        :meth:`drivers.mistral.MistralGui.read`.
        """
        result: dict[str, Optional[float]] = {
            "v_set": None, "v_actual": None,
            "i_set": None, "i_actual": None,
        }
        if not self._connected:
            return result
        if not self._read_config:
            # Discovery hasn't populated the mapping yet — return all-None
            # rather than errors. Worker sees a working, no-reading driver.
            return result

        for out_key in result:
            entry = self._read_config.get(out_key)
            if entry is None:
                continue
            method, params = entry
            try:
                val = self.call(method, params)
            except (JsonRpcError, JsonRpcTransportError) as exc:
                log.debug(
                    "MistralJsonRpcClient.read() call failed for %s "
                    "(method=%s): %s", out_key, method, exc,
                )
                continue
            # Accept int and float; also accept a top-level {"value": N}
            # envelope, which some vendor APIs return.
            if isinstance(val, dict) and "value" in val:
                val = val["value"]
            if isinstance(val, (int, float)):
                result[out_key] = float(val)
        return result

    def disconnect(self) -> None:
        """Clear the connected flag. No socket to close — urllib is per-request."""
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def hwnd(self) -> int:
        # Symmetric with ElogReader — no window for a network driver.
        return 0

    # -- Read-config configuration ----------------------------------------

    def set_read_config(
        self, config: dict[str, tuple[str, Optional[dict]]]
    ) -> None:
        """Configure which JSON-RPC method populates each ``read()`` output key.

        Example (once discovery identifies the actual methods)::

            client.set_read_config({
                "v_set":    ("getVoltageSetpoint", {"psu": "MBE-Mani"}),
                "v_actual": ("getVoltage",         {"psu": "MBE-Mani"}),
                "i_set":    ("getCurrentSetpoint", {"psu": "MBE-Mani"}),
                "i_actual": ("getCurrent",         {"psu": "MBE-Mani"}),
            })

        Method names and params are placeholders in the docstring — real
        names come from discovery. Params can be ``None`` for parameterless
        methods.
        """
        # Validate keys — accept only the 4 canonical output keys.
        allowed = {"v_set", "v_actual", "i_set", "i_actual"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(
                f"unknown read-config keys {sorted(unknown)}; expected subset "
                f"of {sorted(allowed)}"
            )
        self._read_config = dict(config)

    def get_read_config(self) -> dict[str, tuple[str, Optional[dict]]]:
        return dict(self._read_config)

    # -- Generic JSON-RPC 2.0 primitives ----------------------------------

    def call(
        self,
        method: str,
        params: Optional[dict | list] = None,
    ) -> Any:
        """Send a JSON-RPC 2.0 request and return the ``result`` field.

        Raises:
            JsonRpcError: server responded with a spec-formed error envelope.
            JsonRpcTransportError: network failure, non-JSON body, or a
                200 response missing the expected JSON-RPC fields.
        """
        self._request_id += 1
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._request_id,
        }
        if params is not None:
            body["params"] = params

        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Server returned a well-defined HTTP error. If the body is a
            # JSON-RPC error envelope, prefer that over the raw HTTP code.
            body_bytes = exc.read() if hasattr(exc, "read") else b""
            envelope = _try_parse_envelope(body_bytes)
            if envelope is not None and "error" in envelope:
                err = envelope["error"] or {}
                raise JsonRpcError(
                    int(err.get("code", 0)),
                    str(err.get("message", "")),
                    err.get("data"),
                ) from exc
            raise JsonRpcTransportError(
                f"HTTP {exc.code} from {self._url}: {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise JsonRpcTransportError(
                f"transport error to {self._url}: {exc.reason}"
            ) from exc

        envelope = _try_parse_envelope(raw)
        if envelope is None:
            raise JsonRpcTransportError(
                f"non-JSON response from {self._url}: "
                f"{raw[:200]!r}{'...' if len(raw) > 200 else ''}"
            )

        if envelope.get("jsonrpc") != "2.0":
            raise JsonRpcTransportError(
                f"envelope missing 'jsonrpc: 2.0' marker: {envelope!r}"
            )

        if envelope.get("error"):
            err = envelope["error"]
            raise JsonRpcError(
                int(err.get("code", 0)),
                str(err.get("message", "")),
                err.get("data"),
            )

        return envelope.get("result")

    def try_standard_discovery(self) -> dict[str, dict[str, Any]]:
        """Try the three standard discovery conventions.

        Returns a dict with one entry per attempted method::

            {
                "rpc.discover":       {"ok": True|False, "result": ..., "error": ...},
                "system.listMethods": {...},
                "system.describe":    {...},
            }

        Each entry's ``ok`` is True iff the server returned a non-error
        result. Callers should inspect ``result`` and pick whichever
        produced a usable method catalog.

        Does not raise — captures all errors into the return dict. Safe
        to call blindly against an unknown server (which is the point).
        """
        methods = ["rpc.discover", "system.listMethods", "system.describe"]
        out: dict[str, dict[str, Any]] = {}
        for m in methods:
            try:
                result = self.call(m, params=None)
                out[m] = {"ok": True, "result": result}
            except JsonRpcError as exc:
                out[m] = {
                    "ok": False,
                    "error_code": exc.code,
                    "error_message": exc.message,
                    "error_data": exc.data,
                }
            except JsonRpcTransportError as exc:
                out[m] = {"ok": False, "transport_error": str(exc)}
        return out


# --- Helpers --------------------------------------------------------------

def _try_parse_envelope(raw: bytes) -> Optional[dict]:
    """Return the parsed JSON dict, or None if the body isn't a JSON object.

    JSON-RPC 2.0 responses are objects. Lists are technically valid for
    batch responses but we don't send batches, so if we see a list here
    something's wrong.
    """
    if not raw:
        return None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded
