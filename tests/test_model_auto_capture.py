"""Offline tests for model-driven reconstruction-change capture."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import types

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui.auto_capture import (  # noqa: E402
    ReconstructionChangeCaptureEngine,
    classifier_prediction_from_state,
)


BASE_NS = 1_000_000_000_000


def _metadata(second: float, sequence: int) -> dict:
    return {
        "capture_backend": "fake-wgc",
        "captured_at_utc": f"2026-08-06T12:00:{sequence % 60:02d}Z",
        "captured_monotonic_ns": BASE_NS + int(second * 1_000_000_000),
        "capture_sequence": sequence,
        "source_hwnd": 42,
        "capture_geometry_id": "fake:42:8x8:v1",
        "camera_width": 8,
        "camera_height": 8,
        "session_id": "growth_test",
        "view_segment_id": 0,
        "visual_history_generation": 0,
        "gun_aligned": True,
        "realignment_active": False,
    }


def _state(second: float, sequence: int, scores: dict[str, float], **overrides):
    values = {
        "ready": True,
        "error": "",
        "last_frame_number": sequence,
        "raw_scores": scores,
        "quality": 0.9,
        "is_bad": False,
        "is_ood": False,
        "has_confident_data": True,
        "source_capture_sequence": sequence,
        "source_received_monotonic_ns": (
            BASE_NS + int(second * 1_000_000_000)
        ),
        "model_version": "model-a",
        "view_segment_id": 0,
        "visual_history_generation": 0,
        "gun_aligned": True,
        "model_input_mode": "single_frame",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _ingest_and_predict(
    engine: ReconstructionChangeCaptureEngine,
    second: float,
    sequence: int,
    scores: dict[str, float],
    **overrides,
) -> np.ndarray:
    frame = np.full((8, 8, 3), sequence % 250, dtype=np.uint8)
    engine.ingest_capture(frame, _metadata(second, sequence))
    prediction = classifier_prediction_from_state(
        _state(second, sequence, scores, **overrides),
    )
    engine.observe_prediction(prediction)
    return frame


def test_five_output_model_records_stable_change_with_trigger_roles():
    engine = ReconstructionChangeCaptureEngine(
        pre_window_s=2,
        post_window_s=2,
        sample_interval_s=1,
        stable_predictions=3,
    )
    events = []
    engine.event_ready.connect(events.append)
    engine.enabled = True
    one = {"1x1": 0.8, "Tw(2x1)": 0.05, "c(6x2)": 0.05, "RT13": 0.05, "HTR": 0.05}
    rt13 = {"1x1": 0.05, "Tw(2x1)": 0.05, "c(6x2)": 0.05, "RT13": 0.8, "HTR": 0.05}

    original = None
    for second, scores in enumerate([one, one, one, rt13, rt13, rt13]):
        original = _ingest_and_predict(engine, second, second + 1, scores)
    assert len(events) == 1
    event = events[0]
    assert event.previous_label == "1x1"
    assert event.new_label == "RT13"
    assert event.prediction.supports_1x1 is True
    payloads = event.capture_payloads()
    assert [metadata["frame_role"] for _frame, metadata in payloads] == [
        "pre", "pre", "trigger", "post", "post",
    ]
    assert [metadata["relative_to_trigger_s"] for _frame, metadata in payloads] == [
        -2.0, -1.0, 0.0, 1.0, 2.0,
    ]
    # Engine owns immutable copies; later camera-buffer reuse cannot alter them.
    assert original is not None
    original.fill(0)
    assert int(event.captures[-1].frame[0, 0, 0]) == 6


def test_four_output_model_does_not_synthesize_1x1():
    engine = ReconstructionChangeCaptureEngine(
        pre_window_s=1,
        post_window_s=1,
        stable_predictions=2,
    )
    events = []
    engine.event_ready.connect(events.append)
    engine.enabled = True
    tw = {"Tw(2x1)": 0.8, "c(6x2)": 0.1, "RT13": 0.05, "HTR": 0.05}
    rt13 = {"Tw(2x1)": 0.05, "c(6x2)": 0.05, "RT13": 0.85, "HTR": 0.05}
    for second, scores in enumerate([tw, tw, rt13, rt13]):
        _ingest_and_predict(engine, second, second + 1, scores)

    assert len(events) == 1
    prediction = events[0].prediction
    assert prediction.supports_1x1 is False
    assert "1x1" not in prediction.labels
    assert "1x1" not in prediction.score_dict()


def test_model_or_output_contract_change_relearns_baseline_without_event():
    engine = ReconstructionChangeCaptureEngine(
        pre_window_s=0,
        post_window_s=0,
        stable_predictions=2,
    )
    events = []
    engine.event_ready.connect(events.append)
    engine.enabled = True
    four_a = {"A": 0.9, "B": 0.1}
    five_b = {"A": 0.05, "B": 0.8, "C": 0.05}
    for second, scores in enumerate([four_a, four_a, five_b, five_b]):
        _ingest_and_predict(engine, second, second + 1, scores)
    assert events == []
    assert engine.baseline_label == "B"


def test_duplicate_and_invalid_predictions_do_not_satisfy_stability():
    engine = ReconstructionChangeCaptureEngine(
        pre_window_s=0,
        post_window_s=0,
        stable_predictions=2,
    )
    events = []
    engine.event_ready.connect(events.append)
    engine.enabled = True
    a = {"A": 0.9, "B": 0.1}
    b = {"A": 0.1, "B": 0.9}
    _ingest_and_predict(engine, 0, 1, a)
    _ingest_and_predict(engine, 1, 2, a)
    _ingest_and_predict(engine, 2, 3, b)
    engine.observe_prediction(classifier_prediction_from_state(_state(2, 3, b)))
    _ingest_and_predict(engine, 3, 4, b, is_ood=True)
    _ingest_and_predict(engine, 4, 5, b)
    assert events == []
    _ingest_and_predict(engine, 5, 6, b)
    assert len(events) == 1


def test_default_window_emits_121_one_hz_frames_only_after_plus_60_seconds():
    engine = ReconstructionChangeCaptureEngine()
    events = []
    engine.event_ready.connect(events.append)
    engine.enabled = True
    a = {"A": 0.9, "B": 0.1}
    b = {"A": 0.1, "B": 0.9}
    sequence = 1
    for second in range(-60, 0):
        _ingest_and_predict(engine, second, sequence, a)
        sequence += 1
    for second in range(0, 3):
        _ingest_and_predict(engine, second, sequence, b)
        sequence += 1
    assert events == []
    for second in range(3, 60):
        _ingest_and_predict(engine, second, sequence, b)
        sequence += 1
    assert events == []
    _ingest_and_predict(engine, 60, sequence, b)
    assert len(events) == 1
    event = events[0]
    assert len(event.captures) == 121
    relative = [
        metadata["relative_to_trigger_s"]
        for _frame, metadata in event.capture_payloads()
    ]
    assert relative[0] == -60.0
    assert relative[-1] == 60.0


def test_reset_cancels_incomplete_post_window():
    engine = ReconstructionChangeCaptureEngine(
        pre_window_s=1,
        post_window_s=10,
        stable_predictions=2,
    )
    events = []
    engine.event_ready.connect(events.append)
    engine.enabled = True
    a = {"A": 0.9, "B": 0.1}
    b = {"A": 0.1, "B": 0.9}
    for second, scores in enumerate([a, a, b, b]):
        _ingest_and_predict(engine, second, second + 1, scores)
    assert engine.pending_event_count == 1
    engine.reset()
    assert engine.pending_event_count == 0
    assert engine.sampled_frame_count == 0
    assert events == []


def test_overlapping_changes_complete_as_independent_windows():
    engine = ReconstructionChangeCaptureEngine(
        pre_window_s=1,
        post_window_s=5,
        stable_predictions=2,
    )
    events = []
    engine.event_ready.connect(events.append)
    engine.enabled = True
    scores = {
        "A": {"A": 0.9, "B": 0.05, "C": 0.05},
        "B": {"A": 0.05, "B": 0.9, "C": 0.05},
        "C": {"A": 0.05, "B": 0.05, "C": 0.9},
    }
    labels = ["A", "A", "B", "B", "C", "C", "C", "C", "C", "C"]
    for second, label in enumerate(labels):
        _ingest_and_predict(engine, second, second + 1, scores[label])

    assert [(event.previous_label, event.new_label) for event in events] == [
        ("A", "B"),
        ("B", "C"),
    ]
    assert events[0].prediction.source_capture_sequence == 3
    assert events[1].prediction.source_capture_sequence == 5


def test_sampler_keeps_at_most_one_frame_per_one_second_slot():
    engine = ReconstructionChangeCaptureEngine(
        pre_window_s=2,
        post_window_s=0,
        sample_interval_s=1,
        stable_predictions=1,
    )
    engine.enabled = True
    for index, second in enumerate((0.0, 0.5, 1.0, 1.5, 2.0), start=1):
        frame = np.full((2, 2, 3), index, dtype=np.uint8)
        engine.ingest_capture(frame, _metadata(second, index))
    assert engine.sampled_frame_count == 3
