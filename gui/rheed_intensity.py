"""Qt-free RHEED region-of-interest intensity primitives.

The production WGC path captures kSA's rendered 8-bit RGB image, so the
metric below is explicitly a *display-luminance* sum rather than raw camera
counts.  Definitions are frozen to one capture geometry.  Automatic 1x1
matching is one-shot: it proposes three fixed boxes and never tracks them
between frames, because moving the measurement region would contaminate the
intensity trend itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Iterable, Mapping
import uuid

import numpy as np
from PIL import Image

from gui.equalizer_alignment import (
    MIN_DETECTION_SNR,
    PROCESS_H,
    PROCESS_W,
    detect_live_landmarks,
)


INTENSITY_METRIC = "bt601_display_luminance_sum_v1"
ROI_SCHEMA_VERSION = 1
ROI_MODES = ("manual_rect", "auto_1x1_three_spot")


@dataclass(frozen=True)
class NormalizedRoi:
    """One half-open rectangle in normalized frame coordinates."""

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        values = tuple(float(value) for value in (
            self.left, self.top, self.right, self.bottom,
        ))
        if not all(math.isfinite(value) for value in values):
            raise ValueError("ROI coordinates must be finite")
        left, top, right, bottom = values
        if not (0.0 <= left < right <= 1.0):
            raise ValueError("ROI horizontal bounds must satisfy 0 <= left < right <= 1")
        if not (0.0 <= top < bottom <= 1.0):
            raise ValueError("ROI vertical bounds must satisfy 0 <= top < bottom <= 1")
        object.__setattr__(self, "left", left)
        object.__setattr__(self, "top", top)
        object.__setattr__(self, "right", right)
        object.__setattr__(self, "bottom", bottom)

    def pixel_bounds(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Return clamped half-open ``x0, y0, x1, y1`` pixel bounds."""
        if width <= 0 or height <= 0:
            raise ValueError("Frame dimensions must be positive")
        x0 = max(0, min(width - 1, int(math.floor(self.left * width))))
        y0 = max(0, min(height - 1, int(math.floor(self.top * height))))
        x1 = max(x0 + 1, min(width, int(math.ceil(self.right * width))))
        y1 = max(y0 + 1, min(height, int(math.ceil(self.bottom * height))))
        return x0, y0, x1, y1

    def to_dict(self) -> dict[str, float]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }


