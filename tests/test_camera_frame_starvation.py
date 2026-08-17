"""A camera that stops delivering must say so, not fail quiet.

``VmbCamera.read_frame`` raises ``FrameNotYetAvailableError`` for two very
different situations:

* an ordinary gap between SDK callbacks — routine, must stay silent;
* no first frame ever, or callbacks that stalled — a dead preview.

Retrying both forever leaves the GUI armed and silent while showing nothing.
Combined with a START button that could be clicked in that state, it let a
grower begin a growth with no live RHEED at all — the one camera failure that
cannot be recovered after the session.

SCOPE AT THIS COMMIT. These tests drive the real ``RheedCameraWorker.run``
against a **stubbed exception seam**: the stub camera raises
``FrameNotYetAvailableError`` on demand, so both the never-delivered and the
went-quiet branches of the deadline are exercised here. Through the REAL
driver, only first-frame starvation is reachable — without sequence
bookkeeping ``read_frame`` keeps returning the cached frame after the first
callback and never raises again. The guard that makes a genuine mid-arm Vimba
stall observable arrives in the following commit.

A fake clock advances only when the worker sleeps, so the deadline logic is
exercised without waiting in real time.

Run:
    python -m pytest -q tests/test_camera_frame_starvation.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import threading

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from drivers.rheed_camera import FrameNotYetAvailableError  # noqa: E402
from gui.workers import RheedCameraWorker  # noqa: E402


def _frame() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


class _StubCamera:
    """Yields a scripted sequence; ``None`` means "no frame yet"."""

    def __init__(self, script):
        self._script = list(script)
        self.exposure_us = None

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def read_frame(self):
        item = self._script.pop(0) if self._script else None
        if item is None:
            raise FrameNotYetAvailableError("no new frame since previous read")
        return item


class _Clock:
    """Monotonic clock that only advances when the worker sleeps."""

    def __init__(self, step: float):
        self.now = 1000.0
        self.step = step

    def monotonic(self) -> float:
        return self.now


def _run_worker(script, *, trigger_hz=1.0, max_iterations=40, sleep_step=1.0):
    """Run the real worker loop against a stub camera and a fake clock."""
    worker = RheedCameraWorker(mode="vimba", poll_interval=0.0,
                               trigger_hz=trigger_hz)
    camera = _StubCamera(script)
    worker._create_camera = lambda: camera  # type: ignore[method-assign]

    emitted = []
    worker.state_updated = type("_Sig", (), {  # minimal signal stand-in
        "emit": staticmethod(lambda s: emitted.append(s)),
    })()

    clock = _Clock(sleep_step)
    iterations = {"n": 0}

    def fake_sleep(_seconds):
        clock.now += clock.step
        iterations["n"] += 1
        if iterations["n"] >= max_iterations:
            worker.running = False

    import gui.workers as workers_mod
    real_sleep, real_monotonic = workers_mod.time.sleep, workers_mod.time.monotonic
    workers_mod.time.sleep = fake_sleep
    workers_mod.time.monotonic = clock.monotonic
    try:
        worker.run()
    finally:
        workers_mod.time.sleep = real_sleep
        workers_mod.time.monotonic = real_monotonic
    return emitted


class FrameStarvationTests(unittest.TestCase):

    def test_ordinary_gap_between_callbacks_stays_silent(self):
        """A couple of empty polls inside the deadline must not report an error."""
        # 1 Hz -> 5 s deadline (the floor). Two 1 s gaps, then a frame.
        emitted = _run_worker(
            [None, None, _frame()], max_iterations=4, sleep_step=1.0,
        )
        errors = [s for s in emitted if s.error]
        self.assertEqual(
            errors, [], f"an ordinary callback gap reported an error: {errors}",
        )
        self.assertTrue(
            any(s.valid for s in emitted), "the real frame was never emitted",
        )

    def test_no_first_frame_ever_is_reported_once(self):
        """A camera that connects but never delivers must be surfaced."""
        emitted = _run_worker([], max_iterations=20, sleep_step=1.0)
        errors = [s for s in emitted if s.error]
        self.assertTrue(
            errors, "a camera that never delivered a frame stayed silent",
        )
        self.assertEqual(
            len(errors), 1,
            "starvation was reported repeatedly instead of once",
        )
        self.assertFalse(errors[0].connected)
        self.assertIsNone(errors[0].frame)
        self.assertIn("first", errors[0].error.lower())

    def test_stall_after_a_good_frame_is_reported(self):
        """Delivery that stops mid-session must not leave a stale preview."""
        emitted = _run_worker(
            [_frame()] + [None] * 30, max_iterations=20, sleep_step=1.0,
        )
        self.assertTrue(any(s.valid for s in emitted), "no good frame emitted")
        errors = [s for s in emitted if s.error]
        self.assertTrue(errors, "a post-frame stall was never reported")
        self.assertIn("new", errors[0].error.lower())
        self.assertFalse(errors[0].connected)

    def test_recovery_clears_the_error_and_rearms_reporting(self):
        """A camera that comes back is announced healthy and can fail again."""
        script = [_frame()] + [None] * 12 + [_frame()] + [None] * 12
        emitted = _run_worker(script, max_iterations=30, sleep_step=1.0)
        errors = [s for s in emitted if s.error]
        self.assertGreaterEqual(
            len(errors), 2,
            "a second stall after recovery was swallowed by the first report",
        )
        valid_after_error = False
        seen_error = False
        for s in emitted:
            if s.error:
                seen_error = True
            elif seen_error and s.valid:
                valid_after_error = True
        self.assertTrue(
            valid_after_error, "recovery never produced a valid state again",
        )

    def test_deadline_scales_with_the_trigger_period(self):
        """Three trigger periods, floored — not a hardcoded constant."""
        slow = RheedCameraWorker(mode="vimba", trigger_hz=0.1)
        fast = RheedCameraWorker(mode="vimba", trigger_hz=100.0)
        self.assertAlmostEqual(slow._frame_deadline_s(), 30.0)
        self.assertAlmostEqual(
            fast._frame_deadline_s(), RheedCameraWorker.MIN_FRAME_DEADLINE_S,
            msg="a fast trigger must not shrink the deadline below the floor",
        )


class _MonitorWithCamera:
    def __init__(self, latest):
        self._latest_camera = latest


class _App:
    """Enough of GrowthApp for _has_live_camera_frame."""

    def __init__(self, latest):
        self.monitor = _MonitorWithCamera(latest)


class StartGateTests(unittest.TestCase):
    """START must require a real frame, not merely the armed UI state."""

    @staticmethod
    def _check(latest) -> bool:
        from gui.growth_app import GrowthApp
        return GrowthApp._has_live_camera_frame(_App(latest))

    def test_no_state_at_all_blocks_start(self):
        self.assertFalse(self._check(None))

    def test_connected_but_no_frame_blocks_start(self):
        """The exact hole: armed, connected, and nothing ever delivered."""
        from gui.state import CameraState
        self.assertFalse(self._check(
            CameraState(connected=True, valid=False, frame=None),
        ))

    def test_stale_disconnected_state_blocks_start(self):
        """After the starvation deadline clears connected, START stays shut."""
        from gui.state import CameraState
        self.assertFalse(self._check(CameraState(
            connected=False, valid=False, frame=None,
            error="No new RHEED frame in 6s.",
        )))

    def test_live_valid_frame_permits_start(self):
        from gui.state import CameraState
        self.assertTrue(self._check(
            CameraState(connected=True, valid=True, frame=_frame()),
        ))


class StartButtonGateTests(unittest.TestCase):
    """The button itself must be gated, not just the slot that handles it.

    A slot-side rejection was too late: GrowthMonitor started the elapsed
    timer and stamped _start_time on the click, then GrowthApp returned
    without unwinding either. The session clock was left running behind an
    "armed" UI, so a later successful start inherited an age predating it.
    """

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _monitor(self):
        from gui.growth_monitor import GrowthMonitor
        return GrowthMonitor()

    @staticmethod
    def _live_state():
        from gui.state import CameraState
        return CameraState(connected=True, valid=True, frame=_frame())

    @staticmethod
    def _dead_state(error="No new RHEED frame in 6s."):
        from gui.state import CameraState
        return CameraState(connected=False, valid=False, frame=None,
                           error=error)

    def test_arm_without_a_frame_leaves_start_disabled(self):
        m = self._monitor()
        m.set_state("armed")
        self.assertFalse(
            m.start_btn.isEnabled(),
            "START was clickable after ARM with no camera frame",
        )

    def test_first_live_frame_enables_start(self):
        m = self._monitor()
        m.set_state("armed")
        m.update_camera_state(self._live_state())
        self.assertTrue(m.start_btn.isEnabled())

    def test_camera_loss_disables_start_again(self):
        m = self._monitor()
        m.set_state("armed")
        m.update_camera_state(self._live_state())
        m.update_camera_state(self._dead_state())
        self.assertFalse(
            m.start_btn.isEnabled(),
            "START stayed enabled after the camera stopped delivering",
        )

    def test_recovery_re_enables_start(self):
        m = self._monitor()
        m.set_state("armed")
        m.update_camera_state(self._dead_state())
        m.update_camera_state(self._live_state())
        self.assertTrue(m.start_btn.isEnabled())

    def test_rejected_start_click_does_not_start_the_clock(self):
        """The exact regression: a refused START must leave no clock behind."""
        m = self._monitor()
        m.set_state("armed")  # no frame -> request must not even be emitted
        emitted = []
        m.start_requested.connect(lambda: emitted.append(True))
        m._on_start_clicked()
        self.assertEqual(emitted, [], "a startable request was emitted")
        self.assertIsNone(m._start_time, "_start_time was populated")
        self.assertFalse(
            m._elapsed_timer.isActive(), "the elapsed timer was started",
        )

    def test_successful_start_begins_the_clock_exactly_once(self):
        m = self._monitor()
        m.set_state("armed")
        m.update_camera_state(self._live_state())
        emitted = []
        m.start_requested.connect(lambda: emitted.append(True))
        m._on_start_clicked()
        self.assertEqual(len(emitted), 1)
        # Still not running: GrowthApp has not accepted it yet.
        self.assertIsNone(m._start_time)
        self.assertFalse(m._elapsed_timer.isActive())

        m.set_state("running")          # GrowthApp accepted
        self.assertIsNotNone(m._start_time)
        self.assertTrue(m._elapsed_timer.isActive())

        first = m._start_time
        m.set_state("running")          # redundant re-entry
        self.assertEqual(
            m._start_time, first,
            "a repeated set_state('running') restarted the session clock",
        )


if __name__ == "__main__":
    unittest.main()


class CameraTransactionTests(unittest.TestCase):
    """ARM is one hardware transaction: it commits whole or rolls back whole.

    Access mode, exposure, four trigger features and start_streaming were
    previously independent steps, so a failure late in the sequence left
    earlier mutations applied — a camera sitting at a requested exposure
    behind an ARM the GUI had reported as failed.
    """

    def setUp(self):
        import test_vimba_camera as T
        self.T = T
        self.addCleanup(T.uninstall_fake_vmbpy)

    def _cam(self, stub, **kw):
        self.T.install_fake_vmbpy([stub])
        from drivers.rheed_camera import VmbCamera
        params = dict(trigger_hz=1.0, access_mode="full", exposure_us=250_000.0)
        params.update(kw)
        cam = VmbCamera(**params)
        cam.CONNECT_TIMEOUT_S = 0.4
        cam.DISCONNECT_TIMEOUT_S = 0.2
        return cam

    def test_worker_stop_cancels_a_blocked_connect(self):
        """DISARM during a blocked connect must stop setup, not just the loop.

        RheedCameraWorker.stop() only cleared its own `running` flag; a worker
        blocked inside connect() never reached the check, so the driver's stop
        event stayed unset and setup carried on writing.
        """
        from gui.workers import RheedCameraWorker
        stub = self.T.FakeCamera()
        feature = self.T.BlockingExposureFeature(
            initial_value=300_000.0, value_range=(100.0, 900_000.0),
        )
        stub.ExposureTimeAbs = feature
        cam = self._cam(stub)

        worker = RheedCameraWorker(mode="vimba", poll_interval=0.01)
        worker._camera = cam
        worker._create_camera = lambda: cam

        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        self.assertTrue(feature.entered.wait(timeout=5.0), "setup never blocked")

        worker.stop()                      # the grower presses DISARM
        feature.release.set()              # the SDK call finally returns
        t.join(timeout=10.0)

        self.assertFalse(t.is_alive(), "worker did not exit after stop()")
        self.assertEqual(
            feature.set_call_count, 0,
            "exposure was written after DISARM cancelled the connect",
        )
        self.assertEqual(
            stub.start_streaming_calls, 0,
            "streaming started after DISARM cancelled the connect",
        )

    def test_blocked_trigger_setter_is_rolled_back(self):
        """A trigger setter that applies after cancellation must be undone."""
        stub = self.T.FakeCamera()
        blocking = self.T.BlockingExposureFeature(block_on="set")
        blocking.value = "Line0"
        stub.TriggerSource = blocking
        cam = self._cam(stub)
        try:
            cam.connect()
        except Exception:
            pass
        cam.disconnect()
        blocking.release.set()
        if cam._stream_thread is not None:
            cam._stream_thread.join(timeout=5.0)
        self.assertEqual(
            blocking.value, "Line0",
            "a trigger write that landed after cancellation was not rolled back",
        )

    def test_streaming_failure_rolls_the_exposure_back(self):
        """Exposure stays provisional until streaming commits."""
        stub = self.T.FakeCamera()

        def boom(_handler):
            raise RuntimeError("stream refused")
        stub.start_streaming = boom
        cam = self._cam(stub)
        raised = None
        try:
            cam.connect()
        except Exception as exc:
            raised = exc
        cam.disconnect()
        self.assertIsNotNone(raised)
        self.assertEqual(
            stub.ExposureTimeAbs.value, 300_000.0,
            "start_streaming failed but the camera was left at the requested "
            "exposure — the ARM was not atomic",
        )

    def test_a_second_instance_cannot_take_the_same_camera(self):
        """The guard must span instances: the GUI discards failed drivers."""
        stub = self.T.FakeCamera()
        feature = self.T.BlockingExposureFeature(
            initial_value=300_000.0, value_range=(100.0, 900_000.0),
        )
        stub.ExposureTimeAbs = feature
        first = self._cam(stub)
        try:
            first.connect()
        except Exception:
            pass
        first.disconnect()

        from drivers.rheed_camera import VmbCamera
        second = VmbCamera(
            camera_index=0, trigger_hz=1.0, access_mode="full",
            exposure_us=250_000.0,
        )
        raised = None
        try:
            second.connect()
        except Exception as exc:
            raised = exc
        self.assertIsNotNone(
            raised,
            "a second VmbCamera connected to the same camera while the first "
            "instance's setup thread was still alive",
        )
        self.assertIn("already held", str(raised))

        feature.release.set()
        if first._stream_thread is not None:
            first._stream_thread.join(timeout=5.0)

    def test_lease_is_released_on_every_exit_path(self):
        """A lease outliving its thread would lock the camera out forever."""
        from drivers.rheed_camera import _CAMERA_LEASES
        stub = self.T.FakeCamera()
        stub.ExposureTimeAbs = self.T.FakeSettable(
            initial_value=300_000.0, value_range=(100.0, 900_000.0),
        )
        cam = self._cam(stub)
        cam.connect()
        cam.disconnect()
        if cam._stream_thread is not None:
            cam._stream_thread.join(timeout=5.0)
        self.assertEqual(
            _CAMERA_LEASES, {}, "lease survived a clean connect/disconnect",
        )


class LabSafetyRegressionTests(unittest.TestCase):
    """The five demonstrated paths that could make a lab run unsafe.

    Each corresponds to a failure where the GUI would report ARM failed or
    disarmed while the camera stayed modified, streaming, or unrecoverable.
    """

    def setUp(self):
        import test_vimba_camera as T
        self.T = T
        self.addCleanup(T.uninstall_fake_vmbpy)

    def _cam(self, stub, **kw):
        self.T.install_fake_vmbpy([stub])
        from drivers.rheed_camera import VmbCamera
        params = dict(trigger_hz=1.0, access_mode="full", exposure_us=250_000.0)
        params.update(kw)
        cam = VmbCamera(**params)
        cam.CONNECT_TIMEOUT_S = 0.4
        cam.DISCONNECT_TIMEOUT_S = 0.2
        return cam

    def test_rollback_runs_before_the_camera_context_closes(self):
        """Restoring after __exit__ writes to a closed camera and does nothing."""
        exits = []

        # Subclass, not an instance attribute: Python resolves dunder methods
        # on the TYPE, so assigning stub.__exit__ is never consulted.
        class _RecordingCamera(self.T.FakeCamera):
            def __exit__(self, *a):
                exits.append(self.ExposureTimeAbs.value)
                return super().__exit__(*a)

        stub = _RecordingCamera()

        def boom(_handler):
            raise RuntimeError("stream refused")
        stub.start_streaming = boom

        cam = self._cam(stub)
        try:
            cam.connect()
        except Exception:
            pass
        cam.disconnect()
        self.assertTrue(exits, "the camera context never closed")
        self.assertEqual(
            exits[0], 300_000.0,
            "the exposure was still the requested value when the camera "
            "context closed — rollback ran too late to reach the hardware",
        )

    def test_unreadable_trigger_original_refuses_the_mutation(self):
        """No readable original means no possible rollback, so do not write."""
        stub = self.T.FakeCamera()
        stub.TriggerSource = self.T.FakeSettable()
        stub.TriggerSource.get = None          # present but cannot answer
        cam = self._cam(stub)
        raised = None
        try:
            cam.connect()
        except Exception as exc:
            raised = exc
        cam.disconnect()
        self.assertIsNotNone(raised, "mutated a feature it could not restore")
        self.assertEqual(
            stub.TriggerSource.set_call_count, 0,
            "wrote a trigger feature whose original value was unreadable",
        )

    def test_request_stop_and_commit_are_serialised(self):
        """Cancellation and commit must not interleave."""
        from drivers.rheed_camera import VmbCamera
        cam = VmbCamera(trigger_hz=1.0)
        held = threading.Event()
        released = threading.Event()

        def hold():
            with cam._lifecycle_lock:
                held.set()
                released.wait(timeout=5.0)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        self.assertTrue(held.wait(timeout=5.0))

        done = threading.Event()
        threading.Thread(
            target=lambda: (cam.request_stop(), done.set()), daemon=True,
        ).start()
        self.assertFalse(
            done.wait(timeout=0.3),
            "request_stop() did not take the lifecycle lock, so cancellation "
            "can interleave with the commit decision",
        )
        released.set()
        self.assertTrue(done.wait(timeout=5.0))
        t.join(timeout=5.0)

    def test_worker_stop_before_camera_assignment_is_not_lost(self):
        """DISARM between _create_camera() and connect() must still cancel."""
        from gui.workers import RheedCameraWorker
        stub = self.T.FakeCamera()
        cam = self._cam(stub)
        worker = RheedCameraWorker(mode="vimba", poll_interval=0.01)

        def create_then_disarm():
            worker.stop()          # the grower disarms during construction
            return cam
        worker._create_camera = create_then_disarm

        emitted = []
        worker.state_updated = type("_Sig", (), {
            "emit": staticmethod(lambda s: emitted.append(s)),
        })()
        worker.run()

        self.assertEqual(
            stub.ExposureTimeAbs.set_call_count, 0,
            "exposure was written after a DISARM that landed before the "
            "camera was assigned",
        )
        self.assertEqual(stub.start_streaming_calls, 0)

    def test_unstoppable_stream_blocks_the_next_arm(self):
        """An unknown hardware state must not be re-armed automatically."""
        stub = self.T.FakeCamera()

        def wont_stop():
            raise RuntimeError("stop_streaming refused")
        stub.stop_streaming = wont_stop

        cam = self._cam(stub)
        try:
            cam.connect()
        except Exception:
            pass
        cam.disconnect()
        if cam._stream_thread is not None:
            cam._stream_thread.join(timeout=5.0)

        self.assertIsNotNone(
            cam._unknown_hardware_state,
            "a stream that could not be stopped was treated as a clean stop",
        )
        raised = None
        try:
            cam.connect()
        except Exception as exc:
            raised = exc
        self.assertIsNotNone(raised, "re-armed a camera in an unknown state")
        self.assertIn("power-cycle", str(raised).lower())
