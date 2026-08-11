"""Tests for scripts/precheck_direct_camera.py.

Focus: the --allow-dark-frame flag (P5). Before this flag, a beamless
RHEED validation always printed 'Verdict: BLOCKED' because the mean/std
heuristic correctly flagged 'no signal' — the RIGHT signal for grow-time
runs but the WRONG signal for code-path validation. --allow-dark-frame
reclassifies the dark-frame FAIL as INFO so the verdict isn't blocked.

Testing strategy: extract the stats/verdict decision into a pure
function (``evaluate_frame_stats``) so we don't need to mock VmbCamera,
vmbpy, or the whole precheck pipeline. Also exercise --help + argparse
choices for the flag itself.

No hardware. Runs from Mac dev env:

    python -m pytest -q tests/test_precheck_direct_camera.py
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.precheck_direct_camera import (  # noqa: E402
    STATS_MEAN_MAX,
    STATS_MEAN_MIN,
    STATS_STD_MIN,
    _parse_args,
    evaluate_frame_stats,
)


# ---------------------------------------------------------------------------
# Frame fixtures — one obviously-dark, one obviously-bright, one saturated
# ---------------------------------------------------------------------------

def _dark_frame() -> np.ndarray:
    """Beamless RHEED — all zeros, tiny range. Real Jul 28 pattern."""
    f = np.zeros((492, 656, 3), dtype=np.uint8)
    # Add a couple of stray 2s to match the Jul 28 evidence (mean=0.0,
    # std=0.0, range=[0, 2]) — otherwise np.std is exactly 0 which is
    # still a dark-frame FAIL but a less realistic fixture.
    f[0, 0, 0] = 2
    f[0, 0, 1] = 1
    return f


def _bright_signal_frame() -> np.ndarray:
    """Simulated live RHEED — mean well inside sensible band, real std."""
    rng = np.random.default_rng(42)
    f = rng.integers(low=20, high=180, size=(100, 100, 3), dtype=np.uint8)
    return f


def _saturated_white_frame() -> np.ndarray:
    """Fully saturated — mean at ceiling, std ~0. Also a FAIL under default."""
    return np.full((100, 100, 3), 255, dtype=np.uint8)


# ---------------------------------------------------------------------------
# evaluate_frame_stats — pure-function tests
# ---------------------------------------------------------------------------

class EvaluateFrameStatsHeuristicTests(unittest.TestCase):
    """The heuristic itself is unchanged by --allow-dark-frame — the flag
    only reclassifies the outcome. These tests lock the heuristic."""

    def test_bright_signal_frame_passes_heuristic(self):
        r = evaluate_frame_stats(_bright_signal_frame(), allow_dark_frame=False)
        self.assertTrue(r["heuristic_ok"])
        self.assertGreater(r["mean"], STATS_MEAN_MIN)
        self.assertLess(r["mean"], STATS_MEAN_MAX)
        self.assertGreater(r["std"], STATS_STD_MIN)

    def test_dark_frame_fails_heuristic(self):
        r = evaluate_frame_stats(_dark_frame(), allow_dark_frame=False)
        self.assertFalse(r["heuristic_ok"])
        # Range info is still populated so the operator sees the actual
        # numbers even when the check reports failure.
        self.assertGreaterEqual(r["max"], 0)

    def test_saturated_frame_fails_heuristic(self):
        """mean=255 exceeds STATS_MEAN_MAX (250). Also std=0."""
        r = evaluate_frame_stats(_saturated_white_frame(), allow_dark_frame=False)
        self.assertFalse(r["heuristic_ok"])


class EvaluateFrameStatsDefaultBehaviorTests(unittest.TestCase):
    """Default (allow_dark_frame=False) — dark frame is a FAIL that blocks
    the verdict. This is the Jul 28 pre-P5 behavior, kept as a regression
    guard so the default heuristic doesn't drift silently."""

    def test_pass_frame_gets_pass_tag_and_contributes(self):
        r = evaluate_frame_stats(_bright_signal_frame(), allow_dark_frame=False)
        self.assertEqual(r["tag"], "PASS")
        self.assertTrue(r["contributes_pass"])
        # Detail line does NOT mention the flag — clean pass.
        self.assertNotIn("--allow-dark-frame", r["detail"])

    def test_dark_frame_gets_fail_tag_and_blocks_verdict(self):
        """Regression guard for the default behavior: without the flag,
        dark frame → FAIL → contributes_pass=False → verdict BLOCKED."""
        r = evaluate_frame_stats(_dark_frame(), allow_dark_frame=False)
        self.assertEqual(r["tag"], "FAIL")
        self.assertFalse(r["contributes_pass"])
        # Detail line does NOT append the flag hint under default mode —
        # only allow-dark-frame runs add that annotation.
        self.assertNotIn("--allow-dark-frame", r["detail"])


