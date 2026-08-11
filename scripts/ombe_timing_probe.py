#!/usr/bin/env python3
"""Observe an O-MBE ``sensor_log.csv`` and summarize timing provenance.

The probe is deliberately passive: it never imports an instrument driver and
never changes GUI or hardware state. By default it records the rows appended
during a one-hour window, then writes a machine-readable JSON summary and a
Markdown report. ``--no-wait`` analyzes all rows already present, which is
useful for regression tests and post-session analysis.

The GUI sensor log is a ~1 Hz latest-state snapshot. A faster worker can advance
its sequence more than once between rows, so the report calls its interval a
"sequence-normalized estimate" and reports skipped intermediate samples.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import statistics
import subprocess
import time
from pathlib import Path
from typing import Iterable, Optional


INSTRUMENTS = {
    "rheed": {
        "values": ["rheed_sample_sequence"],
    },
    "pyrometer": {
        "values": ["pyrometer_temp_C"],
    },
    "mistral": {
        "values": [
            "mistral_v_set_V",
            "mistral_v_actual_V",
            "mistral_i_set_A",
            "mistral_i_actual_A",
        ],
    },
    "evap": {
        "values": [
            "chamber_pressure_mbar",
            "substrate_temp_pv_C",
            "substrate_temp_setpoint_C",
            "cell_HTEC2_pv_C",
            "cell_Y_pv_C",
            "cell_Sr_pv_C",
            "cell_Eu_pv_C",
            "cell_Er_pv_C",
            "plasma_dc_bias_V",
            "plasma_forward_W",
            "plasma_reflected_W",
        ],
    },
}


def _parse_float(value: object) -> Optional[float]:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_int(value: object) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_utc(value: object) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _percentile(values: list[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _stats(values: Iterable[float]) -> dict[str, Optional[float] | int]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "count": len(finite),
        "min": min(finite) if finite else None,
        "mean": statistics.fmean(finite) if finite else None,
        "p50": _percentile(finite, 0.50),
        "p95": _percentile(finite, 0.95),
        "p99": _percentile(finite, 0.99),
        "max": max(finite) if finite else None,
    }


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def _read_jsonl(path: Optional[Path]) -> tuple[list[dict], int]:
    if path is None or not path.exists():
        return [], 0
    records: list[dict] = []
    parse_errors = 0
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                parse_errors += 1
    return records, parse_errors


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def summarize_instrument(
    rows: list[dict[str, str]],
    prefix: str,
    value_fields: list[str],
) -> dict:
    received_field = f"{prefix}_received_at_utc"
    source_field = f"{prefix}_source_at_utc"
    sequence_field = f"{prefix}_sample_sequence"
    age_field = f"{prefix}_age_ms"
    duration_field = f"{prefix}_read_duration_ms"

    missing_provenance = 0
    value_missing = 0
    reused = 0
    unobserved_intermediate = 0
    sequence_resets = 0
    ages: list[float] = []
    intervals: list[float] = []
    durations: list[float] = []
    worker_to_gui: list[float] = []
    source_offsets: list[float] = []
    source_intervals: list[float] = []
    reused_source_timestamps = 0
    unique_samples = 0
    previous_pair: Optional[tuple[int, str]] = None
    previous_sequence: Optional[int] = None
    previous_received: Optional[dt.datetime] = None
    previous_source: Optional[dt.datetime] = None

    for row in rows:
        sequence = _parse_int(row.get(sequence_field))
        received_text = str(row.get(received_field, "")).strip()
        received = _parse_utc(received_text)
        if sequence is None or sequence <= 0 or received is None:
            missing_provenance += 1
        if not any(str(row.get(field, "")).strip() for field in value_fields):
            value_missing += 1

        age = _parse_float(row.get(age_field))
        if age is not None:
            ages.append(age)

        if sequence is None or sequence <= 0 or received is None:
            continue
        pair = (sequence, received_text)
        if pair == previous_pair:
            reused += 1
            continue

        unique_samples += 1
        duration = _parse_float(row.get(duration_field))
        if duration is not None:
            durations.append(duration)
        queue_delay = _parse_float(row.get(f"{prefix}_worker_to_gui_ms"))
        if queue_delay is not None:
            worker_to_gui.append(queue_delay)

        source = _parse_utc(row.get(source_field))
        if source is not None:
            source_offsets.append((received - source).total_seconds() * 1000.0)
            if previous_source is not None:
                source_delta = (source - previous_source).total_seconds()
                if source_delta > 0:
                    source_intervals.append(source_delta)
                elif source_delta == 0:
                    reused_source_timestamps += 1
            previous_source = source

        if previous_sequence is not None and previous_received is not None:
            sequence_delta = sequence - previous_sequence
            elapsed_s = (received - previous_received).total_seconds()
            if sequence_delta > 0 and elapsed_s >= 0:
                # Logger snapshots may skip worker emissions. Dividing by the
                # sequence advance estimates the mean interval across that gap.
                intervals.append(elapsed_s / sequence_delta)
                unobserved_intermediate += max(0, sequence_delta - 1)
            elif sequence_delta <= 0:
                sequence_resets += 1

        previous_pair = pair
        previous_sequence = sequence
        previous_received = received

    row_count = len(rows)

    def _rate(count: int) -> Optional[float]:
        return 100.0 * count / row_count if row_count else None

    interval_stats = _stats(intervals)
    interval_p95 = interval_stats["p95"]
    stale_audit_threshold_ms = max(
        3000.0,
        2.0 * interval_p95 * 1000.0 if interval_p95 is not None else 0.0,
    )
    stale_valid_rows = sum(
        str(row.get(f"{prefix}_valid", "")).lower() == "true"
        and (age := _parse_float(row.get(age_field))) is not None
        and age > stale_audit_threshold_ms
        for row in rows
    )
    return {
        "row_count": row_count,
        "unique_observed_samples": unique_samples,
        "missing_provenance_rows": missing_provenance,
        "missing_provenance_percent": _rate(missing_provenance),
        "value_missing_rows": value_missing,
        "value_missing_percent": _rate(value_missing),
        "reused_sequence_rows": reused,
        "reused_sequence_percent": _rate(reused),
        "unobserved_intermediate_samples": unobserved_intermediate,
        "sequence_resets": sequence_resets,
        "sequence_normalized_interval_s": interval_stats,
        "read_duration_ms": _stats(durations),
        "worker_to_gui_ms": _stats(worker_to_gui),
        "logged_age_ms": _stats(ages),
        "source_to_receive_offset_ms": _stats(source_offsets),
        "source_update_interval_s": _stats(source_intervals),
        "reused_source_timestamp_rows": reused_source_timestamps,
        "stale_audit_threshold_ms": stale_audit_threshold_ms,
        "stale_valid_rows": stale_valid_rows,
    }


def summarize_cross_source(rows: list[dict[str, str]]) -> dict:
    spans = [
        value for row in rows
        if (value := _parse_float(row.get("sync_span_ms"))) is not None
    ]
    intervals = [
        value for row in rows
        if (value := _parse_float(
            row.get("snapshot_timer_interval_ms"),
        )) is not None
    ]
    jitter = [
        value for row in rows
        if (value := _parse_float(
            row.get("snapshot_timer_jitter_ms"),
        )) is not None
    ]
    complete = sum(
        str(row.get("sync_complete", "")).lower() == "true" for row in rows
    )
    valid = sum(
        str(row.get("sync_valid", "")).lower() == "true" for row in rows
    )
    return {
        "sync_span_ms": _stats(spans),
        "snapshot_timer_interval_ms": _stats(intervals),
        "snapshot_timer_jitter_ms": _stats(jitter),
        "sync_complete_rows": complete,
        "sync_valid_rows": valid,
        "row_count": len(rows),
    }


def summarize_trace(records: list[dict], parse_errors: int) -> dict:
    by_source: dict[str, list[int]] = {}
    classifier_capture_ms: list[float] = []
    classifier_inference_ms: list[float] = []
    frame_write_ms: list[float] = []
    frame_age_ms: list[float] = []
    save_operation_values: dict[str, dict[str, list[float]]] = {
        "heartbeat": {"write_ms": [], "source_age_ms": []},
        "manual_event": {"write_ms": [], "source_age_ms": []},
        "equalizer": {"write_ms": [], "source_age_ms": []},
    }
    csv_flush_ms: list[float] = []
    event_loop_ms: list[float] = []
    rss_bytes: list[float] = []
    os_threads: list[float] = []
    for record in records:
        event = str(record.get("event", ""))
        source = str(record.get("source", ""))
        timing = record.get("timing") or {}
        details = record.get("details") or {}
        if event == "state_received":
            received = _parse_int(timing.get("received_monotonic_ns"))
            valid = bool(timing.get("valid"))
            if received is not None and valid:
                by_source.setdefault(source, []).append(received)
        elif event == "classifier_state":
            if (value := _parse_float(
                details.get("capture_to_inference_complete_ms"),
            )) is not None:
                classifier_capture_ms.append(value)
            if (value := _parse_float(details.get("inference_ms"))) is not None:
                classifier_inference_ms.append(value)
        elif event in {
            "frame_saved", "manual_event_saved", "equalizer_label_saved",
        }:
            operation = {
                "frame_saved": "heartbeat",
                "manual_event_saved": "manual_event",
                "equalizer_label_saved": "equalizer",
            }[event]
            if (value := _parse_float(details.get("write_duration_ms"))) is not None:
                save_operation_values[operation]["write_ms"].append(value)
                if operation == "heartbeat":
                    frame_write_ms.append(value)
            if (value := _parse_float(details.get("source_age_at_save_ms"))) is not None:
                save_operation_values[operation]["source_age_ms"].append(value)
                if operation == "heartbeat":
                    frame_age_ms.append(value)
        elif event == "sensor_snapshot":
            if (value := _parse_float(details.get("csv_flush_duration_ms"))) is not None:
                csv_flush_ms.append(value)
            process = details.get("process") or {}
            if (value := _parse_float(process.get("rss_bytes"))) is not None:
                rss_bytes.append(value)
            if (value := _parse_float(process.get("os_thread_count"))) is not None:
                os_threads.append(value)
        elif event == "event_loop_turn":
            if (value := _parse_float(
                details.get("next_turn_latency_ms"),
            )) is not None:
                event_loop_ms.append(value)

    nearest: dict[str, dict] = {}
    anchors = sorted(set(by_source.get("rheed", [])))
    for source in ("pyrometer", "mistral", "evap"):
        candidates = sorted(set(by_source.get(source, [])))
        deltas: list[float] = []
        if candidates:
            import bisect
            for anchor in anchors:
                pos = bisect.bisect_left(candidates, anchor)
                neighbors = candidates[max(0, pos - 1):pos + 1]
                if neighbors:
                    deltas.append(
                        min(abs(value - anchor) for value in neighbors)
                        / 1_000_000.0
                    )
        nearest[source] = {
            "matched_anchor_count": len(deltas),
            "unmatched_anchor_count": len(anchors) - len(deltas),
            "absolute_delta_ms": _stats(deltas),
        }
    return {
        "record_count": len(records),
        "parse_errors": parse_errors,
        "rheed_anchor_count": len(anchors),
        "nearest_neighbor_to_rheed": nearest,
        "classifier_capture_to_complete_ms": _stats(classifier_capture_ms),
        "classifier_inference_ms": _stats(classifier_inference_ms),
        "frame_write_ms": _stats(frame_write_ms),
        "frame_age_at_save_ms": _stats(frame_age_ms),
        "save_operations": {
            operation: {
                "count": len(values["write_ms"]),
                "write_ms": _stats(values["write_ms"]),
                "source_age_at_save_ms": _stats(values["source_age_ms"]),
            }
            for operation, values in save_operation_values.items()
        },
        "csv_flush_ms": _stats(csv_flush_ms),
        "event_loop_turn_ms": _stats(event_loop_ms),
        "rss_bytes": _stats(rss_bytes),
        "rss_first_bytes": rss_bytes[0] if rss_bytes else None,
        "rss_last_bytes": rss_bytes[-1] if rss_bytes else None,
        "rss_delta_bytes": (
            rss_bytes[-1] - rss_bytes[0] if len(rss_bytes) >= 2 else None
        ),
        "os_thread_count": _stats(os_threads),
    }


def summarize_operator_actions(
    trace_records: list[dict], actions: list[dict], parse_errors: int,
) -> dict:
    states: dict[str, list[tuple[int, bool, str, Optional[float], int]]] = {}
    for record in trace_records:
        if record.get("event") != "state_received":
            continue
        source = str(record.get("source", ""))
        timing = record.get("timing") or {}
        recorded_ns = _parse_int(record.get("recorded_monotonic_ns"))
        if recorded_ns is None:
            continue
        states.setdefault(source, []).append((
            recorded_ns,
            bool(timing.get("valid")),
            str(timing.get("error", "")),
            _parse_float(timing.get("age_ms")),
            _parse_int(timing.get("sequence")) or 0,
        ))
    results: list[dict] = []
    for action in actions:
        action_ns = _parse_int(action.get("recorded_monotonic_ns"))
        source = str(action.get("source", ""))
        phase = str(action.get("phase", ""))
        target_valid = True if phase == "after" else False if phase == "before" else None
        match = None
        if action_ns is not None and target_valid is not None:
            for state_ns, valid, error, age_ms, sequence in sorted(
                states.get(source, []),
            ):
                if state_ns >= action_ns and valid is target_valid:
                    match = {
                        "state_monotonic_ns": state_ns,
                        "latency_ms": (state_ns - action_ns) / 1_000_000.0,
                        "valid": valid,
                        "error": error,
                        "retained_sample_age_ms": age_ms,
                        "sample_sequence": sequence,
                    }
                    break
        results.append({
            "label": action.get("label", ""),
            "phase": phase,
            "source": source,
            "action_monotonic_ns": action_ns,
            "transition": match,
        })
    return {
        "action_count": len(actions),
        "parse_errors": parse_errors,
        "transitions": results,
    }


def build_summary(
    sensor_log: Path,
    rows: list[dict[str, str]],
    fieldnames: list[str],
    metadata: dict,
    capture: dict,
    repo_root: Path,
    temporal_trace: Optional[Path] = None,
    operator_actions: Optional[Path] = None,
) -> dict:
    instruments = {
        prefix: summarize_instrument(rows, prefix, config["values"])
        for prefix, config in INSTRUMENTS.items()
    }
    expected_fields = {
        f"{prefix}_{suffix}"
        for prefix in INSTRUMENTS
        for suffix in (
            "source_at_utc",
            "received_at_utc",
            "sample_sequence",
            "age_ms",
            "read_duration_ms",
        )
    }
    trace_records, trace_parse_errors = _read_jsonl(temporal_trace)
    action_records, action_parse_errors = _read_jsonl(operator_actions)
    return {
        "schema_version": 2,
        "generated_at_utc": dt.datetime.now(
            dt.timezone.utc,
        ).isoformat(timespec="seconds"),
        "source": {
            "sensor_log": str(sensor_log.resolve()),
            "sha256": _file_sha256(sensor_log),
            "timing_columns_present": expected_fields.issubset(fieldnames),
            "missing_timing_columns": sorted(expected_fields - set(fieldnames)),
            "analyzed_row_count": len(rows),
        },
        "software": {
            "git_commit": _git_commit(repo_root),
        },
        "capture": capture,
        "metadata": metadata,
        "instruments": instruments,
        "cross_source": summarize_cross_source(rows),
        "temporal_trace": {
            "path": str(temporal_trace.resolve()) if temporal_trace else "",
            "sha256": (
                _file_sha256(temporal_trace)
                if temporal_trace and temporal_trace.exists() else ""
            ),
            **summarize_trace(trace_records, trace_parse_errors),
        },
        "operator_actions": {
            "path": str(operator_actions.resolve()) if operator_actions else "",
            **summarize_operator_actions(
                trace_records, action_records, action_parse_errors,
            ),
        },
        "interpretation": {
            "pass_fail_threshold_applied": False,
            "interval_definition": (
                "elapsed receive time divided by sample-sequence advance "
                "between observed sensor-log snapshots"
            ),
            "missing_provenance_definition": (
                "row lacks a positive sequence or parseable received_at_utc"
            ),
            "value_missing_definition": (
                "all configured value columns for the instrument are blank"
            ),
        },
    }


def _fmt(value: object, places: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{places}f}"
    return str(value)


def render_markdown(summary: dict) -> str:
    source = summary["source"]
    capture = summary["capture"]
    metadata = summary["metadata"]
    lines = [
        "# O-MBE Timing Observability Report",
        "",
        "This report is descriptive. No synchronization or pass/fail threshold "
        "has been assumed.",
        "",
        "## Run metadata",
        "",
        f"- Git commit: `{summary['software']['git_commit']}`",
        f"- Probe start (UTC): `{capture['started_at_utc']}`",
        f"- Probe end (UTC): `{capture['ended_at_utc']}`",
        f"- Requested duration: `{capture['requested_duration_s']} s`",
        f"- Analyzed rows: `{source['analyzed_row_count']}`",
        f"- Sensor log: `{source['sensor_log']}`",
        f"- Sensor log SHA-256: `{source['sha256']}`",
        f"- Timing columns present: `{source['timing_columns_present']}`",
        f"- Operator: `{metadata.get('operator') or 'not recorded'}`",
        f"- Windows/DPI: `{metadata.get('windows_dpi') or 'not recorded'}`",
        f"- GUI modes: `{metadata.get('gui_modes') or 'not recorded'}`",
        f"- Window titles: `{metadata.get('window_titles') or 'not recorded'}`",
        f"- Sampling settings: "
        f"`{metadata.get('sampling_settings') or 'not recorded'}`",
        f"- Software versions: "
        f"`{metadata.get('software_versions') or 'not recorded'}`",
        f"- Notes: `{metadata.get('notes') or 'none'}`",
        "",
    ]
    if source["missing_timing_columns"]:
        lines.extend([
            "## Schema warning",
            "",
            "The input is missing timing columns: "
            + ", ".join(f"`{name}`" for name in source["missing_timing_columns"])
            + ". Timing metrics may be unavailable.",
            "",
        ])

    lines.extend([
        "## Timing results",
        "",
        "| Instrument | Unique samples | Missing provenance | Value missing | "
        "Reused rows | Unobserved intermediate | Interval p50/p95/p99 (s) | "
        "Read p50/p95/p99 (ms) | Age p50/p95/p99 (ms) | "
        "Source offset p50/p95/p99 (ms) | "
        "Source update p50/p95/p99 (s) | Reused source rows |",
        "|---|---:|---:|---:|---:|---:|---|---|---|---|---|---:|",
    ])
    for name, result in summary["instruments"].items():
        interval = result["sequence_normalized_interval_s"]
        duration = result["read_duration_ms"]
        age = result["logged_age_ms"]
        offset = result["source_to_receive_offset_ms"]
        source_interval = result["source_update_interval_s"]
        lines.append(
            f"| {name} "
            f"| {result['unique_observed_samples']} "
            f"| {result['missing_provenance_rows']} "
            f"({_fmt(result['missing_provenance_percent'], 2)}%) "
            f"| {result['value_missing_rows']} "
            f"({_fmt(result['value_missing_percent'], 2)}%) "
            f"| {result['reused_sequence_rows']} "
            f"| {result['unobserved_intermediate_samples']} "
            f"| {_fmt(interval['p50'])} / {_fmt(interval['p95'])} / "
            f"{_fmt(interval['p99'])} "
            f"| {_fmt(duration['p50'])} / {_fmt(duration['p95'])} / "
            f"{_fmt(duration['p99'])} "
            f"| {_fmt(age['p50'])} / {_fmt(age['p95'])} / "
            f"{_fmt(age['p99'])} "
            f"| {_fmt(offset['p50'])} / {_fmt(offset['p95'])} / "
            f"{_fmt(offset['p99'])} "
            f"| {_fmt(source_interval['p50'])} / "
            f"{_fmt(source_interval['p95'])} / "
            f"{_fmt(source_interval['p99'])} "
            f"| {result['reused_source_timestamp_rows']} |"
        )

    stale_lines = [
        f"- {name}: `{result['stale_valid_rows']}` valid row(s) older than "
        f"the audit threshold `{_fmt(result['stale_audit_threshold_ms'])} ms`."
        for name, result in summary["instruments"].items()
    ]
    save_lines = [
        f"- {name}: count `{values['count']}`, write p50/p95/p99 "
        f"`{_fmt(values['write_ms']['p50'])} / "
        f"{_fmt(values['write_ms']['p95'])} / "
        f"{_fmt(values['write_ms']['p99'])} ms`, source age p50/p95/p99 "
        f"`{_fmt(values['source_age_at_save_ms']['p50'])} / "
        f"{_fmt(values['source_age_at_save_ms']['p95'])} / "
        f"{_fmt(values['source_age_at_save_ms']['p99'])} ms`"
        for name, values in (
            summary["temporal_trace"].get("save_operations") or {}
        ).items()
    ]
    if not save_lines:
        save_lines.append("- No per-operation save timing was recorded.")
    lines.extend([
        "",
        "### Stale-data audit",
        "",
        *stale_lines,
        "",
        "## Cross-source and GUI pipeline",
        "",
        f"- Sync span p50/p95/p99 (ms): "
        f"`{_fmt(summary['cross_source']['sync_span_ms']['p50'])} / "
        f"{_fmt(summary['cross_source']['sync_span_ms']['p95'])} / "
        f"{_fmt(summary['cross_source']['sync_span_ms']['p99'])}`",
        f"- Structurally valid snapshots: "
        f"`{summary['cross_source']['sync_valid_rows']} / "
        f"{summary['cross_source']['row_count']}`",
        f"- Temporal trace parse errors: "
        f"`{summary['temporal_trace']['parse_errors']}`",
        f"- Classifier capture-to-complete p50/p95/p99 (ms): "
        f"`{_fmt(summary['temporal_trace']['classifier_capture_to_complete_ms']['p50'])} / "
        f"{_fmt(summary['temporal_trace']['classifier_capture_to_complete_ms']['p95'])} / "
        f"{_fmt(summary['temporal_trace']['classifier_capture_to_complete_ms']['p99'])}`",
        f"- CSV flush p50/p95/p99 (ms): "
        f"`{_fmt(summary['temporal_trace']['csv_flush_ms']['p50'])} / "
        f"{_fmt(summary['temporal_trace']['csv_flush_ms']['p95'])} / "
        f"{_fmt(summary['temporal_trace']['csv_flush_ms']['p99'])}`",
        f"- GUI next-turn p50/p95/p99 (ms): "
        f"`{_fmt(summary['temporal_trace']['event_loop_turn_ms']['p50'])} / "
        f"{_fmt(summary['temporal_trace']['event_loop_turn_ms']['p95'])} / "
        f"{_fmt(summary['temporal_trace']['event_loop_turn_ms']['p99'])}`",
        f"- Operator fault/recovery markers: "
        f"`{summary['operator_actions']['action_count']}`",
        "",
        "### RHEED save operations",
        "",
        *save_lines,
        "",
        "## Interpretation notes",
        "",
        "- Intervals are sequence-normalized estimates from ~1 Hz logger "
        "snapshots; intermediate worker emissions are not individually stored.",
        "- `logged_age_ms` uses the process monotonic clock and is not affected "
        "by wall-clock or timezone adjustments.",
        "- Elog source offset is signed `received_at_utc - source_at_utc`. It "
        "does not prove when each underlying instrument completed a measurement.",
        "- Elog source-update intervals use changes in the embedded record "
        "timestamp; repeated timestamps are counted separately.",
        "- Blank source offsets are expected for pyrometer, MISTRAL, OCR, and "
        "dummy modes because those sources expose no source timestamp.",
        "",
        "## Operator observations",
        "",
        "- Unexpected pauses/errors:",
        "- Window or mode changes:",
        "- Follow-up measurements:",
        "",
    ])
    return "\n".join(lines)


def wait_for_rows(
    path: Path,
    duration_s: float,
    poll_s: float,
) -> tuple[int, dict]:
    started = dt.datetime.now(dt.timezone.utc)
    deadline = time.monotonic() + duration_s
    _, initial_rows = _read_rows(path)
    initial_count = len(initial_rows)
    next_progress = time.monotonic() + 60.0

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        time.sleep(min(poll_s, max(0.0, remaining)))
        if time.monotonic() >= next_progress:
            _, current_rows = _read_rows(path)
            print(
                f"probe progress: {max(0, len(current_rows) - initial_count)} "
                f"new rows, {max(0.0, deadline - time.monotonic()):.0f} s left",
                flush=True,
            )
            next_progress = time.monotonic() + 60.0

    ended = dt.datetime.now(dt.timezone.utc)
    return initial_count, {
        "mode": "wait",
        "started_at_utc": started.isoformat(timespec="seconds"),
        "ended_at_utc": ended.isoformat(timespec="seconds"),
        "requested_duration_s": duration_s,
        "initial_row_count": initial_count,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sensor_log", type=Path)
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=3600.0,
        help="Observation window; default: 3600 (one hour)",
    )
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--temporal-trace", type=Path,
        help="temporal_trace.jsonl; defaults to the sensor log directory",
    )
    parser.add_argument(
        "--operator-actions", type=Path,
        help="operator_actions.jsonl; defaults to the sensor log directory",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Analyze all existing rows immediately",
    )
    parser.add_argument("--operator", default="")
    parser.add_argument("--windows-dpi", default="")
    parser.add_argument("--gui-modes", default="")
    parser.add_argument("--window-titles", default="")
    parser.add_argument("--sampling-settings", default="")
    parser.add_argument("--software-versions", default="")
    parser.add_argument("--notes", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration_seconds < 0:
        raise SystemExit("--duration-seconds must be non-negative")
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be positive")

    sensor_log = args.sensor_log.resolve()
    repo_root = Path(__file__).resolve().parent.parent
    output_dir = args.output_dir
    if output_dir is None:
        tag = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = sensor_log.parent / f"timing_probe_{tag}"
    output_dir.mkdir(parents=True, exist_ok=False)

    if args.no_wait:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        initial_count = 0
        capture = {
            "mode": "no-wait",
            "started_at_utc": now,
            "ended_at_utc": now,
            "requested_duration_s": 0.0,
            "initial_row_count": 0,
        }
    else:
        print(
            f"passively observing {sensor_log} for "
            f"{args.duration_seconds:.0f} s",
            flush=True,
        )
        initial_count, capture = wait_for_rows(
            sensor_log, args.duration_seconds, args.poll_seconds,
        )

    fieldnames, all_rows = _read_rows(sensor_log)
    if not sensor_log.exists():
        raise SystemExit(f"sensor log does not exist: {sensor_log}")
    rows = all_rows[initial_count:]
    capture["final_row_count"] = len(all_rows)

    metadata = {
        "operator": args.operator,
        "windows_dpi": args.windows_dpi,
        "gui_modes": args.gui_modes,
        "window_titles": args.window_titles,
        "sampling_settings": args.sampling_settings,
        "software_versions": args.software_versions,
        "notes": args.notes,
    }
    summary = build_summary(
        sensor_log,
        rows,
        fieldnames,
        metadata,
        capture,
        repo_root,
        temporal_trace=(
            args.temporal_trace.resolve()
            if args.temporal_trace else sensor_log.parent / "temporal_trace.jsonl"
        ),
        operator_actions=(
            args.operator_actions.resolve()
            if args.operator_actions else sensor_log.parent / "operator_actions.jsonl"
        ),
    )

    json_path = output_dir / "timing_summary.json"
    report_path = output_dir / "timing_report.md"
    json_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    report_path.write_text(render_markdown(summary), encoding="utf-8")
    print(f"summary: {json_path}", flush=True)
    print(f"report:  {report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
