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
    ):
        self.value: object = None
        self.set_call_count = 0
        self._readable = readable
        self._writable = writable
        self._get_access_mode_raises = get_access_mode_raises

    def set(self, value) -> None:  # noqa: A003
        self.value = value
        self.set_call_count += 1

    def get_access_mode(self) -> tuple[bool, bool]:
        if self._get_access_mode_raises is not None:
            raise self._get_access_mode_raises
        return (self._readable, self._writable)


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


def uninstall_fake_vmbpy() -> None:
    sys.modules.pop("vmbpy", None)
    FakeVmbSystem._cameras = []
    FakeVmbSystem._raise_on_get_all_cameras = None


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


def test_read_mode_get_access_mode_raise_is_logged_and_skipped() -> None:
    """(k) In Read mode, feature.get_access_mode() raising is logged and skipped.

    The driver is intentionally passive in Read; a bounded probe failure
    on one feature shouldn't kill the whole connect. Subsequent features
    with functioning probes proceed as normal — constraint 3's
    "log-and-skip in Read" rule.
    """
    try:
        fake_cam = install_fake_vmbpy()
        # Probe raise on TriggerSource ONLY; other features probe normally
        fake_cam.TriggerSource._get_access_mode_raises = RuntimeError(
            "SDK probe broke"
        )
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=100.0, access_mode="read")
        cam.connect()
        # Read-mode's probe-raise handling let connect succeed
        assert cam.connected
        assert cam.access_mode == "read"
        # TriggerSource.set() was skipped (probe raised, driver logged-and-skipped)
        assert fake_cam.TriggerSource.set_call_count == 0, (
            "TriggerSource.set should be skipped when probe raises in Read"
        )
        # Loop continued past TriggerSource — TriggerSelector's probe
        # succeeded (default writable=True), so .set() fired.
        assert fake_cam.TriggerSelector.set_call_count == 1, (
            "TriggerSelector.set should have fired — proves the driver "
            "continued past TriggerSource's probe failure"
        )
        cam.disconnect()
    finally:
        uninstall_fake_vmbpy()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

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
    test_read_mode_frame_not_yet_error_includes_read_context,
    test_full_mode_get_access_mode_raise_propagates,
    test_read_mode_get_access_mode_raise_is_logged_and_skipped,
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
