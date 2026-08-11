"""Mac-side test suite for the grower-correction UI (deliverable #5).

Exercises the ``✎ Correct`` toggle, Pattern A proportional adjustment,
sum-to-100 normalization, correction-mode guard on
``update_classifier_state``, and the enriched ``_on_commit`` payload
(paired classifier + grower fields, ``grower_corrected`` flag,
auto-lock after LOG ENTRY).

Runs headless via ``QT_QPA_PLATFORM=offscreen`` so no display required.
Instantiates a real ``GrowthMonitor`` widget so the tests exercise the
same signal/slot wiring that ships. ClassifierState dataclasses are
built directly — no worker, no torch, no model file.

Run: ``python -m pytest -q tests/test_grower_correction.py``.
"""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock
from pathlib import Path

# Must set BEFORE any Qt import — headless CI/local runs on Mac + Linux.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

from gui.growth_monitor import GrowthMonitor  # noqa: E402
from gui.recon_labels import RECON_LABELS  # noqa: E402
from gui.state import ClassifierState, MistralState, PowerSupplyState  # noqa: E402


def _make_classifier_state(
    smoothed: dict[str, int] | None = None,
    quality: float = 0.9,
    is_ood: bool = False,
    has_confident_data: bool = True,
    ready: bool = True,
    loading: bool = False,
    error: str = "",
    model_version: str = "test-model (2026-07-06)",
) -> ClassifierState:
    """Build a ClassifierState with sane defaults for tests.

    smoothed defaults to a lopsided distribution (60% Twinned + tail)
    so it's obvious when correction *does* vs *doesn't* touch sliders.
    """
    if smoothed is None:
        smoothed = {
            "1x1": 10,
            "Twinned (2x1)": 60,
            "c(6x2)": 20,
            "rt13xrt13": 5,
            "HTR": 5,
        }
    return ClassifierState(
        loading=loading,
        ready=ready,
        error=error,
        last_frame_number=42,
        raw_scores={n: v / 100.0 for n, v in smoothed.items()},
        normalized_percent=smoothed,
        smoothed_percent=smoothed,
        raw_sum=1.0,
        quality=quality,
        is_bad=False,
        bad_confidence=0.0,
        is_ood=is_ood,
        has_confident_data=has_confident_data,
        inference_ms=13.0,
        model_version=model_version,
        view_segment_id=3,
        visual_history_generation=7,
        gun_aligned=True,
        history_required=0,
        history_ready=False,
        prediction_actionable=False,
        model_input_mode="single_frame",
    )


def _slider_values(monitor: GrowthMonitor) -> dict[str, int]:
    return {n: s.value() for n, s in monitor._recon_sliders.items()}


def _make_mistral_state(
    v_actual: float | None = 10.043,
    i_actual: float | None = 2.051,
    connected: bool = True,
) -> MistralState:
    """Build a MistralState for tests. Defaults match yesterday's dummy-mode
    readings (~10 V, ~2 A) so the numeric assertions look realistic."""
    return MistralState(
        v_set=10.0,
        v_actual=v_actual,
        i_set=2.0,
        i_actual=i_actual,
        connected=connected,
        error="",
        mode="dummy",
    )


def _make_psu_state(
    voltage_measured: float = 5.5,
    current_measured: float = 1.2,
    connected: bool = True,
) -> PowerSupplyState:
    """Build a PowerSupplyState for tests. Values distinct from MistralState
    defaults so tests can tell which source populated a row."""
    return PowerSupplyState(
        voltage_setpoint=5.5,
        current_setpoint=1.5,
        voltage_measured=voltage_measured,
        current_measured=current_measured,
        power_measured=voltage_measured * current_measured,
        output_enabled=True,
        connected=connected,
        error="",
    )


# ---------------------------------------------------------------------------
# _normalize_sliders_to_100
# ---------------------------------------------------------------------------

