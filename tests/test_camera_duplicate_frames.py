#!/usr/bin/env python3
"""Duplicate-frame handling across the acquisition consumers.

A camera cannot deliver a new image faster than its exposure time. At the
300000 us exposure measured on the Ch-MBE Manta (2026-08-06) that ceiling
is ~3.33 fps, and VmbCamera.read_frame() re-serves its cached frame for
any poll above it — silently, with no exception and no fault indicator
(27b010c).

Four consumers receive those duplicates: the heartbeat log, the
classifier, the change detector and the intensity trace. The first two
already held capture-identity guards, but on the direct path their keys
were built from synthesised values that changed every read, so the guards
could never fire; the other two had no guard at all.

The heartbeat and classifier cases are behavioural — they drive the real
handler and the real run loop and count disk writes and inference calls,
rather than reconstructing a key and asserting it repeats. A token-shaped
test would pass against a consumer that computed the right key and then
ignored it.

Note on rates: production polls at 1 Hz against a ~3.33 fps ceiling, so
the default configuration does not normally over-poll. These paths matter
for stalls, misconfiguration, and any future faster cadence — not because
the current GUI is saturating the camera.
"""

from __future__ import annotations

import sys
import threading
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


class _StubBridge:
    """Counts inference calls; returns a well-formed score dict."""

    def __init__(self):
        self.calls = 0

    def classify(self, frame):
        from gui.recon_labels import RECON_LABELS
        self.calls += 1
        return {
            label: (100.0 if index == 0 else 0.0)
            for index, label in enumerate(RECON_LABELS)
        }


def _settled(predicate, timeout_s: float = 2.0) -> None:
    """Wait until predicate() holds, or fail with the deadline."""
    deadline = _real_time.monotonic() + timeout_s
    while _real_time.monotonic() < deadline:
        if predicate():
            return
        _real_time.sleep(0.01)
    raise AssertionError("condition not reached within timeout")


def test_classifier_runs_inference_once_per_exposure() -> None:
    """The classifier infers per exposure, not per delivered state.

    Behavioural: this drives the real run loop with a stub bridge and
    counts classify() calls. Its guard compares
    ("capture", capture_sequence, captured_monotonic_ns) against the last
    classified key — both components used to be minted fresh per read on
    the direct path, so the key never repeated and the guard was dead
    code. Inference is the most expensive thing in the pipeline, so a
    duplicate cost a full model pass to reproduce a result already held.
    """
    worker = ClassifierWorker(ai_repo_root="/nowhere")
    worker.POLL_INTERVAL_S = 0.01
    bridge = _StubBridge()
    worker._create_bridge = lambda: bridge

    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        def feed(sequence: int, monotonic_ns: int, number: int) -> None:
            worker.on_rheed_state(CameraState(
                frame=_frame(), frame_number=number, connected=True,
                valid=True, capture_sequence=sequence,
                captured_monotonic_ns=monotonic_ns,
            ))

        # One exposure, delivered four times.
        for number in range(1, 5):
            feed(5001, 1_000, number)
        _settled(lambda: bridge.calls >= 1)
        _real_time.sleep(0.1)     # give a wrong implementation time to add more
        assert bridge.calls == 1, (
            f"{bridge.calls} inferences for a single exposure"
        )

        # Three genuinely new exposures.
        for offset in range(1, 4):
            feed(5001 + offset, 1_000 * (offset + 1), 4 + offset)
            _settled(lambda o=offset: bridge.calls >= 1 + o)
        assert bridge.calls == 4, (
            f"expected 4 inferences across 4 exposures, got {bridge.calls}"
        )
    finally:
        worker.stop()
        thread.join(timeout=2.0)


class _GrowthLogStub:
    def __init__(self):
        self.active = True
        self.saved_frames = 0
        self.logged_heartbeats = 0
        self._heartbeat_counter = 0

    def save_heartbeat_frame(self, frame):
        self.saved_frames += 1
        self._heartbeat_counter += 1
        return f"/tmp/frame_{self.saved_frames:03d}.bmp"

    def log_heartbeat(self, **kwargs):
        self.logged_heartbeats += 1


class _MonitorStub:
    def __init__(self):
        self.frame = _frame()
        self.metadata: dict = {}
        self._latest_pyro = None
        self.capture_count = 0

    def get_current_frame(self):
        return self.frame

    def get_current_capture_metadata(self) -> dict:
        return dict(self.metadata)

    def get_elapsed_seconds(self) -> float:
        return 1.0

    def increment_continuous_capture_count(self) -> None:
        self.capture_count += 1


class _StatusBar:
    def showMessage(self, *args) -> None:
        return None


def test_heartbeat_writes_once_per_exposure() -> None:
    """_on_heartbeat writes one frame per exposure, not per tick.

    Behavioural rather than token-shaped: this drives the real handler and
    counts disk writes. Its dedup token includes captured_at_utc, which on
    the direct path used to be datetime.now() per read — so the token
    always differed, the guard never fired, and every tick wrote a fresh
    copy of the same image.
    """
    from gui.growth_app import GrowthApp

    app = GrowthApp.__new__(GrowthApp)
    app.growth_log = _GrowthLogStub()
    app.monitor = _MonitorStub()
    app._last_heartbeat_capture_token = None
    app.statusBar = lambda: _StatusBar()

    def metadata_for(capture: CapturedFrame) -> dict:
        return {
            "capture_backend": capture.backend,
            "source_hwnd": capture.source_hwnd,
            "capture_sequence": capture.sequence,
            "captured_at_utc": capture.captured_at_utc,
            "captured_monotonic_ns": capture.captured_monotonic_ns,
        }

    first = _capture(1, 1_000)
    app.monitor.metadata = metadata_for(first)
    app._on_heartbeat()
    app._on_heartbeat()          # same exposure, re-served
    app._on_heartbeat()
    assert app.growth_log.saved_frames == 1, (
        f"{app.growth_log.saved_frames} writes for one exposure"
    )
    assert app.growth_log.logged_heartbeats == 1

    # A genuinely new exposure must still be written.
    app.monitor.metadata = metadata_for(_capture(2, 2_000))
    app._on_heartbeat()
    assert app.growth_log.saved_frames == 2, (
        "a new exposure was suppressed as a duplicate"
    )
    assert app.growth_log.logged_heartbeats == 2


def test_intensity_trace_ignores_duplicates() -> None:
    """The intensity trace records measurements, not poll ticks.

    A duplicate would append the identical value at a later timestamp,
    making the trace report the poll rate as the acquisition rate and
    flattening real change across the repeat.
    """
    from gui.rheed_intensity_window import RheedIntensityWindow

    window = RheedIntensityWindow.__new__(RheedIntensityWindow)
    window._t0 = None
    window._times = []
    window._intensities = []

    class _Curve:
        def setData(self, *args) -> None:
            return None

    window._curve = _Curve()

    def state(intensity: float, duplicate: bool) -> CameraState:
        return CameraState(
            frame=_frame(), connected=True, valid=True,
            intensity=intensity, is_duplicate=duplicate,
        )

    window.on_camera_state(state(10.0, False))
    window.on_camera_state(state(10.0, True))
    window.on_camera_state(state(20.0, False))
    assert window._intensities == [10.0, 20.0], (
        f"duplicate entered the trace: {window._intensities}"
    )


# Discovered, not hand-listed — a list that drifts silently under-reports.
TESTS = [
    value for name, value in sorted(globals().items())
    if name.startswith("test_") and callable(value)
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
