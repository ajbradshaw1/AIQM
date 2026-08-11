"""Unit tests for scripts/build_cs_dataset.py.

Covers the catalog scanner + quality-flag assessor + tar bundler +
CLI. All tests use synthetic session dirs — no dependency on
logs/growths/ or archived Bulbasaur data.

Run:
    python -m pytest -q tests/test_build_cs_dataset.py
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.build_cs_dataset import (  # noqa: E402
    LONG_DURATION_MIN_SECONDS,
    STARTUP_TEST_MAX_SECONDS,
    assess_quality,
    bundle_sessions,
    catalog_session,
    scan_catalog,
    write_catalog_json,
)
from scripts.growth_profile_explorer import SessionArtifacts  # noqa: E402


def _write_csv(path: Path, fieldnames: list[str],
               rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _make_session(logs_root: Path, name: str,
                   sensor_rows: int = 10,
                   commit_rows: int = 0,
                   label_rows: int = 0,
                   auto_capture_rows: int = 0,
                   manual_event_rows: int = 0,
                   include_classifier: bool = False,
                   grower: str = "TEST",
                   sample_id: str = "SAMP",
                   temp_variance: float = 5.0) -> Path:
    """Helper: build a synthetic session dir with the requested shape."""
    session_dir = logs_root / name
    session_dir.mkdir(parents=True)

    if sensor_rows:
        _write_csv(
            session_dir / "sensor_log.csv",
            ["timestamp", "elapsed_s", "pyrometer_temp_C",
             "pyrometer_temp_std_C"],
            [
                {"timestamp": f"2026-07-14T10:00:{i:02d}",
                 "elapsed_s": str(15.0 * i),
                 "pyrometer_temp_C": str(
                     500.0 + (temp_variance * (i % 3) / 2)
                 ),
                 "pyrometer_temp_std_C": "1.0"}
                for i in range(sensor_rows)
            ],
        )
    if commit_rows:
        fields = ["timestamp", "elapsed_s", "sample_id", "grower",
                  "grower_corrected"]
        if include_classifier:
            fields += [f"classifier_recon_{c}" for c in [
                "1x1", "Twinned (2x1)", "c(6x2)", "rt13xrt13", "HTR",
            ]]
        rows = []
        for i in range(commit_rows):
            row = {
                "timestamp": f"2026-07-14T10:{i:02d}:00",
                "elapsed_s": str(30.0 + i * 10),
                "sample_id": sample_id,
                "grower": grower,
                "grower_corrected": "False",
            }
            if include_classifier:
                for c in ["1x1", "Twinned (2x1)", "c(6x2)",
                          "rt13xrt13", "HTR"]:
                    row[f"classifier_recon_{c}"] = "20"
            rows.append(row)
        _write_csv(session_dir / "commit_log.csv", fields, rows)
    if label_rows:
        _write_csv(
            session_dir / "events_labels.csv",
            ["event_idx", "primary_reconstruction", "notes"],
            [{"event_idx": str(i), "primary_reconstruction": "1x1",
              "notes": ""} for i in range(label_rows)],
        )
    if auto_capture_rows:
        _write_csv(
            session_dir / "auto_capture_events.csv",
            ["timestamp", "elapsed_s", "event_idx", "change_score",
             "event_state"],
            [{"timestamp": f"2026-07-14T10:00:{i:02d}",
              "elapsed_s": str(45.0 + i),
              "event_idx": str(i), "change_score": "0.5",
              "event_state": "kept_default"}
             for i in range(auto_capture_rows)],
        )
    if manual_event_rows:
        _write_csv(
            session_dir / "manual_events.csv",
            ["timestamp", "elapsed_s", "event_idx"],
            [{"timestamp": "T", "elapsed_s": str(60.0 + i),
              "event_idx": str(i)}
             for i in range(manual_event_rows)],
        )
    return session_dir


class CatalogSessionTests(unittest.TestCase):
    """catalog_session() produces correct counts + duration + IDs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_and_duration_extracted(self):
        s = _make_session(
            self.logs, "growth_TEST_20260714_100000",
            sensor_rows=20, commit_rows=3, label_rows=2,
            auto_capture_rows=5, manual_event_rows=1,
            include_classifier=True,
        )
        entry = catalog_session(s)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.counts["sensor_rows"], 20)
        self.assertEqual(entry.counts["commit_rows"], 3)
        self.assertEqual(entry.counts["event_label_rows"], 2)
        self.assertEqual(entry.counts["auto_capture_rows"], 5)
        self.assertEqual(entry.counts["manual_event_rows"], 1)
        self.assertEqual(entry.counts["rheed_view_event_rows"], 0)
        # Last elapsed_s: 15 * 19 = 285
        self.assertEqual(entry.duration_s, 285.0)
        self.assertTrue(entry.has_classifier_data)
        self.assertTrue(entry.has_grower_labels)

    def test_sample_id_and_grower_from_first_commit(self):
        _make_session(
            self.logs, "growth_STO_20260714_100000",
            sensor_rows=5, commit_rows=1,
            grower="Jiangang", sample_id="STO_1",
        )
        entry = catalog_session(
            self.logs / "growth_STO_20260714_100000",
        )
        self.assertEqual(entry.sample_id, "STO_1")
        self.assertEqual(entry.grower, "Jiangang")

    def test_malformed_session_returns_none(self):
        # Pass a nonexistent path — SessionArtifacts.from_session_dir
        # raises FileNotFoundError which catalog_session catches.
        result = catalog_session(self.logs / "does-not-exist")
        self.assertIsNone(result)

    def test_malformed_rheed_view_csv_returns_none(self):
        session = _make_session(
            self.logs, "growth_BAD_VIEW_CSV", sensor_rows=2
        )
        (session / "rheed_view_events.csv").write_bytes(b"\xff\xfe\xfa")
        self.assertIsNone(catalog_session(session))


