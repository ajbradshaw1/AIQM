#!/usr/bin/env python3
"""A failed ARM must return the GUI to idle — but only a failed ARM.

_on_arm sets `armed` before the camera thread answers. When the connect is
refused (kSA holding the camera, an exposure the device rejects) the async
error path stopped automation but never returned to idle, leaving the
grower with a locked config panel and an ARM button reading DISARM after an
arm that did not succeed.

The fix performs a real disarm rather than relabelling the state: the
pyrometer, MISTRAL, evap and classifier workers arm independently and
succeed, so unlocking the config panel while they still own their hardware
is the condition _on_disarm's "Disarm incomplete" guard exists to prevent.

The other half matters just as much. Losing the camera MID-SESSION must not
tear the session down — sensor logging continues so the record still shows
when capture was lost. These tests pin both directions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui.growth_app import GrowthApp  # noqa: E402
from gui.state import CameraState  # noqa: E402


class _Monitor:
    def __init__(self, state: str = "armed"):
        self._state = state
        self.reset_called = False

    def set_state(self, new_state: str) -> None:
        self._state = new_state

    def reset_displays(self) -> None:
        self.reset_called = True


class _GrowthLog:
    def __init__(self, active: bool = False):
        self.active = active


class _StatusBar:
    def __init__(self):
        self.messages: list[str] = []

    def showMessage(self, text: str, *_args) -> None:
        self.messages.append(text)


class _App:
    """Only what _return_to_idle_if_arm_failed touches."""

    _return_to_idle_if_arm_failed = GrowthApp._return_to_idle_if_arm_failed

    def __init__(self, *, state: str = "armed", session_active: bool = False):
        self.monitor = _Monitor(state)
        self.growth_log = _GrowthLog(session_active)
        self._shutdown_pending = False
        self._status_bar = _StatusBar()
        self.disarm_calls = 0

    def statusBar(self) -> _StatusBar:
        return self._status_bar

    def _on_disarm(self) -> None:
        # Models a CLEAN teardown. The incomplete case, where _on_disarm
        # stays armed and posts its own warning, is covered by the
        # integrated tests below against the real method.
        self.disarm_calls += 1
        self.monitor.set_state("idle")
        self._status_bar.showMessage("Disarmed \u2014 idle")


def _failure() -> CameraState:
    return CameraState(
        connected=False,
        error="Manual exposure requires Full camera access",
    )


def test_failed_arm_performs_a_real_disarm() -> None:
    """Not a relabel: the workers have to be stopped."""
    app = _App()
    app._return_to_idle_if_arm_failed(_failure())
    assert app.disarm_calls == 1, (
        "state was changed without disarming; other workers would keep "
        "their hardware while the config panel unlocked"
    )
    assert app.monitor._state == "idle"


def test_the_grower_is_told_why() -> None:
    """The reason, not just the outcome — the outcome is on the button."""
    app = _App()
    assert app._return_to_idle_if_arm_failed(_failure()) is True
    assert app._status_bar.messages, "no message shown"
    last = app._status_bar.messages[-1]
    assert "Camera did not connect" in last, last
    assert "Full camera access" in last, (
        f"the driver's reason was dropped: {last}"
    )


def test_returns_false_when_it_does_not_act() -> None:
    """The caller keys its own message off this, so it must be honest."""
    assert _App(state="idle")._return_to_idle_if_arm_failed(_failure()) is False
    assert _App(state="running", session_active=True)._return_to_idle_if_arm_failed(
        _failure()
    ) is False


def test_camera_lost_during_preview_also_disarms() -> None:
    """Scope is any pre-session camera loss, not only a refused connect.

    A camera that connects and then drops during preview leaves the grower
    armed with no frames — the same dead end as a refusal, and the same
    honest state is idle. Named for the motivating case; both are covered.
    """
    app = _App()
    dropped = CameraState(connected=False, error="GigE link lost")
    assert app._return_to_idle_if_arm_failed(dropped) is True
    assert app.disarm_calls == 1
    assert "GigE link lost" in app._status_bar.messages[-1]


def test_mid_session_camera_loss_does_not_tear_down() -> None:
    """A running session survives losing the camera.

    Sensor logging continues so the record shows when capture was lost.
    Disarming here would end the session on a camera hiccup — strictly
    worse than a session with a gap in its frames.
    """
    app = _App(state="running", session_active=True)
    app._return_to_idle_if_arm_failed(_failure())
    assert app.disarm_calls == 0, "a mid-session camera loss disarmed"
    assert app.monitor._state == "running"


def test_armed_with_an_active_session_is_left_alone() -> None:
    """growth_log.active is the gate, not the state label alone."""
    app = _App(state="armed", session_active=True)
    app._return_to_idle_if_arm_failed(_failure())
    assert app.disarm_calls == 0
    assert app.monitor._state == "armed"


def test_idle_is_a_no_op() -> None:
    """A camera error while idle has no arm to undo."""
    app = _App(state="idle")
    app._return_to_idle_if_arm_failed(_failure())
    assert app.disarm_calls == 0
    assert not app._status_bar.messages


def test_shutdown_in_progress_is_left_alone() -> None:
    """closeEvent owns teardown once it has started."""
    app = _App()
    app._shutdown_pending = True
    app._return_to_idle_if_arm_failed(_failure())
    assert app.disarm_calls == 0


def test_repeated_errors_disarm_once() -> None:
    """The camera worker can emit several error states; one disarm."""
    app = _App()
    for _ in range(4):
        app._return_to_idle_if_arm_failed(_failure())
    assert app.disarm_calls == 1, (
        f"{app.disarm_calls} disarms from repeated error states"
    )


# ---------------------------------------------------------------------------
# Integrated: the REAL _on_camera_state and the REAL _on_disarm.
#
# The stub above always disarms cleanly, so on its own it cannot see either
# message being overwritten. These drive the whole handler with a real
# _on_disarm, including the case where a worker refuses to stop.
# ---------------------------------------------------------------------------

class _StubbornWorker:
    """A worker that will not stop within the deadline."""

    def __init__(self, stops: bool):
        self._stops = stops
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1

    def isRunning(self) -> bool:
        return not self._stops

    def wait(self, _ms: int = 0) -> bool:
        return self._stops


class _IntegratedMonitor(_Monitor):
    """Enough of GrowthMonitor for _on_disarm and the error branch."""

    def __init__(self, state: str = "armed"):
        super().__init__(state)
        self.start_btn = _Toggle()
        self.live_equalizer_tab = _EqualizerTab()
        self.rheed_reconnect_required = None
        self.classifier_messages: list[str] = []
        self.auto_capture_statuses: list[str] = []
        self.camera_states: list[object] = []

    def update_camera_state(self, state) -> None:
        self.camera_states.append(state)

    def set_rheed_reconnect_required(self, required: bool) -> None:
        self.rheed_reconnect_required = required

    def set_classifier_capture_unavailable(self, text: str) -> None:
        self.classifier_messages.append(text)

    def set_auto_capture_status(self, text: str) -> None:
        self.auto_capture_statuses.append(text)


class _Toggle:
    def __init__(self):
        self.enabled = True

    def setEnabled(self, value: bool) -> None:
        self.enabled = value


class _EqualizerTab:
    def set_save_enabled(self, _value: bool) -> None:
        return None


class _Timer:
    def __init__(self):
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _AutoCapture:
    def __init__(self):
        self.enabled = True

    def reset(self) -> None:
        return None


class _IntegratedApp:
    """GrowthApp shell carrying the real methods under test."""

    _return_to_idle_if_arm_failed = GrowthApp._return_to_idle_if_arm_failed
    _on_disarm = GrowthApp._on_disarm
    # staticmethod on GrowthApp — a bare assignment would rebind it as an
    # instance method and pass `self` in as the first worker.
    _stop_workers = staticmethod(GrowthApp._stop_workers)
    _on_camera_state = GrowthApp._on_camera_state
    _announce_camera_exposure = GrowthApp._announce_camera_exposure

    def __init__(self, *, camera_stops: bool = True):
        self.monitor = _IntegratedMonitor("armed")
        self.growth_log = _GrowthLog(False)
        self._shutdown_pending = False
        self._status_bar = _StatusBar()
        self._heartbeat_timer = _Timer()
        self.auto_capture_engine = _AutoCapture()
        self.camera_worker = _StubbornWorker(camera_stops)
        self.pyrometer_worker = None
        self.mistral_worker = None
        self.evap_worker = None
        self.classifier_worker = None
        self._rheed_qc_state = _QcState()
        self._camera_capture_interrupted = False
        self._reported_camera_exposure_us = None
        self.rheed_intensity_window = _IntensityWindow()

    def statusBar(self) -> _StatusBar:
        return self._status_bar


class _QcState:
    session_active = False


class _IntensityWindow:
    def on_camera_state(self, _state) -> None:
        return None


def _drive(app) -> None:
    """Run the real _on_camera_state error branch."""
    GrowthApp._on_camera_state(app, _failure())


def test_integrated_clean_teardown_leaves_the_reason_on_screen() -> None:
    """Through the whole handler, the final message names the camera fault.

    _on_disarm posts "Disarmed", and the error branch would normally post
    "RHEED capture stopped". Both are less useful than the reason, so the
    reason has to be last.
    """
    app = _IntegratedApp(camera_stops=True)
    _drive(app)
    assert app.monitor._state == "idle", app.monitor._state
    final = app._status_bar.messages[-1]
    assert "Camera did not connect" in final, app._status_bar.messages
    assert "Full camera access" in final, final
    assert "RHEED capture stopped" not in final, (
        "the generic message overwrote the specific one"
    )


def test_integrated_incomplete_disarm_keeps_the_actionable_warning() -> None:
    """A worker that will not stop must leave ITS warning last.

    _on_disarm deliberately stays armed and tells the grower to wait and
    press DISARM again. That instruction is actionable; "camera did not
    connect" is not. Overwriting it — which is what the first version of
    this fix did, twice over — hides the only thing the grower can act on.
    """
    app = _IntegratedApp(camera_stops=False)
    _drive(app)
    assert app.monitor._state == "armed", (
        "stayed idle despite a worker that never stopped"
    )
    final = app._status_bar.messages[-1]
    assert "Disarm incomplete" in final, app._status_bar.messages
    assert "press DISARM again" in final, final


def test_integrated_mid_session_loss_keeps_the_generic_message() -> None:
    """With a session running nothing is torn down, so the generic message
    is the right one and must not be suppressed."""
    app = _IntegratedApp(camera_stops=True)
    app.growth_log.active = True
    app.monitor.set_state("running")
    _drive(app)
    assert app.monitor._state == "running"
    assert "RHEED capture stopped" in app._status_bar.messages[-1], (
        app._status_bar.messages
    )

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
