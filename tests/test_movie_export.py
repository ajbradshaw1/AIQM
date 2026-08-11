"""Unit tests for the movie exporter and its worker.

Two layers:
  - ``MovieExporter`` — pure Python (+ cv2). Tests synthesize a session
    directory with heartbeat_log.csv + a handful of BMPs, then verify
    frame_count + export_movie behavior.
  - ``MovieExportWorker`` — QThread wrapping the exporter. Tests
    instantiate + start + wait, verifying signals fire correctly.
    Requires QT_QPA_PLATFORM=offscreen for headless.

Test frames are small (32x32) so encodes finish in <1 s. Real growth
frames are 656x492 but nothing here depends on that.

Run:
    python -m pytest -q tests/test_movie_export.py
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

# QApplication must precede any QObject/QThread creation. Instantiated
# at module load time so the worker tests can start their threads.
from PyQt6.QtWidgets import QApplication  # noqa: E402
_app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

from gui.movie_export import (  # noqa: E402
    DEFAULT_FPS,
    DEFAULT_MOVIE_NAME,
    MovieExporter,
    MovieExportWorker,
)

try:
    import cv2  # noqa: F401
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

import numpy as np  # noqa: E402


def _write_bmp(path: Path, size: tuple[int, int] = (32, 32), color: int = 128):
    """Write a small solid-color BMP so cv2.imread can decode it."""
    if not HAS_CV2:
        return
    # cv2 wants BGR; a uint8 array is enough. Fill with the color arg
    # scaled by channel so frames can be visually distinguished when
    # someone hand-inspects the resulting mp4.
    arr = np.full((size[0], size[1], 3), color, dtype=np.uint8)
    cv2.imwrite(str(path), arr)


def _make_session_with_frames(
    tmp: str, n_frames: int = 5,
) -> tuple[Path, list[Path]]:
    """Build a session dir with heartbeat_log.csv + n BMP frames.

    Returns (session_dir, list of frame paths). The BMPs live under
    session_dir/frames/ following the actual session layout.
    """
    session_dir = Path(tmp)
    frames_dir = session_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    frame_paths: list[Path] = []
    rows: list[dict] = []
    for i in range(1, n_frames + 1):
        fp = frames_dir / f"heartbeat_{i:03d}_120000.bmp"
        _write_bmp(fp, color=(i * 30) % 256)  # different color per frame
        frame_paths.append(fp)
        rows.append({
            "timestamp": f"2026-07-10T12:00:{i:02d}",
            "elapsed_s": f"{i * 5}.00",
            "heartbeat_idx": str(i),
            "pyrometer_temp_C": "640.0",
            "frame_path": str(fp),
        })

    csv_path = session_dir / "heartbeat_log.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp", "elapsed_s", "heartbeat_idx",
                        "pyrometer_temp_C", "frame_path"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return session_dir, frame_paths


class FrameCountTests(unittest.TestCase):
    """MovieExporter.frame_count() semantics — must be usable BEFORE
    triggering an encode so the UI can disable the button when there's
    nothing to export."""

    def test_empty_session_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            exporter = MovieExporter(Path(tmp))
            self.assertEqual(exporter.frame_count(), 0)

    def test_missing_csv_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            (session_dir / "frames").mkdir()
            exporter = MovieExporter(session_dir)
            self.assertEqual(exporter.frame_count(), 0)

    def test_csv_with_missing_frames_counts_only_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            (session_dir / "frames").mkdir()
            csv_path = session_dir / "heartbeat_log.csv"
            with open(csv_path, "w", newline="") as f:
                w = csv.DictWriter(
                    f, fieldnames=["timestamp", "elapsed_s",
                                   "heartbeat_idx", "pyrometer_temp_C",
                                   "frame_path"],
                )
                w.writeheader()
                # Frame path pointing at a file that doesn't exist —
                # exporter must not count it.
                w.writerow({
                    "timestamp": "t0", "elapsed_s": "5.00",
                    "heartbeat_idx": "1", "pyrometer_temp_C": "640",
                    "frame_path": str(session_dir / "frames" / "ghost.bmp"),
                })
            exporter = MovieExporter(session_dir)
            self.assertEqual(exporter.frame_count(), 0)

    @unittest.skipUnless(HAS_CV2, "cv2 not installed")
    def test_populated_session_counts_all_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir, _ = _make_session_with_frames(tmp, n_frames=7)
            exporter = MovieExporter(session_dir)
            self.assertEqual(exporter.frame_count(), 7)


@unittest.skipUnless(HAS_CV2, "cv2 not installed")
class ExportMovieTests(unittest.TestCase):
    """MovieExporter.export_movie behavior — synthesizes a session dir,
    runs the encode, verifies the output file exists and has plausible
    size + duration. Skipped when cv2 isn't installed."""

    def test_export_creates_playable_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir, _ = _make_session_with_frames(tmp, n_frames=10)
            exporter = MovieExporter(session_dir)
            out_path = session_dir / DEFAULT_MOVIE_NAME

            result = exporter.export_movie(out_path, fps=DEFAULT_FPS)
            self.assertEqual(result, str(out_path))
            self.assertTrue(out_path.exists())
            # Size > 1 KB — trivially small files indicate a failed
            # write (0-byte mp4 header + no frames).
            self.assertGreater(out_path.stat().st_size, 1024)

    def test_export_calls_progress_callback_per_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir, _ = _make_session_with_frames(tmp, n_frames=5)
            exporter = MovieExporter(session_dir)
            out_path = session_dir / DEFAULT_MOVIE_NAME

            progress: list[tuple[int, int]] = []
            exporter.export_movie(
                out_path, fps=DEFAULT_FPS,
                progress_cb=lambda c, t: progress.append((c, t)),
            )
            # Progress fires once per frame — 5 frames → 5 events, each
            # with the same total but monotonically increasing current.
            self.assertEqual(len(progress), 5)
            self.assertEqual([c for c, _ in progress], [1, 2, 3, 4, 5])
            self.assertTrue(all(t == 5 for _, t in progress))

    def test_export_empty_session_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            (session_dir / "frames").mkdir()
            exporter = MovieExporter(session_dir)
            out_path = session_dir / DEFAULT_MOVIE_NAME
            self.assertEqual(exporter.export_movie(out_path), "")

    def test_export_missing_csv_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            exporter = MovieExporter(Path(tmp))
            out_path = Path(tmp) / DEFAULT_MOVIE_NAME
            self.assertEqual(exporter.export_movie(out_path), "")

    def test_cancel_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir, _ = _make_session_with_frames(tmp, n_frames=10)
            exporter = MovieExporter(session_dir)
            out_path = session_dir / DEFAULT_MOVIE_NAME
            calls = 0

            def cancel_after_first_frame() -> bool:
                nonlocal calls
                calls += 1
                return calls > 1

            self.assertEqual(
                exporter.export_movie(out_path, cancel_cb=cancel_after_first_frame),
                "",
            )
            self.assertFalse(out_path.exists())


