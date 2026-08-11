"""
Evaporation Control driver — OCR readout of MBE chamber pressure.

'Evaporation control (Logged in: master)' is a LabVIEW-based chamber
monitor. Canvas-drawn, no UIA controls — we screengrab + OCR the
MBE > Pressure cell (chamber ion gauge, in mbar).

The window must be visible on screen during reads.

Also exposes ``ElogReader`` (added Jun 23 2026) — a direct-read
alternative that reads MBE.Pressure straight out of EvapControl's
own ``.elo`` binary log file via ``drivers.elog``. No window
positioning, no OCR, no tesseract dependency. See ``drivers/elog.py``
for the format. The screengrab path is kept as a fallback for
sessions where the .elo file is unreachable.
"""

import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from drivers.ocr import capture_window, find_window, ocr_crop

log = logging.getLogger(__name__)


# Crop for the 'Pressure' label + value in the MBE column, (x, y, w, h).
# Measured on Bulbasaur at 1497x567 window size. x start is deliberately
# set past the LabVIEW panel's 3D-inset bezel, which occupies x=9..16
# (two stacked 4-px gradients — outer and inner bevel). The first green
# pixel of the '2' digit is at x=19-20, so x=18 leaves ~2 px of clean
# black interior before the text. Earlier shifts (x=8, x=15) still
# captured bezel pixels that tesseract read as a phantom leading '1'.
PRESSURE_BBOX = (18, 130, 100, 50)
CALIBRATION_WINDOW_SIZE = (1497, 567)


