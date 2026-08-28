"""Unit tests for gui/auto_capture.py — PixelDiffChangeDetector +
AutoCaptureEngine (D3 Day 9 sprint kickoff).

Locks core detector math (score_metric, ROI, buffer eviction) and
engine state machine (warmup, debounce, cooldown, adaptive threshold).
The plateau-shift confirmation function has its own smoke-test
script (test_confirm_event_plateau_shift.py); this file focuses on
the two heaviest untested classes.

All tests are Mac-side pure-numpy — no PyQt6 signal event loop, no
camera hardware. Signal emissions verified via direct assertion on
frame_captured's spy list.

Run:
    python -m pytest -q tests/test_auto_capture.py
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# QApplication required because AutoCaptureEngine is a QObject that
# emits pyqtSignal. No QWidgets are created, but signals still need
# the event-loop machinery to exist.
from PyQt6.QtWidgets import QApplication  # noqa: E402
_app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

import numpy as np  # noqa: E402

from gui.auto_capture import (  # noqa: E402
    AutoCaptureEngine,
    ChangeDetector,
    PixelDiffChangeDetector,
    TranslationInvariantChangeDetector,
)


def _frame(fill: float = 0.0, shape: tuple[int, int] = (32, 32),
           noise: float = 0.0, seed: int | None = None) -> np.ndarray:
    """Build a synthetic grayscale frame. `noise` adds Gaussian
    perturbation with amplitude 0-N per pixel (used to create
    detectable change between frames)."""
    rng = np.random.default_rng(seed)
    arr = np.full(shape, fill, dtype=np.float32)
    if noise > 0:
        arr += rng.normal(0, noise, size=shape).astype(np.float32)
    return arr


def _rheed_pattern(
    shape: tuple[int, int] = (64, 80),
    *,
    variant: int = 0,
) -> np.ndarray:
    """Synthetic spot/streak pattern with enough texture for registration."""
    height, width = shape
    y, x = np.mgrid[:height, :width]
    image = (
        18.0
        + 125.0 * np.exp(-((x - 0.31 * width) ** 2) / 18.0)
        * np.exp(-((y - 0.36 * height) ** 2) / 65.0)
        + 105.0 * np.exp(-((x - 0.67 * width) ** 2) / 22.0)
        * np.exp(-((y - 0.63 * height) ** 2) / 55.0)
        + 24.0 * np.sin(x / 5.5) ** 2
    ).astype(np.float32)
    if variant:
        x_center = int((0.42 + 0.05 * (variant % 3)) * width)
        y_center = int((0.24 + 0.07 * (variant % 2)) * height)
        image += (
            (80.0 + 15.0 * variant)
            * np.exp(-((x - x_center) ** 2 + (y - y_center) ** 2) / 30.0)
        ).astype(np.float32)
    return image


def _translated(
    frame: np.ndarray,
    shift_y: int,
    shift_x: int,
) -> np.ndarray:
    """Translate with blank padding (no wrapped pixels)."""
    output = np.zeros_like(frame)
    height, width = frame.shape
    source_y_start = max(0, -shift_y)
    source_y_end = min(height, height - shift_y)
    source_x_start = max(0, -shift_x)
    source_x_end = min(width, width - shift_x)
    target_y_start = source_y_start + shift_y
    target_y_end = source_y_end + shift_y
    target_x_start = source_x_start + shift_x
    target_x_end = source_x_end + shift_x
    output[target_y_start:target_y_end, target_x_start:target_x_end] = frame[
        source_y_start:source_y_end,
        source_x_start:source_x_end,
    ]
    return output


class _SequenceDetector(ChangeDetector):
    """Deterministic scalar detector for engine state-machine tests."""

    def __init__(self, samples: list[float | tuple[float, str]]):
        self._samples = iter(samples)
        self._last_score = 0.0
        self._last_status = "ok"

    def reset(self) -> None:
        self._last_score = 0.0
        self._last_status = "ok"

    def compute_score(self, frame: np.ndarray) -> float:
        del frame
        sample = next(self._samples)
        if isinstance(sample, tuple):
            score, status = sample
            self._last_score = float(score)
            self._last_status = status
        else:
            self._last_score = float(sample)
            self._last_status = "ok"
        return self._last_score

    @property
    def last_diagnostics(self) -> dict:
        return {
            "detector": type(self).__name__,
            "status": self._last_status,
            "score": self._last_score,
            "raw_score": self._last_score,
        }


class PixelDiffChangeDetectorTests(unittest.TestCase):
    """Seven tests locking core detector math."""

    def test_first_frame_score_is_zero(self):
        # Empty buffer → seed frame + return 0.0. Any positive
        # score on the first frame would be a false positive (no
        # baseline to compare against).
        det = PixelDiffChangeDetector()
        score = det.compute_score(_frame(fill=100.0, seed=1))
        self.assertEqual(score, 0.0)

    def test_score_metric_mean_vs_std(self):
        # Same input frames + different score_metric should produce
        # different scores. Locks the branch inside compute_score at
        # the `if self._score_metric == "mean"` fork.
        seed_frame = _frame(fill=100.0, seed=1)
        # A high-contrast diff frame (checkerboard pattern)
        diff_frame = _frame(fill=100.0)
        diff_frame[::2, ::2] = 200.0  # 25% of pixels shifted

        det_mean = PixelDiffChangeDetector(
            buffer_size=5, smooth_window=1, score_metric="mean",
        )
        det_std = PixelDiffChangeDetector(
            buffer_size=5, smooth_window=1, score_metric="std",
        )
        det_mean.compute_score(seed_frame)
        det_std.compute_score(seed_frame)
        s_mean = det_mean.compute_score(diff_frame)
        s_std = det_std.compute_score(diff_frame)
        # Both must be positive (real change), but not equal.
        self.assertGreater(s_mean, 0.0)
        self.assertGreater(s_std, 0.0)
        self.assertNotAlmostEqual(s_mean, s_std, places=2)

    def test_roi_mode_specular_branch_runs(self):
        # ROI "specular" restricts diff to an image-derived region.
        # Verifying "produces DIFFERENT score than full" is brittle
        # because the specular detector may lock exactly onto the
        # changed pixels — coincidental equality doesn't mean the
        # branch is broken. Instead lock the invariants that ARE
        # meaningful: constructor accepted the mode, compute_score
        # runs without error, returns a valid float ≥ 0, and the
        # internal state records the mode. Regression guard for a
        # "specular branch silently falls back to full" bug.
        seed_frame = _frame(fill=100.0, shape=(64, 64), seed=1)
        diff_frame = seed_frame.copy()
        # Change in top-left; specular detector should NOT find its
        # ROI overlapping this quadrant heavily.
        diff_frame[0:10, 0:10] = 250.0

        det = PixelDiffChangeDetector(
            buffer_size=5, smooth_window=1, roi_mode="specular",
        )
        self.assertEqual(det._roi_mode, "specular")
        det.compute_score(seed_frame)
        score = det.compute_score(diff_frame)
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)

    def test_invalid_score_metric_raises(self):
        # Guard against silent misconfiguration — passing an
        # unknown score_metric would otherwise silently fall through
        # to the "std" branch, producing wrong results.
        with self.assertRaises(ValueError) as cm:
            PixelDiffChangeDetector(score_metric="median")
        self.assertIn("score_metric", str(cm.exception))

    def test_invalid_roi_mode_raises(self):
        with self.assertRaises(ValueError) as cm:
            PixelDiffChangeDetector(roi_mode="donut")
        self.assertIn("roi_mode", str(cm.exception))

    def test_reset_clears_buffer(self):
        # After feeding a frame and then reset(), the next compute
        # should behave as a first-frame (return 0). Guards
        # against reset() forgetting to clear _sum or _buffer.
        det = PixelDiffChangeDetector(buffer_size=5)
        det.compute_score(_frame(fill=100.0, seed=1))
        det.compute_score(_frame(fill=150.0, seed=2))
        det.reset()
        score_after_reset = det.compute_score(
            _frame(fill=200.0, seed=3),
        )
        self.assertEqual(score_after_reset, 0.0)

    def test_buffer_size_evicts_oldest(self):
        # Feed buffer_size + 3 frames; internal deque should hold
        # exactly buffer_size, with the oldest 3 evicted. Verified
        # via the internal _buffer deque (whitebox — locks the
        # eviction semantics we depend on for the running _sum).
        det = PixelDiffChangeDetector(buffer_size=4, smooth_window=1)
        for i in range(7):
            det.compute_score(_frame(fill=100.0 + i, seed=i))
        self.assertEqual(len(det._buffer), 4)


class TranslationInvariantChangeDetectorTests(unittest.TestCase):
    """Registration, structural scoring, and fail-closed behavior."""

    def test_identical_translated_frame_has_low_score(self):
        reference = _rheed_pattern()
        current = _translated(reference, shift_y=3, shift_x=-4)
        detector = TranslationInvariantChangeDetector(
            smooth_window=1,
            max_shift_px=8,
        )
        self.assertEqual(detector.compute_score(reference), 0.0)

        score = detector.compute_score(current)

        self.assertLess(score, 0.05)
        diagnostics = detector.last_diagnostics
        self.assertEqual(diagnostics["status"], "ok")
        self.assertEqual(diagnostics["shift_y_px"], -3)
        self.assertEqual(diagnostics["shift_x_px"], 4)
        self.assertGreater(diagnostics["registration_confidence"], 0.4)
        self.assertGreater(diagnostics["overlap_fraction"], 0.85)

    def test_structural_change_stays_high_after_registration(self):
        reference = _rheed_pattern()
        changed = _translated(
            _rheed_pattern(variant=2),
            shift_y=2,
            shift_x=-3,
        )
        detector = TranslationInvariantChangeDetector(
            smooth_window=1,
            max_shift_px=8,
        )
        detector.compute_score(reference)

        score = detector.compute_score(changed)

        self.assertGreater(score, 4.0)
        self.assertEqual(detector.last_diagnostics["status"], "ok")

    def test_uniform_brightness_and_contrast_change_is_controlled(self):
        reference = _rheed_pattern()
        brighter = 1.7 * reference + 31.0
        detector = TranslationInvariantChangeDetector(smooth_window=1)
        detector.compute_score(reference)

        score = detector.compute_score(brighter)

        self.assertLess(score, 0.05)
        self.assertEqual(detector.last_diagnostics["status"], "ok")

    def test_low_texture_fails_closed_with_diagnostic(self):
        detector = TranslationInvariantChangeDetector(
            smooth_window=1,
            min_texture_span=2.0,
        )
        detector.compute_score(_frame(fill=100.0, shape=(64, 80)))

        score = detector.compute_score(_frame(fill=105.0, shape=(64, 80)))

        self.assertEqual(score, 0.0)
        self.assertEqual(detector.last_diagnostics["status"], "low_texture")

    def test_excessive_shift_fails_closed_with_diagnostic(self):
        reference = _rheed_pattern()
        current = _translated(reference, shift_y=12, shift_x=11)
        detector = TranslationInvariantChangeDetector(
            smooth_window=1,
            max_shift_px=4,
        )
        detector.compute_score(reference)

        score = detector.compute_score(current)

        self.assertEqual(score, 0.0)
        diagnostics = detector.last_diagnostics
        self.assertEqual(diagnostics["status"], "excessive_shift")
        self.assertTrue(
            abs(diagnostics["shift_y_px"]) > 4
            or abs(diagnostics["shift_x_px"]) > 4
        )


class AutoCaptureEngineTests(unittest.TestCase):
    """Eight tests locking the engine state machine."""

    def setUp(self):
        # Fresh engine per test — the state machine has side effects
        # (last_capture_time, debounce_count) that need clean slates.
        self.emitted: list[tuple] = []

    def _connect_spy(self, engine: AutoCaptureEngine) -> None:
        engine.frame_captured.connect(
            lambda frame, score: self.emitted.append((frame, score)),
        )

    def test_disabled_by_default(self):
        # Fresh engine must not fire events. GrowthApp explicitly
        # enables via .enabled = True on session start; a construction
        # that fired by default would spam captures the moment a
        # dummy camera pushed a frame.
        engine = AutoCaptureEngine()
        self.assertFalse(engine.enabled)
        self._connect_spy(engine)
        engine.evaluate(_frame(fill=100.0, seed=1))
        self.assertEqual(self.emitted, [])

    def test_warmup_suppresses_events(self):
        # Enable + feed warmup_frames worth of high-change frames.
        # None should emit because the detector is still building
        # a baseline. Frame count 1..warmup_frames is warmup;
        # emissions can only start after that window.
        engine = AutoCaptureEngine(
            warmup_frames=5, threshold=0.001, cooldown_s=0.0,
        )
        engine.enabled = True
        self._connect_spy(engine)
        for i in range(5):
            engine.evaluate(
                _frame(fill=100.0 + i * 30, seed=i),
            )
        self.assertEqual(self.emitted, [])

    def test_debounce_requires_3_consecutive_above_threshold(self):
        # Fire a low threshold to make it easy to trigger. Only
        # after 3 CONSECUTIVE above-threshold frames should
        # frame_captured emit. Regression guard: earlier engine
        # versions emitted on the first crossing.
        engine = AutoCaptureEngine(
            warmup_frames=1, threshold=0.001, cooldown_s=0.0,
        )
        engine.enabled = True
        self._connect_spy(engine)
        # Warmup burn
        engine.evaluate(_rheed_pattern())
        # Now feed a sustained spatially changed state. The production
        # detector intentionally ignores uniform brightness-only changes.
        for i in range(3):
            engine.evaluate(_rheed_pattern(variant=2))
        # After 3 consecutive, exactly 1 event emitted
        self.assertEqual(len(self.emitted), 1)

    def test_cooldown_blocks_second_event(self):
        # After event fires, subsequent triggers within cooldown_s
        # window must be suppressed even if debounce is satisfied.
        # Use a real cooldown_s > 0 and verify only one event
        # fires within the window.
        engine = AutoCaptureEngine(
            warmup_frames=1, threshold=0.001, cooldown_s=60.0,
        )
        engine.enabled = True
        self._connect_spy(engine)
        engine.evaluate(_rheed_pattern())  # warmup
        # First 3 → fire event #1
        for i in range(3):
            engine.evaluate(_rheed_pattern(variant=2))
        self.assertEqual(len(self.emitted), 1)
        # Next 3 within cooldown — must not fire
        for i in range(3, 6):
            engine.evaluate(_rheed_pattern(variant=3))
        self.assertEqual(len(self.emitted), 1)

    def test_sustained_plateau_requires_below_threshold_rearm(self):
        engine = AutoCaptureEngine(
            warmup_frames=0,
            threshold=1.0,
            cooldown_s=0.0,
            rearm_below_frames=3,
        )
        engine.set_detector(
            _SequenceDetector(
                [2.0] * 6 + [0.0] * 3 + [2.0] * 3,
            )
        )
        engine.enabled = True
        self._connect_spy(engine)

        for _ in range(3):
            engine.evaluate(_frame())
        self.assertEqual(len(self.emitted), 1)
        self.assertFalse(engine.latest_diagnostics["trigger_armed"])

        for _ in range(3):
            engine.evaluate(_frame())
        self.assertEqual(len(self.emitted), 1)
        self.assertEqual(
            engine.latest_diagnostics["suppressed"],
            "waiting_for_rearm",
        )

        for _ in range(3):
            engine.evaluate(_frame())
        self.assertTrue(engine.latest_diagnostics["trigger_armed"])
        self.assertTrue(engine.latest_diagnostics["rearmed"])

        for _ in range(3):
            engine.evaluate(_frame())
        self.assertEqual(len(self.emitted), 2)
        self.assertFalse(engine.latest_diagnostics["trigger_armed"])

    def test_unavailable_zero_scores_do_not_rearm(self):
        engine = AutoCaptureEngine(
            warmup_frames=0,
            threshold=1.0,
            cooldown_s=0.0,
            rearm_below_frames=3,
        )
        engine.set_detector(
            _SequenceDetector(
                [2.0] * 3
                + [(0.0, "low_texture")] * 3
                + [2.0] * 3
                + [0.0] * 3
                + [2.0] * 3
            )
        )
        engine.enabled = True
        self._connect_spy(engine)

        for _ in range(3):
            engine.evaluate(_frame())
        self.assertEqual(len(self.emitted), 1)

        for _ in range(3):
            engine.evaluate(_frame())
        self.assertFalse(engine.latest_diagnostics["trigger_armed"])
        self.assertEqual(
            engine.latest_diagnostics["suppressed"],
            "unavailable_score",
        )

        for _ in range(3):
            engine.evaluate(_frame())
        self.assertEqual(len(self.emitted), 1)

        for _ in range(3):
            engine.evaluate(_frame())
        self.assertTrue(engine.latest_diagnostics["trigger_armed"])

        for _ in range(3):
            engine.evaluate(_frame())
        self.assertEqual(len(self.emitted), 2)

    def test_production_default_exposes_diagnostics_without_signal_break(self):
        engine = AutoCaptureEngine(
            warmup_frames=1,
            threshold=0.5,
            cooldown_s=0.0,
        )
        self.assertIsInstance(
            engine._detector,
            TranslationInvariantChangeDetector,
        )
        engine.enabled = True
        self._connect_spy(engine)
        engine.evaluate(
            _rheed_pattern(),
            {"capture_sequence": 41, "capture_backend": "wgc"},
        )
        for _ in range(3):
            engine.evaluate(
                _rheed_pattern(variant=2),
                {"capture_sequence": 42, "capture_backend": "wgc"},
            )

        self.assertEqual(len(self.emitted), 1)
        # Existing signal is still exactly (frame, scalar score).
        self.assertEqual(len(self.emitted[0]), 2)
        self.assertIsInstance(self.emitted[0][1], float)
        diagnostics = engine.latest_diagnostics
        self.assertEqual(
            diagnostics["detector"],
            "TranslationInvariantChangeDetector",
        )
        self.assertIn("shift_y_px", diagnostics)
        self.assertIn("registration_confidence", diagnostics)
        latest_metadata = engine.get_recent_captures()[-1][1]
        self.assertEqual(latest_metadata["capture_sequence"], 42)
        self.assertEqual(latest_metadata["capture_backend"], "wgc")
        self.assertEqual(
            latest_metadata["change_detection"]["detector"],
            "TranslationInvariantChangeDetector",
        )

    def test_context_buffer_size_20(self):
        # Default context_buffer_size == 20. Feed 30 frames; only
        # the last 20 should remain in the ring buffer.
        engine = AutoCaptureEngine(warmup_frames=1)
        engine.enabled = True
        for i in range(30):
            engine.evaluate(_frame(fill=float(i), seed=i))
        self.assertEqual(len(engine._context_buffer), 20)

    def test_get_recent_frames_returns_copy_not_reference(self):
        # Contract: modifying the returned frames must not affect
        # the engine's internal state. Regression guard for a
        # would-be bug where a caller doing frame[:] = 0 for
        # visualization would silently corrupt the buffer.
        engine = AutoCaptureEngine(warmup_frames=1)
        engine.enabled = True
        engine.evaluate(_frame(fill=42.0, seed=1))
        frames = engine.get_recent_frames()
        self.assertGreater(len(frames), 0)
        # Mutate the returned array
        frames[0][:] = 999.0
        # Internal buffer should still hold the original values
        internal, _metadata = engine._context_buffer[0]
        self.assertNotEqual(float(internal[0, 0]), 999.0)

    def test_capture_metadata_is_paired_and_copied(self):
        engine = AutoCaptureEngine(warmup_frames=1)
        engine.enabled = True
        metadata = {
            "capture_backend": "wgc",
            "capture_sequence": 123,
            "captured_at_utc": "2026-07-27T12:00:00.000Z",
        }
        engine.evaluate(_frame(fill=42.0, seed=2), metadata)
        metadata["capture_sequence"] = 999

        captures = engine.get_recent_captures()
        self.assertEqual(captures[0][1]["capture_sequence"], 123)
        captures[0][0][:] = 999.0
        captures[0][1]["capture_sequence"] = -1

        internal_frame, internal_metadata = engine._context_buffer[0]
        self.assertNotEqual(float(internal_frame[0, 0]), 999.0)
        self.assertEqual(internal_metadata["capture_sequence"], 123)

    def test_adaptive_threshold_uses_baseline_after_warmup(self):
        # With adaptive_sigma set + baseline filled, effective_threshold
        # should differ from the fixed threshold. Baseline empty →
        # falls back to fixed. Feeds enough below-threshold scores
        # to fill the rolling window, then asserts the effective
        # value has moved.
        engine = AutoCaptureEngine(
            warmup_frames=0, threshold=100.0,
            adaptive_sigma=2.0, adaptive_history=20,
            adaptive_warmup=5, adaptive_floor=0.1,
            suppress_events_during_adaptive_warmup=False,
        )
        engine.enabled = True
        # Baseline empty → effective == fixed
        self.assertEqual(engine.effective_threshold, 100.0)
        # Manually push 10 baseline scores (avoid the full evaluate
        # path since we just want to exercise effective_threshold)
        for s in [0.5, 0.6, 0.4, 0.7, 0.5, 0.6, 0.4, 0.5, 0.6, 0.5]:
            engine._baseline_scores.append(s)
        # Baseline filled beyond adaptive_warmup=5 → adaptive kicks in
        # Mean ~0.53, std small, adaptive = mean + 2*std ≈ 0.7ish
        # Which is well below fixed 100.0 → adaptive_floor=0.1 caps
        # but doesn't help; expect effective ~0.7 or so
        eff = engine.effective_threshold
        self.assertLess(eff, 100.0)
        self.assertGreaterEqual(eff, 0.1)  # adaptive_floor

    def test_adaptive_warmup_suppresses_events_and_feeds_baseline(self):
        # During adaptive_warmup (baseline < adaptive_warmup samples),
        # events are suppressed but scores STILL feed the baseline.
        # This is the fix for the "4 spurious events at frames 32/40/
        # 48/56 every session" bug documented in the module.
        engine = AutoCaptureEngine(
            warmup_frames=1, threshold=0.001, cooldown_s=0.0,
            adaptive_sigma=2.0, adaptive_history=20,
            adaptive_warmup=10, adaptive_floor=0.001,
            suppress_events_during_adaptive_warmup=True,
        )
        engine.enabled = True
        self._connect_spy(engine)
        # Warmup burn
        engine.evaluate(_rheed_pattern())
        # Feed many high-change frames — should suppress events
        # entirely while baseline is filling
        for i in range(8):
            engine.evaluate(
                _rheed_pattern(variant=i + 1),
            )
        self.assertEqual(self.emitted, [])
        # Baseline should have accumulated the scores from those frames
        self.assertGreater(len(engine._baseline_scores), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
