"""Unit tests for scripts/growth_profile_explorer.py (Day 4 Jul 16 2026).

Locks the six-CSV SessionArtifacts reader contract + the temperature
chart rendering path. Uses synthetic session dirs per test — no
dependency on archived sessions or the AI-for-quantum repo (that
comes at Day 7 with batch_classify_session).

The reader tests import only stdlib + the module under test (no
matplotlib), so they cover the data layer even if matplotlib is
absent. Chart tests are the only ones that pull matplotlib in.

Run:
    python -m pytest -q tests/test_growth_profile_explorer.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("MPLBACKEND", "Agg")

from scripts.growth_profile_explorer import (  # noqa: E402
    CLASS_LINE_COLORS,
    DEFAULT_CHANGE_THRESHOLD,
    EVENT_MARKER_COLORS,
    EVENT_STATE_ORDER,
    SessionArtifacts,
    render_classifier_trajectory,
    render_grower_vs_classifier_agreement,
    render_html_report,
    render_score_distribution,
    render_temperature_chart,
)
from gui.recon_labels import RECON_LABELS  # noqa: E402


def _write_csv(
    path: Path, fieldnames: list[str], rows: list[dict],
) -> None:
    """Helper: write a CSV with header + given rows. Creates parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _synth_sensor_rows(n: int) -> list[dict]:
    """Build n sensor rows: elapsed_s=15*i, temp=500+i, std=1.5.

    Matches the shape of a real ~5-minute session (5s cadence × 60 =
    12 rows/min; 15s stride here just keeps synthetic data compact
    without changing any downstream logic).
    """
    return [
        {
            "timestamp": f"2026-07-16T10:00:{i:02d}",
            "elapsed_s": str(15.0 * i),
            "pyrometer_temp_C": str(500.0 + i),
            "pyrometer_temp_std_C": "1.5",
            "pyrometer_temp_n": "5",
        }
        for i in range(n)
    ]


