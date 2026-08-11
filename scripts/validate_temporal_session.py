#!/usr/bin/env python3
"""Validate durability and temporal invariants of one Growth Monitor session."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Optional


IMAGE_SUFFIXES = frozenset({".bmp", ".png", ".tif", ".tiff"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _float(value: object) -> Optional[float]:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]], list[str]]:
    errors: list[str] = []
    if not path.exists():
        return [], [], [f"missing file: {path.name}"]
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if any(None in row for row in rows):
        errors.append(f"{path.name}: malformed row with extra columns")
    return fields, rows, errors


def _resolve_frame_reference(session: Path, csv_path: Path, raw: str) -> Path:
    """Resolve both session-relative logs and manifest-relative frame names."""
    value = Path(raw)
    if value.is_absolute():
        return value.resolve()
    session_candidate = (session / value).resolve()
    if session_candidate.exists():
        return session_candidate
    return (csv_path.parent / value).resolve()


def _positive_int(value: object) -> Optional[int]:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def validate(session: Path) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    manifests: dict[str, str] = {}
    sensor_fields, sensor_rows, sensor_errors = _csv_rows(
        session / "sensor_log.csv",
    )
    errors.extend(sensor_errors)
    for row_index, row in enumerate(sensor_rows, start=2):
        for field in sensor_fields:
            if not (
                field.endswith("_age_ms")
                or field.endswith("_duration_ms")
                or field == "sync_span_ms"
            ):
                continue
            value = _float(row.get(field))
            if value is not None and value < 0:
                errors.append(
                    f"sensor_log.csv:{row_index}: negative {field}={value}",
                )
        for source in ("rheed", "pyrometer", "mistral", "evap"):
            valid = str(row.get(f"{source}_valid", "")).lower() == "true"
            sequence = str(row.get(f"{source}_sample_sequence", "")).strip()
            received = str(row.get(f"{source}_received_at_utc", "")).strip()
            if valid and (not sequence or not received):
                errors.append(
                    f"sensor_log.csv:{row_index}: {source} valid without provenance",
                )

    trace_path = session / "temporal_trace.jsonl"
    trace_count = 0
    trace_records: list[tuple[int, dict]] = []
    if not trace_path.exists():
        errors.append("missing file: temporal_trace.jsonl")
    else:
        with trace_path.open(encoding="utf-8-sig") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(
                        f"temporal_trace.jsonl:{line_number}: {exc.msg}",
                    )
                    continue
                trace_count += 1
                if not isinstance(record, dict) or not record.get("event"):
                    errors.append(
                        f"temporal_trace.jsonl:{line_number}: invalid record",
                    )
                else:
                    trace_records.append((line_number, record))

    _heartbeat_fields, heartbeat_rows, heartbeat_errors = _csv_rows(
        session / "heartbeat_log.csv",
    )
    errors.extend(heartbeat_errors)

    # Audit every image-bearing CSV, including per-event capture manifests.
    # This catches orphaned manual/QC/Live/auto-capture images, not only the
    # periodic heartbeat subset checked by schema v1.
    listed_images: set[Path] = set()
    path_sequences: dict[Path, set[int]] = {}
    capture_identities: dict[tuple[str, str, str], int] = {}
    image_row_count = 0
    frames_root = session / "frames"
    resolved_frames_root = frames_root.resolve()
    for csv_path in sorted(session.rglob("*.csv")):
        fields, rows, csv_errors = _csv_rows(csv_path)
        relative_csv = csv_path.relative_to(session).as_posix()
        errors.extend(f"{relative_csv}: {error}" for error in csv_errors)
        if "frame_path" not in fields:
            continue
        for row_number, row in enumerate(rows, start=2):
            raw = str(row.get("frame_path", "")).strip()
            if not raw:
                continue
            image_row_count += 1
            path = _resolve_frame_reference(session, csv_path, raw)
            listed_images.add(path)
            try:
                path.relative_to(resolved_frames_root)
            except ValueError:
                errors.append(
                    f"{relative_csv}:{row_number}: frame_path escapes session "
                    f"frames directory: {raw}",
                )
            if not path.is_file():
                errors.append(
                    f"{relative_csv}:{row_number}: missing frame_path {raw}",
                )
            sequence = _positive_int(row.get("capture_sequence"))
            backend = str(row.get("capture_backend", "")).strip()
            captured_at = str(row.get("captured_at_utc", "")).strip()
            hwnd = str(row.get("source_hwnd", "")).strip()
            if sequence is None:
                errors.append(
                    f"{relative_csv}:{row_number}: saved frame without positive "
                    "capture_sequence",
                )
                continue
            if not backend or not captured_at:
                errors.append(
                    f"{relative_csv}:{row_number}: saved frame without complete "
                    "capture provenance",
                )
            path_sequences.setdefault(path, set()).add(sequence)
            identity = (backend, hwnd, captured_at)
            previous = capture_identities.get(identity)
            if previous is not None and previous != sequence:
                errors.append(
                    f"{relative_csv}:{row_number}: capture identity maps to two "
                    "sequences",
                )
            capture_identities[identity] = sequence

    calibration_journal = session / "equalizer_calibrations.jsonl"
    if calibration_journal.exists():
        with calibration_journal.open(encoding="utf-8-sig") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(
                        f"equalizer_calibrations.jsonl:{line_number}: {exc.msg}",
                    )
                    continue
                if not isinstance(event, dict) or event.get("event") != "accepted":
                    continue
                calibration = event.get("calibration")
                if not isinstance(calibration, dict):
                    errors.append(
                        f"equalizer_calibrations.jsonl:{line_number}: accepted "
                        "record lacks calibration",
                    )
                    continue
                raw = str(event.get("orientation_evidence_path", "")).strip()
                sequence = _positive_int(calibration.get("capture_sequence"))
                backend = str(calibration.get("capture_backend", "")).strip()
                captured_at = str(calibration.get("captured_at_utc", "")).strip()
                hwnd = str(calibration.get("source_hwnd", "")).strip()
                if not raw or sequence is None or not backend or not captured_at:
                    errors.append(
                        f"equalizer_calibrations.jsonl:{line_number}: accepted "
                        "evidence lacks complete capture provenance",
                    )
                    continue
                image_row_count += 1
                path = _resolve_frame_reference(
                    session, calibration_journal, raw,
                )
                listed_images.add(path)
                try:
                    path.relative_to(resolved_frames_root)
                except ValueError:
                    errors.append(
                        f"equalizer_calibrations.jsonl:{line_number}: evidence "
                        "path escapes session frames directory",
                    )
                path_sequences.setdefault(path, set()).add(sequence)
                if not path.is_file():
                    errors.append(
                        f"equalizer_calibrations.jsonl:{line_number}: missing "
                        f"orientation evidence {raw}",
                    )
                expected_hash = str(
                    calibration.get("orientation_evidence_sha256", ""),
                ).strip().lower()
                if not expected_hash:
                    errors.append(
                        f"equalizer_calibrations.jsonl:{line_number}: accepted "
                        "evidence lacks SHA-256",
                    )
                elif path.is_file() and _sha256(path) != expected_hash:
                    errors.append(
                        f"equalizer_calibrations.jsonl:{line_number}: orientation "
                        "evidence SHA-256 mismatch",
                    )
                identity = (backend, hwnd, captured_at)
                previous = capture_identities.get(identity)
                if previous is not None and previous != sequence:
                    errors.append(
                        f"equalizer_calibrations.jsonl:{line_number}: capture "
                        "identity maps to two sequences",
                    )
                capture_identities[identity] = sequence

    for path, sequences in sorted(
        path_sequences.items(), key=lambda item: str(item[0]),
    ):
        if len(sequences) != 1:
            errors.append(
                f"frame_path maps to multiple capture sequences: {path}",
            )

    stored_images = {
        path.resolve()
        for path in frames_root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    } if frames_root.exists() else set()
    orphaned = sorted(stored_images - listed_images, key=str)
    errors.extend(f"orphan RHEED image: {path}" for path in orphaned)

    # A frame_saved trace is a timing assertion about the same durable image.
    # Validate its path/sequence against the CSV provenance rather than
    # trusting two independent pieces of metadata that could be mismatched.
    traced_frame_count = 0
    for line_number, record in trace_records:
        if record.get("event") != "frame_saved":
            continue
        traced_frame_count += 1
        details = record.get("details")
        if not isinstance(details, dict):
            errors.append(
                f"temporal_trace.jsonl:{line_number}: frame_saved details missing",
            )
            continue
        raw = str(details.get("frame_path", "")).strip()
        sequence = _positive_int(details.get("capture_sequence"))
        if not raw or sequence is None:
            errors.append(
                f"temporal_trace.jsonl:{line_number}: frame_saved lacks path/sequence",
            )
            continue
        path = _resolve_frame_reference(session, trace_path, raw)
        if not path.is_file():
            errors.append(
                f"temporal_trace.jsonl:{line_number}: missing saved frame {raw}",
            )
        csv_sequences = path_sequences.get(path, set())
        if csv_sequences != {sequence}:
            errors.append(
                f"temporal_trace.jsonl:{line_number}: frame/sequence does not "
                "match CSV provenance",
            )
        expected_hash = str(details.get("frame_sha256", "")).strip().lower()
        if expected_hash and path.is_file() and _sha256(path) != expected_hash:
            errors.append(
                f"temporal_trace.jsonl:{line_number}: frame SHA-256 mismatch",
            )

    for name in (
        "sensor_log.csv", "heartbeat_log.csv", "temporal_trace.jsonl",
        "operator_actions.jsonl", "equalizer_calibrations.jsonl",
    ):
        path = session / name
        if path.exists():
            manifests[name] = _sha256(path)
    if not sensor_rows:
        warnings.append("sensor_log.csv contains no data rows")
    if trace_count == 0:
        warnings.append("temporal_trace.jsonl contains no events")
    return {
        "schema_version": 2,
        "session": str(session.resolve()),
        "sensor_rows": len(sensor_rows),
        "heartbeat_rows": len(heartbeat_rows),
        "trace_records": trace_count,
        "image_rows": image_row_count,
        "stored_images": len(stored_images),
        "traced_frames": traced_frame_count,
        "errors": errors,
        "warnings": warnings,
        "sha256": manifests,
        "passed": not errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(args.session)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
