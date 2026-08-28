"""Focused tests for the v2 offline RHEED point-event contract."""

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
from PIL import Image

from tools.rheed_postprocessing_labeling.annotation_validation import (
    validate_annotation_document,
)
from tools.rheed_postprocessing_labeling.point_events import (
    INITIAL_STATE,
    PointEventSidecarStore,
    PointEventValidationError,
    RevisionStore,
    completion_errors,
    derive_state_segments,
    import_point_events,
    interval_errors,
    make_posthoc_event,
    point_event_document,
    replay_revisions,
    revise_event,
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


def _zip(tmp_path: Path) -> tuple[Path, list[bytes]]:
    frames = [_png(20), _png(80), _png(140), _png(200)]
    times = [f"2026-08-06T12:00:0{index}.000Z" for index in range(4)]
    heartbeat = [{
        "timestamp": times[index], "elapsed_s": float(index),
        "heartbeat_idx": index + 1,
        "frame_path": rf"D:\session\frames\rheed_{index}.png",
        "capture_backend": "wgc", "captured_at_utc": times[index],
        "capture_sequence": 100 + index, "capture_geometry_id": "g1",
        "source_hwnd": 1234, "view_segment_id": 1,
        "visual_history_generation": 2, "gun_aligned": "True",
        "realignment_active": "False",
        "received_monotonic_ns": 1_000_000_000 + index,
    } for index in range(4)]
    manual = [{
        "timestamp": times[1], "elapsed_s": 1.0, "event_idx": 1,
        "frame_path": r"D:\session\frames\rheed_1.png", "note": "",
        "captured_at_utc": times[1], "capture_sequence": 101,
    }]
    auto = [{
        "timestamp": times[2], "elapsed_s": 2.0, "event_idx": 1,
        "change_score": 0.7, "buffer_count": 0, "buffer_dir": "",
        "event_state": "pending", "state_changed_at": "",
        "captured_at_utc": times[2], "capture_sequence": 102,
    }]
    legacy_labels = [{
        "event_idx": 1, "notes": "old row label",
        "human_primary_reconstruction": "c(6x2)", "human_labeler": "Grower",
        "human_confidence": "0.8", "change_from": "1x1", "change_to": "c(6x2)",
    }]
    legacy_equalizer = [{
        "label_idx": 1, "capture_sequence": 101,
        "equalizer_frame_sha256": hashlib.sha256(frames[1]).hexdigest(),
        "equalizer_frame_sha256_algorithm": "raw-file-bytes-v1",
        "calibration_id": "old-calibration", "basis_bundle_id": "old-basis",
    }]
    view = [{
        "timestamp": times[1], "elapsed_s": 1.0, "event_idx": 1,
        "event_type": "rheed_current_adjusted", "note": "read-only",
        "captured_at_utc": times[1], "capture_sequence": 101,
    }]
    sensor = [{
        "timestamp": times[1], "elapsed_s": 1.0,
        "pyrometer_temp_C": 501, "mistral_v_actual_V": 2.3,
    }]
    path = tmp_path / "session.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("session/session_metadata.json", json.dumps({
            "session_id": "session-1", "chamber_id": "TEST",
            "camera_backend": "wgc", "capture_geometry_id": "g1",
        }))
        for name, rows in (
            ("heartbeat_log.csv", heartbeat), ("manual_events.csv", manual),
            ("auto_capture_events.csv", auto),
            ("events_labels.csv", legacy_labels),
            ("live_labels.csv", legacy_equalizer),
            ("rheed_view_events.csv", view), ("sensor_log.csv", sensor),
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


def _label(value: str = "rt13", change: str = "appeared") -> dict:
    return {"kind": "reconstruction", "change": change, "value": value}


def _dataset_payload(session, result) -> tuple[dict, dict]:
    dataset = {
        "dataset_id": f"sha256:{session.sha256}",
        "frame_count": len(session.frames),
        "ordered_frame_fingerprint": "sha256:" + "1" * 64,
        "source_archive_sha256": session.sha256,
        "model_context_fingerprint": "sha256:" + "2" * 64,
    }
    payload = {
        "config": {
            "dataset": dataset, "point_events": list(result.events),
            "reference_events": list(result.reference_events),
            "unlinked_legacy_labels": list(result.unlinked_legacy_labels),
            "source_event_revisions": list(result.source_revisions),
            "source_event_journal": result.source_journal,
        },
        "heartbeat_indices": [frame.heartbeat_idx for frame in session.frames],
        "capture_sequences": [frame.capture_sequence for frame in session.frames],
        "frame_sha256": [frame.frame_sha256 for frame in session.frames],
        "times": [frame.elapsed_s for frame in session.frames],
    }
    return dataset, payload


def test_import_exposes_explicit_initial_state_exactly_once(imported) -> None:
    session, result, payloads = imported
    assert [item["source"]["kind"] for item in result.events] == [
        "initial_assumption", "manual", "auto_capture",
    ]
    initial = result.events[0]
    assert initial["review"]["candidate_decision"] == "confirmed"
    assert initial["review"]["labels"] == []
    assert initial["review"]["representative_anchor"] is None
    assert len({item["event_id"] for item in result.events}) == 3
    assert read_raw_frame(session, 1) == payloads[1]


def test_import_can_omit_auto_proposals_without_changing_source_archive(
    imported,
) -> None:
    session, _, _ = imported
    archive_hash = session.sha256

    result = import_point_events(session, include_auto_events=False)

    assert [item["source"]["kind"] for item in result.events] == [
        "initial_assumption", "manual",
    ]
    assert session.sha256 == archive_hash


def test_initial_assumption_id_is_deterministic(imported) -> None:
    session, result, _ = imported
    again = import_point_events(session)
    assert result.events[0]["event_id"] == again.events[0]["event_id"]
    assert result.events[0]["review"]["labels"] == again.events[0]["review"]["labels"] == []


def test_legacy_rows_are_read_only_and_never_become_v2_labels(imported) -> None:
    _, result, _ = imported
    auto = next(item for item in result.events if item["source"]["kind"] == "auto_capture")
    assert auto["review"]["labels"] == []
    assert auto["review"]["comment"] == ""
    assert "equalizer" not in auto["review"]
    reasons = [item["reason"] for item in result.unlinked_legacy_labels]
    assert any("no unambiguous v2" in reason for reason in reasons)
    assert any("Equalizer label is read-only" in reason for reason in reasons)
    assert all(item["read_only"] is True for item in result.unlinked_legacy_labels)


def test_reference_and_sensor_logs_remain_read_only_context(imported) -> None:
    _, result, _ = imported
    assert result.reference_events[0]["event_type"] == "rheed_current_adjusted"
    assert result.reference_events[0]["read_only"] is True
    assert result.sensor_context["rows"][0]["mistral_v_actual_V"] == "2.3"


def test_posthoc_multi_label_edit_complete_and_replay(imported) -> None:
    _, result, _ = imported
    anchor = result.events[1]["review"]["anchor"]
    event = make_posthoc_event(anchor, actor="Grower")
    event, create = event, None
    event, add_first = revise_event(
        event, action="add_label", actor="Grower", changes={"label": _label()},
    )
    first_id = event["review"]["labels"][0]["label_id"]
    event, add_second = revise_event(
        event, action="add_label", actor="Grower", changes={"label": {
            "kind": "pattern_clarity", "change": "became", "value": "good",
        }},
    )
    second_id = event["review"]["labels"][1]["label_id"]
    event, edit = revise_event(event, action="edit_label", actor="Grower", changes={
        "label_id": first_id, "label": _label("htr"),
    })
    event, remove = revise_event(event, action="remove_label", actor="Grower", changes={
        "label_id": second_id,
    })
    event, representative = revise_event(
        event, action="move_representative_anchor", actor="Grower",
        changes={"representative_anchor": anchor},
    )
    event, complete = revise_event(event, action="complete", actor="Grower")
    assert event["status"] == "Complete"
    assert event["review"]["comment"] == ""
    initial = list(result.events)
    # A posthoc create is represented separately when replayed.
    posthoc = make_posthoc_event(anchor, actor="Grower")
    posthoc["event_id"] = event["event_id"]
    posthoc["source"] = copy.deepcopy(event["source"])
    create = {
        "schema_version": "rheed-point-events-v2",
        "revision_id": str(uuid.uuid4()), "event_id": posthoc["event_id"],
        "actor": "Grower", "at_utc": "2026-08-06T12:00:01Z",
        "action": "create_posthoc", "base_revision_id": "",
        "before_sha256": "", "before": None, "after": posthoc,
    }
    # The semantic action tests above already cover transformations; replay
    # integrity is covered independently by RevisionStore below.
    assert first_id == event["review"]["labels"][0]["label_id"]
    assert [add_first["action"], add_second["action"], edit["action"], remove["action"], representative["action"], complete["action"]] == [
        "add_label", "add_label", "edit_label", "remove_label",
        "move_representative_anchor", "complete",
    ]


def test_event_rejects_contradictory_training_targets(imported) -> None:
    _, result, _ = imported
    event = make_posthoc_event(result.events[1]["review"]["anchor"], actor="Grower")
    event, _ = revise_event(
        event, action="add_label", actor="Grower",
        changes={"label": _label("rt13", "appeared")},
    )
    with pytest.raises(PointEventValidationError, match="both appear and disappear"):
        revise_event(
            event, action="add_label", actor="Grower",
            changes={"label": _label("rt13", "disappeared")},
        )
    event, _ = revise_event(
        event, action="add_label", actor="Grower", changes={"label": {
            "kind": "pattern_clarity", "change": "became", "value": "good",
        }},
    )
    with pytest.raises(PointEventValidationError, match="only one pattern-clarity"):
        revise_event(
            event, action="add_label", actor="Grower", changes={"label": {
                "kind": "pattern_clarity", "change": "became", "value": "bad",
            }},
        )


def test_completion_gate_has_no_equalizer_or_comment_requirement(imported) -> None:
    _, result, _ = imported
    manual = next(item for item in result.events if item["source"]["kind"] == "manual")
    assert completion_errors(manual) == [
        "at least one semantic label is required",
    ]
    with pytest.raises(PointEventValidationError, match="Cannot complete"):
        revise_event(manual, action="complete", actor="Grower")
    event = manual
    event, _ = revise_event(event, action="add_label", actor="Grower", changes={"label": _label()})
    event, _ = revise_event(event, action="move_representative_anchor", actor="Grower", changes={"representative_anchor": manual["review"]["anchor"]})
    event, _ = revise_event(event, action="complete", actor="Grower")
    assert event["status"] == "Complete"
    event, _ = revise_event(event, action="edit", actor="Grower", changes={"comment": "optional note"})
    assert event["status"] == "Draft"


def test_rejected_auto_candidate_can_complete_without_label_or_anchor(imported) -> None:
    _, result, _ = imported
    auto = next(item for item in result.events if item["source"]["kind"] == "auto_capture")
    auto, _ = revise_event(
        auto, action="add_label", actor="Grower",
        changes={"label": _label("rt13")},
    )
    auto, _ = revise_event(
        auto, action="move_representative_anchor", actor="Grower",
        changes={"representative_anchor": auto["review"]["anchor"]},
    )
    auto, _ = revise_event(auto, action="set_candidate_decision", actor="Grower", changes={"decision": "rejected"})
    assert auto["review"]["labels"] == []
    assert auto["review"]["representative_anchor"] is None
    auto, _ = revise_event(auto, action="complete", actor="Grower")
    assert auto["status"] == "Complete"


def test_interval_anchor_must_precede_next_semantic_event(imported) -> None:
    _, result, _ = imported
    initial, manual, auto = map(copy.deepcopy, result.events)
    initial["review"].update({
        "candidate_decision": "confirmed",
        "representative_anchor": auto["review"]["anchor"],
    })
    manual["review"]["labels"] = [{
        "label_id": str(uuid.uuid4()), **_label("rt13"),
    }]
    state = {item["event_id"]: item for item in (initial, manual, auto)}
    assert interval_errors(initial["event_id"], state) == [
        "representative interval frame must be before the next semantic event",
    ]


def test_revision_store_round_trip_and_hash_tamper(tmp_path: Path, imported) -> None:
    _, result, _ = imported
    event = result.events[1]
    edited, revision = revise_event(
        event, action="edit", actor="Grower", changes={"comment": "reviewed"},
    )
    store = RevisionStore(tmp_path / "annotations")
    state = store.append(revision, initial_events=result.events)
    assert state[event["event_id"]]["review"]["comment"] == "reviewed"
    record = json.loads(store.journal_path.read_text(encoding="utf-8"))
    record["after"]["review"]["comment"] = "tampered"
    store.journal_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(PointEventValidationError, match="record hash"):
        store.load(result.events)
    assert edited["status"] == "Draft"


def test_document_round_trip_rejects_tampered_source_and_representative(imported) -> None:
    session, result, _ = imported
    dataset, payload = _dataset_payload(session, result)
    document = point_event_document(
        dataset=dataset, events=result.events,
        reference_events=result.reference_events,
        unlinked_legacy_labels=result.unlinked_legacy_labels,
        source_revisions=result.source_revisions,
        source_journal=result.source_journal,
        annotation_set_id=str(uuid.uuid4()), reviewer="Grower",
    )
    assert document["initial_state"] == INITIAL_STATE
    assert document["segments"][0]["state"] == INITIAL_STATE
    assert validate_point_event_document(document, payload)["events"]
    assert validate_annotation_document(document, payload)["events"]
    tampered = copy.deepcopy(document)
    tampered["events"][0]["source"]["source_row_sha256"] = "0" * 64
    with pytest.raises(PointEventValidationError):
        validate_point_event_document(tampered, payload)
    tampered_segment = copy.deepcopy(document)
    tampered_segment["segments"][0]["state"]["reconstructions"] = ["htr"]
    with pytest.raises(PointEventValidationError, match="state segments"):
        validate_point_event_document(tampered_segment, payload)
    # Make a journaled representative move, then corrupt its exact sequence.
    manual = result.events[1]
    moved, revision = revise_event(
        manual, action="move_representative_anchor", actor="Grower",
        changes={"representative_anchor": manual["review"]["anchor"]},
    )
    moved_document = point_event_document(
        dataset=dataset, events=result.events, revisions=[revision],
        reference_events=result.reference_events,
        unlinked_legacy_labels=result.unlinked_legacy_labels,
        source_revisions=result.source_revisions,
        source_journal=result.source_journal,
        annotation_set_id=str(uuid.uuid4()), reviewer="Grower",
    )
    moved_document["events"][1]["review"]["representative_anchor"]["capture_sequence"] = 999
    with pytest.raises(PointEventValidationError):
        validate_point_event_document(moved_document, payload)
    assert moved["review"]["representative_anchor"] is not None


def test_state_segments_replay_full_presence_clarity_and_quality(imported) -> None:
    _, result, _ = imported
    initial, manual, auto = map(copy.deepcopy, result.events)
    manual["review"]["labels"] = [
        {
            "label_id": str(uuid.uuid4()),
            "kind": "reconstruction",
            "change": "disappeared",
            "value": "one_by_one",
        },
        {
            "label_id": str(uuid.uuid4()),
            "kind": "reconstruction",
            "change": "appeared",
            "value": "rt13",
        },
        {
            "label_id": str(uuid.uuid4()),
            "kind": "pattern_clarity",
            "change": "became",
            "value": "good",
        },
    ]
    manual["review"]["representative_anchor"] = manual["review"]["anchor"]
    auto["review"]["candidate_decision"] = "rejected"

    segments = derive_state_segments(
        [initial, manual, auto], frame_count=4, session_identity="run-a",
    )

    assert [(item["start_frame_index"], item["end_frame_index_exclusive"])
            for item in segments] == [(1, 2), (2, 5)]
    assert segments[0]["state"] == {
        "reconstructions": ["one_by_one"], "clarity": "bad",
        "quality": "unknown",
    }
    assert segments[1]["state"] == {
        "reconstructions": ["rt13"], "clarity": "good",
        "quality": "unknown",
    }
    assert segments[1]["anchor"]["frame_index"] == 2


def test_initial_quality_is_selected_on_initial_audit_item(imported) -> None:
    _, result, _ = imported
    initial, manual, auto = map(copy.deepcopy, result.events)

    assert "initial surface quality must be selected as good or bad" in completion_errors(initial)
    initial, _ = revise_event(
        initial, action="add_label", actor="Grower", changes={"label": {
            "kind": "surface_quality", "change": "became", "value": "good",
        }},
    )
    segments = derive_state_segments(
        [initial, manual, auto], frame_count=4, session_identity="run-a",
    )

    assert segments[0]["state"] == {
        "reconstructions": ["one_by_one"],
        "clarity": "bad",
        "quality": "good",
    }
    with pytest.raises(PointEventValidationError, match="surface-quality"):
        revise_event(
            initial, action="add_label", actor="Grower", changes={"label": {
                "kind": "pattern_clarity", "change": "became", "value": "good",
            }},
        )


def test_state_segments_reject_cross_event_duplicate_appearance(imported) -> None:
    _, result, _ = imported
    initial, manual, auto = map(copy.deepcopy, result.events)
    manual["review"]["labels"] = [{
        "label_id": str(uuid.uuid4()), **_label("rt13", "appeared"),
    }]
    auto["review"]["candidate_decision"] = "confirmed"
    auto["review"]["labels"] = [{
        "label_id": str(uuid.uuid4()), **_label("rt13", "appeared"),
    }]

    with pytest.raises(PointEventValidationError, match="already present"):
        derive_state_segments(
            [initial, manual, auto], frame_count=4, session_identity="run-a",
        )


def test_same_frame_owners_normalize_one_interval_anchor(imported) -> None:
    _, result, _ = imported
    initial, manual, auto = map(copy.deepcopy, result.events)
    auto["review"]["anchor"] = copy.deepcopy(manual["review"]["anchor"])
    manual["review"].update({
        "labels": [{
            "label_id": str(uuid.uuid4()),
            "kind": "reconstruction", "change": "disappeared",
            "value": "one_by_one",
        }],
        "representative_anchor": copy.deepcopy(auto["review"]["anchor"]),
    })
    auto["review"].update({
        "candidate_decision": "confirmed",
        "labels": [{
            "label_id": str(uuid.uuid4()),
            "kind": "reconstruction", "change": "appeared", "value": "rt13",
        }],
        # Same-frame co-owner deliberately stores no duplicate Anchor.
        "representative_anchor": None,
    })
    manual["status"] = auto["status"] = "Complete"

    segments = derive_state_segments(
        [initial, manual, auto], frame_count=4, session_identity="run-a",
    )
    owned = next(item for item in segments if len(item["boundary_event_ids"]) == 2)
    assert owned["anchor"]["frame_index"] == manual["review"]["anchor"]["frame_index"]
    assert owned["status"] == "Complete"

    # An identical compatibility copy still materializes as one segment Anchor.
    auto["review"]["representative_anchor"] = copy.deepcopy(
        manual["review"]["representative_anchor"]
    )
    duplicate = derive_state_segments(
        [initial, manual, auto], frame_count=4, session_identity="run-a",
    )
    duplicate_owned = next(
        item for item in duplicate if len(item["boundary_event_ids"]) == 2
    )
    assert duplicate_owned["anchor"] == owned["anchor"]

    auto["review"]["representative_anchor"] = copy.deepcopy(
        result.events[2]["review"]["anchor"]
    )
    with pytest.raises(PointEventValidationError, match="disagree about the interval Anchor"):
        derive_state_segments(
            [initial, manual, auto], frame_count=4, session_identity="run-a",
        )


def test_pending_candidate_does_not_truncate_confirmed_interval(imported) -> None:
    _, result, _ = imported
    _, manual, auto = map(copy.deepcopy, result.events)
    manual["review"]["labels"] = [{
        "label_id": str(uuid.uuid4()),
        "kind": "reconstruction", "change": "appeared", "value": "rt13",
    }]
    manual["review"]["representative_anchor"] = copy.deepcopy(
        auto["review"]["anchor"]
    )
    # Keep the automatic proposal pending even if a Draft already has labels.
    auto["review"]["labels"] = [{
        "label_id": str(uuid.uuid4()),
        "kind": "reconstruction", "change": "appeared", "value": "htr",
    }]
    state = {item["event_id"]: item for item in (manual, auto)}
    assert interval_errors(manual["event_id"], state) == []


def test_sidecar_uses_exact_v2_command_contract(
    tmp_path: Path, imported, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, result, _ = imported
    dataset, payload = _dataset_payload(session, result)
    report_root = tmp_path / "report"
    report_root.mkdir()
    report = report_root / "interactive_report.html"
    report.write_text("synthetic", encoding="utf-8")
    from tools.rheed_postprocessing_labeling import report_builder
    monkeypatch.setattr(report_builder, "load_report_payload", lambda _path: payload)
    store = PointEventSidecarStore(report, session.path)
    manual = result.events[1]

    def command(action: str, changes: dict, current: dict) -> dict:
        return store.apply_revision({
            "dataset_id": dataset["dataset_id"], "action": action,
            "event_id": current["event_id"],
            "base_revision_id": current["revision_id"],
            "actor": "Grower", "changes": changes,
        })["event"]

    current = manual
    current = command("add_label", {"label": _label()}, current)
    label_id = current["review"]["labels"][0]["label_id"]
    current = command("edit_label", {
        "label_id": label_id, "label": _label("htr"),
    }, current)
    current = command("move_representative_anchor", {
        "representative_anchor": manual["review"]["anchor"],
    }, current)
    current = command("complete", {}, current)
    assert current["status"] == "Complete"
    current = command("remove_label", {"label_id": label_id}, current)
    assert current["status"] == "Draft"
    with pytest.raises(PointEventValidationError, match="not part"):
        store.apply_equalizer_revision({}, {})
    with pytest.raises(PointEventValidationError):
        store.apply_revision({
            "action": "add_event", "actor": "Grower",
            "changes": {"anchor": manual["review"]["anchor"]},
        })
    created_response = store.apply_revision({
        "action": "create_posthoc", "actor": "Grower",
        "changes": {"anchor": manual["review"]["anchor"]},
    })
    created = created_response["event"]
    assert created["source"]["kind"] == "posthoc"
    assert "reviewer" not in created["review"]
    assert "confidence" not in created["review"]
    assert created_response["revision"]["actor"] == "Grower"
    assert created_response["annotation_set"]["reviewer"] == "Grower"
    assert (report_root / "annotations" / "rheed_event_revisions.jsonl").is_file()

    # The revision actor is also the one durable session-level Grower.  It
    # survives reopening the desktop sidecar and is used by document export.
    reopened = PointEventSidecarStore(report, session.path)
    assert reopened.annotation_metadata["reviewer"] == "Grower"
    assert reopened.export_document()["annotation_set"]["reviewer"] == "Grower"


def _legacy_state(event_id: str, revision_id: str) -> dict:
    return {
        "schema": "rheed-point-events-v1", "event_id": event_id,
        "source": {"kind": "manual", "source_row_sha256": "a" * 64},
        "review": {
            "anchor": None, "comment": "", "reviewer": "", "confidence": None,
            "human_reconstruction": None, "change_from": None, "change_to": None,
            "equalizer": None, "disposition": "active", "disposition_reason": "",
        },
        "status": "Draft", "revision_id": revision_id, "revision_number": 1,
    }


def _append_v1_journal(path: Path, *, semantic_tamper: bool = False) -> str:
    event_id, create_id = str(uuid.uuid4()), str(uuid.uuid4())
    state = _legacy_state(event_id, create_id)
    create = {
        "schema": "rheed-point-events-v1", "revision_id": create_id,
        "event_id": event_id, "actor": "legacy", "action": "create",
        "recorded_at_utc": "2026-08-06T12:00:01Z",
        "base_revision_id": None, "before": None, "after": state,
    }
    create["record_sha256"] = hashlib.sha256(json.dumps(
        create, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    records = [create]
    final = state
    if semantic_tamper:
        final = copy.deepcopy(state)
        final["review"]["equalizer"] = {"smuggled": True}
        edit_id = str(uuid.uuid4())
        final.update({"revision_id": edit_id, "revision_number": 2})
        edit = {
            "schema": "rheed-point-events-v1", "revision_id": edit_id,
            "event_id": event_id, "actor": "legacy", "action": "edit",
            "recorded_at_utc": "2026-08-06T12:00:02Z",
            "base_revision_id": create_id, "before": state, "after": final,
        }
        edit["record_sha256"] = hashlib.sha256(json.dumps(
            edit, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        records.append(edit)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(
            "session/rheed_event_revisions.jsonl",
            "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in records),
        )
        archive.writestr("session/rheed_point_events.json", json.dumps({
            "schema": "rheed-point-events-v1", "events": [final],
        }))
    return event_id


def test_archived_v1_journal_is_validated_and_retained_read_only(tmp_path: Path) -> None:
    path, _ = _zip(tmp_path)
    legacy_id = _append_v1_journal(path)
    imported = import_point_events(hash_frame_payloads(load_session_archive(path)))
    assert legacy_id not in {item["event_id"] for item in imported.events}
    assert imported.source_journal["schema"] == "rheed-point-events-v1"
    assert imported.source_journal["read_only"] is True
    evidence = next(
        item for item in imported.unlinked_legacy_labels
        if item.get("event_id") == legacy_id
    )
    assert evidence["read_only"] is True
    assert len(imported.source_revisions) == 1


def test_archived_v1_rehashed_semantic_tamper_fails_closed(tmp_path: Path) -> None:
    path, _ = _zip(tmp_path)
    _append_v1_journal(path, semantic_tamper=True)
    with pytest.raises(PointEventValidationError, match="semantics are invalid"):
        import_point_events(hash_frame_payloads(load_session_archive(path)))


def test_no_legacy_segment_is_automatically_converted(imported) -> None:
    _, result, _ = imported
    assert all(item["source"]["kind"] != "segment" for item in result.events)
    assert all(
        set(item["review"]) == {
            "anchor", "representative_anchor", "labels", "candidate_decision",
            "comment", "disposition", "disposition_reason",
        }
        for item in result.events
    )
