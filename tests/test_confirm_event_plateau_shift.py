#!/usr/bin/env python3
"""Smoke test for confirm_event_by_plateau_shift.

Exercise the plateau-shift confirmation test on synthetic data:

    1. Bump            — single-frame spike, baselines before/after match → REJECT
    2. Transition      — spatially-structured baseline shift              → CONFIRM
    3. Uniform shift   — global brightness change                         → REJECT (std)
                                                                          → CONFIRM (mean)
    4. Edge cases      — too-small buffer, out-of-range windows           → reason flags
    5. RGB input       — green channel reduction                          → CONFIRM

Run from repo root:

    python -m pytest -q tests/test_confirm_event_plateau_shift.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui.auto_capture import confirm_event_by_plateau_shift  # noqa: E402


def _make_frame(h: int, w: int, value: float) -> np.ndarray:
    return np.full((h, w), value, dtype=np.float32)


def test_bump_rejected():
    """Single-frame bump with matching baselines either side → REJECT."""
    h, w = 64, 64
    baseline = _make_frame(h, w, 50.0)
    bump = _make_frame(h, w, 150.0)
    buffer = [baseline] * 10 + [bump] + [baseline] * 9
    result = confirm_event_by_plateau_shift(buffer, trigger_idx=10)
    assert not result.confirmed, f"bump should be rejected: {result}"
    assert result.reason == "rejected_below_threshold", result.reason
    print(f"  bump (std)            → {result}")


def test_spatial_transition_confirmed():
    """Spatially-structured plateau shift → CONFIRM (matches reconstruction)."""
    h, w = 64, 64
    pre = _make_frame(h, w, 50.0)
    # Post: top half rises to 100, bottom half unchanged — spatial structure
    post = _make_frame(h, w, 50.0)
    post[: h // 2] = 100.0
    buffer = [pre] * 10 + [post] * 10
    result = confirm_event_by_plateau_shift(buffer, trigger_idx=10)
    assert result.confirmed, f"spatial transition should be confirmed: {result}"
    assert result.reason == "confirmed", result.reason
    print(f"  spatial transition    → {result}")


def test_uniform_shift_metric_dependent():
    """Uniform brightness shift has no spatial variance.

    Documents an important behavior: std metric (default) rejects uniform
    shifts because they're more likely to be intensity drift / heater
    changes than reconstruction transitions. Mean metric accepts them.
    Both behaviors are intentional; the choice depends on what you want
    to confirm.
    """
    h, w = 64, 64
    pre = _make_frame(h, w, 50.0)
    post = _make_frame(h, w, 100.0)
    buffer = [pre] * 10 + [post] * 10

    std_result = confirm_event_by_plateau_shift(
        buffer, trigger_idx=10, score_metric="std"
    )
    assert not std_result.confirmed, f"uniform under std: {std_result}"
    print(f"  uniform shift (std)   → {std_result}")

    mean_result = confirm_event_by_plateau_shift(
        buffer, trigger_idx=10, score_metric="mean"
    )
    assert mean_result.confirmed, f"uniform under mean: {mean_result}"
    print(f"  uniform shift (mean)  → {mean_result}")


def test_buffer_too_small():
    """Buffer smaller than the required window total → buffer_too_small reason."""
    h, w = 64, 64
    buffer = [_make_frame(h, w, 50.0)] * 5
    result = confirm_event_by_plateau_shift(buffer, trigger_idx=2)
    assert not result.confirmed
    assert result.reason == "buffer_too_small", result.reason
    print(f"  too-small buffer      → {result}")


def test_windows_out_of_range():
    """Trigger near a buffer edge → windows_out_of_range reason."""
    h, w = 64, 64
    buffer = [_make_frame(h, w, 50.0)] * 20
    # trigger_idx=1 → pre_start would be -6 (out of range)
    result = confirm_event_by_plateau_shift(buffer, trigger_idx=1)
    assert not result.confirmed
    assert result.reason == "windows_out_of_range", result.reason
    print(f"  edge trigger          → {result}")


def test_rgb_input_uses_green_channel():
    """RGB inputs are reduced to green channel per project convention."""
    h, w = 64, 64
    pre_rgb = np.zeros((h, w, 3), dtype=np.float32)
    pre_rgb[..., 1] = 50.0
    post_rgb = np.zeros((h, w, 3), dtype=np.float32)
    post_rgb[..., 1] = 50.0
    post_rgb[: h // 2, :, 1] = 100.0  # spatial change in green channel only
    buffer = [pre_rgb] * 10 + [post_rgb] * 10
    result = confirm_event_by_plateau_shift(buffer, trigger_idx=10)
    assert result.confirmed, f"RGB transition: {result}"
    print(f"  RGB spatial transition→ {result}")


def main():
    print("\nSmoke tests for confirm_event_by_plateau_shift:\n")
    test_bump_rejected()
    test_spatial_transition_confirmed()
    test_uniform_shift_metric_dependent()
    test_buffer_too_small()
    test_windows_out_of_range()
    test_rgb_input_uses_green_channel()
    print("\nAll tests PASSED\n")


if __name__ == "__main__":
    main()
