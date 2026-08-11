"""
MISTRAL GUI driver — OCR readout of cell V/I setpoints and readback.

MistralGui is Scienta Omicron cell-control software. Its V/I display is a
canvas-painted region with no UIA-navigable controls, so we screengrab
and OCR the labeled region. Both setpoint ('Set') and measured ('Actual')
values are extracted per quantity.

The window must be visible on screen (not occluded) during reads, same
constraint as the kSA RHEED screengrab.
"""

from datetime import datetime, timezone
import re
import time
from typing import Optional

from drivers.ocr import capture_window, find_window, ocr_crop


# V/I block crop (x, y, w, h), relative to the MistralGui window top-left.
# Measured on Bulbasaur at 1793x1039 window size — see CALIBRATION_WINDOW_SIZE.
VI_BBOX = (970, 90, 585, 80)
CALIBRATION_WINDOW_SIZE = (1793, 1039)


def scale_bbox_to_window(
    calibration_bbox: tuple[int, int, int, int],
    calibration_size: tuple[int, int],
    current_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Scale a calibrated bbox proportionally to the current window size.

    Handles the common case where a grower resizes the window or where DPI
    scaling differs between machines. Assumes the OCR target stays in the
    same proportional location — a uniform stretch. If the window layout
    *reflows* (the V/I block moves to a different relative position), this
    won't help; that needs anchor-based detection (template-match the label,
    crop relative to it), tracked as a future improvement.
    """
    cur_w, cur_h = current_size
    cal_w, cal_h = calibration_size
    x, y, w, h = calibration_bbox
    sx = cur_w / cal_w
    sy = cur_h / cal_h
    return (round(x * sx), round(y * sy), round(w * sx), round(h * sy))

# PSM 6 = uniform block of text. Whitelist trims OCR search space to the
# actual characters present in the V/I display.
OCR_CONFIG = (
    "--psm 6 "
    "-c tessedit_char_whitelist=SetActualVoltageCurrent:0123456789.- VA"
)

V_RANGE = (-1.0, 500.0)
I_RANGE = (-1.0, 100.0)

_REGEX = {
    "v_set":    re.compile(r"Set\s*Voltage\s*:\s*([-\d.]+)", re.IGNORECASE),
    "i_set":    re.compile(r"Set\s*Current\s*:\s*([-\d.]+)", re.IGNORECASE),
    "v_actual": re.compile(r"Actual\s*Voltage\s*:\s*([-\d.]+)", re.IGNORECASE),
    "i_actual": re.compile(r"Actual\s*Current\s*:\s*([-\d.]+)", re.IGNORECASE),
}


def _in_range(val: float, lo: float, hi: float) -> Optional[float]:
    return val if lo <= val <= hi else None


class MistralGui:
    """Scrapes V/I setpoint + readback from the MistralGui cell window.

    read() always returns a dict with the four expected keys; individual
    values are float or None (OCR failure, parse failure, out-of-range,
    or window not found). Callers should treat None as 'unavailable'.
    """

    def __init__(
        self,
        window_title_substring: str = "MistralGui",
        bbox: Optional[tuple[int, int, int, int]] = None,
    ):
        """Construct a MISTRAL OCR reader.

        ``bbox`` overrides the auto-scaled crop. Pass ``None`` (default)
        to let the driver scale ``VI_BBOX`` proportionally to the captured
        window size at each read — the right choice unless you've measured
        a custom crop yourself.
        """
        self._substring = window_title_substring
        self._bbox = bbox  # explicit override; None → auto-scale per read
        self._hwnd = 0
        self._connected = False
        self._last_capture_at_utc: Optional[str] = None
        self._last_capture_monotonic_ns: Optional[int] = None

    def connect(self) -> None:
        self._hwnd = find_window(self._substring)
        if not self._hwnd:
            raise RuntimeError(
                f"MistralGui window not found (substring "
                f"'{self._substring}'). Is MISTRAL running?"
            )
        self._connected = True

    def read(self) -> dict[str, Optional[float]]:
        result: dict[str, Optional[float]] = {
            "v_set": None, "v_actual": None,
            "i_set": None, "i_actual": None,
        }
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

        # frame.shape is (H, W, C); ocr_crop expects (x, y, w, h) bbox.
        if self._bbox is not None:
            bbox = self._bbox
        else:
            h, w = frame.shape[:2]
            bbox = scale_bbox_to_window(VI_BBOX, CALIBRATION_WINDOW_SIZE, (w, h))
        text = ocr_crop(frame, bbox, OCR_CONFIG, label="mistral")
        if not text:
            return result

        for key, rx in _REGEX.items():
            m = rx.search(text)
            if not m:
                continue
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            lo, hi = V_RANGE if key.startswith("v_") else I_RANGE
            result[key] = _in_range(v, lo, hi)

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


class DummyMistralGui:
    """Synthetic driver for offline GUI development — slowly varying values."""

    def __init__(self, base_v: float = 10.0, base_i: float = 2.0):
        self._base_v = base_v
        self._base_i = base_i
        self._connected = False
        self._read_count = 0

    def connect(self) -> None:
        self._connected = True
        self._read_count = 0

    def read(self) -> dict[str, Optional[float]]:
        if not self._connected:
            return {"v_set": None, "v_actual": None,
                    "i_set": None, "i_actual": None}

        import math
        import random
        self._read_count += 1
        drift_v = 0.5 * math.sin(self._read_count * 0.05)
        drift_i = 0.1 * math.sin(self._read_count * 0.03)
        return {
            "v_set": self._base_v,
            "v_actual": self._base_v + drift_v + random.gauss(0, 0.02),
            "i_set": self._base_i,
            "i_actual": self._base_i + drift_i + random.gauss(0, 0.01),
        }

    def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def hwnd(self) -> int:
        return 0
