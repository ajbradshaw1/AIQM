#!/usr/bin/env python3
"""Replay the production translation-insensitive RHEED detector on a session ZIP.

This is a validation utility, not an annotation generator.  It streams the
saved heartbeat frames in acquisition order, records every registration and
score diagnostic, and applies the same warmup, adaptive threshold, debounce,
elapsed-time cooldown, and below-threshold rearm policy as the live Growth
Monitor.  No archive member is extracted or modified.

The heartbeat cadence is whatever the session recorded.  A replay at 1 Hz is
therefore not interchangeable with a future 10 Hz live camera stream; the
output manifest states the observed cadence and must be interpreted as a
session-specific detector audit.
"""

from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import math
import statistics
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gui.auto_capture import TranslationInvariantChangeDetector  # noqa: E402


UNAVAILABLE_SCORE_STATUSES = frozenset({
    "invalid_frame", "shape_reset", "low_texture", "insufficient_overlap",
    "excessive_shift", "frame_error",
})


SCORE_FIELDS = (
    "frame_index", "heartbeat_idx", "elapsed_s", "captured_at_utc",
    "capture_sequence", "frame_name", "detector_status", "score",
    "raw_score", "shift_y_px", "shift_x_px", "registration_confidence",
    "phase_peak_prominence", "overlap_fraction", "effective_threshold",
    "suppressed", "above_threshold", "trigger_armed",
    "below_threshold_count", "rearmed", "triggered", "error",
)


def _one_member(names: Iterable[str], suffix: str) -> str:
    matches = [name for name in names if name.replace("\\", "/").endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one ZIP member ending {suffix!r}; found {len(matches)}"
        )
    return matches[0]


def _read_csv(archive: zipfile.ZipFile, member: str) -> list[dict[str, str]]:
    text = io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", newline="")
    try:
        return list(csv.DictReader(text))
    finally:
        text.close()


def _frame_member(
    available: set[str], *, heartbeat_member: str, frame_path: str,
) -> str:
    session_root = PurePosixPath(heartbeat_member).parent
    basename = PurePosixPath(str(frame_path).replace("\\", "/")).name
    candidate = (session_root / "frames" / basename).as_posix()
    if candidate not in available:
        raise FileNotFoundError(f"Heartbeat frame is missing from ZIP: {candidate}")
    return candidate


