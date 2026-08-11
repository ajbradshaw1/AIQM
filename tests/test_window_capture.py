#!/usr/bin/env python3
"""Platform-independent tests for the WGC latest-frame adapter."""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from drivers.window_capture import (  # noqa: E402
    CapturedFrame,
    WindowCaptureError,
    WindowCaptureTimeout,
    WindowsGraphicsCapture,
    bgra_to_rgb,
)
from gui.workers import RheedCameraWorker  # noqa: E402


class _Frame:
    def __init__(self, buffer: np.ndarray, timespan=None):
        self.frame_buffer = buffer
        self.timespan = timespan


class _Control:
    def __init__(self):
        self.stopped = False
        self.waited = False
        self.stop_count = 0
        self.wait_count = 0

    def stop(self):
        self.stopped = True
        self.stop_count += 1

    def wait(self):
        self.waited = True
        self.wait_count += 1


class _FakeCapture:
    instances: list["_FakeCapture"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.handlers = {}
        self.control = _Control()
        self.__class__.instances.append(self)

    def event(self, handler):
        self.handlers[handler.__name__] = handler
        return handler

    def start_free_threaded(self):
        bgra = np.zeros((4, 6, 4), dtype=np.uint8)
        bgra[:, :, 0] = 30
        bgra[:, :, 1] = 20
        bgra[:, :, 2] = 10
        bgra[:, :, 3] = 255
        self.handlers["on_frame_arrived"](_Frame(bgra), None)
        # Mutating the native-like source after the callback verifies that
        # the adapter copied it rather than retaining a borrowed view.
        bgra[:] = 0
        return self.control


class _SilentCapture(_FakeCapture):
    def start_free_threaded(self):
        return self.control


def test_bgra_conversion() -> None:
    bgra = np.array([[[3, 2, 1, 255]]], dtype=np.uint8)
    rgb = bgra_to_rgb(bgra)
    assert rgb.tolist() == [[[1, 2, 3]]]
    assert rgb.flags["C_CONTIGUOUS"]


def test_first_frame_and_hwnd_config() -> None:
    session = WindowsGraphicsCapture(
        1234, capture_factory=_FakeCapture, first_frame_timeout_s=0.1,
    )
    sample = session.start()
    fake = _FakeCapture.instances[-1]
    assert fake.kwargs["window_hwnd"] == 1234
    assert fake.kwargs["cursor_capture"] is False
    assert fake.kwargs["draw_border"] is False
    assert sample.sequence > 0
    assert sample.source_hwnd == 1234
    assert sample.backend == "wgc"
    assert sample.image.shape == (4, 6, 3)
    assert sample.image[0, 0].tolist() == [10, 20, 30]
    assert sample.captured_at_utc.endswith("Z")
    session.close()
    assert fake.control.stopped
    assert fake.control.waited


def test_sequence_continues_across_reconnect_and_close_is_idempotent() -> None:
    first_session = WindowsGraphicsCapture(2001, capture_factory=_FakeCapture)
    first = first_session.start()
    first_control = _FakeCapture.instances[-1].control
    first_session.close()
    first_session.close()
    assert first_control.stop_count == 1
    assert first_control.wait_count == 1

    second_session = WindowsGraphicsCapture(2001, capture_factory=_FakeCapture)
    second = second_session.start()
    second_session.close()
    assert second.sequence > first.sequence


def test_latest_frame_replaces_atomically() -> None:
    session = WindowsGraphicsCapture(9, capture_factory=_FakeCapture)
    first = session.start()
    fake = _FakeCapture.instances[-1]
    next_bgra = np.zeros((2, 3, 4), dtype=np.uint8)
    next_bgra[:, :, 2] = 99
    fake.handlers["on_frame_arrived"](_Frame(next_bgra), None)
    second = session.read_latest()
    assert second.sequence == first.sequence + 1
    assert second.image.shape == (2, 3, 3)
    assert second.image[0, 0].tolist() == [99, 0, 0]
    assert first.image.shape == (4, 6, 3)
    session.close()


def test_duplicate_native_timespan_is_not_counted_twice() -> None:
    session = WindowsGraphicsCapture(10, capture_factory=_FakeCapture)
    first = session.start()
    fake = _FakeCapture.instances[-1]
    bgra = np.zeros((2, 3, 4), dtype=np.uint8)
    fake.handlers["on_frame_arrived"](_Frame(bgra, timespan=777), None)
    unique = session.read_latest()
    fake.handlers["on_frame_arrived"](_Frame(bgra, timespan=777), None)
    duplicate = session.read_latest()
    assert unique.sequence == first.sequence + 1
    assert duplicate.sequence == unique.sequence
    session.close()


def test_first_frame_timeout() -> None:
    session = WindowsGraphicsCapture(
        7, capture_factory=_SilentCapture, first_frame_timeout_s=0.01,
    )
    try:
        session.start()
    except WindowCaptureTimeout:
        session.close()
        return
    raise AssertionError("expected first-frame timeout")


def test_stale_and_closed_fail_closed() -> None:
    session = WindowsGraphicsCapture(
        8, capture_factory=_FakeCapture, stale_timeout_s=0.005,
    )
    first = session.start()
    session._latest = replace(
        first, captured_monotonic_ns=time.monotonic_ns() - 10_000_000,
    )
    try:
        session.read_latest()
    except WindowCaptureTimeout:
        pass
    else:
        raise AssertionError("expected stale-frame timeout")

    fake = _FakeCapture.instances[-1]
    fake.handlers["on_closed"]()
    try:
        session.read_latest()
    except WindowCaptureError:
        session.close()
        return
    raise AssertionError("expected closed-session error")


def test_wgc_worker_stops_on_capture_error() -> None:
    class _FailedCamera:
        disconnected = False

        def connect(self):
            return None

        def read_frame(self):
            raise WindowCaptureError("simulated WGC loss")

        def disconnect(self):
            self.disconnected = True

    camera = _FailedCamera()
    worker = RheedCameraWorker(mode="screengrab", poll_interval=0.0)
    worker._create_camera = lambda: camera
    states = []
    worker.state_updated.connect(states.append)
    worker.run()
    assert len(states) == 1
    assert states[0].frame is None
    assert states[0].connected is False
    assert states[0].capture_backend == "wgc"
    assert "simulated WGC loss" in states[0].error
    assert camera.disconnected


def test_wgc_worker_cleans_up_after_connect_error() -> None:
    class _ConnectFailedCamera:
        disconnected = False

        def connect(self):
            raise RuntimeError("simulated invalid crop")

        def disconnect(self):
            self.disconnected = True

    camera = _ConnectFailedCamera()
    worker = RheedCameraWorker(mode="screengrab", poll_interval=0.0)
    worker._create_camera = lambda: camera
    states = []
    worker.state_updated.connect(states.append)
    worker.run()
    assert len(states) == 1
    assert states[0].connected is False
    assert "simulated invalid crop" in states[0].error
    assert camera.disconnected


def test_worker_propagates_dpi_bearing_capture_geometry_id() -> None:
    image = np.zeros((4, 6, 3), dtype=np.uint8)
    capture = CapturedFrame(
        image=image,
        captured_at_utc="2026-07-31T12:00:00.000Z",
        captured_monotonic_ns=time.monotonic_ns(),
        sequence=123,
        source_hwnd=456,
        width=6,
        height=4,
        backend="wgc",
    )

    class _OneFrameCamera:
        disconnected = False
        last_capture = capture
        capture_geometry_id = (
            "ksa-chrome-v2:1:75:30:applied:dpi-getdpiforwindow-144"
        )

        def connect(self):
            return None

        def read_frame(self):
            worker.running = False
            return image

        def disconnect(self):
            self.disconnected = True

    camera = _OneFrameCamera()
    worker = RheedCameraWorker(mode="screengrab", poll_interval=0.0)
    worker._create_camera = lambda: camera
    states = []
    worker.state_updated.connect(states.append)
    worker.run()
    assert len(states) == 1
    assert states[0].capture_geometry_id == camera.capture_geometry_id
    assert states[0].source_hwnd == 456
    assert states[0].capture_sequence == 123
    assert camera.disconnected


TESTS = [
    test_bgra_conversion,
    test_first_frame_and_hwnd_config,
    test_sequence_continues_across_reconnect_and_close_is_idempotent,
    test_latest_frame_replaces_atomically,
    test_duplicate_native_timespan_is_not_counted_twice,
    test_first_frame_timeout,
    test_stale_and_closed_fail_closed,
    test_wgc_worker_stops_on_capture_error,
    test_wgc_worker_cleans_up_after_connect_error,
    test_worker_propagates_dpi_bearing_capture_geometry_id,
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
