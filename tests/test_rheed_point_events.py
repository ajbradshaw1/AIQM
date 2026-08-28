"""Point-event journal and live acquisition integration tests."""

from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from gui.growth_logger import GrowthLogger
from gui.rheed_point_events import (
    ACTIVE_EQUALIZER_CLASSES,
    JOURNAL_NAME,
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
    source_row_sha256,
)
from gui.equalizer_label_contract import build_equalizer_payload


def _frame(path: Path, payload: bytes = b"saved-rheed-frame") -> dict:
    path.write_bytes(payload)
    return make_review_anchor(
        frame_path=path,
        capture_sequence=7,
        captured_at_utc="2026-08-17T12:00:00+00:00",
        elapsed_s=12.5,
        view_segment_id=3,
        capture_geometry_id="geom-a",
    )


def _create(store: PointEventStore, anchor: dict | None, **kwargs) -> dict:
    return store.create_event(
        source_kind=kwargs.pop("source_kind", "manual"),
        actor="grower-live",
        session_identity="growth_sample_20260817_120000",
        source_file=kwargs.pop("source_file", "manual_events.csv"),
        source_index=kwargs.pop("source_index", 1),
        source_row=kwargs.pop("source_row", {"event_idx": "1", "note": ""}),
        original_at_utc="2026-08-17T12:00:00+00:00",
        original_elapsed_s=12.5,
        original_note=kwargs.pop("original_note", ""),
        capture_sequence=7,
        original_frame_path=anchor["frame_path"] if anchor else "",
        original_image_sha256=anchor["image_sha256"] if anchor else "",
        review_anchor=anchor,
        **kwargs,
    )


def _equalizer(anchor: dict) -> dict:
    values = {label: float(index + 1) for index, label in enumerate(ACTIVE_EQUALIZER_CLASSES)}
    normalized = {label: value / 10.0 for label, value in values.items()}
    return {
        "valid": True,
        "calibration_id": "cal-1",
        "basis_bundle_id": "basis-1",
        "frame_sha256": anchor["image_sha256"],
        "frame_sha256_algorithm": "raw-file-bytes-v1",
        "capture_sequence": anchor["capture_sequence"],
        "active_classes": list(ACTIVE_EQUALIZER_CLASSES),
        "weights": {
            "raw": {**values, "HTR": None},
            "final": {**values, "HTR": None},
            "normalized": {**normalized, "HTR": None},
        },
        "fit_residual": 1.25,
        "valid_coverage": 0.8,
        "HTR": None,
    }


def test_legacy_ids_are_stable_and_do_not_collide_across_sources():
    common = dict(
        session_identity="session-a",
        source_index=1,
        original_at_utc="2026-08-17T12:00:00Z",
        capture_sequence=42,
    )
    first = deterministic_legacy_event_id(source_file="manual_events.csv", **common)
    assert first == deterministic_legacy_event_id(source_file="manual_events.csv", **common)
    assert first != deterministic_legacy_event_id(
        source_file="auto_capture_events.csv", **common,
    )
    assert first != deterministic_legacy_event_id(
        source_file="manual_events.csv", **{**common, "session_identity": "session-b"},
    )


def test_blank_live_mark_is_draft_and_complete_is_strict(tmp_path):
    anchor = _frame(tmp_path / "frame.bmp")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor)
    assert event["status"] == STATUS_DRAFT
    assert event["review"]["comment"] == ""

    with pytest.raises(PointEventCompletionError) as rejected:
        store.complete(event["event_id"], actor="reviewer-a")
    assert "comment is required" in rejected.value.errors
    assert "reviewer is required" in rejected.value.errors
    assert any("Equalizer" in message for message in rejected.value.errors)

    event = store.edit_review(
        event["event_id"], actor="reviewer-a",
        comment="The streak pattern changed.", reviewer="reviewer-a",
    )
    event = store.set_equalizer(
        event["event_id"], actor="reviewer-a", equalizer=_equalizer(anchor),
    )
    event = store.complete(event["event_id"], actor="reviewer-a")
    assert event["status"] == STATUS_COMPLETE

    event = store.edit_review(
        event["event_id"], actor="reviewer-a", comment="Revised comment",
    )
    assert event["status"] == STATUS_DRAFT


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"capture_sequence": 8}, "different capture sequence"),
        ({"frame_sha256_algorithm": "md5"}, "hash algorithm is incompatible"),
        ({"valid_coverage": 0.0}, "valid coverage is invalid"),
    ),
)
def test_live_equalizer_rejects_offline_incompatible_frame_contract(
    tmp_path, changes, message,
):
    anchor = _frame(tmp_path / "frame.bmp")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor)
    measurement = _equalizer(anchor)
    measurement.update(changes)
    revision_count = len(
        (tmp_path / JOURNAL_NAME).read_text(encoding="utf-8").splitlines()
    )

    with pytest.raises(PointEventError, match=message):
        store.set_equalizer(
            event["event_id"], actor="reviewer-a", equalizer=measurement,
        )

    assert len(
        (tmp_path / JOURNAL_NAME).read_text(encoding="utf-8").splitlines()
    ) == revision_count
    assert store.get(event["event_id"])["review"]["equalizer"] is None


