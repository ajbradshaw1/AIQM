"""Unit tests for deterministic RHEED event-to-state replay."""

from __future__ import annotations

import pytest

from gui.rheed_event_state import (
    AnchorValidationError,
    EventLabel,
    EventValidationError,
    RheedEvent,
    SegmentAnchor,
    TransitionValidationError,
    replay_event_states,
)


def event(
    event_id: str,
    frame_index: int,
    *labels: EventLabel,
    decision: str = "confirmed",
    disposition: str = "active",
    status: str = "Draft",
) -> RheedEvent:
    return RheedEvent(
        event_id=event_id,
        frame_index=frame_index,
        labels=tuple(labels),
        candidate_decision=decision,
        disposition=disposition,
        status=status,
    )


def appeared(value: str) -> EventLabel:
    return EventLabel("reconstruction", "appeared", value)


def disappeared(value: str) -> EventLabel:
    return EventLabel("reconstruction", "disappeared", value)


def clarity(value: str) -> EventLabel:
    return EventLabel("pattern_clarity", "became", value)


def quality(value: str) -> EventLabel:
    return EventLabel("surface_quality", "became", value)


def test_default_initial_state_is_one_by_one_bad_clarity_unknown_quality() -> None:
    first = replay_event_states([], frame_count=12, session_identity="session-a")
    second = replay_event_states([], frame_count=12, session_identity="session-a")

    assert len(first.segments) == 1
    segment = first.segments[0]
    assert (segment.start_frame, segment.end_frame_exclusive) == (1, 13)
    assert segment.reconstructions == frozenset({"one_by_one"})
    assert segment.clarity == "bad"
    assert segment.quality == "unknown"
    assert segment.boundary_event_ids == ()
    assert segment.anchor is None
    assert segment.segment_id == second.segments[0].segment_id


def test_unsorted_events_replay_into_complete_half_open_states() -> None:
    events = [
        event(
            "later", 7,
            disappeared("rt13"), appeared("htr"), clarity("bad"), quality("bad"),
            status="Complete",
        ),
        event(
            "earlier", 4,
            disappeared("one_by_one"), appeared("rt13"), clarity("good"), quality("good"),
        ),
    ]

    result = replay_event_states(events, frame_count=10)

    assert [
        (item.start_frame, item.end_frame_exclusive)
        for item in result.segments
    ] == [(1, 4), (4, 7), (7, 11)]
    assert [item.reconstructions for item in result.segments] == [
        frozenset({"one_by_one"}),
        frozenset({"rt13"}),
        frozenset({"htr"}),
    ]
    assert [item.clarity for item in result.segments] == [
        "bad", "good", "bad",
    ]
    assert [item.quality for item in result.segments] == [
        "unknown", "good", "bad",
    ]
    assert result.segment_at(6) == result.segments[1]
    assert result.segment_at(7) == result.segments[2]


def test_same_frame_events_are_one_atomic_boundary_and_order_independent() -> None:
    remove_initial = event("remove-initial", 3, disappeared("one_by_one"))
    add_target = event("add-target", 3, appeared("rt13"), clarity("good"))

    forward = replay_event_states(
        [remove_initial, add_target], frame_count=6, session_identity="run",
    )
    reverse = replay_event_states(
        [add_target, remove_initial], frame_count=6, session_identity="run",
    )

    assert len(forward.segments) == 2
    assert forward.segments[1].start_frame == 3
    assert forward.segments[1].reconstructions == frozenset({"rt13"})
    assert forward.segments[1].clarity == "good"
    assert forward.segments[1].boundary_event_ids == (
        "add-target", "remove-initial",
    )
    assert forward.segments == reverse.segments


def test_pending_rejected_deleted_and_dismissed_events_do_not_change_state() -> None:
    ignored = [
        event("pending", 2, disappeared("one_by_one"), decision="pending"),
        event("rejected", 3, appeared("rt13"), decision="rejected"),
        event("deleted", 4, appeared("htr"), disposition="deleted"),
        event("dismissed", 5, clarity("bad"), disposition="dismissed"),
        # A confirmed event without a semantic label is an unfinished point,
        # not a state boundary.
        event("empty", 6),
    ]

    result = replay_event_states(ignored, frame_count=8)

    assert len(result.segments) == 1
    assert result.segments[0].reconstructions == frozenset({"one_by_one"})
    assert result.segments[0].clarity == "bad"
    assert result.segments[0].quality == "unknown"
    assert result.inactive_event_ids == (
        "deleted", "dismissed", "empty", "pending", "rejected",
    )


