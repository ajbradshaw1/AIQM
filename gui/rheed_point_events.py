"""Append-only point-event review records for saved RHEED frames.

The acquisition CSV files remain the immutable source of live evidence.  This
module stores every review operation as a separate, hash-protected JSONL
revision and materializes a replaceable current-state summary from that
journal.  The journal, not the summary, is authoritative.
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


SCHEMA_ID = "rheed-point-events-v1"
JOURNAL_NAME = "rheed_event_revisions.jsonl"
SUMMARY_NAME = "rheed_point_events.json"
TRANSACTION_NAME = ".rheed_event_revision.pending.json"

STATUS_DRAFT = "Draft"
STATUS_COMPLETE = "Complete"
LABELABLE_SOURCES = frozenset({"manual", "auto_capture", "posthoc"})
SOURCE_EVENTS = frozenset({"manual", "auto_capture"})
ACTIVE_EQUALIZER_CLASSES = ("1x1", "Tw(2x1)", "c(6x2)", "RT13")

_LEGACY_NAMESPACE = uuid.UUID("07756558-3619-5d20-9fe9-1ff011f45a5c")


class PointEventError(ValueError):
    """Base exception for invalid or conflicting point-event operations."""


class PointEventIntegrityError(PointEventError):
    """Raised when the append-only journal fails integrity validation."""


class PointEventCompletionError(PointEventError):
    """Raised when an event does not satisfy the explicit Complete gate."""

    def __init__(self, errors: list[str]):
        self.errors = tuple(errors)
        super().__init__("Event cannot be completed: " + "; ".join(errors))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_value(value: object) -> str:
    """Return the SHA-256 of a value's canonical JSON representation."""
    return hashlib.sha256(_json_bytes(value)).hexdigest()


_AUTO_CAPTURE_MUTABLE_FIELDS = frozenset({"event_state", "state_changed_at"})


def source_identity_row(
    row: Mapping[str, object], *, source_kind: str = "manual",
) -> dict[str, str]:
    """Return immutable source identity columns for one acquisition row."""

    excluded = (
        _AUTO_CAPTURE_MUTABLE_FIELDS
        if str(source_kind) == "auto_capture"
        else frozenset()
    )
    return {
        str(key): "" if value is None else str(value)
        for key, value in row.items()
        if str(key) not in excluded
    }


def source_row_sha256(
    row: Mapping[str, object], *, source_kind: str = "manual",
) -> str:
    """Hash only immutable identity fields of an acquisition CSV row."""
    return sha256_value(source_identity_row(row, source_kind=source_kind))


