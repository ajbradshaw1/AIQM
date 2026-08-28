"""
Action log viewer tab widget.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog,
    QMessageBox,
)
from PyQt6.QtCore import Qt

from gui.state import ActionLogEntry
from gui.heater_control.action_logger import ActionLogger, MAX_ENTRIES


class ActionLogTab(QWidget):
    """Timestamped table of user actions and instrument readings."""

    COLUMNS = [
        "Timestamp", "Category", "Action", "Details",
        "PSU V", "PSU I", "PSU P", "Output", "Temp (C)",
    ]

    def __init__(self, action_logger: ActionLogger, parent=None):
        super().__init__(parent)
        self.action_logger = action_logger
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Table
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        # Bottom bar
        bottom = QHBoxLayout()

        self.count_label = QLabel("0 entries")
        bottom.addWidget(self.count_label)

        bottom.addStretch()

        self.export_btn = QPushButton("Export CSV")
        self.export_btn.clicked.connect(self._on_export)
        bottom.addWidget(self.export_btn)

        self.clear_btn = QPushButton("Clear Log")
        self.clear_btn.clicked.connect(self._on_clear)
        bottom.addWidget(self.clear_btn)

        layout.addLayout(bottom)

    def _connect_signals(self):
        self.action_logger.entry_added.connect(self._on_entry_added)
        self.action_logger.cleared.connect(self._on_cleared)

    def _on_entry_added(self, entry: ActionLogEntry):
        """Append a new row to the table."""
        if self.table.rowCount() >= MAX_ENTRIES:
            self.table.removeRow(0)
        row = self.table.rowCount()
        self.table.insertRow(row)

        values = [
            entry.timestamp.strftime("%H:%M:%S.%f")[:-3],
            entry.category,
            entry.action,
            entry.details,
            f"{entry.psu_voltage:.3f}" if entry.psu_voltage is not None else "",
            f"{entry.psu_current:.3f}" if entry.psu_current is not None else "",
            f"{entry.psu_power:.3f}" if entry.psu_power is not None else "",
            "ON" if entry.psu_output else ("OFF" if entry.psu_output is not None else ""),
            f"{entry.temperature:.2f}" if entry.temperature is not None else "",
        ]

        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, col, item)

        self.table.scrollToBottom()
        self.count_label.setText(f"{self.action_logger.count()} entries")

    def _on_cleared(self):
        """Clear the table."""
        self.table.setRowCount(0)
        self.count_label.setText("0 entries")

    def _on_export(self):
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Export Action Log", "action_log.csv", "CSV Files (*.csv)"
        )
        if filepath:
            try:
                self.action_logger.export_csv(filepath)
                QMessageBox.information(
                    self, "Export Complete", f"Exported {self.action_logger.count()} entries to:\n{filepath}"
                )
            except Exception as e:
                QMessageBox.critical(self, "Export Error", str(e))

    def _on_clear(self):
        reply = QMessageBox.question(
            self, "Clear Log",
            f"Clear all {self.action_logger.count()} log entries?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.action_logger.clear()
