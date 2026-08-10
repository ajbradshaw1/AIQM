#!/usr/bin/env python3
"""Duplicate-frame handling across the acquisition consumers.

A camera cannot deliver a new image faster than its exposure time. At the
300000 us exposure measured on the Ch-MBE Manta (2026-08-06) that ceiling
is ~3.33 fps, and VmbCamera.read_frame() re-serves its cached frame for
any poll above it — silently, with no exception and no fault indicator
(27b010c).

Three consumers were named as receiving those duplicates: the heartbeat
log, the classifier, and the change detector. The first two already held
capture-identity guards, but on the direct path their keys were built
from synthesised values that changed every read, so the guards could
never fire. These tests pin both halves: that the guards now see repeating
identities, and that the change detector — which had no guard — is skipped
explicitly.
"""

from __future__ import annotations

import sys
import time as _real_time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui import workers as workers_module  # noqa: E402
from drivers.window_capture import CapturedFrame  # noqa: E402
from gui.state import CameraState  # noqa: E402
from gui.workers import ClassifierWorker, RheedCameraWorker  # noqa: E402


def _frame(value: int = 128) -> np.ndarray:
    return np.full((8, 8, 3), value, dtype=np.uint8)


def _capture(sequence: int, monotonic_ns: int) -> CapturedFrame:
    return CapturedFrame(
        image=_frame(),
        captured_at_utc="2026-08-10T12:00:00.000Z",
        captured_monotonic_ns=monotonic_ns,
        sequence=sequence,
        source_hwnd=0,
        width=8,
        height=8,
        backend="vimba",
    )


class _ScriptedCamera:
    """Camera returning a scripted sequence of captures, then stopping.

    ``captures`` is consumed one per read. Passing the same CapturedFrame
    twice models the over-trigger case: the driver re-serves what it
    already delivered, so the provenance is byte-identical rather than
    merely similar.
    """

    def __init__(self, worker, captures):
        self._worker = worker
        self._captures = list(captures)
        self._index = 0
        self.last_capture = None
        self.disconnected = False
        self.capture_geometry_id = "vimba:12bit:palette1"

    def connect(self):
        return None

    def read_frame(self):
        self.last_capture = self._captures[self._index]
        self._index += 1
        if self._index >= len(self._captures):
            # Let the loop emit this state, then exit.
            self._worker.running = False
        return self.last_capture.image

    def disconnect(self):
        self.disconnected = True


