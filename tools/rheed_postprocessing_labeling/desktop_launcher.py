"""Minimal PyQt6 launcher for offline RHEED report build/open/validation."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QByteArray, QProcess, QProcessEnvironment, QSettings, QUrl
from PyQt6.QtGui import QCloseEvent, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .report_builder import (
    validate_existing_report_destination,
    validate_output_destination,
)
from .loopback_service import LoopbackReportService


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_MODULE = "tools.rheed_postprocessing_labeling"
SETTINGS_VERSION = 1
MANUAL_PATH = REPOSITORY_ROOT / "docs" / "RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf"


@dataclass(frozen=True)
class ModelPair:
    predictions: Path
    model_spec: Path


@dataclass(frozen=True)
class BuildRequest:
    session: Path
    model_pairs: tuple[ModelPair, ...]
    output_dir: Path
    title: str
    review_quality: int = 78
    overwrite: bool = False


@dataclass(frozen=True)
class ValidationResult:
    dataset_id: str
    item_count: int
    item_kind: str

    @property
    def segment_count(self) -> int:
        """Compatibility alias for callers that validate legacy v1 reports."""

        return self.item_count

    @property
    def event_count(self) -> int:
        return self.item_count


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def validate_build_request(
    request: BuildRequest,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> list[str]:
    """Return actionable preflight errors without reading laboratory data."""
    errors: list[str] = []
    if not request.session.is_file():
        errors.append("Choose an existing Growth Monitor session ZIP.")
    elif request.session.suffix.lower() != ".zip":
        errors.append("The session input must be a .zip archive.")

    if not request.model_pairs:
        errors.append("Add at least one prediction CSV / model-spec JSON pair.")
    for index, pair in enumerate(request.model_pairs, start=1):
        if not pair.predictions.is_file():
            errors.append(f"Model {index}: prediction CSV does not exist.")
        elif pair.predictions.suffix.lower() != ".csv":
            errors.append(f"Model {index}: predictions must be a .csv file.")
        if not pair.model_spec.is_file():
            errors.append(f"Model {index}: model spec does not exist.")
        elif pair.model_spec.suffix.lower() != ".json":
            errors.append(f"Model {index}: model spec must be a .json file.")

    input_paths = [request.session]
    input_paths.extend(pair.predictions for pair in request.model_pairs)
    input_paths.extend(pair.model_spec for pair in request.model_pairs)
    try:
        validate_output_destination(
            request.output_dir,
            input_paths,
            repository_root=repository_root,
        )
        validate_existing_report_destination(
            request.output_dir,
            overwrite=request.overwrite,
        )
    except (OSError, ValueError) as exc:
        errors.append(str(exc))

    if not request.title.strip():
        errors.append("Enter a report title.")
    if not 25 <= request.review_quality <= 100:
        errors.append("Review-image quality must be between 25 and 100.")
    return errors


def build_cli_arguments(request: BuildRequest) -> list[str]:
    """Return a shell-free argument vector preserving positional model pairs."""
    arguments = [
        "-m",
        PACKAGE_MODULE,
        "build",
        "--session",
        str(request.session),
        "--predictions",
    ]
    arguments.extend(str(pair.predictions) for pair in request.model_pairs)
    arguments.append("--model-spec")
    arguments.extend(str(pair.model_spec) for pair in request.model_pairs)
    arguments.extend([
        "--output-dir",
        str(request.output_dir),
        "--title",
        request.title.strip(),
        "--review-quality",
        str(request.review_quality),
    ])
    if request.overwrite:
        arguments.append("--overwrite")
    return arguments


def validation_cli_arguments(report: Path, annotations: Path) -> list[str]:
    return [
        "-m",
        PACKAGE_MODULE,
        "validate",
        "--report",
        str(report),
        "--annotations",
        str(annotations),
    ]


def parse_validation_response(payload: str) -> ValidationResult:
    """Strictly parse the validate subcommand's success response."""
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Validation worker returned malformed JSON") from exc
    if not isinstance(value, dict) or value.get("valid") is not True:
        raise ValueError("Validation worker returned an unexpected response schema")
    if not isinstance(value.get("dataset_id"), str) or not value["dataset_id"].strip():
        raise ValueError("Validation worker returned an invalid dataset ID")
    count_keys = [key for key in ("event_count", "segment_count") if key in value]
    if len(count_keys) != 1:
        raise ValueError("Validation worker returned no unambiguous item count")
    count_key = count_keys[0]
    allowed_keys = {"valid", "dataset_id", count_key, "schema_version"}
    if not set(value).issubset(allowed_keys):
        raise ValueError("Validation worker returned an unexpected response schema")
    if type(value[count_key]) is not int or value[count_key] < 0:
        raise ValueError("Validation worker returned an invalid item count")
    return ValidationResult(
        value["dataset_id"].strip(),
        value[count_key],
        "event" if count_key == "event_count" else "segment",
    )


