#!/usr/bin/env python3
"""Distill a growth session's raw BMP frames into small, permanent artifacts.

Raw sessions are dominated by BMPs (~968 KB/frame). The scientific content
survives at ~1/11 the size losslessly, or ~1/300 at processing resolution.
This script extracts everything worth keeping so the raw frames can be
deleted:

    gray/                full-resolution LOSSLESS grayscale PNG
    thumb/               128x96 grayscale PNG (canonical processing size,
                         matches gui/equalizer_alignment.py PROCESS_WH)
    descriptors.npy      (N, 64) float32 peak-aware structural descriptors
    frame_manifest.csv   per-frame joined table: path + time + temperature
                         + capture provenance + causal history features
    csv/                 verbatim copy of every session CSV
    extraction_report.json

**Grayscale channel selection matters.** Frames from a Vimba deployment
predating commit ``da9a558`` carry intensity in the GREEN channel only
(R=B=0). ``PIL.convert('L')`` computes 0.299R+0.587G+0.114B, which clips
such frames at 0.587*255 ~= 150 -- a 41% undercount (see
``docs/ksa_palette_classifier_input.md``). This script detects the channel
layout per frame and extracts the channel that actually carries intensity,
recording which rule fired in the manifest so downstream consumers can
audit it.

**Peak-aware descriptor** (64-D) follows the physics-guided encoder of
Jiang et al., *Peak Sequence Transformer*, Cryst. Growth Des. 2026, 26,
5744: sum brightness down columns to get a 1-D profile, detect vertical
diffraction streaks by height and prominence, then walk each streak to
localize intensity maxima ("dots") and vectorize their brightness,
prominence, position and FWHM. Layout is written to the report.

History features are **prefix-only** (expanding window). Whole-trajectory
statistics would leak the future into any model trained on the output.

Usage:
    python scripts/extract_frame_descriptors.py SESSION_DIR OUT_DIR
    python scripts/extract_frame_descriptors.py SESSION_DIR OUT_DIR --workers 8
    python scripts/extract_frame_descriptors.py SESSION_DIR OUT_DIR --limit 200
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Optional

import numpy as np

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment guard
    print("error: Pillow is required (pip install Pillow)", file=sys.stderr)
    raise

try:
    from scipy.signal import find_peaks, peak_widths
except ImportError:  # pragma: no cover - environment guard
    print("error: scipy is required (pip install scipy)", file=sys.stderr)
    raise


THUMB_SIZE = (128, 96)          # matches gui/equalizer_alignment.py PROCESS_WH
MAX_DOTS = 14                   # per Jiang et al. for spot-dense patterns
DOTS_PER_FEATURE = 4            # brightness, prominence, position, fwhm
N_GLOBAL = 64 - MAX_DOTS * DOTS_PER_FEATURE   # -> 8 global features

# Detection thresholds, expressed relative to each frame so they survive
# exposure changes between chambers and sessions.
STREAK_HEIGHT_FRAC = 0.15       # of (max - median) of the column profile
STREAK_PROMINENCE_FRAC = 0.08
DOT_HEIGHT_FRAC = 0.10
DOT_PROMINENCE_FRAC = 0.05
STREAK_BAND_HALFWIDTH = 3       # px either side of a streak centre

DESCRIPTOR_LAYOUT = {
    "total_length": 64,
    "global": [
        "mean_intensity", "std_intensity", "max_intensity",
        "saturated_fraction", "total_intensity_norm",
        "n_streaks_detected", "n_dots_detected", "profile_contrast",
    ],
    "per_dot": ["brightness", "prominence", "y_position_norm", "fwhm_px"],
    "max_dots": MAX_DOTS,
    "dot_ordering": "descending prominence; zero-padded when fewer than max_dots",
}


# ---------------------------------------------------------------------------
# Grayscale extraction
# ---------------------------------------------------------------------------

def _build_bgw_inverse() -> np.ndarray:
    """(R, G) -> original monochrome index, inverting the kSA BGW palette.

    Mirrors ``gui/ksa_palette.py._build_bgw_lut``. The forward map is
    injective except at the knee, where indices 127 and 128 both render as
    (0, 255, 0); we resolve that to 127 by filling the low ramp last.
    """
    table = np.zeros((256, 256), dtype=np.uint8)
    covered = np.zeros((256, 256), dtype=bool)

    def _round(value: float) -> int:
        return int(np.floor(value + 0.5))

    for i in range(128, 256):
        delta = _round(255 * (i - 128) / 127)
        table[delta, 255] = i
        covered[delta, 255] = True
    for i in range(128):
        green = _round(255 * i / 127)
        table[0, green] = i
        covered[0, green] = True
    return table, covered


_BGW_INVERSE, _BGW_COVERED = _build_bgw_inverse()


def to_grayscale(rgb: np.ndarray) -> tuple[np.ndarray, str]:
    """Return (intensity uint8, rule), recovering the true monochrome frame.

    Never uses PIL's luminance conversion. On a BGW-palettized frame that
    would both clip (L = 0.587*G on the green ramp) and discard the white
    top end entirely, which is exactly where the diffraction spots live.
    """
    if rgb.ndim == 2:
        return rgb, "already_gray"

    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    if np.array_equal(red, green) and np.array_equal(green, blue):
        return green, "identical_channels"

    # kSA BGW palette: black -> green (G ramps, R=B=0) -> white (G pinned at
    # 255, R=B ramp). Bijective, so invert it exactly rather than picking a
    # channel -- taking G alone would flatten every saturated pixel.
    if np.array_equal(red, blue) and bool(_BGW_COVERED[red, green].all()):
        return _BGW_INVERSE[red, green], "bgw_palette_inverted"

    spans = [int(c.max()) - int(c.min()) for c in (red, green, blue)]
    if spans[1] > 0 and spans[0] == 0 and spans[2] == 0:
        return green, "green_only"

    dominant = int(np.argmax(spans))
    others = max(s for i, s in enumerate(spans) if i != dominant)
    if spans[dominant] > 4 * others:
        return rgb[:, :, dominant], f"dominant_channel_{'RGB'[dominant]}"

    # Genuinely polychrome (e.g. an unrecognised screengrab palette). Max
    # preserves the ramp's dynamic range better than a luminance mix.
    return rgb.max(axis=2), "channel_max_fallback"


# ---------------------------------------------------------------------------
# Peak-aware descriptor
# ---------------------------------------------------------------------------

def peak_descriptor(gray: np.ndarray) -> np.ndarray:
    """64-D physics-guided structural descriptor for one frame."""
    image = gray.astype(np.float32)
    height, width = image.shape
    vector = np.zeros(64, dtype=np.float32)

    max_intensity = float(image.max())
    saturated = float((image >= 255).sum()) / image.size
    total_norm = float(image.sum()) / (image.size * 255.0)

    column_profile = image.sum(axis=0)
    baseline = float(np.median(column_profile))
    dynamic = float(column_profile.max()) - baseline
    contrast = dynamic / (baseline + 1e-6)

    streaks: np.ndarray = np.array([], dtype=int)
    dots: list[tuple[float, float, float, float]] = []

    if dynamic > 0:
        streaks, _ = find_peaks(
            column_profile,
            height=baseline + STREAK_HEIGHT_FRAC * dynamic,
            prominence=STREAK_PROMINENCE_FRAC * dynamic,
        )
        for x in streaks:
            lo = max(0, int(x) - STREAK_BAND_HALFWIDTH)
            hi = min(width, int(x) + STREAK_BAND_HALFWIDTH + 1)
            row_profile = image[:, lo:hi].sum(axis=1)
            row_base = float(np.median(row_profile))
            row_dyn = float(row_profile.max()) - row_base
            if row_dyn <= 0:
                continue
            found, props = find_peaks(
                row_profile,
                height=row_base + DOT_HEIGHT_FRAC * row_dyn,
                prominence=DOT_PROMINENCE_FRAC * row_dyn,
            )
            if len(found) == 0:
                continue
            widths = peak_widths(row_profile, found, rel_height=0.5)[0]
            for idx, y in enumerate(found):
                dots.append((
                    float(row_profile[y] / (hi - lo)),      # brightness
                    float(props["prominences"][idx] / (hi - lo)),
                    float(y) / max(height - 1, 1),          # normalized position
                    float(widths[idx]),                     # FWHM in px
                ))

    vector[0] = float(image.mean())
    vector[1] = float(image.std())
    vector[2] = max_intensity
    vector[3] = saturated
    vector[4] = total_norm
    vector[5] = float(len(streaks))
    vector[6] = float(len(dots))
    vector[7] = contrast

    dots.sort(key=lambda d: d[1], reverse=True)
    for slot, dot in enumerate(dots[:MAX_DOTS]):
        base = N_GLOBAL + slot * DOTS_PER_FEATURE
        vector[base:base + DOTS_PER_FEATURE] = dot

    return vector


# ---------------------------------------------------------------------------
# Per-frame worker
# ---------------------------------------------------------------------------

def process_frame(job: tuple[str, str, str, str]) -> dict:
    src_str, rel, gray_out, thumb_out = job
    src = Path(src_str)
    try:
        with Image.open(src) as handle:
            rgb = np.array(handle.convert("RGB") if handle.mode != "L" else handle)
    except Exception as exc:  # noqa: BLE001 - report, never abort the batch
        return {"rel": rel, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    gray, rule = to_grayscale(rgb)
    descriptor = peak_descriptor(gray)

    image = Image.fromarray(gray, mode="L")
    gray_path = Path(gray_out)
    gray_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(gray_path, format="PNG", optimize=True)

    thumb_path = Path(thumb_out)
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    image.resize(THUMB_SIZE, Image.BILINEAR).save(thumb_path, format="PNG", optimize=True)

    return {
        "rel": rel,
        "ok": True,
        "channel_rule": rule,
        "src_bytes": src.stat().st_size,
        "gray_bytes": gray_path.stat().st_size,
        "thumb_bytes": thumb_path.stat().st_size,
        "descriptor": descriptor.tolist(),
    }


# ---------------------------------------------------------------------------
# Session metadata join
# ---------------------------------------------------------------------------

def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as stream:
        return list(csv.DictReader(stream))


def build_frame_metadata(session: Path) -> dict[str, dict]:
    """Map frame basename -> metadata, joined across every CSV that names one.

    This is the per-frame table the bundle format has never had: a single row
    per image carrying its time, temperature and capture provenance, so a
    folder of images stays interpretable once separated from the session.
    """
    by_name: dict[str, dict] = {}

    sources = (
        ("heartbeat_log.csv", "frame_path", "heartbeat"),
        ("commit_log.csv", "frame_path", "commit"),
        ("manual_events.csv", "frame_path", "manual_event"),
        ("rheed_view_events.csv", "frame_path", "view_event"),
        ("live_labels.csv", "frame_path", "live_label"),
    )
    for filename, column, surface in sources:
        for row in _rows(session / filename):
            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            name = raw.replace("\\", "/").rsplit("/", 1)[-1]
            record = {"capture_surface": surface}
            for key in ("timestamp", "elapsed_s", "pyrometer_temp_C",
                        "capture_backend", "captured_at_utc", "capture_sequence",
                        "frame_age_ms", "capture_geometry_id", "note",
                        "frame_quality_pass"):
                if row.get(key) not in (None, ""):
                    record[key] = row[key]
            by_name[name] = record

    # Auto-capture buffers are referenced by directory, not by file.
    for row in _rows(session / "auto_capture_events.csv"):
        buffer_dir = (row.get("buffer_dir") or "").strip()
        if not buffer_dir:
            continue
        folder = buffer_dir.replace("\\", "/").rsplit("/", 1)[-1]
        local = session / "frames" / folder
        if not local.is_dir():
            continue
        for bmp in local.glob("*.bmp"):
            by_name[bmp.name] = {
                "capture_surface": "auto_capture",
                "timestamp": row.get("timestamp", ""),
                "elapsed_s": row.get("elapsed_s", ""),
                "pyrometer_temp_C": row.get("pyrometer_temp_C", ""),
                "auto_event_idx": row.get("event_idx", ""),
                "change_score": row.get("change_score", ""),
                "event_state": row.get("event_state", ""),
            }
    return by_name


def add_history_features(records: list[dict]) -> None:
    """Attach prefix-only thermal-history features, in place.

    STO surface reconstruction is a memory of the anneal rather than a
    function of instantaneous temperature: two frames at the same reading
    mean different things on the way up and on the way down. These are the
    minimum features that disambiguate that, and every one is computed from
    the growth prefix so nothing leaks the future.
    """
    indexed = []
    for record in records:
        try:
            elapsed = float(record.get("elapsed_s", ""))
            temp = float(record.get("pyrometer_temp_C", ""))
        except (TypeError, ValueError):
            continue
        indexed.append((elapsed, temp, record))
    indexed.sort(key=lambda item: item[0])

    running_max = -float("inf")
    peak_seen_at: Optional[float] = None
    global_peak = max((t for _, t, _ in indexed), default=None)

    for position, (elapsed, temp, record) in enumerate(indexed):
        if temp > running_max:
            running_max = temp
        record["max_temp_reached_C"] = f"{running_max:.2f}"

        window = indexed[max(0, position - 10):position + 1]
        if len(window) >= 2:
            span = window[-1][0] - window[0][0]
            rate = (window[-1][1] - window[0][1]) / span if span > 0 else 0.0
        else:
            rate = 0.0
        record["dT_dt_C_per_s"] = f"{rate:.5f}"

        if global_peak is not None and peak_seen_at is None and temp >= global_peak - 1e-9:
            peak_seen_at = elapsed
        record["ramp_direction"] = (
            "down" if (peak_seen_at is not None and elapsed > peak_seen_at)
            else ("up" if rate > 0.05 else "hold" if abs(rate) <= 0.05 else "down")
        )
        record["time_above_800C_s"] = f"{sum(1 for e, t, _ in indexed[:position + 1] if t >= 800):.0f}"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def collect_frames(session: Path) -> list[Path]:
    frames_dir = session / "frames"
    if not frames_dir.is_dir():
        return []
    return sorted(frames_dir.rglob("*.bmp"))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("session", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0,
                        help="process only the first N frames (smoke test)")
    args = parser.parse_args(argv)

    session: Path = args.session
    out: Path = args.out
    if not session.is_dir():
        print(f"error: {session} is not a directory", file=sys.stderr)
        return 2

    frames = collect_frames(session)
    if args.limit:
        frames = frames[:args.limit]
    if not frames:
        print("error: no .bmp frames found", file=sys.stderr)
        return 2

    out.mkdir(parents=True, exist_ok=True)
    print(f"session : {session.name}")
    print(f"frames  : {len(frames)}")

    jobs = []
    for path in frames:
        rel = path.relative_to(session / "frames").as_posix()
        stem = rel[:-4] if rel.lower().endswith(".bmp") else rel
        jobs.append((
            str(path), rel,
            str(out / "gray" / f"{stem}.png"),
            str(out / "thumb" / f"{stem}.png"),
        ))

    started = time.time()
    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for done, result in enumerate(pool.map(process_frame, jobs, chunksize=16), 1):
            results.append(result)
            if done % 250 == 0 or done == len(jobs):
                rate = done / max(time.time() - started, 1e-6)
                print(f"  {done}/{len(jobs)}  ({rate:.0f} frames/s)", flush=True)

    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]

    descriptors = np.array([r["descriptor"] for r in ok], dtype=np.float32)
    np.save(out / "descriptors.npy", descriptors)

    metadata = build_frame_metadata(session)
    records = []
    for result in ok:
        name = result["rel"].rsplit("/", 1)[-1]
        record = {
            "frame": result["rel"],
            "gray_png": f"gray/{result['rel'][:-4]}.png",
            "thumb_png": f"thumb/{result['rel'][:-4]}.png",
            "channel_rule": result["channel_rule"],
        }
        record.update(metadata.get(name, {}))
        records.append(record)

    add_history_features(records)

    columns: list[str] = []
    for record in records:
        for key in record:
            if key not in columns:
                columns.append(key)
    with open(out / "frame_manifest.csv", "w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for record in records:
            writer.writerow(record)

    csv_dir = out / "csv"
    csv_dir.mkdir(exist_ok=True)
    copied = 0
    for extra in list(session.glob("*.csv")) + list(session.glob("*.json")):
        shutil.copy2(extra, csv_dir / extra.name)
        copied += 1

    src_bytes = sum(r["src_bytes"] for r in ok)
    gray_bytes = sum(r["gray_bytes"] for r in ok)
    thumb_bytes = sum(r["thumb_bytes"] for r in ok)
    rules: dict[str, int] = {}
    for r in ok:
        rules[r["channel_rule"]] = rules.get(r["channel_rule"], 0) + 1

    report = {
        "session": session.name,
        "frames_processed": len(ok),
        "frames_failed": len(failed),
        "failures": failed[:20],
        "channel_rules": rules,
        "descriptor_layout": DESCRIPTOR_LAYOUT,
        "bytes": {
            "source_bmp": src_bytes,
            "gray_png": gray_bytes,
            "thumb_png": thumb_bytes,
            "descriptors": int(descriptors.nbytes),
        },
        "elapsed_s": round(time.time() - started, 1),
        "csv_files_copied": copied,
    }
    (out / "extraction_report.json").write_text(json.dumps(report, indent=2))

    def mb(value: int) -> str:
        return f"{value / 1e6:8.1f} MB"

    print("\n" + "=" * 58)
    print(f"  processed {len(ok)} frames in {report['elapsed_s']:.0f}s"
          + (f"  ({len(failed)} FAILED)" if failed else ""))
    print(f"  channel rules: {rules}")
    print(f"  source BMP     {mb(src_bytes)}")
    print(f"  gray PNG       {mb(gray_bytes)}   {src_bytes / max(gray_bytes, 1):5.1f}x smaller")
    print(f"  thumbnails     {mb(thumb_bytes)}   {src_bytes / max(thumb_bytes, 1):5.1f}x smaller")
    print(f"  descriptors    {mb(int(descriptors.nbytes))}   shape {descriptors.shape}")
    print(f"  total kept     {mb(gray_bytes + thumb_bytes + int(descriptors.nbytes))}")
    print("=" * 58)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
