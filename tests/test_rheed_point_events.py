"""V2 semantic point-event journal tests."""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest

from gui.rheed_point_events import (
    JOURNAL_NAME,
    LEGACY_SCHEMA_ID,
    STATUS_COMPLETE,
    STATUS_DRAFT,
    SUMMARY_NAME,
    TRANSACTION_NAME,
    PointEventCompletionError,
    PointEventError,
    PointEventIntegrityError,
    PointEventStore,
    deterministic_legacy_event_id,
    make_review_anchor,
)


def _anchor(path: Path, elapsed: float, sequence: int, payload: bytes) -> dict:
    path.write_bytes(payload)
    return make_review_anchor(
        frame_path=path, capture_sequence=sequence,
        captured_at_utc=f"2026-08-17T12:00:{sequence:02d}+00:00",
        elapsed_s=elapsed, view_segment_id=3, capture_geometry_id="geom-a",
    )


def _create(
    store: PointEventStore, anchor: dict, *, source_kind: str = "manual",
    source_index: int = 1,
) -> dict:
    return store.create_event(
        source_kind=source_kind, actor="grower-live",
        session_identity="growth_sample_20260817_120000",
        source_file=f"{source_kind}_events.csv", source_index=source_index,
        source_row={"event_idx": str(source_index), "note": ""},
        original_at_utc=anchor["captured_at_utc"],
        original_elapsed_s=anchor["elapsed_s"], capture_sequence=anchor["capture_sequence"],
        original_frame_path=anchor["frame_path"],
        original_image_sha256=anchor["image_sha256"], review_anchor=anchor,
    )


def _label(value: str = "rt13", change: str = "appeared") -> dict:
    return {"kind": "reconstruction", "change": change, "value": value}


def test_legacy_ids_are_stable_and_do_not_collide_across_sources():
    common = dict(
        session_identity="session-a", source_index=1,
        original_at_utc="2026-08-17T12:00:00Z", capture_sequence=42,
    )
    first = deterministic_legacy_event_id(source_file="manual_events.csv", **common)
    assert first == deterministic_legacy_event_id(source_file="manual_events.csv", **common)
    assert first != deterministic_legacy_event_id(source_file="auto_capture_events.csv", **common)


def test_comment_is_optional_but_label_and_representative_are_required(tmp_path):
    anchor = _anchor(tmp_path / "frame.bmp", 12.5, 7, b"frame")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor)
    assert event["status"] == STATUS_DRAFT
    assert event["review"]["comment"] == ""
    assert "equalizer" not in event["review"]
    assert "reviewer" not in event["review"]
    assert "confidence" not in event["review"]

    with pytest.raises(PointEventCompletionError) as rejected:
        store.complete(event["event_id"], actor="reviewer-a")
    assert set(rejected.value.errors) == {
        "at least one semantic label is required",
        "a representative interval frame is required",
    }
    event = store.add_label(event["event_id"], actor="reviewer-a", label=_label())
    event = store.move_representative_anchor(event["event_id"], actor="reviewer-a", anchor=anchor)
    event = store.complete(event["event_id"], actor="reviewer-a")
    assert event["status"] == STATUS_COMPLETE
    assert event["review"]["comment"] == ""


def test_non_rejected_complete_requires_saved_frame_review_point(tmp_path):
    anchor = _anchor(tmp_path / "frame.bmp", 12.5, 7, b"frame")
    store = PointEventStore(tmp_path)
    event = store.create_event(
        source_kind="manual", actor="grower-live",
        session_identity="growth_sample_20260817_120000",
        source_file="manual_events.csv", source_index=1,
        source_row={"event_idx": "1", "note": ""},
        original_at_utc=anchor["captured_at_utc"],
        original_elapsed_s=anchor["elapsed_s"],
        capture_sequence=anchor["capture_sequence"],
        original_frame_path=anchor["frame_path"],
        original_image_sha256=anchor["image_sha256"],
        review_anchor=None,
    )
    event = store.add_label(
        event["event_id"], actor="reviewer-a", label=_label(),
    )
    store.move_representative_anchor(
        event["event_id"], actor="reviewer-a", anchor=anchor,
    )

    with pytest.raises(
        PointEventCompletionError,
        match="saved-frame review point",
    ):
        store.complete(event["event_id"], actor="reviewer-a")