class LabelingDesktopLauncher(QMainWindow):
    """Desktop shell; CPU/memory-heavy report work stays in a child process."""

    def __init__(
        self,
        *,
        settings: QSettings | None = None,
        url_opener: Callable[[QUrl], bool] | None = None,
        repository_root: Path = REPOSITORY_ROOT,
        manual_path: Path = MANUAL_PATH,
        restore_settings: bool = True,
    ) -> None:
        super().__init__()
        self._repository_root = _resolved(repository_root)
        self._manual_path = _resolved(manual_path)
        self._settings = settings or QSettings(
            "AI4MBE",
            "RHEEDPostprocessingLabeling",
        )
        self._url_opener = url_opener or QDesktopServices.openUrl
        self._process = QProcess(self)
        self._process.setWorkingDirectory(str(self._repository_root))
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONNOUSERSITE", "1")
        self._process.setProcessEnvironment(environment)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.readyReadStandardOutput.connect(self._read_stdout)
        self._process.readyReadStandardError.connect(self._read_stderr)
        self._process.finished.connect(self._process_finished)
        self._process.errorOccurred.connect(self._process_error)
        self._pending_action = ""
        self._expected_report: Path | None = None
        self._stdout_buffer: list[str] = []
        self._stderr_buffer: list[str] = []
        self._completion_handled = False
        self._loopback_service: LoopbackReportService | None = None
        self._equalizer_coordinator = None

        self.setWindowTitle("RHEED Post-processing and Temporal Labeling")
        self.resize(980, 760)
        central = QWidget(self)
        root = QVBoxLayout(central)
        self.setCentralWidget(central)

        intro = QLabel(
            "Build a portable report from an archived Growth Monitor session, "
            "then review and label saved RHEED frames in the system browser. "
            "This tool never connects to instruments or changes the live GUI."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)
        safety = QLabel(
            "Model outputs are visible: exported labels are model-assisted review "
            "data and are not eligible as blind-gold training labels."
        )
        safety.setWordWrap(True)
        root.addWidget(safety)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self._build_tab = self._create_build_tab()
        self._review_tab = self._create_review_tab()
        self.tabs.addTab(self._build_tab, "Build")
        self.tabs.addTab(self._review_tab, "Open / Validate")

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        root.addWidget(self.progress)
        self.status_label = QLabel("Ready")
        root.addWidget(self.status_label)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText("Build and validation messages appear here.")
        root.addWidget(self.log, 1)

        self._mutable_widgets = [
            self.session_edit,
            self.session_button,
            self.model_table,
            self.add_pair_button,
            self.remove_pair_button,
            self.move_up_button,
            self.move_down_button,
            self.output_edit,
            self.output_button,
            self.title_edit,
            self.quality_spin,
            self.build_button,
            self.report_edit,
            self.report_button,
            self.open_report_button,
            self.open_manual_button,
            self.annotations_edit,
            self.annotations_button,
            self.validate_button,
        ]
        if restore_settings:
            self._restore_settings()
        self._update_action_states()

    def _create_build_tab(self) -> QWidget:
        tab = QWidget()
        layout = QGridLayout(tab)
        self.session_edit = QLineEdit()
        self.session_button = QPushButton("Browse...")
        self.session_button.clicked.connect(self._choose_session)
        layout.addWidget(QLabel("Session ZIP"), 0, 0)
        layout.addWidget(self.session_edit, 0, 1)
        layout.addWidget(self.session_button, 0, 2)

        self.model_table = QTableWidget(0, 2)
        self.model_table.setHorizontalHeaderLabels(["Prediction CSV", "Model-spec JSON"])
        self.model_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.model_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.model_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.model_table.setSortingEnabled(False)
        self.model_table.setMinimumHeight(170)
        self.model_table.itemSelectionChanged.connect(self._update_pair_buttons)
        layout.addWidget(QLabel("Ordered model pairs"), 1, 0)
        layout.addWidget(self.model_table, 1, 1, 1, 2)

        pair_buttons = QHBoxLayout()
        self.add_pair_button = QPushButton("Add pair...")
        self.remove_pair_button = QPushButton("Remove")
        self.move_up_button = QPushButton("Move up")
        self.move_down_button = QPushButton("Move down")
        self.add_pair_button.clicked.connect(self._add_model_pair)
        self.remove_pair_button.clicked.connect(self._remove_selected_pair)
        self.move_up_button.clicked.connect(lambda: self._move_selected_pair(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected_pair(1))
        for button in (
            self.add_pair_button,
            self.remove_pair_button,
            self.move_up_button,
            self.move_down_button,
        ):
            pair_buttons.addWidget(button)
        pair_buttons.addStretch(1)
        layout.addLayout(pair_buttons, 2, 1, 1, 2)

        self.output_edit = QLineEdit()
        self.output_button = QPushButton("Browse...")
        self.output_button.clicked.connect(self._choose_output)
        layout.addWidget(QLabel("Output directory"), 3, 0)
        layout.addWidget(self.output_edit, 3, 1)
        layout.addWidget(self.output_button, 3, 2)

        self.title_edit = QLineEdit("RHEED reconstruction timeline")
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(25, 100)
        self.quality_spin.setValue(78)
        layout.addWidget(QLabel("Report title"), 4, 0)
        layout.addWidget(self.title_edit, 4, 1, 1, 2)
        layout.addWidget(QLabel("Review WebP quality"), 5, 0)
        layout.addWidget(self.quality_spin, 5, 1)

        self.build_button = QPushButton("Build report and open")
        self.build_button.clicked.connect(self._start_build)
        layout.addWidget(self.build_button, 6, 1)
        layout.setRowStretch(7, 1)
        return tab

    def _create_review_tab(self) -> QWidget:
        tab = QWidget()
        layout = QGridLayout(tab)
        self.report_edit = QLineEdit()
        self.report_button = QPushButton("Browse...")
        self.open_report_button = QPushButton("Open report")
        self.open_manual_button = QPushButton("Open English PDF manual")
        self.report_button.clicked.connect(self._choose_validation_report)
        self.open_report_button.clicked.connect(self._open_report)
        self.open_manual_button.clicked.connect(self._open_manual)
        self.report_edit.textChanged.connect(self._update_action_states)
        layout.addWidget(QLabel("Report HTML"), 0, 0)
        layout.addWidget(self.report_edit, 0, 1)
        layout.addWidget(self.report_button, 0, 2)
        layout.addWidget(self.open_report_button, 0, 3)

        self.annotations_edit = QLineEdit()
        self.annotations_button = QPushButton("Browse...")
        self.annotations_button.clicked.connect(self._choose_annotations)
        layout.addWidget(QLabel("Annotation JSON"), 1, 0)
        layout.addWidget(self.annotations_edit, 1, 1)
        layout.addWidget(self.annotations_button, 1, 2)
        self.validate_button = QPushButton("Validate annotations")
        self.validate_button.clicked.connect(self._start_validation)
        layout.addWidget(self.validate_button, 2, 1)
        layout.addWidget(self.open_manual_button, 2, 3)
        self.validation_result = QLabel("")
        self.validation_result.setWordWrap(True)
        layout.addWidget(self.validation_result, 3, 0, 1, 4)
        layout.setRowStretch(4, 1)
        return tab

    def _dialog_start(self, value: str) -> str:
        path = Path(value.strip()) if value.strip() else Path()
        return str(path if path.is_dir() else path.parent)

    def _choose_session(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Growth Monitor session ZIP",
            self._dialog_start(self.session_edit.text()),
            "ZIP archives (*.zip)",
        )
        if not filename:
            return
        self.session_edit.setText(filename)
        if not self.output_edit.text().strip():
            session = Path(filename)
            self.output_edit.setText(str(session.parent / f"{session.stem}_labeling_report"))

    def _add_model_pair(self) -> None:
        predictions, _ = QFileDialog.getOpenFileName(
            self,
            "Choose prediction CSV",
            self._dialog_start(self.session_edit.text()),
            "CSV files (*.csv)",
        )
        if not predictions:
            return
        model_spec, _ = QFileDialog.getOpenFileName(
            self,
            "Choose matching model-spec JSON",
            str(Path(predictions).parent),
            "JSON files (*.json)",
        )
        if model_spec:
            self.add_model_pair(Path(predictions), Path(model_spec))

    def add_model_pair(self, predictions: Path, model_spec: Path) -> None:
        row = self.model_table.rowCount()
        self.model_table.insertRow(row)
        self.model_table.setItem(row, 0, QTableWidgetItem(str(predictions)))
        self.model_table.setItem(row, 1, QTableWidgetItem(str(model_spec)))
        self.model_table.selectRow(row)
        self._update_pair_buttons()

    def _selected_model_row(self) -> int:
        rows = {index.row() for index in self.model_table.selectedIndexes()}
        return next(iter(rows)) if len(rows) == 1 else -1

    def _remove_selected_pair(self) -> None:
        row = self._selected_model_row()
        if row >= 0:
            self.model_table.removeRow(row)
        self._update_pair_buttons()

    def _move_selected_pair(self, delta: int) -> None:
        row = self._selected_model_row()
        target = row + delta
        if row < 0 or target < 0 or target >= self.model_table.rowCount():
            return
        items = [self.model_table.takeItem(row, column) for column in range(2)]
        self.model_table.removeRow(row)
        self.model_table.insertRow(target)
        for column, item in enumerate(items):
            self.model_table.setItem(target, column, item)
        self.model_table.selectRow(target)
        self._update_pair_buttons()

    def _update_pair_buttons(self) -> None:
        if not hasattr(self, "remove_pair_button"):
            return
        idle = self._process.state() == QProcess.ProcessState.NotRunning
        row = self._selected_model_row()
        self.remove_pair_button.setEnabled(idle and row >= 0)
        self.move_up_button.setEnabled(idle and row > 0)
        self.move_down_button.setEnabled(
            idle and row >= 0 and row < self.model_table.rowCount() - 1
        )

    def _choose_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose a dedicated report output directory",
            self._dialog_start(self.output_edit.text() or self.session_edit.text()),
        )
        if directory:
            self.output_edit.setText(directory)

    def _choose_validation_report(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Choose interactive_report.html",
            self._dialog_start(self.report_edit.text()),
            "HTML files (*.html)",
        )
        if filename:
            self.report_edit.setText(filename)

    def _choose_annotations(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Choose exported annotation JSON",
            self._dialog_start(self.annotations_edit.text()),
            "JSON files (*.json)",
        )
        if filename:
            self.annotations_edit.setText(filename)

    def _model_pairs(self) -> tuple[ModelPair, ...]:
        pairs: list[ModelPair] = []
        for row in range(self.model_table.rowCount()):
            predictions = self.model_table.item(row, 0)
            model_spec = self.model_table.item(row, 1)
            pairs.append(ModelPair(
                Path(predictions.text().strip()) if predictions else Path(),
                Path(model_spec.text().strip()) if model_spec else Path(),
            ))
        return tuple(pairs)

    def _build_request(self) -> BuildRequest:
        return BuildRequest(
            session=Path(self.session_edit.text().strip()),
            model_pairs=self._model_pairs(),
            output_dir=Path(self.output_edit.text().strip()),
            title=self.title_edit.text(),
            review_quality=self.quality_spin.value(),
            overwrite=False,
        )

    def _start_build(self) -> None:
        request = self._build_request()
        errors = validate_build_request(request, repository_root=self._repository_root)
        if errors:
            self._warning(
                "Cannot build report",
                "\n".join(f"- {item}" for item in errors),
            )
            return
        output = _resolved(request.output_dir)
        self._save_settings()
        self._start_process(
            build_cli_arguments(request),
            "Building report...",
            action="build",
            expected_report=output / "interactive_report.html",
        )

    def _start_validation(self) -> None:
        report = Path(self.report_edit.text().strip())
        annotations = Path(self.annotations_edit.text().strip())
        errors: list[str] = []
        if not report.is_file() or report.suffix.lower() != ".html":
            errors.append("Choose an existing report HTML file.")
        if not annotations.is_file() or annotations.suffix.lower() != ".json":
            errors.append("Choose an existing annotation JSON file.")
        if errors:
            self._warning(
                "Cannot validate annotations",
                "\n".join(f"- {item}" for item in errors),
            )
            return
        self._save_settings()
        self.validation_result.clear()
        self._start_process(
            validation_cli_arguments(report, annotations),
            "Validating annotations...",
            action="validate",
        )

    def _start_process(
        self,
        arguments: list[str],
        status: str,
        *,
        action: str,
        expected_report: Path | None = None,
    ) -> None:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._information("Task already running", "Wait for the current task to finish.")
            return
        self._pending_action = action
        self._expected_report = expected_report
        self._stdout_buffer.clear()
        self._stderr_buffer.clear()
        self._completion_handled = False
        self.log.clear()
        command = subprocess.list2cmdline([sys.executable, *arguments])
        self.log.appendPlainText(f"> {command}")
        self.status_label.setText(status)
        self._set_busy(True)
        self._process.start(sys.executable, arguments)

    def _read_stdout(self) -> None:
        self._consume_process_bytes(self._process.readAllStandardOutput(), stdout=True)

    def _read_stderr(self) -> None:
        self._consume_process_bytes(self._process.readAllStandardError(), stdout=False)

    def _consume_process_bytes(self, payload: QByteArray, *, stdout: bool) -> None:
        if not payload:
            return
        text = bytes(payload).decode("utf-8", errors="replace")
        (self._stdout_buffer if stdout else self._stderr_buffer).append(text)
        stripped = text.rstrip()
        if stripped:
            self.log.appendPlainText(stripped)

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart or self._completion_handled:
            return
        self._completion_handled = True
        self._set_busy(False)
        self.status_label.setText("Could not start the worker process")
        self._critical(
            "Worker failed to start",
            "The selected Python environment could not start the offline worker.",
            self._process.errorString(),
        )
        self._clear_pending()

    def _process_finished(
        self,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> None:
        self._read_stdout()
        self._read_stderr()
        if self._completion_handled:
            return
        self._completion_handled = True
        action = self._pending_action
        expected_report = self._expected_report
        stdout = "".join(self._stdout_buffer).strip()
        stderr = "".join(self._stderr_buffer).strip()
        self._set_busy(False)

        if exit_status != QProcess.ExitStatus.NormalExit:
            self.status_label.setText("Worker process crashed")
            self._critical(
                "Offline worker crashed",
                "The child process ended unexpectedly; the live GUI and instruments were untouched.",
                stderr or self._process.errorString(),
            )
            self._clear_pending()
            return
        if exit_code != 0:
            summary = self._last_nonempty_line(stderr or stdout)
            title = "Launcher command error" if exit_code == 2 else "Offline task failed"
            self.status_label.setText(f"Failed (exit code {exit_code})")
            self._critical(
                title,
                summary or "The operation failed. Review the process log for details.",
                stderr or stdout,
            )
            self._clear_pending()
            return

        if action == "build":
            if expected_report is None or not expected_report.is_file():
                self.status_label.setText("Build ended without the expected report")
                self._critical(
                    "Report was not created",
                    "The worker exited successfully, but interactive_report.html is missing.",
                    stdout,
                )
            else:
                self.report_edit.setText(str(expected_report))
                self.status_label.setText(f"Report created: {expected_report}")
                self.tabs.setCurrentWidget(self._review_tab)
                self._save_settings()
                if not self._open_report_with_desktop_controls(expected_report):
                    self.status_label.setText(
                        f"Report created, but Windows did not open it: {expected_report}"
                    )
        elif action == "validate":
            try:
                result = parse_validation_response(stdout)
            except ValueError as exc:
                self.status_label.setText("Validation response was invalid")
                self._critical("Invalid validation response", str(exc), stdout)
            else:
                self.status_label.setText("Annotation JSON is valid for this report")
                self.validation_result.setText(
                    f"Valid - {result.item_count} {result.item_kind}(s) - dataset {result.dataset_id}"
                )
        else:
            self.status_label.setText("Worker returned for an unknown task")
            self._critical("Unknown task result", "The launcher lost the worker task context.", stdout)
        self._clear_pending()

    def _clear_pending(self) -> None:
        self._pending_action = ""
        self._expected_report = None

    def _set_busy(self, busy: bool) -> None:
        for widget in self._mutable_widgets:
            widget.setEnabled(not busy)
        self.progress.setVisible(busy)
        if not busy:
            self._update_action_states()

    def _update_action_states(self) -> None:
        if not hasattr(self, "open_report_button"):
            return
        idle = self._process.state() == QProcess.ProcessState.NotRunning
        self.open_report_button.setEnabled(
            idle and Path(self.report_edit.text().strip()).is_file()
        )
        self._update_pair_buttons()

    @staticmethod
    def _last_nonempty_line(text: str) -> str:
        return next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")

    def _open_report(self) -> None:
        self._open_report_with_desktop_controls(
            Path(self.report_edit.text().strip())
        )

    def _stop_report_service(self) -> None:
        coordinator = self._equalizer_coordinator
        self._equalizer_coordinator = None
        if coordinator is not None:
            try:
                coordinator.close()
            except (AttributeError, RuntimeError):
                pass
        service = self._loopback_service
        self._loopback_service = None
        if service is not None:
            service.stop()

    def _open_report_with_desktop_controls(self, path: Path) -> bool:
        """Open through an authenticated loopback service when ZIP is present.

        Without the source archive, the static report still supports Draft
        review and export.  Equalizer and Complete remain unavailable because
        their exact raw-frame provenance cannot be checked.
        """
        if not path.is_file():
            self._warning("File not found", "Choose an existing report file.")
            return False
        session_path = Path(self.session_edit.text().strip())
        report_payload = None
        report_error: Exception | None = None
        try:
            from .point_events import SCHEMA_VERSION as POINT_EVENT_SCHEMA
            from .report_builder import load_report_payload

            report_payload = load_report_payload(path)
        except (OSError, TypeError, ValueError) as exc:
            report_error = exc
        if (
            report_payload is not None
            and report_payload.get("config", {}).get("annotation_schema")
                != POINT_EVENT_SCHEMA
        ):
            self._information(
                "Legacy report is read-only",
                "This rheed-temporal-segments-v1 report cannot be edited through "
                "the point-event desktop Labeler. It remains available only to "
                "the legacy validator; rebuild the report to create point events.",
            )
            return False
        if not session_path.is_file() or session_path.suffix.lower() != ".zip":
            self._information(
                "Draft review only",
                "Choose the matching source session ZIP in the Build tab before "
                "opening this report if you need Run Equalizer or Complete. "
                "The report will open as static Draft-only review.",
            )
            return self._open_local_file(path, "report")
        if report_error is not None:
            self._warning("Unreadable report", str(report_error))
            return False

        self._stop_report_service()
        coordinator_holder: dict[str, object] = {}
        sidecar_holder: dict[str, object] = {}
        sidecar_lock = threading.Lock()

        def sidecar_store():
            """Lazily verify the large source archive off the Qt thread."""

            with sidecar_lock:
                store = sidecar_holder.get("store")
                if store is None:
                    from .point_events import PointEventSidecarStore

                    store = PointEventSidecarStore(path, session_path)
                    sidecar_holder["store"] = store
                return store

        def persist_revision(command):
            return sidecar_store().apply_revision(command)

        def persist_equalizer_revision(command, measurement):
            return sidecar_store().apply_equalizer_revision(command, measurement)

        def import_draft_document(document):
            return sidecar_store().import_document(document)

        def sidecar_event_ids() -> set[str]:
            return set(sidecar_store().event_ids)

        def durable_event_state():
            store = sidecar_store()
            snapshot = store.atomic_state_snapshot()
            events = snapshot["events"]
            return {
                "ok": True,
                "events": events,
                "revisions": snapshot["revisions"],
                "annotation_set": snapshot["annotation_set"],
                "unfinished_count": sum(
                    event.get("status") != "Complete"
                    and event.get("review", {}).get("disposition") == "active"
                    for event in events
                ),
            }

        def enqueue(request) -> None:
            coordinator = coordinator_holder.get("coordinator")
            if coordinator is None:
                raise RuntimeError("Equalizer controller is not ready")
            coordinator.enqueue_from_http(request)

        try:
            # Import lazily so ordinary report build/validation and headless
            # CLI use never initialize the Equalizer or its Qt graphics code.
            from .offline_equalizer import OfflineEqualizerCoordinator

            service = LoopbackReportService(
                path,
                equalizer_callback=enqueue,
                revision_callback=persist_revision,
                equalizer_revision_callback=persist_equalizer_revision,
                import_callback=import_draft_document,
                state_callback=durable_event_state,
            )
            coordinator = OfflineEqualizerCoordinator(
                report_path=path,
                session_path=session_path,
                service=service,
                additional_event_ids=sidecar_event_ids,
                session_loader=lambda: sidecar_store().ensure_session_verified(),
                parent=self,
            )
            coordinator_holder["coordinator"] = coordinator
            url = service.start()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._critical(
                "Could not start desktop report controls",
                "The report was not opened because the provenance-controlled "
                "desktop service could not start.",
                str(exc),
            )
            return False
        self._loopback_service = service
        self._equalizer_coordinator = coordinator
        try:
            opened = bool(self._url_opener(QUrl(url)))
        except Exception as exc:  # desktop integration boundary
            opened = False
            detail = str(exc)
        else:
            detail = url.split("?", 1)[0]
        if not opened:
            self._stop_report_service()
            self._critical(
                "Could not open report",
                "Windows did not open the report through the desktop labeler.",
                detail,
            )
            return False
        self.status_label.setText(
            "Opened report with desktop Equalizer controls on 127.0.0.1"
        )
        return True

    def _open_manual(self) -> None:
        self._open_local_file(self._manual_path, "English PDF manual")

    def _open_local_file(self, path: Path, description: str) -> bool:
        if not path.is_file():
            self._warning("File not found", f"Choose an existing {description} file.")
            return False
        try:
            opened = bool(self._url_opener(QUrl.fromLocalFile(str(path.resolve()))))
        except Exception as exc:  # external desktop integration boundary
            opened = False
            detail = str(exc)
        else:
            detail = str(path.resolve())
        if not opened:
            self._critical(
                "Could not open file",
                f"Windows did not open the selected {description}.",
                detail,
            )
            return False
        self.status_label.setText(f"Opened {description}: {path.resolve()}")
        return True

    def _warning(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def _information(self, title: str, message: str) -> None:
        QMessageBox.information(self, title, message)

    def _critical(self, title: str, message: str, details: str = "") -> None:
        box = QMessageBox(QMessageBox.Icon.Critical, title, message, parent=self)
        if details:
            box.setDetailedText(details)
        box.exec()

    def _restore_settings(self) -> None:
        try:
            version = int(self._settings.value("settings/version", 0))
        except (TypeError, ValueError):
            return
        if version != SETTINGS_VERSION:
            return
        self.session_edit.setText(str(self._settings.value("build/session", "")))
        self.output_edit.setText(str(self._settings.value("build/output", "")))
        self.report_edit.setText(str(self._settings.value("review/report", "")))
        self.annotations_edit.setText(str(self._settings.value("review/annotations", "")))
        self.title_edit.setText(str(self._settings.value("build/title", self.title_edit.text())))
        try:
            quality = int(self._settings.value("build/review_quality", 78))
        except (TypeError, ValueError):
            quality = 78
        self.quality_spin.setValue(max(25, min(100, quality)))
        try:
            pairs = json.loads(str(self._settings.value("build/model_pairs", "[]")))
        except (json.JSONDecodeError, TypeError):
            pairs = []
        if isinstance(pairs, list):
            for item in pairs:
                if isinstance(item, list) and len(item) == 2:
                    self.add_model_pair(Path(str(item[0])), Path(str(item[1])))
        geometry = self._settings.value("window/geometry")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)

    def _save_settings(self) -> None:
        self._settings.setValue("settings/version", SETTINGS_VERSION)
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue("build/session", self.session_edit.text())
        self._settings.setValue("build/output", self.output_edit.text())
        self._settings.setValue("review/report", self.report_edit.text())
        self._settings.setValue("review/annotations", self.annotations_edit.text())
        self._settings.setValue("build/title", self.title_edit.text())
        self._settings.setValue("build/review_quality", self.quality_spin.value())
        pairs = [[str(pair.predictions), str(pair.model_spec)] for pair in self._model_pairs()]
        self._settings.setValue("build/model_pairs", json.dumps(pairs))
        self._settings.sync()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._information(
                "Offline task still running",
                "Wait for the current build or validation to finish before closing. "
                "The launcher will not force-kill a worker because that could leave partial staging files.",
            )
            event.ignore()
            return
        self._stop_report_service()
        self._save_settings()
        super().closeEvent(event)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName("AI4MBE")
    app.setApplicationName("RHEED Post-processing and Temporal Labeling")
    window = LabelingDesktopLauncher()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
