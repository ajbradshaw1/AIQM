#!/usr/bin/env python3
"""Offline O-MBE RHEED signal validation on WGC-provenance frame runs.

This script never connects to kSA, a camera, or an MBE control interface.
Inputs must be external WGC probe directories captured with ``--save-every 1``
so detector cadence and image provenance stay auditable.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import gui.auto_capture as auto_capture_module  # noqa: E402
from gui.auto_capture import AutoCaptureEngine, PixelDiffChangeDetector  # noqa: E402
from gui.growth_app import (  # noqa: E402
    AUTO_CAPTURE_ADAPTIVE_FLOOR,
    AUTO_CAPTURE_ADAPTIVE_SIGMA,
    AUTO_CAPTURE_BUFFER_SIZE,
    AUTO_CAPTURE_COOLDOWN_S,
    AUTO_CAPTURE_THRESHOLD,
)
from gui.specular import detect_specular, image_derived_roi  # noqa: E402

SAMPLE_PATTERN = re.compile(r"^sample_(\d+)_cropped$")
CLASSIFIER_OOD_QUALITY = 0.3


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[round((len(ordered) - 1) * fraction)])


def describe(values: Iterable[float]) -> dict[str, float | int | None]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "n": len(finite),
        "mean": statistics.fmean(finite) if finite else None,
        "median": statistics.median(finite) if finite else None,
        "p95": percentile(finite, 0.95),
        "p99": percentile(finite, 0.99),
        "min": min(finite) if finite else None,
        "max": max(finite) if finite else None,
    }


def load_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def frame_metrics(frame: np.ndarray) -> dict[str, Any]:
    """Compute three intensity definitions and the image-derived ROI."""
    rgb = frame.astype(np.float32)
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    bt601 = 0.299 * red + 0.587 * green + 0.114 * blue
    x, y = detect_specular(green)
    roi = image_derived_roi(green, x, y)
    roi_green = green[roi]
    return {
        "bt601_full_mean": float(bt601.mean()),
        "green_full_mean": float(green.mean()),
        "specular_roi_green_mean": float(roi_green.mean()),
        "specular_x": x,
        "specular_y": y,
        "roi_y0": roi[0].start,
        "roi_y1": roi[0].stop,
        "roi_x0": roi[1].start,
        "roi_x1": roi[1].stop,
        "roi_pixels": int(roi_green.size),
    }


def discover_frames(directory: Path) -> list[Path]:
    """Return only canonical WGC probe crops, ordered by read index."""
    candidates: list[tuple[int, Path]] = []
    for path in directory.rglob("*_cropped.*"):
        match = SAMPLE_PATTERN.match(path.stem)
        if path.is_file() and match:
            candidates.append((int(match.group(1)), path))
    return [path for _index, path in sorted(candidates)]


def parse_input(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        raise ValueError("--input must be CONDITION=PATH")
    condition, raw_path = spec.split("=", 1)
    condition = condition.strip()
    path = Path(raw_path).expanduser()
    if not condition:
        raise ValueError("input condition cannot be empty")
    if not path.is_dir():
        raise ValueError(f"input directory does not exist: {path}")
    return condition, path


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _sample_index(path: Path) -> int:
    match = SAMPLE_PATTERN.match(path.stem)
    if not match:
        raise ValueError(f"not a canonical WGC crop: {path}")
    return int(match.group(1))


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read required WGC evidence {path}: {exc}") from exc


def load_wgc_provenance(
    directory: Path,
    frames: list[Path],
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Validate one-to-one crop/raw/metadata association and 1 Hz evidence."""
    summary_path = directory / "summary.json"
    metadata_path = directory / "capture_metadata.json"
    source_manifest_path = directory / "sha256_manifest.json"
    summary = _load_json(summary_path)
    metadata_rows = _load_json(metadata_path)
    source_manifest = _load_json(source_manifest_path)
    save_every = summary.get("configuration", {}).get("save_every")
    if save_every != 1:
        raise ValueError(
            f"{directory} was not captured with --save-every 1 "
            f"(summary reports {save_every!r})"
        )
    if summary.get("configuration", {}).get("capture_backend") != "wgc":
        raise ValueError(f"{directory} is not a WGC capture run")

    by_index = {int(row["read_index"]): dict(row) for row in metadata_rows}
    source_hashes = {
        str(row["path"]): str(row["sha256"]) for row in source_manifest
    }
    for frame in frames:
        index = _sample_index(frame)
        if index not in by_index:
            raise ValueError(f"missing capture metadata for read_index={index}")
        row = by_index[index]
        required = (
            "captured_at_utc",
            "captured_monotonic_ns",
            "capture_sequence",
            "source_hwnd",
        )
        missing = [key for key in required if row.get(key) in (None, "")]
        if missing:
            raise ValueError(
                f"capture metadata read_index={index} lacks {missing}"
            )
        raw_path = directory / f"sample_{index:05d}_raw.png"
        if not raw_path.is_file():
            raise ValueError(f"missing paired raw WGC frame: {raw_path}")
        for source_path in (frame, raw_path):
            expected = source_hashes.get(source_path.name)
            actual = sha256_file(source_path)
            if expected is None or actual != expected:
                raise ValueError(
                    f"source hash missing or mismatched for {source_path}"
                )
        row.update({
            "raw_path": str(raw_path.resolve()),
            "raw_sha256": source_hashes[raw_path.name],
            "cropped_sha256": source_hashes[frame.name],
        })
    return by_index, {
        "capture_commit": summary.get("git_commit", ""),
        "source_summary_path": str(summary_path.resolve()),
        "source_summary_sha256": sha256_file(summary_path),
        "capture_metadata_path": str(metadata_path.resolve()),
        "capture_metadata_sha256": sha256_file(metadata_path),
        "source_manifest_path": str(source_manifest_path.resolve()),
        "source_manifest_sha256": sha256_file(source_manifest_path),
        "interval_s": summary.get("configuration", {}).get("interval_s"),
        "save_every": save_every,
    }