class _NoProvenanceCamera:
    """Backend that reports no last_capture — duplicates undetectable."""

    def __init__(self, worker, reads: int):
        self._worker = worker
        self._remaining = reads
        self.disconnected = False

    def connect(self):
        return None

    def read_frame(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._worker.running = False
        return _frame()

    def disconnect(self):
        self.disconnected = True


def _run(camera_factory, mode: str = "vimba") -> list[CameraState]:
    worker = RheedCameraWorker(mode=mode, poll_interval=0.0)
    worker._create_camera = lambda: camera_factory(worker)
    states: list[CameraState] = []
    worker.state_updated.connect(states.append)
    worker.run()
    return states


def test_reserved_frame_is_marked_duplicate() -> None:
    """The same capture delivered twice marks the second read a duplicate."""
    first = _capture(5001, 1_000)
    states = _run(lambda w: _ScriptedCamera(w, [first, first]))
    assert len(states) == 2, f"expected 2 states, got {len(states)}"
    assert states[0].is_duplicate is False, "first delivery flagged as a repeat"
    assert states[1].is_duplicate is True, "re-served frame not detected"


def test_distinct_exposures_are_not_duplicates() -> None:
    """Genuinely new exposures are never flagged."""
    states = _run(lambda w: _ScriptedCamera(
        w, [_capture(5001, 1_000), _capture(5002, 2_000)],
    ))
    assert [s.is_duplicate for s in states] == [False, False], (
        f"fresh exposures misflagged: {[s.is_duplicate for s in states]}"
    )


class _ScriptedClock:
    """Stands in for the ``time`` module with a scripted ``time()``.

    Only wall-clock time is scripted; perf_counter_ns, monotonic_ns and
    sleep delegate to the real module so the rest of the loop is
    unaffected. The FPS window is one real second wide, so without this
    a fast test never reaches the branch that computes the rate — and an
    assertion on the still-zero default would pass whether or not
    duplicates were excluded.
    """

    def __init__(self, values):
        self._values = list(values)
        self._last = self._values[-1] if self._values else 0.0

    def time(self) -> float:
        if self._values:
            self._last = self._values.pop(0)
        return self._last

    def __getattr__(self, name):
        return getattr(_real_time, name)


def test_fps_counts_exposures_not_reads() -> None:
    """A duplicate must not inflate the reported frame rate.

    Counting re-served frames made fps track the poll interval instead of
    the camera, which hid the very condition the number should surface.

    Five reads: one genuine exposure then four re-serves, with the clock
    scripted so the averaging window closes on the last of them. Excluding
    duplicates gives 1 frame / 1.0 s; counting them would give 5.0.
    """
    first = _capture(5001, 1_000)
    clock = _ScriptedClock([0.0, 0.1, 0.2, 0.3, 0.4, 1.0])
    original = workers_module.time
    workers_module.time = clock
    try:
        states = _run(lambda w: _ScriptedCamera(w, [first] * 5))
    finally:
        workers_module.time = original

    assert len(states) == 5, f"expected 5 states, got {len(states)}"
    assert [s.is_duplicate for s in states] == [False, True, True, True, True]
    # frame_number counts read attempts and still advances; fps must not.
    assert states[-1].frame_number == 5, states[-1].frame_number
    assert states[-1].fps == 1.0, (
        f"fps was {states[-1].fps}; 5.0 means duplicates were counted"
    )


def test_backend_without_provenance_never_claims_duplicate() -> None:
    """Where duplicates are undetectable, don't assert either way."""
    states = _run(lambda w: _NoProvenanceCamera(w, 3), mode="dummy")
    assert len(states) == 3
    assert all(s.is_duplicate is False for s in states), (
        "claimed duplicate detection on a backend with no capture identity"
    )


def test_classifier_guard_sees_a_repeating_key() -> None:
    """The classifier's existing skip-guard now actually fires.

    on_rheed_state builds ("capture", capture_sequence,
    captured_monotonic_ns). Before real provenance both components were
    minted fresh per read, so the key never repeated and the guard at
    `frame_key == self._last_classified_frame_key` was dead code.
    """
    worker = ClassifierWorker(ai_repo_root="/nowhere")
    duplicate = dict(
        frame=_frame(), frame_number=1,
        capture_sequence=5001, captured_monotonic_ns=1_000,
    )
    worker.on_rheed_state(CameraState(**duplicate))
    first_key = worker._latest_frame_key
    # Same exposure re-served: frame_number advances (it counts reads) but
    # the capture identity does not.
    worker.on_rheed_state(CameraState(**{**duplicate, "frame_number": 2}))
    assert worker._latest_frame_key == first_key, (
        f"key changed across a duplicate: {first_key} -> "
        f"{worker._latest_frame_key}"
    )
    worker.on_rheed_state(CameraState(
        frame=_frame(), frame_number=3,
        capture_sequence=5002, captured_monotonic_ns=2_000,
    ))
    assert worker._latest_frame_key != first_key, (
        "key did not change across a genuinely new exposure"
    )


def test_heartbeat_token_repeats_for_a_duplicate() -> None:
    """The heartbeat dedup token is stable across a re-served frame.

    Mirrors the tuple built in GrowthApp._on_heartbeat. Its
    captured_at_utc component used to be datetime.now() per read on the
    direct path, so the token always differed and the guard never fired.
    """
    def token(state: CameraState) -> tuple:
        return (
            state.capture_backend, state.source_hwnd,
            state.capture_sequence, state.captured_at_utc,
        )

    first = _capture(5001, 1_000)
    states = _run(lambda w: _ScriptedCamera(w, [first, first, _capture(5002, 2_000)]))
    assert token(states[0]) == token(states[1]), "token differed across a duplicate"
    assert token(states[1]) != token(states[2]), "token stable across a new exposure"


TESTS = [
    test_reserved_frame_is_marked_duplicate,
    test_distinct_exposures_are_not_duplicates,
    test_fps_counts_exposures_not_reads,
    test_backend_without_provenance_never_claims_duplicate,
    test_classifier_guard_sees_a_repeating_key,
    test_heartbeat_token_repeats_for_a_duplicate,
]


def main() -> int:
    failures = []
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