def test_multiple_labels_have_stable_ids_and_each_is_editable(tmp_path):
    anchor = _anchor(tmp_path / "frame.bmp", 5, 5, b"frame")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor)
    event = store.add_label(event["event_id"], actor="grower", label=_label("rt13"))
    first_id = event["review"]["labels"][0]["label_id"]
    event = store.add_label(event["event_id"], actor="grower", label={
        "kind": "pattern_clarity", "change": "became", "value": "good",
    })
    second_id = event["review"]["labels"][1]["label_id"]
    assert first_id != second_id
    event = store.edit_label(
        event["event_id"], actor="grower", label_id=first_id,
        label=_label("htr"),
    )
    assert event["review"]["labels"][0] == {
        "label_id": first_id, **_label("htr"),
    }
    event = store.remove_label(event["event_id"], actor="grower", label_id=second_id)
    assert [item["label_id"] for item in event["review"]["labels"]] == [first_id]
    with pytest.raises(PointEventError, match="Duplicate semantic label"):
        store.add_label(event["event_id"], actor="grower", label=_label("htr"))
    with pytest.raises(PointEventError, match="both appear and disappear"):
        store.add_label(
            event["event_id"], actor="grower",
            label=_label("htr", change="disappeared"),
        )
    event = store.add_label(event["event_id"], actor="grower", label={
        "kind": "pattern_clarity", "change": "became", "value": "good",
    })
    with pytest.raises(PointEventError, match="only one pattern-clarity"):
        store.add_label(event["event_id"], actor="grower", label={
            "kind": "pattern_clarity", "change": "became", "value": "bad",
        })


def test_auto_candidate_requires_decision_and_rejected_candidate_can_complete(tmp_path):
    anchor = _anchor(tmp_path / "frame.bmp", 8, 8, b"frame")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor, source_kind="auto_capture")
    assert event["review"]["candidate_decision"] == "pending"
    event = store.add_label(event["event_id"], actor="reviewer", label=_label("rt13"))
    event = store.move_representative_anchor(
        event["event_id"], actor="reviewer", anchor=anchor,
    )
    with pytest.raises(PointEventCompletionError, match="confirmed or rejected"):
        store.complete(event["event_id"], actor="reviewer")
    event = store.set_candidate_decision(event["event_id"], actor="reviewer", decision="rejected")
    assert event["review"]["labels"] == []
    assert event["review"]["representative_anchor"] is None
    revision = json.loads(
        (tmp_path / JOURNAL_NAME).read_text(encoding="utf-8").splitlines()[-1]
    )
    assert revision["action"] == "set_candidate_decision"
    assert revision["before"]["review"]["labels"]
    assert revision["after"]["review"]["labels"] == []
    event = store.complete(event["event_id"], actor="reviewer")
    assert event["status"] == STATUS_COMPLETE
    assert event["review"]["labels"] == []
    assert event["review"]["representative_anchor"] is None
    event = store.set_candidate_decision(event["event_id"], actor="reviewer", decision="confirmed")
    assert event["status"] == STATUS_DRAFT
    assert event["review"]["labels"] == []


def test_initial_one_by_one_state_is_deterministic_and_exactly_once(tmp_path):
    anchor = _anchor(tmp_path / "first.bmp", 0, 1, b"first")
    store = PointEventStore(tmp_path)
    first = store.ensure_initial_assumption(
        actor="system", session_identity="session-a", first_frame_anchor=anchor,
    )
    again = store.ensure_initial_assumption(
        actor="system", session_identity="session-a", first_frame_anchor=anchor,
    )
    assert again["event_id"] == first["event_id"]
    assert first["source"]["kind"] == "initial_assumption"
    assert first["review"]["candidate_decision"] == "confirmed"
    assert first["review"]["labels"] == []
    assert len(store.states) == 1

    with pytest.raises(
        PointEventCompletionError,
        match="initial surface quality must be selected",
    ):
        store.complete(first["event_id"], actor="grower")

    first = store.add_label(first["event_id"], actor="grower", label={
        "kind": "surface_quality", "change": "became", "value": "good",
    })
    first = store.move_representative_anchor(
        first["event_id"], actor="grower", anchor=anchor,
    )
    assert store.complete(first["event_id"], actor="grower")["status"] == STATUS_COMPLETE


def test_representative_anchor_must_lie_in_following_interval(tmp_path):
    before = _anchor(tmp_path / "before.bmp", 4, 4, b"before")
    first_anchor = _anchor(tmp_path / "first.bmp", 10, 10, b"first")
    representative = _anchor(tmp_path / "representative.bmp", 15, 15, b"representative")
    next_anchor = _anchor(tmp_path / "next.bmp", 20, 20, b"next")
    after_next = _anchor(tmp_path / "after_next.bmp", 21, 21, b"after-next")
    store = PointEventStore(tmp_path)
    first = _create(store, first_anchor, source_index=1)
    second = _create(store, next_anchor, source_index=2)
    first = store.add_label(first["event_id"], actor="r", label=_label("rt13"))
    second = store.add_label(second["event_id"], actor="r", label=_label("htr"))
    first = store.move_representative_anchor(first["event_id"], actor="r", anchor=before)
    with pytest.raises(PointEventCompletionError, match="at or after"):
        store.complete(first["event_id"], actor="r")
    first = store.move_representative_anchor(first["event_id"], actor="r", anchor=after_next)
    with pytest.raises(PointEventCompletionError, match="before the next"):
        store.complete(first["event_id"], actor="r")
    first = store.move_representative_anchor(first["event_id"], actor="r", anchor=representative)
    assert store.complete(first["event_id"], actor="r")["status"] == STATUS_COMPLETE


