#!/usr/bin/env python3
"""Derive the Copilot's latency budget from a real recorded session.

Motivation: the Copilot's hosting question -- local model on the lab PC, hosted
API, or an adjacent machine -- is usually argued from opinions about the
hardware. It cannot be settled that way, because nobody has stated what "fast
enough" means as a number. This script produces that number from a session we
actually recorded, so the hardware measurement has something to be compared
against.

The budget is set by the **trigger policy**, not by the hardware:

  * per-frame           -- one advisory per captured frame
  * auto-capture only   -- one per auto-capture event
  * all events          -- auto-capture plus set-change

These differ by more than an order of magnitude, and the choice is ours.

The central finding this script surfaces: auto-capture events are *engine
refractory*. ``AutoCaptureEngine`` takes a ``cooldown_s`` (``gui/auto_capture.py``,
default 5.0) and enforces it before firing, so consecutive auto-capture events
cannot be closer than that. The budget for an auto-capture-triggered copilot is
therefore a **guaranteed floor we control**, not a statistical property we hope
holds. If a candidate model needs 20 s, raising the cooldown to 20 s is a
legitimate answer -- the constraint moves to what the science can tolerate.

Set-change events have no such floor. They arrive in bursts when an operator
changes several setpoints together, so a policy that triggers on them needs
coalescing rather than raw speed. The report prints the low percentiles
precisely so that mean inter-arrival -- which is misleading here -- is not what
anyone plans against.

Decode arithmetic: CPU generation is memory-bandwidth-bound, so
``tokens/s ~= effective bandwidth / resident model size``. Prefill is
compute-bound instead, which is why a large projected state view can dominate
the budget on an old CPU and why bounding that view is a performance measure as
well as an auditability one.

Usage:
    python scripts/copilot_latency_budget.py path/to/session
    python scripts/copilot_latency_budget.py path/to/session --advisory-tokens 150
    python scripts/copilot_latency_budget.py path/to/session --json out.json
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Optional

# Event sources that could plausibly trigger an advisory. Frame-level capture is
# handled separately because it is not an "event" file.
EVENT_SOURCES = {
    "auto_capture": "auto_capture_events.csv",
    "set_change": "set_change_events.csv",
    "manual": "manual_events.csv",
    "commit": "commit_log.csv",
}

# BMP is the recorded frame format on Bulbasaur; the others are defensive.
IMAGE_SUFFIXES = frozenset((".bmp", ".png", ".tif", ".tiff", ".jpg", ".jpeg"))

# Resident sizes for common quantized sizes, in GB. Q4 is assumed throughout --
# it is the usual choice for CPU serving and the one llama.cpp defaults toward.
MODEL_SIZES_GB = {
    "1B Q4": 0.8,
    "3B Q4": 2.0,
    "7B Q4": 4.5,
    "14B Q4": 9.0,
}


def _elapsed_seconds(path: Path) -> list[float]:
    """Read the ``elapsed_s`` column, sorted. Missing file -> empty list."""
    if not path.exists():
        return []
    values: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            raw = row.get("elapsed_s")
            if not raw:
                continue
            try:
                values.append(float(raw))
            except ValueError:
                continue
    return sorted(values)


def _gap_stats(events: list[float]) -> Optional[dict[str, Any]]:
    """Inter-arrival statistics. Low percentiles matter more than the mean."""
    if len(events) < 2:
        return None
    gaps = sorted(b - a for a, b in zip(events, events[1:]))
    count = len(gaps)

    def percentile(p: float) -> float:
        return gaps[int(p / 100 * (count - 1))]

    return {
        "event_count": len(events),
        "span_s": round(events[-1] - events[0], 1),
        "mean_gap_s": round(statistics.mean(gaps), 1),
        "median_gap_s": round(statistics.median(gaps), 1),
        "p10_gap_s": round(percentile(10), 2),
        "p05_gap_s": round(percentile(5), 2),
        "min_gap_s": round(min(gaps), 2),
        "gaps_under_5s": sum(1 for g in gaps if g < 5),
        "gaps_under_10s": sum(1 for g in gaps if g < 10),
        # A floor this clean is a mechanism, not a coincidence: it is the
        # AutoCaptureEngine cooldown showing through.
        "apparent_floor_s": round(min(gaps), 2) if min(gaps) >= 1.0 else None,
    }


def _frame_cadence(session: Path) -> Optional[dict[str, Any]]:
    """Heartbeat rows plus buffered auto-capture frames, over the session span."""
    heartbeat = _elapsed_seconds(session / "heartbeat_log.csv")
    frames_dir = session / "frames"
    buffered = 0
    if frames_dir.is_dir():
        for child in frames_dir.iterdir():
            if child.is_dir():
                # Each auto-capture buffer also holds capture_manifest.csv and a
                # metadata JSON; counting those as frames inflates the cadence.
                buffered += sum(
                    1 for item in child.iterdir()
                    if item.suffix.lower() in IMAGE_SUFFIXES
                )
    total = len(heartbeat) + buffered
    if not heartbeat or total == 0:
        return None
    span = heartbeat[-1] - heartbeat[0]
    if span <= 0:
        return None
    return {
        "heartbeat_frames": len(heartbeat),
        "buffered_frames": buffered,
        "total_frames": total,
        "span_s": round(span, 1),
        "frames_per_s": round(total / span, 2),
        "budget_ms_per_frame": round(span / total * 1000, 1),
    }


def _required_bandwidth(budget_s: float, advisory_tokens: int) -> dict[str, Any]:
    """What each model size demands to meet this budget on decode alone.

    Deliberately optimistic: it ignores prefill entirely. A model that only just
    clears this bar will miss it in practice once the state view is read.
    """
    if budget_s <= 0:
        return {}
    tokens_per_s = advisory_tokens / budget_s
    return {
        name: {
            "tokens_per_s_needed": round(tokens_per_s, 1),
            "bandwidth_gb_s_needed": round(tokens_per_s * size_gb, 1),
        }
        for name, size_gb in MODEL_SIZES_GB.items()
    }


def analyze(session: Path, advisory_tokens: int) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    all_events: list[float] = []
    for name, filename in EVENT_SOURCES.items():
        events = _elapsed_seconds(session / filename)
        stats = _gap_stats(events)
        if stats:
            sources[name] = stats
        all_events.extend(events)

    combined = _gap_stats(sorted(all_events))
    auto_only = sources.get("auto_capture")
    frames = _frame_cadence(session)

    policies: dict[str, Any] = {}
    if frames:
        policies["per_frame"] = {
            "budget_s": round(frames["budget_ms_per_frame"] / 1000, 3),
            "basis": "one advisory per captured frame",
            "requirements": _required_bandwidth(
                frames["budget_ms_per_frame"] / 1000, advisory_tokens),
        }
    if auto_only:
        # The guaranteed floor is the honest planning number, not the median.
        budget = auto_only["min_gap_s"]
        policies["auto_capture_only"] = {
            "budget_s": budget,
            "basis": "engine-refractory floor (AutoCaptureEngine cooldown_s)",
            "median_gap_s": auto_only["median_gap_s"],
            "requirements": _required_bandwidth(budget, advisory_tokens),
        }
    if combined:
        policies["all_events"] = {
            "budget_s": combined["p05_gap_s"],
            "basis": "p05 inter-arrival; bursty, needs coalescing not speed",
            "median_gap_s": combined["median_gap_s"],
            "requirements": _required_bandwidth(
                max(combined["p05_gap_s"], 0.001), advisory_tokens),
        }

    return {
        "session": str(session),
        "advisory_tokens": advisory_tokens,
        "frame_cadence": frames,
        "event_sources": sources,
        "combined_events": combined,
        "trigger_policies": policies,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"# Copilot latency budget — {Path(report['session']).name}",
        "",
        f"Advisory length assumed: {report['advisory_tokens']} tokens.",
        "",
        "## Event cadence",
        "",
        "| Source | Events | Mean gap | Median | p10 | p05 | Min |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, s in report["event_sources"].items():
        lines.append(
            f"| {name} | {s['event_count']} | {s['mean_gap_s']} s | "
            f"{s['median_gap_s']} s | {s['p10_gap_s']} s | {s['p05_gap_s']} s | "
            f"{s['min_gap_s']} s |"
        )
    c = report.get("combined_events")
    if c:
        lines.append(
            f"| **combined** | {c['event_count']} | {c['mean_gap_s']} s | "
            f"{c['median_gap_s']} s | {c['p10_gap_s']} s | {c['p05_gap_s']} s | "
            f"{c['min_gap_s']} s |"
        )
        lines += [
            "",
            f"Combined gaps under 5 s: {c['gaps_under_5s']}; under 10 s: "
            f"{c['gaps_under_10s']}. **Plan against the low percentiles, not the "
            "mean** — the mean is inflated by long quiet stretches while the "
            "load arrives in bursts.",
        ]

    frames = report.get("frame_cadence")
    if frames:
        lines += [
            "",
            "## Frame cadence",
            "",
            f"- {frames['total_frames']} frames "
            f"({frames['heartbeat_frames']} heartbeat + "
            f"{frames['buffered_frames']} buffered) over {frames['span_s']} s",
            f"- {frames['frames_per_s']} frames/s → "
            f"{frames['budget_ms_per_frame']} ms per frame",
        ]

    lines += ["", "## Budget by trigger policy", ""]
    for name, policy in report["trigger_policies"].items():
        lines += [
            f"### {name} — {policy['budget_s']} s",
            "",
            f"Basis: {policy['basis']}.",
            "",
            "| Model | tokens/s needed | Memory bandwidth needed | Measured |",
            "|---|---|---|---|",
        ]
        for model, need in policy["requirements"].items():
            lines.append(
                f"| {model} | {need['tokens_per_s_needed']} | "
                f"{need['bandwidth_gb_s_needed']} GB/s | _____ |"
            )
        lines.append("")

    lines += [
        "## How to use this",
        "",
        "Fill the **Measured** column from `machine_capability_probe.py`'s",
        "`memory_copy` figure. A row clears only if measured bandwidth exceeds",
        "the requirement — and note these requirements ignore prefill, which is",
        "compute-bound and can dominate on an old CPU. Treat a row that only",
        "just clears as failing.",
        "",
        "The auto-capture budget is set by `AutoCaptureEngine`'s `cooldown_s`",
        "(`gui/auto_capture.py`), so it is a parameter rather than a constraint:",
        "if no model clears the bar, raising the cooldown is a legitimate move,",
        "and the real question becomes what spacing the science tolerates.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session", type=Path, help="recorded session directory")
    parser.add_argument("--advisory-tokens", type=int, default=100,
                        help="assumed advisory length in tokens (default: 100)")
    parser.add_argument("--json", type=Path, default=None,
                        help="also write the raw report to this path")
    args = parser.parse_args(argv)

    if not args.session.is_dir():
        print(f"not a directory: {args.session}", file=sys.stderr)
        return 2

    report = analyze(args.session, args.advisory_tokens)
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
