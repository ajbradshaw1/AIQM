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
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Mapping

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
        """Provide scores in the loaded model's native class order."""
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

# Model-driven reconstruction-change types and state adapter.
@dataclass(frozen=True)
class ReconstructionPrediction:
    """One immutable prediction using the model's real output labels.

    Four-output models do not acquire a synthetic ``1x1`` score; five-output
    models retain it normally. ``scores`` is normalized only over ``labels``.
    """

    source: str
    model_version: str
    source_capture_sequence: int
    source_captured_monotonic_ns: int
    labels: tuple[str, ...]
    scores: tuple[float, ...]
    predicted_label: str
    valid: bool = True
    invalid_reason: str = ""
    view_segment_id: int | None = None
    visual_history_generation: int = 0
    model_input_mode: str = "unknown"

    @property
    def supports_1x1(self) -> bool:
        return any(_is_1x1_label(label) for label in self.labels)

    def score_dict(self) -> dict[str, float]:
        return dict(zip(self.labels, self.scores))


@dataclass(frozen=True)
class CapturedFrameSample:
    """One full-resolution frame selected by the 1 Hz rolling sampler."""

    frame: np.ndarray = field(repr=False, compare=False)
    metadata: dict = field(repr=False, compare=False)
    captured_monotonic_ns: int
    capture_sequence: int


@dataclass(frozen=True)
class ReconstructionChangeEvent:
    """A complete pre/post frame window around one stable label change."""

    prediction: ReconstructionPrediction
    previous_label: str
    new_label: str
    change_score: float
    trigger_capture: CapturedFrameSample
    captures: tuple[CapturedFrameSample, ...]
    pre_window_s: float
    post_window_s: float
    sample_interval_s: float

    def capture_payloads(self) -> list[tuple[np.ndarray, dict]]:
        """Return logger-ready captures with relative time and frame role."""
        payloads: list[tuple[np.ndarray, dict]] = []
        trigger_ns = self.prediction.source_captured_monotonic_ns
        trigger_sequence = self.prediction.source_capture_sequence
        for sample in self.captures:
            relative_s = (
                sample.captured_monotonic_ns - trigger_ns
            ) / 1_000_000_000.0
            if sample.capture_sequence == self.trigger_capture.capture_sequence:
                role = "trigger"
            elif sample.captured_monotonic_ns < trigger_ns:
                role = "pre"
            else:
                role = "post"
            metadata = dict(sample.metadata)
            metadata.update({
                "frame_role": role,
                "relative_to_trigger_s": relative_s,
                "trigger_capture_sequence": trigger_sequence,
            })
            payloads.append((sample.frame, metadata))
        return payloads


@dataclass
class _PendingReconstructionEvent:
    prediction: ReconstructionPrediction
    previous_label: str
    new_label: str
    change_score: float
    trigger_capture: CapturedFrameSample
    captures: list[CapturedFrameSample]
    end_monotonic_ns: int


def _is_1x1_label(label: str) -> bool:
    token = str(label).lower().replace(" ", "").replace("×", "x")
    return token in {"1x1", "(1x1)"}


def _invalid_prediction(reason: str, state) -> ReconstructionPrediction:
    return ReconstructionPrediction(
        source="classifier",
        model_version=str(getattr(state, "model_version", "") or ""),
        source_capture_sequence=int(
            getattr(state, "source_capture_sequence", 0) or 0
        ),
        source_captured_monotonic_ns=int(
            getattr(state, "source_received_monotonic_ns", 0) or 0
        ),
        labels=(),
        scores=(),
        predicted_label="",
        valid=False,
        invalid_reason=reason,
        view_segment_id=getattr(state, "view_segment_id", None),
        visual_history_generation=int(
            getattr(state, "visual_history_generation", 0) or 0
        ),
        model_input_mode=str(
            getattr(state, "model_input_mode", "unknown") or "unknown"
        ),
    )