class EvaluateFrameStatsAllowDarkFrameTests(unittest.TestCase):
    """P5 semantics: --allow-dark-frame turns a dark-frame FAIL into a
    non-blocking INFO. The stats numbers are STILL printed."""

    def test_dark_frame_reclassified_as_info(self):
        """The whole point of P5 — dark frame no longer blocks verdict."""
        r = evaluate_frame_stats(_dark_frame(), allow_dark_frame=True)
        self.assertEqual(r["tag"], "INFO")
        self.assertTrue(
            r["contributes_pass"],
            "Under --allow-dark-frame, a dark frame must not block verdict.",
        )
        # But the underlying heuristic still reported failure — the
        # reclassification is UI-only; the sensor data hasn't changed.
        self.assertFalse(r["heuristic_ok"])
        # And the detail line SHOULD annotate the reclassification so
        # the operator sees why a "failing" heuristic didn't block.
        self.assertIn("--allow-dark-frame", r["detail"])

    def test_bright_frame_still_passes_under_allow_dark_frame(self):
        """The flag doesn't change the pass path — only the fail path.

        A grower running with beam on and getting a real signal should
        still see [PASS], not [INFO], regardless of the flag.
        """
        r = evaluate_frame_stats(_bright_signal_frame(), allow_dark_frame=True)
        self.assertEqual(r["tag"], "PASS")
        self.assertTrue(r["contributes_pass"])
        # Bright frames don't get the annotation — the flag only kicks
        # in when the heuristic itself failed.
        self.assertNotIn("--allow-dark-frame", r["detail"])

    def test_saturated_frame_also_reclassified_by_flag(self):
        """The flag reclassifies ANY heuristic-fail as INFO, including
        saturation (mean=255). Debatable but simpler than a saturation-
        specific carve-out — and beam-off validation of a hypothetical
        stuck-saturated camera would benefit from the same reclassification.
        """
        r = evaluate_frame_stats(_saturated_white_frame(), allow_dark_frame=True)
        self.assertEqual(r["tag"], "INFO")
        self.assertTrue(r["contributes_pass"])


class EvaluateFrameStatsAlwaysReportsNumbersTests(unittest.TestCase):
    """The stats numbers appear in the detail line regardless of tag —
    the flag hides the FAIL banner but does NOT hide the data."""

    def test_dark_frame_default_reports_numbers(self):
        r = evaluate_frame_stats(_dark_frame(), allow_dark_frame=False)
        self.assertIn("mean=", r["detail"])
        self.assertIn("std=", r["detail"])
        self.assertIn("range=", r["detail"])

    def test_dark_frame_with_flag_reports_numbers(self):
        r = evaluate_frame_stats(_dark_frame(), allow_dark_frame=True)
        self.assertIn("mean=", r["detail"])
        self.assertIn("std=", r["detail"])
        self.assertIn("range=", r["detail"])


# ---------------------------------------------------------------------------
# _parse_args — argparse plumbing for the new flag
# ---------------------------------------------------------------------------

class CliFlagTests(unittest.TestCase):

    def test_allow_dark_frame_defaults_to_false(self):
        """Regression guard: default behavior is unchanged."""
        args = _parse_args([])
        self.assertFalse(args.allow_dark_frame)

    def test_allow_dark_frame_flag_sets_true(self):
        args = _parse_args(["--allow-dark-frame"])
        self.assertTrue(args.allow_dark_frame)

    def test_flag_composes_with_access_mode(self):
        """Real-world invocation combines both flags."""
        args = _parse_args(["--access-mode", "read", "--allow-dark-frame"])
        self.assertEqual(args.access_mode, "read")
        self.assertTrue(args.allow_dark_frame)

    def test_help_mentions_dark_frame_flag(self):
        """Smoke test that --help renders the new flag."""
        with self.assertRaises(SystemExit), \
                patch.object(sys, "stdout", new_callable=io.StringIO) as buf:
            _parse_args(["--help"])
        out = buf.getvalue()
        self.assertIn("--allow-dark-frame", out)
        # And the help text names the RHEED-gun-off use case so operators
        # know when to reach for it.
        self.assertIn("beam-off", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