class QualityFlagTests(unittest.TestCase):
    """assess_quality() produces the documented flag set."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _artifacts(self, name: str, **kwargs) -> SessionArtifacts:
        _make_session(self.logs, name, **kwargs)
        return SessionArtifacts.from_session_dir(self.logs / name)

    def test_empty_flag_when_no_data(self):
        art = self._artifacts("growth_EMPTY_1", sensor_rows=0)
        flags = assess_quality(art, 0.0)
        self.assertIn("empty", flags)
        # empty short-circuits — other flags shouldn't fire
        self.assertNotIn("startup_test", flags)
        self.assertNotIn("real_growth", flags)

    def test_startup_test_flag_when_short_and_no_engagement(self):
        art = self._artifacts("growth_SHORT_1", sensor_rows=3)
        # 3 rows * 15s = 30s elapsed (< STARTUP_TEST_MAX_SECONDS 120)
        # No commits, no manual events — no grower engagement
        flags = assess_quality(art, 30.0)
        self.assertIn("startup_test", flags)
        self.assertNotIn("real_growth", flags)

    def test_real_growth_flag_on_long_or_engaged_session(self):
        # Long-only (no engagement): real_growth still fires
        art = self._artifacts("growth_LONG_1", sensor_rows=20)
        flags = assess_quality(art, 300.0)
        self.assertIn("real_growth", flags)
        # Short but engaged: also real_growth
        art2 = self._artifacts(
            "growth_SHORT_ENGAGED", sensor_rows=3, commit_rows=1,
        )
        flags2 = assess_quality(art2, 30.0)
        self.assertIn("real_growth", flags2)

    def test_long_duration_threshold(self):
        art = self._artifacts("growth_VLONG", sensor_rows=50)
        # Just under 10min
        flags = assess_quality(art, LONG_DURATION_MIN_SECONDS - 1)
        self.assertNotIn("long_duration", flags)
        # Just at 10min
        flags = assess_quality(art, LONG_DURATION_MIN_SECONDS)
        self.assertIn("long_duration", flags)

    def test_has_labels_from_events_labels(self):
        art = self._artifacts(
            "growth_LABELED_1", sensor_rows=5, label_rows=2,
        )
        flags = assess_quality(art, 75.0)
        self.assertIn("has_labels", flags)

    def test_has_classifier_data_flag(self):
        art = self._artifacts(
            "growth_CLASS_1", sensor_rows=5, commit_rows=2,
            include_classifier=True,
        )
        flags = assess_quality(art, 75.0)
        self.assertIn("has_classifier_data", flags)
        # Without classifier columns → flag absent
        art2 = self._artifacts(
            "growth_NOCLASS", sensor_rows=5, commit_rows=2,
            include_classifier=False,
        )
        flags2 = assess_quality(art2, 75.0)
        self.assertNotIn("has_classifier_data", flags2)

    def test_dummy_flag_on_flat_sensor_readings(self):
        # temp_variance=0 → all readings identical → flat → dummy
        art = self._artifacts(
            "growth_DUMMY", sensor_rows=10, temp_variance=0.0,
        )
        flags = assess_quality(art, 150.0)
        self.assertIn("dummy", flags)


class ScanCatalogTests(unittest.TestCase):
    """scan_catalog() walks the logs root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_growth_prefixed_dirs_included(self):
        _make_session(self.logs, "growth_A_1", sensor_rows=5)
        _make_session(self.logs, "growth_B_1", sensor_rows=5)
        # Non-growth dir should be skipped
        (self.logs / "not_a_session").mkdir()
        (self.logs / "not_a_session" / "sensor_log.csv").write_text(
            "elapsed_s,pyrometer_temp_C\n10,500\n",
        )
        entries = scan_catalog(self.logs)
        ids = {e.session_id for e in entries}
        self.assertEqual(ids, {"growth_A_1", "growth_B_1"})

    def test_missing_root_raises_fnf(self):
        with self.assertRaises(FileNotFoundError):
            scan_catalog(self.logs / "does-not-exist")


