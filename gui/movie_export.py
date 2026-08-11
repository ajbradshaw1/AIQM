"""
Movie export — post-hoc time-lapse encoding of a session's continuous
capture frames.

Ships workstream #5 from the Jul 10 2026 group-meeting queue. Uses
cv2.VideoWriter (already in the stack) to stitch heartbeat_log.csv's
BMP frames into an mp4 the grower can archive / share.

Design notes:
  - Only heartbeat frames are included. They're evenly spaced by
    design (interval Y from the config); mixing in event frames from
    manual_events.csv / auto_capture_events.csv would create timing
    jitter without adding much visual value (grower can already see
    those on the Scrubber tab).
  - Default 15 fps playback = 75× speedup at 5-second capture. A
    4-hour growth (~2,880 frames) plays in ~3.2 minutes. Fast enough
    to be scannable, slow enough to see reconstructions evolve.
  - cv2.VideoWriter with 'mp4v' codec — widely playable across VLC,
    QuickTime, browsers, Windows Media Player.
  - Runs on a QThread (MovieExportWorker) so the GUI stays responsive
    for a 30-60 s encode. Progress signal fires per-frame so the UI
    can show "Exporting… (250 / 2880)" without polling.

v2+ deferred:
  - Grower-configurable fps (v1: fixed at 15)
  - Manual-event marker overlay on the frames (annotations)
  - Include event frames (interleave into the timeline)
  - Preview + start-frame / end-frame selection
  - Non-mp4 formats (webm, gif) — no current grower ask

Perf notes: The whole path is bounded by BMP decode + mp4 encode CPU.
On the O-MBE Bulbasaur PC, a 2,880-frame export at 656×492 typically
completes in 30-90 s. A future optimization could batch-load frames
with an executor, but for v1 sequential is simple + measurable.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QThread, pyqtSignal


DEFAULT_FPS = 15
DEFAULT_MOVIE_NAME = "growth_movie.mp4"


class MovieExporter:
    """Non-Qt utility that stitches heartbeat frames into an mp4.

    Instantiate with a session dir, then call ``export_movie`` with an
    output path. Frame count is exposed separately so the UI can
    decide (e.g. disable the button when the session has no frames)
    before triggering the full encode.
    """

    def __init__(self, session_dir: Path):
        self.session_dir = Path(session_dir)

    def _frame_paths(self) -> list[str]:
        """Read heartbeat_log.csv and return existing frame paths in order.

        Rows without a frame_path (rare — quality gate rejected the
        frame but the row was still logged), and rows whose frame_path
        doesn't exist on disk (e.g. hand-deleted post-session), are
        silently skipped. CSV row order preserves capture time order
        because the writer only appends.
        """
        csv_path = self.session_dir / "heartbeat_log.csv"
        if not csv_path.exists():
            return []
        paths: list[str] = []
        try:
            with open(csv_path, newline="") as f:
                for row in csv.DictReader(f):
                    path = row.get("frame_path", "") or ""
                    if path and Path(path).exists():
                        paths.append(path)
        except OSError:
            return []
        return paths

    def frame_count(self) -> int:
        """Number of usable heartbeat frames the export would include."""
        return len(self._frame_paths())

    def export_movie(
        self,
        output_path: Path,
        fps: int = DEFAULT_FPS,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> str:
        """Encode heartbeat frames into an mp4 at the requested fps.

        Returns the output path (string) on success, ``""`` on failure.
        Failure modes handled here (no exception raised):
          - cv2 not importable
          - heartbeat_log.csv missing or empty
          - all frames unreadable
          - first frame's cv2.imread returns None (bad BMP)
          - cv2.VideoWriter fails to open the output file

        ``progress_cb(current, total)`` is invoked after each frame,
        including skipped ones so the UI count stays monotonic
        against the total. Callers must NOT raise from the callback
        (this method won't catch it — Qt signal emission from a
        worker thread is already safe, but a sync callback that
        raises would bubble up here).
        """
        try:
            import cv2
        except ImportError:
            return ""

        paths = self._frame_paths()
        if not paths:
            return ""

        first_frame = cv2.imread(paths[0])
        if first_frame is None:
            return ""
        h, w = first_frame.shape[:2]

        # 'mp4v' fourcc is broadly compatible. Alternative 'avc1' (H.264)
        # is smaller but requires an OpenCV build with H.264 support that
        # isn't guaranteed across pip installs.
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(output_path), fourcc, fps, (w, h))
        if not writer.isOpened():
            return ""

        total = len(paths)
        cancelled = False
        written = 0
        try:
            for i, path in enumerate(paths, 1):
                if cancel_cb is not None and cancel_cb():
                    cancelled = True
                    break
                frame = cv2.imread(path)
                if frame is not None:
                    # Frames from continuous capture should all be the
                    # same size, but resize defensively in case someone
                    # mixed sources or the camera resolution changed.
                    if frame.shape[:2] != (h, w):
                        frame = cv2.resize(frame, (w, h))
                    writer.write(frame)
                    written += 1
                # else: skip silently — the row's frame_path was valid
                # but cv2 couldn't decode. Rare; not worth aborting.
                if progress_cb is not None:
                    progress_cb(i, total)
        finally:
            writer.release()

        if cancelled:
            try:
                Path(output_path).unlink(missing_ok=True)
            except OSError:
                pass
            return ""

        # Sanity check the output: cv2 won't raise if it wrote 0 bytes.
        # A fixed byte floor is codec-dependent (short clips of small
        # frames legitimately encode under 1 KB), so fail only when no
        # frame was actually written or the file is missing/empty.
        out = Path(output_path)
        if written == 0 or not out.exists() or out.stat().st_size == 0:
            return ""

        return str(out)


class MovieExportWorker(QThread):
    """QThread wrapper around ``MovieExporter.export_movie``.

    Signals:
      - ``progress(int, int)`` — (current, total) after each frame
      - ``finished_ok(str)``   — output path on success
      - ``failed(str)``        — error message on failure

    The worker owns neither the exporter nor the output path lifetime.
    Caller wires ``finished_ok`` / ``failed`` to slots that update the
    status bar / show a dialog, then calls ``start()``. GUI stays
    responsive throughout because encode runs off the main thread.
    """

    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        exporter: MovieExporter,
        output_path: Path,
        fps: int = DEFAULT_FPS,
        parent=None,
    ):
        super().__init__(parent)
        self._exporter = exporter
        self._output_path = Path(output_path)
        self._fps = fps

    def stop(self) -> None:
        """Request cooperative cancellation at the next frame boundary."""
        self.requestInterruption()

    def run(self):
        try:
            path = self._exporter.export_movie(
                self._output_path,
                fps=self._fps,
                progress_cb=lambda c, t: self.progress.emit(c, t),
                cancel_cb=self.isInterruptionRequested,
            )
        except Exception as exc:  # broad on purpose — background thread
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        if path:
            self.finished_ok.emit(path)
        else:
            if self.isInterruptionRequested():
                return
            # Distinguish the three most common failure modes with a
            # human-readable hint the status bar can display verbatim.
            if not self._exporter.frame_count():
                self.failed.emit(
                    "No heartbeat frames to export — session may not have "
                    "captured any yet.",
                )
                return
            try:
                import cv2  # noqa: F401
            except ImportError:
                self.failed.emit(
                    "cv2 (opencv-python) not installed — "
                    "run: pip install opencv-python",
                )
                return
            self.failed.emit(
                "Movie encode failed — check disk space and codec support.",
            )
