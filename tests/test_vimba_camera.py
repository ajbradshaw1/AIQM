#!/usr/bin/env python3
"""End-to-end test of VmbCamera against an in-process vmbpy mock.

Validates the streaming-callback pattern, connect/disconnect lifecycle,
palette conversion (the historically-buggy (0,I,0) → correct (I,I,I)
fix), and error propagation — all Mac-side, without needing the Allied
Vision Vimba X SDK or a real camera.

Approach: replace ``vmbpy`` in ``sys.modules`` with a fake module whose
``VmbSystem``/camera classes accept the SDK calls VmbCamera makes,
produce mock frames on demand, and record which methods were called
so tests can assert on the driver's behavior.

Usage:
    python -m pytest -q tests/test_vimba_camera.py

Exits 0 on success; raises AssertionError with a diagnostic on failure.

Built Jul 2 2026 as the pre-lab safety net for the direct-camera path.
If this passes on Mac, VmbCamera's threading + palette + error paths
work; anything that fails at Bulbasaur is then a real-SDK problem, not
a driver-logic problem.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Fake vmbpy — minimal shape that VmbCamera exercises
# ---------------------------------------------------------------------------

class FakeFrame:
    """Fake vmbpy Frame — mimics ``as_numpy_ndarray()`` returning a 2D view."""

    def __init__(self, img: np.ndarray):
        self._img = img

    def as_numpy_ndarray(self) -> np.ndarray:
        return self._img


class FakeVmbCameraError(Exception):
    """Mimics vmbpy.VmbCameraError for access-mode denial tests.

    The driver's `_run_one_session` catches `vmbpy.VmbCameraError` at both
    `set_access_mode` and `__enter__` and wraps it in `_AccessDenialError`.
    This fake is what the driver's `except` clause sees under test.
    """


class FakeAccessMode:
    """Mimics vmbpy.AccessMode with `Full` / `Read` attributes.

    Values are string sentinels so tests can assert on which mode the
    driver requested (`cam.set_access_mode(vmbpy.AccessMode.Full)`
    resolves to the string `"AccessMode.Full"` on the fake, easy to log
    and inspect).
    """
    Full = "AccessMode.Full"
    Read = "AccessMode.Read"


class FakeSettable:
    """Camera setting proxy — captures which values were set for assertions.

    Supports the writable-bit gate the driver uses in Read mode:
    `get_access_mode() -> (readable, writable)`. Default `(True, True)`
    preserves the existing pre-eccba75 behavior, so historical tests still
    fire their `.set()` calls unchanged. Optional `get_access_mode_raises`
    injects an exception at the probe boundary to test constraint 3's
    "propagate in Full, skip in Read" rule.
    """

    def __init__(
        self,
        readable: bool = True,
        writable: bool = True,
        get_access_mode_raises: Exception | None = None,
        initial_value=None,
        value_range: tuple[float, float] | None = None,
        increment: float | None = None,
    ):
        self.value: object = initial_value
        self.set_call_count = 0
        self._readable = readable
        self._writable = writable
        self._get_access_mode_raises = get_access_mode_raises
        self._value_range = value_range
        self._increment = increment

    def set(self, value) -> None:  # noqa: A003
        self.value = value
        self.set_call_count += 1

    def get_access_mode(self) -> tuple[bool, bool]:
        if self._get_access_mode_raises is not None:
            raise self._get_access_mode_raises
        return (self._readable, self._writable)

    def get(self):
        return self.value

    def get_range(self):
        return self._value_range

    def get_increment(self):
        return self._increment


class FakeTriggerSoftware:
    """Represents cam.TriggerSoftware — exposes a run() that produces a frame.

    Each ``run()`` call either produces a frame (via the frame handler),
    raises the queued ``raise_on_run`` exception once, or does nothing if
    the camera is in a "silent" mode.
    """

    def __init__(self, camera: "FakeCamera"):
        self._camera = camera

    def run(self) -> None:
        self._camera.trigger_run_count += 1
        exc = self._camera.raise_on_run.pop(0) if self._camera.raise_on_run else None
        if exc is not None:
            raise exc
        if self._camera.silent:
            return
        # Produce one mock frame and pass it through the registered handler.
        # The frame's shape matches G-033B: 656x492 uint16 (12-bit values
        # stored in the low bits — but we use raw uint16 for simplicity).
        img = np.full(
            self._camera.frame_shape, self._camera.frame_value, dtype=np.uint16,
        )
        if self._camera.handler is not None:
            self._camera.handler(self._camera, None, FakeFrame(img))


class FakeCamera:
    """Fake vmbpy Camera — context-manager entry + streaming lifecycle.

    Supports three independent access-denial injection flags per constraint 2
    of the plan:

    * `refuse_full_on_set_access_mode` — `set_access_mode(Full)` raises
      FakeVmbCameraError (proves the driver catches at that boundary).
    * `refuse_full_on_enter` — `__enter__` raises FakeVmbCameraError when
      the last set mode was Full.
    * `refuse_read_on_enter` — `__enter__` raises FakeVmbCameraError when
      the last set mode was Read (proves the "both modes failed" combined
      error path).

    `last_set_access_mode` records the most recent `set_access_mode` call
    so tests can verify the driver's auto-mode retry actually asked for
    Read after Full was refused.
    """

    def __init__(
        self,
        trigger_source_writable: bool = True,
        trigger_selector_writable: bool = True,
        trigger_mode_writable: bool = True,
        acquisition_mode_writable: bool = True,
    ):
        self.TriggerSource = FakeSettable(writable=trigger_source_writable)
        self.TriggerSelector = FakeSettable(writable=trigger_selector_writable)
        self.TriggerMode = FakeSettable(writable=trigger_mode_writable)
        self.AcquisitionMode = FakeSettable(writable=acquisition_mode_writable)
        self.ExposureTimeAbs = FakeSettable(
            initial_value=300_000.0,
            value_range=(100.0, 900_000.0),
            increment=1.0,
        )
        self.ExposureAuto = FakeSettable(initial_value="Off")
        self.AcquisitionFrameRateLimit = FakeSettable(initial_value=3.3323)
        self.TriggerSoftware = FakeTriggerSoftware(self)

        self.handler = None
        self.start_streaming_calls = 0
        self.stop_streaming_calls = 0
        self.queued_frames: list = []
        self.trigger_run_count = 0
        self.raise_on_run: list[Exception] = []

        # Frame shape + fill value — tests can override before triggering.
        self.frame_shape = (492, 656)
        self.frame_value = 2048  # mid-12-bit value

        # Modes: silent → run() doesn't dispatch to handler
        self.silent = False

        # Access-mode injection state (constraint 2 of the plan).
        self.last_set_access_mode: str = ""
        self.set_access_mode_calls: list[str] = []
        self.refuse_full_on_set_access_mode: bool = False
        self.refuse_full_on_enter: bool = False
        self.refuse_read_on_enter: bool = False

        # Context-manager: __enter__ returns self, __exit__ is a no-op
        self.entered = False
        self.exited = False

    def set_access_mode(self, mode: str) -> None:
        """Record the requested mode; raise if the refuse flag is set for Full.

        The driver calls this BEFORE entering `with cam:`, so this is the
        first of the two boundaries where access denial can surface.
        """
        self.set_access_mode_calls.append(mode)
        if (
            self.refuse_full_on_set_access_mode
            and mode == FakeAccessMode.Full
        ):
            raise FakeVmbCameraError(
                "fake: set_access_mode(Full) refused"
            )
        self.last_set_access_mode = mode

    def __enter__(self) -> "FakeCamera":
        if (
            self.last_set_access_mode == FakeAccessMode.Full
            and self.refuse_full_on_enter
        ):
            raise FakeVmbCameraError(
                "fake: __enter__ refused for AccessMode.Full"
            )
        if (
            self.last_set_access_mode == FakeAccessMode.Read
            and self.refuse_read_on_enter
        ):
            raise FakeVmbCameraError(
                "fake: __enter__ refused for AccessMode.Read"
            )
        self.entered = True
        return self

    def __exit__(self, *exc_info) -> None:
        self.exited = True

    def start_streaming(self, handler) -> None:
        self.start_streaming_calls += 1
        self.handler = handler

    def stop_streaming(self) -> None:
        self.stop_streaming_calls += 1
        self.handler = None

    def queue_frame(self, frame) -> None:
        self.queued_frames.append(frame)


class FakeVmbSystem:
    """Fake vmbpy VmbSystem — get_instance() returns a context manager."""

    _cameras: list[FakeCamera] = []
    _raise_on_get_all_cameras: Exception | None = None

    @classmethod
    def get_instance(cls) -> "FakeVmbSystem":
        return cls()

    def __enter__(self) -> "FakeVmbSystem":
        return self

    def __exit__(self, *exc_info) -> None:
        pass

    def get_all_cameras(self) -> list[FakeCamera]:
        if FakeVmbSystem._raise_on_get_all_cameras is not None:
            raise FakeVmbSystem._raise_on_get_all_cameras
        return list(FakeVmbSystem._cameras)


def install_fake_vmbpy(
    cameras: list[FakeCamera] | None = None,
) -> FakeCamera | None:
    """Install a fake vmbpy module with the given cameras.

    Returns the first camera if any exist (for the common one-camera happy
    path), or None if the caller explicitly installed an empty list (to
    exercise the "no cameras found" error path).

    The fake module exposes `VmbSystem`, `AccessMode`, and `VmbCameraError`
    — the three vmbpy names the driver references at runtime.
    """
    if cameras is None:
        cameras = [FakeCamera()]
    FakeVmbSystem._cameras = cameras
    FakeVmbSystem._raise_on_get_all_cameras = None
    fake = types.SimpleNamespace(
        VmbSystem=FakeVmbSystem,
        AccessMode=FakeAccessMode,
        VmbCameraError=FakeVmbCameraError,
    )
    sys.modules["vmbpy"] = fake
    return cameras[0] if cameras else None


def _reset_driver_globals() -> None:
    """Clear the process-level camera-lease registry between cases.

    Several tests connect without disconnecting, so their stream thread is
    still alive in the idle loop when the next case starts — and the lease
    guard would (correctly) refuse it. Tearing down the fake SDK stands for
    the process ending, so the registry resets with it.
    """
    try:
        from drivers.rheed_camera import _reset_camera_leases
    except Exception:  # noqa: BLE001
        return
    _reset_camera_leases()


def uninstall_fake_vmbpy() -> None:
    sys.modules.pop("vmbpy", None)
    FakeVmbSystem._cameras = []
    FakeVmbSystem._raise_on_get_all_cameras = None
    _reset_driver_globals()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_palette_intensity_in_all_channels() -> None:
    """With apply_palette=False, _to_rgb_uint8 writes intensity into R, G, B (I,I,I)."""
    from drivers.rheed_camera import VmbCamera
    cam = VmbCamera(apply_palette=False)
    mono = np.array([[0, 128, 255]], dtype=np.uint8)
    rgb = cam._to_rgb_uint8(mono)
    assert rgb.shape == (1, 3, 3), f"expected (1, 3, 3), got {rgb.shape}"
    assert (rgb[:, :, 0] == mono).all(), f"R != intensity: {rgb[:, :, 0]}"
    assert (rgb[:, :, 1] == mono).all(), f"G != intensity: {rgb[:, :, 1]}"
    assert (rgb[:, :, 2] == mono).all(), f"B != intensity: {rgb[:, :, 2]}"
    L = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    assert (np.round(L).astype(np.uint8) == mono).all(), (
        f"L != intensity: L={L.round().astype(int).tolist()} vs {mono.tolist()}"
    )


def test_palette_bgw_output() -> None:
    """With apply_palette=True (default), _to_rgb_uint8 maps intensity through the kSA BGW LUT."""
    from drivers.rheed_camera import VmbCamera
    cam = VmbCamera(apply_palette=True)
    # value 64 → Black→Green ramp (indices 0-127): R=0, B=0, G>0
    low = np.array([[64]], dtype=np.uint8)
    rgb_low = cam._to_rgb_uint8(low)
    assert rgb_low.shape == (1, 1, 3), f"expected (1, 1, 3), got {rgb_low.shape}"
    assert rgb_low[0, 0, 0] == 0, f"R=0 expected in Black→Green ramp, got {rgb_low[0, 0, 0]}"
    assert rgb_low[0, 0, 2] == 0, f"B=0 expected in Black→Green ramp, got {rgb_low[0, 0, 2]}"
    assert rgb_low[0, 0, 1] > 0, f"G>0 expected at value 64, got {rgb_low[0, 0, 1]}"
    # value 200 → Green→White ramp (indices 128-255): R>0, G=255, B>0
    high = np.array([[200]], dtype=np.uint8)
    rgb_high = cam._to_rgb_uint8(high)
    assert all(rgb_high[0, 0, c] > 0 for c in range(3)), (
        f"All channels >0 expected in Green→White ramp, got {rgb_high[0, 0]}"
    )
    assert rgb_high[0, 0, 1] == 255, (
        f"G=255 expected in Green→White ramp, got {rgb_high[0, 0, 1]}"
    )


def test_normalization_fixed_denominator() -> None:
    """12-bit uint16 input normalizes with /(2^12 - 1), not per-frame max."""
    from drivers.rheed_camera import VmbCamera
    cam = VmbCamera(bit_depth=12, apply_palette=False)
    # Two frames with different peak values but the same raw pixel value at [0, 0]
    frame_low = np.array([[2048]], dtype=np.uint16)
    frame_high = np.array([[[2048, 4095]]], dtype=np.uint16).reshape(1, 2)
    rgb_low = cam._to_rgb_uint8(frame_low)
    rgb_high = cam._to_rgb_uint8(frame_high)
    # The [0, 0] pixel MUST have the same value in both frames because the
    # denominator is fixed (per-frame max would make them differ).
    assert rgb_low[0, 0, 0] == rgb_high[0, 0, 0], (
        f"per-frame max normalization leaked: {rgb_low[0, 0, 0]} vs "
        f"{rgb_high[0, 0, 0]} — normalization is not fixed"
    )
    # Value is (2048/4095 * 255) → 127.506... → truncates to 127 via
    # astype(np.uint8). If we ever change to rounding this becomes 128 —
    # both are acceptable, but pin the current behavior explicitly.
    assert rgb_low[0, 0, 0] == 127, f"expected 127 (trunc), got {rgb_low[0, 0, 0]}"


def test_connect_then_read_frame() -> None:
    """After connect() + one trigger cycle, read_frame() returns a valid RGB frame."""
    try:
        fake_cam = install_fake_vmbpy()
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0)  # fast trigger so a frame arrives quickly
        cam.connect()
        assert cam.connected
        # Give the stream loop a moment to trigger a few times.
        for _ in range(50):
            if fake_cam.start_streaming_calls > 0 and fake_cam.trigger_run_count > 0:
                break
            time.sleep(0.01)
        # Wait briefly for the frame handler to process at least one frame.
        for _ in range(50):
            with cam._frame_lock:
                if cam._latest_frame is not None:
                    break
            time.sleep(0.01)
        rgb = cam.read_frame()
        assert rgb.shape == (492, 656, 3), f"unexpected shape: {rgb.shape}"
        assert rgb.dtype == np.uint8, f"unexpected dtype: {rgb.dtype}"
        # Verify trigger config was actually set
        assert fake_cam.TriggerSource.value == "Software"
        assert fake_cam.TriggerMode.value == "On"
        assert fake_cam.AcquisitionMode.value == "Continuous"
        cam.disconnect()
        assert not cam.connected
        assert fake_cam.stop_streaming_calls >= 1, "stop_streaming was not called"
    finally:
        uninstall_fake_vmbpy()


def test_connect_no_cameras_raises() -> None:
    """No cameras found → RuntimeError with a clear diagnostic."""
    try:
        install_fake_vmbpy(cameras=[])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera()
        try:
            cam.connect()
        except RuntimeError as exc:
            assert "no allied vision cameras found" in str(exc).lower(), (
                f"unexpected message: {exc}"
            )
            return
        raise AssertionError("expected RuntimeError, none raised")
    finally:
        uninstall_fake_vmbpy()


def test_connect_import_error_raises_early() -> None:
    """If vmbpy isn't importable, connect() raises ImportError before spawning thread."""
    # Ensure vmbpy is NOT in sys.modules.
    uninstall_fake_vmbpy()
    # Also block re-import in case the real vmbpy is present.
    sys.modules["vmbpy"] = None  # type: ignore
    try:
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera()
        try:
            cam.connect()
        except ImportError as exc:
            assert "vmbpy" in str(exc).lower(), f"unexpected message: {exc}"
            # Stream thread should NOT have been started
            assert cam._stream_thread is None, "thread should not have started"
            return
        raise AssertionError("expected ImportError, none raised")
    finally:
        uninstall_fake_vmbpy()


