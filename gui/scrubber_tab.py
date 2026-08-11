"""
Scrubber tab — timeline "movie" playback for a growth session.

Ships workstream #3 from the Jul 10 2026 group-meeting design shift.
Growers asked for a scrubber they could flip through with a progress
bar; this is the retrospective label surface that pairs with the
Monitor tab's MARK EVENT (real-time mark) and the continuous-capture
"heartbeat" system (movie source).

Three-concern architecture:
  - Capture     — heartbeat_log.csv + heartbeat_NNN_*.bmp (interval Y)
  - Mark        — manual_events.csv + manual_event_NNN_*.bmp (grower)
  - Label       — this tab, retrospective, via scrubber (unimplemented v1;
                  labeling still lives on Events tab + Equalizer tab)

v1 scope:
  - Timeline slider aggregated across heartbeat + manual events +
    auto-capture rows, sorted by elapsed_s.
  - Custom-painted event markers on the slider (cyan/amber/grey by
    source).
  - Frame image display area with lazy PIL load — no eager BMP loading,
    no in-memory cache. A 4-hour growth at 5-second interval is ~2,880
    frames × ~500 KB = ~1.4 GB, so eager loading would blow past
    Bulbasaur's memory budget. Load-on-scrub is the perf strategy.
  - Prev / Next / Reload nav buttons; keyboard Left/Right on the slider.
  - Metadata line under the image: elapsed, pyro, V, I, note, score.
  - Reload button forces a re-read of the CSVs — used for live sessions
    where new frames land after the tab was attached.

Live auto-poll (shipped Jul 22 2026 — Day 8 sprint):
  - GrowthApp.on_start calls set_live_polling(True); on_stop calls
    set_live_polling(False). Timer ticks every 5s and calls
    _reload_index — same code path as the manual ↻ Reload button.
  - Race guard: if the grower dragged the slider in the last 3s,
    the tick is skipped so the auto-reload never yanks the
    playback position out from under a scrubbing session.
  - Reload button label appends " (auto)" while live-polling is
    active to signal to the grower that manual Reload is
    optional.

Deferred to v2+:
  - LRU frame cache (measure first, add if scrubbing feels laggy)
  - Play button with fps control
  - Filter by event source
  - Movie export (workstream #5)
  - Labeling form on this tab (Events tab keeps ownership for now)

Perf note: this tab must stay light for the O-MBE Bulbasaur PC. No
eager frame loads, no per-frame CSV re-reads, no frame decoding on the
UI thread beyond the currently-displayed frame.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import NamedTuple, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from gui.widgets import ScalingImageLabel


# Source-to-marker-color mapping — kept in sync with the Monitor tab's
# footer colors (cyan for continuous capture, amber for manual events,
# grey for auto-capture) so the same visual identity carries across
# tabs. If those Monitor colors change, update here too.
_SOURCE_COLORS = {
    "heartbeat": "#0891b2",  # cyan — continuous capture
    "manual":    "#d97706",  # amber — MARK EVENT
    "view":      "#a855f7",  # purple — RHEED alignment / global QC
    "auto":      "#888888",  # grey — auto-capture engine
}


class FrameIndexEntry(NamedTuple):
    """One point on the scrubber timeline.

    ``source`` is one of ``"heartbeat"`` / ``"manual"`` / ``"view"`` /
    ``"auto"`` and determines the marker color and metadata format.
    ``metadata`` is a free-form dict pulled from the row so the display line
    can show source-specific fields without a big Union type on the tuple.
    """
    elapsed_s: float
    source: str
    source_idx: int
    frame_path: str
    metadata: dict


def _safe_float(row: dict, key: str) -> Optional[float]:
    val = row.get(key, "")
    if val == "" or val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(row: dict, key: str) -> Optional[int]:
    val = row.get(key, "")
    if val == "" or val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def build_frame_index(session_dir: Path) -> list[FrameIndexEntry]:
    """Aggregate the three per-session frame-emitting CSVs into a sorted list.

    Reads (in order): ``heartbeat_log.csv``, ``manual_events.csv``,
    ``rheed_view_events.csv``, ``auto_capture_events.csv``. Missing files
    are silently skipped —
    sessions that finish before an auto-capture event have no
    auto_capture_events.csv rows, sessions with no manual clicks have
    no manual_events.csv rows, etc. Bad rows (missing elapsed_s or
    unparseable numbers) are skipped individually so a single corrupt
    row doesn't take down the whole index.

    Sorted by ``elapsed_s`` ascending — the scrubber slider walks in
    time order regardless of source.

    Auto-capture rows point at ``buffer_dir/*.bmp`` (the first frame
    in the pre-event buffer) since the row itself doesn't carry a
    single canonical frame path. If the buffer_dir is missing or
    empty, ``frame_path`` is left blank and the scrubber will render
    the mark point without a preview image.
    """
    entries: list[FrameIndexEntry] = []

    # --- Heartbeat (continuous capture) ---
    hb_path = session_dir / "heartbeat_log.csv"
    if hb_path.exists():
        try:
            with open(hb_path, newline="") as f:
                for row in csv.DictReader(f):
                    elapsed = _safe_float(row, "elapsed_s")
                    idx = _safe_int(row, "heartbeat_idx")
                    if elapsed is None or idx is None:
                        continue
                    entries.append(FrameIndexEntry(
                        elapsed_s=elapsed,
                        source="heartbeat",
                        source_idx=idx,
                        frame_path=row.get("frame_path", "") or "",
                        metadata={
                            "pyro": row.get("pyrometer_temp_C", ""),
                        },
                    ))
        except OSError:
            pass  # Corrupt or race with writer — skip rest of file

    # --- Manual events (grower marks) ---
    me_path = session_dir / "manual_events.csv"
    if me_path.exists():
        try:
            with open(me_path, newline="") as f:
                for row in csv.DictReader(f):
                    elapsed = _safe_float(row, "elapsed_s")
                    idx = _safe_int(row, "event_idx")
                    if elapsed is None or idx is None:
                        continue
                    entries.append(FrameIndexEntry(
                        elapsed_s=elapsed,
                        source="manual",
                        source_idx=idx,
                        frame_path=row.get("frame_path", "") or "",
                        metadata={
                            "pyro": row.get("pyrometer_temp_C", ""),
                            "V": row.get("voltage_V", ""),
                            "I": row.get("current_A", ""),
                            "psu": row.get("psu_source", ""),
                            "note": row.get("note", ""),
                        },
                    ))
        except OSError:
            pass

    # --- Structured RHEED alignment / global-QC events ---
    view_path = session_dir / "rheed_view_events.csv"
    if view_path.exists():
        try:
            with open(view_path, newline="") as f:
                for row in csv.DictReader(f):
                    elapsed = _safe_float(row, "elapsed_s")
                    idx = _safe_int(row, "event_idx")
                    if elapsed is None or idx is None:
                        continue
                    entries.append(FrameIndexEntry(
                        elapsed_s=elapsed,
                        source="view",
                        source_idx=idx,
                        frame_path=row.get("frame_path", "") or "",
                        metadata={
                            "event_type": row.get("event_type", ""),
                            "segment": row.get("view_segment_id", ""),
                            "gun_aligned": row.get("gun_aligned", ""),
                            "history_ready": row.get("history_ready", ""),
                            "qc_reason": row.get("qc_reason", ""),
                            "note": row.get("note", ""),
                        },
                    ))
        except OSError:
            pass

    # --- Auto-capture events ---
    ac_path = session_dir / "auto_capture_events.csv"
    if ac_path.exists():
        try:
            with open(ac_path, newline="") as f:
                for row in csv.DictReader(f):
                    elapsed = _safe_float(row, "elapsed_s")
                    idx = _safe_int(row, "event_idx")
                    if elapsed is None or idx is None:
                        continue
                    # The row records a buffer_dir, not a single frame.
                    # For v1 preview, glob the first .bmp — enough for
                    # the scrubber to show "something happened here".
                    buffer_dir = row.get("buffer_dir", "") or ""
                    frame_path = ""
                    if buffer_dir:
                        bd = Path(buffer_dir)
                        if bd.exists() and bd.is_dir():
                            bmps = sorted(bd.glob("*.bmp"))
                            if bmps:
                                frame_path = str(bmps[0])
                    entries.append(FrameIndexEntry(
                        elapsed_s=elapsed,
                        source="auto",
                        source_idx=idx,
                        frame_path=frame_path,
                        metadata={
                            "pyro": row.get("pyrometer_temp_C", ""),
                            "score": row.get("change_score", ""),
                            "state": row.get("event_state", ""),
                        },
                    ))
        except OSError:
            pass

    entries.sort(key=lambda e: e.elapsed_s)
    return entries


class _MarkedSlider(QSlider):
    """Horizontal QSlider that paints per-source event markers on the track.

    The base QSlider draws its own track + handle in paintEvent; we call
    ``super().paintEvent(event)`` first so the marks land on top of the
    stock rendering. Marks are tuples of ``(slider_value, source_name)``
    where ``source_name`` keys into ``_SOURCE_COLORS``.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._marks: list[tuple[int, str]] = []

    def set_marks(self, marks: list[tuple[int, str]]) -> None:
        self._marks = list(marks)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._marks:
            return
        span = self.maximum() - self.minimum()
        if span <= 0:
            return
        painter = QPainter(self)
        # Marks sit above the track (top 8 px of the widget) so they
        # don't fight with the handle at click time. Handle stays
        # dominant for scrubbing; marks are informational.
        top_y = 2
        bottom_y = min(8, self.height() - 2)
        width = self.width()
        for value, source in self._marks:
            color = QColor(_SOURCE_COLORS.get(source, "#ffffff"))
            painter.setPen(QPen(color, 2))
            x = int((value - self.minimum()) / span * width)
            painter.drawLine(x, top_y, x, bottom_y)
        painter.end()


class ScrubberTab(QWidget):
    """Timeline scrubber over a growth session's captured frames.

    Public API mirrors ``EventsTab.attach_session`` — GrowthApp calls
    ``attach_session(session_dir)`` at session start (idle → running),
    which reads the CSVs and populates the slider. Reload button forces
    a re-read for live sessions where frames land after attach.

    The scrubber does NOT own the labeling flow — that stays on Events
    tab + Equalizer tab. Its one job is fast, low-friction retrospective
    playback of the growth movie.
    """

    # Emitted when the user scrubs to a new position. Not currently
    # consumed (scrubber is standalone in v1) but kept for a future
    # cross-tab sync (e.g., "when scrubber moves, Events tab highlights
    # the matching row").
    position_changed = pyqtSignal(int)

    # Race-guard window: if the grower dragged the slider in the last
    # N seconds, skip the next auto-poll tick. Keeps _reload_index
    # from stealing the playback position mid-scrub. 3s is enough
    # slack for a grower to think between drags without also
    # blocking useful reloads.
    AUTO_POLL_INTERVAL_MS: int = 5000
    AUTO_POLL_RACE_GUARD_S: float = 3.0

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._session_dir: Optional[Path] = None
        self._index: list[FrameIndexEntry] = []
        self._current_pos: int = 0
        # Live-polling state — flipped via set_live_polling(bool).
        # Default False so tests + standalone launches don't start
        # firing timers before a session is armed.
        self._live_polling: bool = False
        self._last_slider_interaction: float = 0.0
        # QTimer is parented to self so Qt cleans it up on tab
        # destruction; setSingleShot(False) means it re-fires each
        # AUTO_POLL_INTERVAL_MS until stopped.
        self._auto_poll_timer = QTimer(self)
        self._auto_poll_timer.setInterval(self.AUTO_POLL_INTERVAL_MS)
        self._auto_poll_timer.setSingleShot(False)
        self._auto_poll_timer.timeout.connect(self._on_auto_poll_tick)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Frame image — top, biggest single element.
        # Shared widget extracted Jul 13 2026; scrubber preserves its larger
        # 480×360 minimum-size hint (typical playback pane has more vertical
        # space than the events-tab buffer preview).
        self._image_label = ScalingImageLabel(minimum_size=(480, 360))
        layout.addWidget(self._image_label, 1)

        # Metadata line under the image.
        self._metadata_label = QLabel("Attach a session to begin scrubbing.")
        self._metadata_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._metadata_label.setStyleSheet("font-size: 12px; color: #aaa;")
        layout.addWidget(self._metadata_label)

        # Timeline slider with per-source event markers.
        self._slider = _MarkedSlider()
        self._slider.setEnabled(False)
        self._slider.setMinimumHeight(24)
        self._slider.valueChanged.connect(self._on_slider_changed)
        layout.addWidget(self._slider)

        # Navigation row: Prev / Next / position / Reload.
        nav = QHBoxLayout()
        nav.setSpacing(8)

        self._prev_btn = QPushButton("◄ Prev")
        self._prev_btn.setFixedHeight(32)
        self._prev_btn.clicked.connect(self._on_prev)
        nav.addWidget(self._prev_btn)

        self._next_btn = QPushButton("Next ►")
        self._next_btn.setFixedHeight(32)
        self._next_btn.clicked.connect(self._on_next)
        nav.addWidget(self._next_btn)

        self._position_label = QLabel("Frame 0 / 0")
        self._position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._position_label.setStyleSheet("font-size: 12px; color: #ccc;")
        nav.addWidget(self._position_label, 1)

        # Reload — for live sessions where new frames land after attach.
        # Auto-polling deferred; manual button is the low-effort v1
        # answer that still gives the grower a way to refresh.
        self._reload_btn = QPushButton("↻ Reload")
        self._reload_btn.setFixedHeight(32)
        self._reload_btn.setToolTip(
            "Re-read the session CSVs to pick up frames that landed after "
            "this tab was attached. Auto-refresh is a future enhancement."
        )
        self._reload_btn.clicked.connect(self._on_reload)
        nav.addWidget(self._reload_btn)

        layout.addLayout(nav)

        # Legend row — quick visual key for the marker colors.
        legend = QHBoxLayout()
        legend.setSpacing(12)
        legend.setContentsMargins(0, 0, 0, 0)
        for src, label in (
            ("heartbeat", "continuous"),
            ("manual", "manual event"),
            ("view", "RHEED QC/alignment"),
            ("auto", "auto-capture"),
        ):
            swatch = QLabel("█")
            swatch.setStyleSheet(
                f"color: {_SOURCE_COLORS[src]}; font-size: 12px;"
            )
            text = QLabel(label)
            text.setStyleSheet("font-size: 10px; color: #888;")
            legend.addWidget(swatch)
            legend.addWidget(text)
        legend.addStretch(1)
        layout.addLayout(legend)

        self._set_nav_enabled(False)

    # ----- Session lifecycle -----------------------------------------------

    def attach_session(self, session_dir: Optional[Path]) -> None:
        """Point the tab at a session directory and read the CSVs.

        Passing ``None`` clears the tab back to the placeholder state.
        Mirrors EventsTab.attach_session's contract but takes the
        session_dir directly (no logger dep) since the scrubber is
        read-only against the on-disk CSVs.
        """
        self._session_dir = session_dir
        self._reload_index()

    # ----- Internal --------------------------------------------------------

    def _reload_index(self):
        if self._session_dir is None:
            self._reset_to_placeholder("Attach a session to begin scrubbing.")
            return

        self._index = build_frame_index(self._session_dir)
        n = len(self._index)
        if n == 0:
            self._reset_to_placeholder(
                "No frames captured yet in this session. "
                "Wait for a heartbeat or press ↻ Reload.",
            )
            return

        self._slider.blockSignals(True)
        self._slider.setRange(0, n - 1)
        self._slider.set_marks([(i, e.source) for i, e in enumerate(self._index)])
        self._slider.setEnabled(True)
        self._slider.blockSignals(False)

        # Preserve position on reload if still in range; otherwise start
        # at 0. Keeps the grower's context when they hit Reload to pick
        # up new frames.
        pos = min(self._current_pos, n - 1) if self._current_pos >= 0 else 0
        self._current_pos = pos
        self._slider.setValue(pos)
        self._display_frame(pos)
        self._set_nav_enabled(True)

    def _reset_to_placeholder(self, message: str):
        self._index = []
        self._current_pos = 0
        self._slider.blockSignals(True)
        self._slider.setRange(0, 0)
        self._slider.setValue(0)
        self._slider.set_marks([])
        self._slider.setEnabled(False)
        self._slider.blockSignals(False)
        self._image_label.clearImage()
        self._metadata_label.setText(message)
        self._position_label.setText("Frame 0 / 0")
        self._set_nav_enabled(False)

    def _on_slider_changed(self, value: int):
        if value == self._current_pos:
            return
        # Timestamp any user-driven position change so the auto-poll
        # tick can back off during active scrubbing (see
        # _on_auto_poll_tick + AUTO_POLL_RACE_GUARD_S). Programmatic
        # position updates (attach_session, Prev/Next buttons) also
        # go through here — the race guard is intentionally
        # conservative, treating any position change as
        # user-initiated. Cost: one skipped auto-reload after a
        # Prev/Next click. Benefit: no false-positive scrub-stealing.
        self._last_slider_interaction = time.time()
        self._current_pos = value
        self._display_frame(value)
        self.position_changed.emit(value)

    def _on_prev(self):
        if self._current_pos > 0:
            self._slider.setValue(self._current_pos - 1)

    def _on_next(self):
        if self._current_pos < len(self._index) - 1:
            self._slider.setValue(self._current_pos + 1)

    def _on_reload(self):
        self._reload_index()

    # ----- Live auto-poll --------------------------------------------------

    def set_live_polling(self, enabled: bool) -> None:
        """Turn the 5-second auto-reload timer on or off.

        GrowthApp calls this from _on_start (True) and _on_stop
        (False). Idempotent — calling True twice, or False on a
        non-active timer, is a no-op. The reload button label
        reflects state so the grower always sees whether background
        refresh is active.
        """
        enabled = bool(enabled)
        if enabled == self._live_polling:
            return
        self._live_polling = enabled
        if enabled:
            self._auto_poll_timer.start()
            self._reload_btn.setText("↻ Reload (auto)")
            self._reload_btn.setToolTip(
                "Session is live-polling every "
                f"{self.AUTO_POLL_INTERVAL_MS // 1000}s — the tab "
                "picks up new frames automatically. Click to force "
                "an immediate reload."
            )
        else:
            self._auto_poll_timer.stop()
            self._reload_btn.setText("↻ Reload")
            self._reload_btn.setToolTip(
                "Re-read the session CSVs to pick up frames that "
                "landed after this tab was attached."
            )

    def _on_auto_poll_tick(self) -> None:
        """Timer callback — reload the index unless the grower is scrubbing.

        Race guard: if the last user slider interaction was less
        than AUTO_POLL_RACE_GUARD_S ago, skip this tick to avoid
        yanking the playback position out from under an active
        drag. The next tick fires AUTO_POLL_INTERVAL_MS later and
        will pick up any new frames that landed in the interim.
        """
        if (time.time() - self._last_slider_interaction
                < self.AUTO_POLL_RACE_GUARD_S):
            return
        self._reload_index()

    def _display_frame(self, pos: int):
        if not (0 <= pos < len(self._index)):
            return
        entry = self._index[pos]

        # Lazy load — no cache in v1. QPixmap(path) is the whole load;
        # PIL is not needed because QPixmap can read BMP directly.
        path = entry.frame_path
        if path and Path(path).exists():
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self._image_label.setOriginalPixmap(pixmap)
            else:
                self._image_label.clearImage()
        else:
            self._image_label.clearImage()

        self._metadata_label.setText(_format_metadata(entry))
        self._position_label.setText(
            f"Frame {pos + 1} / {len(self._index)}  ·  {entry.source} "
            f"#{entry.source_idx}"
        )

    def _set_nav_enabled(self, enabled: bool):
        self._prev_btn.setEnabled(enabled)
        self._next_btn.setEnabled(enabled)
        self._reload_btn.setEnabled(enabled or self._session_dir is not None)


def _format_metadata(entry: FrameIndexEntry) -> str:
    """One-line summary of the frame's context. Skips blank fields."""
    total = int(entry.elapsed_s)
    mm_ss = f"{total // 60:02d}:{total % 60:02d}"
    parts = [f"[{mm_ss}]"]
    md = entry.metadata

    # Common fields — pyro applies to all sources.
    pyro = md.get("pyro", "")
    if pyro:
        parts.append(f"pyro {pyro}°C")

    # Manual-event-specific fields.
    if entry.source == "manual":
        v = md.get("V", "")
        i = md.get("I", "")
        if v:
            parts.append(f"V {v}")
        if i:
            parts.append(f"I {i}")
        psu = md.get("psu", "")
        if psu and psu != "none":
            parts.append(f"[{psu}]")
        note = md.get("note", "")
        if note:
            parts.append(f'"{note}"')

    # Structured view/QC boundary fields.
    if entry.source == "view":
        event_type = md.get("event_type", "")
        if event_type:
            parts.append(event_type)
        segment = md.get("segment", "")
        if segment != "":
            parts.append(f"segment {segment}")
        aligned = md.get("gun_aligned", "")
        if aligned != "":
            parts.append(f"aligned={aligned}")
        ready = md.get("history_ready", "")
        if ready != "":
            parts.append(f"history_ready={ready}")
        reason = md.get("qc_reason", "")
        if reason:
            parts.append(f"reason: {reason}")
        note = md.get("note", "")
        if note:
            parts.append(f'"{note}"')

    # Auto-capture-specific fields.
    if entry.source == "auto":
        score = md.get("score", "")
        if score:
            parts.append(f"score {score}")
        state = md.get("state", "")
        if state:
            parts.append(f"[{state}]")

    return "  ·  ".join(parts)
