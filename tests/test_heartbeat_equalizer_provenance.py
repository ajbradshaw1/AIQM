"""Forward-compatible heartbeat provenance for retrospective Equalizer."""

from __future__ import annotations

import csv
from types import SimpleNamespace

import numpy as np

from gui.growth_app import GrowthApp
from gui.growth_logger import GrowthLogger


LEGACY_HEARTBEAT_FIELDS = [
    "timestamp", "elapsed_s", "heartbeat_idx",
    "pyrometer_temp_C", "frame_path",
    "capture_backend", "captured_at_utc", "capture_sequence",
    "frame_age_ms", "source_hwnd", "capture_geometry_id",
]

APPENDED_HEARTBEAT_FIELDS = [
    "captured_monotonic_ns", "frame_width", "frame_height",
    "view_segment_id", "visual_history_generation",
    "gun_aligned", "realignment_active",
    "calibration_id", "basis_bundle_id",
    "sensor_row_idx",
]


def _read_only_row(logger: GrowthLogger) -> dict[str, str]:
    path = logger.session_dir / "heartbeat_log.csv"
    logger.end_session()
    with open(path, newline="", encoding="utf-8") as stream:
        return next(csv.DictReader(stream))


def test_heartbeat_schema_only_appends_new_fields() -> None:
    assert GrowthLogger.HEARTBEAT_FIELDS[:len(LEGACY_HEARTBEAT_FIELDS)] == (
        LEGACY_HEARTBEAT_FIELDS
    )
    assert GrowthLogger.HEARTBEAT_FIELDS[len(LEGACY_HEARTBEAT_FIELDS):] == (
        APPENDED_HEARTBEAT_FIELDS
    )


def test_heartbeat_writes_frame_state_and_equalizer_provenance(tmp_path) -> None:
    logger = GrowthLogger(base_dir=tmp_path)
    logger.start_session("heartbeat-provenance")
    logger.log_heartbeat(
        elapsed_s=2.5,
        frame_path="frames/heartbeat_001.bmp",
        capture_metadata={
            "capture_backend": "wgc",
            "captured_at_utc": "2026-08-17T12:00:00.123Z",
            "captured_monotonic_ns": 9_123_456_789,
            "capture_sequence": 41,
            "frame_age_ms": 7.25,
            "source_hwnd": 9001,
            "capture_geometry_id": "wgc:9001:640x480:roi-full:v1",
            # Logger accepts either explicit frame_* or older camera_* names.
            "camera_width": 640,
            "camera_height": 480,
            "view_segment_id": 3,
            "visual_history_generation": 2,
            "gun_aligned": True,
            "realignment_active": False,
            "calibration_id": "cal-accepted-1",
            "basis_bundle_id": "basis-sha256-1",
        },
    )
    row = _read_only_row(logger)
    assert row["captured_monotonic_ns"] == "9123456789"
    assert row["frame_width"] == "640"
    assert row["frame_height"] == "480"
    assert row["view_segment_id"] == "3"
    assert row["visual_history_generation"] == "2"
    assert row["gun_aligned"] == "True"
    assert row["realignment_active"] == "False"
    assert row["calibration_id"] == "cal-accepted-1"
    assert row["basis_bundle_id"] == "basis-sha256-1"


def test_old_capture_metadata_keeps_appended_columns_blank(tmp_path) -> None:
    logger = GrowthLogger(base_dir=tmp_path)
    logger.start_session("legacy-heartbeat")
    logger.log_heartbeat(
        elapsed_s=1.0,
        frame_path="frames/heartbeat_001.bmp",
        capture_metadata={
            "capture_backend": "vimba",
            "captured_at_utc": "2026-08-17T12:00:00.123Z",
            "capture_sequence": 1,
        },
    )
    row = _read_only_row(logger)
    assert all(row[field] == "" for field in APPENDED_HEARTBEAT_FIELDS)
    assert row["capture_backend"] == "vimba"
    assert row["capture_sequence"] == "1"


def test_heartbeat_references_latest_successful_sensor_row(tmp_path) -> None:
    logger = GrowthLogger(base_dir=tmp_path)
    logger.start_session("heartbeat-sensor-link")
    assert logger.log_sensors(500.0, 1.0) == 1
    logger.log_heartbeat(
        elapsed_s=1.2,
        frame_path="frames/heartbeat_001.bmp",
    )

    row = _read_only_row(logger)

    assert row["sensor_row_idx"] == "1"


def test_growth_app_heartbeat_metadata_uses_saved_shape_and_accepted_calibration() -> None:
    camera_metadata = {
        "captured_monotonic_ns": 777,
        "view_segment_id": 4,
        "visual_history_generation": 6,
        "gun_aligned": True,
        "realignment_active": False,
        "camera_width": 999,
        "camera_height": 999,
    }
    monitor = SimpleNamespace(
        get_current_capture_metadata=lambda: dict(camera_metadata),
        live_equalizer_tab=SimpleNamespace(
            get_basis_bundle_id=lambda: "unaccepted-current-basis",
        ),
    )
    owner = SimpleNamespace(
        monitor=monitor,
        _equalizer_calibration=SimpleNamespace(
            calibration_id="accepted-calibration",
            basis_bundle_id="accepted-basis",
        ),
    )
    frame = np.zeros((96, 128, 3), dtype=np.uint8)
    metadata = GrowthApp._current_heartbeat_metadata(owner, frame)
    assert metadata["frame_width"] == 128
    assert metadata["frame_height"] == 96
    assert metadata["calibration_id"] == "accepted-calibration"
    assert metadata["basis_bundle_id"] == "accepted-basis"
    assert metadata["captured_monotonic_ns"] == 777
    assert metadata["view_segment_id"] == 4


def test_growth_app_records_basis_without_claiming_unaccepted_calibration() -> None:
    monitor = SimpleNamespace(
        get_current_capture_metadata=lambda: {},
        live_equalizer_tab=SimpleNamespace(
            get_basis_bundle_id=lambda: "loaded-basis",
        ),
    )
    owner = SimpleNamespace(monitor=monitor, _equalizer_calibration=None)
    metadata = GrowthApp._current_heartbeat_metadata(
        owner, np.zeros((12, 20, 3), dtype=np.uint8),
    )
    assert metadata["basis_bundle_id"] == "loaded-basis"
    assert "calibration_id" not in metadata