def test_read_frame_before_any_frame_raises_specific_error() -> None:
    """FrameNotYetAvailableError distinguishes warmup from real failure."""
    try:
        fake_cam = install_fake_vmbpy()
        fake_cam.silent = True  # trigger runs but no frame arrives
        from drivers.rheed_camera import VmbCamera, FrameNotYetAvailableError
        cam = VmbCamera(trigger_hz=100.0)
        cam.connect()
        try:
            cam.read_frame()
        except FrameNotYetAvailableError as exc:
            assert "no frame" in str(exc).lower(), f"unexpected message: {exc}"
        finally:
            cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_read_frame_before_connect_generic_runtime_error() -> None:
    """read_frame() before connect() raises a generic RuntimeError, NOT FrameNotYet."""
    try:
        install_fake_vmbpy()
        from drivers.rheed_camera import VmbCamera, FrameNotYetAvailableError
        cam = VmbCamera()
        try:
            cam.read_frame()
        except FrameNotYetAvailableError:
            raise AssertionError(
                "not-connected should raise generic RuntimeError, not "
                "FrameNotYetAvailableError — those are different failure modes"
            )
        except RuntimeError as exc:
            assert "not connected" in str(exc).lower(), (
                f"unexpected message: {exc}"
            )
    finally:
        uninstall_fake_vmbpy()


def test_stream_error_propagates_to_read_frame() -> None:
    """A stream-thread crash surfaces in read_frame() with the underlying cause."""
    try:
        install_fake_vmbpy()
        # Force get_all_cameras to raise → the stream loop's outer try
        # catches it, sets _stream_error, and unblocks connect via
        # _ready_event.set() in the finally block.
        FakeVmbSystem._raise_on_get_all_cameras = RuntimeError(
            "SDK ate the socket"
        )
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera()
        try:
            cam.connect()
        except RuntimeError as exc:
            # connect() itself raises because _stream_error is set before
            # returning — verify the original message survives.
            assert "SDK ate the socket" in str(exc), (
                f"expected chained error message, got: {exc}"
            )
            assert not cam.connected
            return
        raise AssertionError("expected RuntimeError from connect, none raised")
    finally:
        uninstall_fake_vmbpy()


def test_disconnect_idempotent_when_never_connected() -> None:
    """disconnect() on a never-connected driver is a no-op, not a raise."""
    from drivers.rheed_camera import VmbCamera
    cam = VmbCamera()
    cam.disconnect()  # should not raise
    assert not cam.connected


