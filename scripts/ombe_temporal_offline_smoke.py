#!/usr/bin/env python3
"""Generate and validate a hardware-free O-MBE temporal session fixture."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gui.growth_logger import GrowthLogger
from gui.state import CameraState, EvapControlState, MistralState, PyrometerState
from gui.temporal_observability import snapshot_state, synchronization_summary
from scripts.ombe_timing_probe import build_summary, render_markdown
from scripts.validate_temporal_session import validate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)

    logger = GrowthLogger(base_dir=str(output / "sessions"))
    logger.start_session("TEMPORAL_SMOKE")
    session = logger.session_dir
    now_ns = time.perf_counter_ns()
    now_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    delays_ms = {
        "rheed": 0,
        "pyrometer": 100,
        "mistral": 500,
        "evap": 1500,
    }
    states = {
        "rheed": CameraState(
            connected=True, valid=True, mode="screengrab",
            capture_backend="wgc", capture_sequence=1, sample_sequence=1,
            captured_at_utc=now_utc, received_at_utc=now_utc,
            captured_monotonic_ns=now_ns,
            received_monotonic_ns=now_ns,
            worker_emitted_monotonic_ns=now_ns,
            gui_received_monotonic_ns=now_ns,
        ),
        "pyrometer": PyrometerState(
            connected=True, valid=True, mode="exactus", temperature=500.0,
            sample_sequence=1, received_at_utc=now_utc,
            received_monotonic_ns=now_ns - delays_ms["pyrometer"] * 1_000_000,
            worker_emitted_monotonic_ns=now_ns - 99_000_000,
            gui_received_monotonic_ns=now_ns - 98_000_000,
            read_duration_ms=500.0, sample_span_ms=400.0,
        ),
        "mistral": MistralState(
            connected=True, valid=True, mode="screengrab",
            v_set=1.0, v_actual=1.0, i_set=2.0, i_actual=2.0,
            sample_sequence=1, received_at_utc=now_utc,
            received_monotonic_ns=now_ns - delays_ms["mistral"] * 1_000_000,
            worker_emitted_monotonic_ns=now_ns - 499_000_000,
            gui_received_monotonic_ns=now_ns - 498_000_000,
            read_duration_ms=50.0,
        ),
        "evap": EvapControlState(
            connected=True, valid=True, mode="elog",
            chamber_pressure_mbar=2e-9, sample_sequence=1,
            source_at_utc=now_utc, received_at_utc=now_utc,
            received_monotonic_ns=now_ns - delays_ms["evap"] * 1_000_000,
            worker_emitted_monotonic_ns=now_ns - 1_499_000_000,
            gui_received_monotonic_ns=now_ns - 1_498_000_000,
            read_duration_ms=5.0,
        ),
    }
    snapshots = {
        source: snapshot_state(source, state, now_ns)
        for source, state in states.items()
    }
    sync = synchronization_summary(snapshots)
    for source, timing in snapshots.items():
        logger.log_temporal_event(
            "state_received", source, timing=timing.to_dict(),
        )
    logger.log_sensors(
        500.0, 1.0,
        v_set=1.0, v_actual=1.0, i_set=2.0, i_actual=2.0,
        chamber_pressure_mbar=2e-9,
        snapshot_at_utc=now_utc,
        snapshot_monotonic_ns=now_ns,
        sync_span_ms=sync["sync_span_ms"],
        sync_complete=sync["sync_complete"],
        sync_valid=sync["sync_valid"],
        rheed_timing={
            **snapshots["rheed"].to_dict(),
            "capture_backend": "wgc", "source_hwnd": 123,
        },
        pyrometer_timing=snapshots["pyrometer"].to_dict(),
        mistral_timing=snapshots["mistral"].to_dict(),
        evap_timing=snapshots["evap"].to_dict(),
        pyrometer_received_at_utc=now_utc,
        pyrometer_sample_sequence=1,
        pyrometer_age_ms=delays_ms["pyrometer"],
        pyrometer_read_duration_ms=500.0,
        mistral_received_at_utc=now_utc,
        mistral_sample_sequence=1,
        mistral_age_ms=delays_ms["mistral"],
        mistral_read_duration_ms=50.0,
        evap_source_at_utc=now_utc,
        evap_received_at_utc=now_utc,
        evap_sample_sequence=1,
        evap_age_ms=delays_ms["evap"],
        evap_read_duration_ms=5.0,
    )
    logger.end_session()

    with (session / "sensor_log.csv").open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    summary = build_summary(
        session / "sensor_log.csv", rows, fields,
        metadata={"notes": "hardware-free deterministic smoke"},
        capture={
            "mode": "offline-smoke", "started_at_utc": now_utc,
            "ended_at_utc": now_utc, "requested_duration_s": 0,
        },
        repo_root=Path(__file__).resolve().parent.parent,
        temporal_trace=session / "temporal_trace.jsonl",
        operator_actions=session / "operator_actions.jsonl",
    )
    validation = validate(session)
    (output / "timing_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    (output / "timing_report.md").write_text(
        render_markdown(summary), encoding="utf-8",
    )
    (output / "session_validation.json").write_text(
        json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    if not validation["passed"]:
        print(json.dumps(validation, indent=2, sort_keys=True))
        return 1
    if summary["cross_source"]["sync_span_ms"]["p50"] != 1500.0:
        print("unexpected synthetic sync span")
        return 1
    print(f"PASS: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
