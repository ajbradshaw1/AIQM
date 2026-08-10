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
from .session_archive import (
    FrameRecord, hash_frame_payloads, load_session_archive,
    ordered_frame_fingerprint, sha256_file,
)


PACKAGE = Path(__file__).resolve().parent


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


def _extract_review_images(archive_path: Path, frames: Sequence[FrameRecord], output: Path, quality: int = 78) -> tuple[int | None, int | None, bool]:
    output.mkdir(parents=True)
    sizes: set[tuple[int, int]] = set()
    with zipfile.ZipFile(archive_path) as archive:
        for index, frame in enumerate(frames, 1):
            from io import BytesIO
            with Image.open(BytesIO(archive.read(frame.member))) as source:
                rgb = source.convert("RGB")
                sizes.add(rgb.size)
                rgb.save(output / f"frame_{index:04d}.webp", "WEBP", quality=quality, method=3)
    width, height = next(iter(sizes)) if len(sizes) == 1 else (None, None)
    return width, height, len(sizes) != 1


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
    overwrite: bool = False,
) -> Path:
    if len(predictions_paths) != len(model_spec_paths) or not predictions_paths:
        raise ValueError("Provide one prediction CSV per model spec")
    destination = Path(output_dir).resolve()
    input_paths = [Path(session_zip).resolve()]
    input_paths.extend(Path(path).resolve() for path in predictions_paths)
    input_paths.extend(Path(path).resolve() for path in model_spec_paths)
    protected_paths = (PACKAGE.resolve(), PACKAGE.parent.resolve(), Path.cwd().resolve())
    volume_root = Path(destination.anchor)
    if destination == volume_root or destination.parent == volume_root:
        raise ValueError(f"Refusing broad output directory: {destination}")
    if any(protected.is_relative_to(destination) for protected in protected_paths):
        raise ValueError(f"Output directory contains source code or the working directory: {destination}")
    if any(source.is_relative_to(destination) for source in input_paths):
        raise ValueError(f"Output directory contains an input file: {destination}")
    if review_quality < 25 or review_quality > 100:
        raise ValueError("review_quality must be between 25 and 100")
    if destination.exists() and any(destination.iterdir()) and not overwrite:
        raise ValueError(f"Output directory is not empty: {destination}")
    if destination.exists() and overwrite:
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    specs = [ModelSpec.load(path) for path in model_spec_paths]
    if len({spec.key for spec in specs}) != len(specs):
        raise ValueError("Model keys must be unique")
    session = hash_frame_payloads(load_session_archive(session_zip))
    tables = [load_predictions(path, spec, session.frames) for path, spec in zip(predictions_paths, specs)]
    times = np.asarray([frame.elapsed_s for frame in session.frames], dtype=np.float64)
    captures = [_parse_utc(frame.captured_at_utc) for frame in session.frames]
    if any(b < a for a, b in zip(captures, captures[1:])):
        raise ValueError("captured_at_utc must not move backwards")
    fingerprint = ordered_frame_fingerprint(session.frames)
    stage = Path(tempfile.mkdtemp(prefix=".rheed-labeling-", dir=destination.parent))
    try:
        review_width, review_height, mixed_dimensions = _extract_review_images(
            session.path, session.frames, stage / "images", review_quality
        )
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
                             "mixed_dimensions": mixed_dimensions},
            "annotation_mode": "model_assisted_review",
            "model_outputs_visible": True,
            "eligible_for_gold": False,
            "model_context_fingerprint": model_context_fingerprint,
            "model_outputs": model_context,
        }
        config = {
            "title": report_title or f"RHEED temporal review — {session.path.stem}",
            "count": len(session.frames), "start_capture_utc": session.frames[0].captured_at_utc,
            "models": report_models, "probability_columns": [len(spec.classes) for spec in specs],
            "sprite": {"data_uri": "", "columns": 1, "tile_width": 1, "tile_height": 1},
            "full_assets": True, "image_pattern": "images/frame_{index}.webp", "dataset": dataset,
            "session": {"camera_backend": session.frames[0].capture_backend,
                        "geometry": session.frames[0].capture_geometry_id,
                        "first_frame": session.frames[0].frame_name, "last_frame": session.frames[-1].frame_name},
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
                "vendor": "vendor/",
                "third_party_notices": "vendor/THIRD_PARTY_NOTICES.md",
            },
            "annotation_policy": {"annotation_mode": "model_assisted_review",
                                  "model_outputs_visible": True, "eligible_for_gold": False},
        }
        (stage / "run_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if destination.exists(): destination.rmdir()
        stage.rename(destination)
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
