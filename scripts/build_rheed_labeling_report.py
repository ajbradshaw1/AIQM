#!/usr/bin/env python3
"""Build or validate an offline RHEED post-processing and labeling report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from rheed_labeling.annotation_validation import validate_annotation_document
from rheed_labeling.report_builder import build_report, load_report_payload


def _build(args: argparse.Namespace) -> None:
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


def _validate(args: argparse.Namespace) -> None:
    payload = load_report_payload(args.report)
    document = json.loads(args.annotations.read_text(encoding="utf-8"))
    validated = validate_annotation_document(document, payload)
    print(json.dumps({
        "valid": True,
        "dataset_id": validated["dataset"]["dataset_id"],
        "segment_count": len(validated["segments"]),
    }, indent=2))


def main() -> None:
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

    args = parser.parse_args()
    if args.command == "build" and len(args.predictions) != len(args.model_spec):
        parser.error("--predictions and --model-spec must contain the same number of paths")
    args.func(args)


if __name__ == "__main__":
    main()