def test_reconnect_after_disconnect() -> None:
    """A fresh connect() cycle uses new event primitives — no stale state."""
    try:
        fake_cam = install_fake_vmbpy()
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0)
        # First cycle
        cam.connect()
        assert cam.connected
        cam.disconnect()
        assert not cam.connected
        # Second cycle — the fake camera got __exit__'d, install a fresh one.
        fake_cam2 = install_fake_vmbpy()
        cam.connect()
        assert cam.connected
        cam.disconnect()
        # Both cycles start_streaming called
        assert fake_cam2.start_streaming_calls >= 1
    finally:
        uninstall_fake_vmbpy()


def test_bad_frame_recorded_but_stream_survives() -> None:
    """A handler exception on one frame is recorded but doesn't kill the loop."""
    try:
        fake_cam = install_fake_vmbpy()
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0)
        cam.connect()

        # Wait for stream to produce a good frame.
        for _ in range(100):
            with cam._frame_lock:
                if cam._latest_frame is not None:
                    break
            time.sleep(0.01)

        # Sanity: we DID get a frame
        assert cam._latest_frame is not None, "no baseline frame produced"

        # Now switch the fake camera to produce garbage that breaks the
        # handler's numpy path — the handler must survive and continue.
        class BadFrame:
            def as_numpy_ndarray(self):
                raise ValueError("simulated bad frame")

        # Manually invoke the handler with a bad frame — represents the
        # SDK giving us a corrupted frame.
        cam._frame_handler(fake_cam, None, BadFrame())

        # The handler should have recorded the error, not raised out.
        with cam._error_lock:
            err = cam._last_frame_error
        assert err is not None and "simulated bad frame" in err, (
            f"expected recorded frame error, got: {err!r}"
        )
        # And the stream should still be alive — connected is still True
        assert cam.connected, "stream died on a single bad frame"
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_trigger_backoff_after_consecutive_fails() -> None:
    """MAX_CONSECUTIVE_TRIGGER_FAILS raises → backoff, then loop continues."""
    try:
        fake_cam = install_fake_vmbpy()
        from drivers.rheed_camera import VmbCamera
        # Force fake camera to raise on the first few TriggerSoftware.run() calls
        fake_cam.raise_on_run = [
            RuntimeError(f"transient {i}") for i in range(
                VmbCamera.MAX_CONSECUTIVE_TRIGGER_FAILS + 1
            )
        ]
        cam = VmbCamera(trigger_hz=100.0)
        # Reduce backoff for the test — actual constant is 2s, too slow
        cam.TRIGGER_BACKOFF_S = 0.05
        cam.connect()

        # Give the loop enough time to hit the failures + backoff + retry
        # (5 fails × ~10ms period + 50ms backoff = ~100ms, plus post-backoff runs)
        time.sleep(0.4)

        # Verify last_frame_error was recorded from a failed trigger
        with cam._error_lock:
            err = cam._last_frame_error
        assert err is not None and "TriggerSoftware.run" in err, (
            f"expected trigger fail recorded, got: {err!r}"
        )
        # Should still be connected — loop backed off, didn't die
        assert cam.connected
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


# ---------------------------------------------------------------------------
# Access-mode fallback tests (Jul 27 2026 refactor — kSA-open coexistence)
#
# These cover the constraints from the fluffy-sleeping-babbage plan:
#   (a-c) auto-mode Full-first, Read-fallback at both denial boundaries
#   (d)   both-modes-denied combined error, no over-pointing at multicast
#   (e)   Read-mode feature .set() gating by the writable bit
#   (f)   Read-mode never calls TriggerSoftware.run()
#   (g)   access_mode public property matches negotiated mode
#   (h)   invalid access_mode string rejected at __init__
#   (i)   Read-mode FrameNotYetAvailableError includes Read context
# ---------------------------------------------------------------------------

def test_auto_full_succeeds_records_access_mode() -> None:
    """(a) In auto mode, if Full opens, access_mode property returns 'full'."""
    try:
        fake_cam = install_fake_vmbpy()  # no refuse flags → Full opens
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0)  # default access_mode="auto"
        assert cam.access_mode == "", (
            f"expected empty pre-connect, got {cam.access_mode!r}"
        )
        cam.connect()
        assert cam.connected
        assert cam.access_mode == "full", (
            f"expected access_mode='full', got {cam.access_mode!r}"
        )
        assert fake_cam.set_access_mode_calls == [FakeAccessMode.Full], (
            f"expected auto to try Full only, got "
            f"{fake_cam.set_access_mode_calls}"
        )
        cam.disconnect()
        assert cam.access_mode == "", (
            f"expected empty after disconnect, got {cam.access_mode!r}"
        )
    finally:
        uninstall_fake_vmbpy()


def test_auto_full_denied_at_enter_falls_back_to_read() -> None:
    """(b) auto → Full denied at __enter__, Read opens successfully."""
    try:
        fake_cam = install_fake_vmbpy()
        fake_cam.refuse_full_on_enter = True
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0)  # auto
        cam.connect()
        assert cam.connected
        assert cam.access_mode == "read", (
            f"expected fallback to 'read', got {cam.access_mode!r}"
        )
        assert fake_cam.set_access_mode_calls == [
            FakeAccessMode.Full, FakeAccessMode.Read,
        ], f"unexpected sequence: {fake_cam.set_access_mode_calls}"
        # Read attempt did enter the with-block
        assert fake_cam.entered
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_auto_full_denied_at_set_access_mode_falls_back_to_read() -> None:
    """(c) auto → Full denied at set_access_mode (before __enter__), Read opens."""
    try:
        fake_cam = install_fake_vmbpy()
        fake_cam.refuse_full_on_set_access_mode = True
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0)  # auto
        cam.connect()
        assert cam.connected
        assert cam.access_mode == "read", (
            f"expected fallback to 'read', got {cam.access_mode!r}"
        )
        # Driver attempted set_access_mode(Full) — refused, then tried Read
        assert fake_cam.set_access_mode_calls == [
            FakeAccessMode.Full, FakeAccessMode.Read,
        ], f"unexpected sequence: {fake_cam.set_access_mode_calls}"
        # last_set_access_mode should be Read (Full never took effect)
        assert fake_cam.last_set_access_mode == FakeAccessMode.Read
        assert fake_cam.entered
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_auto_full_and_read_both_denied_raises_combined_error() -> None:
    """(d) auto → Full AND Read both denied → combined error names both."""
    try:
        fake_cam = install_fake_vmbpy()
        fake_cam.refuse_full_on_enter = True
        fake_cam.refuse_read_on_enter = True
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0)  # auto
        try:
            cam.connect()
        except RuntimeError as exc:
            msg = str(exc)
            msg_lower = msg.lower()
            # Must name both failure modes
            assert "full denied" in msg_lower, (
                f"expected 'Full denied' in msg, got: {exc}"
            )
            assert "read also failed" in msg_lower, (
                f"expected 'Read also failed' in msg, got: {exc}"
            )
            # Multicast mentioned as ONE candidate, not the sole cause
            assert "multicast" in msg_lower, (
                f"expected 'multicast' among candidates, got: {exc}"
            )
            # Multiple candidate causes surfaced (not just multicast)
            assert "ksa" in msg_lower or "permitted access modes" in msg_lower, (
                f"expected multiple candidates, got: {exc}"
            )
            assert not cam.connected
            return
        raise AssertionError("expected RuntimeError, none raised")
    finally:
        uninstall_fake_vmbpy()


def test_read_mode_skips_writes_when_feature_not_writable() -> None:
    """(e) In Read mode, every feature .set() is gated by the writable bit."""
    try:
        # Simulate Read-mode SDK behavior: features report writable=False
        fake_cam = install_fake_vmbpy(cameras=[FakeCamera(
            trigger_source_writable=False,
            trigger_selector_writable=False,
            trigger_mode_writable=False,
            acquisition_mode_writable=False,
        )])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0, access_mode="read")
        cam.connect()
        assert cam.connected
        assert cam.access_mode == "read"
        # No writes should have fired — writable bit was False on all four
        assert fake_cam.TriggerSource.set_call_count == 0, (
            f"TriggerSource.set called {fake_cam.TriggerSource.set_call_count} "
            "times despite writable=False"
        )
        assert fake_cam.TriggerSelector.set_call_count == 0
        assert fake_cam.TriggerMode.set_call_count == 0
        assert fake_cam.AcquisitionMode.set_call_count == 0
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_read_mode_never_calls_trigger_software_run() -> None:
    """(f) In Read mode, TriggerSoftware.run() is never called."""
    try:
        fake_cam = install_fake_vmbpy()
        from drivers.rheed_camera import VmbCamera
        # Fast trigger_hz — if the loop mistakenly called run(), count > 0
        cam = VmbCamera(trigger_hz=1000.0, access_mode="read")
        cam.connect()
        assert cam.connected
        assert cam.access_mode == "read"
        # Give the idle loop time to iterate many periods
        time.sleep(0.1)
        assert fake_cam.trigger_run_count == 0, (
            f"Read mode must never trigger, got trigger_run_count="
            f"{fake_cam.trigger_run_count}"
        )
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_access_mode_property_matches_negotiated_mode() -> None:
    """(g) access_mode property returns 'full'/'read' matching negotiation."""
    from drivers.rheed_camera import VmbCamera
    # Explicit Full
    try:
        install_fake_vmbpy()
        cam_full = VmbCamera(trigger_hz=100.0, access_mode="full")
        assert cam_full.access_mode == ""  # not connected yet
        cam_full.connect()
        assert cam_full.access_mode == "full"
        cam_full.disconnect()
    finally:
        uninstall_fake_vmbpy()
    # Explicit Read
    try:
        install_fake_vmbpy()
        cam_read = VmbCamera(trigger_hz=100.0, access_mode="read")
        assert cam_read.access_mode == ""
        cam_read.connect()
        assert cam_read.access_mode == "read"
        cam_read.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_invalid_access_mode_raises_value_error_at_init() -> None:
    """(h) VmbCamera(access_mode='banana') → ValueError at __init__."""
    from drivers.rheed_camera import VmbCamera
    try:
        VmbCamera(access_mode="banana")
    except ValueError as exc:
        msg = str(exc)
        assert "access_mode" in msg, f"unexpected message: {exc}"
        assert "banana" in msg, f"expected 'banana' in message, got: {exc}"
        return
    raise AssertionError("expected ValueError, none raised")