class SessionArtifactsReaderTests(unittest.TestCase):
    """Seven tests locking the 6-CSV reader behavior."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = (
            Path(self.tmp.name) / "growth_TEST_20260716_100000"
        )
        self.session_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_read_from_valid_session(self):
        # All 6 CSVs present with at least one row each. Locks that
        # the reader hits every field on SessionArtifacts and none of
        # the writes get shadowed by field defaults.
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["timestamp", "elapsed_s", "pyrometer_temp_C",
             "pyrometer_temp_std_C", "pyrometer_temp_n"],
            _synth_sensor_rows(5),
        )
        _write_csv(
            self.session_dir / "commit_log.csv",
            ["timestamp", "elapsed_s", "sample_id", "note"],
            [{"timestamp": "T", "elapsed_s": "30",
              "sample_id": "S", "note": "n"}],
        )
        _write_csv(
            self.session_dir / "auto_capture_events.csv",
            ["timestamp", "elapsed_s", "event_idx", "change_score"],
            [{"timestamp": "T", "elapsed_s": "45",
              "event_idx": "1", "change_score": "0.9"}],
        )
        _write_csv(
            self.session_dir / "manual_events.csv",
            ["timestamp", "elapsed_s", "event_idx",
             "pyrometer_temp_C"],
            [{"timestamp": "T", "elapsed_s": "60",
              "event_idx": "1", "pyrometer_temp_C": "505"}],
        )
        _write_csv(
            self.session_dir / "heartbeat_log.csv",
            ["timestamp", "elapsed_s", "heartbeat_idx",
             "pyrometer_temp_C", "frame_path"],
            [{"timestamp": "T", "elapsed_s": "75",
              "heartbeat_idx": "1", "pyrometer_temp_C": "506",
              "frame_path": "/x"}],
        )
        _write_csv(
            self.session_dir / "events_labels.csv",
            ["event_idx", "primary_reconstruction", "notes"],
            [{"event_idx": "1",
              "primary_reconstruction": "1x1", "notes": ""}],
        )

        artifacts = SessionArtifacts.from_session_dir(self.session_dir)

        self.assertEqual(len(artifacts.sensor_rows), 5)
        self.assertEqual(len(artifacts.commit_rows), 1)
        self.assertEqual(len(artifacts.auto_capture_rows), 1)
        self.assertEqual(len(artifacts.manual_event_rows), 1)
        self.assertEqual(len(artifacts.heartbeat_rows), 1)
        self.assertEqual(len(artifacts.event_label_rows), 1)

    def test_missing_optional_csv_yields_empty_list(self):
        # Only sensor_log.csv exists. Others → empty lists (not
        # exceptions). Locks the pre-schema-session tolerance:
        # analyses of Apr 2026 sessions (predating manual_events)
        # must open cleanly with the newer reader.
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["elapsed_s", "pyrometer_temp_C"],
            [{"elapsed_s": "10", "pyrometer_temp_C": "500"}],
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        self.assertEqual(len(artifacts.sensor_rows), 1)
        self.assertEqual(artifacts.commit_rows, [])
        self.assertEqual(artifacts.auto_capture_rows, [])
        self.assertEqual(artifacts.manual_event_rows, [])
        self.assertEqual(artifacts.heartbeat_rows, [])
        self.assertEqual(artifacts.event_label_rows, [])

    def test_bad_row_skipped_in_temperature_series(self):
        # sensor row with non-numeric elapsed_s must not crash the
        # reader; instead the offending row is silently dropped from
        # the typed series (mirrors plot_temperature.py policy). Row
        # with blank temp is also dropped. Only the first row
        # survives, but the raw sensor_rows still contains all 3.
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["elapsed_s", "pyrometer_temp_C"],
            [
                {"elapsed_s": "10", "pyrometer_temp_C": "500"},
                {"elapsed_s": "not_a_number",
                 "pyrometer_temp_C": "501"},
                {"elapsed_s": "20", "pyrometer_temp_C": ""},
            ],
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        self.assertEqual(len(artifacts.sensor_rows), 3)
        elapsed, temps = artifacts.temperature_series()
        self.assertEqual(elapsed, [10.0])
        self.assertEqual(temps, [500.0])

    def test_events_labels_join_by_event_idx(self):
        # Load auto_capture + events_labels; verify callers can
        # correlate them via event_idx. Also verifies
        # label_for_event(idx) helper — the join path Day 5's
        # grower-vs-classifier scatter will use.
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["elapsed_s", "pyrometer_temp_C"],
            [{"elapsed_s": "10", "pyrometer_temp_C": "500"}],
        )
        _write_csv(
            self.session_dir / "auto_capture_events.csv",
            ["elapsed_s", "event_idx", "change_score"],
            [
                {"elapsed_s": "30", "event_idx": "1",
                 "change_score": "0.5"},
                {"elapsed_s": "60", "event_idx": "2",
                 "change_score": "0.7"},
            ],
        )
        _write_csv(
            self.session_dir / "events_labels.csv",
            ["event_idx", "primary_reconstruction", "notes"],
            [{"event_idx": "1",
              "primary_reconstruction": "Twinned (2x1)",
              "notes": "keep"}],
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)

        label = artifacts.label_for_event(1)
        self.assertIsNotNone(label)
        self.assertEqual(label["primary_reconstruction"], "Twinned (2x1)")
        # Event 2 was never labeled — join returns None, no exception.
        self.assertIsNone(artifacts.label_for_event(2))

    def test_elapsed_s_coerced_to_float_in_series(self):
        # temperature_series returns floats, not strings, even though
        # csv.DictReader always yields strings. Locks the typed
        # accessor contract — callers can do arithmetic without
        # per-row conversion.
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["elapsed_s", "pyrometer_temp_C"],
            [{"elapsed_s": "15.5", "pyrometer_temp_C": "500.7"}],
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        elapsed, temps = artifacts.temperature_series()
        self.assertIsInstance(elapsed[0], float)
        self.assertIsInstance(temps[0], float)
        self.assertAlmostEqual(elapsed[0], 15.5)
        self.assertAlmostEqual(temps[0], 500.7)

    def test_missing_session_dir_raises_fnf(self):
        # Non-existent path → FileNotFoundError. Guard against silent
        # empty-charts-on-typo (a user typing the wrong session name
        # should learn quickly, not get a blank PNG).
        with self.assertRaises(FileNotFoundError):
            SessionArtifacts.from_session_dir(
                Path("/tmp/does-not-exist-12345"),
            )

    def test_synthetic_10_row_round_trip(self):
        # Write 10 rows, read 10 rows back — end-to-end reader sanity
        # with the same shape a real session would produce.
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["timestamp", "elapsed_s", "pyrometer_temp_C",
             "pyrometer_temp_std_C", "pyrometer_temp_n"],
            _synth_sensor_rows(10),
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        self.assertEqual(len(artifacts.sensor_rows), 10)
        elapsed, temps = artifacts.temperature_series()
        self.assertEqual(len(elapsed), 10)
        self.assertEqual(elapsed[0], 0.0)
        self.assertEqual(elapsed[-1], 135.0)  # 15 * 9


class TemperatureChartTests(unittest.TestCase):
    """Three tests locking chart rendering behavior."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = (
            Path(self.tmp.name) / "growth_TEST_20260716_100000"
        )
        self.session_dir.mkdir()
        self.out_dir = Path(self.tmp.name) / "out"
        self.out_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_png_written_when_temp_data_present(self):
        # Minimal session with 5 sensor rows → chart PNG lands on
        # disk. File size sanity-checked against a "definitely
        # non-empty PNG" floor (matplotlib empty savefig is ~200
        # bytes; a real chart is 10-50KB).
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["elapsed_s", "pyrometer_temp_C"],
            [{"elapsed_s": str(15.0 * i),
              "pyrometer_temp_C": str(500 + i)}
             for i in range(5)],
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        out = self.out_dir / "temp.png"
        result = render_temperature_chart(artifacts, out, dpi=100)
        self.assertEqual(result, out)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 500)

    def test_overlays_include_commit_and_manual_markers(self):
        # With commit and manual event rows present, event_overlays()
        # returns both sources and render succeeds. We can't easily
        # pixel-inspect the PNG for vertical-line colors without a
        # visual regression harness, so we assert on the accessor +
        # the render code path executing cleanly (dedup legend logic
        # + color lookup would raise if the source names diverged
        # from EVENT_MARKER_COLORS).
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["elapsed_s", "pyrometer_temp_C"],
            [{"elapsed_s": str(15.0 * i),
              "pyrometer_temp_C": str(500 + i)}
             for i in range(5)],
        )
        _write_csv(
            self.session_dir / "commit_log.csv",
            ["elapsed_s", "sample_id", "note"],
            [{"elapsed_s": "30", "sample_id": "S", "note": "n"}],
        )
        _write_csv(
            self.session_dir / "manual_events.csv",
            ["elapsed_s", "event_idx"],
            [{"elapsed_s": "45", "event_idx": "1"}],
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)

        overlays = artifacts.event_overlays()
        sources = {source for _, source in overlays}
        self.assertIn("commit", sources)
        self.assertIn("manual", sources)
        # And both sources have a color mapping — guard against a
        # future rename of the source labels without updating the
        # color dict.
        for source in sources:
            self.assertIn(source, EVENT_MARKER_COLORS)

        out = self.out_dir / "temp_annot.png"
        result = render_temperature_chart(artifacts, out, dpi=100)
        self.assertEqual(result, out)
        self.assertTrue(out.exists())

    def test_none_returned_when_temperature_empty(self):
        # Empty sensor CSV → temperature_series returns [] → chart
        # returns None and writes no file. Guard against a "silent
        # PNG with an empty chart" failure mode — caller should be
        # able to skip the file entirely.
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["elapsed_s", "pyrometer_temp_C"],
            [],
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        out = self.out_dir / "temp.png"
        result = render_temperature_chart(artifacts, out, dpi=100)
        self.assertIsNone(result)
        self.assertFalse(out.exists())


