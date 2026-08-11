"""
State dataclasses for the hardware control GUI.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class PowerSupplyState:
    """Current state of the power supply."""
    voltage_setpoint: float = 0.0
    current_setpoint: float = 0.0
    voltage_measured: float = 0.0
    current_measured: float = 0.0
    power_measured: float = 0.0
    output_enabled: bool = False
    ovp_limit: float = 0.0
    ocp_limit: float = 0.0
    connected: bool = False
    error: str = ""


@dataclass
class TemperatureState:
    """Current state of the thermocouple reader."""
    temperature: float = 0.0
    cold_junction: float = 0.0
    unit: str = "C"
    channel: str = "temperature"
    connected: bool = False
    error: str = ""
    device_info: str = ""
    product_id: str = ""
    serial_number: str = ""


@dataclass
class CameraState:
    """Current state of the RHEED camera."""
    frame: Optional[object] = None  # numpy ndarray (H, W, 3) uint8; typed as object for signal compat
    frame_number: int = 0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    intensity: float = 0.0  # ROI mean intensity for oscillation tracking
    connected: bool = False
    error: str = ""
    mode: str = ""  # "vimba", "screengrab", "screengrab_mss", or "dummy"
    capture_backend: str = ""
    captured_at_utc: str = ""
    capture_sequence: int = 0
    frame_age_ms: float = 0.0
    source_hwnd: int = 0
    captured_monotonic_ns: int = 0  # internal age calculation, not serialized
    capture_geometry_id: str = ""  # ROI/chrome-crop identity
    acquire_started_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    received_at_utc: Optional[str] = None
    received_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    sample_sequence: int = 0
    read_duration_ms: Optional[float] = None
    worker_emitted_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    gui_received_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    valid: bool = False


@dataclass
class PyrometerState:
    """Current state of the pyrometer (separate from thermocouple).

    ``temperature`` is the mean of ``temperature_n`` rapid sub-readings
    taken within a single poll cycle. ``temperature_std`` is the sample
    standard deviation (0.0 when n=1). Polybot-inspired: a per-poll
    mean/std gives downstream consumers a cheap statistical-consistency
    check without requiring a second pass over the sensor log.

    ``None`` semantics: "no valid reading has been taken since connect
    (or since the last error batch)." The worker emits ``connected=True``
    with ``temperature=None`` right after ``connect()`` succeeds and
    before the first poll, and again whenever a poll batch fails
    entirely. Consumers MUST NOT persist ``0.0`` in place of ``None`` —
    that leaks a spurious real-looking reading into sensor_log.csv and
    downstream analyses. Use the ``has_valid_reading`` predicate below.
    """
    temperature: Optional[float] = None
    temperature_std: Optional[float] = None
    temperature_n: int = 0
    emissivity: Optional[float] = None  # 0.0-1.0, from TemperaSure or Modbus
    unit: str = "C"
    connected: bool = False
    error: str = ""
    device_info: str = ""
    mode: str = ""  # "modbus", "screengrab", or "dummy"
    # Read-only sample provenance. UTC fields are ISO-8601 strings; the
    # monotonic timestamp is process-local and is used only to calculate age.
    source_at_utc: Optional[str] = None
    received_at_utc: Optional[str] = None
    sample_sequence: int = 0
    read_duration_ms: Optional[float] = None
    acquire_started_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    received_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    worker_emitted_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    gui_received_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    valid: bool = False
    sample_span_ms: Optional[float] = None
    subread_monotonic_ns: tuple[int, ...] = field(
        default_factory=tuple, repr=False, compare=False,
    )

    @property
    def has_valid_reading(self) -> bool:
        """True iff a real finite numeric reading is available right now.

        Consumers should gate CSV writes / label renders / thresholds on
        this — NOT on ``connected`` alone, which is True for the interval
        between ``connect()`` and the first successful read. ``math.isfinite``
        rejects ``nan`` and ``inf`` — a driver with a dual-endian ambiguity
        can produce non-finite floats, and treating them as valid would
        leak ``nan`` into every downstream analysis that assumes float
        arithmetic.
        """
        return (
            self.connected
            and self.valid
            and self.temperature is not None
            and math.isfinite(self.temperature)
        )


@dataclass
class MistralState:
    """Current state of the MistralGui cell V/I readout (OCR-scraped)."""
    v_set: Optional[float] = None
    v_actual: Optional[float] = None
    i_set: Optional[float] = None
    i_actual: Optional[float] = None
    connected: bool = False
    error: str = ""
    mode: str = ""  # "screengrab", "jsonrpc", "ads", or "dummy"
    # Populated by MistralWorker when mode="ads" (Beckhoff TwinCAT ADS).
    # ADS is the primary MISTRAL path for both chambers as of Jul 27 2026:
    # Ch-MBE via netId 10.0.42.112.1.1 (7 cells), Bulbasaur/O-MBE via
    # netId 10.0.42.111.1.1 (6 cells). Full read() output from
    # MistralAdsClient — superset of the 4 standard keys. Per-cell keys:
    # cell{i}_{T, T_set, active_setpoint, V, I, prog_V, prog_A, power,
    # state, shutter_open, shutter_closed}. System keys: ebvm_*,
    # ion_gauge_*_P, pirani_*_P, turbo*_rpm, service_mode.
    # None in all other modes (screengrab / jsonrpc / dummy).
    ads_cells: Optional[dict] = None
    # ``source_at_utc`` is reserved for a future hardware/source timestamp;
    # current MISTRAL modes expose only the Python receive timestamp.
    source_at_utc: Optional[str] = None
    received_at_utc: Optional[str] = None
    sample_sequence: int = 0
    read_duration_ms: Optional[float] = None
    acquire_started_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    received_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    worker_emitted_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    gui_received_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    valid: bool = False
    capture_completed_at_utc: Optional[str] = None
    capture_completed_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    processing_duration_ms: Optional[float] = None
    # Latest poll-attempt timing is separate from the latest successful
    # sample above. On OCR parse failure the success sequence/timestamps stay
    # unchanged while these fields still describe the failed screenshot/OCR.
    attempt_capture_completed_at_utc: Optional[str] = None
    attempt_capture_completed_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    attempt_completed_at_utc: Optional[str] = None
    attempt_completed_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    attempt_duration_ms: Optional[float] = None


@dataclass
class EvapControlState:
    """Current state from EvapControl / ElogReader.

    Field provenance:
    - ``screengrab`` mode (OCR): only ``chamber_pressure_mbar`` is populated.
    - ``elog`` mode (.elo binary log): all fields populated where the elog
      schema contains the underlying variable. Missing variables stay None
      (different MBE systems have different cells).

    Field-to-elog-variable mapping is defined in
    ``drivers.evap_control.ElogReader.DEFAULT_VAR_MAP``. To track different
    cells on a different system, pass a custom ``var_map`` when constructing
    the reader and extend this dataclass to match.
    """
    # Always populated (both modes)
    chamber_pressure_mbar: Optional[float] = None
    # Elog-mode only: substrate manipulator (the substrate temperature itself)
    substrate_temp_pv_C: Optional[float] = None
    substrate_temp_setpoint_C: Optional[float] = None
    # Elog-mode only: effusion cell process values (Bulbasaur OMBE config)
    cell_HTEC2_pv_C: Optional[float] = None
    cell_Y_pv_C: Optional[float] = None       # Yttrium
    cell_Sr_pv_C: Optional[float] = None      # Strontium
    cell_Eu_pv_C: Optional[float] = None      # Europium
    cell_Er_pv_C: Optional[float] = None      # Erbium
    # Elog-mode only: plasma source state (when in use)
    plasma_dc_bias_V: Optional[float] = None
    plasma_forward_W: Optional[float] = None
    plasma_reflected_W: Optional[float] = None
    connected: bool = False
    error: str = ""
    mode: str = ""  # "screengrab", "elog", or "dummy"
    # Elog mode populates ``source_at_utc`` from the LabVIEW record. OCR and
    # dummy modes have no source clock and leave it None.
    source_at_utc: Optional[str] = None
    received_at_utc: Optional[str] = None
    sample_sequence: int = 0
    read_duration_ms: Optional[float] = None
    acquire_started_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    received_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    worker_emitted_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    gui_received_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    valid: bool = False
    capture_completed_at_utc: Optional[str] = None
    capture_completed_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    processing_duration_ms: Optional[float] = None
    # Per-attempt OCR/Elog provenance; kept separate from the last successful
    # sample so a failed poll cannot cross-wire two sample generations.
    attempt_capture_completed_at_utc: Optional[str] = None
    attempt_capture_completed_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    attempt_completed_at_utc: Optional[str] = None
    attempt_completed_monotonic_ns: Optional[int] = field(
        default=None, repr=False, compare=False,
    )
    attempt_duration_ms: Optional[float] = None


@dataclass
class ClassifierState:
    """Current state of the RHEED classifier worker.

    Lifecycle:
        - Initial: ``loading=True, ready=False, error=""``
        - After model loads: ``loading=False, ready=True``
        - Failure: ``loading=False, ready=False, error="..."``

    Score field provenance — per-cycle transformation chain:
        1. Bridge returns raw win-rates in ``raw_scores`` (0-1 per class,
           sum unconstrained — Bradley-Terry pairwise, not softmax).
        2. Equalizer recipe (clip negatives → divide by sum → uniform
           fallback) produces ``normalized_percent`` (0-100, sums to 100).
        3. EMA over successive cycles produces ``smoothed_percent`` —
           this is what the UI sliders display. Frozen while ``is_ood``.

    Confidence signals:
        - ``is_bad`` — classifier's own low-quality flag (bad-reference
          bank matched better than any real-class reference). Independent
          of ``is_ood``.
        - ``is_ood`` — derived by the worker from ``quality`` below the
          worker's OOD threshold. Signals the UI to grey out the sliders
          and stop advancing the EMA.

    Sentinel: ``last_frame_number == -1`` means no frame has been
    classified yet this session.
    """
    # Lifecycle
    loading: bool = True
    ready: bool = False
    error: str = ""

    # Latest inference — see class docstring for the transformation chain
    last_frame_number: int = -1
    raw_scores: dict[str, float] = field(default_factory=dict)
    normalized_percent: dict[str, int] = field(default_factory=dict)
    smoothed_percent: dict[str, int] = field(default_factory=dict)
    raw_sum: float = 0.0  # for the "Sum: X.XX" transparency label

    # Confidence / OOD
    quality: float = 0.0
    is_bad: bool = False
    bad_confidence: float = 0.0
    is_ood: bool = False
    # Latches True on the first non-OOD classification and stays True
    # for the rest of the session. Lets the UI distinguish "OOD, showing
    # last confident data" from "OOD, no confident data has ever
    # arrived" (in which case ``smoothed_percent`` is just the ready-time
    # uniform-20 placeholder and shouldn't be shown as if it were a
    # real prediction).
    has_confident_data: bool = False

    # Perf
    inference_ms: float = 0.0
    source_capture_sequence: int = 0
    source_received_monotonic_ns: int = field(
        default=0, repr=False, compare=False,
    )
    inference_started_monotonic_ns: int = field(
        default=0, repr=False, compare=False,
    )
    inference_completed_monotonic_ns: int = field(
        default=0, repr=False, compare=False,
    )
    worker_emitted_monotonic_ns: int = field(
        default=0, repr=False, compare=False,
    )
    gui_received_monotonic_ns: int = field(
        default=0, repr=False, compare=False,
    )

    # Model identity — filename + mtime of best_model.pth, set once at
    # bridge-load time and repeated on every emission. Non-empty means
    # the bridge loaded successfully. Displayed in the UI's tooltip so
    # growers can tell at a glance which model checkpoint is running.
    model_version: str = ""

    # Acquisition-side RHEED view state.  These fields are deliberately
    # separate from ``is_bad`` / ``is_ood`` above: alignment and history
    # readiness are operator-/pipeline-known facts, while Bad/OOD are model
    # predictions. ``None`` means that alignment has not yet been confirmed
    # for the current session.
    view_segment_id: Optional[int] = None
    # Monotonic token for any reset of pixel-coordinate-dependent state.
    # Unlike ``view_segment_id``, this also changes on a camera-continuity
    # reset within the same stable gun alignment.
    visual_history_generation: int = 0
    gun_aligned: Optional[bool] = None
    history_frame_count: int = 0
    # Zero means that the loaded runtime bridge is single-frame-only.  The
    # offline temporal experiments still use 32-frame causal histories.
    history_required: int = 0
    history_ready: bool = False
    prediction_actionable: bool = False
    model_input_mode: str = "unknown"


@dataclass
class WeakPrimaryShadowState:
    """Read-only four-output diagnostic; never a control/advice signal."""

    loading: bool = True
    ready: bool = False
    error: str = ""
    last_frame_number: int = -1
    source_capture_sequence: int = 0
    source_received_monotonic_ns: int = field(default=0, repr=False, compare=False)
    inference_started_monotonic_ns: int = field(default=0, repr=False, compare=False)
    inference_completed_monotonic_ns: int = field(default=0, repr=False, compare=False)
    worker_emitted_monotonic_ns: int = field(default=0, repr=False, compare=False)
    gui_received_monotonic_ns: int = field(default=0, repr=False, compare=False)
    inference_ms: float = 0.0
    conditional_probabilities: dict[str, float] = field(default_factory=dict)
    predicted_class: str = ""
    predicted_applicability: float = 0.0
    normalized_entropy: float = 0.0
    checkpoint_disagreement: float = 0.0
    checkpoint_count: int = 0
    ensemble_id: str = ""
    bundle_family: str = "brightness_robust_weak_primary_v1"
    brightness_policy: str = "all_extreme"
    output_classes: tuple[str, ...] = field(default_factory=tuple)
    lambda_pair: float = 0.1
    execution_scope: str = "weak_shadow_only"
    actionable: bool = False
    abstain_reason: str = (
        "weak_shadow_only_no_registered_real_frame_actionability_policy"
    )


@dataclass
class RheedQcState:
    """Acquisition-side validity of the current RHEED view.

    ``view_segment_id`` changes only after a gun realignment is completed.
    Images are never deleted: frames captured while ``gun_aligned`` is False
    remain useful realignment/QC data, but must not be mixed into the visual
    history of the next stable segment. Temperature and process histories are
    intentionally outside this state and continue across segment boundaries.

    ``history_ready=False`` is not a Bad label. It only means that a temporal
    adviser has not yet accumulated enough same-segment visual observations.
    """

    session_active: bool = False
    view_segment_id: Optional[int] = None
    visual_history_generation: int = 0
    realignment_id: int = 0
    gun_aligned: Optional[bool] = None
    realignment_active: bool = False
    history_frame_count: int = 0
    history_required: int = 0
    history_ready: bool = False

    @property
    def prediction_actionable(self) -> bool:
        return (
            self.session_active
            and self.gun_aligned is True
            and not self.realignment_active
            and self.history_required > 0
            and self.history_ready
        )


@dataclass
class ActionLogEntry:
    """A single entry in the action log."""
    timestamp: datetime = field(default_factory=datetime.now)
    category: str = ""
    action: str = ""
    details: str = ""
    psu_voltage: Optional[float] = None
    psu_current: Optional[float] = None
    psu_power: Optional[float] = None
    psu_output: Optional[bool] = None
    temperature: Optional[float] = None
