"""A refused camera ARM must return the GUI to idle — not fake being armed.

`RheedCameraWorker.run()` emits exactly one CameraState and returns when
`connect()` raises, so a refused ARM is a single `connected=False` state with
a populated `error`. `GrowthApp._on_arm` sets the monitor to "armed" before
the worker thread has answered, so without an explicit teardown the GUI is
left with a locked config panel, a DISARM button, and every other worker
running while it owns no camera.

Manual exposure makes that reachable by design: requesting an exposure write
while kSA or the Vimba X Viewer holds Full access is refused on purpose.

The mid-session case is deliberately the opposite — losing the camera during
a growth must not tear down a running session, so the teardown is gated on
the "armed" state rather than on the error alone.

These tests drive `GrowthApp._on_camera_state` against a stub `self` rather
than constructing the full window: the logic under test is the state
decision, and a real GrowthApp pulls in every tab, worker and the classifier
bridge.

Run:
    python -m pytest -q tests/test_camera_arm_failure.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gui.state import CameraState  # noqa: E402


class _StubMonitor:
    def __init__(self, state: str):
        self._state = state
        self.camera_states: list[CameraState] = []
        self.reset_calls = 0

    @property
    def state(self) -> str:
        return self._state

    def update_camera_state(self, state: CameraState) -> None:
        self.camera_states.append(state)

    def set_state(self, new_state: str) -> None:
        self._state = new_state

    def reset_displays(self) -> None:
        self.reset_calls += 1


class _FakeWorker:
    """Stands in for a QThread worker — records the stop()/wait() contract."""

    def __init__(self, name: str):
        self.name = name
        self.stop_calls = 0
        self.wait_timeout = None

    def stop(self) -> None:
        self.stop_calls += 1

    def wait(self, timeout: int = 0) -> bool:
        self.wait_timeout = timeout
        return True


class _StubStatusBar:
    def __init__(self):
        self.messages: list[str] = []

    def showMessage(self, text: str, timeout: int = 0) -> None:
        self.messages.append(text)


class _StubTrendWindow:
    def on_camera_state(self, state: CameraState) -> None:
        pass


class _StubAutoCapture:
    enabled = False

    def __init__(self):
        self.evaluated = 0

    def evaluate(self, frame) -> None:
        self.evaluated += 1


class _StubApp:
    """Minimal stand-in for GrowthApp with only what _on_camera_state uses."""

    def __init__(self, monitor_state: str):
        self.monitor = _StubMonitor(monitor_state)
        self.rheed_intensity_window = _StubTrendWindow()
        self.auto_capture_engine = _StubAutoCapture()
        self._reported_camera_exposure_us = None
        self._status = _StubStatusBar()
        self.disarm_calls = 0

    def statusBar(self):
        return self._status

    def _on_disarm(self) -> None:
        self.disarm_calls += 1
        self.monitor._state = "idle"


def _dispatch(app, state: CameraState) -> None:
    """Invoke the real GrowthApp._on_camera_state against a stub."""
    from gui.growth_app import GrowthApp
    GrowthApp._on_camera_state(app, state)


# The decision tests above stub _on_disarm to isolate the branch logic. This
# harness instead runs the PRODUCTION _on_disarm, because the whole point of
# the fix is that the other successfully-armed workers must not keep running
# after the camera refuses. A disarm that only relabels the state would pass
# every decision test and still leave the pyrometer, MISTRAL, EvapControl and
# classifier threads live against hardware the GUI claims to have released.

WORKER_ATTRS = (
    "camera_worker",
    "pyrometer_worker",
    "mistral_worker",
    "evap_worker",
    "classifier_worker",
)


def _make_real_disarm_app(monitor_state: str = "armed"):
    from gui.growth_app import GrowthApp

    class _RealDisarmApp(_StubApp):
        # staticmethod() matters: a bare assignment would rebind the plain
        # function as an instance method and pass `self` as the worker.
        _stop_worker = staticmethod(GrowthApp._stop_worker)
        _on_disarm = GrowthApp._on_disarm

    app = _RealDisarmApp(monitor_state)
    app.workers = {name: _FakeWorker(name) for name in WORKER_ATTRS}
    for name, worker in app.workers.items():
        setattr(app, name, worker)
    return app


class RefusedArmTests(unittest.TestCase):

    def test_failed_connect_while_armed_performs_a_real_disarm(self):
        app = _StubApp("armed")
        _dispatch(app, CameraState(
            connected=False,
            error="Manual exposure requires Full camera access.",
        ))
        self.assertEqual(app.disarm_calls, 1, "GUI stayed armed without a camera")
        self.assertEqual(app.monitor.state, "idle")

    def test_the_refusal_reason_reaches_the_status_bar(self):
        app = _StubApp("armed")
        _dispatch(app, CameraState(
            connected=False,
            error="Manual exposure requires Full camera access.",
        ))
        self.assertTrue(
            any("Full camera access" in m for m in app._status.messages),
            f"camera's reason was not surfaced: {app._status.messages}",
        )

    def test_camera_loss_during_a_session_does_not_disarm(self):
        """A running growth must survive mid-session camera loss."""
        app = _StubApp("running")
        _dispatch(app, CameraState(
            connected=False, error="stream died mid-session",
        ))
        self.assertEqual(
            app.disarm_calls, 0,
            "mid-session camera loss tore down a running session",
        )
        self.assertEqual(app.monitor.state, "running")

    def test_idle_state_is_untouched(self):
        """An error arriving while already idle must not re-disarm."""
        app = _StubApp("idle")
        _dispatch(app, CameraState(connected=False, error="late error"))
        self.assertEqual(app.disarm_calls, 0)

    def test_repeated_error_states_disarm_only_once(self):
        app = _StubApp("armed")
        for _ in range(3):
            _dispatch(app, CameraState(connected=False, error="refused"))
        self.assertEqual(
            app.disarm_calls, 1, "re-entered disarm on a repeated error state",
        )

    def test_successful_connect_does_not_disarm(self):
        app = _StubApp("armed")
        _dispatch(app, CameraState(
            connected=True, exposure_us=250_000.0, error="",
        ))
        self.assertEqual(app.disarm_calls, 0)
        self.assertEqual(app.monitor.state, "armed")

    def test_confirmed_exposure_is_announced_once(self):
        app = _StubApp("armed")
        for _ in range(2):
            _dispatch(app, CameraState(connected=True, exposure_us=250_000.0))
        announcements = [
            m for m in app._status.messages if "exposure confirmed" in m
        ]
        self.assertEqual(len(announcements), 1, app._status.messages)
        self.assertIn("250 ms", announcements[0])


class RealDisarmTests(unittest.TestCase):
    """Drives the production _on_disarm, not a stub of it."""

    def test_every_worker_is_stopped_and_waited_on(self):
        app = _make_real_disarm_app()
        app._on_disarm()
        for name, worker in app.workers.items():
            self.assertEqual(
                worker.stop_calls, 1, f"{name}.stop() not called exactly once",
            )
            self.assertEqual(
                worker.wait_timeout, 5000,
                f"{name}.wait() not called with the 5 s join timeout",
            )

    def test_every_worker_reference_is_cleared(self):
        app = _make_real_disarm_app()
        app._on_disarm()
        for name in WORKER_ATTRS:
            self.assertIsNone(
                getattr(app, name), f"{name} still holds a worker reference",
            )

    def test_displays_reset_and_state_returns_to_idle(self):
        app = _make_real_disarm_app()
        app._on_disarm()
        self.assertEqual(app.monitor.reset_calls, 1)
        self.assertEqual(app.monitor.state, "idle")

    def test_refused_arm_tears_down_the_other_hardware_workers(self):
        """End to end: camera refusal -> production disarm -> threads stopped.

        This is the behaviour the fix exists for. The pyrometer, MISTRAL,
        EvapControl and classifier workers armed successfully and are holding
        real instruments; a camera refusal must release them, not strand them.
        """
        app = _make_real_disarm_app("armed")
        _dispatch(app, CameraState(
            connected=False,
            error="Manual exposure requires Full camera access.",
        ))
        self.assertEqual(app.monitor.state, "idle")
        for name, worker in app.workers.items():
            self.assertEqual(
                worker.stop_calls, 1, f"{name} left running after refused ARM",
            )
            self.assertIsNone(getattr(app, name), f"{name} reference stranded")
        self.assertTrue(
            any("Full camera access" in m for m in app._status.messages),
            f"camera's reason was not surfaced: {app._status.messages}",
        )

    def test_mid_session_loss_leaves_every_worker_running(self):
        app = _make_real_disarm_app("running")
        _dispatch(app, CameraState(
            connected=False, error="stream died mid-session",
        ))
        self.assertEqual(app.monitor.state, "running")
        for name, worker in app.workers.items():
            self.assertEqual(
                worker.stop_calls, 0,
                f"{name} was stopped by a mid-session camera loss",
            )


if __name__ == "__main__":
    unittest.main()