class NormalizationTests(unittest.TestCase):

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def _set_sliders(self, values: dict[str, int]):
        for name, v in values.items():
            slider = self.monitor._recon_sliders[name]
            slider.blockSignals(True)
            slider.setValue(v)
            slider.blockSignals(False)

    def test_already_at_100_no_change(self):
        target = {"1x1": 20, "Twinned (2x1)": 20, "c(6x2)": 20,
                  "rt13xrt13": 20, "HTR": 20}
        self._set_sliders(target)
        self.monitor._normalize_sliders_to_100()
        self.assertEqual(_slider_values(self.monitor), target)

    def test_all_zero_distributes_uniformly(self):
        self._set_sliders({n: 0 for n in RECON_LABELS})
        self.monitor._normalize_sliders_to_100()
        result = _slider_values(self.monitor)
        self.assertEqual(sum(result.values()), 100)
        # 100/5 = 20 each, no residual
        for v in result.values():
            self.assertEqual(v, 20)

    def test_sum_below_100_scales_up_preserving_ratio(self):
        # 10+30+15+2+3 = 60 → scale to 100 preserving ratios.
        self._set_sliders({"1x1": 10, "Twinned (2x1)": 30, "c(6x2)": 15,
                           "rt13xrt13": 2, "HTR": 3})
        self.monitor._normalize_sliders_to_100()
        result = _slider_values(self.monitor)
        self.assertEqual(sum(result.values()), 100)
        # Argmax preserved.
        self.assertEqual(max(result, key=result.get), "Twinned (2x1)")

    def test_sum_above_100_scales_down_preserving_ratio(self):
        # 30+60+40+20+10 = 160 → scale to 100.
        self._set_sliders({"1x1": 30, "Twinned (2x1)": 60, "c(6x2)": 40,
                           "rt13xrt13": 20, "HTR": 10})
        self.monitor._normalize_sliders_to_100()
        result = _slider_values(self.monitor)
        self.assertEqual(sum(result.values()), 100)
        self.assertEqual(max(result, key=result.get), "Twinned (2x1)")

    def test_rounding_residual_assigned_to_largest(self):
        # A distribution that rounds off-by-one after proportional scaling.
        # 11+22+33+11+22 = 99 → scale to 100. round(each * 100/99):
        #   11.11→11, 22.22→22, 33.33→33, 11.11→11, 22.22→22 = 99.
        # Residual +1 goes to the largest (c(6x2)=33).
        self._set_sliders({"1x1": 11, "Twinned (2x1)": 22, "c(6x2)": 33,
                           "rt13xrt13": 11, "HTR": 22})
        self.monitor._normalize_sliders_to_100()
        result = _slider_values(self.monitor)
        self.assertEqual(sum(result.values()), 100)
        # c(6x2) started largest and stays largest — residual landed on it.
        self.assertEqual(max(result, key=result.get), "c(6x2)")


# ---------------------------------------------------------------------------
# _on_grower_slider_changed — Pattern A proportional adjustment
# ---------------------------------------------------------------------------