def classifier_prediction_from_state(state) -> ReconstructionPrediction:
    """Adapt ``ClassifierState`` without assuming four or five outputs.

    This is an evidence-capture gate, not an advisory/control gate, so the
    current single-frame runtime's ``prediction_actionable=False`` does not
    suppress collection. Bad, OOD, unaligned, stale, or malformed outputs
    are still rejected fail-closed.
    """
    if not bool(getattr(state, "ready", False)):
        return _invalid_prediction("classifier_not_ready", state)
    if str(getattr(state, "error", "") or ""):
        return _invalid_prediction("classifier_error", state)
    if int(getattr(state, "last_frame_number", -1)) < 0:
        return _invalid_prediction("no_classified_frame", state)
    if int(getattr(state, "source_capture_sequence", 0) or 0) <= 0:
        return _invalid_prediction("missing_capture_sequence", state)
    if int(getattr(state, "source_received_monotonic_ns", 0) or 0) <= 0:
        return _invalid_prediction("missing_capture_time", state)
    if getattr(state, "gun_aligned", None) is not True:
        return _invalid_prediction("gun_not_aligned", state)
    if bool(getattr(state, "is_bad", False)):
        return _invalid_prediction("bad_frame", state)
    if bool(getattr(state, "is_ood", False)):
        return _invalid_prediction("out_of_distribution", state)
    if not bool(getattr(state, "has_confident_data", False)):
        return _invalid_prediction("no_confident_prediction", state)

    raw = getattr(state, "raw_scores", None)
    if not isinstance(raw, Mapping) or len(raw) < 2:
        return _invalid_prediction("invalid_output_contract", state)
    values: dict[str, float] = {}
    try:
        for raw_label, raw_value in raw.items():
            label = str(raw_label).strip()
            value = float(raw_value)
            if not label or label in values or not math.isfinite(value) or value < 0:
                raise ValueError
            values[label] = value
    except (TypeError, ValueError):
        return _invalid_prediction("non_finite_model_scores", state)
    total = sum(values.values())
    if not math.isfinite(total) or total <= 0:
        return _invalid_prediction("zero_model_score_sum", state)

    labels = tuple(sorted(values))
    scores = tuple(values[label] / total for label in labels)
    predicted_label = max(labels, key=lambda label: (values[label], label))
    return ReconstructionPrediction(
        source="classifier",
        model_version=str(getattr(state, "model_version", "") or "unknown"),
        source_capture_sequence=int(state.source_capture_sequence),
        source_captured_monotonic_ns=int(state.source_received_monotonic_ns),
        labels=labels,
        scores=scores,
        predicted_label=predicted_label,
        view_segment_id=getattr(state, "view_segment_id", None),
        visual_history_generation=int(
            getattr(state, "visual_history_generation", 0) or 0
        ),
        model_input_mode=str(
            getattr(state, "model_input_mode", "unknown") or "unknown"
        ),
    )


