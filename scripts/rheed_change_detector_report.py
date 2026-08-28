#!/usr/bin/env python3
"""HTML report for the legacy pixel-difference RHEED baseline.

Produces a single self-contained HTML file (matplotlib figures embedded as
base64 PNGs) covering five validation views:

    1. Summary stats card
    2. Score distribution histogram with threshold overlay
    3. Timeseries plot with shaded event bands
    4. Mode comparison: previous-frame vs buffer-mean on the same axes
    5. Flagged-event frame-pair gallery (frame[start-1] | frame[peak] | |diff|)
    6. Threshold sensitivity sweep (event count vs threshold)

This report is retained for historical comparison.  The live GUI now uses
``TranslationInvariantChangeDetector``; validate that implementation with
``replay_translation_change_detector.py``.  These score plots must not be
presented as evidence for the current production detector.

Chronological order:
    Frames must be in true acquisition order for the timeseries to mean
    anything. If the dataset includes a rename log mapping original
    frame_num to renamed file (one mapping per line, original on the left),
    pass it via --rename-log and sort will follow that order. Otherwise
    falls back to lexicographic sort with a warning.

Usage:
    python rheed_change_detector_report.py <frames_dir> [options]

Example:
    python rheed_change_detector_report.py \\
        /path/to/Substrates/ \\
        --rename-log /path/to/rename_log.txt \\
        --threshold 2.0 \\
        --buffer-size 20 \\
        --output /path/to/report.html
"""

from __future__ import annotations

import argparse
import base64
import collections
import io
import re
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path

import numpy as np
from PIL import Image
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for gui.auto_capture
from rheed_change_detector import (  # noqa: E402
    compute_diffs_buffer_mean,
    compute_diffs_previous,
    group_events,
    list_frames,
    load_frame_grayscale,
    smooth,
)
from gui.auto_capture import (  # noqa: E402
    ConfirmationResult,
    confirm_event_by_plateau_shift,
)


DEFAULT_EXTENSIONS = (".png", ".bmp", ".tif", ".tiff", ".jpg", ".jpeg")
GALLERY_FRAME_WIDTH_PX = 320  # rendered width per frame in the event gallery
SENSITIVITY_THRESHOLDS = np.arange(1.0, 4.01, 0.25)


# ---------------------------------------------------------------------------
# Frame ordering — chronological from rename log, fallback to lex sort
# ---------------------------------------------------------------------------

def order_frames(
    frames_dir: Path,
    extensions: tuple[str, ...],
    rename_log: Path | None,
) -> tuple[list[Path], str]:
    """Return frames in true acquisition order plus a description of how.

    With a rename log (original_name -> renamed_name, one per line), uses
    the line order as chronological order. Without one, falls back to
    lexicographic sort and warns.
    """
    available = {p.name: p for p in list_frames(frames_dir, extensions)}
    if not available:
        raise FileNotFoundError(f"no frames in {frames_dir}")

    if rename_log and rename_log.is_file():
        ordered: list[Path] = []
        # Match any line containing an arrow (-> or →), capture the last
        # non-whitespace token after it. Tolerates leading prefixes like
        # "RENAMED:" and varying whitespace.
        arrow_re = re.compile(r"(?:->|–>|→)\s*(\S+)")
        for line in rename_log.read_text().splitlines():
            m = arrow_re.search(line)
            if not m:
                continue
            renamed = m.group(1)
            if renamed in available:
                ordered.append(available[renamed])
        if len(ordered) >= len(available) * 0.9:
            return ordered, f"chronological from {rename_log.name}"
        print(
            f"  warning: rename log only matched {len(ordered)}/{len(available)} "
            "frames; falling back to lex sort",
            file=sys.stderr,
        )

    print(
        "  warning: using lexicographic sort. If this dataset has temps that "
        "wrap (165C, 178C, ..., 291C, ..., 988C, ..., 165C cooldown), lex "
        "sort scrambles chronological order and the timeseries will be "
        "misleading. Pass --rename-log if available.",
        file=sys.stderr,
    )
    return sorted(available.values()), "lexicographic (chronological order unverified)"


# ---------------------------------------------------------------------------
# Figure helpers — render matplotlib to base64 PNG for embedding
# ---------------------------------------------------------------------------

