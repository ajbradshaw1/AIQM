"""Immutable-source, append-only review model for RHEED point events.

The acquisition CSV files and session ZIP are evidence.  This module never
rewrites them: it imports stable source events, applies auditable revisions,
and materializes a current review state that can always be rebuilt.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
import threading
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from gui.rheed_event_state import (
    DEFAULT_INITIAL_STATE,
    EventStateError,
    SegmentAnchor,
    replay_event_states,
)
from gui.rheed_point_events import (
    CANDIDATE_DECISIONS,
    CANDIDATE_SOURCES,
    LEGACY_SCHEMA_ID,
    PATTERN_CLARITY_VALUES,
    PointEventIntegrityError as LivePointEventIntegrityError,
    RECONSTRUCTION_VALUES,
    SCHEMA_ID as LIVE_SCHEMA_ID,
    V2_SCHEMA_ID,
    _validate_revision_transition as validate_live_revision_transition,
    deterministic_legacy_event_id as deterministic_live_event_id,
    make_semantic_label,
    source_row_sha256 as source_identity_sha256,
    validate_semantic_label,
)

from .session_archive import (
    CsvMember,
    FrameRecord,
    SessionArchive,
    load_session_archive,
    optional_member,
    read_archived_bytes,
    read_csv_member,
    resolve_archived_path,
)


SCHEMA_VERSION = LIVE_SCHEMA_ID
V2_SCHEMA_VERSION = V2_SCHEMA_ID
LEGACY_SCHEMA_VERSION = LEGACY_SCHEMA_ID
DOCUMENT_TYPE = "ai4mbe_rheed_point_event_annotations"
INITIAL_STATE = {
    "reconstructions": sorted(DEFAULT_INITIAL_STATE.reconstructions),
    "clarity": DEFAULT_INITIAL_STATE.clarity,
}
# Retained only so archived v1 Equalizer evidence can be validated read-only.
ACTIVE_EQUALIZER_BASES = ("1x1", "Tw(2x1)", "c(6x2)", "RT13")
SOURCE_TYPES = frozenset({
    "manual", "auto_capture", "posthoc", "initial_assumption",
})
STATUSES = frozenset({"Draft", "Complete"})
REVISION_ACTIONS = frozenset({
    "create_posthoc", "edit", "add_label", "edit_label", "remove_label",
    "set_candidate_decision", "move_anchor", "move_representative_anchor",
    "complete", "reopen", "dismiss", "undismiss", "delete", "restore",
})
_EVENT_NAMESPACE = uuid.UUID("51f4c9b1-f062-4b8f-8b9a-527e7899c921")


class PointEventValidationError(ValueError):
    """Point-event data violates the immutable provenance/review contract."""


@dataclass(frozen=True)
class ImportedPointEvents:
    events: tuple[dict[str, Any], ...]
    reference_events: tuple[dict[str, Any], ...]
    unlinked_legacy_labels: tuple[dict[str, Any], ...]
    sensor_context: dict[str, Any]
    frame_contexts: tuple[dict[str, Any], ...]
    source_revisions: tuple[dict[str, Any], ...] = ()
    source_journal: dict[str, Any] | None = None


class RevisionStore:
    """Crash-recoverable sidecar for one append-only event revision journal."""

    JOURNAL_NAME = "rheed_event_revisions.jsonl"
    SUMMARY_NAME = "rheed_point_events.json"
    PENDING_NAME = ".rheed_event_revision.pending.json"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.journal_path = self.directory / self.JOURNAL_NAME
        self.summary_path = self.directory / self.SUMMARY_NAME
        self.pending_path = self.directory / self.PENDING_NAME

    @staticmethod
    def _atomic_json(path: Path, value: object) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, indent=2,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_journal(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        records: list[dict[str, Any]] = []
        raw_lines = self.journal_path.read_bytes().splitlines()
        for line_number, raw_line in enumerate(raw_lines, 1):
            if not raw_line.strip():
                continue
            try:
                line = raw_line.decode("utf-8")
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                # If a process died during the one-line append, a durable
                # transaction marker contains the complete expected bytes.
                # Repair only a trailing prefix of that exact line.
                if line_number == len(raw_lines) and self.pending_path.exists():
                    try:
                        pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
                        expected = _canonical_json(pending["revision"])
                    except (OSError, KeyError, TypeError, json.JSONDecodeError):
                        expected = b""
                    if raw_line and expected.startswith(raw_line):
                        self._rewrite_records(records)
                        return records
                raise PointEventValidationError(
                    f"revision journal line {line_number} is not valid JSON"
                ) from exc
            if not isinstance(value, dict):
                raise PointEventValidationError(
                    f"revision journal line {line_number} is not an object"
                )
            records.append(value)
        return records

    def _assert_current_writable_schema(self) -> None:
        """Reject legacy sidecars before recovery can append or rewrite files."""

        candidates: list[tuple[str, object]] = []
        if self.summary_path.exists():
            try:
                summary = json.loads(self.summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                summary = None  # the ordinary validator reports the exact fault
            if isinstance(summary, Mapping):
                candidates.append(("summary", summary.get("schema_version")))
        if self.journal_path.exists():
            try:
                for line in self.journal_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if isinstance(record, Mapping):
                        candidates.append(("journal", record.get("schema_version")))
            except (OSError, json.JSONDecodeError):
                pass
        if self.pending_path.exists():
            try:
                pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pending = None
            revision = pending.get("revision") if isinstance(pending, Mapping) else None
            if isinstance(revision, Mapping):
                candidates.append(("pending transaction", revision.get("schema_version")))
        for source, schema in candidates:
            if schema == V2_SCHEMA_VERSION:
                raise PointEventValidationError(
                    f"legacy {V2_SCHEMA_VERSION} {source} is read-only; "
                    "use a new v3 annotation directory"
                )
            if schema not in {None, SCHEMA_VERSION}:
                raise PointEventValidationError(
                    f"unsupported {source} schema; refusing to modify sidecar"
                )

    def _rewrite_records(self, records: Sequence[Mapping[str, Any]]) -> None:
        temporary = self.journal_path.with_name(
            f".{self.journal_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("xb") as stream:
                for record in records:
                    stream.write(_canonical_json(dict(record)) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.journal_path)
        finally:
            temporary.unlink(missing_ok=True)

    def recover(self) -> list[dict[str, Any]]:
        """Finish a revision whose durable transaction marker survived a crash."""

        self._assert_current_writable_schema()
        self.directory.mkdir(parents=True, exist_ok=True)
        records = self._read_journal()
        if not self.pending_path.exists():
            _validate_record_integrity(records)
            return records
        try:
            pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
            revision = pending["revision"]
            expected = str(pending["revision_sha256"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise PointEventValidationError("pending revision transaction is invalid") from exc
        if _sha256_json(revision) != expected:
            raise PointEventValidationError("pending revision transaction hash is invalid")
        identifier = str(revision.get("revision_id", ""))
        matches = [record for record in records if str(record.get("revision_id", "")) == identifier]
        if len(matches) > 1 or (matches and matches[0] != revision):
            raise PointEventValidationError("pending revision conflicts with the journal")
        if not matches:
            _validate_record_integrity([*records, revision])
            self._append_line(revision)
            records.append(revision)
        self.pending_path.unlink()
        _validate_record_integrity(records)
        return records

    def _append_line(self, revision: Mapping[str, Any]) -> None:
        line = _canonical_json(dict(revision)) + b"\n"
        with self.journal_path.open("ab") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    def append(
        self, revision: Mapping[str, Any], *,
        initial_events: Sequence[Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Commit one revision, replay it, and atomically refresh the summary."""

        self.directory.mkdir(parents=True, exist_ok=True)
        records = self.recover()
        candidate = _with_record_integrity(
            revision,
            previous_record_sha256=(
                str(records[-1]["record_sha256"]) if records else ""
            ),
        )
        # Validate the whole chain before persisting the new record.
        state = replay_revisions(
            initial_events, [*records, candidate], require_integrity=True,
        )
        marker = {
            "schema_version": 1,
            "revision_id": candidate.get("revision_id", ""),
            "revision_sha256": _sha256_json(candidate),
            "revision": candidate,
        }
        self._atomic_json(self.pending_path, marker)
        self._append_line(candidate)
        self._atomic_json(self.summary_path, {
            "schema_version": SCHEMA_VERSION,
            "events": list(state.values()),
            "last_revision_id": candidate.get("revision_id", ""),
        })
        self.pending_path.unlink()
        return state

    def load(
        self, initial_events: Sequence[Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        records = self.recover()
        state = replay_revisions(initial_events, records, require_integrity=True)
        self._atomic_json(self.summary_path, {
            "schema_version": SCHEMA_VERSION,
            "events": list(state.values()),
            "last_revision_id": records[-1]["revision_id"] if records else "",
        })
        return state


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _record_hash(record: Mapping[str, Any]) -> str:
    return _sha256_json({
        key: value for key, value in record.items() if key != "record_sha256"
    })


def _with_record_integrity(
    revision: Mapping[str, Any], *, previous_record_sha256: str,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(revision))
    result.pop("record_sha256", None)
    result["previous_record_sha256"] = str(previous_record_sha256 or "").lower()
    result["record_sha256"] = _record_hash(result)
    return result


def _validate_record_integrity(records: Sequence[Mapping[str, Any]]) -> None:
    previous = ""
    for index, raw in enumerate(records, 1):
        expected_previous = str(raw.get("previous_record_sha256", "")).lower()
        if expected_previous != previous:
            raise PointEventValidationError(
                f"revision journal hash chain is broken at record {index}"
            )
        supplied = str(raw.get("record_sha256", "")).lower()
        if len(supplied) != 64 or supplied != _record_hash(raw):
            raise PointEventValidationError(
                f"revision journal record hash is invalid at record {index}"
            )
        previous = supplied


def _utc(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(parsed_text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _int(value: object) -> int | None:
    try:
        text = str(value or "").strip()
        return int(text) if text else None
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    try:
        text = str(value or "").strip()
        number = float(text) if text else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


def _bool(value: object) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def _frame_contexts(session: SessionArchive) -> list[dict[str, Any]]:
    """Reconstruct only provenance that is actually present in archived logs.

    Older heartbeat schemas omit the monotonic receive time and view state.
    Those fields remain ``None`` and are named in ``missing_provenance``;
    inventing a timestamp would make an old frame appear calibration-safe.
    """

    heartbeat = read_csv_member(session, "heartbeat_log.csv")
    rows_by_heartbeat = {
        _int(row.get("heartbeat_idx")): row
        for row in (heartbeat.rows if heartbeat is not None else ())
    }
    view = read_csv_member(session, "rheed_view_events.csv")
    view_rows = sorted(
        (dict(row) for row in (view.rows if view is not None else ())),
        key=lambda row: (_float(row.get("elapsed_s")) or 0.0, _int(row.get("event_idx")) or 0),
    )
    view_position = 0
    state: dict[str, Any] = {
        "view_segment_id": _int(session.metadata.get("view_segment_id")),
        "visual_history_generation": _int(session.metadata.get("visual_history_generation")),
        "gun_aligned": _bool(session.metadata.get("gun_aligned")),
        "realignment_active": _bool(session.metadata.get("realignment_active")),
    }
    contexts: list[dict[str, Any]] = []
    for frame in session.frames:
        while view_position < len(view_rows):
            view_elapsed = _float(view_rows[view_position].get("elapsed_s"))
            if view_elapsed is not None and view_elapsed > frame.elapsed_s + 1e-9:
                break
            item = view_rows[view_position]
            for key in ("view_segment_id", "visual_history_generation"):
                parsed = _int(item.get(key))
                if parsed is not None:
                    state[key] = parsed
            parsed_aligned = _bool(item.get("gun_aligned"))
            if parsed_aligned is not None:
                state["gun_aligned"] = parsed_aligned
            event_type = str(item.get("event_type", ""))
            if event_type == "realign_start":
                state["realignment_active"] = True
            elif event_type in {"realign_end", "alignment_confirmed"}:
                state["realignment_active"] = False
            view_position += 1
        row = rows_by_heartbeat.get(frame.heartbeat_idx, {})
        received = _int(
            row.get("captured_monotonic_ns")
            or row.get("received_monotonic_ns")
        )
        row_view_segment = _int(row.get("view_segment_id"))
        row_generation = _int(row.get("visual_history_generation"))
        row_aligned = _bool(row.get("gun_aligned"))
        row_realigning = _bool(row.get("realignment_active"))
        context: dict[str, Any] = {
            "frame_index": frame.frame_index + 1,
            "heartbeat_idx": frame.heartbeat_idx,
            "capture_sequence": frame.capture_sequence,
            "frame_path": frame.frame_name,
            "archive_member": frame.member,
            "captured_at_utc": frame.captured_at_utc,
            "received_monotonic_ns": received,
            "captured_monotonic_ns": received,
            "frame_age_ms": _float(row.get("frame_age_ms")),
            "source_hwnd": _int(row.get("source_hwnd")),
            "capture_backend": frame.capture_backend,
            "capture_geometry_id": frame.capture_geometry_id,
            "camera_width": _int(row.get("frame_width") or row.get("camera_width")),
            "camera_height": _int(row.get("frame_height") or row.get("camera_height")),
            "session_id": str(
                row.get("session_id")
                or session.metadata.get("session_id")
                or session.path.stem
            ),
            "view_segment_id": row_view_segment if row_view_segment is not None else state.get("view_segment_id"),
            "visual_history_generation": row_generation if row_generation is not None else state.get("visual_history_generation"),
            "gun_aligned": row_aligned if row_aligned is not None else state.get("gun_aligned"),
            "realignment_active": row_realigning if row_realigning is not None else state.get("realignment_active"),
            "calibration_id": str(row.get("calibration_id", "")),
            "basis_bundle_id": str(row.get("basis_bundle_id", "")),
        }
        required = (
            "received_monotonic_ns", "source_hwnd", "view_segment_id",
            "visual_history_generation", "gun_aligned", "realignment_active",
        )
        missing = [key for key in required if context.get(key) is None]
        context["provenance_complete"] = not missing
        context["missing_provenance"] = missing
        contexts.append(context)
    return contexts


def _session_anchor_identity(session: SessionArchive) -> str:
    return str(
        session.metadata.get("session_id")
        or session.path.stem
        or session.sha256
    )


def frame_anchor(
    frame: FrameRecord, *, session_identity: str = "",
) -> dict[str, Any]:
    """Return a serializable saved-frame anchor (one-based ordinal).

    Session/capture/path-or-member fields are the primary identity.  A frame
    hash is carried only when it already exists on ``frame``; constructing an
    annotation anchor never reads image bytes merely to create a digest.
    """

    frame_hash = str(frame.frame_sha256 or "").strip().lower()

    return {
        "session_identity": str(session_identity or ""),
        "frame_index": frame.frame_index + 1,
        "heartbeat_idx": frame.heartbeat_idx,
        "elapsed_s": frame.elapsed_s,
        "captured_at_utc": frame.captured_at_utc,
        "capture_sequence": frame.capture_sequence,
        "frame_path": frame.frame_name,
        "frame_name": frame.frame_name,
        "image_sha256": frame_hash,
        "image_sha256_algorithm": "raw-file-bytes-v1" if frame_hash else "",
        "archive_member": frame.member,
    }


def _nearest_saved_frame(
    frames: Sequence[FrameRecord], *, capture_sequence: int | None,
    elapsed_s: float | None, captured_at_utc: str,
) -> FrameRecord:
    exact = [frame for frame in frames if capture_sequence is not None and frame.capture_sequence == capture_sequence]
    if len(exact) == 1:
        return exact[0]
    if elapsed_s is not None:
        return min(frames, key=lambda frame: (abs(frame.elapsed_s - elapsed_s), frame.frame_index))
    target = _utc(captured_at_utc)
    if target:
        parsed = datetime.fromisoformat(target.replace("Z", "+00:00"))
        return min(
            frames,
            key=lambda frame: abs(
                (datetime.fromisoformat(frame.captured_at_utc.replace("Z", "+00:00")) - parsed).total_seconds()
            ),
        )
    return frames[0]


def _source_frame_anchor(
    session: SessionArchive, row: Mapping[str, str], review: FrameRecord,
    *, explicit_member: str | None = None, allow_sequence_match: bool = True,
) -> dict[str, Any]:
    sequence = _int(row.get("capture_sequence"))
    member = explicit_member or resolve_archived_path(session, str(row.get("frame_path", "")))
    payload_hash = str(
        row.get("image_sha256") or row.get("frame_sha256") or ""
    ).strip().lower()
    exact_saved = [
        frame for frame in session.frames
        if (member and frame.member == member) or (
            allow_sequence_match
            and
            sequence is not None and frame.capture_sequence == sequence
            and (
                not payload_hash
                or not frame.frame_sha256
                or frame.frame_sha256 == payload_hash
            )
        )
    ]
    if len(exact_saved) == 1:
        return frame_anchor(
            exact_saved[0], session_identity=_session_anchor_identity(session),
        )
    return {
        "session_identity": _session_anchor_identity(session),
        "frame_index": None,
        "heartbeat_idx": None,
        "elapsed_s": _float(row.get("elapsed_s")),
        # The acquisition-event timestamp is not a camera timestamp.  Keep it
        # exclusively in source.original_at_utc; an anchor capture time is
        # populated only when the source row actually recorded one.
        "captured_at_utc": _utc(row.get("captured_at_utc")),
        "capture_sequence": sequence,
        "frame_path": str(row.get("frame_path", "")),
        "frame_name": PurePosixPath(member).name if member else str(row.get("frame_path", "")).replace("\\", "/").rsplit("/", 1)[-1],
        "image_sha256": payload_hash,
        "image_sha256_algorithm": "raw-file-bytes-v1" if payload_hash else "",
        "archive_member": member or "",
        "review_fallback_frame_index": review.frame_index + 1,
        "association_status": "unresolved_saved_frame",
    }


def deterministic_event_id(
    *, session_identity: str, source: str, source_file: str,
    source_sequence: int, source_event_idx: object, event_time: object,
    capture_sequence: object, source_row_sha256: str,
) -> str:
    """Build a stable UUID from the full legacy identity, never event_idx alone."""

    if source not in {"manual", "auto_capture"}:
        raise ValueError("Only imported source events receive deterministic IDs")
    identity = {
        "session_identity": str(session_identity), "source": source,
        "source_file": str(source_file), "source_sequence": int(source_sequence),
        "source_event_idx": str(source_event_idx or ""),
        "event_time": str(event_time or ""),
        "capture_sequence": str(capture_sequence or ""),
        "source_row_sha256": str(source_row_sha256),
    }
    return str(uuid.uuid5(_EVENT_NAMESPACE, _canonical_json(identity).decode("utf-8")))


def new_posthoc_event_id() -> str:
    return str(uuid.uuid4())


def _source_evidence(
    session: SessionArchive, csv_member: CsvMember, source_sequence: int,
    row: Mapping[str, str], *, source_kind: str = "manual",
) -> dict[str, Any]:
    canonical_row = {str(key): str(value or "") for key, value in row.items()}
    stable_hash = source_identity_sha256(
        canonical_row, source_kind=source_kind,
    )
    result: dict[str, Any] = {
        "source_file": csv_member.member,
        "source_file_sha256": csv_member.sha256,
        "source_index": str(row.get("event_idx") or source_sequence),
        "source_sequence": source_sequence,
        "source_event_idx": str(row.get("event_idx", "")),
        "source_row_sha256": stable_hash,
        "source_row_hash_algorithm": (
            "sha256-canonical-json-auto-immutable-v1"
            if source_kind == "auto_capture"
            else "sha256-canonical-json-row-v1"
        ),
        "source_row_full_sha256": _sha256_json(canonical_row),
        "session_archive_sha256": session.sha256,
    }
    if source_kind == "auto_capture":
        pending = dict(canonical_row)
        pending.update({"event_state": "pending", "state_changed_at": ""})
        result["compatible_legacy_source_row_sha256"] = sorted({
            result["source_row_full_sha256"], _sha256_json(pending),
        })
        result["source_decision"] = {
            "event_state": canonical_row.get("event_state", ""),
            "state_changed_at": canonical_row.get("state_changed_at", ""),
            "read_only": True,
        }
    return result


def _auto_frame_member(
    session: SessionArchive, row: Mapping[str, str], sequence: int | None,
) -> str | None:
    buffer_dir = str(row.get("buffer_dir", "")).strip().replace("\\", "/").strip("/")
    if not buffer_dir:
        return None
    suffix = f"{buffer_dir}/capture_manifest.csv"
    manifest_member = optional_member(session, suffix)
    if manifest_member is None:
        return None
    with zipfile.ZipFile(session.path) as archive:
        manifest_rows = list(csv.DictReader(io.StringIO(archive.read(manifest_member).decode("utf-8-sig"), newline="")))
    if not manifest_rows:
        return None
    candidates = [item for item in manifest_rows if _int(item.get("capture_sequence")) == sequence]
    if len(candidates) != 1:
        return None
    chosen = candidates[0]
    parent = PurePosixPath(manifest_member).parent
    member = str(parent / str(chosen.get("frame_path", "")))
    return member if member in session.members else None


def _base_source_event(
    session: SessionArchive, csv_member: CsvMember, source: str,
    source_sequence: int, row: Mapping[str, str], *, member: str | None = None,
) -> dict[str, Any]:
    session_identity = _session_anchor_identity(session)
    elapsed = _float(row.get("elapsed_s"))
    sequence = _int(row.get("capture_sequence"))
    # timestamp is when the grower/software recorded the event.  A distinct
    # captured_at_utc belongs to the referenced image and must not replace it.
    event_utc = _utc(row.get("timestamp"))
    review_frame = _nearest_saved_frame(
        session.frames, capture_sequence=sequence, elapsed_s=elapsed,
        captured_at_utc=event_utc,
    )
    evidence = _source_evidence(
        session, csv_member, source_sequence, row, source_kind=source,
    )
    identifier = deterministic_event_id(
        session_identity=session_identity,
        source=source, source_file=csv_member.member,
        source_sequence=source_sequence, source_event_idx=row.get("event_idx"),
        event_time=event_utc or row.get("timestamp"),
        capture_sequence=row.get("capture_sequence"),
        source_row_sha256=evidence["source_row_sha256"],
    )
    original_comment = str(row.get("note", "") or "") if source == "manual" else ""
    original_anchor = _source_frame_anchor(
        session, row, review_frame, explicit_member=member,
        allow_sequence_match=source != "auto_capture",
    )
    return {
        "schema": SCHEMA_VERSION,
        "event_id": identifier,
        "source": {
            "kind": source,
            "session_identity": session_identity,
            **evidence,
            "original_at_utc": event_utc,
            "original_elapsed_s": elapsed,
            "original_anchor": original_anchor,
            "original_note": original_comment,
            "legacy_imports": [],
        },
        "review": {
            "anchor": frame_anchor(
                review_frame, session_identity=session_identity,
            ),
            "representative_anchor": None,
            "labels": [],
            "candidate_decision": (
                "pending" if source == "auto_capture" else "confirmed"
            ),
            "comment": original_comment,
            "disposition": "active",
            "disposition_reason": "",
        },
        "status": "Draft",
        "revision_id": "",
    }


def _anchor_archive_member(
    session: SessionArchive, anchor: Mapping[str, Any],
) -> str | None:
    member = str(anchor.get("archive_member", "") or "")
    if member in session.members:
        return member
    path_member = resolve_archived_path(
        session, str(anchor.get("frame_path", "") or ""),
    )
    if path_member is not None:
        return path_member
    frame_index = _int(anchor.get("frame_index"))
    if frame_index is None or not 1 <= frame_index <= len(session.frames):
        return None
    candidate = session.frames[frame_index - 1]
    if (
        candidate.member in session.members
        and candidate.capture_sequence == _int(anchor.get("capture_sequence"))
    ):
        return candidate.member
    return None


def _apply_legacy_event_labels(
    events: list[dict[str, Any]], csv_member: CsvMember | None,
) -> list[dict[str, Any]]:
    """Retain old row-style labels as evidence without inventing v3 points.

    ``primary_reconstruction`` and ``change_from/change_to`` do not say which
    reconstruction appeared or disappeared at one exact instant.  They are
    therefore offered for manual association, never silently promoted to v3.
    """
    if csv_member is None:
        return []
    by_index: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event["source"]["kind"] == "auto_capture":
            by_index.setdefault(str(event["source"]["source_event_idx"]), []).append(event)
    unlinked: list[dict[str, Any]] = []
    for source_sequence, row in enumerate(csv_member.rows, 1):
        candidates = by_index.get(str(row.get("event_idx", "")), [])
        unlinked.append({
            "source_file": csv_member.member,
            "source_file_sha256": csv_member.sha256,
            "source_sequence": source_sequence,
            "source_row_sha256": _sha256_json(dict(row)),
            "reason": (
                "legacy row label has no unambiguous v3 appeared/disappeared semantics"
                if len(candidates) == 1
                else "event_idx did not uniquely identify one auto-capture event"
            ),
            "candidate_event_id": (
                str(candidates[0]["event_id"]) if len(candidates) == 1 else ""
            ),
            "event_idx": str(row.get("event_idx", "")),
            "legacy_row": copy.deepcopy(dict(row)),
            "read_only": True,
        })
    return unlinked


def _legacy_live_labels(
    _session: SessionArchive,
    _events: list[dict[str, Any]],
    csv_member: CsvMember | None,
) -> list[dict[str, Any]]:
    if csv_member is None:
        return []
    # live_labels.csv is an Equalizer-era v1 artifact.  V2 deliberately has
    # no Equalizer field, so retain every row as read-only evidence instead of
    # injecting its measurement into a semantic human label.
    return [
        {
            "source_file": csv_member.member,
            "source_file_sha256": csv_member.sha256,
            "source_sequence": source_sequence,
            "source_row_sha256": _sha256_json(dict(row)),
            "reason": "legacy Equalizer label is read-only in point-events v3",
            "legacy_row": copy.deepcopy(dict(row)),
            "read_only": True,
        }
        for source_sequence, row in enumerate(csv_member.rows, 1)
    ]

def _reference_events(session: SessionArchive, csv_member: CsvMember | None) -> list[dict[str, Any]]:
    if csv_member is None:
        return []
    result: list[dict[str, Any]] = []
    for source_sequence, row in enumerate(csv_member.rows, 1):
        elapsed = _float(row.get("elapsed_s"))
        sequence = _int(row.get("capture_sequence"))
        event_utc = _utc(row.get("timestamp"))
        frame = _nearest_saved_frame(
            session.frames, capture_sequence=sequence, elapsed_s=elapsed,
            captured_at_utc=event_utc,
        )
        result.append({
            "reference_id": str(uuid.uuid5(
                _EVENT_NAMESPACE,
                f"reference|{session.sha256}|{csv_member.member}|{source_sequence}|{_sha256_json(dict(row))}",
            )),
            "event_type": str(row.get("event_type", "")),
            "event_at_utc": event_utc,
            "elapsed_s": elapsed,
            "anchor": frame_anchor(
                frame, session_identity=_session_anchor_identity(session),
            ),
            "note": str(row.get("note", "") or row.get("qc_reason", "") or ""),
            "source_evidence": _source_evidence(session, csv_member, source_sequence, row),
            "read_only": True,
        })
    return result


def _sensor_context(csv_member: CsvMember | None) -> dict[str, Any]:
    if csv_member is None:
        return {"source_file": "", "source_file_sha256": "", "columns": [], "rows": []}
    columns = list(csv_member.rows[0].keys()) if csv_member.rows else []
    return {
        "source_file": csv_member.member,
        "source_file_sha256": csv_member.sha256,
        "columns": columns,
        "rows": [dict(row) for row in csv_member.rows],
        "read_only": True,
    }


def _live_record_hash(record: Mapping[str, Any]) -> str:
    return _sha256_json({
        key: value for key, value in record.items() if key != "record_sha256"
    })


def _read_archived_jsonl(
    session: SessionArchive, suffix: str,
) -> tuple[str, str, list[dict[str, Any]]] | None:
    member = optional_member(session, suffix)
    if member is None:
        return None
    payload = read_archived_bytes(session, member)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PointEventValidationError(f"archived {suffix} is not UTF-8") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PointEventValidationError(
                f"archived {suffix} line {line_number} is invalid"
            ) from exc
        if not isinstance(value, dict):
            raise PointEventValidationError(
                f"archived {suffix} line {line_number} is not an object"
            )
        records.append(value)
    return member, hashlib.sha256(payload).hexdigest(), records


def _replay_archived_live_journal(
    records: Sequence[Mapping[str, Any]],
    *, summary_events: object = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    states: dict[str, dict[str, Any]] = {}
    summary_is_prefix = summary_events == []
    revision_ids: set[str] = set()
    schemas: set[str] = set()
    previous_record_hash = ""
    for raw in records:
        record = copy.deepcopy(dict(raw))
        schema = str(record.get("schema") or "")
        schemas.add(schema)
        if schema not in {
            SCHEMA_VERSION, V2_SCHEMA_VERSION, LEGACY_SCHEMA_VERSION,
        } or len(schemas) > 1:
            raise PointEventValidationError("archived live revision schema is invalid")
        if str(record.get("record_sha256", "")).lower() != _live_record_hash(record):
            raise PointEventValidationError("archived live revision hash mismatch")
        if schema in {SCHEMA_VERSION, V2_SCHEMA_VERSION} and str(
            record.get("previous_record_sha256", "")
        ) != previous_record_hash:
            raise PointEventValidationError("archived live global hash chain is broken")
        revision_id = str(record.get("revision_id", ""))
        event_id = str(record.get("event_id", ""))
        try:
            uuid.UUID(revision_id)
            uuid.UUID(event_id)
        except ValueError as exc:
            raise PointEventValidationError("archived live revision UUID is invalid") from exc
        if revision_id in revision_ids:
            raise PointEventValidationError("archived live revision UUID is duplicated")
        revision_ids.add(revision_id)
        before = record.get("before")
        after = record.get("after")
        previous = states.get(event_id)
        if not isinstance(after, dict) or str(after.get("event_id", "")) != event_id:
            raise PointEventValidationError("archived live after-state identity is invalid")
        if previous is None:
            if record.get("action") != "create" or before is not None:
                raise PointEventValidationError("archived live event has no create revision")
        else:
            if record.get("base_revision_id") != previous.get("revision_id"):
                raise PointEventValidationError("archived live revision chain is broken")
            if before != previous:
                raise PointEventValidationError("archived live before-state is inconsistent")
            if after.get("source") != previous.get("source"):
                raise PointEventValidationError("archived live source evidence changed")
        if str(after.get("revision_id", "")) != revision_id:
            raise PointEventValidationError("archived live state revision UUID is invalid")
        try:
            validate_live_revision_transition(
                previous,
                after,
                action=str(record.get("action") or ""),
                actor=str(record.get("actor") or ""),
            )
        except LivePointEventIntegrityError as exc:
            raise PointEventValidationError(
                f"archived live revision semantics are invalid: {exc}"
            ) from exc
        states[event_id] = copy.deepcopy(after)
        previous_record_hash = str(record.get("record_sha256", ""))
        if summary_events == [states[key] for key in sorted(states)]:
            summary_is_prefix = True
    return states, summary_is_prefix


def _resolve_live_review_anchor(
    session: SessionArchive, state: Mapping[str, Any],
    legacy_event: Mapping[str, Any] | None,
) -> dict[str, Any]:
    review = state.get("review")
    raw_anchor = review.get("anchor") if isinstance(review, Mapping) else None
    if not isinstance(raw_anchor, Mapping):
        # A live manual/automatic event can be committed while its exact
        # trigger frame is unresolved (for example, no unique buffered frame
        # matched the capture sequence).  The immutable source remains
        # unresolved, but post-processing still needs a real saved frame on
        # which the grower can finish the Draft.  The deterministic legacy
        # importer already selected that nearest saved review frame without
        # pretending it was the original acquisition evidence.
        if legacy_event is not None:
            fallback = legacy_event.get("review", {}).get("anchor")
            if isinstance(fallback, Mapping):
                return copy.deepcopy(dict(fallback))
        raise PointEventValidationError("archived live event has no saved review anchor")

    expected_session = _session_anchor_identity(session)
    supplied_session = str(raw_anchor.get("session_identity", "") or "")
    compatible_sessions = {
        expected_session,
        str(session.metadata.get("session_id", "") or ""),
        session.path.stem,
        session.sha256,
        f"sha256:{session.sha256}",
        str(state.get("source", {}).get("session_identity", "") or ""),
    } - {""}
    session_matches = (
        not supplied_session or supplied_session in compatible_sessions
    )
    frame_index = _int(raw_anchor.get("frame_index"))
    sequence = _int(raw_anchor.get("capture_sequence"))
    supplied_member = str(raw_anchor.get("archive_member", "") or "")
    member = None
    if supplied_member:
        member = (
            supplied_member
            if supplied_member in session.members
            else resolve_archived_path(session, supplied_member)
        )
    supplied_path = str(raw_anchor.get("frame_path", "") or "")
    path_member = resolve_archived_path(session, supplied_path)
    path_name = supplied_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    image_hash = str(raw_anchor.get("image_sha256", "")).lower()
    matches = [
        frame for frame in session.frames
        if session_matches
        and (frame_index is None or frame.frame_index + 1 == frame_index)
        and (sequence is None or frame.capture_sequence == sequence)
        and (
            not supplied_member
            or member is not None and frame.member == member
        )
        and (
            not supplied_path
            or path_member is not None and frame.member == path_member
            or path_member is None and frame.frame_name.lower() == path_name
        )
        and (
            not image_hash
            or not frame.frame_sha256
            or frame.frame_sha256 == image_hash
        )
    ]
    if len(matches) == 1:
        resolved = frame_anchor(
            matches[0], session_identity=expected_session,
        )
        if image_hash and not resolved["image_sha256"]:
            resolved["image_sha256"] = image_hash
            resolved["image_sha256_algorithm"] = str(
                raw_anchor.get("image_sha256_algorithm", "")
                or "raw-file-bytes-v1"
            )
        return resolved
    # Live labels may use a separately saved BMP that is not one of the 1 Hz
    # heartbeat frames.  The caller preserves that evidence, clears any fit
    # bound to it, and reopens a Draft on an actual report frame.
    if legacy_event is not None:
        return copy.deepcopy(legacy_event["review"]["anchor"])
    frame = _nearest_saved_frame(
        session.frames,
        capture_sequence=sequence,
        elapsed_s=_float(raw_anchor.get("elapsed_s")),
        captured_at_utc=str(raw_anchor.get("captured_at_utc", "")),
    )
    return frame_anchor(frame, session_identity=expected_session)


def _merge_later_legacy_review(
    state: dict[str, Any],
    legacy: Mapping[str, Any],
    event_records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply Events-tab labels written after live Draft creation field-wise."""

    first_after = event_records[0].get("after") if event_records else None
    first_review = first_after.get("review") if isinstance(first_after, Mapping) else {}
    if not isinstance(first_review, Mapping):
        first_review = {}
    current_review = state["review"]
    legacy_review = legacy["review"]
    conflicts: list[dict[str, Any]] = []
    applied: list[str] = []
    for field in (
        "comment", "reviewer", "confidence", "human_reconstruction",
        "change_from", "change_to", "equalizer",
    ):
        legacy_value = legacy_review.get(field)
        initial_value = first_review.get(field)
        if legacy_value == initial_value:
            continue
        if current_review.get(field) == initial_value:
            current_review[field] = copy.deepcopy(legacy_value)
            applied.append(field)
        elif current_review.get(field) != legacy_value:
            conflicts.append({
                "source_file": legacy["source"].get("source_file", ""),
                "source_row_sha256": next((
                    item.get("source_row_sha256", "")
                    for item in legacy["source"].get("legacy_imports", [])
                    if item.get("kind") == "events_labels"
                ), ""),
                "event_id": state["event_id"],
                "field": field,
                "reason": "newer live journal review retained over legacy Events-tab value",
            })
    if applied:
        for item in state["source"].get("legacy_imports", []):
            if item.get("kind") == "events_labels":
                item["applied_review_fields"] = list(applied)
    return conflicts


def _import_archived_live_events(
    session: SessionArchive, legacy_events: Sequence[Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]
] | None:
    journal = _read_archived_jsonl(session, "rheed_event_revisions.jsonl")
    summary_member = optional_member(session, "rheed_point_events.json")
    pending_member = optional_member(session, ".rheed_event_revision.pending.json")
    if journal is None:
        if summary_member is not None or pending_member is not None:
            raise PointEventValidationError(
                "archived point-event summary/transaction exists without its journal"
            )
        return None
    journal_member, journal_sha, records = journal
    pending_recovered = False
    if pending_member is not None:
        pending = json.loads(read_archived_bytes(session, pending_member).decode("utf-8-sig"))
        journal_schema = str(records[0].get("schema") or "") if records else ""
        if (
            not isinstance(pending, dict)
            or pending.get("schema") not in {
                SCHEMA_VERSION, V2_SCHEMA_VERSION, LEGACY_SCHEMA_VERSION,
            }
            or (journal_schema and pending.get("schema") != journal_schema)
        ):
            raise PointEventValidationError("archived pending live revision is invalid")
        if str(pending.get("record_sha256", "")).lower() != _live_record_hash(pending):
            raise PointEventValidationError("archived pending live revision hash mismatch")
        matches = [item for item in records if item.get("revision_id") == pending.get("revision_id")]
        if len(matches) > 1 or (matches and matches[0] != pending):
            raise PointEventValidationError("archived pending live revision conflicts with journal")
        if not matches:
            records.append(pending)
            pending_recovered = True
    # The replaceable summary is checked against journal replay.  A recovered
    # pending transaction or a crash after journal fsync can leave it one or
    # more records behind.  A valid journal is authoritative, but the summary
    # must still equal an actual journal prefix (never extra/tampered state).
    summary_sha = ""
    summary_events: object = None
    if summary_member is not None:
        payload = read_archived_bytes(session, summary_member)
        summary_sha = hashlib.sha256(payload).hexdigest()
        try:
            summary = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PointEventValidationError("archived point-event summary is invalid") from exc
        summary_events = summary.get("events") if isinstance(summary, Mapping) else None
        if not isinstance(summary_events, list):
            raise PointEventValidationError("archived point-event summary has no event list")
    states, summary_is_prefix = _replay_archived_live_journal(
        records, summary_events=summary_events,
    )
    if summary_member is not None and not summary_is_prefix:
        raise PointEventValidationError(
            "archived point-event summary is not a valid journal prefix"
        )

    journal_schema = str(records[0].get("schema") or "") if records else ""
    if journal_schema in {LEGACY_SCHEMA_VERSION, V2_SCHEMA_VERSION}:
        # V1/v2 have been fully hash/semantic replay validated above.  Their
        # label vocabularies include scientific meanings which no longer
        # exist in v3 (notably v2 ``surface_quality`` and its initial quality
        # choice).  Keep every source state and journal row byte-for-byte as
        # read-only evidence; build new editable v3 candidates solely from
        # the immutable acquisition CSVs.
        metadata = {
            "member": journal_member, "sha256": journal_sha,
            "schema": journal_schema,
            "summary_member": summary_member or "",
            "summary_sha256": summary_sha,
            "pending_recovered_in_memory": pending_recovered,
            "deterministic_csv_fallback_count": len(legacy_events),
            "authoritative": True, "read_only": True,
        }
        evidence = [{
            "event_id": event_id,
            "reason": f"archived {journal_schema} state retained read-only",
            "source_schema": journal_schema,
            "legacy_state": copy.deepcopy(state),
            "read_only": True,
        } for event_id, state in sorted(states.items())]
        return (
            [copy.deepcopy(dict(item)) for item in legacy_events],
            [copy.deepcopy(dict(item)) for item in records],
            metadata,
            evidence,
        )

    if journal_schema == SCHEMA_VERSION:
        by_source_hash: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for candidate in legacy_events:
            key = (
                str(candidate["source"]["kind"]),
                str(candidate["source"].get("source_row_sha256", "")).lower(),
            )
            by_source_hash.setdefault(key, []).append(candidate)
        imported: list[dict[str, Any]] = []
        consumed: set[str] = set()
        for event_id, raw_state in states.items():
            state = copy.deepcopy(raw_state)
            source = state.get("source", {})
            kind = str(source.get("kind", ""))
            fallback: Mapping[str, Any] | None = None
            if kind in {"manual", "auto_capture", "initial_assumption"}:
                candidates = by_source_hash.get((
                    kind, str(source.get("source_row_sha256", "")).lower(),
                ), [])
                if len(candidates) != 1:
                    raise PointEventValidationError(
                        "archived v3 event does not uniquely match immutable source evidence"
                    )
                fallback = candidates[0]
                consumed.add(str(fallback["event_id"]))
            elif kind != "posthoc":
                raise PointEventValidationError("archived v3 event source is invalid")
            state["review"] = copy.deepcopy(dict(state.get("review", {})))
            state["review"]["anchor"] = _resolve_live_review_anchor(
                session, state, fallback,
            )
            representative = raw_state.get("review", {}).get("representative_anchor")
            if representative is not None:
                representative_state = copy.deepcopy(state)
                representative_state["review"]["anchor"] = representative
                state["review"]["representative_anchor"] = _resolve_live_review_anchor(
                    session, representative_state, None,
                )
            for bookkeeping in ("created_at_utc", "updated_at_utc", "revision_number"):
                state.pop(bookkeeping, None)
            state["schema"] = SCHEMA_VERSION
            state["event_id"] = event_id
            imported.append(validate_event(state))
        for candidate in legacy_events:
            if str(candidate["event_id"]) not in consumed:
                if any(item["event_id"] == candidate["event_id"] for item in imported):
                    raise PointEventValidationError("v3 fallback event UUID conflicts")
                imported.append(copy.deepcopy(dict(candidate)))
        metadata = {
            "member": journal_member, "sha256": journal_sha,
            "schema": SCHEMA_VERSION, "summary_member": summary_member or "",
            "summary_sha256": summary_sha,
            "pending_recovered_in_memory": pending_recovered,
            "deterministic_csv_fallback_count": len(legacy_events) - len(consumed),
            "authoritative": True, "read_only": False,
        }
        return (
            imported, [copy.deepcopy(dict(item)) for item in records],
            metadata, [],
        )

    by_source_hash: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for event in legacy_events:
        key = (
            str(event["source"]["kind"]),
            str(event["source"].get("source_row_sha256", "")).lower(),
        )
        by_source_hash.setdefault(key, []).append(event)
    imported: list[dict[str, Any]] = []
    legacy_review_conflicts: list[dict[str, Any]] = []
    consumed: set[tuple[str, str]] = set()
    for event_id, raw_state in states.items():
        state = copy.deepcopy(raw_state)
        source = state.get("source")
        if not isinstance(source, Mapping):
            raise PointEventValidationError("archived live source evidence is missing")
        kind = str(source.get("kind", ""))
        legacy: Mapping[str, Any] | None = None
        if kind in {"manual", "auto_capture"}:
            journal_hash = str(source.get("source_row_sha256", "")).lower()
            key = (kind, journal_hash)
            candidates = by_source_hash.get(key, [])
            if not candidates and kind == "auto_capture":
                # Compatibility for journals created before mutable Keep /
                # Discard columns were excluded from source identity.
                candidates = [
                    event for event in legacy_events
                    if event["source"]["kind"] == "auto_capture"
                    and journal_hash in event["source"].get(
                        "compatible_legacy_source_row_sha256", []
                    )
                    and PurePosixPath(str(event["source"].get("source_file", ""))).name
                        == PurePosixPath(str(source.get("source_file", ""))).name
                    and str(event["source"].get("source_index", ""))
                        == str(source.get("source_index", ""))
                    and str(event["source"].get("original_at_utc", ""))
                        == _utc(source.get("original_at_utc"))
                    and _int(event["source"].get("original_anchor", {}).get("capture_sequence"))
                        == _int(source.get("capture_sequence"))
                ]
            stable_key = (
                kind,
                str(candidates[0]["source"].get("source_row_sha256", "")).lower(),
            ) if len(candidates) == 1 else key
            if len(candidates) != 1 or stable_key in consumed:
                raise PointEventValidationError(
                    "archived live event does not uniquely match its immutable source CSV row"
                )
            legacy = candidates[0]
            consumed.add(stable_key)
            # Retain the live source verbatim while adding archive-level
            # hashes/anchor as immutable verification metadata.
            state["source"] = {
                **copy.deepcopy(dict(source)),
                "archived_live_source_row_sha256": source.get("source_row_sha256", ""),
                "source_row_sha256": legacy["source"].get("source_row_sha256", ""),
                "source_row_hash_algorithm": legacy["source"].get(
                    "source_row_hash_algorithm", ""
                ),
                "archive_source_file": legacy["source"].get("source_file", ""),
                "archive_source_file_sha256": legacy["source"].get("source_file_sha256", ""),
                "session_archive_sha256": session.sha256,
                "original_anchor": copy.deepcopy(legacy["source"].get("original_anchor")),
                "legacy_imports": copy.deepcopy(legacy["source"].get("legacy_imports", [])),
                "source_decision": copy.deepcopy(
                    legacy["source"].get("source_decision")
                ),
            }
        elif kind != "posthoc":
            raise PointEventValidationError("archived live event source is not labelable")
        if not isinstance(state.get("review"), Mapping):
            raise PointEventValidationError("archived live review is missing")
        state["review"] = copy.deepcopy(dict(state["review"]))
        raw_anchor = copy.deepcopy(state["review"].get("anchor"))
        state["review"]["anchor"] = _resolve_live_review_anchor(session, state, legacy)
        if raw_anchor != state["review"]["anchor"]:
            old_equalizer = copy.deepcopy(state["review"].get("equalizer"))
            if isinstance(raw_anchor, Mapping):
                raw_path = str(raw_anchor.get("frame_path", ""))
                member = resolve_archived_path(session, raw_path)
                digest = str(raw_anchor.get("image_sha256", "")).lower()
                if member and digest:
                    actual = hashlib.sha256(read_archived_bytes(session, member)).hexdigest()
                    if actual == digest:
                        state["source"]["live_review_frame_evidence"] = {
                            "archive_member": member,
                            "image_sha256": digest,
                            "image_sha256_algorithm": "raw-file-bytes-v1",
                            "capture_sequence": raw_anchor.get("capture_sequence"),
                            "captured_at_utc": raw_anchor.get("captured_at_utc", ""),
                        }
            if old_equalizer is not None:
                legacy_review_conflicts.append({
                    "event_id": event_id,
                    "reason": (
                        "live Equalizer frame is not an exact saved report frame; "
                        "measurement invalidated and left unlinked"
                    ),
                    "original_review_anchor": raw_anchor,
                    "equalizer": old_equalizer,
                })
            state["review"]["equalizer"] = None
            state["review"]["equalizer_invalidation_reason"] = (
                "review anchor remapped to an actual saved report frame"
            )
            state["status"] = "Draft"
        if legacy is not None:
            event_records = [
                item for item in records if item.get("event_id") == event_id
            ]
            legacy_review_conflicts.extend(
                _merge_later_legacy_review(state, legacy, event_records)
            )
        # These writer bookkeeping fields remain available byte-for-byte in
        # source_revisions; the shared current-state DTO intentionally keeps
        # only the cross-live/offline schema fields.
        for bookkeeping in ("created_at_utc", "updated_at_utc", "revision_number"):
            state.pop(bookkeeping, None)
        state["schema"] = SCHEMA_VERSION
        state["event_id"] = event_id
        imported.append(validate_event(state))
    # A source CSV append is deliberately committed before the live journal.
    # If the GUI died between those writes, retain every verified journal UUID
    # and synthesize only the uncovered CSV rows as deterministic Drafts.
    # Ambiguous journal matches above still fail closed; this fallback never
    # guesses which duplicate row an existing journal event meant.
    journal_event_ids = {str(event["event_id"]) for event in imported}
    recovered_legacy = 0
    for event in legacy_events:
        key = (
            str(event["source"]["kind"]),
            str(event["source"].get("source_row_sha256", "")).lower(),
        )
        if key in consumed:
            continue
        if str(event["event_id"]) in journal_event_ids:
            raise PointEventValidationError(
                "deterministic fallback event UUID conflicts with archived live journal"
            )
        imported.append(copy.deepcopy(dict(event)))
        journal_event_ids.add(str(event["event_id"]))
        recovered_legacy += 1
    metadata = {
        "member": journal_member,
        "sha256": journal_sha,
        "summary_member": summary_member or "",
        "summary_sha256": summary_sha,
        "pending_recovered_in_memory": pending_recovered,
        "deterministic_csv_fallback_count": recovered_legacy,
        "authoritative": True,
    }
    return (
        imported,
        [copy.deepcopy(dict(item)) for item in records],
        metadata,
        legacy_review_conflicts,
    )


def _initial_assumption_event(session: SessionArchive) -> dict[str, Any]:
    """Return the deterministic audit item for the explicit initial state."""

    first = session.frames[0]
    session_identity = _session_anchor_identity(session)
    anchor = frame_anchor(first, session_identity=session_identity)
    row = {
        "kind": "initial_assumption", "default": "one_by_one",
        "capture_sequence": str(first.capture_sequence),
        "frame_index": first.frame_index + 1,
        "frame_path": first.frame_name,
        "archive_member": first.member,
    }
    identifier = deterministic_live_event_id(
        session_identity=session_identity, source_file="initial_assumption",
        source_index=0, original_at_utc=first.captured_at_utc,
        capture_sequence=first.capture_sequence,
        source_row_hash=source_identity_sha256(
            row, source_kind="initial_assumption",
        ),
    )
    return {
        "schema": SCHEMA_VERSION,
        "event_id": identifier,
        "source": {
            "kind": "initial_assumption",
            "session_identity": session_identity,
            "source_file": "initial_assumption",
            "source_index": "0",
            "source_sequence": 0,
            "source_event_idx": "0",
            "source_row_sha256": source_identity_sha256(
                row, source_kind="initial_assumption",
            ),
            "source_row_hash_algorithm": "sha256-canonical-json-row-v1",
            "source_file_sha256": "",
            "session_archive_sha256": session.sha256,
            "original_at_utc": first.captured_at_utc,
            "original_elapsed_s": first.elapsed_s,
            "original_anchor": copy.deepcopy(anchor),
            "original_note": "Initial state is 1x1 with unknown pattern clarity.",
            "legacy_imports": [],
        },
        "review": {
            "anchor": copy.deepcopy(anchor),
            "representative_anchor": None,
            "labels": [],
            "candidate_decision": "confirmed",
            "comment": "",
            "disposition": "active", "disposition_reason": "",
        },
        "status": "Draft", "revision_id": "",
    }


def import_point_events(
    session: SessionArchive,
    *,
    include_auto_events: bool = True,
) -> ImportedPointEvents:
    """Import labelable points and read-only context from one session ZIP.

    ``include_auto_events=False`` is a reversible review-mode choice.  It does
    not modify the immutable acquisition archive; it only omits automatic
    image-change proposals from the generated labeling report.
    """

    if not session.frames:
        raise PointEventValidationError("at least one saved frame is required")
    events: list[dict[str, Any]] = [_initial_assumption_event(session)]
    manual = read_csv_member(session, "manual_events.csv")
    if manual is not None:
        events.extend(
            _base_source_event(session, manual, "manual", index, row)
            for index, row in enumerate(manual.rows, 1)
        )
    auto = read_csv_member(session, "auto_capture_events.csv")
    if auto is not None:
        for index, row in enumerate(auto.rows, 1):
            sequence = _int(row.get("capture_sequence"))
            events.append(_base_source_event(
                session, auto, "auto_capture", index, row,
                member=_auto_frame_member(session, row, sequence),
            ))
    unlinked = _apply_legacy_event_labels(
        events, read_csv_member(session, "events_labels.csv"),
    )
    unlinked.extend(_legacy_live_labels(
        session, events, read_csv_member(session, "live_labels.csv"),
    ))
    archived_live = _import_archived_live_events(session, events)
    source_revisions: list[dict[str, Any]] = []
    source_journal: dict[str, Any] | None = None
    if archived_live is not None:
        events, source_revisions, source_journal, review_conflicts = archived_live
        unlinked.extend(review_conflicts)
    if not include_auto_events:
        events = [
            event for event in events
            if str(event.get("source", {}).get("kind", "")) != "auto_capture"
        ]
    events.sort(key=lambda event: (
        float(event["source"].get("original_elapsed_s")) if event["source"].get("original_elapsed_s") is not None else math.inf,
        str(event["source"]["kind"]), str(event["event_id"]),
    ))
    return ImportedPointEvents(
        tuple(copy.deepcopy(events)),
        tuple(_reference_events(session, read_csv_member(session, "rheed_view_events.csv"))),
        tuple(unlinked),
        _sensor_context(read_csv_member(session, "sensor_log.csv")),
        tuple(_frame_contexts(session)),
        tuple(source_revisions),
        source_journal,
    )


def make_posthoc_event(
    anchor: Mapping[str, Any], *, actor: str, at_utc: str | None = None,
) -> dict[str, Any]:
    """Create a Draft at an existing saved frame; UUIDs are intentionally random."""

    creator = str(actor or "").strip()
    if not creator:
        raise PointEventValidationError("posthoc event actor is required")
    normalized = validate_review_anchor(anchor)
    created = _utc(at_utc or datetime.now(timezone.utc).isoformat())
    identifier = new_posthoc_event_id()
    return {
        "schema": SCHEMA_VERSION,
        "event_id": identifier,
        "source": {
            "kind": "posthoc",
            "created_at_utc": created, "created_by": creator,
            "source_row_sha256": "", "source_file": "",
            "source_index": "", "session_identity": "",
            "original_at_utc": normalized["captured_at_utc"],
            "original_elapsed_s": normalized["elapsed_s"],
            "original_anchor": copy.deepcopy(normalized),
            "original_note": "", "legacy_imports": [],
        },
        "review": {
            "anchor": copy.deepcopy(normalized),
            "representative_anchor": None,
            "labels": [], "candidate_decision": "confirmed", "comment": "",
            "disposition": "active", "disposition_reason": "",
        },
        "status": "Draft", "revision_id": "",
    }


def validate_review_anchor(anchor: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(anchor, Mapping):
        raise PointEventValidationError("review anchor is required")
    try:
        frame_index = int(anchor["frame_index"])
        heartbeat = int(anchor["heartbeat_idx"])
        elapsed = float(anchor["elapsed_s"])
        sequence = int(anchor["capture_sequence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PointEventValidationError("review anchor provenance is invalid") from exc
    frame_hash = str(anchor.get("image_sha256", "")).strip().lower()
    algorithm = str(anchor.get("image_sha256_algorithm", "")).strip().lower()
    captured = _utc(anchor.get("captured_at_utc"))
    frame_path = str(anchor.get("frame_path", "") or "").strip()
    archive_member = str(anchor.get("archive_member", "") or "").strip()
    if frame_index < 1 or heartbeat < 0 or sequence < 0 or not math.isfinite(elapsed):
        raise PointEventValidationError("review anchor provenance is invalid")
    if not frame_path and not archive_member:
        raise PointEventValidationError(
            "review anchor requires a saved path or archive member"
        )
    if frame_hash:
        if len(frame_hash) != 64 or any(
            char not in "0123456789abcdef" for char in frame_hash
        ):
            raise PointEventValidationError(
                "review anchor SHA-256 evidence is invalid"
            )
        if not algorithm:
            algorithm = "raw-file-bytes-v1"
        if algorithm not in {"raw-file-bytes-v1", "sha256"}:
            raise PointEventValidationError(
                "review anchor hash evidence algorithm is incompatible"
            )
    elif algorithm:
        raise PointEventValidationError(
            "review anchor hash algorithm exists without hash evidence"
        )
    if not captured:
        raise PointEventValidationError("review anchor UTC is invalid")
    normalized = dict(anchor)
    normalized.update({
        "session_identity": str(anchor.get("session_identity", "") or ""),
        "frame_index": frame_index, "heartbeat_idx": heartbeat,
        "elapsed_s": elapsed, "captured_at_utc": captured,
        "frame_path": frame_path, "archive_member": archive_member,
        "capture_sequence": sequence, "image_sha256": frame_hash,
        "image_sha256_algorithm": algorithm,
    })
    return normalized


def validate_equalizer_measurement(
    measurement: Mapping[str, Any], anchor: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(measurement, Mapping) or measurement.get("schema_version", 1) != 1:
        raise PointEventValidationError("a versioned Equalizer measurement is required")
    result = copy.deepcopy(dict(measurement))
    if result.get("valid") is not True:
        raise PointEventValidationError("Equalizer measurement is not valid")
    for field in ("calibration_id", "basis_bundle_id"):
        if not str(result.get(field, "")).strip():
            raise PointEventValidationError(f"Equalizer {field} is required")
    if tuple(result.get("active_classes", ())) != ACTIVE_EQUALIZER_BASES:
        raise PointEventValidationError("Equalizer must contain the four active canonical bases")
    weight_sets = result.get("weights")
    if not isinstance(weight_sets, Mapping):
        raise PointEventValidationError("Equalizer raw/final/normalized weights are required")
    for set_name in ("raw", "final", "normalized"):
        values = weight_sets.get(set_name)
        if not isinstance(values, Mapping) or values.get("HTR", "not-null") is not None:
            raise PointEventValidationError(f"Equalizer {set_name} HTR must be null")
        if set(values) != {*ACTIVE_EQUALIZER_BASES, "HTR"}:
            raise PointEventValidationError(f"Equalizer {set_name} basis set is invalid")
        for label in ACTIVE_EQUALIZER_BASES:
            value = values[label]
            if value is None and set_name == "raw":
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise PointEventValidationError(f"Equalizer {set_name} {label} is invalid") from exc
            if not math.isfinite(numeric) or numeric < 0:
                raise PointEventValidationError(f"Equalizer {set_name} {label} is invalid")
    residual = _float(result.get("fit_residual"))
    coverage = _float(result.get("valid_coverage"))
    if residual is None or residual < 0:
        raise PointEventValidationError("Equalizer fit residual is invalid")
    if coverage is None or not 0 < coverage <= 1:
        raise PointEventValidationError("Equalizer coverage is invalid")
    if result.get("HTR", "not-null") is not None:
        raise PointEventValidationError("Equalizer HTR must be null")
    if _int(result.get("capture_sequence")) != int(anchor["capture_sequence"]):
        raise PointEventValidationError("Equalizer result belongs to a different capture sequence")
    frame_hash = str(result.get("frame_sha256", "")).strip().lower()
    if len(frame_hash) != 64 or any(
        character not in "0123456789abcdef" for character in frame_hash
    ):
        raise PointEventValidationError("Equalizer frame SHA-256 is invalid")
    algorithm = str(result.get("frame_sha256_algorithm", "")).strip().lower()
    anchor_hash = str(anchor["image_sha256"]).lower()
    if algorithm == "rgb-array-v1":
        raw_hash = str(result.get("raw_frame_sha256", "")).strip().lower()
        raw_algorithm = str(
            result.get("raw_frame_sha256_algorithm", "")
        ).strip().lower()
        if raw_hash != anchor_hash or raw_algorithm != "raw-file-bytes-v1":
            raise PointEventValidationError(
                "Equalizer RGB hash is not bound to the exact raw review frame"
            )
        if result.get("rgb_hash_verified_from_raw_frame") is not True:
            raise PointEventValidationError(
                "Equalizer RGB hash was not verified from the raw review frame"
            )
        result["raw_frame_sha256"] = raw_hash
        result["raw_frame_sha256_algorithm"] = "raw-file-bytes-v1"
        result["frame_sha256_algorithm"] = "rgb-array-v1"
    elif algorithm in {
        "", "raw-file-bytes-v1", "sha256-file-bytes", "sha256",
    }:
        if frame_hash != anchor_hash:
            raise PointEventValidationError("Equalizer result belongs to a different frame")
        result["frame_sha256_algorithm"] = "raw-file-bytes-v1"
    else:
        raise PointEventValidationError("Equalizer frame hash algorithm is incompatible")
    result["frame_sha256"] = frame_hash
    result["fit_residual"] = residual
    result["valid_coverage"] = coverage
    return result


def completion_errors(event: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    review = event.get("review")
    if not isinstance(review, Mapping):
        return ["review content is missing"]
    if not str(review.get("comment", "")).strip():
        errors.append("comment is required")
    if not str(review.get("reviewer", "")).strip():
        errors.append("reviewer is required")
    if review.get("disposition") != "active":
        errors.append("dismissed or deleted events cannot be completed")
    try:
        anchor = validate_review_anchor(review.get("anchor", {}))
        validate_equalizer_measurement(review.get("equalizer", {}), anchor)
    except PointEventValidationError as exc:
        errors.append(str(exc))
    return errors


def _immutable_projection(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(event.get(key))
        for key in ("event_id", "source")
    }


def _changed_review_fields(
    before: Mapping[str, Any], after: Mapping[str, Any],
) -> set[str]:
    keys = set(before) | set(after)
    return {key for key in keys if before.get(key) != after.get(key)}


def _validate_revision_transition(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    *,
    action: str,
    actor: str,
) -> None:
    """Validate the semantic operation recorded by one journal row.

    Hashes detect byte-level mutation.  This check separately prevents a
    validly re-hashed row from claiming one action while applying another.
    """

    if action not in REVISION_ACTIONS:
        raise PointEventValidationError("revision action is invalid")
    if not str(actor or "").strip():
        raise PointEventValidationError("revision actor is required")
    if before is None:
        if action != "create_posthoc":
            raise PointEventValidationError("new events require create_posthoc")
        if (
            after["source"]["kind"] != "posthoc"
            or after["status"] != "Draft"
            or after["review"].get("disposition") != "active"
            or after["review"].get("equalizer") is not None
            or after["source"].get("original_anchor") != after["review"].get("anchor")
        ):
            raise PointEventValidationError("posthoc create state is invalid")
        return

    if _immutable_projection(before) != _immutable_projection(after):
        raise PointEventValidationError("revision altered immutable source evidence")
    protected_before = {
        key: value for key, value in before.items()
        if key not in {"review", "status", "revision_id"}
    }
    protected_after = {
        key: value for key, value in after.items()
        if key not in {"review", "status", "revision_id"}
    }
    if protected_before != protected_after:
        raise PointEventValidationError("revision changed protected event metadata")
    before_review = before["review"]
    after_review = after["review"]
    changed = _changed_review_fields(before_review, after_review)
    before_status = before["status"]
    after_status = after["status"]

    if action == "edit":
        allowed = {
            "comment", "reviewer", "confidence", "human_reconstruction",
            "change_from", "change_to",
        }
        if not changed <= allowed or after_status != "Draft":
            raise PointEventValidationError("edit revision changed a protected field")
    elif action == "move_anchor":
        if (
            not changed <= {"anchor", "equalizer"}
            or after_review.get("equalizer") is not None
            or after_status != "Draft"
        ):
            raise PointEventValidationError("move_anchor transition is invalid")
    elif action == "set_equalizer":
        if (
            not changed <= {"equalizer"}
            or after_review.get("equalizer") is None
            or after_status != "Draft"
        ):
            raise PointEventValidationError("set_equalizer transition is invalid")
    elif action == "complete":
        if (
            changed
            or before_status != "Draft"
            or after_status != "Complete"
            or completion_errors(after)
        ):
            raise PointEventValidationError("complete transition is invalid")
    elif action == "reopen":
        if changed or before_status != "Complete" or after_status != "Draft":
            raise PointEventValidationError("reopen transition is invalid")
    elif action == "dismiss":
        if (
            before["source"]["kind"] == "posthoc"
            or not changed <= {"disposition", "disposition_reason"}
            or after_review.get("disposition") != "dismissed"
            or not str(after_review.get("disposition_reason", "")).strip()
            or after_status != "Draft"
        ):
            raise PointEventValidationError("dismiss transition is invalid")
    elif action == "undismiss":
        if (
            before["source"]["kind"] == "posthoc"
            or before_review.get("disposition") != "dismissed"
            or not changed <= {"disposition", "disposition_reason"}
            or after_review.get("disposition") != "active"
            or after_review.get("disposition_reason") != ""
            or after_status != "Draft"
        ):
            raise PointEventValidationError("undismiss transition is invalid")
    elif action == "delete":
        if (
            before["source"]["kind"] != "posthoc"
            or not changed <= {"disposition", "disposition_reason"}
            or after_review.get("disposition") != "deleted"
            or not str(after_review.get("disposition_reason", "")).strip()
            or after_status != "Draft"
        ):
            raise PointEventValidationError("delete transition is invalid")
    elif action == "restore":
        if (
            before["source"]["kind"] != "posthoc"
            or before_review.get("disposition") != "deleted"
            or not changed <= {"disposition", "disposition_reason"}
            or after_review.get("disposition") != "active"
            or after_review.get("disposition_reason") != ""
            or after_status != "Draft"
        ):
            raise PointEventValidationError("restore transition is invalid")


def validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise PointEventValidationError("event must be an object")
    result = copy.deepcopy(dict(event))
    try:
        uuid.UUID(str(result.get("event_id", "")))
    except ValueError as exc:
        raise PointEventValidationError("event_id must be a UUID") from exc
    source = result.get("source")
    if not isinstance(source, Mapping):
        raise PointEventValidationError("source evidence is required")
    source_kind = str(source.get("kind", ""))
    if source_kind not in SOURCE_TYPES:
        raise PointEventValidationError("event source is invalid")
    if source_kind != "posthoc" and not str(source.get("source_row_sha256", "")):
        raise PointEventValidationError("imported source row hash is required")
    review = result.get("review")
    if not isinstance(review, Mapping):
        raise PointEventValidationError("review content is required")
    result["review"] = copy.deepcopy(dict(review))
    result["review"]["anchor"] = validate_review_anchor(review.get("anchor", {}))
    status = str(result.get("status", ""))
    if status not in STATUSES:
        raise PointEventValidationError("event status is invalid")
    if status == "Complete":
        errors = completion_errors(result)
        if errors:
            raise PointEventValidationError("Complete event is invalid: " + "; ".join(errors))
    disposition = str(result["review"].get("disposition", ""))
    if disposition not in {"active", "dismissed", "deleted"}:
        raise PointEventValidationError("review disposition is invalid")
    if disposition == "deleted" and source_kind != "posthoc":
        raise PointEventValidationError("source events cannot be deleted")
    if disposition != "active" and not str(result["review"].get("disposition_reason", "")).strip():
        raise PointEventValidationError("dismissed source event requires a reason")
    if result["review"].get("equalizer") is not None:
        result["review"]["equalizer"] = validate_equalizer_measurement(
            result["review"]["equalizer"], result["review"]["anchor"],
        )
    return result


def make_revision(
    before: Mapping[str, Any] | None, after: Mapping[str, Any], *,
    action: str, actor: str, base_revision_id: str = "",
    at_utc: str | None = None, revision_id: str | None = None,
) -> dict[str, Any]:
    """Create a self-checking append-only revision DTO."""

    if action not in REVISION_ACTIONS:
        raise PointEventValidationError("revision action is invalid")
    actor_text = str(actor or "").strip()
    if not actor_text:
        raise PointEventValidationError("revision actor is required")
    after_copy = validate_event(after)
    before_copy = validate_event(before) if before is not None else None
    _validate_revision_transition(
        before_copy, after_copy, action=action, actor=actor_text,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "revision_id": revision_id or str(uuid.uuid4()),
        "event_id": after_copy["event_id"],
        "actor": actor_text,
        "at_utc": _utc(at_utc or datetime.now(timezone.utc).isoformat()),
        "action": action,
        "base_revision_id": str(base_revision_id or ""),
        "before_sha256": _sha256_json(before_copy) if before_copy is not None else "",
        "before": before_copy,
        "after": after_copy,
    }


def revise_event(
    event: Mapping[str, Any], *, action: str, actor: str,
    changes: Mapping[str, Any] | None = None, at_utc: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one semantic edit and return ``(new_state, journal_record)``."""

    before = validate_event(event)
    after = copy.deepcopy(before)
    changes = dict(changes or {})
    edit_fields = {
        "comment", "reviewer", "confidence", "human_reconstruction",
        "change_from", "change_to",
    }
    if action in {"edit", "set_equalizer", "move_anchor"}:
        allowed = (
            edit_fields if action == "edit"
            else {"equalizer"} if action == "set_equalizer"
            else {"anchor"}
        )
        if any(key not in allowed for key in changes):
            raise PointEventValidationError("revision contains a non-editable field")
        after["review"].update(copy.deepcopy(changes))
        if action == "move_anchor":
            after["review"]["anchor"] = validate_review_anchor(after["review"]["anchor"])
            after["review"]["equalizer"] = None
        after["status"] = "Draft"
    elif action == "complete":
        if changes:
            raise PointEventValidationError("Complete cannot include edits")
        errors = completion_errors(after)
        if errors:
            raise PointEventValidationError("Cannot complete event: " + "; ".join(errors))
        after["status"] = "Complete"
    elif action == "reopen":
        after["status"] = "Draft"
    elif action == "dismiss":
        if after["source"]["kind"] == "posthoc":
            raise PointEventValidationError("posthoc events are deleted, not dismissed")
        reason = str(changes.get("dismiss_reason", "")).strip()
        if not reason:
            raise PointEventValidationError("dismiss reason is required")
        after["review"].update({"disposition": "dismissed", "disposition_reason": reason})
        after["status"] = "Draft"
    elif action == "undismiss":
        after["review"].update({"disposition": "active", "disposition_reason": ""})
        after["status"] = "Draft"
    elif action == "delete":
        if after["source"]["kind"] != "posthoc":
            raise PointEventValidationError("source events cannot be deleted")
        reason = str(changes.get("dismiss_reason", "")).strip()
        if not reason:
            raise PointEventValidationError("delete reason is required")
        after["review"].update({"disposition": "deleted", "disposition_reason": reason})
        after["status"] = "Draft"
    elif action == "restore":
        if after["source"]["kind"] != "posthoc":
            raise PointEventValidationError("only posthoc events can be restored")
        after["review"].update({"disposition": "active", "disposition_reason": ""})
        after["status"] = "Draft"
    else:
        raise PointEventValidationError("create_posthoc is only valid for a new event")
    revision = make_revision(
        before, after, action=action, actor=actor,
        base_revision_id=str(before.get("revision_id", "")), at_utc=at_utc,
    )
    after["revision_id"] = revision["revision_id"]
    revision["after"] = copy.deepcopy(after)
    return validate_event(after), revision


def replay_revisions(
    initial_events: Iterable[Mapping[str, Any]],
    revisions: Iterable[Mapping[str, Any]],
    *,
    require_integrity: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fail closed while rebuilding materialized state from append-only rows."""

    state: dict[str, dict[str, Any]] = {}
    for event in initial_events:
        checked = validate_event(event)
        identifier = checked["event_id"]
        if identifier in state:
            raise PointEventValidationError("duplicate initial event_id")
        state[identifier] = checked
    records = list(revisions)
    if require_integrity:
        _validate_record_integrity(records)
    seen_revisions: set[str] = set()
    for raw in records:
        if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION:
            raise PointEventValidationError("revision schema is invalid")
        revision_id = str(raw.get("revision_id", ""))
        try:
            uuid.UUID(revision_id)
        except ValueError as exc:
            raise PointEventValidationError("revision_id must be a UUID") from exc
        if revision_id in seen_revisions:
            raise PointEventValidationError("duplicate revision_id")
        seen_revisions.add(revision_id)
        event_id = str(raw.get("event_id", ""))
        action = str(raw.get("action", ""))
        if not _utc(raw.get("at_utc")):
            raise PointEventValidationError("revision UTC is invalid")
        if action == "create_posthoc":
            if event_id in state or raw.get("before") is not None:
                raise PointEventValidationError("posthoc create revision conflicts with existing state")
            after = validate_event(raw.get("after", {}))
            _validate_revision_transition(
                None, after, action=action, actor=str(raw.get("actor", "")),
            )
        else:
            if event_id not in state:
                raise PointEventValidationError("revision targets an unknown event")
            before = state[event_id]
            if str(raw.get("base_revision_id", "")) != str(before.get("revision_id", "")):
                raise PointEventValidationError("revision base does not match current state")
            if str(raw.get("before_sha256", "")) != _sha256_json(before):
                raise PointEventValidationError("revision before hash does not match current state")
            supplied_before = validate_event(raw.get("before", {}))
            if supplied_before != before:
                raise PointEventValidationError("revision before snapshot does not match current state")
            after = validate_event(raw.get("after", {}))
            _validate_revision_transition(
                before, after, action=action, actor=str(raw.get("actor", "")),
            )
        if after["event_id"] != event_id:
            raise PointEventValidationError("revision event identity is inconsistent")
        after["revision_id"] = revision_id
        state[event_id] = after
    return copy.deepcopy(state)


# ---------------------------------------------------------------------------
# V2 semantic model.  These definitions intentionally supersede the v1-era
# helpers above; the older Equalizer validator remains available solely for
# forensic validation of archived v1 evidence.


def _current_labels(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise PointEventValidationError("review labels must be an array")
    result: list[dict[str, str]] = []
    identifiers: set[str] = set()
    meanings: set[tuple[str, str, str]] = set()
    reconstruction_values: set[str] = set()
    clarity_seen = False
    for raw in value:
        try:
            label = validate_semantic_label(raw)
        except (TypeError, ValueError) as exc:
            raise PointEventValidationError(str(exc)) from exc
        meaning = (label["kind"], label["change"], label["value"])
        if label["label_id"] in identifiers:
            raise PointEventValidationError("semantic label_id is duplicated")
        if meaning in meanings:
            raise PointEventValidationError("duplicate semantic label at one event")
        if label["kind"] == "pattern_clarity":
            if clarity_seen:
                raise PointEventValidationError(
                    "an event can contain only one pattern-clarity change"
                )
            clarity_seen = True
        elif label["value"] in reconstruction_values:
            raise PointEventValidationError(
                "a reconstruction cannot both appear and disappear at one event"
            )
        else:
            reconstruction_values.add(label["value"])
        identifiers.add(label["label_id"])
        meanings.add(meaning)
        result.append(label)
    return result


def completion_errors(event: Mapping[str, Any]) -> list[str]:
    review = event.get("review")
    if not isinstance(review, Mapping):
        return ["review content is missing"]
    errors: list[str] = []
    if review.get("disposition") != "active":
        errors.append("dismissed or deleted events cannot be completed")
    decision = str(review.get("candidate_decision", ""))
    source_kind = str(event.get("source", {}).get("kind", ""))
    if decision not in CANDIDATE_DECISIONS:
        errors.append("candidate decision is invalid")
    elif source_kind in CANDIDATE_SOURCES and decision == "pending":
        errors.append("candidate must be confirmed or rejected")
    if decision == "rejected":
        if source_kind not in CANDIDATE_SOURCES:
            errors.append("only automatic candidates can be rejected")
    else:
        try:
            labels = _current_labels(review.get("labels"))
        except PointEventValidationError as exc:
            errors.append(str(exc))
        else:
            if source_kind == "initial_assumption":
                if labels:
                    errors.append(
                        "initial state cannot contain semantic change labels"
                    )
            elif not labels:
                errors.append("at least one semantic label is required")
    return errors


def _anchor_frame_index(event: Mapping[str, Any], field: str) -> int | None:
    anchor = event.get("review", {}).get(field)
    if not isinstance(anchor, Mapping) or anchor.get("frame_index") in (None, ""):
        return None
    return int(anchor["frame_index"])


def _owns_state_interval(event: Mapping[str, Any]) -> bool:
    """Whether ``event`` owns a derived state interval after its boundary."""

    review = event.get("review", {})
    if not isinstance(review, Mapping) or review.get("disposition") != "active":
        return False
    if event.get("source", {}).get("kind") == "initial_assumption":
        return True
    return (
        review.get("candidate_decision") == "confirmed"
        and bool(review.get("labels"))
    )


def _representative_anchor_identity(
    anchor: Mapping[str, Any],
) -> tuple[str, str, str, str, str]:
    return (
        str(anchor.get("session_identity", "")),
        str(anchor.get("capture_sequence", "")),
        str(anchor.get("archive_member", "")),
        str(anchor.get("frame_index", "")),
        str(anchor.get("frame_path", "")),
    )


def interval_errors(
    event_id: str, state: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Validate the one independent Anchor for an event-owned state interval.

    Events on the same saved frame form one atomic boundary and therefore own
    one following interval together.  A journal may retain the same Anchor on
    more than one owner for compatibility; identical copies normalize to one
    interval Anchor, while conflicting copies fail closed.
    """

    event = state[event_id]
    review = event["review"]
    if review.get("candidate_decision") == "rejected":
        return []
    if not _owns_state_interval(event):
        return []
    boundary = _anchor_frame_index(event, "anchor")
    if boundary is None:
        return []  # completion_errors/validate_event reports the missing point

    source_kind = str(event.get("source", {}).get("kind", ""))
    if source_kind == "initial_assumption":
        owners = [event]
    else:
        owners = [
            other for other in state.values()
            if other.get("source", {}).get("kind") != "initial_assumption"
            and _owns_state_interval(other)
            and _anchor_frame_index(other, "anchor") == boundary
        ]
    raw_anchors = [
        owner.get("review", {}).get("representative_anchor")
        for owner in owners
        if isinstance(owner.get("review", {}).get("representative_anchor"), Mapping)
    ]
    anchors = {
        _representative_anchor_identity(anchor): anchor
        for anchor in raw_anchors
    }
    errors: list[str] = []
    if not anchors:
        return ["a representative interval frame is required"]
    if len(anchors) > 1:
        return ["events at one state boundary disagree about the interval Anchor"]
    representative = int(next(iter(anchors.values()))["frame_index"])
    if representative < boundary:
        errors.append("representative interval frame must be at or after the event anchor")
    later: list[int] = []
    for other_id, other in state.items():
        if other_id == event_id:
            continue
        if other.get("source", {}).get("kind") == "initial_assumption":
            continue
        if not _owns_state_interval(other):
            continue
        other_boundary = _anchor_frame_index(other, "anchor")
        if other_boundary is not None and other_boundary > boundary:
            later.append(other_boundary)
    if later and representative >= min(later):
        errors.append("representative interval frame must be before the next semantic event")
    return errors


def _immutable_projection(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(event.get(key)) for key in ("event_id", "source")}


def _changed_review_fields(
    before: Mapping[str, Any], after: Mapping[str, Any],
) -> set[str]:
    return {key for key in set(before) | set(after) if before.get(key) != after.get(key)}


def validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise PointEventValidationError("event must be an object")
    result = copy.deepcopy(dict(event))
    if result.get("schema") != SCHEMA_VERSION:
        raise PointEventValidationError("event schema is invalid")
    try:
        uuid.UUID(str(result.get("event_id", "")))
    except ValueError as exc:
        raise PointEventValidationError("event_id must be a UUID") from exc
    source = result.get("source")
    if not isinstance(source, Mapping):
        raise PointEventValidationError("source evidence is required")
    source_kind = str(source.get("kind", ""))
    if source_kind not in SOURCE_TYPES:
        raise PointEventValidationError("event source is invalid")
    if source_kind != "posthoc" and not str(source.get("source_row_sha256", "")):
        raise PointEventValidationError("imported source row hash is required")
    review = result.get("review")
    if not isinstance(review, Mapping):
        raise PointEventValidationError("review content is required")
    expected = {
        "anchor", "representative_anchor", "labels", "candidate_decision",
        "comment", "disposition", "disposition_reason",
    }
    accepted = expected | {"reviewer", "confidence"}
    if set(review) != expected and set(review) != accepted:
        raise PointEventValidationError("v3 review fields are invalid")
    normalized_review = copy.deepcopy(dict(review))
    # Early, pre-deployment v2 drafts stored these fields per event.  Preserve
    # their journal evidence, but normalize active state to the one reviewer
    # kept on the annotation set/session.
    normalized_review.pop("reviewer", None)
    normalized_review.pop("confidence", None)
    normalized_review["anchor"] = validate_review_anchor(review.get("anchor", {}))
    representative = review.get("representative_anchor")
    normalized_review["representative_anchor"] = (
        validate_review_anchor(representative) if representative is not None else None
    )
    normalized_review["labels"] = _current_labels(review.get("labels"))
    if source_kind == "initial_assumption" and normalized_review["labels"]:
        raise PointEventValidationError(
            "initial state cannot contain semantic change labels"
        )
    decision = str(review.get("candidate_decision", ""))
    if decision not in CANDIDATE_DECISIONS:
        raise PointEventValidationError("candidate decision is invalid")
    if source_kind not in CANDIDATE_SOURCES and decision == "rejected":
        raise PointEventValidationError("human-created events cannot be rejected")
    if decision == "rejected" and (
        normalized_review["labels"]
        or normalized_review["representative_anchor"] is not None
    ):
        raise PointEventValidationError(
            "a rejected candidate cannot retain semantic labels or a representative anchor"
        )
    normalized_review["candidate_decision"] = decision
    disposition = str(review.get("disposition", ""))
    if disposition not in {"active", "dismissed", "deleted"}:
        raise PointEventValidationError("review disposition is invalid")
    if disposition == "deleted" and source_kind != "posthoc":
        raise PointEventValidationError("source events cannot be deleted")
    if disposition != "active" and not str(review.get("disposition_reason", "")).strip():
        raise PointEventValidationError("inactive event requires a reason")
    result["review"] = normalized_review
    status = str(result.get("status", ""))
    if status not in STATUSES:
        raise PointEventValidationError("event status is invalid")
    if status == "Complete" and completion_errors(result):
        raise PointEventValidationError(
            "Complete event is invalid: " + "; ".join(completion_errors(result))
        )
    return result


def _validate_revision_transition(
    before: Mapping[str, Any] | None, after: Mapping[str, Any], *,
    action: str, actor: str,
) -> None:
    if action not in REVISION_ACTIONS:
        raise PointEventValidationError("revision action is invalid")
    if not str(actor or "").strip():
        raise PointEventValidationError("revision actor is required")
    if before is None:
        if action != "create_posthoc":
            raise PointEventValidationError("new events require create_posthoc")
        review = after["review"]
        if (
            after["source"]["kind"] != "posthoc" or after["status"] != "Draft"
            or review.get("disposition") != "active" or review.get("labels") != []
            or review.get("candidate_decision") != "confirmed"
            or review.get("representative_anchor") is not None
            or after["source"].get("original_anchor") != review.get("anchor")
        ):
            raise PointEventValidationError("posthoc create state is invalid")
        return
    if _immutable_projection(before) != _immutable_projection(after):
        raise PointEventValidationError("revision altered immutable source evidence")
    protected_before = {
        key: value for key, value in before.items()
        if key not in {"review", "status", "revision_id"}
    }
    protected_after = {
        key: value for key, value in after.items()
        if key not in {"review", "status", "revision_id"}
    }
    if protected_before != protected_after:
        raise PointEventValidationError("revision changed protected event metadata")
    before_review, after_review = before["review"], after["review"]
    changed = _changed_review_fields(before_review, after_review)
    before_status, after_status = before["status"], after["status"]
    if action == "edit":
        valid = changed <= {"comment"} and after_status == "Draft"
    elif action in {"add_label", "remove_label", "edit_label"}:
        valid = (
            changed == {"labels"}
            and after_status == "Draft"
        )
        old = {item["label_id"]: item for item in before_review["labels"]}
        new = {item["label_id"]: item for item in after_review["labels"]}
        if action == "add_label":
            valid = valid and len(new) == len(old) + 1 and all(new.get(key) == value for key, value in old.items())
        elif action == "remove_label":
            valid = valid and len(new) == len(old) - 1 and all(old.get(key) == value for key, value in new.items())
        else:
            changed_ids = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
            valid = valid and set(old) == set(new) and len(changed_ids) == 1
    elif action == "set_candidate_decision":
        valid = (
            "candidate_decision" in changed
            and changed <= {"candidate_decision", "labels", "representative_anchor"}
            and after_status == "Draft"
        )
        if after_review["candidate_decision"] == "rejected":
            valid = (
                valid and after_review["labels"] == []
                and after_review["representative_anchor"] is None
            )
        else:
            valid = valid and changed == {"candidate_decision"}
    elif action == "move_anchor":
        valid = (
            "anchor" in changed
            and changed <= {"anchor", "representative_anchor"}
            and after_review.get("representative_anchor") is None
            and after_status == "Draft"
        )
    elif action == "move_representative_anchor":
        valid = changed == {"representative_anchor"} and after_status == "Draft"
    elif action == "complete":
        valid = not changed and before_status == "Draft" and after_status == "Complete" and not completion_errors(after)
    elif action == "reopen":
        valid = not changed and before_status == "Complete" and after_status == "Draft"
    elif action == "dismiss":
        valid = before["source"]["kind"] in {"manual", "auto_capture"} and changed <= {"disposition", "disposition_reason"} and after_review["disposition"] == "dismissed" and bool(str(after_review["disposition_reason"]).strip()) and after_status == "Draft"
    elif action == "undismiss":
        valid = before["source"]["kind"] != "posthoc" and before_review["disposition"] == "dismissed" and changed <= {"disposition", "disposition_reason"} and after_review["disposition"] == "active" and after_review["disposition_reason"] == "" and after_status == "Draft"
    elif action == "delete":
        valid = before["source"]["kind"] == "posthoc" and changed <= {"disposition", "disposition_reason"} and after_review["disposition"] == "deleted" and bool(str(after_review["disposition_reason"]).strip()) and after_status == "Draft"
    elif action == "restore":
        valid = before["source"]["kind"] == "posthoc" and before_review["disposition"] == "deleted" and changed <= {"disposition", "disposition_reason"} and after_review["disposition"] == "active" and after_review["disposition_reason"] == "" and after_status == "Draft"
    else:
        valid = False
    if not valid:
        raise PointEventValidationError(f"{action} transition is invalid")


def make_revision(
    before: Mapping[str, Any] | None, after: Mapping[str, Any], *,
    action: str, actor: str, base_revision_id: str = "",
    at_utc: str | None = None, revision_id: str | None = None,
) -> dict[str, Any]:
    actor_text = str(actor or "").strip()
    if not actor_text:
        raise PointEventValidationError("revision actor is required")
    after_copy = validate_event(after)
    before_copy = validate_event(before) if before is not None else None
    _validate_revision_transition(before_copy, after_copy, action=action, actor=actor_text)
    return {
        "schema_version": SCHEMA_VERSION,
        "revision_id": revision_id or str(uuid.uuid4()),
        "event_id": after_copy["event_id"], "actor": actor_text,
        "at_utc": _utc(at_utc or datetime.now(timezone.utc).isoformat()),
        "action": action, "base_revision_id": str(base_revision_id or ""),
        "before_sha256": _sha256_json(before_copy) if before_copy is not None else "",
        "before": before_copy, "after": after_copy,
    }


def revise_event(
    event: Mapping[str, Any], *, action: str, actor: str,
    changes: Mapping[str, Any] | None = None, at_utc: str | None = None,
    state: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    before = validate_event(event)
    after = copy.deepcopy(before)
    changes = copy.deepcopy(dict(changes or {}))
    review = after["review"]
    if action == "edit":
        if set(changes) - {"comment"}:
            raise PointEventValidationError("edit contains a non-editable field")
        review.update(changes)
        after["status"] = "Draft"
    elif action == "add_label":
        initial = before["source"]["kind"] == "initial_assumption"
        if set(changes) != {"label"} or not isinstance(changes["label"], Mapping):
            raise PointEventValidationError("add_label requires one label")
        supplied = dict(changes["label"])
        supplied.setdefault("label_id", str(uuid.uuid4()))
        if initial:
            raise PointEventValidationError(
                "initial state cannot contain semantic change labels"
            )
        review["labels"] = _current_labels([*review["labels"], supplied])
        after["status"] = "Draft"
    elif action == "edit_label":
        initial = before["source"]["kind"] == "initial_assumption"
        if set(changes) != {"label_id", "label"} or not isinstance(changes["label"], Mapping):
            raise PointEventValidationError("edit_label requires label_id and label")
        label_id = str(changes["label_id"])
        supplied = dict(changes["label"])
        if supplied.get("label_id") not in (None, "", label_id):
            raise PointEventValidationError("semantic label_id cannot be changed")
        supplied["label_id"] = label_id
        if initial:
            raise PointEventValidationError(
                "initial state cannot contain semantic change labels"
            )
        matches = [index for index, item in enumerate(review["labels"]) if item["label_id"] == label_id]
        if len(matches) != 1:
            raise PointEventValidationError("unknown or duplicate semantic label_id")
        labels = copy.deepcopy(review["labels"])
        labels[matches[0]] = supplied
        review["labels"] = _current_labels(labels)
        after["status"] = "Draft"
    elif action == "remove_label":
        if set(changes) != {"label_id"}:
            raise PointEventValidationError("remove_label requires label_id")
        label_id = str(changes["label_id"])
        labels = [item for item in review["labels"] if item["label_id"] != label_id]
        if len(labels) != len(review["labels"]) - 1:
            raise PointEventValidationError("unknown or duplicate semantic label_id")
        review["labels"] = labels
        after["status"] = "Draft"
    elif action == "set_candidate_decision":
        if set(changes) != {"decision"} or changes["decision"] not in CANDIDATE_DECISIONS:
            raise PointEventValidationError("candidate decision is invalid")
        if after["source"]["kind"] not in CANDIDATE_SOURCES and changes["decision"] != "confirmed":
            raise PointEventValidationError("human-created events are already confirmed")
        if review["candidate_decision"] == changes["decision"]:
            raise PointEventValidationError("candidate decision is unchanged")
        review["candidate_decision"] = changes["decision"]
        if changes["decision"] == "rejected":
            # Rejected proposals remain immutable source evidence but cease to
            # be semantic training targets.  The revision `before` snapshot
            # retains the grower's prior labels and representative frame.
            review["labels"] = []
            review["representative_anchor"] = None
        after["status"] = "Draft"
    elif action == "move_anchor":
        if set(changes) != {"anchor"}:
            raise PointEventValidationError("move_anchor requires anchor")
        review["anchor"] = validate_review_anchor(changes["anchor"])
        # Moving a boundary changes its following state interval.  The old
        # representative frame is therefore never silently retained.
        review["representative_anchor"] = None
        after["status"] = "Draft"
    elif action == "move_representative_anchor":
        if set(changes) != {"representative_anchor"}:
            raise PointEventValidationError("move_representative_anchor requires representative_anchor")
        review["representative_anchor"] = validate_review_anchor(changes["representative_anchor"])
        after["status"] = "Draft"
    elif action == "complete":
        if changes:
            raise PointEventValidationError("Complete cannot include edits")
        candidate_state = dict(state) if state is not None else {}
        candidate_state[after["event_id"]] = after
        errors = [
            *completion_errors(after),
            *interval_errors(after["event_id"], candidate_state),
        ]
        if errors:
            raise PointEventValidationError("Cannot complete event: " + "; ".join(errors))
        after["status"] = "Complete"
    elif action == "reopen":
        if changes:
            raise PointEventValidationError("Reopen cannot include edits")
        after["status"] = "Draft"
    elif action in {"dismiss", "delete"}:
        reason = str(changes.get("reason", "")).strip()
        if set(changes) != {"reason"} or not reason:
            raise PointEventValidationError(f"{action} reason is required")
        if action == "dismiss" and after["source"]["kind"] not in {"manual", "auto_capture"}:
            raise PointEventValidationError("only manual or automatic source events can be dismissed")
        if action == "delete" and after["source"]["kind"] != "posthoc":
            raise PointEventValidationError("source events cannot be deleted")
        review.update({
            "disposition": "dismissed" if action == "dismiss" else "deleted",
            "disposition_reason": reason,
        })
        after["status"] = "Draft"
    elif action in {"undismiss", "restore"}:
        if changes:
            raise PointEventValidationError(f"{action} cannot include edits")
        review.update({"disposition": "active", "disposition_reason": ""})
        after["status"] = "Draft"
    else:
        raise PointEventValidationError("create_posthoc is only valid for a new event")
    revision = make_revision(
        before, after, action=action, actor=actor,
        base_revision_id=str(before.get("revision_id", "")), at_utc=at_utc,
    )
    after["revision_id"] = revision["revision_id"]
    revision["after"] = copy.deepcopy(after)
    return validate_event(after), revision


def replay_revisions(
    initial_events: Iterable[Mapping[str, Any]],
    revisions: Iterable[Mapping[str, Any]], *, require_integrity: bool = False,
) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for event in initial_events:
        checked = validate_event(event)
        if checked["event_id"] in state:
            raise PointEventValidationError("duplicate initial event_id")
        state[checked["event_id"]] = checked
    if sum(item["source"]["kind"] == "initial_assumption" for item in state.values()) != 1:
        raise PointEventValidationError("exactly one initial 1x1 assumption is required")
    records = list(revisions)
    if require_integrity:
        _validate_record_integrity(records)
    seen: set[str] = set()
    for raw in records:
        if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION:
            raise PointEventValidationError("revision schema is invalid")
        revision_id = str(raw.get("revision_id", ""))
        try:
            uuid.UUID(revision_id)
        except ValueError as exc:
            raise PointEventValidationError("revision_id must be a UUID") from exc
        if revision_id in seen:
            raise PointEventValidationError("duplicate revision_id")
        seen.add(revision_id)
        event_id, action = str(raw.get("event_id", "")), str(raw.get("action", ""))
        if not _utc(raw.get("at_utc")):
            raise PointEventValidationError("revision UTC is invalid")
        if action == "create_posthoc":
            if event_id in state or raw.get("before") is not None:
                raise PointEventValidationError("posthoc create conflicts with current state")
            after = validate_event(raw.get("after", {}))
            _validate_revision_transition(None, after, action=action, actor=str(raw.get("actor", "")))
        else:
            if event_id not in state:
                raise PointEventValidationError("revision targets an unknown event")
            before = state[event_id]
            if str(raw.get("base_revision_id", "")) != str(before.get("revision_id", "")):
                raise PointEventValidationError("revision base does not match current state")
            if str(raw.get("before_sha256", "")) != _sha256_json(before):
                raise PointEventValidationError("revision before hash does not match current state")
            supplied_before = validate_event(raw.get("before", {}))
            if supplied_before != before:
                raise PointEventValidationError("revision before snapshot does not match current state")
            after = validate_event(raw.get("after", {}))
            _validate_revision_transition(before, after, action=action, actor=str(raw.get("actor", "")))
        if after["event_id"] != event_id:
            raise PointEventValidationError("revision event identity is inconsistent")
        after["revision_id"] = revision_id
        state[event_id] = after
    for event_id, event in state.items():
        if event["status"] == "Complete":
            errors = interval_errors(event_id, state)
            if errors:
                raise PointEventValidationError("Complete event interval is invalid: " + "; ".join(errors))
    return copy.deepcopy(state)


def derive_state_segments(
    events: Iterable[Mapping[str, Any]], *, frame_count: int,
    session_identity: str,
) -> list[dict[str, Any]]:
    """Materialize complete interval states from the editable point deltas.

    ``initial_assumption`` is workflow evidence for reviewing the default
    initial state, not a physical ``1x1 appeared`` transition.  It owns the
    first interval Anchor while the baseline remains 1x1 with unknown
    visible-pattern clarity.  It has no semantic change labels.

    The current journal stores a representative Anchor beside its boundary
    event for revision compatibility.  This function normalizes that storage
    into one first-class ``anchor`` on each exported state segment.
    """

    checked = [validate_event(item) for item in events]
    initial_events = [
        item for item in checked
        if item["source"]["kind"] == "initial_assumption"
    ]
    if len(initial_events) != 1:
        raise PointEventValidationError(
            "exactly one initial 1x1 state review is required"
        )
    semantic_events = [
        item for item in checked
        if item["source"]["kind"] != "initial_assumption"
    ]
    replay_initial_state = copy.deepcopy(INITIAL_STATE)
    try:
        replay = replay_event_states(
            semantic_events,
            frame_count=int(frame_count),
            initial_state=replay_initial_state,
            session_identity=str(session_identity),
        )
    except EventStateError as exc:
        raise PointEventValidationError(
            "event-state replay is inconsistent: " + str(exc)
        ) from exc

    by_id = {item["event_id"]: item for item in checked}
    result: list[dict[str, Any]] = []
    for segment in replay.segments:
        owner_ids = (
            segment.boundary_event_ids
            if segment.boundary_event_ids
            else (initial_events[0]["event_id"],)
        )
        owners = [by_id[event_id] for event_id in owner_ids]
        raw_anchors = [
            owner["review"].get("representative_anchor")
            for owner in owners
            if owner["review"].get("representative_anchor") is not None
        ]
        unique_anchors = {
            _representative_anchor_identity(anchor): anchor
            for anchor in raw_anchors
        }
        if len(unique_anchors) > 1:
            raise PointEventValidationError(
                "events at one state boundary disagree about the interval Anchor"
            )
        anchor = copy.deepcopy(next(iter(unique_anchors.values()), None))
        if anchor is not None:
            try:
                replay_event_states(
                    semantic_events,
                    frame_count=int(frame_count),
                    initial_state=replay_initial_state,
                    session_identity=str(session_identity),
                    anchors=[SegmentAnchor(
                        segment_id=segment.segment_id,
                        frame_index=int(anchor["frame_index"]),
                        capture_sequence=int(anchor["capture_sequence"]),
                        image_sha256=str(anchor.get("image_sha256", "")),
                    )],
                )
            except EventStateError as exc:
                raise PointEventValidationError(
                    "state-segment Anchor is invalid: " + str(exc)
                ) from exc
        complete = bool(anchor) and all(
            owner["status"] == "Complete" for owner in owners
        )
        result.append({
            "segment_id": segment.segment_id,
            "start_frame_index": segment.start_frame,
            "end_frame_index_exclusive": segment.end_frame_exclusive,
            "frame_count": segment.end_frame_exclusive - segment.start_frame,
            "state": {
                "reconstructions": sorted(segment.reconstructions),
                "clarity": segment.clarity,
            },
            "boundary_event_ids": list(segment.boundary_event_ids),
            "anchor": anchor,
            "status": "Complete" if complete else "Draft",
        })
    return result


def point_event_document(
    *, dataset: Mapping[str, Any], events: Sequence[Mapping[str, Any]],
    reference_events: Sequence[Mapping[str, Any]] = (),
    unlinked_legacy_labels: Sequence[Mapping[str, Any]] = (),
    source_revisions: Sequence[Mapping[str, Any]] = (),
    source_journal: Mapping[str, Any] | None = None,
    revisions: Sequence[Mapping[str, Any]] = (), annotation_set_id: str,
    reviewer: str,
) -> dict[str, Any]:
    current = replay_revisions(events, revisions)
    state_segments = derive_state_segments(
        current.values(),
        frame_count=int(dataset.get("frame_count", 0)),
        session_identity=str(dataset.get("dataset_id", "")),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": DOCUMENT_TYPE,
        "dataset": copy.deepcopy(dict(dataset)),
        "initial_state": copy.deepcopy(INITIAL_STATE),
        "segments": state_segments,
        "annotation_set": {
            "annotation_set_id": str(annotation_set_id),
            "reviewer": str(reviewer),
            "model_outputs_visible": True,
            "eligible_for_gold": False,
        },
        "events": list(current.values()),
        "reference_events": copy.deepcopy(list(reference_events)),
        "unlinked_legacy_labels": copy.deepcopy(list(unlinked_legacy_labels)),
        "source_revisions": copy.deepcopy(list(source_revisions)),
        "source_journal": copy.deepcopy(dict(source_journal)) if source_journal else None,
        "revisions": copy.deepcopy(list(revisions)),
    }


V2_INITIAL_STATE = {
    "reconstructions": ["one_by_one"],
    "clarity": "bad",
    "quality": "unknown",
}
_V2_SEGMENT_NAMESPACE = uuid.UUID("877062c8-84f3-5bc5-9bd0-e032edcbaeb5")


def _v2_segment_id(
    *, session_identity: str, start_frame: int,
    boundary_event_ids: Sequence[str],
) -> str:
    """Reproduce the frozen v2 deterministic segment identity."""

    identity = json.dumps({
        "session_identity": str(session_identity),
        "start_frame": int(start_frame),
        "boundary_event_ids": sorted(str(item) for item in boundary_event_ids),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return str(uuid.uuid5(_V2_SEGMENT_NAMESPACE, identity))


def _validate_v2_label_evidence(value: object) -> dict[str, str]:
    """Validate one v2 label without reinterpreting it as a v3 label."""

    if not isinstance(value, Mapping):
        raise PointEventValidationError("legacy v2 semantic label must be an object")
    result = {str(key): str(item) for key, item in value.items()}
    if set(result) != {"label_id", "kind", "change", "value"}:
        raise PointEventValidationError("legacy v2 semantic label fields are invalid")
    try:
        uuid.UUID(result["label_id"])
    except ValueError as exc:
        raise PointEventValidationError("legacy v2 semantic label_id must be a UUID") from exc
    kind, change, label_value = (
        result["kind"], result["change"], result["value"],
    )
    if kind == "reconstruction":
        if change not in {"appeared", "disappeared"}:
            raise PointEventValidationError("legacy v2 reconstruction change is invalid")
        if label_value not in RECONSTRUCTION_VALUES:
            raise PointEventValidationError("legacy v2 reconstruction value is invalid")
    elif kind == "pattern_clarity":
        if change != "became" or label_value not in PATTERN_CLARITY_VALUES:
            raise PointEventValidationError("legacy v2 pattern-clarity label is invalid")
    elif kind == "surface_quality":
        if change != "became" or label_value not in {"good", "bad"}:
            raise PointEventValidationError("legacy v2 surface-quality label is invalid")
    else:
        raise PointEventValidationError("legacy v2 semantic label kind is invalid")
    return result


def _validate_v2_labels_evidence(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise PointEventValidationError("legacy v2 labels must be an array")
    labels = [_validate_v2_label_evidence(item) for item in value]
    identifiers = [item["label_id"] for item in labels]
    meanings = [(item["kind"], item["change"], item["value"]) for item in labels]
    if len(set(identifiers)) != len(identifiers):
        raise PointEventValidationError("legacy v2 semantic label_id is duplicated")
    if len(set(meanings)) != len(meanings):
        raise PointEventValidationError("legacy v2 semantic label is duplicated")
    if sum(item["kind"] == "pattern_clarity" for item in labels) > 1:
        raise PointEventValidationError("legacy v2 has multiple pattern-clarity labels")
    if sum(item["kind"] == "surface_quality" for item in labels) > 1:
        raise PointEventValidationError("legacy v2 has multiple surface-quality labels")
    reconstruction_changes: dict[str, set[str]] = {}
    for item in labels:
        if item["kind"] == "reconstruction":
            reconstruction_changes.setdefault(item["value"], set()).add(item["change"])
    if any(len(changes) > 1 for changes in reconstruction_changes.values()):
        raise PointEventValidationError(
            "legacy v2 reconstruction both appears and disappears at one event"
        )
    return labels


def _validate_v2_event_evidence(event: object) -> dict[str, Any]:
    """Strictly validate an old offline event while preserving every field."""

    if not isinstance(event, Mapping):
        raise PointEventValidationError("legacy v2 event must be an object")
    result = copy.deepcopy(dict(event))
    if result.get("schema") != V2_SCHEMA_VERSION:
        raise PointEventValidationError("legacy v2 event schema is invalid")
    if set(result) != {
        "schema", "event_id", "source", "review", "status", "revision_id",
    }:
        raise PointEventValidationError("legacy v2 event fields are invalid")
    try:
        uuid.UUID(str(result.get("event_id", "")))
    except ValueError as exc:
        raise PointEventValidationError("legacy v2 event_id must be a UUID") from exc
    revision_id = str(result.get("revision_id", ""))
    if revision_id:
        try:
            uuid.UUID(revision_id)
        except ValueError as exc:
            raise PointEventValidationError("legacy v2 event revision_id is invalid") from exc
    source = result.get("source")
    if not isinstance(source, Mapping) or source.get("kind") not in SOURCE_TYPES:
        raise PointEventValidationError("legacy v2 source evidence is invalid")
    if source.get("kind") != "posthoc" and not str(source.get("source_row_sha256", "")):
        raise PointEventValidationError("legacy v2 source row hash is missing")
    review = result.get("review")
    expected_review = {
        "anchor", "representative_anchor", "labels", "candidate_decision",
        "comment", "disposition", "disposition_reason",
    }
    if not isinstance(review, Mapping) or frozenset(review) not in {
        frozenset(expected_review),
        frozenset(expected_review | {"reviewer", "confidence"}),
    }:
        raise PointEventValidationError("legacy v2 review fields are invalid")
    anchor = validate_review_anchor(review.get("anchor", {}))
    if len(str(anchor.get("image_sha256", ""))) != 64:
        raise PointEventValidationError("legacy v2 anchor hash evidence is missing")
    representative = review.get("representative_anchor")
    if representative is not None:
        checked_representative = validate_review_anchor(representative)
        if len(str(checked_representative.get("image_sha256", ""))) != 64:
            raise PointEventValidationError(
                "legacy v2 representative anchor hash evidence is missing"
            )
    labels = _validate_v2_labels_evidence(review.get("labels"))
    source_kind = str(source.get("kind"))
    if source_kind == "initial_assumption" and (
        len(labels) > 1
        or any(item["kind"] != "surface_quality" for item in labels)
    ):
        raise PointEventValidationError(
            "legacy v2 initial state accepts only one surface-quality label"
        )
    decision = str(review.get("candidate_decision", ""))
    if decision not in CANDIDATE_DECISIONS:
        raise PointEventValidationError("legacy v2 candidate decision is invalid")
    if source_kind not in CANDIDATE_SOURCES and decision == "rejected":
        raise PointEventValidationError("legacy v2 human event cannot be rejected")
    if decision == "rejected" and (labels or representative is not None):
        raise PointEventValidationError("legacy v2 rejected candidate retains targets")
    disposition = str(review.get("disposition", ""))
    if disposition not in {"active", "dismissed", "deleted"}:
        raise PointEventValidationError("legacy v2 disposition is invalid")
    if disposition == "deleted" and source_kind != "posthoc":
        raise PointEventValidationError("legacy v2 source event cannot be deleted")
    if disposition != "active" and not str(review.get("disposition_reason", "")).strip():
        raise PointEventValidationError("legacy v2 inactive event reason is missing")
    status = str(result.get("status", ""))
    if status not in STATUSES:
        raise PointEventValidationError("legacy v2 status is invalid")
    if status == "Complete" and decision != "rejected":
        if source_kind == "initial_assumption":
            if len(labels) != 1 or labels[0]["kind"] != "surface_quality":
                raise PointEventValidationError(
                    "legacy v2 Complete initial quality is missing"
                )
        elif not labels:
            raise PointEventValidationError("legacy v2 Complete event has no labels")
    return result


def _derive_v2_state_segments_read_only(
    events: Iterable[Mapping[str, Any]], *, frame_count: int,
    session_identity: str,
) -> list[dict[str, Any]]:
    """Replay the frozen v2 quality-aware state model without migrating it.

    This intentionally duplicates the deployed v2 semantics.  The current v3
    state engine cannot consume ``surface_quality`` and must never reinterpret
    that evidence as visible-pattern clarity.
    """

    count = int(frame_count)
    if count < 1:
        raise PointEventValidationError("legacy v2 frame_count is invalid")
    checked = [_validate_v2_event_evidence(item) for item in events]
    initial = [
        item for item in checked
        if item["source"]["kind"] == "initial_assumption"
    ]
    if len(initial) != 1:
        raise PointEventValidationError(
            "legacy v2 requires one initial assumption"
        )
    initial_quality = next((
        label["value"] for label in initial[0]["review"]["labels"]
        if label["kind"] == "surface_quality"
    ), "unknown")
    state: dict[str, Any] = {
        "reconstructions": {"one_by_one"},
        "clarity": "bad",
        "quality": initial_quality,
    }
    active = [
        item for item in checked
        if item["source"]["kind"] != "initial_assumption"
        and item["review"]["candidate_decision"] == "confirmed"
        and item["review"]["disposition"] == "active"
        and bool(item["review"]["labels"])
    ]
    active.sort(key=lambda item: (
        int(item["review"]["anchor"]["frame_index"]), item["event_id"],
    ))
    groups: list[tuple[int, list[dict[str, Any]]]] = []
    for item in active:
        frame_index = int(item["review"]["anchor"]["frame_index"])
        if frame_index < 1 or frame_index > count:
            raise PointEventValidationError(
                "legacy v2 event anchor lies outside the report"
            )
        if not groups or groups[-1][0] != frame_index:
            groups.append((frame_index, []))
        groups[-1][1].append(item)

    raw_segments: list[dict[str, Any]] = []
    segment_start = 1
    boundary_ids: tuple[str, ...] = ()

    def append_segment(end_exclusive: int) -> None:
        raw_segments.append({
            "segment_id": _v2_segment_id(
                session_identity=session_identity,
                start_frame=segment_start,
                boundary_event_ids=boundary_ids,
            ),
            "start_frame_index": segment_start,
            "end_frame_index_exclusive": end_exclusive,
            "frame_count": end_exclusive - segment_start,
            "state": {
                "reconstructions": sorted(state["reconstructions"]),
                "clarity": state["clarity"],
                "quality": state["quality"],
            },
            "boundary_event_ids": list(boundary_ids),
        })

    for frame_index, same_frame in groups:
        if frame_index > segment_start:
            append_segment(frame_index)
        appeared: list[str] = []
        disappeared: list[str] = []
        clarity_changes: list[str] = []
        quality_changes: list[str] = []
        for item in same_frame:
            for label in item["review"]["labels"]:
                if label["kind"] == "reconstruction":
                    target = appeared if label["change"] == "appeared" else disappeared
                    target.append(label["value"])
                elif label["kind"] == "pattern_clarity":
                    clarity_changes.append(label["value"])
                elif label["kind"] == "surface_quality":
                    quality_changes.append(label["value"])
        duplicate_appeared = {item for item in appeared if appeared.count(item) > 1}
        duplicate_disappeared = {
            item for item in disappeared if disappeared.count(item) > 1
        }
        contradictory = set(appeared) & set(disappeared)
        already_present = set(appeared) & state["reconstructions"]
        absent = set(disappeared) - state["reconstructions"]
        if duplicate_appeared or duplicate_disappeared or contradictory:
            raise PointEventValidationError(
                "legacy v2 has duplicate or contradictory reconstruction changes"
            )
        if already_present or absent:
            raise PointEventValidationError(
                "legacy v2 reconstruction transition is inconsistent"
            )
        if len(clarity_changes) > 1 or len(quality_changes) > 1:
            raise PointEventValidationError(
                "legacy v2 has multiple same-frame clarity or quality changes"
            )
        if clarity_changes and clarity_changes[0] == state["clarity"]:
            raise PointEventValidationError(
                "legacy v2 clarity transition does not change state"
            )
        if quality_changes and quality_changes[0] == state["quality"]:
            raise PointEventValidationError(
                "legacy v2 quality transition does not change state"
            )
        state["reconstructions"] = (
            state["reconstructions"] - set(disappeared)
        ) | set(appeared)
        if clarity_changes:
            state["clarity"] = clarity_changes[0]
        if quality_changes:
            state["quality"] = quality_changes[0]
        segment_start = frame_index
        boundary_ids = tuple(sorted(item["event_id"] for item in same_frame))
    append_segment(count + 1)

    by_id = {item["event_id"]: item for item in checked}
    result: list[dict[str, Any]] = []
    for segment in raw_segments:
        owner_ids = segment["boundary_event_ids"] or [initial[0]["event_id"]]
        owners = [by_id[event_id] for event_id in owner_ids]
        unique_anchors: dict[tuple[int, int, str], Mapping[str, Any]] = {}
        for owner in owners:
            anchor = owner["review"].get("representative_anchor")
            if anchor is None:
                continue
            identity = (
                int(anchor["frame_index"]),
                int(anchor["capture_sequence"]),
                str(anchor["image_sha256"]).lower(),
            )
            unique_anchors[identity] = anchor
        if len(unique_anchors) > 1:
            raise PointEventValidationError(
                "legacy v2 events at one boundary disagree about the interval Anchor"
            )
        anchor = copy.deepcopy(next(iter(unique_anchors.values()), None))
        if anchor is not None and not (
            int(segment["start_frame_index"])
            <= int(anchor["frame_index"])
            < int(segment["end_frame_index_exclusive"])
        ):
            raise PointEventValidationError(
                "legacy v2 state-segment Anchor is outside its interval"
            )
        complete = bool(anchor) and all(
            owner["status"] == "Complete" for owner in owners
        )
        result.append({
            **segment,
            "anchor": anchor,
            "status": "Complete" if complete else "Draft",
        })
    return result


def _validate_v2_revision_transition_evidence(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    *,
    action: str,
    actor: str,
) -> None:
    if action not in REVISION_ACTIONS or not str(actor).strip():
        raise PointEventValidationError("legacy v2 revision action or actor is invalid")
    if before is None:
        if (
            action != "create_posthoc"
            or after["source"]["kind"] != "posthoc"
            or after["status"] != "Draft"
            or after["review"]["labels"]
        ):
            raise PointEventValidationError("legacy v2 posthoc create is invalid")
        return
    if _immutable_projection(before) != _immutable_projection(after):
        raise PointEventValidationError("legacy v2 revision altered source evidence")
    protected_before = {
        key: value for key, value in before.items()
        if key not in {"review", "status", "revision_id"}
    }
    protected_after = {
        key: value for key, value in after.items()
        if key not in {"review", "status", "revision_id"}
    }
    if protected_before != protected_after:
        raise PointEventValidationError("legacy v2 revision changed protected metadata")
    old, new = before["review"], after["review"]
    changed = _changed_review_fields(old, new)
    if action == "edit":
        valid = changed <= {"comment", "reviewer", "confidence"} and after["status"] == "Draft"
    elif action in {"add_label", "edit_label", "remove_label"}:
        valid = changed == {"labels"} and after["status"] == "Draft"
        old_labels = {item["label_id"]: item for item in old["labels"]}
        new_labels = {item["label_id"]: item for item in new["labels"]}
        if action == "add_label":
            valid = (
                valid and len(new_labels) == len(old_labels) + 1
                and all(new_labels.get(key) == value for key, value in old_labels.items())
            )
        elif action == "remove_label":
            valid = (
                valid and len(new_labels) == len(old_labels) - 1
                and all(old_labels.get(key) == value for key, value in new_labels.items())
            )
        else:
            changed_ids = {
                key for key in set(old_labels) | set(new_labels)
                if old_labels.get(key) != new_labels.get(key)
            }
            valid = valid and set(old_labels) == set(new_labels) and len(changed_ids) == 1
    elif action == "set_candidate_decision":
        valid = (
            "candidate_decision" in changed
            and changed <= {"candidate_decision", "labels", "representative_anchor"}
            and after["status"] == "Draft"
        )
    elif action == "move_anchor":
        valid = "anchor" in changed and changed <= {"anchor", "representative_anchor"} and after["status"] == "Draft"
    elif action == "move_representative_anchor":
        valid = changed == {"representative_anchor"} and after["status"] == "Draft"
    elif action == "complete":
        valid = not changed and before["status"] == "Draft" and after["status"] == "Complete"
    elif action == "reopen":
        valid = not changed and before["status"] == "Complete" and after["status"] == "Draft"
    elif action in {"dismiss", "delete", "undismiss", "restore"}:
        valid = changed <= {"disposition", "disposition_reason"} and after["status"] == "Draft"
        if action == "dismiss":
            valid = (
                valid and before["source"]["kind"] in {"manual", "auto_capture"}
                and new["disposition"] == "dismissed"
                and bool(str(new["disposition_reason"]).strip())
            )
        elif action == "delete":
            valid = (
                valid and before["source"]["kind"] == "posthoc"
                and new["disposition"] == "deleted"
                and bool(str(new["disposition_reason"]).strip())
            )
        elif action == "undismiss":
            valid = (
                valid and before["source"]["kind"] != "posthoc"
                and old["disposition"] == "dismissed"
                and new["disposition"] == "active"
                and new["disposition_reason"] == ""
            )
        else:
            valid = (
                valid and before["source"]["kind"] == "posthoc"
                and old["disposition"] == "deleted"
                and new["disposition"] == "active"
                and new["disposition_reason"] == ""
            )
    else:
        valid = False
    if not valid:
        raise PointEventValidationError(f"legacy v2 {action} transition is invalid")


def validate_v2_point_event_document_read_only(
    document: Mapping[str, Any], payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a v2 export as immutable evidence; never migrate or write it."""

    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") != V2_SCHEMA_VERSION
        or document.get("document_type") != DOCUMENT_TYPE
    ):
        raise PointEventValidationError("Unsupported legacy v2 document schema")
    expected_keys = {
        "schema_version", "document_type", "dataset", "initial_state",
        "segments", "annotation_set", "events", "reference_events",
        "unlinked_legacy_labels", "source_revisions", "source_journal",
        "revisions",
    }
    if set(document) != expected_keys:
        raise PointEventValidationError("legacy v2 document fields are invalid")
    config = payload.get("config", {})
    expected_dataset = config.get("dataset", {})
    incoming_dataset = document.get("dataset")
    identity_fields = (
        "dataset_id", "frame_count", "ordered_frame_fingerprint",
        "source_archive_sha256", "model_context_fingerprint",
    )
    if not isinstance(incoming_dataset, Mapping) or any(
        incoming_dataset.get(key) != expected_dataset.get(key)
        for key in identity_fields
    ):
        raise PointEventValidationError("legacy v2 dataset does not match this report")
    annotation_set = document.get("annotation_set")
    if not isinstance(annotation_set, Mapping):
        raise PointEventValidationError("legacy v2 annotation set is invalid")
    try:
        uuid.UUID(str(annotation_set.get("annotation_set_id", "")))
    except ValueError as exc:
        raise PointEventValidationError("legacy v2 annotation_set_id is invalid") from exc
    if (
        not str(annotation_set.get("reviewer", "")).strip()
        or annotation_set.get("model_outputs_visible") is not True
        or annotation_set.get("eligible_for_gold") is not False
    ):
        raise PointEventValidationError("legacy v2 annotation safety metadata is invalid")
    if document.get("initial_state") != V2_INITIAL_STATE:
        raise PointEventValidationError("legacy v2 initial quality state was changed")
    for document_key, config_key in (
        ("source_revisions", "source_event_revisions"),
        ("source_journal", "source_event_journal"),
        ("reference_events", "reference_events"),
        ("unlinked_legacy_labels", "unlinked_legacy_labels"),
    ):
        expected = config.get(config_key, [] if document_key != "source_journal" else None)
        if document.get(document_key) != expected:
            raise PointEventValidationError(
                f"legacy v2 {document_key} evidence was changed or omitted"
            )
    raw_events = document.get("events")
    if not isinstance(raw_events, list):
        raise PointEventValidationError("legacy v2 events must be an array")
    events: dict[str, dict[str, Any]] = {}
    for raw in raw_events:
        checked = _validate_v2_event_evidence(raw)
        if checked["event_id"] in events:
            raise PointEventValidationError("legacy v2 event_id is duplicated")
        events[checked["event_id"]] = checked
    if sum(
        item["source"]["kind"] == "initial_assumption"
        for item in events.values()
    ) != 1:
        raise PointEventValidationError("legacy v2 requires one initial assumption")
    raw_revisions = document.get("revisions")
    if not isinstance(raw_revisions, list):
        raise PointEventValidationError("legacy v2 revisions must be an array")
    has_integrity = any(
        isinstance(item, Mapping)
        and ("record_sha256" in item or "previous_record_sha256" in item)
        for item in raw_revisions
    )
    if has_integrity:
        _validate_record_integrity(raw_revisions)
    chain_state: dict[str, dict[str, Any]] = {}
    revision_ids: set[str] = set()
    for raw in raw_revisions:
        if not isinstance(raw, Mapping) or raw.get("schema_version") != V2_SCHEMA_VERSION:
            raise PointEventValidationError("legacy v2 revision schema is invalid")
        revision_id = str(raw.get("revision_id", ""))
        try:
            uuid.UUID(revision_id)
        except ValueError as exc:
            raise PointEventValidationError("legacy v2 revision_id is invalid") from exc
        if revision_id in revision_ids or not _utc(raw.get("at_utc")):
            raise PointEventValidationError("legacy v2 revision identity is invalid")
        revision_ids.add(revision_id)
        event_id = str(raw.get("event_id", ""))
        action = str(raw.get("action", ""))
        before_raw = raw.get("before")
        before = (
            _validate_v2_event_evidence(before_raw)
            if before_raw is not None else None
        )
        after = _validate_v2_event_evidence(raw.get("after"))
        if after["event_id"] != event_id or after.get("revision_id") != revision_id:
            raise PointEventValidationError("legacy v2 revision event identity is invalid")
        if before is not None:
            if before["event_id"] != event_id:
                raise PointEventValidationError("legacy v2 before identity is invalid")
            if str(raw.get("before_sha256", "")) != _sha256_json(before):
                raise PointEventValidationError("legacy v2 before hash is invalid")
            prior = chain_state.get(event_id)
            if prior is not None and prior != before:
                raise PointEventValidationError("legacy v2 revision chain is broken")
            if str(raw.get("base_revision_id", "")) != str(before.get("revision_id", "")):
                raise PointEventValidationError("legacy v2 base revision is invalid")
        elif action != "create_posthoc" or event_id in chain_state:
            raise PointEventValidationError("legacy v2 revision starts without before state")
        _validate_v2_revision_transition_evidence(
            before, after, action=action, actor=str(raw.get("actor", "")),
        )
        chain_state[event_id] = after
    for event_id, state in chain_state.items():
        if events.get(event_id) != state:
            raise PointEventValidationError(
                "legacy v2 materialized event does not match its revision chain"
            )
    segments = document.get("segments")
    if not isinstance(segments, list):
        raise PointEventValidationError("legacy v2 segments must be an array")
    frame_count = int(expected_dataset.get("frame_count", 0))
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise PointEventValidationError("legacy v2 segment is invalid")
        state = segment.get("state")
        if not isinstance(state, Mapping) or set(state) != {
            "reconstructions", "clarity", "quality",
        }:
            raise PointEventValidationError("legacy v2 segment state is invalid")
        if (
            not isinstance(state.get("reconstructions"), list)
            or any(item not in RECONSTRUCTION_VALUES for item in state["reconstructions"])
            or state.get("clarity") not in PATTERN_CLARITY_VALUES
            or state.get("quality") not in {"good", "bad", "unknown"}
        ):
            raise PointEventValidationError("legacy v2 segment label is invalid")
        start = _int(segment.get("start_frame_index"))
        end = _int(segment.get("end_frame_index_exclusive"))
        if start is None or end is None or start < 1 or end <= start or end > frame_count + 1:
            raise PointEventValidationError("legacy v2 segment bounds are invalid")
        boundary_ids = segment.get("boundary_event_ids")
        if not isinstance(boundary_ids, list) or any(
            str(identifier) not in events for identifier in boundary_ids
        ):
            raise PointEventValidationError("legacy v2 segment event identity is invalid")
    expected_segments = _derive_v2_state_segments_read_only(
        events.values(),
        frame_count=frame_count,
        session_identity=str(expected_dataset.get("dataset_id", "")),
    )
    if segments != expected_segments:
        raise PointEventValidationError(
            "legacy v2 materialized state segments do not match frozen replay"
        )
    _validate_document_anchor_provenance(events.values(), payload)
    return copy.deepcopy(dict(document))


def _validate_document_anchor_provenance(
    events: Iterable[Mapping[str, Any]], payload: Mapping[str, Any],
) -> None:
    indices = payload.get("heartbeat_indices", [])
    sequences = payload.get("capture_sequences", [])
    hashes = payload.get("frame_sha256", [])
    times = payload.get("times", [])
    for event in events:
        anchors = [event["review"]["anchor"]]
        if event["review"].get("representative_anchor") is not None:
            anchors.append(event["review"]["representative_anchor"])
        for anchor in anchors:
            index = int(anchor["frame_index"]) - 1
            if index < 0 or index >= len(hashes):
                raise PointEventValidationError("review anchor lies outside the report")
            frame_hash = str(anchor.get("image_sha256", "")).lower()
            if (
                int(anchor["heartbeat_idx"]) != int(indices[index])
                or int(anchor["capture_sequence"]) != int(sequences[index])
                or frame_hash and frame_hash != str(hashes[index]).lower()
                or not math.isclose(
                    float(anchor["elapsed_s"]), float(times[index]), abs_tol=0.0015,
                )
            ):
                raise PointEventValidationError(
                    "review anchor provenance does not match the report"
                )


def validate_point_event_document(
    document: Mapping[str, Any], payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(document, Mapping) or document.get("schema_version") != SCHEMA_VERSION or document.get("document_type") != DOCUMENT_TYPE:
        raise PointEventValidationError("Unsupported point-event document schema")
    expected_dataset = payload.get("config", {}).get("dataset", {})
    incoming_dataset = document.get("dataset")
    identity_fields = (
        "dataset_id", "frame_count", "ordered_frame_fingerprint",
        "source_archive_sha256", "model_context_fingerprint",
    )
    if not isinstance(incoming_dataset, Mapping) or any(
        incoming_dataset.get(key) != expected_dataset.get(key) for key in identity_fields
    ):
        raise PointEventValidationError("Annotation dataset does not match this report")
    annotation_set = document.get("annotation_set")
    if not isinstance(annotation_set, Mapping) or not str(annotation_set.get("annotation_set_id", "")).strip() or not str(annotation_set.get("reviewer", "")).strip():
        raise PointEventValidationError("annotation set identity and reviewer are required")
    try:
        uuid.UUID(str(annotation_set["annotation_set_id"]))
    except (KeyError, ValueError) as exc:
        raise PointEventValidationError("annotation_set_id must be a UUID") from exc
    if annotation_set.get("model_outputs_visible") is not True or annotation_set.get("eligible_for_gold") is not False:
        raise PointEventValidationError("model-assisted annotations must remain ineligible for gold use")
    config = payload.get("config", {})
    if document.get("source_revisions", []) != config.get("source_event_revisions", []):
        raise PointEventValidationError("archived live revision history was changed or omitted")
    if document.get("source_journal") != config.get("source_event_journal"):
        raise PointEventValidationError("archived live journal provenance was changed or omitted")
    if document.get("reference_events", []) != config.get("reference_events", []):
        raise PointEventValidationError("read-only reference events were changed or omitted")
    if document.get("unlinked_legacy_labels", []) != config.get("unlinked_legacy_labels", []):
        raise PointEventValidationError("unlinked legacy-label evidence was changed or omitted")
    report_events = config.get("point_events", [])
    document_revisions = document.get("revisions", [])
    if not isinstance(document_revisions, list):
        raise PointEventValidationError("revisions must be an array")
    has_integrity = any(
        isinstance(item, Mapping)
        and ("record_sha256" in item or "previous_record_sha256" in item)
        for item in document_revisions
    )
    current = replay_revisions(
        report_events, document_revisions, require_integrity=has_integrity,
    )
    supplied_events = document.get("events")
    if not isinstance(supplied_events, list):
        raise PointEventValidationError("events must be an array")
    checked_supplied: dict[str, dict[str, Any]] = {}
    for item in map(validate_event, supplied_events):
        if item["event_id"] in checked_supplied:
            raise PointEventValidationError("duplicate event_id in materialized events")
        checked_supplied[item["event_id"]] = item
    if checked_supplied != current:
        raise PointEventValidationError("materialized events do not match revision replay")
    if document.get("initial_state") != INITIAL_STATE:
        raise PointEventValidationError("initial 1x1 state was changed or omitted")
    expected_segments = derive_state_segments(
        current.values(),
        frame_count=int(expected_dataset.get("frame_count", 0)),
        session_identity=str(expected_dataset.get("dataset_id", "")),
    )
    if document.get("segments") != expected_segments:
        raise PointEventValidationError(
            "materialized state segments do not match point-event replay"
        )
    # Every review anchor must identify the exact saved frame in this report.
    # Hashes are optional evidence; when present they remain fail-closed.
    indices = payload.get("heartbeat_indices", [])
    sequences = payload.get("capture_sequences", [])
    hashes = payload.get("frame_sha256", [])
    times = payload.get("times", [])
    frame_contexts = config.get("frame_contexts", [])
    for event in current.values():
        anchors = [event["review"]["anchor"]]
        if event["review"].get("representative_anchor") is not None:
            anchors.append(event["review"]["representative_anchor"])
        for anchor in anchors:
            index = int(anchor["frame_index"]) - 1
            if index < 0 or index >= len(hashes):
                raise PointEventValidationError("review anchor lies outside the report")
            frame_hash = str(anchor.get("image_sha256", "")).lower()
            context = (
                frame_contexts[index]
                if index < len(frame_contexts)
                and isinstance(frame_contexts[index], Mapping)
                else {}
            )
            anchor_session = str(anchor.get("session_identity", "") or "")
            expected_session = str(context.get("session_id", "") or "")
            anchor_member = str(anchor.get("archive_member", "") or "").replace("\\", "/")
            expected_member = str(context.get("archive_member", "") or "").replace("\\", "/")
            anchor_path = str(anchor.get("frame_path", "") or "").replace("\\", "/")
            expected_path = str(context.get("frame_path", "") or "").replace("\\", "/")
            if (
                int(anchor["heartbeat_idx"]) != int(indices[index])
                or int(anchor["capture_sequence"]) != int(sequences[index])
                or frame_hash and frame_hash != str(hashes[index]).lower()
                or anchor_session and expected_session and anchor_session != expected_session
                or anchor_member and expected_member and anchor_member != expected_member
                or anchor_path and expected_path
                and anchor_path.rsplit("/", 1)[-1].lower()
                != expected_path.rsplit("/", 1)[-1].lower()
                or not math.isclose(float(anchor["elapsed_s"]), float(times[index]), abs_tol=0.0015)
            ):
                raise PointEventValidationError("review anchor provenance does not match the report")
    return copy.deepcopy(dict(document))


class PointEventSidecarStore:
    """Validated report-side API used by the localhost desktop bridge.

    The source ZIP remains read-only.  Every browser edit is reduced to a
    semantic command, checked against the report's dataset/frame provenance,
    then committed under ``report_root/annotations``.
    """

    def __init__(self, report_path: str | Path, session_path: str | Path) -> None:
        from .report_builder import load_report_payload

        self.report_path = Path(report_path).resolve()
        self.session_path = Path(session_path).resolve()
        self.payload = load_report_payload(self.report_path)
        dataset = self.payload.get("config", {}).get("dataset", {})
        self._expected_session_sha256 = str(
            dataset.get("source_archive_sha256", "")
        ).lower()
        if not self._expected_session_sha256:
            raise PointEventValidationError("report source archive hash is missing")
        self._session_archive: SessionArchive | None = None
        self._session_lock = threading.RLock()
        self._revision_lock = threading.RLock()
        raw_events = self.payload.get("config", {}).get("point_events", [])
        if not isinstance(raw_events, list):
            raise PointEventValidationError("report point-event payload is invalid")
        self.initial_events = tuple(map(validate_event, raw_events))
        self.dataset = copy.deepcopy(dict(dataset))
        self.store = RevisionStore(self.report_path.parent / "annotations")
        self._state = self.store.load(self.initial_events)
        self.annotation_metadata_path = self.store.directory / "rheed_annotation_set.json"
        self._annotation_metadata = self._load_annotation_metadata()
        self._update_summary_metadata()

    def _load_annotation_metadata(self) -> dict[str, Any]:
        if self.annotation_metadata_path.exists():
            try:
                value = json.loads(self.annotation_metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PointEventValidationError("annotation-set metadata is invalid") from exc
            if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
                raise PointEventValidationError("annotation-set metadata schema is invalid")
            if value.get("dataset_id") != self.dataset.get("dataset_id"):
                raise PointEventValidationError("annotation-set metadata belongs to another dataset")
            try:
                uuid.UUID(str(value.get("annotation_set_id", "")))
            except ValueError as exc:
                raise PointEventValidationError("annotation_set_id is invalid") from exc
            return value
        value = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": self.dataset.get("dataset_id", ""),
            "annotation_set_id": str(uuid.uuid4()),
            "reviewer": "",
            "model_outputs_visible": True,
            "eligible_for_gold": False,
        }
        RevisionStore._atomic_json(self.annotation_metadata_path, value)
        return value

    def _update_summary_metadata(self) -> None:
        try:
            summary = json.loads(self.store.summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PointEventValidationError("materialized point-event summary is invalid") from exc
        summary["annotation_set"] = copy.deepcopy(self._annotation_metadata)
        RevisionStore._atomic_json(self.store.summary_path, summary)

    @property
    def event_ids(self) -> tuple[str, ...]:
        with self._revision_lock:
            return tuple(sorted(self._state))

    @property
    def session_archive(self) -> SessionArchive | None:
        """Cached verified archive, or ``None`` before the first mutation/use."""

        return self._session_archive

    def ensure_session_verified(self) -> SessionArchive:
        """Hash/load the large ZIP once, immediately before a trusted action."""

        with self._session_lock:
            if self._session_archive is None:
                session = load_session_archive(self.session_path)
                if session.sha256 != self._expected_session_sha256:
                    raise PointEventValidationError(
                        "session ZIP does not match the report source archive"
                    )
                self._session_archive = session
            return self._session_archive

    @property
    def states(self) -> dict[str, dict[str, Any]]:
        with self._revision_lock:
            return copy.deepcopy(self._state)

    @property
    def revisions(self) -> tuple[dict[str, Any], ...]:
        with self._revision_lock:
            return tuple(copy.deepcopy(self.store.recover()))

    @property
    def annotation_metadata(self) -> dict[str, Any]:
        return copy.deepcopy(self._annotation_metadata)

    def atomic_state_snapshot(self) -> dict[str, Any]:
        """Return materialized events and their exact journal under one lock."""

        with self._revision_lock:
            records = self.store.recover()
            state = replay_revisions(
                self.initial_events, records, require_integrity=True,
            )
            self._state = copy.deepcopy(state)
            return {
                "events": [copy.deepcopy(state[key]) for key in sorted(state)],
                "revisions": copy.deepcopy(records),
                "annotation_set": copy.deepcopy(self._annotation_metadata),
            }

    def _assert_dataset(self, command: Mapping[str, Any]) -> None:
        supplied = str(command.get("dataset_id", ""))
        if supplied and supplied != str(self.dataset.get("dataset_id", "")):
            raise PointEventValidationError("revision targets a different dataset")

    def _assert_saved_anchor(self, anchor: Mapping[str, Any]) -> dict[str, Any]:
        checked = validate_review_anchor(anchor)
        index = checked["frame_index"] - 1
        if index < 0 or index >= len(self.payload["frame_sha256"]):
            raise PointEventValidationError("review anchor lies outside the report")
        supplied_hash = str(checked.get("image_sha256", "")).lower()
        if (
            int(checked["heartbeat_idx"]) != int(self.payload["heartbeat_indices"][index])
            or int(checked["capture_sequence"]) != int(self.payload["capture_sequences"][index])
            or supplied_hash
            and supplied_hash != str(self.payload["frame_sha256"][index]).lower()
            or not math.isclose(
                float(checked["elapsed_s"]), float(self.payload["times"][index]),
                abs_tol=0.0015,
            )
        ):
            raise PointEventValidationError(
                "review anchor does not identify an exact saved report frame"
            )
        session = self.ensure_session_verified()
        frame = session.frames[index]
        expected_session = _session_anchor_identity(session)
        supplied_session = str(checked.get("session_identity", "") or "")
        compatible_sessions = {
            expected_session,
            str(session.metadata.get("session_id", "") or ""),
            session.path.stem,
            session.sha256,
            f"sha256:{session.sha256}",
            str(self.dataset.get("dataset_id", "") or ""),
        } - {""}
        supplied_member = str(checked.get("archive_member", "") or "")
        resolved_member = (
            supplied_member
            if supplied_member in session.members
            else resolve_archived_path(session, supplied_member)
        ) if supplied_member else None
        supplied_path = str(checked.get("frame_path", "") or "")
        resolved_path = resolve_archived_path(session, supplied_path)
        path_name = supplied_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if (
            supplied_session and supplied_session not in compatible_sessions
            or supplied_member and resolved_member != frame.member
            or supplied_path
            and resolved_path != frame.member
            and path_name != frame.frame_name.lower()
        ):
            raise PointEventValidationError(
                "review anchor stable identity does not match the saved report frame"
            )
        normalized = dict(checked)
        normalized.update({
            "session_identity": expected_session,
            "frame_index": frame.frame_index + 1,
            "heartbeat_idx": frame.heartbeat_idx,
            "elapsed_s": frame.elapsed_s,
            "captured_at_utc": frame.captured_at_utc,
            "capture_sequence": frame.capture_sequence,
            "frame_path": frame.frame_name,
            "frame_name": frame.frame_name,
            "archive_member": frame.member,
        })
        return normalized

    def apply_revision(self, command: Mapping[str, Any]) -> dict[str, Any]:
        self.ensure_session_verified()
        with self._revision_lock:
            return self._apply_revision_locked(command)

    def apply_equalizer_revision(
        self,
        command: Mapping[str, Any],
        measurement: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Reject the removed v1 bridge explicitly."""

        raise PointEventValidationError(
            "Equalizer is not part of rheed-point-events-v3"
        )

    def import_document(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Import a static-browser Draft journal through the trusted bridge.

        Existing durable revisions must be an exact prefix.  New rows are
        re-emitted with the server's hash chain; source evidence and dataset
        identity are validated before the first append.
        """

        self.ensure_session_verified()
        with self._revision_lock:
            canonical_document = copy.deepcopy(dict(document))
            raw_revisions = canonical_document.get("revisions", [])
            if not isinstance(raw_revisions, list):
                raise PointEventValidationError("imported revisions must be an array")
            # Browser JSON number rendering is not byte-identical to Python's
            # canonical encoding (for example 1 versus 1.0).  The server
            # re-canonicalizes hashes from the supplied before snapshot, then
            # semantic replay still requires that snapshot to equal state.
            for raw in raw_revisions:
                if not isinstance(raw, dict):
                    raise PointEventValidationError("imported revision is invalid")
                raw.pop("record_sha256", None)
                raw.pop("previous_record_sha256", None)
                raw["before_sha256"] = (
                    _sha256_json(raw["before"])
                    if isinstance(raw.get("before"), Mapping)
                    else ""
                )
            checked = validate_point_event_document(canonical_document, self.payload)
            incoming = [copy.deepcopy(dict(item)) for item in checked["revisions"]]
            existing = self.store.recover()

            def semantic(record: Mapping[str, Any]) -> dict[str, Any]:
                return {
                    key: copy.deepcopy(value) for key, value in record.items()
                    if key not in {"record_sha256", "previous_record_sha256"}
                }

            if len(existing) > len(incoming) or any(
                semantic(current) != semantic(candidate)
                for current, candidate in zip(existing, incoming)
            ):
                raise PointEventValidationError(
                    "imported Draft history conflicts with durable sidecar revisions"
                )
            if any(
                str(item.get("action", "")) == "complete"
                for item in incoming[len(existing):]
            ):
                raise PointEventValidationError(
                    "static Draft imports cannot attest Complete actions"
                )
            annotation_set = checked["annotation_set"]
            if existing and str(annotation_set.get("annotation_set_id")) != str(
                self._annotation_metadata.get("annotation_set_id")
            ):
                raise PointEventValidationError(
                    "imported Draft belongs to another annotation set"
                )
            if not existing:
                # Persist identity before the first journal append.  If the
                # process stops mid-batch, retry sees the same annotation set
                # and resumes from the durable revision prefix.
                self._annotation_metadata.update({
                    "annotation_set_id": str(annotation_set["annotation_set_id"]),
                    "reviewer": str(annotation_set["reviewer"]),
                })
                RevisionStore._atomic_json(
                    self.annotation_metadata_path, self._annotation_metadata,
                )
            for candidate in incoming[len(existing):]:
                self._state = self.store.append(
                    semantic(candidate), initial_events=self.initial_events,
                )
            self._state = replay_revisions(
                self.initial_events, self.store.recover(), require_integrity=True,
            )
            self._update_summary_metadata()
            snapshot = self.atomic_state_snapshot()
            snapshot.update({
                "ok": True,
                "unfinished_count": sum(
                    event.get("status") != "Complete"
                    and event.get("review", {}).get("disposition") == "active"
                    for event in snapshot["events"]
                ),
            })
            return snapshot

    def _apply_revision_locked(
        self, command: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply one browser command and return its durable materialized state."""

        if not isinstance(command, Mapping):
            raise PointEventValidationError("revision command must be an object")
        self._assert_dataset(command)
        action = str(command.get("action", ""))
        actor = str(command.get("actor", "")).strip()
        changes = copy.deepcopy(dict(command.get("changes") or {}))
        if action == "create_posthoc":
            anchor = self._assert_saved_anchor(changes.get("anchor", {}))
            event = make_posthoc_event(anchor, actor=actor)
            revision = make_revision(
                None, event, action="create_posthoc", actor=actor,
                base_revision_id="",
            )
            event["revision_id"] = revision["revision_id"]
            revision["after"] = copy.deepcopy(event)
        else:
            event_id = str(command.get("event_id", ""))
            current = self._state.get(event_id)
            if current is None:
                raise PointEventValidationError("revision targets an unknown event")
            base = str(command.get("base_revision_id", ""))
            if base != str(current.get("revision_id", "")):
                raise PointEventValidationError("event changed since it was opened")
            if action == "move_anchor":
                changes = {"anchor": self._assert_saved_anchor(changes.get("anchor", {}))}
            elif action == "move_representative_anchor":
                changes = {"representative_anchor": self._assert_saved_anchor(
                    changes.get("representative_anchor", {})
                )}
            event, revision = revise_event(
                current, action=action, actor=actor, changes=changes,
                state=self._state,
            )
        candidate_state = {**self._state, event["event_id"]: event}
        derive_state_segments(
            candidate_state.values(),
            frame_count=int(self.dataset.get("frame_count", 0)),
            session_identity=str(self.dataset.get("dataset_id", "")),
        )
        self._state = self.store.append(
            revision, initial_events=self.initial_events,
        )
        # Return the exact hash-chained record that was persisted rather than
        # the pre-append semantic DTO.
        revision = self.store.recover()[-1]
        if actor and actor != self._annotation_metadata.get("reviewer"):
            self._annotation_metadata["reviewer"] = actor
            RevisionStore._atomic_json(
                self.annotation_metadata_path, self._annotation_metadata,
            )
        self._update_summary_metadata()
        return {
            "ok": True,
            "event": copy.deepcopy(self._state[event["event_id"]]),
            "revision": copy.deepcopy(revision),
            # The desktop/browser sends the Grower name once as the revision
            # actor.  Echo the durable session-level identity so the UI can
            # refresh without inventing a per-event reviewer field.
            "annotation_set": copy.deepcopy(self._annotation_metadata),
            "unfinished_count": sum(
                item.get("status") != "Complete"
                and item.get("review", {}).get("disposition") == "active"
                for item in self._state.values()
            ),
        }

    def export_document(
        self, *, annotation_set_id: str | None = None,
        reviewer: str | None = None,
    ) -> dict[str, Any]:
        # Keep the exported replay chain and materialized state from one
        # revision-lock snapshot.  Without this lock, an HTTP edit could land
        # between reading the journal and building the document.
        with self._revision_lock:
            records = self.store.recover()
            imported = self.payload.get("config", {})
            return point_event_document(
                dataset=self.dataset, events=self.initial_events,
                reference_events=imported.get("reference_events", []),
                unlinked_legacy_labels=imported.get("unlinked_legacy_labels", []),
                source_revisions=imported.get("source_event_revisions", []),
                source_journal=imported.get("source_event_journal"),
                revisions=records,
                annotation_set_id=(
                    annotation_set_id
                    or str(self._annotation_metadata["annotation_set_id"])
                ),
                reviewer=(
                    reviewer
                    if reviewer is not None
                    else str(self._annotation_metadata.get("reviewer", ""))
                    or "browser draft"
                ),
            )


__all__ = [
    "ACTIVE_EQUALIZER_BASES", "DOCUMENT_TYPE", "ImportedPointEvents",
    "LEGACY_SCHEMA_VERSION",
    "PointEventSidecarStore", "PointEventValidationError", "RevisionStore",
    "derive_state_segments",
    "SCHEMA_VERSION", "V2_INITIAL_STATE", "V2_SCHEMA_VERSION",
    "completion_errors",
    "deterministic_event_id", "frame_anchor", "import_point_events",
    "interval_errors",
    "make_posthoc_event", "make_revision", "new_posthoc_event_id",
    "point_event_document", "replay_revisions", "revise_event",
    "validate_equalizer_measurement", "validate_event",
    "validate_point_event_document", "validate_review_anchor",
    "validate_v2_point_event_document_read_only",
]