class ProportionalAdjustmentTests(unittest.TestCase):

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def _prime_correction(self, values: dict[str, int]):
        """Turn on correction and set explicit starting values.

        Bypasses the toggle-on normalization by writing values directly
        under blockSignals — needed to test starting states that don't
        sum to 100 yet."""
        for name, v in values.items():
            slider = self.monitor._recon_sliders[name]
            slider.blockSignals(True)
            slider.setValue(v)
            slider.blockSignals(False)
        self.monitor._correction_active = True
        for s in self.monitor._recon_sliders.values():
            s.setEnabled(True)

    def test_no_op_when_correction_off(self):
        self._prime_correction({"1x1": 30, "Twinned (2x1)": 30, "c(6x2)": 20,
                                "rt13xrt13": 10, "HTR": 10})
        self.monitor._correction_active = False  # override the prime
        before = _slider_values(self.monitor)
        # Simulate a valueChanged handler call — should be a no-op.
        self.monitor._on_grower_slider_changed("1x1", 90)
        self.assertEqual(_slider_values(self.monitor), before)

    def test_no_op_during_reentrant_adjustment(self):
        self._prime_correction({"1x1": 20, "Twinned (2x1)": 20, "c(6x2)": 20,
                                "rt13xrt13": 20, "HTR": 20})
        self.monitor._adjusting = True  # simulate mid-adjustment
        before = _slider_values(self.monitor)
        self.monitor._on_grower_slider_changed("1x1", 90)
        self.assertEqual(_slider_values(self.monitor), before)
        self.monitor._adjusting = False  # cleanup

    def test_drag_up_reduces_others_proportionally(self):
        # Uniform starting state (sum=100).
        self._prime_correction({"1x1": 20, "Twinned (2x1)": 20, "c(6x2)": 20,
                                "rt13xrt13": 20, "HTR": 20})
        # Grower drags 1x1 up to 40. Others must sum to 60, each was 20 of
        # a 80 total → each * 60 / 80 = 15.
        self.monitor._recon_sliders["1x1"].blockSignals(True)
        self.monitor._recon_sliders["1x1"].setValue(40)
        self.monitor._recon_sliders["1x1"].blockSignals(False)
        self.monitor._on_grower_slider_changed("1x1", 40)
        result = _slider_values(self.monitor)
        self.assertEqual(sum(result.values()), 100)
        self.assertEqual(result["1x1"], 40)
        for other in ("Twinned (2x1)", "c(6x2)", "rt13xrt13", "HTR"):
            self.assertEqual(result[other], 15)

    def test_drag_to_100_zeros_others(self):
        self._prime_correction({"1x1": 20, "Twinned (2x1)": 20, "c(6x2)": 20,
                                "rt13xrt13": 20, "HTR": 20})
        self.monitor._recon_sliders["1x1"].blockSignals(True)
        self.monitor._recon_sliders["1x1"].setValue(100)
        self.monitor._recon_sliders["1x1"].blockSignals(False)
        self.monitor._on_grower_slider_changed("1x1", 100)
        result = _slider_values(self.monitor)
        self.assertEqual(sum(result.values()), 100)
        self.assertEqual(result["1x1"], 100)
        for other in ("Twinned (2x1)", "c(6x2)", "rt13xrt13", "HTR"):
            self.assertEqual(result[other], 0)

    def test_drag_down_when_others_zero_uniform_distributes(self):
        # All in one class. Grower drags it down.
        self._prime_correction({"1x1": 100, "Twinned (2x1)": 0, "c(6x2)": 0,
                                "rt13xrt13": 0, "HTR": 0})
        self.monitor._recon_sliders["1x1"].blockSignals(True)
        self.monitor._recon_sliders["1x1"].setValue(20)
        self.monitor._recon_sliders["1x1"].blockSignals(False)
        self.monitor._on_grower_slider_changed("1x1", 20)
        result = _slider_values(self.monitor)
        self.assertEqual(sum(result.values()), 100)
        self.assertEqual(result["1x1"], 20)
        # 80 distributed over 4 → 20 each, no residual.
        for other in ("Twinned (2x1)", "c(6x2)", "rt13xrt13", "HTR"):
            self.assertEqual(result[other], 20)

    def test_rounding_residual_lands_on_largest_other(self):
        # Others 13, 13, 13, 13 = 52. Grower drags changed to 51.
        # Others must sum to 49. Each: 13 * 49/52 = 12.25 → round to 12.
        # 12*4 = 48, residual +1 goes to the largest — since all are tied,
        # dict ordering picks Twinned (2x1) (first "other" in dict order).
        self._prime_correction({"1x1": 48, "Twinned (2x1)": 13, "c(6x2)": 13,
                                "rt13xrt13": 13, "HTR": 13})
        self.monitor._recon_sliders["1x1"].blockSignals(True)
        self.monitor._recon_sliders["1x1"].setValue(51)
        self.monitor._recon_sliders["1x1"].blockSignals(False)
        self.monitor._on_grower_slider_changed("1x1", 51)
        result = _slider_values(self.monitor)
        self.assertEqual(sum(result.values()), 100)
        self.assertEqual(result["1x1"], 51)
        # Exactly one of the others carries the +1 residual.
        others = [result[k] for k in
                  ("Twinned (2x1)", "c(6x2)", "rt13xrt13", "HTR")]
        self.assertEqual(sorted(others), [12, 12, 12, 13])

    def test_reentrancy_guard_prevents_stack_overflow(self):
        # This test proves the guard prevents infinite recursion when
        # our own setValue() re-triggers the connected valueChanged
        # slot on sibling sliders. Without the guard, the test hits
        # Python's recursion limit.
        self._prime_correction({"1x1": 20, "Twinned (2x1)": 20, "c(6x2)": 20,
                                "rt13xrt13": 20, "HTR": 20})
        # Real valueChanged signal path — call setValue directly and
        # let Qt fire the connected slot.
        self.monitor._recon_sliders["1x1"].setValue(40)
        result = _slider_values(self.monitor)
        self.assertEqual(sum(result.values()), 100)
        self.assertEqual(result["1x1"], 40)


# ---------------------------------------------------------------------------
# _on_correction_toggled
# ---------------------------------------------------------------------------

