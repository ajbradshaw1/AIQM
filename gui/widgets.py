"""
Reusable widgets for the hardware control GUI.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QGridLayout, QLabel, QPushButton,
    QGroupBox, QDoubleSpinBox, QSizePolicy, QWidget,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap

from gui.state import PowerSupplyState


class ValueDisplay(QFrame):
    """Widget for displaying a labeled value."""

    def __init__(self, label: str, unit: str = "", decimals: int = 3):
        super().__init__()
        self.unit = unit
        self.decimals = decimals

        self.setFrameStyle(QFrame.Shape.Box | QFrame.Shadow.Raised)
        self.setLineWidth(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)

        self.label = QLabel(label)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.value = QLabel("---")
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFont("Monospace", 24, QFont.Weight.Bold)
        self.value.setFont(font)

        layout.addWidget(self.label)
        layout.addWidget(self.value)

    def set_value(self, value: float):
        """Update the displayed value."""
        self.value.setText(f"{value:.{self.decimals}f} {self.unit}")

    def set_color(self, color: str):
        """Set the value text color."""
        self.value.setStyleSheet(f"color: {color};")


class ControlPanel(QGroupBox):
    """Panel for controlling voltage and current."""

    voltage_changed = pyqtSignal(float)
    current_changed = pyqtSignal(float)
    output_toggled = pyqtSignal(bool)

    def __init__(self, max_voltage: float = 24.0, max_current: float = 1.0):
        super().__init__("Controls")

        layout = QGridLayout(self)

        # Voltage control
        layout.addWidget(QLabel("Voltage (V):"), 0, 0)
        self.voltage_spin = QDoubleSpinBox()
        self.voltage_spin.setRange(0, max_voltage)
        self.voltage_spin.setDecimals(2)
        self.voltage_spin.setSingleStep(0.5)
        layout.addWidget(self.voltage_spin, 0, 1)

        self.voltage_btn = QPushButton("Set")
        self.voltage_btn.clicked.connect(self._on_voltage_set)
        layout.addWidget(self.voltage_btn, 0, 2)

        # Current control
        layout.addWidget(QLabel("Current (A):"), 1, 0)
        self.current_spin = QDoubleSpinBox()
        self.current_spin.setRange(0, max_current)
        self.current_spin.setDecimals(3)
        self.current_spin.setSingleStep(0.1)
        layout.addWidget(self.current_spin, 1, 1)

        self.current_btn = QPushButton("Set")
        self.current_btn.clicked.connect(self._on_current_set)
        layout.addWidget(self.current_btn, 1, 2)

        # Output toggle
        self.output_btn = QPushButton("OUTPUT OFF")
        self.output_btn.setCheckable(True)
        self.output_btn.setMinimumHeight(50)
        self.output_btn.clicked.connect(self._on_output_toggle)
        self._update_output_button(False)
        layout.addWidget(self.output_btn, 2, 0, 1, 3)

        # Prevents the poll loop from fighting the button for 2 s after a click
        self._output_pending = False
        self._confirmed_output = False
        self._pending_timer = QTimer(self)
        self._pending_timer.setSingleShot(True)
        self._pending_timer.setInterval(2000)
        self._pending_timer.timeout.connect(self._clear_output_pending)

    def _on_voltage_set(self):
        self.voltage_changed.emit(self.voltage_spin.value())

    def _on_current_set(self):
        self.current_changed.emit(self.current_spin.value())

    def _on_output_toggle(self):
        enabled = self.output_btn.isChecked()
        self._output_pending = True
        self._pending_timer.start()
        # A click is only a request. Keep the visible/checkable state at the
        # last hardware-confirmed value until the worker's forced readback.
        self.output_btn.setChecked(self._confirmed_output)
        self.output_btn.setEnabled(False)
        self.output_btn.setText(
            "REQUESTING OUTPUT ON..." if enabled else "REQUESTING OUTPUT OFF..."
        )
        self.output_toggled.emit(enabled)

    def _clear_output_pending(self):
        # Timeout is not success. Revert to the last confirmed value.
        self.complete_output_request(False, self._confirmed_output)

    def complete_output_request(self, confirmed: bool, actual: bool) -> None:
        self._pending_timer.stop()
        self._output_pending = False
        self.output_btn.setEnabled(True)
        if confirmed:
            self._confirmed_output = bool(actual)
        self.output_btn.setChecked(self._confirmed_output)
        self._update_output_button(self._confirmed_output)

    def _update_output_button(self, enabled: bool):
        if enabled:
            self.output_btn.setText("OUTPUT ON")
            self.output_btn.setStyleSheet(
                "background-color: #4CAF50; color: white; font-weight: bold;"
            )
        else:
            self.output_btn.setText("OUTPUT OFF")
            self.output_btn.setStyleSheet("background-color: #666; color: white;")

    def mark_output_unknown(self) -> None:
        """Do not present an old OUTP? value as current hardware state."""
        self._pending_timer.stop()
        self._output_pending = False
        self._confirmed_output = False
        self.output_btn.setChecked(False)
        self.output_btn.setEnabled(False)
        self.output_btn.setText("OUTPUT UNKNOWN / STALE")
        self.output_btn.setStyleSheet(
            "background-color: #FF9800; color: black; font-weight: bold;"
        )

    def update_state(self, state: PowerSupplyState):
        """Update controls to reflect current state."""
        if self._output_pending:
            return
        self.output_btn.setEnabled(True)
        self._confirmed_output = bool(state.output_enabled)
        self.output_btn.setChecked(state.output_enabled)
        self._update_output_button(state.output_enabled)


class ProtectionPanel(QGroupBox):
    """Panel for OVP/OCP settings."""

    ovp_changed = pyqtSignal(float)
    ocp_changed = pyqtSignal(float)

    def __init__(self):
        super().__init__("Protection Settings")
        self._initialized = False

        layout = QGridLayout(self)

        # OVP
        layout.addWidget(QLabel("OVP (V):"), 0, 0)
        self.ovp_spin = QDoubleSpinBox()
        self.ovp_spin.setRange(0, 82)
        self.ovp_spin.setDecimals(1)
        layout.addWidget(self.ovp_spin, 0, 1)

        self.ovp_btn = QPushButton("Set")
        self.ovp_btn.clicked.connect(
            lambda: self.ovp_changed.emit(self.ovp_spin.value())
        )
        layout.addWidget(self.ovp_btn, 0, 2)

        # OCP
        layout.addWidget(QLabel("OCP (A):"), 1, 0)
        self.ocp_spin = QDoubleSpinBox()
        self.ocp_spin.setRange(0, 5.2)
        self.ocp_spin.setDecimals(2)
        layout.addWidget(self.ocp_spin, 1, 1)

        self.ocp_btn = QPushButton("Set")
        self.ocp_btn.clicked.connect(
            lambda: self.ocp_changed.emit(self.ocp_spin.value())
        )
        layout.addWidget(self.ocp_btn, 1, 2)

    def update_state(self, state: PowerSupplyState):
        """Update displayed protection values only on initial load or if not focused."""
        if not self._initialized:
            self.ovp_spin.setValue(state.ovp_limit)
            self.ocp_spin.setValue(state.ocp_limit)
            self._initialized = True
        else:
            if not self.ovp_spin.hasFocus():
                self.ovp_spin.setValue(state.ovp_limit)
            if not self.ocp_spin.hasFocus():
                self.ocp_spin.setValue(state.ocp_limit)

    def reset_initialized(self):
        """Reset initialization flag (call on disconnect)."""
        self._initialized = False


class ScalingImageLabel(QLabel):
    """QLabel that keeps an aspect-ratio-scaled view of a source pixmap.

    Extracted Jul 13 2026 from two near-identical private classes:
      - ``gui.events_tab._ScalingImageLabel`` (minimum size 320×240,
        auto-capture buffer preview pane)
      - ``gui.scrubber_tab._ScrubberImageLabel`` (minimum size 480×360,
        full-timeline playback pane)

    The two originals differed only in their initial ``setMinimumSize``
    call. Consumers now pass their prior minimum size via the
    ``minimum_size`` parameter (default 320×240 preserves the smaller
    events-tab default for any incidental callers).

    Behavior contract (preserved verbatim from both originals):
      - ``setOriginalPixmap`` caches the full-resolution pixmap so each
        resize re-scales from source (not from an already-shrunk pixmap
        — compounding lossy rescales would blur the image over time).
      - ``resizeEvent`` re-runs the aspect-ratio-preserving scale so a
        QSplitter drag re-fits the label content.
      - ``clearImage`` drops the cached pixmap and clears the label.
      - ``setOriginalPixmap(None)`` (or a null pixmap) is treated the
        same as ``clearImage`` — makes the "no image yet" and "clear
        the image" call sites identical.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        minimum_size: tuple[int, int] = (320, 240),
    ) -> None:
        super().__init__(parent)
        self._original: Optional[QPixmap] = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            "background-color: #000; border: 1px solid #555;"
        )
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self.setMinimumSize(minimum_size[0], minimum_size[1])

    def setOriginalPixmap(self, pixmap: Optional[QPixmap]) -> None:
        if pixmap is None or pixmap.isNull():
            self._original = None
            self.clear()
            return
        self._original = pixmap
        self._rescale()

    def clearImage(self) -> None:
        self._original = None
        self.clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._original is None:
            return
        scaled = self._original.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)