@dataclass(frozen=True)
class RheedRoiDefinition:
    """Immutable ROI selection tied to one RHEED capture geometry."""

    roi_definition_id: str
    mode: str
    regions: tuple[NormalizedRoi, ...]
    capture_backend: str
    source_hwnd: int
    capture_geometry_id: str
    frame_width: int
    frame_height: int
    definition_capture_sequence: int
    definition_captured_at_utc: str
    landmark_points_processed: tuple[tuple[float, float], ...] = ()
    detector_confidence: float | None = None
    detector_peak_snr: float | None = None
    detector_method: str = ""

    def __post_init__(self) -> None:
        if not str(self.roi_definition_id).strip():
            raise ValueError("ROI definition ID must be non-empty")
        if self.mode not in ROI_MODES:
            raise ValueError(f"Unsupported ROI mode: {self.mode}")
        regions = tuple(self.regions)
        expected = 1 if self.mode == "manual_rect" else 3
        if len(regions) != expected:
            raise ValueError(f"{self.mode} requires {expected} region(s)")
        if int(self.frame_width) <= 0 or int(self.frame_height) <= 0:
            raise ValueError("ROI definition requires positive frame dimensions")
        points = tuple(
            (float(point[0]), float(point[1]))
            for point in self.landmark_points_processed
        )
        if points and len(points) != 3:
            raise ValueError("Automatic ROI provenance requires three landmarks")
        object.__setattr__(self, "regions", regions)
        object.__setattr__(self, "landmark_points_processed", points)

    @classmethod
    def create(
        cls,
        *,
        mode: str,
        regions: Iterable[NormalizedRoi],
        capture_backend: str,
        source_hwnd: int,
        capture_geometry_id: str,
        frame_width: int,
        frame_height: int,
        definition_capture_sequence: int,
        definition_captured_at_utc: str,
        landmark_points_processed: Iterable[tuple[float, float]] = (),
        detector_confidence: float | None = None,
        detector_peak_snr: float | None = None,
        detector_method: str = "",
    ) -> "RheedRoiDefinition":
        return cls(
            roi_definition_id=f"rheed-roi-{uuid.uuid4().hex}",
            mode=mode,
            regions=tuple(regions),
            capture_backend=str(capture_backend or ""),
            source_hwnd=int(source_hwnd or 0),
            capture_geometry_id=str(capture_geometry_id or ""),
            frame_width=int(frame_width),
            frame_height=int(frame_height),
            definition_capture_sequence=int(definition_capture_sequence or 0),
            definition_captured_at_utc=str(definition_captured_at_utc or ""),
            landmark_points_processed=tuple(landmark_points_processed),
            detector_confidence=(
                None if detector_confidence is None else float(detector_confidence)
            ),
            detector_peak_snr=(
                None if detector_peak_snr is None else float(detector_peak_snr)
            ),
            detector_method=str(detector_method or ""),
        )

    def compatibility_error(self, state: object, frame: np.ndarray) -> str:
        """Return an explicit reason if ``state`` cannot use this ROI."""
        arr = np.asarray(frame)
        if arr.ndim not in (2, 3):
            return f"unsupported frame shape {arr.shape}"
        height, width = arr.shape[:2]
        checks = (
            (str(getattr(state, "capture_backend", "") or ""), self.capture_backend,
             "capture backend changed"),
            (int(getattr(state, "source_hwnd", 0) or 0), self.source_hwnd,
             "source HWND changed"),
            (str(getattr(state, "capture_geometry_id", "") or ""),
             self.capture_geometry_id, "capture geometry changed"),
            (int(width), self.frame_width, "frame width changed"),
            (int(height), self.frame_height, "frame height changed"),
        )
        for actual, expected, reason in checks:
            if actual != expected:
                return reason
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ROI_SCHEMA_VERSION,
            "roi_definition_id": self.roi_definition_id,
            "mode": self.mode,
            "regions_normalized": [region.to_dict() for region in self.regions],
            "capture_backend": self.capture_backend,
            "source_hwnd": self.source_hwnd,
            "capture_geometry_id": self.capture_geometry_id,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "definition_capture_sequence": self.definition_capture_sequence,
            "definition_captured_at_utc": self.definition_captured_at_utc,
            "landmark_points_processed": [list(point) for point in self.landmark_points_processed],
            "detector_confidence": self.detector_confidence,
            "detector_peak_snr": self.detector_peak_snr,
            "detector_method": self.detector_method,
            "intensity_metric": INTENSITY_METRIC,
        }


@dataclass(frozen=True)
class RheedIntensitySample:
    """One ROI measurement bound to one unique captured frame."""

    roi: RheedRoiDefinition
    captured_at_utc: str
    capture_sequence: int
    captured_monotonic_ns: int
    measured_monotonic_ns: int
    frame_age_ms: float
    intensity_sum: float
    intensity_mean: float
    pixel_count: int
    relative_change_pct: float
    measurement_duration_ms: float

    @property
    def capture_backend(self) -> str:
        return self.roi.capture_backend

    @property
    def source_hwnd(self) -> int:
        return self.roi.source_hwnd

    @property
    def capture_geometry_id(self) -> str:
        return self.roi.capture_geometry_id


def frame_luminance_plane(frame: np.ndarray) -> np.ndarray:
    """Return BT.601 display luminance as float64 without clipping sums."""
    arr = np.asarray(frame)
    if arr.ndim == 2:
        return arr.astype(np.float64, copy=False)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError(f"Expected grayscale or RGB frame, got {arr.shape}")
    rgb = arr[:, :, :3].astype(np.float64, copy=False)
    return 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]


