"""Tests for exact-frame offline Equalizer sidecar persistence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

import numpy as np

from gui.equalizer_alignment import (
    ACTIVE_SIMULATOR_LABELS,
    BasisAsset,
    BasisBundle,
    CalibrationRecord,
    PROCESS_H,
    PROCESS_W,
    RheedFrameSnapshot,
)
from tools.rheed_postprocessing_labeling.offline_equalizer import (
    OfflineCalibrationJournal,
    read_archived_calibrations,
)
from tools.rheed_postprocessing_labeling.session_archive import SessionArchive


def _bundle() -> BasisBundle:
    assets = tuple(
        BasisAsset(
            label=label,
            image=np.full((PROCESS_H, PROCESS_W), index + 1, dtype=np.float32),
            active=True,
            source_kind="simulator",
            source_path=f"fixture/{index}.png",
        )
        for index, label in enumerate(ACTIVE_SIMULATOR_LABELS)
    ) + (
        BasisAsset(
            label="HTR",
            image=None,
            active=False,
            source_kind="unavailable",
            unavailable_reason="canonical basis pending",
        ),
    )
    return BasisBundle(assets, version="offline-sidecar-test")


def _snapshot() -> RheedFrameSnapshot:
    gray = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32)
    rgb = np.zeros((PROCESS_H, PROCESS_W, 3), dtype=np.uint8)
    rgb[20:70, 30:90, 1] = 180
    return RheedFrameSnapshot.freeze(
        rgb,
        gray,
        captured_at_utc="2026-08-17T12:00:00+00:00",
        received_monotonic_ns=123456789,
        capture_sequence=44,
        source_hwnd=9001,
        capture_backend="vimba",
        capture_geometry_id="vimba:full-frame",
        camera_width=PROCESS_W,
        camera_height=PROCESS_H,
        session_id="growth_fixture",
        view_segment_id=2,
        visual_history_generation=3,
        gun_aligned=True,
        realignment_active=False,
        source_frame_age_ms=10.0,
        retrospective=True,
    )


def _calibration(bundle: BasisBundle, snapshot: RheedFrameSnapshot) -> CalibrationRecord:
    points = np.array([[20.0, 48.0], [64.0, 40.0], [108.0, 48.0]])
    calibration_id = "offline-calibration-1"
    return CalibrationRecord(
        calibration_id=calibration_id,
        basis_bundle_id=bundle.bundle_id,
        candidate_id="candidate-fixture",
        matrix=np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]),
        parity="normal",
        endpoint_order="forward",
        basis_points=points,
        live_points=points,
        residuals_px=np.zeros(3),
        rotation_deg=0.0,
        scale=1.0,
        rms_residual_px=0.0,
        max_residual_px=0.0,
        valid_coverage=1.0,
        correlation=1.0,
        source_hwnd=snapshot.source_hwnd,
        camera_width=snapshot.camera_width,
        camera_height=snapshot.camera_height,
        capture_backend=snapshot.capture_backend,
        capture_geometry_id=snapshot.capture_geometry_id,
        captured_at_utc=snapshot.captured_at_utc,
        capture_sequence=snapshot.capture_sequence,
        received_monotonic_ns=snapshot.received_monotonic_ns,
        session_id=snapshot.session_id,
        view_segment_id=snapshot.view_segment_id,
        visual_history_generation=snapshot.visual_history_generation,
        gun_aligned=True,
        realignment_active=False,
        grower_accepted=True,
        accepted_by="Grower A",
        orientation_evidence_kind="streak/tail",
        orientation_evidence_sha256=snapshot.orientation_evidence_sha256(),
        orientation_evidence_path=(
            f"frames/equalizer_calibration_{calibration_id}_orientation.png"
        ),
    )


def test_offline_calibration_is_append_only_and_replays_exact_evidence() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        report_root = Path(temporary)
        journal = OfflineCalibrationJournal(report_root)
        bundle = _bundle()
        snapshot = _snapshot()
        calibration = _calibration(bundle, snapshot)

        journal.accept(calibration, snapshot, bundle)
        active = journal.read_active()
        assert active[calibration.calibration_id].to_json_dict() == calibration.to_json_dict()
        evidence = journal.root / calibration.orientation_evidence_path
        assert hashlib.sha256(evidence.read_bytes()).hexdigest() == calibration.orientation_evidence_sha256
        journal.invalidate(calibration, "review frame moved")
        assert journal.read_active() == {}
        events = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
        assert [event["event"] for event in events] == ["accepted", "invalidated"]
        assert not journal.transaction.exists()


def test_offline_calibration_recovers_commit_marker_after_durable_append() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        journal = OfflineCalibrationJournal(Path(temporary))
        bundle = _bundle()
        snapshot = _snapshot()
        calibration = _calibration(bundle, snapshot)
        journal.accept(calibration, snapshot, bundle)
        event = json.loads(journal.path.read_text(encoding="utf-8").splitlines()[0])
        line = journal._event_line(event)
        journal.transaction.write_text(json.dumps({
            "schema_version": 1,
            "event_sha256": hashlib.sha256(line).hexdigest(),
            "event": event,
            "evidence_path": calibration.orientation_evidence_path,
            "evidence_sha256": calibration.orientation_evidence_sha256,
            "started_at_utc": "2026-08-17T12:00:01+00:00",
        }), encoding="utf-8")

        assert calibration.calibration_id in journal.read_active()
        assert not journal.transaction.exists()
        assert not journal.untrusted.exists()


def test_offline_calibration_rolls_back_orphan_evidence_before_append() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        journal = OfflineCalibrationJournal(Path(temporary))
        bundle = _bundle()
        snapshot = _snapshot()
        calibration = _calibration(bundle, snapshot)
        evidence = snapshot.orientation_evidence_png()
        evidence_path = journal.root / calibration.orientation_evidence_path
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_bytes(evidence)
        event = {
            "journal_schema_version": 1,
            "event": "accepted",
            "recorded_at_utc": "2026-08-17T12:00:01+00:00",
            "calibration": calibration.to_json_dict(),
            "orientation_evidence_path": calibration.orientation_evidence_path,
            "basis_bundle_manifest": bundle.to_manifest_dict(),
        }
        line = journal._event_line(event)
        journal.transaction.write_text(json.dumps({
            "schema_version": 1,
            "event_sha256": hashlib.sha256(line).hexdigest(),
            "event": event,
            "evidence_path": calibration.orientation_evidence_path,
            "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
            "started_at_utc": "2026-08-17T12:00:01+00:00",
        }), encoding="utf-8")

        assert journal.read_active() == {}
        assert not evidence_path.exists()
        assert not journal.transaction.exists()


def test_archived_calibration_replays_from_read_only_session_zip() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bundle = _bundle()
        snapshot = _snapshot()
        calibration = _calibration(bundle, snapshot)
        evidence = snapshot.orientation_evidence_png()
        event = {
            "journal_schema_version": 1,
            "event": "accepted",
            "recorded_at_utc": "2026-08-17T12:00:01+00:00",
            "calibration": calibration.to_json_dict(),
            "orientation_evidence_path": calibration.orientation_evidence_path,
            "basis_bundle_manifest": bundle.to_manifest_dict(),
        }
        archive_path = root / "session.zip"
        journal_member = "growth_fixture/equalizer_calibrations.jsonl"
        evidence_member = "growth_fixture/" + calibration.orientation_evidence_path
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(journal_member, json.dumps(event) + "\n")
            archive.writestr(evidence_member, evidence)
        session = SessionArchive(
            path=archive_path,
            sha256="0" * 64,
            heartbeat_member="growth_fixture/heartbeat_log.csv",
            metadata_member="growth_fixture/session_metadata.json",
            metadata={},
            frames=(),
            root_member="growth_fixture",
            members=(journal_member, evidence_member),
        )

        active = read_archived_calibrations(session)
        assert active[calibration.calibration_id].to_json_dict() == calibration.to_json_dict()
        assert archive_path.is_file()
