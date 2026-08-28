"""Shared application icon for GUI windows and Windows launch surfaces."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication


APP_ICON_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "ai4mbe_app_icon.ico"
)


def install_application_icon(app: QApplication) -> QIcon:
    """Install the bundled icon and return it for verification or reuse."""
    icon = QIcon(str(APP_ICON_PATH))
    if not icon.isNull():
        app.setWindowIcon(icon)
    return icon
