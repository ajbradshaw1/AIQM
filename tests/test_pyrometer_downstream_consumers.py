"""Regression tests for downstream consumers of PyrometerState.

The P1 fix (plan fluffy-sleeping-babbage.md) added ``PyrometerState.has_valid_reading``
and made ``temperature`` an ``Optional[float]`` that defaults to ``None``.
Every consumer that previously read ``pyro.temperature`` under a
``connected``-only guard has been rerouted through ``has_valid_reading``,
so the None-sentinel doesn't crash the render path AND the previously-
silent ``0.0`` doesn't leak into logs.

This suite regression-guards the load-bearing consumer sites:

  * ``GrowthLogger.log_sensors`` — the periodic CSV writer. When
    ``pyro_temp=None`` is passed, the pyrometer column MUST be an empty
    string (not ``"0.0"`` and not ``"nan"``).
  * ``GrowthLogger.log_heartbeat`` — same rule for the heartbeat CSV.
    Codex round 4 correction: heartbeat frame capture MUST NOT depend
    on pyrometer validity. We assert both (a) the frame IS logged and
    (b) the pyrometer column is empty when the state is invalid.
  * ``GrowthLogger.log_auto_capture_event`` — same rule; event fires
    regardless of pyrometer health.
  * The commit-event / manual-event temp-string rendering pattern
    (``f"{state.temperature:.1f}" if state.has_valid_reading else ""``) —
    tested as a pure predicate against ``PyrometerState`` fixtures to
    avoid spinning up the whole GrowthMonitor GUI.

These are integration-lite tests. GrowthLogger is instantiated against a
temp directory; no PyQt6/GUI harness required.
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gui.growth_logger import GrowthLogger  # noqa: E402
from gui.state import PyrometerState  # noqa: E402


def _fresh_logger(tmp_path: Path) -> GrowthLogger:
    """Start a session in ``tmp_path`` so the per-CSV writers are open."""
    logger = GrowthLogger(base_dir=str(tmp_path))
    logger.start_session(sample_id="p1_regression_test")
    return logger


def _read_sensor_csv(session_dir: Path) -> list[dict]:
    csv_path = session_dir / "sensor_log.csv"
    with csv_path.open() as f:
        return list(csv.DictReader(f))


def _read_heartbeat_csv(session_dir: Path) -> list[dict]:
    csv_path = session_dir / "heartbeat_log.csv"
    with csv_path.open() as f:
        return list(csv.DictReader(f))


def _read_auto_event_csv(session_dir: Path) -> list[dict]:
    csv_path = session_dir / "auto_capture_events.csv"
    with csv_path.open() as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# GrowthLogger.log_sensors — CSV column must be empty for None
# ---------------------------------------------------------------------------

class LogSensorsPyroTempTests(unittest.TestCase):

    def test_none_pyro_temp_writes_empty_cell(self):
        """The P1 bug: 0.0 leaked into sensor_log; fix routes None → ''."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = _fresh_logger(Path(tmp))
            try:
                logger.log_sensors(pyro_temp=None, elapsed_s=1.0)
                rows = _read_sensor_csv(Path(logger.session_dir))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["pyrometer_temp_C"], "")
                self.assertNotEqual(rows[0]["pyrometer_temp_C"], "0.0")
                self.assertNotEqual(rows[0]["pyrometer_temp_C"], "nan")
            finally:
                logger.end_session()

    def test_numeric_pyro_temp_writes_formatted_value(self):
        """Happy path — a real reading rounds to 1 decimal place."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = _fresh_logger(Path(tmp))
            try:
                logger.log_sensors(pyro_temp=652.34, elapsed_s=1.0)
                rows = _read_sensor_csv(Path(logger.session_dir))
                self.assertEqual(rows[0]["pyrometer_temp_C"], "652.3")
            finally:
                logger.end_session()

    def test_none_pyro_temp_std_writes_empty_cell(self):
        """temperature_std must ALSO handle None — same code path as temp."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = _fresh_logger(Path(tmp))
            try:
                logger.log_sensors(
                    pyro_temp=None, elapsed_s=1.0,
                    pyro_temp_std=None, pyro_temp_n=None,
                )
                rows = _read_sensor_csv(Path(logger.session_dir))
                self.assertEqual(rows[0]["pyrometer_temp_std_C"], "")
                self.assertEqual(rows[0]["pyrometer_temp_n"], "")
            finally:
                logger.end_session()


# ---------------------------------------------------------------------------
# GrowthLogger.log_heartbeat — frame path recorded, temp blank when None
# ---------------------------------------------------------------------------