def _load_rgb(archive: zipfile.ZipFile, member: str) -> np.ndarray:
    with archive.open(member) as stream:
        payload = stream.read()
    with Image.open(io.BytesIO(payload)) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def _finite(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def replay(
    session_zip: Path,
    *,
    output_dir: Path,
    limit: int | None = None,
    fixed_threshold: float = 2.0,
    adaptive_sigma: float = 3.0,
    adaptive_floor: float = 0.5,
    detector_warmup_frames: int = 20,
    adaptive_warmup_frames: int = 20,
    adaptive_history: int = 100,
    debounce_frames: int = 3,
    cooldown_s: float = 10.0,
    rearm_below_frames: int = 3,
) -> dict[str, Any]:
    """Stream a session ZIP and write a deterministic detector audit."""

    session_zip = session_zip.resolve()
    output_dir = output_dir.resolve()
    if not session_zip.is_file():
        raise FileNotFoundError(session_zip)
    if rearm_below_frames < 1:
        raise ValueError("rearm_below_frames must be at least 1")
    if output_dir == session_zip.parent or output_dir == REPO_ROOT:
        raise ValueError("Choose a dedicated output directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    score_path = output_dir / "translation_change_scores.csv"
    summary_path = output_dir / "translation_change_summary.json"

    detector = TranslationInvariantChangeDetector()
    baseline: collections.deque[float] = collections.deque(maxlen=adaptive_history)
    debounce_count = 0
    below_threshold_count = 0
    trigger_armed = True
    last_trigger_elapsed = -math.inf
    trigger_elapsed: list[float] = []
    intervals: list[float] = []
    status_counts: collections.Counter[str] = collections.Counter()
    processed_scores: list[float] = []

    with zipfile.ZipFile(session_zip, "r") as archive:
        names = archive.namelist()
        available = set(names)
        heartbeat_member = _one_member(names, "/heartbeat_log.csv")
        rows = _read_csv(archive, heartbeat_member)
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        if not rows:
            raise ValueError("Heartbeat log has no rows to replay")

        auto_rows: list[dict[str, str]] = []
        auto_members = [
            name for name in names
            if name.replace("\\", "/").endswith("/auto_capture_events.csv")
        ]
        if len(auto_members) == 1:
            auto_rows = _read_csv(archive, auto_members[0])

        with open(score_path, "w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=SCORE_FIELDS)
            writer.writeheader()
            previous_elapsed: float | None = None
            for index, row in enumerate(rows):
                elapsed = _finite(row.get("elapsed_s"), float(index))
                if previous_elapsed is not None and elapsed <= previous_elapsed:
                    raise ValueError("Heartbeat elapsed_s must be strictly increasing")
                if previous_elapsed is not None:
                    intervals.append(elapsed - previous_elapsed)
                previous_elapsed = elapsed
                frame_name = PurePosixPath(
                    str(row.get("frame_path") or "").replace("\\", "/")
                ).name

                error = ""
                try:
                    member = _frame_member(
                        available,
                        heartbeat_member=heartbeat_member,
                        frame_path=str(row.get("frame_path") or ""),
                    )
                    frame = _load_rgb(archive, member)
                    score = float(detector.compute_score(frame))
                    diagnostics = detector.last_diagnostics
                except Exception as exc:
                    score = 0.0
                    diagnostics = {
                        "status": "frame_error", "score": 0.0,
                        "raw_score": 0.0,
                    }
                    error = f"{type(exc).__name__}: {exc}"

                processed_scores.append(score)
                status = str(diagnostics.get("status") or "unknown")
                score_available = status not in UNAVAILABLE_SCORE_STATUSES
                status_counts[status] += 1
                suppressed = ""
                above = False
                rearmed = False
                triggered = False

                if index < detector_warmup_frames:
                    threshold = fixed_threshold
                    suppressed = "fixed_warmup"
                elif len(baseline) < adaptive_warmup_frames:
                    threshold = fixed_threshold
                    if score_available:
                        baseline.append(score)
                        suppressed = "adaptive_warmup"
                    else:
                        suppressed = "unavailable_for_adaptive_baseline"
                else:
                    values = np.asarray(baseline, dtype=np.float64)
                    threshold = max(
                        adaptive_floor,
                        float(values.mean() + adaptive_sigma * values.std()),
                    )
                    above = score_available and score >= threshold
                    if not score_available:
                        debounce_count = 0
                        below_threshold_count = 0
                        suppressed = "unavailable_score"
                    elif above:
                        below_threshold_count = 0
                        if trigger_armed:
                            debounce_count += 1
                        else:
                            debounce_count = 0
                            suppressed = "waiting_for_rearm"
                    else:
                        debounce_count = 0
                        baseline.append(score)
                        if not trigger_armed:
                            below_threshold_count += 1
                            if below_threshold_count >= rearm_below_frames:
                                trigger_armed = True
                                below_threshold_count = 0
                                rearmed = True
                    if (
                        trigger_armed
                        and debounce_count >= debounce_frames
                        and elapsed - last_trigger_elapsed >= cooldown_s
                    ):
                        triggered = True
                        trigger_elapsed.append(elapsed)
                        last_trigger_elapsed = elapsed
                        debounce_count = 0
                        below_threshold_count = 0
                        trigger_armed = False

                writer.writerow({
                    "frame_index": index,
                    "heartbeat_idx": row.get("heartbeat_idx", ""),
                    "elapsed_s": f"{elapsed:.6f}",
                    "captured_at_utc": row.get("captured_at_utc", ""),
                    "capture_sequence": row.get("capture_sequence", ""),
                    "frame_name": frame_name,
                    "detector_status": status,
                    "score": f"{score:.8f}",
                    "raw_score": f"{_finite(diagnostics.get('raw_score')):.8f}",
                    "shift_y_px": diagnostics.get("shift_y_px", ""),
                    "shift_x_px": diagnostics.get("shift_x_px", ""),
                    "registration_confidence": diagnostics.get(
                        "registration_confidence", "",
                    ),
                    "phase_peak_prominence": diagnostics.get(
                        "phase_peak_prominence", "",
                    ),
                    "overlap_fraction": diagnostics.get("overlap_fraction", ""),
                    "effective_threshold": f"{threshold:.8f}",
                    "suppressed": suppressed,
                    "above_threshold": int(above),
                    "trigger_armed": int(trigger_armed),
                    "below_threshold_count": below_threshold_count,
                    "rearmed": int(rearmed),
                    "triggered": int(triggered),
                    "error": error,
                })

    existing_elapsed = sorted(
        _finite(row.get("elapsed_s"), math.nan)
        for row in auto_rows
        if math.isfinite(_finite(row.get("elapsed_s"), math.nan))
    )
    nearest_deltas = [
        min((abs(candidate - trigger) for trigger in trigger_elapsed), default=math.inf)
        for candidate in existing_elapsed
    ]
    finite_scores = [value for value in processed_scores if math.isfinite(value)]
    summary: dict[str, Any] = {
        "schema": "rheed-translation-change-replay-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "session_zip": session_zip.name,
        "heartbeat_frame_count": len(processed_scores),
        "observed_cadence_s": {
            "median": statistics.median(intervals) if intervals else None,
            "p95": float(np.percentile(intervals, 95)) if intervals else None,
            "maximum": max(intervals) if intervals else None,
        },
        "detector": {
            "name": "TranslationInvariantChangeDetector",
            "fixed_threshold": fixed_threshold,
            "adaptive_sigma": adaptive_sigma,
            "adaptive_floor": adaptive_floor,
            "detector_warmup_frames": detector_warmup_frames,
            "adaptive_warmup_frames": adaptive_warmup_frames,
            "adaptive_history": adaptive_history,
            "debounce_frames": debounce_frames,
            "cooldown_s": cooldown_s,
            "rearm_below_frames": rearm_below_frames,
        },
        "scores": {
            "median": statistics.median(finite_scores) if finite_scores else None,
            "p95": float(np.percentile(finite_scores, 95)) if finite_scores else None,
            "p99": float(np.percentile(finite_scores, 99)) if finite_scores else None,
            "maximum": max(finite_scores) if finite_scores else None,
        },
        "status_counts": dict(sorted(status_counts.items())),
        "replay_trigger_count": len(trigger_elapsed),
        "replay_trigger_elapsed_s": trigger_elapsed,
        "legacy_detector_candidate_count": len(existing_elapsed),
        "legacy_candidates_within_5s_of_replay_trigger": sum(
            delta <= 5.0 for delta in nearest_deltas
        ),
        "legacy_candidates_within_10s_of_replay_trigger": sum(
            delta <= 10.0 for delta in nearest_deltas
        ),
        "interpretation": (
            "Detector audit only. Existing candidates were produced by an older "
            "algorithm, and correlated saved frames are not independent labels."
        ),
        "outputs": {"scores_csv": score_path.name},
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_zip", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fixed-threshold", type=float, default=2.0)
    parser.add_argument("--adaptive-sigma", type=float, default=3.0)
    parser.add_argument("--adaptive-floor", type=float, default=0.5)
    parser.add_argument("--rearm-below-frames", type=int, default=3)
    args = parser.parse_args()
    summary = replay(
        args.session_zip,
        output_dir=args.output_dir,
        limit=args.limit,
        fixed_threshold=args.fixed_threshold,
        adaptive_sigma=args.adaptive_sigma,
        adaptive_floor=args.adaptive_floor,
        rearm_below_frames=args.rearm_below_frames,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
