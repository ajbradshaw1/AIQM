"""Loopback-only control channel for the desktop RHEED point-event labeler.

The generated report is ordinary static HTML.  When it is opened through the
desktop labeler, this module serves that same directory from ``127.0.0.1`` and
adds a small authenticated API for provenance-sensitive actions.  The random
token is process-local and deliberately never written to the report or disk.

The HTTP worker never opens Qt widgets.  It only enqueues a request; the
desktop controller receives the callback and performs Equalizer work on the
Qt main thread.  This split keeps untrusted browser input away from both raw
ZIP reads and calibration acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import parse_qs, quote, urlsplit
import uuid


MAX_REQUEST_BYTES = 128 * 1024
_ALLOWED_STATIC_ROOTS = frozenset({"interactive_report.html", "images", "vendor"})


@dataclass(frozen=True)
class EqualizerRequest:
    request_id: str
    event_id: str
    review_frame_index: int
    reviewer: str


class _LoopbackHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class LoopbackReportService:
    """Serve one report and authenticated desktop-control requests.

    ``equalizer_callback`` is called from an HTTP worker thread.  A PyQt
    caller must bridge that callback through a queued signal before touching
    widgets.  Results are returned later with :meth:`complete_equalizer` or
    :meth:`fail_equalizer` and polled by the report.
    """

    def __init__(
        self,
        report_path: str | Path,
        *,
        equalizer_callback: Callable[[EqualizerRequest], None],
        revision_callback: Callable[[Mapping[str, object]], Mapping[str, object]]
        | None = None,
        equalizer_revision_callback: Callable[
            [Mapping[str, object], Mapping[str, object]], Mapping[str, object]
        ] | None = None,
        import_callback: Callable[[Mapping[str, object]], Mapping[str, object]]
        | None = None,
        state_callback: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        report = Path(report_path).expanduser().resolve()
        if not report.is_file() or report.name != "interactive_report.html":
            raise ValueError("Loopback service requires interactive_report.html")
        self.report_path = report
        self.report_root = report.parent
        self.token = secrets.token_urlsafe(32)
        self._equalizer_callback = equalizer_callback
        self._revision_callback = revision_callback
        self._equalizer_revision_callback = equalizer_revision_callback
        self._import_callback = import_callback
        self._state_callback = state_callback
        self._lock = threading.RLock()
        self._requests: dict[str, dict[str, object]] = {}
        self._server: _LoopbackHttpServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    @property
    def base_url(self) -> str:
        server = self._server
        if server is None:
            raise RuntimeError("Loopback report service is not running")
        host, port = server.server_address[:2]
        if host != "127.0.0.1":
            raise RuntimeError("Loopback report service escaped the loopback interface")
        return f"http://127.0.0.1:{int(port)}"

    @property
    def report_url(self) -> str:
        return f"{self.base_url}/interactive_report.html?bridge_token={quote(self.token)}"

    def start(self) -> str:
        if self.running:
            return self.report_url
        handler = partial(_ReportRequestHandler, service=self)
        server = _LoopbackHttpServer(("127.0.0.1", 0), handler)
        if server.server_address[0] != "127.0.0.1":
            server.server_close()
            raise RuntimeError("Control service did not bind to 127.0.0.1")
        thread = threading.Thread(
            target=server.serve_forever,
            name="rheed-labeler-loopback",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()
        return self.report_url

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        with self._lock:
            for state in self._requests.values():
                if state.get("status") == "pending":
                    state.update({
                        "status": "error",
                        "error": "Desktop labeler closed before Equalizer finished.",
                    })

    def enqueue_equalizer(self, payload: Mapping[str, object]) -> dict[str, object]:
        event_id = str(payload.get("event_id") or "").strip()
        reviewer = str(payload.get("reviewer") or "").strip()
        raw_index = payload.get("review_frame_index")
        if not event_id or len(event_id) > 160 or any(ord(char) < 33 for char in event_id):
            raise ValueError("event_id is missing or invalid")
        if not reviewer:
            raise ValueError("Enter the reviewer before running Equalizer")
        if type(raw_index) is not int or raw_index < 1:
            raise ValueError("review_frame_index must be a positive saved-frame ordinal")
        request = EqualizerRequest(
            request_id=str(uuid.uuid4()),
            event_id=event_id,
            review_frame_index=raw_index,
            reviewer=reviewer,
        )
        with self._lock:
            self._requests[request.request_id] = {
                "status": "pending",
                "event_id": event_id,
                "review_frame_index": raw_index,
            }
            # Bound memory during a long review session.  Never discard a
            # request that is still pending.
            completed = [
                key for key, value in self._requests.items()
                if value.get("status") != "pending"
            ]
            for old in completed[:-128]:
                self._requests.pop(old, None)
        try:
            self._equalizer_callback(request)
        except Exception as exc:
            self.fail_equalizer(request.request_id, f"Equalizer request dispatch failed: {exc}")
        return {"request_id": request.request_id, "status": "pending"}

    def equalizer_status(self, request_id: str) -> dict[str, object]:
        with self._lock:
            state = self._requests.get(str(request_id))
            if state is None:
                raise KeyError("Unknown Equalizer request")
            return dict(state)

    def complete_equalizer(
        self,
        request_id: str,
        measurement: Mapping[str, object],
    ) -> None:
        canonical = json.loads(json.dumps(measurement, allow_nan=False))
        if not isinstance(canonical, dict):
            raise ValueError("Equalizer measurement must be an object")
        with self._lock:
            state = self._requests.get(str(request_id))
            if state is None or state.get("status") != "pending":
                raise KeyError("Equalizer request is not pending")
            state.update({
                "status": "complete",
                "measurement": canonical,
                "result_token": secrets.token_urlsafe(32),
                "consumed": False,
            })

    def fail_equalizer(self, request_id: str, reason: str) -> None:
        reason = str(reason or "Equalizer failed").strip()
        with self._lock:
            state = self._requests.get(str(request_id))
            if state is None or state.get("status") != "pending":
                return
            state.update({"status": "error", "error": reason})

    def append_revision(self, payload: Mapping[str, object]) -> dict[str, object]:
        if str(payload.get("action", "")) == "set_equalizer":
            callback = self._equalizer_revision_callback
            if callback is None:
                raise RuntimeError("Trusted Equalizer revision persistence is unavailable")
            changes = payload.get("changes")
            if not isinstance(changes, Mapping) or set(changes) != {"equalizer_result_token"}:
                raise ValueError(
                    "set_equalizer requires exactly one server-issued result token"
                )
            token = str(changes.get("equalizer_result_token", ""))
            with self._lock:
                matches = [
                    state for state in self._requests.values()
                    if state.get("result_token") == token
                ]
                if len(matches) != 1:
                    raise ValueError("Equalizer result token is invalid")
                state = matches[0]
                if state.get("status") != "complete" or state.get("consumed") is True:
                    raise ValueError("Equalizer result token was already consumed")
                if str(state.get("event_id", "")) != str(payload.get("event_id", "")):
                    raise ValueError("Equalizer result belongs to a different event")
                measurement = state.get("measurement")
                if not isinstance(measurement, Mapping):
                    raise ValueError("Server-held Equalizer result is unavailable")
                trusted_command = dict(payload)
                trusted_command["changes"] = {}
                # Serialize redemption with the durable write: the token is
                # consumed only after the server-owned measurement commits.
                result = callback(trusted_command, measurement)
                state["consumed"] = True
                state["status"] = "consumed"
                state.pop("measurement", None)
            canonical = json.loads(json.dumps(result, allow_nan=False))
            if not isinstance(canonical, dict):
                raise ValueError("Revision persistence returned an invalid response")
            return canonical
        callback = self._revision_callback
        if callback is None:
            raise RuntimeError("Desktop revision persistence is unavailable")
        result = callback(payload)
        canonical = json.loads(json.dumps(result, allow_nan=False))
        if not isinstance(canonical, dict):
            raise ValueError("Revision persistence returned an invalid response")
        return canonical

    def current_state(self) -> dict[str, object]:
        callback = self._state_callback
        if callback is None:
            raise RuntimeError("Desktop event-state persistence is unavailable")
        result = callback()
        canonical = json.loads(json.dumps(result, allow_nan=False))
        if not isinstance(canonical, dict):
            raise ValueError("Event-state persistence returned an invalid response")
        return canonical

    def import_events(self, payload: Mapping[str, object]) -> dict[str, object]:
        callback = self._import_callback
        if callback is None:
            raise RuntimeError("Desktop Draft import is unavailable")
        document = payload.get("document")
        if not isinstance(document, Mapping):
            raise ValueError("Draft import requires one annotation document")
        result = callback(document)
        canonical = json.loads(json.dumps(result, allow_nan=False))
        if not isinstance(canonical, dict):
            raise ValueError("Draft import returned an invalid response")
        return canonical

    def authorized(self, handler: SimpleHTTPRequestHandler) -> bool:
        parsed = urlsplit(handler.path)
        query_token = parse_qs(parsed.query).get("bridge_token", [""])[0]
        header_token = handler.headers.get("X-AI4MBE-Bridge-Token", "")
        supplied = header_token or query_token
        return bool(supplied) and secrets.compare_digest(supplied, self.token)


class _ReportRequestHandler(SimpleHTTPRequestHandler):
    """Strict static/report API handler; no directory listing or CGI."""

    server_version = "AI4MBE-RHEED-Labeler/1"

    def __init__(self, *args, service: LoopbackReportService, **kwargs) -> None:
        self.service = service
        super().__init__(*args, directory=str(service.report_root), **kwargs)

    def log_message(self, _format: str, *_args: object) -> None:
        # Avoid leaking the session token through ordinary HTTP access logs.
        return

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, status: HTTPStatus, payload: Mapping[str, object]) -> None:
        data = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _api_authorized(self) -> bool:
        if self.service.authorized(self):
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid desktop session token"})
        return False

    def _read_json(self) -> Mapping[str, object]:
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length is invalid") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is empty or too large")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        return value

    def do_GET(self) -> None:  # noqa: N802 - HTTP API
        parsed = urlsplit(self.path)
        if parsed.path == "/api/status":
            if not self._api_authorized():
                return
            self._json(HTTPStatus.OK, {
                "schema_version": 1,
                "desktop": True,
                "equalizer_available": True,
                "revision_persistence_available": self.service._revision_callback is not None,
                "event_state_available": self.service._state_callback is not None,
                "event_import_available": self.service._import_callback is not None,
            })
            return
        if parsed.path == "/api/events":
            if not self._api_authorized():
                return
            try:
                result = self.service.current_state()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            else:
                self._json(HTTPStatus.OK, result)
            return
        if parsed.path == "/api/equalizer/result":
            if not self._api_authorized():
                return
            request_id = parse_qs(parsed.query).get("request_id", [""])[0]
            try:
                result = self.service.equalizer_status(request_id)
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            else:
                self._json(HTTPStatus.OK, result)
            return
        self._serve_static(parsed.path)

    def do_HEAD(self) -> None:  # noqa: N802 - HTTP API
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - HTTP API
        parsed = urlsplit(self.path)
        if parsed.path not in {
            "/api/equalizer/request", "/api/revisions", "/api/events/import",
        }:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown API endpoint"})
            return
        if not self._api_authorized():
            return
        try:
            payload = self._read_json()
            if parsed.path == "/api/equalizer/request":
                result = self.service.enqueue_equalizer(payload)
                status = HTTPStatus.ACCEPTED
            elif parsed.path == "/api/events/import":
                result = self.service.import_events(payload)
                status = HTTPStatus.OK
            else:
                result = self.service.append_revision(payload)
                status = HTTPStatus.OK
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(status, result)

    def _serve_static(self, raw_path: str) -> None:
        normalized = raw_path.lstrip("/") or "interactive_report.html"
        parts = Path(normalized).parts
        if (
            not parts
            or parts[0] not in _ALLOWED_STATIC_ROOTS
            or any(part in {"", ".", ".."} for part in parts)
        ):
            self.send_error(HTTPStatus.NOT_FOUND.value)
            return
        try:
            candidate = (self.service.report_root / Path(*parts)).resolve(strict=True)
            candidate.relative_to(self.service.report_root)
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND.value)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND.value)
            return
        # SimpleHTTPRequestHandler handles byte ranges and MIME types after
        # translate_path; the strict preflight above prevents traversal.
        self.path = "/" + "/".join(parts)
        if self.command == "HEAD":
            super().do_HEAD()
        else:
            super().do_GET()


__all__ = ["EqualizerRequest", "LoopbackReportService"]