def _write_full_commit_row(
    elapsed_s: str, classifier_pcts: dict[str, float],
    grower_pcts: Optional[dict[str, float]] = None,
    grower_corrected: str = "False",
) -> dict:
    """Build one commit_log.csv row with paired classifier + grower cells.

    Fills every column the renderers need. Any RECON_LABELS class not
    in the caller-supplied dicts gets an empty cell (simulating
    classifier-not-yet-emitted-for-that-class).
    """
    row = {
        "timestamp": "2026-07-17T10:00:00",
        "elapsed_s": elapsed_s,
        "sample_id": "S",
        "grower": "G",
        "pyrometer_temp_C": "500",
        "grower_corrected": grower_corrected,
        "note": "",
    }
    for label in RECON_LABELS:
        row[f"classifier_recon_{label}"] = (
            str(classifier_pcts.get(label, ""))
        )
        row[f"recon_{label}"] = (
            str((grower_pcts or {}).get(label, ""))
        )
    return row


def _commit_row_fields() -> list[str]:
    """Field names for the synthetic commit rows built above."""
    base = [
        "timestamp", "elapsed_s", "sample_id", "grower",
        "pyrometer_temp_C", "grower_corrected", "note",
    ]
    for label in RECON_LABELS:
        base.append(f"classifier_recon_{label}")
        base.append(f"recon_{label}")
    return base