def test_live_equalizer_round_trips_through_offline_measurement_contract(tmp_path):
    from tools.rheed_postprocessing_labeling.point_events import (
        validate_equalizer_measurement,
    )

    anchor = _frame(tmp_path / "frame.bmp")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor)
    measurement = _equalizer(anchor)

    assert validate_equalizer_measurement(measurement, anchor)[
        "frame_sha256_algorithm"
    ] == "raw-file-bytes-v1"
    saved = store.set_equalizer(
        event["event_id"], actor="reviewer-a", equalizer=measurement,
    )
    replayed = PointEventStore(tmp_path).get(event["event_id"])
    assert replayed["review"]["equalizer"] == saved["review"]["equalizer"]


def test_live_equalizer_accepts_offline_verified_rgb_hash_binding(tmp_path):
    from tools.rheed_postprocessing_labeling.point_events import (
        validate_equalizer_measurement,
    )

    anchor = _frame(tmp_path / "frame.bmp")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor)
    measurement = _equalizer(anchor)
    measurement.update({
        "frame_sha256": "a" * 64,
        "frame_sha256_algorithm": "rgb-array-v1",
        "raw_frame_sha256": anchor["image_sha256"],
        "raw_frame_sha256_algorithm": "raw-file-bytes-v1",
        "rgb_hash_verified_from_raw_frame": True,
    })

    validate_equalizer_measurement(measurement, anchor)
    saved = store.set_equalizer(
        event["event_id"], actor="reviewer-a", equalizer=measurement,
    )
    assert saved["review"]["equalizer"]["frame_sha256_algorithm"] == "rgb-array-v1"


def test_move_anchor_snaps_to_saved_frame_and_invalidates_equalizer(tmp_path):
    first = _frame(tmp_path / "first.bmp", b"first")
    second = _frame(tmp_path / "second.bmp", b"second")
    store = PointEventStore(tmp_path)
    event = _create(store, first)
    event = store.edit_review(
        event["event_id"], actor="reviewer-a", comment="changed", reviewer="reviewer-a",
    )
    event = store.set_equalizer(
        event["event_id"], actor="reviewer-a", equalizer=_equalizer(first),
    )
    event = store.complete(event["event_id"], actor="reviewer-a")

    event = store.move_review_anchor(
        event["event_id"], actor="reviewer-a", anchor=second,
    )
    assert event["status"] == STATUS_DRAFT
    assert event["review"]["equalizer"] is None
    assert event["review"]["anchor"]["image_sha256"] == second["image_sha256"]
    with pytest.raises(PointEventError, match="not a saved frame"):
        store.move_review_anchor(
            event["event_id"], actor="reviewer-a",
            anchor={**second, "frame_path": str(tmp_path / "missing.bmp")},
        )


def test_same_timestamp_events_remain_distinct_and_replay(tmp_path):
    anchor = _frame(tmp_path / "same.bmp")
    store = PointEventStore(tmp_path)
    first = _create(store, anchor, source_index=1)
    second = _create(store, anchor, source_index=2)
    assert first["event_id"] != second["event_id"]

    replayed = PointEventStore(tmp_path)
    assert set(replayed.states) == {first["event_id"], second["event_id"]}
    summary = json.loads((tmp_path / SUMMARY_NAME).read_text(encoding="utf-8"))
    assert len(summary["events"]) == 2
    records = [
        json.loads(line)
        for line in (tmp_path / JOURNAL_NAME).read_text(encoding="utf-8").splitlines()
    ]
    assert all(record["actor"] == "grower-live" for record in records)
    assert all("before" in record and "after" in record for record in records)


def test_source_is_immutable_and_tampering_fails_closed(tmp_path):
    anchor = _frame(tmp_path / "frame.bmp")
    event = _create(PointEventStore(tmp_path), anchor)
    journal = tmp_path / JOURNAL_NAME
    record = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    record["after"]["source"]["original_note"] = "tampered"
    journal.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(PointEventIntegrityError, match="hash mismatch"):
        PointEventStore(tmp_path)
    assert event["source"]["original_note"] == ""