class ToggleTests(unittest.TestCase):

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def test_toggle_on_enables_sliders(self):
        self.assertFalse(
            self.monitor._recon_sliders["1x1"].isEnabled()
        )
        self.monitor.correction_btn.setChecked(True)
        self.monitor._on_correction_toggled(True)
        for slider in self.monitor._recon_sliders.values():
            self.assertTrue(slider.isEnabled())
        self.assertTrue(self.monitor._correction_active)

    def test_toggle_off_disables_sliders(self):
        self.monitor._on_correction_toggled(True)
        self.monitor._on_correction_toggled(False)
        for slider in self.monitor._recon_sliders.values():
            self.assertFalse(slider.isEnabled())
        self.assertFalse(self.monitor._correction_active)

    def test_toggle_on_normalizes_to_100(self):
        # Start with a non-100 sum (mirrors real classifier drift).
        for name, v in [("1x1", 25), ("Twinned (2x1)", 30), ("c(6x2)", 25),
                        ("rt13xrt13", 10), ("HTR", 9)]:
            self.monitor._recon_sliders[name].blockSignals(True)
            self.monitor._recon_sliders[name].setValue(v)
            self.monitor._recon_sliders[name].blockSignals(False)
        self.assertEqual(sum(_slider_values(self.monitor).values()), 99)
        self.monitor._on_correction_toggled(True)
        self.assertEqual(sum(_slider_values(self.monitor).values()), 100)

    def test_toggle_off_restores_classifier_state(self):
        # Cache a classifier state, then toggle on → drag → toggle off.
        # Toggle-off must re-render the cached state (not the grower drag).
        state = _make_classifier_state()
        self.monitor.update_classifier_state(state)
        expected_after_off = dict(state.smoothed_percent)

        self.monitor._on_correction_toggled(True)
        # Simulate a drag.
        self.monitor._recon_sliders["1x1"].setValue(80)
        # Toggle off — classifier state should be re-rendered.
        self.monitor._on_correction_toggled(False)
        self.assertEqual(_slider_values(self.monitor), expected_after_off)

    def test_toggle_off_with_no_classifier_falls_back_to_idle(self):
        # No classifier state ever received; toggle on then off should
        # leave sliders at 0 with idle status.
        self.monitor._on_correction_toggled(True)
        self.monitor._on_correction_toggled(False)
        self.assertEqual(
            _slider_values(self.monitor),
            {n: 0 for n in RECON_LABELS},
        )
        self.assertIn("idle", self.monitor._recon_status_label.text().lower())


# ---------------------------------------------------------------------------
# update_classifier_state guard
# ---------------------------------------------------------------------------

class UpdateClassifierStateGuardTests(unittest.TestCase):

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def test_state_cached_even_during_correction(self):
        self.monitor._on_correction_toggled(True)
        state = _make_classifier_state()
        self.monitor.update_classifier_state(state)
        self.assertIs(self.monitor._latest_classifier, state)

    def test_sliders_not_updated_during_correction(self):
        # Grower drags to a specific configuration.
        self.monitor._on_correction_toggled(True)
        self.monitor._recon_sliders["1x1"].setValue(70)
        grower_values = _slider_values(self.monitor)

        # New classifier state arrives — sliders must not move.
        different_state = _make_classifier_state(smoothed={
            "1x1": 5, "Twinned (2x1)": 5, "c(6x2)": 5,
            "rt13xrt13": 5, "HTR": 80,
        })
        self.monitor.update_classifier_state(different_state)
        self.assertEqual(_slider_values(self.monitor), grower_values)

    def test_status_label_not_overwritten_during_correction(self):
        # Toggle on sets the correction-mode message; a classifier update
        # arriving while correction is active must NOT stomp it.
        self.monitor._on_correction_toggled(True)
        correction_msg = self.monitor._recon_status_label.text()

        state = _make_classifier_state()
        self.monitor.update_classifier_state(state)
        self.assertEqual(
            self.monitor._recon_status_label.text(), correction_msg,
        )


class BlindLabelingSuppressionTests(unittest.TestCase):
    """Model-derived answers are unavailable during audited blind review."""

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def test_blind_mode_hides_classifier_and_disables_live_equalizer(self):
        self.monitor._on_blind_labeling_mode_changed(True)
        self.assertTrue(all(
            widget.isHidden()
            for widget in self.monitor._live_classifier_widgets
        ))
        self.assertFalse(self.monitor._tabs.isTabEnabled(
            self.monitor._live_equalizer_tab_index,
        ))

        self.monitor._on_blind_labeling_mode_changed(False)
        self.assertTrue(all(
            not widget.isHidden()
            for widget in self.monitor._live_classifier_widgets
        ))
        self.assertTrue(self.monitor._tabs.isTabEnabled(
            self.monitor._live_equalizer_tab_index,
        ))

    def test_visible_classifier_output_is_audited_once(self):
        with unittest.mock.patch.object(
            self.monitor.events_tab,
            "record_classifier_output_visible",
        ) as record:
            state = _make_classifier_state()
            self.monitor.update_classifier_state(state)
            self.monitor.update_classifier_state(state)
        record.assert_called_once_with()