def test_manual_exposure_applied_and_read_back_in_full_mode() -> None:
    """A validated manual exposure is applied before streaming starts."""
    try:
        fake_cam = install_fake_vmbpy()
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0,
            access_mode="full",
            exposure_us=250_000.0,
        )
        cam.connect()
        assert fake_cam.ExposureTimeAbs.value == 250_000.0
        assert fake_cam.ExposureTimeAbs.set_call_count == 1
        assert cam.exposure_us == 250_000.0
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_manual_exposure_refuses_read_mode() -> None:
    """The GUI must not claim an exposure was applied without Full access."""
    try:
        fake_cam = install_fake_vmbpy()
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0,
            access_mode="read",
            exposure_us=250_000.0,
        )
        try:
            cam.connect()
        except RuntimeError as exc:
            assert "requires Full camera access" in str(exc)
            assert fake_cam.ExposureTimeAbs.set_call_count == 0
            return
        raise AssertionError("manual exposure unexpectedly succeeded in Read mode")
    finally:
        uninstall_fake_vmbpy()


def test_manual_exposure_requires_auto_off() -> None:
    """A running auto-exposure loop would race and overwrite the request."""
    try:
        fake_cam = install_fake_vmbpy()
        fake_cam.ExposureAuto.value = "Continuous"
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0,
            access_mode="full",
            exposure_us=250_000.0,
        )
        try:
            cam.connect()
        except RuntimeError as exc:
            assert "ExposureAuto=Off" in str(exc)
            assert fake_cam.ExposureTimeAbs.set_call_count == 0
            return
        raise AssertionError("manual exposure unexpectedly raced ExposureAuto")
    finally:
        uninstall_fake_vmbpy()


def test_manual_exposure_rejects_unsafe_trigger_pair_at_init() -> None:
    """Exposure must retain timing headroom within the trigger period."""
    from drivers.rheed_camera import VmbCamera
    try:
        VmbCamera(trigger_hz=1.0, exposure_us=950_000.0)
    except ValueError as exc:
        assert "10% acquisition headroom" in str(exc)
        return
    raise AssertionError("unsafe exposure/trigger pair was accepted")



def test_read_mode_frame_not_yet_error_includes_read_context() -> None:
    """(i) In Read mode, FrameNotYetAvailableError includes Read-mode context."""
    try:
        install_fake_vmbpy()
        from drivers.rheed_camera import VmbCamera, FrameNotYetAvailableError
        cam = VmbCamera(trigger_hz=100.0, access_mode="read")
        cam.connect()
        assert cam.access_mode == "read"
        # In Read mode the driver never triggers, so no frame will ever
        # arrive on this synthetic setup. Read frame → FrameNotYet.
        try:
            cam.read_frame()
        except FrameNotYetAvailableError as exc:
            msg = str(exc)
            assert "AccessMode.Read" in msg, (
                f"expected 'AccessMode.Read' in message, got: {msg}"
            )
            assert "multicast" in msg.lower(), (
                f"expected multicast hint in Read message, got: {msg}"
            )
            cam.disconnect()
            return
        cam.disconnect()
        raise AssertionError("expected FrameNotYetAvailableError, none raised")
    finally:
        uninstall_fake_vmbpy()


def test_full_mode_get_access_mode_raise_propagates() -> None:
    """(j) In Full mode, feature.get_access_mode() raising propagates.

    Silently skipping a required trigger config would leave the camera in
    an unusable state — constraint 3 says Full-mode probe failures must
    propagate rather than be swallowed.
    """
    try:
        fake_cam = install_fake_vmbpy()
        # Inject a probe failure on the first feature the driver touches
        fake_cam.TriggerSource._get_access_mode_raises = RuntimeError(
            "SDK probe broke"
        )
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0, access_mode="full")
        try:
            cam.connect()
        except RuntimeError as exc:
            assert "SDK probe broke" in str(exc), (
                f"expected propagation of probe error, got: {exc}"
            )
            assert not cam.connected
            # Never made it to start_streaming — TriggerSource is the first
            # feature the driver probes, so nothing beyond that should have
            # happened. In particular, the .set() should not have fired.
            assert fake_cam.TriggerSource.set_call_count == 0
            return
        raise AssertionError(
            "expected RuntimeError to propagate from Full-mode probe failure"
        )
    finally:
        uninstall_fake_vmbpy()


def test_read_mode_performs_zero_configuration_writes() -> None:
    """Read mode is the kSA-coexistence path: it writes NOTHING.

    REPLACES a test that asserted Read mode still wrote TriggerSelector when
    its writable bit came back True, skipping only the feature whose probe
    raised. That encoded a contradiction: the mode is documented as passive,
    yet a camera reporting these features writable in Read got its trigger
    pipeline reconfigured out from under kSA — the exact interference the
    mode exists to prevent.

    Passivity is now unconditional and does not depend on what the camera
    says about writability.
    """
    try:
        fake_cam = install_fake_vmbpy()
        fake_cam.TriggerSource._get_access_mode_raises = RuntimeError(
            "SDK probe broke"
        )
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0, access_mode="read")
        cam.connect()
        assert cam.connected
        assert cam.access_mode == "read"
        for name in ("TriggerSource", "TriggerSelector",
                     "TriggerMode", "AcquisitionMode"):
            assert getattr(fake_cam, name).set_call_count == 0, (
                f"Read mode wrote {name} — this breaks the passive guarantee "
                "and can disturb kSA"
            )
        assert fake_cam.ExposureTimeAbs.set_call_count == 0, (
            "Read mode wrote the exposure"
        )
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()



def test_frame_luminance_2d_frame() -> None:
    """_frame_luminance returns mean pixel value for a 2-D (H, W) frame."""
    from gui.workers import _frame_luminance
    frame = np.array([[0, 128, 255]], dtype=np.uint8)
    result = _frame_luminance(frame)
    assert isinstance(result, float)
    assert abs(result - (0 + 128 + 255) / 3) < 0.5


def test_frame_luminance_rgb_bt601_weights() -> None:
    """_frame_luminance uses BT.601 weights, not green-channel-only mean.

    Pure green pixel: G-only mean = 200; BT.601 = 0.587 * 200 = 117.4.
    """
    from gui.workers import _frame_luminance
    frame = np.array([[[0, 200, 0]]], dtype=np.uint8)
    lum = _frame_luminance(frame)
    assert abs(lum - 0.587 * 200) < 0.5, f"Expected ~{0.587*200:.1f}, got {lum:.2f}"
    assert lum != 200.0, "Should differ from green-channel-only mean"


def test_frame_luminance_rgb_includes_r_and_b() -> None:
    """_frame_luminance accounts for all three channels (BGW upper-ramp case)."""
    from gui.workers import _frame_luminance
    white = np.array([[[255, 255, 255]]], dtype=np.uint8)
    assert abs(_frame_luminance(white) - 255.0) < 0.5
    # BGW LUT[200]: R=145, G=255, B=145
    frame = np.array([[[145, 255, 145]]], dtype=np.uint8)
    expected = 0.299 * 145 + 0.587 * 255 + 0.114 * 145
    assert abs(_frame_luminance(frame) - expected) < 0.5


# ---------------------------------------------------------------------------
# Exposure hardening — raising feature lookup, and the restore path
# ---------------------------------------------------------------------------

