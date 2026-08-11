"""Headless tests for the offline RHEED labeling desktop launcher."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QProcess, QSettings, QUrl  # noqa: E402
from PyQt6.QtGui import QCloseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from tools.rheed_postprocessing_labeling.desktop_launcher import (  # noqa: E402
    MANUAL_PATH,
    BuildRequest,
    LabelingDesktopLauncher,
    ModelPair,
    build_cli_arguments,
    parse_validation_response,
    validate_build_request,
)
from tools.rheed_postprocessing_labeling.report_builder import (  # noqa: E402
    _install_staged_report,
    build_report,
    validate_output_destination,
)


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication(["test-desktop-launcher"])
    return app


class RecordingLauncher(LabelingDesktopLauncher):
    """Launcher variant that records dialogs instead of blocking tests."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.warnings: list[tuple[str, str]] = []
        self.information: list[tuple[str, str]] = []
        self.critical: list[tuple[str, str, str]] = []
        super().__init__(*args, **kwargs)

    def _warning(self, title: str, message: str) -> None:
        self.warnings.append((title, message))

    def _information(self, title: str, message: str) -> None:
        self.information.append((title, message))

    def _critical(self, title: str, message: str, details: str = "") -> None:
        self.critical.append((title, message, details))

def _settings(path: Path) -> QSettings:
    return QSettings(str(path), QSettings.Format.IniFormat)


def test_default_manual_is_the_english_latex_edition() -> None:
    assert MANUAL_PATH.name == "RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf"


def _launcher(
    tmp_path: Path,
    qt_app: QApplication,
    *,
    opener: object | None = None,
    manual: Path | None = None,
    settings: QSettings | None = None,
    restore_settings: bool = False,
) -> RecordingLauncher:
    del qt_app
    repository = tmp_path / "checkout"
    repository.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {
        "settings": settings or _settings(tmp_path / "launcher.ini"),
        "repository_root": repository,
        "manual_path": manual or tmp_path / "manual.pdf",
        "restore_settings": restore_settings,
    }
    if opener is not None:
        kwargs["url_opener"] = opener
    return RecordingLauncher(**kwargs)


def _request_files(tmp_path: Path) -> tuple[Path, ModelPair]:
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    session = inputs / "session.zip"
    predictions = inputs / "predictions.csv"
    spec = inputs / "model.json"
    for path in (session, predictions, spec):
        path.write_bytes(b"test-only-placeholder")
    return session, ModelPair(predictions, spec)


def _write_generated_report(directory: Path, *, report_text: str = "generated") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "interactive_report.html").write_text(report_text, encoding="utf-8")
    (directory / "run_manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "outputs": {
                "interactive_report": "interactive_report.html",
                "images": "images/",
                "vendor": "vendor/",
                "third_party_notices": "vendor/THIRD_PARTY_NOTICES.md",
            },
            "annotation_policy": {
                "annotation_mode": "model_assisted_review",
                "model_outputs_visible": True,
                "eligible_for_gold": False,
            },
        }),
        encoding="utf-8",
    )
    images = directory / "images"
    images.mkdir(exist_ok=True)
    (images / "frame_0001.webp").write_bytes(b"synthetic-review-frame")
    vendor = directory / "vendor"
    vendor.mkdir(exist_ok=True)
    (vendor / "d3.v7.9.0.min.js").write_text("// vendored", encoding="utf-8")
    (vendor / "THIRD_PARTY_NOTICES.md").write_text("notice", encoding="utf-8")


