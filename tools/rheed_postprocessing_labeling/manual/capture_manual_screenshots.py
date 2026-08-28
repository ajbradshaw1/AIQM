"""Capture current application UIs for the English operator manual.

The harness is deliberately hardware-free.  It creates analytic RHEED frames,
synthetic predictions, and an isolated session archive under ``--fixture-root``.
It never arms Growth Monitor, starts a worker, loads a checkpoint, or reads a
laboratory archive.  The resulting PNGs are screenshots of the real Qt and
browser applications, not redrawn interface mock-ups.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PYTHONNOUSERSITE", "1")
os.environ["AIQM_CHAMBER"] = "chmbe"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")
os.environ.setdefault("QT_FONT_DPI", "96")
os.environ.setdefault("MPLBACKEND", "Agg")

import numpy as np
from PIL import Image
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QMainWindow

from drivers.config import CHALCOGENIDE_MBE, OXIDE_MBE
from gui.growth_monitor import GrowthMonitor
from gui.live_equalizer_tab import LiveEqualizerTab
from gui.state import (
    CameraState,
    ClassifierState,
    EvapControlState,
    MistralState,
    PyrometerState,
    RheedQcState,
)
from tools.rheed_postprocessing_labeling.desktop_launcher import (
    LabelingDesktopLauncher,
)
from tools.rheed_postprocessing_labeling.report_builder import build_report


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MANUAL_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = MANUAL_ROOT / "assets" / "screenshots"
FRAME_COUNT = 60


def _rheed_frame(index: int) -> np.ndarray:
    """Return a deterministic 656x492 RGB synthetic diffraction frame."""
    height, width = 492, 656
    yy, xx = np.mgrid[0:height, 0:width]
    phase = min(index // 15, 3)
    local = index % 15
    brightness = 0.92 + 0.06 * math.sin(local * math.pi / 7.0)
    image = np.full((height, width), 4.0, dtype=np.float64)
    center_y = 250 + (phase - 1.5) * 5
    image += 24.0 * np.exp(-((yy - center_y) ** 2) / (2 * 2.7**2))
    spot_layouts = (
        (-170, 0, 170),
        (-150, -48, 48, 150),
        (-205, -125, -42, 42, 125, 205),
        (-188, -96, 0, 96, 188),
    )
    for number, offset in enumerate(spot_layouts[phase]):
        x0 = width / 2 + offset
        y0 = center_y - 24 - 10 * ((number + phase) % 2)
        image += 185.0 * np.exp(
            -((xx - x0) ** 2) / (2 * 6.0**2)
            -((yy - y0) ** 2) / (2 * 4.0**2)
        )
        image += 58.0 * np.exp(
            -((xx - x0) ** 2) / (2 * 3.5**2)
            -((yy - center_y) ** 2) / (2 * 45.0**2)
        )
    image *= brightness
    image = np.clip(image, 0, 255).astype(np.uint8)
    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    rgb[..., 0] = (image.astype(np.float32) * 0.16).astype(np.uint8)
    rgb[..., 1] = image
    rgb[..., 2] = (image.astype(np.float32) * 0.10).astype(np.uint8)
    return rgb


def _png_bytes(frame: np.ndarray) -> bytes:
    output = io.BytesIO()
    Image.fromarray(frame, "RGB").save(output, format="PNG", optimize=False)
    return output.getvalue()


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _csv_text(rows: list[dict[str, object]]) -> str:
    """Serialize deterministic synthetic rows for an in-memory ZIP member."""

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=list(rows[0]),
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _scores(index: int, class_count: int, *, include_1x1: bool) -> list[float]:
    phase = min(index // 15, 3)
    if include_1x1:
        target = phase
        size = class_count
    else:
        target = max(phase - 1, 0)
        size = class_count
    raw = np.full(size, 0.035, dtype=np.float64)
    raw[target] = 0.86
    if target + 1 < size:
        raw[target + 1] += 0.035 * ((index % 15) / 14.0)
    raw /= raw.sum()
    return [float(value) for value in raw]


def _create_fixture(root: Path) -> tuple[Path, tuple[Path, ...], tuple[Path, ...]]:
    root.mkdir(parents=True, exist_ok=True)
    archive_path = root / "synthetic_rheed_session.zip"
    heartbeat_rows: list[dict[str, object]] = []
    sensor_rows: list[dict[str, object]] = []
    provenance_rows: list[dict[str, object]] = []
    frame_payloads: list[tuple[str, bytes]] = []
    start = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    for index in range(FRAME_COUNT):
        captured = start + timedelta(seconds=index + (0.2 if index >= 30 else 0.0))
        captured_text = captured.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        name = f"synthetic_rheed_{index + 1:04d}.png"
        payload = _png_bytes(_rheed_frame(index))
        digest = hashlib.sha256(payload).hexdigest()
        frame_payloads.append((name, payload))
        provenance = {
            "frame_index": index,
            "heartbeat_idx": index + 1,
            "elapsed_s": round(index + (0.2 if index >= 30 else 0.0), 3),
            "captured_at_utc": captured_text,
            "capture_sequence": 1000 + index,
            "frame_name": name,
            "frame_sha256": digest,
        }
        provenance_rows.append(provenance)
        heartbeat_rows.append(
            {
                "timestamp": captured_text,
                "captured_at_utc": captured_text,
                "elapsed_s": provenance["elapsed_s"],
                "heartbeat_idx": provenance["heartbeat_idx"],
                "capture_sequence": provenance["capture_sequence"],
                "pyrometer_temp_C": round(410.0 + 2.0 * index, 1),
                "frame_path": rf"D:\RHEED_Demo\frames\{name}",
                "frame_sha256": digest,
                "capture_backend": "synthetic_manual_fixture",
            }
        )
        sensor_rows.append(
            {
                "timestamp": captured_text,
                "snapshot_at_utc": captured_text,
                "elapsed_s": provenance["elapsed_s"],
                "pyrometer_temp_C": round(410.0 + 2.0 * index, 1),
                "mistral_v_actual_V": round(3.2 + 0.006 * index, 3),
                "mistral_i_actual_A": round(0.118 + 0.0004 * index, 4),
                "pyrometer_age_ms": 34 + index % 5,
                "mistral_age_ms": 76 + index % 7,
                "evap_age_ms": 128 + index % 11,
                "rheed_age_ms": 18 + index % 3,
            }
        )

    auto_event_rows: list[dict[str, object]] = []
    auto_capture_members: list[tuple[str, str, str, bytes]] = []
    for event_index, frame_index in enumerate((18, 38), 1):
        provenance = provenance_rows[frame_index]
        buffer_dir = f"auto_capture/event_{event_index:04d}"
        buffered_name = f"candidate_{event_index:04d}.png"
        buffered_member = (
            f"synthetic_session/{buffer_dir}/frames/{buffered_name}"
        )
        auto_event_rows.append(
            {
                "event_idx": event_index,
                "timestamp": provenance["captured_at_utc"],
                "elapsed_s": provenance["elapsed_s"],
                "capture_sequence": provenance["capture_sequence"],
                "change_score": 0.84 if event_index == 1 else 0.73,
                "event_state": "pending",
                "state_changed_at": "",
                "buffer_dir": buffer_dir,
                "frame_path": f"frames/{buffered_name}",
            }
        )
        manifest_text = _csv_text(
            [
                {
                    "capture_sequence": provenance["capture_sequence"],
                    "frame_path": f"frames/{buffered_name}",
                    "captured_at_utc": provenance["captured_at_utc"],
                    "elapsed_s": provenance["elapsed_s"],
                    "frame_sha256": provenance["frame_sha256"],
                }
            ]
        )
        auto_capture_members.append(
            (
                f"synthetic_session/{buffer_dir}/capture_manifest.csv",
                manifest_text,
                buffered_member,
                frame_payloads[frame_index][1],
            )
        )
    metadata = {
        "session_id": "synthetic-manual-screenshot-session",
        "chamber_id": "DEMO-MBE",
        "camera_backend": "synthetic_manual_fixture",
        "capture_geometry_id": "synthetic-656x492",
        "geometry": "synthetic-656x492",
        "started_at_utc": heartbeat_rows[0]["captured_at_utc"],
    }
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "synthetic_session/session_metadata.json",
            json.dumps(metadata, sort_keys=True),
        )
        archive.writestr(
            "synthetic_session/heartbeat_log.csv",
            _csv_text(heartbeat_rows),
        )
        archive.writestr(
            "synthetic_session/sensor_log.csv",
            _csv_text(sensor_rows),
        )
        archive.writestr(
            "synthetic_session/auto_capture_events.csv",
            _csv_text(auto_event_rows),
        )
        for name, payload in frame_payloads:
            archive.writestr(f"synthetic_session/frames/{name}", payload)
        for (
            manifest_member,
            manifest_text,
            buffered_member,
            payload,
        ) in auto_capture_members:
            archive.writestr(manifest_member, manifest_text)
            archive.writestr(buffered_member, payload)

    specs = (
        {
            "schema_version": 1,
            "key": "demo_five_output",
            "title": "Synthetic five-output demonstration",
            "subtitle": "Generated scores for UI documentation only",
            "classes": ["1x1", "twinned_2x1", "c_6x2", "rt13", "htr"],
            "probability_columns": ["p_1x1", "p_twinned", "p_c6", "p_rt13", "p_htr"],
            "quality_column": "quality",
            "predicted_column": "predicted",
            "status": "synthetic_test_only",
            "provenance": {"checkpoint_sha256": "0" * 64},
        },
        {
            "schema_version": 1,
            "key": "demo_four_output",
            "title": "Synthetic four-output demonstration",
            "subtitle": "No 1x1 output; generated scores only",
            "classes": ["twinned_2x1", "c_6x2", "rt13", "htr"],
            "probability_columns": ["p_twinned", "p_c6", "p_rt13", "p_htr"],
            "quality_column": "quality",
            "predicted_column": "predicted",
            "status": "synthetic_test_only",
            "provenance": {"checkpoint_sha256": "1" * 64},
        },
    )
    prediction_paths: list[Path] = []
    spec_paths: list[Path] = []
    for model_index, spec in enumerate(specs):
        spec_path = root / f"model_{model_index + 1}_spec.json"
        spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        spec_paths.append(spec_path)
        rows: list[dict[str, object]] = []
        for index, provenance in enumerate(provenance_rows):
            values = _scores(
                index,
                len(spec["classes"]),
                include_1x1=model_index == 0,
            )
            row = dict(provenance)
            row.update(dict(zip(spec["probability_columns"], values, strict=True)))
            row["quality"] = 0.93
            row["predicted"] = spec["classes"][int(np.argmax(values))]
            rows.append(row)
        prediction_path = root / f"model_{model_index + 1}_predictions.csv"
        _write_csv(prediction_path, rows)
        prediction_paths.append(prediction_path)
    return archive_path, tuple(prediction_paths), tuple(spec_paths)


def _save_widget(widget: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap = widget.grab()
    if pixmap.isNull() or not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"Unable to capture Qt widget: {path}")


def _capture_qt(
    output_dir: Path,
    fixture_root: Path,
    archive: Path,
    predictions: tuple[Path, ...],
    specs: tuple[Path, ...],
    report: Path,
) -> None:
    app = QApplication.instance() or QApplication(["manual-screenshot-capture"])
    font_paths = (
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
        for name in (
            "segoeui.ttf",
            "segoeuib.ttf",
            "seguisym.ttf",
            "arial.ttf",
            "arialbd.ttf",
            "consola.ttf",
            "consolab.ttf",
        )
    )
    loaded_fonts = 0
    for font_path in font_paths:
        if font_path.is_file() and QFontDatabase.addApplicationFont(str(font_path)) >= 0:
            loaded_fonts += 1
    if loaded_fonts == 0:
        raise RuntimeError("No Windows fonts could be loaded for offscreen capture")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))
    def capture_monitor(config, stem: str) -> None:
        # GrowthMonitor is the exact central widget used by GrowthApp, but it
        # owns no hardware workers or logger. The retained, hidden Equalizer
        # compatibility widget must not read repository image assets while the
        # documentation window is being constructed.
        os.environ["AIQM_CHAMBER"] = config.chamber_id
        with patch.object(LiveEqualizerTab, "_load_basis", lambda self: None):
            monitor = GrowthMonitor(config=config)
        shell = QMainWindow()
        shell.setWindowTitle(f"{config.name} Growth Monitor")
        shell.setCentralWidget(monitor)
        # A common 16:9 workstation canvas keeps the five read-only RHEED
        # adjustment buttons and their status label legible without altering
        # the production widget layout.
        shell.resize(1600, 900)
        chamber_token = stem.upper().replace("-", "_")
        monitor.grower_input.setText("Demo Operator")
        monitor.sample_id_input.setText(f"SYNTHETIC_{chamber_token}_DEMO")
        monitor.config_save_path.setText(
            rf"D:\SYNTHETIC_{chamber_token}_DEMO"
        )
        monitor.config_exactus_port.setText("DEMO")
        for combo in (
            monitor.config_camera_mode,
            monitor.config_pyrometer_mode,
            monitor.config_mistral_mode,
            monitor.config_evap_mode,
        ):
            # These are display-only selections on a standalone monitor.
            # No GrowthApp signal is connected, so DummyCamera is not made.
            combo.setCurrentText("dummy")
        monitor.update_camera_state(
            CameraState(
                frame=_rheed_frame(48),
                frame_number=49,
                width=656,
                height=492,
                intensity=42.0,
                connected=True,
                valid=True,
                mode="dummy",
                capture_backend="synthetic_manual_fixture",
                captured_at_utc="2026-01-02T03:04:53.200Z",
                capture_sequence=1048,
                sample_sequence=49,
                capture_geometry_id="synthetic-656x492",
            )
        )
        monitor.update_pyrometer_state(
            PyrometerState(
                temperature=506.0,
                temperature_std=0.2,
                temperature_n=5,
                connected=True,
                valid=True,
                mode="dummy",
                sample_sequence=49,
            )
        )
        monitor.update_mistral_state(
            MistralState(
                v_set=4.0,
                v_actual=3.98,
                i_set=0.55,
                i_actual=0.54,
                connected=True,
                valid=True,
                mode="dummy",
                sample_sequence=49,
            )
        )
        monitor.update_evap_state(
            EvapControlState(
                chamber_pressure_mbar=2.1e-10,
                connected=True,
                valid=True,
                mode="dummy",
                sample_sequence=49,
            )
        )
        scores = {
            "1x1": 4,
            "Twinned (2x1)": 8,
            "c(6x2)": 13,
            "rt13xrt13": 69,
            "HTR": 6,
        }
        monitor.update_classifier_state(
            ClassifierState(
                loading=False,
                ready=True,
                last_frame_number=49,
                raw_scores={
                    key: value / 100.0 for key, value in scores.items()
                },
                normalized_percent=scores,
                smoothed_percent=scores,
                raw_sum=1.0,
                quality=0.91,
                has_confident_data=True,
                inference_ms=86.7,
                model_version="synthetic documentation state",
                prediction_actionable=False,
                model_input_mode="single_frame",
            )
        )
        monitor.update_rheed_qc_state(
            RheedQcState(
                session_active=False,
                view_segment_id=1,
                visual_history_generation=1,
                gun_aligned=True,
                history_frame_count=32,
                history_required=32,
                history_ready=True,
            )
        )
        monitor.elapsed_display.value.setText("00:00:49.20")
        monitor._tabs.setCurrentIndex(0)
        shell.show()
        app.processEvents()
        _save_widget(shell, output_dir / f"{stem}_growth_monitor_dummy.png")

        session_index = next(
            index
            for index in range(monitor._tabs.count())
            if monitor._tabs.tabText(index) == "Session"
        )
        monitor._tabs.setCurrentIndex(session_index)
        app.processEvents()
        _save_widget(shell, output_dir / f"{stem}_growth_monitor_session.png")

        monitor.live_equalizer_tab._gate_timer.stop()
        monitor._elapsed_timer.stop()
        shell.close()
        shell.deleteLater()
        app.processEvents()

    capture_monitor(CHALCOGENIDE_MBE, "chmbe")
    capture_monitor(OXIDE_MBE, "ombe")

    settings = QSettings(
        str(fixture_root / "labeler-screenshot.ini"),
        QSettings.Format.IniFormat,
    )
    labeler = LabelingDesktopLauncher(
        settings=settings,
        restore_settings=False,
        url_opener=lambda _url: False,
    )
    labeler.resize(1280, 820)
    labeler.session_edit.setText(str(archive))
    for prediction, spec in zip(predictions, specs, strict=True):
        labeler.add_model_pair(prediction, spec)
    next_output = fixture_root / "next-report"
    next_output.mkdir()
    labeler.output_edit.setText(str(next_output))
    labeler.title_edit.setText("Synthetic reconstruction review - operator training")
    labeler.status_label.setText("Ready - generated demonstration inputs")
    labeler.show()
    app.processEvents()
    _save_widget(labeler, output_dir / "rheed_labeler_build.png")

    labeler.close()
    labeler.deleteLater()
    app.processEvents()


def _capture_browser(report: Path, output_dir: Path) -> None:
    script = MANUAL_ROOT / "capture_manual_report_screenshots.js"
    completed = subprocess.run(
        ["node", str(script), str(report), str(output_dir)],
        cwd=REPOSITORY_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode:
        raise RuntimeError(
            "Browser screenshot capture failed.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    if completed.stdout:
        print(completed.stdout.rstrip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--fixture-root",
        type=Path,
        required=True,
        help="Scratch directory outside the repository for generated inputs",
    )
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    fixture_parent = args.fixture_root.resolve()
    if fixture_parent == REPOSITORY_ROOT or fixture_parent.is_relative_to(
        REPOSITORY_ROOT
    ):
        parser.error("--fixture-root must be outside the repository")
    fixture_parent.mkdir(parents=True, exist_ok=True)
    fixture_root = Path(tempfile.mkdtemp(prefix="capture-", dir=fixture_parent))
    archive, predictions, specs = _create_fixture(fixture_root / "inputs")
    report = build_report(
        archive,
        predictions,
        specs,
        fixture_root / "report",
        report_title="Synthetic reconstruction review - operator training",
    )
    _capture_qt(output_dir, fixture_root, archive, predictions, specs, report)
    if not args.skip_browser:
        _capture_browser(report, output_dir)
    expected = [
        "chmbe_growth_monitor_dummy.png",
        "chmbe_growth_monitor_session.png",
        "ombe_growth_monitor_dummy.png",
        "ombe_growth_monitor_session.png",
        "rheed_labeler_build.png",
    ]
    if not args.skip_browser:
        expected.extend(("rheed_timeline_editor.png", "rheed_timeline_zoom.png"))
    for filename in expected:
        path = output_dir / filename
        if not path.is_file():
            raise RuntimeError(f"Expected screenshot was not produced: {path}")
        with Image.open(path) as image:
            print(f"{path} | {image.width}x{image.height} | {path.stat().st_size} bytes")
    print(f"Synthetic fixture retained outside Git: {fixture_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