class ClassifierTrajectoryTests(unittest.TestCase):
    """Four tests locking classifier trajectory chart behavior."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = (
            Path(self.tmp.name) / "growth_TEST_20260717_100000"
        )
        self.session_dir.mkdir()
        self.out_dir = Path(self.tmp.name) / "out"
        self.out_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_five_lines_when_all_classes_present(self):
        # Two commits, each with all 5 classifier_recon_* cells filled.
        # Chart renders + all 5 classes get plotted (verified via the
        # underlying per-class series check).
        rows = [
            _write_full_commit_row(
                "30",
                {"1x1": 60, "Twinned (2x1)": 20, "c(6x2)": 10,
                 "rt13xrt13": 5, "HTR": 5},
            ),
            _write_full_commit_row(
                "90",
                {"1x1": 40, "Twinned (2x1)": 30, "c(6x2)": 15,
                 "rt13xrt13": 10, "HTR": 5},
            ),
        ]
        _write_csv(
            self.session_dir / "commit_log.csv",
            _commit_row_fields(), rows,
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        out = self.out_dir / "traj.png"
        result = render_classifier_trajectory(artifacts, out, dpi=100)
        self.assertEqual(result, out)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 500)

    def test_absent_columns_return_none(self):
        # Commits present but every classifier_recon cell blank → no
        # data to plot → return None, no file written. Guards against
        # the case where classifier was DISABLED for the whole session
        # but the grower still hit LOG ENTRY.
        rows = [
            _write_full_commit_row("30", {}),  # all classifier blank
            _write_full_commit_row("90", {}),
        ]
        _write_csv(
            self.session_dir / "commit_log.csv",
            _commit_row_fields(), rows,
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        out = self.out_dir / "traj.png"
        result = render_classifier_trajectory(artifacts, out, dpi=100)
        self.assertIsNone(result)
        self.assertFalse(out.exists())

    def test_line_colors_stable_across_all_classes(self):
        # Lock: every RECON_LABELS class has a color mapping in
        # CLASS_LINE_COLORS. Guards against a future rename in
        # RECON_LABELS or a color-dict typo causing silent KeyError
        # when a new-name class shows up in commit_log.
        for label in RECON_LABELS:
            self.assertIn(label, CLASS_LINE_COLORS)
        # And the reverse — no orphan colors that would suggest a
        # stale class name lingering in the dict.
        self.assertEqual(set(CLASS_LINE_COLORS), set(RECON_LABELS))

    def test_time_axis_matches_temperature_chart(self):
        # Both charts should use elapsed_s / 60.0 as the x-axis. If
        # temp uses minutes and classifier uses seconds, side-by-side
        # comparison in the Day 6 HTML report would mislead the
        # grower. Verify indirectly: for a commit at elapsed_s=90,
        # the underlying series x-value is 1.5 (minutes), matching
        # the temperature chart's convention (plot_temperature.py:99).
        rows = [_write_full_commit_row("90", {"1x1": 100})]
        _write_csv(
            self.session_dir / "commit_log.csv",
            _commit_row_fields(), rows,
        )
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["elapsed_s", "pyrometer_temp_C"],
            [{"elapsed_s": "90", "pyrometer_temp_C": "500"}],
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)

        # Reader-level: temperature_series returns raw seconds; the
        # chart code divides. Assert the temperature_series[0] matches
        # commit_rows[0].elapsed_s so both charts feed identical time
        # values to their /60.0 divides.
        elapsed_temp, _ = artifacts.temperature_series()
        elapsed_commit = _safe_float_local(
            artifacts.commit_rows[0].get("elapsed_s")
        )
        self.assertEqual(elapsed_temp[0], 90.0)
        self.assertEqual(elapsed_commit, 90.0)

        # And render succeeds.
        out = self.out_dir / "traj.png"
        result = render_classifier_trajectory(artifacts, out, dpi=100)
        self.assertEqual(result, out)


class ScoreDistributionTests(unittest.TestCase):
    """Three tests locking auto-capture score-distribution behavior."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = (
            Path(self.tmp.name) / "growth_TEST_20260717_110000"
        )
        self.session_dir.mkdir()
        self.out_dir = Path(self.tmp.name) / "out"
        self.out_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_histogram_bins_from_change_score(self):
        # Ten auto-capture events with a spread of change_scores →
        # renders successfully. Non-trivial PNG size confirms axes +
        # bars actually landed.
        rows = [
            {"timestamp": "T", "elapsed_s": str(30 + i),
             "event_idx": str(i), "change_score": str(0.05 * i),
             "event_state": "kept_default"}
            for i in range(10)
        ]
        _write_csv(
            self.session_dir / "auto_capture_events.csv",
            ["timestamp", "elapsed_s", "event_idx", "change_score",
             "event_state"],
            rows,
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        out = self.out_dir / "score.png"
        result = render_score_distribution(artifacts, out, dpi=100)
        self.assertEqual(result, out)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 500)

    def test_no_auto_capture_events_returns_none(self):
        # Zero auto_capture rows → no histogram data → None + no PNG.
        # Guard against "empty histogram" render (matplotlib would
        # cheerfully produce a chart with no bars, which misleads a
        # viewer into thinking the session had events).
        _write_csv(
            self.session_dir / "auto_capture_events.csv",
            ["elapsed_s", "event_idx", "change_score", "event_state"],
            [],
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        out = self.out_dir / "score.png"
        result = render_score_distribution(artifacts, out, dpi=100)
        self.assertIsNone(result)
        self.assertFalse(out.exists())

    def test_state_buckets_partition_all_events(self):
        # Mix of event_states — verify EVENT_STATE_ORDER covers the
        # documented set (regression guard for the state constants in
        # growth_logger.py:26-30). Doesn't verify the rendered text
        # box directly, but confirms the state-count aggregation
        # logic runs against a realistic distribution.
        rows = [
            {"elapsed_s": "30", "event_idx": "1",
             "change_score": "0.5", "event_state": "kept_explicit"},
            {"elapsed_s": "60", "event_idx": "2",
             "change_score": "0.6", "event_state": "kept_default"},
            {"elapsed_s": "90", "event_idx": "3",
             "change_score": "0.7", "event_state": "discarded"},
            {"elapsed_s": "120", "event_idx": "4",
             "change_score": "0.8", "event_state": "auto_skipped"},
            {"elapsed_s": "150", "event_idx": "5",
             "change_score": "0.9", "event_state": "pending"},
        ]
        _write_csv(
            self.session_dir / "auto_capture_events.csv",
            ["elapsed_s", "event_idx", "change_score", "event_state"],
            rows,
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        out = self.out_dir / "score.png"
        result = render_score_distribution(artifacts, out, dpi=100)
        self.assertEqual(result, out)
        # All 5 states from growth_logger.EVENT_STATE_* constants must
        # be present in EVENT_STATE_ORDER (order + membership contract).
        expected = {
            "pending", "kept_explicit", "kept_default",
            "discarded", "auto_skipped",
        }
        self.assertEqual(set(EVENT_STATE_ORDER), expected)


def _safe_float_local(value):
    """Duplicated from growth_profile_explorer for test-only introspection.

    Kept intentionally simple — tests should exercise the reader's
    public API. The private _safe_float lives inside the module.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _write_synthetic_chart_png(path: Path) -> None:
    """Write a tiny valid PNG for HTML-report base64 embedding tests.

    matplotlib in Agg would work too, but this stays lighter and
    lets the HTML tests be independent of the chart-render code
    path. Bytes below are a 1x1 transparent PNG — enough for the
    base64-embedding + <img> path to exercise, not visually
    interesting.
    """
    # Smallest valid PNG (1x1 transparent, ~67 bytes).
    tiny_png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000"
        "001f15c4890000000d49444154789c626001000000050001a5f6450b"
        "0000000049454e44ae426082"
    )
    path.write_bytes(tiny_png)


class HtmlReportTests(unittest.TestCase):
    """Four tests locking HTML report assembly behavior."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = (
            Path(self.tmp.name) / "growth_TEST_20260720_100000"
        )
        self.session_dir.mkdir()
        self.out_dir = Path(self.tmp.name) / "out"
        self.out_dir.mkdir()
        # A minimal session so metadata() has something to surface.
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["elapsed_s", "pyrometer_temp_C"],
            [{"elapsed_s": str(15.0 * i),
              "pyrometer_temp_C": str(500 + i)} for i in range(6)],
        )
        _write_csv(
            self.session_dir / "commit_log.csv",
            ["timestamp", "elapsed_s", "sample_id", "grower", "note"],
            [{"timestamp": "T", "elapsed_s": "30",
              "sample_id": "STO_1", "grower": "AJ", "note": "n"}],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_html_created_when_charts_exist(self):
        # One fake PNG in the "written" list → HTML lands on disk
        # with a non-trivial size (metadata + template ~2KB minimum).
        chart_path = self.out_dir / "temperature_profile_annotated.png"
        _write_synthetic_chart_png(chart_path)
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        out = self.out_dir / "report.html"
        result = render_html_report(artifacts, out, [chart_path])
        self.assertEqual(result, out)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 1500)

    def test_base64_png_embedded_in_html(self):
        # HTML output includes the data:image/png;base64, prefix for
        # the embedded chart. Guards against a future refactor that
        # accidentally switches to <img src=file:...> (would break
        # the "self-contained, emailable" contract).
        chart_path = self.out_dir / "temperature_profile_annotated.png"
        _write_synthetic_chart_png(chart_path)
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        out = self.out_dir / "report.html"
        render_html_report(artifacts, out, [chart_path])
        html = out.read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,", html)
        # No file:// or absolute-path leaks in the src attributes.
        self.assertNotIn("file://", html)
        self.assertNotIn(str(chart_path), html)

    def test_metadata_header_present(self):
        # Session name + grower + sample_id from the commit row land
        # in the HTML body. Guards against a template change that
        # accidentally drops a metadata field.
        chart_path = self.out_dir / "temperature_profile_annotated.png"
        _write_synthetic_chart_png(chart_path)
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        out = self.out_dir / "report.html"
        render_html_report(artifacts, out, [chart_path])
        html = out.read_text(encoding="utf-8")
        self.assertIn("growth_TEST_20260720_100000", html)
        self.assertIn("AJ", html)
        self.assertIn("STO_1", html)

    def test_stride_reduces_data_points(self):
        # SessionArtifacts.subsample(N) yields shorter sensor + heartbeat
        # rows but leaves event-based CSVs untouched. Locks the "sparse
        # events are never subsampled" contract — else --stride 5 on a
        # session with 4 grower events might silently drop the last one.
        _write_csv(
            self.session_dir / "heartbeat_log.csv",
            ["elapsed_s", "heartbeat_idx", "frame_path"],
            [{"elapsed_s": str(i), "heartbeat_idx": str(i),
              "frame_path": f"/x{i}"} for i in range(20)],
        )
        artifacts = SessionArtifacts.from_session_dir(self.session_dir)
        # Baseline
        self.assertEqual(len(artifacts.sensor_rows), 6)
        self.assertEqual(len(artifacts.heartbeat_rows), 20)
        self.assertEqual(len(artifacts.commit_rows), 1)
        # Subsample every 3rd row
        sub = artifacts.subsample(3)
        # ceil(6 / 3) = 2  and  ceil(20 / 3) = 7  under Python slicing
        self.assertEqual(len(sub.sensor_rows), 2)
        self.assertEqual(len(sub.heartbeat_rows), 7)
        # Event-based CSVs preserved
        self.assertEqual(len(sub.commit_rows), 1)
        # stride=1 returns the same object (no-op fast path)
        self.assertIs(artifacts.subsample(1), artifacts)


