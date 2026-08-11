"""Launch the OMBE Growth Monitor GUI."""
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# WINDOWS DLL LOAD ORDER — import torch BEFORE PyQt6.
#
# On Windows, PyQt6 and torch both bundle their own Intel OpenMP runtime
# (libiomp5md.dll). "First-loaded wins" semantics on Windows mean the
# second package to try to load OpenMP gets the version the first
# package loaded — and if the versions are incompatible, its DLL
# initialization fails with WinError 1114 on c10.dll (first surfaced
# 2026-07-06 on Bulbasaur: Python 3.12 + torch 2.12+cpu + PyQt6).
#
# Torch has stricter version requirements on its OpenMP than PyQt6
# does, so we load torch first — PyQt6 then happily reuses torch's
# OpenMP. Reversing the order does NOT work.
#
# Best-effort: if torch isn't installed, ClassifierBridge will still
# surface a clean "Failed to load classifier" error and the rest of
# the app runs unchanged (same failure semantics covered by
# tests/test_classifier_worker.py::test_startup_emits_error_on_bridge_failure).
# On macOS/Linux the two-level dynamic linker doesn't have this
# issue — no-op there.
#
# KMP_DUPLICATE_LIB_OK=TRUE is an alternative Intel-sanctioned
# escape-hatch (allow duplicate OpenMP), but it's unsafe (can cause
# subtle crashes and hangs). Reordering the imports is the clean fix.
# ---------------------------------------------------------------------------
try:
    import torch  # noqa: F401 — imported for its DLL side effects
except ImportError:
    pass

# Fix Qt platform plugin discovery for Anaconda environments where the compiled-in
# prefix path doesn't match the actual PyQt6 plugin location.
import PyQt6
_qt_plugins = Path(PyQt6.__file__).parent / "Qt6" / "plugins"
if _qt_plugins.exists() and "QT_QPA_PLATFORM_PLUGIN_PATH" not in os.environ:
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(_qt_plugins / "platforms")

from PyQt6.QtWidgets import QApplication
from gui.growth_app import GrowthApp


def main():
    app = QApplication(sys.argv)
    window = GrowthApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
