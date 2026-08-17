"""Focused tests for the immutable-source RHEED point-event contract."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
import numpy as np
from PIL import Image

from gui.equalizer_label_contract import frame_rgb_sha256
from tools.rheed_postprocessing_labeling.point_events import (
    ACTIVE_EQUALIZER_BASES,
    PointEventSidecarStore,
    PointEventValidationError,
    RevisionStore,
    completion_errors,
    deterministic_event_id,
    import_point_events,
    make_posthoc_event,
    point_event_document,
    replay_revisions,
    revise_event,
    validate_equalizer_measurement,
    validate_point_event_document,
)
from tools.rheed_postprocessing_labeling.session_archive import (
    hash_frame_payloads,
    load_session_archive,
    read_raw_frame,
)


def _png(value: int) -> bytes:
    image = Image.new("RGB", (12, 8), (value, value // 2, 30))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def _csv(rows: list[dict[str, object]]) -> bytes:
    if not rows:
        return b""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _bmp_from_png(payload: bytes) -> bytes:
    with Image.open(io.BytesIO(payload)) as image:
        stream = io.BytesIO()
        image.convert("RGB").save(stream, "BMP")
    return stream.getvalue()


def _rgb_hash(payload: bytes) -> str:
    with Image.open(io.BytesIO(payload)) as image:
        return frame_rgb_sha256(
            np.asarray(image.convert("RGB"), dtype=np.uint8),
        )


def _live_rgb_zip(
    tmp_path: Path,
    *,
    event_count: int = 1,
    claimed_rgb_hash: str | None = None,
) -> tuple[Path, bytes, bytes]:
    heartbeat_payload = _png(80)
    live_payload = _bmp_from_png(heartbeat_payload)
    rgb_hash = claimed_rgb_hash or _rgb_hash(heartbeat_payload)
    timestamp = "2026-08-06T12:00:01.000Z"
    heartbeat = [{
        "timestamp": timestamp,
        "elapsed_s": 1.0,
        "heartbeat_idx": 1,
        "pyrometer_temp_C": 501,
        "frame_path": r"D:\session\frames\rheed_1.png",
        "capture_backend": "wgc",
        "captured_at_utc": timestamp,
        "capture_sequence": 101,
        "capture_geometry_id": "g1",
    }]
    manual = [{
        "timestamp": timestamp,
        "elapsed_s": 1.0,
        "event_idx": index + 1,
        "frame_path": r"D:\session\frames\rheed_1.png",
        "note": "",
        "captured_at_utc": timestamp,
        "capture_sequence": 101,
    } for index in range(event_count)]
    live = [{
        "label_idx": 1,
        "capture_sequence": 101,
        "frame_path": r"D:\session\frames\live_label_001.bmp",
        "equalizer_frame_sha256": rgb_hash,
        "equalizer_frame_sha256_algorithm": "rgb-array-v1",
        "equalizer_calibration_valid": "True",
        "calibration_id": "cal-rgb",
        "basis_bundle_id": "basis-rgb",
        "equalizer_fit_residual": "1.25",
        "equalizer_valid_coverage": "0.8",
        **{
            f"equalizer_{kind}_{suffix}": (
                "" if kind == "raw" else "0.25"
            )
            for kind in ("raw", "final", "normalized")
            for suffix in ("1x1", "tw", "c6x2", "rt13")
        },
    }]
    path = tmp_path / f"live-rgb-{event_count}-{rgb_hash[:8]}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("session/session_metadata.json", json.dumps({
            "session_id": "live-rgb-session",
            "chamber_id": "TEST",
            "camera_backend": "wgc",
            "capture_geometry_id": "g1",
        }))
        archive.writestr("session/heartbeat_log.csv", _csv(heartbeat))
        archive.writestr("session/manual_events.csv", _csv(manual))
        archive.writestr("session/live_labels.csv", _csv(live))
        archive.writestr("session/frames/rheed_1.png", heartbeat_payload)
        archive.writestr("session/frames/live_label_001.bmp", live_payload)
    return path, heartbeat_payload, live_payload


def _zip(
    tmp_path: Path, *, auto_state: str = "pending", auto_state_changed_at: str = "",
) -> tuple[Path, list[bytes]]:
    frames = [_png(20), _png(80), _png(140)]
    times = ["2026-08-06T12:00:00.000Z", "2026-08-06T12:00:01.000Z", "2026-08-06T12:00:02.000Z"]
    heartbeat = [
        {
            "timestamp": times[index], "elapsed_s": float(index),
            "heartbeat_idx": index + 1, "pyrometer_temp_C": 500 + index,
            "frame_path": rf"D:\session\frames\rheed_{index}.png",
            "capture_backend": "wgc", "captured_at_utc": times[index],
            "capture_sequence": 100 + index, "capture_geometry_id": "g1",
            "captured_monotonic_ns": 1_000_000_000 + index * 1_000_000_000,
            "frame_width": 12, "frame_height": 8, "source_hwnd": 1234,
            "view_segment_id": 1, "visual_history_generation": 2,
            "gun_aligned": "True", "realignment_active": "False",
            "calibration_id": "", "basis_bundle_id": "",
        }
        for index in range(3)
    ]
    # Two rows intentionally share event_idx/time across different source
    # files; their IDs must still be different.
    manual = [{
        "timestamp": times[1], "elapsed_s": 1.0, "event_idx": 1,
        "frame_path": r"D:\session\frames\rheed_1.png", "note": "",
        "captured_at_utc": times[2], "capture_sequence": 101,
    }]
    auto = [{
        "timestamp": times[1], "elapsed_s": 1.0, "event_idx": 1,
        "change_score": 0.7, "buffer_count": 0, "buffer_dir": "",
        "event_state": auto_state, "state_changed_at": auto_state_changed_at,
        "captured_at_utc": times[2],
        "capture_sequence": 101,
    }]
    view = [{
        "timestamp": times[1], "elapsed_s": 1.0, "event_idx": 1,
        "event_type": "rheed_current_adjusted", "note": "read-only",
        "captured_at_utc": times[2], "capture_sequence": 101,
    }]
    labels = [{
        "event_idx": 1, "notes": "legacy auto review",
        "human_primary_reconstruction": "c(6x2)", "human_labeler": "Grower A",
        "human_confidence": "0.8", "change_from": "1x1", "change_to": "c(6x2)",
        "calibration_id": "", "basis_bundle_id": "",
    }]
    live = [{
        "label_idx": 1, "capture_sequence": 101,
        # Two events share this provenance, so this must remain unlinked.
        "equalizer_frame_sha256": hashlib.sha256(frames[1]).hexdigest(),
        "equalizer_frame_sha256_algorithm": "sha256-file-bytes",
        "calibration_id": "cal", "basis_bundle_id": "basis",
    }]
    sensor = [{
        "timestamp": times[1], "elapsed_s": 1.0,
        "pyrometer_temp_C": 501, "mistral_v_actual_V": 2.3,
        "mistral_i_actual_A": 0.4,
    }]
    path = tmp_path / "session.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("session/session_metadata.json", json.dumps({
            "session_id": "session-1", "chamber_id": "TEST",
            "camera_backend": "wgc", "capture_geometry_id": "g1",
        }))
        for name, rows in (
            ("heartbeat_log.csv", heartbeat), ("manual_events.csv", manual),
            ("auto_capture_events.csv", auto), ("rheed_view_events.csv", view),
            ("events_labels.csv", labels), ("live_labels.csv", live),
            ("sensor_log.csv", sensor),
        ):
            archive.writestr(f"session/{name}", _csv(rows))
        for index, payload in enumerate(frames):
            archive.writestr(f"session/frames/rheed_{index}.png", payload)
    return path, frames


@pytest.fixture
def imported(tmp_path: Path):
    path, payloads = _zip(tmp_path)
    session = hash_frame_payloads(load_session_archive(path))
    return session, import_point_events(session), payloads


def _valid_equalizer(event: dict) -> dict:
    anchor = event["review"]["anchor"]
    raw = {label: 0.25 for label in ACTIVE_EQUALIZER_BASES}
    raw["HTR"] = None
    return {
        "schema_version": 1,
        "valid": True,
        "calibration_id": "calibration-1", "basis_bundle_id": "basis-1",
        "active_classes": list(ACTIVE_EQUALIZER_BASES),
        "weights": {"raw": dict(raw), "final": dict(raw), "normalized": dict(raw)},
        "fit_residual": 1.5, "valid_coverage": 0.8,
        "frame_sha256": anchor["image_sha256"],
        "frame_sha256_algorithm": "raw-file-bytes-v1",
        "capture_sequence": anchor["capture_sequence"],
        "view_segment_id": "view-1", "capture_geometry_id": "g1",
        "HTR": None,
    }


def test_imports_only_manual_and_auto_as_labelable_points(imported) -> None:
    session, result, payloads = imported
    assert [event["source"]["kind"] for event in result.events] == ["auto_capture", "manual"]
    assert len({event["event_id"] for event in result.events}) == 2
    assert all(event["status"] == "Draft" for event in result.events)
    assert next(event for event in result.events if event["source"]["kind"] == "manual")["review"]["comment"] == ""
    auto = next(event for event in result.events if event["source"]["kind"] == "auto_capture")
    assert auto["review"]["comment"] == "legacy auto review"
    assert auto["review"]["human_reconstruction"] == "c(6x2)"
    assert auto["source"]["original_at_utc"] == "2026-08-06T12:00:01.000Z"
    assert auto["source"]["original_anchor"]["captured_at_utc"] == "2026-08-06T12:00:02.000Z"
    assert auto["source"]["original_anchor"]["archive_member"] == ""
    assert auto["source"]["original_anchor"]["image_sha256"] == ""
    assert auto["source"]["original_anchor"]["association_status"] == "unresolved_saved_frame"
    assert auto["review"]["anchor"]["captured_at_utc"] == "2026-08-06T12:00:01.000Z"
    assert len(result.reference_events) == 1
    assert result.reference_events[0]["event_type"] == "rheed_current_adjusted"
    assert result.reference_events[0]["event_at_utc"] == "2026-08-06T12:00:01.000Z"
    assert result.reference_events[0]["read_only"] is True
    assert result.sensor_context["rows"][0]["mistral_v_actual_V"] == "2.3"
    assert result.frame_contexts[0]["provenance_complete"] is True
    assert result.frame_contexts[0]["received_monotonic_ns"] == 1_000_000_000
    assert len(result.unlinked_legacy_labels) == 1
    assert read_raw_frame(session, 1) == payloads[1]


def test_live_rgb_hash_uniquely_relinks_without_mislabeling_raw_hash(
    tmp_path: Path,
) -> None:
    path, heartbeat_payload, live_payload = _live_rgb_zip(tmp_path)
    session = hash_frame_payloads(load_session_archive(path))
    result = import_point_events(session)

    assert len(result.events) == 1
    assert result.unlinked_legacy_labels == ()
    event = result.events[0]
    anchor = event["review"]["anchor"]
    measurement = event["review"]["equalizer"]
    assert measurement["frame_sha256_algorithm"] == "rgb-array-v1"
    assert measurement["frame_sha256"] == _rgb_hash(heartbeat_payload)
    assert measurement["raw_frame_sha256_algorithm"] == "raw-file-bytes-v1"
    assert measurement["raw_frame_sha256"] == hashlib.sha256(
        heartbeat_payload,
    ).hexdigest()
    assert measurement["source_frame_sha256"] == hashlib.sha256(
        live_payload,
    ).hexdigest()
    assert measurement["source_frame_sha256"] != measurement["raw_frame_sha256"]
    assert measurement["rgb_hash_verified_from_raw_frame"] is True
    assert validate_equalizer_measurement(measurement, anchor) == measurement


def test_live_rgb_hash_relink_rejects_ambiguous_same_frame_events(
    tmp_path: Path,
) -> None:
    path, _, _ = _live_rgb_zip(tmp_path, event_count=2)
    result = import_point_events(hash_frame_payloads(load_session_archive(path)))

    assert len(result.events) == 2
    assert all(event["review"]["equalizer"] is None for event in result.events)
    assert len(result.unlinked_legacy_labels) == 1
    assert "multiple events" in result.unlinked_legacy_labels[0]["reason"]
    assert result.unlinked_legacy_labels[0]["frame_sha256_algorithm"] == "rgb-array-v1"


def test_live_rgb_hash_relink_rejects_pixel_hash_mismatch(tmp_path: Path) -> None:
    path, _, live_payload = _live_rgb_zip(
        tmp_path, claimed_rgb_hash="f" * 64,
    )
    result = import_point_events(hash_frame_payloads(load_session_archive(path)))

    assert result.events[0]["review"]["equalizer"] is None
    assert len(result.unlinked_legacy_labels) == 1
    unlinked = result.unlinked_legacy_labels[0]
    assert unlinked["frame_sha256"] == "f" * 64
    assert unlinked["frame_raw_sha256"] == hashlib.sha256(live_payload).hexdigest()


def test_rgb_measurement_requires_verified_dual_hash_binding(imported) -> None:
    _, result, payloads = imported
    event = next(
        item for item in result.events if item["source"]["kind"] == "manual"
    )
    anchor = event["review"]["anchor"]
    measurement = _valid_equalizer(event)
    measurement.update({
        "frame_sha256": _rgb_hash(payloads[1]),
        "frame_sha256_algorithm": "rgb-array-v1",
    })
    with pytest.raises(PointEventValidationError, match="not bound"):
        validate_equalizer_measurement(measurement, anchor)

    measurement.update({
        "raw_frame_sha256": anchor["image_sha256"],
        "raw_frame_sha256_algorithm": "raw-file-bytes-v1",
        "rgb_hash_verified_from_raw_frame": True,
    })
    validated = validate_equalizer_measurement(measurement, anchor)
    assert validated["frame_sha256_algorithm"] == "rgb-array-v1"
    assert validated["raw_frame_sha256"] == anchor["image_sha256"]


def test_deterministic_id_uses_source_and_row_identity() -> None:
    common = dict(
        session_identity="s", source_file="session/manual_events.csv",
        source_sequence=1, source_event_idx=1,
        event_time="2026-01-01T00:00:00Z", capture_sequence=3,
        source_row_sha256="a" * 64,
    )
    first = deterministic_event_id(source="manual", **common)
    assert first == deterministic_event_id(source="manual", **common)
    assert first != deterministic_event_id(
        source="auto_capture", **{**common, "source_file": "session/auto_capture_events.csv"}
    )


def test_auto_manifest_requires_one_exact_capture_sequence(tmp_path: Path) -> None:
    from tools.rheed_postprocessing_labeling import point_events as module

    path, _ = _zip(tmp_path)
    duplicate_manifest = _csv([
        {"capture_sequence": 101, "frame_path": "a.bmp"},
        {"capture_sequence": 101, "frame_path": "b.bmp"},
    ])
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("session/buffer/capture_manifest.csv", duplicate_manifest)
        archive.writestr("session/buffer/a.bmp", b"a")
        archive.writestr("session/buffer/b.bmp", b"b")
    session = hash_frame_payloads(load_session_archive(path))
    assert module._auto_frame_member(
        session, {"buffer_dir": "buffer"}, 101,
    ) is None


def test_complete_is_explicit_and_strict_then_edit_reopens(imported) -> None:
    _, result, _ = imported
    event = copy.deepcopy(result.events[0])
    assert "comment is required" not in completion_errors(event)  # legacy note exists
    assert any("Equalizer" in error for error in completion_errors(event))
    with pytest.raises(PointEventValidationError, match="Cannot complete"):
        revise_event(event, action="complete", actor="Grower A")
    equalizer = _valid_equalizer(event)
    equalizer["weights"]["raw"]["1x1"] = None  # valid manual-fit sentinel
    event, edit_revision = revise_event(
        event, action="set_equalizer", actor="Grower A",
        changes={"equalizer": equalizer},
    )
    assert event["review"]["human_reconstruction"] == "c(6x2)"
    assert event["review"]["equalizer"]["HTR"] is None
    event, complete_revision = revise_event(event, action="complete", actor="Grower A")
    assert event["status"] == "Complete"
    event, _ = revise_event(
        event, action="edit", actor="Grower A", changes={"comment": "revised"},
    )
    assert event["status"] == "Draft"
    assert replay_revisions([result.events[0]], [edit_revision, complete_revision])[
        event["event_id"]
    ]["status"] == "Complete"


def test_moving_anchor_snaps_to_saved_frame_and_invalidates_equalizer(imported) -> None:
    session, result, _ = imported
    event = copy.deepcopy(result.events[0])
    event["review"]["reviewer"] = "Grower"
    event["review"]["equalizer"] = _valid_equalizer(event)
    moved, _ = revise_event(
        event, action="move_anchor", actor="Grower",
        changes={"anchor": {
            **event["review"]["anchor"],
            "frame_index": 3, "heartbeat_idx": session.frames[2].heartbeat_idx,
            "elapsed_s": session.frames[2].elapsed_s,
            "captured_at_utc": session.frames[2].captured_at_utc,
            "capture_sequence": session.frames[2].capture_sequence,
            "image_sha256": session.frames[2].frame_sha256,
            "frame_name": session.frames[2].frame_name,
            "archive_member": session.frames[2].member,
        }},
    )
    assert moved["review"]["anchor"]["frame_index"] == 3
    assert moved["review"]["equalizer"] is None
    assert moved["status"] == "Draft"


def test_source_event_cannot_be_deleted_but_posthoc_can(imported) -> None:
    _, result, _ = imported
    with pytest.raises(PointEventValidationError, match="source events"):
        revise_event(
            result.events[0], action="delete", actor="Grower",
            changes={"dismiss_reason": "duplicate"},
        )
    posthoc = make_posthoc_event(result.events[0]["review"]["anchor"], actor="Grower")
    deleted, _ = revise_event(
        posthoc, action="delete", actor="Grower",
        changes={"dismiss_reason": "mistaken click"},
    )
    assert deleted["review"]["disposition"] == "deleted"


def test_revision_store_recovers_pending_transaction(tmp_path: Path, imported) -> None:
    _, result, _ = imported
    event, revision = revise_event(
        result.events[0], action="edit", actor="Grower",
        changes={"comment": "finished later"},
    )
    store = RevisionStore(tmp_path / "annotations")
    state = store.append(revision, initial_events=result.events)
    assert state[event["event_id"]]["review"]["comment"] == "finished later"
    # Simulate a crash after the transaction marker but before append.
    second, second_revision = revise_event(
        state[event["event_id"]], action="edit", actor="Grower",
        changes={"reviewer": "Grower"},
    )
    from tools.rheed_postprocessing_labeling import point_events as point_events_module

    second_revision = point_events_module._with_record_integrity(
        second_revision,
        previous_record_sha256=store.recover()[-1]["record_sha256"],
    )
    RevisionStore._atomic_json(store.pending_path, {
        "schema_version": 1, "revision_id": second_revision["revision_id"],
        "revision_sha256": hashlib.sha256(json.dumps(
            second_revision, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest(),
        "revision": second_revision,
    })
    partial = json.dumps(
        second_revision, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    with store.journal_path.open("ab") as stream:
        stream.write(partial[: len(partial) // 2])
    recovered = store.load(result.events)
    assert recovered[event["event_id"]]["review"]["reviewer"] == "Grower"
    assert not store.pending_path.exists()
    assert all(json.loads(line) for line in store.journal_path.read_text(encoding="utf-8").splitlines())


def test_revision_store_rejects_hash_tamper_and_rehashed_action_disguise(
    tmp_path: Path, imported,
) -> None:
    from tools.rheed_postprocessing_labeling import point_events as module

    _, result, _ = imported
    event = result.events[0]
    edited, revision = revise_event(
        event, action="edit", actor="Grower", changes={"comment": "reviewed"},
    )
    hash_store = RevisionStore(tmp_path / "hash")
    hash_store.append(revision, initial_events=result.events)
    record = json.loads(hash_store.journal_path.read_text(encoding="utf-8"))
    record["after"]["review"]["comment"] = "tampered"
    hash_store.journal_path.write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PointEventValidationError, match="record hash"):
        hash_store.load(result.events)

    equalized, equalizer_revision = revise_event(
        event, action="set_equalizer", actor="Grower",
        changes={"equalizer": _valid_equalizer(event)},
    )
    semantic_store = RevisionStore(tmp_path / "semantic")
    semantic_store.append(equalizer_revision, initial_events=result.events)
    disguised = json.loads(semantic_store.journal_path.read_text(encoding="utf-8"))
    disguised["action"] = "complete"
    disguised["after"]["status"] = "Complete"
    disguised["record_sha256"] = module._record_hash(disguised)
    semantic_store.journal_path.write_text(
        json.dumps(disguised, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PointEventValidationError, match="complete transition"):
        semantic_store.load(result.events)


def test_point_document_rejects_tampered_source_and_anchor(imported) -> None:
    session, result, _ = imported
    dataset = {
        "dataset_id": f"sha256:{session.sha256}", "frame_count": len(session.frames),
        "ordered_frame_fingerprint": "sha256:" + "1" * 64,
        "source_archive_sha256": session.sha256,
        "model_context_fingerprint": "sha256:" + "2" * 64,
    }
    payload = {
        "config": {
            "dataset": dataset, "point_events": list(result.events),
            "reference_events": list(result.reference_events),
            "unlinked_legacy_labels": list(result.unlinked_legacy_labels),
        },
        "heartbeat_indices": [frame.heartbeat_idx for frame in session.frames],
        "capture_sequences": [frame.capture_sequence for frame in session.frames],
        "frame_sha256": [frame.frame_sha256 for frame in session.frames],
        "times": [frame.elapsed_s for frame in session.frames],
    }
    document = point_event_document(
        dataset=dataset, events=result.events, annotation_set_id=str(uuid.uuid4()),
        reviewer="Grower", reference_events=result.reference_events,
        unlinked_legacy_labels=result.unlinked_legacy_labels,
    )
    assert validate_point_event_document(document, payload)["events"]
    tampered = copy.deepcopy(document)
    tampered["events"][0]["source"]["source_row_sha256"] = "0" * 64
    with pytest.raises(PointEventValidationError):
        validate_point_event_document(tampered, payload)
    bad_anchor = copy.deepcopy(document)
    bad_anchor["events"][0]["review"]["anchor"]["capture_sequence"] = 999
    with pytest.raises(PointEventValidationError):
        validate_point_event_document(bad_anchor, payload)
    bad_reference = copy.deepcopy(document)
    bad_reference["reference_events"][0]["note"] = "tampered"
    with pytest.raises(PointEventValidationError, match="reference events"):
        validate_point_event_document(bad_reference, payload)
    bad_unlinked = copy.deepcopy(document)
    bad_unlinked["unlinked_legacy_labels"] = []
    with pytest.raises(PointEventValidationError, match="legacy-label"):
        validate_point_event_document(bad_unlinked, payload)


def test_same_time_posthoc_events_are_distinct(imported) -> None:
    _, result, _ = imported
    first = make_posthoc_event(result.events[0]["review"]["anchor"], actor="Grower")
    second = make_posthoc_event(result.events[0]["review"]["anchor"], actor="Grower")
    assert uuid.UUID(first["event_id"])
    assert first["event_id"] != second["event_id"]


def test_sidecar_store_writes_only_beside_report(
    tmp_path: Path, imported, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, result, _ = imported
    report_root = tmp_path / "report"
    report_root.mkdir()
    report = report_root / "interactive_report.html"
    report.write_text("synthetic", encoding="utf-8")
    dataset = {
        "dataset_id": f"sha256:{session.sha256}",
        "source_archive_sha256": session.sha256,
    }
    payload = {
        "config": {"dataset": dataset, "point_events": list(result.events)},
        "heartbeat_indices": [frame.heartbeat_idx for frame in session.frames],
        "capture_sequences": [frame.capture_sequence for frame in session.frames],
        "frame_sha256": [frame.frame_sha256 for frame in session.frames],
        "times": [frame.elapsed_s for frame in session.frames],
    }
    from tools.rheed_postprocessing_labeling import report_builder
    from tools.rheed_postprocessing_labeling import point_events as point_events_module

    monkeypatch.setattr(report_builder, "load_report_payload", lambda _path: payload)
    real_loader = point_events_module.load_session_archive
    load_count = 0

    def counted_loader(source):
        nonlocal load_count
        load_count += 1
        return real_loader(source)

    monkeypatch.setattr(point_events_module, "load_session_archive", counted_loader)
    store = PointEventSidecarStore(report, session.path)
    assert load_count == 0, "opening a report must not hash the large source ZIP"
    event = result.events[0]
    response = store.apply_revision({
        "dataset_id": dataset["dataset_id"], "action": "edit",
        "event_id": event["event_id"], "base_revision_id": "",
        "actor": "Grower", "changes": {"comment": "reviewed"},
    })
    assert load_count == 1
    assert response["event"]["review"]["comment"] == "reviewed"
    assert (report_root / "annotations" / "rheed_event_revisions.jsonl").is_file()
    assert (report_root / "annotations" / "rheed_point_events.json").is_file()
    assert len(store.revisions) == 1
    assert store.annotation_metadata["reviewer"] == "Grower"
    assert store.export_document()["revisions"] == list(store.revisions)
    assert validate_point_event_document(store.export_document(), payload)["events"]
    assert session.path.is_file(), "source ZIP remains untouched"
    with pytest.raises(PointEventValidationError, match="server-saved"):
        store.apply_revision({
            "action": "complete", "actor": "Grower",
            "event_id": event["event_id"],
            "base_revision_id": response["event"]["revision_id"], "changes": {},
        })
    with pytest.raises(PointEventValidationError, match="server-held"):
        store.apply_revision({
            "action": "set_equalizer", "actor": "Grower",
            "event_id": event["event_id"],
            "base_revision_id": response["event"]["revision_id"],
            "changes": {"equalizer": _valid_equalizer(response["event"])},
        })
    trusted = store.apply_equalizer_revision({
        "action": "set_equalizer", "actor": "Grower",
        "event_id": event["event_id"],
        "base_revision_id": response["event"]["revision_id"],
        "changes": {},
    }, _valid_equalizer(response["event"]))
    assert trusted["event"]["review"]["equalizer"]["valid"] is True
    completed = store.apply_revision({
        "action": "complete", "actor": "Grower",
        "event_id": event["event_id"],
        "base_revision_id": trusted["event"]["revision_id"], "changes": {},
    })
    assert completed["event"]["status"] == "Complete"
    durable = store.atomic_state_snapshot()
    assert durable["revisions"][1]["previous_record_sha256"] == durable["revisions"][0]["record_sha256"]
    assert {
        item["event_id"]: item for item in durable["events"]
    } == replay_revisions(
        result.events, durable["revisions"], require_integrity=True,
    )
    with pytest.raises(PointEventValidationError, match="exact saved"):
        bad = copy.deepcopy(event["review"]["anchor"])
        bad["capture_sequence"] = 999
        store.apply_revision({
            "action": "add_event", "actor": "Grower",
            "changes": {"anchor": bad},
        })
    created = store.apply_revision({
        "action": "add_event", "actor": "Grower",
        "changes": {"anchor": event["review"]["anchor"]},
    })
    assert load_count == 1, "the verified SessionArchive is reused"
    assert created["event"]["source"]["kind"] == "posthoc"
    assert created["event"]["status"] == "Draft"

    import_root = tmp_path / "import-report"
    import_root.mkdir()
    import_report = import_root / "interactive_report.html"
    import_report.write_text("synthetic", encoding="utf-8")
    _edited, browser_revision = revise_event(
        event, action="edit", actor="Browser Grower",
        changes={"comment": "static browser draft"},
    )
    browser_document = point_event_document(
        dataset=dataset, events=result.events,
        revisions=[browser_revision], annotation_set_id=str(uuid.uuid4()),
        reviewer="Browser Grower",
    )
    # Simulate JavaScript's independently rendered before hash; the trusted
    # importer re-canonicalizes it while still checking the full snapshot.
    browser_document["revisions"][0]["before_sha256"] = "0" * 64
    import_store = PointEventSidecarStore(import_report, session.path)
    imported_state = import_store.import_document(browser_document)
    imported_event = next(
        item for item in imported_state["events"] if item["event_id"] == event["event_id"]
    )
    assert imported_event["review"]["comment"] == "static browser draft"
    assert imported_state["revisions"][0]["record_sha256"]
    assert imported_state["revisions"][0]["previous_record_sha256"] == ""
    before_failed_import = import_store.atomic_state_snapshot()
    annotations_dir = import_root / "annotations"

    def persisted_files() -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in annotations_dir.iterdir()
            if path.is_file()
        }

    files_before_failure = persisted_files()
    invalid_annotation_set = copy.deepcopy(browser_document)
    invalid_annotation_set["annotation_set"]["annotation_set_id"] = "not-a-uuid"
    with pytest.raises(PointEventValidationError, match="must be a UUID"):
        import_store.import_document(invalid_annotation_set)
    assert import_store.atomic_state_snapshot() == before_failed_import
    assert persisted_files() == files_before_failure

    duplicate_event = copy.deepcopy(browser_document)
    duplicate_event["events"].append(copy.deepcopy(duplicate_event["events"][0]))
    with pytest.raises(PointEventValidationError, match="duplicate event_id"):
        import_store.import_document(duplicate_event)
    assert import_store.atomic_state_snapshot() == before_failed_import
    assert persisted_files() == files_before_failure

    tampered = copy.deepcopy(browser_document)
    tampered["events"][0]["source"]["source_row_sha256"] = "f" * 64
    with pytest.raises(PointEventValidationError):
        import_store.import_document(tampered)
    assert import_store.atomic_state_snapshot() == before_failed_import
    assert persisted_files() == files_before_failure


def test_archived_live_journal_preserves_uuid_and_blank_draft(tmp_path: Path) -> None:
    path, _ = _zip(tmp_path)
    external_frame = b"separately-saved-live-equalizer-frame"
    external_hash = hashlib.sha256(external_frame).hexdigest()
    legacy_session = hash_frame_payloads(load_session_archive(path))
    legacy = import_point_events(legacy_session)
    records: list[dict] = []
    states: list[dict] = []
    manual_id = ""
    for imported_event in legacy.events:
        state = copy.deepcopy(imported_event)
        event_id = str(uuid.uuid4())
        equalizer = None
        # A live create revision predates any Events-tab review and cannot
        # already contain an Equalizer result.  Later review evidence must be
        # represented by its own semantic revision.
        state["review"].update({
            "comment": state["source"].get("original_note", ""),
            "reviewer": "", "confidence": None,
            "human_reconstruction": None, "change_from": None,
            "change_to": None, "equalizer": None,
        })
        if state["source"]["kind"] == "manual":
            manual_id = event_id
            state["review"]["anchor"] = {
                "frame_path": "session/frames/live_label.bmp",
                "image_sha256": external_hash,
                "image_sha256_algorithm": "raw-file-bytes-v1",
                "capture_sequence": 101,
                "captured_at_utc": "2026-08-06T12:00:01.250Z",
                "elapsed_s": 1.25,
                "view_segment_id": 1,
                "capture_geometry_id": "g1",
            }
            equalizer = _valid_equalizer(state)
        state["event_id"] = event_id
        # Match the compact source object written by the live PointEventStore.
        source = state["source"]
        original_hash = source["original_anchor"].get("image_sha256", "")
        state["source"] = {
            "kind": source["kind"], "session_identity": "session-1",
            "source_file": Path(source["source_file"]).name,
            "source_index": source["source_index"],
            "source_row_sha256": source["source_row_sha256"],
            "source_row_hash_algorithm": source["source_row_hash_algorithm"],
            "original_at_utc": source["original_at_utc"],
            "original_elapsed_s": source["original_elapsed_s"],
            "original_frame_path": source["original_anchor"].get("frame_name", ""),
            "original_image_sha256": original_hash,
            "original_image_sha256_algorithm": (
                "raw-file-bytes-v1" if original_hash else ""
            ),
            "capture_sequence": source["original_anchor"].get("capture_sequence"),
            "original_note": source["original_note"],
        }
        revision_id = str(uuid.uuid4())
        state.update({
            "revision_id": revision_id, "revision_number": 1,
            "created_at_utc": "2026-08-06T12:00:01.000+00:00",
            "updated_at_utc": "2026-08-06T12:00:01.000+00:00",
        })
        record = {
            "schema": "rheed-point-events-v1", "revision_id": revision_id,
            "event_id": event_id, "actor": "Grower", "action": "create",
            "recorded_at_utc": "2026-08-06T12:00:01.000+00:00",
            "base_revision_id": None, "before": None, "after": state,
        }
        record["record_sha256"] = hashlib.sha256(json.dumps(
            record, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()
        records.append(record)
        final_state = state
        if equalizer is not None:
            before = copy.deepcopy(state)
            final_state = copy.deepcopy(state)
            equalizer_revision_id = str(uuid.uuid4())
            final_state["review"]["equalizer"] = equalizer
            final_state.update({
                "revision_id": equalizer_revision_id,
                "revision_number": 2,
                "updated_at_utc": "2026-08-06T12:00:02.000+00:00",
            })
            equalizer_record = {
                "schema": "rheed-point-events-v1",
                "revision_id": equalizer_revision_id,
                "event_id": event_id,
                "actor": "Grower",
                "action": "set_equalizer",
                "recorded_at_utc": "2026-08-06T12:00:02.000+00:00",
                "base_revision_id": revision_id,
                "before": before,
                "after": final_state,
            }
            equalizer_record["record_sha256"] = hashlib.sha256(json.dumps(
                equalizer_record, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")).hexdigest()
            records.append(equalizer_record)
        states.append(final_state)
    summary = {
        "schema": "rheed-point-events-v1",
        "generated_at_utc": "2026-08-06T12:01:00.000+00:00",
        "journal": "rheed_event_revisions.jsonl",
        "events": sorted(states, key=lambda item: item["event_id"]),
    }
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("session/frames/live_label.bmp", external_frame)
        archive.writestr(
            "session/rheed_event_revisions.jsonl",
            "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
        )
        archive.writestr("session/rheed_point_events.json", json.dumps(summary, sort_keys=True))
    session = hash_frame_payloads(load_session_archive(path))
    imported = import_point_events(session)
    assert manual_id in {event["event_id"] for event in imported.events}
    manual = next(event for event in imported.events if event["event_id"] == manual_id)
    assert manual["status"] == "Draft"
    assert manual["review"]["comment"] == ""
    assert manual["review"]["equalizer"] is None
    assert manual["source"]["live_review_frame_evidence"]["image_sha256"] == external_hash
    assert any("measurement invalidated" in item.get("reason", "") for item in imported.unlinked_legacy_labels)
    assert len(imported.source_revisions) == 3
    assert imported.source_journal and imported.source_journal["authoritative"] is True


def test_mixed_live_journal_recovers_unjournaled_csv_and_later_legacy_review(
    tmp_path: Path,
) -> None:
    path, _ = _zip(
        tmp_path, auto_state="discarded",
        auto_state_changed_at="2026-08-06T12:00:03.000Z",
    )
    session = hash_frame_payloads(load_session_archive(path))
    legacy = import_point_events(session)
    auto = next(event for event in legacy.events if event["source"]["kind"] == "auto_capture")
    manual = next(event for event in legacy.events if event["source"]["kind"] == "manual")
    state = copy.deepcopy(auto)
    event_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    source = state["source"]
    state.update({
        "event_id": event_id, "revision_id": revision_id,
        "revision_number": 1, "created_at_utc": "2026-08-06T12:00:01.000+00:00",
        "updated_at_utc": "2026-08-06T12:00:01.000+00:00",
    })
    state["source"] = {
        "kind": "auto_capture", "session_identity": "session-1",
        "source_file": "auto_capture_events.csv", "source_index": source["source_index"],
        # Pre-v1 mutable-row hash captured while the decision was pending.
        "source_row_sha256": next(
            value for value in source["compatible_legacy_source_row_sha256"]
            if value != source["source_row_full_sha256"]
        ),
        "source_row_hash_algorithm": "sha256-canonical-json-row-v1",
        "original_at_utc": source["original_at_utc"],
        "original_elapsed_s": source["original_elapsed_s"],
        "original_frame_path": "", "original_image_sha256": "",
        "original_image_sha256_algorithm": "",
        "capture_sequence": source["original_anchor"]["capture_sequence"],
        "original_note": "",
    }
    # Simulate the real ordering: live Draft create first, Events-tab label CSV later.
    state["review"].update({
        # The trigger capture was not uniquely present in the empty auto
        # buffer, so live acquisition correctly left the review anchor unset.
        "anchor": None,
        "comment": "", "reviewer": "", "confidence": None,
        "human_reconstruction": None, "change_from": None,
        "change_to": None, "equalizer": None,
    })
    record = {
        "schema": "rheed-point-events-v1", "revision_id": revision_id,
        "event_id": event_id, "actor": "auto-capture", "action": "create",
        "recorded_at_utc": "2026-08-06T12:00:01.000+00:00",
        "base_revision_id": None, "before": None, "after": state,
    }
    record["record_sha256"] = hashlib.sha256(json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    summary = {
        "schema": "rheed-point-events-v1", "journal": "rheed_event_revisions.jsonl",
        "events": [state],
    }
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(
            "session/rheed_event_revisions.jsonl",
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
        )
        archive.writestr("session/rheed_point_events.json", json.dumps(summary, sort_keys=True))
    imported = import_point_events(hash_frame_payloads(load_session_archive(path)))
    assert {event["event_id"] for event in imported.events} == {event_id, manual["event_id"]}
    imported_auto = next(event for event in imported.events if event["event_id"] == event_id)
    assert imported_auto["review"]["comment"] == "legacy auto review"
    assert imported_auto["review"]["reviewer"] == "Grower A"
    assert imported_auto["status"] == "Draft"
    assert imported_auto["review"]["anchor"]["frame_index"] == 2
    assert imported_auto["review"]["anchor"]["image_sha256"]
    assert (
        imported_auto["source"]["original_anchor"]["association_status"]
        == "unresolved_saved_frame"
    )
    assert imported_auto["source"]["source_row_sha256"] == source["source_row_sha256"]
    assert imported_auto["source"]["source_decision"]["event_state"] == "discarded"
    assert imported.source_journal["deterministic_csv_fallback_count"] == 1


def test_archived_stale_summary_may_be_valid_prefix_but_not_tampered(tmp_path: Path) -> None:
    path, _ = _zip(tmp_path)
    session = hash_frame_payloads(load_session_archive(path))
    legacy = import_point_events(session)
    event = copy.deepcopy(legacy.events[0])
    event_id = str(uuid.uuid4())
    create_id = str(uuid.uuid4())
    event.update({
        "event_id": event_id, "revision_id": create_id, "revision_number": 1,
        "created_at_utc": "2026-08-06T12:00:01+00:00",
        "updated_at_utc": "2026-08-06T12:00:01+00:00",
    })
    source = event["source"]
    event["source"] = {
        "kind": source["kind"], "session_identity": "session-1",
        "source_file": Path(source["source_file"]).name,
        "source_index": source["source_index"],
        "source_row_sha256": source["source_row_sha256"],
        "original_at_utc": source["original_at_utc"],
        "original_elapsed_s": source["original_elapsed_s"],
        "original_frame_path": "", "original_image_sha256": "",
        "original_image_sha256_algorithm": "", "capture_sequence": 101,
        "original_note": source["original_note"],
    }
    create = {
        "schema": "rheed-point-events-v1", "revision_id": create_id,
        "event_id": event_id, "actor": "live", "action": "create",
        "recorded_at_utc": "2026-08-06T12:00:01+00:00", "base_revision_id": None,
        "before": None, "after": event,
    }
    create["record_sha256"] = hashlib.sha256(json.dumps(
        create, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    edited = copy.deepcopy(event)
    edited["review"]["comment"] = "after summary"
    edit_id = str(uuid.uuid4())
    edited.update({"revision_id": edit_id, "revision_number": 2})
    edit = {
        "schema": "rheed-point-events-v1", "revision_id": edit_id,
        "event_id": event_id, "actor": "live", "action": "edit",
        "recorded_at_utc": "2026-08-06T12:00:02+00:00",
        "base_revision_id": create_id, "before": event, "after": edited,
    }
    edit["record_sha256"] = hashlib.sha256(json.dumps(
        edit, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(
            "session/rheed_event_revisions.jsonl",
            "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in (create, edit)),
        )
        archive.writestr("session/rheed_point_events.json", json.dumps({"events": [event]}))
    imported = import_point_events(hash_frame_payloads(load_session_archive(path)))
    assert next(item for item in imported.events if item["event_id"] == event_id)["review"]["comment"] == "after summary"


@pytest.mark.parametrize("tamper_kind", ("equalizer_as_edit", "revision_gap"))
def test_archived_live_journal_rejects_rehashed_semantic_tamper(
    tmp_path: Path, tamper_kind: str,
) -> None:
    path, _ = _zip(tmp_path)
    session = hash_frame_payloads(load_session_archive(path))
    legacy = import_point_events(session)
    event = copy.deepcopy(next(
        item for item in legacy.events if item["source"]["kind"] == "manual"
    ))
    event_id = str(uuid.uuid4())
    create_id = str(uuid.uuid4())
    source = event["source"]
    original_hash = source["original_anchor"].get("image_sha256", "")
    event.update({
        "event_id": event_id,
        "revision_id": create_id,
        "revision_number": 1,
        "created_at_utc": "2026-08-06T12:00:01+00:00",
        "updated_at_utc": "2026-08-06T12:00:01+00:00",
    })
    event["source"] = {
        "kind": "manual", "session_identity": "session-1",
        "source_file": "manual_events.csv",
        "source_index": source["source_index"],
        "source_row_sha256": source["source_row_sha256"],
        "source_row_hash_algorithm": source["source_row_hash_algorithm"],
        "original_at_utc": source["original_at_utc"],
        "original_elapsed_s": source["original_elapsed_s"],
        "original_frame_path": source["original_anchor"].get("frame_name", ""),
        "original_image_sha256": original_hash,
        "original_image_sha256_algorithm": (
            "raw-file-bytes-v1" if original_hash else ""
        ),
        "capture_sequence": source["original_anchor"].get("capture_sequence"),
        "original_note": source["original_note"],
    }
    event["review"].update({
        "comment": source["original_note"], "reviewer": "",
        "confidence": None, "human_reconstruction": None,
        "change_from": None, "change_to": None, "equalizer": None,
    })
    create = {
        "schema": "rheed-point-events-v1", "revision_id": create_id,
        "event_id": event_id, "actor": "live", "action": "create",
        "recorded_at_utc": "2026-08-06T12:00:01+00:00",
        "base_revision_id": None, "before": None, "after": event,
    }
    create["record_sha256"] = hashlib.sha256(json.dumps(
        create, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()

    disguised_after = copy.deepcopy(event)
    if tamper_kind == "equalizer_as_edit":
        disguised_after["review"]["equalizer"] = _valid_equalizer(
            disguised_after,
        )
    else:
        disguised_after["review"]["comment"] = "legitimate edit, wrong revision"
    disguised_id = str(uuid.uuid4())
    disguised_after.update({
        "revision_id": disguised_id,
        "revision_number": 3 if tamper_kind == "revision_gap" else 2,
        "updated_at_utc": "2026-08-06T12:00:02+00:00",
    })
    disguised = {
        "schema": "rheed-point-events-v1", "revision_id": disguised_id,
        "event_id": event_id, "actor": "live", "action": "edit",
        "recorded_at_utc": "2026-08-06T12:00:02+00:00",
        "base_revision_id": create_id, "before": event,
        "after": disguised_after,
    }
    # The record hash is internally consistent: semantic replay, not the hash
    # check, must reject a disguised action or a non-consecutive revision.
    disguised["record_sha256"] = hashlib.sha256(json.dumps(
        disguised, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(
            "session/rheed_event_revisions.jsonl",
            "".join(
                json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
                for item in (create, disguised)
            ),
        )
        archive.writestr(
            "session/rheed_point_events.json",
            json.dumps({"events": [disguised_after]}, sort_keys=True),
        )

    with pytest.raises(PointEventValidationError, match="semantics are invalid"):
        import_point_events(hash_frame_payloads(load_session_archive(path)))