class RaisingLookupCamera(FakeCamera):
    """FakeCamera whose ``ExposureTimeAbs`` lookup raises, not returns None.

    vmbpy resolves features through ``__getattr__`` against the live GenICam
    node map, so an unavailable node can surface as an SDK error rather than
    the AttributeError that ``getattr(..., default)`` absorbs. This camera
    reproduces that shape: the legacy AVT spelling raises, and only the SFNC
    spelling is present.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        del self.ExposureTimeAbs
        self.ExposureTime = FakeSettable(
            initial_value=300_000.0,
            value_range=(100.0, 900_000.0),
            increment=1.0,
        )

    def __getattr__(self, name):
        # Only fires for attributes not found normally — i.e. the deleted
        # ExposureTimeAbs, which is exactly the case under test.
        if name == "ExposureTimeAbs":
            raise FakeVmbCameraError("feature unavailable on this node map")
        raise AttributeError(name)


class ClampingFeature(FakeSettable):
    """A device that clamps the first write, then honours the restore.

    Models silent clamping: the requested value is accepted by ``set`` but
    the node lands somewhere else, so the readback disagrees. The restore
    write is honoured, which is what lets the test prove the original value
    was actually put back rather than merely that ``set`` was called twice.
    """

    CLAMPED_TO = 111_111.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_values: list[float] = []

    def set(self, value) -> None:  # noqa: A003
        self.set_call_count += 1
        self.set_values.append(value)
        self.value = self.CLAMPED_TO if self.set_call_count == 1 else value


class LookupRaisingCamera(FakeCamera):
    """A camera whose FEATURE LOOKUP raises for named nodes.

    Models what vmbpy actually does: features resolve through ``__getattr__``
    against the live GenICam node map, so an SDK error can surface while
    *locating* the node — before any accessor is called. That is a different
    failure from RaisingAutoFeature, where the node resolves fine and only
    ``get()`` fails.

    The distinction is the whole point: a lookup error proves nothing about
    the camera, so treating it as "this feature is absent" silently skips a
    safety precondition the caller believes it checked.
    """

    def __init__(self, *args, raising_names=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._raising_names = set(raising_names)
        # Drop the real attributes so normal lookup misses and __getattr__ runs.
        for name in self._raising_names:
            self.__dict__.pop(name, None)

    def __getattr__(self, name):
        # Reached only when normal attribute lookup fails. Read the raising set
        # out of __dict__ directly so this cannot recurse.
        if name in self.__dict__.get("_raising_names", ()):
            raise FakeVmbCameraError(f"SDK failure resolving {name}")
        raise AttributeError(name)


class MissingRangeFeature(FakeSettable):
    """A feature whose ``get_range`` is absent entirely."""

    get_range = None


class MalformedRangeFeature(FakeSettable):
    """A feature whose ``get_range`` returns something unusable."""

    def get_range(self):
        return (100.0,)  # one element, not [min, max]


class RaisingRangeFeature(FakeSettable):
    """A feature that is present but whose ``get_range`` probe raises."""

    def get_range(self):
        raise FakeVmbCameraError("range unavailable")


class RaisingRangeLookupFeature(FakeSettable):
    """A feature where *accessing* ``get_range`` raises, before any call.

    Distinct from RaisingRangeFeature, where the lookup succeeds and the
    call raises. Both are failed reads and must fail closed identically;
    routing the lookup through the tolerant camera-level helper would have
    misclassified this one as an absent accessor.
    """

    @property
    def get_range(self):
        raise FakeVmbCameraError("get_range unavailable on this node")


class RaisingAutoFeature(FakeSettable):
    """An ExposureAuto that is present but cannot be read."""

    def get(self):
        raise FakeVmbCameraError("ExposureAuto unreadable")


class RaisingLimitFeature(FakeSettable):
    """An AcquisitionFrameRateLimit that is present but cannot be read."""

    def get(self):
        raise FakeVmbCameraError("frame-rate limit unreadable")


def test_exposure_lookup_survives_a_raising_feature_and_falls_back() -> None:
    """A raising ExposureTimeAbs must not hide a valid ExposureTime."""
    try:
        fake_cam = install_fake_vmbpy([RaisingLookupCamera()])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        cam.connect()
        assert fake_cam.ExposureTime.value == 250_000.0, (
            "fell back to ExposureTime but did not apply the exposure"
        )
        assert cam.exposure_us == 250_000.0
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_out_of_range_exposure_fails_without_writing() -> None:
    """A request outside the device range is refused before any set()."""
    try:
        fake_cam = install_fake_vmbpy()
        from drivers.rheed_camera import VmbCamera
        # Range is (100.0, 900_000.0); 50 us clears the constructor's
        # 90%-of-period ceiling at 1 Hz but is below the device minimum.
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=50.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, "out-of-range exposure was not refused"
        assert "outside the camera range" in str(raised), str(raised)
        assert fake_cam.ExposureTimeAbs.set_call_count == 0, (
            "driver wrote to the camera despite an out-of-range request"
        )
        assert fake_cam.ExposureTimeAbs.value == 300_000.0
    finally:
        uninstall_fake_vmbpy()


def test_bad_readback_restores_the_original_exposure() -> None:
    """A disagreeing readback puts the original value back on the camera."""
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureTimeAbs = ClampingFeature(
            initial_value=300_000.0,
            value_range=(100.0, 900_000.0),
            increment=1.0,
        )
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, "a bad readback was accepted as confirmed"
        assert "readback" in str(raised), str(raised)
        # The restore must carry the ORIGINAL value, not merely happen.
        assert cam_stub.ExposureTimeAbs.set_values == [250_000.0, 300_000.0], (
            f"expected attempt then restore-to-original, got "
            f"{cam_stub.ExposureTimeAbs.set_values}"
        )
        assert cam_stub.ExposureTimeAbs.value == 300_000.0, (
            "camera did not end up back at its original exposure"
        )
        assert cam.exposure_us == 300_000.0
    finally:
        uninstall_fake_vmbpy()


# --- Present-but-unreadable safety reads must refuse, not skip -------------
#
# Absence and unreadability are different. A camera with no ExposureAuto has
# no auto loop to race; a camera whose ExposureAuto cannot be read leaves
# that precondition unproven. Skipping the check in the second case would
# silently drop validation the driver's contract promises.

def test_unreadable_exposure_auto_refuses_the_write() -> None:
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureAuto = RaisingAutoFeature(initial_value="Off")
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, "wrote exposure without proving auto is Off"
        assert "ExposureAuto" in str(raised), str(raised)
        assert cam_stub.ExposureTimeAbs.set_call_count == 0
    finally:
        uninstall_fake_vmbpy()


def test_exposure_auto_LOOKUP_failure_refuses_the_write() -> None:
    """A raising ExposureAuto *lookup* must refuse, not read as absent.

    This is the fail-open the sentinels exist to stop: the camera-level
    helper maps a raising lookup to None, which is indistinguishable from
    "this camera has no auto-exposure" — and absence is tolerated. The write
    would then proceed against a possibly-active auto loop.
    """
    try:
        cam_stub = LookupRaisingCamera(raising_names=["ExposureAuto"])
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, (
            "a raising ExposureAuto lookup was treated as absent and the "
            "write proceeded without proving the auto loop is off"
        )
        assert "ExposureAuto" in str(raised), str(raised)
        assert cam_stub.ExposureTimeAbs.set_call_count == 0
    finally:
        uninstall_fake_vmbpy()


def test_frame_rate_limit_LOOKUP_failure_refuses_the_write() -> None:
    """A raising AcquisitionFrameRateLimit lookup must refuse, not read as absent."""
    try:
        cam_stub = LookupRaisingCamera(
            raising_names=["AcquisitionFrameRateLimit"],
        )
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, (
            "a raising frame-rate-limit lookup was treated as absent, so the "
            "trigger rate was never confirmed achievable"
        )
        assert "AcquisitionFrameRateLimit" in str(raised), str(raised)
        # The write is attempted before this check, so it must be restored.
        assert cam_stub.ExposureTimeAbs.value == 300_000.0, (
            "original exposure not restored after a failed verification"
        )
    finally:
        uninstall_fake_vmbpy()


def test_missing_range_refuses_the_write() -> None:
    """An absent get_range must refuse — not silently skip bounds checking."""
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureTimeAbs = MissingRangeFeature(initial_value=300_000.0)
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, "wrote exposure with no range validation"
        assert "range" in str(raised).lower(), str(raised)
        assert cam_stub.ExposureTimeAbs.set_call_count == 0
    finally:
        uninstall_fake_vmbpy()


def test_malformed_range_refuses_the_write() -> None:
    """A range that is not a usable [min, max] pair must refuse."""
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureTimeAbs = MalformedRangeFeature(
            initial_value=300_000.0,
        )
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, "wrote exposure on a malformed range"
        assert "range" in str(raised).lower(), str(raised)
        assert cam_stub.ExposureTimeAbs.set_call_count == 0
    finally:
        uninstall_fake_vmbpy()


def test_unreadable_range_refuses_the_write() -> None:
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureTimeAbs = RaisingRangeFeature(
            initial_value=300_000.0, increment=1.0,
        )
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, "wrote exposure without validating range"
        assert "range" in str(raised), str(raised)
        assert cam_stub.ExposureTimeAbs.set_call_count == 0
    finally:
        uninstall_fake_vmbpy()


def test_unreadable_range_lookup_refuses_the_write() -> None:
    """A raising accessor *lookup* fails closed like a raising call."""
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureTimeAbs = RaisingRangeLookupFeature(
            initial_value=300_000.0, increment=1.0,
        )
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, (
            "a raising get_range lookup was treated as an absent accessor "
            "and range validation was skipped"
        )
        assert "range" in str(raised), str(raised)
        assert cam_stub.ExposureTimeAbs.set_call_count == 0
    finally:
        uninstall_fake_vmbpy()


def test_unreadable_frame_rate_limit_refuses_and_restores() -> None:
    """Verification attempted and failed is not verification skipped."""
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureTimeAbs = ClampingFeature(
            initial_value=300_000.0,
            value_range=(100.0, 900_000.0),
            increment=1.0,
        )
        # Honour the first write so the readback passes and the limit check
        # is actually reached.
        cam_stub.ExposureTimeAbs.CLAMPED_TO = 250_000.0
        cam_stub.AcquisitionFrameRateLimit = RaisingLimitFeature(
            initial_value=3.3323,
        )
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, "accepted an unverifiable frame-rate limit"
        assert "AcquisitionFrameRateLimit" in str(raised), str(raised)
        assert cam_stub.ExposureTimeAbs.set_values == [250_000.0, 300_000.0], (
            f"exposure not restored: {cam_stub.ExposureTimeAbs.set_values}"
        )
    finally:
        uninstall_fake_vmbpy()


def _refuses_write(mutate, *, expect_restored=None, expect_text=None,
                   expect_sets=None):
    """Run a full connect with a mutated camera; assert the write was refused.

    ``expect_restored`` asserts the original exposure was put back, which only
    applies to validation that happens AFTER the set.
    """
    try:
        cam_stub = FakeCamera()
        mutate(cam_stub)
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        finally:
            try:
                cam.disconnect()
            except Exception:  # noqa: BLE001
                pass
        assert raised is not None, (
            "an unprovable safety precondition was treated as satisfied and "
            f"the exposure write proceeded (final "
            f"{cam_stub.ExposureTimeAbs.value})"
        )
        if expect_text is not None:
            # Search the whole __cause__ chain, not just the surfaced error.
            # When a post-write check fails AND the restore cannot be
            # verified, the restoration error is what surfaces and the
            # original refusal is chained beneath it — both are real, and the
            # test should not depend on which one ends up outermost.
            chain, node = [], raised
            while node is not None:
                chain.append(str(node))
                node = node.__cause__
            joined = " || ".join(chain).lower()
            assert expect_text.lower() in joined, joined
        if expect_restored is not None:
            assert cam_stub.ExposureTimeAbs.value == expect_restored, (
                f"original exposure not restored after a post-write refusal: "
                f"{cam_stub.ExposureTimeAbs.value} != {expect_restored}"
            )
        if expect_sets is not None:
            # An exception alone does not prove the camera was untouched.
            # Pre-write refusals must reach set() zero times; asserting only
            # that "something raised" would pass even if the write landed and
            # a later check rejected it.
            assert cam_stub.ExposureTimeAbs.set_call_count == expect_sets, (
                f"expected {expect_sets} set() call(s), got "
                f"{cam_stub.ExposureTimeAbs.set_call_count}"
            )
        return raised
    finally:
        uninstall_fake_vmbpy()


def test_noncallable_exposure_auto_get_refuses_the_write() -> None:
    """``ExposureAuto.get = None`` is a feature that cannot answer, not absence.

    The older helper returned its ``default`` for a non-callable accessor,
    which is indistinguishable from "this camera has no auto-exposure" — and
    absence is tolerated. The write then proceeded with the auto loop unproven.
    """
    _refuses_write(
        lambda c: setattr(c.ExposureAuto, "get", None),
        expect_text="ExposureAuto",
    )


def test_exposure_auto_reporting_no_state_refuses_the_write() -> None:
    """A present ExposureAuto that answers None proves nothing."""
    _refuses_write(
        lambda c: setattr(c.ExposureAuto, "value", None),
        expect_text="ExposureAuto",
    )


def test_noncallable_frame_rate_limit_get_refuses_and_restores() -> None:
    """Same hole on the frame-rate limit, and it must restore afterwards."""
    _refuses_write(
        lambda c: setattr(c.AcquisitionFrameRateLimit, "get", None),
        expect_restored=300_000.0,
        expect_text="AcquisitionFrameRateLimit",
    )


def test_nonfinite_frame_rate_limit_refuses_and_restores() -> None:
    """NaN passes isinstance(float) but proves nothing about the rate."""
    _refuses_write(
        lambda c: setattr(c.AcquisitionFrameRateLimit, "value", float("nan")),
        expect_restored=300_000.0,
        expect_text="unusable",
    )


def test_nonpositive_frame_rate_limit_refuses_and_restores() -> None:
    """A zero/negative limit cannot confirm any trigger rate."""
    _refuses_write(
        lambda c: setattr(c.AcquisitionFrameRateLimit, "value", 0),
        expect_restored=300_000.0,
        expect_text="unusable",
    )


def test_boolean_frame_rate_limit_refuses_and_restores() -> None:
    """bool is an int subclass: True would otherwise compare as 1.0 fps."""
    _refuses_write(
        lambda c: setattr(c.AcquisitionFrameRateLimit, "value", True),
        expect_restored=300_000.0,
        expect_text="unusable",
    )


def test_raising_increment_accessor_refuses_the_write() -> None:
    """An unreadable increment must not be downgraded to "no increment".

    Without the grid, the value written may not be representable on the
    device — so the readback check becomes the only guard, and the caller
    believes quantisation was handled.
    """
    def mutate(cam_stub):
        def boom():
            raise FakeVmbCameraError("increment unavailable")
        cam_stub.ExposureTimeAbs.get_increment = boom

    _refuses_write(mutate, expect_text="increment")


def test_noncallable_increment_accessor_refuses_the_write() -> None:
    _refuses_write(
        lambda c: setattr(c.ExposureTimeAbs, "get_increment", None),
        expect_text="increment",
    )


class NanReadbackFeature(FakeSettable):
    """Reports the original once, then NaN — a device that stops answering."""

    def get(self):
        self._reads = getattr(self, "_reads", 0) + 1
        return self.value if self._reads == 1 else float("nan")


class OffGridFeature(FakeSettable):
    """Lands a whole increment away from whatever it is told to set."""

    def set(self, value) -> None:  # noqa: A003
        self.value = value + (self._increment or 0.0)
        self.set_call_count += 1


class UnrestorableFeature(FakeSettable):
    """Accepts the first write, then silently refuses to move again.

    Models a device that clamps: the configuration write lands somewhere
    unexpected AND the restore cannot put it back.
    """

    def set(self, value) -> None:  # noqa: A003
        self.set_call_count += 1
        if self.set_call_count == 1:
            self.value = value
        # later sets (the restore) are ignored


class RaisingRestoreReadFeature(FakeSettable):
    """Readback raises only after the restore write."""

    def set(self, value) -> None:  # noqa: A003
        self.value = value
        self.set_call_count += 1

    def get(self):
        if self.set_call_count >= 2:
            raise FakeVmbCameraError("device stopped answering")
        return self.value


def test_nan_original_exposure_refuses_the_write() -> None:
    """The original is the restore target; an unusable one makes the write one-way."""
    _refuses_write(
        lambda c: setattr(c.ExposureTimeAbs, "value", float("nan")),
        expect_text="original",
    )


def test_nan_readback_refuses_the_write() -> None:
    """float(nan) succeeds and every comparison against it is False.

    ``abs(nan - requested) > tolerance`` is False, so a NaN readback was
    silently accepted and stored as the confirmed exposure.
    """
    _refuses_write(
        lambda c: setattr(c, "ExposureTimeAbs", NanReadbackFeature(
            initial_value=300_000.0, value_range=(100.0, 900_000.0),
            increment=1.0,
        )),
        expect_text="readback",
    )


def test_boolean_range_endpoint_refuses_the_write() -> None:
    _refuses_write(
        lambda c: setattr(c.ExposureTimeAbs, "_value_range", (True, 900_000.0)),
        expect_text="range",
    )


def test_nonfinite_range_endpoint_refuses_the_write() -> None:
    _refuses_write(
        lambda c: setattr(
            c.ExposureTimeAbs, "_value_range", (float("nan"), 900_000.0),
        ),
        expect_text="range",
    )


def test_quantisation_outside_the_range_refuses_the_write() -> None:
    """Snapping to the grid can step past `high`.

    Range (100, 160) with increment 100: a request of 160 is inside the range,
    snaps to 200, and was written anyway — outside the device's own declared
    range, then "confirmed" by a tolerance wide enough to accept it.
    """
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureTimeAbs = FakeSettable(
            initial_value=100.0, value_range=(100.0, 160.0), increment=100.0,
        )
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=1.0, access_mode="full", exposure_us=160.0)
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        finally:
            try:
                cam.disconnect()
            except Exception:  # noqa: BLE001
                pass
        assert raised is not None, (
            "a request that quantised outside the declared range was written"
        )
        assert "range" in str(raised).lower(), str(raised)
        assert cam_stub.ExposureTimeAbs.set_call_count == 0
    finally:
        uninstall_fake_vmbpy()


def test_readback_one_increment_away_refuses_the_write() -> None:
    """The request is already snapped to the grid, so a whole increment of
    slack accepted a device that quantised somewhere else entirely."""
    _refuses_write(
        lambda c: setattr(c, "ExposureTimeAbs", OffGridFeature(
            initial_value=300_000.0, value_range=(100.0, 900_000.0),
            increment=1000.0,
        )),
        expect_text="readback",
    )


class AppliesThenRaisesFeature(FakeSettable):
    """Applies the value, then raises — a lost acknowledgement.

    The camera really did change. Marking the write as done only after set()
    RETURNS meant this path skipped restoration entirely and left the camera
    altered with no record of it.
    """

    def set(self, value) -> None:  # noqa: A003
        self.value = value
        self.set_call_count += 1
        if self.set_call_count == 1:
            raise FakeVmbCameraError("acknowledgement lost after apply")


class AppliesThenRaisesUnrestorableFeature(FakeSettable):
    """Applies and raises, and then will not move again."""

    def set(self, value) -> None:  # noqa: A003
        self.set_call_count += 1
        if self.set_call_count == 1:
            self.value = value
            raise FakeVmbCameraError("acknowledgement lost after apply")
        # the restore write is ignored


def test_constructor_rejects_nonfinite_and_boolean_arguments() -> None:
    """`nan <= 0` is False, inf gives a zero-length trigger period, and bool
    is an int subclass — all three slipped through the old positivity test."""
    from drivers.rheed_camera import VmbCamera
    bad = [
        {"trigger_hz": float("nan")},
        {"trigger_hz": float("inf")},
        {"trigger_hz": float("-inf")},
        {"trigger_hz": True},
        {"trigger_hz": 0},
        {"trigger_hz": -1.0},
        {"exposure_us": float("nan")},
        {"exposure_us": float("inf")},
        {"exposure_us": float("-inf")},
        {"exposure_us": True},
    ]
    for kwargs in bad:
        raised = None
        try:
            VmbCamera(**kwargs)
        except ValueError as exc:
            raised = exc
        assert raised is not None, f"constructor accepted {kwargs}"


def test_malformed_access_mode_refuses_the_write() -> None:
    """Every non-empty value is truthy, so bool(access[1]) authorised them all."""
    for bogus in ("False", 1, object(), None):
        _refuses_write(
            lambda c, b=bogus: setattr(c.ExposureTimeAbs, "_writable", b),
            expect_text="access mode",
            expect_sets=0,
        )


def test_unreadable_but_writable_feature_refuses_the_write() -> None:
    """A readback check is meaningless if the feature cannot be read."""
    _refuses_write(
        lambda c: setattr(c.ExposureTimeAbs, "_readable", False),
        expect_text="readable",
    )


def test_write_that_applies_then_raises_is_restored() -> None:
    """A lost acknowledgement still changed the camera."""
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureTimeAbs = AppliesThenRaisesFeature(
            initial_value=300_000.0, value_range=(100.0, 900_000.0),
        )
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        finally:
            try:
                cam.disconnect()
            except Exception:  # noqa: BLE001
                pass
        assert raised is not None, "a raising write was reported as success"
        assert cam_stub.ExposureTimeAbs.value == 300_000.0, (
            "the camera was left at the applied value: set() raised after "
            "applying, and restoration never ran"
        )
    finally:
        uninstall_fake_vmbpy()


def test_write_that_applies_then_raises_unrestorably_warns_power_cycle() -> None:
    raised = _refuses_write(
        lambda c: setattr(c, "ExposureTimeAbs",
                          AppliesThenRaisesUnrestorableFeature(
                              initial_value=300_000.0,
                              value_range=(100.0, 900_000.0))),
    )
    assert "power-cycle" in str(raised).lower(), str(raised)
    assert raised.__cause__ is not None


def test_original_outside_the_reported_range_refuses_before_writing() -> None:
    """An unrestorable original must stop the write before it happens."""
    raised = _refuses_write(
        lambda c: setattr(c.ExposureTimeAbs, "value", 950_000.0),
        expect_text="restored",
        expect_sets=0,
    )
    assert "950000" in str(raised), str(raised)


def test_dotted_lookalike_exposure_auto_values_are_rejected() -> None:
    """Discarding an arbitrary prefix accepted strings about nothing at all."""
    for bogus in ("Bogus.Off", "ExposureAuto.Not.Off", "Something.Weird.Off"):
        _refuses_write(
            lambda c, v=bogus: setattr(c.ExposureAuto, "value", v),
            expect_text="ExposureAuto",
            expect_sets=0,
        )



def test_keep_current_does_not_publish_an_unusable_exposure() -> None:
    """No write, and no invalid number entering CameraState/session metadata."""
    for bogus in (float("nan"), float("inf"), float("-inf"), True, 0, -1.0):
        try:
            cam_stub = FakeCamera()
            cam_stub.ExposureTimeAbs = FakeSettable(
                initial_value=bogus, value_range=(100.0, 900_000.0),
            )
            install_fake_vmbpy([cam_stub])
            from drivers.rheed_camera import VmbCamera
            cam = VmbCamera(trigger_hz=1.0, access_mode="full")  # keep current
            cam.connect()
            assert cam_stub.ExposureTimeAbs.set_call_count == 0, (
                "keep-current performed a camera write"
            )
            assert cam.exposure_us is None, (
                f"published {cam.exposure_us!r} as a confirmed exposure from "
                f"an unusable reading {bogus!r}"
            )
            cam.disconnect()
        finally:
            uninstall_fake_vmbpy()


def _fail_after_write(cam_stub) -> None:
    """Make a POST-write check fail so the restore path is actually taken.

    The restore only runs when configuration failed after ``set``. A device
    that misbehaves solely on restore never reaches it, so these tests pair
    the misbehaving exposure feature with a frame-rate limit below the
    trigger rate — a genuine post-write refusal.
    """
    cam_stub.AcquisitionFrameRateLimit = FakeSettable(initial_value=0.5)


def test_unverifiable_restore_raises_a_power_cycle_error() -> None:
    """A restore that silently does not take must not be reported as success.

    The old path set the original, read once, and stored whatever came back
    without comparing it — so a clamping device left the camera altered while
    the caller saw only the original configuration error.
    """
    def mutate(cam_stub):
        cam_stub.ExposureTimeAbs = UnrestorableFeature(
            initial_value=300_000.0, value_range=(100.0, 900_000.0),
        )
        _fail_after_write(cam_stub)

    raised = _refuses_write(mutate)
    assert "power-cycle" in str(raised).lower(), str(raised)
    assert "restore" in str(raised).lower(), str(raised)
    assert raised.__cause__ is not None, (
        "the original configuration failure was not chained onto the "
        "restoration error"
    )
    assert "fps limit" in str(raised.__cause__).lower(), str(raised.__cause__)


def test_raising_restore_readback_raises_a_power_cycle_error() -> None:
    def mutate(cam_stub):
        cam_stub.ExposureTimeAbs = RaisingRestoreReadFeature(
            initial_value=300_000.0, value_range=(100.0, 900_000.0),
        )
        _fail_after_write(cam_stub)

    raised = _refuses_write(mutate)
    assert "power-cycle" in str(raised).lower(), str(raised)
    assert raised.__cause__ is not None


def test_exposure_auto_suffix_lookalikes_are_rejected() -> None:
    """"NotOff" ends with "off" and means the exact opposite."""
    for bogus in ("NotOff", "TurnedOff", "Backoff", "off-ish"):
        raised = _refuses_write(
            lambda c, v=bogus: setattr(c.ExposureAuto, "value", v),
            expect_text="ExposureAuto",
            expect_sets=0,
        )
        assert bogus in str(raised), str(raised)



class BlockingExposureFeature(FakeSettable):
    """Blocks inside an accessor until released, then continues.

    Models an SDK call that outlasts CONNECT_TIMEOUT_S and DISCONNECT_TIMEOUT_S.
    The setup thread is still inside it when connect() gives up and
    disconnect() returns, and it wakes afterwards — which is how an abandoned
    cycle used to reconfigure a camera the GUI had already released.
    """

    def __init__(self, *args, block_on="get", **kwargs):
        super().__init__(*args, **kwargs)
        self.release = threading.Event()
        self.entered = threading.Event()
        self._block_on = block_on

    def _maybe_block(self, which):
        if which == self._block_on:
            self.entered.set()
            self.release.wait(timeout=10.0)

    def get(self):
        self._maybe_block("get")
        return super().get()

    def set(self, value):  # noqa: A003
        self._maybe_block("set")
        return super().set(value)


def _abandoned_cycle(block_on):
    """connect() times out, disconnect() runs, THEN the blocked call returns."""
    cam_stub = FakeCamera()
    feature = BlockingExposureFeature(
        initial_value=300_000.0, value_range=(100.0, 900_000.0),
        block_on=block_on,
    )
    cam_stub.ExposureTimeAbs = feature
    install_fake_vmbpy([cam_stub])
    from drivers.rheed_camera import VmbCamera
    cam = VmbCamera(trigger_hz=1.0, access_mode="full", exposure_us=250_000.0)
    cam.CONNECT_TIMEOUT_S = 0.4
    cam.DISCONNECT_TIMEOUT_S = 0.2

    raised = None
    try:
        cam.connect()
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None, "connect() should have timed out"
    cam.disconnect()
    sets_at_disconnect = feature.set_call_count

    feature.release.set()          # the blocked SDK call finally returns
    thread = cam._stream_thread
    if thread is not None:
        thread.join(timeout=5.0)
    return cam, cam_stub, feature, sets_at_disconnect


def test_abandoned_setup_does_not_write_exposure_afterwards() -> None:
    """The reported defect: ARM fails, then the orphan writes anyway."""
    try:
        cam, cam_stub, feature, sets_at_disconnect = _abandoned_cycle("get")
        assert sets_at_disconnect == 0, "wrote before disconnect returned"
        assert feature.set_call_count == 0, (
            "an abandoned setup thread wrote the exposure after connect() "
            "timed out and disconnect() had already returned — the GUI "
            "reported ARM failed and went idle, then the camera changed"
        )
        assert cam_stub.ExposureTimeAbs.value == 300_000.0
        assert cam_stub.start_streaming_calls == 0, (
            "an abandoned cycle started streaming and held the camera"
        )
    finally:
        uninstall_fake_vmbpy()


def test_cancellation_during_the_write_restores_the_original() -> None:
    """Blocked inside set(): the value may have applied, so restore and verify."""
    try:
        cam, cam_stub, feature, _ = _abandoned_cycle("set")
        assert cam_stub.ExposureTimeAbs.value == 300_000.0, (
            "a write cancelled mid-flight left the camera altered"
        )
        assert cam_stub.start_streaming_calls == 0
    finally:
        uninstall_fake_vmbpy()


def test_reconnect_is_refused_while_the_orphan_thread_lives() -> None:
    """Two threads must never own the same camera."""
    try:
        cam_stub = FakeCamera()
        feature = BlockingExposureFeature(
            initial_value=300_000.0, value_range=(100.0, 900_000.0),
        )
        cam_stub.ExposureTimeAbs = feature
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        cam.CONNECT_TIMEOUT_S = 0.4
        cam.DISCONNECT_TIMEOUT_S = 0.2
        try:
            cam.connect()
        except Exception:  # noqa: BLE001
            pass
        cam.disconnect()

        raised = None
        try:
            cam.connect()               # orphan still blocked
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, (
            "reconnect was allowed while the previous setup thread was still "
            "alive, giving two threads the same camera"
        )
        assert "still running" in str(raised), str(raised)

        # After it drains, a reconnect is permitted again.
        feature.release.set()
        if cam._stream_thread is not None:
            cam._stream_thread.join(timeout=5.0)
        cam.disconnect()
        assert cam._stream_thread is None, (
            "a dead thread's reference was not cleared, so reconnect stays "
            "blocked forever"
        )
    finally:
        uninstall_fake_vmbpy()


def test_set_if_writable_refuses_malformed_access_in_full_mode() -> None:
    from drivers.rheed_camera import VmbCamera
    cam = VmbCamera(trigger_hz=1.0)
    stub = FakeCamera()
    stub.TriggerSource = FakeSettable()
    stub.TriggerSource.get_access_mode = lambda: (True, "False")
    raised = None
    try:
        cam._set_if_writable(stub, "TriggerSource", "Software", "full")
    except RuntimeError as exc:
        raised = exc
    assert raised is not None, "malformed access mode authorised a write"
    assert stub.TriggerSource.set_call_count == 0


def test_set_if_writable_skips_malformed_access_in_read_mode() -> None:
    """Read mode is passive: log and skip, never write, never raise."""
    from drivers.rheed_camera import VmbCamera
    cam = VmbCamera(trigger_hz=1.0)
    stub = FakeCamera()
    stub.TriggerSource = FakeSettable()
    for bogus in ((True, "False"), (1, 1), "nonsense", None):
        stub.TriggerSource.set_call_count = 0
        stub.TriggerSource.get_access_mode = lambda b=bogus: b
        cam._set_if_writable(stub, "TriggerSource", "Software", "read")
        assert stub.TriggerSource.set_call_count == 0, (
            f"Read mode wrote to the camera on access mode {bogus!r} — this "
            "breaks the passive guarantee and can disturb kSA"
        )


class FakeEnumEntry:
    """Shaped like vmbpy's EnumEntry: its string form IS the entry name.

    No separate `.name` attribute — the real class does not offer one that
    can disagree with str(), and adding one would be inventing SDK behaviour
    in order to test it.
    """

    def __init__(self, name: str):
        self._name = name

    def __str__(self) -> str:
        return self._name


def test_enum_entry_off_is_accepted() -> None:
    """The representation vmbpy actually produces for a disabled auto loop."""
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureAuto = FakeSettable(initial_value=FakeEnumEntry("Off"))
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        cam.connect()
        assert cam_stub.ExposureTimeAbs.value == 250_000.0
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_plain_off_string_is_accepted() -> None:
    """A driver handing back the bare string must still work."""
    try:
        cam_stub = FakeCamera()
        cam_stub.ExposureAuto = FakeSettable(initial_value="Off")
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        cam.connect()
        assert cam_stub.ExposureTimeAbs.value == 250_000.0
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


def test_exposure_auto_is_judged_only_by_its_string_form() -> None:
    """EnumEntry's string form IS the entry name — nothing else is consulted.

    REPLACES a "contradictory representation" test that gave the mock a
    separate `.name` disagreeing with its `str()`. Real EnumEntry has no such
    split, so the scenario was unrepresentable, and honouring `.name` meant
    inventing SDK behaviour in order to test it. Anything whose string form
    is not exactly "off" is refused, with no write attempted.
    """
    for bogus in ("Continuous", "Once", "", "0", "False",
                  "'Off'", '"Off"', "ExposureAuto.Off", "EnumEntry(Off)"):
        _refuses_write(
            lambda c, v=bogus: setattr(c.ExposureAuto, "value", v),
            expect_text="ExposureAuto",
            expect_sets=0,
        )

    # Whitespace and case ARE normalised — that is the specified contract,
    # and neither changes which entry the camera named.
    for good in ("Off", "OFF ", " off", "\toff\n"):
        try:
            cam_stub = FakeCamera()
            cam_stub.ExposureAuto = FakeSettable(initial_value=good)
            install_fake_vmbpy([cam_stub])
            from drivers.rheed_camera import VmbCamera
            cam = VmbCamera(
                trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
            )
            cam.connect()
            assert cam_stub.ExposureTimeAbs.value == 250_000.0, good
            cam.disconnect()
        finally:
            uninstall_fake_vmbpy()



def test_sdk_never_emitted_qualified_forms_are_rejected() -> None:
    """These are not vmbpy output; accepting them widened the check for nothing."""
    for bogus in ("ExposureAuto.Off", "EnumEntry(Off)",
                  "EnumEntry(ExposureAuto.Off)"):
        _refuses_write(
            lambda c, v=bogus: setattr(c.ExposureAuto, "value", v),
            expect_text="ExposureAuto",
            expect_sets=0,
        )

def test_absent_optional_features_still_allow_a_valid_write() -> None:
    """Absence is tolerated where unreadability is not — the other half."""
    try:
        cam_stub = FakeCamera()
        # No ExposureAuto, no AcquisitionFrameRateLimit, no increment.
        del cam_stub.ExposureAuto
        del cam_stub.AcquisitionFrameRateLimit
        cam_stub.ExposureTimeAbs = FakeSettable(
            initial_value=300_000.0, value_range=(100.0, 900_000.0),
        )
        install_fake_vmbpy([cam_stub])
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(
            trigger_hz=1.0, access_mode="full", exposure_us=250_000.0,
        )
        cam.connect()
        assert cam_stub.ExposureTimeAbs.value == 250_000.0
        assert cam.exposure_us == 250_000.0
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


TESTS = [
    test_palette_intensity_in_all_channels,
    test_palette_bgw_output,
    test_normalization_fixed_denominator,
    test_frame_luminance_2d_frame,
    test_frame_luminance_rgb_bt601_weights,
    test_frame_luminance_rgb_includes_r_and_b,
    test_connect_then_read_frame,
    test_connect_no_cameras_raises,
    test_connect_import_error_raises_early,
    test_read_frame_before_any_frame_raises_specific_error,
    test_read_frame_before_connect_generic_runtime_error,
    test_stream_error_propagates_to_read_frame,
    test_disconnect_idempotent_when_never_connected,
    test_reconnect_after_disconnect,
    test_bad_frame_recorded_but_stream_survives,
    test_trigger_backoff_after_consecutive_fails,
    # Access-mode fallback (Jul 27 2026 refactor)
    test_auto_full_succeeds_records_access_mode,
    test_auto_full_denied_at_enter_falls_back_to_read,
    test_auto_full_denied_at_set_access_mode_falls_back_to_read,
    test_auto_full_and_read_both_denied_raises_combined_error,
    test_read_mode_skips_writes_when_feature_not_writable,
    test_read_mode_never_calls_trigger_software_run,
    test_access_mode_property_matches_negotiated_mode,
    test_invalid_access_mode_raises_value_error_at_init,
    test_manual_exposure_applied_and_read_back_in_full_mode,
    test_manual_exposure_refuses_read_mode,
    test_manual_exposure_requires_auto_off,
    test_manual_exposure_rejects_unsafe_trigger_pair_at_init,
    test_exposure_lookup_survives_a_raising_feature_and_falls_back,
    test_out_of_range_exposure_fails_without_writing,
    test_bad_readback_restores_the_original_exposure,
    test_unreadable_exposure_auto_refuses_the_write,
    test_exposure_auto_LOOKUP_failure_refuses_the_write,
    test_frame_rate_limit_LOOKUP_failure_refuses_the_write,
    test_missing_range_refuses_the_write,
    test_malformed_range_refuses_the_write,
    test_unreadable_range_refuses_the_write,
    test_unreadable_range_lookup_refuses_the_write,
    test_unreadable_frame_rate_limit_refuses_and_restores,
    test_noncallable_exposure_auto_get_refuses_the_write,
    test_exposure_auto_reporting_no_state_refuses_the_write,
    test_noncallable_frame_rate_limit_get_refuses_and_restores,
    test_nonfinite_frame_rate_limit_refuses_and_restores,
    test_nonpositive_frame_rate_limit_refuses_and_restores,
    test_boolean_frame_rate_limit_refuses_and_restores,
    test_raising_increment_accessor_refuses_the_write,
    test_noncallable_increment_accessor_refuses_the_write,
    test_constructor_rejects_nonfinite_and_boolean_arguments,
    test_malformed_access_mode_refuses_the_write,
    test_unreadable_but_writable_feature_refuses_the_write,
    test_write_that_applies_then_raises_is_restored,
    test_write_that_applies_then_raises_unrestorably_warns_power_cycle,
    test_original_outside_the_reported_range_refuses_before_writing,
    test_dotted_lookalike_exposure_auto_values_are_rejected,
    test_keep_current_does_not_publish_an_unusable_exposure,
    test_nan_original_exposure_refuses_the_write,
    test_nan_readback_refuses_the_write,
    test_boolean_range_endpoint_refuses_the_write,
    test_nonfinite_range_endpoint_refuses_the_write,
    test_quantisation_outside_the_range_refuses_the_write,
    test_readback_one_increment_away_refuses_the_write,
    test_unverifiable_restore_raises_a_power_cycle_error,
    test_raising_restore_readback_raises_a_power_cycle_error,
    test_exposure_auto_suffix_lookalikes_are_rejected,
    test_abandoned_setup_does_not_write_exposure_afterwards,
    test_cancellation_during_the_write_restores_the_original,
    test_reconnect_is_refused_while_the_orphan_thread_lives,
    test_set_if_writable_refuses_malformed_access_in_full_mode,
    test_set_if_writable_skips_malformed_access_in_read_mode,
    test_enum_entry_off_is_accepted,
    test_plain_off_string_is_accepted,
    test_exposure_auto_is_judged_only_by_its_string_form,
    test_sdk_never_emitted_qualified_forms_are_rejected,
    test_absent_optional_features_still_allow_a_valid_write,
    test_read_mode_frame_not_yet_error_includes_read_context,
    test_full_mode_get_access_mode_raise_propagates,
    test_read_mode_performs_zero_configuration_writes,
]


def main() -> int:
    print(f"VmbCamera smoke test — {len(TESTS)} cases")
    print()
    failures: list[tuple[str, BaseException]] = []
    for t in TESTS:
        name = t.__name__
        try:
            t()
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"  ✗ {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ✓ {name}")

    print()
    if failures:
        print(f"FAIL — {len(failures)}/{len(TESTS)} tests failed")
        return 1
    print(f"PASS — {len(TESTS)}/{len(TESTS)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
