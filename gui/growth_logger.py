"""
Session data logger for MBE growth monitoring.

Creates a session directory with periodic sensor CSV, commit log CSV,
saved RHEED frames, session metadata JSON, and growth log export.
"""

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np


# Auto-capture event states (used by event_state column in
# auto_capture_events.csv). Set to PENDING at fire time; transitions to one
# of the other three when the grower interacts with the AutoCaptureBanner or
# the keep-default countdown fires. AUTO_SKIPPED is set at log time when
# the quality gate rejected all 20 buffer frames — there's nothing to
# review, so the event has no banner and no resolution path. Marking it
# at log time gives the row a terminal state instead of leaving it as
# pending forever.
EVENT_STATE_PENDING = "pending"
EVENT_STATE_KEPT_EXPLICIT = "kept_explicit"
EVENT_STATE_KEPT_DEFAULT = "kept_default"
EVENT_STATE_DISCARDED = "discarded"
EVENT_STATE_AUTO_SKIPPED = "auto_skipped"


class GrowthLogger:
    """Logs sensor data and timestamped entries during a growth session."""

    SENSOR_FIELDS = [
        "timestamp", "elapsed_s",
        "pyrometer_temp_C", "pyrometer_temp_std_C", "pyrometer_temp_n",
        "mistral_v_set_V", "mistral_v_actual_V",
        "mistral_i_set_A", "mistral_i_actual_A",
        "chamber_pressure_mbar",
        # Elog-direct columns (populated when EvapControl mode = "elog";
        # blank in screengrab mode where the .elo binary isn't read).
        # See drivers.evap_control.ElogReader.DEFAULT_VAR_MAP for the
        # elog-variable-name → column-name mapping.
        "substrate_temp_pv_C", "substrate_temp_setpoint_C",
        "cell_HTEC2_pv_C",
        "cell_Y_pv_C", "cell_Sr_pv_C", "cell_Eu_pv_C", "cell_Er_pv_C",
        "plasma_dc_bias_V", "plasma_forward_W", "plasma_reflected_W",
        # ADS-mode cell temperatures (Ch-MBE, mode="ads"). Empty in O-MBE
        # sessions (MistralState.ads_cells is None). Cell1–7 map to the
        # Beckhoff PLC's PIDProgram.Cell{i}_pidTDK.ActualTemperature.
        "cell1_T_C", "cell2_T_C", "cell3_T_C", "cell4_T_C",
        "cell5_T_C", "cell6_T_C", "cell7_T_C",
    ]
    COMMIT_FIELDS = [
        "timestamp", "time_display", "elapsed_s", "sample_id", "grower",
        "pyrometer_temp_C", "voltage_V", "current_A",
        # Source path that populated voltage_V/current_A. Named for the
        # method, not the vendor, so it stays correct through hardware
        # swaps. Three values:
        #   mistral — via MISTRAL's screengrab OCR (current O-MBE path;
        #             TDK-Lambda hardware → MISTRAL software → GUI OCR)
        #   direct  — via a direct-read PSU worker (currently unwired;
        #             future TDK/OWON/etc. when we get a serial or
        #             network path to the PSU without going through
        #             MISTRAL)
        #   none    — no PSU state cached at LOG time (voltage_V and
        #             current_A will be blank)
        # Downstream analysis (Yuxin's #1) reads this to know whether
        # a voltage_V value came from screengrab OCR (~1 Hz, subject to
        # OCR noise) or a direct instrument read (higher precision).
        "psu_source",
        # Slider values at LOG time. When ``grower_corrected == False`` these
        # equal the classifier's smoothed_percent (sliders are read-only and
        # mirror the classifier). When ``grower_corrected == True`` these
        # represent the grower's belief, sum to 100 (Pattern A proportional
        # adjustment enforces it), and can differ from ``classifier_recon_*``.
        "recon_1x1", "recon_Twinned (2x1)", "recon_c(6x2)",
        "recon_rt13xrt13", "recon_HTR",
        # Classifier's smoothed_percent at LOG time — always populated when a
        # classifier state has been received this session, regardless of
        # correction toggle. Empty when the classifier is disabled or the
        # session logged before any frame was classified. Pairs with the
        # matching ``recon_*`` columns to form the active-comparisons training
        # signal for Yuxin's #1 deliverable (see yuxin_deliverables_jul06.md).
        "classifier_recon_1x1", "classifier_recon_Twinned (2x1)",
        "classifier_recon_c(6x2)", "classifier_recon_rt13xrt13",
        "classifier_recon_HTR",
        # "True" when the grower had ``✎ Correct`` active at LOG time (may
        # equal classifier values if grower agreed without dragging), "False"
        # when correction was off, "" when the classifier itself was disabled
        # for the session.
        "grower_corrected",
        # Classifier lifecycle state at LOG time. Four values:
        #   OK       — classifier ready + emitting valid classifications
        #   LOADING  — worker up but no successful classify() yet
        #   ERROR    — worker emitted an error (missing model, bad state)
        #   DISABLED — classifier never armed for this session (config off)
        # Disambiguates the classifier_recon_* columns downstream. A "0"
        # in classifier_recon_1x1 could mean "classifier confident it's
        # not 1x1" (OK), "classifier initialising" (LOADING), or "no
        # data" (blank when ERROR/DISABLED, but the status column makes
        # the reason explicit). Yuxin's #1 pipeline reads this to
        # partition rows appropriately.
        "classifier_status",
        "note", "frame_path",
        # Boolean-ish (str "True"/"False"/""). "True" when the saved
        # frame passed the frame_quality gate at LOG time; "False" when
        # the gate flagged it (too dark, uniform, saturated) but LOG
        # ENTRY saved it anyway because the grower explicitly requested
        # the entry. Empty when no frame was available at all (camera
        # not connected / no state emitted yet). Downstream can filter
        # or weight rows by this — training data curators may want
        # only frame_quality_pass=True rows, while UX audits may want
        # every entry.
        "frame_quality_pass",
        "capture_backend", "captured_at_utc", "capture_sequence",
        "frame_age_ms", "source_hwnd",
    ]
    AUTO_CAPTURE_FIELDS = [
        "timestamp", "elapsed_s", "event_idx",
        "change_score", "pyrometer_temp_C",
        "buffer_count", "buffer_dir",
        "event_state", "state_changed_at",
        "capture_backend", "captured_at_utc", "capture_sequence",
        "frame_age_ms", "source_hwnd",
    ]
    HEARTBEAT_FIELDS = [
        "timestamp", "elapsed_s", "heartbeat_idx",
        "pyrometer_temp_C", "frame_path",
        "capture_backend", "captured_at_utc", "capture_sequence",
        "frame_age_ms", "source_hwnd",
    ]
    SET_CHANGE_FIELDS = [
        "timestamp", "elapsed_s", "event_idx",
        "channel", "old_value", "new_value", "delta",
        "pyrometer_temp_C",
    ]
    # Grower-marked events (Jul 10 2026 group-meeting design shift). One
    # row per MARK EVENT click. Deliberately kept lean so the click stays
    # sub-second — no classifier snapshot, no slider dump. The scrubber
    # (workstream #3) reads this file to render manual markers on the
    # timeline; the wider (classifier + slider) capture stays on LOG
    # ENTRY where the grower opts in to the fuller decision. See
    # meeting_jul10_2026.md for the three-concern architecture
    # (capture / mark / label) that motivated the split from
    # AUTO_CAPTURE_FIELDS.
    MANUAL_EVENT_FIELDS = [
        "timestamp", "elapsed_s", "event_idx",
        "pyrometer_temp_C",
        "voltage_V", "current_A", "psu_source",
        "frame_path",
        # Optional — populated from the log-note input if the grower had
        # text queued at click time. Blank otherwise (single-tap case is
        # the primary path).
        "note",
        "capture_backend", "captured_at_utc", "capture_sequence",
        "frame_age_ms", "source_hwnd",
    ]
    # Live-Equalizer labels (Jul 10 2026 group-meeting workstream #4).
    # One row per Save click in the Live Equalizer window. The 5 recon_*
    # columns are the sliders' current mixture (Pattern A normalization
    # NOT applied here; grower chooses whether to hit Normalize before
    # Save). Coexists with commit_log.csv:recon_* (which are the Monitor-
    # tab sliders on a LOG ENTRY) — same class labels, different capture
    # surface + rate. Downstream analyses can join on timestamp.
    LIVE_LABEL_FIELDS = [
        "timestamp", "elapsed_s", "label_idx",
        "recon_1x1", "recon_tw", "recon_c6x2",
        "recon_rt13", "recon_HTR",
        "pyrometer_temp_C",
        "voltage_V", "current_A", "psu_source",
        "frame_path",
        "capture_backend", "captured_at_utc", "capture_sequence",
        "frame_age_ms", "source_hwnd",
        # Camera alignment calibration provenance (Jul 30 2026).
        "calibration_dx_px", "calibration_dy_px",
        "calibration_max_disagreement_px",
        "calibration_view_segment_id",
        "calibration_source_hwnd",
        "calibration_accepted",
    ]
    # Event labels written by the Events tab labeling form. The from/to
    # columns are reserved for the deferred reconstruction-transition
    # dropdowns — they exist now so future UI additions don't require a
    # CSV schema migration. Each row is per-event_idx (upsert-by-key).
    EVENT_LABEL_FIELDS = [
        "event_idx",
        "primary_reconstruction",
        "change_from",
        "change_to",
        "notes",
        "label_timestamp_iso",
        # Mixture-label columns written by the Equalizer (May 19 2026 sprint).
        # Each is a float in [0, 1]; ideally sum to ~1 after normalization.
        # primary_reconstruction stays populated as argmax for back-compat and
        # quick filtering by single-class users.
        "recon_1x1",
        "recon_tw",
        "recon_c6x2",
        "recon_rt13",
        "recon_HTR",
    ]

    @staticmethod
    def _capture_columns(capture_metadata: Optional[dict] = None) -> dict:
        """Normalize optional camera provenance for CSV writers."""
        metadata = capture_metadata or {}
        frame_age = metadata.get("frame_age_ms", "")
        if isinstance(frame_age, (float, int)):
            frame_age = f"{float(frame_age):.3f}"
        return {
            "capture_backend": metadata.get("capture_backend", ""),
            "captured_at_utc": metadata.get("captured_at_utc", ""),
            "capture_sequence": metadata.get("capture_sequence", ""),
            "frame_age_ms": frame_age,
            "source_hwnd": metadata.get("source_hwnd", ""),
        }

    def __init__(self, base_dir: str = "logs/growths"):
        self._base_dir = Path(base_dir)
        self._filename_prefix: str = "growth"
        self._session_dir: Optional[Path] = None
        self._sensor_file = None
        self._sensor_writer = None
        self._commit_file = None
        self._commit_writer = None
        self._auto_capture_file = None
        self._auto_capture_writer = None
        self._heartbeat_file = None
        self._heartbeat_writer = None
        self._set_change_file = None
        self._set_change_writer = None
        self._manual_event_file = None
        self._manual_event_writer = None
        self._live_label_file = None
        self._live_label_writer = None
        self._commit_counter = 0
        self._heartbeat_counter = 0
        self._set_change_counter = 0
        self._manual_event_counter = 0
        self._live_label_counter = 0
        self._sensor_row_counter = 0
        self._session_start: Optional[datetime] = None
        self._entries: list[dict] = []  # Accumulated entries for export

    @property
    def active(self) -> bool:
        """True while CSV files are open and accepting writes."""
        return self._sensor_writer is not None

    @property
    def session_dir(self) -> Optional[Path]:
        return self._session_dir

    def start_session(self, sample_id: str):
        """Create session directory and open CSV files."""
        # Clear any previous session data
        self._session_dir = None
        self._entries = []
        tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_id = sample_id.strip().replace(" ", "_") or "unnamed"
        prefix = self._filename_prefix or "growth"
        self._session_dir = self._base_dir / f"{prefix}_{safe_id}_{tag}"
        self._session_dir.mkdir(parents=True, exist_ok=True)
        (self._session_dir / "frames").mkdir(exist_ok=True)

        sensor_path = self._session_dir / "sensor_log.csv"
        self._sensor_file = open(sensor_path, "w", newline="")
        self._sensor_writer = csv.DictWriter(
            self._sensor_file, fieldnames=self.SENSOR_FIELDS,
        )
        self._sensor_writer.writeheader()

        commit_path = self._session_dir / "commit_log.csv"
        self._commit_file = open(commit_path, "w", newline="")
        self._commit_writer = csv.DictWriter(
            self._commit_file, fieldnames=self.COMMIT_FIELDS,
        )
        self._commit_writer.writeheader()

        auto_capture_path = self._session_dir / "auto_capture_events.csv"
        self._auto_capture_file = open(auto_capture_path, "w", newline="")
        self._auto_capture_writer = csv.DictWriter(
            self._auto_capture_file, fieldnames=self.AUTO_CAPTURE_FIELDS,
        )
        self._auto_capture_writer.writeheader()

        heartbeat_path = self._session_dir / "heartbeat_log.csv"
        self._heartbeat_file = open(heartbeat_path, "w", newline="")
        self._heartbeat_writer = csv.DictWriter(
            self._heartbeat_file, fieldnames=self.HEARTBEAT_FIELDS,
        )
        self._heartbeat_writer.writeheader()

        set_change_path = self._session_dir / "set_change_events.csv"
        self._set_change_file = open(set_change_path, "w", newline="")
        self._set_change_writer = csv.DictWriter(
            self._set_change_file, fieldnames=self.SET_CHANGE_FIELDS,
        )
        self._set_change_writer.writeheader()

        manual_event_path = self._session_dir / "manual_events.csv"
        self._manual_event_file = open(manual_event_path, "w", newline="")
        self._manual_event_writer = csv.DictWriter(
            self._manual_event_file, fieldnames=self.MANUAL_EVENT_FIELDS,
        )
        self._manual_event_writer.writeheader()
        # Flush the header immediately — downstream scrubber-timeline
        # tooling may open the file for read while the session is running,
        # and DictWriter buffers by default. Cost is negligible (one
        # header line, one flush, per session).
        self._manual_event_file.flush()

        live_label_path = self._session_dir / "live_labels.csv"
        self._live_label_file = open(live_label_path, "w", newline="")
        self._live_label_writer = csv.DictWriter(
            self._live_label_file, fieldnames=self.LIVE_LABEL_FIELDS,
        )
        self._live_label_writer.writeheader()
        self._live_label_file.flush()

        self._commit_counter = 0
        self._heartbeat_counter = 0
        self._set_change_counter = 0
        self._manual_event_counter = 0
        self._live_label_counter = 0
        self._sensor_row_counter = 0
        self._session_start = datetime.now()
        self._entries = []

    def log_sensors(
        self, pyro_temp, elapsed_s,
        v_set=None, v_actual=None, i_set=None, i_actual=None,
        chamber_pressure_mbar=None,
        pyro_temp_std=None, pyro_temp_n=None,
        # Elog-direct extensions (Jun 23 2026 — EvapControl mode="elog").
        # All optional; blank in the CSV when None. Order matches
        # EvapControlState field order so the call site can pass through
        # state attributes one-for-one.
        substrate_temp_pv_C=None, substrate_temp_setpoint_C=None,
        cell_HTEC2_pv_C=None,
        cell_Y_pv_C=None, cell_Sr_pv_C=None,
        cell_Eu_pv_C=None, cell_Er_pv_C=None,
        plasma_dc_bias_V=None, plasma_forward_W=None,
        plasma_reflected_W=None,
        # ADS-mode extensions (Jul 23 2026 — MistralWorker mode="ads").
        # Full read() dict from MistralAdsClient; None in O-MBE sessions.
        ads_cells=None,
    ):
        """Append a row to sensor_log.csv. All values may be None.

        ``pyro_temp_std`` and ``pyro_temp_n`` capture the per-poll
        statistical spread when the pyrometer worker takes multiple
        sub-readings per cycle. Empty strings if not provided.

        The ``substrate_*``, ``cell_*``, and ``plasma_*`` kwargs are
        populated only when EvapControl is in ``elog`` mode (reading the
        .elo binary log directly). In ``screengrab`` mode they default
        to None and the columns are blank.
        """
        if not self._sensor_writer:
            return

        def _f(val, places):
            return f"{val:.{places}f}" if val is not None else ""

        def _sci(val):
            return f"{val:.3e}" if val is not None else ""

        self._sensor_writer.writerow({
            "timestamp": datetime.now().isoformat(),
            "elapsed_s": f"{elapsed_s:.2f}",
            "pyrometer_temp_C":     _f(pyro_temp, 1),
            "pyrometer_temp_std_C": _f(pyro_temp_std, 2),
            "pyrometer_temp_n":     pyro_temp_n if pyro_temp_n is not None else "",
            "mistral_v_set_V":    _f(v_set, 3),
            "mistral_v_actual_V": _f(v_actual, 3),
            "mistral_i_set_A":    _f(i_set, 3),
            "mistral_i_actual_A": _f(i_actual, 3),
            "chamber_pressure_mbar": _sci(chamber_pressure_mbar),
            # Elog-direct fields. Temps to 1 dp (TemperaSure-style),
            # plasma DC bias to 1 dp, plasma powers to 1 dp.
            "substrate_temp_pv_C":       _f(substrate_temp_pv_C, 1),
            "substrate_temp_setpoint_C": _f(substrate_temp_setpoint_C, 1),
            "cell_HTEC2_pv_C": _f(cell_HTEC2_pv_C, 1),
            "cell_Y_pv_C":     _f(cell_Y_pv_C, 1),
            "cell_Sr_pv_C":    _f(cell_Sr_pv_C, 1),
            "cell_Eu_pv_C":    _f(cell_Eu_pv_C, 1),
            "cell_Er_pv_C":    _f(cell_Er_pv_C, 1),
            "plasma_dc_bias_V":   _f(plasma_dc_bias_V, 1),
            "plasma_forward_W":   _f(plasma_forward_W, 1),
            "plasma_reflected_W": _f(plasma_reflected_W, 1),
            # ADS cell temps — populated from ads_cells dict when mode="ads"
            **{
                f"cell{i}_T_C": _f(
                    ads_cells.get(f"cell{i}_T") if ads_cells else None, 1
                )
                for i in range(1, 8)
            },
        })
        self._sensor_file.flush()
        self._sensor_row_counter += 1

    def log_commit(self, entry: dict):
        """Append a row to commit_log.csv and accumulate for export."""
        if not self._commit_writer:
            return
        row = {field: entry.get(field, "") for field in self.COMMIT_FIELDS}
        self._commit_writer.writerow(row)
        self._commit_file.flush()
        self._entries.append(entry)

    def save_heartbeat_frame(
        self, frame: np.ndarray, timestamp: str = ""
    ) -> str:
        """Save a heartbeat anchor frame as ``heartbeat_NNN_HHMMSS.bmp``.

        BMP matches Justin's training-data format (Jul 9 2026 switch;
        was .png before). Same quality gate as ``save_frame``; failed
        frames return "" and are not counted toward the heartbeat counter.
        """
        if self._session_dir is None:
            return ""

        try:
            from drivers.frame_quality import check_frame_quality
            qa = check_frame_quality(frame)
            if not qa.passed:
                import sys
                print(
                    f"[GrowthLogger] heartbeat frame rejected: {qa.reason}",
                    file=sys.stderr,
                    flush=True,
                )
                return ""
        except ImportError:
            pass

        self._heartbeat_counter += 1
        ts = timestamp or datetime.now().strftime("%H%M%S")
        # .bmp per Jul 9 2026 switch — matches Justin's training data
        # format. PIL / cv2 infer format from the extension; no other
        # code change needed.
        fname = f"heartbeat_{self._heartbeat_counter:03d}_{ts}.bmp"
        path = self._session_dir / "frames" / fname

        try:
            from PIL import Image
            Image.fromarray(frame).save(str(path))
        except ImportError:
            try:
                import cv2
                cv2.imwrite(str(path), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
            except ImportError:
                return ""

        return str(path)

    def log_set_change_event(
        self,
        elapsed_s: float,
        channel: str,
        old_value: float,
        new_value: float,
        pyro_temp: Optional[float] = None,
    ):
        """Append a row to set_change_events.csv.

        Pairs operator setpoint changes (Set Voltage / Set Current button
        presses on the MISTRAL GUI) with timestamps, so the model can
        learn from when V/I are deliberately adjusted vs drifting.
        """
        if not self._set_change_writer:
            return
        self._set_change_counter += 1
        self._set_change_writer.writerow({
            "timestamp": datetime.now().isoformat(),
            "elapsed_s": f"{elapsed_s:.2f}",
            "event_idx": self._set_change_counter,
            "channel": channel,
            "old_value": f"{old_value:.4f}",
            "new_value": f"{new_value:.4f}",
            "delta": f"{new_value - old_value:+.4f}",
            "pyrometer_temp_C": (
                f"{pyro_temp:.1f}" if pyro_temp is not None else ""
            ),
        })
        self._set_change_file.flush()

    def record_manual_event(
        self,
        elapsed_s: float,
        pyro_temp: Optional[float] = None,
        voltage_V: Optional[float] = None,
        current_A: Optional[float] = None,
        psu_source: str = "none",
        frame: Optional[np.ndarray] = None,
        note: str = "",
        capture_metadata: Optional[dict] = None,
    ) -> int:
        """Append a grower-marked event to manual_events.csv.

        Sub-second reconstruction transitions surfaced at the Jul 10 2026
        group meeting as a hard UX requirement — labeling takes ~30 s but
        reconstructions can flip in ~1 s. Manual events decouple "grower
        saw something NOW" from "grower labels retrospectively later".
        Every click writes one row; the scrubber (workstream #3) reads
        this file to render manual markers on the timeline.

        When ``frame`` is provided, it's saved as
        ``manual_event_NNN_HHMMSS.bmp`` alongside ``entry_*`` and
        ``heartbeat_*`` under ``frames/`` — the scrubber can attach a
        per-mark preview. Frame save is best-effort: the CSV row is
        written even if PIL/cv2 aren't importable (grower's click is
        stronger evidence of intent than a working image encoder).

        Returns the 1-indexed event_idx so callers can update UI
        counters without re-reading the file. Returns 0 (silent no-op)
        if no session is active — mirrors log_commit's contract.
        """
        if not self._manual_event_writer:
            return 0

        self._manual_event_counter += 1
        idx = self._manual_event_counter

        # Save the frame first — a failed frame save must not block the
        # row from landing (grower's click is evidence-of-intent). Same
        # BMP + name-taxonomy pattern as save_frame + save_heartbeat_frame.
        frame_path = ""
        if frame is not None and self._session_dir is not None:
            ts = datetime.now().strftime("%H%M%S")
            fname = f"manual_event_{idx:03d}_{ts}.bmp"
            path = self._session_dir / "frames" / fname
            try:
                from PIL import Image
                Image.fromarray(frame).save(str(path))
                frame_path = str(path)
            except ImportError:
                try:
                    import cv2
                    cv2.imwrite(
                        str(path), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                    )
                    frame_path = str(path)
                except ImportError:
                    pass

        self._manual_event_writer.writerow({
            "timestamp": datetime.now().isoformat(),
            "elapsed_s": f"{elapsed_s:.2f}",
            "event_idx": idx,
            "pyrometer_temp_C": (
                f"{pyro_temp:.1f}" if pyro_temp is not None else ""
            ),
            "voltage_V": (
                f"{voltage_V:.3f}" if voltage_V is not None else ""
            ),
            "current_A": (
                f"{current_A:.3f}" if current_A is not None else ""
            ),
            "psu_source": psu_source,
            "frame_path": frame_path,
            "note": note,
            **self._capture_columns(capture_metadata),
        })
        self._manual_event_file.flush()
        return idx

    def record_live_label(
        self,
        elapsed_s: float,
        weights: dict,
        frame: Optional[np.ndarray] = None,
        pyro_temp: Optional[float] = None,
        voltage_V: Optional[float] = None,
        current_A: Optional[float] = None,
        psu_source: str = "none",
        capture_metadata: Optional[dict] = None,
        calibration=None,  # Optional[Calibration]
    ) -> int:
        """Append a Live Equalizer label to live_labels.csv.

        Ships workstream #4 from the Jul 10 2026 group meeting. The
        Live Equalizer window subscribes to the camera stream and lets
        the grower drive slider weights against the evolving frame;
        Save triggers this method with the current mixture + a snapshot
        of whatever the window was displaying at click time.

        ``weights`` keys follow the 5 equalizer-class labels: ``"1x1"``,
        ``"Tw(2x1)"``, ``"c(6x2)"``, ``"RT13"``, ``"HTR"``. Missing keys
        default to 0.0 (grower may have moved only a subset of sliders).
        Row schema is intentionally distinct from commit_log.csv so
        downstream analysis can tell "monitor-slider label" from "live-
        equalizer label" without a source column — they live in
        different files.

        Frame save follows the same best-effort pattern as
        record_manual_event: PIL first, cv2 fallback, blank frame_path
        if both fail. Returns 1-indexed label_idx or 0 for no-session.
        """
        if not self._live_label_writer:
            return 0

        self._live_label_counter += 1
        idx = self._live_label_counter

        # Save the frame as a BMP under frames/ — separate name-space
        # from entry_ / heartbeat_ / manual_event_ so the scrubber and
        # any post-hoc classifier training pipeline can distinguish
        # sources by filename glob alone.
        frame_path = ""
        if frame is not None and self._session_dir is not None:
            ts = datetime.now().strftime("%H%M%S")
            fname = f"live_label_{idx:03d}_{ts}.bmp"
            path = self._session_dir / "frames" / fname
            try:
                from PIL import Image
                Image.fromarray(frame).save(str(path))
                frame_path = str(path)
            except ImportError:
                try:
                    import cv2
                    cv2.imwrite(
                        str(path), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                    )
                    frame_path = str(path)
                except ImportError:
                    pass

        # Weight keys — mirror the EqualizerWindow's slider labels
        # (see scripts/equalizer_ui.py::CLASS_DIRS) and the events_labels
        # kwarg mapping in events_tab._make_equalizer_save_callback.
        # Any missing entry defaults to 0.0 so partial-slider-move Saves
        # still land a valid row.
        def _w(key: str) -> str:
            return f"{float(weights.get(key, 0.0)):.4f}"

        self._live_label_writer.writerow({
            "timestamp": datetime.now().isoformat(),
            "elapsed_s": f"{elapsed_s:.2f}",
            "label_idx": idx,
            "recon_1x1":   _w("1x1"),
            "recon_tw":    _w("Tw(2x1)"),
            "recon_c6x2":  _w("c(6x2)"),
            "recon_rt13":  _w("RT13"),
            "recon_HTR":   _w("HTR"),
            "pyrometer_temp_C": (
                f"{pyro_temp:.1f}" if pyro_temp is not None else ""
            ),
            "voltage_V": (
                f"{voltage_V:.3f}" if voltage_V is not None else ""
            ),
            "current_A": (
                f"{current_A:.3f}" if current_A is not None else ""
            ),
            "psu_source": psu_source,
            "frame_path": frame_path,
            # Camera alignment calibration provenance (Jul 30 2026).
            "calibration_dx_px": (
                f"{calibration.offset[0]:.1f}"
                if calibration is not None and calibration.grower_accepted
                else ""
            ),
            "calibration_dy_px": (
                f"{calibration.offset[1]:.1f}"
                if calibration is not None and calibration.grower_accepted
                else ""
            ),
            "calibration_max_disagreement_px": (
                f"{calibration.max_disagreement_px:.1f}"
                if calibration is not None and calibration.grower_accepted
                else ""
            ),
            "calibration_view_segment_id": (
                str(calibration.view_segment_id)
                if calibration is not None and calibration.grower_accepted
                else ""
            ),
            "calibration_source_hwnd": (
                str(calibration.source_hwnd)
                if calibration is not None and calibration.grower_accepted
                else ""
            ),
            "calibration_accepted": (
                "True"
                if calibration is not None and calibration.grower_accepted
                else ""
            ),
            **self._capture_columns(capture_metadata),
        })
        self._live_label_file.flush()
        return idx

    def log_heartbeat(
        self,
        elapsed_s: float,
        pyro_temp: Optional[float] = None,
        frame_path: str = "",
        capture_metadata: Optional[dict] = None,
    ):
        """Append a row to heartbeat_log.csv. Pairs with save_heartbeat_frame."""
        if not self._heartbeat_writer:
            return
        self._heartbeat_writer.writerow({
            "timestamp": datetime.now().isoformat(),
            "elapsed_s": f"{elapsed_s:.2f}",
            "heartbeat_idx": self._heartbeat_counter,
            "pyrometer_temp_C": (
                f"{pyro_temp:.1f}" if pyro_temp is not None else ""
            ),
            "frame_path": frame_path,
            **self._capture_columns(capture_metadata),
        })
        self._heartbeat_file.flush()

    def log_auto_capture_event(
        self,
        event_idx: int,
        score: float,
        elapsed_s: float,
        pyro_temp: Optional[float] = None,
        buffer_count: int = 0,
        buffer_dir: str = "",
        event_state: str = EVENT_STATE_PENDING,
        capture_metadata: Optional[dict] = None,
    ):
        """Append a row to auto_capture_events.csv for shadow-mode logging.

        Called by GrowthApp when AutoCaptureEngine emits frame_captured.
        Writes timestamp + change score + temp at trigger time, so the
        flagged moments can be cross-referenced against grower notes
        and pyrometer trajectory after the session. ``buffer_count`` and
        ``buffer_dir`` capture how many context frames were dumped and
        where, so post-hoc analysis can locate them.

        ``event_state`` defaults to ``"pending"`` at fire time and is updated
        to one of ``"kept_explicit"`` / ``"kept_default"`` / ``"discarded"``
        when the grower interacts with the AutoCaptureBanner (see
        ``update_auto_capture_state``).
        """
        if not self._auto_capture_writer:
            return
        timestamp = datetime.now().isoformat()
        # Non-pending initial states (auto_skipped, etc.) are terminal at
        # log time — set state_changed_at so the CSV stays internally
        # consistent with the rule that any non-pending row has a stamp.
        state_changed_at = (
            timestamp if event_state != EVENT_STATE_PENDING else ""
        )
        self._auto_capture_writer.writerow({
            "timestamp": timestamp,
            "elapsed_s": f"{elapsed_s:.2f}",
            "event_idx": event_idx,
            "change_score": f"{score:.4f}",
            "pyrometer_temp_C": (
                f"{pyro_temp:.1f}" if pyro_temp is not None else ""
            ),
            "buffer_count": buffer_count,
            "buffer_dir": buffer_dir,
            "event_state": event_state,
            "state_changed_at": state_changed_at,
            **self._capture_columns(capture_metadata),
        })
        self._auto_capture_file.flush()

    def update_auto_capture_state(
        self,
        event_idx: int,
        new_state: str,
    ) -> bool:
        """Update event_state and state_changed_at for an existing event row.

        Called when the grower interacts with the AutoCaptureBanner (Keep
        Now, Discard) or when the keep-default countdown fires. Rewrites
        auto_capture_events.csv in place — small file, infrequent updates,
        simple semantics. Returns True if the row was found and updated.

        Non-destructive design: discard updates the row state but does NOT
        remove the buffer directory. The grower can recover discarded
        events from the Events tab if they change their mind. See
        ``feedback_aiqm_grower_friction.md`` for the design rationale.
        """
        if self._session_dir is None:
            return False
        csv_path = self._session_dir / "auto_capture_events.csv"
        if not csv_path.exists():
            return False

        # Close the writer's file handle so we can rewrite in-place.
        if self._auto_capture_file and not self._auto_capture_file.closed:
            self._auto_capture_file.close()

        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        timestamp = datetime.now().isoformat()
        found = False
        for row in rows:
            if str(row.get("event_idx", "")) == str(event_idx):
                row["event_state"] = new_state
                row["state_changed_at"] = timestamp
                found = True
                break

        if found:
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.AUTO_CAPTURE_FIELDS)
                writer.writeheader()
                writer.writerows(rows)

        # Reopen for append so subsequent log_auto_capture_event calls work.
        # No header write — the file already has the header from start_session
        # (or the rewrite above when found=True).
        self._auto_capture_file = open(csv_path, "a", newline="")
        self._auto_capture_writer = csv.DictWriter(
            self._auto_capture_file, fieldnames=self.AUTO_CAPTURE_FIELDS,
        )
        return found

    def read_event_labels(self) -> dict[int, dict]:
        """Load all rows from events_labels.csv keyed by event_idx.

        Returns an empty dict if no session is active, the file doesn't
        exist yet (no labels applied), or the file can't be read. The
        Events tab uses this once per session attach to seed an in-memory
        cache; the cache is then kept in sync as the grower applies new
        labels through update_event_label.
        """
        if self._session_dir is None:
            return {}
        csv_path = self._session_dir / "events_labels.csv"
        if not csv_path.exists():
            return {}
        labels: dict[int, dict] = {}
        try:
            with open(csv_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        idx = int(row.get("event_idx", "") or 0)
                    except (TypeError, ValueError):
                        continue
                    labels[idx] = dict(row)
        except OSError:
            return {}
        return labels

    def update_event_label(
        self,
        event_idx: int,
        primary_reconstruction: Optional[str] = None,
        change_from: Optional[str] = None,
        change_to: Optional[str] = None,
        notes: Optional[str] = None,
        recon_1x1: Optional[float] = None,
        recon_tw: Optional[float] = None,
        recon_c6x2: Optional[float] = None,
        recon_rt13: Optional[float] = None,
        recon_HTR: Optional[float] = None,
    ) -> bool:
        """Atomically upsert a labeling row in events_labels.csv.

        Reads the existing file (if any), updates or appends the row for
        ``event_idx``, and rewrites the whole file. Each call is a
        per-change atomic write — the design accepts ~3-5 rewrites per
        labeled event (one per dropdown / notes commit) in exchange for
        no "save button" cognitive load on the grower.

        Only fields explicitly passed are updated; ``None`` means "leave
        existing value alone." This lets the EventsTab call with just
        ``primary_reconstruction=...`` when the dropdown changes without
        clobbering a previously-typed notes string. ``label_timestamp_iso``
        is always refreshed to record when the label was last touched.

        Kwarg source map (Jul 15 2026):
          - primary_reconstruction, change_from, change_to → three
            dropdowns on events_tab labeling form (Jul 15). change_from/to
            went from "reserved-but-unused" to UI-populated in the same
            commit that landed the labeling-form dropdowns.
          - notes → events_tab text field (debounced)
          - recon_1x1 / recon_tw / recon_c6x2 / recon_rt13 / recon_HTR →
            Equalizer popup save callback (see events_tab.
            _make_equalizer_save_callback)

        File is created on first write — sessions with no labeling
        activity won't leave behind an empty events_labels.csv.

        Returns True on successful write, False on no-session or I/O error.
        """
        if self._session_dir is None:
            return False
        csv_path = self._session_dir / "events_labels.csv"

        rows: list[dict] = []
        if csv_path.exists():
            try:
                with open(csv_path, "r", newline="") as f:
                    reader = csv.DictReader(f)
                    rows = [dict(r) for r in reader]
            except OSError:
                return False

        target = str(event_idx)
        existing = next(
            (r for r in rows if str(r.get("event_idx", "")) == target),
            None,
        )
        if existing is None:
            existing = {f: "" for f in self.EVENT_LABEL_FIELDS}
            existing["event_idx"] = target
            rows.append(existing)

        if primary_reconstruction is not None:
            existing["primary_reconstruction"] = primary_reconstruction
        if change_from is not None:
            existing["change_from"] = change_from
        if change_to is not None:
            existing["change_to"] = change_to
        if notes is not None:
            existing["notes"] = notes
        for col_name, val in (
            ("recon_1x1", recon_1x1),
            ("recon_tw", recon_tw),
            ("recon_c6x2", recon_c6x2),
            ("recon_rt13", recon_rt13),
            ("recon_HTR", recon_HTR),
        ):
            if val is not None:
                existing[col_name] = f"{val:.4f}"
        existing["label_timestamp_iso"] = datetime.now().isoformat()

        try:
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=self.EVENT_LABEL_FIELDS,
                )
                writer.writeheader()
                writer.writerows(rows)
        except OSError:
            return False
        return True

    def save_auto_capture_buffer(
        self,
        event_idx: int,
        frames: list[np.ndarray],
        capture_metadata: Optional[list[dict]] = None,
    ) -> tuple[int, str]:
        """Save the auto-capture context buffer for a flagged event.

        Each frame is written to a per-event subdirectory under frames/ so
        that sessions with many events stay browsable. The quality gate is
        applied per-frame; rejected frames are silently skipped (they don't
        carry information worth keeping).

        Returns ``(saved_count, relative_dir)``. ``relative_dir`` is empty
        if no session is active.
        """
        if self._session_dir is None or not frames:
            return 0, ""

        try:
            from drivers.frame_quality import check_frame_quality
        except ImportError:
            check_frame_quality = None

        event_dir = self._session_dir / "frames" / f"auto_event_{event_idx:03d}"
        event_dir.mkdir(parents=True, exist_ok=True)

        ts_tag = datetime.now().strftime("%H%M%S")
        saved = 0
        manifest_rows: list[dict] = []
        for pos, frame in enumerate(frames):
            if check_frame_quality is not None:
                qa = check_frame_quality(frame)
                if not qa.passed:
                    continue
            # .bmp per Jul 9 2026 switch — training-data-format-match.
            fname = f"buf_{pos:02d}_{ts_tag}.bmp"
            path = event_dir / fname
            try:
                from PIL import Image
                Image.fromarray(frame).save(str(path))
                saved += 1
            except ImportError:
                try:
                    import cv2
                    cv2.imwrite(str(path), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                    saved += 1
                except ImportError:
                    break
            metadata = (
                capture_metadata[pos]
                if capture_metadata is not None
                and pos < len(capture_metadata)
                else {}
            )
            manifest_rows.append({
                "frame_path": fname,
                **self._capture_columns(metadata),
            })

        if manifest_rows:
            manifest_path = event_dir / "capture_manifest.csv"
            with open(manifest_path, "w", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=[
                        "frame_path",
                        "capture_backend",
                        "captured_at_utc",
                        "capture_sequence",
                        "frame_age_ms",
                        "source_hwnd",
                    ],
                )
                writer.writeheader()
                writer.writerows(manifest_rows)

        rel_dir = str(event_dir.relative_to(self._session_dir))
        return saved, rel_dir

    def save_frame(
        self, frame: np.ndarray, timestamp: str = "",
    ) -> tuple[str, Optional[bool]]:
        """Save frame as PNG to session frames/ subdir. Returns (path, quality_pass).

        Manual LOG ENTRY intent takes precedence over the frame quality
        gate — if the grower explicitly clicked LOG ENTRY, the frame is
        saved regardless of quality. The gate result is captured as
        metadata (quality_pass) so downstream training-data consumers
        can filter to high-quality frames while the grower's original
        intent is preserved in the archive.

        This is a deliberate contract difference from save_heartbeat_frame
        and save_auto_capture_buffer (both periodic + implicit — quality
        gate rejects there because no grower is asking for that frame).

        Returns:
            (path, quality_pass) tuple.
              path: str filesystem path if the frame was saved successfully,
                    "" if no session dir or PIL/cv2 both missing.
              quality_pass: True if the quality gate passed, False if it
                    flagged the frame (still saved), None if the gate
                    couldn't be evaluated (frame_quality module missing).
        """
        if self._session_dir is None:
            return "", None

        # Capture the quality gate result but don't gate saving on it.
        # LOG ENTRY = grower's explicit intent (see Jul 8 2026 decision
        # in bulbasaur_lab_day_jul07.md follow-up 2).
        quality_pass: Optional[bool] = None
        try:
            from drivers.frame_quality import check_frame_quality
            qa = check_frame_quality(frame)
            quality_pass = bool(qa.passed)
            if not qa.passed:
                import sys
                print(
                    f"[GrowthLogger] LOG ENTRY frame quality flagged "
                    f"(saved anyway per grower intent): {qa.reason}",
                    file=sys.stderr,
                    flush=True,
                )
        except ImportError:
            pass  # Quality gate optional; leave quality_pass=None.

        self._commit_counter += 1
        ts = timestamp or datetime.now().strftime("%H%M%S")
        # .bmp per Jul 9 2026 switch — matches Justin's training data
        # format. Files ~10x larger than PNG (~300 KB vs ~30 KB for a
        # 656x492x3 uint8 frame), still trivial vs the T9 SSD's capacity.
        fname = f"entry_{self._commit_counter:03d}_{ts}.bmp"
        path = self._session_dir / "frames" / fname

        try:
            from PIL import Image
            img = Image.fromarray(frame)
            img.save(str(path))
        except ImportError:
            try:
                import cv2
                cv2.imwrite(str(path), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
            except ImportError:
                return "", quality_pass

        return str(path), quality_pass

    def save_session_metadata(self, metadata: dict):
        """Save session metadata to a JSON file."""
        if self._session_dir is None:
            return
        now = datetime.now()
        meta = {
            **metadata,
            "session_start": self._session_start.isoformat() if self._session_start else None,
            "session_end": now.isoformat(),
            "session_duration_s": (
                round((now - self._session_start).total_seconds(), 1)
                if self._session_start else None
            ),
            "sensor_row_count": self._sensor_row_counter,
            "commit_entry_count": len(self._entries),
            "heartbeat_frame_count": self._heartbeat_counter,
            "manual_event_count": self._manual_event_counter,
            "live_label_count": self._live_label_counter,
        }
        meta_path = self._session_dir / "session_metadata.json"
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

    def export_growth_log(self, metadata: dict) -> str:
        """Export session as OMBE growth log. Returns file path or empty string."""
        if self._session_dir is None or not self._entries:
            return ""

        try:
            return self._export_xlsx(metadata)
        except ImportError:
            return self._export_csv_log(metadata)

    def _export_xlsx(self, metadata: dict) -> str:
        """Export as xlsx matching OMBE growth log template."""
        from openpyxl import Workbook
        from openpyxl.styles import Font

        wb = Workbook()
        ws = wb.active
        ws.title = "Growth Log"

        bold = Font(bold=True)
        bold_large = Font(bold=True, size=12)

        # --- Header section ---
        ws['B2'] = 'Date:'
        ws['B2'].font = bold
        ws['D2'] = metadata.get('date', '')
        ws['G2'] = 'Substrate:'
        ws['G2'].font = bold

        ws['B3'] = 'Grower:'
        ws['B3'].font = bold
        ws['D3'] = metadata.get('grower', '')
        ws['G3'] = 'Sample ID:'
        ws['G3'].font = bold
        ws['I3'] = metadata.get('sample_id', '')

        ws['B4'] = 'Base pressure (mbar):'
        ws['B4'].font = bold
        ws['G4'] = 'Growth pressure (mbar):'
        ws['G4'].font = bold

        # --- Source parameter headers (OMBE elements) ---
        elements = ['Substrate', 'Sr', 'Ti', 'Y', 'Er', 'Eu', 'O', 'Al', 'Ta']
        for i, elem in enumerate(elements):
            cell = ws.cell(row=5, column=3 + i, value=elem)
            cell.font = bold
        ws.cell(row=5, column=12, value='Flux ratio').font = bold

        ws['B6'] = 'Temperature (\u2103)'
        ws['B6'].font = bold
        ws['B7'] = 'Flux (mbar)'
        ws['B7'].font = bold
        ws['B8'] = 'Time (min)'
        ws['B8'].font = bold

        # --- Growth notes section ---
        ws['B9'] = 'Growth Notes'
        ws['B9'].font = bold_large

        ws['B10'] = 'Time (hh:mm)'
        ws['B10'].font = bold
        ws['C10'] = 'Temp (\u2103)'
        ws['C10'].font = bold
        ws['D10'] = 'Operation'
        ws['D10'].font = bold

        # Pre/post annealing headers
        ws['C11'] = 'Pre-growth annealing T (\u2103)'
        ws['F11'] = 'Pre-growth annealing time (min)'
        ws['H11'] = 'Post-growth annealing T (\u2103)'
        ws['K11'] = 'Post-growth annealing time (min)'

        # --- Operations log entries ---
        for i, entry in enumerate(self._entries):
            row = 13 + i
            ws.cell(row=row, column=2, value=entry.get('time_display', ''))
            temp = entry.get('pyrometer_temp_C', '')
            if temp:
                try:
                    ws.cell(row=row, column=3, value=float(temp))
                except ValueError:
                    ws.cell(row=row, column=3, value=temp)
            ws.cell(row=row, column=4, value=entry.get('note', ''))

        # --- Column widths ---
        ws.column_dimensions['B'].width = 18
        ws.column_dimensions['C'].width = 14
        ws.column_dimensions['D'].width = 60
        ws.column_dimensions['G'].width = 22
        ws.column_dimensions['I'].width = 20

        export_path = self._session_dir / "growth_log.xlsx"
        wb.save(str(export_path))
        return str(export_path)

    def _export_csv_log(self, metadata: dict) -> str:
        """Fallback CSV export if openpyxl is not available."""
        if self._session_dir is None:
            return ""
        export_path = self._session_dir / "growth_log_export.csv"
        with open(export_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["OMBE Growth Log"])
            writer.writerow(["Date", metadata.get('date', '')])
            writer.writerow(["Grower", metadata.get('grower', '')])
            writer.writerow(["Sample ID", metadata.get('sample_id', '')])
            writer.writerow([])
            writer.writerow([
                "Time", "Temp (\u2103)", "Voltage (V)", "Current (A)", "Note",
            ])
            for entry in self._entries:
                writer.writerow([
                    entry.get("time_display", ""),
                    entry.get("pyrometer_temp_C", ""),
                    entry.get("voltage_V", ""),
                    entry.get("current_A", ""),
                    entry.get("note", ""),
                ])
        return str(export_path)

    def end_session(self):
        """Close CSV files. Preserves session_dir and entries for post-stop export."""
        for f in (
            self._sensor_file, self._commit_file,
            self._auto_capture_file, self._heartbeat_file,
            self._set_change_file, self._manual_event_file,
            self._live_label_file,
        ):
            if f and not f.closed:
                f.close()
        self._sensor_file = None
        self._sensor_writer = None
        self._commit_file = None
        self._commit_writer = None
        self._auto_capture_file = None
        self._auto_capture_writer = None
        self._heartbeat_file = None
        self._heartbeat_writer = None
        self._set_change_file = None
        self._set_change_writer = None
        self._manual_event_file = None
        self._manual_event_writer = None
        self._live_label_file = None
        self._live_label_writer = None
        # NOTE: _session_dir and _entries intentionally preserved
        # so Export Growth Log works after STOP.

    def generate_temperature_plot(self, metadata: Optional[dict] = None) -> Optional[Path]:
        """Generate a Temperature-vs-Time PNG from this session's sensor_log.csv.

        Convenience auto-plot called from GrowthApp at session end (per Jun 23
        2026 — Frankie's f_version pattern, ported to the canonical product).
        Reads ``pyrometer_temp_C`` + ``elapsed_s`` from the sensor log this
        session just wrote and saves ``temperature_profile.png`` to the same
        directory.

        Works post-``end_session()`` because ``_session_dir`` is preserved
        through close. Returns the plot path on success; returns None and
        logs a warning if matplotlib isn't installed or the sensor log has
        no temperature data (e.g. pyrometer disconnected the whole session).

        For richer post-hoc plotting (custom titles, output format, dpi),
        use ``python scripts/plot_temperature.py <session_dir>``.
        """
        if self._session_dir is None:
            return None

        sensor_path = self._session_dir / "sensor_log.csv"
        if not sensor_path.exists():
            return None

        # Matplotlib is a soft dependency — present on Mac dev env, may not
        # be on Bulbasaur. Lazy-import + graceful failure.
        try:
            import matplotlib
            matplotlib.use("Agg")  # No GUI needed; just write a file
            import matplotlib.pyplot as plt
        except ImportError:
            import logging
            logging.getLogger(__name__).warning(
                "matplotlib not installed; skipping auto T-vs-t plot. "
                "Install with: pip install matplotlib"
            )
            return None

        # Parse sensor_log.csv — same shape as scripts/plot_temperature.py
        elapsed: list[float] = []
        temps: list[float] = []
        with open(sensor_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    t = float(row["elapsed_s"])
                except (KeyError, ValueError):
                    continue
                temp_str = row.get("pyrometer_temp_C", "").strip()
                if not temp_str:
                    continue
                try:
                    temp = float(temp_str)
                except ValueError:
                    continue
                elapsed.append(t)
                temps.append(temp)

        if not temps:
            # Pyrometer was offline the whole session — nothing to plot.
            return None

        # Title: prefer metadata if provided (richer); fall back to dir name.
        if metadata:
            title_parts = []
            if metadata.get("sample_id"):
                title_parts.append(str(metadata["sample_id"]))
            if metadata.get("grower"):
                title_parts.append(f"by {metadata['grower']}")
            if metadata.get("date"):
                title_parts.append(metadata["date"])
            title = " — ".join(title_parts) if title_parts else self._session_dir.name
        else:
            title = self._session_dir.name

        elapsed_min = [t / 60.0 for t in elapsed]

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(elapsed_min, temps, color="#0d9488", linewidth=1.2)
        ax.set_xlabel("Elapsed Time (min)", fontsize=12)
        ax.set_ylabel("Temperature (°C)", fontsize=12)
        ax.set_title(title, fontsize=13)
        ax.grid(True, alpha=0.3)

        # Annotate min/max so the curve is glanceable
        t_min, t_max = min(temps), max(temps)
        ax.axhline(y=t_max, color="#dc2626", linestyle="--", alpha=0.5, linewidth=0.8)
        ax.axhline(y=t_min, color="#2563eb", linestyle="--", alpha=0.5, linewidth=0.8)
        ax.text(
            elapsed_min[-1], t_max, f"  {t_max:.0f}°C",
            va="bottom", color="#dc2626", fontsize=9,
        )
        ax.text(
            elapsed_min[-1], t_min, f"  {t_min:.0f}°C",
            va="top", color="#2563eb", fontsize=9,
        )

        fig.tight_layout()
        out_path = self._session_dir / "temperature_profile.png"
        fig.savefig(str(out_path), dpi=150)
        plt.close(fig)  # Free figure memory; we're done with it.
        return out_path