@unittest.skipUnless(HAS_CV2, "cv2 not installed")
class MovieExportWorkerTests(unittest.TestCase):
    """QThread wrapper: verifies progress + finished_ok signals fire."""

    def test_worker_encodes_and_emits_finished_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Use enough tiny frames to clear the exporter's >1 KiB sanity
            # threshold across supported OpenCV/FFmpeg wheel builds.
            session_dir, _ = _make_session_with_frames(tmp, n_frames=10)
            exporter = MovieExporter(session_dir)
            out_path = session_dir / DEFAULT_MOVIE_NAME
            worker = MovieExportWorker(exporter, out_path)

            captured_path: list[str] = []
            captured_progress: list[tuple[int, int]] = []
            worker.finished_ok.connect(captured_path.append)
            worker.progress.connect(
                lambda c, t: captured_progress.append((c, t))
            )

            worker.start()
            self.assertTrue(worker.wait(10000), "worker did not finish in 10s")

            # Drive the Qt event loop briefly so queued signals land.
            _app.processEvents()

            self.assertEqual(len(captured_path), 1)
            self.assertEqual(captured_path[0], str(out_path))
            # 10 progress events fired (one per frame), delivered via
            # queued connection to the main thread.
            self.assertGreaterEqual(len(captured_progress), 10)

    def test_worker_empty_session_emits_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            (session_dir / "frames").mkdir()
            exporter = MovieExporter(session_dir)
            out_path = session_dir / DEFAULT_MOVIE_NAME
            worker = MovieExportWorker(exporter, out_path)

            captured_failure: list[str] = []
            worker.failed.connect(captured_failure.append)

            worker.start()
            self.assertTrue(worker.wait(5000))
            _app.processEvents()

            self.assertEqual(len(captured_failure), 1)
            self.assertIn(
                "No heartbeat frames", captured_failure[0],
            )


class GrowthMonitorButtonRegistrationTests(unittest.TestCase):
    """The 'Export Movie' button is exposed as ``export_movie_btn`` so
    GrowthApp can toggle its enabled state during encode. This test
    locks the attribute name + basic properties."""

    def setUp(self):
        from gui.growth_monitor import GrowthMonitor
        self.monitor = GrowthMonitor()

    def tearDown(self):
        self.monitor.deleteLater()

    def test_export_movie_btn_exists(self):
        self.assertTrue(hasattr(self.monitor, "export_movie_btn"))

    def test_export_movie_btn_starts_enabled(self):
        # Button is enabled at construction — GrowthApp disables it only
        # during an active encode. Pre-arm click will still show "No
        # session data" via _on_movie_export.
        self.assertTrue(self.monitor.export_movie_btn.isEnabled())

    def test_movie_export_requested_signal_declared(self):
        from PyQt6.QtCore import pyqtBoundSignal
        self.assertIsInstance(
            self.monitor.movie_export_requested, pyqtBoundSignal,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
