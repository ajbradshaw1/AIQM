"""Offline tests for the authenticated point-event desktop bridge."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import urllib.error
import urllib.request

import pytest

from tools.rheed_postprocessing_labeling.loopback_service import (
    EqualizerRequest,
    LoopbackReportService,
)


def _json_request(url: str, *, token: str, payload: dict | None = None):
    headers = {"X-AI4MBE-Bridge-Token": token}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_loopback_service_is_token_bound_and_completes_one_equalizer_request() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        report = root / "interactive_report.html"
        report.write_text("<!doctype html><title>offline</title>", encoding="utf-8")
        (root / "images").mkdir()
        (root / "images" / "frame_1.webp").write_bytes(b"fixture")
        received: list[EqualizerRequest] = []
        ready = threading.Event()

        def receive(request: EqualizerRequest) -> None:
            received.append(request)
            ready.set()

        service = LoopbackReportService(report, equalizer_callback=receive)
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

            status, queued = _json_request(
                service.base_url + "/api/equalizer/request",
                token=service.token,
                payload={
                    "event_id": "legacy-74d3f6d68b624584",
                    "review_frame_index": 3,
                    "reviewer": "Grower A",
                },
            )
            assert status == 202
            assert ready.wait(1)
            request = received[0]
            assert request.request_id == queued["request_id"]
            assert request.review_frame_index == 3

            service.complete_equalizer(request.request_id, {
                "schema_version": 1,
                "frame_sha256": "a" * 64,
                "final_weights": {"1x1": 1.0, "HTR": None},
            })
            _, result = _json_request(
                service.base_url
                + "/api/equalizer/result?request_id="
                + request.request_id,
                token=service.token,
            )
            assert result["status"] == "complete"
            assert result["measurement"]["final_weights"]["HTR"] is None
            assert len(result["result_token"]) >= 32
        finally:
            service.stop()


def test_equalizer_revision_redeems_server_result_once() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        report = Path(temporary) / "interactive_report.html"
        report.write_text("<!doctype html>", encoding="utf-8")
        requests: list[EqualizerRequest] = []
        trusted: list[tuple[dict, dict]] = []

        def persist_equalizer(command, measurement):
            trusted.append((dict(command), dict(measurement)))
            return {"ok": True, "event": {"event_id": command["event_id"]}}

        service = LoopbackReportService(
            report,
            equalizer_callback=requests.append,
            revision_callback=lambda _command: {"ok": True},
            equalizer_revision_callback=persist_equalizer,
        )
        try:
            service.start()
            _, queued = _json_request(
                service.base_url + "/api/equalizer/request", token=service.token,
                payload={"event_id": "event-1", "review_frame_index": 2, "reviewer": "Grower"},
            )
            service.complete_equalizer(
                queued["request_id"], {"valid": True, "frame_sha256": "a" * 64},
            )
            _, result = _json_request(
                service.base_url + "/api/equalizer/result?request_id=" + queued["request_id"],
                token=service.token,
            )
            with pytest.raises(urllib.error.HTTPError) as arbitrary:
                _json_request(
                    service.base_url + "/api/revisions", token=service.token,
                    payload={
                        "action": "set_equalizer", "event_id": "event-1",
                        "changes": {"equalizer": {"valid": True}},
                    },
                )
            assert arbitrary.value.code == 400
            command = {
                "action": "set_equalizer", "event_id": "event-1",
                "changes": {"equalizer_result_token": result["result_token"]},
            }
            _, saved = _json_request(
                service.base_url + "/api/revisions", token=service.token,
                payload=command,
            )
            assert saved["ok"] is True
            assert trusted[0][0]["changes"] == {}
            assert trusted[0][1]["frame_sha256"] == "a" * 64
            with pytest.raises(urllib.error.HTTPError) as reused:
                _json_request(
                    service.base_url + "/api/revisions", token=service.token,
                    payload=command,
                )
            assert reused.value.code == 400
        finally:
            service.stop()
        assert not service.running


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
            equalizer_callback=lambda _request: None,
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
                payload={"document": {"schema_version": "rheed-point-events-v1"}},
            )
            assert imported["ok"] is True
            assert imports == [{"schema_version": "rheed-point-events-v1"}]
        finally:
            service.stop()


def test_loopback_service_rejects_traversal_and_incomplete_requests() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        report = root / "interactive_report.html"
        report.write_text("<!doctype html>", encoding="utf-8")
        service = LoopbackReportService(report, equalizer_callback=lambda _request: None)
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
                assert b"reviewer" in exc.read()
            else:  # pragma: no cover
                raise AssertionError("request without reviewer unexpectedly succeeded")
        finally:
            service.stop()