def test_preflight_reuses_fail_closed_output_safety(tmp_path: Path) -> None:
    repository = tmp_path / "checkout"
    repository.mkdir()
    session, pair = _request_files(tmp_path)
    output = tmp_path / "reports" / "run-a"
    request = BuildRequest(session, (pair,), output, "Meeting review")

    assert validate_build_request(request, repository_root=repository) == []

    output.mkdir(parents=True)
    (output / "old.txt").write_text("old", encoding="utf-8")
    errors = validate_build_request(request, repository_root=repository)
    assert any("not empty" in error for error in errors)
    overwrite = BuildRequest(session, (pair,), output, "Meeting review", overwrite=True)
    errors = validate_build_request(overwrite, repository_root=repository)
    assert any("Overwrite is limited" in error for error in errors)
    (output / "old.txt").unlink()
    _write_generated_report(output)
    assert validate_build_request(overwrite, repository_root=repository) == []
    annotations = output / "annotations.json"
    annotations.write_text("{}", encoding="utf-8")
    errors = validate_build_request(overwrite, repository_root=repository)
    assert any("unmodified generated report" in error for error in errors)
    annotations.unlink()

    inside_repository = BuildRequest(
        session,
        (pair,),
        repository / "generated",
        "Unsafe",
    )
    errors = validate_build_request(inside_repository, repository_root=repository)
    assert any("outside the repository" in error for error in errors)

    input_ancestor = BuildRequest(session, (pair,), tmp_path / "inputs", "Unsafe")
    errors = validate_build_request(input_ancestor, repository_root=repository)
    assert any("contains an input file" in error for error in errors)


def test_output_guard_rejects_current_working_directory_and_ancestor(
    tmp_path: Path,
) -> None:
    unrelated_repository = tmp_path / "unrelated-checkout"
    unrelated_repository.mkdir()

    with pytest.raises(ValueError, match="current working directory"):
        validate_output_destination(Path.cwd(), repository_root=unrelated_repository)
    with pytest.raises(ValueError, match="current working directory"):
        validate_output_destination(Path.cwd().parent, repository_root=unrelated_repository)


def test_invalid_overwrite_input_preserves_existing_report(tmp_path: Path) -> None:
    session, pair = _request_files(tmp_path)
    pair.model_spec.write_text("{invalid json", encoding="utf-8")
    output = tmp_path / "reports" / "existing"
    _write_generated_report(output, report_text="original report sentinel")

    with pytest.raises(ValueError):
        build_report(
            session,
            [pair.predictions],
            [pair.model_spec],
            output,
            overwrite=True,
        )

    assert (
        output / "interactive_report.html"
    ).read_text(encoding="utf-8") == "original report sentinel"
    assert sorted(path.name for path in output.iterdir()) == [
        "images",
        "interactive_report.html",
        "run_manifest.json",
        "vendor",
    ]


def test_complete_stage_replaces_existing_generated_report(tmp_path: Path) -> None:
    destination = tmp_path / "existing-report"
    _write_generated_report(destination, report_text="old")
    stage = tmp_path / ".complete-stage"
    _write_generated_report(stage, report_text="new")

    _install_staged_report(stage, destination)

    assert (destination / "interactive_report.html").read_text(encoding="utf-8") == "new"
    assert not stage.exists()
    assert not list(tmp_path.glob(".existing-report.backup-*"))


def test_report_changed_during_build_is_restored_without_data_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "existing-report"
    _write_generated_report(destination, report_text="old")
    stage = tmp_path / ".complete-stage"
    _write_generated_report(stage, report_text="new")
    original_rename = Path.rename

    def inject_annotation_after_backup(path: Path, target: Path) -> Path:
        result = original_rename(path, target)
        if path == destination:
            (target / "rheed_segment_annotations.json").write_text(
                '{"sentinel":"must survive"}\n', encoding="utf-8"
            )
        return result

    monkeypatch.setattr(Path, "rename", inject_annotation_after_backup)

    with pytest.raises(ValueError, match="unmodified generated report"):
        _install_staged_report(stage, destination)

    assert (destination / "interactive_report.html").read_text(encoding="utf-8") == "old"
    assert (destination / "rheed_segment_annotations.json").read_text(
        encoding="utf-8"
    ) == '{"sentinel":"must survive"}\n'
    assert stage.is_dir()
    assert not list(tmp_path.glob(".existing-report.backup-*"))