# ---------------------------------------------------------------------------
# _on_commit enrichment + auto-lock
# ---------------------------------------------------------------------------

class CommitTests(unittest.TestCase):

    def setUp(self):
        self.monitor = GrowthMonitor()
        # Signal capture — commit_requested emits the entry dict.
        self.captured: list[dict] = []
        self.monitor.commit_requested.connect(self.captured.append)
        # Simulate a running session so _on_commit doesn't bail on the
        # commit_btn.isEnabled() early-return guard.
        self.monitor.commit_btn.setEnabled(True)

    def tearDown(self):
        self.monitor.deleteLater()

    def test_commit_correction_off_pairs_slider_with_classifier(self):
        state = _make_classifier_state(smoothed={
            "1x1": 10, "Twinned (2x1)": 60, "c(6x2)": 20,
            "rt13xrt13": 5, "HTR": 5,
        })
        self.monitor.update_classifier_state(state)

        self.monitor._on_commit()
        entry = self.captured[-1]

        # Sliders mirror classifier (correction off) → recon_* ==
        # classifier_recon_*.
        for name in RECON_LABELS:
            self.assertEqual(entry[f"recon_{name}"], "")
            self.assertNotEqual(entry[f"classifier_recon_{name}"], "")
        self.assertEqual(entry["grower_corrected"], "False")
        self.assertEqual(entry["recon_label_source"], "")
        # Fully-ready classifier → OK.
        self.assertEqual(entry["classifier_status"], "OK")
        self.assertEqual(entry["classifier_input_mode"], "single_frame")
        self.assertEqual(
            entry["classifier_prediction_actionable"], "False"
        )
        self.assertEqual(entry["classifier_view_segment_id"], "3")
        self.assertEqual(
            entry["classifier_visual_history_generation"], "7"
        )
        self.assertEqual(entry["classifier_history_ready"], "False")

    def test_commit_correction_on_captures_both(self):
        # Classifier says Twinned; grower disagrees and says 1x1.
        classifier_smoothed = {
            "1x1": 5, "Twinned (2x1)": 60, "c(6x2)": 20,
            "rt13xrt13": 10, "HTR": 5,
        }
        state = _make_classifier_state(smoothed=classifier_smoothed)
        self.monitor.update_classifier_state(state)

        # Enter correction, grower argues for 1x1.
        self.monitor._on_correction_toggled(True)
        self.monitor._recon_sliders["1x1"].setValue(80)

        self.monitor._on_commit()
        entry = self.captured[-1]

        # Grower recon = slider values (sum 100).
        grower_total = sum(
            int(entry[f"recon_{n}"]) for n in RECON_LABELS
        )
        self.assertEqual(grower_total, 100)
        self.assertEqual(int(entry["recon_1x1"]), 80)

        # Classifier recon = the snapshot (unchanged by correction).
        for name, v in classifier_smoothed.items():
            self.assertEqual(int(entry[f"classifier_recon_{name}"]), v)

        self.assertEqual(entry["grower_corrected"], "True")
        self.assertEqual(entry["recon_label_source"], "human_corrected")
        # Classifier still OK — correction mode doesn't change lifecycle.
        self.assertEqual(entry["classifier_status"], "OK")

    def test_commit_without_classifier_state_leaves_columns_empty(self):
        # No classifier state ever received (classifier disabled for
        # session, or hasn't emitted yet).
        self.monitor._on_commit()
        entry = self.captured[-1]
        for name in RECON_LABELS:
            self.assertEqual(entry[f"classifier_recon_{name}"], "")
        self.assertEqual(entry["grower_corrected"], "")
        # DISABLED because _latest_classifier is None.
        self.assertEqual(entry["classifier_status"], "DISABLED")
        self.assertEqual(entry["classifier_input_mode"], "")
        self.assertEqual(entry["classifier_prediction_actionable"], "")

    def test_commit_classifier_status_error(self):
        # Worker emits an error state (e.g. missing best_model.pth).
        state = _make_classifier_state(
            loading=False,
            ready=False,
            error="Failed to load classifier: model file missing",
        )
        self.monitor.update_classifier_state(state)

        self.monitor._on_commit()
        entry = self.captured[-1]

        self.assertEqual(entry["classifier_status"], "ERROR")
        # classifier_recon_* still populated (from smoothed_percent which
        # is uniform placeholder or empty when errored) — status column
        # is the source of truth for "was the classifier trustworthy".
        for name in RECON_LABELS:
            self.assertIn(f"classifier_recon_{name}", entry)

    def test_commit_classifier_status_loading(self):
        # Worker just started; hasn't loaded model or emitted a classify yet.
        state = _make_classifier_state(
            loading=True,
            ready=False,
            error="",
            has_confident_data=False,
        )
        self.monitor.update_classifier_state(state)

        self.monitor._on_commit()
        entry = self.captured[-1]

        self.assertEqual(entry["classifier_status"], "LOADING")

    def test_commit_classifier_status_error_takes_precedence_over_loading(self):
        # Edge case: state has loading=True AND error set — error wins so
        # downstream analysis doesn't misclassify a broken load as transient.
        state = _make_classifier_state(
            loading=True,
            ready=False,
            error="Some transient failure during load",
        )
        self.monitor.update_classifier_state(state)

        self.monitor._on_commit()
        entry = self.captured[-1]

        self.assertEqual(entry["classifier_status"], "ERROR")

    def test_commit_auto_locks_correction(self):
        state = _make_classifier_state()
        self.monitor.update_classifier_state(state)
        self.monitor._on_correction_toggled(True)
        self.assertTrue(self.monitor._correction_active)
        self.assertTrue(self.monitor.correction_btn.isChecked())

        self.monitor._on_commit()

        # Auto-lock: toggle off, button unchecked, sliders resume tracking.
        self.assertFalse(self.monitor._correction_active)
        self.assertFalse(self.monitor.correction_btn.isChecked())
        for slider in self.monitor._recon_sliders.values():
            self.assertFalse(slider.isEnabled())


