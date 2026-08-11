"""
Standalone OCR driver smoke test — exercises MistralGui + EvapControl
without touching the GUI. Use to validate OCR accuracy before wiring the
drivers into the Growth Monitor sensor loop.

Run on Bulbasaur with both target windows open AND visible (not occluded).

Usage:
    python scripts/test_ocr.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drivers.evap_control import (
    EvapControl,
    OCR_CONFIG as EVAP_CONFIG,
    PRESSURE_BBOX,
)
from drivers.mistral import (
    MistralGui,
    OCR_CONFIG as MISTRAL_CONFIG,
    VI_BBOX,
)
from drivers.ocr import capture_window, configure_tesseract, ocr_crop


def _banner(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def test_tesseract() -> None:
    _banner("Tesseract install")
    path = configure_tesseract()
    if path:
        print(f"  binary: {path}")
    else:
        print("  binary: NOT FOUND in standard paths — "
              "pytesseract will fall back to PATH (may fail)")


def test_mistral() -> None:
    _banner("MistralGui")
    drv = MistralGui()
    try:
        drv.connect()
    except Exception as e:
        print(f"  connect FAILED: {e}")
        return
    print(f"  hwnd: {drv.hwnd}")

    frame = capture_window(drv.hwnd)
    print(f"  window frame shape: {frame.shape}")

    raw = ocr_crop(frame, VI_BBOX, MISTRAL_CONFIG, label="mistral")
    print(f"  raw OCR ({len(raw)} chars):")
    for line in raw.splitlines():
        print(f"    | {line}")

    vals = drv.read()
    print("  parsed values:")
    for k, v in vals.items():
        tag = "OK  " if v is not None else "FAIL"
        print(f"    [{tag}] {k:<10} = {v}")

    drv.disconnect()


def test_evap() -> None:
    _banner("Evap Control")
    drv = EvapControl()
    try:
        drv.connect()
    except Exception as e:
        print(f"  connect FAILED: {e}")
        return
    print(f"  hwnd: {drv.hwnd}")

    frame = capture_window(drv.hwnd)
    print(f"  window frame shape: {frame.shape}")

    raw = ocr_crop(frame, PRESSURE_BBOX, EVAP_CONFIG, label="evap")
    print(f"  raw OCR ({len(raw)} chars):")
    for line in raw.splitlines():
        print(f"    | {line}")

    vals = drv.read()
    print("  parsed values:")
    for k, v in vals.items():
        tag = "OK  " if v is not None else "FAIL"
        print(f"    [{tag}] {k:<30} = {v}")

    drv.disconnect()


if __name__ == "__main__":
    test_tesseract()
    test_mistral()
    test_evap()
    print()