def _new_gui_engine() -> AutoCaptureEngine:
    engine = AutoCaptureEngine(
        threshold=AUTO_CAPTURE_THRESHOLD,
        cooldown_s=AUTO_CAPTURE_COOLDOWN_S,
        warmup_frames=AUTO_CAPTURE_BUFFER_SIZE,
        adaptive_sigma=AUTO_CAPTURE_ADAPTIVE_SIGMA,
        adaptive_floor=AUTO_CAPTURE_ADAPTIVE_FLOOR,
    )
    engine.set_detector(PixelDiffChangeDetector(
        buffer_size=AUTO_CAPTURE_BUFFER_SIZE,
        score_metric="std",
        roi_mode="specular",
    ))
    engine.enabled = True
    return engine


def analyze_condition(
    condition: str,
    directory: Path,
    classifier=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    frames = discover_frames(directory)
    provenance, source_identity = load_wgc_provenance(directory, frames)
    diagnostic_detector = PixelDiffChangeDetector(
        buffer_size=AUTO_CAPTURE_BUFFER_SIZE,
        smooth_window=3,
        score_metric="std",
        roi_mode="specular",
    )
    engine = _new_gui_engine()
    event_indices: list[int] = []
    active_index = [-1]
    engine.frame_captured.connect(
        lambda _frame, _score: event_indices.append(active_index[0]),
    )
    rows: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    replay_clock = [0.0]
    real_time = auto_capture_module.time.time
    auto_capture_module.time.time = lambda: replay_clock[0]
    try:
        for frame_index, path in enumerate(frames):
            read_index = _sample_index(path)
            source = provenance[read_index]
            replay_clock[0] = int(source["captured_monotonic_ns"]) / 1e9
            active_index[0] = frame_index
            frame = load_rgb(path)
            diagnostic_score = float(diagnostic_detector.compute_score(frame))
            prior_events = len(event_indices)
            engine.evaluate(frame)
            row: dict[str, Any] = {
                "condition": condition,
                "frame_index": frame_index,
                "read_index": read_index,
                "path": str(path.resolve()),
                "capture_backend": source.get("capture_backend", "wgc"),
                "captured_at_utc": source["captured_at_utc"],
                "captured_monotonic_ns": source["captured_monotonic_ns"],
                "capture_sequence": source["capture_sequence"],
                "capture_gap_ms": source.get("capture_gap_ms"),
                "frame_age_ms": source.get("frame_age_ms"),
                "source_hwnd": source["source_hwnd"],
                "raw_path": source["raw_path"],
                "raw_sha256": source["raw_sha256"],
                "cropped_sha256": source["cropped_sha256"],
                **frame_metrics(frame),
                "diagnostic_change_score": diagnostic_score,
                "gui_change_score": float(engine.latest_score),
                "gui_effective_threshold": float(engine.effective_threshold),
                "gui_event_triggered": len(event_indices) > prior_events,
                "gui_warmup": frame_index < AUTO_CAPTURE_BUFFER_SIZE,
            }
            if classifier is not None:
                result = classifier.classify(frame)
                row.update({
                    "classifier_quality": result.get("quality"),
                    "classifier_is_bad": result.get("is_bad"),
                    "classifier_bad_confidence": result.get("bad_confidence"),
                    "classifier_predicted_class": result.get("predicted_class"),
                    "classifier_scores_json": json.dumps(
                        result.get("classification_scores", {}),
                        sort_keys=True,
                    ),
                })
            rows.append(row)
            manifest.extend([
                {
                    "condition": condition,
                    "kind": "cropped",
                    "read_index": read_index,
                    "path": str(path.resolve()),
                    "bytes": path.stat().st_size,
                    "sha256": source["cropped_sha256"],
                    "capture_sequence": source["capture_sequence"],
                },
                {
                    "condition": condition,
                    "kind": "raw",
                    "read_index": read_index,
                    "path": source["raw_path"],
                    "bytes": Path(source["raw_path"]).stat().st_size,
                    "sha256": source["raw_sha256"],
                    "capture_sequence": source["capture_sequence"],
                },
            ])
    finally:
        auto_capture_module.time.time = real_time
    return rows, manifest, source_identity


def _classifier_summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    classified = [
        row for row in rows if "classifier_predicted_class" in row
    ]
    if not classified:
        return None
    predictions = [str(row["classifier_predicted_class"]) for row in classified]
    quality = [
        float(row["classifier_quality"])
        for row in classified if row.get("classifier_quality") is not None
    ]
    counts: dict[str, int] = {}
    for prediction in predictions:
        counts[prediction] = counts.get(prediction, 0) + 1
    flips = sum(a != b for a, b in zip(predictions, predictions[1:]))
    score_vectors = [
        json.loads(str(row["classifier_scores_json"])) for row in classified
    ]
    score_l1 = []
    for previous, current in zip(score_vectors, score_vectors[1:]):
        keys = set(previous) | set(current)
        score_l1.append(sum(
            abs(float(current.get(key, 0.0)) - float(previous.get(key, 0.0)))
            for key in keys
        ))
    return {
        "quality": describe(quality),
        "ood_proxy_threshold": CLASSIFIER_OOD_QUALITY,
        "ood_proxy_count": sum(value < CLASSIFIER_OOD_QUALITY for value in quality),
        "is_bad_count": sum(bool(row.get("classifier_is_bad")) for row in classified),
        "predicted_class_counts": counts,
        "prediction_flips": flips,
        "prediction_flip_rate": flips / max(1, len(predictions) - 1),
        "successive_score_l1": describe(score_l1),
    }


def build_summary(
    rows: list[dict[str, Any]],
    stable_conditions: set[str],
) -> dict[str, Any]:
    by_condition: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_condition.setdefault(str(row["condition"]), []).append(row)

    conditions: dict[str, Any] = {}
    for condition, condition_rows in by_condition.items():
        conditions[condition] = {
            metric: describe(float(row[metric]) for row in condition_rows)
            for metric in (
                "bt601_full_mean",
                "green_full_mean",
                "specular_roi_green_mean",
                "diagnostic_change_score",
                "gui_change_score",
            )
        }
        conditions[condition].update({
            "gui_event_count": sum(
                bool(row["gui_event_triggered"]) for row in condition_rows
            ),
            "naive_current_threshold_crossings": sum(
                float(row["diagnostic_change_score"]) >= AUTO_CAPTURE_THRESHOLD
                for row in condition_rows[AUTO_CAPTURE_BUFFER_SIZE:]
            ),
            "duplicate_or_reversed_capture_sequences": sum(
                int(current["capture_sequence"])
                <= int(previous["capture_sequence"])
                for previous, current in zip(
                    condition_rows, condition_rows[1:],
                )
            ),
            "capture_gap_ms": describe(
                float(row["capture_gap_ms"])
                for row in condition_rows
                if row.get("capture_gap_ms") is not None
            ),
            "classifier": _classifier_summary(condition_rows),
        })

    stable_scores = [
        float(row["diagnostic_change_score"])
        for row in rows
        if str(row["condition"]) in stable_conditions
        and int(row["frame_index"]) >= AUTO_CAPTURE_BUFFER_SIZE
    ]
    stable_p99 = percentile(stable_scores, 0.99)
    candidate = (
        max(AUTO_CAPTURE_ADAPTIVE_FLOOR, 1.2 * stable_p99)
        if stable_p99 is not None else None
    )
    return {
        "conditions": conditions,
        "stable_conditions": sorted(stable_conditions),
        "stable_change_score_p99": stable_p99,
        "diagnostic_candidate_threshold": candidate,
        "candidate_rule": (
            f"max({AUTO_CAPTURE_ADAPTIVE_FLOOR}, "
            "1.2 * stable diagnostic change-score p99)"
        ),
        "candidate_limitation": (
            "Noise-floor suggestion only. It is not a production default and "
            "requires full-policy replay against independently labeled natural "
            "changes before deployment."
        ),
        "current_gui_policy": {
            "threshold": AUTO_CAPTURE_THRESHOLD,
            "buffer_and_initial_warmup_frames": AUTO_CAPTURE_BUFFER_SIZE,
            "adaptive_sigma": AUTO_CAPTURE_ADAPTIVE_SIGMA,
            "adaptive_floor": AUTO_CAPTURE_ADAPTIVE_FLOOR,
            "adaptive_warmup_frames": 20,
            "debounce_frames": 3,
            "cooldown_s": AUTO_CAPTURE_COOLDOWN_S,
            "detector": "PixelDiff std, specular ROI, smooth_window=3",
        },
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _classifier_identity(classifier: Any) -> dict[str, Any] | None:
    if classifier is None:
        return None
    model_path = getattr(classifier, "model_path", None)
    if model_path is None:
        return {"checkpoint_path": "", "checkpoint_sha256": ""}
    path = Path(model_path)
    return {
        "checkpoint_path": str(path.resolve()),
        "checkpoint_sha256": sha256_file(path) if path.is_file() else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", action="append", required=True, metavar="CONDITION=PATH",
    )
    parser.add_argument("--stable-condition", action="append", default=[])
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("logs") / "validation" / f"rheed_signal_{tag}",
    )
    parser.add_argument("--run-classifier", action="store_true")
    parser.add_argument(
        "--surface", choices=("bare-sto", "other"), default="other",
        help="Classifier execution is permitted only for confirmed bare STO.",
    )
    parser.add_argument("--classifier-root", type=Path)
    args = parser.parse_args()

    if args.run_classifier and args.surface != "bare-sto":
        parser.error("--run-classifier requires --surface bare-sto")
    if args.run_classifier and args.classifier_root is None:
        parser.error("--run-classifier requires --classifier-root")

    classifier = None
    if args.run_classifier:
        from gui.classifier_bridge import ClassifierBridge
        classifier = ClassifierBridge(args.classifier_root)

    args.output_dir.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    inputs = [parse_input(spec) for spec in args.input]
    source_identities: list[dict[str, Any]] = []
    for condition, directory in inputs:
        new_rows, new_manifest, source_identity = analyze_condition(
            condition, directory, classifier=classifier,
        )
        rows.extend(new_rows)
        manifest.extend(new_manifest)
        source_identities.append({
            "condition": condition,
            "path": str(directory.resolve()),
            **source_identity,
        })
    if not rows:
        raise SystemExit("No canonical WGC cropped frames found.")

    stable = set(args.stable_condition)
    unknown_stable = stable - {condition for condition, _ in inputs}
    if unknown_stable:
        parser.error(
            "Unknown --stable-condition values: "
            + ", ".join(sorted(unknown_stable))
        )

    summary = {
        "created_at_utc": utc_now(),
        "analysis_commit": _git_commit(),
        "surface": args.surface,
        "classifier_enabled": bool(classifier),
        "classifier_identity": _classifier_identity(classifier),
        "inputs": source_identities,
        "frame_count": len(rows),
        **build_summary(rows, stable),
    }
    write_csv(args.output_dir / "frame_metrics.csv", rows)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    (args.output_dir / "input_sha256_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