def scale_bbox_to_window(
    calibration_bbox: tuple[int, int, int, int],
    calibration_size: tuple[int, int],
    current_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Scale a calibrated bbox proportionally to the current window size.

    Same shape-uniform-stretch assumption as the matching helper in
    drivers.mistral. If the LabVIEW panel reflows on resize (the Pressure
    cell moves to a different relative position), this won't help — that
    needs anchor-based detection (template-match the label, crop relative
    to it), tracked as a future improvement.
    """
    cur_w, cur_h = current_size
    cal_w, cal_h = calibration_size
    x, y, w, h = calibration_bbox
    sx = cur_w / cal_w
    sy = cur_h / cal_h
    return (round(x * sx), round(y * sy), round(w * sx), round(h * sy))

OCR_CONFIG = (
    "--psm 6 "
    "-c tessedit_char_whitelist=Pressure0123456789.eE-+ mbar"
)

PRESSURE_REGEX = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*[eE]\s*([+-]?\d+)")

# UHV chamber plausibility range (mbar)
PRESSURE_RANGE_MBAR = (1e-12, 1e-2)


class EvapControl:
    """Scrapes MBE chamber pressure from the Evap Control LabVIEW window."""

    def __init__(
        self,
        window_title_substring: str = "Evaporation control",
        bbox: Optional[tuple[int, int, int, int]] = None,
    ):
        """``bbox`` overrides the auto-scaled crop. Default ``None`` =
        scale ``PRESSURE_BBOX`` proportionally to the captured window
        size at each read."""
        self._substring = window_title_substring
        self._bbox = bbox
        self._hwnd = 0
        self._connected = False
        self._last_capture_at_utc: Optional[str] = None
        self._last_capture_monotonic_ns: Optional[int] = None

    def connect(self) -> None:
        self._hwnd = find_window(self._substring)
        if not self._hwnd:
            raise RuntimeError(
                f"Evap Control window not found (substring "
                f"'{self._substring}'). Is the window open?"
            )
        self._connected = True

    def read(self) -> dict[str, Optional[float]]:
        result: dict[str, Optional[float]] = {"chamber_pressure_mbar": None}
        self._last_capture_at_utc = None
        self._last_capture_monotonic_ns = None
        if not self._connected or not self._hwnd:
            return result

        try:
            frame = capture_window(self._hwnd)
        except Exception:
            return result
        self._last_capture_monotonic_ns = time.perf_counter_ns()
        self._last_capture_at_utc = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds",
        )

        if self._bbox is not None:
            bbox = self._bbox
        else:
            h, w = frame.shape[:2]
            bbox = scale_bbox_to_window(
                PRESSURE_BBOX, CALIBRATION_WINDOW_SIZE, (w, h),
            )
        text = ocr_crop(frame, bbox, OCR_CONFIG, label="evap")
        if not text:
            return result

        m = PRESSURE_REGEX.search(text)
        if not m:
            return result

        try:
            val = float(m.group(1)) * (10 ** int(m.group(2)))
        except (ValueError, OverflowError):
            return result

        lo, hi = PRESSURE_RANGE_MBAR
        if not (lo <= val <= hi):
            return result

        result["chamber_pressure_mbar"] = val
        return result

    def disconnect(self) -> None:
        self._hwnd = 0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_capture_at_utc(self) -> Optional[str]:
        """Python time immediately after the OCR source screenshot."""
        return self._last_capture_at_utc

    @property
    def last_capture_monotonic_ns(self) -> Optional[int]:
        return self._last_capture_monotonic_ns

    @property
    def hwnd(self) -> int:
        return self._hwnd


class ElogReader:
    """Direct-read growth state via EvapControl's own ``.elo`` log.

    Drop-in for ``EvapControl``: same ``connect/read/disconnect`` shape so
    ``EvapControlWorker`` swaps it in by changing the ``mode`` parameter.
    Returns the same dict key (``chamber_pressure_mbar``) the screengrab
    path always returned, PLUS richer growth-state values (substrate
    temperature, cell temperatures, plasma) that the elog schema exposes
    but the OCR path can't reach.

    Variable selection is configurable via ``var_map``: an elog-variable
    name → output-dict-key mapping. Variables absent from the elog file's
    schema are silently skipped (the output dict key stays None), so the
    same reader works across MBE systems with different cell
    configurations. Default map covers the Bulbasaur OMBE configuration
    per ``elog_direct_read_may15.md``.

    Path discovery uses ``elog.find_current_log()`` which targets
    ``C:\\_Omicron_Software\\EvapControl\\evap_control_1.2.0.51\\evap_control_1.2.0.51\\log``.
    Override via ``log_dir`` if the install path differs.

    Midnight rotation: ``find_current_log()`` resolves today's filename
    each call, so a session that spans midnight picks up the new file on
    the next ``read()``.
    """

    # Known EvapControl log directory paths, in preference order.
    # Ships as a list because different lab machines install EvapControl at
    # different paths — the Bulbasaur O-MBE has a Scienta-managed install
    # with a doubly-nested version dir under _Omicron_Software; the Ch-MBE
    # (Omicron) has a flat install at C:\evap_control_<version>\.
    # Resolution: check AIQM_EVAP_LOG_DIR env var first, then the first
    # entry from this list that actually exists. Extend as we onboard new
    # machines rather than forcing each site to set an env var.
    KNOWN_LOG_DIRS = [
        # Bulbasaur (O-MBE), evap_control 1.2.0.51 nested install
        r"C:\_Omicron_Software\EvapControl\evap_control_1.2.0.51"
        r"\evap_control_1.2.0.51\log",
        # Ch-MBE, evap_control 1.2.0.48 flat install
        r"C:\evap_control_1.2.0.48\log",
    ]

    # Kept for backward compatibility with any external callers that
    # reference DEFAULT_LOG_DIR. New code should call _resolve_log_dir().
    DEFAULT_LOG_DIR = KNOWN_LOG_DIRS[0]

    @classmethod
    def _resolve_log_dir(cls) -> str:
        """Find the EvapControl log dir on this machine.

        Precedence:
          1. ``AIQM_EVAP_LOG_DIR`` env var — per-machine escape hatch
          2. First existing dir from ``KNOWN_LOG_DIRS``
          3. First entry of ``KNOWN_LOG_DIRS`` (fallback so the eventual
             error message points at *some* concrete path we tried)
        """
        env = os.environ.get("AIQM_EVAP_LOG_DIR", "").strip()
        if env:
            return env
        for candidate in cls.KNOWN_LOG_DIRS:
            if Path(candidate).exists():
                return candidate
        return cls.KNOWN_LOG_DIRS[0]

    # elog variable name → EvapControlState field name (also the dict
    # key returned by ``read()``). Edit this to expose more growth state
    # to downstream consumers; also extend ``EvapControlState`` in
    # ``gui/state.py`` and the ``log_sensors`` schema in
    # ``gui/growth_logger.py`` so the new fields land in sensor_log.csv.
    DEFAULT_VAR_MAP: dict[str, str] = {
        # Chamber pressure — also available via screengrab; always populated
        "MBE.Pressure": "chamber_pressure_mbar",
        # Substrate manipulator — the substrate temperature growers track
        "MBE-Mani.PV": "substrate_temp_pv_C",
        "MBE-Mani.setpoint": "substrate_temp_setpoint_C",
        # Effusion cells (Bulbasaur OMBE)
        "HTEC 2.PV": "cell_HTEC2_pv_C",
        "HTEC Y.PV": "cell_Y_pv_C",
        "LTEC 1 Sr.PV": "cell_Sr_pv_C",
        "LTEC 2 Eu.PV": "cell_Eu_pv_C",
        "MTEC Er.PV": "cell_Er_pv_C",
        # Plasma source (when in use)
        "Plasma.DCBias": "plasma_dc_bias_V",
        "Plasma.forward": "plasma_forward_W",
        "Plasma.reflected": "plasma_reflected_W",
    }

    def __init__(
        self,
        log_dir: Optional[str] = None,
        var_map: Optional[dict[str, str]] = None,
    ):
        self._log_dir = log_dir or self._resolve_log_dir()
        self._var_map = var_map or self.DEFAULT_VAR_MAP
        self._connected = False
        self._last_log_path: Optional[Path] = None
        # Cached schema intersection: which of our wanted vars actually
        # exist in this elog's schema. Populated at first successful read,
        # invalidated when the log file rotates (re-checked).
        self._schema_present: Optional[list[str]] = None
        self._schema_log_path: Optional[Path] = None
        self._last_source_at_utc: Optional[str] = None

    def connect(self) -> None:
        # Resolve once at connect to fail fast — re-resolves at each read
        # to handle midnight rotation. Failure here means EvapControl
        # isn't running or the install path differs from default.
        from drivers.elog import find_current_log

        path = find_current_log(self._log_dir)
        if path is None:
            raise RuntimeError(
                f"No live .elo file found in {self._log_dir}. Is EvapControl "
                f"running? Expected a file named log_<today>_<HHMMSS>.elo"
            )
        self._last_log_path = path
        self._connected = True
        # WARNING, not INFO, for the mid-day-start case. The GUI never calls
        # logging.basicConfig, so it runs on logging's lastResort handler —
        # stderr at WARNING and above. INFO goes nowhere. An operator who
        # has just been handed pressure readings deserves to see which
        # session file they came from whenever it is not the ordinary
        # midnight rotation, because that is exactly when "is this file
        # live?" is a reasonable thing to wonder.
        if path.name.endswith("_000000.elo"):
            log.info("ElogReader connected: %s", path)
        else:
            log.warning(
                "ElogReader connected to mid-day session file: %s "
                "(EvapControl was started during the day rather than "
                "rotating at midnight)", path.name,
            )

    def read(self) -> dict[str, Optional[float]]:
        # All output keys start None; populated only if the variable
        # exists in the elog schema and the read succeeds.
        result: dict[str, Optional[float]] = {
            out_key: None for out_key in self._var_map.values()
        }
        # This property describes only the current read attempt. Clearing it
        # prevents a failed tail read from being paired with an older record.
        self._last_source_at_utc = None
        if not self._connected:
            return result

        from drivers.elog import find_current_log, latest_record

        # Re-resolve every read so we cleanly handle midnight log rotation
        # mid-session without needing a reconnect.
        path = find_current_log(self._log_dir)
        if path is None:
            return result

        # Cache which of our wanted vars are actually in this elog's schema.
        # Re-check on log rotation (midnight) because a system reconfig
        # could change the variable set.
        if self._schema_present is None or self._schema_log_path != path:
            from drivers.elog import parse_schema
            try:
                with open(path, "rb") as f:
                    names, _fmts, _ = parse_schema(f)
            except OSError as exc:
                log.debug("ElogReader schema read failed: %s", exc)
                return result
            wanted = list(self._var_map.keys())
            self._schema_present = [v for v in wanted if v in names]
            self._schema_log_path = path
            missing = set(wanted) - set(self._schema_present)
            if missing:
                # WARNING, not INFO: every name in `missing` is a column
                # that will be silently blank in sensor_log.csv for the
                # whole session. At INFO this never reached the operator's
                # console, so an empty Direct-read tab looked like a dead
                # subsystem rather than a name mismatch we could fix.
                log.warning(
                    "ElogReader: %d/%d wanted vars absent from elog schema "
                    "(these columns will be blank): %s",
                    len(missing), len(wanted), sorted(missing),
                )
        self._last_log_path = path

        # Batch read all present vars in one open+schema+tail.
        try:
            source_ts, var_values = latest_record(path, self._schema_present)
        except (KeyError, OSError, ValueError) as exc:
            log.debug("ElogReader read failed: %s", exc)
            return result
        if source_ts.tzinfo is None:
            source_ts = source_ts.replace(tzinfo=timezone.utc)
        self._last_source_at_utc = source_ts.astimezone(timezone.utc).isoformat()

        for elog_name, (val, _fmt) in var_values.items():
            out_key = self._var_map[elog_name]
            # NaN → None (leave the key at its initialized None value).
            # EvapControl writes literal float NaN into plasma variables
            # (Plasma.DCBias / Plasma.forward / Plasma.reflected) whenever
            # the plasma source is off. Passing NaN downstream renders as
            # the string "nan" in sensor_log.csv, which looks like a bug.
            # Treating NaN as "unavailable" gives the CSV an empty cell —
            # the same shape it would have if the variable were absent
            # from the elog schema entirely.
            if isinstance(val, float) and math.isnan(val):
                continue
            # Pressure: plausibility-filter to UHV range.
            if out_key == "chamber_pressure_mbar":
                lo, hi = PRESSURE_RANGE_MBAR
                if not (lo <= val <= hi):
                    continue
            result[out_key] = val

        return result

    def disconnect(self) -> None:
        self._last_log_path = None
        self._schema_present = None
        self._schema_log_path = None
        self._last_source_at_utc = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_source_at_utc(self) -> Optional[str]:
        """UTC timestamp embedded in the last successfully read Elog record."""
        return self._last_source_at_utc

    @property
    def hwnd(self) -> int:
        # Symmetry with EvapControl/DummyEvapControl; no window for ElogReader.
        return 0


class DummyEvapControl:
    """Synthetic driver for offline GUI development — slowly varying pressure."""

    def __init__(self, base_mbar: float = 1e-9):
        self._base = base_mbar
        self._connected = False
        self._read_count = 0

    def connect(self) -> None:
        self._connected = True
        self._read_count = 0

    def read(self) -> dict[str, Optional[float]]:
        if not self._connected:
            return {"chamber_pressure_mbar": None}

        import math
        import random
        self._read_count += 1
        drift = 0.1 * math.sin(self._read_count * 0.02)
        return {"chamber_pressure_mbar": self._base * (1.0 + drift + random.gauss(0, 0.01))}

    def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def hwnd(self) -> int:
        return 0
