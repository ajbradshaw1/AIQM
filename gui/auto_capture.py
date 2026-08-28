"""
Auto-capture engine for MBE Growth Monitor.

Watches incoming RHEED frames for significant changes and emits a signal
when the change score exceeds a configurable threshold (with debounce and
cooldown).  Designed around a pluggable ChangeDetector ABC so higher-tier
strategies (SimCLR embedding distance, Classifier2 output change) can drop
in without refactoring.
"""
from __future__ import annotations

import collections
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from .specular import detect_specular, image_derived_roi


# ---------------------------------------------------------------------------
# ChangeDetector ABC + implementations
# ---------------------------------------------------------------------------

class ChangeDetector(ABC):
    """Base class for frame change detection strategies."""

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def compute_score(self, frame: np.ndarray) -> float:
        """Return a score indicating how different this frame is from reference.

        Score range is detector-specific. Tier 1/2 detectors return values on
        the 0-255 absolute pixel-intensity scale; Tier 3 returns 0-1.
        """
        ...

    @property
    def last_diagnostics(self) -> dict:
        """Diagnostics for the most recent score.

        Older/custom detectors are not required to provide diagnostics.  The
        engine therefore treats an empty mapping as ``not_available`` and
        preserves the existing scalar-score and Qt-signal contracts.
        """
        return {}


class IntensityChangeDetector(ChangeDetector):
    """Tier 1: Global mean pixel intensity comparison."""

    def __init__(self):
        self._reference_intensity: float | None = None

    def reset(self) -> None:
        self._reference_intensity = None

    def compute_score(self, frame: np.ndarray) -> float:
        gray = frame if frame.ndim == 2 else frame.mean(axis=2)
        current = float(gray.mean())
        if self._reference_intensity is None:
            self._reference_intensity = current
            return 0.0
        delta = abs(current - self._reference_intensity) / max(self._reference_intensity, 1e-6)
        self._reference_intensity = current  # rolling reference
        return delta


class PixelDiffChangeDetector(ChangeDetector):
    """Tier 1.5: Mean absolute pixel diff against a FIFO buffer of recent frames.

    Maintains a deque of the last ``buffer_size`` grayscale frames. Each new
    frame is scored as the mean absolute pixel difference between it and the
    *mean* of the buffer. Score is then smoothed by a rolling mean over the
    last ``smooth_window`` raw scores.

    Compared to ``IntensityChangeDetector`` (mean intensity only), this
    catches spatial pattern changes — the actual signal in reconstruction
    transitions — not just global brightness shifts.

    Threshold tuning notes (against Rahim's 2022_02_04 STO trajectory using
    diff-vs-previous-frame): baseline ~0.5, real reconstruction events peak
    at 2.5-9.0. Diff-vs-buffer-mean produces *larger* peaks for the same
    transitions (full delta vs rate of delta), so an in-GUI threshold of
    2.0-2.5 is a reasonable starting point. Re-tune offline against the
    same dataset with the buffer-mean variant before relying on the value.

    May 2026 — ``score_metric`` and ``roi_mode`` are opt-in parameters that
    route the score through alternative scoring (std of |diff| vs mean) and
    ROI restriction (specular-anchored, image-derived). Defaults preserve
    the original full-frame mean-of-|diff| behaviour for backward compat.
    Threshold values DO NOT transfer between metric/ROI combinations and
    must be re-tuned per configuration.
    """

    def __init__(
        self,
        buffer_size: int = 20,
        smooth_window: int = 3,
        score_metric: str = "mean",
        roi_mode: str = "full",
        roi_threshold_frac: float = 0.5,
    ):
        if score_metric not in ("mean", "std"):
            raise ValueError(
                f"score_metric must be 'mean' or 'std', got {score_metric!r}"
            )
        if roi_mode not in ("full", "specular"):
            raise ValueError(
                f"roi_mode must be 'full' or 'specular', got {roi_mode!r}"
            )

        self._buffer_size = buffer_size
        self._smooth_window = smooth_window
        self._score_metric = score_metric
        self._roi_mode = roi_mode
        self._roi_threshold_frac = roi_threshold_frac
        self._buffer: collections.deque[np.ndarray] = collections.deque(
            maxlen=buffer_size,
        )
        # Running sum of buffer contents — O(1) buffer-mean updates rather
        # than O(buffer_size) per frame.
        self._sum: np.ndarray | None = None
        self._recent_scores: collections.deque[float] = collections.deque(
            maxlen=max(1, smooth_window),
        )

    def reset(self) -> None:
        self._buffer.clear()
        self._sum = None
        self._recent_scores.clear()

    def compute_score(self, frame: np.ndarray) -> float:
        gray = self._to_gray(frame)

        # Defensive reset if the camera resolution changed mid-session;
        # the running sum is invalid against a different shape.
        if self._sum is not None and gray.shape != self._sum.shape:
            self.reset()

        if not self._buffer:
            self._buffer.append(gray)
            self._sum = gray.copy()
            self._recent_scores.append(0.0)
            return 0.0

        buffer_mean = self._sum / len(self._buffer)
        diff = np.abs(gray - buffer_mean)

        if self._roi_mode == "specular":
            x, y = detect_specular(gray)
            roi = image_derived_roi(
                gray, x, y, threshold_frac=self._roi_threshold_frac
            )
            diff = diff[roi]

        if self._score_metric == "mean":
            raw_score = float(np.mean(diff))
        else:  # "std"
            raw_score = float(np.std(diff))

        # Update running sum: subtract the about-to-be-evicted frame
        # before deque.append silently drops it.
        if len(self._buffer) == self._buffer_size:
            self._sum -= self._buffer[0]
        self._buffer.append(gray)
        self._sum += gray

        self._recent_scores.append(raw_score)
        return float(sum(self._recent_scores) / len(self._recent_scores))

    @staticmethod
    def _to_gray(frame: np.ndarray) -> np.ndarray:
        """Convert frame to float32 grayscale.

        For RGB inputs (kSA false-color screengrabs), takes the green
        channel — RHEED intensity lives there per project convention.
        """
        if frame.ndim == 2:
            return frame.astype(np.float32)
        return frame[:, :, 1].astype(np.float32)


