"""
MBE Growth Monitor — application orchestrator.

Manages worker lifecycle, signal fan-out from workers to the monitor widget,
and ARM / START / STOP / DISARM session state transitions.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from copy import copy
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtWidgets import QApplication, QFileDialog, QMainWindow
from PyQt6.QtCore import QEvent, pyqtSlot, Qt, QTimer

log = logging.getLogger(__name__)

from gui.auto_capture import AutoCaptureEngine, PixelDiffChangeDetector
from gui.equalizer_alignment import (
    BasisBundle,
    CalibrationRecord,
    RheedFrameSnapshot,
    calibration_is_stale,
)
from gui.growth_logger import (
    EVENT_STATE_DISCARDED,
    RHEED_VIEW_EVENT_CURRENT_ADJUSTED,
    RHEED_VIEW_EVENT_ENERGY_ADJUSTED,
    RHEED_VIEW_EVENT_HISTORY_RESET,
    RHEED_VIEW_EVENT_HISTORY_READY,
    RHEED_VIEW_EVENT_QC_PASS,
    RHEED_VIEW_EVENT_QC_REJECT,
    RHEED_VIEW_EVENT_SAMPLE_DIRECTION_ADJUSTED,
    RHEED_VIEW_EVENT_SESSION_START,
)
from gui.state import (
    CameraState, ClassifierState, EvapControlState, MistralState,
    PyrometerState, RheedQcState, WeakPrimaryShadowState,
)
from gui.workers import (
    ClassifierWorker, EvapControlWorker, MistralWorker,
    PyrometerWorker, RheedCameraWorker, WeakPrimaryShadowWorker,
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
from gui.temporal_observability import (
    process_metrics,
    snapshot_state,
    synchronization_summary,
    utc_now_iso,
)


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


def _sample_timing_snapshot(
    state,
    logged_monotonic_ns: Optional[int] = None,
    source: str = "instrument",
):
    """Copy one state's provenance and calculate its current monotonic age."""
    snap = snapshot_state(source, state, logged_monotonic_ns)
    result = snap.to_dict()
    # Compatibility aliases retained for existing timing tests/callers.
    result.update({
        "source": snap.source_at_utc,
        "received": snap.received_at_utc,
        "read_duration_ms": snap.read_duration_ms,
    })
    return result


def _stamp_gui_received(state):
    """Copy and stamp a queued State, including lightweight test doubles."""
    received_ns = time.perf_counter_ns()
    try:
        return replace(state, gui_received_monotonic_ns=received_ns)
    except TypeError:
        snapshot = copy(state)
        setattr(snapshot, "gui_received_monotonic_ns", received_ns)
        return snapshot


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

# Operator QC/alignment labels must bind to a live camera observation, not a
# stale image left in the widget cache.
RHEED_EVENT_MAX_FRAME_AGE_MS = 3000.0

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
    # Bulbasaur (O-MBE)
    r"C:\Users\Lab10\AI_for_quantum",
    # Ch-MBE (Omicron chalcogenide MBE) — added 2026-07-21
    r"C:\Users\Omicron\AI_for_quantum",
    # AJ's Mac dev clone
    "/Users/aj/ai-for-quantum",
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


_BUNDLED_WEAK_PRIMARY_AI_ROOT = (
    WORKSPACE_DIR / "models" / "weak_primary_lambda_0_1" / "RHEEDClassify"
)


def _resolve_weak_primary_ai_repo_root() -> str:
    """Resolve the self-contained weak-primary runtime package.

    An explicit ``AI_REPO_ROOT`` remains the operator override. Otherwise the
    model package shipped with this branch takes precedence over unrelated
    workstation checkouts, keeping code and weights on one audited commit.
    """
    env = os.environ.get("AI_REPO_ROOT")
    if env:
        return env
    if _BUNDLED_WEAK_PRIMARY_AI_ROOT.is_dir():
        return str(_BUNDLED_WEAK_PRIMARY_AI_ROOT)
    return _resolve_ai_repo_root()