class BundleSessionsTests(unittest.TestCase):
    """bundle_sessions() writes a valid .tar.gz with expected layout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name) / "growths"
        self.logs.mkdir()
        self.out_dir = Path(self.tmp.name) / "out"
        self.out_dir.mkdir()
        session_one = _make_session(
            self.logs, "growth_TEST_1", sensor_rows=10,
            commit_rows=2, label_rows=1, include_classifier=True,
        )
        _write_csv(
            session_one / "rheed_view_events.csv",
            ["timestamp", "elapsed_s", "event_idx", "event_type", "frame_path"],
            [{
                "timestamp": "2026-07-29T12:00:00",
                "elapsed_s": "0.0",
                "event_idx": "1",
                "event_type": "session_start",
                "frame_path": "",
            }],
        )
        _make_session(
            self.logs, "growth_TEST_2", sensor_rows=15,
            commit_rows=3, auto_capture_rows=4,
        )
        self.entries = scan_catalog(self.logs)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tar_created_with_expected_structure(self):
        out_tar = self.out_dir / "bundle.tar.gz"
        result = bundle_sessions(
            self.entries, self.logs, out_tar,
            include_frames=False,
        )
        self.assertEqual(result, out_tar)
        self.assertTrue(out_tar.exists())
        self.assertGreater(out_tar.stat().st_size, 500)

        with tarfile.open(out_tar) as tar:
            names = tar.getnames()
            readme = tar.extractfile("bundle/README.md").read().decode(
                "utf-8"
            )
        # Root files
        self.assertIn("bundle/catalog.json", names)
        self.assertIn("bundle/schema.md", names)
        self.assertIn("bundle/README.md", names)
        # Session subdirs
        self.assertIn(
            "bundle/sessions/growth_TEST_1/sensor_log.csv", names,
        )
        self.assertIn(
            "bundle/sessions/growth_TEST_1/commit_log.csv", names,
        )
        self.assertIn(
            "bundle/sessions/growth_TEST_1/events_labels.csv", names,
        )
        self.assertIn(
            "bundle/sessions/growth_TEST_1/rheed_view_events.csv", names,
        )
        self.assertIn("rheed_view_events", readme)
        # Per-session meta + frame manifest
        self.assertIn(
            "bundle/sessions/growth_TEST_1/session_meta.json", names,
        )
        self.assertIn(
            "bundle/sessions/growth_TEST_1/frame_manifest.json", names,
        )
        # No frames/ dir when include_frames=False
        self.assertFalse(any("/frames/" in n for n in names))

    def test_catalog_json_matches_sessions_included(self):
        out_tar = self.out_dir / "bundle2.tar.gz"
        bundle_sessions(self.entries, self.logs, out_tar)
        # Extract catalog.json and verify
        with tarfile.open(out_tar) as tar:
            member = tar.getmember("bundle2/catalog.json")
            catalog_bytes = tar.extractfile(member).read()
        catalog = json.loads(catalog_bytes)
        self.assertEqual(catalog["session_count"], 2)
        ids = {s["session_id"] for s in catalog["sessions"]}
        self.assertEqual(ids, {"growth_TEST_1", "growth_TEST_2"})
        first = next(
            item for item in catalog["sessions"]
            if item["session_id"] == "growth_TEST_1"
        )
        self.assertEqual(first["counts"]["rheed_view_event_rows"], 1)


class CliSmokeTests(unittest.TestCase):
    """Subprocess-level CLI exercise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name) / "growths"
        self.logs.mkdir()
        self.out_dir = Path(self.tmp.name) / "out"
        self.out_dir.mkdir()
        _make_session(
            self.logs, "growth_CLI_1", sensor_rows=10, commit_rows=2,
        )
        self.script = REPO_ROOT / "scripts" / "build_cs_dataset.py"

    def tearDown(self):
        self.tmp.cleanup()

    def test_catalog_only_writes_no_tar(self):
        result = subprocess.run(
            [
                sys.executable, str(self.script),
                "--logs-root", str(self.logs),
                "--output-dir", str(self.out_dir),
                "--catalog-only",
            ],
            capture_output=True, text=True,
            env={**os.environ, "MPLBACKEND": "Agg"},
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"stderr: {result.stderr}",
        )
        self.assertTrue((self.out_dir / "catalog.json").exists())
        # No tar.gz produced in --catalog-only mode
        tars = list(self.out_dir.glob("*.tar.gz"))
        self.assertEqual(len(tars), 0)

    def test_missing_logs_root_exits_1(self):
        result = subprocess.run(
            [
                sys.executable, str(self.script),
                "--logs-root", "/tmp/does-not-exist-987",
                "--output-dir", str(self.out_dir),
                "--catalog-only",
            ],
            capture_output=True, text=True,
            env={**os.environ, "MPLBACKEND": "Agg"},
        )
        self.assertEqual(result.returncode, 1)

    def test_full_bundle_writes_tar_and_catalog(self):
        result = subprocess.run(
            [
                sys.executable, str(self.script),
                "--logs-root", str(self.logs),
                "--output-dir", str(self.out_dir),
                "--bundle-name", "cli_test_bundle",
            ],
            capture_output=True, text=True,
            env={**os.environ, "MPLBACKEND": "Agg"},
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"stderr: {result.stderr}",
        )
        self.assertTrue((self.out_dir / "catalog.json").exists())
        tar_path = self.out_dir / "cli_test_bundle.tar.gz"
        self.assertTrue(tar_path.exists())

    def test_exclude_quality_filter(self):
        # Session shape produces "startup_test" flag (10 rows * 15s
        # = 150s = 2.5min, so above 2min → real_growth). To get
        # startup_test, need to make it short AND unengaged. This
        # test verifies the CLI honors --exclude-quality regardless
        # of exact flags present.
        _make_session(
            self.logs, "growth_EMPTY_TEST",
            sensor_rows=0,
        )
        result = subprocess.run(
            [
                sys.executable, str(self.script),
                "--logs-root", str(self.logs),
                "--output-dir", str(self.out_dir),
                "--catalog-only",
                "--exclude-quality", "empty",
            ],
            capture_output=True, text=True,
            env={**os.environ, "MPLBACKEND": "Agg"},
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"stderr: {result.stderr}",
        )
        catalog = json.loads(
            (self.out_dir / "catalog.json").read_text(),
        )
        # growth_EMPTY_TEST (flagged empty) should be excluded;
        # growth_CLI_1 should remain
        ids = {s["session_id"] for s in catalog["sessions"]}
        self.assertNotIn("growth_EMPTY_TEST", ids)
        self.assertIn("growth_CLI_1", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
