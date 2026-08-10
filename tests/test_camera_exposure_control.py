#!/usr/bin/env python3
"""Grower-facing exposure control — slider, textbox, and the safety ceiling.

The control is a kSA-style pair: a 1-999 ms slider for a quick sweep and a
textbox for an exact value, two views of one integer. "Keep current" is a
separate checkbox rather than a magic zero, because the range starts at
1 ms and the no-write path has to stay reachable — it is the only state in
which the GUI issues no camera write at all.

The full 1-999 ms range is offered because that is what growers know from
kSA, but achievability also depends on the trigger rate: an exposure longer
than ~90% of the trigger period leaves no room for transport and readout.
At the 1 Hz default that ceiling is 900 ms. The slider still reaches 999,
the warning appears above the ceiling, and VmbCamera refuses the value at
ARM — so an unachievable request fails early and legibly instead of turning
into silently under-delivered frames.

NOT RUN on the Mac dev box as of 2026-08-10: the Qt platform plugin is
unavailable there, so every QApplication-based test aborts regardless of
branch. Verified in CI (ubuntu-24.04) and on the lab machine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# QApplication precedes any QWidget. Same pattern as test_events_tab.py.
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

from gui.growth_monitor import GrowthMonitor  # noqa: E402


def _monitor() -> GrowthMonitor:
    monitor = GrowthMonitor()
    # Default is "Keep current"; most cases here are about a chosen value.
    monitor.config_camera_exposure_keep.setChecked(False)
    return monitor


def test_range_matches_ksa() -> None:
    monitor = _monitor()
    assert (
        monitor.config_camera_exposure_slider.minimum(),
        monitor.config_camera_exposure_slider.maximum(),
    ) == (1, 999)
    assert (
        monitor.config_camera_exposure_ms.minimum(),
        monitor.config_camera_exposure_ms.maximum(),
    ) == (1, 999)


def test_slider_and_textbox_track_each_other() -> None:
    """Two views of one value, in both directions, without feedback loops."""
    monitor = _monitor()
    monitor.config_camera_exposure_slider.setValue(250)
    assert monitor.config_camera_exposure_ms.value() == 250
    monitor.config_camera_exposure_ms.setValue(700)
    assert monitor.config_camera_exposure_slider.value() == 700
    # A round trip must not drift — the mutual updates block signals to
    # avoid re-entering, and a bug there would show up as an off-by-step.
    monitor.config_camera_exposure_slider.setValue(1)
    assert monitor.config_camera_exposure_ms.value() == 1
    monitor.config_camera_exposure_ms.setValue(999)
    assert monitor.config_camera_exposure_slider.value() == 999


def test_selected_exposure_is_microseconds() -> None:
    """The driver takes microseconds; the UI speaks milliseconds."""
    monitor = _monitor()
    monitor.config_camera_exposure_ms.setValue(300)
    assert monitor.selected_exposure_us() == 300_000.0


def test_keep_current_means_no_request() -> None:
    """Checked, the control yields None and disables its value widgets."""
    monitor = _monitor()
    monitor.config_camera_exposure_ms.setValue(300)
    monitor.config_camera_exposure_keep.setChecked(True)
    assert monitor.selected_exposure_us() is None
    assert not monitor.config_camera_exposure_slider.isEnabled()
    assert not monitor.config_camera_exposure_ms.isEnabled()
    # No warning while nothing will be written.
    assert monitor.config_camera_exposure_warning.text() == ""


def test_warning_appears_only_above_the_ceiling() -> None:
    """The grower learns before ARM, not from a failed connect."""
    monitor = _monitor()
    ceiling = monitor._exposure_ceiling_ms

    monitor.config_camera_exposure_ms.setValue(int(ceiling) - 1)
    assert monitor.config_camera_exposure_warning.text() == "", (
        "warned about an achievable exposure"
    )
    monitor.config_camera_exposure_ms.setValue(int(ceiling) + 1)
    text = monitor.config_camera_exposure_warning.text()
    assert text, "no warning above the trigger-rate ceiling"
    assert f"{ceiling:.0f}" in text, text


def test_over_ceiling_value_is_refused_by_the_driver() -> None:
    """The UI warns; the driver is what actually enforces it.

    The warning is guidance and could be dismissed or missed. Pin that the
    same value the UI flags is genuinely rejected before any camera write,
    so the two cannot drift into a UI that warns about a value the driver
    happily accepts.
    """
    from drivers.rheed_camera import VmbCamera

    monitor = _monitor()
    monitor.config_camera_exposure_ms.setValue(999)
    requested_us = monitor.selected_exposure_us()
    assert monitor.config_camera_exposure_warning.text(), "UI did not warn"

    try:
        VmbCamera(trigger_hz=1.0, exposure_us=requested_us)
    except ValueError as exc:
        assert "headroom" in str(exc), exc
    else:
        raise AssertionError(
            "the driver accepted an exposure the UI flagged as unachievable"
        )


def test_exposure_controls_lock_during_a_session() -> None:
    """Exposure is fixed at ARM; it must not be editable mid-growth.

    The connect-time settings snapshot describes the whole session, which is
    only honest while the value cannot change under it.
    """
    monitor = _monitor()
    # The lock set is the keys of the saved-tooltip dict: every widget the
    # arm/disarm cycle disables and re-enables.
    locked = monitor._config_widget_original_tooltips
    assert monitor.config_camera_exposure_slider in locked
    assert monitor.config_camera_exposure_ms in locked
    assert monitor.config_camera_exposure_keep in locked


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
