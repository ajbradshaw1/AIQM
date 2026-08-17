"""Validated model specifications and precomputed prediction CSV input."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from .session_archive import FrameRecord, sha256_file


@dataclass(frozen=True)
class ModelSpec:
    path: Path
    key: str
    title: str
    classes: tuple[str, ...]
    probability_columns: tuple[str, ...]
    quality_column: str | None
    predicted_column: str | None
    subtitle: str
    status: str
    smoothing_window_s: float
    dwell_s: float
    provenance: dict[str, object]
    raw: dict[str, object]

    @classmethod
    def load(cls, path: str | Path) -> "ModelSpec":
        source = Path(path).resolve()
        raw = json.loads(source.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1:
            raise ValueError("Model spec schema_version must be 1")
        key = str(raw.get("key", "")).strip()
        title = str(raw.get("title", "")).strip()
        classes = tuple(str(item).strip() for item in raw.get("classes", []))
        columns = tuple(str(item).strip() for item in raw.get("probability_columns", []))
        if not key or not title or len(classes) < 2 or len(classes) != len(columns):
            raise ValueError("Model spec needs key/title and one ordered probability column per class")
        if any(not item for item in classes + columns) or len(set(classes)) != len(classes) or len(set(columns)) != len(columns):
            raise ValueError("Model classes and probability columns must be non-empty and unique")
        transition = raw.get("transition_rule", {}) or {}
        smoothing = float(raw.get("smoothing_window_s", transition.get("smoothing_window_s", 15.0)))
        dwell = float(raw.get("new_argmax_minimum_dwell_s", transition.get("new_argmax_minimum_dwell_s", 10.0)))
        if not math.isfinite(smoothing) or smoothing < 0 or not math.isfinite(dwell) or dwell < 0:
            raise ValueError("Smoothing and dwell values must be finite and non-negative")
        quality = raw.get("quality_column")
        predicted = raw.get("predicted_column")
        return cls(source, key, title, classes, columns,
                   str(quality) if quality else None, str(predicted) if predicted else None,
                   str(raw.get("subtitle", "")), str(raw.get("status", "")), smoothing, dwell,
                   dict(raw.get("provenance", {}) or {}), raw)


@dataclass(frozen=True)
class PredictionTable:
    path: Path
    sha256: str
    spec: ModelSpec
    probabilities: np.ndarray
    quality: np.ndarray


PROVENANCE_COLUMNS = ("frame_index", "heartbeat_idx", "elapsed_s", "captured_at_utc", "capture_sequence", "frame_name", "frame_sha256")


def load_predictions(path: str | Path, spec: ModelSpec, frames: Sequence[FrameRecord]) -> PredictionTable:
    source = Path(path).resolve()
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required_columns = PROVENANCE_COLUMNS + spec.probability_columns
        if spec.quality_column:
            required_columns += (spec.quality_column,)
        if spec.predicted_column:
            required_columns += (spec.predicted_column,)
        if reader.fieldnames is None or any(column not in reader.fieldnames for column in required_columns):
            raise ValueError(f"Prediction CSV columns do not satisfy model spec {spec.key}")
        rows = list(reader)
    if len(rows) != len(frames):
        raise ValueError(f"Prediction row count for {spec.key} does not match the session")
    probability = np.empty((len(rows), len(spec.classes)), dtype=np.float32)
    quality = np.zeros(len(rows), dtype=np.float32)
    index_base = int(rows[0]["frame_index"])
    if index_base not in {0, 1}:
        raise ValueError("Prediction frame_index must start at 0 or 1")
    for index, (row, frame) in enumerate(zip(rows, frames, strict=True)):
        expected = dict(frame.provenance())
        expected["frame_index"] = frame.frame_index + index_base
        observed = {
            "frame_index": int(row["frame_index"]), "heartbeat_idx": int(row["heartbeat_idx"]),
            "elapsed_s": float(row["elapsed_s"]), "captured_at_utc": row["captured_at_utc"].strip(),
            "capture_sequence": int(row["capture_sequence"]), "frame_name": row["frame_name"].strip(),
            "frame_sha256": row["frame_sha256"].strip().lower(),
        }
        if observed != expected:
            raise ValueError(f"Prediction provenance mismatch for {spec.key} at row {index + 2}")
        values = [float(row[column]) for column in spec.probability_columns]
        if any(not math.isfinite(value) or value < 0 or value > 1 for value in values):
            raise ValueError(f"Invalid probability for {spec.key} at frame {index}")
        probability[index] = values
        if spec.quality_column:
            value = float(row[spec.quality_column])
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"Invalid quality for {spec.key} at frame {index}")
            quality[index] = value
        if spec.predicted_column and row.get(spec.predicted_column, "").strip() not in spec.classes:
            raise ValueError(f"Unknown predicted class for {spec.key} at frame {index}")
    return PredictionTable(source, sha256_file(source), spec, probability, quality)
