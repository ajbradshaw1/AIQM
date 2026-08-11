"""Focused tests for bounded GrowthApp worker shutdown.

These tests use small synchronous fakes and never start a Qt worker thread.
One regression test creates an offscreen ``QApplication`` to verify that
auxiliary top-level windows cannot keep the event loop alive.  Run directly
with::

    python -m pytest -q tests/test_worker_shutdown.py
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ``growth_app`` imports plotting windows, although these shutdown tests never
# instantiate them.  Keep this focused test runnable in minimal CI images that
# provide PyQt6 but omit the optional plotting package.
try:
    import pyqtgraph  # noqa: F401
except ModuleNotFoundError:
    sys.modules["pyqtgraph"] = types.ModuleType("pyqtgraph")

from gui.growth_app import GrowthApp  # noqa: E402
from gui.growth_logger import AutoCaptureCommitResult  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMainWindow  # noqa: E402


class _FakeClock:
    def __init__(self, start: float = 100.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now


class _FakeWorker:
    def __init__(
        self,
        name: str,
        clock: _FakeClock,
        event_log: list[tuple[str, str, int | None]],
        wait_cost_ms: int = 0,
        wait_result: bool = True,
    ):
        self.name = name
        self.clock = clock
        self.event_log = event_log
        self.wait_cost_ms = wait_cost_ms
        self.wait_result = wait_result
        self.wait_timeouts: list[int] = []
        self.running = True

    def stop(self) -> None:
        self.event_log.append((self.name, "stop", None))

    def isRunning(self) -> bool:  # Qt spelling is part of the worker API.
        return self.running

    def wait(self, timeout_ms: int) -> bool:
        self.wait_timeouts.append(timeout_ms)
        self.event_log.append((self.name, "wait", timeout_ms))
        self.clock.now += self.wait_cost_ms / 1000.0
        if self.wait_result:
            self.running = False
        return self.wait_result


class _Timer:
    def __init__(self):
        self.stopped = False
        self.started = False

    def stop(self) -> None:
        self.stopped = True

    def start(self) -> None:
        self.started = True
        self.stopped = False

    def isActive(self) -> bool:
        return self.started and not self.stopped


class _AutoCaptureEngine:
    def __init__(self):
        self.enabled = True
        self.reset_count = 0
        self.evaluations: list[tuple[object, dict]] = []

    def reset(self) -> None:
        self.reset_count += 1

    def evaluate(self, frame, metadata: dict) -> None:
        self.evaluations.append((frame, metadata))

    @staticmethod
    def get_recent_captures() -> list:
        return []


class _AuxiliaryWindow:
    def __init__(self):
        self.closed = False
        self.camera_states: list[object] = []

    def close(self) -> None:
        self.closed = True

    def on_camera_state(self, state) -> None:
        self.camera_states.append(state)


class _Monitor:
    def __init__(self):
        self.reset_called = False
        self.state = None
        self.start_btn = _Button()
        self.arm_btn = _Button()
        self._elapsed_timer = _Timer()
        self._state = "armed"
        self.config_camera_mode = _Combo("screengrab")
        self.reconnect_states: list[tuple[bool, bool]] = []
        self.camera_states: list[object] = []
        self._latest_pyro = None
        self.auto_capture_statuses: list[str] = []
        self.auto_capture_events: list[tuple[int, float, str]] = []

    def reset_displays(self) -> None:
        self.reset_called = True

    def set_state(self, state: str) -> None:
        self.state = state

    def set_rheed_reconnect_required(
        self, required: bool, *, in_progress: bool = False,
    ) -> None:
        self.reconnect_states.append((required, in_progress))

    def update_camera_state(self, state) -> None:
        self.camera_states.append(state)

    @staticmethod
    def get_elapsed_seconds() -> float:
        return 12.0

    def set_auto_capture_status(self, status: str) -> None:
        self.auto_capture_statuses.append(status)

    def show_auto_capture_event(
        self, *, event_idx: int, score: float, buffer_dir: str,
    ) -> None:
        self.auto_capture_events.append((event_idx, score, buffer_dir))

    @staticmethod
    def get_session_metadata() -> dict:
        return {"sample_id": "shutdown-test"}


class _Combo:
    def __init__(self, value: str):
        self.value = value

    def currentText(self) -> str:
        return self.value


class _Button:
    def __init__(self):
        self.enabled = True

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled


class _StatusBar:
    def __init__(self):
        self.messages: list[str] = []

    def showMessage(self, message: str, *_args) -> None:
        self.messages.append(message)


class _GrowthLog:
    def __init__(self):
        self.active = False
        self.saved_metadata = None
        self.ended = False
        self.plot_generated = False
        self.auto_capture_transaction_writes = 0
        self.auto_capture_result = AutoCaptureCommitResult(
            buffer_count=1,
            buffer_dir="frames/auto_event_001",
            committed=True,
            writer_ready=True,
        )
        self.auto_capture_decision_result = True
        self.last_auto_capture_kwargs = None

    def save_session_metadata(self, metadata: dict) -> None:
        self.saved_metadata = metadata

    def end_session(self) -> None:
        self.active = False
        self.ended = True

    def generate_temperature_plot(self, _metadata: dict) -> None:
        self.plot_generated = True

    def record_auto_capture_event(self, **kwargs) -> AutoCaptureCommitResult:
        self.auto_capture_transaction_writes += 1
        self.last_auto_capture_kwargs = kwargs
        return self.auto_capture_result

    def update_auto_capture_state(self, _event_idx: int, _state: str) -> bool:
        return self.auto_capture_decision_result


class _CloseEvent:
    def __init__(self):
        self.accepted = False
        self.ignored = False

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.ignored = True


class _AppHarness:
    """Only the attributes touched by ``_on_disarm``/``closeEvent``."""

    def __init__(self):
        self.camera_worker = object()
        self.pyrometer_worker = object()
        self.mistral_worker = object()
        self.evap_worker = object()
        self.classifier_worker = object()
        self._movie_worker = object()
        self.monitor = _Monitor()
        self._status_bar = _StatusBar()
        self._sensor_log_timer = _Timer()
        self._heartbeat_timer = _Timer()
        self.growth_log = _GrowthLog()
        self.auto_capture_engine = _AutoCaptureEngine()
        self._auto_capture_event_count = 0
        self._shutdown_pending = False
        self._camera_capture_interrupted = False
        self.rheed_intensity_window = _AuxiliaryWindow()
        self.pyrometer_window = _AuxiliaryWindow()
        self.stopped_workers: tuple[object, ...] | None = None
        self.stop_survivors: tuple[object, ...] = ()
        self.invalidations: list[str] = []
        self.auto_capture_state_at_worker_stop = None
        self._latest_classifier = None

    def _stop_workers(self, *workers) -> tuple[object, ...]:
        self.stopped_workers = workers
        self.auto_capture_state_at_worker_stop = (
            self._shutdown_pending,
            self.auto_capture_engine.enabled,
            self.auto_capture_engine.reset_count,
        )
        return self.stop_survivors

    def _stop_worker(self, worker) -> tuple[object, ...]:
        return self._stop_workers(worker)

    def statusBar(self) -> _StatusBar:
        return self._status_bar

    def _invalidate_equalizer_calibration(self, reason: str) -> bool:
        self.invalidations.append(reason)
        return True

    @staticmethod
    def _current_auto_capture_metadata() -> dict:
        return {"capture_sequence": 1}

    @staticmethod
    def _build_classifier_snapshot_payload(**_kwargs):
        return None


class StopWorkersTests(unittest.TestCase):
    def test_workers_share_one_declining_wait_deadline(self):
        clock = _FakeClock()
        events: list[tuple[str, str, int | None]] = []
        workers = [
            _FakeWorker("camera", clock, events, wait_cost_ms=400),
            _FakeWorker("sensor", clock, events, wait_cost_ms=400),
            _FakeWorker("classifier", clock, events, wait_cost_ms=0),
        ]

        with patch("gui.growth_app.time.monotonic", clock.monotonic):
            survivors = GrowthApp._stop_workers(*workers, timeout_ms=1000)

        # Every polling loop is signalled before the first potentially
        # blocking wait, so a slow worker cannot delay another's stop signal.
        self.assertEqual(
            events[:3],
            [
                ("camera", "stop", None),
                ("sensor", "stop", None),
                ("classifier", "stop", None),
            ],
        )
        self.assertTrue(all(event[1] == "wait" for event in events[3:]))

        budgets = [worker.wait_timeouts[0] for worker in workers]
        self.assertGreaterEqual(budgets[0], 999)
        self.assertLessEqual(budgets[0], 1000)
        self.assertGreaterEqual(budgets[1], 599)
        self.assertLessEqual(budgets[1], 600)
        self.assertGreaterEqual(budgets[2], 199)
        self.assertLessEqual(budgets[2], 200)
        self.assertGreater(budgets[0], budgets[1])
        self.assertGreater(budgets[1], budgets[2])
        self.assertEqual(survivors, ())

    def test_stop_workers_reports_worker_still_running_after_wait(self):
        clock = _FakeClock()
        events: list[tuple[str, str, int | None]] = []
        stopped = _FakeWorker("stopped", clock, events)
        stuck = _FakeWorker(
            "stuck",
            clock,
            events,
            wait_result=False,
        )

        with patch("gui.growth_app.time.monotonic", clock.monotonic):
            survivors = GrowthApp._stop_workers(
                stopped,
                stuck,
                timeout_ms=1000,
            )

        self.assertEqual(survivors, (stuck,))

    def test_disarm_includes_classifier_and_clears_worker_references(self):
        app = _AppHarness()
        original_classifier = app.classifier_worker

        GrowthApp._on_disarm(app)

        self.assertIsNotNone(app.stopped_workers)
        self.assertIn(original_classifier, app.stopped_workers)
        self.assertEqual(len(app.stopped_workers), 5)
        for name in (
            "camera_worker",
            "pyrometer_worker",
            "mistral_worker",
            "evap_worker",
            "classifier_worker",
        ):
            self.assertIsNone(getattr(app, name))
        self.assertTrue(app.monitor.reset_called)
        self.assertEqual(app.monitor.state, "idle")

    def test_disarm_retains_surviving_worker_and_does_not_claim_idle(self):
        app = _AppHarness()
        stuck_sensor = app.pyrometer_worker
        app.stop_survivors = (stuck_sensor,)

        GrowthApp._on_disarm(app)

        self.assertIs(app.pyrometer_worker, stuck_sensor)
        for name in (
            "camera_worker",
            "mistral_worker",
            "evap_worker",
            "classifier_worker",
        ):
            self.assertIsNone(getattr(app, name))
        self.assertTrue(app.monitor.reset_called)
        self.assertEqual(app.monitor.state, "armed")
        self.assertFalse(app.monitor.start_btn.enabled)
        self.assertTrue(app._status_bar.messages)
        self.assertIn("Disarm incomplete", app._status_bar.messages[-1])

    def test_close_includes_classifier_in_bounded_shutdown(self):
        app = _AppHarness()
        original_classifier = app.classifier_worker
        original_movie = app._movie_worker
        event = _CloseEvent()

        GrowthApp.closeEvent(app, event)

        self.assertTrue(app._sensor_log_timer.stopped)
        self.assertTrue(app._heartbeat_timer.stopped)
        self.assertIsNotNone(app.stopped_workers)
        self.assertIn(original_classifier, app.stopped_workers)
        self.assertIn(original_movie, app.stopped_workers)
        self.assertEqual(len(app.stopped_workers), 6)
        self.assertEqual(
            app.auto_capture_state_at_worker_stop,
            (True, False, 1),
        )
        self.assertTrue(app.rheed_intensity_window.closed)
        self.assertTrue(app.pyrometer_window.closed)
        self.assertTrue(event.accepted)
        self.assertFalse(event.ignored)

    def test_close_is_refused_while_any_worker_remains_running(self):
        app = _AppHarness()
        stuck_movie = app._movie_worker
        app.stop_survivors = (stuck_movie,)
        event = _CloseEvent()

        GrowthApp.closeEvent(app, event)

        self.assertFalse(event.accepted)
        self.assertTrue(event.ignored)
        self.assertTrue(app._sensor_log_timer.stopped)
        self.assertTrue(app._heartbeat_timer.stopped)
        self.assertEqual(app.monitor.state, "armed")
        self.assertFalse(app.monitor.start_btn.enabled)
        self.assertFalse(app.monitor.arm_btn.enabled)
        self.assertTrue(app.monitor._elapsed_timer.stopped)
        self.assertTrue(app._status_bar.messages)
        self.assertIn("Shutdown pending", app._status_bar.messages[-1])

    def test_rheed_reconnect_retains_old_worker_until_it_stops(self):
        app = _AppHarness()

        class RunningWorker:
            @staticmethod
            def isRunning() -> bool:
                return True

        old_worker = RunningWorker()
        app.camera_worker = old_worker
        app.classifier_worker = None
        app.stop_survivors = (old_worker,)

        GrowthApp._on_reconnect_rheed(app)

        self.assertIs(app.camera_worker, old_worker)
        self.assertEqual(
            app.monitor.reconnect_states,
            [(False, True), (True, False)],
        )
        self.assertIn("still stopping", app._status_bar.messages[-1])

    def test_refused_close_ends_active_session_in_shutdown_pending_state(self):
        app = _AppHarness()
        app.growth_log.active = True
        app.stop_survivors = (app._movie_worker,)
        event = _CloseEvent()

        GrowthApp.closeEvent(app, event)

        self.assertTrue(event.ignored)
        self.assertTrue(app.growth_log.ended)
        self.assertTrue(app.growth_log.plot_generated)
        self.assertEqual(app.invalidations, ["GUI closed"])
        self.assertTrue(app._shutdown_pending)
        self.assertFalse(app.auto_capture_engine.enabled)
        self.assertEqual(app.auto_capture_engine.reset_count, 1)
        self.assertEqual(app.monitor.state, "armed")
        self.assertFalse(app.monitor.start_btn.enabled)

    def test_queued_camera_and_capture_signals_are_ignored_after_refused_close(self):
        app = _AppHarness()
        app.growth_log.active = True
        app.stop_survivors = (app.camera_worker,)
        event = _CloseEvent()

        GrowthApp.closeEvent(app, event)
        GrowthApp._on_camera_state(app, object())
        GrowthApp._on_auto_capture_event(
            app,
            np.zeros((4, 4, 3), dtype=np.uint8),
            99.0,
        )

        self.assertTrue(event.ignored)
        self.assertEqual(app.auto_capture_engine.evaluations, [])
        self.assertEqual(app.monitor.camera_states, [])
        self.assertEqual(app.rheed_intensity_window.camera_states, [])
        self.assertEqual(app._auto_capture_event_count, 0)
        self.assertEqual(app.growth_log.auto_capture_transaction_writes, 0)

    def test_camera_and_capture_persistence_require_active_session(self):
        app = _AppHarness()
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        state = types.SimpleNamespace(
            connected=True,
            frame=frame,
            mode="dummy",
            error="",
        )

        GrowthApp._on_camera_state(app, state)
        GrowthApp._on_auto_capture_event(app, frame, 10.0)

        self.assertEqual(app.auto_capture_engine.evaluations, [])
        self.assertEqual(app.growth_log.auto_capture_transaction_writes, 0)

    def test_committed_event_with_failed_reopen_disables_auto_capture(self):
        app = _AppHarness()
        app.growth_log.active = True
        app.growth_log.auto_capture_result = AutoCaptureCommitResult(
            buffer_count=1,
            buffer_dir="frames/auto_event_001",
            committed=True,
            writer_ready=False,
        )

        GrowthApp._on_auto_capture_event(
            app,
            np.zeros((4, 4, 3), dtype=np.uint8),
            4.0,
        )

        self.assertEqual(app._auto_capture_event_count, 1)
        self.assertFalse(app.auto_capture_engine.enabled)
        self.assertIn("CSV writer unavailable", app.monitor.auto_capture_statuses[-1])
        self.assertIn("further capture is disabled", app._status_bar.messages[-1])

    def test_failed_auto_capture_decision_is_not_reported_as_saved(self):
        app = _AppHarness()
        app.growth_log.auto_capture_decision_result = False

        GrowthApp._on_auto_capture_decision(
            app,
            1,
            "frames/auto_event_001",
            "discarded",
        )

        self.assertIn("decision was not saved", app._status_bar.messages[-1])
        self.assertNotIn("marked discarded", app._status_bar.messages[-1])

    def test_close_hides_auxiliary_top_levels_before_main_exit(self):
        qt_app = QApplication.instance() or QApplication([])
        app = _AppHarness()
        rheed_window = QMainWindow()
        pyrometer_window = QMainWindow()
        app.rheed_intensity_window = rheed_window
        app.pyrometer_window = pyrometer_window
        rheed_window.show()
        pyrometer_window.show()
        qt_app.processEvents()
        self.assertTrue(rheed_window.isVisible())
        self.assertTrue(pyrometer_window.isVisible())

        event = _CloseEvent()
        GrowthApp.closeEvent(app, event)
        qt_app.processEvents()

        self.assertTrue(event.accepted)
        self.assertFalse(rheed_window.isVisible())
        self.assertFalse(pyrometer_window.isVisible())
        self.assertFalse(any(
            window.isVisible()
            for window in qt_app.topLevelWidgets()
            if window in (rheed_window, pyrometer_window)
        ))
        rheed_window.deleteLater()
        pyrometer_window.deleteLater()
        qt_app.processEvents()


if __name__ == "__main__":
    unittest.main()
