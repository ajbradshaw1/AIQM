"""Append-only semantic RHEED point-event review records.

V2 separates immutable acquisition evidence, a movable event-boundary frame,
and the representative frame for the stable interval after an event.  V1
journals remain integrity-checked and read-only; ambiguous v1 transitions are
never silently converted into v2 semantic labels.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from gui.rheed_event_state import EventStateError, replay_event_states


SCHEMA_ID = "rheed-point-events-v2"
LEGACY_SCHEMA_ID = "rheed-point-events-v1"
JOURNAL_NAME = "rheed_event_revisions.jsonl"
SUMMARY_NAME = "rheed_point_events.json"
TRANSACTION_NAME = ".rheed_event_revision.pending.json"
STATUS_DRAFT = "Draft"
STATUS_COMPLETE = "Complete"
LABELABLE_SOURCES = frozenset({
    "manual", "auto_capture", "posthoc", "initial_assumption",
})
SOURCE_EVENTS = frozenset({"manual", "auto_capture", "initial_assumption"})
CANDIDATE_SOURCES = frozenset({"auto_capture"})
CANDIDATE_DECISIONS = frozenset({"pending", "confirmed", "rejected"})
RECONSTRUCTION_VALUES = frozenset({
    "one_by_one", "twinned_two_by_one", "c_six_by_two", "rt13", "htr",
})
PATTERN_CLARITY_VALUES = frozenset({"good", "bad"})
SURFACE_QUALITY_VALUES = frozenset({"good", "bad"})

_LEGACY_NAMESPACE = uuid.UUID("07756558-3619-5d20-9fe9-1ff011f45a5c")
_AUTO_CAPTURE_MUTABLE_FIELDS = frozenset({"event_state", "state_changed_at"})


class PointEventError(ValueError):
    """Base exception for invalid point-event operations."""


class PointEventIntegrityError(PointEventError):
    """The append-only journal or an event failed integrity validation."""


class PointEventCompletionError(PointEventError):
    """The event does not satisfy the explicit Complete gate."""

    def __init__(self, errors: list[str]):
        self.errors = tuple(errors)
        super().__init__("Event cannot be completed: " + "; ".join(errors))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_value(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def source_identity_row(
    row: Mapping[str, object], *, source_kind: str = "manual",
) -> dict[str, str]:
    excluded = (
        _AUTO_CAPTURE_MUTABLE_FIELDS
        if str(source_kind) == "auto_capture" else frozenset()
    )
    return {
        str(key): "" if value is None else str(value)
        for key, value in row.items() if str(key) not in excluded
    }


def source_row_sha256(
    row: Mapping[str, object], *, source_kind: str = "manual",
) -> str:
    return sha256_value(source_identity_row(row, source_kind=source_kind))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deterministic_legacy_event_id(
    *, session_identity: str, source_file: str, source_index: str | int,
    original_at_utc: str, capture_sequence: str | int | None,
    source_row_hash: str = "",
) -> str:
    identity = {
        "session_identity": str(session_identity),
        "source_file": str(source_file).replace("\\", "/"),
        "source_index": str(source_index),
        "original_at_utc": str(original_at_utc),
        "capture_sequence": "" if capture_sequence is None else str(capture_sequence),
        "source_row_sha256": str(source_row_hash or "").lower(),
    }
    return str(uuid.uuid5(_LEGACY_NAMESPACE, _json_bytes(identity).decode("utf-8")))


def new_event_id() -> str:
    return str(uuid.uuid4())


def new_label_id() -> str:
    return str(uuid.uuid4())


def make_review_anchor(
    *, frame_path: str | Path, capture_sequence: str | int | None,
    image_sha256: Optional[str] = None,
    image_sha256_algorithm: str = "raw-file-bytes-v1",
    captured_at_utc: str = "", elapsed_s: float | str | None = None,
    view_segment_id: str | int | None = None, capture_geometry_id: str = "",
) -> dict[str, Any]:
    """Bind a marker to one exact, losslessly saved frame."""

    algorithm = str(image_sha256_algorithm or "raw-file-bytes-v1").lower()
    if algorithm not in {"sha256", "raw-file-bytes-v1"}:
        raise PointEventError("Review anchors require raw-file-bytes SHA-256")
    path = Path(frame_path)
    if not path.is_file():
        raise PointEventError(f"Review anchor is not a saved frame: {path}")
    digest = (image_sha256 or sha256_file(path)).lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise PointEventError("Review anchor requires a valid SHA-256")
    if sha256_file(path) != digest:
        raise PointEventIntegrityError("Review anchor frame SHA-256 does not match")
    try:
        elapsed = None if elapsed_s in (None, "") else float(elapsed_s)
    except (TypeError, ValueError) as exc:
        raise PointEventError("Review anchor elapsed_s must be numeric") from exc
    if elapsed is not None and (not math.isfinite(elapsed) or elapsed < 0):
        raise PointEventError("Review anchor elapsed_s must be finite and non-negative")
    return {
        "frame_path": str(path), "image_sha256": digest,
        "image_sha256_algorithm": "raw-file-bytes-v1",
        "capture_sequence": capture_sequence,
        "captured_at_utc": str(captured_at_utc or ""), "elapsed_s": elapsed,
        "view_segment_id": view_segment_id,
        "capture_geometry_id": str(capture_geometry_id or ""),
    }


def _validate_anchor_shape(anchor: object, *, required: bool = False) -> None:
    if anchor is None:
        if required:
            raise PointEventIntegrityError("A saved frame anchor is required")
        return
    if not isinstance(anchor, Mapping):
        raise PointEventIntegrityError("Frame anchor must be an object")
    digest = str(anchor.get("image_sha256") or "").lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise PointEventIntegrityError("Frame anchor SHA-256 is invalid")
    if str(anchor.get("image_sha256_algorithm") or "").lower() != "raw-file-bytes-v1":
        raise PointEventIntegrityError("Frame anchor hash algorithm is invalid")
    try:
        elapsed = anchor.get("elapsed_s")
        if elapsed not in (None, "") and (
            not math.isfinite(float(elapsed)) or float(elapsed) < 0
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise PointEventIntegrityError("Frame anchor elapsed_s is invalid") from None


def validate_semantic_label(label: Mapping[str, Any]) -> dict[str, str]:
    """Validate and canonicalize one stable-ID human label."""

    if not isinstance(label, Mapping):
        raise PointEventError("Semantic label must be an object")
    result = {str(key): str(value) for key, value in label.items()}
    if set(result) != {"label_id", "kind", "change", "value"}:
        raise PointEventError("Semantic label fields must be label_id, kind, change, value")
    try:
        uuid.UUID(result["label_id"])
    except (ValueError, AttributeError) as exc:
        raise PointEventError("Semantic label_id must be a UUID") from exc
    if result["kind"] == "reconstruction":
        if result["change"] not in {"appeared", "disappeared"}:
            raise PointEventError("Reconstruction change must be appeared or disappeared")
        if result["value"] not in RECONSTRUCTION_VALUES:
            raise PointEventError("Reconstruction value is invalid")
    elif result["kind"] == "pattern_clarity":
        if result["change"] != "became":
            raise PointEventError("Pattern clarity change must be became")
        if result["value"] not in PATTERN_CLARITY_VALUES:
            raise PointEventError("Pattern clarity value must be good or bad")
    elif result["kind"] == "surface_quality":
        if result["change"] != "became":
            raise PointEventError("Surface quality change must be became")
        if result["value"] not in SURFACE_QUALITY_VALUES:
            raise PointEventError("Surface quality value must be good or bad")
    else:
        raise PointEventError("Semantic label kind is invalid")
    return result


def make_semantic_label(
    *, kind: str, change: str, value: str, label_id: str | None = None,
) -> dict[str, str]:
    return validate_semantic_label({
        "label_id": label_id or new_label_id(), "kind": kind,
        "change": change, "value": value,
    })


def _validate_labels(labels: object) -> list[dict[str, str]]:
    if not isinstance(labels, list):
        raise PointEventIntegrityError("review.labels must be an array")
    checked: list[dict[str, str]] = []
    identifiers: set[str] = set()
    meanings: set[tuple[str, str, str]] = set()
    reconstruction_values: set[str] = set()
    clarity_seen = False
    quality_seen = False
    for raw in labels:
        try:
            label = validate_semantic_label(raw)
        except PointEventError as exc:
            raise PointEventIntegrityError(str(exc)) from exc
        meaning = (label["kind"], label["change"], label["value"])
        if label["label_id"] in identifiers:
            raise PointEventIntegrityError("Semantic label_id is duplicated")
        if meaning in meanings:
            raise PointEventIntegrityError("Duplicate semantic label at one event")
        if label["kind"] == "pattern_clarity":
            if clarity_seen:
                raise PointEventIntegrityError(
                    "An event can contain only one pattern-clarity change"
                )
            clarity_seen = True
        elif label["kind"] == "surface_quality":
            if quality_seen:
                raise PointEventIntegrityError(
                    "An event can contain only one surface-quality change"
                )
            quality_seen = True
        elif label["value"] in reconstruction_values:
            raise PointEventIntegrityError(
                "A reconstruction cannot both appear and disappear at one event"
            )
        else:
            reconstruction_values.add(label["value"])
        identifiers.add(label["label_id"])
        meanings.add(meaning)
        checked.append(label)
    return checked


def _empty_review(
    anchor: Optional[Mapping[str, Any]], comment: str, *, source_kind: str,
    labels: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "anchor": copy.deepcopy(dict(anchor)) if anchor is not None else None,
        "representative_anchor": None,
        "labels": [validate_semantic_label(item) for item in (labels or [])],
        "candidate_decision": "pending" if source_kind in CANDIDATE_SOURCES else "confirmed",
        "comment": str(comment or ""),
        "disposition": "active", "disposition_reason": "",
    }


def _legacy_completion_errors(event: Mapping[str, Any]) -> list[str]:
    review = event.get("review")
    if not isinstance(review, Mapping):
        return ["review content is missing"]
    errors = []
    if not str(review.get("comment") or "").strip():
        errors.append("comment is required")
    if not str(review.get("reviewer") or "").strip():
        errors.append("reviewer is required")
    if review.get("disposition") != "active":
        errors.append("dismissed or deleted events cannot be completed")
    if not isinstance(review.get("equalizer"), Mapping):
        errors.append("a valid Equalizer measurement is required")
    return errors


def completion_errors(event: Mapping[str, Any]) -> list[str]:
    """Return unmet per-event v2 Complete requirements."""

    if event.get("schema") == LEGACY_SCHEMA_ID:
        return _legacy_completion_errors(event)
    review = event.get("review")
    if not isinstance(review, Mapping):
        return ["review content is missing"]
    errors: list[str] = []
    if review.get("disposition") != "active":
        errors.append("dismissed or deleted events cannot be completed")
    decision = str(review.get("candidate_decision") or "")
    source_kind = str(event.get("source", {}).get("kind") or "")
    if decision not in CANDIDATE_DECISIONS:
        errors.append("candidate decision is invalid")
    elif source_kind in CANDIDATE_SOURCES and decision == "pending":
        errors.append("candidate must be confirmed or rejected")
    if decision == "rejected":
        if source_kind not in CANDIDATE_SOURCES:
            errors.append("only automatic candidates can be rejected")
    else:
        if review.get("anchor") is None:
            errors.append("a saved-frame review point is required")
        try:
            labels = _validate_labels(review.get("labels"))
        except PointEventIntegrityError as exc:
            errors.append(str(exc))
        else:
            if source_kind == "initial_assumption":
                if len(labels) != 1 or labels[0]["kind"] != "surface_quality":
                    errors.append("initial surface quality must be selected as good or bad")
            elif not labels:
                errors.append("at least one semantic label is required")
    return errors


def _changed_review_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> set[str]:
    return {key for key in set(before) | set(after) if before.get(key) != after.get(key)}


def _validate_legacy_transition(
    before: Optional[Mapping[str, Any]], after: Mapping[str, Any], *,
    action: str, actor: str,
) -> None:
    """Integrity-check v1 state while keeping it read-only."""

    if action not in {
        "create", "edit", "move_anchor", "set_equalizer", "complete",
        "reopen", "dismiss", "delete",
    } or not str(actor or "").strip() or after.get("schema") != LEGACY_SCHEMA_ID:
        raise PointEventIntegrityError("Legacy revision identity is invalid")
    source, review = after.get("source"), after.get("review")
    if not isinstance(source, Mapping) or source.get("kind") not in {
        "manual", "auto_capture", "posthoc",
    } or not isinstance(review, Mapping):
        raise PointEventIntegrityError("Legacy event structure is invalid")
    if after.get("status") not in {STATUS_DRAFT, STATUS_COMPLETE}:
        raise PointEventIntegrityError("Legacy event status is invalid")
    if before is None:
        if action != "create" or after.get("status") != STATUS_DRAFT:
            raise PointEventIntegrityError("Legacy create transition is invalid")
        return
    if before.get("event_id") != after.get("event_id") or before.get("source") != source:
        raise PointEventIntegrityError("Immutable legacy source evidence was modified")
    before_review = before.get("review")
    if not isinstance(before_review, Mapping):
        raise PointEventIntegrityError("Legacy previous review is invalid")
    protected = set(before) | set(after)
    protected -= {
        "review", "status", "revision_id", "revision_number", "updated_at_utc",
    }
    if any(before.get(key) != after.get(key) for key in protected):
        raise PointEventIntegrityError("Protected legacy event metadata was modified")
    try:
        if int(after.get("revision_number", 0)) != int(
            before.get("revision_number", 0)
        ) + 1:
            raise ValueError
    except (TypeError, ValueError):
        raise PointEventIntegrityError(
            "Legacy revision number is not consecutive"
        ) from None
    changed = _changed_review_fields(before_review, review)
    if action == "edit":
        valid = changed <= {
            "comment", "reviewer", "confidence", "human_reconstruction",
            "change_from", "change_to",
        } and after.get("status") == STATUS_DRAFT
    elif action == "move_anchor":
        valid = (
            changed <= {"anchor", "equalizer"}
            and review.get("equalizer") is None
            and after.get("status") == STATUS_DRAFT
        )
    elif action == "set_equalizer":
        valid = (
            changed <= {"equalizer"} and review.get("equalizer") is not None
            and after.get("status") == STATUS_DRAFT
        )
    elif action == "complete":
        valid = (
            not changed and before.get("status") == STATUS_DRAFT
            and after.get("status") == STATUS_COMPLETE
            and not _legacy_completion_errors(after)
        )
    elif action == "reopen":
        valid = (
            not changed and before.get("status") == STATUS_COMPLETE
            and after.get("status") == STATUS_DRAFT
        )
    elif action == "dismiss":
        valid = (
            source.get("kind") in {"manual", "auto_capture"}
            and changed <= {"disposition", "disposition_reason"}
            and review.get("disposition") == "dismissed"
            and bool(str(review.get("disposition_reason") or "").strip())
            and after.get("status") == STATUS_DRAFT
        )
    elif action == "delete":
        valid = (
            source.get("kind") == "posthoc"
            and changed <= {"disposition", "disposition_reason"}
            and review.get("disposition") == "deleted"
            and bool(str(review.get("disposition_reason") or "").strip())
            and after.get("status") == STATUS_DRAFT
        )
    else:
        valid = False
    if not valid:
        raise PointEventIntegrityError(f"Legacy {action} transition is invalid")


_V2_ACTIONS = frozenset({
    "create", "edit", "add_label", "edit_label", "remove_label",
    "set_candidate_decision", "move_anchor", "move_representative_anchor",
    "complete", "reopen", "dismiss", "delete",
})


def _validate_v2_event(event: Mapping[str, Any]) -> None:
    if event.get("schema") != SCHEMA_ID:
        raise PointEventIntegrityError("Point-event schema is invalid")
    try:
        uuid.UUID(str(event.get("event_id") or ""))
    except ValueError as exc:
        raise PointEventIntegrityError("Point-event event_id is invalid") from exc
    source, review = event.get("source"), event.get("review")
    if not isinstance(source, Mapping) or source.get("kind") not in LABELABLE_SOURCES:
        raise PointEventIntegrityError("Point-event source evidence is invalid")
    if not isinstance(review, Mapping):
        raise PointEventIntegrityError("Point-event review state is invalid")
    expected = {
        "anchor", "representative_anchor", "labels", "candidate_decision",
        "comment", "disposition", "disposition_reason",
    }
    early_v2 = expected | {"reviewer", "confidence"}
    if set(review) != expected and set(review) != early_v2:
        raise PointEventIntegrityError("Point-event v2 review fields are invalid")
    _validate_anchor_shape(review.get("anchor"))
    _validate_anchor_shape(review.get("representative_anchor"))
    _validate_labels(review.get("labels"))
    if source.get("kind") == "initial_assumption" and (
        len(review["labels"]) > 1
        or any(label["kind"] != "surface_quality" for label in review["labels"])
    ):
        raise PointEventIntegrityError(
            "Initial state accepts only one surface-quality selection"
        )
    decision = review.get("candidate_decision")
    if decision not in CANDIDATE_DECISIONS:
        raise PointEventIntegrityError("Point-event candidate decision is invalid")
    if source.get("kind") not in CANDIDATE_SOURCES and decision == "rejected":
        raise PointEventIntegrityError("A human-created event cannot be rejected")
    if decision == "rejected" and (
        review.get("labels") or review.get("representative_anchor") is not None
    ):
        raise PointEventIntegrityError(
            "A rejected candidate cannot retain semantic labels or a representative anchor"
        )
    if event.get("status") not in {STATUS_DRAFT, STATUS_COMPLETE}:
        raise PointEventIntegrityError("Point-event status is invalid")
    disposition = review.get("disposition")
    if disposition not in {"active", "dismissed", "deleted"}:
        raise PointEventIntegrityError("Point-event disposition is invalid")
    if disposition == "deleted" and source.get("kind") != "posthoc":
        raise PointEventIntegrityError("Acquisition source events cannot be deleted")
    if disposition != "active" and not str(review.get("disposition_reason") or "").strip():
        raise PointEventIntegrityError("Inactive point event requires a reason")
    if event.get("status") == STATUS_COMPLETE and completion_errors(event):
        raise PointEventIntegrityError(
            "Invalid Complete point-event state: " + "; ".join(completion_errors(event))
        )


def _validate_revision_transition(
    before: Optional[Mapping[str, Any]], after: Mapping[str, Any], *,
    action: str, actor: str,
) -> None:
    if after.get("schema") == LEGACY_SCHEMA_ID:
        _validate_legacy_transition(before, after, action=action, actor=actor)
        return
    if action not in _V2_ACTIONS or not str(actor or "").strip():
        raise PointEventIntegrityError("Point-event revision action or actor is invalid")
    _validate_v2_event(after)
    review = after["review"]
    if before is None:
        if action != "create" or after.get("status") != STATUS_DRAFT or int(after.get("revision_number", 0)) != 1:
            raise PointEventIntegrityError("Point-event create transition is invalid")
        return
    _validate_v2_event(before)
    if before.get("event_id") != after.get("event_id") or before.get("source") != after.get("source"):
        raise PointEventIntegrityError("Immutable source evidence was modified")
    protected = set(before) | set(after)
    protected -= {"review", "status", "revision_id", "revision_number", "updated_at_utc"}
    if any(before.get(key) != after.get(key) for key in protected):
        raise PointEventIntegrityError("Protected point-event metadata was modified")
    if int(after.get("revision_number", 0)) != int(before.get("revision_number", 0)) + 1:
        raise PointEventIntegrityError("Point-event revision number is not consecutive")
    changed = _changed_review_fields(before["review"], review)
    before_status, after_status = before.get("status"), after.get("status")
    fields = {
        "add_label": "labels", "edit_label": "labels", "remove_label": "labels",
        "set_candidate_decision": "candidate_decision", "move_anchor": "anchor",
        "move_representative_anchor": "representative_anchor",
    }
    if action == "edit":
        valid = changed <= {"comment"} and after_status == STATUS_DRAFT
    elif action in {"add_label", "edit_label", "remove_label"}:
        valid = (
            changed == {"labels"}
            and after_status == STATUS_DRAFT
        )
        old = {
            item["label_id"]: item for item in before["review"]["labels"]
        }
        new = {item["label_id"]: item for item in review["labels"]}
        if action == "add_label":
            valid = (
                valid and len(new) == len(old) + 1
                and all(new.get(key) == value for key, value in old.items())
            )
        elif action == "remove_label":
            valid = (
                valid and len(new) == len(old) - 1
                and all(old.get(key) == value for key, value in new.items())
            )
        else:
            changed_ids = {
                key for key in set(old) | set(new)
                if old.get(key) != new.get(key)
            }
            valid = (
                valid and set(old) == set(new) and len(changed_ids) == 1
            )
    elif action == "set_candidate_decision":
        valid = (
            "candidate_decision" in changed
            and changed <= {"candidate_decision", "labels", "representative_anchor"}
            and after_status == STATUS_DRAFT
        )
        if review["candidate_decision"] == "rejected":
            valid = valid and review["labels"] == [] and review["representative_anchor"] is None
        else:
            valid = valid and changed == {"candidate_decision"}
    elif action == "move_anchor":
        valid = (
            "anchor" in changed
            and changed <= {"anchor", "representative_anchor"}
            and review.get("representative_anchor") is None
            and after_status == STATUS_DRAFT
        )
    elif action in fields:
        valid = changed == {fields[action]} and after_status == STATUS_DRAFT
    elif action == "complete":
        valid = not changed and before_status == STATUS_DRAFT and after_status == STATUS_COMPLETE and not completion_errors(after)
    elif action == "reopen":
        valid = not changed and before_status == STATUS_COMPLETE and after_status == STATUS_DRAFT
    elif action == "dismiss":
        valid = before["source"]["kind"] in {"manual", "auto_capture"} and changed <= {"disposition", "disposition_reason"} and review["disposition"] == "dismissed" and bool(str(review["disposition_reason"]).strip()) and after_status == STATUS_DRAFT
    elif action == "delete":
        valid = before["source"]["kind"] == "posthoc" and changed <= {"disposition", "disposition_reason"} and review["disposition"] == "deleted" and bool(str(review["disposition_reason"]).strip()) and after_status == STATUS_DRAFT
    else:
        valid = False
    if not valid:
        raise PointEventIntegrityError(f"Point-event {action} transition is semantically invalid")


class PointEventStore:
    """Crash-recoverable append-only journal for one live session."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.directory / JOURNAL_NAME
        self.summary_path = self.directory / SUMMARY_NAME
        self.transaction_path = self.directory / TRANSACTION_NAME
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._revision_ids: set[str] = set()
        self._journal_schema = SCHEMA_ID
        self._last_record_sha256 = ""
        with self._lock:
            self._recover_pending()
            self._replay()
            self._write_summary()

    @property
    def read_only_legacy(self) -> bool:
        return self._journal_schema == LEGACY_SCHEMA_ID

    @property
    def states(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._states)

    def get(self, event_id: str) -> Optional[dict[str, Any]]:
        state = self._states.get(str(event_id))
        return copy.deepcopy(state) if state is not None else None

    @staticmethod
    def _record_hash(record: Mapping[str, Any]) -> str:
        return sha256_value({key: value for key, value in record.items() if key != "record_sha256"})

    @classmethod
    def _validate_record_hash(cls, record: Mapping[str, Any]) -> None:
        if str(record.get("record_sha256") or "").lower() != cls._record_hash(record):
            raise PointEventIntegrityError("Point-event revision hash mismatch")

    def _read_records(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        records = []
        with open(self.journal_path, "r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PointEventIntegrityError(f"Invalid point-event JSONL at line {line_number}") from exc
                if not isinstance(record, dict):
                    raise PointEventIntegrityError(f"Point-event JSONL line {line_number} is not an object")
                self._validate_record_hash(record)
                records.append(record)
        return records

    def _replay(self) -> None:
        states: dict[str, dict[str, Any]] = {}
        revision_ids: set[str] = set()
        schemas: set[str] = set()
        previous_hash = ""
        for record in self._read_records():
            schema = str(record.get("schema") or "")
            schemas.add(schema)
            if schema not in {SCHEMA_ID, LEGACY_SCHEMA_ID} or len(schemas) > 1:
                raise PointEventIntegrityError("Unsupported or mixed point-event journal schema")
            if schema == SCHEMA_ID and str(record.get("previous_record_sha256") or "") != previous_hash:
                raise PointEventIntegrityError("Point-event global revision hash chain is broken")
            revision_id, event_id = str(record.get("revision_id") or ""), str(record.get("event_id") or "")
            if not revision_id or revision_id in revision_ids or not event_id:
                raise PointEventIntegrityError("Duplicate or missing revision identity")
            before, after = record.get("before"), record.get("after")
            if not isinstance(after, dict) or after.get("event_id") != event_id:
                raise PointEventIntegrityError("Point-event after-state identity is invalid")
            previous = states.get(event_id)
            if previous is None:
                if record.get("action") != "create" or before is not None:
                    raise PointEventIntegrityError("Event journal starts without create")
            elif record.get("base_revision_id") != previous.get("revision_id") or before != previous:
                raise PointEventIntegrityError("Point-event revision chain is broken")
            if after.get("revision_id") != revision_id:
                raise PointEventIntegrityError("Point-event state revision ID is invalid")
            _validate_revision_transition(previous, after, action=str(record.get("action") or ""), actor=str(record.get("actor") or ""))
            states[event_id] = copy.deepcopy(after)
            revision_ids.add(revision_id)
            previous_hash = str(record["record_sha256"])
        self._states, self._revision_ids = states, revision_ids
        self._journal_schema = next(iter(schemas), SCHEMA_ID)
        self._last_record_sha256 = previous_hash
        if self._journal_schema == SCHEMA_ID:
            for event_id, event in states.items():
                if event.get("status") == STATUS_COMPLETE:
                    errors = self._interval_errors(event_id, state=states)
                    if errors:
                        raise PointEventIntegrityError(
                            "Complete event interval is invalid: "
                            + "; ".join(errors)
                        )

    def _atomic_write(self, path: Path, data: bytes) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _append_record(self, record: Mapping[str, Any]) -> None:
        with open(self.journal_path, "ab") as stream:
            stream.write(_json_bytes(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _recover_pending(self) -> None:
        if not self.transaction_path.exists():
            return
        try:
            record = json.loads(self.transaction_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PointEventIntegrityError("Point-event pending transaction is invalid") from exc
        if not isinstance(record, dict):
            raise PointEventIntegrityError("Point-event pending transaction is not an object")
        self._validate_record_hash(record)
        matches = [item for item in self._read_records() if item.get("revision_id") == record.get("revision_id")]
        if len(matches) > 1:
            raise PointEventIntegrityError("Pending point-event revision identity is duplicated in journal")
        if matches and _json_bytes(matches[0]) != _json_bytes(record):
            raise PointEventIntegrityError("Pending point-event revision conflicts with journal")
        if not matches:
            self._append_record(record)
        self.transaction_path.unlink()

    def _write_summary(self) -> None:
        self._atomic_write(self.summary_path, _json_bytes({
            "schema": self._journal_schema, "generated_at_utc": _utc_now(),
            "journal": JOURNAL_NAME, "read_only_legacy": self.read_only_legacy,
            "events": [self._states[key] for key in sorted(self._states)],
        }) + b"\n")

    def materialize_summary(self) -> Path:
        with self._lock:
            self._write_summary()
        return self.summary_path

    def _assert_writable(self) -> None:
        if self.read_only_legacy:
            raise PointEventError("rheed-point-events-v1 journals are read-only; start a v2 annotation sidecar")

    def _commit(
        self, *, event_id: str, actor: str, action: str,
        before: Optional[Mapping[str, Any]], after: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._assert_writable()
        actor = str(actor or "").strip()
        if not actor:
            raise PointEventError("Every revision requires an actor")
        revision_id = str(uuid.uuid4())
        next_state = copy.deepcopy(dict(after))
        next_state.update({
            "revision_id": revision_id, "updated_at_utc": _utc_now(),
            "revision_number": int(before.get("revision_number", 0)) + 1 if before else 1,
        })
        record: dict[str, Any] = {
            "schema": SCHEMA_ID, "revision_id": revision_id,
            "event_id": event_id, "actor": actor, "recorded_at_utc": _utc_now(),
            "action": action,
            "base_revision_id": before.get("revision_id") if before else None,
            "before": copy.deepcopy(dict(before)) if before else None,
            "after": next_state, "previous_record_sha256": self._last_record_sha256,
        }
        _validate_revision_transition(before, next_state, action=action, actor=actor)
        candidate_states = {**self._states, event_id: next_state}
        # Live anchors predate the offline one-based frame-index field.  For
        # consistency replay, assign deterministic ordinals from the exact
        # saved-frame elapsed/capture identity without rewriting evidence.
        state_changers = []
        for item in candidate_states.values():
            review = item.get("review", {})
            anchor = review.get("anchor")
            if (
                item.get("source", {}).get("kind") == "initial_assumption"
                or review.get("candidate_decision") != "confirmed"
                or review.get("disposition") != "active"
                or not review.get("labels")
                or not isinstance(anchor, Mapping)
            ):
                continue
            coordinate = self._anchor_coordinate(item, "anchor")
            if coordinate is None:
                raise PointEventError(
                    "A confirmed semantic event requires a timed saved-frame anchor"
                )
            state_changers.append((
                coordinate[0], coordinate[1],
                str(item.get("event_id", "")),
                item,
            ))
        coordinates = sorted({
            (elapsed, sequence)
            for elapsed, sequence, _event_id, _item in state_changers
        })
        frame_ordinals = {
            coordinate: index + 1 for index, coordinate in enumerate(coordinates)
        }
        normalized_states = []
        for elapsed, sequence, _event_id, item in state_changers:
            normalized = copy.deepcopy(item)
            normalized["frame_index"] = frame_ordinals[(elapsed, sequence)]
            normalized_states.append(normalized)
        try:
            replay_event_states(
                normalized_states,
                frame_count=max(1, len(coordinates)),
                initial_state={
                    "reconstructions": ["one_by_one"],
                    "clarity": "bad",
                    "quality": next((
                        label["value"]
                        for candidate in candidate_states.values()
                        if candidate.get("source", {}).get("kind") == "initial_assumption"
                        for label in candidate.get("review", {}).get("labels", [])
                        if label.get("kind") == "surface_quality"
                    ), "unknown"),
                },
                session_identity=str(
                    next_state.get("source", {}).get("session_identity", "")
                ),
            )
        except EventStateError as exc:
            raise PointEventError(
                "Revision creates an inconsistent state transition: " + str(exc)
            ) from exc
        for candidate_id, candidate in candidate_states.items():
            if not self._owns_state_interval(candidate):
                continue
            complete = candidate.get("status") == STATUS_COMPLETE
            errors = self._interval_errors(
                candidate_id, state=candidate_states,
                require_anchor=complete,
                validate_bounds=complete,
            )
            if errors:
                prefix = (
                    "Revision would invalidate a Complete event interval: "
                    if complete else "Revision creates an invalid interval Anchor: "
                )
                raise PointEventError(prefix + "; ".join(errors))
        record["record_sha256"] = self._record_hash(record)
        self._atomic_write(self.transaction_path, _json_bytes(record) + b"\n")
        self._append_record(record)
        self._states[event_id] = copy.deepcopy(next_state)
        self._revision_ids.add(revision_id)
        self._last_record_sha256 = record["record_sha256"]
        self._write_summary()
        self.transaction_path.unlink()
        return copy.deepcopy(next_state)

    def create_event(
        self, *, source_kind: str, actor: str, session_identity: str,
        source_file: str, source_index: str | int, source_row: Mapping[str, Any],
        original_at_utc: str, original_elapsed_s: float | str | None,
        original_note: str = "", capture_sequence: str | int | None = None,
        original_frame_path: str = "", original_image_sha256: str = "",
        review_anchor: Optional[Mapping[str, Any]] = None,
        event_id: Optional[str] = None, current_software: bool = True,
        initial_labels: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        kind = str(source_kind)
        if kind not in LABELABLE_SOURCES:
            raise PointEventError(f"Unsupported labelable source: {kind}")
        verified_anchor = make_review_anchor(**dict(review_anchor)) if review_anchor is not None else None
        try:
            elapsed = None if original_elapsed_s in (None, "") else float(original_elapsed_s)
        except (TypeError, ValueError) as exc:
            raise PointEventError("Original event elapsed_s must be numeric") from exc
        if elapsed is not None and (not math.isfinite(elapsed) or elapsed < 0):
            raise PointEventError("Original event elapsed_s must be finite and non-negative")
        row = {str(key): value for key, value in source_row.items()}
        if event_id is None:
            event_id = new_event_id() if current_software else deterministic_legacy_event_id(
                session_identity=session_identity, source_file=source_file,
                source_index=source_index, original_at_utc=original_at_utc,
                capture_sequence=capture_sequence,
                source_row_hash=source_row_sha256(row, source_kind=kind),
            )
        event_id = str(event_id)
        with self._lock:
            self._assert_writable()
            if event_id in self._states:
                raise PointEventError(f"Point event already exists: {event_id}")
            if kind == "initial_assumption" and any(
                item.get("source", {}).get("kind") == kind for item in self._states.values()
            ):
                raise PointEventError("The session already has an initial 1x1 assumption")
            source = {
                "kind": kind, "session_identity": str(session_identity),
                "source_file": str(source_file).replace("\\", "/"),
                "source_index": str(source_index),
                "source_row_sha256": source_row_sha256(row, source_kind=kind),
                "source_row_hash_algorithm": "sha256-canonical-json-auto-immutable-v1" if kind == "auto_capture" else "sha256-canonical-json-row-v1",
                "original_at_utc": str(original_at_utc or ""),
                "original_elapsed_s": elapsed,
                "original_frame_path": str(original_frame_path or ""),
                "original_image_sha256": str(original_image_sha256 or "").lower(),
                "original_image_sha256_algorithm": "raw-file-bytes-v1" if original_image_sha256 else "",
                "capture_sequence": capture_sequence,
                "original_note": str(original_note or ""),
            }
            state = {
                "schema": SCHEMA_ID, "event_id": event_id, "source": source,
                "review": _empty_review(verified_anchor, original_note, source_kind=kind, labels=initial_labels),
                "status": STATUS_DRAFT, "created_at_utc": _utc_now(),
                "revision_id": "", "revision_number": 0, "updated_at_utc": "",
            }
            return self._commit(event_id=event_id, actor=actor, action="create", before=None, after=state)

    def ensure_initial_assumption(
        self, *, actor: str, session_identity: str,
        first_frame_anchor: Mapping[str, Any], original_at_utc: str = "",
    ) -> dict[str, Any]:
        """Create exactly one deterministic review item for the default 1x1 state."""

        with self._lock:
            existing = [item for item in self._states.values() if item.get("source", {}).get("kind") == "initial_assumption"]
            if len(existing) > 1:
                raise PointEventIntegrityError("Multiple initial assumptions exist")
            if existing:
                return copy.deepcopy(existing[0])
        anchor = make_review_anchor(**dict(first_frame_anchor))
        row = {
            "kind": "initial_assumption", "default": "one_by_one",
            "capture_sequence": anchor.get("capture_sequence"),
            "image_sha256": anchor["image_sha256"],
        }
        timestamp = original_at_utc or str(anchor.get("captured_at_utc") or "")
        event_id = deterministic_legacy_event_id(
            session_identity=session_identity, source_file="initial_assumption",
            source_index=0, original_at_utc=timestamp,
            capture_sequence=anchor.get("capture_sequence"),
            source_row_hash=source_row_sha256(row, source_kind="initial_assumption"),
        )
        return self.create_event(
            source_kind="initial_assumption", actor=actor,
            session_identity=session_identity, source_file="initial_assumption",
            source_index=0, source_row=row, original_at_utc=timestamp,
            original_elapsed_s=anchor.get("elapsed_s"),
            original_note=(
                "Initial state is 1x1 with Bad clarity; grower selects "
                "initial surface quality."
            ),
            capture_sequence=anchor.get("capture_sequence"),
            original_frame_path=str(anchor.get("frame_path") or ""),
            original_image_sha256=anchor["image_sha256"], review_anchor=anchor,
            event_id=event_id, current_software=False,
            initial_labels=[],
        )

    def _current(self, event_id: str, base_revision_id: Optional[str]) -> dict[str, Any]:
        current = self._states.get(str(event_id))
        if current is None:
            raise PointEventError(f"Unknown point event: {event_id}")
        if base_revision_id is not None and current.get("revision_id") != base_revision_id:
            raise PointEventError("Point event changed since it was opened")
        return current

    def _review_update(
        self, event_id: str, *, actor: str, action: str, updater,
        base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            before = self._current(event_id, base_revision_id)
            after = copy.deepcopy(before)
            updater(after["review"])
            after["status"] = STATUS_DRAFT
            return self._commit(event_id=str(event_id), actor=actor, action=action, before=before, after=after)

    def edit_review(
        self, event_id: str, *, actor: str,
        base_revision_id: Optional[str] = None, **changes: Any,
    ) -> dict[str, Any]:
        unknown = set(changes) - {"comment"}
        if unknown:
            raise PointEventError("Unsupported review fields: " + ", ".join(sorted(unknown)))
        return self._review_update(event_id, actor=actor, action="edit", base_revision_id=base_revision_id, updater=lambda review: review.update(copy.deepcopy(changes)))

    def add_label(
        self, event_id: str, *, actor: str, label: Mapping[str, Any],
        base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        initial = (
            self._current(event_id, base_revision_id)["source"]["kind"]
            == "initial_assumption"
        )
        supplied = dict(label)
        supplied.setdefault("label_id", new_label_id())
        checked = validate_semantic_label(supplied)
        if initial and checked["kind"] != "surface_quality":
            raise PointEventError(
                "The initial state accepts only a surface-quality selection"
            )
        def update(review):
            labels = [*review["labels"], checked]
            _validate_labels(labels)
            review["labels"] = labels
        return self._review_update(event_id, actor=actor, action="add_label", updater=update, base_revision_id=base_revision_id)

    def edit_label(
        self, event_id: str, *, actor: str, label_id: str,
        label: Mapping[str, Any], base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        initial = (
            self._current(event_id, base_revision_id)["source"]["kind"]
            == "initial_assumption"
        )
        supplied = dict(label)
        if supplied.get("label_id") not in (None, "", label_id):
            raise PointEventError("Semantic label_id cannot be changed")
        supplied["label_id"] = str(label_id)
        checked = validate_semantic_label(supplied)
        if initial and checked["kind"] != "surface_quality":
            raise PointEventError(
                "The initial state accepts only a surface-quality selection"
            )
        def update(review):
            matches = [index for index, item in enumerate(review["labels"]) if item["label_id"] == label_id]
            if len(matches) != 1:
                raise PointEventError("Unknown or duplicate semantic label_id")
            labels = copy.deepcopy(review["labels"])
            labels[matches[0]] = checked
            _validate_labels(labels)
            review["labels"] = labels
        return self._review_update(event_id, actor=actor, action="edit_label", updater=update, base_revision_id=base_revision_id)

    def remove_label(
        self, event_id: str, *, actor: str, label_id: str,
        base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        def update(review):
            labels = [item for item in review["labels"] if item["label_id"] != label_id]
            if len(labels) != len(review["labels"]) - 1:
                raise PointEventError("Unknown or duplicate semantic label_id")
            review["labels"] = labels
        return self._review_update(event_id, actor=actor, action="remove_label", updater=update, base_revision_id=base_revision_id)

    def set_candidate_decision(
        self, event_id: str, *, actor: str, decision: str,
        base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        decision = str(decision)
        if decision not in CANDIDATE_DECISIONS:
            raise PointEventError("Candidate decision must be pending, confirmed, or rejected")
        current = self._current(event_id, base_revision_id)
        if current["source"]["kind"] not in CANDIDATE_SOURCES and decision != "confirmed":
            raise PointEventError("Human-created events are already confirmed")
        if current["review"]["candidate_decision"] == decision:
            raise PointEventError("Candidate decision is unchanged")
        def update(review: dict[str, Any]) -> None:
            review["candidate_decision"] = decision
            if decision == "rejected":
                # A rejected proposal is retained as acquisition evidence, not
                # as a physical training target.  The audit record's `before`
                # state preserves any labels or representative frame.
                review["labels"] = []
                review["representative_anchor"] = None

        return self._review_update(
            event_id, actor=actor, action="set_candidate_decision",
            base_revision_id=base_revision_id, updater=update,
        )

    def move_review_anchor(
        self, event_id: str, *, actor: str, anchor: Mapping[str, Any],
        base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        verified = make_review_anchor(**dict(anchor))
        return self._review_update(
            event_id,
            actor=actor,
            action="move_anchor",
            base_revision_id=base_revision_id,
            updater=lambda review: review.update({
                "anchor": verified,
                # The following interval changed.  Require a fresh, explicit
                # representative-frame choice instead of retaining stale data.
                "representative_anchor": None,
            }),
        )

    def move_representative_anchor(
        self, event_id: str, *, actor: str, anchor: Mapping[str, Any],
        base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        verified = make_review_anchor(**dict(anchor))
        return self._review_update(event_id, actor=actor, action="move_representative_anchor", base_revision_id=base_revision_id, updater=lambda review: review.update({"representative_anchor": verified}))

    def set_equalizer(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise PointEventError("Equalizer is not part of rheed-point-events-v2")

    @staticmethod
    def _anchor_coordinate(
        event: Mapping[str, Any], field: str,
    ) -> tuple[float, int] | None:
        value = event.get("review", {}).get(field)
        if not isinstance(value, Mapping) or value.get("elapsed_s") in (None, ""):
            return None
        try:
            sequence = int(value.get("capture_sequence"))
        except (TypeError, ValueError):
            sequence = -1
        return float(value["elapsed_s"]), sequence

    @staticmethod
    def _owns_state_interval(event: Mapping[str, Any]) -> bool:
        review = event.get("review", {})
        if not isinstance(review, Mapping) or review.get("disposition") != "active":
            return False
        if event.get("source", {}).get("kind") == "initial_assumption":
            return True
        return (
            review.get("candidate_decision") == "confirmed"
            and bool(review.get("labels"))
        )

    @staticmethod
    def _representative_anchor_identity(
        anchor: Mapping[str, Any],
    ) -> tuple[str, str]:
        return (
            str(anchor.get("capture_sequence", "")),
            str(anchor.get("image_sha256", "")).lower(),
        )

    def _interval_errors(
        self, event_id: str, *,
        state: Mapping[str, Mapping[str, Any]] | None = None,
        require_anchor: bool = True,
        validate_bounds: bool = True,
    ) -> list[str]:
        """Validate the one Anchor shared by an atomic boundary's interval."""

        states = self._states if state is None else state
        current = states[event_id]
        if current["review"]["candidate_decision"] == "rejected":
            return []
        if not self._owns_state_interval(current):
            review = current.get("review", {})
            provisional = (
                require_anchor
                and isinstance(review, Mapping)
                and review.get("disposition") == "active"
                and review.get("candidate_decision") == "confirmed"
            )
            if not provisional:
                return []
        boundary = self._anchor_coordinate(current, "anchor")
        if boundary is None:
            return []  # per-event completion_errors reports the missing point

        source_kind = str(current.get("source", {}).get("kind", ""))
        if source_kind == "initial_assumption":
            owners = [current]
        else:
            owners = [
                other for other in states.values()
                if other.get("source", {}).get("kind") != "initial_assumption"
                and self._owns_state_interval(other)
                and self._anchor_coordinate(other, "anchor") == boundary
            ]
        raw_anchors = [
            owner.get("review", {}).get("representative_anchor")
            for owner in owners
            if isinstance(
                owner.get("review", {}).get("representative_anchor"), Mapping,
            )
        ]
        anchors = {
            self._representative_anchor_identity(anchor): anchor
            for anchor in raw_anchors
        }
        if not anchors:
            return (
                ["a representative interval frame is required"]
                if require_anchor else []
            )
        if len(anchors) > 1:
            return [
                "events at one state boundary disagree about the interval Anchor"
            ]
        if not validate_bounds:
            return []
        representative = self._anchor_coordinate(
            {"review": {"representative_anchor": next(iter(anchors.values()))}},
            "representative_anchor",
        )
        if representative is None:
            return ["a representative interval frame is required"]
        errors: list[str] = []
        if representative < boundary:
            errors.append("representative interval frame must be at or after the event anchor")
        later_boundaries: list[tuple[float, int]] = []
        for other_id, other in states.items():
            if other_id == event_id:
                continue
            if other.get("source", {}).get("kind") == "initial_assumption":
                continue
            if not self._owns_state_interval(other):
                continue
            other_boundary = self._anchor_coordinate(other, "anchor")
            if other_boundary is not None and other_boundary > boundary:
                later_boundaries.append(other_boundary)
        if later_boundaries and representative >= min(later_boundaries):
            errors.append("representative interval frame must be before the next semantic event")
        return errors

    def complete(
        self, event_id: str, *, actor: str,
        base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            before = self._current(event_id, base_revision_id)
            errors = [*completion_errors(before), *self._interval_errors(str(event_id))]
            if errors:
                raise PointEventCompletionError(errors)
            after = copy.deepcopy(before)
            after["status"] = STATUS_COMPLETE
            return self._commit(event_id=str(event_id), actor=actor, action="complete", before=before, after=after)

    def reopen(self, event_id: str, *, actor: str) -> dict[str, Any]:
        with self._lock:
            before = self._current(event_id, None)
            after = copy.deepcopy(before)
            after["status"] = STATUS_DRAFT
            return self._commit(event_id=str(event_id), actor=actor, action="reopen", before=before, after=after)

    def dismiss(self, event_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise PointEventError("Dismiss requires a reason")
        with self._lock:
            before = self._current(event_id, None)
            if before["source"]["kind"] == "initial_assumption":
                raise PointEventError("The initial state cannot be dismissed")
            if before["source"]["kind"] not in SOURCE_EVENTS:
                raise PointEventError("Only source events use dismiss; delete a posthoc event")
            after = copy.deepcopy(before)
            after["review"].update({"disposition": "dismissed", "disposition_reason": reason})
            after["status"] = STATUS_DRAFT
            return self._commit(event_id=str(event_id), actor=actor, action="dismiss", before=before, after=after)

    def delete_posthoc(self, event_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise PointEventError("Delete requires a reason")
        with self._lock:
            before = self._current(event_id, None)
            if before["source"]["kind"] != "posthoc":
                raise PointEventError("Acquisition source events cannot be deleted")
            after = copy.deepcopy(before)
            after["review"].update({"disposition": "deleted", "disposition_reason": reason})
            after["status"] = STATUS_DRAFT
            return self._commit(event_id=str(event_id), actor=actor, action="delete", before=before, after=after)


__all__ = [
    "CANDIDATE_DECISIONS", "CANDIDATE_SOURCES", "JOURNAL_NAME",
    "LABELABLE_SOURCES", "LEGACY_SCHEMA_ID", "PATTERN_CLARITY_VALUES",
    "PointEventCompletionError", "PointEventError", "PointEventIntegrityError",
    "PointEventStore", "RECONSTRUCTION_VALUES", "SCHEMA_ID", "STATUS_COMPLETE",
    "SURFACE_QUALITY_VALUES",
    "STATUS_DRAFT", "SUMMARY_NAME", "TRANSACTION_NAME", "completion_errors",
    "deterministic_legacy_event_id", "make_review_anchor", "make_semantic_label",
    "new_event_id", "new_label_id", "sha256_file", "sha256_value",
    "source_identity_row", "source_row_sha256", "validate_semantic_label",
]
