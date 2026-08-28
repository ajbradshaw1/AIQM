"""Tests for offline O-MBE RHEED signal validation."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.analyze_ombe_rheed_signals import (
    analyze_condition,
    build_summary,
    discover_frames,
    frame_metrics,
    load_wgc_provenance,
    parse_input,
)


def _frame(level: int, spot_level: int) -> np.ndarray:
    frame = np.full((80, 120, 3), level, dtype=np.uint8)
    yy, xx = np.ogrid[:80, :120]
    spot = (xx - 60) ** 2 + (yy - 35) ** 2 < 10 ** 2
    frame[spot, 1] = spot_level
    return frame


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wgc_corpus(directory: Path, count: int = 45, save_every: int = 1):
    metadata = []
    hashes = []
    for index in range(count):
        image = _frame(10, 200 + index % 2)
        for suffix in ("raw", "cropped"):
            path = directory / f"sample_{index:05d}_{suffix}.png"
            Image.fromarray(image).save(path)
            hashes.append({
                "path": path.name,
                "sha256": _sha(path),
                "bytes": path.stat().st_size,
            })
        metadata.append({
            "read_index": index,
            "capture_backend": "wgc",
            "captured_at_utc": f"2026-07-27T12:00:{index:02d}.000Z",
            "captured_monotonic_ns": 1_000_000_000 + index * 1_000_000_000,
            "capture_gap_ms": None if index == 0 else 1000.0,
            "capture_sequence": 1000 + index,
            "frame_age_ms": 5.0,
            "source_hwnd": 9001,
        })
    (directory / "capture_metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8",
    )
    (directory / "sha256_manifest.json").write_text(
        json.dumps(hashes), encoding="utf-8",
    )
    (directory / "summary.json").write_text(json.dumps({
        "git_commit": "capture-commit",
        "configuration": {
            "capture_backend": "wgc",
            "save_every": save_every,
            "interval_s": 1.0,
        },
    }), encoding="utf-8")


class MetricTests(unittest.TestCase):
    def test_metrics_locate_bright_spot_and_roi(self):
        metrics = frame_metrics(_frame(10, 220))
        self.assertAlmostEqual(metrics["specular_x"], 60, delta=4)
        self.assertAlmostEqual(metrics["specular_y"], 35, delta=4)
        self.assertGreater(
            metrics["specular_roi_green_mean"],
            metrics["green_full_mean"],
        )


class DirectoryTests(unittest.TestCase):
    def test_provenance_discovery_and_full_policy_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_wgc_corpus(directory)
            frames = discover_frames(directory)
            self.assertEqual(len(frames), 45)
            provenance, identity = load_wgc_provenance(directory, frames)
            self.assertEqual(provenance[0]["capture_sequence"], 1000)
            self.assertEqual(identity["capture_commit"], "capture-commit")

            rows, manifest, identity = analyze_condition("stable", directory)
            self.assertEqual(len(rows), 45)
            self.assertEqual(len(manifest), 90)
            self.assertTrue(all(
                row["capture_backend"] == "wgc" for row in rows
            ))
            self.assertTrue(all(
                "gui_effective_threshold" in row for row in rows
            ))
            summary = build_summary(rows, {"stable"})
            self.assertIsNotNone(summary["stable_change_score_p99"])
            self.assertIsNotNone(summary["diagnostic_candidate_threshold"])
            self.assertEqual(
                summary["conditions"]["stable"]["gui_event_count"], 0,
            )
            self.assertIn("debounce_frames", summary["current_gui_policy"])

    def test_sparse_probe_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_wgc_corpus(directory, count=2, save_every=10)
            with self.assertRaisesRegex(ValueError, "--save-every 1"):
                load_wgc_provenance(directory, discover_frames(directory))

    def test_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_wgc_corpus(directory, count=2)
            (directory / "sample_00000_raw.png").write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "hash"):
                load_wgc_provenance(directory, discover_frames(directory))

    def test_parse_input_rejects_missing_condition(self):
        with self.assertRaises(ValueError):
            parse_input("missing-separator")


if __name__ == "__main__":
    unittest.main(verbosity=2)
