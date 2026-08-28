"""Build portable, fully offline reports from archived frames and predictions."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import re
import shutil
import struct
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image

from .prediction_io import ModelSpec, PredictionTable, load_predictions
from .point_events import (
    INITIAL_STATE,
    SCHEMA_VERSION as POINT_EVENT_SCHEMA,
    derive_state_segments,
    import_point_events,
)
from .session_archive import (
    FrameRecord, hash_frame_payloads, load_session_archive,
    ordered_frame_fingerprint, sha256_file,
)


PACKAGE = Path(__file__).resolve().parent
REPOSITORY_ROOT = PACKAGE.parents[1]


def validate_output_destination(
    output_dir: str | Path,
    input_paths: Sequence[str | Path] = (),
    *,
    repository_root: str | Path = REPOSITORY_ROOT,
) -> Path:
    """Resolve a report output directory and reject dangerous targets.

    Generated reports are intentionally kept outside the GUI checkout.  This
    guard is shared by the CLI/API and the desktop launcher so ``--overwrite``
    can never remove source code, a filesystem root, or an input file's parent
    tree.
    """
    destination = Path(output_dir).resolve()
    repository = Path(repository_root).resolve()
    working_directory = Path.cwd().resolve()
    volume_root = Path(destination.anchor).resolve()
    sources = tuple(Path(path).resolve() for path in input_paths)

    if destination == volume_root or destination.parent == volume_root:
        raise ValueError(f"Refusing broad output directory: {destination}")
    if working_directory == destination or working_directory.is_relative_to(destination):
        raise ValueError(f"Output directory contains the current working directory: {destination}")
    if destination == repository or destination.is_relative_to(repository):
        raise ValueError(f"Output directory must be outside the repository: {destination}")
    if repository.is_relative_to(destination):
        raise ValueError(f"Output directory contains the repository: {destination}")
    if any(source.is_relative_to(destination) for source in sources):
        raise ValueError(f"Output directory contains an input file: {destination}")
    return destination


def validate_existing_report_destination(
    output_dir: str | Path,
    *,
    overwrite: bool,
) -> Path:
    """Validate existing output state, with overwrite limited to our reports."""
    destination = Path(output_dir).resolve()
    if not destination.exists():
        return destination
    if not destination.is_dir():
        raise ValueError(f"Output path exists but is not a directory: {destination}")
    if not any(destination.iterdir()):
        return destination
    if not overwrite:
        raise ValueError(f"Output directory is not empty: {destination}")
    allowed_names = {
        "interactive_report.html",
        "run_manifest.json",
        "images",
        "vendor",
    }
    entries = {path.name: path for path in destination.iterdir()}
    if set(entries) != allowed_names or any(path.is_symlink() for path in entries.values()):
        raise ValueError(
            "Overwrite is limited to an unmodified generated report directory; "
            "move annotations or other files elsewhere and choose a new output directory"
        )
    if (
        not entries["interactive_report.html"].is_file()
        or not entries["run_manifest.json"].is_file()
        or not entries["images"].is_dir()
        or not entries["vendor"].is_dir()
    ):
        raise ValueError("Existing report output types are invalid; refusing overwrite")
    image_entries = list(entries["images"].iterdir())
    if not image_entries or any(
        path.is_symlink()
        or not path.is_file()
        or re.fullmatch(r"frame_\d+\.webp", path.name) is None
        for path in image_entries
    ):
        raise ValueError("Existing report images are not an unmodified generated set")
    vendor_names = {path.name for path in entries["vendor"].iterdir()}
    if vendor_names != {"d3.v7.9.0.min.js", "THIRD_PARTY_NOTICES.md"} or any(
        path.is_symlink() or not path.is_file()
        for path in entries["vendor"].iterdir()
    ):
        raise ValueError("Existing report vendor assets are not an unmodified generated set")
    try:
        manifest = json.loads(entries["run_manifest.json"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Existing report manifest is not valid JSON; refusing overwrite") from exc
    expected_outputs = {
        "interactive_report": "interactive_report.html",
        "images": "images/",
        "vendor": "vendor/",
        "third_party_notices": "vendor/THIRD_PARTY_NOTICES.md",
    }
    legacy_policy = {
        "annotation_mode": "model_assisted_review",
        "model_outputs_visible": True,
        "eligible_for_gold": False,
    }
    point_policy = {**legacy_policy, "schema_version": POINT_EVENT_SCHEMA}
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("outputs") != expected_outputs
        or manifest.get("annotation_policy") not in (legacy_policy, point_policy)
    ):
        raise ValueError("Existing report manifest does not identify a compatible generated report")
    return destination


def _install_staged_report(stage: Path, destination: Path) -> None:
    """Atomically install a complete report while preserving an old report on failure."""
    backup_root: Path | None = None
    backup: Path | None = None
    if destination.exists():
        backup_root = Path(tempfile.mkdtemp(
            prefix=f".{destination.name}.backup-",
            dir=destination.parent,
        ))
        backup = backup_root / "previous"
        try:
            destination.rename(backup)
        except Exception:
            shutil.rmtree(backup_root, ignore_errors=True)
            raise
        try:
            # Close the build-time validation race. Files such as exported
            # annotations may have appeared while the replacement report was
            # being staged, so inspect the directory again only after it has
            # moved out of the writer-visible destination path.
            validate_existing_report_destination(backup, overwrite=True)
        except Exception:
            try:
                backup.rename(destination)
            except Exception as restore_error:
                raise RuntimeError(
                    "Existing report changed during the build and could not be "
                    f"restored automatically; it remains at {backup}"
                ) from restore_error
            shutil.rmtree(backup_root, ignore_errors=True)
            raise
    try:
        stage.rename(destination)
    except Exception as install_error:
        if backup is not None and backup.exists():
            if destination.exists():
                raise RuntimeError(
                    "Report destination changed during installation; the previous "
                    f"report remains at {backup}"
                ) from install_error
            try:
                backup.rename(destination)
            except Exception as restore_error:
                raise RuntimeError(
                    "Report installation failed and the previous report could not be "
                    f"restored automatically; it remains at {backup}"
                ) from restore_error
        if backup_root is not None and (backup is None or not backup.exists()):
            shutil.rmtree(backup_root, ignore_errors=True)
        raise
    if backup_root is not None:
        shutil.rmtree(backup_root, ignore_errors=True)


def _parse_utc(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("captured_at_utc must include a timezone")
    return parsed.astimezone(timezone.utc)


def _pack(values: np.ndarray, dtype: str) -> str:
    array = np.asarray(values, dtype=np.dtype(dtype).newbyteorder("<"))
    return base64.b64encode(array.tobytes(order="C")).decode("ascii")


def _constant_or_mixed(values: Sequence[str]) -> str:
    unique = {value for value in values if value}
    return next(iter(unique)) if len(unique) == 1 else "mixed" if unique else ""


def _script_json(value: object) -> str:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _rolling_mean(values: np.ndarray, times: np.ndarray, window_s: float) -> np.ndarray:
    if window_s <= 0:
        return values.copy()
    half = window_s / 2
    prefix = np.vstack((np.zeros((1, values.shape[1])), np.cumsum(values, axis=0, dtype=np.float64)))
    result = np.empty_like(values)
    left = right = 0
    for index, center in enumerate(times):
        while left < len(times) and times[left] < center - half:
            left += 1
        while right < len(times) and times[right] <= center + half:
            right += 1
        result[index] = (prefix[right] - prefix[left]) / max(1, right - left)
    return result


def _transitions(values: np.ndarray, times: np.ndarray, classes: Sequence[str], smoothing_s: float, dwell_s: float) -> list[dict[str, object]]:
    states = _rolling_mean(values, times, smoothing_s).argmax(axis=1)
    current = int(states[0]); candidate = None; candidate_start = 0
    nodes: list[dict[str, object]] = []
    for index in range(1, len(states)):
        observed = int(states[index])
        if observed == current:
            candidate = None
        elif candidate != observed:
            candidate, candidate_start = observed, index
            if dwell_s == 0:
                nodes.append({"index": candidate_start, "elapsed_s": round(float(times[candidate_start]), 6),
                              "from": classes[current], "to": classes[observed], "confirmation_index": index})
                current, candidate = observed, None
        elif times[index] - times[candidate_start] >= dwell_s:
            nodes.append({"index": candidate_start, "elapsed_s": round(float(times[candidate_start]), 6),
                          "from": classes[current], "to": classes[observed], "confirmation_index": index})
            current, candidate = observed, None
    return nodes


def _render(template: str, replacements: dict[str, str]) -> str:
    output = template
    for marker, value in replacements.items():
        if output.count(marker) != 1:
            raise RuntimeError(f"Template must contain exactly one {marker}")
        output = output.replace(marker, value)
    return output


def select_review_frame_indices(
    frame_count: int,
    requested_count: int | None,
    *,
    retain_frame_ordinals: Sequence[int] = (),
) -> tuple[int, ...]:
    """Choose deterministic, nearly uniform saved frames for review images.

    The report keeps the complete acquisition timeline and exact source-frame
    ordinals.  This selection only limits the lossy WebP review assets.  The
    first and last frames are always retained, and explicitly requested
    one-based ordinals replace their nearest non-required uniform sample so the
    requested asset count stays fixed.
    """

    if frame_count < 1:
        raise ValueError("frame_count must be positive")
    if requested_count is None or requested_count >= frame_count:
        return tuple(range(frame_count))
    if isinstance(requested_count, bool) or requested_count < 2:
        raise ValueError("review_frame_count must be at least 2")

    required: set[int] = {0, frame_count - 1}
    for ordinal in retain_frame_ordinals:
        if isinstance(ordinal, bool):
            raise ValueError("retained review-frame ordinals must be integers")
        try:
            index = int(ordinal) - 1
        except (TypeError, ValueError) as exc:
            raise ValueError("retained review-frame ordinals must be integers") from exc
        if index < 0 or index >= frame_count:
            raise ValueError(
                f"retained review-frame ordinal is outside 1..{frame_count}: {ordinal}"
            )
        required.add(index)
    if len(required) > requested_count:
        raise ValueError(
            "review_frame_count is smaller than the number of required review frames"
        )

    denominator = requested_count - 1
    selected = {
        (position * (frame_count - 1) + denominator // 2) // denominator
        for position in range(requested_count)
    }
    if len(selected) != requested_count:  # Defensive; count <= frame_count guarantees this.
        raise RuntimeError("uniform review-frame selection produced duplicate indices")

    protected = set(required)
    for required_index in sorted(required):
        if required_index in selected:
            continue
        removable = [index for index in selected if index not in protected]
        if not removable:
            raise RuntimeError("unable to retain the requested review frame")
        victim = min(removable, key=lambda index: (abs(index - required_index), index))
        selected.remove(victim)
        selected.add(required_index)
    return tuple(sorted(selected))


def _extract_review_images(
    archive_path: Path, frames: Sequence[FrameRecord], output: Path,
    quality: int = 78, *, frame_indices: Sequence[int] | None = None,
) -> tuple[int | None, int | None, bool, list[tuple[int, int] | None]]:
    output.mkdir(parents=True)
    sizes: set[tuple[int, int]] = set()
    ordered_sizes: list[tuple[int, int] | None] = [None] * len(frames)
    selected = tuple(range(len(frames))) if frame_indices is None else tuple(frame_indices)
    if tuple(sorted(set(selected))) != selected or any(
        index < 0 or index >= len(frames) for index in selected
    ):
        raise ValueError("review frame indices must be unique, ordered, and in range")
    with zipfile.ZipFile(archive_path) as archive:
        for index in selected:
            frame = frames[index]
            from io import BytesIO
            with Image.open(BytesIO(archive.read(frame.member))) as source:
                rgb = source.convert("RGB")
                sizes.add(rgb.size)
                ordered_sizes[index] = rgb.size
                rgb.save(
                    output / f"frame_{index + 1:04d}.webp",
                    "WEBP",
                    quality=quality,
                    method=3,
                )
    width, height = next(iter(sizes)) if len(sizes) == 1 else (None, None)
    return width, height, len(sizes) != 1, ordered_sizes


def build_report(
    session_zip: str | Path,
    predictions_paths: Sequence[str | Path],
    model_spec_paths: Sequence[str | Path],
    output_dir: str | Path,
    *,
    smoothing_s: float | None = None,
    dwell_s: float | None = None,
    report_title: str | None = None,
    review_quality: int = 78,
    review_frame_count: int | None = 100,
    retain_frame_ordinals: Sequence[int] = (),
    include_auto_events: bool = True,
    overwrite: bool = False,
) -> Path:
    if len(predictions_paths) != len(model_spec_paths) or not predictions_paths:
        raise ValueError("Provide one prediction CSV per model spec")
    input_paths = [Path(session_zip).resolve()]
    input_paths.extend(Path(path).resolve() for path in predictions_paths)
    input_paths.extend(Path(path).resolve() for path in model_spec_paths)
    destination = validate_output_destination(output_dir, input_paths)
    if review_quality < 25 or review_quality > 100:
        raise ValueError("review_quality must be between 25 and 100")
    validate_existing_report_destination(destination, overwrite=overwrite)
    destination.parent.mkdir(parents=True, exist_ok=True)
    specs = [ModelSpec.load(path) for path in model_spec_paths]
    if len({spec.key for spec in specs}) != len(specs):
        raise ValueError("Model keys must be unique")
    session = hash_frame_payloads(load_session_archive(session_zip))
    review_frame_indices = select_review_frame_indices(
        len(session.frames),
        review_frame_count,
        retain_frame_ordinals=retain_frame_ordinals,
    )
    imported_events = import_point_events(
        session,
        include_auto_events=include_auto_events,
    )
    tables = [load_predictions(path, spec, session.frames) for path, spec in zip(predictions_paths, specs)]
    times = np.asarray([frame.elapsed_s for frame in session.frames], dtype=np.float64)
    captures = [_parse_utc(frame.captured_at_utc) for frame in session.frames]
    if any(b < a for a, b in zip(captures, captures[1:])):
        raise ValueError("captured_at_utc must not move backwards")
    fingerprint = ordered_frame_fingerprint(session.frames)
    stage = Path(tempfile.mkdtemp(prefix=".rheed-labeling-", dir=destination.parent))
    try:
        review_width, review_height, mixed_dimensions, frame_dimensions = _extract_review_images(
            session.path,
            session.frames,
            stage / "images",
            review_quality,
            frame_indices=review_frame_indices,
        )
        frame_contexts = [dict(item) for item in imported_events.frame_contexts]
        for context, dimensions in zip(frame_contexts, frame_dimensions, strict=True):
            if dimensions is None:
                continue
            width, height = dimensions
            context["camera_width"] = context.get("camera_width") or width
            context["camera_height"] = context.get("camera_height") or height
        (stage / "vendor").mkdir()
        shutil.copy2(PACKAGE / "static" / "d3.v7.9.0.min.js", stage / "vendor" / "d3.v7.9.0.min.js")
        shutil.copy2(
            PACKAGE / "static" / "THIRD_PARTY_NOTICES.md",
            stage / "vendor" / "THIRD_PARTY_NOTICES.md",
        )
        report_models = []
        for model_index, table in enumerate(tables):
            local_smoothing = table.spec.smoothing_window_s if smoothing_s is None else float(smoothing_s)
            local_dwell = table.spec.dwell_s if dwell_s is None else float(dwell_s)
            if local_smoothing < 0 or local_dwell < 0:
                raise ValueError("Smoothing and dwell overrides must be non-negative")
            report_models.append({
                "key": table.spec.key, "title": table.spec.title, "subtitle": table.spec.subtitle,
                "status": table.spec.status, "classes": list(table.spec.classes),
                "offset": sum(len(other.spec.classes) for other in tables[:model_index]),
                "nodes": _transitions(table.probabilities, times, table.spec.classes, local_smoothing, local_dwell),
                "smoothing_s": local_smoothing, "dwell_s": local_dwell,
                "quality_available": table.spec.quality_column is not None, "quality_offset": model_index,
                "provenance": table.spec.provenance,
            })
        model_context = [
            {
                "key": table.spec.key,
                "classes": list(table.spec.classes),
                "status": table.spec.status,
                "prediction_sha256": table.sha256,
                "model_spec_sha256": sha256_file(table.spec.path),
                "provenance": table.spec.provenance,
            }
            for table in tables
        ]
        model_context_fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(model_context, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        dataset = {
            "dataset_id": f"sha256:{session.sha256}", "ordered_frame_fingerprint": fingerprint,
            "acquisition_run": session.path.stem, "chamber_id": str(session.metadata.get("chamber_id", "")),
            "source_archive_name": session.path.name, "source_archive_sha256": session.sha256,
            "frame_count": len(session.frames), "first_frame_index": 1,
            "last_frame_index": len(session.frames), "first_elapsed_s": float(times[0]),
            "last_elapsed_s": float(times[-1]), "first_captured_at_utc": session.frames[0].captured_at_utc,
            "last_captured_at_utc": session.frames[-1].captured_at_utc,
            "first_capture_sequence": session.frames[0].capture_sequence,
            "last_capture_sequence": session.frames[-1].capture_sequence,
            "capture_backend": _constant_or_mixed([frame.capture_backend for frame in session.frames]),
            "capture_geometry_id": _constant_or_mixed([frame.capture_geometry_id for frame in session.frames]),
            "review_asset": {"format": "webp", "lossy": True, "quality": review_quality,
                             "width": review_width, "height": review_height,
                             "mixed_dimensions": mixed_dimensions,
                             "frame_count": len(review_frame_indices),
                             "frame_ordinals": [index + 1 for index in review_frame_indices],
                             "sampling": (
                                 "all_saved_frames"
                                 if len(review_frame_indices) == len(session.frames)
                                 else "uniform_saved_frame_index"
                             )},
            "annotation_mode": "model_assisted_review",
            "model_outputs_visible": True,
            "eligible_for_gold": False,
            "automatic_event_proposals_included": bool(include_auto_events),
            "model_context_fingerprint": model_context_fingerprint,
            "model_outputs": model_context,
        }
        state_segments = derive_state_segments(
            imported_events.events,
            frame_count=len(session.frames),
            session_identity=str(dataset["dataset_id"]),
        )
        config = {
            "title": report_title or f"RHEED temporal review — {session.path.stem}",
            "count": len(session.frames), "start_capture_utc": session.frames[0].captured_at_utc,
            "review_frame_indices": list(review_frame_indices),
            "models": report_models, "probability_columns": [len(spec.classes) for spec in specs],
            "sprite": {"data_uri": "", "columns": 1, "tile_width": 1, "tile_height": 1},
            "full_assets": True, "image_pattern": "images/frame_{index}.webp", "dataset": dataset,
            "session": {"camera_backend": session.frames[0].capture_backend,
                        "geometry": session.frames[0].capture_geometry_id,
                        "first_frame": session.frames[0].frame_name, "last_frame": session.frames[-1].frame_name},
            # Point-event data is imported from immutable source CSVs.  The
            # browser edits a revision journal, not these acquisition rows.
            "annotation_schema": POINT_EVENT_SCHEMA,
            "initial_state": INITIAL_STATE,
            "state_segments": state_segments,
            "point_events": list(imported_events.events),
            "reference_events": list(imported_events.reference_events),
            "unlinked_legacy_labels": list(imported_events.unlinked_legacy_labels),
            "sensor_context": imported_events.sensor_context,
            "source_event_revisions": list(imported_events.source_revisions),
            "source_event_journal": imported_events.source_journal,
            # Exact per-saved-frame provenance for the desktop Equalizer.
            # Missing legacy fields stay null and are listed explicitly; the
            # desktop path therefore fails closed instead of inventing state.
            "frame_contexts": frame_contexts,
        }
        probabilities = np.concatenate([table.probabilities for table in tables], axis=1)
        quality = np.stack([table.quality for table in tables], axis=1)
        replacements = {
            "__CONFIG_JSON__": _script_json(config),
            "__TIMES_B64__": _pack(times, "<f4"),
            "__HEARTBEATS_B64__": _pack([frame.heartbeat_idx for frame in session.frames], "<u4"),
            "__SEQUENCES_B64__": _pack([frame.capture_sequence for frame in session.frames], "<u4"),
            "__TEMPERATURES_B64__": _pack([int(round(frame.temperature_c * 10)) if math.isfinite(frame.temperature_c) else -32768 for frame in session.frames], "<i2"),
            "__CAPTURE_OFFSETS_B64__": _pack([(item - captures[0]).total_seconds() for item in captures], "<f4"),
            "__PROBABILITIES_B64__": _pack(np.rint(probabilities.clip(0, 1) * 65535), "<u2"),
            "__QUALITY_B64__": _pack(np.rint(quality.clip(0, 1) * 65535), "<u2"),
            "__FRAME_HASHES_B64__": base64.b64encode(b"".join(bytes.fromhex(frame.frame_sha256) for frame in session.frames)).decode("ascii"),
        }
        fragment = _render((PACKAGE / "templates" / "timeline.html").read_text(encoding="utf-8"), replacements)
        wrapper = (PACKAGE / "templates" / "standalone.html").read_text(encoding="utf-8")
        if wrapper.count("__FRAGMENT__") != 1 or wrapper.count("__REPORT_TITLE__") != 1:
            raise RuntimeError("Standalone template markers are invalid")
        report = wrapper.replace("__REPORT_TITLE__", html.escape(str(config["title"]), quote=True)).replace("__FRAGMENT__", fragment)
        (stage / "interactive_report.html").write_text(report, encoding="utf-8", newline="\n")
        manifest = {
            "schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "input": {"source_archive_name": session.path.name, "source_archive_sha256": session.sha256,
                      "heartbeat_member": session.heartbeat_member, "session_metadata_member": session.metadata_member,
                      "frame_count": len(session.frames), "ordered_frame_fingerprint": fingerprint,
                      "session_metadata": session.metadata},
            "models": [{"key": table.spec.key,
                        "prediction_csv": table.path.name,
                        "prediction_csv_sha256": table.sha256,
                        "model_spec": table.spec.path.name,
                        "model_spec_sha256": sha256_file(table.spec.path),
                        "provenance": table.spec.provenance} for table in tables],
            "outputs": {
                "interactive_report": "interactive_report.html",
                "images": "images/",
                "review_frame_count": len(review_frame_indices),
                "review_frame_ordinals": [index + 1 for index in review_frame_indices],
                "vendor": "vendor/",
                "third_party_notices": "vendor/THIRD_PARTY_NOTICES.md",
            },
            "annotation_policy": {
                "annotation_mode": "model_assisted_review",
                "model_outputs_visible": True,
                "eligible_for_gold": False,
                "automatic_event_proposals_included": bool(include_auto_events),
                "schema_version": POINT_EVENT_SCHEMA,
            },
            "point_events": {
                "labelable_count": len(imported_events.events),
                "reference_count": len(imported_events.reference_events),
                "unlinked_legacy_label_count": len(imported_events.unlinked_legacy_labels),
                "source_revision_count": len(imported_events.source_revisions),
                "source_journal": imported_events.source_journal,
                "source_files": sorted({
                    event["source"]["source_file"]
                    for event in imported_events.events
                    if event["source"].get("source_file")
                }),
            },
        }
        (stage / "run_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _install_staged_report(stage, destination)
        return destination / "interactive_report.html"
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def load_report_payload(path: str | Path) -> dict[str, object]:
    text = Path(path).read_text(encoding="utf-8")
    config_match = re.search(r"const\s+config\s*=\s*(\{.*?\});\s*root\.querySelector", text, re.S)
    if not config_match:
        raise ValueError("Report config was not found")
    config = json.loads(config_match.group(1))
    decoded = {name: payload for name, payload in re.findall(r"const\s+(\w+)\s*=\s*decode\('([^']*)',\s*\w+Array\)", text)}
    def unpack(names: Sequence[str], code: str) -> list[object]:
        value = next((decoded[name] for name in names if name in decoded), None)
        if value is None: raise ValueError(f"Packed report array missing: {names[0]}")
        raw = base64.b64decode(value, validate=True); size = struct.calcsize(code)
        return [item[0] for item in struct.iter_unpack("<" + code, raw)] if len(raw) % size == 0 else (_ for _ in ()).throw(ValueError("Malformed packed array"))
    count = int(config["count"])
    times = unpack(("times",), "f"); heartbeats = unpack(("heartbeatIndices", "heartbeats"), "I")
    sequences = unpack(("sequences", "captureSequences"), "I")
    hashes_b64 = next((decoded[name] for name in ("frameHashBytes", "frameHashes", "frameSha256") if name in decoded), None)
    if hashes_b64 is None: raise ValueError("Packed frame hashes missing")
    hashes_raw = base64.b64decode(hashes_b64, validate=True)
    result = {"config": config, "times": times, "heartbeat_indices": heartbeats,
              "capture_sequences": sequences, "temperatures": unpack(("temperatures",), "h"),
              "capture_offsets": unpack(("captureOffsets",), "f"),
              "probabilities": unpack(("probabilities",), "H"), "quality": unpack(("qualityValues", "legacyQuality", "quality"), "H"),
              "frame_sha256": [hashes_raw[index:index + 32].hex() for index in range(0, len(hashes_raw), 32)]}
    if any(len(result[key]) != count for key in ("times", "heartbeat_indices", "capture_sequences", "temperatures", "capture_offsets", "frame_sha256")):
        raise ValueError("Packed provenance length mismatch")
    return result
