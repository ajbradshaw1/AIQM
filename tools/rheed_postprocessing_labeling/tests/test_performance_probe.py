"""Offline-only tests for the Ch-MBE point-event performance probe."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

from PIL import Image

from tools.rheed_postprocessing_labeling.performance_probe import (
    PROBE_SCHEMA_VERSION,
    _git_environment,
    _process_rss_bytes,
    run_performance_probe,
    timing_summary,
    write_performance_report,
)
from tools.rheed_postprocessing_labeling.report_builder import build_report


def _csv(rows: list[dict[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _png(value: int) -> bytes:
    image = Image.new("RGB", (32, 24), (value, value // 2, 20))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def _make_report(tmp_path: Path) -> tuple[Path, Path]:
    frames = [_png(30), _png(90)]
    hashes = [hashlib.sha256(frame).hexdigest() for frame in frames]
    times = ["2026-08-17T12:00:00.000Z", "2026-08-17T12:00:01.000Z"]
    heartbeats: list[dict[str, object]] = []
    for index in range(2):
        heartbeats.append({
            "timestamp": times[index],
            "captured_at_utc": times[index],
            "elapsed_s": float(index),
            "heartbeat_idx": index + 1,
            "capture_sequence": 20 + index,
            "pyrometer_temp_C": 500 + index,
            "frame_path": rf"D:\synthetic\frames\rheed_{index}.png",
            "capture_backend": "wgc",
            "capture_geometry_id": "synthetic-32x24",
            "captured_monotonic_ns": 1_000_000_000 + index * 1_000_000_000,
            "frame_width": 32,
            "frame_height": 24,
            "source_hwnd": 1234,
            "view_segment_id": 1,
            "visual_history_generation": 2,
            "gun_aligned": "True",
            "realignment_active": "False",
        })
    manual = [{
        "timestamp": times[0],
        "captured_at_utc": times[0],
        "elapsed_s": 0.0,
        "event_idx": 1,
        "frame_path": r"D:\synthetic\frames\rheed_0.png",
        "capture_sequence": 20,
        "note": "",
    }]
    archive_path = tmp_path / "synthetic_session.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("session/session_metadata.json", json.dumps({
            "session_id": "performance-session",
            "chamber_id": "TEST",
            "camera_backend": "wgc",
            "capture_geometry_id": "synthetic-32x24",
        }))
        archive.writestr("session/heartbeat_log.csv", _csv(heartbeats))
        archive.writestr("session/manual_events.csv", _csv(manual))
        for index, frame in enumerate(frames):
            archive.writestr(f"session/frames/rheed_{index}.png", frame)

    spec_path = tmp_path / "model.json"
    spec_path.write_text(json.dumps({
        "schema_version": 1,
        "key": "synthetic",
        "title": "Synthetic test context",
        "classes": ["1x1", "c_6x2"],
        "probability_columns": ["p_1x1", "p_c6x2"],
        "quality_column": "quality",
        "predicted_column": "predicted",
        "status": "synthetic_test_only",
        "smoothing_window_s": 0,
        "new_argmax_minimum_dwell_s": 0,
        "provenance": {"checkpoint_sha256": "0" * 64},
    }), encoding="utf-8")
    prediction_rows: list[dict[str, object]] = []
    for index in range(2):
        prediction_rows.append({
            "frame_index": index,
            "heartbeat_idx": index + 1,
            "elapsed_s": float(index),
            "captured_at_utc": times[index],
            "capture_sequence": 20 + index,
            "frame_name": f"rheed_{index}.png",
            "frame_sha256": hashes[index],
            "p_1x1": 0.8 - 0.1 * index,
            "p_c6x2": 0.2 + 0.1 * index,
            "quality": 0.9,
            "predicted": "1x1",
        })
    predictions_path = tmp_path / "predictions.csv"
    predictions_path.write_bytes(_csv(prediction_rows))
    report = build_report(
        archive_path,
        [predictions_path],
        [spec_path],
        tmp_path / "report",
    )
    return report, archive_path


def test_timing_summary_is_deterministic() -> None:
    summary = timing_summary([4.0, 1.0, 3.0, 2.0])
    assert summary["count"] == 4
    assert summary["mean_ms"] == 2.5
    assert summary["p50_ms"] == 2.5
    assert summary["max_ms"] == 4.0


def test_windows_rss_uses_a_valid_pointer_sized_process_handle() -> None:
    if sys.platform != "win32":
        return
    first = _process_rss_bytes()
    second = _process_rss_bytes()
    assert isinstance(first, int) and first > 0
    assert isinstance(second, int) and second > 0


def test_git_commit_falls_back_to_linked_worktree_metadata_without_path_git(
    tmp_path: Path, monkeypatch,
) -> None:
    worktree = tmp_path / "worktree"
    common = tmp_path / "repository" / ".git"
    git_dir = common / "worktrees" / "probe"
    reference = common / "refs" / "heads" / "codex" / "probe"
    worktree.mkdir()
    git_dir.mkdir(parents=True)
    reference.parent.mkdir(parents=True)
    commit = "1234567890abcdef1234567890abcdef12345678"
    (worktree / ".git").write_text(
        f"gitdir: {git_dir.as_posix()}\n", encoding="utf-8",
    )
    (git_dir / "HEAD").write_text(
        "ref: refs/heads/codex/probe\n", encoding="ascii",
    )
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    reference.write_text(f"{commit}\n", encoding="ascii")
    monkeypatch.setattr(
        "tools.rheed_postprocessing_labeling.performance_probe.shutil.which",
        lambda _name: None,
    )

    result = _git_environment(worktree)

    assert result["commit"] == commit
    assert result["git_commit_source"] == "git-metadata"
    assert result["git_status_available"] is False
    assert result["tracked_worktree_dirty"] is None


def test_probe_uses_one_frame_four_bases_and_scratch_revisions(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    report, archive = _make_report(tmp_path)
    archive_before = archive.read_bytes()
    report_before = report.read_bytes()
    annotations = report.parent / "annotations"

    result = run_performance_probe(report, archive, edit_iterations=3)

    assert result["schema_version"] == PROBE_SCHEMA_VERSION
    assert result["selected_raw_frame"]["frames_decoded"] == 1
    assert result["selected_raw_frame"]["whole_session_image_precompute"] is False
    assert result["equalizer"]["active_basis_count"] == 4
    assert result["equalizer"]["active_basis_labels"] == [
        "1x1", "Tw(2x1)", "c(6x2)", "RT13",
    ]
    assert result["equalizer"]["model_inference_executed"] is False
    assert result["continuous_event_edits"]["iterations"] == 3
    assert result["continuous_event_edits"][
        "all_revisions_durable_and_replayable"
    ] is True
    assert result["safety"]["source_zip_stat_unchanged"] is True
    assert result["safety"]["laboratory_hardware_access"] is False
    assert result["safety"]["new_model_runtime_module_roots"] == []
    assert result["safety"]["new_instrument_interface_module_roots"] == []
    assert archive.read_bytes() == archive_before
    assert report.read_bytes() == report_before
    assert not annotations.exists()

    output = write_performance_report(tmp_path / "result.json", result)
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["input"]["selected_frame_index"] == 1
    assert loaded["memory"]["python_traced_peak_bytes"] >= 0


def test_cli_help_mentions_safety_contract(capsys) -> None:
    from tools.rheed_postprocessing_labeling.cli import main

    try:
        main(["performance-probe", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "never accesses instruments or runs a classifier" in output