class TranslationInvariantChangeDetector(ChangeDetector):
    """RHEED structural-change score after bounded translation alignment.

    A small drift of the screen capture or electron-beam image must not be
    labeled as a reconstruction transition.  This detector estimates only a
    2-D translation (never scale or rotation), aligns the current frame to a
    rolling reference, and scores the valid overlap.  Registration uses a
    down-sampled NumPy phase correlation followed by a bounded integer-pixel
    normalized-correlation refinement, so it remains suitable for the older
    CPU-only Ch-MBE workstation.

    The comparison independently normalizes the robust luminance range of the
    reference and current overlap.  Uniform brightness and contrast changes
    therefore contribute little, while spatial redistribution of RHEED spots
    and streaks remains visible.  Scores retain the familiar approximately
    0--255 pixel-residual scale used by ``PixelDiffChangeDetector``.

    Low-texture frames, insufficient overlap, and translations beyond
    ``max_shift_px`` fail closed with a zero score.  The reason, estimated
    shift, overlap, and registration confidence remain available through
    :attr:`last_diagnostics` for logging and operator review.
    """

    _EPS = 1e-8

    def __init__(
        self,
        buffer_size: int = 20,
        smooth_window: int = 3,
        max_shift_px: int = 12,
        registration_max_dimension: int = 384,
        min_texture_span: float = 2.0,
        min_overlap_fraction: float = 0.70,
        brightness_percentiles: tuple[float, float] = (5.0, 95.0),
    ):
        if buffer_size < 1:
            raise ValueError("buffer_size must be at least 1")
        if smooth_window < 1:
            raise ValueError("smooth_window must be at least 1")
        if max_shift_px < 0:
            raise ValueError("max_shift_px must be non-negative")
        if registration_max_dimension < 32:
            raise ValueError("registration_max_dimension must be at least 32")
        if min_texture_span < 0:
            raise ValueError("min_texture_span must be non-negative")
        if not 0.0 < min_overlap_fraction <= 1.0:
            raise ValueError("min_overlap_fraction must be in (0, 1]")
        low_percentile, high_percentile = brightness_percentiles
        if not 0.0 <= low_percentile < high_percentile <= 100.0:
            raise ValueError(
                "brightness_percentiles must be increasing values in [0, 100]"
            )

        self._buffer_size = int(buffer_size)
        self._smooth_window = int(smooth_window)
        self._max_shift_px = int(max_shift_px)
        self._registration_max_dimension = int(registration_max_dimension)
        self._min_texture_span = float(min_texture_span)
        self._min_overlap_fraction = float(min_overlap_fraction)
        self._brightness_percentiles = (
            float(low_percentile),
            float(high_percentile),
        )

        # Frames in this deque have already been translated into the first
        # frame's coordinate system.  A validity mask accompanies each frame
        # so padded borders never enter the rolling reference or score.
        self._buffer: collections.deque[
            tuple[np.ndarray, np.ndarray]
        ] = collections.deque(maxlen=self._buffer_size)
        self._sum: np.ndarray | None = None
        self._count: np.ndarray | None = None
        self._recent_scores: collections.deque[float] = collections.deque(
            maxlen=self._smooth_window,
        )
        self._last_diagnostics: dict = self._diagnostics("uninitialized")

    @property
    def last_diagnostics(self) -> dict:
        return dict(self._last_diagnostics)

    def reset(self) -> None:
        self._buffer.clear()
        self._sum = None
        self._count = None
        self._recent_scores.clear()
        self._last_diagnostics = self._diagnostics("reset")

    def compute_score(self, frame: np.ndarray) -> float:
        gray = PixelDiffChangeDetector._to_gray(frame)
        if gray.ndim != 2 or gray.size == 0:
            self._recent_scores.clear()
            self._last_diagnostics = self._diagnostics("invalid_frame")
            return 0.0

        finite = np.isfinite(gray)
        if not np.all(finite):
            if not np.any(finite):
                self._recent_scores.clear()
                self._last_diagnostics = self._diagnostics("invalid_frame")
                return 0.0
            gray = gray.copy()
            gray[~finite] = float(np.median(gray[finite]))

        if self._sum is not None and gray.shape != self._sum.shape:
            self.reset()
            return self._seed(gray, status="shape_reset")
        if not self._buffer:
            return self._seed(gray, status="seed")

        assert self._sum is not None and self._count is not None
        reference_valid = self._count > 0.0
        reference = np.zeros_like(self._sum, dtype=np.float32)
        np.divide(
            self._sum,
            self._count,
            out=reference,
            where=reference_valid,
        )

        reference_values = reference[reference_valid]
        if (
            reference_values.size < 16
            or self._texture_span(reference_values) < self._min_texture_span
            or self._texture_span(gray.ravel()) < self._min_texture_span
        ):
            return self._fail_closed("low_texture")

        registration = self._estimate_translation(
            reference,
            gray,
            reference_valid,
        )
        if registration["status"] != "ok":
            return self._fail_closed(
                registration["status"],
                shift_y_px=registration["shift_y_px"],
                shift_x_px=registration["shift_x_px"],
                registration_confidence=registration[
                    "registration_confidence"
                ],
                phase_peak_prominence=registration[
                    "phase_peak_prominence"
                ],
                overlap_fraction=registration["overlap_fraction"],
            )

        shift_y = int(registration["shift_y_px"])
        shift_x = int(registration["shift_x_px"])
        reference_slice, current_slice = self._overlap_slices(
            gray.shape,
            shift_y,
            shift_x,
        )
        reference_overlap = reference[reference_slice]
        current_overlap = gray[current_slice]
        valid_overlap = reference_valid[reference_slice]
        overlap_fraction = float(valid_overlap.sum() / gray.size)
        if (
            overlap_fraction < self._min_overlap_fraction
            or valid_overlap.sum() < 16
        ):
            return self._fail_closed(
                "insufficient_overlap",
                shift_y_px=shift_y,
                shift_x_px=shift_x,
                registration_confidence=registration[
                    "registration_confidence"
                ],
                overlap_fraction=overlap_fraction,
            )

        reference_values = reference_overlap[valid_overlap]
        current_values = current_overlap[valid_overlap]
        reference_normalized = self._robust_normalize(reference_values)
        current_normalized = self._robust_normalize(current_values)
        if reference_normalized is None or current_normalized is None:
            return self._fail_closed(
                "low_texture",
                shift_y_px=shift_y,
                shift_x_px=shift_x,
                registration_confidence=registration[
                    "registration_confidence"
                ],
                overlap_fraction=overlap_fraction,
            )

        residual = np.abs(reference_normalized - current_normalized)
        # Winsorising a tiny fraction prevents one damaged/hot pixel from
        # dominating an otherwise stable frame without hiding spot changes.
        if residual.size >= 200:
            residual = np.minimum(residual, np.percentile(residual, 99.5))
        raw_score = float(255.0 * np.mean(residual))

        aligned = np.zeros_like(gray, dtype=np.float32)
        aligned_valid = np.zeros_like(gray, dtype=bool)
        aligned[reference_slice] = current_overlap
        aligned_valid[reference_slice] = True
        self._append_aligned(aligned, aligned_valid)

        self._recent_scores.append(raw_score)
        smoothed_score = float(np.mean(self._recent_scores))
        self._last_diagnostics = self._diagnostics(
            "ok",
            score=smoothed_score,
            raw_score=raw_score,
            shift_y_px=shift_y,
            shift_x_px=shift_x,
            registration_confidence=registration[
                "registration_confidence"
            ],
            phase_peak_prominence=registration["phase_peak_prominence"],
            overlap_fraction=overlap_fraction,
        )
        return smoothed_score

    def _seed(self, gray: np.ndarray, *, status: str) -> float:
        valid = np.ones_like(gray, dtype=bool)
        self._append_aligned(gray.copy(), valid)
        self._recent_scores.append(0.0)
        self._last_diagnostics = self._diagnostics(
            status,
            overlap_fraction=1.0,
        )
        return 0.0

    def _append_aligned(
        self,
        aligned: np.ndarray,
        valid: np.ndarray,
    ) -> None:
        if self._sum is None:
            self._sum = np.zeros_like(aligned, dtype=np.float32)
            self._count = np.zeros_like(aligned, dtype=np.float32)
        assert self._count is not None
        if len(self._buffer) == self._buffer_size:
            old_frame, old_valid = self._buffer[0]
            self._sum[old_valid] -= old_frame[old_valid]
            self._count[old_valid] -= 1.0
        self._buffer.append((aligned, valid))
        self._sum[valid] += aligned[valid]
        self._count[valid] += 1.0

    def _fail_closed(self, status: str, **diagnostics) -> float:
        self._recent_scores.clear()
        self._recent_scores.append(0.0)
        self._last_diagnostics = self._diagnostics(status, **diagnostics)
        return 0.0

    def _estimate_translation(
        self,
        reference: np.ndarray,
        current: np.ndarray,
        reference_valid: np.ndarray,
    ) -> dict:
        height, width = reference.shape
        stride = max(
            1,
            int(np.ceil(max(height, width) / self._registration_max_dimension)),
        )
        reference_small = reference[::stride, ::stride]
        current_small = current[::stride, ::stride]
        valid_small = reference_valid[::stride, ::stride]
        if valid_small.sum() < 16:
            return self._registration_result("insufficient_overlap")

        reference_fill = float(np.median(reference_small[valid_small]))
        registration_reference = np.where(
            valid_small,
            reference_small,
            reference_fill,
        ).astype(np.float64, copy=False)
        registration_current = current_small.astype(np.float64, copy=False)
        registration_reference -= registration_reference.mean()
        registration_current -= registration_current.mean()
        reference_std = float(registration_reference.std())
        current_std = float(registration_current.std())
        if reference_std < self._EPS or current_std < self._EPS:
            return self._registration_result("low_texture")
        registration_reference /= reference_std
        registration_current /= current_std

        # A Hann window reduces FFT wrap-edge energy.  The score itself is
        # always computed on the unwindowed, non-wrapped valid overlap.
        window = np.outer(
            np.hanning(reference_small.shape[0]),
            np.hanning(reference_small.shape[1]),
        )
        reference_fft = np.fft.fft2(registration_reference * window)
        current_fft = np.fft.fft2(registration_current * window)
        cross_power = reference_fft * np.conj(current_fft)
        magnitude = np.abs(cross_power)
        cross_power /= np.maximum(magnitude, self._EPS)
        correlation = np.abs(np.fft.ifft2(cross_power))
        peak_index = np.unravel_index(np.argmax(correlation), correlation.shape)
        global_y_small = self._wrapped_shift(
            peak_index[0], correlation.shape[0]
        )
        global_x_small = self._wrapped_shift(
            peak_index[1], correlation.shape[1]
        )
        global_y = global_y_small * stride
        global_x = global_x_small * stride

        peak_value = float(correlation[peak_index])
        excluded = correlation.copy()
        peak_y, peak_x = peak_index
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                excluded[
                    (peak_y + dy) % excluded.shape[0],
                    (peak_x + dx) % excluded.shape[1],
                ] = 0.0
        second_peak = float(np.max(excluded))
        phase_prominence = max(
            0.0,
            (peak_value - second_peak) / max(peak_value, self._EPS),
        )

        # Phase peaks can be a few pixels broad for streak-like RHEED images.
        # Refine a small neighborhood rather than trusting a single FFT bin.
        tolerance = max(3, (stride + 1) // 2 + 1)
        global_correlation, global_overlap = self._correlation_at_shift(
            reference,
            current,
            reference_valid,
            global_y,
            global_x,
            sample_stride=stride,
            required_overlap=0.25,
        )
        if (
            (
                abs(global_y) > self._max_shift_px + tolerance
                or abs(global_x) > self._max_shift_px + tolerance
            )
            # A structural change can make an unrelated FFT peak the global
            # maximum.  Require the images to agree strongly *after* applying
            # that out-of-bounds shift before classifying it as camera drift.
            and global_correlation >= 0.85
        ):
            return self._registration_result(
                "excessive_shift",
                shift_y_px=global_y,
                shift_x_px=global_x,
                registration_confidence=float(np.clip(
                    0.75 * ((global_correlation + 1.0) / 2.0)
                    + 0.25 * phase_prominence,
                    0.0,
                    1.0,
                )),
                phase_peak_prominence=phase_prominence,
                overlap_fraction=global_overlap,
            )

        # Use the strongest *bounded* phase-correlation peak for refinement.
        # This keeps a true structural change scoreable even when its weak
        # global FFT maximum happens to be far from the physical drift range.
        max_small_shift = int(np.ceil(self._max_shift_px / stride))
        bounded_peak = -np.inf
        bounded_y_small = 0
        bounded_x_small = 0
        for shift_y_small in range(-max_small_shift, max_small_shift + 1):
            for shift_x_small in range(-max_small_shift, max_small_shift + 1):
                value = float(correlation[
                    shift_y_small % correlation.shape[0],
                    shift_x_small % correlation.shape[1],
                ])
                if value > bounded_peak:
                    bounded_peak = value
                    bounded_y_small = shift_y_small
                    bounded_x_small = shift_x_small
        coarse_y = int(np.clip(
            bounded_y_small * stride,
            -self._max_shift_px,
            self._max_shift_px,
        ))
        coarse_x = int(np.clip(
            bounded_x_small * stride,
            -self._max_shift_px,
            self._max_shift_px,
        ))

        best_shift: tuple[int, int] | None = None
        best_correlation = -np.inf
        second_correlation = -np.inf
        best_overlap = 0.0
        for shift_y in range(
            max(-self._max_shift_px, coarse_y - tolerance),
            min(self._max_shift_px, coarse_y + tolerance) + 1,
        ):
            for shift_x in range(
                max(-self._max_shift_px, coarse_x - tolerance),
                min(self._max_shift_px, coarse_x + tolerance) + 1,
            ):
                candidate_correlation, overlap = self._correlation_at_shift(
                    reference,
                    current,
                    reference_valid,
                    shift_y,
                    shift_x,
                    sample_stride=stride,
                )
                if candidate_correlation > best_correlation:
                    second_correlation = best_correlation
                    best_correlation = candidate_correlation
                    best_shift = (shift_y, shift_x)
                    best_overlap = overlap
                elif candidate_correlation > second_correlation:
                    second_correlation = candidate_correlation

        if best_shift is None or not np.isfinite(best_correlation):
            return self._registration_result("insufficient_overlap")
        correlation_strength = np.clip((best_correlation + 1.0) / 2.0, 0.0, 1.0)
        correlation_margin = max(0.0, best_correlation - second_correlation)
        confidence = float(np.clip(
            0.65 * correlation_strength
            + 0.25 * phase_prominence
            + 0.10 * min(1.0, correlation_margin * 10.0),
            0.0,
            1.0,
        ))
        return self._registration_result(
            "ok",
            shift_y_px=best_shift[0],
            shift_x_px=best_shift[1],
            registration_confidence=confidence,
            phase_peak_prominence=phase_prominence,
            overlap_fraction=best_overlap,
        )

    def _correlation_at_shift(
        self,
        reference: np.ndarray,
        current: np.ndarray,
        reference_valid: np.ndarray,
        shift_y: int,
        shift_x: int,
        *,
        sample_stride: int,
        required_overlap: float | None = None,
    ) -> tuple[float, float]:
        reference_slice, current_slice = self._overlap_slices(
            reference.shape,
            shift_y,
            shift_x,
        )
        reference_values = reference[reference_slice][::sample_stride, ::sample_stride]
        current_values = current[current_slice][::sample_stride, ::sample_stride]
        valid = reference_valid[reference_slice][::sample_stride, ::sample_stride]
        sampled_frame_size = reference[::sample_stride, ::sample_stride].size
        overlap_fraction = float(valid.sum() / sampled_frame_size)
        minimum_overlap = (
            self._min_overlap_fraction
            if required_overlap is None
            else required_overlap
        )
        if valid.sum() < 16 or overlap_fraction < minimum_overlap:
            return -np.inf, overlap_fraction
        a = reference_values[valid].astype(np.float64, copy=False)
        b = current_values[valid].astype(np.float64, copy=False)
        a = a - a.mean()
        b = b - b.mean()
        denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denominator < self._EPS:
            return -np.inf, overlap_fraction
        return float(np.dot(a, b) / denominator), overlap_fraction

    def _texture_span(self, values: np.ndarray) -> float:
        low, high = np.percentile(values, self._brightness_percentiles)
        return float(high - low)

    def _robust_normalize(self, values: np.ndarray) -> np.ndarray | None:
        low, high = np.percentile(values, self._brightness_percentiles)
        span = float(high - low)
        if span < self._min_texture_span:
            return None
        normalized = (values.astype(np.float32, copy=False) - low) / span
        return np.clip(normalized, -0.5, 1.5)

    @staticmethod
    def _wrapped_shift(index: int, size: int) -> int:
        return int(index - size if index > size // 2 else index)

    @staticmethod
    def _overlap_slices(
        shape: tuple[int, int],
        shift_y: int,
        shift_x: int,
    ) -> tuple[tuple[slice, slice], tuple[slice, slice]]:
        height, width = shape
        if shift_y >= 0:
            reference_y = slice(shift_y, height)
            current_y = slice(0, height - shift_y)
        else:
            reference_y = slice(0, height + shift_y)
            current_y = slice(-shift_y, height)
        if shift_x >= 0:
            reference_x = slice(shift_x, width)
            current_x = slice(0, width - shift_x)
        else:
            reference_x = slice(0, width + shift_x)
            current_x = slice(-shift_x, width)
        return (
            (reference_y, reference_x),
            (current_y, current_x),
        )

    def _diagnostics(self, status: str, **values) -> dict:
        diagnostics = {
            "detector": type(self).__name__,
            "status": status,
            "score": 0.0,
            "raw_score": 0.0,
            "shift_y_px": 0,
            "shift_x_px": 0,
            "registration_confidence": 0.0,
            "phase_peak_prominence": 0.0,
            "overlap_fraction": 0.0,
            "reference_frames": len(self._buffer),
        }
        diagnostics.update(values)
        return diagnostics

    @staticmethod
    def _registration_result(status: str, **values) -> dict:
        result = {
            "status": status,
            "shift_y_px": 0,
            "shift_x_px": 0,
            "registration_confidence": 0.0,
            "phase_peak_prominence": 0.0,
            "overlap_fraction": 0.0,
        }
        result.update(values)
        return result


# Future Tier 2:
# class EmbeddingChangeDetector(ChangeDetector):
#     """Cosine distance in SimCLR embedding space."""
#     def compute_score(self, frame): ...


class ClassificationChangeDetector(ChangeDetector):
    """Tier 3: Triggers when Classifier2 output distribution changes.

    Uses Jensen-Shannon divergence between the current and previous
    classification probability distributions.  Returns 1.0 when the
    argmax label flips; otherwise returns the JS-divergence (0-1).

    Unlike Tiers 1/2, this detector does NOT run the model itself.
    The caller must feed pre-computed classification scores via
    :meth:`set_scores` before calling :meth:`compute_score`.
    """

    def __init__(self):
        self._prev_scores: np.ndarray | None = None
        self._prev_label: int | None = None
        # Latest scores set by the caller (GrowthApp after inference).
        self._current_scores: np.ndarray | None = None

    def reset(self) -> None:
        self._prev_scores = None
        self._prev_label = None
        self._current_scores = None

    def set_scores(self, scores: list[float]) -> None:
        """Provide the latest Classifier2 win-rate scores (len 5)."""
        self._current_scores = np.asarray(scores, dtype=np.float64)

    def compute_score(self, frame: np.ndarray) -> float:  # noqa: ARG002 — frame unused
        """Return change score.  ``frame`` is accepted for ABC compat but unused."""
        if self._current_scores is None:
            return 0.0

        cur = self._current_scores
        cur_label = int(np.argmax(cur))

        if self._prev_scores is None:
            self._prev_scores = cur.copy()
            self._prev_label = cur_label
            return 0.0

        # Hard trigger: argmax label changed → score = 1.0
        if cur_label != self._prev_label:
            self._prev_scores = cur.copy()
            self._prev_label = cur_label
            return 1.0

        # Soft trigger: JS-divergence between distributions.
        score = float(self._js_divergence(self._prev_scores, cur))
        self._prev_scores = cur.copy()
        self._prev_label = cur_label
        return score

    @staticmethod
    def _js_divergence(p: np.ndarray, q: np.ndarray) -> float:
        """Jensen-Shannon divergence (base-2, range 0-1)."""
        # Normalise to probability distributions.
        p = np.clip(p, 1e-12, None)
        q = np.clip(q, 1e-12, None)
        p = p / p.sum()
        q = q / q.sum()
        m = 0.5 * (p + q)
        kl_pm = float(np.sum(p * np.log2(p / m)))
        kl_qm = float(np.sum(q * np.log2(q / m)))
        return 0.5 * (kl_pm + kl_qm)


# ---------------------------------------------------------------------------
# Event confirmation — plateau-shift test (A: May 22 PI proposal, first cut)
# ---------------------------------------------------------------------------

@dataclass
class ConfirmationResult:
    """Outcome of the plateau-shift confirmation test.

    Returned by :func:`confirm_event_by_plateau_shift`. ``confirmed`` is the
    pass/fail decision; ``diff_score`` is the underlying metric (useful for
    threshold tuning); ``reason`` is a stable string token suitable for
    logging or CSV (one of ``"confirmed"``, ``"rejected_below_threshold"``,
    ``"buffer_too_small"``, ``"windows_out_of_range"``).
    """
    confirmed: bool
    diff_score: float
    reason: str


def confirm_event_by_plateau_shift(
    buffer: "list[np.ndarray] | collections.deque",
    trigger_idx: int,
    pre_window_size: int = 5,
    post_window_size: int = 5,
    skip_around_trigger: int = 2,
    confirmation_threshold: float = 1.0,
    score_metric: str = "std",
) -> ConfirmationResult:
    """Distinguish a real reconstruction transition from a one-frame bump.

    The May 22 PI proposal: bumps and transitions both spike the change
    detector, but only transitions produce a *sustained baseline shift*.
    This test reformulates the PI's "middle frames drastically different
    from neighbors" criterion as a plateau-comparison:

        bump          → pre and post plateaus are SAME      (low diff)
        transition    → pre and post plateaus are DIFFERENT (high diff)

    Operationally: average frames before and after the trigger (skipping
    the transition frames themselves), then measure how different the two
    averaged frames are. The metric matches PixelDiffChangeDetector so
    confirmation thresholds can be reasoned about in the same units.

    Parameters
    ----------
    buffer
        Ordered sequence of frames (oldest → newest). Frames may be 2D
        (grayscale) or 3D (RGB); RGB is reduced to the green channel per
        project convention (see ``PixelDiffChangeDetector._to_gray``).
    trigger_idx
        Index in ``buffer`` of the trigger frame.
    pre_window_size, post_window_size
        Number of frames to average for each plateau.
    skip_around_trigger
        Frames skipped on each side of ``trigger_idx`` — these are the
        transition frames themselves and shouldn't pollute either plateau.
    confirmation_threshold
        Minimum plateau-shift score to confirm. Suggested starting value
        is ~0.5x the live trigger threshold (plateau shift is substantial
        relative to noise but smaller than the spike that triggered).
    score_metric
        ``"std"`` (default) or ``"mean"`` of the per-pixel ``|pre - post|``.
        Matches the same parameter in ``PixelDiffChangeDetector``.

        Note: ``"std"`` rejects purely uniform brightness shifts because
        they have no spatial variability — this is usually a feature
        (reconstruction transitions ARE spatially structured), but if
        uniform-shift detection is desired, use ``"mean"`` instead.

    Returns
    -------
    ConfirmationResult
        See class docstring for reason codes.

    Notes
    -----
    This function does NOT couple to the engine. Engine integration is
    the follow-up: add a PENDING_CONFIRMATION state to AutoCaptureEngine,
    delay the ``frame_captured`` emission by N frames after a trigger,
    then call this function on the rotated buffer. See
    ``aimbe_a_first_cut_design.md`` for the full FSM sketch.

    Examples
    --------
    >>> buffer = [np.full((64, 64), 50.0, np.float32)] * 10 + \\
    ...          [np.full((64, 64), 100.0, np.float32)] * 10
    >>> # Uniform shift — rejected under default std metric (no spatial variation)
    >>> result = confirm_event_by_plateau_shift(buffer, trigger_idx=10)
    >>> result.confirmed
    False
    """
    if score_metric not in ("std", "mean"):
        raise ValueError(
            f"score_metric must be 'std' or 'mean', got {score_metric!r}"
        )

    if isinstance(buffer, collections.deque):
        buffer = list(buffer)

    needed = pre_window_size + post_window_size + 2 * skip_around_trigger + 1
    if len(buffer) < needed:
        return ConfirmationResult(False, 0.0, "buffer_too_small")

    pre_start = trigger_idx - skip_around_trigger - pre_window_size
    pre_end = trigger_idx - skip_around_trigger
    post_start = trigger_idx + skip_around_trigger + 1
    post_end = post_start + post_window_size

    if pre_start < 0 or post_end > len(buffer):
        return ConfirmationResult(False, 0.0, "windows_out_of_range")

    def _to_gray(f: np.ndarray) -> np.ndarray:
        if f.ndim == 2:
            return f.astype(np.float32)
        return f[:, :, 1].astype(np.float32)

    pre_gray = [_to_gray(f) for f in buffer[pre_start:pre_end]]
    post_gray = [_to_gray(f) for f in buffer[post_start:post_end]]

    pre_mean = np.mean(pre_gray, axis=0)
    post_mean = np.mean(post_gray, axis=0)
    diff = np.abs(pre_mean - post_mean)
    diff_score = float(np.std(diff) if score_metric == "std" else np.mean(diff))

    confirmed = diff_score >= confirmation_threshold
    return ConfirmationResult(
        confirmed=confirmed,
        diff_score=diff_score,
        reason="confirmed" if confirmed else "rejected_below_threshold",
    )


# ---------------------------------------------------------------------------
# AutoCaptureEngine
# ---------------------------------------------------------------------------

class AutoCaptureEngine(QObject):
    """Evaluates each camera frame and emits *frame_captured* when a
    significant change is detected (after debounce, respecting cooldown)."""

    frame_captured = pyqtSignal(np.ndarray, float)  # (frame, change_score)
    _UNAVAILABLE_SCORE_STATUSES = frozenset({
        "invalid_frame",
        "shape_reset",
        "low_texture",
        "insufficient_overlap",
        "excessive_shift",
        "frame_error",
    })

    def __init__(
        self,
        threshold: float = 2.0,
        cooldown_s: float = 5.0,
        warmup_frames: int = 30,
        context_buffer_size: int = 20,
        adaptive_sigma: float | None = None,
        adaptive_history: int = 100,
        adaptive_warmup: int = 20,
        adaptive_floor: float = 1.0,
        suppress_events_during_adaptive_warmup: bool = True,
        rearm_below_frames: int = 3,
        parent=None,
    ):
        super().__init__(parent)
        if rearm_below_frames < 1:
            raise ValueError("rearm_below_frames must be at least 1")
        self._detector: ChangeDetector = TranslationInvariantChangeDetector()
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._warmup_frames = warmup_frames

        self._frame_count = 0
        self._last_capture_time = 0.0
        self._enabled = False
        self._debounce_count = 0
        self._debounce_required = 3  # consecutive frames above threshold
        self._rearm_below_required = int(rearm_below_frames)
        self._below_threshold_count = 0
        self._trigger_armed = True
        self._latest_score = 0.0
        self._latest_diagnostics: dict = {
            "detector": type(self._detector).__name__,
            "status": "uninitialized",
            "score": 0.0,
        }

        # Pre-event ring buffer of full-resolution RGB frames. Maintained
        # in parallel with the detector's internal grayscale buffer so that
        # when frame_captured fires we can dump the visual context that
        # led up to the trigger. Sized in frames; at ~10 Hz, 20 ≈ 2 s.
        self._context_buffer: collections.deque[
            tuple[np.ndarray, dict]
        ] = collections.deque(
            maxlen=context_buffer_size,
        )

        # Adaptive thresholding: when adaptive_sigma is set, the trigger
        # threshold becomes max(adaptive_floor, μ + Nσ) over a rolling
        # window of recent below-threshold scores. Cross-dataset validation
        # on Rahim's STO trajectories showed real events varying by an
        # order of magnitude in raw score, with stable baselines — adaptive
        # generalizes better than a fixed cutoff. Set to None to keep the
        # original fixed-threshold behavior.
        self._adaptive_sigma = adaptive_sigma
        self._adaptive_history = adaptive_history
        self._adaptive_warmup = adaptive_warmup
        self._adaptive_floor = adaptive_floor
        self._suppress_events_during_adaptive_warmup = (
            suppress_events_during_adaptive_warmup
        )
        self._baseline_scores: collections.deque[float] = collections.deque(
            maxlen=adaptive_history,
        )

    # -- Public API ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def threshold(self) -> float:
        """The fixed-threshold value (used when adaptive is off, or as a
        fallback during the adaptive warmup)."""
        return self._threshold

    @threshold.setter
    def threshold(self, value: float) -> None:
        self._threshold = value

    @property
    def effective_threshold(self) -> float:
        """The threshold actually applied this cycle.

        Adaptive (μ + Nσ over the rolling baseline) when adaptive_sigma is
        configured AND the baseline has filled to at least adaptive_warmup
        samples. Falls back to the fixed threshold during warmup so the
        detector behaves predictably in the first ~30 frames of a session.
        Always clamped to adaptive_floor to prevent runaway sensitivity in
        pathologically quiet sessions.
        """
        if (
            self._adaptive_sigma is None
            or len(self._baseline_scores) < self._adaptive_warmup
        ):
            return self._threshold
        arr = np.asarray(self._baseline_scores, dtype=np.float64)
        return max(
            self._adaptive_floor,
            float(arr.mean() + self._adaptive_sigma * arr.std()),
        )

    @property
    def latest_score(self) -> float:
        return self._latest_score

    @property
    def latest_diagnostics(self) -> dict:
        """Registration and score diagnostics for the latest frame.

        Returned defensively so logging/UI code cannot mutate detector state.
        The existing ``frame_captured(frame, score)`` signal remains unchanged.
        """
        return dict(self._latest_diagnostics)

    def set_detector(self, detector: ChangeDetector) -> None:
        """Swap in a different detection strategy (Tier 2/3)."""
        self._detector = detector
        self._latest_diagnostics = {
            "detector": type(detector).__name__,
            "status": "detector_changed",
            "score": 0.0,
        }

    def get_recent_frames(self) -> list[np.ndarray]:
        """Snapshot of the context buffer (oldest → newest), defensively copied.

        Used by the caller after frame_captured to dump the visual context
        leading up to a flagged event. Returns an empty list before any
        frames have been evaluated.
        """
        return [frame.copy() for frame, _metadata in self._context_buffer]

    def get_recent_captures(self) -> list[tuple[np.ndarray, dict]]:
        """Snapshot frames with the provenance captured alongside each one."""
        return [
            (frame.copy(), dict(metadata))
            for frame, metadata in self._context_buffer
        ]

    def reset(self) -> None:
        """Reset internal counters (call on session start)."""
        self._detector.reset()
        self._frame_count = 0
        self._last_capture_time = 0.0
        self._debounce_count = 0
        self._below_threshold_count = 0
        self._trigger_armed = True
        self._latest_score = 0.0
        self._latest_diagnostics = {
            "detector": type(self._detector).__name__,
            "status": "reset",
            "score": 0.0,
        }
        self._context_buffer.clear()
        self._baseline_scores.clear()

    def evaluate(
        self, frame: np.ndarray, capture_metadata: dict | None = None,
    ) -> None:
        """Called once per camera frame. Emits *frame_captured* if triggered."""
        if not self._enabled:
            return

        # Populate the context buffer regardless of warmup state — when a
        # trigger fires shortly after warmup ends, we want pre-event context
        # from the warmup window itself.
        context_metadata = dict(capture_metadata or {})
        self._context_buffer.append((frame.copy(), context_metadata))

        self._frame_count += 1
        if self._frame_count <= self._warmup_frames:
            detector_score = self._detector.compute_score(frame)  # feed baseline
            self._latest_score = 0.0
            self._record_diagnostics(
                context_metadata,
                detector_score=detector_score,
                engine_score=0.0,
                suppressed="fixed_warmup",
            )
            return

        score = self._detector.compute_score(frame)
        self._latest_score = score
        self._record_diagnostics(
            context_metadata,
            detector_score=score,
            engine_score=score,
        )
        now = time.time()

        # During adaptive warmup, the detector's internal buffer is still
        # settling and the rolling baseline hasn't filled — effective_threshold
        # falls back to the fixed _threshold (often near the floor), so any
        # noise above the floor fires events on a 5-frame cooldown. Cross-
        # dataset replay (Rahim 02_04/02_06/04_11) showed this consistently
        # produces 4 spurious events at frames 32/40/48/56 every session.
        # Fix: feed all scores to the baseline during adaptive warmup but
        # suppress event emission entirely.
        in_adaptive_warmup = (
            self._adaptive_sigma is not None
            and self._suppress_events_during_adaptive_warmup
            and len(self._baseline_scores) < self._adaptive_warmup
        )
        detector_status = str(
            self._latest_diagnostics.get("status") or "not_available"
        )
        score_available = (
            detector_status not in self._UNAVAILABLE_SCORE_STATUSES
        )
        self._latest_diagnostics["score_available"] = score_available
        if in_adaptive_warmup:
            if score_available:
                self._baseline_scores.append(score)
                self._latest_diagnostics["suppressed"] = "adaptive_warmup"
            else:
                self._latest_diagnostics["suppressed"] = (
                    "unavailable_for_adaptive_baseline"
                )
            self._latest_diagnostics["effective_threshold"] = float(
                self.effective_threshold
            )
            self._latest_diagnostics["trigger_armed"] = self._trigger_armed
            context_metadata["change_detection"] = dict(
                self._latest_diagnostics
            )
            return

        threshold = self.effective_threshold
        self._latest_diagnostics["effective_threshold"] = float(threshold)
        above_threshold = score_available and score >= threshold
        self._latest_diagnostics["above_threshold"] = above_threshold
        if not score_available:
            self._debounce_count = 0
            self._below_threshold_count = 0
            self._latest_diagnostics["suppressed"] = "unavailable_score"
        elif above_threshold:
            self._below_threshold_count = 0
            if self._trigger_armed:
                self._debounce_count += 1
            else:
                self._debounce_count = 0
                self._latest_diagnostics["suppressed"] = "waiting_for_rearm"
        else:
            self._debounce_count = 0
            # Only non-flagged scores feed the adaptive baseline — events
            # would pollute the rolling mean and pull the threshold up
            # behind their own peak. The fixed-mode path appends too,
            # which is harmless (the deque is just unused).
            self._baseline_scores.append(score)
            if not self._trigger_armed:
                self._below_threshold_count += 1
                if self._below_threshold_count >= self._rearm_below_required:
                    self._trigger_armed = True
                    self._below_threshold_count = 0
                    self._latest_diagnostics["rearmed"] = True

        if (
            self._trigger_armed
            and self._debounce_count >= self._debounce_required
            and (now - self._last_capture_time) >= self._cooldown_s
        ):
            self._last_capture_time = now
            self._debounce_count = 0
            self._below_threshold_count = 0
            self._trigger_armed = False
            self._latest_diagnostics["triggered"] = True
            self._latest_diagnostics["suppressed"] = ""
            self._latest_diagnostics["trigger_armed"] = False
            self._latest_diagnostics["debounce_count"] = 0
            context_metadata["change_detection"] = dict(
                self._latest_diagnostics
            )
            self.frame_captured.emit(frame.copy(), score)
            return

        self._latest_diagnostics["trigger_armed"] = self._trigger_armed
        self._latest_diagnostics["debounce_count"] = self._debounce_count
        self._latest_diagnostics["below_threshold_count"] = (
            self._below_threshold_count
        )
        context_metadata["change_detection"] = dict(
            self._latest_diagnostics
        )

    def _record_diagnostics(
        self,
        context_metadata: dict,
        *,
        detector_score: float,
        engine_score: float,
        suppressed: str | None = None,
    ) -> None:
        diagnostics = dict(self._detector.last_diagnostics)
        diagnostics.setdefault("detector", type(self._detector).__name__)
        diagnostics.setdefault("status", "not_available")
        diagnostics.setdefault("raw_score", float(detector_score))
        diagnostics["score"] = float(detector_score)
        diagnostics["engine_score"] = float(engine_score)
        if suppressed is not None:
            diagnostics["suppressed"] = suppressed
        self._latest_diagnostics = diagnostics
        # Preserve all caller provenance and add one namespaced diagnostic
        # object.  The frame_captured Qt signal stays exactly two-argument.
        context_metadata["change_detection"] = dict(diagnostics)