def test_same_time_semantic_events_share_the_following_interval(tmp_path):
    boundary = _anchor(tmp_path / "boundary.bmp", 10, 10, b"boundary")
    representative = _anchor(tmp_path / "representative.bmp", 12, 12, b"representative")
    store = PointEventStore(tmp_path)
    first = _create(store, boundary, source_index=1)
    second = _create(store, boundary, source_index=2)
    for event, value in ((first, "rt13"), (second, "c_six_by_two")):
        store.add_label(event["event_id"], actor="r", label=_label(value))
    # The atomic boundary owns one interval Anchor, not one per event.
    store.move_representative_anchor(
        first["event_id"], actor="r", anchor=representative,
    )
    assert store.complete(first["event_id"], actor="r")["status"] == STATUS_COMPLETE
    completed_second = store.complete(second["event_id"], actor="r")
    assert completed_second["status"] == STATUS_COMPLETE
    assert completed_second["review"]["representative_anchor"] is None


def test_same_time_conflicting_interval_anchors_fail_closed(tmp_path):
    boundary = _anchor(tmp_path / "boundary.bmp", 10, 10, b"boundary")
    first_anchor = _anchor(tmp_path / "first_rep.bmp", 12, 12, b"first")
    conflicting = _anchor(tmp_path / "second_rep.bmp", 13, 13, b"second")
    store = PointEventStore(tmp_path)
    first = _create(store, boundary, source_index=1)
    second = _create(store, boundary, source_index=2)
    store.add_label(first["event_id"], actor="r", label=_label("rt13"))
    store.add_label(second["event_id"], actor="r", label=_label("c_six_by_two"))
    store.move_representative_anchor(
        first["event_id"], actor="r", anchor=first_anchor,
    )

    with pytest.raises(PointEventError, match="disagree about the interval Anchor"):
        store.move_representative_anchor(
            second["event_id"], actor="r", anchor=conflicting,
        )
    assert store.get(second["event_id"])["review"]["representative_anchor"] is None


def test_later_semantic_edit_cannot_silently_invalidate_complete_interval(tmp_path):
    boundary = _anchor(tmp_path / "boundary.bmp", 10, 10, b"boundary")
    late_representative = _anchor(tmp_path / "late.bmp", 30, 30, b"late")
    later_boundary = _anchor(tmp_path / "later_boundary.bmp", 20, 20, b"later")
    store = PointEventStore(tmp_path)
    first = _create(store, boundary, source_index=1)
    later = _create(store, later_boundary, source_index=2)
    first = store.add_label(first["event_id"], actor="r", label=_label("rt13"))
    first = store.move_representative_anchor(first["event_id"], actor="r", anchor=late_representative)
    store.complete(first["event_id"], actor="r")

    with pytest.raises(PointEventError, match="invalidate a Complete event interval"):
        store.add_label(later["event_id"], actor="r", label=_label("htr"))
    assert store.get(later["event_id"])["review"]["labels"] == []

    store.reopen(first["event_id"], actor="r")
    assert store.add_label(later["event_id"], actor="r", label=_label("htr"))[
        "review"
    ]["labels"]


def test_completed_semantic_or_anchor_edit_reopens_draft(tmp_path):
    first = _anchor(tmp_path / "first.bmp", 10, 10, b"first")
    second = _anchor(tmp_path / "second.bmp", 11, 11, b"second")
    store = PointEventStore(tmp_path)
    event = _create(store, first)
    event = store.add_label(event["event_id"], actor="r", label=_label())
    event = store.move_representative_anchor(event["event_id"], actor="r", anchor=second)
    event = store.complete(event["event_id"], actor="r")
    label_id = event["review"]["labels"][0]["label_id"]
    event = store.edit_label(event["event_id"], actor="r", label_id=label_id, label=_label("htr"))
    assert event["status"] == STATUS_DRAFT
    event = store.complete(event["event_id"], actor="r")
    event = store.move_review_anchor(event["event_id"], actor="r", anchor=second)
    assert event["status"] == STATUS_DRAFT


def test_source_is_immutable_and_global_hash_chain_fails_closed(tmp_path):
    anchor = _anchor(tmp_path / "frame.bmp", 1, 1, b"frame")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor)
    store.edit_review(event["event_id"], actor="r", comment="reviewed")
    journal = tmp_path / JOURNAL_NAME
    records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    records[-1]["previous_record_sha256"] = "f" * 64
    records[-1]["record_sha256"] = PointEventStore._record_hash(records[-1])
    journal.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    with pytest.raises(PointEventIntegrityError, match="global revision hash chain"):
        PointEventStore(tmp_path)


