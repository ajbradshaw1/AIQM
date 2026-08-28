"""Fail-closed validation for point events and legacy temporal segments."""

from __future__ import annotations

import copy
import math
from typing import Any


class AnnotationValidationError(ValueError):
    """The annotation document does not match the report provenance contract."""


LABELS = {"none_weak", "twinned_2x1", "c_6x2", "rt13", "htr", "unknown"}


def _fail(message: str) -> None:
    raise AnnotationValidationError(message)


def _validate_segment_annotation_document(document: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema_version") != "rheed-temporal-segments-v1" or document.get("document_type") != "ai4mbe_rheed_segment_annotations":
        _fail("Unsupported annotation document schema")
    try:
        dataset = payload["config"]["dataset"]
        count = int(payload["config"]["count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AnnotationValidationError("Invalid report payload") from exc
    incoming = document.get("dataset")
    identity_fields = (
        "dataset_id", "frame_count", "ordered_frame_fingerprint",
        "source_archive_sha256", "model_context_fingerprint",
    )
    if not isinstance(incoming, dict) or any(
        incoming.get(key) != dataset.get(key) for key in identity_fields
    ):
        _fail("Annotation dataset does not match this report")
    annotation_set = document.get("annotation_set")
    if not isinstance(annotation_set, dict) or not str(annotation_set.get("annotation_set_id", "")).strip() or not str(annotation_set.get("labeler", "")).strip():
        _fail("Annotation set identity and labeler are required")
    if annotation_set.get("annotation_source") != "human_assisted_temporal_segment" or annotation_set.get("annotation_mode") != "model_assisted_review" or annotation_set.get("model_outputs_visible") is not True or annotation_set.get("eligible_for_gold") is not False:
        _fail("Annotations must remain model-visible and ineligible for gold use")
    segments = document.get("segments")
    if not isinstance(segments, list): _fail("segments must be an array")
    ids: set[str] = set(); intervals: list[tuple[int, int]] = []
    expected = {
        "heartbeat_idx": payload["heartbeat_indices"], "capture_sequence": payload["capture_sequences"],
        "frame_sha256": payload["frame_sha256"], "elapsed_s": payload["times"],
    }
    first_utc = str(payload["config"]["start_capture_utc"])
    from datetime import datetime, timedelta, timezone
    def utc_at(index: int) -> str:
        base = datetime.fromisoformat(first_utc[:-1] + "+00:00" if first_utc.endswith("Z") else first_utc).astimezone(timezone.utc)
        value = base + timedelta(seconds=float(payload["capture_offsets"][index]))
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    for segment in segments:
        if not isinstance(segment, dict): _fail("Each segment must be an object")
        identifier = str(segment.get("annotation_id", ""))
        if not identifier or len(identifier) > 200 or identifier in ids: _fail("Annotation IDs must be unique")
        ids.add(identifier)
        if segment.get("model_outputs_visible") is not True or segment.get("eligible_for_gold") is not False or segment.get("annotation_source") != "human_assisted_temporal_segment":
            _fail("Segment safety metadata is invalid")
        if str(segment.get("label_code", "")) not in LABELS: _fail("Unknown reconstruction label")
        start, end = segment.get("start"), segment.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict): _fail("Segment endpoints are required")
        try:
            start_frame, end_frame = int(start["frame_index"]), int(end["frame_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnnotationValidationError("Segment frame bounds are invalid") from exc
        if start_frame < 1 or end_frame < start_frame or end_frame > count: _fail("Segment frame bounds are invalid")
        start_index, end_index = start_frame - 1, end_frame - 1
        if segment.get("frame_count") != end_frame - start_frame + 1: _fail("Segment frame_count is invalid")
        for endpoint, index in ((start, start_index), (end, end_index)):
            if int(endpoint.get("heartbeat_idx", -1)) != int(expected["heartbeat_idx"][index]) or int(endpoint.get("capture_sequence", -1)) != int(expected["capture_sequence"][index]) or str(endpoint.get("frame_sha256", "")).lower() != expected["frame_sha256"][index] or not math.isclose(float(endpoint.get("elapsed_s", math.nan)), float(expected["elapsed_s"][index]), abs_tol=0.0015):
                _fail("Segment endpoint provenance does not match this report")
            if endpoint.get("captured_at_utc") != utc_at(index): _fail("Segment endpoint UTC does not match this report")
        intervals.append((start_index, end_index + 1))
    intervals.sort()
    if any(previous[1] > current[0] for previous, current in zip(intervals, intervals[1:])):
        _fail("Segments overlap")
    return copy.deepcopy(document)


def validate_annotation_document(
    document: dict[str, Any], payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate the current point-event schema or legacy read-only segments."""

    if isinstance(document, dict) and document.get("schema_version") == "rheed-point-events-v2":
        from .point_events import PointEventValidationError, validate_point_event_document

        try:
            return validate_point_event_document(document, payload)
        except PointEventValidationError as exc:
            raise AnnotationValidationError(str(exc)) from exc
    return _validate_segment_annotation_document(document, payload)
