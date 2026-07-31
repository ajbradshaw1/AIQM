"""
MBE Growth Monitor — application orchestrator.

Manages worker lifecycle, signal fan-out from workers to the monitor widget,
and ARM / START / STOP / DISARM session state transitions.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtWidgets import QFileDialog, QMainWindow
from PyQt6.QtCore import pyqtSlot, Qt, QTimer

log = logging.getLogger(__name__)

from gui.auto_capture import AutoCaptureEngine, PixelDiffChangeDetector
from gui.growth_logger import (
    EVENT_STATE_AUTO_SKIPPED,
    EVENT_STATE_DISCARDED,
    EVENT_STATE_PENDING,
)
from gui.state import (
    CameraState, ClassifierState, EvapControlState, MistralState, PyrometerState,
)
from gui.workers import (
    ClassifierWorker, EvapControlWorker, MistralWorker,
    PyrometerWorker, RheedCameraWorker,
)
from gui.growth_monitor import GrowthMonitor
from gui.growth_logger import GrowthLogger
from gui.movie_export import (
    DEFAULT_MOVIE_NAME,
    MovieExporter,
    MovieExportWorker,
)
from gui.rheed_intensity_window import RheedIntensityWindow
from gui.pyrometer_window import PyrometerWindow


# Tuned against Rahim's 2022_02_04 STO trajectory.
#
# Rahim's data was captured at ~30 s cadence (manual). At our 1 Hz live
# cadence, a 20-frame buffer covers 20 s — much shorter than Rahim's
# 10-minute equivalent — so slow ramp drift contributes negligibly to
# the live baseline. That makes our live buffer-mean(20)@1Hz behaviorally
# closer to Rahim's *previous-frame* mode than to his *buffer-mean(20)*:
#   - previous-frame on Rahim:   baseline 0.5,  peaks 2.5-9.0 → threshold 1.5
#   - buffer-mean(5) on Rahim:   baseline 0.70, peaks 6.4-9.0 → similar
#   - buffer-mean(20) on Rahim:  baseline 1.15, peaks 4.9-8.5 (loses small
#                                events) — NOT representative of live behavior
# We expect live baseline ~0.5-1.0; smallest real transitions ~2.5+. A
# threshold of 2.0 sits ~3x above the expected baseline and below the
# smallest expected event. First lab session is the real ground truth —
# adjust after reviewing auto_capture_events.csv vs grower notes.
AUTO_CAPTURE_THRESHOLD = 2.0
AUTO_CAPTURE_BUFFER_SIZE = 20
AUTO_CAPTURE_COOLDOWN_S = 10.0
AUTO_CAPTURE_ADAPTIVE_SIGMA = 3.0
AUTO_CAPTURE_ADAPTIVE_FLOOR = 0.5

# Heartbeat dense-capture: every N seconds during a session, save the
# latest RHEED frame regardless of detector flags. Per the May 21 joint
# decision and Yuxin's CS-side "background data" request, capture as
# much continuous data as possible — frames between detector events
# that show what's happening when nothing has been flagged. Lets the
# model learn when *not* to act, and captures grower-initiated V/I
# changes that detector-based capture would miss. PI affirmed 5 s as
# the working default at the joint meeting.
#
# Tunable via env var AIQM_HEARTBEAT_INTERVAL_SECONDS for grower-side
# adjustment without code edits.
HEARTBEAT_INTERVAL_SECONDS = float(
    os.environ.get("AIQM_HEARTBEAT_INTERVAL_SECONDS", "5.0")
)

# MISTRAL set-V/I change detection — derives "operator pressed Set Voltage /
# Set Current" events from successive OCR'd setpoint values changing.
# Tolerances are larger than typical OCR jitter but smaller than any
# meaningful operator-commanded change.
SET_V_CHANGE_TOLERANCE = 0.05  # volts
SET_I_CHANGE_TOLERANCE = 0.01  # amps

WORKSPACE_DIR = Path(__file__).resolve().parents[1]


def resolve_workspace_folder(folder: str | Path) -> Path:
    """Resolve relative output folders inside this repository."""
    path = Path(folder).expanduser()
    if not path.is_absolute():
        path = WORKSPACE_DIR / path
    return path


# Known AI_for_quantum clone locations, in preference order. Kept in
# sync with EventsTab._KNOWN_AI_REPO_ROOTS in gui/events_tab.py — if
# a third caller shows up, move both to a shared module.
_KNOWN_AI_REPO_ROOTS = [
    # Local workstation — RHEEDClassify repository.
    r"D:\AI4MBE\RHEEDClassify",
    # Bulbasaur (O-MBE)
    r"C:\Users\Lab10\AI_for_quantum",
    # Ch-MBE (Omicron chalcogenide MBE) — added 2026-07-21
    r"C:\Users\Omicron\AI_for_quantum",
    # AJ's Mac dev clone
    "/Users/aj/test-claude/projects/ai-for-quantum",
]


def _resolve_ai_repo_root() -> str:
    """Find the AI_for_quantum repo path for ClassifierBridge.

    Precedence:
      1. ``AI_REPO_ROOT`` env var — per-machine escape hatch
      2. First existing path from ``_KNOWN_AI_REPO_ROOTS``
      3. First entry as fallback (error message will point at a concrete
         path we tried)

    Kept in sync with ``gui/events_tab.py::_default_ai_repo_root``.
    """
    env = os.environ.get("AI_REPO_ROOT")
    if env:
        return env
    from pathlib import Path
    for candidate in _KNOWN_AI_REPO_ROOTS:
        if Path(candidate).exists():
            return candidate
    return _KNOWN_AI_REPO_ROOTS[0]


class GrowthApp(QMainWindow):
    """Main window for the OMBE Growth Monitor application."""

    def __init__(self):
        super().__init__()
        from drivers.config import get_active_config
        self._chamber_config = get_active_config()
        self.setWindowTitle(f"{self._chamber_config.name} Growth Monitor")
        self.setMinimumSize(1000, 700)

        self.camera_worker: Optional[RheedCameraWorker] = None
        self.pyrometer_worker: Optional[PyrometerWorker] = None
        self.mistral_worker: Optional[MistralWorker] = None
        self.evap_worker: Optional[EvapControlWorker] = None
        self.classifier_worker: Optional[ClassifierWorker] = None
        # Latest classifier state, cached from _on_classifier_state so the
        # auto-capture event handler can snapshot it into each event
        # directory (saves the model's opinion at trigger time — feeds the
        # CS-team training-data pipeline).
        self._latest_classifier: Optional[ClassifierState] = None
        self.growth_log = GrowthLogger()

        # Periodic sensor logging timer (1 second interval while running)
        self._sensor_log_timer = QTimer(self)
        self._sensor_log_timer.setInterval(1000)
        self._sensor_log_timer.timeout.connect(self._log_sensors)

        # Heartbeat dense-capture timer — fires every N seconds during a
        # session and saves whatever the latest RHEED frame is. See
        # HEARTBEAT_INTERVAL_SECONDS for the May 21 joint decision context.
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(int(HEARTBEAT_INTERVAL_SECONDS * 1000))
        self._heartbeat_timer.timeout.connect(self._on_heartbeat)

        # Auto-capture engine — shadow-mode pixel-diff change detection.
        # Engine is disarmed at construction; armed in _on_start, disarmed
        # in _on_stop. Frame ingestion happens in _on_camera_state.
        self.auto_capture_engine = AutoCaptureEngine(
            threshold=AUTO_CAPTURE_THRESHOLD,
            cooldown_s=AUTO_CAPTURE_COOLDOWN_S,
            warmup_frames=AUTO_CAPTURE_BUFFER_SIZE,
            adaptive_sigma=AUTO_CAPTURE_ADAPTIVE_SIGMA,
            adaptive_floor=AUTO_CAPTURE_ADAPTIVE_FLOOR,
        )
        # Hardcoded opt-in for specular-anchored ROI + std-of-|diff|;
        # remove these kwargs to fall back to full-frame mean-of-|diff|.
        self.auto_capture_engine.set_detector(
            PixelDiffChangeDetector(
                buffer_size=AUTO_CAPTURE_BUFFER_SIZE,
                score_metric="std",
                roi_mode="specular",
            ),
        )
        self.auto_capture_engine.frame_captured.connect(
            self._on_auto_capture_event,
        )
        self._auto_capture_event_count = 0

        # Movie export worker (Jul 10 2026). QThread that runs
        # MovieExporter.export_movie off the GUI thread. Held on the
        # instance so it doesn't get garbage-collected mid-encode.
        # Single-worker policy: a second click while one is running is
        # ignored — the button gets disabled during encode.
        self._movie_worker: Optional[MovieExportWorker] = None

        # Tracking for MISTRAL set V/I change detection. Initialised to None
        # at construction; reset to None in _on_start so each session detects
        # changes relative to its own first reading, not the previous session.
        self._last_v_set: Optional[float] = None
        self._last_i_set: Optional[float] = None

        # Central widget — pass chamber config so the monitor can set
        # cell display labels and driver mode defaults from it.
        self.monitor = GrowthMonitor(config=self._chamber_config)
        self.setCentralWidget(self.monitor)

        # Connect monitor signals → app handlers
        self.monitor.arm_requested.connect(self._on_arm)
        self.monitor.disarm_requested.connect(self._on_disarm)
        self.monitor.reconnect_rheed_requested.connect(
            self._on_reconnect_rheed,
        )
        self.monitor.start_requested.connect(self._on_start)
        self.monitor.stop_requested.connect(self._on_stop)
        self.monitor.commit_requested.connect(self._on_commit)
        # Grower-marked events (Jul 10 2026 group-meeting design shift).
        # Widget emits the pyro/PSU/note snapshot; app-side attaches the
        # current frame from the monitor's cache and calls
        # logger.record_manual_event. Frame attachment lives here (not in
        # the widget) so the widget stays independent of the growth-log
        # file lifecycle.
        self.monitor.manual_event_requested.connect(self._on_manual_event)
        self.monitor.export_requested.connect(self._on_export)
        # Movie export (Jul 10 2026 workstream #5) — file dialog + QThread
        # encode. Handler owns the worker lifetime so cross-session
        # exports don't leak workers into the background.
        self.monitor.movie_export_requested.connect(self._on_movie_export)
        # Live Equalizer save (Jul 10 2026 workstream #4) — the tab emits
        # the current slider weights on Save; app-side handler snapshots
        # the sensor state + calls logger.record_live_label. Frame comes
        # from the tab's own get_current_full_frame() so it's the frame
        # the grower was actually looking at, not a race-lagged monitor
        # cache.
        self.monitor.live_equalizer_tab.live_label_save_requested.connect(
            self._on_live_label_save,
        )
        self.monitor.auto_capture_pause_toggled.connect(
            self._on_auto_capture_pause_toggled,
        )
        self.monitor.auto_capture_decision.connect(
            self._on_auto_capture_decision,
        )

        # Events tab listens to frame_captured AFTER GrowthApp's own
        # handler, so the CSV row is already on disk when the tab re-reads
        # it. Connection order is the synchronization mechanism — the
        # engine emits to slots in the order they were connected.
        self.auto_capture_engine.frame_captured.connect(
            self.monitor.events_tab.on_frame_captured,
        )
        # Events tab also reflects banner Keep/Discard decisions in its
        # state column + unreviewed badge. Same connection-order reasoning:
        # GrowthApp's handler (above) writes the CSV first, the tab updates
        # its UI second using the state arg from the signal payload.
        self.monitor.auto_capture_decision.connect(
            self.monitor.events_tab.on_decision_made,
        )

        # Floating trend windows — growers can open and drag to a second
        # monitor. Fed through _on_camera_state/_on_pyrometer_state (not
        # directly from workers) so they survive worker recreation on
        # session restart without needing reconnection.
        self.rheed_intensity_window = RheedIntensityWindow()
        self.pyrometer_window = PyrometerWindow()
        self.monitor.open_rheed_trend_requested.connect(
            lambda: (
                self.rheed_intensity_window.show(),
                self.rheed_intensity_window.raise_(),
            )
        )
        self.monitor.open_temp_plot_requested.connect(
            lambda: (
                self.pyrometer_window.show(),
                self.pyrometer_window.raise_(),
            )
        )

    # --- ARM / DISARM ------------------------------------------------------

    @pyqtSlot()
    def _on_arm(self):
        """Connect camera, pyrometer, MISTRAL, and Evap Control workers."""
        camera_mode = self.monitor.config_camera_mode.currentText()
        pyrometer_mode = self.monitor.config_pyrometer_mode.currentText()
        mistral_mode = self.monitor.config_mistral_mode.currentText()
        evap_mode = self.monitor.config_evap_mode.currentText()

        new_camera_worker = False
        if not self.camera_worker or not self.camera_worker.isRunning():
            self.camera_worker = RheedCameraWorker(
                mode=camera_mode, poll_interval=1.0,
            )
            self.camera_worker.state_updated.connect(self._on_camera_state)
            self.camera_worker.start()
            new_camera_worker = True

        # Live RHEED classifier — reads frames from the camera worker via a
        # DirectConnection slot (mutex-protected write in sender's thread,
        # no receiver event loop required) and emits smoothed
        # classification percentages that drive the main-tab sliders.
        # Bridge load happens inside the worker thread so the UI stays
        # responsive during the ~1-2 s model load. If AI_for_quantum or
        # torch is missing, the worker emits an error state and the
        # sliders show "Classifier unavailable" — non-blocking for the
        # rest of the app. Skipped entirely when the config checkbox is
        # unchecked so a misbehaving classifier can't block a session.
        classifier_enabled = self.monitor.config_classifier_enabled.isChecked()
        if classifier_enabled and (
            not self.classifier_worker or not self.classifier_worker.isRunning()
        ):
            self.classifier_worker = ClassifierWorker(
                ai_repo_root=_resolve_ai_repo_root(),
            )
            self.camera_worker.state_updated.connect(
                self.classifier_worker.on_rheed_state,
                Qt.ConnectionType.DirectConnection,
            )
            self.classifier_worker.state_updated.connect(
                self._on_classifier_state,
            )
            self.classifier_worker.start()
        elif not classifier_enabled:
            # Defensive stop: if a previous arm cycle left a classifier
            # worker running and the user re-armed with the checkbox
            # off, kill it so the UI doesn't get updates from a stale
            # worker while showing "disabled".
            if self.classifier_worker and self.classifier_worker.isRunning():
                self._stop_workers(self.classifier_worker)
                self.classifier_worker = None
            # Explicit "disabled" signal to the monitor so the sliders
            # show a clear message instead of just staying at "idle".
            self.monitor.set_classifier_disabled()
        elif new_camera_worker and self.classifier_worker.isRunning():
            # A fail-closed WGC worker can be re-armed while the classifier
            # thread is still alive. Wire the replacement camera worker to
            # that existing classifier instance.
            self.camera_worker.state_updated.connect(
                self.classifier_worker.on_rheed_state,
                Qt.ConnectionType.DirectConnection,
            )

        if not self.pyrometer_worker or not self.pyrometer_worker.isRunning():
            exactus_port = self.monitor.config_exactus_port.text().strip() or "COM4"
            exactus_baud = int(self.monitor.config_exactus_baud.currentText())
            self.pyrometer_worker = PyrometerWorker(
                mode=pyrometer_mode,
                poll_interval=0.5,
                port=exactus_port,
                baudrate=exactus_baud,
            )
            self.pyrometer_worker.state_updated.connect(self._on_pyrometer_state)
            self.pyrometer_worker.start()

        if not self.mistral_worker or not self.mistral_worker.isRunning():
            self.mistral_worker = MistralWorker(
                mode=mistral_mode, poll_interval=1.0,
            )
            self.mistral_worker.state_updated.connect(self._on_mistral_state)
            self.mistral_worker.start()

        if not self.evap_worker or not self.evap_worker.isRunning():
            self.evap_worker = EvapControlWorker(
                mode=evap_mode, poll_interval=1.0,
            )
            self.evap_worker.state_updated.connect(self._on_evap_state)
            self.evap_worker.start()

        self.monitor.set_state("armed")
        self.statusBar().showMessage("Armed \u2014 live readings active")

    @pyqtSlot()
    def _on_disarm(self):
        """Disconnect all hardware workers (parallel stop for minimal lag)."""
        self._stop_workers(
            self.camera_worker,
            self.pyrometer_worker,
            self.mistral_worker,
            self.evap_worker,
            self.classifier_worker,
        )
        self.camera_worker = None
        self.pyrometer_worker = None
        self.mistral_worker = None
        self.evap_worker = None
        self.classifier_worker = None

        self.monitor.reset_displays()
        self.monitor.set_state("idle")
        self.statusBar().showMessage("Disarmed \u2014 idle")

    @pyqtSlot()
    def _on_reconnect_rheed(self):
        """Reconnect only WGC while preserving the current ARM/session state."""
        if self.monitor._state == "idle":
            self.statusBar().showMessage(
                "ARM the monitor before reconnecting RHEED.",
                5000,
            )
            return
        if self.monitor.config_camera_mode.currentText() != "screengrab":
            self.statusBar().showMessage(
                "In-session reconnect is restricted to WGC screengrab mode.",
                5000,
            )
            return

        self.monitor.set_rheed_reconnect_required(
            False, in_progress=True,
        )
        if self.camera_worker is not None and self.camera_worker.isRunning():
            self._stop_workers(self.camera_worker)
        self.camera_worker = RheedCameraWorker(
            mode="screengrab", poll_interval=1.0,
        )
        self.camera_worker.state_updated.connect(self._on_camera_state)
        if self.classifier_worker and self.classifier_worker.isRunning():
            self.camera_worker.state_updated.connect(
                self.classifier_worker.on_rheed_state,
                Qt.ConnectionType.DirectConnection,
            )
        self.camera_worker.start()
        self.statusBar().showMessage(
            "Reconnecting detached kSA Live Video...", 5000,
        )

    # --- START / STOP ------------------------------------------------------

    @pyqtSlot()
    def _on_start(self):
        """Begin a growth session — start logging."""
        # Apply config panel settings
        save_path = self.monitor.config_save_path.text().strip()
        prefix = self.monitor.config_prefix.text().strip()
        if save_path:
            self.growth_log._base_dir = resolve_workspace_folder(save_path)
        if prefix:
            self.growth_log._filename_prefix = prefix

        sample_id = self.monitor.sample_id_input.text().strip() or "unnamed"
        self.growth_log.start_session(sample_id)

        # Clear trend window histories so a new growth starts fresh —
        # without this, a second growth inherits the first growth's trace.
        self.rheed_intensity_window.reset()
        self.pyrometer_window.reset()

        # Point the events tab at this session's logger so backfill
        # (handles GUI-restart-mid-session), the live append-on-signal,
        # and labeling reads/writes all flow through the same source.
        self.monitor.events_tab.attach_session(self.growth_log)
        # Point the scrubber tab at the session dir so its ↻ Reload
        # button can build the frame index against the correct session.
        # Reads three CSVs (heartbeat / manual / auto-capture); missing
        # or empty CSVs are handled gracefully so attach right at session
        # start (before any heartbeat has fired) just shows the "No
        # frames captured yet" placeholder until the grower hits Reload.
        self.monitor.scrubber_tab.attach_session(self.growth_log.session_dir)
        # Turn on live auto-poll: the scrubber's 5s QTimer will pick
        # up new heartbeat/manual/auto-capture rows without the
        # grower having to hit ↻ Reload. The race guard inside
        # scrubber_tab defers reloads if the grower is actively
        # scrubbing, so background refresh doesn't yank playback
        # position mid-drag. Turned off in _on_stop.
        self.monitor.scrubber_tab.set_live_polling(True)
        # Reset the other two session-scoped tables (Sensor Log + Growth
        # Notes). Events tab handles its own reset inside attach_session.
        self.monitor.clear_session_tables()

        interval_ms = int(self.monitor.config_interval_spin.value() * 1000)
        self._sensor_log_timer.setInterval(interval_ms)
        self._sensor_log_timer.start()

        # Arm shadow-mode auto-capture for this session
        self.auto_capture_engine.reset()
        self.auto_capture_engine.enabled = True
        self._auto_capture_event_count = 0
        self.monitor.set_auto_capture_status(
            "Auto-capture: armed (warmup)"
        )
        self.monitor.set_auto_capture_pause_enabled(True)

        # Reset MISTRAL set-tracking so we don't fire a false change event
        # against the previous session's last value.
        self._last_v_set = None
        self._last_i_set = None

        # Start heartbeat dense-capture. Interval comes from the Config
        # tab spinbox (since Jun 23) — falls back to HEARTBEAT_INTERVAL_SECONDS
        # (env-var or default) if the monitor widget isn't reachable, but
        # in practice the spinbox always wins. First fire is after one
        # full interval; we don't fire-on-start to avoid saving a black
        # frame before the camera has produced anything useful.
        try:
            heartbeat_s = float(
                self.monitor.config_heartbeat_interval_spin.value()
            )
        except AttributeError:
            heartbeat_s = HEARTBEAT_INTERVAL_SECONDS
        self._heartbeat_timer.setInterval(int(heartbeat_s * 1000))
        self._heartbeat_timer.start()
        _frames_per_hr = round(3600 / heartbeat_s)
        _est_mb_per_hr = _frames_per_hr * 200 / 1024  # ~200 KB/frame PNG estimate
        log.info(
            "Heartbeat: every %.1f s -> ~%d frames/hr "
            "(~%.0f MB/hr at ~200 KB/frame PNG estimate)",
            heartbeat_s, _frames_per_hr, _est_mb_per_hr,
        )
        # Surface the cadence on the Monitor tab so growers can confirm at
        # a glance that the "movie" is being captured (Jul 10 2026 group-
        # meeting design pressure). The footer indicator counts up on
        # every successful heartbeat frame save.
        self.monitor.set_continuous_capture_interval(heartbeat_s)

        self.monitor.set_state("running")
        self.statusBar().showMessage(f"Running \u2014 {sample_id}")

    @pyqtSlot()
    def _on_stop(self):
        """End a growth session — save metadata, auto-export, stop logging."""
        self._sensor_log_timer.stop()
        self._heartbeat_timer.stop()

        # Turn off scrubber live-polling — the session's CSVs will
        # stop being appended to, so background reloads only waste
        # cycles. Set BEFORE growth_log.end_session so the timer
        # doesn't race with a partially-closed CSV.
        self.monitor.scrubber_tab.set_live_polling(False)

        # Disarm auto-capture; engine state cleaned up so the next session
        # starts fresh in _on_start.
        self.auto_capture_engine.enabled = False
        self.monitor.set_auto_capture_status(
            f"Auto-capture: idle "
            f"({self._auto_capture_event_count} events this session)"
        )
        self.monitor.set_auto_capture_pause_enabled(False)

        metadata = self.monitor.get_session_metadata()

        # Save session metadata
        self.growth_log.save_session_metadata(metadata)

        # Auto-export growth log before closing session files
        path = self.growth_log.export_growth_log(metadata)

        self.growth_log.end_session()

        # Auto-generate T vs t plot from sensor_log.csv (Frankie-style).
        # Soft-deps matplotlib; returns None and logs a warning if absent
        # or the pyrometer was offline all session. The plot is a
        # post-mortem convenience \u2014 failure doesn't block STOP completion.
        try:
            plot_path = self.growth_log.generate_temperature_plot(metadata)
            if plot_path:
                log.info("Auto T vs t plot saved: %s", plot_path)
        except Exception as e:
            log.warning("Auto T vs t plot failed: %s", e)

        self.monitor.set_state("armed")

        if path:
            self.statusBar().showMessage(
                f"Session saved \u2014 growth log: {path}", 8000,
            )
        else:
            self.statusBar().showMessage("Stopped \u2014 session saved")

    # --- LOG ENTRY ---------------------------------------------------------

    @pyqtSlot(dict)
    def _on_commit(self, entry: dict):
        """Save a timestamped log entry with optional RHEED frame.

        LOG ENTRY frame contract (Jul 8 2026): the frame is saved
        unconditionally when available — the grower's explicit click is
        stronger evidence of intent than the automated quality gate.
        The gate's verdict is recorded as ``frame_quality_pass`` metadata
        so downstream consumers can still filter to high-quality frames.
        """
        # Save frame if available
        frame = self.monitor.get_current_frame()
        if frame is not None:
            capture_metadata = self.monitor.get_current_capture_metadata()
            ts = entry.get("timestamp", "").replace(":", "").split(".")[0][-6:]
            path, quality_pass = self.growth_log.save_frame(frame, ts)
            entry["frame_path"] = path
            entry.update(capture_metadata)
            # quality_pass may be None if the quality-gate module isn't
            # importable; propagate that as blank so the CSV column stays
            # unambiguous ("" ≠ True ≠ False).
            if quality_pass is None:
                entry["frame_quality_pass"] = ""
            else:
                entry["frame_quality_pass"] = "True" if quality_pass else "False"
        # If no frame was available (camera not connected / no state
        # emitted yet), leave both fields as their default "" from the
        # log_commit dict comprehension — captures "no frame" cleanly.

        self.growth_log.log_commit(entry)
        self.statusBar().showMessage("Entry logged", 3000)

    # --- MARK EVENT --------------------------------------------------------

    @pyqtSlot(dict)
    def _on_manual_event(self, payload: dict):
        """Write a grower-marked event to manual_events.csv.

        Payload arrives with the pyro/PSU/note snapshot already assembled
        by the widget. This handler attaches the current frame from the
        monitor's cache (same source LOG ENTRY uses) and hands off to
        the logger. The logger writes both the CSV row and the BMP
        preview under ``frames/``.

        Silent no-op if no session is active — matches the widget's
        button gating and the logger's record_manual_event contract.
        """
        frame = self.monitor.get_current_frame()
        capture_metadata = self.monitor.get_current_capture_metadata()
        self.growth_log.record_manual_event(
            elapsed_s=payload.get("elapsed_s", 0.0),
            pyro_temp=payload.get("pyro_temp"),
            voltage_V=payload.get("voltage_V"),
            current_A=payload.get("current_A"),
            psu_source=payload.get("psu_source", "none"),
            frame=frame,
            note=payload.get("note", ""),
            capture_metadata=capture_metadata,
        )
        self.statusBar().showMessage("Event marked", 2000)

    # --- LIVE EQUALIZER save (Jul 10 2026 workstream #4) -------------------

    @pyqtSlot(dict)
    def _on_live_label_save(self, weights: dict):
        """Snapshot current sensor state + write a live_labels.csv row.

        Frame comes from the LiveEqualizerTab's own cache
        (``get_current_full_frame``) rather than ``monitor.get_current_frame``
        — the tab-cached frame is what the grower was actually looking at
        while balancing sliders, which is the training-signal ground
        truth. Difference is usually one frame either way (~30-500 ms).

        No-ops when there's no live frame (grower opened the tab pre-
        session and hit Save). PSU + pyro snapshot pattern mirrors
        ``_on_manual_event`` for consistency across the three label
        surfaces (LOG ENTRY / MARK EVENT / Live Equalizer).
        """
        if not self.growth_log.active:
            self.statusBar().showMessage(
                "Start a session before saving live labels.", 3000,
            )
            return

        frame = self.monitor.live_equalizer_tab.get_current_full_frame()
        capture_metadata = (
            self.monitor.live_equalizer_tab.get_current_capture_metadata()
        )

        # PSU snapshot — identical priority to _on_manual_event: mistral
        # first (current O-MBE topology), direct-read next.
        voltage_v: Optional[float] = None
        current_a: Optional[float] = None
        psu_source = "none"
        m = self.monitor._latest_mistral
        if m is not None and m.connected:
            voltage_v = m.v_actual
            current_a = m.i_actual
            psu_source = "mistral"
        else:
            p = self.monitor._latest_psu
            if p is not None and p.connected:
                voltage_v = p.voltage_measured
                current_a = p.current_measured
                psu_source = "direct"

        pyro_temp: Optional[float] = None
        pyro = self.monitor._latest_pyro
        if pyro is not None and pyro.connected:
            pyro_temp = pyro.temperature

        cal = self.monitor.live_equalizer_tab.get_calibration()
        idx = self.growth_log.record_live_label(
            elapsed_s=self.monitor.get_elapsed_seconds(),
            weights=weights,
            frame=frame,
            pyro_temp=pyro_temp,
            voltage_V=voltage_v,
            current_A=current_a,
            psu_source=psu_source,
            capture_metadata=capture_metadata,
            calibration=cal,
        )
        if idx > 0:
            self.statusBar().showMessage(
                f"Live label #{idx} saved", 3000,
            )
        else:
            self.statusBar().showMessage(
                "Live label save failed — no session data.", 3000,
            )

    # --- Export ------------------------------------------------------------

    @pyqtSlot()
    def _on_export(self):
        """Export the current session as a growth log."""
        metadata = self.monitor.get_session_metadata()
        path = self.growth_log.export_growth_log(metadata)
        if path:
            self.statusBar().showMessage(f"Growth log exported: {path}", 5000)
        else:
            self.statusBar().showMessage("Export failed \u2014 no session data", 5000)

    # --- Movie export (Jul 10 2026 workstream #5) --------------------------

    @pyqtSlot()
    def _on_movie_export(self):
        """Prompt for a save path and encode a heartbeat time-lapse mp4.

        Blocks a second click while one export is running (single-worker
        policy) \u2014 the button gets disabled up front and re-enabled by
        the worker's finished_ok / failed slot. Session need not be
        stopped: a mid-session export encodes everything captured so
        far, which is legit ("show me the movie of the growth up to
        this point").
        """
        # If a previous worker is still running, ignore the click.
        # Should be visually impossible (button is disabled) but guard
        # in case the user somehow triggered via keyboard.
        if self._movie_worker is not None and self._movie_worker.isRunning():
            self.statusBar().showMessage(
                "Movie export already running; please wait.", 3000,
            )
            return

        session_dir = self.growth_log.session_dir
        if session_dir is None:
            self.statusBar().showMessage(
                "No session data \u2014 arm + start a session first.", 4000,
            )
            return

        exporter = MovieExporter(session_dir)
        n = exporter.frame_count()
        if n == 0:
            self.statusBar().showMessage(
                "No continuous-capture frames to export yet.", 4000,
            )
            return

        default_path = session_dir / DEFAULT_MOVIE_NAME
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export Movie",
            str(default_path),
            "MP4 Video (*.mp4)",
        )
        if not path_str:
            return  # Grower cancelled the dialog

        # Force .mp4 extension since the codec is mp4v \u2014 silently
        # correcting is better than surfacing a codec / extension
        # mismatch after a 60 s encode.
        output_path = Path(path_str)
        if output_path.suffix.lower() != ".mp4":
            output_path = output_path.with_suffix(".mp4")

        self._movie_worker = MovieExportWorker(exporter, output_path)
        self._movie_worker.progress.connect(self._on_movie_export_progress)
        self._movie_worker.finished_ok.connect(self._on_movie_export_ok)
        self._movie_worker.failed.connect(self._on_movie_export_failed)
        # Disable the button during encode; re-enabled in the slots.
        self.monitor.export_movie_btn.setEnabled(False)
        self.statusBar().showMessage(
            f"Encoding movie \u2014 {n} frames total\u2026", 0,
        )
        self._movie_worker.start()

    @pyqtSlot(int, int)
    def _on_movie_export_progress(self, current: int, total: int):
        # Rate-limit the status-bar update \u2014 every 20 frames is
        # smooth-looking without spamming the event loop mid-encode.
        # (progress signal fires from the worker thread; Qt queues it
        # onto the main thread automatically, but very high signal
        # rates still cost.)
        if current == total or current % 20 == 0:
            self.statusBar().showMessage(
                f"Encoding movie \u2014 {current} / {total} frames", 0,
            )

    @pyqtSlot(str)
    def _on_movie_export_ok(self, path: str):
        self.statusBar().showMessage(f"Movie exported: {path}", 8000)
        self.monitor.export_movie_btn.setEnabled(True)
        self._movie_worker = None

    @pyqtSlot(str)
    def _on_movie_export_failed(self, message: str):
        self.statusBar().showMessage(f"Movie export failed: {message}", 8000)
        self.monitor.export_movie_btn.setEnabled(True)
        self._movie_worker = None

    # --- Periodic sensor logging -------------------------------------------

    def _log_sensors(self):
        if not self.growth_log.active:
            return
        pyro = self.monitor._latest_pyro
        pyro_ok = pyro is not None and pyro.connected
        pyro_temp = pyro.temperature if pyro_ok else None
        pyro_temp_std = pyro.temperature_std if pyro_ok else None
        pyro_temp_n = pyro.temperature_n if pyro_ok else None
        m = self.monitor._latest_mistral
        e = self.monitor._latest_evap
        mistral_ok = m is not None and m.connected
        evap_ok = e is not None and e.connected
        ads_cells = m.ads_cells if mistral_ok else None
        # Pressure priority: EvapControl elog (O-MBE) → ADS ion_gauge_1_P (Ch-MBE ADS mode)
        _evap_p = e.chamber_pressure_mbar if evap_ok else None
        _ads_p  = ads_cells.get("ion_gauge_1_P") if ads_cells else None
        _pressure = _evap_p if _evap_p is not None else _ads_p
        self.growth_log.log_sensors(
            pyro_temp,
            self.monitor.get_elapsed_seconds(),
            v_set=m.v_set if mistral_ok else None,
            v_actual=m.v_actual if mistral_ok else None,
            i_set=m.i_set if mistral_ok else None,
            i_actual=m.i_actual if mistral_ok else None,
            chamber_pressure_mbar=_pressure,
            pyro_temp_std=pyro_temp_std,
            pyro_temp_n=pyro_temp_n,
            # Elog-direct fields (None outside elog mode — see
            # drivers.evap_control.ElogReader).
            substrate_temp_pv_C=(
                e.substrate_temp_pv_C if evap_ok else None
            ),
            substrate_temp_setpoint_C=(
                e.substrate_temp_setpoint_C if evap_ok else None
            ),
            cell_HTEC2_pv_C=e.cell_HTEC2_pv_C if evap_ok else None,
            cell_Y_pv_C=e.cell_Y_pv_C if evap_ok else None,
            cell_Sr_pv_C=e.cell_Sr_pv_C if evap_ok else None,
            cell_Eu_pv_C=e.cell_Eu_pv_C if evap_ok else None,
            cell_Er_pv_C=e.cell_Er_pv_C if evap_ok else None,
            plasma_dc_bias_V=e.plasma_dc_bias_V if evap_ok else None,
            plasma_forward_W=e.plasma_forward_W if evap_ok else None,
            plasma_reflected_W=e.plasma_reflected_W if evap_ok else None,
            ads_cells=ads_cells,
        )

        from datetime import datetime
        self.monitor.add_sensor_log_row(
            datetime.now().strftime("%H:%M:%S"),
            pyro_temp,
            voltage=m.v_actual if mistral_ok else None,
            current=m.i_actual if mistral_ok else None,
            pressure=_pressure,
        )

    # --- Worker state fan-out to monitor -----------------------------------

    @pyqtSlot(ClassifierState)
    def _on_classifier_state(self, state: ClassifierState):
        """Forward classifier worker state to the monitor's slider slot.

        Also caches the state so ``_on_auto_capture_event`` can snapshot
        it into each event's buffer directory. The cache holds the most
        recent emission by reference — no deep copy — because the worker
        replaces state dict fields (raw_scores etc.) with new objects on
        each cycle rather than mutating them.
        """
        self._latest_classifier = state
        self.monitor.update_classifier_state(state)

    @pyqtSlot(CameraState)
    def _on_camera_state(self, state: CameraState):
        self.monitor.update_camera_state(state)
        self.rheed_intensity_window.on_camera_state(state)

        if (
            state.mode == "screengrab"
            and not state.connected
            and state.error
        ):
            # WGC is deliberately fail-closed. Stop all image-producing
            # automation; sensor logging may continue so the session record
            # still shows when the camera source was lost.
            self._heartbeat_timer.stop()
            self.auto_capture_engine.enabled = False
            self.auto_capture_engine.reset()
            self.monitor.set_rheed_reconnect_required(True)
            self.monitor.live_equalizer_tab.set_save_enabled(False)
            self.monitor.set_classifier_capture_unavailable(
                "RHEED capture unavailable; classification stopped",
            )
            self.monitor.set_auto_capture_status(
                "Auto-capture: stopped (RHEED WGC unavailable)"
            )
            self.statusBar().showMessage(
                f"RHEED WGC stopped: {state.error} Reconnect to resume.",
                10000,
            )
        elif (
            state.mode == "screengrab"
            and state.connected
            and state.frame is not None
        ):
            # The operator restored the detached window and explicitly
            # reconnected. Hide the retry control only after a fresh frame.
            self.monitor.set_rheed_reconnect_required(False)
            if self.growth_log.active and not self._heartbeat_timer.isActive():
                # Resume image automation only after a fresh frame. Do not
                # compare it with a pre-loss reference or undo an explicit
                # grower pause.
                self._heartbeat_timer.start()
                self.monitor.live_equalizer_tab.set_save_enabled(True)
                self.auto_capture_engine.reset()
                if self.monitor.is_auto_capture_paused():
                    self.auto_capture_engine.enabled = False
                    self.monitor.set_auto_capture_status(
                        "Auto-capture: PAUSED (RHEED WGC restored)"
                    )
                else:
                    self.auto_capture_engine.enabled = True
                    self.monitor.set_auto_capture_status(
                        "Auto-capture: armed (warmup after WGC reconnect)"
                    )

        # Feed the auto-capture engine. Engine internally guards on `enabled`,
        # so this is a no-op outside an active session.
        if state.frame is not None and state.connected:
            self.auto_capture_engine.evaluate(
                state.frame,
                self.monitor.get_current_capture_metadata(),
            )
            if self.auto_capture_engine.enabled:
                self.monitor.set_auto_capture_status(
                    f"Auto-capture: armed | "
                    f"score: {self.auto_capture_engine.latest_score:.2f} | "
                    f"events: {self._auto_capture_event_count}"
                )

    def _on_heartbeat(self):
        """Heartbeat timer tick — save the latest RHEED frame as an anchor.

        Skips silently if no frame is available yet (camera worker hasn't
        emitted, or session just started). Bad frames (rejected by the
        quality gate inside save_heartbeat_frame) are also skipped — the
        gate prints to stderr.
        """
        if not self.growth_log.active:
            return
        frame = self.monitor.get_current_frame()
        if frame is None:
            return
        capture_metadata = self.monitor.get_current_capture_metadata()
        path = self.growth_log.save_heartbeat_frame(frame)
        if not path:
            return  # Quality gate rejected, or save failed
        pyro_temp = (
            self.monitor._latest_pyro.temperature
            if self.monitor._latest_pyro and self.monitor._latest_pyro.connected
            else None
        )
        self.growth_log.log_heartbeat(
            elapsed_s=self.monitor.get_elapsed_seconds(),
            pyro_temp=pyro_temp,
            frame_path=path,
            capture_metadata=capture_metadata,
        )
        # Bump the Monitor-tab footer counter only after a successful
        # save — a quality-gated skip must not inflate the display,
        # otherwise the count drifts from the frames on disk that the
        # scrubber will actually read.
        self.monitor.increment_continuous_capture_count()
        self.statusBar().showMessage(
            f"Heartbeat anchor saved (#{self.growth_log._heartbeat_counter})", 3000,
        )

    @pyqtSlot(np.ndarray, float)
    def _on_auto_capture_event(self, frame: np.ndarray, score: float):
        """Engine flagged a frame — log event + dump pre-event context buffer.

        The CSV row records timestamp, score, and temp at trigger time
        for cross-referencing against grower notes + sensor_log. The
        context buffer (last ~20 frames including the flagged one) is
        dumped to ``frames/auto_event_NNN/`` so post-hoc analysis can see
        the visual evolution leading up to the trigger.
        """
        self._auto_capture_event_count += 1
        pyro_temp = (
            self.monitor._latest_pyro.temperature
            if self.monitor._latest_pyro and self.monitor._latest_pyro.connected
            else None
        )
        context_captures = self.auto_capture_engine.get_recent_captures()
        buffer_count, buffer_dir = self.growth_log.save_auto_capture_buffer(
            event_idx=self._auto_capture_event_count,
            frames=[frame for frame, _metadata in context_captures],
            capture_metadata=[
                metadata for _frame, metadata in context_captures
            ],
        )
        # Empty-buffer events have nothing to review — quality gate rejected
        # all 20 frames. Mark as auto_skipped at log time so the CSV row
        # gets a terminal state instead of sitting at pending forever.
        initial_state = (
            EVENT_STATE_AUTO_SKIPPED if buffer_count == 0
            else EVENT_STATE_PENDING
        )
        self.growth_log.log_auto_capture_event(
            event_idx=self._auto_capture_event_count,
            score=score,
            elapsed_s=self.monitor.get_elapsed_seconds(),
            pyro_temp=pyro_temp,
            buffer_count=buffer_count,
            buffer_dir=buffer_dir,
            event_state=initial_state,
            capture_metadata=self.monitor.get_current_capture_metadata(),
        )
        # Snapshot the latest classifier state into the event directory.
        # Gives Justin's team a per-event training pair — visual context
        # buffer + the model's opinion at trigger time — for every flagged
        # event, essentially free. Only writes when there's a buffer dir
        # (buffer_count > 0) AND a classifier state to snapshot.
        if buffer_count > 0 and buffer_dir and self._latest_classifier is not None:
            self._save_classifier_snapshot(
                buffer_dir=buffer_dir,
                event_idx=self._auto_capture_event_count,
                event_score=score,
            )

        # Surface the banner so the grower can discard if it looks spurious.
        # Default action (timeout) is to keep — the buffer is already on disk.
        if buffer_count > 0:
            self.monitor.show_auto_capture_event(
                event_idx=self._auto_capture_event_count,
                score=score,
                buffer_dir=buffer_dir,
            )

    def _save_classifier_snapshot(
        self, buffer_dir: str, event_idx: int, event_score: float,
    ) -> None:
        """Write ``classifier_state.json`` next to the event's frame buffer.

        Note: the snapshot reflects the *most recent* classifier emission,
        which may be up to ``ClassifierWorker.POLL_INTERVAL_S`` (~0.5 s)
        older than the frame that triggered the auto-capture. That
        latency is acceptable for training-data purposes; if it ever
        becomes limiting, the fix is to classify the flagged frame
        synchronously here instead of using the cached state.
        """
        import json
        from datetime import datetime as _dt

        cs = self._latest_classifier
        if cs is None:  # defensive — caller already checked, keep for lint
            return

        smoothed = cs.smoothed_percent or {}
        argmax_class = (
            max(smoothed.items(), key=lambda kv: kv[1])[0]
            if smoothed and cs.has_confident_data
            else None
        )
        snapshot = {
            "timestamp_iso": _dt.now().isoformat(),
            "event_idx": event_idx,
            "event_score": float(event_score),
            "elapsed_s": self.monitor.get_elapsed_seconds(),
            "predicted_class": argmax_class,
            "smoothed_percent": {k: int(v) for k, v in smoothed.items()},
            "normalized_percent": {
                k: int(v) for k, v in (cs.normalized_percent or {}).items()
            },
            "raw_scores": {k: float(v) for k, v in (cs.raw_scores or {}).items()},
            "raw_sum": float(cs.raw_sum),
            "quality": float(cs.quality),
            "is_bad": bool(cs.is_bad),
            "bad_confidence": float(cs.bad_confidence),
            "is_ood": bool(cs.is_ood),
            "has_confident_data": bool(cs.has_confident_data),
            "inference_ms": float(cs.inference_ms),
            "model_version": cs.model_version,
            "last_classified_frame_number": int(cs.last_frame_number),
        }
        out_path = Path(buffer_dir) / "classifier_state.json"
        try:
            out_path.write_text(json.dumps(snapshot, indent=2))
        except Exception as e:
            log.warning(
                "Failed to write classifier snapshot to %s: %s", out_path, e,
            )

    @pyqtSlot(int, str, str)
    def _on_auto_capture_decision(
        self, event_idx: int, buffer_dir: str, state: str,
    ):
        """Route the grower's decision (or default-keep timeout) for an
        auto-captured event into the CSV.

        Updates the event's row in auto_capture_events.csv to reflect the
        decision state — one of ``kept_explicit``, ``kept_default``, or
        ``discarded``. **Non-destructive by design**: the buffer directory
        is preserved on disk regardless of state, so a "discarded" event
        can be recovered from the Events tab if the grower changes their
        mind. Rationale: feedback_aiqm_grower_friction.md (decisions
        worth making should be recorded; a 10-second decision shouldn't
        be irreversible).
        """
        self.growth_log.update_auto_capture_state(event_idx, state)
        if state == EVENT_STATE_DISCARDED:
            self.statusBar().showMessage(
                f"Event #{event_idx} marked discarded (buffer preserved at {buffer_dir})",
                3000,
            )

    @pyqtSlot(bool)
    def _on_auto_capture_pause_toggled(self, paused: bool):
        """Soft-halt auto-capture without touching session-wide logging.

        Pause: disable the engine — sensor log, heartbeat, frame display,
        manual commits all keep working.
        Resume: reset the engine first so the (now-stale) context buffer
        doesn't fire a spurious trigger from the diff between the pre-pause
        reference and the post-pause first frame.
        """
        if paused:
            self.auto_capture_engine.enabled = False
            self.monitor.set_auto_capture_status(
                f"Auto-capture: PAUSED "
                f"({self._auto_capture_event_count} events this session)"
            )
            self.statusBar().showMessage("Auto-capture paused", 3000)
        else:
            self.auto_capture_engine.reset()
            self.auto_capture_engine.enabled = True
            self.monitor.set_auto_capture_status(
                "Auto-capture: armed (warmup)"
            )
            self.statusBar().showMessage("Auto-capture resumed", 3000)

    @pyqtSlot(PyrometerState)
    def _on_pyrometer_state(self, state: PyrometerState):
        self.monitor.update_pyrometer_state(state)
        self.pyrometer_window.on_pyrometer_state(state)

    @pyqtSlot(MistralState)
    def _on_mistral_state(self, state: MistralState):
        self.monitor.update_mistral_state(state)

        if not self.growth_log.active or not state.connected:
            return

        # Detect operator setpoint changes (Set Voltage / Set Current button
        # presses on MISTRAL). OCR jitter can produce sub-tolerance noise on
        # an unchanged setpoint, so only fire when the change exceeds the
        # configured tolerance. Ignore None readings (transient OCR failures).
        elapsed = self.monitor.get_elapsed_seconds()
        pyro_temp = (
            self.monitor._latest_pyro.temperature
            if self.monitor._latest_pyro and self.monitor._latest_pyro.connected
            else None
        )

        if state.v_set is not None:
            if (
                self._last_v_set is not None
                and abs(state.v_set - self._last_v_set) > SET_V_CHANGE_TOLERANCE
            ):
                self.growth_log.log_set_change_event(
                    elapsed_s=elapsed,
                    channel="voltage",
                    old_value=self._last_v_set,
                    new_value=state.v_set,
                    pyro_temp=pyro_temp,
                )
                self.statusBar().showMessage(
                    f"Set Voltage changed: {self._last_v_set:.2f} → {state.v_set:.2f} V",
                    3000,
                )
            self._last_v_set = state.v_set

        if state.i_set is not None:
            if (
                self._last_i_set is not None
                and abs(state.i_set - self._last_i_set) > SET_I_CHANGE_TOLERANCE
            ):
                self.growth_log.log_set_change_event(
                    elapsed_s=elapsed,
                    channel="current",
                    old_value=self._last_i_set,
                    new_value=state.i_set,
                    pyro_temp=pyro_temp,
                )
                self.statusBar().showMessage(
                    f"Set Current changed: {self._last_i_set:.3f} → {state.i_set:.3f} A",
                    3000,
                )
            self._last_i_set = state.i_set

    @pyqtSlot(EvapControlState)
    def _on_evap_state(self, state: EvapControlState):
        self.monitor.update_evap_state(state)

    # --- Helpers -----------------------------------------------------------

    @staticmethod
    def _stop_workers(*workers):
        """Stop multiple workers in parallel, then wait for all.

        Calls stop() on every worker first (non-blocking boolean flags),
        then waits for all threads to exit. Total wait time is bounded by
        the slowest worker's remaining poll interval (~1 s), not the sum
        of all workers' intervals (~7.5 s sequential).
        """
        # Phase 1: signal all to stop (non-blocking).
        for w in workers:
            if w is not None:
                w.stop()
        # Phase 2: wait for all to finish (they exit concurrently).
        for w in workers:
            if w is not None and w.isRunning():
                w.wait(2000)

    # --- Shutdown ----------------------------------------------------------

    def closeEvent(self, event):
        self._sensor_log_timer.stop()
        self._heartbeat_timer.stop()
        if self.growth_log.active:
            metadata = self.monitor.get_session_metadata()
            self.growth_log.save_session_metadata(metadata)
            self.growth_log.end_session()
            # Same auto-plot as _on_stop — also covers the "user closed
            # window mid-session" path, not just clean STOP.
            try:
                self.growth_log.generate_temperature_plot(metadata)
            except Exception as e:
                log.warning("Auto T vs t plot failed during close: %s", e)
        self._stop_workers(
            self.camera_worker,
            self.pyrometer_worker,
            self.mistral_worker,
            self.evap_worker,
            self.classifier_worker,
        )
        event.accept()