def fig_to_b64(fig, dpi: int = 110) -> str:
    """Encode a matplotlib figure as a base64 PNG data URI body."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def array_to_b64_png(arr: np.ndarray, cmap: str | None = None) -> str:
    """Encode a 2D array as a base64 PNG, with optional colormap."""
    if cmap is None:
        # Grayscale frame: PIL handles it directly.
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # Heatmap: render via matplotlib so the colormap actually applies.
    fig, ax = plt.subplots(figsize=(4, 3))
    ax.imshow(arr, cmap=cmap, aspect="auto")
    ax.set_axis_off()
    fig.tight_layout(pad=0)
    return fig_to_b64(fig, dpi=90)


# ---------------------------------------------------------------------------
# Adaptive thresholding (mirrors AutoCaptureEngine.effective_threshold)
# ---------------------------------------------------------------------------

def adaptive_threshold_per_frame(
    smoothed: np.ndarray,
    sigma: float,
    floor: float = 1.0,
    history: int = 100,
    warmup: int = 30,
    fallback_threshold: float = 2.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute per-frame adaptive thresholds and a flag mask.

    Mirrors the live engine's logic: a rolling FIFO of the last ``history``
    *non-flagged* scores; once it has at least ``warmup`` samples, the
    threshold for the next frame is ``max(floor, μ + sigma·σ)`` over the
    rolling baseline. During the warmup the fixed ``fallback_threshold``
    is used so the algorithm doesn't fire spuriously on its first frames.

    The "exclude flagged from baseline" trick keeps events from polluting
    the rolling mean — otherwise a sustained event would pull the
    threshold up behind its own peak and the algorithm would self-suppress.
    """
    n = len(smoothed)
    thresholds = np.full(n, fallback_threshold, dtype=float)
    flagged = np.zeros(n, dtype=bool)
    baseline: collections.deque[float] = collections.deque(maxlen=history)

    for i, score in enumerate(smoothed):
        if len(baseline) >= warmup:
            arr = np.asarray(baseline, dtype=np.float64)
            thresholds[i] = max(floor, float(arr.mean() + sigma * arr.std()))
        # else: thresholds[i] keeps the fallback set by np.full

        flagged[i] = score > thresholds[i]
        if not flagged[i]:
            baseline.append(float(score))

    return thresholds, flagged


# ---------------------------------------------------------------------------
# Offline plateau-shift confirmation
# ---------------------------------------------------------------------------

