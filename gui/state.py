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
    mode: str = ""  # "direct", "screengrab", or "dummy"
    exposure_us: Optional[float] = None  # confirmed direct-camera readback


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

    # Model identity — filename + mtime of best_model.pth, set once at
    # bridge-load time and repeated on every emission. Non-empty means
    # the bridge loaded successfully. Displayed in the UI's tooltip so
    # growers can tell at a glance which model checkpoint is running.
    model_version: str = ""


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