# ---------------------------------------------------------------------------
# reset paths
# ---------------------------------------------------------------------------

class ResetPathTests(unittest.TestCase):

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def test_reset_displays_locks_correction(self):
        self.monitor._on_correction_toggled(True)
        self.assertTrue(self.monitor._correction_active)
        self.monitor.reset_displays()
        self.assertFalse(self.monitor._correction_active)
        self.assertFalse(self.monitor.correction_btn.isChecked())
        # Button is still enabled after reset — it's a fresh session,
        # correction should be available on the next arm.
        self.assertTrue(self.monitor.correction_btn.isEnabled())

    def test_reset_displays_clears_latest_classifier(self):
        self.monitor.update_classifier_state(_make_classifier_state())
        self.assertIsNotNone(self.monitor._latest_classifier)
        self.monitor.reset_displays()
        self.assertIsNone(self.monitor._latest_classifier)

    def test_set_classifier_disabled_locks_and_disables_button(self):
        self.monitor._on_correction_toggled(True)
        self.monitor.set_classifier_disabled()
        # Correction forced off.
        self.assertFalse(self.monitor._correction_active)
        # Button disabled — correcting a disabled classifier makes no
        # sense (no classifier state to pair with).
        self.assertFalse(self.monitor.correction_btn.isEnabled())

    def test_set_classifier_disabled_clears_latest_classifier(self):
        # Jul 8 Path Y regression: even if the caller doesn't disarm
        # first, set_classifier_disabled must wipe the stale cache so
        # the next LOG ENTRY writes DISABLED rows, not stale OK rows.
        state = _make_classifier_state(smoothed={
            "1x1": 2, "Twinned (2x1)": 32, "c(6x2)": 6,
            "rt13xrt13": 12, "HTR": 47,
        })
        self.monitor.update_classifier_state(state)
        self.assertIsNotNone(self.monitor._latest_classifier)

        # Grower unchecks "Live classifier" mid-session; rearm calls
        # set_classifier_disabled without disarm.
        self.monitor.set_classifier_disabled()

        self.assertIsNone(
            self.monitor._latest_classifier,
            "set_classifier_disabled must wipe stale classifier state",
        )


