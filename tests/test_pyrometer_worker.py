"""Mac-side tests for gui.workers.PyrometerWorker + PyrometerState.has_valid_reading.

Regression guard for the P1 valid-reading fix (plan
fluffy-sleeping-babbage.md). Before the fix, ``PyrometerState`` defaulted
``temperature=0.0`` and ``PyrometerWorker.run`` emitted state after
``connect()`` succeeded but before the first ``read_temperature()`` —
downstream loggers persisted ``0.0`` as a real measurement.

The fix:
  * ``PyrometerState.temperature`` / ``temperature_std`` now default to
    ``None`` (Optional[float]); ``temperature_n`` defaults to ``0``.
  * ``has_valid_reading`` gates on ``connected AND valid AND temperature
    is not None AND math.isfinite(temperature)``.
  * Worker emits the post-connect state with the None sentinels; also
    resets to None when a poll batch fails entirely (previously the
    stale last-good value leaked into the next emission).

Run with ``python -m pytest -q tests/test_pyrometer_worker.py``.
No hardware, no serial, no drivers.pyrometer imports —
uses a FakeSensor injected via ``_create_sensor``.
"""
from __future__ import annotations

import copy
import sys
import time
import unittest
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# QCoreApplication is enough for signal + thread machinery (no widgets).
from PyQt6.QtCore import QCoreApplication, Qt  # noqa: E402

_app = QCoreApplication.instance() or QCoreApplication(sys.argv)

from gui.state import PyrometerState  # noqa: E402
from gui.workers import PyrometerWorker  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeSensor:
    """In-memory replacement for ModbusPyrometer/ScreenGrabPyrometer.

    Parameters
    ----------
    temps : list[float] | Callable[[int], float] | None
        Values returned by successive ``read_temperature()`` calls. If a
        callable, receives the 0-indexed call count. If None (default),
        every call raises RuntimeError — models a sensor that connects
        but can never read.
    connect_raises : bool
        If True, ``connect()`` itself raises.
    info : dict | None
        Optional dict returned by ``get_info()``. Absence of the
        attribute is one exercised path (mirrors sensors without info).
    """

    def __init__(
        self,
        temps: list | Callable[[int], float] | None = None,
        connect_raises: bool = False,
        info: dict | None = None,
    ):
        self._temps = temps
        self._connect_raises = connect_raises
        self._info = info
        self.connect_calls = 0
        self.read_calls = 0
        self.disconnect_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_raises:
            raise RuntimeError("fake sensor connect failed")

    def read_temperature(self) -> float:
        n = self.read_calls
        self.read_calls += 1
        if self._temps is None:
            raise RuntimeError("fake sensor: no readings configured (all fail)")
        if callable(self._temps):
            return float(self._temps(n))
        # List — cycle when exhausted (matches classifier worker's pattern)
        return float(self._temps[n % len(self._temps)])

    def disconnect(self) -> None:
        self.disconnect_calls += 1


class FakeSensorNoInfo(FakeSensor):
    """FakeSensor variant that lacks ``get_info`` entirely (attr check path)."""