class LogHeartbeatDecouplingTests(unittest.TestCase):
    """Codex round 4: heartbeat frame capture MUST NOT depend on pyro health.

    A pyrometer in the pre-first-read window or a failed-batch window
    should NOT prevent the camera from being anchored via heartbeat.
    The frame gets saved; only the pyrometer column is blank.
    """

    def test_heartbeat_records_frame_path_when_pyro_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = _fresh_logger(Path(tmp))
            try:
                logger.log_heartbeat(
                    elapsed_s=5.0,
                    pyro_temp=None,
                    frame_path="frames/heartbeat_001.png",
                )
                rows = _read_heartbeat_csv(Path(logger.session_dir))
                self.assertEqual(len(rows), 1)
                # Frame path MUST be present — this is the load-bearing
                # assertion for the "capture doesn't depend on pyro" invariant.
                self.assertEqual(rows[0]["frame_path"], "frames/heartbeat_001.png")
                # And the pyrometer column MUST be blank.
                self.assertEqual(rows[0]["pyrometer_temp_C"], "")

            finally:
                logger.end_session()

    def test_heartbeat_records_frame_and_pyro_when_both_present(self):
        """Regression guard the other way — happy path still works."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = _fresh_logger(Path(tmp))
            try:
                logger.log_heartbeat(
                    elapsed_s=5.0,
                    pyro_temp=723.5,
                    frame_path="frames/heartbeat_002.png",
                )
                rows = _read_heartbeat_csv(Path(logger.session_dir))
                self.assertEqual(rows[0]["frame_path"], "frames/heartbeat_002.png")
                self.assertEqual(rows[0]["pyrometer_temp_C"], "723.5")
            finally:
                logger.end_session()


# ---------------------------------------------------------------------------
# GrowthLogger.log_auto_capture_event — same decoupling rule
# ---------------------------------------------------------------------------

class LogAutoCaptureEventTests(unittest.TestCase):

    def test_event_fires_with_blank_pyro_when_reading_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = _fresh_logger(Path(tmp))
            try:
                self.assertTrue(logger.log_auto_capture_event(
                    event_idx=1,
                    score=42.5,
                    elapsed_s=10.0,
                    pyro_temp=None,
                    event_state="pending",
                ))
                rows = _read_auto_event_csv(Path(logger.session_dir))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["pyrometer_temp_C"], "")
                # Event still records score/state with blank buffer provenance.
                # (change_score is formatted to 4 decimals per growth_logger.py.)
                self.assertEqual(rows[0]["change_score"], "42.5000")
                self.assertEqual(rows[0]["buffer_count"], "0")
                self.assertEqual(rows[0]["buffer_dir"], "")
                self.assertEqual(rows[0]["event_state"], "pending")
            finally:
                logger.end_session()


# ---------------------------------------------------------------------------
# Commit/manual event temp-string rendering — pure predicate against state
# ---------------------------------------------------------------------------

class CommitAndManualEventTempStringTests(unittest.TestCase):
    """The pattern used in gui/growth_monitor.py:

        temp_str = (
            f"{self._latest_pyro.temperature:.1f}"
            if self._latest_pyro and self._latest_pyro.has_valid_reading
            else ""
        )

    Testing the predicate directly (against PyrometerState fixtures)
    avoids the full-GUI harness while locking in the render invariants.
    Any drift here will surface as bad UX (0.00 °C on labels that should
    read blank).
    """

    @staticmethod
    def _render_temp_str(pyro: PyrometerState | None) -> str:
        """Exact copy of the render pattern from growth_monitor.py."""
        if pyro is not None and pyro.has_valid_reading:
            return f"{pyro.temperature:.1f}"
        return ""

    def test_none_pyro_renders_empty_string(self):
        self.assertEqual(self._render_temp_str(None), "")

    def test_disconnected_pyro_renders_empty_string(self):
        self.assertEqual(
            self._render_temp_str(PyrometerState(connected=False)),
            "",
        )

    def test_connected_no_reading_renders_empty_string(self):
        """The load-bearing test — this is the exact state the pre-fix
        bug turned into '0.0'. After the fix it MUST render blank."""
        pyro = PyrometerState(connected=True, temperature=None)
        self.assertEqual(self._render_temp_str(pyro), "")

    def test_connected_and_reading_renders_formatted_value(self):
        pyro = PyrometerState(
            connected=True, valid=True, temperature=712.85,
        )
        self.assertEqual(self._render_temp_str(pyro), "712.9")

    def test_nan_reading_renders_empty_string(self):
        """NaN is rejected by has_valid_reading → renders blank, not 'nan'."""
        pyro = PyrometerState(
            connected=True, valid=True, temperature=float("nan"),
        )
        self.assertEqual(self._render_temp_str(pyro), "")


# ---------------------------------------------------------------------------
# _log_sensors extraction pattern — the actual growth_app.py logic
# ---------------------------------------------------------------------------

class LogSensorsExtractionPatternTests(unittest.TestCase):
    """Mirror the extraction pattern from gui/growth_app.py:_log_sensors:

        pyro_ok = pyro is not None and pyro.has_valid_reading
        pyro_temp = pyro.temperature if pyro_ok else None
        pyro_temp_std = pyro.temperature_std if pyro_ok else None
        pyro_temp_n = pyro.temperature_n if pyro_ok else None

    This is the pattern that used to be gated on `connected` alone and
    leaked 0.0 into every sensor_log row for the pre-first-read window.
    Testing the extraction directly regression-guards the invariant.
    """

    @staticmethod
    def _extract(pyro):
        pyro_ok = pyro is not None and pyro.has_valid_reading
        return {
            "pyro_temp": pyro.temperature if pyro_ok else None,
            "pyro_temp_std": pyro.temperature_std if pyro_ok else None,
            "pyro_temp_n": pyro.temperature_n if pyro_ok else None,
        }

    def test_none_state_yields_all_none(self):
        r = self._extract(None)
        self.assertIsNone(r["pyro_temp"])
        self.assertIsNone(r["pyro_temp_std"])
        self.assertIsNone(r["pyro_temp_n"])

    def test_connected_no_reading_yields_all_none(self):
        """The regression case — connected=True but never read."""
        r = self._extract(PyrometerState(connected=True))
        self.assertIsNone(r["pyro_temp"])
        self.assertIsNone(r["pyro_temp_std"])
        self.assertIsNone(r["pyro_temp_n"])

    def test_valid_reading_yields_all_populated(self):
        pyro = PyrometerState(
            connected=True,
            valid=True,
            temperature=612.0,
            temperature_std=1.5,
            temperature_n=5,
        )
        r = self._extract(pyro)
        self.assertEqual(r["pyro_temp"], 612.0)
        self.assertEqual(r["pyro_temp_std"], 1.5)
        self.assertEqual(r["pyro_temp_n"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