def test_pending_transaction_is_recovered_exactly_once(tmp_path):
    anchor = _frame(tmp_path / "frame.bmp")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor)
    last_line = (tmp_path / JOURNAL_NAME).read_bytes().splitlines()[-1] + b"\n"
    (tmp_path / TRANSACTION_NAME).write_bytes(last_line)

    recovered = PointEventStore(tmp_path)
    assert recovered.get(event["event_id"])["revision_id"] == event["revision_id"]
    assert not (tmp_path / TRANSACTION_NAME).exists()
    assert len((tmp_path / JOURNAL_NAME).read_text(encoding="utf-8").splitlines()) == 1


def test_conflicting_pending_transaction_is_rejected_and_retained(tmp_path):
    anchor = _frame(tmp_path / "frame.bmp")
    _create(PointEventStore(tmp_path), anchor)
    journal = tmp_path / JOURNAL_NAME
    committed = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    conflicting = copy.deepcopy(committed)
    conflicting["actor"] = "different-actor"
    conflicting["record_sha256"] = PointEventStore._record_hash(conflicting)
    pending = tmp_path / TRANSACTION_NAME
    pending.write_text(json.dumps(conflicting) + "\n", encoding="utf-8")

    with pytest.raises(PointEventIntegrityError, match="conflicts with journal"):
        PointEventStore(tmp_path)
    assert pending.is_file()
    assert json.loads(journal.read_text(encoding="utf-8")) == committed


def test_unjournaled_pending_transaction_is_appended_once(tmp_path, monkeypatch):
    anchor = _frame(tmp_path / "frame.bmp")
    store = PointEventStore(tmp_path)
    event = _create(store, anchor)

    def fail_append(_record):
        raise OSError("simulated exit before journal append")

    monkeypatch.setattr(store, "_append_record", fail_append)
    with pytest.raises(OSError, match="before journal append"):
        store.edit_review(
            event["event_id"], actor="reviewer-a", comment="recovered edit",
        )
    assert (tmp_path / TRANSACTION_NAME).is_file()
    assert len((tmp_path / JOURNAL_NAME).read_text(encoding="utf-8").splitlines()) == 1

    recovered = PointEventStore(tmp_path)
    assert recovered.get(event["event_id"])["review"]["comment"] == "recovered edit"
    assert not (tmp_path / TRANSACTION_NAME).exists()
    assert len((tmp_path / JOURNAL_NAME).read_text(encoding="utf-8").splitlines()) == 2
    assert len(PointEventStore(tmp_path).states) == 1


def test_rehashed_edit_cannot_smuggle_protected_state(tmp_path):
    mutations = {
        "status": lambda record, _anchor: record["after"].update(
            {"status": STATUS_COMPLETE}
        ),
        "equalizer": lambda record, anchor: record["after"]["review"].update(
            {"equalizer": _equalizer(anchor)}
        ),
        "source": lambda record, _anchor: record["after"]["source"].update(
            {"original_note": "rehashed tamper"}
        ),
    }
    for name, mutate in mutations.items():
        case = tmp_path / name
        case.mkdir()
        anchor = _frame(case / "frame.bmp")
        store = PointEventStore(case)
        event = _create(store, anchor)
        store.edit_review(event["event_id"], actor="reviewer-a", comment="legitimate")
        journal = case / JOURNAL_NAME
        records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        mutate(records[-1], anchor)
        records[-1]["record_sha256"] = PointEventStore._record_hash(records[-1])
        journal.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        with pytest.raises(PointEventIntegrityError):
            PointEventStore(case)


def test_summary_write_failure_keeps_pending_marker_until_restart(
    tmp_path, monkeypatch,
):
    anchor = _frame(tmp_path / "frame.bmp")
    store = PointEventStore(tmp_path)

    def fail_summary():
        raise OSError("simulated forced-exit window")

    monkeypatch.setattr(store, "_write_summary", fail_summary)
    with pytest.raises(OSError, match="forced-exit"):
        _create(store, anchor)

    assert (tmp_path / TRANSACTION_NAME).is_file()
    assert len((tmp_path / JOURNAL_NAME).read_text(encoding="utf-8").splitlines()) == 1

    recovered = PointEventStore(tmp_path)
    assert len(recovered.states) == 1
    assert not (tmp_path / TRANSACTION_NAME).exists()
    summary = json.loads((tmp_path / SUMMARY_NAME).read_text(encoding="utf-8"))
    assert len(summary["events"]) == 1


def test_source_events_dismiss_but_cannot_delete(tmp_path):
    anchor = _frame(tmp_path / "frame.bmp")
    store = PointEventStore(tmp_path)
    source = _create(store, anchor)
    with pytest.raises(PointEventError, match="cannot be deleted"):
        store.delete_posthoc(source["event_id"], actor="reviewer-a", reason="duplicate")
    dismissed = store.dismiss(
        source["event_id"], actor="reviewer-a", reason="false trigger",
    )
    assert dismissed["review"]["disposition"] == "dismissed"

    posthoc = _create(
        store, anchor, source_kind="posthoc", source_file="posthoc",
        source_index=1, source_row={"created_in_review": True},
    )
    with pytest.raises(PointEventError, match="Only source events"):
        store.dismiss(posthoc["event_id"], actor="reviewer-a", reason="duplicate")
    deleted = store.delete_posthoc(
        posthoc["event_id"], actor="reviewer-a", reason="duplicate",
    )
    assert deleted["review"]["disposition"] == "deleted"


