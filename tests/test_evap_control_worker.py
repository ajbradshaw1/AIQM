"""Tests for EvapControlWorker mode routing + Direct-read tab UI display.

Worker: verifies mode="screengrab"|"elog"|"dummy" routes to the correct
driver class in ``_create_driver``. This is the contract that decides
whether elog direct-read even gets a chance to connect — a bug here
silently reverts to screengrab or dummy without any visible signal.

Direct-read tab: verifies the Jul 9 2026 UI addition — new tab shows
all 10 .elo-sourced fields, plasma section auto-hides when the plasma
source is off (all-None state from ``ElogReader``'s NaN-filter), and
``reset_displays`` clears everything back to ``"---"`` on disarm.

Runs headless via ``QT_QPA_PLATFORM=offscreen``.
Run: ``python -m pytest -q tests/test_evap_control_worker.py``.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Must set BEFORE any Qt import — headless CI/local runs on Mac + Linux.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

from drivers.evap_control import (  # noqa: E402
    DummyEvapControl, ElogReader, EvapControl,
)
from gui.growth_monitor import GrowthMonitor  # noqa: E402
from gui.state import EvapControlState  # noqa: E402
from gui.workers import EvapControlWorker  # noqa: E402


def _make_evap_state(
    connected: bool = True,
    mode: str = "elog",
    chamber_pressure_mbar: float | None = 3.2e-9,
    substrate_temp_pv_C: float | None = 720.5,
    substrate_temp_setpoint_C: float | None = 725.0,
    cell_HTEC2_pv_C: float | None = 425.0,
    cell_Y_pv_C: float | None = 632.1,
    cell_Sr_pv_C: float | None = 550.3,
    cell_Eu_pv_C: float | None = 580.7,
    cell_Er_pv_C: float | None = 610.2,
    plasma_dc_bias_V: float | None = None,
    plasma_forward_W: float | None = None,
    plasma_reflected_W: float | None = None,
) -> EvapControlState:
    """Build an EvapControlState for tests. Defaults model a live
    elog-mode session with the plasma source OFF (all plasma_* None —
    matches ``ElogReader``'s NaN-filter behavior when plasma is off)."""
    return EvapControlState(
        mode=mode,
        connected=connected,
        valid=connected,
        error="",
        chamber_pressure_mbar=chamber_pressure_mbar,
        substrate_temp_pv_C=substrate_temp_pv_C,
        substrate_temp_setpoint_C=substrate_temp_setpoint_C,
        cell_HTEC2_pv_C=cell_HTEC2_pv_C,
        cell_Y_pv_C=cell_Y_pv_C,
        cell_Sr_pv_C=cell_Sr_pv_C,
        cell_Eu_pv_C=cell_Eu_pv_C,
        cell_Er_pv_C=cell_Er_pv_C,
        plasma_dc_bias_V=plasma_dc_bias_V,
        plasma_forward_W=plasma_forward_W,
        plasma_reflected_W=plasma_reflected_W,
    )


def _all_direct_read_displays(monitor: GrowthMonitor):
    """The direct-read ValueDisplay widgets on the Direct-read tab.

    Kept as a helper so tests that iterate over "all direct-read displays"
    automatically pick up new fields. Cell widgets live in
    ``monitor._cell_displays`` (built from config) so adding/removing
    cells only requires updating ``_build_direct_read_tab``, not this list.
    """
    return (
        monitor.substrate_pv_display,
        monitor.substrate_sp_display,
        *monitor._cell_displays,
        monitor.plasma_dc_display,
        monitor.plasma_fwd_display,
        monitor.plasma_rfl_display,
    )


# ---------------------------------------------------------------------------
# Worker: mode routing
# ---------------------------------------------------------------------------

class WorkerModeRoutingTests(unittest.TestCase):
    """``_create_driver`` returns the right driver class per mode.

    Guarding this is the whole point of the mode flag — if elog silently
    reverted to screengrab, growers wouldn't see any error, they'd just
    see a session without direct-read data. Locking the contract with a
    test.
    """

    def test_screengrab_mode_returns_evap_control(self):
        worker = EvapControlWorker(mode="screengrab")
        driver = worker._create_driver()
        self.assertIsInstance(driver, EvapControl)

    def test_elog_mode_returns_elog_reader(self):
        worker = EvapControlWorker(mode="elog")
        driver = worker._create_driver()
        self.assertIsInstance(driver, ElogReader)

    def test_dummy_mode_returns_dummy_evap_control(self):
        worker = EvapControlWorker(mode="dummy")
        driver = worker._create_driver()
        self.assertIsInstance(driver, DummyEvapControl)

    def test_unknown_mode_falls_back_to_dummy(self):
        # Any unrecognized string hits the else branch in _create_driver
        # and returns DummyEvapControl. Documents the current
        # default-safe fallback — a typo'd mode string yields fake
        # data instead of a crash. Worth being explicit; if we ever
        # tighten this to raise ValueError, this test breaks first
        # and forces a call-site audit.
        worker = EvapControlWorker(mode="eloge")  # plausible typo
        driver = worker._create_driver()
        self.assertIsInstance(driver, DummyEvapControl)


# ---------------------------------------------------------------------------
# Config tab: mode combobox contents + default
# ---------------------------------------------------------------------------

class ConfigModeOptionsTests(unittest.TestCase):
    """Locks the option set and default of the Evap Control mode selector
    in the Config tab. The set of available modes drives which drivers
    can be exercised; the default determines what most sessions actually
    use.
    """

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def test_config_evap_mode_offers_three_options(self):
        options = {
            self.monitor.config_evap_mode.itemText(i)
            for i in range(self.monitor.config_evap_mode.count())
        }
        self.assertEqual(options, {"dummy", "elog", "screengrab"})

    def test_config_evap_mode_default_is_elog(self):
        # Default flipped from "screengrab" to "elog" 2026-07-09 per
        # the P3 decision — direct-read is strictly better when
        # EvapControl is running. Bulbasaur end-to-end validation is
        # pending (see elog_direct_read_may15.md Jul 9 checklist). If
        # the failure mode turns out too noisy for growers and we add
        # auto-fallback or revert the default, update this test and
        # the corresponding setCurrentText call in growth_monitor.py.
        self.assertEqual(
            self.monitor.config_evap_mode.currentText(),
            "elog",
        )


# ---------------------------------------------------------------------------
# Direct-read tab: UI display of the 10 elog-direct fields
# ---------------------------------------------------------------------------

class DirectReadTabTests(unittest.TestCase):
    """Jul 9 2026 UI addition. Verifies the tab exists, displays state
    correctly, and ``reset_displays`` clears it. Each of the 10
    elog-direct fields has its own ``ValueDisplay``; plasma section is
    hidden when the plasma source is off (all-None state) and shown when
    any plasma field is populated.
    """

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def test_direct_read_tab_registered(self):
        tab_labels = [
            self.monitor._tabs.tabText(i)
            for i in range(self.monitor._tabs.count())
        ]
        self.assertIn("Direct-read", tab_labels)

    def test_all_displays_start_at_dashes(self):
        # ValueDisplay's default value text is "---" — verifying this
        # so we know each widget exists and the tab construction
        # didn't crash somewhere partway through.
        for d in _all_direct_read_displays(self.monitor):
            self.assertEqual(d.value.text(), "---")

    def test_plasma_section_hidden_at_construction(self):
        # ``_build_direct_read_tab`` calls ``plasma_group.setVisible(False)``
        # so a fresh monitor doesn't show the plasma UI until an actual
        # populated state arrives.
        self.assertTrue(self.monitor.plasma_group.isHidden())

    def test_populated_state_populates_every_display(self):
        state = _make_evap_state(
            plasma_dc_bias_V=45.2,
            plasma_forward_W=250.5,
            plasma_reflected_W=3.1,
        )
        self.monitor.update_evap_state(state)

        # Substrate (1 decimal place, °C unit — matches ValueDisplay
        # constructor args in _build_direct_read_tab).
        self.assertEqual(
            self.monitor.substrate_pv_display.value.text(), "720.5 °C",
        )
        self.assertEqual(
            self.monitor.substrate_sp_display.value.text(), "725.0 °C",
        )
        # Cells — order matches OXIDE_MBE.cell_display: HTEC2, Y, Sr, Eu, Er
        expected_cell_temps = ["425.0 °C", "632.1 °C", "550.3 °C", "580.7 °C", "610.2 °C"]
        for display, expected in zip(self.monitor._cell_displays, expected_cell_temps):
            self.assertEqual(display.value.text(), expected)
        # Plasma
        self.assertEqual(
            self.monitor.plasma_dc_display.value.text(), "45.2 V",
        )
        self.assertEqual(
            self.monitor.plasma_fwd_display.value.text(), "250.5 W",
        )
        self.assertEqual(
            self.monitor.plasma_rfl_display.value.text(), "3.1 W",
        )

    def test_plasma_section_hidden_when_all_plasma_none(self):
        # Default _make_evap_state has all plasma fields None (models
        # an off plasma source that ElogReader NaN-filters). The
        # section should stay hidden.
        state = _make_evap_state()  # plasma_* all None
        self.monitor.update_evap_state(state)
        self.assertTrue(self.monitor.plasma_group.isHidden())

    def test_plasma_section_shown_when_any_plasma_populated(self):
        # One populated plasma field is enough to reveal the section
        # (the ``any`` clause in update_evap_state's visibility check).
        state = _make_evap_state(plasma_dc_bias_V=45.0)
        self.monitor.update_evap_state(state)
        self.assertFalse(self.monitor.plasma_group.isHidden())

    def test_disconnected_state_clears_all_direct_read_displays(self):
        # Populate first, then disconnect (worker's error path
        # produces connected=False + error text). Every direct-read
        # display should return to "---" — mirrors the pressure
        # display's connection-conditional behavior on the Monitor tab.
        self.monitor.update_evap_state(_make_evap_state())
        self.assertEqual(
            self.monitor.substrate_pv_display.value.text(), "720.5 °C",
        )
        disconnected = _make_evap_state(connected=False)
        self.monitor.update_evap_state(disconnected)
        for d in _all_direct_read_displays(self.monitor):
            self.assertEqual(d.value.text(), "---")

    def test_screengrab_mode_populates_pressure_only(self):
        # In screengrab mode, EvapControl.read() returns only
        # chamber_pressure_mbar. The worker sets all elog-direct
        # fields via .get() which returns None for missing keys, so
        # they stay at their None default. UI should show pressure
        # on the Monitor tab but "---" on all Direct-read fields.
        screengrab_state = EvapControlState(
            mode="screengrab",
            connected=True,
            valid=True,
            error="",
            chamber_pressure_mbar=1e-9,
            # All elog-direct fields left at their None defaults.
        )
        self.monitor.update_evap_state(screengrab_state)
        self.assertEqual(
            self.monitor.pressure_display.value.text(), "1.00e-09 mbar",
        )
        for d in _all_direct_read_displays(self.monitor):
            self.assertEqual(d.value.text(), "---")
        self.assertTrue(self.monitor.plasma_group.isHidden())

    def test_reset_displays_clears_direct_read_and_hides_plasma(self):
        # End-of-session: reset_displays runs. All fields back to "---",
        # plasma section re-hidden regardless of last-populated state.
        plasma_on = _make_evap_state(
            plasma_dc_bias_V=45.0, plasma_forward_W=250.0,
            plasma_reflected_W=3.0,
        )
        self.monitor.update_evap_state(plasma_on)
        self.assertEqual(
            self.monitor.substrate_pv_display.value.text(), "720.5 °C",
        )
        self.assertFalse(self.monitor.plasma_group.isHidden())

        self.monitor.reset_displays()

        for d in _all_direct_read_displays(self.monitor):
            self.assertEqual(d.value.text(), "---")
        self.assertTrue(self.monitor.plasma_group.isHidden())


class ContinuousCaptureIndicatorTests(unittest.TestCase):
    """Jul 10 2026 Monitor-tab footer indicator for continuous capture.

    Surfaces the 'movie' cadence + running frame count that the growers
    asked for at the group meeting. Backed by the existing heartbeat
    timer + save_heartbeat_frame path; this test class covers only the
    UI-side counter + label formatting.
    """

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def test_idle_state_at_construction(self):
        self.assertEqual(
            self.monitor.continuous_capture_label.text(), "Capture: idle",
        )

    def test_set_interval_shows_zero_frames_with_plural_noun(self):
        self.monitor.set_continuous_capture_interval(5.0)
        # "0 frames" not "0 frame" — plural noun at zero count matches
        # standard English usage and downstream test/log formatting.
        self.assertEqual(
            self.monitor.continuous_capture_label.text(),
            "Capture: every 5s · 0 frames",
        )

    def test_integer_interval_renders_without_decimal(self):
        self.monitor.set_continuous_capture_interval(10.0)
        self.assertIn("every 10s", self.monitor.continuous_capture_label.text())
        self.assertNotIn("10.0s", self.monitor.continuous_capture_label.text())

    def test_fractional_interval_renders_one_decimal(self):
        self.monitor.set_continuous_capture_interval(2.5)
        self.assertIn("every 2.5s", self.monitor.continuous_capture_label.text())

    def test_first_frame_uses_singular_noun(self):
        self.monitor.set_continuous_capture_interval(5.0)
        self.monitor.increment_continuous_capture_count()
        self.assertEqual(
            self.monitor.continuous_capture_label.text(),
            "Capture: every 5s · 1 frame",
        )

    def test_count_increments_monotonically(self):
        self.monitor.set_continuous_capture_interval(5.0)
        for _ in range(3):
            self.monitor.increment_continuous_capture_count()
        self.assertEqual(
            self.monitor.continuous_capture_label.text(),
            "Capture: every 5s · 3 frames",
        )

    def test_set_interval_none_clears_indicator(self):
        self.monitor.set_continuous_capture_interval(5.0)
        self.monitor.increment_continuous_capture_count()
        self.monitor.set_continuous_capture_interval(None)
        # Interval None resets the display but the state fields stay
        # consistent — count is preserved internally until the next
        # session reset (via reset_displays).
        self.assertEqual(
            self.monitor.continuous_capture_label.text(), "Capture: idle",
        )

    def test_reset_displays_clears_indicator_and_count(self):
        self.monitor.set_continuous_capture_interval(5.0)
        for _ in range(4):
            self.monitor.increment_continuous_capture_count()
        self.monitor.reset_displays()
        self.assertEqual(
            self.monitor.continuous_capture_label.text(), "Capture: idle",
        )
        # Internal count reset — the next set_interval call starts from 0.
        self.assertEqual(self.monitor._continuous_capture_count, 0)
        self.assertIsNone(self.monitor._continuous_capture_interval_s)


class ContinuousCaptureConfigTests(unittest.TestCase):
    """Config-tab widget renames landed Jul 10 2026 — user-facing label
    switched from 'RHEED heartbeat interval' to 'Continuous capture
    interval' to match grower vocabulary. Internal name kept."""

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def test_config_label_uses_grower_vocabulary(self):
        # The label is set via config_form.addRow(label_text, widget) —
        # QFormLayout stores it as a QLabel whose text() we can read.
        # Find the row whose field is our spinbox.
        spin = self.monitor.config_heartbeat_interval_spin
        form = None
        # Walk parent-widget siblings for the form layout that owns spin.
        # Not perfect but works with the current widget hierarchy.
        parent = spin.parentWidget()
        if parent is not None:
            for layout in parent.findChildren(type(parent.layout())):
                if layout is not None and layout.indexOf(spin) != -1:
                    form = layout
                    break
        # Fallback: iterate widget tree looking for a QLabel whose
        # buddy is our spinbox.
        from PyQt6.QtWidgets import QLabel
        labels = self.monitor.findChildren(QLabel)
        label_texts = [lbl.text() for lbl in labels if lbl.text()]
        # New label should be present, old should not.
        joined = " | ".join(label_texts)
        self.assertIn("Continuous capture interval", joined)
        self.assertNotIn("RHEED heartbeat interval", joined)

    def test_internal_widget_name_unchanged(self):
        # Programmatic access still uses config_heartbeat_interval_spin
        # so nothing external breaks. Documents the compat contract.
        self.assertTrue(
            hasattr(self.monitor, "config_heartbeat_interval_spin")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