def preprocess_for_landmarks(frame: np.ndarray) -> np.ndarray:
    """Match the Equalizer's 128x96 grayscale preprocessing exactly."""
    arr = np.asarray(frame)
    if arr.ndim == 2:
        image = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    elif arr.ndim == 3 and arr.shape[2] >= 3:
        image = Image.fromarray(np.ascontiguousarray(arr[:, :, :3]).astype(np.uint8)).convert("L")
    else:
        raise ValueError(f"Expected grayscale or RGB frame, got {arr.shape}")
    return np.asarray(
        image.resize((PROCESS_W, PROCESS_H), Image.Resampling.LANCZOS),
        dtype=np.float32,
    )


def manual_roi_definition(
    normalized_rect: tuple[float, float, float, float],
    state: object,
    frame: np.ndarray,
) -> RheedRoiDefinition:
    """Build a source-bound definition from a grower-drawn rectangle."""
    arr = np.asarray(frame)
    height, width = arr.shape[:2]
    region = NormalizedRoi(*normalized_rect)
    return RheedRoiDefinition.create(
        mode="manual_rect",
        regions=(region,),
        capture_backend=str(getattr(state, "capture_backend", "") or ""),
        source_hwnd=int(getattr(state, "source_hwnd", 0) or 0),
        capture_geometry_id=str(getattr(state, "capture_geometry_id", "") or ""),
        frame_width=width,
        frame_height=height,
        definition_capture_sequence=int(
            getattr(state, "capture_sequence", 0)
            or getattr(state, "sample_sequence", 0)
            or getattr(state, "frame_number", 0)
        ),
        definition_captured_at_utc=str(getattr(state, "captured_at_utc", "") or ""),
    )


def auto_three_spot_roi_definition(
    state: object,
    frame: np.ndarray,
    *,
    box_size_processed_px: int = 7,
) -> RheedRoiDefinition:
    """Detect three bright spots once and return three fixed square boxes.

    ``detect_live_landmarks`` detects geometry only; it does not prove the
    surface is 1x1.  Low-confidence successful candidates are preserved for
    explicit operator review rather than silently accepted or discarded.
    """
    size = int(box_size_processed_px)
    if size < 3 or size > 31 or size % 2 == 0:
        raise ValueError("Spot box size must be an odd integer from 3 to 31")
    processed = preprocess_for_landmarks(frame)
    detection = detect_live_landmarks(processed)
    if not detection.success:
        raise ValueError(f"Three-spot detection failed: {detection.reason}")

    half = size / 2.0
    regions: list[NormalizedRoi] = []
    for x, y in detection.points:
        left = max(0.0, (float(x) + 0.5 - half) / PROCESS_W)
        right = min(1.0, (float(x) + 0.5 + half) / PROCESS_W)
        top = max(0.0, (float(y) + 0.5 - half) / PROCESS_H)
        bottom = min(1.0, (float(y) + 0.5 + half) / PROCESS_H)
        regions.append(NormalizedRoi(left, top, right, bottom))

    arr = np.asarray(frame)
    height, width = arr.shape[:2]
    return RheedRoiDefinition.create(
        mode="auto_1x1_three_spot",
        regions=regions,
        capture_backend=str(getattr(state, "capture_backend", "") or ""),
        source_hwnd=int(getattr(state, "source_hwnd", 0) or 0),
        capture_geometry_id=str(getattr(state, "capture_geometry_id", "") or ""),
        frame_width=width,
        frame_height=height,
        definition_capture_sequence=int(
            getattr(state, "capture_sequence", 0)
            or getattr(state, "sample_sequence", 0)
            or getattr(state, "frame_number", 0)
        ),
        definition_captured_at_utc=str(getattr(state, "captured_at_utc", "") or ""),
        landmark_points_processed=(
            (float(point[0]), float(point[1])) for point in detection.points
        ),
        detector_confidence=float(detection.confidence),
        detector_peak_snr=float(detection.peak_snr),
        detector_method=detection.method,
    )


