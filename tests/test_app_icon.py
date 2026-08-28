"""Checks for the shared GUI and Windows shortcut icon asset."""

from __future__ import annotations

import os

from PIL import Image


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from gui.app_icon import APP_ICON_PATH, install_application_icon  # noqa: E402


def test_windows_icon_contains_all_supported_shell_sizes() -> None:
    with Image.open(APP_ICON_PATH) as icon:
        assert icon.format == "ICO"
        assert icon.ico.sizes() == {
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        }


def test_shared_icon_loads_into_qt_application() -> None:
    app = QApplication.instance() or QApplication(["test-app-icon"])

    icon = install_application_icon(app)

    assert not icon.isNull()
    assert not app.windowIcon().isNull()
