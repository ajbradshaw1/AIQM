"""Loopback-only control channel for the desktop RHEED point-event labeler.

The generated report is ordinary static HTML.  When it is opened through the
desktop labeler, this module serves that same directory from ``127.0.0.1`` and
adds a small authenticated API for provenance-sensitive actions.  The random
token is process-local and deliberately never written to the report or disk.

The HTTP worker never opens Qt widgets.  The current point-event editor uses
this service only for authenticated revision, import, and recovery operations.
Historical Equalizer request data structures remain importable by offline
diagnostic modules, but the point-event control channel rejects those actions.
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
        equalizer_callback: Callable[[EqualizerRequest], None] | None = None,
        revision_callback: Callable[[Mapping[str, object]], Mapping[str, object]]
        | None = None,
        equalizer_revision_callback: Callable[
            [Mapping[str, object], Mapping[str, object]], Mapping[str, object]
        ] | None = None,
        import_callback: Callable[[Mapping[str, object]], Mapping[str, object]]
        | None = None,
        state_callback: Callable[[], Mapping[str, object]] | None = None,
        session_verification_callback: Callable[[], object] | None = None,
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
        self._session_verification_callback = session_verification_callback
        self._lock = threading.RLock()
        self._requests: dict[str, dict[str, object]] = {}
        self._session_verification = (
            "pending" if session_verification_callback is not None else "ready"
        )
        self._session_verification_error = ""
        self._session_verification_thread: threading.Thread | None = None
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
        self._start_session_verification()
        return self.report_url

    def _start_session_verification(self) -> None:
        callback = self._session_verification_callback
        if callback is None or self._session_verification_thread is not None:
            return

        def verify() -> None:
            try:
                callback()
            except Exception as exc:  # archive/provenance boundary
                with self._lock:
                    self._session_verification = "error"
                    self._session_verification_error = str(exc) or type(exc).__name__
            else:
                with self._lock:
                    self._session_verification = "ready"
                    self._session_verification_error = ""

        thread = threading.Thread(
            target=verify,
            name="rheed-labeler-session-verification",
            daemon=True,
        )
        self._session_verification_thread = thread
        thread.start()

    def session_verification_status(self) -> tuple[str, str]:
        """Return the non-blocking source-archive verification state."""

        with self._lock:
            return (
                self._session_verification,
                self._session_verification_error,
            )

    def _require_verified_session(self) -> None:
        """Wait for preflight and reject mutations after a failed check."""

        thread = self._session_verification_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        state, error = self.session_verification_status()
        if state != "ready":
            detail = error or "source session verification did not complete"
            raise RuntimeError(f"Source session verification failed: {detail}")

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
        raise RuntimeError(
            "Equalizer is a separate diagnostic and is unavailable in "
            "point-event labeling"
        )

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
            raise ValueError(
                "Equalizer is not part of rheed-point-events-v2"
            )
        callback = self._revision_callback
        if callback is None:
            raise RuntimeError("Desktop revision persistence is unavailable")
        self._require_verified_session()
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
        self._require_verified_session()
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
            verification, verification_error = (
                self.service.session_verification_status()
            )
            self._json(HTTPStatus.OK, {
                "schema_version": 1,
                "desktop": True,
                "equalizer_available": False,
                "revision_persistence_available": self.service._revision_callback is not None,
                "event_state_available": self.service._state_callback is not None,
                "event_import_available": self.service._import_callback is not None,
                "session_verification": verification,
                "session_verification_error": verification_error,
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