class ConfigLockTests(unittest.TestCase):
    """Fix A for the Jul 8 Path Y bug: config panel widgets lock during
    a session so the "checkbox says off but worker's still running"
    silent divergence can't happen."""

    def setUp(self):
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def _config_widgets(self):
        return [
            self.monitor.config_interval_spin,
            self.monitor.config_heartbeat_interval_spin,
            self.monitor.config_save_path,
            self.monitor.config_browse_btn,
            self.monitor.config_prefix,
            self.monitor.config_camera_mode,
            self.monitor.config_pyrometer_mode,
            self.monitor.config_exactus_port,
            self.monitor.config_exactus_baud,
            self.monitor.config_mistral_mode,
            self.monitor.config_evap_mode,
            self.monitor.config_classifier_enabled,
        ]

    def test_idle_state_unlocks_all_config_widgets(self):
        # Idle is the initial state and the setUp already applied it.
        for w in self._config_widgets():
            self.assertTrue(w.isEnabled(), f"{w} should be enabled in idle")

    def test_armed_state_locks_all_config_widgets(self):
        self.monitor.set_state("armed")
        for w in self._config_widgets():
            self.assertFalse(w.isEnabled(), f"{w} should be disabled in armed")

    def test_running_state_locks_all_config_widgets(self):
        self.monitor.set_state("running")
        for w in self._config_widgets():
            self.assertFalse(w.isEnabled(), f"{w} should be disabled in running")

    def test_disarm_reverts_to_idle_and_unlocks(self):
        # Go idle → armed → back to idle. Widgets should unlock again.
        self.monitor.set_state("armed")
        self.assertFalse(self.monitor.config_classifier_enabled.isEnabled())
        self.monitor.set_state("idle")
        for w in self._config_widgets():
            self.assertTrue(w.isEnabled(), f"{w} should be enabled after disarm")

    def test_locked_tooltip_reason_visible(self):
        # Locked tooltip should communicate WHY, not just "disabled".
        self.monitor.set_state("armed")
        for w in self._config_widgets():
            self.assertIn(
                "Disarm",
                w.toolTip(),
                f"{w} locked tooltip should mention 'Disarm'",
            )

    def test_unlocked_tooltip_restored(self):
        # After unlock, tooltips should be back to their originals — the
        # long explanatory ones on e.g. config_heartbeat_interval_spin
        # shouldn't be permanently overwritten by the lock reminder.
        original_heartbeat = self.monitor.config_heartbeat_interval_spin.toolTip()
        self.monitor.set_state("armed")
        self.assertNotEqual(
            original_heartbeat,
            self.monitor.config_heartbeat_interval_spin.toolTip(),
            "sanity: locked tooltip should differ from original",
        )
        self.monitor.set_state("idle")
        self.assertEqual(
            original_heartbeat,
            self.monitor.config_heartbeat_interval_spin.toolTip(),
            "unlock should restore original tooltip",
        )


