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


class _StubEqualizerTab:
    def __init__(self):
        self.save_enabled: list[bool] = []

    def set_save_enabled(self, value: bool) -> None:
        self.save_enabled.append(value)


class _StubMonitor:
    def __init__(self, state: str):
        self._state = state
        self.camera_states: list[CameraState] = []
        self.reset_calls = 0
        # Members below exist because _on_camera_state grew when the launcher
        # baseline merged in RHEED-QC, reconnect and Equalizer handling. They
        # are inert recorders: the assertions in this file are unchanged, and
        # nothing here decides a branch under test.
        self.live_equalizer_tab = _StubEqualizerTab()
        self.rheed_reconnect_required: list[bool] = []
        self.classifier_messages: list[str] = []
        self.auto_capture_statuses: list[str] = []
        self.auto_capture_paused = False
        self.elapsed_seconds = 0.0

    @property
    def state(self) -> str:
        return self._state

    def update_camera_state(self, state: CameraState) -> None:
        self.camera_states.append(state)

    def set_state(self, new_state: str) -> None:
        self._state = new_state

    def reset_displays(self) -> None:
        self.reset_calls += 1

    def set_rheed_reconnect_required(self, required: bool) -> None:
        self.rheed_reconnect_required.append(required)

    def set_classifier_capture_unavailable(self, text: str) -> None:
        self.classifier_messages.append(text)

    def set_auto_capture_status(self, text: str) -> None:
        self.auto_capture_statuses.append(text)

    def is_auto_capture_paused(self) -> bool:
        return self.auto_capture_paused

    def get_elapsed_seconds(self) -> float:
        return self.elapsed_seconds


class _FakeWorker:
    """Stands in for a QThread worker — records the stop()/wait() contract.

    ``isRunning()`` reports True until ``stop()`` is called, which is what
    ``_stop_workers`` polls to decide whether a join is needed and whether the
    worker survived the shared deadline.
    """

    def __init__(self, name: str):
        self.name = name
        self.stop_calls = 0
        self.wait_timeout = None
        self._running = True

    def stop(self) -> None:
        # Deliberately does NOT clear _running: a worker that exits instantly
        # would make _stop_workers skip the join, leaving the contract this
        # test exists to check unexercised. A worker that acknowledges stop
        # and exits during the join is both realistic and the case that
        # matters.
        self.stop_calls += 1

    def isRunning(self) -> bool:
        return self._running

    def wait(self, timeout: int = 0) -> bool:
        self.wait_timeout = timeout
        self._running = False
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
    latest_score = 0.0

    def __init__(self):
        self.evaluated = 0
        self.reset_calls = 0

    def evaluate(self, frame, metadata=None) -> None:
        self.evaluated += 1

    def reset(self) -> None:
        self.reset_calls += 1


class _StubTimer:
    def __init__(self):
        self.running = False

    def isActive(self) -> bool:
        return self.running

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


class _StubGrowthLog:
    def __init__(self, active: bool = False):
        self.active = active
        self.exposure_changes: list[dict] = []

    def record_camera_exposure_change(self, **kwargs) -> bool:
        self.exposure_changes.append(kwargs)
        return True


class _StubQcState:
    session_active = False
    view_segment_id = ""


class _StubApp:
    """Minimal stand-in for GrowthApp with only what _on_camera_state uses."""

    def __init__(self, monitor_state: str):
        self.monitor = _StubMonitor(monitor_state)
        self.rheed_intensity_window = _StubTrendWindow()
        self.auto_capture_engine = _StubAutoCapture()
        self._reported_camera_exposure_us = None
        self._reported_camera_exposure_error = ""
        self._journalled_camera_exposure_generation = 0
        self._journalled_camera_exposure_us = None
        self._journalled_camera_exposure_error = ""
        self._status = _StubStatusBar()
        self.disarm_calls = 0
        # Required by the merged _on_camera_state. `_shutdown_pending` and
        # `growth_log.active` are real branch inputs — both default to the
        # pre-session values these tests intend. The rest are inert.
        self._shutdown_pending = False
        self.growth_log = _StubGrowthLog(active=(monitor_state == "running"))
        self._heartbeat_timer = _StubTimer()
        self._rheed_qc_state = _StubQcState()
        self._camera_capture_interrupted = False
        self._auto_capture_event_count = 0
        # Bind the REAL implementations of the two extracted decision methods
        # — they are precisely what these tests exercise. Only the surrounding
        # scaffolding is stubbed. Bound here rather than as class attributes so
        # the heavy GUI module is imported only when a test actually runs.
        from gui.growth_app import GrowthApp
        self._announce_camera_exposure = (
            GrowthApp._announce_camera_exposure.__get__(self)
        )
        self._journal_camera_exposure = (
            GrowthApp._journal_camera_exposure.__get__(self)
        )
        self._return_to_idle_if_arm_failed = (
            GrowthApp._return_to_idle_if_arm_failed.__get__(self)
        )

    def statusBar(self):
        return self._status

    def _on_disarm(self) -> None:
        self.disarm_calls += 1
        self.monitor._state = "idle"

    # Inert stand-ins for the RHEED-QC recording path, which these tests do
    # not assert on. Present so the real handler can run to completion.
    def _apply_rheed_qc_state(self, *_args, **_kwargs) -> None:
        pass

    def _record_rheed_view_event(self, *_args, **_kwargs) -> None:
        pass

    def _current_auto_capture_metadata(self) -> dict:
        return {}

    @staticmethod
    def _trace_temporal(*_args, **_kwargs) -> None:
        pass


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
        #
        # _stop_workers (plural) is the current contract: it signals every
        # worker first, then joins them inside one shared deadline, and
        # returns the ones still running. The older per-worker _stop_worker
        # gave each its own full timeout.
        _stop_workers = staticmethod(GrowthApp._stop_workers)
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
            # One SHARED 5 s deadline, not 5 s each: the first worker joined
            # gets nearly the whole budget and later ones get what remains, so
            # the contract is "joined within the deadline", not an exact value.
            # Asserting == 5000 would silently re-impose the old serial
            # stop-and-wait that made disarm take the sum of the poll
            # intervals.
            self.assertIsNotNone(
                worker.wait_timeout, f"{name}.wait() was never called",
            )
            self.assertGreater(
                worker.wait_timeout, 0,
                f"{name}.wait() got a non-positive timeout — deadline already "
                f"exhausted before it was joined",
            )
            self.assertLessEqual(
                worker.wait_timeout, 5000,
                f"{name}.wait() exceeded the shared 5 s deadline",
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