def test_growth_logger_manual_mark_preserves_csv_and_creates_draft(tmp_path):
    logger = GrowthLogger(base_dir=tmp_path)
    logger.start_session("point-event")
    frame = np.full((16, 16, 3), 80, dtype=np.uint8)
    original_fields = list(GrowthLogger.MANUAL_EVENT_FIELDS)
    index = logger.record_manual_event(
        elapsed_s=1.25,
        event_at_utc="2026-08-17T12:00:00+00:00",
        frame=frame,
        note="",
        capture_metadata={
            "captured_at_utc": "2026-08-17T12:00:00+00:00",
            "capture_sequence": 12,
            "capture_geometry_id": "geom-a",
        },
    )
    assert index == 1
    assert logger.last_point_event_id
    state = logger.point_event_store.get(logger.last_point_event_id)
    assert state["source"]["kind"] == "manual"
    assert state["status"] == STATUS_DRAFT
    assert state["review"]["comment"] == ""
    assert state["review"]["anchor"]["capture_sequence"] == 12

    with open(logger.session_dir / "manual_events.csv", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == original_fields
        csv_rows = list(reader)
        assert len(csv_rows) == 1
        assert csv_rows[0]["timestamp"] == "2026-08-17T12:00:00+00:00"
    assert state["source"]["source_row_sha256"] == source_row_sha256(csv_rows[0])
    logger.end_session()
    assert (logger.session_dir / JOURNAL_NAME).is_file()
    assert (logger.session_dir / SUMMARY_NAME).is_file()


def test_growth_logger_auto_event_creates_distinct_draft(tmp_path):
    logger = GrowthLogger(base_dir=tmp_path)
    logger.start_session("auto-point")
    assert logger.log_auto_capture_event(
        event_idx=1, score=0.5, elapsed_s=3.0,
        capture_metadata={"capture_sequence": 99},
    )
    state = logger.point_event_store.get(logger.last_point_event_id)
    assert state["source"]["kind"] == "auto_capture"
    assert state["source"]["capture_sequence"] == "99"
    assert state["review"]["anchor"] is None
    identity_hash = state["source"]["source_row_sha256"]
    assert logger.update_auto_capture_state(1, "discarded")
    with open(logger.session_dir / "auto_capture_events.csv", newline="") as stream:
        final_row = next(csv.DictReader(stream))
    assert final_row["event_state"] == "discarded"
    assert source_row_sha256(
        final_row, source_kind="auto_capture",
    ) == identity_hash
    logger.end_session()


def test_live_equalizer_rejects_unjournaled_context_before_event_revision(tmp_path):
    session = tmp_path / "growth_explicit"
    session.mkdir()
    (session / "frames").mkdir()
    initial = session / "frames" / "manual.bmp"
    anchor = _frame(initial, b"manual")
    store = PointEventStore(session)
    event = _create(store, anchor)

    logger = GrowthLogger(base_dir=tmp_path)
    logger._session_dir = session
    logger._rheed_point_event_store = store
    values = {
        "1x1": 0.4,
        "Tw(2x1)": 0.3,
        "c(6x2)": 0.2,
        "RT13": 0.1,
    }
    payload = build_equalizer_payload(
        raw_weights=None,
        final_weights=values,
        fit_mode="manual",
        normalization_applied=False,
        residual_rms=2.5,
        valid_coverage=0.8,
    )
    calibration = SimpleNamespace(
        calibration_id="cal-1",
        basis_bundle_id="bundle-1",
        active_classes=ACTIVE_EQUALIZER_CLASSES,
        matrix=np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]),
        parity="normal",
        endpoint_order="forward",
        rotation_deg=0.0,
        scale=1.0,
        rms_residual_px=0.0,
        max_residual_px=0.0,
        valid_coverage=1.0,
        orientation_evidence_kind="streak/tail",
    )
    snapshot = SimpleNamespace(
        capture_sequence=8,
        captured_at_utc="2026-08-17T12:00:01+00:00",
        view_segment_id=3,
        capture_geometry_id="geom-a",
    )

    before = store.get(event["event_id"])
    with pytest.raises(PointEventError, match="context"):
        logger.attach_live_equalizer_to_point_event(
            event["event_id"],
            live_label_index=1,
            actor="reviewer-a",
            calibration=calibration,
            snapshot=snapshot,
            equalizer_payload=payload,
        )
    assert store.get(event["event_id"]) == before