def test_failed_final_install_restores_previous_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "existing-report"
    _write_generated_report(destination, report_text="old sentinel")
    stage = tmp_path / ".complete-stage"
    _write_generated_report(stage, report_text="new")
    original_rename = Path.rename

    def fail_stage_rename(path: Path, target: Path) -> Path:
        if path == stage:
            raise OSError("simulated final rename failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_stage_rename)

    with pytest.raises(OSError, match="simulated final rename failure"):
        _install_staged_report(stage, destination)

    assert (
        destination / "interactive_report.html"
    ).read_text(encoding="utf-8") == "old sentinel"
    assert stage.is_dir()
    assert not list(tmp_path.glob(".existing-report.backup-*"))


def test_build_arguments_preserve_model_pair_order_and_spaces(tmp_path: Path) -> None:
    request = BuildRequest(
        tmp_path / "session with spaces.zip",
        (
            ModelPair(tmp_path / "first.csv", tmp_path / "first spec.json"),
            ModelPair(tmp_path / "第二.csv", tmp_path / "第二.json"),
        ),
        tmp_path / "output folder",
        "Anneal review",
        review_quality=82,
        overwrite=True,
    )

    arguments = build_cli_arguments(request)

    predictions = arguments[arguments.index("--predictions") + 1:arguments.index("--model-spec")]
    specifications = arguments[
        arguments.index("--model-spec") + 1:arguments.index("--output-dir")
    ]
    assert predictions == [str(pair.predictions) for pair in request.model_pairs]
    assert specifications == [str(pair.model_spec) for pair in request.model_pairs]
    assert arguments[-1] == "--overwrite"


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "{}",
        '{"valid":false,"dataset_id":"run","segment_count":1}',
        '{"valid":true,"dataset_id":"","segment_count":1}',
        '{"valid":true,"dataset_id":"run","segment_count":true}',
        '{"valid":true,"dataset_id":"run","segment_count":-1}',
        '{"valid":true,"dataset_id":"run","segment_count":1,"extra":0}',
    ],
)
def test_validation_response_is_strict(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_validation_response(payload)


def test_validation_response_accepts_only_exact_success_schema() -> None:
    result = parse_validation_response(
        '{"valid":true,"dataset_id":" run-123 ","segment_count":4}'
    )
    assert result.dataset_id == "run-123"
    assert result.segment_count == 4


def test_model_pairs_move_together_and_remain_ordered(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    launcher = _launcher(tmp_path, qt_app)
    launcher.add_model_pair(Path("first.csv"), Path("first.json"))
    launcher.add_model_pair(Path("second.csv"), Path("second.json"))

    launcher._move_selected_pair(-1)

    assert launcher._model_pairs() == (
        ModelPair(Path("second.csv"), Path("second.json")),
        ModelPair(Path("first.csv"), Path("first.json")),
    )
    launcher.deleteLater()


def test_versioned_settings_restore_order_but_never_overwrite(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    settings_path = tmp_path / "launcher.ini"
    first = _launcher(
        tmp_path,
        qt_app,
        settings=_settings(settings_path),
    )
    first.session_edit.setText("session.zip")
    first.output_edit.setText("report-output")
    first.report_edit.setText("interactive_report.html")
    first.annotations_edit.setText("annotations.json")
    first.title_edit.setText("Custom title")
    first.quality_spin.setValue(91)
    first.add_model_pair(Path("a.csv"), Path("a.json"))
    first.add_model_pair(Path("b.csv"), Path("b.json"))
    first._save_settings()
    first.deleteLater()

    restored = _launcher(
        tmp_path,
        qt_app,
        settings=_settings(settings_path),
        restore_settings=True,
    )

    assert restored.session_edit.text() == "session.zip"
    assert restored.output_edit.text() == "report-output"
    assert restored.report_edit.text() == "interactive_report.html"
    assert restored.annotations_edit.text() == "annotations.json"
    assert restored.title_edit.text() == "Custom title"
    assert restored.quality_spin.value() == 91
    assert restored._model_pairs() == (
        ModelPair(Path("a.csv"), Path("a.json")),
        ModelPair(Path("b.csv"), Path("b.json")),
    )
    assert not restored._build_request().overwrite
    restored.deleteLater()


def test_busy_state_is_indeterminate_and_disables_all_actions(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    launcher = _launcher(tmp_path, qt_app)

    launcher._set_busy(True)

    assert launcher.progress.minimum() == 0
    assert launcher.progress.maximum() == 0
    assert not launcher.progress.isHidden()
    assert all(not widget.isEnabled() for widget in launcher._mutable_widgets)
    assert launcher._process.processEnvironment().value("PYTHONNOUSERSITE") == "1"
    launcher._set_busy(False)
    launcher.deleteLater()


def test_report_and_manual_opening_check_desktop_service_return(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    opened: list[str] = []

    def opener(url: QUrl) -> bool:
        opened.append(url.toLocalFile())
        return True

    report = tmp_path / "interactive_report.html"
    manual = tmp_path / "manual.pdf"
    report.write_text("<html></html>", encoding="utf-8")
    manual.write_bytes(b"%PDF-test")
    launcher = _launcher(tmp_path, qt_app, opener=opener, manual=manual)
    launcher.report_edit.setText(str(report))

    launcher._open_report()
    launcher._open_manual()

    assert [Path(path) for path in opened] == [report.resolve(), manual.resolve()]
    assert launcher.critical == []
    launcher.deleteLater()

    rejected = _launcher(tmp_path / "rejected", qt_app, opener=lambda _url: False)
    rejected_manual = tmp_path / "rejected-manual.pdf"
    rejected_manual.write_bytes(b"%PDF-test")
    rejected._manual_path = rejected_manual
    rejected._open_manual()
    assert rejected.critical[-1][0] == "Could not open file"
    rejected.deleteLater()

    missing = _launcher(tmp_path / "missing", qt_app)
    missing._open_manual()
    assert missing.warnings[-1][0] == "File not found"
    missing.deleteLater()


def test_successful_build_auto_opens_expected_report(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    opened: list[str] = []
    launcher = _launcher(
        tmp_path,
        qt_app,
        opener=lambda url: opened.append(url.toLocalFile()) is None,
    )
    report = tmp_path / "report" / "interactive_report.html"
    report.parent.mkdir()
    report.write_text("<html></html>", encoding="utf-8")
    launcher._pending_action = "build"
    launcher._expected_report = report
    launcher._completion_handled = False

    launcher._process_finished(0, QProcess.ExitStatus.NormalExit)

    assert [Path(path) for path in opened] == [report.resolve()]
    assert launcher.report_edit.text() == str(report)
    assert launcher.tabs.currentWidget() is launcher._review_tab
    assert launcher.status_label.text().startswith("Opened report:")
    assert launcher.critical == []
    launcher.deleteLater()


def test_build_and_validation_results_fail_closed(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    launcher = _launcher(tmp_path, qt_app, opener=lambda _url: False)
    report = tmp_path / "interactive_report.html"
    report.write_text("<html></html>", encoding="utf-8")
    launcher._pending_action = "build"
    launcher._expected_report = report
    launcher._completion_handled = False
    launcher._process_finished(0, QProcess.ExitStatus.NormalExit)
    assert "did not open" in launcher.status_label.text()
    assert launcher.critical[-1][0] == "Could not open file"

    launcher._pending_action = "validate"
    launcher._stdout_buffer = [
        json.dumps({"valid": True, "dataset_id": "run-a", "segment_count": 3})
    ]
    launcher._completion_handled = False
    launcher._process_finished(0, QProcess.ExitStatus.NormalExit)
    assert launcher.validation_result.text() == "Valid - 3 segment(s) - dataset run-a"

    launcher._pending_action = "validate"
    launcher._stdout_buffer = ["warning before JSON\n{}"]
    launcher._completion_handled = False
    launcher._process_finished(0, QProcess.ExitStatus.NormalExit)
    assert launcher.critical[-1][0] == "Invalid validation response"
    launcher.deleteLater()


def test_close_does_not_kill_running_worker(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    launcher = _launcher(tmp_path, qt_app)
    launcher._start_process(
        ["-c", "import time; time.sleep(5)"],
        "Testing...",
        action="test",
    )
    assert launcher._process.waitForStarted(3000)
    event = QCloseEvent()

    launcher.closeEvent(event)

    assert not event.isAccepted()
    assert launcher._process.state() != QProcess.ProcessState.NotRunning
    assert launcher.information[-1][0] == "Offline task still running"

    # The UI deliberately leaves the worker alive; the test owns cleanup.
    launcher._completion_handled = True
    launcher._process.kill()
    assert launcher._process.waitForFinished(3000)
    launcher.deleteLater()


def test_desktop_subcommand_import_is_lazy() -> None:
    source = Path(__file__).parents[1].joinpath("cli.py").read_text(encoding="utf-8")
    import_line = "from .desktop_launcher import main as desktop_main"
    assert import_line in source
    assert source.index("def _desktop") < source.index(import_line)
    assert import_line not in source[:source.index("def _desktop")]