class DefaultSavePathTests(unittest.TestCase):
    """Tests _default_save_path's three-way branching for T9 SSD /
    Windows fallback / non-Windows fallback. Path.exists() is patched
    per test to simulate the environments."""

    def test_windows_with_t9_returns_ssd_path(self):
        # Simulate Bulbasaur with T9 mounted.
        import gui.growth_monitor as gm
        with unittest.mock.patch.object(gm, "sys") as mock_sys:
            mock_sys.platform = "win32"
            with unittest.mock.patch.object(
                gm.Path, "exists", return_value=True,
            ):
                result = gm._default_save_path()
        self.assertEqual(result, r"E:\OMBE\GrowthMonitor")

    def test_windows_without_t9_falls_back_to_documents(self):
        # Simulate a Windows box without the T9 SSD.
        import gui.growth_monitor as gm
        with unittest.mock.patch.object(gm, "sys") as mock_sys:
            mock_sys.platform = "win32"
            with unittest.mock.patch.object(
                gm.Path, "exists", return_value=False,
            ):
                result = gm._default_save_path()
        # Should end in Documents/OMBE — cross-platform-safe suffix check
        # avoids hardcoding the user's home path.
        self.assertTrue(
            result.endswith("Documents/OMBE")
            or result.endswith("Documents\\OMBE"),
            f"expected Documents/OMBE fallback, got {result}",
        )

    def test_non_windows_returns_repo_relative(self):
        # Simulate Mac dev environment.
        import gui.growth_monitor as gm
        with unittest.mock.patch.object(gm, "sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = gm._default_save_path()
        self.assertEqual(result, "logs/growths")

    def test_t9_check_uses_drive_not_folder(self):
        # Regression: the old code required E:\OMBE to already exist,
        # which fell back to the repo when a fresh Bulbasaur launch
        # hadn't yet materialized the folder. The new code should only
        # need the drive itself. Verify by patching exists() to return
        # True only for E:\ — if the implementation asked about
        # E:\OMBE, the mock would still return True, but a subtler
        # regression would be: implementation asks about the wrong
        # path and gets False. So we assert the SSD path is returned,
        # implying the drive check succeeded.
        import gui.growth_monitor as gm
        calls = []

        def track_exists(self):
            calls.append(str(self))
            # Only "E:\\" should be checked in the happy Windows path.
            return str(self) in (r"E:\\", "E:\\")

        with unittest.mock.patch.object(gm, "sys") as mock_sys:
            mock_sys.platform = "win32"
            with unittest.mock.patch.object(
                gm.Path, "exists", track_exists,
            ):
                result = gm._default_save_path()

        self.assertEqual(result, r"E:\OMBE\GrowthMonitor")
        # Should NOT have checked E:\OMBE — that's the old buggy behavior.
        self.assertNotIn(
            r"E:\OMBE",
            calls,
            "should only check drive, not OMBE folder (Jul 8 regression)",
        )


# ---------------------------------------------------------------------------
# psu_source column + voltage_V/current_A snapshot fallthrough
# ---------------------------------------------------------------------------

class PsuSourceTests(unittest.TestCase):
    """Regression tests for the Jul 8 fix — commit_log's voltage_V/current_A
    columns now read from _latest_mistral first, then _latest_psu, with a
    psu_source column indicating which path produced the values.

    Baseline before the fix: voltage_V/current_A were blank whenever
    MISTRAL was the active PSU (which is always, on the O-MBE right now
    since TDK-Lambda hardware feeds through MISTRAL). See Jul 7 Bulbasaur
    session for the diagnostic evidence."""

    def setUp(self):
        self.monitor = GrowthMonitor()
        self.captured: list[dict] = []
        self.monitor.commit_requested.connect(self.captured.append)
        self.monitor.commit_btn.setEnabled(True)

    def tearDown(self):
        self.monitor.deleteLater()

    def test_mistral_connected_populates_voltage_current(self):
        # MISTRAL is the current O-MBE path.
        self.monitor.update_mistral_state(_make_mistral_state(
            v_actual=10.043, i_actual=2.051,
        ))
        self.monitor._on_commit()
        entry = self.captured[-1]
        self.assertEqual(entry["voltage_V"], "10.043")
        self.assertEqual(entry["current_A"], "2.051")
        self.assertEqual(entry["psu_source"], "mistral")

    def test_no_state_writes_blanks_and_none_source(self):
        # No PSU state at all — voltage/current blank, source "none".
        self.monitor._on_commit()
        entry = self.captured[-1]
        self.assertEqual(entry["voltage_V"], "")
        self.assertEqual(entry["current_A"], "")
        self.assertEqual(entry["psu_source"], "none")

    def test_direct_psu_only_falls_through_when_no_mistral(self):
        # No MISTRAL, but direct PSU wired up (future state).
        self.monitor.update_psu_state(_make_psu_state(
            voltage_measured=5.5, current_measured=1.2,
        ))
        self.monitor._on_commit()
        entry = self.captured[-1]
        self.assertEqual(entry["voltage_V"], "5.500")
        self.assertEqual(entry["current_A"], "1.200")
        self.assertEqual(entry["psu_source"], "direct")

    def test_mistral_takes_precedence_over_direct(self):
        # Both connected — MISTRAL wins (matches display-widget behavior
        # where MISTRAL updates the same voltage_display).
        self.monitor.update_psu_state(_make_psu_state(
            voltage_measured=5.5, current_measured=1.2,
        ))
        self.monitor.update_mistral_state(_make_mistral_state(
            v_actual=10.043, i_actual=2.051,
        ))
        self.monitor._on_commit()
        entry = self.captured[-1]
        self.assertEqual(entry["voltage_V"], "10.043")
        self.assertEqual(entry["current_A"], "2.051")
        self.assertEqual(entry["psu_source"], "mistral")

    def test_mistral_disconnected_falls_through_to_direct(self):
        # MISTRAL is present but marked disconnected — treated same as
        # None; direct-read wins.
        self.monitor.update_mistral_state(_make_mistral_state(
            connected=False,
        ))
        self.monitor.update_psu_state(_make_psu_state())
        self.monitor._on_commit()
        entry = self.captured[-1]
        self.assertEqual(entry["voltage_V"], "5.500")
        self.assertEqual(entry["psu_source"], "direct")

    def test_mistral_connected_but_v_actual_none_still_mistral_source(self):
        # Edge case: MISTRAL worker connected but no numeric reading
        # (OCR frame lookup returned None for that field). We record
        # source=mistral because the intended path is still MISTRAL —
        # the reading is blank, but the topology is unambiguous.
        self.monitor.update_mistral_state(_make_mistral_state(
            v_actual=None, i_actual=None, connected=True,
        ))
        self.monitor._on_commit()
        entry = self.captured[-1]
        self.assertEqual(entry["voltage_V"], "")
        self.assertEqual(entry["current_A"], "")
        self.assertEqual(entry["psu_source"], "mistral")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