def confirm_events_offline(
    events: list,
    frames: list[Path],
    pre_window: int = 5,
    post_window: int = 5,
    skip: int = 2,
    threshold: float = 1.0,
    metric: str = "std",
) -> list[ConfirmationResult]:
    """Run confirm_event_by_plateau_shift on each detected event.

    The live engine will do this via a PENDING_CONFIRMATION state that
    delays emission for N frames after each trigger; here we replay it
    offline against the on-disk frames. For each event, load a window of
    ``pre_window + post_window + 2*skip + 1`` frames centered such that
    the event's peak sits at the local trigger index, then call the
    plateau function unchanged.

    Result list is aligned by index with ``events`` — one entry per event
    in the same order. Events too close to the frame-list boundary to fit
    the full window return ``ConfirmationResult(False, 0.0,
    "windows_out_of_range")`` so they're distinguishable from rejected
    events in the report.
    """
    needed = pre_window + post_window + 2 * skip + 1
    trigger_local_idx = pre_window + skip

    results: list[ConfirmationResult] = []
    for ev in events:
        start = ev.peak_idx - trigger_local_idx
        end = start + needed
        if start < 0 or end > len(frames):
            results.append(
                ConfirmationResult(False, 0.0, "windows_out_of_range")
            )
            continue
        window = [load_frame_grayscale(frames[i]) for i in range(start, end)]
        results.append(
            confirm_event_by_plateau_shift(
                window,
                trigger_idx=trigger_local_idx,
                pre_window_size=pre_window,
                post_window_size=post_window,
                skip_around_trigger=skip,
                confirmation_threshold=threshold,
                score_metric=metric,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Section builders — each returns an HTML string
# ---------------------------------------------------------------------------

@dataclass
class ReportInputs:
    frames_dir: Path
    frames: list[Path]
    order_desc: str
    raw_previous: np.ndarray
    raw_buffer_mean: np.ndarray
    smoothed: np.ndarray
    threshold: float
    smooth_window: int
    buffer_size: int
    primary_mode: str
    flagged: np.ndarray
    # Adaptive bookkeeping — None when running in fixed-threshold mode.
    adaptive_sigma: float | None = None
    adaptive_thresholds: np.ndarray | None = None  # per-frame, len == flagged
    # Plateau-shift confirmation — None when confirmation is off.
    # confirmation_results is aligned by index with the event list from
    # group_events(flagged, raw, frames) at build time.
    confirmation_results: list[ConfirmationResult] | None = None
    confirmation_threshold: float | None = None
    confirmation_metric: str | None = None


def build_summary(inp: ReportInputs) -> str:
    """Top-of-report stats card."""
    raw = inp.raw_previous if inp.primary_mode == "previous" else inp.raw_buffer_mean
    nonzero = raw[1:]
    flagged_mask = inp.flagged.astype(bool)
    baseline = raw[~flagged_mask][1:]  # exclude first 0.0 and any flagged points
    events = group_events(inp.flagged, raw, inp.frames)

    if inp.adaptive_thresholds is not None and inp.adaptive_sigma is not None:
        post_warmup = inp.adaptive_thresholds[inp.adaptive_thresholds < inp.threshold]
        adaptive_summary = (
            f"adaptive μ + {inp.adaptive_sigma:g}σ — "
            f"min {post_warmup.min():.2f}, "
            f"median {float(np.median(post_warmup)):.2f}, "
            f"max {post_warmup.max():.2f}"
            if post_warmup.size else
            f"adaptive μ + {inp.adaptive_sigma:g}σ — never warmed up"
        )
        threshold_label = f"fixed fallback {inp.threshold:.2f}; {adaptive_summary}"
    else:
        threshold_label = f"fixed {inp.threshold:.2f}"

    rows = [
        ("Frames directory", str(inp.frames_dir)),
        ("Frame count", f"{len(inp.frames)}"),
        ("Order", inp.order_desc),
        ("Primary mode", inp.primary_mode),
        ("Buffer size", f"{inp.buffer_size}" if inp.primary_mode == "buffer-mean" else "n/a"),
        ("Smooth window", f"{inp.smooth_window}"),
        ("Threshold", threshold_label),
        ("Baseline (non-flagged) mean ± std", f"{baseline.mean():.2f} ± {baseline.std():.2f}"),
        ("Score range (raw)", f"{nonzero.min():.2f} – {nonzero.max():.2f}"),
        ("Flagged frames", f"{int(flagged_mask.sum())} ({100 * flagged_mask.mean():.1f}%)"),
        ("Discrete events", f"{len(events)}"),
    ]

    # If plateau confirmation ran, break down events by outcome. The
    # reduced count is the number that survive the confirmation pass —
    # what the live engine would emit if the plateau check were wired in.
    if inp.confirmation_results is not None:
        confirmed = sum(1 for r in inp.confirmation_results if r.confirmed)
        rejected = sum(
            1 for r in inp.confirmation_results
            if r.reason == "rejected_below_threshold"
        )
        oor = sum(
            1 for r in inp.confirmation_results
            if r.reason == "windows_out_of_range"
        )
        too_small = sum(
            1 for r in inp.confirmation_results
            if r.reason == "buffer_too_small"
        )
        rows.append((
            "Plateau confirmation",
            f"threshold {inp.confirmation_threshold:.2f}, "
            f"metric {inp.confirmation_metric}",
        ))
        rows.append((
            "Events after confirmation",
            f"{confirmed} confirmed · {rejected} rejected · "
            f"{oor} out-of-range · {too_small} buffer-too-small",
        ))

    body = "".join(
        f'<tr><td class="label">{escape(label)}</td><td>{escape(value)}</td></tr>'
        for label, value in rows
    )
    return f'<div class="stats"><table>{body}</table></div>'


def build_histogram(inp: ReportInputs) -> str:
    """Score distribution + threshold position. The visual gut-check."""
    raw = inp.raw_previous if inp.primary_mode == "previous" else inp.raw_buffer_mean
    scores = raw[1:]  # drop the synthetic 0.0 at index 0
    flagged_mask = inp.flagged[1:].astype(bool)

    fig, ax = plt.subplots(figsize=(10, 4))
    bins = np.linspace(0, max(scores.max() * 1.05, inp.threshold * 2), 60)
    ax.hist(scores[~flagged_mask], bins=bins, color="steelblue", alpha=0.85, label="below threshold")
    ax.hist(scores[flagged_mask], bins=bins, color="crimson", alpha=0.85, label="flagged")
    if inp.adaptive_thresholds is not None and inp.adaptive_sigma is not None:
        # Show the adaptive threshold's range as a shaded band + median line
        # — a single dashed line would lie about a varying threshold.
        post_warmup = inp.adaptive_thresholds[inp.adaptive_thresholds < inp.threshold]
        if post_warmup.size:
            t_med = float(np.median(post_warmup))
            t_min, t_max = float(post_warmup.min()), float(post_warmup.max())
            ax.axvspan(t_min, t_max, color="black", alpha=0.10,
                       label=f"adaptive range [{t_min:.2f}, {t_max:.2f}]")
            ax.axvline(t_med, color="black", linestyle="--", linewidth=1.2,
                       label=f"adaptive median {t_med:.2f}")
    else:
        ax.axvline(inp.threshold, color="black", linestyle="--", linewidth=1.2,
                   label=f"threshold = {inp.threshold:.2f}")
    ax.set_xlabel("smoothed mean abs pixel diff (0–255)")
    ax.set_ylabel("frame count")
    ax.set_title("Score distribution — does the threshold sit in a clean gap?")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    b64 = fig_to_b64(fig)
    return (
        '<div class="figure">'
        f'<img alt="score histogram" src="data:image/png;base64,{b64}" />'
        "</div>"
    )


def build_timeseries(inp: ReportInputs) -> str:
    """Smoothed score over time with shaded event bands."""
    raw = inp.raw_previous if inp.primary_mode == "previous" else inp.raw_buffer_mean
    smoothed = smooth(raw, inp.smooth_window)
    events = group_events(inp.flagged, raw, inp.frames)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(raw, color="lightgray", linewidth=0.6, label="raw |Δ|")
    ax.plot(smoothed, color="steelblue", linewidth=1.5, label="smoothed |Δ|")
    if inp.adaptive_thresholds is not None and inp.adaptive_sigma is not None:
        ax.plot(inp.adaptive_thresholds, color="crimson", linestyle="--",
                linewidth=1.2, label=f"adaptive (μ + {inp.adaptive_sigma:g}σ)")
    else:
        ax.axhline(inp.threshold, color="crimson", linestyle="--", linewidth=1,
                   label=f"threshold = {inp.threshold:.2f}")
    for ev in events:
        ax.axvspan(ev.start_idx - 0.5, ev.end_idx + 0.5, color="crimson", alpha=0.18)
    ax.set_xlabel("frame index (chronological)")
    ax.set_ylabel("mean abs pixel diff (0–255)")
    ax.set_title(f"Score timeseries — {len(events)} events flagged")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    b64 = fig_to_b64(fig)
    return (
        '<div class="figure">'
        f'<img alt="score timeseries" src="data:image/png;base64,{b64}" />'
        "</div>"
    )


def build_mode_comparison(inp: ReportInputs) -> str:
    """Overlay previous-frame and buffer-mean modes on the same axis."""
    sm_prev = smooth(inp.raw_previous, inp.smooth_window)
    sm_buf = smooth(inp.raw_buffer_mean, inp.smooth_window)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(sm_prev, color="seagreen", linewidth=1.4, label="previous-frame")
    ax.plot(sm_buf, color="darkorange", linewidth=1.4, label=f"buffer-mean (N={inp.buffer_size})")
    ax.axhline(inp.threshold, color="crimson", linestyle="--", linewidth=1,
               label=f"threshold = {inp.threshold:.2f}")
    ax.set_xlabel("frame index")
    ax.set_ylabel("smoothed mean abs pixel diff")
    ax.set_title("Mode comparison — buffer-mean catches sustained shifts; previous catches sharp jumps")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    b64 = fig_to_b64(fig)
    return (
        '<div class="figure">'
        f'<img alt="mode comparison" src="data:image/png;base64,{b64}" />'
        "</div>"
    )


def build_event_gallery(inp: ReportInputs, max_events: int = 20) -> str:
    """For each flagged event, render frame[start-1] | frame[peak] | |diff|.

    This is the load-bearing section — a skeptic should be able to look at
    each row and decide whether the detector caught a real change.
    """
    raw = inp.raw_previous if inp.primary_mode == "previous" else inp.raw_buffer_mean
    events = group_events(inp.flagged, raw, inp.frames)

    if not events:
        return (
            '<p><em>No events flagged at this threshold. Either the threshold '
            "is too high, or the dataset contains no transitions detectable by "
            "this algorithm.</em></p>"
        )

    headline = (
        f'<p>Showing {min(len(events), max_events)} of {len(events)} events. '
        f'For each event, three panels: the frame before the change, the peak '
        f'frame, and the per-pixel absolute diff between them (hot = larger '
        f'change). Visually verify each is a real RHEED transition.</p>'
    )

    rows: list[str] = []
    for n, ev in enumerate(events[:max_events], start=1):
        before_idx = max(0, ev.start_idx - 1)
        peak_idx = ev.peak_idx
        before = load_frame_grayscale(inp.frames[before_idx])
        peak = load_frame_grayscale(inp.frames[peak_idx])
        if before.shape != peak.shape:
            # Resize the smaller one to match — defensive, rare in practice.
            target = peak.shape
            before = np.asarray(
                Image.fromarray(np.clip(before, 0, 255).astype(np.uint8)).resize(
                    (target[1], target[0])
                )
            ).astype(np.float32)
        diff = np.abs(peak - before)

        b64_before = array_to_b64_png(before)
        b64_peak = array_to_b64_png(peak)
        b64_diff = array_to_b64_png(diff, cmap="hot")

        # Confirmation badge (green = confirmed, red = rejected, gray =
        # unable to test). Reads like a linter report — the eye can
        # quickly scan the gallery and cross-check the confirmation
        # decision against the visible frame content.
        confirmation_badge = ""
        if inp.confirmation_results is not None:
            r = inp.confirmation_results[n - 1]
            if r.reason == "confirmed":
                color, label = "#166534", "CONFIRMED"
            elif r.reason == "rejected_below_threshold":
                color, label = "#991b1b", "REJECTED"
            else:
                color, label = "#6b7280", "UNTESTABLE"
            confirmation_badge = (
                f' <span style="background: {color}; color: white; '
                f'padding: 0.15em 0.55em; border-radius: 3px; '
                f'font-size: 0.85em; margin-left: 0.6em;">'
                f'{label} · Δ={r.diff_score:.2f} · {escape(r.reason)}'
                f'</span>'
            )

        header = (
            f'<div class="event-header">Event #{n} — '
            f'frames {ev.start_idx}–{ev.end_idx} (duration {ev.duration}), '
            f'peak score {ev.peak_diff:.2f} at frame {peak_idx}'
            f'{confirmation_badge}</div>'
        )
        cells = (
            f'<div class="frame">'
            f'<img src="data:image/png;base64,{b64_before}" />'
            f'<div class="caption">before — frame {before_idx}<br>{escape(inp.frames[before_idx].name)}</div>'
            f'</div>'
            f'<div class="frame">'
            f'<img src="data:image/png;base64,{b64_peak}" />'
            f'<div class="caption">peak — frame {peak_idx}<br>{escape(inp.frames[peak_idx].name)}</div>'
            f'</div>'
            f'<div class="frame">'
            f'<img src="data:image/png;base64,{b64_diff}" />'
            f'<div class="caption">|diff| heatmap (hot = larger change)</div>'
            f'</div>'
        )
        rows.append(f'<div class="gallery">{header}{cells}</div>')

    return headline + "\n".join(rows)


def build_sensitivity_sweep(inp: ReportInputs) -> str:
    """Sweep threshold and plot event count — look for an elbow."""
    raw = inp.raw_previous if inp.primary_mode == "previous" else inp.raw_buffer_mean
    smoothed = smooth(raw, inp.smooth_window)

    counts = []
    flagged_pcts = []
    for t in SENSITIVITY_THRESHOLDS:
        flagged = smoothed > t
        flagged[0] = False
        events = group_events(flagged, raw, inp.frames)
        counts.append(len(events))
        flagged_pcts.append(100 * flagged.mean())

    fig, ax1 = plt.subplots(figsize=(10, 4))
    ax1.plot(SENSITIVITY_THRESHOLDS, counts, color="steelblue", marker="o",
             linewidth=1.5, label="discrete event count")
    ax1.set_xlabel("threshold")
    ax1.set_ylabel("event count", color="steelblue")
    ax1.tick_params(axis="y", labelcolor="steelblue")
    ax1.axvline(inp.threshold, color="crimson", linestyle="--", linewidth=1,
                label=f"current = {inp.threshold:.2f}")
    ax1.grid(True, alpha=0.3)

    ax2 = ax1.twinx()
    ax2.plot(SENSITIVITY_THRESHOLDS, flagged_pcts, color="darkorange", marker="s",
             linewidth=1.5, label="% frames flagged")
    ax2.set_ylabel("% frames flagged", color="darkorange")
    ax2.tick_params(axis="y", labelcolor="darkorange")

    ax1.set_title("Threshold sensitivity — look for an elbow near the chosen threshold")
    fig.tight_layout()
    b64 = fig_to_b64(fig)
    return (
        '<div class="figure">'
        f'<img alt="sensitivity sweep" src="data:image/png;base64,{b64}" />'
        "</div>"
    )


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>RHEED Change Detector — Validation Report</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 1200px; margin: 2em auto; padding: 0 1em; color: #222;
         line-height: 1.5; }
  h1 { border-bottom: 2px solid #333; padding-bottom: 0.3em; }
  h2 { margin-top: 2.5em; color: #333; border-bottom: 1px solid #ccc;
       padding-bottom: 0.2em; }
  p.lede { color: #555; font-size: 0.95em; }
  .stats { background: #f4f4f4; padding: 1em 1.5em; border-radius: 4px;
           margin: 1em 0; }
  .stats table { border-collapse: collapse; }
  .stats td { padding: 0.25em 1.5em 0.25em 0; vertical-align: top; }
  .stats td.label { color: #666; min-width: 18em; }
  .figure { text-align: center; margin: 1em 0; }
  .figure img { max-width: 100%; height: auto; border: 1px solid #ddd; }
  .gallery { display: grid; grid-template-columns: 1fr 1fr 1fr;
             gap: 0.6em; margin: 1em 0 2em; align-items: start; }
  .gallery .event-header { grid-column: 1 / -1; background: #fff5e6;
                            padding: 0.6em 1em; font-weight: 600;
                            border-left: 4px solid #d97706; border-radius: 2px; }
  .gallery .frame { text-align: center; }
  .gallery .frame img { max-width: 100%; height: auto; border: 1px solid #ccc; }
  .gallery .caption { font-size: 0.8em; color: #666; padding: 0.3em 0; }
  footer { margin-top: 4em; padding-top: 1em; border-top: 1px solid #ccc;
           color: #888; font-size: 0.85em; }
</style>
</head>
<body>
"""

HTML_TAIL = """
<footer>
Generated by <code>scripts/rheed_change_detector_report.py</code>.
For questions about the algorithm, see <code>gui/auto_capture.py</code> —
specifically the <code>PixelDiffChangeDetector</code> class.
</footer>
</body>
</html>
"""


def assemble_html(inp: ReportInputs) -> str:
    sections: list[str] = [
        HTML_HEAD,
        "<h1>RHEED Change Detector — Validation Report</h1>",
        '<p class="lede">Offline validation of the live <code>PixelDiffChangeDetector</code> '
        "against a reference dataset. Five views answer five questions: "
        "are the chosen knobs (mode, threshold, buffer size) defensible, "
        "and does the detector flag what a human would call a real RHEED transition?</p>",
        "<h2>1. Summary</h2>",
        build_summary(inp),
        "<h2>2. Score distribution</h2>",
        '<p class="lede">If the threshold (dashed line) sits inside a clear valley between '
        "the noise mass on the left and the event mass on the right, the choice is robust.</p>",
        build_histogram(inp),
        "<h2>3. Score timeseries</h2>",
        '<p class="lede">Smoothed score over the full session. Shaded bands mark '
        "contiguous flagged-event runs.</p>",
        build_timeseries(inp),
        "<h2>4. Mode comparison</h2>",
        '<p class="lede">Same dataset under both comparison modes. '
        "<em>Previous-frame</em> reacts to instantaneous rate of change; "
        "<em>buffer-mean</em> reacts to the magnitude of sustained shifts. "
        "Buffer-mean usually peaks higher for the same transitions because it "
        "compares against an older, stable reference.</p>",
        build_mode_comparison(inp),
        "<h2>5. Flagged-event gallery</h2>",
        build_event_gallery(inp),
        "<h2>6. Threshold sensitivity</h2>",
        '<p class="lede">Sweep threshold from 1.0 to 4.0 and plot how many events fall out. '
        "An elbow near the current threshold means the answer doesn't depend on a magic constant.</p>",
        build_sensitivity_sweep(inp),
        HTML_TAIL,
    ]
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate a self-contained HTML validation report for the RHEED change detector.",
    )
    p.add_argument("frames_dir", type=Path, help="Directory containing RHEED frames.")
    p.add_argument(
        "--rename-log", type=Path, default=None,
        help="Optional rename log mapping original_name -> renamed_name, one per line. "
             "Used to derive true chronological order. Without it, falls back to lex sort.",
    )
    p.add_argument("--threshold", type=float, default=2.0,
                   help="Smoothed-diff threshold for flagging (default: 2.0).")
    p.add_argument("--smooth-window", type=int, default=3,
                   help="Rolling-mean window for smoothing (default: 3).")
    p.add_argument("--buffer-size", type=int, default=20,
                   help="FIFO buffer size for buffer-mean mode (default: 20).")
    p.add_argument("--mode", choices=["previous", "buffer-mean"], default="buffer-mean",
                   help="Primary mode used for the timeseries / histogram / gallery (default: buffer-mean — matches live GUI).")
    p.add_argument("--ext", default=",".join(DEFAULT_EXTENSIONS),
                   help=f"Comma-separated frame extensions (default: {','.join(DEFAULT_EXTENSIONS)}).")
    p.add_argument("--output", type=Path, default=None,
                   help="Output HTML path (default: <frames_dir>/../validation_report.html).")
    p.add_argument("--adaptive-sigma", type=float, default=None,
                   help="If set, use adaptive μ + Nσ thresholding instead of the fixed --threshold. "
                        "Mirrors the live AutoCaptureEngine when configured the same way. "
                        "Common values: 3.0 (3σ above noise; defensible by stats) or 4.0 (more conservative).")
    p.add_argument("--adaptive-floor", type=float, default=1.0,
                   help="Lower bound on the adaptive threshold (default 1.0). Prevents runaway "
                        "sensitivity in pathologically quiet sessions where σ is tiny.")
    p.add_argument("--adaptive-history", type=int, default=100,
                   help="Rolling-window size for the adaptive baseline (default 100 frames).")
    p.add_argument("--adaptive-warmup", type=int, default=20,
                   help="Frames required in the rolling baseline before adaptive kicks in. "
                        "During warmup, --threshold is used as a fallback. Default 20 — "
                        "tuned against Rahim's 02_06 dataset where the first real event "
                        "occurs around frame 25; warmup=30 misses it entirely.")
    # Plateau-shift confirmation (May 22 PI proposal). Offline mirror of
    # what AutoCaptureEngine's PENDING_CONFIRMATION path will do once
    # engine integration lands — see gui/auto_capture.py:269 for the
    # underlying function.
    p.add_argument("--confirmation-threshold", type=float, default=None,
                   help="If set, run plateau-shift confirmation on each flagged event. "
                        "The threshold is the minimum std(|pre_mean - post_mean|) required "
                        "to CONFIRM an event; smaller shifts are rejected as bumps/artifacts. "
                        "Suggested starting value ~0.5 (half the live floor of 1.0) — the "
                        "confirmation should be less strict than the trigger, not more.")
    p.add_argument("--confirmation-metric", choices=["std", "mean"], default="std",
                   help="'std' (default) rejects uniform brightness shifts because they have "
                        "no spatial variability — the right choice for reconstruction "
                        "transitions, which ARE spatially structured. 'mean' also confirms "
                        "uniform shifts; use only if you want to catch flash-like events.")
    p.add_argument("--confirmation-pre-window", type=int, default=5,
                   help="Frames averaged for the pre-event plateau (default 5).")
    p.add_argument("--confirmation-post-window", type=int, default=5,
                   help="Frames averaged for the post-event plateau (default 5).")
    p.add_argument("--confirmation-skip", type=int, default=2,
                   help="Frames on each side of the trigger to exclude from both plateaus "
                        "(default 2). Prevents transition frames themselves from polluting "
                        "either plateau.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.frames_dir.is_dir():
        print(f"error: {args.frames_dir} is not a directory", file=sys.stderr)
        return 1

    extensions = tuple(e.strip() for e in args.ext.split(",") if e.strip())
    print(f"loading frames from {args.frames_dir}...")
    frames, order_desc = order_frames(args.frames_dir, extensions, args.rename_log)
    print(f"  {len(frames)} frames, ordering: {order_desc}")
    if len(frames) < 2:
        print("error: need at least 2 frames", file=sys.stderr)
        return 1

    print("computing diffs (previous-frame mode)...")
    raw_prev = compute_diffs_previous(frames)
    print("computing diffs (buffer-mean mode)...")
    raw_buf = compute_diffs_buffer_mean(frames, buffer_size=args.buffer_size)

    primary_raw = raw_prev if args.mode == "previous" else raw_buf
    smoothed = smooth(primary_raw, args.smooth_window)

    adaptive_thresholds = None
    if args.adaptive_sigma is not None:
        print(
            f"computing adaptive thresholds (μ + {args.adaptive_sigma}σ, "
            f"floor={args.adaptive_floor}, history={args.adaptive_history}, "
            f"warmup={args.adaptive_warmup})..."
        )
        adaptive_thresholds, flagged = adaptive_threshold_per_frame(
            smoothed,
            sigma=args.adaptive_sigma,
            floor=args.adaptive_floor,
            history=args.adaptive_history,
            warmup=args.adaptive_warmup,
            fallback_threshold=args.threshold,
        )
        flagged[0] = False
    else:
        flagged = smoothed > args.threshold
        flagged[0] = False

    # Plateau-shift confirmation runs after flagging, on the grouped
    # events. Uses primary_raw for event grouping so the peak_idx aligns
    # with what the timeseries/gallery display.
    confirmation_results = None
    if args.confirmation_threshold is not None:
        events_for_confirmation = group_events(flagged, primary_raw, frames)
        print(
            f"running plateau-shift confirmation on {len(events_for_confirmation)} "
            f"events (threshold={args.confirmation_threshold}, "
            f"metric={args.confirmation_metric}, "
            f"pre/post={args.confirmation_pre_window}/{args.confirmation_post_window}, "
            f"skip={args.confirmation_skip})..."
        )
        confirmation_results = confirm_events_offline(
            events_for_confirmation,
            frames,
            pre_window=args.confirmation_pre_window,
            post_window=args.confirmation_post_window,
            skip=args.confirmation_skip,
            threshold=args.confirmation_threshold,
            metric=args.confirmation_metric,
        )
        confirmed = sum(1 for r in confirmation_results if r.confirmed)
        rejected = sum(
            1 for r in confirmation_results
            if r.reason == "rejected_below_threshold"
        )
        oor = sum(
            1 for r in confirmation_results
            if r.reason == "windows_out_of_range"
        )
        print(
            f"  confirmation: {confirmed} confirmed, {rejected} rejected, "
            f"{oor} out-of-range"
        )

    inputs = ReportInputs(
        frames_dir=args.frames_dir,
        frames=frames,
        order_desc=order_desc,
        raw_previous=raw_prev,
        raw_buffer_mean=raw_buf,
        smoothed=smoothed,
        threshold=args.threshold,
        smooth_window=args.smooth_window,
        buffer_size=args.buffer_size,
        primary_mode=args.mode,
        flagged=flagged,
        adaptive_sigma=args.adaptive_sigma,
        adaptive_thresholds=adaptive_thresholds,
        confirmation_results=confirmation_results,
        confirmation_threshold=args.confirmation_threshold,
        confirmation_metric=args.confirmation_metric if confirmation_results else None,
    )

    print("rendering HTML...")
    html = assemble_html(inputs)
    out_path = args.output or args.frames_dir.parent / "validation_report.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"\nwrote: {out_path}")
    print(f"size:  {out_path.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