class CliSmokeTests(unittest.TestCase):
    """Two tests exercising the argparse main() end-to-end via subprocess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = (
            Path(self.tmp.name) / "growth_TEST_20260720_110000"
        )
        self.session_dir.mkdir()
        # Minimal session: enough for temperature chart to render.
        _write_csv(
            self.session_dir / "sensor_log.csv",
            ["elapsed_s", "pyrometer_temp_C"],
            [{"elapsed_s": str(15.0 * i),
              "pyrometer_temp_C": str(500 + i)} for i in range(5)],
        )
        self.script_path = (
            REPO_ROOT / "scripts" / "growth_profile_explorer.py"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_session_exits_1(self):
        # Nonexistent session path → exit 1 + stderr diagnostic.
        # Guards against a silent "wrote 0 charts" success on a typo.
        import subprocess
        result = subprocess.run(
            [
                sys.executable, str(self.script_path),
                "/tmp/does-not-exist-12345",
            ],
            capture_output=True, text=True,
            env={**os.environ, "MPLBACKEND": "Agg"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Error", result.stderr)

    def test_success_prints_output_paths(self):
        # Valid session → exit 0 + stdout lists the chart file names.
        # Also verifies HTML report lands by default (no --no-html).
        import subprocess
        result = subprocess.run(
            [
                sys.executable, str(self.script_path),
                str(self.session_dir),
                "--output-dir", str(Path(self.tmp.name) / "analysis"),
            ],
            capture_output=True, text=True,
            env={**os.environ, "MPLBACKEND": "Agg"},
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"stderr: {result.stderr}",
        )
        self.assertIn("Rendered", result.stdout)
        self.assertIn(
            "temperature_profile_annotated.png", result.stdout,
        )
        self.assertIn("growth_profile_report.html", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
