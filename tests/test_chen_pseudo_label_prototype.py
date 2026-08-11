"""Unit tests for scripts/chen_pseudo_label_prototype.py.

Tests the scaffold's structural + metric-computation contracts.
Actual pseudo-labeling *improvement* is expected on the synthetic
dataset (classes are learnable), but the tests don't lock a
specific delta — that would be brittle to sklearn version drift.

Run:
    python -m pytest -q tests/test_chen_pseudo_label_prototype.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from scripts.chen_pseudo_label_prototype import (  # noqa: E402
    LABELED_TRAIN_FRAC,
    MIN_LABELED_EVENTS,
    PSEUDO_CONFIDENCE_THRESHOLD,
    _synthetic_dataset,
    format_report,
    run_pseudo_label_loop,
)
from gui.recon_labels import RECON_LABELS  # noqa: E402


class SyntheticDatasetTests(unittest.TestCase):
    """Verify synthetic dataset shape + class balance."""

    def test_dataset_shapes(self):
        X_l, y_l, X_u = _synthetic_dataset(
            n_labeled=100, n_unlabeled=200, seed=1,
        )
        self.assertEqual(X_l.shape, (100, 8))
        self.assertEqual(y_l.shape, (100,))
        self.assertEqual(X_u.shape, (200, 8))

    def test_labels_are_five_classes(self):
        _, y_l, _ = _synthetic_dataset(seed=1)
        self.assertTrue((y_l >= 0).all())
        self.assertTrue((y_l < 5).all())
        # All 5 classes should appear in a large enough sample
        # (5% HTR × 200 ≈ 10; should show up)
        for c in range(5):
            self.assertIn(c, y_l, msg=f"class {c} missing from sample")

    def test_score_columns_clipped_to_unit_range(self):
        # First 5 features are classifier_recon scores → should be
        # in [0, 1] after the clip in _synthetic_dataset.
        X_l, _, _ = _synthetic_dataset(seed=1)
        self.assertTrue((X_l[:, :5] >= 0).all())
        self.assertTrue((X_l[:, :5] <= 1).all())


class PseudoLabelLoopTests(unittest.TestCase):
    """Verify the pipeline runs + returns expected metric fields."""

    def test_loop_returns_all_metric_fields(self):
        X_l, y_l, X_u = _synthetic_dataset(seed=42)
        metrics = run_pseudo_label_loop(X_l, y_l, X_u, seed=42)
        for key in [
            "n_labeled_train", "n_labeled_test", "n_unlabeled_pool",
            "baseline_acc", "pseudo_kept", "pseudo_dropped",
            "augmented_acc", "delta_acc",
            "baseline_confusion", "augmented_confusion",
            "baseline_report", "augmented_report",
        ]:
            self.assertIn(key, metrics, msg=f"missing metric: {key}")

    def test_accuracies_in_valid_range(self):
        X_l, y_l, X_u = _synthetic_dataset(seed=42)
        metrics = run_pseudo_label_loop(X_l, y_l, X_u, seed=42)
        for acc_key in ("baseline_acc", "augmented_acc"):
            self.assertGreaterEqual(metrics[acc_key], 0.0)
            self.assertLessEqual(metrics[acc_key], 1.0)

    def test_pseudo_kept_within_pool(self):
        X_l, y_l, X_u = _synthetic_dataset(seed=42)
        metrics = run_pseudo_label_loop(X_l, y_l, X_u, seed=42)
        self.assertLessEqual(
            metrics["pseudo_kept"] + metrics["pseudo_dropped"],
            metrics["n_unlabeled_pool"],
        )

    def test_empty_unlabeled_pool_handled(self):
        # If no unlabeled data, augmented model = baseline model
        # (train set unchanged; delta should be exactly 0).
        X_l, y_l, _ = _synthetic_dataset(
            n_labeled=100, n_unlabeled=0, seed=42,
        )
        metrics = run_pseudo_label_loop(
            X_l, y_l, np.zeros((0, 8)), seed=42,
        )
        self.assertEqual(metrics["pseudo_kept"], 0)
        # Same seed + same train fold + no pseudo → same augmented
        # accuracy as baseline (both trained on identical data)
        self.assertAlmostEqual(
            metrics["baseline_acc"], metrics["augmented_acc"],
            places=6,
        )


class ReportFormatTests(unittest.TestCase):
    """Verify format_report produces a well-formed markdown report."""

    def test_report_includes_headline_metrics(self):
        X_l, y_l, X_u = _synthetic_dataset(seed=42)
        metrics = run_pseudo_label_loop(X_l, y_l, X_u, seed=42)
        report = format_report(metrics, "synthetic", "test")
        self.assertIn("Chen 2025 pseudo-labeling prototype", report)
        self.assertIn(f"{metrics['baseline_acc']:.3f}", report)
        self.assertIn(f"{metrics['augmented_acc']:.3f}", report)
        # Confusion tables include all 5 class labels
        for class_label in RECON_LABELS:
            self.assertIn(class_label, report)


class ConstantsTests(unittest.TestCase):
    """Guard the tuning constants against unintended changes."""

    def test_labeled_train_frac_in_reasonable_range(self):
        # Chen 2025's 10-40% sweet spot. Our default should sit there.
        self.assertGreaterEqual(LABELED_TRAIN_FRAC, 0.10)
        self.assertLessEqual(LABELED_TRAIN_FRAC, 0.40)

    def test_pseudo_threshold_is_confident(self):
        # Chen 2025 used 0.9; we use lower for small-dataset viability
        # but should still be a "confident" threshold (≥ 0.5).
        self.assertGreaterEqual(PSEUDO_CONFIDENCE_THRESHOLD, 0.5)
        self.assertLessEqual(PSEUDO_CONFIDENCE_THRESHOLD, 0.95)

    def test_min_labeled_events_prevents_spurious_reports(self):
        # If MIN_LABELED_EVENTS is too low, we'd publish nonsense
        # accuracy from a 5-sample training set. Should be at least 20.
        self.assertGreaterEqual(MIN_LABELED_EVENTS, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
