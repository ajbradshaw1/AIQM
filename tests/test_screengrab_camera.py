#!/usr/bin/env python3
"""Pure-logic tests for ScreenGrabCamera — Mac-side, no ctypes / no mss.

Covers the driver's *portable* logic paths:

- ``_crop_chrome_pixels`` with various frame sizes + defensive returns
- ``_bgra_to_rgb`` channel-swap correctness
- ``get_info`` dict shape
- ``visualize_crop`` overlay geometry
- ``connected`` property + consecutive-failure disconnect

Skips the ``ctypes.windll`` window-finding and the ``mss`` full-integration
paths — both are Windows-only and require real screen state to test
meaningfully. Those need lab validation, not smoke tests.

Usage:
    python -m pytest -q tests/test_screengrab_camera.py

Exits 0 on success; raises AssertionError with a diagnostic on failure.

Built Jul 2 2026 as the pre-lab safety net for the historically-primary
screen-capture path. Tests protect the pure algorithm; integration is
still Bulbasaur-only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from drivers.rheed_camera import (  # noqa: E402
    ScreenGrabCamera,
    _configure_rheed_user32_argtypes,
    _window_dpi_identity,
)


# ---------------------------------------------------------------------------
# Tests — pure functions
# ---------------------------------------------------------------------------

def test_construct_defaults() -> None:
    """Constructor stores documented defaults and never touches mss/ctypes."""
    cam = ScreenGrabCamera()
    assert cam._window_title == "Live Video"
    assert cam._crop_chrome is True
    assert cam._chrome_top_px == 75
    assert cam._chrome_bottom_px == 30
    assert cam._backend == "wgc"
    assert not cam.connected
    assert cam._capture_method is None  # not set until connect()
    assert cam._consecutive_fails == 0


def test_construct_custom_params() -> None:
    """Constructor honors overrides for window_title, chrome bounds, and disable."""
    cam = ScreenGrabCamera(
        window_title="Custom",
        crop_chrome=False,
        chrome_top_px=100,
        chrome_bottom_px=50,
    )
    assert cam._window_title == "Custom"
    assert cam._crop_chrome is False
    assert cam._chrome_top_px == 100
    assert cam._chrome_bottom_px == 50
    assert cam.capture_geometry_id == "ksa-chrome-v2:0:100:50:disabled"


def test_window_dpi_identity_is_native_or_explicit_fallback() -> None:
    """DPI lookup is deterministic and never disguises fallback as native."""
    assert (
        _window_dpi_identity(1234, lambda hwnd: 144 if hwnd == 1234 else 0)
        == "dpi-getdpiforwindow-144"
    )
    assert _window_dpi_identity(1234, lambda _hwnd: 0) == "dpi-fallback-96"

    def failed_lookup(_hwnd: int) -> int:
        raise RuntimeError("simulated GetDpiForWindow failure")

    assert _window_dpi_identity(1234, failed_lookup) == "dpi-fallback-96"


def test_only_wgc_geometry_identity_tracks_window_dpi() -> None:
    """WGC includes DPI while the opt-in legacy mss ID stays compatible."""
    dpi = [96]
    cam = ScreenGrabCamera(dpi_provider=lambda _hwnd: dpi[0])
    cam._capture_method = "wgc"
    cam._refresh_wgc_dpi_identity(1234)
    first = cam.capture_geometry_id
    assert first.endswith(":dpi-getdpiforwindow-96")

    dpi[0] = 144
    cam._refresh_wgc_dpi_identity(1234)
    second = cam.capture_geometry_id
    assert second.endswith(":dpi-getdpiforwindow-144")
    assert second != first

    legacy = ScreenGrabCamera.legacy_mss(
        crop_chrome=False,
        chrome_top_px=100,
        chrome_bottom_px=50,
    )
    legacy._capture_method = "mss"
    assert legacy.capture_geometry_id == "ksa-chrome-v2:0:100:50:disabled"


def test_user32_argtypes_configuration_is_safe() -> None:
    """64-bit HWND declarations are idempotent and cross-platform safe."""
    _configure_rheed_user32_argtypes()
    _configure_rheed_user32_argtypes()


def test_crop_chrome_pixels_typical() -> None:
    """Standard crop on a 500-row frame: output = 500 - 75 - 30 = 395 rows."""
    cam = ScreenGrabCamera(backend="mss")
    frame = np.zeros((500, 800, 3), dtype=np.uint8)
    out = cam._crop_chrome_pixels(frame)
    assert out.shape == (395, 800, 3), f"got {out.shape}"
    assert cam.capture_geometry_id.endswith(":applied")


def test_crop_chrome_pixels_disabled() -> None:
    """crop_chrome=False returns the frame unchanged."""
    cam = ScreenGrabCamera(crop_chrome=False)
    frame = np.random.randint(0, 255, (500, 800, 3), dtype=np.uint8)
    out = cam._crop_chrome_pixels(frame)
    assert out.shape == frame.shape
    assert (out == frame).all()


def test_crop_chrome_pixels_defensive_tiny_frame() -> None:
    """Frame smaller than crop bounds → returned unchanged (no empty array)."""
    cam = ScreenGrabCamera()  # defaults: 75 top + 30 bottom = 105 min height
    tiny = np.random.randint(0, 255, (50, 100, 3), dtype=np.uint8)
    out = cam._crop_chrome_pixels(tiny)
    # 50 - 30 = 20; 20 <= 75 → defensive path returns frame unchanged
    assert out.shape == tiny.shape
    assert (out == tiny).all()
    assert cam.capture_geometry_id.endswith(":fallback-full")
    try:
        cam._require_effective_crop()
    except RuntimeError as exc:
        assert "crop is invalid" in str(exc)
    else:
        raise AssertionError("WGC must reject an unapplied configured crop")


def test_crop_chrome_pixels_edge_exact_min() -> None:
    """Frame exactly at min height (top + bottom + 1) → returns 1-row slice."""
    cam = ScreenGrabCamera(chrome_top_px=10, chrome_bottom_px=5)
    frame = np.arange(16 * 4 * 3, dtype=np.uint8).reshape(16, 4, 3)
    out = cam._crop_chrome_pixels(frame)
    # bottom = 16 - 5 = 11; top = 10; result has 1 row
    assert out.shape == (1, 4, 3), f"expected (1, 4, 3), got {out.shape}"


def test_bgra_to_rgb_channel_swap() -> None:
    """BGRA → RGB drops alpha + swaps channel order."""
    bgra = np.zeros((5, 5, 4), dtype=np.uint8)
    bgra[:, :, 0] = 200  # B
    bgra[:, :, 1] = 100  # G
    bgra[:, :, 2] = 50   # R
    bgra[:, :, 3] = 255  # A (should be discarded)
    rgb = ScreenGrabCamera._bgra_to_rgb(bgra)
    assert rgb.shape == (5, 5, 3), f"expected (5, 5, 3), got {rgb.shape}"
    assert (rgb[:, :, 0] == 50).all(), "R channel wrong"
    assert (rgb[:, :, 1] == 100).all(), "G channel wrong"
    assert (rgb[:, :, 2] == 200).all(), "B channel wrong"


def test_bgra_to_rgb_preserves_shape_dtype() -> None:
    """RGB output preserves dtype and (H, W) dimensions."""
    bgra = np.random.randint(0, 255, (12, 20, 4), dtype=np.uint8)
    rgb = ScreenGrabCamera._bgra_to_rgb(bgra)
    assert rgb.dtype == np.uint8
    assert rgb.shape == (12, 20, 3)


def test_get_info_shape() -> None:
    """get_info returns the expected keys with correct types."""
    cam = ScreenGrabCamera(
        window_title="Live Video",
        chrome_top_px=75,
        chrome_bottom_px=30,
    )
    info = cam.get_info()
    expected_keys = {
        "name", "platform", "capture_method", "window_title",
        "backend", "crop_chrome", "chrome_top_px", "chrome_bottom_px",
    }
    assert set(info) == expected_keys, f"unexpected keys: {set(info)}"
    assert info["name"] == "ScreenGrabCamera"
    assert info["window_title"] == "Live Video"
    assert info["chrome_top_px"] == 75
    assert info["chrome_bottom_px"] == 30
    assert info["crop_chrome"] is True
    # capture_method is "not_connected" pre-connect
    assert info["capture_method"] == "not_connected"


def test_visualize_crop_green_lines() -> None:
    """visualize_crop draws horizontal green lines at the top/bottom bounds."""
    cam = ScreenGrabCamera(chrome_top_px=75, chrome_bottom_px=30)
    frame = np.zeros((500, 800, 3), dtype=np.uint8)
    annotated = cam.visualize_crop(frame)

    # Original frame untouched (we returned a copy)
    assert (frame == 0).all(), "original frame was mutated"

    # Top line at rows 75-76 (2-px thick)
    assert (annotated[75, :, 1] == 255).all(), "top-line row 75 not green"
    assert (annotated[76, :, 1] == 255).all(), "top-line row 76 not green"
    assert (annotated[75, :, 0] == 0).all()  # R = 0
    assert (annotated[75, :, 2] == 0).all()  # B = 0

    # Bottom line at rows 468-469 (500 - 30 - 2 = 468, thickness 2)
    assert (annotated[468, :, 1] == 255).all(), "bottom row 468 not green"
    assert (annotated[469, :, 1] == 255).all(), "bottom row 469 not green"

    # Interior rows are unchanged
    assert (annotated[200, :, :] == 0).all()


def test_visualize_crop_defensive_bounds_too_large() -> None:
    """visualize_crop clamps top/bottom to frame bounds, no IndexError."""
    cam = ScreenGrabCamera(chrome_top_px=1000, chrome_bottom_px=1000)
    frame = np.zeros((100, 50, 3), dtype=np.uint8)
    # Should not raise
    out = cam.visualize_crop(frame)
    assert out.shape == frame.shape


def test_read_frame_before_connect_raises() -> None:
    """read_frame before connect() → RuntimeError 'not connected'."""
    cam = ScreenGrabCamera(backend="mss")
    try:
        cam.read_frame()
    except RuntimeError as exc:
        assert "not connected" in str(exc).lower(), f"unexpected: {exc}"
        return
    raise AssertionError("expected RuntimeError")


def test_disconnect_resets_state() -> None:
    """disconnect() clears connected + consecutive-fail + cross-platform warn."""
    cam = ScreenGrabCamera(backend="mss")
    # Manually flip flags to simulate an active session
    cam._connected = True
    cam._consecutive_fails = 3
    cam._warned_cross_platform = True

    cam.disconnect()

    assert not cam.connected
    assert cam._consecutive_fails == 0
    assert cam._warned_cross_platform is False


def test_consecutive_failure_marks_disconnected() -> None:
    """MAX_CONSECUTIVE_FAILS raises from read_frame flips connected to False."""
    cam = ScreenGrabCamera(backend="mss")
    cam._connected = True  # simulate a connected driver

    # Replace the grab method with one that always raises so read_frame's
    # try/except path always registers a failure. We use _grab_cross_platform
    # since it's what runs on darwin, but overriding both is safer.
    def always_fail():
        raise RuntimeError("simulated grab failure")

    cam._grab_win32 = always_fail
    cam._grab_cross_platform = always_fail

    for i in range(cam.MAX_CONSECUTIVE_FAILS):
        assert cam.connected, f"prematurely disconnected at iteration {i}"
        try:
            cam.read_frame()
        except RuntimeError:
            pass
    # After MAX_CONSECUTIVE_FAILS raises, connected should be False
    assert not cam.connected, "expected disconnected after N consecutive fails"


def test_consecutive_failure_counter_resets_on_success() -> None:
    """A successful read after N-1 failures resets the counter."""
    cam = ScreenGrabCamera(backend="mss")
    cam._connected = True

    # Alternate: N-1 failures, then one success, then N-1 more failures
    # → should NOT disconnect (counter reset after the success).
    call_count = [0]

    def flaky_grab():
        call_count[0] += 1
        # Fail on the first N-1 calls, succeed on the Nth, fail after.
        if call_count[0] == cam.MAX_CONSECUTIVE_FAILS:
            # Return a valid frame that survives _crop_chrome_pixels.
            return np.zeros((500, 800, 3), dtype=np.uint8)
        raise RuntimeError("simulated grab failure")

    cam._grab_win32 = flaky_grab
    cam._grab_cross_platform = flaky_grab

    # First N-1: raise
    for _ in range(cam.MAX_CONSECUTIVE_FAILS - 1):
        try:
            cam.read_frame()
        except RuntimeError:
            pass
    assert cam.connected  # not yet at threshold
    # The Nth call: succeeds, resets counter
    _ = cam.read_frame()
    assert cam.connected
    assert cam._consecutive_fails == 0

    # Now N-1 more failures: still connected (counter is fresh)
    for _ in range(cam.MAX_CONSECUTIVE_FAILS - 1):
        try:
            cam.read_frame()
        except RuntimeError:
            pass
    assert cam.connected, "counter did not reset after successful read"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_construct_defaults,
    test_construct_custom_params,
    test_window_dpi_identity_is_native_or_explicit_fallback,
    test_only_wgc_geometry_identity_tracks_window_dpi,
    test_user32_argtypes_configuration_is_safe,
    test_crop_chrome_pixels_typical,
    test_crop_chrome_pixels_disabled,
    test_crop_chrome_pixels_defensive_tiny_frame,
    test_crop_chrome_pixels_edge_exact_min,
    test_bgra_to_rgb_channel_swap,
    test_bgra_to_rgb_preserves_shape_dtype,
    test_get_info_shape,
    test_visualize_crop_green_lines,
    test_visualize_crop_defensive_bounds_too_large,
    test_read_frame_before_connect_raises,
    test_disconnect_resets_state,
    test_consecutive_failure_marks_disconnected,
    test_consecutive_failure_counter_resets_on_success,
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    print(f"ScreenGrabCamera smoke test — {len(TESTS)} cases")
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