def measure_regions(
    frame: np.ndarray,
    regions: Iterable[NormalizedRoi],
) -> tuple[float, float, int]:
    """Measure a rectangle union without double-counting overlaps."""
    luminance = frame_luminance_plane(frame)
    height, width = luminance.shape
    mask = np.zeros((height, width), dtype=bool)
    count = 0
    for region in tuple(regions):
        x0, y0, x1, y1 = region.pixel_bounds(width, height)
        mask[y0:y1, x0:x1] = True
        count += 1
    if count == 0 or not np.any(mask):
        raise ValueError("ROI contains no pixels")
    values = luminance[mask]
    total = float(np.sum(values, dtype=np.float64))
    pixel_count = int(values.size)
    mean = float(total / pixel_count)
    if not all(math.isfinite(value) for value in (total, mean)):
        raise ValueError("ROI measurement produced a non-finite intensity")
    return total, mean, pixel_count


def measure_camera_state(
    state: object,
    roi: RheedRoiDefinition,
    *,
    baseline_sum: float | None = None,
) -> RheedIntensitySample:
    """Measure one valid camera state and preserve its capture provenance."""
    if not bool(getattr(state, "connected", False)):
        raise ValueError("Camera is disconnected")
    if not bool(getattr(state, "valid", False)):
        raise ValueError("Camera frame is invalid")
    frame = getattr(state, "frame", None)
    if frame is None:
        raise ValueError("Camera state has no frame")
    arr = np.asarray(frame)
    reason = roi.compatibility_error(state, arr)
    if reason:
        raise ValueError(reason)

    started_ns = time.perf_counter_ns()
    total, mean, pixel_count = measure_regions(arr, roi.regions)
    measured_ns = time.perf_counter_ns()
    captured_ns = int(
        getattr(state, "captured_monotonic_ns", 0)
        or getattr(state, "received_monotonic_ns", 0)
        or measured_ns
    )
    frame_age_ms = max(0.0, (measured_ns - captured_ns) / 1_000_000.0)
    relative = 0.0
    if baseline_sum is not None and math.isfinite(baseline_sum) and baseline_sum != 0:
        relative = 100.0 * (total - baseline_sum) / baseline_sum
    return RheedIntensitySample(
        roi=roi,
        captured_at_utc=str(getattr(state, "captured_at_utc", "") or ""),
        capture_sequence=int(
            getattr(state, "capture_sequence", 0)
            or getattr(state, "sample_sequence", 0)
            or getattr(state, "frame_number", 0)
        ),
        captured_monotonic_ns=captured_ns,
        measured_monotonic_ns=measured_ns,
        frame_age_ms=frame_age_ms,
        intensity_sum=total,
        intensity_mean=mean,
        pixel_count=pixel_count,
        relative_change_pct=relative,
        measurement_duration_ms=max(
            0.0, (measured_ns - started_ns) / 1_000_000.0,
        ),
    )


def definition_from_mapping(payload: Mapping[str, Any]) -> RheedRoiDefinition:
    """Small deserializer used by offline tools and tests."""
    return RheedRoiDefinition(
        roi_definition_id=str(payload["roi_definition_id"]),
        mode=str(payload["mode"]),
        regions=tuple(
            NormalizedRoi(
                float(item["left"]), float(item["top"]),
                float(item["right"]), float(item["bottom"]),
            )
            for item in payload["regions_normalized"]
        ),
        capture_backend=str(payload.get("capture_backend", "")),
        source_hwnd=int(payload.get("source_hwnd", 0)),
        capture_geometry_id=str(payload.get("capture_geometry_id", "")),
        frame_width=int(payload["frame_width"]),
        frame_height=int(payload["frame_height"]),
        definition_capture_sequence=int(payload.get("definition_capture_sequence", 0)),
        definition_captured_at_utc=str(payload.get("definition_captured_at_utc", "")),
        landmark_points_processed=tuple(
            tuple(point) for point in payload.get("landmark_points_processed", ())
        ),
        detector_confidence=payload.get("detector_confidence"),
        detector_peak_snr=payload.get("detector_peak_snr"),
        detector_method=str(payload.get("detector_method", "")),
    )


__all__ = [
    "INTENSITY_METRIC",
    "MIN_DETECTION_SNR",
    "NormalizedRoi",
    "RheedIntensitySample",
    "RheedRoiDefinition",
    "auto_three_spot_roi_definition",
    "definition_from_mapping",
    "frame_luminance_plane",
    "manual_roi_definition",
    "measure_camera_state",
    "measure_regions",
    "preprocess_for_landmarks",
]