@pytest.mark.parametrize(
    "events, message",
    [
        (
            [event("a", 3, appeared("rt13")), event("b", 3, appeared("rt13"))],
            "duplicate appeared",
        ),
        (
            [event("a", 2, appeared("rt13")), event("b", 4, appeared("rt13"))],
            "already present",
        ),
        (
            [event("a", 3, disappeared("rt13"))],
            "absent before disappeared",
        ),
        (
            [event("a", 3, appeared("rt13"), disappeared("rt13"))],
            "appear and disappear",
        ),
    ],
)
def test_invalid_reconstruction_transitions_are_rejected_atomically(
    events: list[RheedEvent], message: str,
) -> None:
    with pytest.raises(TransitionValidationError, match=message):
        replay_event_states(events, frame_count=8)


@pytest.mark.parametrize("value", ["good", "bad"])
def test_good_to_good_and_bad_to_bad_are_rejected(value: str) -> None:
    with pytest.raises(
        TransitionValidationError,
        match=rf"{value.title()} cannot change to {value.title()}",
    ):
        replay_event_states(
            [event("first", 2, clarity(value)), event("repeat", 5, clarity(value))],
            frame_count=8,
        )


@pytest.mark.parametrize("value", ["good", "bad"])
def test_quality_good_to_good_and_bad_to_bad_are_rejected(value: str) -> None:
    with pytest.raises(
        TransitionValidationError,
        match=rf"quality {value.title()} cannot change to {value.title()}",
    ):
        replay_event_states(
            [event("first", 2, quality(value)), event("repeat", 5, quality(value))],
            frame_count=8,
        )


def test_clarity_and_quality_change_atomically_but_independently() -> None:
    result = replay_event_states(
        [
            event("clarity", 3, clarity("good")),
            event("quality", 3, quality("bad")),
        ],
        frame_count=5,
    )

    assert result.segments[0].clarity == "bad"
    assert result.segments[0].quality == "unknown"
    assert result.segments[1].clarity == "good"
    assert result.segments[1].quality == "bad"


def test_anchor_is_independent_and_must_lie_inside_its_segment() -> None:
    bare = replay_event_states(
        [event("change", 4, disappeared("one_by_one"), appeared("rt13"))],
        frame_count=8,
        session_identity="anchor-run",
    )
    first_id, second_id = (segment.segment_id for segment in bare.segments)
    digest = "a" * 64

    anchored = replay_event_states(
        [event("change", 4, disappeared("one_by_one"), appeared("rt13"))],
        frame_count=8,
        session_identity="anchor-run",
        anchors=[
            SegmentAnchor(first_id, frame_index=3),
            {
                "segment_id": second_id,
                "frame_index": 4,
                "capture_sequence": 17,
                "image_sha256": digest,
            },
        ],
    )

    assert anchored.segments[0].anchor == SegmentAnchor(first_id, frame_index=3)
    assert anchored.segments[1].anchor == SegmentAnchor(
        second_id, frame_index=4, capture_sequence=17, image_sha256=digest,
    )

    with pytest.raises(AnchorValidationError, match="outside segment"):
        replay_event_states(
            [event("change", 4, disappeared("one_by_one"), appeared("rt13"))],
            frame_count=8,
            session_identity="anchor-run",
            anchors=[SegmentAnchor(first_id, frame_index=4)],
        )


def test_unknown_or_duplicate_anchor_segment_is_rejected() -> None:
    segment_id = replay_event_states([], frame_count=4).segments[0].segment_id
    with pytest.raises(AnchorValidationError, match="unknown or obsolete"):
        replay_event_states(
            [], frame_count=4,
            anchors=[SegmentAnchor("obsolete-segment", frame_index=2)],
        )
    with pytest.raises(AnchorValidationError, match="more than one Anchor"):
        replay_event_states(
            [], frame_count=4,
            anchors=[
                SegmentAnchor(segment_id, frame_index=1),
                SegmentAnchor(segment_id, frame_index=2),
            ],
        )


def test_current_journal_shaped_mapping_is_supported_without_gui_dependency() -> None:
    result = replay_event_states(
        [{
            "event_id": "journal-event",
            "status": "Complete",
            "review": {
                "anchor": {"frame_index": 2},
                "labels": [
                    {
                        "label_id": "editable-label",
                        "kind": "reconstruction",
                        "change": "appeared",
                        "value": "rt13",
                    },
                ],
                "candidate_decision": "confirmed",
                "disposition": "active",
            },
        }],
        frame_count=3,
    )

    assert result.segments[1].reconstructions == frozenset({
        "one_by_one", "rt13",
    })
    assert result.segments[1].boundary_event_ids == ("journal-event",)


def test_invalid_identity_frame_or_status_fails_closed() -> None:
    with pytest.raises(EventValidationError, match="duplicate event_id"):
        replay_event_states(
            [event("same", 2), event("same", 3)], frame_count=4,
        )
    with pytest.raises(EventValidationError, match="exceeds frame_count"):
        replay_event_states([event("late", 5)], frame_count=4)
    with pytest.raises(EventValidationError, match="status"):
        replay_event_states(
            [event("bad-status", 2, status="finished")], frame_count=4,
        )
