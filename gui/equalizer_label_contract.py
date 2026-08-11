"""Strict, model-independent contract for Equalizer measurement records.

Equalizer coefficients describe a visual basis fit.  They are not
Classifier2 win rates, class probabilities, or human reconstruction labels.
This module keeps that distinction explicit at the GUI/logging boundary.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping

import numpy as np


ACTIVE_LABELS = ("1x1", "Tw(2x1)", "c(6x2)", "RT13")
CSV_SUFFIX = {
    "1x1": "1x1",
    "Tw(2x1)": "tw",
    "c(6x2)": "c6x2",
    "RT13": "rt13",
}
FIT_MODES = frozenset({"manual", "least_squares", "least_squares_then_manual"})


def frame_rgb_sha256(rgb: np.ndarray) -> str:
    """Hash exact decoded RGB pixels with an unambiguous shape/dtype prefix."""
    array = np.ascontiguousarray(rgb)
    digest = hashlib.sha256()
    digest.update(str(array.shape).encode("ascii"))
    digest.update(array.dtype.str.encode("ascii"))
    digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


def normalize_active_weights(
    weights: Mapping[str, object],
) -> dict[str, float | None]:
    """Return a normalized copy without inventing a uniform fallback."""
    values: dict[str, float] = {}
    for label in ACTIVE_LABELS:
        value = float(weights.get(label, 0.0) or 0.0)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"Equalizer final weight for {label} must be in [0, 1]")
        values[label] = value
    total = sum(values.values())
    normalized: dict[str, float | None]
    if total <= 0.0:
        normalized = {label: None for label in ACTIVE_LABELS}
    else:
        normalized = {label: value / total for label, value in values.items()}
    normalized["HTR"] = None
    return normalized


def fit_residual_rms(
    basis: Mapping[str, np.ndarray],
    target: np.ndarray,
    weights: Mapping[str, object],
    valid_mask: np.ndarray,
) -> float:
    """Pixel-intensity RMS of the saved final mixture on the valid warp mask."""
    target_array = np.asarray(target, dtype=np.float32)
    mask = np.asarray(valid_mask, dtype=bool)
    if target_array.ndim != 2 or mask.shape != target_array.shape or not np.any(mask):
        raise ValueError("Equalizer residual requires a non-empty target-shaped mask")
    reconstructed = np.zeros_like(target_array)
    for label in ACTIVE_LABELS:
        image = np.asarray(basis[label], dtype=np.float32)
        if image.shape != target_array.shape:
            raise ValueError("Equalizer basis and target shapes differ")
        reconstructed += float(weights.get(label, 0.0) or 0.0) * image
    return float(np.sqrt(np.mean(np.square(reconstructed[mask] - target_array[mask]))))


def build_equalizer_payload(
    *,
    raw_weights: Mapping[str, object] | None,
    final_weights: Mapping[str, object],
    fit_mode: str,
    normalization_applied: bool,
    residual_rms: float,
    valid_coverage: float,
    confidence: object = "",
) -> dict[str, object]:
    """Build and validate one versioned Equalizer measurement payload."""
    if fit_mode not in FIT_MODES:
        raise ValueError(f"Unsupported Equalizer fit mode {fit_mode!r}")
    final: dict[str, float | None] = {}
    for label in ACTIVE_LABELS:
        value = float(final_weights.get(label, 0.0) or 0.0)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"Equalizer final weight for {label} must be in [0, 1]")
        final[label] = value
    final["HTR"] = None

    raw: dict[str, float | None] = {}
    for label in ACTIVE_LABELS:
        value = None if raw_weights is None else raw_weights.get(label)
        if value is None:
            raw[label] = None
            continue
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0.0:
            raise ValueError(f"Equalizer raw coefficient for {label} must be non-negative")
        raw[label] = numeric
    raw["HTR"] = None

    normalized = normalize_active_weights(final)
    total = sum(float(final[label] or 0.0) for label in ACTIVE_LABELS)
    residual = float(residual_rms)
    coverage = float(valid_coverage)
    if not math.isfinite(residual) or residual < 0.0:
        raise ValueError("Equalizer residual must be finite and non-negative")
    if not math.isfinite(coverage) or not 0.0 < coverage <= 1.0:
        raise ValueError("Equalizer valid coverage must be in (0, 1]")

    confidence_text = str(confidence or "").strip()
    if confidence_text:
        confidence_value = float(confidence_text)
        if not math.isfinite(confidence_value) or not 0.0 <= confidence_value <= 1.0:
            raise ValueError("Equalizer confidence must be in [0, 1]")
        confidence_text = f"{confidence_value:.3f}"

    available = [
        label for label in ACTIVE_LABELS if normalized[label] is not None
    ]
    argmax = (
        max(available, key=lambda label: float(normalized[label] or 0.0))
        if available else ""
    )
    source = {
        "manual": "equalizer_manual",
        "least_squares": "equalizer_auto_fit",
        "least_squares_then_manual": "equalizer_auto_fit_adjusted",
    }[fit_mode]
    return {
        "schema_version": 1,
        "label_source": source,
        "fit_mode": fit_mode,
        "normalization_applied": bool(normalization_applied),
        "final_is_normalized": bool(abs(total - 1.0) <= 0.011),
        "final_sum": total,
        "raw_weights": raw,
        "final_weights": final,
        "normalized_weights": normalized,
        "argmax": argmax,
        "residual_rms": residual,
        "valid_coverage": coverage,
        "confidence": confidence_text,
    }


def validate_equalizer_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Fail closed and return a canonical copy of an external payload."""
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ValueError("Equalizer payload schema_version must be 1")
    rebuilt = build_equalizer_payload(
        raw_weights=payload.get("raw_weights") if isinstance(payload.get("raw_weights"), Mapping) else None,
        final_weights=(
            payload["final_weights"]
            if isinstance(payload.get("final_weights"), Mapping)
            else {}
        ),
        fit_mode=str(payload.get("fit_mode", "")),
        normalization_applied=bool(payload.get("normalization_applied", False)),
        residual_rms=payload.get("residual_rms", float("nan")),
        valid_coverage=payload.get("valid_coverage", float("nan")),
        confidence=payload.get("confidence", ""),
    )
    for key in (
        "label_source", "final_is_normalized", "argmax",
    ):
        if payload.get(key) != rebuilt[key]:
            raise ValueError(f"Equalizer payload has inconsistent {key}")
    for weight_set in ("raw_weights", "final_weights", "normalized_weights"):
        supplied_set = payload.get(weight_set)
        if not isinstance(supplied_set, Mapping) or supplied_set.get("HTR") is not None:
            raise ValueError(f"Equalizer {weight_set} HTR must be null")
    supplied_normalized = payload.get("normalized_weights")
    if not isinstance(supplied_normalized, Mapping):
        raise ValueError("Equalizer normalized_weights are missing")
    for label in (*ACTIVE_LABELS, "HTR"):
        expected = rebuilt["normalized_weights"][label]
        supplied = supplied_normalized.get(label)
        if expected is None:
            if supplied is not None:
                raise ValueError(f"Equalizer normalized {label} must be null")
        elif supplied is None or abs(float(supplied) - float(expected)) > 1e-9:
            raise ValueError(f"Equalizer normalized {label} is inconsistent")
    return rebuilt


__all__ = [
    "ACTIVE_LABELS",
    "CSV_SUFFIX",
    "FIT_MODES",
    "build_equalizer_payload",
    "fit_residual_rms",
    "frame_rgb_sha256",
    "normalize_active_weights",
    "validate_equalizer_payload",
]