def sha256_file(path: str | Path) -> str:
    """Hash a saved frame without decoding or transforming its pixels."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deterministic_legacy_event_id(
    *,
    session_identity: str,
    source_file: str,
    source_index: str | int,
    original_at_utc: str,
    capture_sequence: str | int | None,
    source_row_hash: str = "",
) -> str:
    """Build the same legacy event ID on every import.

    A bare ``event_idx`` is intentionally insufficient because each source
    CSV and each session starts its own counter at one.
    """
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
    """Return a UUID for an event created by current software."""
    return str(uuid.uuid4())


def make_review_anchor(
    *,
    frame_path: str | Path,
    capture_sequence: str | int | None,
    image_sha256: Optional[str] = None,
    image_sha256_algorithm: str = "raw-file-bytes-v1",
    captured_at_utc: str = "",
    elapsed_s: float | str | None = None,
    view_segment_id: str | int | None = None,
    capture_geometry_id: str = "",
) -> dict[str, Any]:
    """Describe one real, saved frame that can anchor a review.

    Review anchors cannot point at interpolated timeline positions.  The file
    must exist at creation time and its content hash becomes part of the
    anchor identity.
    """
    algorithm = str(image_sha256_algorithm or "raw-file-bytes-v1").lower()
    if algorithm not in {"sha256", "raw-file-bytes-v1"}:
        raise PointEventError("Review anchors require raw-file-bytes SHA-256")
    path = Path(frame_path)
    if not path.is_file():
        raise PointEventError(f"Review anchor is not a saved frame: {path}")
    digest = (image_sha256 or sha256_file(path)).lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise PointEventError("Review anchor requires a valid SHA-256")
    actual = sha256_file(path)
    if actual != digest:
        raise PointEventIntegrityError("Review anchor frame SHA-256 does not match")
    try:
        elapsed = None if elapsed_s in (None, "") else float(elapsed_s)
    except (TypeError, ValueError) as exc:
        raise PointEventError("Review anchor elapsed_s must be numeric") from exc
    if elapsed is not None and (not math.isfinite(elapsed) or elapsed < 0):
        raise PointEventError("Review anchor elapsed_s must be finite and non-negative")
    return {
        "frame_path": str(path),
        "image_sha256": digest,
        "image_sha256_algorithm": "raw-file-bytes-v1",
        "capture_sequence": capture_sequence,
        "captured_at_utc": str(captured_at_utc or ""),
        "elapsed_s": elapsed,
        "view_segment_id": view_segment_id,
        "capture_geometry_id": str(capture_geometry_id or ""),
    }


def _empty_review(anchor: Optional[Mapping[str, Any]], comment: str) -> dict[str, Any]:
    return {
        "anchor": copy.deepcopy(dict(anchor)) if anchor is not None else None,
        "comment": str(comment or ""),
        "reviewer": "",
        "confidence": None,
        "human_reconstruction": None,
        "change_from": None,
        "change_to": None,
        "equalizer": None,
        "disposition": "active",
        "disposition_reason": "",
    }


def _validate_equalizer(equalizer: object, anchor: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(anchor, Mapping):
        return ["a saved review frame is required"]
    if not isinstance(equalizer, Mapping):
        return ["a valid Equalizer measurement is required"]
    if equalizer.get("valid") is not True:
        errors.append("Equalizer measurement is not valid")
    for key in ("calibration_id", "basis_bundle_id", "frame_sha256"):
        if not str(equalizer.get(key) or "").strip():
            errors.append(f"Equalizer {key} is missing")
    try:
        measurement_sequence = int(str(equalizer.get("capture_sequence") or "").strip())
        anchor_sequence = int(str(anchor.get("capture_sequence") or "").strip())
    except (TypeError, ValueError):
        errors.append("Equalizer capture sequence is invalid")
    else:
        if measurement_sequence != anchor_sequence:
            errors.append("Equalizer result belongs to a different capture sequence")

    frame_hash = str(equalizer.get("frame_sha256") or "").strip().lower()
    anchor_hash = str(anchor.get("image_sha256") or "").strip().lower()
    if len(frame_hash) != 64 or any(
        character not in "0123456789abcdef" for character in frame_hash
    ):
        errors.append("Equalizer frame SHA-256 is invalid")
    algorithm = str(
        equalizer.get("frame_sha256_algorithm") or ""
    ).strip().lower()
    if algorithm == "rgb-array-v1":
        raw_hash = str(equalizer.get("raw_frame_sha256") or "").strip().lower()
        raw_algorithm = str(
            equalizer.get("raw_frame_sha256_algorithm") or ""
        ).strip().lower()
        if raw_hash != anchor_hash or raw_algorithm != "raw-file-bytes-v1":
            errors.append(
                "Equalizer RGB hash is not bound to the exact raw review frame"
            )
        if equalizer.get("rgb_hash_verified_from_raw_frame") is not True:
            errors.append(
                "Equalizer RGB hash was not verified from the raw review frame"
            )
    elif algorithm in {
        "", "raw-file-bytes-v1", "sha256-file-bytes", "sha256",
    }:
        if frame_hash != anchor_hash:
            errors.append("Equalizer result belongs to a different review frame")
    else:
        errors.append("Equalizer frame hash algorithm is incompatible")
    active = equalizer.get("active_classes")
    if tuple(active or ()) != ACTIVE_EQUALIZER_CLASSES:
        errors.append(
            "Equalizer active classes must be 1x1, Tw(2x1), c(6x2), and RT13"
        )
    weights = equalizer.get("weights")
    if not isinstance(weights, Mapping):
        errors.append("Equalizer raw/final/normalized weights are missing")
    else:
        for stage in ("raw", "final", "normalized"):
            values = weights.get(stage)
            if not isinstance(values, Mapping):
                errors.append(f"Equalizer {stage} weights are missing")
                continue
            for label in ACTIVE_EQUALIZER_CLASSES:
                if stage == "raw" and values.get(label) is None:
                    # Manual Equalizer fits have no pre-normalization least-
                    # squares coefficient.  The explicit null is evidence,
                    # not a missing class or a zero coefficient.
                    continue
                try:
                    value = float(values[label])
                except (KeyError, TypeError, ValueError):
                    errors.append(f"Equalizer {stage} weight for {label} is invalid")
                    continue
                if not math.isfinite(value):
                    errors.append(f"Equalizer {stage} weight for {label} is invalid")
            if values.get("HTR") not in (None, ""):
                errors.append("Equalizer HTR weight must be empty")
    if equalizer.get("HTR") not in (None, ""):
        errors.append("Equalizer HTR value must be empty")
    try:
        residual = float(equalizer.get("fit_residual"))
        if not math.isfinite(residual) or residual < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Equalizer fit residual is invalid")
    try:
        coverage = float(equalizer.get("valid_coverage"))
        if not math.isfinite(coverage) or not 0 < coverage <= 1:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Equalizer valid coverage is invalid")
    return errors


def completion_errors(event: Mapping[str, Any]) -> list[str]:
    """Return human-readable reasons an event cannot be completed."""
    review = event.get("review")
    if not isinstance(review, Mapping):
        return ["review content is missing"]
    errors: list[str] = []
    if not str(review.get("comment") or "").strip():
        errors.append("comment is required")
    if not str(review.get("reviewer") or "").strip():
        errors.append("reviewer is required")
    if review.get("disposition") != "active":
        errors.append("dismissed or deleted events cannot be completed")
    errors.extend(_validate_equalizer(review.get("equalizer"), review.get("anchor")))
    return errors


def _changed_review_fields(
    before: Mapping[str, Any], after: Mapping[str, Any],
) -> set[str]:
    keys = set(before) | set(after)
    return {key for key in keys if before.get(key) != after.get(key)}


def _validate_revision_transition(
    before: Optional[Mapping[str, Any]],
    after: Mapping[str, Any],
    *,
    action: str,
    actor: str,
) -> None:
    """Reject a validly re-hashed row that claims the wrong operation."""

    if action not in {
        "create", "edit", "move_anchor", "set_equalizer", "complete",
        "reopen", "dismiss", "delete",
    }:
        raise PointEventIntegrityError("Point-event revision action is invalid")
    if not str(actor or "").strip():
        raise PointEventIntegrityError("Point-event revision actor is missing")
    if not isinstance(after, Mapping):
        raise PointEventIntegrityError("Point-event after-state is invalid")
    if after.get("schema") != SCHEMA_ID or not str(after.get("event_id") or ""):
        raise PointEventIntegrityError("Point-event state identity is invalid")
    source = after.get("source")
    review = after.get("review")
    if not isinstance(source, Mapping) or source.get("kind") not in LABELABLE_SOURCES:
        raise PointEventIntegrityError("Point-event source evidence is invalid")
    if not isinstance(review, Mapping):
        raise PointEventIntegrityError("Point-event review state is invalid")
    if after.get("status") not in {STATUS_DRAFT, STATUS_COMPLETE}:
        raise PointEventIntegrityError("Point-event status is invalid")
    disposition = review.get("disposition")
    if disposition not in {"active", "dismissed", "deleted"}:
        raise PointEventIntegrityError("Point-event disposition is invalid")
    if disposition == "deleted" and source.get("kind") != "posthoc":
        raise PointEventIntegrityError("Acquisition source events cannot be deleted")
    if disposition != "active" and not str(
        review.get("disposition_reason") or ""
    ).strip():
        raise PointEventIntegrityError("Inactive point event requires a reason")
    if review.get("equalizer") is not None:
        errors = _validate_equalizer(review.get("equalizer"), review.get("anchor"))
        if errors:
            raise PointEventIntegrityError(
                "Invalid Equalizer state in point-event revision: "
                + "; ".join(errors)
            )
    if after.get("status") == STATUS_COMPLETE:
        errors = completion_errors(after)
        if errors:
            raise PointEventIntegrityError(
                "Invalid Complete point-event state: " + "; ".join(errors)
            )

    if before is None:
        if (
            action != "create"
            or after.get("status") != STATUS_DRAFT
            or review.get("disposition") != "active"
            or review.get("disposition_reason") != ""
            or review.get("equalizer") is not None
            or int(after.get("revision_number", 0)) != 1
        ):
            raise PointEventIntegrityError("Point-event create transition is invalid")
        return

    before_review = before.get("review")
    if not isinstance(before_review, Mapping):
        raise PointEventIntegrityError("Point-event previous review state is invalid")
    if before.get("event_id") != after.get("event_id") or before.get(
        "source"
    ) != after.get("source"):
        raise PointEventIntegrityError("Immutable source evidence was modified")
    protected_keys = set(before) | set(after)
    protected_keys -= {
        "review", "status", "revision_id", "revision_number", "updated_at_utc",
    }
    if any(before.get(key) != after.get(key) for key in protected_keys):
        raise PointEventIntegrityError("Protected point-event metadata was modified")
    try:
        expected_revision_number = int(before.get("revision_number", 0)) + 1
        actual_revision_number = int(after.get("revision_number", 0))
    except (TypeError, ValueError) as exc:
        raise PointEventIntegrityError(
            "Point-event revision number is invalid"
        ) from exc
    if actual_revision_number != expected_revision_number:
        raise PointEventIntegrityError("Point-event revision number is not consecutive")

    changed = _changed_review_fields(before_review, review)
    before_status = before.get("status")
    after_status = after.get("status")
    if action == "edit":
        allowed = {
            "comment", "reviewer", "confidence", "human_reconstruction",
            "change_from", "change_to",
        }
        valid = changed <= allowed and after_status == STATUS_DRAFT
    elif action == "move_anchor":
        valid = (
            changed <= {"anchor", "equalizer"}
            and review.get("equalizer") is None
            and after_status == STATUS_DRAFT
        )
    elif action == "set_equalizer":
        valid = (
            changed <= {"equalizer"}
            and review.get("equalizer") is not None
            and after_status == STATUS_DRAFT
        )
    elif action == "complete":
        valid = (
            not changed
            and before_status == STATUS_DRAFT
            and after_status == STATUS_COMPLETE
            and not completion_errors(after)
        )
    elif action == "reopen":
        valid = (
            not changed
            and before_status == STATUS_COMPLETE
            and after_status == STATUS_DRAFT
        )
    elif action == "dismiss":
        valid = (
            before.get("source", {}).get("kind") in SOURCE_EVENTS
            and changed <= {"disposition", "disposition_reason"}
            and review.get("disposition") == "dismissed"
            and bool(str(review.get("disposition_reason") or "").strip())
            and after_status == STATUS_DRAFT
        )
    elif action == "delete":
        valid = (
            before.get("source", {}).get("kind") == "posthoc"
            and changed <= {"disposition", "disposition_reason"}
            and review.get("disposition") == "deleted"
            and bool(str(review.get("disposition_reason") or "").strip())
            and after_status == STATUS_DRAFT
        )
    else:  # ``create`` is valid only when ``before`` is absent.
        valid = False
    if not valid:
        raise PointEventIntegrityError(
            f"Point-event {action} transition is semantically invalid"
        )


class PointEventStore:
    """Crash-recoverable append-only review journal for one session."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.directory / JOURNAL_NAME
        self.summary_path = self.directory / SUMMARY_NAME
        self.transaction_path = self.directory / TRANSACTION_NAME
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._revision_ids: set[str] = set()
        with self._lock:
            self._recover_pending()
            self._replay()
            self._write_summary()

    @property
    def states(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._states)

    def get(self, event_id: str) -> Optional[dict[str, Any]]:
        state = self._states.get(str(event_id))
        return copy.deepcopy(state) if state is not None else None

    @staticmethod
    def _record_hash(record: Mapping[str, Any]) -> str:
        payload = {key: value for key, value in record.items() if key != "record_sha256"}
        return sha256_value(payload)

    @classmethod
    def _validate_record_hash(cls, record: Mapping[str, Any]) -> None:
        expected = str(record.get("record_sha256") or "").lower()
        if expected != cls._record_hash(record):
            raise PointEventIntegrityError("Point-event revision hash mismatch")

    def _read_records(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with open(self.journal_path, "r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PointEventIntegrityError(
                        f"Invalid point-event JSONL at line {line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise PointEventIntegrityError(
                        f"Point-event JSONL line {line_number} is not an object"
                    )
                self._validate_record_hash(record)
                records.append(record)
        return records

    def _replay(self) -> None:
        states: dict[str, dict[str, Any]] = {}
        revision_ids: set[str] = set()
        for record in self._read_records():
            if record.get("schema") != SCHEMA_ID:
                raise PointEventIntegrityError("Unsupported point-event journal schema")
            revision_id = str(record.get("revision_id") or "")
            event_id = str(record.get("event_id") or "")
            if not revision_id or revision_id in revision_ids or not event_id:
                raise PointEventIntegrityError("Duplicate or missing revision identity")
            before = record.get("before")
            after = record.get("after")
            if not isinstance(after, dict) or after.get("event_id") != event_id:
                raise PointEventIntegrityError("Point-event after-state identity is invalid")
            previous = states.get(event_id)
            if previous is None:
                if record.get("action") != "create" or before is not None:
                    raise PointEventIntegrityError("Event journal starts without create")
            else:
                if record.get("base_revision_id") != previous.get("revision_id"):
                    raise PointEventIntegrityError("Point-event revision chain is broken")
                if before != previous:
                    raise PointEventIntegrityError("Point-event revision before-state differs")
                if after.get("source") != previous.get("source"):
                    raise PointEventIntegrityError("Immutable source evidence was modified")
            if after.get("revision_id") != revision_id:
                raise PointEventIntegrityError("Point-event state revision ID is invalid")
            _validate_revision_transition(
                previous,
                after,
                action=str(record.get("action") or ""),
                actor=str(record.get("actor") or ""),
            )
            states[event_id] = copy.deepcopy(after)
            revision_ids.add(revision_id)
        self._states = states
        self._revision_ids = revision_ids

    def _atomic_write(self, path: Path, data: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def _append_record(self, record: Mapping[str, Any]) -> None:
        line = _json_bytes(record) + b"\n"
        with open(self.journal_path, "ab") as stream:
            stream.write(line)
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
        revision_id = str(record.get("revision_id") or "")
        matches = [
            item for item in self._read_records()
            if str(item.get("revision_id") or "") == revision_id
        ]
        if len(matches) > 1:
            raise PointEventIntegrityError(
                "Pending point-event revision identity is duplicated in journal"
            )
        if matches and _json_bytes(matches[0]) != _json_bytes(record):
            raise PointEventIntegrityError(
                "Pending point-event revision conflicts with journal"
            )
        if not matches:
            self._append_record(record)
        self.transaction_path.unlink()

    def _write_summary(self) -> None:
        payload = {
            "schema": SCHEMA_ID,
            "generated_at_utc": _utc_now(),
            "journal": JOURNAL_NAME,
            "events": [self._states[key] for key in sorted(self._states)],
        }
        self._atomic_write(self.summary_path, _json_bytes(payload) + b"\n")

    def materialize_summary(self) -> Path:
        with self._lock:
            self._write_summary()
        return self.summary_path

    def _commit(
        self,
        *,
        event_id: str,
        actor: str,
        action: str,
        before: Optional[Mapping[str, Any]],
        after: Mapping[str, Any],
    ) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise PointEventError("Every revision requires an actor")
        revision_id = str(uuid.uuid4())
        next_state = copy.deepcopy(dict(after))
        next_state["revision_id"] = revision_id
        next_state["updated_at_utc"] = _utc_now()
        next_state["revision_number"] = (
            int(before.get("revision_number", 0)) + 1 if before is not None else 1
        )
        record: dict[str, Any] = {
            "schema": SCHEMA_ID,
            "revision_id": revision_id,
            "event_id": event_id,
            "actor": actor,
            "recorded_at_utc": _utc_now(),
            "action": action,
            "base_revision_id": before.get("revision_id") if before is not None else None,
            "before": copy.deepcopy(dict(before)) if before is not None else None,
            "after": next_state,
        }
        _validate_revision_transition(
            before,
            next_state,
            action=action,
            actor=actor,
        )
        record["record_sha256"] = self._record_hash(record)
        self._atomic_write(self.transaction_path, _json_bytes(record) + b"\n")
        self._append_record(record)
        self._states[event_id] = copy.deepcopy(next_state)
        self._revision_ids.add(revision_id)
        self._write_summary()
        # The journal is authoritative and the summary is replaceable, but
        # retain the transaction marker until both durable representations
        # contain the revision. A forced exit between journal append and
        # summary replacement is then unambiguously recoverable.
        self.transaction_path.unlink()
        return copy.deepcopy(next_state)

    def create_event(
        self,
        *,
        source_kind: str,
        actor: str,
        session_identity: str,
        source_file: str,
        source_index: str | int,
        source_row: Mapping[str, Any],
        original_at_utc: str,
        original_elapsed_s: float | str | None,
        original_note: str = "",
        capture_sequence: str | int | None = None,
        original_frame_path: str = "",
        original_image_sha256: str = "",
        review_anchor: Optional[Mapping[str, Any]] = None,
        event_id: Optional[str] = None,
        current_software: bool = True,
    ) -> dict[str, Any]:
        """Create one Draft while retaining the exact source-row binding."""
        kind = str(source_kind)
        if kind not in LABELABLE_SOURCES:
            raise PointEventError(f"Unsupported labelable source: {kind}")
        verified_anchor = (
            make_review_anchor(**dict(review_anchor))
            if review_anchor is not None else None
        )
        try:
            elapsed = (
                None if original_elapsed_s in (None, "") else float(original_elapsed_s)
            )
        except (TypeError, ValueError) as exc:
            raise PointEventError("Original event elapsed_s must be numeric") from exc
        if elapsed is not None and (not math.isfinite(elapsed) or elapsed < 0):
            raise PointEventError(
                "Original event elapsed_s must be finite and non-negative"
            )
        if event_id is None:
            row = {str(key): value for key, value in source_row.items()}
            event_id = (
                new_event_id()
                if current_software
                else deterministic_legacy_event_id(
                    session_identity=session_identity,
                    source_file=source_file,
                    source_index=source_index,
                    original_at_utc=original_at_utc,
                    capture_sequence=capture_sequence,
                    source_row_hash=source_row_sha256(row, source_kind=kind),
                )
            )
        event_id = str(event_id)
        with self._lock:
            if event_id in self._states:
                raise PointEventError(f"Point event already exists: {event_id}")
            row = {str(key): value for key, value in source_row.items()}
            source = {
                "kind": kind,
                "session_identity": str(session_identity),
                "source_file": str(source_file).replace("\\", "/"),
                "source_index": str(source_index),
                "source_row_sha256": source_row_sha256(row, source_kind=kind),
                "source_row_hash_algorithm": (
                    "sha256-canonical-json-auto-immutable-v1"
                    if kind == "auto_capture"
                    else "sha256-canonical-json-row-v1"
                ),
                "original_at_utc": str(original_at_utc or ""),
                "original_elapsed_s": elapsed,
                "original_frame_path": str(original_frame_path or ""),
                "original_image_sha256": str(original_image_sha256 or "").lower(),
                "original_image_sha256_algorithm": (
                    "raw-file-bytes-v1" if original_image_sha256 else ""
                ),
                "capture_sequence": capture_sequence,
                "original_note": str(original_note or ""),
            }
            state = {
                "schema": SCHEMA_ID,
                "event_id": event_id,
                "source": source,
                "review": _empty_review(verified_anchor, original_note),
                "status": STATUS_DRAFT,
                "created_at_utc": _utc_now(),
                "revision_id": "",
                "revision_number": 0,
                "updated_at_utc": "",
            }
            return self._commit(
                event_id=event_id,
                actor=actor,
                action="create",
                before=None,
                after=state,
            )

    def _current(self, event_id: str, base_revision_id: Optional[str]) -> dict[str, Any]:
        current = self._states.get(str(event_id))
        if current is None:
            raise PointEventError(f"Unknown point event: {event_id}")
        if base_revision_id is not None and current.get("revision_id") != base_revision_id:
            raise PointEventError("Point event changed since it was opened")
        return current

    def edit_review(
        self,
        event_id: str,
        *,
        actor: str,
        base_revision_id: Optional[str] = None,
        **changes: Any,
    ) -> dict[str, Any]:
        allowed = {
            "comment", "reviewer", "confidence", "human_reconstruction",
            "change_from", "change_to",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise PointEventError("Unsupported review fields: " + ", ".join(sorted(unknown)))
        with self._lock:
            before = self._current(event_id, base_revision_id)
            after = copy.deepcopy(before)
            for key, value in changes.items():
                after["review"][key] = value
            after["status"] = STATUS_DRAFT
            return self._commit(
                event_id=str(event_id), actor=actor, action="edit",
                before=before, after=after,
            )

    def move_review_anchor(
        self,
        event_id: str,
        *,
        actor: str,
        anchor: Mapping[str, Any],
        base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        verified = make_review_anchor(**dict(anchor))
        with self._lock:
            before = self._current(event_id, base_revision_id)
            after = copy.deepcopy(before)
            after["review"]["anchor"] = verified
            after["review"]["equalizer"] = None
            after["status"] = STATUS_DRAFT
            return self._commit(
                event_id=str(event_id), actor=actor, action="move_anchor",
                before=before, after=after,
            )

    def set_equalizer(
        self,
        event_id: str,
        *,
        actor: str,
        equalizer: Mapping[str, Any],
        base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            before = self._current(event_id, base_revision_id)
            errors = _validate_equalizer(equalizer, before["review"].get("anchor"))
            if errors:
                raise PointEventError("Invalid Equalizer measurement: " + "; ".join(errors))
            after = copy.deepcopy(before)
            after["review"]["equalizer"] = copy.deepcopy(dict(equalizer))
            after["status"] = STATUS_DRAFT
            return self._commit(
                event_id=str(event_id), actor=actor, action="set_equalizer",
                before=before, after=after,
            )

    def complete(
        self,
        event_id: str,
        *,
        actor: str,
        base_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            before = self._current(event_id, base_revision_id)
            errors = completion_errors(before)
            if errors:
                raise PointEventCompletionError(errors)
            after = copy.deepcopy(before)
            after["status"] = STATUS_COMPLETE
            return self._commit(
                event_id=str(event_id), actor=actor, action="complete",
                before=before, after=after,
            )

    def reopen(self, event_id: str, *, actor: str) -> dict[str, Any]:
        with self._lock:
            before = self._current(event_id, None)
            after = copy.deepcopy(before)
            after["status"] = STATUS_DRAFT
            return self._commit(
                event_id=str(event_id), actor=actor, action="reopen",
                before=before, after=after,
            )

    def dismiss(self, event_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise PointEventError("Dismiss requires a reason")
        with self._lock:
            before = self._current(event_id, None)
            if before["source"]["kind"] not in SOURCE_EVENTS:
                raise PointEventError("Only source events use dismiss; delete a posthoc event")
            after = copy.deepcopy(before)
            after["review"]["disposition"] = "dismissed"
            after["review"]["disposition_reason"] = reason
            after["status"] = STATUS_DRAFT
            return self._commit(
                event_id=str(event_id), actor=actor, action="dismiss",
                before=before, after=after,
            )

    def delete_posthoc(self, event_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise PointEventError("Delete requires a reason")
        with self._lock:
            before = self._current(event_id, None)
            if before["source"]["kind"] != "posthoc":
                raise PointEventError("Acquisition source events cannot be deleted")
            after = copy.deepcopy(before)
            after["review"]["disposition"] = "deleted"
            after["review"]["disposition_reason"] = reason
            after["status"] = STATUS_DRAFT
            return self._commit(
                event_id=str(event_id), actor=actor, action="delete",
                before=before, after=after,
            )
