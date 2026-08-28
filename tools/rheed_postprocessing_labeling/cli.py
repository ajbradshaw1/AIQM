#!/usr/bin/env python3
"""Build or validate an offline RHEED post-processing and labeling report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .annotation_validation import validate_annotation_document
from .report_builder import build_report, load_report_payload
from .session_archive import load_session_archive, read_raw_frame


def _build(args: argparse.Namespace) -> int:
    report = build_report(
        args.session,
        args.predictions,
        args.model_spec,
        args.output_dir,
        report_title=args.title,
        review_quality=args.review_quality,
        overwrite=args.overwrite,
    )
    print(report)
    return 0


def _validate(args: argparse.Namespace) -> int:
    payload = load_report_payload(args.report)
    document = json.loads(args.annotations.read_text(encoding="utf-8"))
    validated = validate_annotation_document(document, payload)
    is_point = validated.get("schema_version") == "rheed-point-events-v1"
    print(json.dumps({
        "valid": True,
        "dataset_id": validated["dataset"]["dataset_id"],
        "schema_version": validated["schema_version"],
        "event_count" if is_point else "segment_count": len(
            validated["events"] if is_point else validated["segments"]
        ),
    }, indent=2))
    return 0


def _extract_frame(args: argparse.Namespace) -> int:
    session = load_session_archive(args.session)
    # User-facing ordinals are one-based throughout the report and journal.
    payload = read_raw_frame(session, int(args.frame_index) - 1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(args.output.resolve())
    return 0


def _desktop(_args: argparse.Namespace) -> int:
    # Keep PyQt6 out of build/validate startup and headless environments.
    from .desktop_launcher import main as desktop_main

    return desktop_main()


def _performance_probe(args: argparse.Namespace) -> int:
    # Keep PyQt6 and the Equalizer widget out of ordinary CLI startup.
    from .performance_probe import run_performance_probe, write_performance_report

    result = run_performance_probe(
        args.report,
        args.session,
        event_id=args.event_id,
        edit_iterations=args.edit_iterations,
        actor=args.actor,
    )
    destination = write_performance_report(args.output, result)
    print(destination)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Render a local report from a session ZIP and predictions")
    build.add_argument("--session", type=Path, required=True)
    build.add_argument("--predictions", type=Path, nargs="+", required=True)
    build.add_argument("--model-spec", type=Path, nargs="+", required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--title", default="RHEED reconstruction timeline")
    build.add_argument("--review-quality", type=int, default=78)
    build.add_argument("--overwrite", action="store_true", help="Replace an existing output directory")
    build.set_defaults(func=_build)

    validate = subparsers.add_parser("validate", help="Fail closed unless an annotation export matches a report")
    validate.add_argument("--report", type=Path, required=True)
    validate.add_argument("--annotations", type=Path, required=True)
    validate.set_defaults(func=_validate)

    desktop = subparsers.add_parser("desktop", help="Open the PyQt6 report launcher")
    desktop.set_defaults(func=_desktop)

    extract = subparsers.add_parser(
        "extract-frame",
        help="Copy one exact BMP/PNG payload from the immutable session ZIP",
    )
    extract.add_argument("--session", type=Path, required=True)
    extract.add_argument("--frame-index", type=int, required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.set_defaults(func=_extract_frame)

    performance = subparsers.add_parser(
        "performance-probe",
        help=(
            "Measure one offline point event and four-basis Equalizer path; "
            "never accesses instruments or runs a classifier"
        ),
        description=(
            "Measure one offline point event and the four-basis Equalizer path. "
            "This never accesses instruments or runs a classifier."
        ),
    )
    performance.add_argument("--report", type=Path, required=True)
    performance.add_argument("--session", type=Path, required=True)
    performance.add_argument(
        "--event-id",
        help="Point-event UUID (default: earliest active labelable event)",
    )
    performance.add_argument("--edit-iterations", type=int, default=50)
    performance.add_argument("--actor", default="Ch-MBE performance probe")
    performance.add_argument("--output", type=Path, required=True)
    performance.set_defaults(func=_performance_probe)

    args = parser.parse_args(argv)
    if args.command == "build" and len(args.predictions) != len(args.model_spec):
        parser.error("--predictions and --model-spec must contain the same number of paths")
    try:
        return int(args.func(args) or 0)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