def _jensen_shannon(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    p = np.asarray(left, dtype=np.float64)
    q = np.asarray(right, dtype=np.float64)
    midpoint = 0.5 * (p + q)

    def _kl(values: np.ndarray, reference: np.ndarray) -> float:
        mask = values > 0
        return float(np.sum(
            values[mask] * np.log2(values[mask] / reference[mask]),
        ))

    return 0.5 * _kl(p, midpoint) + 0.5 * _kl(q, midpoint)


# Event confirmation for the legacy pixel detector.
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

# Production engine.
class ReconstructionChangeCaptureEngine(QObject):
    """Capture one frame/second around stable model-label transitions.

    Camera frames and classifier emissions arrive on separate Qt paths. This
    engine binds them through capture sequence and monotonic receive time,
    requires consecutive unique predictions, and emits only after the full
    post-event window exists.
    """

    event_ready = pyqtSignal(object)

    def __init__(
        self,
        *,
        pre_window_s: float = 60.0,
        post_window_s: float = 60.0,
        sample_interval_s: float = 1.0,
        stable_predictions: int = 3,
        max_pending_events: int = 4,
        parent=None,
    ):
        super().__init__(parent)
        if pre_window_s < 0 or post_window_s < 0 or sample_interval_s <= 0:
            raise ValueError("capture windows must be non-negative and cadence positive")
        if stable_predictions < 1 or max_pending_events < 1:
            raise ValueError("stability and pending-event limits must be positive")
        self.pre_window_s = float(pre_window_s)
        self.post_window_s = float(post_window_s)
        self.sample_interval_s = float(sample_interval_s)
        self.stable_predictions = int(stable_predictions)
        self.max_pending_events = int(max_pending_events)
        # A label change is confirmed a few classifier cycles after its first
        # candidate frame. Retain a bounded grace period so those cycles do
        # not consume the requested pre-trigger window.
        self._confirmation_grace_s = max(
            10.0,
            self.stable_predictions * self.sample_interval_s,
        )
        retention_s = self.pre_window_s + self._confirmation_grace_s
        max_samples = int(math.ceil(retention_s / sample_interval_s)) + 3
        self._samples: collections.deque[CapturedFrameSample] = collections.deque(
            maxlen=max_samples,
        )
        self._pending: list[_PendingReconstructionEvent] = []
        self._enabled = False
        self._last_sample_ns = 0
        self._last_prediction_sequence = 0
        self._capture_context: tuple | None = None
        self._prediction_contract: tuple | None = None
        self._baseline_label = ""
        self._baseline_scores: tuple[float, ...] = ()
        self._candidate_label = ""
        self._candidate_count = 0
        self._candidate_first: ReconstructionPrediction | None = None
        self._latest_score = 0.0
        self._last_invalid_reason = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    @property
    def latest_score(self) -> float:
        return self._latest_score

    @property
    def baseline_label(self) -> str:
        return self._baseline_label

    @property
    def pending_event_count(self) -> int:
        return len(self._pending)

    @property
    def sampled_frame_count(self) -> int:
        return len(self._samples)

    @property
    def status_text(self) -> str:
        if not self._enabled:
            return "stopped"
        if self._pending:
            return f"collecting post-window ({len(self._pending)} pending)"
        if self._baseline_label:
            if self._candidate_label:
                return (
                    f"baseline {self._baseline_label}; candidate "
                    f"{self._candidate_label} {self._candidate_count}/"
                    f"{self.stable_predictions}"
                )
            return f"baseline {self._baseline_label}"
        if self._last_invalid_reason:
            return f"waiting for valid model output ({self._last_invalid_reason})"
        return "learning model baseline"

    def get_recent_captures(self) -> list[tuple[np.ndarray, dict]]:
        """Compatibility snapshot of the current pre-event ring."""
        return [
            (sample.frame.copy(), dict(sample.metadata))
            for sample in self._samples
        ]

    def reset(self) -> None:
        """Cancel incomplete events and release all retained frame copies."""
        self._samples.clear()
        self._pending.clear()
        self._last_sample_ns = 0
        self._last_prediction_sequence = 0
        self._capture_context = None
        self._prediction_contract = None
        self._baseline_label = ""
        self._baseline_scores = ()
        self._clear_candidate()
        self._latest_score = 0.0
        self._last_invalid_reason = ""

    def evaluate(
        self,
        frame: np.ndarray,
        capture_metadata: dict | None = None,
        *,
        now_monotonic_ns: int | None = None,
    ) -> None:
        """Compatibility name for camera-path ingestion."""
        self.ingest_capture(
            frame,
            capture_metadata,
            now_monotonic_ns=now_monotonic_ns,
        )

    def ingest_capture(
        self,
        frame: np.ndarray,
        capture_metadata: dict | None = None,
        *,
        now_monotonic_ns: int | None = None,
    ) -> None:
        if not self._enabled:
            return
        metadata = dict(capture_metadata or {})
        try:
            sequence = int(metadata.get("capture_sequence") or 0)
            captured_ns = int(metadata.get("captured_monotonic_ns") or 0)
        except (TypeError, ValueError):
            return
        if sequence <= 0:
            return
        if captured_ns <= 0:
            captured_ns = int(now_monotonic_ns or time.perf_counter_ns())
            metadata["captured_monotonic_ns"] = captured_ns
        context = self._capture_context_token(metadata)
        if self._capture_context is not None and context != self._capture_context:
            self.reset()
        self._capture_context = context
        if captured_ns <= self._last_sample_ns:
            return
        cadence_ns = int(self.sample_interval_s * 1_000_000_000)
        if (
            self._last_sample_ns
            and captured_ns - self._last_sample_ns < int(0.9 * cadence_ns)
        ):
            return

        owned_frame = np.ascontiguousarray(frame).copy()
        owned_frame.setflags(write=False)
        sample = CapturedFrameSample(
            frame=owned_frame,
            metadata=metadata,
            captured_monotonic_ns=captured_ns,
            capture_sequence=sequence,
        )
        self._samples.append(sample)
        self._last_sample_ns = captured_ns
        cutoff = captured_ns - int(
            (self.pre_window_s + self._confirmation_grace_s)
            * 1_000_000_000
        )
        while self._samples and self._samples[0].captured_monotonic_ns < cutoff:
            self._samples.popleft()
        self._advance_pending(sample)

    def observe_prediction(self, prediction: ReconstructionPrediction) -> None:
        if not self._enabled:
            return
        if not prediction.valid:
            self._last_invalid_reason = prediction.invalid_reason
            self._clear_candidate()
            return
        self._last_invalid_reason = ""
        if prediction.source_capture_sequence <= self._last_prediction_sequence:
            return
        self._last_prediction_sequence = prediction.source_capture_sequence
        if not self._prediction_matches_capture_context(prediction):
            self._clear_candidate()
            self._last_invalid_reason = "capture_context_mismatch"
            return

        contract = (
            prediction.source,
            prediction.model_version,
            prediction.labels,
            prediction.view_segment_id,
            prediction.visual_history_generation,
        )
        if contract != self._prediction_contract:
            self._prediction_contract = contract
            self._baseline_label = ""
            self._baseline_scores = ()
            self._clear_candidate()

        label = prediction.predicted_label
        if not self._baseline_label:
            if label != self._candidate_label:
                self._candidate_label = label
                self._candidate_count = 1
                self._candidate_first = prediction
            else:
                self._candidate_count += 1
            if self._candidate_count >= self.stable_predictions:
                self._baseline_label = label
                self._baseline_scores = prediction.scores
                self._clear_candidate()
            return

        if label == self._baseline_label:
            self._baseline_scores = prediction.scores
            self._clear_candidate()
            self._latest_score = 0.0
            return

        if label != self._candidate_label:
            self._candidate_label = label
            self._candidate_count = 1
            self._candidate_first = prediction
        else:
            self._candidate_count += 1
        if self._candidate_count < self.stable_predictions:
            return

        trigger_prediction = self._candidate_first or prediction
        score = _jensen_shannon(self._baseline_scores, prediction.scores)
        self._latest_score = score
        self._start_pending_event(
            trigger_prediction,
            previous_label=self._baseline_label,
            new_label=label,
            score=score,
        )
        self._baseline_label = label
        self._baseline_scores = prediction.scores
        self._clear_candidate()

    def _start_pending_event(
        self,
        prediction: ReconstructionPrediction,
        *,
        previous_label: str,
        new_label: str,
        score: float,
    ) -> None:
        if len(self._pending) >= self.max_pending_events or not self._samples:
            self._last_invalid_reason = "pending_event_limit_or_missing_frames"
            return
        trigger = next(
            (
                sample for sample in self._samples
                if sample.capture_sequence == prediction.source_capture_sequence
            ),
            None,
        )
        if trigger is None:
            trigger = min(
                self._samples,
                key=lambda sample: abs(
                    sample.captured_monotonic_ns
                    - prediction.source_captured_monotonic_ns
                ),
            )
            if abs(
                trigger.captured_monotonic_ns
                - prediction.source_captured_monotonic_ns
            ) > int(1.1 * self.sample_interval_s * 1_000_000_000):
                self._last_invalid_reason = "trigger_frame_not_in_sample_window"
                return
        trigger_ns = prediction.source_captured_monotonic_ns
        start_ns = trigger_ns - int(self.pre_window_s * 1_000_000_000)
        end_ns = trigger_ns + int(self.post_window_s * 1_000_000_000)
        captures = [
            sample for sample in self._samples
            if start_ns <= sample.captured_monotonic_ns <= end_ns
        ]
        pending = _PendingReconstructionEvent(
            prediction=prediction,
            previous_label=previous_label,
            new_label=new_label,
            change_score=score,
            trigger_capture=trigger,
            captures=captures,
            end_monotonic_ns=end_ns,
        )
        self._pending.append(pending)
        if self._last_sample_ns >= end_ns:
            self._complete_pending(pending)

    def _advance_pending(self, sample: CapturedFrameSample) -> None:
        tolerance_ns = int(self.sample_interval_s * 500_000_000)
        for pending in tuple(self._pending):
            if (
                sample.captured_monotonic_ns
                <= pending.end_monotonic_ns + tolerance_ns
                and all(
                    prior.capture_sequence != sample.capture_sequence
                    for prior in pending.captures
                )
            ):
                pending.captures.append(sample)
            if sample.captured_monotonic_ns >= pending.end_monotonic_ns:
                self._complete_pending(pending)

    def _complete_pending(self, pending: _PendingReconstructionEvent) -> None:
        if pending not in self._pending:
            return
        self._pending.remove(pending)
        captures = tuple(sorted(
            pending.captures,
            key=lambda sample: sample.captured_monotonic_ns,
        ))
        if not captures:
            return
        self.event_ready.emit(ReconstructionChangeEvent(
            prediction=pending.prediction,
            previous_label=pending.previous_label,
            new_label=pending.new_label,
            change_score=pending.change_score,
            trigger_capture=pending.trigger_capture,
            captures=captures,
            pre_window_s=self.pre_window_s,
            post_window_s=self.post_window_s,
            sample_interval_s=self.sample_interval_s,
        ))

    def _prediction_matches_capture_context(
        self,
        prediction: ReconstructionPrediction,
    ) -> bool:
        if self._capture_context is None:
            return False
        _session, _backend, _hwnd, _geometry, view, generation, _width, _height = (
            self._capture_context
        )
        if prediction.view_segment_id != view:
            return False
        return prediction.visual_history_generation == generation

    @staticmethod
    def _capture_context_token(metadata: Mapping[str, object]) -> tuple:
        return (
            str(metadata.get("session_id") or ""),
            str(metadata.get("capture_backend") or ""),
            int(metadata.get("source_hwnd") or 0),
            str(metadata.get("capture_geometry_id") or ""),
            metadata.get("view_segment_id"),
            int(metadata.get("visual_history_generation") or 0),
            int(metadata.get("camera_width") or 0),
            int(metadata.get("camera_height") or 0),
        )

    def _clear_candidate(self) -> None:
        self._candidate_label = ""
        self._candidate_count = 0
        self._candidate_first = None


# Legacy pixel-driven engine retained for offline comparison.
class AutoCaptureEngine(QObject):
    """Evaluates each camera frame and emits *frame_captured* when a
    significant change is detected (after debounce, respecting cooldown)."""

    frame_captured = pyqtSignal(np.ndarray, float)  # (frame, change_score)

    def __init__(
        self,
        threshold: float = 0.20,
        cooldown_s: float = 5.0,
        warmup_frames: int = 30,
        context_buffer_size: int = 20,
        adaptive_sigma: float | None = None,
        adaptive_history: int = 100,
        adaptive_warmup: int = 20,
        adaptive_floor: float = 1.0,
        suppress_events_during_adaptive_warmup: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._detector: ChangeDetector = IntensityChangeDetector()
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._warmup_frames = warmup_frames

        self._frame_count = 0
        self._last_capture_time = 0.0
        self._enabled = False
        self._debounce_count = 0
        self._debounce_required = 3  # consecutive frames above threshold
        self._latest_score = 0.0

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

    def set_detector(self, detector: ChangeDetector) -> None:
        """Swap in a different detection strategy (Tier 2/3)."""
        self._detector = detector

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
        self._latest_score = 0.0
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
        self._context_buffer.append(
            (frame.copy(), dict(capture_metadata or {})),
        )

        self._frame_count += 1
        if self._frame_count <= self._warmup_frames:
            self._detector.compute_score(frame)  # feed baseline
            self._latest_score = 0.0
            return

        score = self._detector.compute_score(frame)
        self._latest_score = score
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
        if in_adaptive_warmup:
            self._baseline_scores.append(score)
            return

        threshold = self.effective_threshold
        if score >= threshold:
            self._debounce_count += 1
        else:
            self._debounce_count = 0
            # Only non-flagged scores feed the adaptive baseline — events
            # would pollute the rolling mean and pull the threshold up
            # behind their own peak. The fixed-mode path appends too,
            # which is harmless (the deque is just unused).
            self._baseline_scores.append(score)

        if (
            self._debounce_count >= self._debounce_required
            and (now - self._last_capture_time) >= self._cooldown_s
        ):
            self._last_capture_time = now
            self._debounce_count = 0
            self.frame_captured.emit(frame.copy(), score)
