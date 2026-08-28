"""Deterministic state replay for semantic RHEED event labels.

The acquisition and review layers record *points*: a reconstruction appears
or disappears, or the visible RHEED pattern becomes Good or Bad, at one saved
frame.  This module is the small, UI-independent layer that turns those points
into half-open state intervals.

Frame indices are one-based.  A segment ``[start_frame,
end_frame_exclusive)`` therefore contains an Anchor exactly when
``start_frame <= anchor.frame_index < end_frame_exclusive``.

Confirmed events at the same saved frame are applied atomically.  Pending,
rejected, dismissed, and deleted events remain evidence but never change the
replayed physical state.  ``Draft`` versus ``Complete`` is review workflow
metadata and deliberately does not alter the state preview.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from itertools import groupby
from typing import Iterable, Mapping, Sequence


RECONSTRUCTION_VALUES = frozenset({
    "one_by_one",
    "twinned_two_by_one",
    "c_six_by_two",
    "rt13",
    "htr",
})
CLARITY_VALUES = frozenset({"unknown", "good", "bad"})
ACTIVE_DECISIONS = frozenset({"accepted", "confirmed"})
INACTIVE_DECISIONS = frozenset({"pending", "rejected"})
DISPOSITIONS = frozenset({"active", "dismissed", "deleted"})
STATUSES = frozenset({"Draft", "Complete"})

_SEGMENT_NAMESPACE = uuid.UUID("877062c8-84f3-5bc5-9bd0-e032edcbaeb5")


class EventStateError(ValueError):
    """Base error for invalid state-replay inputs."""


class EventValidationError(EventStateError):
    """An event or initial state is structurally invalid."""


class TransitionValidationError(EventStateError):
    """A semantic event contradicts the state immediately before it."""


class AnchorValidationError(EventStateError):
    """An interval Anchor is missing its segment or lies outside that segment."""


@dataclass(frozen=True)
class RheedState:
    """Complete reconstruction-presence and visible-pattern clarity state."""

    reconstructions: frozenset[str]
    clarity: str = "unknown"


DEFAULT_INITIAL_STATE = RheedState(
    reconstructions=frozenset({"one_by_one"}),
    clarity="unknown",
)


@dataclass(frozen=True)
class EventLabel:
    """One editable semantic change attached to a saved-frame event."""

    kind: str
    change: str
    value: str
    label_id: str = ""


@dataclass(frozen=True)
class RheedEvent:
    """Minimal event record consumed by :func:`replay_event_states`."""

    event_id: str
    frame_index: int
    labels: tuple[EventLabel, ...]
    candidate_decision: str
    disposition: str
    status: str

    @property
    def changes_state(self) -> bool:
        return (
            self.candidate_decision in ACTIVE_DECISIONS
            and self.disposition == "active"
            and bool(self.labels)
        )


@dataclass(frozen=True)
class SegmentAnchor:
    """An independently stored representative saved frame for one segment."""

    segment_id: str
    frame_index: int
    capture_sequence: int | None = None
    image_sha256: str = ""


@dataclass(frozen=True)
class StateSegment:
    """A complete state on a one-based, half-open saved-frame interval."""

    segment_id: str
    start_frame: int
    end_frame_exclusive: int
    reconstructions: frozenset[str]
    clarity: str
    boundary_event_ids: tuple[str, ...]
    anchor: SegmentAnchor | None = None

    def contains_frame(self, frame_index: int) -> bool:
        return self.start_frame <= frame_index < self.end_frame_exclusive


@dataclass(frozen=True)
class ReplayResult:
    """Deterministic segments plus events retained only as inactive evidence."""

    segments: tuple[StateSegment, ...]
    inactive_event_ids: tuple[str, ...]

    def segment_at(self, frame_index: int) -> StateSegment:
        for segment in self.segments:
            if segment.contains_frame(frame_index):
                return segment
        raise IndexError(f"saved frame {frame_index} is outside the replay")


def _strict_positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise EventValidationError(f"{field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise EventValidationError(f"{field} must be a positive integer") from exc
    try:
        if float(value) != float(result):
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise EventValidationError(f"{field} must be a positive integer") from exc
    if result < 1:
        raise EventValidationError(f"{field} must be a positive integer")
    return result


def _strict_nonnegative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise EventValidationError(f"{field} must be a non-negative integer")
    try:
        result = int(value)
        if float(value) != float(result):
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise EventValidationError(
            f"{field} must be a non-negative integer"
        ) from exc
    if result < 0:
        raise EventValidationError(f"{field} must be a non-negative integer")
    return result


def _validate_state(value: RheedState | Mapping[str, object] | None) -> RheedState:
    if value is None:
        return DEFAULT_INITIAL_STATE
    if isinstance(value, RheedState):
        state = value
    elif isinstance(value, Mapping):
        raw_reconstructions = value.get("reconstructions", ())
        if isinstance(raw_reconstructions, str):
            raise EventValidationError("initial reconstructions must be a collection")
        try:
            reconstructions = frozenset(str(item) for item in raw_reconstructions)
        except TypeError as exc:
            raise EventValidationError(
                "initial reconstructions must be a collection"
            ) from exc
        state = RheedState(
            reconstructions=reconstructions,
            clarity=str(value.get("clarity", "unknown")),
        )
    else:
        raise EventValidationError("initial_state must be RheedState or a mapping")
    unknown = state.reconstructions - RECONSTRUCTION_VALUES
    if unknown:
        raise EventValidationError(
            "initial state contains unknown reconstruction(s): "
            + ", ".join(sorted(unknown))
        )
    if state.clarity not in CLARITY_VALUES:
        raise EventValidationError(
            "initial clarity must be unknown, good, or bad"
        )
    return RheedState(frozenset(state.reconstructions), state.clarity)


def _coerce_label(value: EventLabel | Mapping[str, object]) -> EventLabel:
    if isinstance(value, EventLabel):
        label = value
    elif isinstance(value, Mapping):
        label = EventLabel(
            kind=str(value.get("kind", "")),
            change=str(value.get("change", "")),
            value=str(value.get("value", "")),
            label_id=str(value.get("label_id", "")),
        )
    else:
        raise EventValidationError("event labels must be EventLabel objects or mappings")
    if label.kind == "reconstruction":
        if label.change not in {"appeared", "disappeared"}:
            raise EventValidationError(
                "reconstruction labels must be appeared or disappeared"
            )
        if label.value not in RECONSTRUCTION_VALUES:
            raise EventValidationError(
                f"unknown reconstruction value: {label.value or '<blank>'}"
            )
    elif label.kind == "pattern_clarity":
        if label.change != "became":
            raise EventValidationError("pattern-clarity labels must use became")
        if label.value not in {"good", "bad"}:
            raise EventValidationError("pattern clarity must become good or bad")
    else:
        raise EventValidationError(f"unknown event-label kind: {label.kind or '<blank>'}")
    return label


def _coerce_event(value: RheedEvent | Mapping[str, object]) -> RheedEvent:
    if isinstance(value, RheedEvent):
        event = value
    elif isinstance(value, Mapping):
        review = value.get("review")
        review_mapping = review if isinstance(review, Mapping) else {}
        raw_frame_index = value.get("frame_index")
        if raw_frame_index is None:
            anchor = review_mapping.get("anchor")
            if isinstance(anchor, Mapping):
                raw_frame_index = anchor.get("frame_index")
        raw_labels = value.get("labels", review_mapping.get("labels", ()))
        if isinstance(raw_labels, (str, bytes)) or not isinstance(raw_labels, Sequence):
            raise EventValidationError("event labels must be a sequence")
        event = RheedEvent(
            event_id=str(value.get("event_id", "")).strip(),
            frame_index=_strict_positive_int(raw_frame_index, field="frame_index"),
            labels=tuple(_coerce_label(item) for item in raw_labels),
            candidate_decision=str(value.get(
                "candidate_decision",
                review_mapping.get("candidate_decision", ""),
            )),
            disposition=str(value.get(
                "disposition", review_mapping.get("disposition", ""),
            )),
            status=str(value.get("status", "")),
        )
    else:
        raise EventValidationError("events must be RheedEvent objects or mappings")
    if not event.event_id:
        raise EventValidationError("event_id is required")
    frame_index = _strict_positive_int(event.frame_index, field="frame_index")
    labels = tuple(_coerce_label(item) for item in event.labels)
    if event.candidate_decision not in ACTIVE_DECISIONS | INACTIVE_DECISIONS:
        raise EventValidationError(
            "candidate_decision must be pending, confirmed, accepted, or rejected"
        )
    if event.disposition not in DISPOSITIONS:
        raise EventValidationError(
            "disposition must be active, dismissed, or deleted"
        )
    if event.status not in STATUSES:
        raise EventValidationError("status must be Draft or Complete")
    return RheedEvent(
        event_id=event.event_id,
        frame_index=frame_index,
        labels=labels,
        candidate_decision=event.candidate_decision,
        disposition=event.disposition,
        status=event.status,
    )


def _segment_id(
    *, session_identity: str, start_frame: int,
    boundary_event_ids: tuple[str, ...],
) -> str:
    identity = json.dumps(
        {
            "session_identity": str(session_identity),
            "start_frame": start_frame,
            "boundary_event_ids": list(boundary_event_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return str(uuid.uuid5(_SEGMENT_NAMESPACE, identity))


def _apply_atomic_boundary(
    state: RheedState,
    events: Sequence[RheedEvent],
    *,
    frame_index: int,
) -> RheedState:
    appeared: list[str] = []
    disappeared: list[str] = []
    clarity_changes: list[str] = []
    for event in events:
        for label in event.labels:
            if label.kind == "reconstruction":
                target = appeared if label.change == "appeared" else disappeared
                target.append(label.value)
            elif label.kind == "pattern_clarity":
                clarity_changes.append(label.value)

    duplicate_appeared = sorted({value for value in appeared if appeared.count(value) > 1})
    if duplicate_appeared:
        raise TransitionValidationError(
            f"frame {frame_index}: duplicate appeared transition for "
            + ", ".join(duplicate_appeared)
        )
    duplicate_disappeared = sorted({
        value for value in disappeared if disappeared.count(value) > 1
    })
    if duplicate_disappeared:
        raise TransitionValidationError(
            f"frame {frame_index}: duplicate disappeared transition for "
            + ", ".join(duplicate_disappeared)
        )
    contradictory = sorted(set(appeared) & set(disappeared))
    if contradictory:
        raise TransitionValidationError(
            f"frame {frame_index}: reconstruction cannot appear and disappear "
            "at the same boundary: " + ", ".join(contradictory)
        )

    already_present = sorted(set(appeared) & state.reconstructions)
    if already_present:
        raise TransitionValidationError(
            f"frame {frame_index}: reconstruction already present before appeared: "
            + ", ".join(already_present)
        )
    absent = sorted(set(disappeared) - state.reconstructions)
    if absent:
        raise TransitionValidationError(
            f"frame {frame_index}: reconstruction absent before disappeared: "
            + ", ".join(absent)
        )
    if len(clarity_changes) > 1:
        raise TransitionValidationError(
            f"frame {frame_index}: only one Good/Bad change is allowed"
        )
    next_clarity = state.clarity
    if clarity_changes:
        next_clarity = clarity_changes[0]
        if next_clarity == state.clarity:
            display = "Good" if next_clarity == "good" else "Bad"
            raise TransitionValidationError(
                f"frame {frame_index}: {display} cannot change to {display}"
            )

    next_reconstructions = (
        state.reconstructions - frozenset(disappeared)
    ) | frozenset(appeared)
    return RheedState(next_reconstructions, next_clarity)


def _coerce_anchor(value: SegmentAnchor | Mapping[str, object]) -> SegmentAnchor:
    if isinstance(value, SegmentAnchor):
        anchor = value
    elif isinstance(value, Mapping):
        raw_sequence = value.get("capture_sequence")
        if raw_sequence in (None, ""):
            capture_sequence = None
        else:
            capture_sequence = _strict_nonnegative_int(
                raw_sequence, field="anchor capture_sequence",
            )
        anchor = SegmentAnchor(
            segment_id=str(value.get("segment_id", "")).strip(),
            frame_index=_strict_positive_int(
                value.get("frame_index"), field="anchor frame_index",
            ),
            capture_sequence=capture_sequence,
            image_sha256=str(
                value.get("image_sha256", value.get("frame_sha256", ""))
            ).lower(),
        )
    else:
        raise AnchorValidationError(
            "anchors must be SegmentAnchor objects or mappings"
        )
    if not anchor.segment_id:
        raise AnchorValidationError("anchor segment_id is required")
    frame_index = _strict_positive_int(
        anchor.frame_index, field="anchor frame_index",
    )
    if anchor.capture_sequence is not None:
        capture_sequence = _strict_nonnegative_int(
            anchor.capture_sequence, field="anchor capture_sequence",
        )
    else:
        capture_sequence = None
    digest = anchor.image_sha256.lower()
    if digest and (
        len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise AnchorValidationError("anchor image_sha256 is invalid")
    return SegmentAnchor(
        segment_id=anchor.segment_id,
        frame_index=frame_index,
        capture_sequence=capture_sequence,
        image_sha256=digest,
    )


def _attach_anchors(
    segments: Sequence[StateSegment],
    anchors: Iterable[SegmentAnchor | Mapping[str, object]],
) -> tuple[StateSegment, ...]:
    by_segment: dict[str, SegmentAnchor] = {}
    for raw_anchor in anchors:
        try:
            anchor = _coerce_anchor(raw_anchor)
        except EventValidationError as exc:
            raise AnchorValidationError(str(exc)) from exc
        if anchor.segment_id in by_segment:
            raise AnchorValidationError(
                f"segment {anchor.segment_id} has more than one Anchor"
            )
        by_segment[anchor.segment_id] = anchor

    known_ids = {segment.segment_id for segment in segments}
    unknown_ids = sorted(set(by_segment) - known_ids)
    if unknown_ids:
        raise AnchorValidationError(
            "Anchor refers to unknown or obsolete segment(s): "
            + ", ".join(unknown_ids)
        )

    result: list[StateSegment] = []
    for segment in segments:
        anchor = by_segment.get(segment.segment_id)
        if anchor is not None and not segment.contains_frame(anchor.frame_index):
            raise AnchorValidationError(
                f"Anchor frame {anchor.frame_index} is outside segment "
                f"[{segment.start_frame}, {segment.end_frame_exclusive})"
            )
        result.append(replace(segment, anchor=anchor))
    return tuple(result)


def replay_event_states(
    events: Iterable[RheedEvent | Mapping[str, object]],
    *,
    frame_count: int,
    initial_state: RheedState | Mapping[str, object] | None = None,
    anchors: Iterable[SegmentAnchor | Mapping[str, object]] = (),
    session_identity: str = "",
) -> ReplayResult:
    """Replay semantic events into deterministic complete-state segments.

    Parameters
    ----------
    events:
        Flat :class:`RheedEvent` objects or mappings.  For integration with
        the journal schema, mappings may alternatively place ``labels``,
        ``candidate_decision``, ``disposition``, and ``anchor.frame_index``
        below ``review``.
    frame_count:
        Number of saved frames.  Valid event and Anchor indices are 1 through
        ``frame_count``.
    initial_state:
        Defaults to 1x1 present with unknown visible-pattern clarity.  The
        baseline is an explicit assumption, not an appearance event.
    anchors:
        Independent interval Anchor records keyed by deterministic
        ``segment_id``.  A first replay without anchors can be used to obtain
        those IDs.
    session_identity:
        Optional stable session ID included in deterministic segment IDs.
    """

    count = _strict_positive_int(frame_count, field="frame_count")
    current_state = _validate_state(initial_state)
    normalized = [_coerce_event(event) for event in events]

    identifiers: set[str] = set()
    for event in normalized:
        if event.event_id in identifiers:
            raise EventValidationError(f"duplicate event_id: {event.event_id}")
        identifiers.add(event.event_id)
        if event.frame_index > count:
            raise EventValidationError(
                f"event {event.event_id} frame {event.frame_index} exceeds "
                f"frame_count {count}"
            )

    active = sorted(
        (event for event in normalized if event.changes_state),
        key=lambda event: (event.frame_index, event.event_id),
    )
    inactive_ids = tuple(sorted(
        event.event_id for event in normalized if not event.changes_state
    ))

    segments: list[StateSegment] = []
    segment_start = 1
    boundary_ids: tuple[str, ...] = ()
    for frame_index, group in groupby(active, key=lambda event: event.frame_index):
        same_frame = tuple(group)
        if frame_index > segment_start:
            segments.append(StateSegment(
                segment_id=_segment_id(
                    session_identity=session_identity,
                    start_frame=segment_start,
                    boundary_event_ids=boundary_ids,
                ),
                start_frame=segment_start,
                end_frame_exclusive=frame_index,
                reconstructions=current_state.reconstructions,
                clarity=current_state.clarity,
                boundary_event_ids=boundary_ids,
            ))
        current_state = _apply_atomic_boundary(
            current_state, same_frame, frame_index=frame_index,
        )
        segment_start = frame_index
        boundary_ids = tuple(sorted(event.event_id for event in same_frame))

    segments.append(StateSegment(
        segment_id=_segment_id(
            session_identity=session_identity,
            start_frame=segment_start,
            boundary_event_ids=boundary_ids,
        ),
        start_frame=segment_start,
        end_frame_exclusive=count + 1,
        reconstructions=current_state.reconstructions,
        clarity=current_state.clarity,
        boundary_event_ids=boundary_ids,
    ))
    return ReplayResult(
        segments=_attach_anchors(segments, anchors),
        inactive_event_ids=inactive_ids,
    )


__all__ = [
    "ACTIVE_DECISIONS",
    "AnchorValidationError",
    "CLARITY_VALUES",
    "DEFAULT_INITIAL_STATE",
    "DISPOSITIONS",
    "EventLabel",
    "EventStateError",
    "EventValidationError",
    "INACTIVE_DECISIONS",
    "RECONSTRUCTION_VALUES",
    "ReplayResult",
    "RheedEvent",
    "RheedState",
    "STATUSES",
    "SegmentAnchor",
    "StateSegment",
    "TransitionValidationError",
    "replay_event_states",
]
