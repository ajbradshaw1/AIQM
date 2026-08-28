"""Offline tests for the authenticated point-event desktop bridge."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

from tools.rheed_postprocessing_labeling.loopback_service import LoopbackReportService


def _json_request(url: str, *, token: str, payload: dict | None = None):
    headers = {"X-AI4MBE-Bridge-Token": token}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_loopback_service_is_token_bound_and_equalizer_is_unavailable() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        report = root / "interactive_report.html"
        report.write_text("<!doctype html><title>offline</title>", encoding="utf-8")
        (root / "images").mkdir()
        (root / "images" / "frame_1.webp").write_bytes(b"fixture")
        service = LoopbackReportService(report)
        try:
            report_url = service.start()
            assert report_url.startswith("http://127.0.0.1:")
            assert "bridge_token=" in report_url
            with urllib.request.urlopen(report_url, timeout=3) as response:
                assert response.status == 200
                assert b"offline" in response.read()

            try:
                _json_request(service.base_url + "/api/status", token="wrong")
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
            else:  # pragma: no cover - explicit security assertion
                raise AssertionError("unauthenticated request unexpectedly succeeded")

            _, status_payload = _json_request(
                service.base_url + "/api/status", token=service.token,
            )
            assert status_payload["equalizer_available"] is False
            assert status_payload["session_verification"] == "ready"
            assert status_payload["session_verification_error"] == ""
            with pytest.raises(urllib.error.HTTPError) as unavailable:
                _json_request(
                    service.base_url + "/api/equalizer/request",
                    token=service.token,
                    payload={
                        "event_id": "event-1",
                        "review_frame_index": 3,
                        "reviewer": "Grower A",
                    },
                )
            assert unavailable.value.code == 400
            assert b"separate diagnostic" in unavailable.value.read()
        finally:
            service.stop()


def test_loopback_rejects_equalizer_revision_even_if_legacy_callbacks_exist() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        report = Path(temporary) / "interactive_report.html"
        report.write_text("<!doctype html>", encoding="utf-8")
        service = LoopbackReportService(
            report,
            revision_callback=lambda _command: {"ok": True},
            equalizer_callback=lambda _request: None,
            equalizer_revision_callback=lambda _command, _measurement: {
                "ok": True,
            },
        )
        try:
            service.start()
            with pytest.raises(urllib.error.HTTPError) as rejected:
                _json_request(
                    service.base_url + "/api/revisions", token=service.token,
                    payload={
                        "action": "set_equalizer", "event_id": "event-1",
                        "changes": {"equalizer_result_token": "legacy-token"},
                    },
                )
            assert rejected.value.code == 400
            assert b"not part of rheed-point-events-v3" in rejected.value.read()
        finally:
            service.stop()
        assert not service.running


def test_session_verification_reports_pending_then_ready_and_gates_revision() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        report = Path(temporary) / "interactive_report.html"
        report.write_text("<!doctype html>", encoding="utf-8")
        verification_started = threading.Event()
        release_verification = threading.Event()
        revision_called = threading.Event()

        def verify_session() -> None:
            verification_started.set()
            assert release_verification.wait(timeout=3)

        def persist(_payload):
            revision_called.set()
            return {"ok": True, "event": {}, "revision": {}}

        service = LoopbackReportService(
            report,
            revision_callback=persist,
            session_verification_callback=verify_session,
        )
        request_result: list[object] = []
        try:
            service.start()
            assert verification_started.wait(timeout=1)
            _, pending = _json_request(
                service.base_url + "/api/status", token=service.token,
            )
            assert pending["session_verification"] == "pending"
            assert pending["session_verification_error"] == ""

            def submit_revision() -> None:
                try:
                    request_result.append(_json_request(
                        service.base_url + "/api/revisions",
                        token=service.token,
                        payload={"action": "edit"},
                    ))
                except Exception as exc:  # pragma: no cover - assertion below
                    request_result.append(exc)

            request_thread = threading.Thread(target=submit_revision)
            request_thread.start()
            request_thread.join(timeout=0.1)
            assert request_thread.is_alive()
            assert not revision_called.is_set()

            release_verification.set()
            request_thread.join(timeout=2)
            assert not request_thread.is_alive()
            assert revision_called.is_set()
            assert request_result == [(200, {
                "ok": True, "event": {}, "revision": {},
            })]
            _, ready = _json_request(
                service.base_url + "/api/status", token=service.token,
            )
            assert ready["session_verification"] == "ready"
            assert ready["session_verification_error"] == ""
        finally:
            release_verification.set()
            service.stop()


def test_session_verification_error_is_reported_and_mutations_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        report = Path(temporary) / "interactive_report.html"
        report.write_text("<!doctype html>", encoding="utf-8")
        revision_called = threading.Event()

        def fail_verification() -> None:
            raise ValueError("session archive mismatch")

        service = LoopbackReportService(
            report,
            revision_callback=lambda _payload: revision_called.set() or {},
            session_verification_callback=fail_verification,
        )
        try:
            service.start()
            deadline = time.monotonic() + 2
            while True:
                _, status = _json_request(
                    service.base_url + "/api/status", token=service.token,
                )
                if status["session_verification"] == "error":
                    break
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert status["session_verification_error"] == "session archive mismatch"

            with pytest.raises(urllib.error.HTTPError) as rejected:
                _json_request(
                    service.base_url + "/api/revisions",
                    token=service.token,
                    payload={"action": "edit"},
                )
            assert rejected.value.code == 400
            assert b"Source session verification failed" in rejected.value.read()
            assert not revision_called.is_set()
        finally:
            service.stop()


def test_loopback_service_persists_revision_via_injected_fail_closed_callback() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        report = Path(temporary) / "interactive_report.html"
        report.write_text("<!doctype html>", encoding="utf-8")
        calls: list[dict] = []
        imports: list[dict] = []

        def persist(value):
            calls.append(dict(value))
            return {"persisted": True, "revision_id": "revision-1"}

        service = LoopbackReportService(
            report,
            revision_callback=persist,
            import_callback=lambda document: (
                imports.append(dict(document))
                or {"ok": True, "events": [], "revisions": []}
            ),
            state_callback=lambda: {
                "ok": True,
                "events": [{"event_id": "event-1", "status": "Draft"}],
                "unfinished_count": 1,
            },
        )
        try:
            service.start()
            _, current = _json_request(
                service.base_url + "/api/events",
                token=service.token,
            )
            assert current["events"][0]["event_id"] == "event-1"
            assert current["unfinished_count"] == 1
            _, result = _json_request(
                service.base_url + "/api/revisions",
                token=service.token,
                payload={"event_id": "event-1", "action": "update"},
            )
            assert result == {"persisted": True, "revision_id": "revision-1"}
            assert calls == [{"event_id": "event-1", "action": "update"}]
            _, imported = _json_request(
                service.base_url + "/api/events/import", token=service.token,
                payload={"document": {"schema_version": "rheed-point-events-v3"}},
            )
            assert imported["ok"] is True
            assert imports == [{"schema_version": "rheed-point-events-v3"}]
            with pytest.raises(urllib.error.HTTPError) as legacy:
                _json_request(
                    service.base_url + "/api/events/import", token=service.token,
                    payload={"document": {"schema_version": "rheed-point-events-v2"}},
                )
            assert legacy.value.code == 400
            assert b"read-only evidence" in legacy.value.read()
            assert imports == [{"schema_version": "rheed-point-events-v3"}]
        finally:
            service.stop()


def test_loopback_service_rejects_traversal_and_incomplete_requests() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        report = root / "interactive_report.html"
        report.write_text("<!doctype html>", encoding="utf-8")
        service = LoopbackReportService(report)
        try:
            service.start()
            for path in ("/../secret.txt", "/run_manifest.json", "/images"):
                try:
                    urllib.request.urlopen(service.base_url + path, timeout=3)
                except urllib.error.HTTPError as exc:
                    assert exc.code == 404
                else:  # pragma: no cover
                    raise AssertionError(f"unsafe static path was served: {path}")
            try:
                _json_request(
                    service.base_url + "/api/equalizer/request",
                    token=service.token,
                    payload={"event_id": "event-1", "review_frame_index": 1},
                )
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
                assert b"separate diagnostic" in exc.read()
            else:  # pragma: no cover
                raise AssertionError("request without reviewer unexpectedly succeeded")
        finally:
            service.stop()
