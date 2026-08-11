"""Unit tests for the scrubber tab and its frame-index reader.

Two layers:
  - ``build_frame_index`` — pure function, no Qt. Aggregates the three
    per-session CSVs into a sorted list of FrameIndexEntry. Tests use
    synthetic session directories under tempfile.
  - ``ScrubberTab`` — Qt widget. Tests instantiate + attach_session
    against synthetic session dirs, verifying nav state + display text.
    Requires QT_QPA_PLATFORM=offscreen on headless environments.

Run:
    python -m pytest -q tests/test_scrubber_tab.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# QApplication must exist before ANY QWidget is constructed. Creating it
# at module load time follows the same pattern as test_evap_control_worker.py.
# Kept as a module-level ref to prevent garbage collection; PyQt6 doesn't
# always reference-count QApplication cleanly across test classes.
from PyQt6.QtWidgets import QApplication  # noqa: E402
_app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

from gui.scrubber_tab import (  # noqa: E402
    FrameIndexEntry,
    _SOURCE_COLORS,
    _format_metadata,
    build_frame_index,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _make_session_dir(
    tmp: str,
    heartbeat_rows: list[dict] = (),
    manual_rows: list[dict] = (),
    view_rows: list[dict] = (),
    auto_rows: list[dict] = (),
) -> Path:
    """Build a synthetic session dir with the three frame-emitting CSVs.

    Missing lists (empty tuples) → no CSV file written. Tests that need
    a specific CSV to be absent should NOT pass that keyword argument.
    """
    session_dir = Path(tmp)
    (session_dir / "frames").mkdir(exist_ok=True)
    if heartbeat_rows:
        _write_csv(
            session_dir / "heartbeat_log.csv",
            ["timestamp", "elapsed_s", "heartbeat_idx",
             "pyrometer_temp_C", "frame_path"],
            list(heartbeat_rows),
        )
    if manual_rows:
        _write_csv(
            session_dir / "manual_events.csv",
            ["timestamp", "elapsed_s", "event_idx",
             "pyrometer_temp_C", "voltage_V", "current_A",
             "psu_source", "frame_path", "note"],
            list(manual_rows),
        )
    if view_rows:
        _write_csv(
            session_dir / "rheed_view_events.csv",
            [
                "timestamp", "elapsed_s", "event_idx", "event_type",
                "view_segment_id", "gun_aligned", "history_ready",
                "qc_reason", "frame_path", "note",
            ],
            list(view_rows),
        )
    if auto_rows:
        _write_csv(
            session_dir / "auto_capture_events.csv",
            ["timestamp", "elapsed_s", "event_idx",
             "change_score", "pyrometer_temp_C",
             "buffer_count", "buffer_dir",
             "event_state", "state_changed_at"],
            list(auto_rows),
        )
    return session_dir


class FrameIndexBuildTests(unittest.TestCase):
    """Aggregation semantics of build_frame_index."""

    def test_empty_session_dir_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = _make_session_dir(tmp)
            idx = build_frame_index(session_dir)
            self.assertEqual(idx, [])

    def test_missing_csvs_return_empty_list(self):
        # Session dir exists but no CSVs — build should return [] not raise.
        with tempfile.TemporaryDirectory() as tmp:
            idx = build_frame_index(Path(tmp))
            self.assertEqual(idx, [])

    def test_heartbeat_only_populates_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"timestamp": "t0", "elapsed_s": "5.00", "heartbeat_idx": "1",
                 "pyrometer_temp_C": "640.5",
                 "frame_path": "/tmp/frames/heartbeat_001.bmp"},
                {"timestamp": "t1", "elapsed_s": "10.00", "heartbeat_idx": "2",
                 "pyrometer_temp_C": "641.0",
                 "frame_path": "/tmp/frames/heartbeat_002.bmp"},
            ]
            session_dir = _make_session_dir(tmp, heartbeat_rows=rows)
            idx = build_frame_index(session_dir)
            self.assertEqual(len(idx), 2)
            self.assertEqual(idx[0].source, "heartbeat")
            self.assertEqual(idx[0].source_idx, 1)
            self.assertEqual(idx[0].elapsed_s, 5.0)
            self.assertEqual(idx[0].metadata["pyro"], "640.5")

    def test_three_sources_sort_by_elapsed(self):
        # heartbeat at 5,10; manual at 7; auto at 3 → sorted 3,5,7,10.
        with tempfile.TemporaryDirectory() as tmp:
            hb = [
                {"timestamp": "t0", "elapsed_s": "5.00", "heartbeat_idx": "1",
                 "pyrometer_temp_C": "640", "frame_path": ""},
                {"timestamp": "t1", "elapsed_s": "10.00", "heartbeat_idx": "2",
                 "pyrometer_temp_C": "641", "frame_path": ""},
            ]
            manual = [
                {"timestamp": "t2", "elapsed_s": "7.00", "event_idx": "1",
                 "pyrometer_temp_C": "640.5", "voltage_V": "12.3",
                 "current_A": "0.7", "psu_source": "mistral",
                 "frame_path": "", "note": "flash"},
            ]
            auto = [
                {"timestamp": "t3", "elapsed_s": "3.00", "event_idx": "1",
                 "change_score": "1.50", "pyrometer_temp_C": "639",
                 "buffer_count": "20", "buffer_dir": "",
                 "event_state": "pending", "state_changed_at": ""},
            ]
            session_dir = _make_session_dir(
                tmp,
                heartbeat_rows=hb,
                manual_rows=manual,
                auto_rows=auto,
            )
            idx = build_frame_index(session_dir)
            elapsed_order = [e.elapsed_s for e in idx]
            self.assertEqual(elapsed_order, [3.0, 5.0, 7.0, 10.0])
            sources = [e.source for e in idx]
            self.assertEqual(sources, ["auto", "heartbeat", "manual", "heartbeat"])

    def test_bad_row_is_skipped_not_fatal(self):
        # A row with an unparseable elapsed_s must not abort the whole read.
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"timestamp": "t0", "elapsed_s": "bad", "heartbeat_idx": "1",
                 "pyrometer_temp_C": "640", "frame_path": ""},
                {"timestamp": "t1", "elapsed_s": "10.00", "heartbeat_idx": "2",
                 "pyrometer_temp_C": "641", "frame_path": ""},
            ]
            session_dir = _make_session_dir(tmp, heartbeat_rows=rows)
            idx = build_frame_index(session_dir)
            self.assertEqual(len(idx), 1)
            self.assertEqual(idx[0].source_idx, 2)

    def test_auto_capture_reads_first_bmp_from_buffer_dir(self):
        # buffer_dir contains .bmps → auto entry gets a frame_path pointing
        # at the first (sorted) BMP.
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            (session_dir / "frames").mkdir(exist_ok=True)
            buffer_dir = session_dir / "buffer_event_001"
            buffer_dir.mkdir()
            # Create two dummy BMP files
            (buffer_dir / "b_02.bmp").write_bytes(b"BMdummy")
            (buffer_dir / "b_01.bmp").write_bytes(b"BMdummy")
            (buffer_dir / "b_03.bmp").write_bytes(b"BMdummy")
            auto = [{
                "timestamp": "t0", "elapsed_s": "3.00", "event_idx": "1",
                "change_score": "1.5", "pyrometer_temp_C": "640",
                "buffer_count": "3", "buffer_dir": str(buffer_dir),
                "event_state": "pending", "state_changed_at": "",
            }]
            session_dir = _make_session_dir(tmp, auto_rows=auto)
            idx = build_frame_index(session_dir)
            self.assertEqual(len(idx), 1)
            # First .bmp alphabetically is b_01.bmp — verifies sort() ordering.
            self.assertTrue(idx[0].frame_path.endswith("b_01.bmp"))

    def test_manual_metadata_carries_note_and_psu(self):
        with tempfile.TemporaryDirectory() as tmp:
            manual = [{
                "timestamp": "t0", "elapsed_s": "5.00", "event_idx": "1",
                "pyrometer_temp_C": "640", "voltage_V": "12.3",
                "current_A": "0.7", "psu_source": "mistral",
                "frame_path": "", "note": "rt13 blooming",
            }]
            session_dir = _make_session_dir(tmp, manual_rows=manual)
            idx = build_frame_index(session_dir)
            md = idx[0].metadata
            self.assertEqual(md["note"], "rt13 blooming")
            self.assertEqual(md["psu"], "mistral")
            self.assertEqual(md["V"], "12.3")
            self.assertEqual(md["I"], "0.7")

    def test_rheed_view_event_is_indexed_with_state_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            view = [{
                "timestamp": "t0",
                "elapsed_s": "8.00",
                "event_idx": "2",
                "event_type": "realign_end",
                "view_segment_id": "1",
                "gun_aligned": "True",
                "history_ready": "False",
                "qc_reason": "",
                "frame_path": "",
                "note": "focus locked",
            }]
            session_dir = _make_session_dir(tmp, view_rows=view)
            idx = build_frame_index(session_dir)
            self.assertEqual(len(idx), 1)
            self.assertEqual(idx[0].source, "view")
            self.assertEqual(idx[0].metadata["event_type"], "realign_end")
            self.assertEqual(idx[0].metadata["segment"], "1")


class FormatMetadataTests(unittest.TestCase):
    """One-liner display formatter used under the scrubber image."""

    def test_heartbeat_entry_shows_elapsed_and_pyro(self):
        entry = FrameIndexEntry(
            elapsed_s=125.0, source="heartbeat", source_idx=1,
            frame_path="", metadata={"pyro": "640.5"},
        )
        text = _format_metadata(entry)
        self.assertIn("[02:05]", text)  # 125s → 02:05
        self.assertIn("pyro 640.5", text)

    def test_manual_entry_shows_note_and_v_i(self):
        entry = FrameIndexEntry(
            elapsed_s=7.0, source="manual", source_idx=1,
            frame_path="",
            metadata={
                "pyro": "640", "V": "12.3", "I": "0.7",
                "psu": "mistral", "note": "flash",
            },
        )
        text = _format_metadata(entry)
        self.assertIn('"flash"', text)
        self.assertIn("V 12.3", text)
        self.assertIn("I 0.7", text)
        self.assertIn("[mistral]", text)

    def test_auto_entry_shows_score_and_state(self):
        entry = FrameIndexEntry(
            elapsed_s=3.0, source="auto", source_idx=1,
            frame_path="",
            metadata={"pyro": "640", "score": "1.50", "state": "kept_explicit"},
        )
        text = _format_metadata(entry)
        self.assertIn("score 1.50", text)
        self.assertIn("[kept_explicit]", text)

    def test_view_entry_shows_alignment_state(self):
        entry = FrameIndexEntry(
            elapsed_s=8.0,
            source="view",
            source_idx=2,
            frame_path="",
            metadata={
                "event_type": "realign_end",
                "segment": "1",
                "gun_aligned": "True",
                "history_ready": "False",
                "qc_reason": "",
                "note": "focus locked",
            },
        )
        text = _format_metadata(entry)
        self.assertIn("realign_end", text)
        self.assertIn("segment 1", text)
        self.assertIn("aligned=True", text)
        self.assertIn('"focus locked"', text)

    def test_blank_fields_are_skipped(self):
        # An entry with no pyro / no note should not produce empty
        # "pyro " or 'note ""' fragments.
        entry = FrameIndexEntry(
            elapsed_s=0.0, source="heartbeat", source_idx=1,
            frame_path="", metadata={"pyro": ""},
        )
        text = _format_metadata(entry)
        self.assertNotIn("pyro °C", text)
        # Only the timestamp element survives.
        self.assertEqual(text, "[00:00]")


class SourceColorTests(unittest.TestCase):
    """Marker colors match the Monitor tab's footer colors — cross-tab
    visual identity for the same three event streams."""

    def test_heartbeat_color_matches_monitor_footer(self):
        # Cyan #0891b2 — same as the "Capture: every 5s" indicator.
        self.assertEqual(_SOURCE_COLORS["heartbeat"], "#0891b2")

    def test_manual_color_matches_monitor_footer(self):
        # Amber #d97706 — same as the "Manual events: N" indicator
        # and the MARK EVENT button on the Monitor tab.
        self.assertEqual(_SOURCE_COLORS["manual"], "#d97706")

    def test_auto_color_grey(self):
        self.assertEqual(_SOURCE_COLORS["auto"], "#888888")

    def test_view_color_is_distinct(self):
        self.assertEqual(_SOURCE_COLORS["view"], "#a855f7")


# --- Qt-side tests -----------------------------------------------------------


class ScrubberTabWidgetTests(unittest.TestCase):
    """UI-side tests for ScrubberTab behavior. Uses the module-level
    QApplication singleton created at import time."""

    def setUp(self):
        from gui.scrubber_tab import ScrubberTab
        self.tab = ScrubberTab()

    def tearDown(self):
        self.tab.deleteLater()

    def test_initial_state_is_placeholder(self):
        self.assertIn(
            "Attach a session", self.tab._metadata_label.text(),
        )
        self.assertFalse(self.tab._slider.isEnabled())
        self.assertFalse(self.tab._prev_btn.isEnabled())
        self.assertFalse(self.tab._next_btn.isEnabled())

    def test_attach_empty_session_shows_no_frames_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            (session_dir / "frames").mkdir()
            self.tab.attach_session(session_dir)
            self.assertIn(
                "No frames captured yet",
                self.tab._metadata_label.text(),
            )
            self.assertFalse(self.tab._slider.isEnabled())

    def test_attach_populated_session_enables_slider_and_nav(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"timestamp": "t0", "elapsed_s": "5.00", "heartbeat_idx": "1",
                 "pyrometer_temp_C": "640", "frame_path": ""},
                {"timestamp": "t1", "elapsed_s": "10.00", "heartbeat_idx": "2",
                 "pyrometer_temp_C": "641", "frame_path": ""},
            ]
            session_dir = _make_session_dir(tmp, heartbeat_rows=rows)
            self.tab.attach_session(session_dir)
            self.assertTrue(self.tab._slider.isEnabled())
            self.assertTrue(self.tab._prev_btn.isEnabled())
            self.assertTrue(self.tab._next_btn.isEnabled())
            self.assertEqual(self.tab._slider.maximum(), 1)  # 2 frames → 0..1
            self.assertIn("Frame 1 / 2", self.tab._position_label.text())

    def test_next_prev_navigation_walks_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"timestamp": f"t{i}", "elapsed_s": f"{i*5}.00",
                 "heartbeat_idx": str(i + 1),
                 "pyrometer_temp_C": "640", "frame_path": ""}
                for i in range(3)
            ]
            session_dir = _make_session_dir(tmp, heartbeat_rows=rows)
            self.tab.attach_session(session_dir)

            # Start at 0
            self.assertEqual(self.tab._current_pos, 0)
            self.tab._on_next()
            self.assertEqual(self.tab._current_pos, 1)
            self.assertIn("Frame 2 / 3", self.tab._position_label.text())
            self.tab._on_next()
            self.assertEqual(self.tab._current_pos, 2)
            # Attempting to go past end stays at end.
            self.tab._on_next()
            self.assertEqual(self.tab._current_pos, 2)
            # Prev walks back.
            self.tab._on_prev()
            self.assertEqual(self.tab._current_pos, 1)
            # Going past start stays at start.
            self.tab._on_prev()
            self.tab._on_prev()
            self.assertEqual(self.tab._current_pos, 0)

    def test_reload_preserves_position_when_still_in_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"timestamp": f"t{i}", "elapsed_s": f"{i*5}.00",
                 "heartbeat_idx": str(i + 1),
                 "pyrometer_temp_C": "640", "frame_path": ""}
                for i in range(4)
            ]
            session_dir = _make_session_dir(tmp, heartbeat_rows=rows)
            self.tab.attach_session(session_dir)
            self.tab._on_next()
            self.tab._on_next()
            self.assertEqual(self.tab._current_pos, 2)

            # Simulate more frames landing between reads — session dir
            # gets updated with two additional rows.
            rows.append({
                "timestamp": "t4", "elapsed_s": "20.00",
                "heartbeat_idx": "5", "pyrometer_temp_C": "640",
                "frame_path": "",
            })
            _write_csv(
                session_dir / "heartbeat_log.csv",
                ["timestamp", "elapsed_s", "heartbeat_idx",
                 "pyrometer_temp_C", "frame_path"],
                rows,
            )
            self.tab._on_reload()
            # Still at position 2 after reload (out of 5 now).
            self.assertEqual(self.tab._current_pos, 2)
            self.assertIn("Frame 3 / 5", self.tab._position_label.text())

    def test_attach_none_returns_to_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"timestamp": "t0", "elapsed_s": "5.00", "heartbeat_idx": "1",
                 "pyrometer_temp_C": "640", "frame_path": ""},
            ]
            session_dir = _make_session_dir(tmp, heartbeat_rows=rows)
            self.tab.attach_session(session_dir)
            self.assertTrue(self.tab._slider.isEnabled())
            self.tab.attach_session(None)
            self.assertFalse(self.tab._slider.isEnabled())
            self.assertIn(
                "Attach a session", self.tab._metadata_label.text(),
            )


class ScrubberTabRegistrationTests(unittest.TestCase):
    """Verifies the scrubber tab is registered on the Growth Monitor
    with the expected placement + attach_session hook exposed."""

    def setUp(self):
        from gui.growth_monitor import GrowthMonitor
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def test_scrubber_tab_registered(self):
        tab_labels = [
            self.monitor._tabs.tabText(i)
            for i in range(self.monitor._tabs.count())
        ]
        self.assertIn("Scrubber", tab_labels)

    def test_scrubber_tab_between_events_and_session(self):
        tab_labels = [
            self.monitor._tabs.tabText(i)
            for i in range(self.monitor._tabs.count())
        ]
        s = tab_labels.index("Scrubber")
        self.assertGreater(s, tab_labels.index("Events"))
        self.assertLess(s, tab_labels.index("Session"))

    def test_scrubber_tab_attribute_exposed(self):
        # GrowthApp reaches through monitor.scrubber_tab, so the attribute
        # name is load-bearing.
        self.assertTrue(hasattr(self.monitor, "scrubber_tab"))


class AutoPollTests(unittest.TestCase):
    """Locks the C4 (Day 8) live-polling behavior:

      - Timer default OFF; set_live_polling(True/False) toggles
      - Tick invokes _reload_index unless race guard fires
      - Reload button label reflects state
      - Recent user scrub defers the next auto-reload
    """

    def setUp(self):
        from gui.scrubber_tab import ScrubberTab
        self.tab = ScrubberTab()

    def tearDown(self):
        self.tab.deleteLater()

    def test_auto_polling_disabled_at_construction(self):
        # Fresh tab must not fire a QTimer — tests + standalone
        # launches shouldn't be pounding _reload_index at 5s cadence
        # before a session has been armed.
        self.assertFalse(self.tab._live_polling)
        self.assertFalse(self.tab._auto_poll_timer.isActive())

    def test_set_live_polling_true_starts_timer(self):
        self.tab.set_live_polling(True)
        self.assertTrue(self.tab._live_polling)
        self.assertTrue(self.tab._auto_poll_timer.isActive())
        # Idempotent: calling True twice doesn't double-start.
        self.tab.set_live_polling(True)
        self.assertTrue(self.tab._auto_poll_timer.isActive())

    def test_set_live_polling_false_stops_timer(self):
        self.tab.set_live_polling(True)
        self.tab.set_live_polling(False)
        self.assertFalse(self.tab._live_polling)
        self.assertFalse(self.tab._auto_poll_timer.isActive())

    def test_tick_calls_reload_index(self):
        # Mock _reload_index and drive the tick directly. QTimer
        # asynchrony would make time-based tests flaky; instead
        # invoke _on_auto_poll_tick and verify the code path.
        call_count = {"n": 0}
        orig_reload = self.tab._reload_index

        def fake_reload():
            call_count["n"] += 1
        self.tab._reload_index = fake_reload
        try:
            self.tab._on_auto_poll_tick()
        finally:
            self.tab._reload_index = orig_reload
        self.assertEqual(call_count["n"], 1)

    def test_reload_btn_label_reflects_state(self):
        default_text = self.tab._reload_btn.text()
        self.assertNotIn("auto", default_text)
        self.tab.set_live_polling(True)
        self.assertIn("auto", self.tab._reload_btn.text())
        self.tab.set_live_polling(False)
        self.assertEqual(self.tab._reload_btn.text(), default_text)

    def test_recent_user_scrub_defers_reload(self):
        # Simulate the grower having dragged the slider "just now"
        # (last interaction = current time). Race guard should kick
        # in and skip the reload. Next tick fires the normal cadence
        # later (not tested here — this is a pure guard-logic test).
        import time as _time
        call_count = {"n": 0}
        orig_reload = self.tab._reload_index

        def fake_reload():
            call_count["n"] += 1
        self.tab._reload_index = fake_reload
        try:
            self.tab._last_slider_interaction = _time.time()
            self.tab._on_auto_poll_tick()
        finally:
            self.tab._reload_index = orig_reload
        self.assertEqual(call_count["n"], 0)  # skipped

        # And when the interaction is far enough in the past, the
        # tick fires normally. Backdate by 2× the guard window.
        self.tab._reload_index = fake_reload
        try:
            self.tab._last_slider_interaction = (
                _time.time()
                - 2 * self.tab.AUTO_POLL_RACE_GUARD_S
            )
            self.tab._on_auto_poll_tick()
        finally:
            self.tab._reload_index = orig_reload
        self.assertEqual(call_count["n"], 1)  # fired


if __name__ == "__main__":
    unittest.main(verbosity=2)
