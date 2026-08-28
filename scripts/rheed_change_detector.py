#!/usr/bin/env python3
"""Legacy pixel-difference baseline for offline RHEED change analysis.

Reads a directory of RHEED frames in chronological filename order, computes
a mean-absolute-pixel-difference signal under one of two comparison modes,
smooths it with a rolling mean, and flags frames where the smoothed diff
exceeds a threshold. Outputs a per-frame CSV, a timeseries plot, and groups
flagged frames into discrete "events".

Comparison modes:
    previous    — diff against the immediately preceding frame
                  (catches sharp transitions; the original mode)
    buffer-mean — diff against the mean of the last N frames in a FIFO
                  buffer (mirrors the historical PixelDiffChangeDetector)

The live GUI now uses ``TranslationInvariantChangeDetector``.  Use
``replay_translation_change_detector.py`` to audit the production algorithm;
this script remains useful only as a comparison baseline.

Usage:
    python rheed_change_detector.py <frames_dir> [options]

Examples:
    python rheed_change_detector.py ~/data/rahim --mode previous --threshold 1.5
    python rheed_change_detector.py ~/data/rahim --mode buffer-mean --buffer-size 20
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
import matplotlib.pyplot as plt


DEFAULT_EXTENSIONS = (".png", ".bmp", ".tif", ".tiff", ".jpg", ".jpeg")


@dataclass
class Event:
    """A contiguous run of frames flagged as significant change."""

    start_idx: int
    end_idx: int
    start_frame: str
    end_frame: str
    peak_idx: int
    peak_diff: float

    @property
    def duration(self) -> int:
        return self.end_idx - self.start_idx + 1


def list_frames(frames_dir: Path, extensions: tuple[str, ...]) -> list[Path]:
    """Return frame files in the directory, sorted lexicographically.

    Frames produced by Growth Monitor encode timestamp + temperature in the
    filename, so lexicographic sort matches chronological order.
    """
    files: list[Path] = []
    for ext in extensions:
        files.extend(frames_dir.glob(f"*{ext}"))
        files.extend(frames_dir.glob(f"*{ext.upper()}"))
    return sorted(set(files))


def load_frame_grayscale(path: Path, prefer_green: bool = True) -> np.ndarray:
    """Load an image as a 2D float32 grayscale array on the [0, 255] scale.

    For RGB inputs (like kSA false-color screengrabs), the green channel
    carries RHEED intensity per project convention. For single-channel inputs,
    convert via PIL's luminance ('L') mode.
    """
    with Image.open(path) as img:
        if img.mode in ("RGB", "RGBA") and prefer_green:
            arr = np.asarray(img)[:, :, 1]
        else:
            arr = np.asarray(img.convert("L"))
    return arr.astype(np.float32)


def compute_diffs_previous(
    frames: list[Path], prefer_green: bool = True
) -> np.ndarray:
    """Mean absolute pixel difference between frame[i] and frame[i-1].

    diffs[0] is defined as 0.0 (no predecessor). Frames with mismatched shape
    are resized to the previous frame's shape with a warning.
    """
    diffs = np.zeros(len(frames), dtype=np.float32)
    prev = load_frame_grayscale(frames[0], prefer_green)
    for i in range(1, len(frames)):
        curr = load_frame_grayscale(frames[i], prefer_green)
        if curr.shape != prev.shape:
            print(
                f"  warning: shape mismatch at frame {i} ({frames[i].name}): "
                f"{curr.shape} vs {prev.shape}, resizing",
                file=sys.stderr,
            )
            resized = Image.fromarray(curr.astype(np.uint8)).resize(
                (prev.shape[1], prev.shape[0])
            )
            curr = np.asarray(resized).astype(np.float32)
        diffs[i] = float(np.mean(np.abs(curr - prev)))
        prev = curr
    return diffs


def compute_diffs_buffer_mean(
    frames: list[Path], buffer_size: int, prefer_green: bool = True
) -> np.ndarray:
    """Mean absolute pixel difference between frame[i] and the mean of the
    FIFO buffer of the preceding ``buffer_size`` frames.

    Mirrors the historical PixelDiffChangeDetector in gui/auto_capture.py.
    Compared to ``compute_diffs_previous``, this catches sustained
    *shifts* (full transition magnitude) rather than just instantaneous
    *rate of change* — peaks for the same transition are larger because we
    compare against an older, stable reference.

    diffs[0] is 0.0. For frames before the buffer is full, the comparison
    is against the partial buffer of all frames so far.
    """
    diffs = np.zeros(len(frames), dtype=np.float32)
    buffer: collections.deque[np.ndarray] = collections.deque(maxlen=buffer_size)
    sum_arr: np.ndarray | None = None

    first = load_frame_grayscale(frames[0], prefer_green)
    buffer.append(first)
    sum_arr = first.copy()

    for i in range(1, len(frames)):
        curr = load_frame_grayscale(frames[i], prefer_green)
        if sum_arr is not None and curr.shape != sum_arr.shape:
            print(
                f"  warning: shape mismatch at frame {i} ({frames[i].name}), "
                f"resetting buffer",
                file=sys.stderr,
            )
            buffer.clear()
            buffer.append(curr)
            sum_arr = curr.copy()
            diffs[i] = 0.0
            continue

        buffer_mean = sum_arr / len(buffer)
        diffs[i] = float(np.mean(np.abs(curr - buffer_mean)))

        if len(buffer) == buffer_size:
            sum_arr -= buffer[0]
        buffer.append(curr)
        sum_arr += curr

    return diffs


def compute_diffs(
    frames: list[Path],
    mode: str = "previous",
    buffer_size: int = 20,
    prefer_green: bool = True,
) -> np.ndarray:
    """Dispatch to the chosen comparison mode."""
    if mode == "previous":
        return compute_diffs_previous(frames, prefer_green)
    if mode == "buffer-mean":
        return compute_diffs_buffer_mean(frames, buffer_size, prefer_green)
    raise ValueError(f"unknown mode: {mode!r} (expected 'previous' or 'buffer-mean')")


def smooth(signal: np.ndarray, window: int) -> np.ndarray:
    """Centered rolling mean. Returns the input unchanged if window <= 1."""
    if window <= 1:
        return signal.copy()
    kernel = np.ones(window, dtype=np.float32) / window
    return np.convolve(signal, kernel, mode="same")


def group_events(flagged: np.ndarray, raw: np.ndarray, frames: list[Path]) -> list[Event]:
    """Collapse consecutive flagged indices into discrete events."""
    events: list[Event] = []
    in_event = False
    start = -1
    for i, hit in enumerate(flagged):
        if hit and not in_event:
            in_event = True
            start = i
        elif not hit and in_event:
            in_event = False
            events.append(_make_event(start, i - 1, raw, frames))
    if in_event:
        events.append(_make_event(start, len(flagged) - 1, raw, frames))
    return events


def _make_event(start: int, end: int, raw: np.ndarray, frames: list[Path]) -> Event:
    segment = raw[start : end + 1]
    peak_offset = int(np.argmax(segment))
    peak_idx = start + peak_offset
    return Event(
        start_idx=start,
        end_idx=end,
        start_frame=frames[start].name,
        end_frame=frames[end].name,
        peak_idx=peak_idx,
        peak_diff=float(raw[peak_idx]),
    )


def write_csv(
    out_path: Path,
    frames: list[Path],
    raw: np.ndarray,
    smoothed: np.ndarray,
    flagged: np.ndarray,
) -> None:
    """Write one row per frame to CSV."""
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["frame_idx", "filename", "raw_diff", "smoothed_diff", "flagged"])
        for i, path in enumerate(frames):
            writer.writerow(
                [i, path.name, f"{raw[i]:.4f}", f"{smoothed[i]:.4f}", int(bool(flagged[i]))]
            )


def plot_timeseries(
    out_path: Path,
    raw: np.ndarray,
    smoothed: np.ndarray,
    threshold: float,
    flagged: np.ndarray,
) -> None:
    """Plot raw and smoothed diff timeseries with the threshold and flagged events."""
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(raw, color="lightgray", linewidth=0.6, label="raw |Δ|")
    ax.plot(smoothed, color="steelblue", linewidth=1.5, label="smoothed |Δ|")
    ax.axhline(threshold, color="crimson", linestyle="--", linewidth=1, label=f"threshold = {threshold}")
    flagged_idx = np.where(flagged)[0]
    if flagged_idx.size:
        ax.scatter(
            flagged_idx,
            smoothed[flagged_idx],
            color="crimson",
            s=18,
            zorder=5,
            label=f"flagged (n={flagged_idx.size})",
        )
    ax.set_xlabel("frame index")
    ax.set_ylabel("mean abs pixel diff (0–255)")
    ax.set_title("RHEED frame-to-frame change")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline RHEED change-point detector for Growth Monitor v3 tuning",
    )
    parser.add_argument("frames_dir", type=Path, help="Directory containing RHEED frames")
    parser.add_argument(
        "--ext",
        default=",".join(DEFAULT_EXTENSIONS),
        help=f"Comma-separated frame extensions (default: {','.join(DEFAULT_EXTENSIONS)})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=5.0,
        help="Smoothed diff threshold for flagging, on the 0–255 intensity scale (default: 5.0)",
    )
    parser.add_argument(
        "--smooth-window",
        type=int,
        default=3,
        help="Rolling-mean window for smoothing the diff signal (default: 3)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: <frames_dir>/../change_detector_output/)",
    )
    parser.add_argument(
        "--no-prefer-green",
        action="store_true",
        help="Convert RGB frames via luminance instead of taking the green channel",
    )
    parser.add_argument(
        "--mode",
        choices=["previous", "buffer-mean"],
        default="previous",
        help=(
            "Comparison mode: 'previous' diffs against frame[i-1]; "
            "'buffer-mean' diffs against the mean of the last N frames "
            "(mirrors the live GUI detector). Default: previous"
        ),
    )
    parser.add_argument(
        "--buffer-size",
        type=int,
        default=20,
        help="FIFO buffer size for --mode buffer-mean (default: 20)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.frames_dir.is_dir():
        print(f"error: {args.frames_dir} is not a directory", file=sys.stderr)
        return 1

    extensions = tuple(e.strip() for e in args.ext.split(",") if e.strip())
    frames = list_frames(args.frames_dir, extensions)
    if len(frames) < 2:
        print(
            f"error: found {len(frames)} frames in {args.frames_dir} (need >= 2)",
            file=sys.stderr,
        )
        return 1

    print(f"loaded {len(frames)} frames from {args.frames_dir}")
    mode_desc = (
        f"buffer-mean (N={args.buffer_size})"
        if args.mode == "buffer-mean"
        else "previous-frame"
    )
    print(
        f"computing diffs: mode={mode_desc}, "
        f"threshold={args.threshold}, smooth_window={args.smooth_window}..."
    )

    raw = compute_diffs(
        frames,
        mode=args.mode,
        buffer_size=args.buffer_size,
        prefer_green=not args.no_prefer_green,
    )
    smoothed = smooth(raw, args.smooth_window)
    flagged = smoothed > args.threshold
    flagged[0] = False

    out_dir = args.output_dir or args.frames_dir.parent / "change_detector_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "per_frame.csv"
    plot_path = out_dir / "timeseries.png"

    write_csv(csv_path, frames, raw, smoothed, flagged)
    plot_timeseries(plot_path, raw, smoothed, args.threshold, flagged)

    events = group_events(flagged, raw, frames)
    nonzero = raw[1:]

    print()
    print("results:")
    print(f"  total frames:    {len(frames)}")
    print(f"  flagged frames:  {int(flagged.sum())} ({100 * flagged.mean():.1f}%)")
    print(f"  discrete events: {len(events)}")
    print(f"  mean diff:       {nonzero.mean():.2f}")
    print(f"  std diff:        {nonzero.std():.2f}")
    print(f"  max diff:        {nonzero.max():.2f} at frame {int(nonzero.argmax()) + 1}")
    print()
    print("outputs:")
    print(f"  csv:  {csv_path}")
    print(f"  plot: {plot_path}")

    if events:
        print()
        print(f"events (showing up to 10 of {len(events)}):")
        for ev in events[:10]:
            print(
                f"  frames {ev.start_idx}–{ev.end_idx} ({ev.duration} frames), "
                f"peak {ev.peak_diff:.2f} @ idx {ev.peak_idx}: "
                f"{ev.start_frame} → {ev.end_frame}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