class GrowthApp(QMainWindow):
    """Main window for the OMBE Growth Monitor application."""

    def __init__(self):
        super().__init__()
        from drivers.config import get_active_config
        self._chamber_config = get_active_config()
        self.setWindowTitle(f"{self._chamber_config.name} Growth Monitor")
        self.setMinimumSize(1000, 700)

        self.camera_worker: Optional[RheedCameraWorker] = None
        self._reported_camera_exposure_us: Optional[float] = None
        self.pyrometer_worker: Optional[PyrometerWorker] = None
        self.mistral_worker: Optional[MistralWorker] = None
        self.evap_worker: Optional[EvapControlWorker] = None
        self.classifier_worker: Optional[ClassifierWorker] = None
        self.weak_primary_shadow_worker: Optional[
            WeakPrimaryShadowWorker
        ] = None
        self._latest_weak_primary_shadow: Optional[
            WeakPrimaryShadowState
        ] = None
        # Latest classifier state, cached from _on_classifier_state so the
        # auto-capture event handler can snapshot it into each event
        # directory (saves the model's opinion at trigger time — feeds the
        # CS-team training-data pipeline).
        self._latest_classifier: Optional[ClassifierState] = None
        # Single source of truth for operator-known RHEED acquisition QC.
        # This exists even when the classifier is disabled.
        self._rheed_qc_state = RheedQcState(
            history_required=ClassifierWorker.HISTORY_FRAMES_REQUIRED,
        )
        # Accepted Equalizer calibration is app-owned.  The tab may edit a
        # candidate, but logging and lifecycle transitions always consult this
        # single record so a stale UI object cannot authorize a Save.
        self._equalizer_calibration: Optional[CalibrationRecord] = None
        self._retrospective_equalizer_calibrations: dict[
            str, CalibrationRecord
        ] = {}
        self._camera_capture_interrupted = False
        self._last_heartbeat_capture_token: Optional[tuple] = None
        # Set before any shutdown work begins.  A close can be refused while a
        # native worker is still exiting, so queued worker signals must remain
        # fail-closed even though the main window is still alive.
        self._shutdown_pending = False
        self.growth_log = GrowthLogger()

        # Periodic sensor logging timer (1 second interval while running)
        self._sensor_log_timer = QTimer(self)
        self._sensor_log_timer.setInterval(1000)
        self._sensor_log_timer.timeout.connect(self._log_sensors)
        self._last_sensor_log_tick_ns: Optional[int] = None

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
        self.monitor.rheed_view_event_requested.connect(
            self._on_rheed_view_event,
        )
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
        self.monitor.live_equalizer_tab.calibration_accept_requested.connect(
            self._on_equalizer_calibration_accept,
        )
        self.monitor.live_equalizer_tab.calibration_invalidation_requested.connect(
            self._on_equalizer_calibration_invalidation,
        )
        self.monitor.events_tab.retrospective_calibration_accept_requested.connect(
            self._on_retrospective_calibration_accept,
        )
        self.monitor.events_tab.retrospective_calibration_invalidation_requested.connect(
            self._on_retrospective_calibration_invalidation,
        )
        self.monitor.events_tab.retrospective_calibration_resolve_requested.connect(
            self._on_retrospective_calibration_resolve,
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

        # Local alignment commissioning does not need an armed instrument
        # session.  Feed one immutable repository-backed dummy frame while
        # idle so Calibrate is immediately available for a desktop demo.
        self._local_demo_sequence = 0
        self.monitor.config_camera_mode.currentTextChanged.connect(
            self._load_local_alignment_demo,
        )
        self._load_local_alignment_demo(
            self.monitor.config_camera_mode.currentText(),
        )
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    @staticmethod
    def _trace_temporal(owner, event: str, source: str, **kwargs) -> bool:
        """Write a trace when the logger supports temporal observability."""
        logger = getattr(owner, "growth_log", None)
        writer = getattr(logger, "log_temporal_event", None)
        if not callable(writer):
            return False
        return bool(writer(event, source, **kwargs))

    def eventFilter(self, watched, event):
        """Measure selected GUI event-loop turns without consuming events."""
        event_type = event.type()
        interactive = {
            QEvent.Type.MouseButtonPress,
            QEvent.Type.KeyPress,
            QEvent.Type.Resize,
            QEvent.Type.WindowStateChange,
        }
        window_event = event_type in {
            QEvent.Type.Resize, QEvent.Type.WindowStateChange,
        }
        if (
            getattr(getattr(self, "growth_log", None), "active", False)
            and event_type in interactive
            and (not window_event or watched is self)
        ):
            received_ns = time.perf_counter_ns()
            try:
                target = watched.objectName() or watched.__class__.__name__
            except (AttributeError, RuntimeError):
                target = type(watched).__name__

            def _record_next_turn(
                event_name=event_type.name,
                target_name=target,
                started_ns=received_ns,
            ):
                completed_ns = time.perf_counter_ns()
                GrowthApp._trace_temporal(self,
                    "event_loop_turn", "gui",
                    details={
                        "qt_event": event_name,
                        "target": target_name,
                        "event_received_monotonic_ns": started_ns,
                        "next_turn_monotonic_ns": completed_ns,
                        "next_turn_latency_ms": (
                            completed_ns - started_ns
                        ) / 1_000_000.0,
                    },
                )

            QTimer.singleShot(0, _record_next_turn)
        return super().eventFilter(watched, event)

    # --- ARM / DISARM ------------------------------------------------------

    @pyqtSlot(str)
    def _load_local_alignment_demo(self, mode: str) -> None:
        """Load an idle dummy frame without starting hardware workers."""
        if self.camera_worker is not None and self.camera_worker.isRunning():
            return
        if not (mode == "dummy" or mode.startswith("dummy_")):
            self.monitor.update_camera_state(CameraState(
                mode=mode,
                capture_backend=mode,
                connected=False,
            ))
            return

        from datetime import datetime, timezone
        from drivers.rheed_camera import DummyCamera

        camera = DummyCamera(preset=mode)
        try:
            camera.connect()
            frame = camera.read_frame()
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            self._on_camera_state(CameraState(
                mode=mode,
                capture_backend=mode,
                connected=False,
                error=f"Local alignment demo unavailable: {exc}",
            ))
            return
        finally:
            camera.disconnect()

        self._local_demo_sequence += 1
        captured_monotonic_ns = time.perf_counter_ns()
        captured_at_utc = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        self._on_camera_state(CameraState(
            frame=frame,
            frame_number=self._local_demo_sequence,
            width=int(frame.shape[1]),
            height=int(frame.shape[0]),
            intensity=float(np.mean(frame)),
            connected=True,
            valid=True,
            mode=mode,
            capture_backend=mode,
            captured_at_utc=captured_at_utc,
            received_at_utc=captured_at_utc,
            received_monotonic_ns=captured_monotonic_ns,
            sample_sequence=self._local_demo_sequence,
            capture_sequence=self._local_demo_sequence,
            frame_age_ms=0.0,
            source_hwnd=0,
            captured_monotonic_ns=captured_monotonic_ns,
            capture_geometry_id=f"{mode}:full-frame",
        ))

    @pyqtSlot()
    def _on_arm(self):
        """Connect camera, pyrometer, MISTRAL, and Evap Control workers."""
        camera_mode = self.monitor.config_camera_mode.currentText()
        pyrometer_mode = self.monitor.config_pyrometer_mode.currentText()
        mistral_mode = self.monitor.config_mistral_mode.currentText()
        evap_mode = self.monitor.config_evap_mode.currentText()

        new_camera_worker = False
        if not self.camera_worker or not self.camera_worker.isRunning():
            exposure_us = (
                self.monitor.selected_exposure_us()
                if camera_mode in ("vimba", "direct") else None
            )
            self.monitor.clear_camera_provenance()
            self._reported_camera_exposure_us = None
            self.camera_worker = RheedCameraWorker(
                mode=camera_mode,
                poll_interval=1.0,
                camera_index=self._chamber_config.camera_index,
                trigger_hz=self._chamber_config.camera_fps,
                exposure_us=exposure_us,
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
                survivors = self._stop_worker(self.classifier_worker)
                if not survivors:
                    self.classifier_worker = None
                else:
                    log.error(
                        "Classifier disable deferred because its worker is still running"
                    )
                    self.statusBar().showMessage(
                        "Classifier disable pending: its worker is still stopping.",
                        7000,
                    )
            # Explicit "disabled" signal to the monitor so the sliders
            # show a clear message instead of just staying at "idle".
            if not (
                self.classifier_worker
                and self.classifier_worker.isRunning()
            ):
                self.monitor.set_classifier_disabled()
        elif new_camera_worker and self.classifier_worker.isRunning():
            # A fail-closed WGC worker can be re-armed while the classifier
            # thread is still alive. Wire the replacement camera worker to
            # that existing classifier instance.
            self.camera_worker.state_updated.connect(
                self.classifier_worker.on_rheed_state,
                Qt.ConnectionType.DirectConnection,
            )

        shadow_enabled = (
            self.monitor.config_weak_primary_shadow_enabled.isChecked()
        )
        if shadow_enabled and (
            not self.weak_primary_shadow_worker
            or not self.weak_primary_shadow_worker.isRunning()
        ):
            self.weak_primary_shadow_worker = WeakPrimaryShadowWorker(
                ai_repo_root=_resolve_weak_primary_ai_repo_root(),
                artifact_root=(
                    os.environ.get("AIQM_WEAK_PRIMARY_SHADOW_ROOT") or None
                ),
                device=(os.environ.get("AIQM_WEAK_PRIMARY_DEVICE") or None),
            )
            self.camera_worker.state_updated.connect(
                self.weak_primary_shadow_worker.on_rheed_state,
                Qt.ConnectionType.DirectConnection,
            )
            self.weak_primary_shadow_worker.state_updated.connect(
                self._on_weak_primary_shadow_state,
            )
            self.weak_primary_shadow_worker.start()
        elif not shadow_enabled:
            if (
                self.weak_primary_shadow_worker
                and self.weak_primary_shadow_worker.isRunning()
            ):
                survivors = self._stop_worker(
                    self.weak_primary_shadow_worker
                )
                if not survivors:
                    self.weak_primary_shadow_worker = None
            self._latest_weak_primary_shadow = None
            self.monitor.set_weak_primary_shadow_disabled()
        elif (
            new_camera_worker
            and self.weak_primary_shadow_worker.isRunning()
        ):
            self.camera_worker.state_updated.connect(
                self.weak_primary_shadow_worker.on_rheed_state,
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
                rts=self._chamber_config.pyrometer_rts,
                modbus_backend=self._chamber_config.pyrometer_modbus_backend,
            )
            self.pyrometer_worker.state_updated.connect(self._on_pyrometer_state)
            self.pyrometer_worker.start()

        if not self.mistral_worker or not self.mistral_worker.isRunning():
            self.mistral_worker = MistralWorker(
                mode=mistral_mode,
                poll_interval=1.0,
                chamber_config=self._chamber_config,
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
        """Disconnect all hardware workers without serial wait stacking."""
        worker_slots = [
            ("camera_worker", self.camera_worker),
            ("pyrometer_worker", self.pyrometer_worker),
            ("mistral_worker", self.mistral_worker),
            ("evap_worker", self.evap_worker),
            ("classifier_worker", self.classifier_worker),
        ]
        shadow_worker = getattr(self, "weak_primary_shadow_worker", None)
        if shadow_worker is not None:
            worker_slots.append(("weak_primary_shadow_worker", shadow_worker))
        survivors = self._stop_workers(
            *(worker for _name, worker in worker_slots),
        )

        # A live QThread must retain its Python/Qt owner.  Clear only workers
        # that actually stopped; a subsequent DISARM click can retry any
        # survivor once its polling or native I/O call returns.
        for attribute, worker in worker_slots:
            survived = worker is not None and any(
                worker is survivor for survivor in survivors
            )
            if not survived:
                setattr(self, attribute, None)

        self.monitor.reset_displays()
        if survivors:
            names = ", ".join(type(worker).__name__ for worker in survivors)
            log.error(
                "Disarm incomplete because %d worker(s) remain running: %s",
                len(survivors),
                names,
            )
            # Stay armed so the UI neither claims a clean idle state nor
            # permits config changes while old workers still own the current
            # hardware configuration.
            self.monitor.set_state("armed")
            self.monitor.start_btn.setEnabled(False)
            self.statusBar().showMessage(
                "Disarm incomplete: background workers are still stopping. "
                "Wait and press DISARM again.",
                7000,
            )
            return
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
            survivors = self._stop_worker(self.camera_worker)
            if survivors:
                self.monitor.set_rheed_reconnect_required(
                    True, in_progress=False,
                )
                self.statusBar().showMessage(
                    "RHEED reconnect blocked: the previous capture worker is "
                    "still stopping. Try again shortly.",
                    7000,
                )
                return
        self.camera_worker = RheedCameraWorker(
            mode="screengrab", poll_interval=1.0,
        )
        self.camera_worker.state_updated.connect(self._on_camera_state)
        if self.classifier_worker and self.classifier_worker.isRunning():
            self.camera_worker.state_updated.connect(
                self.classifier_worker.on_rheed_state,
                Qt.ConnectionType.DirectConnection,
            )
        if (
            self.weak_primary_shadow_worker
            and self.weak_primary_shadow_worker.isRunning()
        ):
            self.camera_worker.state_updated.connect(
                self.weak_primary_shadow_worker.on_rheed_state,
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
        if self._equalizer_calibration is not None:
            self._invalidate_equalizer_calibration(
                "new session started before prior calibration was cleared",
            )
        self._retrospective_equalizer_calibrations.clear()

        # Apply config panel settings
        save_path = self.monitor.config_save_path.text().strip()
        prefix = self.monitor.config_prefix.text().strip()
        if save_path:
            configured_root = resolve_workspace_folder(save_path)
            try:
                recovered = self.growth_log.set_base_dir(configured_root)
            except (OSError, RuntimeError, ValueError) as exc:
                recovered = False
                log.error("Could not apply Growth Monitor log root: %s", exc)
            if not recovered:
                self.statusBar().showMessage(
                    "START blocked: an interrupted Live-label transaction "
                    "under the configured log root needs inspection.",
                    9000,
                )
                return
        if prefix:
            self.growth_log._filename_prefix = prefix

        sample_id = self.monitor.sample_id_input.text().strip() or "unnamed"
        self.growth_log.start_session(sample_id)
        session_dir = self.growth_log.session_dir
        self.monitor.live_equalizer_tab.set_session_id(
            session_dir.name if session_dir is not None else "",
        )
        self._camera_capture_interrupted = False
        self._last_heartbeat_capture_token = None

        # A session begins with an ordinary stable view. Operator adjustments
        # are timestamped observations, not gates on acquisition or analysis.
        # Pixel-coordinate history is still reset at the session boundary so
        # STOP -> START cannot leak classifier state between samples.
        initial_qc = RheedQcState(
            session_active=True,
            view_segment_id=0,
            gun_aligned=True,
            history_required=ClassifierWorker.HISTORY_FRAMES_REQUIRED,
        )
        self._apply_rheed_qc_state(
            initial_qc,
            reset_visual_history=True,
        )
        self._latest_classifier = None
        self._latest_weak_primary_shadow = None
        if self.classifier_worker and self.classifier_worker.isRunning():
            self.monitor.reset_classifier_session_state()
        elif self.monitor.config_classifier_enabled.isChecked():
            self.monitor.reset_classifier_session_state(
                "Classifier unavailable — re-arm to retry model loading"
            )
        else:
            self.monitor.set_classifier_disabled()
        self._record_rheed_view_event(
            RHEED_VIEW_EVENT_SESSION_START,
            0.0,
            frame_role="session_start",
        )

        # Clear trend window histories so a new growth starts fresh —
        # without this, a second growth inherits the first growth's trace.
        self.rheed_intensity_window.reset()
        self.pyrometer_window.reset()

        # Point the events tab at this session's logger so backfill
        # (handles GUI-restart-mid-session), the live append-on-signal,
        # and labeling reads/writes all flow through the same source.
        self.monitor.events_tab.attach_session(
            self.growth_log,
            labeler=self.monitor.grower_input.text().strip(),
        )
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
        self._last_sensor_log_tick_ns = None
        self._sensor_log_timer.start()

        # Arm shadow-mode auto-capture immediately. RHEED adjustment markers
        # do not pause or re-arm this independent acquisition path.
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
        self._last_sensor_log_tick_ns = None
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

        metadata = self._session_metadata_with_camera()

        # Save session metadata
        self.growth_log.save_session_metadata(metadata)

        # Auto-export growth log before closing session files
        path = self.growth_log.export_growth_log(metadata)

        # Journal the invalidation while the session files are still open.
        self._invalidate_equalizer_calibration("session ended")
        self.growth_log.end_session()

        stopped_qc = replace(
            self._rheed_qc_state,
            session_active=False,
            gun_aligned=None,
            realignment_active=False,
            history_frame_count=0,
            history_ready=False,
        )
        self._apply_rheed_qc_state(
            stopped_qc,
            reset_visual_history=True,
        )
        self._latest_classifier = None
        self._latest_weak_primary_shadow = None
        self.monitor.reset_classifier_session_state("Classifier session ended")

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

    # --- RHEED acquisition QC ---------------------------------------------

    def _rheed_qc_snapshot(
        self,
        *,
        qc_reject: Optional[bool] = None,
        qc_reason: str = "",
    ) -> dict:
        """Return the structured post-event state written by GrowthLogger."""
        state = self._rheed_qc_state
        return {
            "realignment_id": state.realignment_id,
            "view_segment_id": state.view_segment_id,
            "visual_history_generation": (
                state.visual_history_generation
            ),
            "gun_aligned": state.gun_aligned,
            "history_frame_count": state.history_frame_count,
            "history_required": state.history_required,
            "history_ready": state.history_ready,
            # Empty means no explicit frame-level QC label. It must not be
            # coerced to PASS merely because the gun is aligned.
            "qc_reject": "" if qc_reject is None else qc_reject,
            "qc_reason": qc_reason,
            "prediction_actionable": state.prediction_actionable,
        }

    def _apply_rheed_qc_state(
        self,
        state: RheedQcState,
        *,
        reset_visual_history: bool,
    ) -> None:
        """Publish app-owned QC state and optionally reset visual inference."""
        if reset_visual_history:
            state = replace(
                state,
                visual_history_generation=(
                    self._rheed_qc_state.visual_history_generation + 1
                ),
            )
        if self._equalizer_calibration is not None:
            metadata = self.monitor.get_current_capture_metadata()
            stale, reason = calibration_is_stale(
                self._equalizer_calibration,
                source_hwnd=int(metadata.get("source_hwnd") or 0),
                camera_width=int(metadata.get("camera_width") or 0),
                camera_height=int(metadata.get("camera_height") or 0),
                capture_backend=str(metadata.get("capture_backend") or ""),
                capture_geometry_id=str(
                    metadata.get("capture_geometry_id") or ""
                ),
                view_segment_id=state.view_segment_id,
                visual_history_generation=state.visual_history_generation,
                session_id=self._equalizer_session_id(),
                basis_bundle_id=(
                    self.monitor.live_equalizer_tab.get_basis_bundle_id()
                ),
                gun_aligned=state.gun_aligned is True,
                realignment_active=state.realignment_active,
                session_active=state.session_active,
            )
            if stale:
                self._invalidate_equalizer_calibration(reason)
        self._rheed_qc_state = state
        self.monitor.update_rheed_qc_state(state)
        if self.classifier_worker and self.classifier_worker.isRunning():
            self.classifier_worker.set_rheed_qc_state(
                state,
                force_reset=reset_visual_history,
            )

    def _equalizer_session_id(self) -> str:
        session_dir = self.growth_log.session_dir
        return session_dir.name if session_dir is not None else ""

    def _session_metadata_with_camera(self) -> dict:
        """Session metadata plus the camera's acquisition provenance.

        In the same spirit as the ADS profile block in
        get_session_metadata: record what produced this session's frames so
        old archives stay auditable. Exposure and gain come from the
        camera's persistent user set, which the GUI does not set and
        nothing was recording — without this, two sessions can feed the
        classifier different intensity distributions with no way to tell
        after the fact.

        Shared by both session-ending paths. A session closed by shutting
        the window is exactly as much a session as one ended with STOP, and
        the two writing different metadata would be a silent gap in the
        archive rather than a visible bug.

        camera_worker is Optional and every other access in this class
        guards it; a session can end without one. Screengrab and dummy have
        no sensor to interrogate and report {}, so the key is omitted
        rather than written empty.

        The key says "at_connect" because that is literally what it is: the
        camera is opened at ARM, a session may START well afterwards, and
        an external holder such as kSA can change exposure mid-session. It
        carries its own read_at_utc so a reader can judge the gap. Do not
        promote this to a session-wide claim without per-frame or
        per-change logging behind it.
        """
        metadata = self.monitor.get_session_metadata()
        worker = self.camera_worker
        settings = (
            worker.sensor_settings_at_connect if worker is not None else {}
        )
        if settings:
            metadata["camera_sensor_settings_at_connect"] = settings
        return metadata

    def _current_auto_capture_metadata(self) -> dict:
        """Bind each buffered frame to its session, basis, and calibration."""
        metadata = self.monitor.get_current_capture_metadata()
        session_id = self._equalizer_session_id()
        if session_id:
            metadata["session_id"] = session_id
        basis_bundle_id = self.monitor.live_equalizer_tab.get_basis_bundle_id()
        if basis_bundle_id:
            metadata["basis_bundle_id"] = basis_bundle_id
        if self._equalizer_calibration is not None:
            metadata["calibration_id"] = (
                self._equalizer_calibration.calibration_id
            )
        return metadata

    def _invalidate_equalizer_calibration(self, reason: str) -> bool:
        """Invalidate the app-owned record and append its lifecycle event."""
        calibration = self._equalizer_calibration
        if calibration is None:
            return True
        reason = reason.strip() or "calibration invalidated"
        local_demo = str(calibration.capture_backend or "").startswith("dummy")
        if local_demo:
            self._equalizer_calibration = None
            self.monitor.live_equalizer_tab.invalidate_calibration(
                reason,
                emit=False,
            )
            return True
        journaled = False
        try:
            journaled = bool(
                self.growth_log.record_calibration_invalidation(
                    calibration,
                    reason,
                )
            )
        except (TypeError, ValueError, OSError) as exc:
            log.error("Equalizer invalidation journal write failed: %s", exc)
        self._equalizer_calibration = None
        self.monitor.live_equalizer_tab.invalidate_calibration(
            reason,
            emit=False,
        )
        if not journaled:
            log.critical(
                "Equalizer calibration %s was cleared in memory but its "
                "invalidation was not durably journaled",
                calibration.calibration_id,
            )
            self.statusBar().showMessage(
                "Equalizer provenance journal failed; calibration reuse is blocked.",
                7000,
            )
        return journaled

    @pyqtSlot(object)
    def _on_equalizer_calibration_accept(self, pending: object) -> None:
        """Validate, persist, and activate one grower-reviewed candidate."""
        if not isinstance(pending, CalibrationRecord):
            self.statusBar().showMessage(
                "Equalizer rejected an invalid calibration payload.", 4000,
            )
            return
        qc = self._rheed_qc_state
        evidence_snapshot = (
            self.monitor.live_equalizer_tab.get_calibration_snapshot()
        )
        evidence_kind = (
            self.monitor.live_equalizer_tab.get_handedness_evidence_kind()
        )
        if evidence_snapshot is None or not evidence_kind:
            self.monitor.live_equalizer_tab.invalidate_calibration(
                "handedness evidence was not explicitly confirmed",
                emit=False,
            )
            self.statusBar().showMessage(
                "Confirm asymmetric handedness evidence before acceptance.",
                5000,
            )
            return
        local_demo = str(
            evidence_snapshot.capture_backend or ""
        ).startswith("dummy")
        if not local_demo and (
            not self.growth_log.active
            or not qc.session_active
        ):
            self.monitor.live_equalizer_tab.invalidate_calibration(
                "A running session is required",
                emit=False,
            )
            self.statusBar().showMessage(
                "Confirm a stable RHEED view before accepting calibration.",
                4000,
            )
            return

        metadata = self.monitor.get_current_capture_metadata()
        basis_bundle = self.monitor.live_equalizer_tab.get_basis_bundle()
        basis_bundle_id = (
            basis_bundle.bundle_id
            if isinstance(basis_bundle, BasisBundle)
            else ""
        )
        if not basis_bundle_id or basis_bundle_id != pending.basis_bundle_id:
            self.monitor.live_equalizer_tab.invalidate_calibration(
                "canonical basis bundle unavailable or mismatched",
                emit=False,
            )
            self.statusBar().showMessage(
                "Calibration cannot be accepted without a canonical basis.",
                5000,
            )
            return
        stale, reason = calibration_is_stale(
            pending,
            source_hwnd=int(metadata.get("source_hwnd") or 0),
            camera_width=int(metadata.get("camera_width") or 0),
            camera_height=int(metadata.get("camera_height") or 0),
            capture_backend=str(metadata.get("capture_backend") or ""),
            capture_geometry_id=str(
                metadata.get("capture_geometry_id") or ""
            ),
            view_segment_id=(
                pending.view_segment_id if local_demo else qc.view_segment_id
            ),
            visual_history_generation=(
                pending.visual_history_generation
                if local_demo else qc.visual_history_generation
            ),
            session_id=(
                pending.session_id
                if local_demo else self._equalizer_session_id()
            ),
            basis_bundle_id=basis_bundle_id,
            gun_aligned=(
                pending.gun_aligned
                if local_demo else qc.gun_aligned is True
            ),
            realignment_active=(
                pending.realignment_active if local_demo else qc.realignment_active
            ),
            session_active=True,
        )
        if stale:
            self.monitor.live_equalizer_tab.invalidate_calibration(
                reason,
                emit=False,
            )
            self.statusBar().showMessage(
                f"Calibration became stale before acceptance: {reason}",
                5000,
            )
            return

        try:
            accepted = pending.accepted(
                accepted_by=self.monitor.grower_input.text().strip(),
                orientation_evidence_sha256=(
                    evidence_snapshot.orientation_evidence_sha256()
                ),
                orientation_evidence_kind=evidence_kind,
            )
        except (TypeError, ValueError, OSError) as exc:
            self.monitor.live_equalizer_tab.invalidate_calibration(
                f"orientation evidence invalid: {exc}",
                emit=False,
            )
            self.statusBar().showMessage(
                f"Calibration evidence rejected: {exc}", 5000,
            )
            return
        if self._equalizer_calibration is not None:
            invalidated = self._invalidate_equalizer_calibration(
                "superseded by a newly accepted calibration",
            )
            if not invalidated:
                self.monitor.live_equalizer_tab.invalidate_calibration(
                    "prior calibration invalidation was not journaled",
                    emit=False,
                )
                return
        if local_demo:
            recorded = True
        else:
            try:
                recorded = self.growth_log.record_calibration(
                    accepted,
                    evidence_snapshot=evidence_snapshot,
                    basis_bundle=basis_bundle,
                )
            except (TypeError, ValueError, OSError) as exc:
                log.warning("Equalizer calibration journal rejected record: %s", exc)
                recorded = False
        if not recorded:
            self.monitor.live_equalizer_tab.invalidate_calibration(
                "calibration journal write failed",
                emit=False,
            )
            self.statusBar().showMessage(
                "Calibration was not activated because provenance could not be saved.",
                5000,
            )
            return
        if not self.monitor.live_equalizer_tab.set_accepted_calibration(accepted):
            if local_demo:
                invalidated = True
            else:
                try:
                    invalidated = self.growth_log.record_calibration_invalidation(
                        accepted,
                        "GUI activation failed after journal write",
                    )
                except (TypeError, ValueError, OSError):
                    invalidated = False
            if not invalidated:
                log.critical(
                    "Accepted calibration %s could not be invalidated after "
                    "GUI activation failure",
                    accepted.calibration_id,
                )
            self.statusBar().showMessage(
                "Calibration failed compatibility checks; recalibrate.", 5000,
            )
            return
        self._equalizer_calibration = accepted
        if local_demo:
            self.statusBar().showMessage(
                f"Local alignment demo active ({accepted.parity}, not saved)",
                5000,
            )
        else:
            self.statusBar().showMessage(
                f"Equalizer calibrated ({accepted.parity}, "
                f"max residual {accepted.max_residual_px:.2f} px)",
                4000,
            )

    @pyqtSlot(str)
    def _on_equalizer_calibration_invalidation(self, reason: str) -> None:
        self._invalidate_equalizer_calibration(reason)

    def _fail_retrospective_calibration_acceptance(
        self,
        request_token: str,
        reason: str,
    ) -> None:
        log.warning("Retrospective Equalizer calibration rejected: %s", reason)
        self.monitor.events_tab.fail_retrospective_calibration_acceptance(
            request_token,
            reason,
        )
        self.statusBar().showMessage(
            f"Retrospective calibration rejected: {reason}", 6000,
        )

    def _fail_retrospective_calibration_resolution(
        self,
        request_token: str,
        reason: str,
    ) -> None:
        log.warning("Historical Equalizer calibration lookup rejected: %s", reason)
        self.monitor.events_tab.fail_retrospective_calibration_resolution(
            request_token,
            reason,
        )
        self.statusBar().showMessage(
            f"Historical calibration lookup rejected: {reason}", 6000,
        )

    @pyqtSlot(object)
    def _on_retrospective_calibration_resolve(self, payload: object) -> None:
        """Resolve historical records without giving Events journal ownership."""
        if not isinstance(payload, dict):
            log.warning("Historical calibration lookup was not a mapping")
            return
        request_token = str(payload.get("request_token") or "")
        raw_ids = payload.get("calibration_ids")
        snapshot = payload.get("snapshot")
        basis_bundle_id = str(payload.get("basis_bundle_id") or "")
        basis_bundle = payload.get("basis_bundle")
        main_basis_bundle = self.monitor.live_equalizer_tab.get_basis_bundle()
        if isinstance(raw_ids, (tuple, list)):
            calibration_ids = tuple(str(value or "").strip() for value in raw_ids)
        else:
            calibration_ids = ()
        if (
            not request_token
            or payload.get("retrospective") is not True
            or not isinstance(snapshot, RheedFrameSnapshot)
            or not snapshot.retrospective
            or not basis_bundle_id
            or not isinstance(basis_bundle, BasisBundle)
            or basis_bundle.bundle_id != basis_bundle_id
            or not isinstance(main_basis_bundle, BasisBundle)
            or main_basis_bundle.bundle_id != basis_bundle.bundle_id
            or not calibration_ids
            or len(calibration_ids) > 2
            or any(not value for value in calibration_ids)
            or len(set(calibration_ids)) != len(calibration_ids)
        ):
            self._fail_retrospective_calibration_resolution(
                request_token,
                "invalid or incomplete app-owned historical lookup request",
            )
            return

        session_id = self._equalizer_session_id()
        if (
            self.growth_log.session_dir is None
            or snapshot.session_id != session_id
        ):
            self._fail_retrospective_calibration_resolution(
                request_token,
                "the historical frame is not bound to the attached session",
            )
            return

        selected = None
        lookup_error = ""
        historical_getter = getattr(
            self.growth_log, "get_historical_calibration", None,
        )
        for calibration_id in calibration_ids:
            candidate = self._retrospective_equalizer_calibrations.get(
                calibration_id,
            )
            if candidate is None and historical_getter is not None:
                try:
                    candidate = historical_getter(calibration_id)
                except (OSError, TypeError, ValueError) as exc:
                    lookup_error = str(exc)
                    break
            if not isinstance(candidate, CalibrationRecord):
                continue
            if candidate.calibration_id != calibration_id:
                continue
            stale, _reason = calibration_is_stale(
                candidate,
                source_hwnd=snapshot.source_hwnd,
                camera_width=snapshot.camera_width,
                camera_height=snapshot.camera_height,
                capture_backend=snapshot.capture_backend,
                capture_geometry_id=snapshot.capture_geometry_id,
                view_segment_id=snapshot.view_segment_id,
                visual_history_generation=snapshot.visual_history_generation,
                session_id=session_id,
                basis_bundle_id=basis_bundle_id,
                gun_aligned=snapshot.gun_aligned,
                realignment_active=snapshot.realignment_active,
                session_active=True,
            )
            if stale or not candidate.grower_accepted:
                continue
            selected = candidate
            break

        if lookup_error:
            self._fail_retrospective_calibration_resolution(
                request_token,
                f"calibration journal lookup failed: {lookup_error}",
            )
            return
        if selected is None:
            self.monitor.events_tab.complete_retrospective_calibration_resolution(
                request_token,
                None,
                "No compatible app-owned calibration was found.",
            )
            return
        if not self.monitor.events_tab.complete_retrospective_calibration_resolution(
            request_token,
            selected,
        ):
            self._fail_retrospective_calibration_resolution(
                request_token,
                "Events rejected the app-owned historical calibration",
            )
            return
        self._retrospective_equalizer_calibrations[
            selected.calibration_id
        ] = selected
        self.statusBar().showMessage(
            f"Historical Equalizer calibration {selected.calibration_id} activated",
            5000,
        )

    @pyqtSlot(object)
    def _on_retrospective_calibration_accept(self, payload: object) -> None:
        """Own, validate, journal, then activate an Events calibration."""
        if not isinstance(payload, dict):
            log.warning("Retrospective calibration request was not a mapping")
            return
        request_token = str(payload.get("request_token") or "")
        pending = payload.get("pending")
        snapshot = payload.get("snapshot")
        evidence_kind = str(payload.get("evidence_kind") or "")
        basis_bundle = payload.get("basis_bundle")
        main_basis_bundle = self.monitor.live_equalizer_tab.get_basis_bundle()
        if (
            not request_token
            or payload.get("retrospective") is not True
            or payload.get("evidence_confirmed") is not True
            or not isinstance(pending, CalibrationRecord)
            or not isinstance(snapshot, RheedFrameSnapshot)
            or not snapshot.retrospective
            or not evidence_kind
            or not isinstance(basis_bundle, BasisBundle)
            or basis_bundle.bundle_id != pending.basis_bundle_id
            or not isinstance(main_basis_bundle, BasisBundle)
            or main_basis_bundle.bundle_id != basis_bundle.bundle_id
            or pending.grower_accepted
            or bool(pending.invalidated_reason)
        ):
            self._fail_retrospective_calibration_acceptance(
                request_token,
                "invalid or incomplete app-owned acceptance request",
            )
            return

        session_id = self._equalizer_session_id()
        stale, reason = calibration_is_stale(
            pending,
            source_hwnd=snapshot.source_hwnd,
            camera_width=snapshot.camera_width,
            camera_height=snapshot.camera_height,
            capture_backend=snapshot.capture_backend,
            capture_geometry_id=snapshot.capture_geometry_id,
            view_segment_id=snapshot.view_segment_id,
            visual_history_generation=snapshot.visual_history_generation,
            session_id=session_id,
            basis_bundle_id=basis_bundle.bundle_id,
            gun_aligned=snapshot.gun_aligned,
            realignment_active=snapshot.realignment_active,
            session_active=(
                self.growth_log.session_dir is not None
                and snapshot.session_id == session_id
            ),
        )
        if stale or not snapshot.gun_aligned or snapshot.realignment_active:
            self._fail_retrospective_calibration_acceptance(
                request_token,
                reason or "historical frame lacks stable RHEED QC provenance",
            )
            return

        try:
            accepted = pending.accepted(
                accepted_by=(
                    self.monitor.grower_input.text().strip()
                    or "events-retrospective"
                ),
                orientation_evidence_sha256=(
                    snapshot.orientation_evidence_sha256()
                ),
                orientation_evidence_kind=evidence_kind,
            )
            recorded = self.growth_log.record_calibration(
                accepted,
                evidence_snapshot=snapshot,
                basis_bundle=basis_bundle,
            )
        except (TypeError, ValueError, OSError) as exc:
            recorded = False
            reason = str(exc)
        if not recorded:
            self._fail_retrospective_calibration_acceptance(
                request_token,
                reason or "calibration journal/evidence write failed",
            )
            return

        if not self.monitor.events_tab.complete_retrospective_calibration_acceptance(
            request_token,
            accepted,
        ):
            try:
                invalidated = self.growth_log.record_calibration_invalidation(
                    accepted,
                    "Events panel activation failed after journal write",
                )
            except (TypeError, ValueError, OSError):
                invalidated = False
            if not invalidated:
                log.critical(
                    "Retrospective calibration %s could not be invalidated",
                    accepted.calibration_id,
                )
            self._fail_retrospective_calibration_acceptance(
                request_token,
                "Events panel rejected the journaled calibration",
            )
            return

        self._retrospective_equalizer_calibrations[
            accepted.calibration_id
        ] = accepted
        self.statusBar().showMessage(
            f"Retrospective Equalizer calibration {accepted.calibration_id} accepted",
            5000,
        )

    @pyqtSlot(object)
    def _on_retrospective_calibration_invalidation(
        self,
        payload: object,
    ) -> None:
        """Journal a popup calibration invalidation before confirming it."""
        if not isinstance(payload, dict):
            return
        request_token = str(payload.get("request_token") or "")
        calibration = payload.get("calibration")
        reason = str(
            payload.get("reason") or "retrospective calibration invalidated"
        )
        if (
            not request_token
            or payload.get("retrospective") is not True
            or not isinstance(calibration, CalibrationRecord)
        ):
            self.monitor.events_tab.fail_retrospective_calibration_invalidation(
                request_token,
                "invalid app-owned invalidation request",
            )
            return
        owned = self._retrospective_equalizer_calibrations.get(
            calibration.calibration_id,
        )
        if owned is not calibration:
            self.monitor.events_tab.fail_retrospective_calibration_invalidation(
                request_token,
                "calibration is not owned by GrowthApp",
            )
            return
        try:
            journaled = self.growth_log.record_calibration_invalidation(
                owned,
                reason,
            )
        except (TypeError, ValueError, OSError) as exc:
            journaled = False
            reason = str(exc)
        if not journaled:
            self.monitor.events_tab.fail_retrospective_calibration_invalidation(
                request_token,
                reason or "calibration invalidation journal write failed",
            )
            self.statusBar().showMessage(
                "Retrospective calibration invalidation was not journaled.",
                6000,
            )
            return
        self._retrospective_equalizer_calibrations.pop(
            calibration.calibration_id,
            None,
        )
        self.monitor.events_tab.complete_retrospective_calibration_invalidation(
            request_token,
        )

    def _record_rheed_view_event(
        self,
        event_type: str,
        elapsed_s: float,
        *,
        previous_view_segment_id: Optional[int] = None,
        frame_role: str = "",
        note: str = "",
        qc_reject: Optional[bool] = None,
        qc_reason: str = "",
        frame: Optional[np.ndarray] = None,
        capture_metadata: Optional[dict] = None,
    ) -> int:
        """Persist one view/QC event with the exact displayed frame."""
        if frame is None:
            frame = self.monitor.get_current_frame()
        if capture_metadata is None:
            capture_metadata = self.monitor.get_current_capture_metadata()
        return self.growth_log.record_rheed_view_event(
            event_type,
            elapsed_s,
            state_snapshot=self._rheed_qc_snapshot(
                qc_reject=qc_reject,
                qc_reason=qc_reason,
            ),
            realignment_id=self._rheed_qc_state.realignment_id,
            previous_view_segment_id=previous_view_segment_id,
            frame_role=frame_role,
            frame=frame,
            note=note,
            capture_metadata=capture_metadata,
            labeler=self.monitor.grower_input.text().strip(),
        )

    def _current_fresh_rheed_capture(
        self,
    ) -> tuple[Optional[np.ndarray], dict, Optional[tuple]]:
        """Return the current frame, metadata, and a stable identity token."""
        frame = self.monitor.get_current_frame()
        metadata = self.monitor.get_current_capture_metadata()
        if frame is None or not metadata:
            return None, {}, None
        try:
            age_ms = float(metadata.get("frame_age_ms", float("inf")))
        except (TypeError, ValueError):
            return None, metadata, None
        if not np.isfinite(age_ms) or age_ms > RHEED_EVENT_MAX_FRAME_AGE_MS:
            return None, metadata, None
        sequence = int(metadata.get("capture_sequence") or 0)
        captured_at = str(metadata.get("captured_at_utc") or "")
        if sequence <= 0 and not captured_at:
            return None, metadata, None
        token = (
            str(metadata.get("capture_backend") or ""),
            int(metadata.get("source_hwnd") or 0),
            sequence,
            captured_at,
        )
        return frame, metadata, token

    @pyqtSlot(dict)
    def _on_rheed_view_event(self, payload: dict):
        """Record a lightweight RHEED adjustment or explicit QC label."""
        if not self.growth_log.active or not self._rheed_qc_state.session_active:
            self.statusBar().showMessage(
                "Start a session before recording RHEED QC.", 3000,
            )
            return

        event_type = str(payload.get("event_type", ""))
        elapsed_s = float(payload.get("elapsed_s", 0.0))
        note = str(payload.get("note", "")).strip()
        state = self._rheed_qc_state
        previous_segment = state.view_segment_id
        frame_role = ""
        qc_reject: Optional[bool] = None
        qc_reason = ""
        frame, capture_metadata, capture_token = (
            self._current_fresh_rheed_capture()
        )
        adjustment_events = {
            RHEED_VIEW_EVENT_SAMPLE_DIRECTION_ADJUSTED,
            RHEED_VIEW_EVENT_CURRENT_ADJUSTED,
            RHEED_VIEW_EVENT_ENERGY_ADJUSTED,
        }
        if event_type in adjustment_events:
            # These are annotations only. They deliberately do not change the
            # view segment, calibration, classifier, visual history, heartbeat,
            # or auto-capture state. A missing frame must not erase knowledge
            # that the operator adjusted the instrument.
            frame_role = event_type
        elif capture_token is None:
            self.statusBar().showMessage(
                "A fresh connected RHEED frame is required for this QC label.",
                4000,
            )
            return
        elif event_type == RHEED_VIEW_EVENT_QC_PASS:
            frame_role = "qc_pass"
            qc_reject = False
        elif event_type == RHEED_VIEW_EVENT_QC_REJECT:
            frame_role = "qc_reject"
            qc_reject = True
            qc_reason = note or "operator_reject"
        else:
            self.statusBar().showMessage(
                f"Unsupported RHEED QC event: {event_type}", 3000,
            )
            return

        self._record_rheed_view_event(
            event_type,
            elapsed_s,
            previous_view_segment_id=previous_segment,
            frame_role=frame_role,
            note=note,
            qc_reject=qc_reject,
            qc_reason=qc_reason,
            frame=frame,
            capture_metadata=capture_metadata,
        )

        self.statusBar().showMessage(f"RHEED event recorded: {event_type}", 2500)

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
        write_started_ns = time.perf_counter_ns()
        event_index = self.growth_log.record_manual_event(
            elapsed_s=payload.get("elapsed_s", 0.0),
            pyro_temp=payload.get("pyro_temp"),
            voltage_V=payload.get("voltage_V"),
            current_A=payload.get("current_A"),
            psu_source=payload.get("psu_source", "none"),
            frame=frame,
            note=payload.get("note", ""),
            capture_metadata=capture_metadata,
        )
        write_completed_ns = time.perf_counter_ns()
        source_ns = int(capture_metadata.get("captured_monotonic_ns") or 0)
        GrowthApp._trace_temporal(
            self,
            "manual_event_saved",
            "manual_event",
            details={
                "event_index": event_index,
                "capture_sequence": capture_metadata.get("capture_sequence"),
                "write_duration_ms": (
                    write_completed_ns - write_started_ns
                ) / 1_000_000.0,
                "source_age_at_save_ms": (
                    (write_completed_ns - source_ns) / 1_000_000.0
                    if source_ns > 0 else None
                ),
            },
        )
        self.statusBar().showMessage("Event marked", 2000)

    # --- LIVE EQUALIZER save (Jul 10 2026 workstream #4) -------------------

    @pyqtSlot(dict)
    def _on_live_label_save(self, payload: dict):
        """Write one label bound to the tab's immutable camera snapshot."""
        weights = payload.get("final_weights") if isinstance(payload, dict) else None
        if not isinstance(weights, dict):
            self.statusBar().showMessage(
                "Live label rejected: invalid Equalizer payload.", 4000,
            )
            return
        if not self.growth_log.active:
            self.statusBar().showMessage(
                "Start a session before saving live labels.", 3000,
            )
            return
        calibration = self._equalizer_calibration
        snapshot = self.monitor.live_equalizer_tab.get_current_snapshot()
        qc = self._rheed_qc_state
        if calibration is None or snapshot is None:
            self.statusBar().showMessage(
                "Accept a camera calibration on a fresh RHEED frame first.",
                4000,
            )
            return
        if (
            not qc.session_active
            or qc.gun_aligned is not True
            or qc.realignment_active
        ):
            self._invalidate_equalizer_calibration(
                "RHEED view is not in a stable aligned state",
            )
            self.statusBar().showMessage(
                "Live label rejected: confirm a stable gun alignment.", 4000,
            )
            return
        frame_age_ms = snapshot.age_ms()
        if frame_age_ms > RHEED_EVENT_MAX_FRAME_AGE_MS:
            self.statusBar().showMessage(
                f"Live label rejected: RHEED frame is stale ({frame_age_ms:.0f} ms).",
                4000,
            )
            return
        stale, reason = calibration_is_stale(
            calibration,
            source_hwnd=snapshot.source_hwnd,
            camera_width=snapshot.camera_width,
            camera_height=snapshot.camera_height,
            capture_backend=snapshot.capture_backend,
            capture_geometry_id=snapshot.capture_geometry_id,
            view_segment_id=qc.view_segment_id,
            visual_history_generation=qc.visual_history_generation,
            session_id=self._equalizer_session_id(),
            basis_bundle_id=(
                self.monitor.live_equalizer_tab.get_basis_bundle_id()
            ),
            gun_aligned=qc.gun_aligned is True,
            realignment_active=qc.realignment_active,
            session_active=qc.session_active,
        )
        if stale:
            self._invalidate_equalizer_calibration(reason)
            self.statusBar().showMessage(
                f"Live label rejected: {reason}", 4000,
            )
            return

        # PSU snapshot — identical priority to _on_manual_event: mistral
        # first (current O-MBE topology), direct-read next.
        voltage_v: Optional[float] = None
        current_a: Optional[float] = None
        psu_source = "none"
        m = self.monitor._latest_mistral
        if m is not None and m.connected and m.valid:
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
        if pyro is not None and pyro.has_valid_reading:
            pyro_temp = pyro.temperature

        write_started_ns = time.perf_counter_ns()
        try:
            idx = self.growth_log.record_live_label(
                elapsed_s=self.monitor.get_elapsed_seconds(),
                weights=weights,
                calibration=calibration,
                snapshot=snapshot,
                pyro_temp=pyro_temp,
                voltage_V=voltage_v,
                current_A=current_a,
                psu_source=psu_source,
                equalizer_payload=payload,
                labeler=self.monitor.grower_input.text().strip(),
            )
        except (TypeError, ValueError, OSError) as exc:
            log.warning("Live Equalizer label rejected: %s", exc)
            self.statusBar().showMessage(
                f"Live label rejected: {exc}", 5000,
            )
            return
        if idx > 0:
            write_completed_ns = time.perf_counter_ns()
            GrowthApp._trace_temporal(self,
                "equalizer_label_saved", "equalizer",
                details={
                    "label_index": idx,
                    "capture_sequence": snapshot.capture_sequence,
                    "calibration_id": calibration.calibration_id,
                    "frame_age_at_request_ms": frame_age_ms,
                    "source_age_at_save_ms": snapshot.age_ms(
                        write_completed_ns,
                    ),
                    "write_duration_ms": (
                        write_completed_ns - write_started_ns
                    ) / 1_000_000.0,
                },
            )
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
        # `has_valid_reading` is stricter than `connected` — it also
        # rejects the pre-first-read window and any failed batch after,
        # so an empty pyrometer cell in sensor_log.csv genuinely means
        # "no reading" instead of a spurious 0.0.
        pyro_ok = pyro is not None and pyro.has_valid_reading
        pyro_temp = pyro.temperature if pyro_ok else None
        pyro_temp_std = pyro.temperature_std if pyro_ok else None
        pyro_temp_n = pyro.temperature_n if pyro_ok else None
        m = self.monitor._latest_mistral
        e = self.monitor._latest_evap
        mistral_ok = m is not None and m.connected and m.valid
        evap_ok = e is not None and e.connected and e.valid
        ads_cells = m.ads_cells if mistral_ok else None
        # Pressure priority: EvapControl elog (O-MBE) → ADS ion_gauge_1_P (Ch-MBE ADS mode)
        _evap_p = e.chamber_pressure_mbar if evap_ok else None
        _ads_p  = ads_cells.get("ion_gauge_1_P") if ads_cells else None
        _pressure = _evap_p if _evap_p is not None else _ads_p
        snapshot_monotonic_ns = time.perf_counter_ns()
        snapshot_at_utc = utc_now_iso()
        previous_tick_ns = self._last_sensor_log_tick_ns
        self._last_sensor_log_tick_ns = snapshot_monotonic_ns
        timer_interval_ms = None
        timer_jitter_ms = None
        if previous_tick_ns is not None:
            timer_interval_ms = (
                snapshot_monotonic_ns - previous_tick_ns
            ) / 1_000_000.0
            timer_jitter_ms = (
                timer_interval_ms - self._sensor_log_timer.interval()
            )
        camera = self.monitor._latest_camera
        snapshots = {
            "rheed": snapshot_state(
                "rheed", camera, snapshot_monotonic_ns,
            ),
            "pyrometer": snapshot_state(
                "pyrometer", pyro, snapshot_monotonic_ns,
            ),
            "mistral": snapshot_state(
                "mistral", m, snapshot_monotonic_ns,
            ),
            "evap": snapshot_state(
                "evap", e, snapshot_monotonic_ns,
            ),
        }
        sync = synchronization_summary(snapshots)
        rheed_timing = snapshots["rheed"].to_dict()
        if camera is not None:
            rheed_timing.update({
                "capture_backend": camera.capture_backend,
                "source_hwnd": camera.source_hwnd,
            })
        pyro_timing = snapshots["pyrometer"].to_dict()
        mistral_timing = snapshots["mistral"].to_dict()
        evap_timing = snapshots["evap"].to_dict()
        write_started_ns = time.perf_counter_ns()
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
            pyrometer_source_at_utc=pyro_timing["source_at_utc"],
            pyrometer_received_at_utc=pyro_timing["received_at_utc"],
            pyrometer_sample_sequence=pyro_timing["sequence"],
            pyrometer_age_ms=pyro_timing["age_ms"],
            pyrometer_read_duration_ms=pyro_timing["read_duration_ms"],
            mistral_source_at_utc=mistral_timing["source_at_utc"],
            mistral_received_at_utc=mistral_timing["received_at_utc"],
            mistral_sample_sequence=mistral_timing["sequence"],
            mistral_age_ms=mistral_timing["age_ms"],
            mistral_read_duration_ms=mistral_timing["read_duration_ms"],
            evap_source_at_utc=evap_timing["source_at_utc"],
            evap_received_at_utc=evap_timing["received_at_utc"],
            evap_sample_sequence=evap_timing["sequence"],
            evap_age_ms=evap_timing["age_ms"],
            evap_read_duration_ms=evap_timing["read_duration_ms"],
            snapshot_at_utc=snapshot_at_utc,
            snapshot_monotonic_ns=snapshot_monotonic_ns,
            snapshot_timer_interval_ms=timer_interval_ms,
            snapshot_timer_jitter_ms=timer_jitter_ms,
            sync_span_ms=sync["sync_span_ms"],
            sync_complete=sync["sync_complete"],
            sync_valid=sync["sync_valid"],
            rheed_timing=rheed_timing,
            pyrometer_timing=pyro_timing,
            mistral_timing=mistral_timing,
            evap_timing=evap_timing,
        )
        flush_completed_ns = time.perf_counter_ns()
        GrowthApp._trace_temporal(self,
            "sensor_snapshot", "gui",
            details={
                "snapshot_at_utc": snapshot_at_utc,
                "snapshot_monotonic_ns": snapshot_monotonic_ns,
                "timer_interval_ms": timer_interval_ms,
                "timer_jitter_ms": timer_jitter_ms,
                "csv_flush_duration_ms": (
                    flush_completed_ns - write_started_ns
                ) / 1_000_000.0,
                **sync,
                "sources": {
                    name: item.to_dict() for name, item in snapshots.items()
                },
                "process": process_metrics(),
            },
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

    @pyqtSlot(WeakPrimaryShadowState)
    def _on_weak_primary_shadow_state(
        self, state: WeakPrimaryShadowState,
    ) -> None:
        """Display and trace the isolated brightness-robust diagnostic."""
        if self._shutdown_pending:
            return
        state = _stamp_gui_received(state)
        self._latest_weak_primary_shadow = state
        self.monitor.update_weak_primary_shadow_state(state)
        if self.growth_log.active:
            GrowthApp._trace_temporal(
                self,
                "weak_primary_shadow_state",
                "brightness_robust_four_output_all_extreme",
                details={
                    "source_capture_sequence": state.source_capture_sequence,
                    "source_received_monotonic_ns": (
                        state.source_received_monotonic_ns
                    ),
                    "inference_started_monotonic_ns": (
                        state.inference_started_monotonic_ns
                    ),
                    "inference_completed_monotonic_ns": (
                        state.inference_completed_monotonic_ns
                    ),
                    "worker_emitted_monotonic_ns": (
                        state.worker_emitted_monotonic_ns
                    ),
                    "gui_received_monotonic_ns": (
                        state.gui_received_monotonic_ns
                    ),
                    "inference_ms": state.inference_ms,
                    "conditional_probabilities": dict(
                        state.conditional_probabilities
                    ),
                    "predicted_class": state.predicted_class,
                    "predicted_applicability": (
                        state.predicted_applicability
                    ),
                    "normalized_entropy": state.normalized_entropy,
                    "checkpoint_disagreement": (
                        state.checkpoint_disagreement
                    ),
                    "checkpoint_count": state.checkpoint_count,
                    "ensemble_id": state.ensemble_id,
                    "bundle_family": state.bundle_family,
                    "brightness_policy": state.brightness_policy,
                    "output_classes": list(state.output_classes),
                    "lambda_pair": state.lambda_pair,
                    "execution_scope": state.execution_scope,
                    "actionable": False,
                    "abstain_reason": state.abstain_reason,
                    "error": state.error,
                },
            )

    @pyqtSlot(ClassifierState)
    def _on_classifier_state(self, state: ClassifierState):
        """Forward classifier worker state to the monitor's slider slot.

        Also caches the immutable worker snapshot so auto-capture can bind it
        to the current event.
        """
        state = _stamp_gui_received(state)
        qc = self._rheed_qc_state
        if (
            qc.session_active
            and state.visual_history_generation
            != qc.visual_history_generation
        ):
            # Queued signals from before a same-segment camera reset or gun
            # boundary must not restore stale percentages/readiness.
            return

        self._latest_classifier = state
        self.monitor.update_classifier_state(state)
        if self.growth_log.active:
            capture_to_complete_ms = None
            if (
                state.source_received_monotonic_ns > 0
                and state.inference_completed_monotonic_ns > 0
            ):
                capture_to_complete_ms = (
                    state.inference_completed_monotonic_ns
                    - state.source_received_monotonic_ns
                ) / 1_000_000.0
            GrowthApp._trace_temporal(self,
                "classifier_state", "classifier",
                details={
                    "source_capture_sequence": state.source_capture_sequence,
                    "source_received_monotonic_ns": (
                        state.source_received_monotonic_ns
                    ),
                    "inference_started_monotonic_ns": (
                        state.inference_started_monotonic_ns
                    ),
                    "inference_completed_monotonic_ns": (
                        state.inference_completed_monotonic_ns
                    ),
                    "worker_emitted_monotonic_ns": (
                        state.worker_emitted_monotonic_ns
                    ),
                    "gui_received_monotonic_ns": (
                        state.gui_received_monotonic_ns
                    ),
                    "inference_ms": state.inference_ms,
                    "capture_to_inference_complete_ms": capture_to_complete_ms,
                    "error": state.error,
                },
            )

        if (
            qc.session_active
            and state.view_segment_id == qc.view_segment_id
            and state.gun_aligned == qc.gun_aligned
            and state.visual_history_generation
            == qc.visual_history_generation
        ):
            became_ready = (
                int(state.history_required) > 0
                and not qc.history_ready
                and bool(state.history_ready)
            )
            updated = replace(
                qc,
                history_frame_count=int(state.history_frame_count),
                history_required=int(state.history_required),
                history_ready=bool(state.history_ready),
            )
            self._rheed_qc_state = updated
            self.monitor.update_rheed_qc_state(updated)
            if became_ready and self.growth_log.active:
                self._record_rheed_view_event(
                    RHEED_VIEW_EVENT_HISTORY_READY,
                    self.monitor.get_elapsed_seconds(),
                    previous_view_segment_id=updated.view_segment_id,
                    frame_role="history_ready",
                )

    @pyqtSlot(CameraState)
    def _on_camera_state(self, state: CameraState):
        # QThread signals queued before closeEvent may be delivered after the
        # session files have closed.  Do not let those frames update UI state,
        # re-arm automation, or enter a post-session auto-capture buffer.
        if self._shutdown_pending:
            return

        state = _stamp_gui_received(state)
        self._announce_camera_exposure(state)
        if self.growth_log.active:
            GrowthApp._trace_temporal(self,
                "state_received", "rheed",
                timing=snapshot_state("rheed", state).to_dict(),
                details={
                    "capture_backend": state.capture_backend,
                    "source_hwnd": state.source_hwnd,
                    "capture_geometry_id": state.capture_geometry_id,
                },
            )

        self.monitor.update_camera_state(state)
        self.rheed_intensity_window.on_camera_state(state)

        if not state.connected and state.error:
            # Every camera backend is fail-closed. Stop all image-producing
            # automation; sensor logging may continue so the session record
            # still shows when the camera source was lost.
            self._heartbeat_timer.stop()
            self.auto_capture_engine.enabled = False
            self.auto_capture_engine.reset()
            self.monitor.set_rheed_reconnect_required(
                state.mode == "screengrab"
            )
            self.monitor.live_equalizer_tab.set_save_enabled(False)
            self.monitor.set_classifier_capture_unavailable(
                "RHEED capture unavailable; classification stopped",
            )
            self.monitor.set_auto_capture_status(
                "Auto-capture: stopped (RHEED capture unavailable)"
            )
            if (
                self._rheed_qc_state.session_active
                and not self._camera_capture_interrupted
            ):
                self._camera_capture_interrupted = True
                interrupted = replace(
                    self._rheed_qc_state,
                    history_frame_count=0,
                    history_ready=False,
                )
                self._apply_rheed_qc_state(
                    interrupted,
                    reset_visual_history=True,
                )
                self._record_rheed_view_event(
                    RHEED_VIEW_EVENT_HISTORY_RESET,
                    self.monitor.get_elapsed_seconds(),
                    previous_view_segment_id=interrupted.view_segment_id,
                    frame_role="camera_disconnect",
                    note=state.error,
                )
            self.statusBar().showMessage(
                f"RHEED capture stopped: {state.error}",
                10000,
            )
        elif (
            state.connected
            and getattr(state, "valid", state.connected)
            and state.frame is not None
        ):
            self._camera_capture_interrupted = False
            # The operator restored the detached window and explicitly
            # reconnected. Hide the retry control only after a fresh frame.
            if state.mode == "screengrab":
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
                        "Auto-capture: PAUSED (RHEED restored)"
                    )
                else:
                    self.auto_capture_engine.enabled = True
                    self.monitor.set_auto_capture_status(
                        "Auto-capture: armed (warmup after camera reconnect)"
                    )

        # ``enabled`` is only one layer of the gate: an explicit active-session
        # check prevents stale UI/QC state from feeding the engine after STOP.
        # A re-served frame is not new evidence. The change detector scores
        # std-of-|diff| against the previous frame, so a duplicate scores a
        # structural zero — it does not merely add a neutral sample, it
        # drags the score down and biases the engine against firing on a
        # real transition. Heartbeat saving and the classifier already skip
        # repeats via their own capture-identity guards; this is the third
        # consumer named in 27b010c and the one with no guard of its own.
        if (
            self.growth_log.active
            and state.frame is not None
            and state.connected
            and getattr(state, "valid", state.connected)
            and not getattr(state, "is_duplicate", False)
        ):
            self.auto_capture_engine.evaluate(
                state.frame,
                self._current_auto_capture_metadata(),
            )
            if self.auto_capture_engine.enabled:
                self.monitor.set_auto_capture_status(
                    f"Auto-capture: armed | "
                    f"score: {self.auto_capture_engine.latest_score:.2f} | "
                    f"events: {self._auto_capture_event_count}"
                )

    def _announce_camera_exposure(self, state) -> None:
        """Tell the grower the exposure the camera actually confirmed.

        The readback, not the request — those differ whenever the device
        quantises onto its increment grid, and the grower needs to see what
        the frames were really taken at. Fires only on change, so a 1 Hz
        state stream does not repaint the status bar every second.
        """
        exposure_us = getattr(state, "exposure_us", None)
        if (
            state.connected
            and exposure_us is not None
            and exposure_us != self._reported_camera_exposure_us
        ):
            self._reported_camera_exposure_us = exposure_us
            self.statusBar().showMessage(
                f"Direct camera exposure confirmed: "
                f"{exposure_us / 1000.0:.0f} ms",
                5000,
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
        try:
            capture_sequence = int(
                capture_metadata.get("capture_sequence") or 0
            )
        except (TypeError, ValueError):
            return
        captured_at_utc = str(
            capture_metadata.get("captured_at_utc") or ""
        )
        if capture_sequence <= 0 and not captured_at_utc:
            return
        capture_token = (
            str(capture_metadata.get("capture_backend") or ""),
            int(capture_metadata.get("source_hwnd") or 0),
            capture_sequence,
            captured_at_utc,
        )
        if capture_token == self._last_heartbeat_capture_token:
            return
        write_started_ns = time.perf_counter_ns()
        path = self.growth_log.save_heartbeat_frame(frame)
        if not path:
            GrowthApp._trace_temporal(self,
                "frame_write_rejected", "heartbeat",
                details={
                    "capture_sequence": capture_sequence,
                    "write_duration_ms": (
                        time.perf_counter_ns() - write_started_ns
                    ) / 1_000_000.0,
                },
            )
            return  # Quality gate rejected, or save failed
        # Heartbeat frame capture MUST NOT depend on pyrometer health —
        # if camera quality passed, we save the frame. Pyrometer just
        # gets logged as blank when there's no valid reading (post-connect
        # window or failed batch); previously this used `connected` alone
        # and leaked 0.0 into the heartbeat log.
        pyro_temp = (
            self.monitor._latest_pyro.temperature
            if self.monitor._latest_pyro and self.monitor._latest_pyro.has_valid_reading
            else None
        )
        self.growth_log.log_heartbeat(
            elapsed_s=self.monitor.get_elapsed_seconds(),
            pyro_temp=pyro_temp,
            frame_path=path,
            capture_metadata=capture_metadata,
        )
        write_completed_ns = time.perf_counter_ns()
        source_ns = int(
            capture_metadata.get("captured_monotonic_ns") or 0,
        )
        GrowthApp._trace_temporal(self,
            "frame_saved", "heartbeat",
            details={
                "capture_sequence": capture_sequence,
                "frame_path": path,
                "write_started_monotonic_ns": write_started_ns,
                "write_completed_monotonic_ns": write_completed_ns,
                "write_duration_ms": (
                    write_completed_ns - write_started_ns
                ) / 1_000_000.0,
                "source_age_at_save_ms": (
                    (write_completed_ns - source_ns) / 1_000_000.0
                    if source_ns > 0 else None
                ),
            },
        )
        self._last_heartbeat_capture_token = capture_token
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
        # A signal already queued when shutdown/STOP began can arrive after
        # GrowthLogger.end_session(), whose session directory intentionally
        # remains available for exports.  Guard before incrementing the event
        # counter or writing any image so that retained path cannot create an
        # orphan, unlogged auto-event directory.
        if self._shutdown_pending or not self.growth_log.active:
            log.debug(
                "Ignored auto-capture event without a writable active session"
            )
            return

        event_idx = self._auto_capture_event_count + 1
        # Auto-capture is driven by RHEED, not pyrometer health. Preserve the
        # event and leave its temperature blank when no fresh reading exists.
        pyro_temp = (
            self.monitor._latest_pyro.temperature
            if self.monitor._latest_pyro and self.monitor._latest_pyro.has_valid_reading
            else None
        )
        context_captures = self.auto_capture_engine.get_recent_captures()
        trigger_metadata = (
            context_captures[-1][1]
            if context_captures
            else self._current_auto_capture_metadata()
        )
        elapsed_s = self.monitor.get_elapsed_seconds()
        try:
            classifier_snapshot = self._build_classifier_snapshot_payload(
                event_idx=event_idx,
                event_score=score,
                elapsed_s=elapsed_s,
                capture_metadata=trigger_metadata,
            )
            result = self.growth_log.record_auto_capture_event(
                event_idx=event_idx,
                score=score,
                elapsed_s=elapsed_s,
                pyro_temp=pyro_temp,
                frames=[item[0] for item in context_captures],
                frame_capture_metadata=[item[1] for item in context_captures],
                event_capture_metadata=trigger_metadata,
                classifier_snapshot=classifier_snapshot,
            )
        except (OSError, TypeError, ValueError) as exc:
            log.error("Auto-capture transaction rejected event %d: %s", event_idx, exc)
            result = None
        if result is None or not result.committed:
            self.statusBar().showMessage(
                "Auto-capture event was not saved; transaction recovery is required.",
                7000,
            )
            return
        self._auto_capture_event_count = event_idx
        buffer_count = result.buffer_count
        buffer_dir = result.buffer_dir
        if not result.writer_ready:
            self.auto_capture_engine.enabled = False
            self.monitor.set_auto_capture_status(
                "Auto-capture: ERROR (event saved, CSV writer unavailable)"
            )
        # Empty-buffer events have nothing to review — quality gate rejected
        # all 20 frames. Mark as auto_skipped at log time so the CSV row
        # gets a terminal state instead of sitting at pending forever.
        # Snapshot the latest classifier state into the event directory.
        # Gives Justin's team a per-event training pair — visual context
        # buffer + the model's opinion at trigger time — for every flagged
        # event, essentially free. Only writes when there's a buffer dir
        # (buffer_count > 0) AND a classifier state to snapshot.
        # Surface the banner so the grower can discard if it looks spurious.
        # Default action (timeout) is to keep — the buffer is already on disk.
        if buffer_count > 0:
            self.monitor.show_auto_capture_event(
                event_idx=self._auto_capture_event_count,
                score=score,
                buffer_dir=buffer_dir,
            )
        if not result.writer_ready:
            self.statusBar().showMessage(
                "Auto-capture event saved, but further capture is disabled: "
                "the event CSV could not be reopened.",
                9000,
            )

    def _build_classifier_snapshot_payload(
        self,
        *,
        event_idx: int,
        event_score: float,
        elapsed_s: float,
        capture_metadata: dict,
    ) -> Optional[dict]:
        """Freeze classifier metadata before the auto-event WAL is written.

        Note: the snapshot reflects the *most recent* classifier emission,
        which may be up to ``ClassifierWorker.POLL_INTERVAL_S`` (~0.5 s)
        older than the frame that triggered the auto-capture. That
        latency is acceptable for training-data purposes; if it ever
        becomes limiting, the fix is to classify the flagged frame
        synchronously here instead of using the cached state.
        """
        from datetime import datetime as _dt

        cs = self._latest_classifier
        if cs is None:
            return None

        smoothed = cs.smoothed_percent or {}
        argmax_class = (
            max(smoothed.items(), key=lambda kv: kv[1])[0]
            if smoothed and cs.has_confident_data
            else None
        )
        return {
            "timestamp_iso": _dt.now().isoformat(),
            "event_idx": event_idx,
            "event_score": float(event_score),
            "elapsed_s": float(elapsed_s),
            "capture_backend": str(capture_metadata.get("capture_backend") or ""),
            "captured_at_utc": str(capture_metadata.get("captured_at_utc") or ""),
            "capture_sequence": int(capture_metadata.get("capture_sequence") or 0),
            "source_hwnd": int(capture_metadata.get("source_hwnd") or 0),
            "capture_geometry_id": str(
                capture_metadata.get("capture_geometry_id") or ""
            ),
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
            "model_input_mode": cs.model_input_mode,
            "view_segment_id": cs.view_segment_id,
            "visual_history_generation": int(
                cs.visual_history_generation
            ),
            "gun_aligned": cs.gun_aligned,
            "history_frame_count": int(cs.history_frame_count),
            "history_required": int(cs.history_required),
            "history_ready": bool(cs.history_ready),
            "prediction_actionable": bool(cs.prediction_actionable),
        }

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
        updated = self.growth_log.update_auto_capture_state(event_idx, state)
        if not updated:
            self.statusBar().showMessage(
                f"Event #{event_idx} decision was not saved; inspect the event CSV.",
                7000,
            )
        elif state == EVENT_STATE_DISCARDED:
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
            qc = self._rheed_qc_state
            _, _, capture_token = self._current_fresh_rheed_capture()
            if not self.growth_log.active:
                self.auto_capture_engine.enabled = False
                self.monitor.set_auto_capture_status(
                    "Auto-capture: idle (no active session)"
                )
                self.statusBar().showMessage(
                    "Start a session before resuming auto-capture.", 3000,
                )
            elif capture_token is None:
                self.auto_capture_engine.enabled = False
                self.monitor.set_auto_capture_status(
                    "Auto-capture: stopped (fresh RHEED frame required)"
                )
                self.statusBar().showMessage(
                    "Auto-capture remains paused until RHEED capture returns.",
                    3000,
                )
            else:
                self.auto_capture_engine.enabled = True
                self.monitor.set_auto_capture_status(
                    "Auto-capture: armed (warmup)"
                )
                self.statusBar().showMessage("Auto-capture resumed", 3000)

    @pyqtSlot(PyrometerState)
    def _on_pyrometer_state(self, state: PyrometerState):
        state = _stamp_gui_received(state)
        if self.growth_log.active:
            GrowthApp._trace_temporal(self,
                "state_received", "pyrometer",
                timing=snapshot_state("pyrometer", state).to_dict(),
            )
        self.monitor.update_pyrometer_state(state)
        self.pyrometer_window.on_pyrometer_state(state)

    @pyqtSlot(MistralState)
    def _on_mistral_state(self, state: MistralState):
        state = _stamp_gui_received(state)
        if self.growth_log.active:
            GrowthApp._trace_temporal(self,
                "state_received", "mistral",
                timing=snapshot_state("mistral", state).to_dict(),
            )
        self.monitor.update_mistral_state(state)

        if not self.growth_log.active or not state.connected or not state.valid:
            return

        # Detect operator setpoint changes (Set Voltage / Set Current button
        # presses on MISTRAL). OCR jitter can produce sub-tolerance noise on
        # an unchanged setpoint, so only fire when the change exceeds the
        # configured tolerance. Ignore None readings (transient OCR failures).
        elapsed = self.monitor.get_elapsed_seconds()
        # Set-change events happen at arbitrary moments — including during
        # the post-connect / failed-batch pyrometer window. `has_valid_reading`
        # ensures we don't leak 0.0/stale readings into set_change_events.csv
        # when the operator changes V-set or I-set at that moment.
        pyro_temp = (
            self.monitor._latest_pyro.temperature
            if self.monitor._latest_pyro and self.monitor._latest_pyro.has_valid_reading
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
        state = _stamp_gui_received(state)
        if self.growth_log.active:
            GrowthApp._trace_temporal(self,
                "state_received", "evap",
                timing=snapshot_state("evap", state).to_dict(),
            )
        self.monitor.update_evap_state(state)

    # --- Helpers -----------------------------------------------------------

    @staticmethod
    def _stop_worker(worker):
        """Compatibility wrapper for call sites stopping one worker."""
        return GrowthApp._stop_workers(worker)

    @staticmethod
    def _stop_workers(*workers, timeout_ms: int = 5000):
        """Signal every worker first, then wait within one shared deadline.

        Each worker owns a polling loop.  Stopping them serially makes the GUI
        wait for the sum of those poll intervals and previously omitted the
        classifier during window close.  This two-phase shutdown bounds the
        total wait while retaining a warning if a native thread does not exit.

        Returns a tuple of workers that are still running after the shared
        deadline.  Window shutdown must treat a non-empty result as a hard
        refusal to close so Qt objects are never destroyed under live threads.
        """
        active = [worker for worker in workers if worker is not None]
        for worker in active:
            worker.stop()

        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        for worker in active:
            if not worker.isRunning():
                continue
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if not worker.wait(remaining_ms):
                log.error(
                    "Worker did not stop within shared %d ms deadline: %r",
                    timeout_ms,
                    worker,
                )
        return tuple(worker for worker in active if worker.isRunning())

    # --- Shutdown ----------------------------------------------------------

    def closeEvent(self, event):
        # Enter a one-way fail-closed state before signalling workers.  Worker
        # shutdown may time out and cause this close event to be ignored, but
        # queued frames/events must never resume acquisition or persist files.
        self._shutdown_pending = True
        self.auto_capture_engine.enabled = False
        self.auto_capture_engine.reset()
        self._sensor_log_timer.stop()
        self._heartbeat_timer.stop()

        # These trend windows are independent Qt top-levels.  Leaving either
        # visible after the main window closes can keep QApplication.exec()
        # alive.  They stay closed if shutdown is pending after a worker
        # timeout, consistent with the one-way fail-closed state above.
        for auxiliary_window in (
            self.rheed_intensity_window,
            self.pyrometer_window,
        ):
            auxiliary_window.close()

        close_workers = [
            self.camera_worker,
            self.pyrometer_worker,
            self.mistral_worker,
            self.evap_worker,
            self.classifier_worker,
            self._movie_worker,
        ]
        shadow_worker = getattr(self, "weak_primary_shadow_worker", None)
        if shadow_worker is not None:
            close_workers.insert(-1, shadow_worker)
        survivors = self._stop_workers(*close_workers)
        # Once shutdown has been requested, never resume a partially stopped
        # acquisition stack.  Stop all image/log timers and close the session
        # before either accepting the window close or showing an explicit
        # fail-closed shutdown-pending state.
        if self.growth_log.active:
            metadata = self._session_metadata_with_camera()
            self.growth_log.save_session_metadata(metadata)
            self._invalidate_equalizer_calibration("GUI closed")
            self.growth_log.end_session()
            # Same auto-plot as _on_stop — also covers the "user closed
            # window mid-session" path, not just clean STOP.
            try:
                self.growth_log.generate_temperature_plot(metadata)
            except Exception as e:
                log.warning("Auto T vs t plot failed during close: %s", e)
        if survivors:
            names = ", ".join(type(worker).__name__ for worker in survivors)
            log.error(
                "GUI close refused because %d worker(s) remain running: %s",
                len(survivors),
                names,
            )
            self.monitor.set_state("armed")
            elapsed_timer = getattr(self.monitor, "_elapsed_timer", None)
            if elapsed_timer is not None:
                elapsed_timer.stop()
            arm_button = getattr(self.monitor, "arm_btn", None)
            if arm_button is not None:
                arm_button.setEnabled(False)
            self.monitor.start_btn.setEnabled(False)
            self.statusBar().showMessage(
                "Shutdown pending: logging and session actions are stopped; "
                "background workers are still exiting. Close again shortly.",
                7000,
            )
            event.ignore()
            return
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except (TypeError, RuntimeError):
                # Lightweight unit-test harnesses call closeEvent without
                # inheriting QObject and never install a Qt event filter.
                pass
        event.accept()