class FakeSensorWithInfo(FakeSensor):
    """FakeSensor variant that has ``get_info`` returning a name+serial dict."""

    def get_info(self) -> dict:
        return self._info or {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll ``predicate`` until True or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _snapshot(state: PyrometerState) -> PyrometerState:
    """Copy the state at emit time — worker reuses one object across cycles."""
    return copy.copy(state)


def _make_worker(sensor: FakeSensor, poll_interval: float = 0.01,
                 samples_per_poll: int = 3) -> tuple[PyrometerWorker, list]:
    """Wire a worker with a fake sensor and a state-capture list.

    Returns (worker, states). states is appended to on every
    ``state_updated`` emission (snapshotted).
    """
    w = PyrometerWorker(
        mode="dummy",  # avoids drivers.pyrometer import (dummy factory branch)
        poll_interval=poll_interval,
        samples_per_poll=samples_per_poll,
    )
    w._create_sensor = lambda: sensor
    states: list[PyrometerState] = []
    w.state_updated.connect(
        lambda s: states.append(_snapshot(s)),
        Qt.ConnectionType.DirectConnection,
    )
    return w, states


# ---------------------------------------------------------------------------
# PyrometerState property tests — pure dataclass, no worker
# ---------------------------------------------------------------------------

class HasValidReadingTests(unittest.TestCase):
    """The ``has_valid_reading`` property is the load-bearing guard.

    Every downstream consumer (label render / CSV writer / trend curve
    append) gates on this predicate. If it drifts, the whole P1 fix
    regresses silently.
    """

    def test_default_state_is_not_valid(self):
        s = PyrometerState()
        self.assertFalse(s.has_valid_reading)

    def test_connected_alone_is_not_valid(self):
        """The pre-first-read window: connected, but no reading yet."""
        s = PyrometerState(connected=True)  # temperature stays None
        self.assertFalse(s.has_valid_reading)

    def test_temperature_present_but_not_connected_is_not_valid(self):
        """Sensor lost connection but old temperature lingers."""
        s = PyrometerState(connected=False, temperature=500.0)
        self.assertFalse(s.has_valid_reading)

    def test_connected_and_numeric_is_valid(self):
        s = PyrometerState(connected=True, valid=True, temperature=650.5)
        self.assertTrue(s.has_valid_reading)

    def test_explicit_invalid_rejects_retained_numeric_value(self):
        s = PyrometerState(connected=True, valid=False, temperature=650.5)
        self.assertFalse(s.has_valid_reading)

    def test_nan_temperature_is_not_valid(self):
        """A driver with dual-endian ambiguity can produce NaN.

        The predicate MUST reject NaN so downstream analyses that assume
        float arithmetic don't get poisoned. Codex round 4 regression
        guard.
        """
        s = PyrometerState(
            connected=True, valid=True, temperature=float("nan"),
        )
        self.assertFalse(s.has_valid_reading)

    def test_inf_temperature_is_not_valid(self):
        """Same rationale as NaN — inf is not a plausible temperature."""
        s = PyrometerState(
            connected=True, valid=True, temperature=float("inf"),
        )
        self.assertFalse(s.has_valid_reading)
        s2 = PyrometerState(
            connected=True, valid=True, temperature=float("-inf"),
        )
        self.assertFalse(s2.has_valid_reading)

    def test_negative_finite_still_valid(self):
        """The predicate is finiteness, not plausibility.

        Callers that need physical-plausibility filtering own that
        check themselves (matches ModbusPyrometer.TEMP_MIN_C bounds).
        The predicate only guarantees "a real number is here."
        """
        s = PyrometerState(connected=True, valid=True, temperature=-40.0)
        self.assertTrue(s.has_valid_reading)


# ---------------------------------------------------------------------------
# Worker lifecycle tests
# ---------------------------------------------------------------------------

class PyrometerWorkerRunTests(unittest.TestCase):
    """End-to-end worker tests via start() + stop() cycles."""

    def _stop_and_wait(self, w: PyrometerWorker) -> None:
        w.stop()
        self.assertTrue(w.wait(2000), "worker did not exit within 2s of stop()")

    def test_first_emitted_state_after_connect_has_temperature_none(self):
        """The pre-first-read emission must NOT carry a stale 0.0.

        Regression guard for the P1 bug: prior code emitted state right
        after connect() with the dataclass default temperature=0.0, and
        the sensor-log writer persisted that as a real reading. Fix
        emits with the None sentinels.
        """
        # Delay reads so we can catch the post-connect emission before
        # the first poll batch overwrites it.
        sensor = FakeSensor(temps=lambda n: (time.sleep(0.5), 500.0)[1])
        w, states = _make_worker(sensor, poll_interval=0.01)

        w.start()
        try:
            self.assertTrue(_wait_for(lambda: len(states) >= 1))
            first = states[0]
            self.assertTrue(first.connected)
            self.assertIsNone(first.temperature)
            self.assertIsNone(first.temperature_std)
            self.assertEqual(first.temperature_n, 0)
        finally:
            self._stop_and_wait(w)

    def test_first_emitted_state_after_connect_has_valid_reading_false(self):
        """The same first-emission state should be `has_valid_reading == False`.

        Downstream consumers gate on this predicate — this test locks
        in that they'll correctly treat post-connect state as "no
        reading yet" instead of persisting 0.0/None as a measurement.
        """
        sensor = FakeSensor(temps=lambda n: (time.sleep(0.5), 500.0)[1])
        w, states = _make_worker(sensor, poll_interval=0.01)

        w.start()
        try:
            self.assertTrue(_wait_for(lambda: len(states) >= 1))
            self.assertFalse(states[0].has_valid_reading)
        finally:
            self._stop_and_wait(w)

    def test_state_temperature_reset_to_none_when_batch_fails(self):
        """After a fully-failed poll batch, temperature MUST reset to None.

        Before the fix, an empty ``readings`` list meant the ``if
        readings:`` branch was skipped and ``state.temperature`` kept
        its previous value (initially 0.0, subsequently the last-good
        reading). Consumers would then get a fresh-looking timestamped
        emission with stale data.
        """
        sensor = FakeSensor(temps=None)  # every read_temperature raises
        w, states = _make_worker(sensor, poll_interval=0.01, samples_per_poll=3)

        w.start()
        try:
            # Wait for at least one post-poll emission (>= 2 total: post-connect
            # + post-batch).
            self.assertTrue(_wait_for(lambda: len(states) >= 2))
            post_batch = states[1]
            self.assertTrue(post_batch.connected)  # sensor object still open
            self.assertIsNone(post_batch.temperature)
            self.assertIsNone(post_batch.temperature_std)
            self.assertEqual(post_batch.temperature_n, 0)
            self.assertFalse(post_batch.has_valid_reading)
            self.assertIn("fake sensor", post_batch.error)
        finally:
            self._stop_and_wait(w)

    def test_temperature_populated_on_first_successful_batch(self):
        """Happy path — after a successful batch, temperature is the mean."""
        sensor = FakeSensor(temps=[500.0, 502.0, 498.0])
        w, states = _make_worker(sensor, poll_interval=0.01, samples_per_poll=3)

        w.start()
        try:
            self.assertTrue(_wait_for(
                lambda: any(s.has_valid_reading for s in states)
            ))
            valid = next(s for s in states if s.has_valid_reading)
            self.assertAlmostEqual(valid.temperature, 500.0, places=1)
            self.assertEqual(valid.temperature_n, 3)
            self.assertIsNotNone(valid.temperature_std)
            self.assertEqual(valid.error, "")
        finally:
            self._stop_and_wait(w)

    def test_nonfinite_batch_does_not_advance_success_sequence(self):
        """NaN/Inf are failed polls, not successful timing samples."""
        sensor = FakeSensor(
            temps=lambda n: 500.0 if n < 3 else float("nan"),
        )
        w, states = _make_worker(sensor, poll_interval=0.01, samples_per_poll=3)

        w.start()
        try:
            self.assertTrue(_wait_for(
                lambda: any("non-finite" in s.error for s in states),
            ))
            successful = next(s for s in states if s.has_valid_reading)
            failed = next(s for s in states if "non-finite" in s.error)
            self.assertEqual(successful.sample_sequence, 1)
            self.assertEqual(failed.sample_sequence, 1)
            self.assertEqual(
                failed.received_monotonic_ns,
                successful.received_monotonic_ns,
            )
            self.assertFalse(failed.valid)
            self.assertIsNone(failed.temperature)
        finally:
            self._stop_and_wait(w)

    def test_connect_failure_emits_disconnected_state_and_exits(self):
        """Failed connect(): one emission with connected=False + error message."""
        sensor = FakeSensor(connect_raises=True)
        w, states = _make_worker(sensor)

        w.start()
        self.assertTrue(w.wait(2000), "worker did not exit after connect failure")
        self.assertGreaterEqual(len(states), 1)
        last = states[-1]
        self.assertFalse(last.connected)
        self.assertIn("fake sensor connect failed", last.error)
        # And temperature stayed None throughout — the connect-failure
        # path never went through the poll batch.
        for s in states:
            self.assertIsNone(s.temperature)

    def test_recovery_from_failed_batch_repopulates_temperature(self):
        """After a failed batch resets to None, next good batch fills it back in.

        Guards against a bug where the reset was too aggressive (e.g.
        also clearing internal counters), preventing recovery.
        """
        call_count = {"n": 0}

        def temps(n):
            call_count["n"] = n
            # First 3 reads (first batch) all raise; subsequent reads succeed.
            if n < 3:
                raise RuntimeError("fake first-batch failure")
            return 600.0

        sensor = FakeSensor(temps=temps)
        w, states = _make_worker(sensor, poll_interval=0.01, samples_per_poll=3)

        w.start()
        try:
            # Wait for a valid-reading state to appear.
            self.assertTrue(_wait_for(
                lambda: any(s.has_valid_reading for s in states),
                timeout=3.0,
            ))
            # And confirm at least one prior state was invalid — recovery
            # implies we saw both states.
            self.assertTrue(any(not s.has_valid_reading for s in states))
            valid = [s for s in states if s.has_valid_reading][-1]
            self.assertAlmostEqual(valid.temperature, 600.0, places=1)
        finally:
            self._stop_and_wait(w)


if __name__ == "__main__":
    unittest.main(verbosity=2)
