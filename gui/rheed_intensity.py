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
    rank_peak_candidates,
)


INTENSITY_METRIC = "bt601_display_luminance_sum_v1"
# 2 adds `regions_normalized` of arbitrary length plus `region_labels`.
# Version 1 payloads still deserialize: they carry one region for
# `manual_rect` and three for `auto_1x1_three_spot`, and labels are derived.
ROI_SCHEMA_VERSION = 2
ROI_MODES = ("manual_rect", "auto_1x1_three_spot", "auto_peaks")

# Upper bound on tracked boxes. Not a storage limit — a legibility one. Past
# roughly this many the plot legend stops being readable and the per-capture
# measurement cost starts competing with the 1 Hz camera cadence.
MAX_ROI_REGIONS = 8

# Names for the three landmarks the Equalizer detector returns, in its own
# left-to-right order. Used as region labels so a curve in the trend plot can
# be matched to a spot on the frame without counting boxes.
AUTO_TRIPLET_LABELS = ("left", "specular", "right")


def default_region_labels(mode: str, count: int) -> tuple[str, ...]:
    """Stable, human-readable names for a definition's regions.

    The auto triplet gets its physical names, so a legend entry reads
    "specular" rather than "box 2". Everything else is numbered from 1.
    """
    if mode == "auto_1x1_three_spot" and count == len(AUTO_TRIPLET_LABELS):
        return AUTO_TRIPLET_LABELS
    if mode == "auto_peaks":
        # Ranked by brightness, so the name carries the ranking.
        return tuple(f"spot {index + 1}" for index in range(count))
    return tuple(f"box {index + 1}" for index in range(count))


