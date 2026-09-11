#!/usr/bin/env python3
"""Characterize what a growth session actually *contains*, for dataset triage.

Complements ``scripts/audit_session_sensor_log.py``, which checks ADS schema
compliance.  This script answers a different question: **is this session worth
its gigabytes, and what experiments can it support?**

Reports per session:

1. **Identity** — grower, sample id, chamber, and the four capture modes.
2. **Row counts** — every session CSV, so tests/aborted runs are obvious.
3. **Populated channels** — for every sensor column: population %, distinct
   value count, and numeric range.  A column that exists but never varies is
   reported as CONSTANT, which is the common failure mode (dummy drivers and
   unwired sources both produce a populated-but-useless column).
4. **Frame inventory** — counts by capture surface, total bytes, and the
   implied cadence.
5. **Trajectory shape** — min/peak/max temperature, time at peak, and the
   up-ramp / down-ramp split.
6. **Matched-pair count** — how many up-ramp frames have a temperature-matched
   down-ramp partner.  This is the feasibility number for the path-dependence
   experiment: two frames at the same temperature but opposite ramp direction
   should differ, because STO reconstruction is a memory of the anneal rather
   than a function of instantaneous temperature.

Exit code is always 0 — this is a description, not a test.

Usage:
    python scripts/audit_session_inventory.py path/to/session_dir
    python scripts/audit_session_inventory.py path/to/parent/ --glob
    python scripts/audit_session_inventory.py path/to/session --json out.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Optional

# Sensor columns are reported in full, but these lead the summary because they
# are the ones that decide which experiments a session can support.
HEADLINE_COLUMNS = (
    "pyrometer_temp_C",
    "chamber_pressure_mbar",
    "mistral_v_actual_V",
    "mistral_i_actual_A",
    "substrate_temp_pv_C",
    "substrate_temp_setpoint_C",
    "cell1_T_C",
    "plasma_forward_W",
)

SESSION_CSVS = (
    "sensor_log.csv",
    "commit_log.csv",
    "auto_capture_events.csv",
    "manual_events.csv",
    "heartbeat_log.csv",
    "rheed_view_events.csv",
    "set_change_events.csv",
    "events_labels.csv",
    "live_labels.csv",
    "human_primary_labels.csv",
)

# Frame filename prefixes -> capture surface, per gui/growth_logger.py naming.
FRAME_SURFACES = {
    "entry_": "LOG ENTRY",
    "heartbeat_": "heartbeat",
    "manual_event_": "MARK EVENT",
    "rheed_view_event_": "view/QC event",
    "live_label_": "live label",
    "buf_": "auto-capture buffer",
}

MATCH_TOLERANCE_C = 15.0


def _as_float(value: Any) -> Optional[float]:
    try:
        text = str(value).strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as stream:
        return list(csv.DictReader(stream))


def _column_report(rows: list[dict]) -> list[dict]:
    """Population, distinctness and range for every column present."""
    if not rows:
        return []
    report = []
    for column in rows[0].keys():
        values = [r.get(column, "") for r in rows]
        present = [v for v in values if v not in ("", None)]
        if not present:
            continue
        distinct = len(set(present))
        numeric = [f for f in (_as_float(v) for v in present) if f is not None]
        entry = {
            "column": column,
            "population_pct": 100.0 * len(present) / len(values),
            "distinct": distinct,
            "constant": distinct == 1,
        }
        if numeric:
            entry["min"] = min(numeric)
            entry["max"] = max(numeric)
        report.append(entry)
    return report


def _frame_inventory(session_dir: Path) -> dict:
    frames_dir = session_dir / "frames"
    if not frames_dir.is_dir():
        return {"total": 0, "bytes": 0, "by_surface": {}}
    by_surface: dict[str, int] = {}
    total_bytes = 0
    total = 0
    for path in frames_dir.rglob("*.bmp"):
        total += 1
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
        label = next(
            (name for prefix, name in FRAME_SURFACES.items()
             if path.name.startswith(prefix)),
            "other",
        )
        by_surface[label] = by_surface.get(label, 0) + 1
    return {"total": total, "bytes": total_bytes, "by_surface": by_surface}


def _trajectory_shape(rows: list[dict]) -> dict:
    """Ramp structure and the path-dependence feasibility count."""
    series = []
    for row in rows:
        temp = _as_float(row.get("pyrometer_temp_C"))
        elapsed = _as_float(row.get("elapsed_s"))
        if temp is not None and elapsed is not None:
            series.append((elapsed, temp))
    if len(series) < 3:
        return {"usable": False}

    series.sort()
    times = [s[0] for s in series]
    temps = [s[1] for s in series]
    peak_index = max(range(len(temps)), key=lambda i: temps[i])

    up = temps[: peak_index + 1]
    down = temps[peak_index:]
    matched = sum(
        1 for tu in up
        if any(abs(tu - td) <= MATCH_TOLERANCE_C for td in down)
    )

    # A run that never came back down was likely abandoned. Those are still
    # valuable -- an aborted growth is an implicit negative outcome label --
    # but they cannot support the matched-pair experiment.
    descended = (temps[peak_index] - temps[-1]) > 100.0

    return {
        "usable": True,
        "n_samples": len(series),
        "duration_min": (times[-1] - times[0]) / 60.0,
        "temp_min": min(temps),
        "temp_max": max(temps),
        "peak_at_min": times[peak_index] / 60.0,
        "up_ramp_samples": len(up),
        "down_ramp_samples": len(down),
        "matched_pairs": matched,
        "descended": descended,
        "complete": descended and peak_index > 0,
    }


def audit(session_dir: Path) -> dict:
    metadata_path = session_dir / "session_metadata.json"
    metadata = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}

    counts = {name: len(_read_rows(session_dir / name)) for name in SESSION_CSVS}
    sensor_rows = _read_rows(session_dir / "sensor_log.csv")

    return {
        "session": session_dir.name,
        "metadata": {
            key: metadata.get(key, "")
            for key in ("grower", "sample_id", "chamber_id", "camera_mode",
                        "pyrometer_mode", "mistral_mode", "evap_mode")
        },
        "row_counts": counts,
        "columns": _column_report(sensor_rows),
        "frames": _frame_inventory(session_dir),
        "trajectory": _trajectory_shape(sensor_rows),
    }


def render(report: dict) -> None:
    print(f"\n{'=' * 72}")
    print(f"SESSION  {report['session']}")
    print("=" * 72)

    meta = {k: v for k, v in report["metadata"].items() if v}
    print("  identity: " + (", ".join(f"{k}={v}" for k, v in meta.items())
                            if meta else "(no session_metadata.json)"))

    counts = {k: v for k, v in report["row_counts"].items() if v}
    print("  rows:     " + (", ".join(f"{k.replace('.csv','')}={v}"
                                      for k, v in counts.items()) or "none"))

    frames = report["frames"]
    if frames["total"]:
        mb = frames["bytes"] / (1024 * 1024)
        surfaces = ", ".join(f"{k}={v}" for k, v in sorted(frames["by_surface"].items()))
        print(f"  frames:   {frames['total']} ({mb:.1f} MB) — {surfaces}")
    else:
        print("  frames:   none")

    traj = report["trajectory"]
    if traj.get("usable"):
        verdict = "COMPLETE" if traj["complete"] else "INCOMPLETE (no descent)"
        print(f"  traject.: {traj['temp_min']:.0f}–{traj['temp_max']:.0f} °C over "
              f"{traj['duration_min']:.0f} min, peak at {traj['peak_at_min']:.0f} min "
              f"[{verdict}]")
        print(f"            up={traj['up_ramp_samples']} down={traj['down_ramp_samples']} "
              f"matched-pairs(±{MATCH_TOLERANCE_C:.0f}°C)={traj['matched_pairs']}")
    else:
        print("  traject.: not reconstructable (need pyrometer_temp_C + elapsed_s)")

    varying = [c for c in report["columns"] if not c["constant"]]
    constant = [c for c in report["columns"] if c["constant"]]
    print(f"  channels: {len(varying)} varying, {len(constant)} constant, "
          f"{len(report['columns'])} populated")

    print("\n  headline channels:")
    by_name = {c["column"]: c for c in report["columns"]}
    for name in HEADLINE_COLUMNS:
        entry = by_name.get(name)
        if entry is None:
            print(f"    {name:28s} —  absent/empty")
        elif entry["constant"]:
            print(f"    {name:28s} CONSTANT ({entry.get('min', '?')})")
        else:
            rng = (f"{entry.get('min', float('nan')):.4g} … "
                   f"{entry.get('max', float('nan')):.4g}")
            print(f"    {name:28s} {entry['population_pct']:5.1f}%  "
                  f"{entry['distinct']:5d} distinct  {rng}")

    extra = [c for c in varying if c["column"] not in HEADLINE_COLUMNS]
    if extra:
        print(f"\n  other varying channels ({len(extra)}):")
        for entry in extra:
            print(f"    {entry['column']:36s} {entry['population_pct']:5.1f}%  "
                  f"{entry['distinct']:5d} distinct")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("path", type=Path, help="session directory (or parent with --glob)")
    parser.add_argument("--glob", action="store_true",
                        help="treat PATH as a parent and audit every subdirectory")
    parser.add_argument("--json", type=Path, default=None,
                        help="also write the full report as JSON")
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"error: {args.path} does not exist", file=sys.stderr)
        return 2

    if args.glob:
        targets = sorted(p for p in args.path.iterdir() if p.is_dir())
    else:
        targets = [args.path]

    reports = []
    for target in targets:
        report = audit(target)
        reports.append(report)
        render(report)

    if args.json:
        args.json.write_text(json.dumps(reports, indent=2, default=str))
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
