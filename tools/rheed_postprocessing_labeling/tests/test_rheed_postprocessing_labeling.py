"""Offline tests for the model-agnostic RHEED post-processing tool.

The fixtures deliberately contain irregular time intervals, skipped heartbeat
indices, and capture-sequence gaps.  Nothing here depends on laboratory data,
network access, or a trained checkpoint.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from PIL import Image

from tools.rheed_postprocessing_labeling.annotation_validation import (
    AnnotationValidationError,
    validate_annotation_document,
)
from tools.rheed_postprocessing_labeling.prediction_io import ModelSpec
from tools.rheed_postprocessing_labeling.report_builder import build_report, load_report_payload


ELAPSED = [0.0, 0.7, 2.1, 2.2, 5.8, 8.0]
HEARTBEATS = [1, 2, 4, 7, 8, 12]
SEQUENCES = [100, 101, 104, 110, 111, 120]
CAPTURED_UTC = [
    "2026-01-02T03:04:05.000Z",
    "2026-01-02T03:04:05.700Z",
    "2026-01-02T03:04:07.100Z",
    "2026-01-02T03:04:07.200Z",
    "2026-01-02T03:04:10.800Z",
    "2026-01-02T03:04:13.000Z",
]


@dataclass(frozen=True)
class SyntheticBundle:
    archive: Path
    prediction_paths: tuple[Path, ...]
    spec_paths: tuple[Path, ...]
    rows: tuple[dict[str, object], ...]


def _png_bytes(index: int, pixel_offset: int = 0) -> bytes:
    """Return a small deterministic RGB image without writing source assets."""
    image = Image.new(
        "RGB",
        (16, 12),
        ((17 * index + pixel_offset) % 256, (41 * index) % 256, 80),
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _csv_text(rows: list[dict[str, object]], fieldnames: list[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _write_zip_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 2, 3, 4, 6))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content)


def _make_bundle(root: Path, *, pixel_offset: int = 0) -> SyntheticBundle:
    root.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[str, bytes, str]] = []
    provenance: list[dict[str, object]] = []
    heartbeat_rows: list[dict[str, object]] = []
    for frame_index, (elapsed, heartbeat, sequence, captured_at) in enumerate(
        zip(ELAPSED, HEARTBEATS, SEQUENCES, CAPTURED_UTC, strict=True)
    ):
        frame_name = f"rheed_{frame_index:04d}.png"
        image_bytes = _png_bytes(frame_index, pixel_offset)
        frame_sha256 = hashlib.sha256(image_bytes).hexdigest()
        frames.append((frame_name, image_bytes, frame_sha256))
        provenance.append(
            {
                "frame_index": frame_index,
                "heartbeat_idx": heartbeat,
                "elapsed_s": elapsed,
                "captured_at_utc": captured_at,
                "capture_sequence": sequence,
                "frame_name": frame_name,
                "frame_sha256": frame_sha256,
            }
        )
        heartbeat_rows.append(
            {
                "timestamp": captured_at,
                "captured_at_utc": captured_at,
                "elapsed_s": elapsed,
                "heartbeat_idx": heartbeat,
                "capture_sequence": sequence,
                "pyrometer_temp_C": 500.0 + 2.5 * frame_index,
                "frame_path": rf"C:\synthetic\frames\{frame_name}",
                "frame_sha256": frame_sha256,
                "capture_backend": "synthetic",
            }
        )

    session_metadata = {
        "session_id": "synthetic-irregular-session",
        "chamber_id": "TEST-MBE",
        "camera_backend": "synthetic",
        "capture_geometry_id": "synthetic-16x12",
        "geometry": "synthetic-16x12",
        "started_at_utc": CAPTURED_UTC[0],
    }
    heartbeat_text = _csv_text(heartbeat_rows, list(heartbeat_rows[0]))
    archive_path = root / "synthetic_session.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        _write_zip_member(
            archive,
            "synthetic_session/session_metadata.json",
            json.dumps(session_metadata, sort_keys=True).encode("utf-8"),
        )
        _write_zip_member(
            archive,
            "synthetic_session/heartbeat_log.csv",
            heartbeat_text.encode("utf-8"),
        )
        for frame_name, image_bytes, _ in frames:
            _write_zip_member(
                archive,
                f"synthetic_session/frames/{frame_name}",
                image_bytes,
            )

    specs = [
        {
            "schema_version": 1,
            "key": "two_output",
            "title": "Synthetic two-output model",
            "subtitle": "Includes a 1x1 output",
            "classes": ["1x1", "c_6x2"],
            "probability_columns": ["p_1x1", "p_c_6x2"],
            "quality_column": "quality",
            "predicted_column": "predicted",
            "status": "synthetic_test_only",
            "provenance": {"checkpoint_sha256": "0" * 64},
        },
        {
            "schema_version": 1,
            "key": "three_output",
            "title": "Synthetic three-output model",
            "subtitle": "Different class vocabulary",
            "classes": ["twinned_2x1", "rt13", "htr"],
            "probability_columns": ["p_twinned", "p_rt13", "p_htr"],
            "quality_column": "quality",
            "predicted_column": "predicted",
            "status": "synthetic_test_only",
            "provenance": {"checkpoint_sha256": "1" * 64},
        },
    ]
    probabilities = [
        [
            {"p_1x1": 0.80 - 0.08 * i, "p_c_6x2": 0.20 + 0.08 * i,
             "quality": 0.90, "predicted": "1x1" if i < 4 else "c_6x2"}
            for i in range(len(provenance))
        ],
        [
            {"p_twinned": 0.60 - 0.05 * i, "p_rt13": 0.20 + 0.04 * i,
             "p_htr": 0.20 + 0.01 * i, "quality": 0.75,
             "predicted": "twinned_2x1" if i < 5 else "rt13"}
            for i in range(len(provenance))
        ],
    ]

    prediction_paths: list[Path] = []
    spec_paths: list[Path] = []
    for model_index, spec in enumerate(specs):
        spec_path = root / f"model_{model_index}.json"
        spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        spec_paths.append(spec_path)
        rows = [dict(base, **probabilities[model_index][index]) for index, base in enumerate(provenance)]
        prediction_path = root / f"predictions_{model_index}.csv"
        prediction_path.write_text(
            _csv_text(rows, list(rows[0])), encoding="utf-8", newline=""
        )
        prediction_paths.append(prediction_path)

    return SyntheticBundle(
        archive=archive_path,
        prediction_paths=tuple(prediction_paths),
        spec_paths=tuple(spec_paths),
        rows=tuple(provenance),
    )


@pytest.fixture
def bundle(tmp_path: Path) -> SyntheticBundle:
    return _make_bundle(tmp_path / "input")


@pytest.fixture
def built_report(tmp_path: Path, bundle: SyntheticBundle) -> tuple[Path, dict, SyntheticBundle]:
    report = build_report(
        bundle.archive,
        bundle.prediction_paths,
        bundle.spec_paths,
        tmp_path / "report",
        report_title="Synthetic irregular acquisition",
    )
    return report, load_report_payload(report), bundle


def _ordered_fingerprint(rows: tuple[dict[str, object], ...]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        )
    return f"sha256:{digest.hexdigest()}"


def test_report_preserves_irregular_provenance_and_is_model_agnostic(
    built_report: tuple[Path, dict, SyntheticBundle],
) -> None:
    report, payload, bundle = built_report
    assert report.name == "interactive_report.html"
    assert payload["times"] == pytest.approx(ELAPSED)
    assert payload["heartbeat_indices"] == HEARTBEATS
    assert payload["capture_sequences"] == SEQUENCES
    assert payload["config"]["count"] == len(ELAPSED)
    assert len(payload["config"]["frame_contexts"]) == len(ELAPSED)
    assert payload["config"]["frame_contexts"][0]["received_monotonic_ns"] is None
    assert "received_monotonic_ns" in payload["config"]["frame_contexts"][0]["missing_provenance"]

    dataset = payload["config"]["dataset"]
    assert dataset["source_archive_sha256"] == hashlib.sha256(bundle.archive.read_bytes()).hexdigest()
    assert dataset["ordered_frame_fingerprint"] == _ordered_fingerprint(bundle.rows)
    assert dataset["model_context_fingerprint"].startswith("sha256:")
    assert [item["key"] for item in dataset["model_outputs"]] == ["two_output", "three_output"]
    assert dataset["frame_count"] == len(bundle.rows)
    assert payload["frame_sha256"] == [row["frame_sha256"] for row in bundle.rows]

    models = payload["config"]["models"]
    assert [model["key"] for model in models] == ["two_output", "three_output"]
    assert models[0]["classes"] == ["1x1", "c_6x2"]
    assert models[1]["classes"] == ["twinned_2x1", "rt13", "htr"]


def test_report_uses_only_local_assets(
    built_report: tuple[Path, dict, SyntheticBundle],
) -> None:
    report, _, _ = built_report
    html = report.read_text(encoding="utf-8")
    references = re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", html, flags=re.I)
    assert references, "Expected at least one local script/image reference"
    for reference in references:
        parsed = urlsplit(reference)
        assert parsed.scheme not in {"http", "https"}, reference
        if parsed.scheme in {"data", "blob"} or reference.startswith("#"):
            continue
        asset = (report.parent / unquote(parsed.path)).resolve()
        assert asset.is_relative_to(report.parent.resolve()), reference
        assert asset.is_file(), reference
    assert (report.parent / "vendor" / "THIRD_PARTY_NOTICES.md").is_file()


def test_report_refuses_broad_or_input_containing_output_directories(
    tmp_path: Path, bundle: SyntheticBundle,
) -> None:
    with pytest.raises(ValueError, match="input file"):
        build_report(
            bundle.archive,
            bundle.prediction_paths,
            bundle.spec_paths,
            bundle.archive.parent,
            overwrite=True,
        )
    with pytest.raises(ValueError, match="broad output directory"):
        build_report(
            bundle.archive,
            bundle.prediction_paths,
            bundle.spec_paths,
            tmp_path.anchor,
            overwrite=True,
        )


def test_fingerprint_is_stable_and_changes_with_frame_content(tmp_path: Path) -> None:
    first = _make_bundle(tmp_path / "first")
    identical = _make_bundle(tmp_path / "identical")
    changed = _make_bundle(tmp_path / "changed", pixel_offset=1)

    def fingerprint(item: SyntheticBundle, name: str) -> str:
        report = build_report(
            item.archive, item.prediction_paths, item.spec_paths, tmp_path / name
        )
        return load_report_payload(report)["config"]["dataset"]["ordered_frame_fingerprint"]

    assert fingerprint(first, "report-first") == fingerprint(identical, "report-identical")
    assert fingerprint(first, "report-first-again") != fingerprint(changed, "report-changed")


def _endpoint(row: dict[str, object]) -> dict[str, object]:
    return {
        # Annotation JSON and the visible UI use saved-frame ordinals (1-based),
        # while the prediction/source CSV row index remains 0-based.
        "frame_index": int(row["frame_index"]) + 1,
        "heartbeat_idx": row["heartbeat_idx"],
        "elapsed_s": row["elapsed_s"],
        "captured_at_utc": row["captured_at_utc"],
        "capture_sequence": row["capture_sequence"],
        "frame_sha256": row["frame_sha256"],
    }


def _annotation_document(payload: dict, rows: tuple[dict[str, object], ...]) -> dict:
    return {
        "schema_version": "rheed-temporal-segments-v1",
        "document_type": "ai4mbe_rheed_segment_annotations",
        "dataset": copy.deepcopy(payload["config"]["dataset"]),
        "annotation_set": {
            "annotation_set_id": "annotation-set-synthetic",
            "labeler": "Synthetic Reviewer",
            "annotation_source": "human_assisted_temporal_segment",
            "annotation_mode": "model_assisted_review",
            "model_outputs_visible": True,
            "eligible_for_gold": False,
        },
        "segments": [
            {
                "annotation_id": "segment-1",
                "labeler": "Synthetic Reviewer",
                "annotation_source": "human_assisted_temporal_segment",
                "label_code": "rt13",
                "model_outputs_visible": True,
                "eligible_for_gold": False,
                "start": _endpoint(rows[1]),
                "end": _endpoint(rows[2]),
                "frame_count": 2,
                "notes": "deterministic synthetic interval",
            }
        ],
    }


def test_annotation_validator_accepts_exact_saved_frame_provenance(
    built_report: tuple[Path, dict, SyntheticBundle],
) -> None:
    _, payload, bundle = built_report
    validated = validate_annotation_document(_annotation_document(payload, bundle.rows), payload)
    assert validated["segments"][0]["start"]["heartbeat_idx"] == 2
    assert validated["segments"][0]["end"]["capture_sequence"] == 104


def test_annotation_validator_fails_closed(
    built_report: tuple[Path, dict, SyntheticBundle],
) -> None:
    _, payload, bundle = built_report
    valid = _annotation_document(payload, bundle.rows)
    invalid_documents: list[dict] = []

    wrong_run = copy.deepcopy(valid)
    wrong_run["dataset"]["ordered_frame_fingerprint"] = "sha256:" + "f" * 64
    invalid_documents.append(wrong_run)

    wrong_endpoint = copy.deepcopy(valid)
    wrong_endpoint["segments"][0]["start"]["capture_sequence"] = 999999
    invalid_documents.append(wrong_endpoint)

    wrong_model_context = copy.deepcopy(valid)
    wrong_model_context["dataset"]["model_context_fingerprint"] = "sha256:" + "e" * 64
    invalid_documents.append(wrong_model_context)

    bad_count = copy.deepcopy(valid)
    bad_count["segments"][0]["frame_count"] = 3
    invalid_documents.append(bad_count)

    gold_claim = copy.deepcopy(valid)
    gold_claim["annotation_set"]["eligible_for_gold"] = True
    invalid_documents.append(gold_claim)

    overlap = copy.deepcopy(valid)
    overlapping_segment = copy.deepcopy(overlap["segments"][0])
    overlapping_segment["annotation_id"] = "segment-overlap"
    overlapping_segment["start"] = _endpoint(bundle.rows[2])
    overlapping_segment["end"] = _endpoint(bundle.rows[3])
    overlap["segments"].append(overlapping_segment)
    invalid_documents.append(overlap)

    for document in invalid_documents:
        with pytest.raises(AnnotationValidationError):
            validate_annotation_document(document, payload)


def test_prediction_provenance_mismatch_is_rejected(tmp_path: Path, bundle: SyntheticBundle) -> None:
    rows = list(csv.DictReader(bundle.prediction_paths[1].open(encoding="utf-8", newline="")))
    rows[3]["capture_sequence"] = "999"
    bundle.prediction_paths[1].write_text(
        _csv_text(rows, list(rows[0])), encoding="utf-8", newline=""
    )
    with pytest.raises((ValueError, RuntimeError)):
        build_report(
            bundle.archive,
            bundle.prediction_paths,
            bundle.spec_paths,
            tmp_path / "bad-report",
        )


def test_one_based_prediction_frame_indices_are_accepted(
    tmp_path: Path, bundle: SyntheticBundle,
) -> None:
    for prediction_path in bundle.prediction_paths:
        rows = list(csv.DictReader(prediction_path.open(encoding="utf-8", newline="")))
        for row in rows:
            row["frame_index"] = str(int(row["frame_index"]) + 1)
        prediction_path.write_text(
            _csv_text(rows, list(rows[0])), encoding="utf-8", newline=""
        )
    report = build_report(
        bundle.archive,
        bundle.prediction_paths,
        bundle.spec_paths,
        tmp_path / "one-based-report",
    )
    assert load_report_payload(report)["heartbeat_indices"] == HEARTBEATS


def test_model_spec_rejects_ambiguous_probability_mapping(tmp_path: Path) -> None:
    path = tmp_path / "bad-spec.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "key": "bad",
                "title": "Bad",
                "classes": ["a", "b"],
                "probability_columns": ["p_a"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises((ValueError, TypeError)):
        ModelSpec.load(path)