def _next_box_label(existing: tuple[str, ...]) -> str:
    """First unused ``box N`` name, so appending never collides."""
    taken = set(existing)
    for index in range(1, MAX_ROI_REGIONS + 2):
        candidate = f"box {index}"
        if candidate not in taken:
            return candidate
    raise ValueError("No free region label remains")


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
    region_labels: tuple[str, ...] = ()
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
        # Both modes now hold 1..MAX_ROI_REGIONS boxes. The old rule was
        # "manual means exactly one, auto means exactly three", which made
        # per-spot tracking impossible: the auto triplet could only ever be
        # union-summed, and a grower could not watch the specular spot next to
        # a background box. The DETECTOR still returns exactly three landmarks
        # (see auto_three_spot_roi_definition) — that constraint lives there,
        # where it is a fact about the physics, not here.
        if not 1 <= len(regions) <= MAX_ROI_REGIONS:
            raise ValueError(
                f"An ROI needs 1 to {MAX_ROI_REGIONS} regions, got "
                f"{len(regions)}"
            )
        labels = tuple(str(label) for label in self.region_labels)
        if not labels:
            labels = default_region_labels(self.mode, len(regions))
        if len(labels) != len(regions):
            raise ValueError(
                f"Got {len(labels)} region labels for {len(regions)} regions"
            )
        if len(set(labels)) != len(labels):
            # Labels key the per-region CSV rows and the plot legend, so a
            # duplicate would silently merge two independent traces.
            raise ValueError(f"Region labels must be unique, got {labels}")
        object.__setattr__(self, "region_labels", labels)
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
        region_labels: Iterable[str] = (),
        landmark_points_processed: Iterable[tuple[float, float]] = (),
        detector_confidence: float | None = None,
        detector_peak_snr: float | None = None,
        detector_method: str = "",
    ) -> "RheedRoiDefinition":
        return cls(
            roi_definition_id=f"rheed-roi-{uuid.uuid4().hex}",
            mode=mode,
            regions=tuple(regions),
            region_labels=tuple(region_labels),
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
            "region_labels": list(self.region_labels),
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
class RegionIntensitySample:
    """One rectangle's own measurement within a multi-box ROI.

    Independent of the union: overlapping boxes each count their shared pixels,
    because a per-box trace is asking "what is happening inside THIS box", not
    "how much unique area is lit".
    """

    index: int
    label: str
    intensity_sum: float
    intensity_mean: float
    pixel_count: int
    relative_change_pct: float


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
    # Per-box series. `intensity_sum` above stays the UNION, which is the
    # documented bt601_display_luminance_sum_v1 metric and what
    # rheed_roi_intensity.csv has always held — adding regions here does not
    # change the meaning of any existing column.
    regions: tuple[RegionIntensitySample, ...] = ()
    # Camera exposure in force for THIS capture, and the driver's count of
    # confirmed live changes. Carried on the sample rather than looked up
    # later because exposure is now changeable mid-session: a reader
    # segmenting the trend needs to know which side of a step each row is on.
    exposure_us: float | None = None
    exposure_generation: int = 0

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
    *,
    append_to: RheedRoiDefinition | None = None,
) -> RheedRoiDefinition:
    """Build a source-bound definition from a grower-drawn rectangle.

    With ``append_to`` the new rectangle joins that definition's existing
    boxes — this is how a grower adds a background box beside the auto-detected
    triplet, or tracks two streaks at once.

    Appending produces a NEW definition ID rather than mutating the old one.
    That is what keeps the audit trail honest: the window's existing
    supersede/journal flow records the replacement, and any samples already
    written against the previous ID still describe the geometry they were
    measured with.
    """
    arr = np.asarray(frame)
    height, width = arr.shape[:2]
    region = NormalizedRoi(*normalized_rect)
    existing: tuple[NormalizedRoi, ...] = ()
    existing_labels: tuple[str, ...] = ()
    if append_to is not None:
        reason = append_to.compatibility_error(state, arr)
        if reason:
            raise ValueError(
                f"Cannot add a box to the active ROI: {reason}"
            )
        existing = tuple(append_to.regions)
        if len(existing) >= MAX_ROI_REGIONS:
            raise ValueError(
                f"Already tracking {MAX_ROI_REGIONS} regions — remove one "
                "before adding another"
            )
        # Carry the old labels forward verbatim. Re-deriving them would rename
        # the auto triplet's "specular" to "box 2" the moment a grower adds a
        # background box, orphaning every per-region row already written under
        # the old name.
        existing_labels = tuple(append_to.region_labels)
    labels = existing_labels + (_next_box_label(existing_labels),)
    return RheedRoiDefinition.create(
        mode="manual_rect",
        regions=existing + (region,),
        region_labels=labels,
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


def without_last_region(
    roi: RheedRoiDefinition,
    state: object,
    frame: np.ndarray,
) -> RheedRoiDefinition:
    """Drop the most recently added box, keeping every other label stable.

    Only the LAST box can be removed, deliberately. Removing box 2 of four
    would renumber boxes 3 and 4, and their per-region CSV rows are keyed by
    label — a rename mid-session would silently splice two different
    rectangles into one trace.
    """
    if len(roi.regions) <= 1:
        raise ValueError(
            "Only one region remains — use Clear ROI to stop measuring"
        )
    arr = np.asarray(frame)
    height, width = arr.shape[:2]
    return RheedRoiDefinition.create(
        # A trimmed set is grower-curated, not the detector's proposal, so it
        # is no longer "auto_1x1_three_spot" and carries no detector
        # provenance. The LABELS survive, because "specular" is still a true
        # statement about the box that kept it.
        mode="manual_rect",
        regions=roi.regions[:-1],
        region_labels=roi.region_labels[:-1],
        capture_backend=roi.capture_backend,
        source_hwnd=roi.source_hwnd,
        capture_geometry_id=roi.capture_geometry_id,
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
        # detect_live_landmarks orders its points left, specular, right; the
        # labels ride along so a legend entry names a spot, not an index.
        region_labels=AUTO_TRIPLET_LABELS,
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


def auto_peak_roi_definition(
    state: object,
    frame: np.ndarray,
    *,
    count: int = 3,
    box_size_processed_px: int = 7,
) -> RheedRoiDefinition:
    """Propose one box per bright spot, for the ``count`` brightest found.

    The general form of what ``auto_three_spot_roi_definition`` does for the
    1x1 triplet. That function stays the right choice when the grower wants
    the named specular/first-order geometry, because it also validates the
    left-specular-right ordering and rejects collinear or too-close triples.
    This one makes no claim about the diffraction pattern at all: it finds
    separated bright maxima and hands back boxes on them, ranked by intensity.

    Like the triplet, detection is ONE-SHOT. The boxes are fixed once
    confirmed and never re-detected per frame, because a region that tracked a
    moving spot would fold that motion into the intensity trend.
    """
    requested = int(count)
    if not 1 <= requested <= MAX_ROI_REGIONS:
        raise ValueError(
            f"Spot count must be between 1 and {MAX_ROI_REGIONS}, got {count}"
        )
    size = int(box_size_processed_px)
    if size < 3 or size > 31 or size % 2 == 0:
        raise ValueError("Spot box size must be an odd integer from 3 to 31")

    processed = preprocess_for_landmarks(frame)
    values = processed.astype(np.float64)
    # Same robust statistics detect_landmarks uses: a median/MAD noise floor
    # rather than a fixed threshold, so a dim idle frame and a bright growth
    # frame are judged on contrast, not absolute level.
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    noise = max(1e-6, 1.4826 * mad)
    dynamic = float(np.percentile(values, 99.9) - median)
    if dynamic <= max(1e-6, MIN_DETECTION_SNR * noise):
        raise ValueError(
            f"Insufficient spot contrast (peak SNR {dynamic / noise:.1f}); "
            "nothing bright enough to place a box on"
        )

    chosen = rank_peak_candidates(
        values, noise=noise, dynamic=dynamic, limit=requested,
        # A general ROI proposal must accept vertically stacked spots; only
        # the 1x1 row detector demands horizontal separation.
        require_x_separation=False,
    )
    if len(chosen) < requested:
        raise ValueError(
            f"Found only {len(chosen)} separated spot(s), not {requested}. "
            "Lower the spot count, or draw the extra boxes by hand."
        )

    half = size / 2.0
    regions = []
    for x, y, _intensity in chosen:
        left = max(0.0, (float(x) + 0.5 - half) / PROCESS_W)
        right = min(1.0, (float(x) + 0.5 + half) / PROCESS_W)
        top = max(0.0, (float(y) + 0.5 - half) / PROCESS_H)
        bottom = min(1.0, (float(y) + 0.5 + half) / PROCESS_H)
        regions.append(NormalizedRoi(left, top, right, bottom))

    peak_snr = min((value - median) / noise for _, _, value in chosen)
    confidence = float(np.clip((peak_snr - MIN_DETECTION_SNR) / 12.0, 0.0, 1.0))
    arr = np.asarray(frame)
    height, width = arr.shape[:2]
    return RheedRoiDefinition.create(
        mode="auto_peaks",
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
        detector_confidence=confidence,
        detector_peak_snr=float(peak_snr),
        detector_method="auto-peaks-2d",
    )


def measure_regions(
    frame: np.ndarray,
    regions: Iterable[NormalizedRoi],
    *,
    luminance: np.ndarray | None = None,
) -> tuple[float, float, int]:
    """Measure a rectangle union without double-counting overlaps.

    ``luminance`` lets a caller that already built the BT.601 plane hand it in.
    Converting a 656x492 RGB frame costs ~1.3 ms, and the union and per-box
    passes measure the SAME capture — recomputing it for each would double the
    per-frame cost to describe one image.
    """
    luminance = (
        frame_luminance_plane(frame) if luminance is None else luminance
    )
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


def measure_region_series(
    frame: np.ndarray,
    roi: RheedRoiDefinition,
    *,
    baseline_sums: Mapping[str, float] | None = None,
    luminance: np.ndarray | None = None,
) -> tuple[RegionIntensitySample, ...]:
    """Measure every rectangle independently, in definition order.

    Deliberately NOT masked like ``measure_regions``: the union exists to
    answer "how much light is in the selected area", while a per-box trace
    answers "what is this box doing". Two boxes that overlap are two questions,
    and each is entitled to the shared pixels.
    """
    luminance = (
        frame_luminance_plane(frame) if luminance is None else luminance
    )
    height, width = luminance.shape
    baselines = dict(baseline_sums or {})
    samples: list[RegionIntensitySample] = []
    for index, region in enumerate(roi.regions):
        label = roi.region_labels[index]
        x0, y0, x1, y1 = region.pixel_bounds(width, height)
        values = luminance[y0:y1, x0:x1]
        if values.size == 0:
            raise ValueError(f"Region {label!r} contains no pixels")
        total = float(np.sum(values, dtype=np.float64))
        pixel_count = int(values.size)
        mean = float(total / pixel_count)
        if not all(math.isfinite(value) for value in (total, mean)):
            raise ValueError(
                f"Region {label!r} produced a non-finite intensity"
            )
        baseline = baselines.get(label)
        relative = 0.0
        if baseline is not None and math.isfinite(baseline) and baseline != 0:
            relative = 100.0 * (total - baseline) / baseline
        samples.append(RegionIntensitySample(
            index=index,
            label=label,
            intensity_sum=total,
            intensity_mean=mean,
            pixel_count=pixel_count,
            relative_change_pct=relative,
        ))
    return tuple(samples)


def measure_camera_state(
    state: object,
    roi: RheedRoiDefinition,
    *,
    baseline_sum: float | None = None,
    baseline_region_sums: Mapping[str, float] | None = None,
) -> RheedIntensitySample:
    """Measure one valid camera state and preserve its capture provenance.

    Produces both the union figure (the documented metric, unchanged) and the
    per-box series, in a single pass over one frame so every number in the
    returned sample describes the same acquisition.
    """
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

    state_exposure = getattr(state, "exposure_us", None)
    started_ns = time.perf_counter_ns()
    # One conversion, both passes — they describe the same capture.
    luminance = frame_luminance_plane(arr)
    total, mean, pixel_count = measure_regions(
        arr, roi.regions, luminance=luminance,
    )
    region_samples = measure_region_series(
        arr, roi,
        baseline_sums=baseline_region_sums,
        luminance=luminance,
    )
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
        regions=region_samples,
        exposure_us=(
            float(state_exposure)
            if isinstance(state_exposure, (int, float))
            and not isinstance(state_exposure, bool)
            and math.isfinite(float(state_exposure))
            else None
        ),
        exposure_generation=int(
            getattr(state, "exposure_generation", 0) or 0
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
        region_labels=tuple(
            str(label) for label in payload.get("region_labels", ())
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
    "AUTO_TRIPLET_LABELS",
    "INTENSITY_METRIC",
    "MAX_ROI_REGIONS",
    "MIN_DETECTION_SNR",
    "NormalizedRoi",
    "RegionIntensitySample",
    "RheedIntensitySample",
    "RheedRoiDefinition",
    "auto_peak_roi_definition",
    "auto_three_spot_roi_definition",
    "default_region_labels",
    "definition_from_mapping",
    "frame_luminance_plane",
    "manual_roi_definition",
    "measure_camera_state",
    "measure_region_series",
    "measure_regions",
    "preprocess_for_landmarks",
    "without_last_region",
]