@pytest.mark.parametrize("action", ["add_label", "edit_label", "remove_label"])
def test_rehashed_journal_cannot_disguise_multi_label_mutation(tmp_path, action):
    anchor = _anchor(tmp_path / "frame.bmp", 1, 1, b"frame")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor)
    if action in {"edit_label", "remove_label"}:
        event = store.add_label(event["event_id"], actor="r", label=_label())
        event = store.add_label(event["event_id"], actor="r", label={
            "kind": "pattern_clarity", "change": "became", "value": "good",
        })
    if action == "add_label":
        store.add_label(event["event_id"], actor="r", label=_label())
    elif action == "edit_label":
        store.edit_label(
            event["event_id"], actor="r",
            label_id=event["review"]["labels"][0]["label_id"],
            label=_label("htr"),
        )
    else:
        store.remove_label(
            event["event_id"], actor="r",
            label_id=event["review"]["labels"][0]["label_id"],
        )

    journal = tmp_path / JOURNAL_NAME
    records = [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
    ]
    after_labels = records[-1]["after"]["review"]["labels"]
    if action == "add_label":
        after_labels.append({
            "label_id": str(uuid.uuid4()),
            "kind": "pattern_clarity", "change": "became", "value": "good",
        })
    elif action == "edit_label":
        after_labels[1] = {
            **after_labels[1],
            "value": "bad",
        }
    else:
        records[-1]["after"]["review"]["labels"] = []
    records[-1]["record_sha256"] = PointEventStore._record_hash(records[-1])
    journal.write_text(
        "".join(json.dumps(item) + "\n" for item in records),
        encoding="utf-8",
    )

    with pytest.raises(PointEventIntegrityError, match=action):
        PointEventStore(tmp_path)


def test_v1_journal_is_validated_and_opened_read_only(tmp_path):
    event_id, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
    state = {
        "schema": LEGACY_SCHEMA_ID, "event_id": event_id,
        "source": {"kind": "manual"},
        "review": {
            "anchor": None, "comment": "", "reviewer": "", "confidence": None,
            "human_reconstruction": None, "change_from": None, "change_to": None,
            "equalizer": None, "disposition": "active", "disposition_reason": "",
        },
        "status": STATUS_DRAFT, "revision_id": revision_id,
        "revision_number": 1,
    }
    record = {
        "schema": LEGACY_SCHEMA_ID, "revision_id": revision_id,
        "event_id": event_id, "actor": "legacy", "action": "create",
        "base_revision_id": None, "before": None, "after": state,
    }
    record["record_sha256"] = PointEventStore._record_hash(record)
    (tmp_path / JOURNAL_NAME).write_text(json.dumps(record) + "\n", encoding="utf-8")
    store = PointEventStore(tmp_path)
    assert store.read_only_legacy is True
    assert store.get(event_id) == state
    with pytest.raises(PointEventError, match="read-only"):
        _create(store, _anchor(tmp_path / "new.bmp", 2, 2, b"new"))


def test_pending_transaction_recovers_exactly_once(tmp_path):
    anchor = _anchor(tmp_path / "frame.bmp", 1, 1, b"frame")
    event = _create(PointEventStore(tmp_path), anchor)
    last_line = (tmp_path / JOURNAL_NAME).read_bytes().splitlines()[-1] + b"\n"
    (tmp_path / TRANSACTION_NAME).write_bytes(last_line)
    recovered = PointEventStore(tmp_path)
    assert recovered.get(event["event_id"])["revision_id"] == event["revision_id"]
    assert not (tmp_path / TRANSACTION_NAME).exists()
    assert len((tmp_path / JOURNAL_NAME).read_text(encoding="utf-8").splitlines()) == 1
    summary = json.loads((tmp_path / SUMMARY_NAME).read_text(encoding="utf-8"))
    assert summary["schema"] == "rheed-point-events-v2"


def test_source_events_dismiss_and_only_posthoc_deletes(tmp_path):
    anchor = _anchor(tmp_path / "frame.bmp", 1, 1, b"frame")
    store = PointEventStore(tmp_path)
    source = _create(store, anchor)
    with pytest.raises(PointEventError, match="cannot be deleted"):
        store.delete_posthoc(source["event_id"], actor="r", reason="duplicate")
    assert store.dismiss(source["event_id"], actor="r", reason="false trigger")["review"]["disposition"] == "dismissed"
    posthoc = _create(store, anchor, source_kind="posthoc", source_index=2)
    assert store.delete_posthoc(posthoc["event_id"], actor="r", reason="duplicate")["review"]["disposition"] == "deleted"
