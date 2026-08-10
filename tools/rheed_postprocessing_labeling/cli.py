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
    print(json.dumps({
        "valid": True,
        "dataset_id": validated["dataset"]["dataset_id"],
        "segment_count": len(validated["segments"]),
    }, indent=2))
    return 0


def _desktop(_args: argparse.Namespace) -> int:
    # Keep PyQt6 out of build/validate startup and headless environments.
    from .desktop_launcher import main as desktop_main

    return desktop_main()


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
