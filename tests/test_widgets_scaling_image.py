"""Unit tests for the shared ``ScalingImageLabel`` widget.

Extracted Jul 13 2026 from two near-identical private classes:
  - ``gui.events_tab._ScalingImageLabel`` (auto-capture buffer pane)
  - ``gui.scrubber_tab._ScrubberImageLabel`` (full-timeline playback pane)

These tests lock the extracted class's public contract so the two
consumers (and any future ones) can rely on the same aspect-ratio-
preserving resize + clear-on-None-pixmap behavior.

Run:
    python -m pytest -q tests/test_widgets_scaling_image.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# QApplication must exist before any QWidget is constructed. Same
# module-scope pattern used by test_live_equalizer_tab.py + test_scrubber_tab.py.
from PyQt6.QtWidgets import QApplication  # noqa: E402
_app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QImage, QPixmap  # noqa: E402

from gui.widgets import ScalingImageLabel  # noqa: E402


def _make_pixmap(width: int = 64, height: int = 48) -> QPixmap:
    """Build a small solid-color pixmap so the label has something to scale."""
    img = QImage(width, height, QImage.Format.Format_RGB888)
    img.fill(Qt.GlobalColor.gray)
    return QPixmap.fromImage(img)


class ScalingImageLabelTests(unittest.TestCase):
    """Public contract of the extracted widget."""

    def test_setOriginalPixmap_scales_to_widget_size(self):
        # Setting a pixmap must land a scaled QPixmap on the label so
        # QLabel::pixmap() returns something non-null. The scaling itself
        # is Qt's — we just assert the plumbing lands.
        label = ScalingImageLabel()
        # Force a size larger than default so scaling produces a visible change.
        label.resize(400, 300)
        label.setOriginalPixmap(_make_pixmap())
        self.assertFalse(label.pixmap().isNull())

    def test_clearImage_removes_pixmap(self):
        label = ScalingImageLabel()
        label.setOriginalPixmap(_make_pixmap())
        # Confirm the pre-clear state has a pixmap so the post-clear
        # assertion is meaningful.
        self.assertFalse(label.pixmap().isNull())
        label.clearImage()
        # QLabel.clear() clears text AND pixmap; pixmap() returns null.
        self.assertTrue(label.pixmap().isNull())

    def test_none_pixmap_treated_as_clear(self):
        # setOriginalPixmap(None) should be indistinguishable from a
        # clearImage() call — same public effect. Locks the "convenience
        # unary" path documented in the class docstring.
        label = ScalingImageLabel()
        label.setOriginalPixmap(_make_pixmap())
        self.assertFalse(label.pixmap().isNull())
        label.setOriginalPixmap(None)
        self.assertTrue(label.pixmap().isNull())

    def test_null_pixmap_treated_as_clear(self):
        # An empty QPixmap() (null but not None) should also be treated
        # as clear. Guards against a subtle race where a producer emits
        # a null pixmap to signal "no image" — same intent as None.
        label = ScalingImageLabel()
        label.setOriginalPixmap(_make_pixmap())
        label.setOriginalPixmap(QPixmap())
        self.assertTrue(label.pixmap().isNull())

    def test_minimum_size_default_matches_events_tab_history(self):
        # Default 320×240 preserves the events-tab default so any caller
        # constructing with no args gets the smaller pane (the historical
        # events-tab behavior).
        label = ScalingImageLabel()
        self.assertEqual(label.minimumWidth(), 320)
        self.assertEqual(label.minimumHeight(), 240)

    def test_minimum_size_param_overrides_default(self):
        # Scrubber uses (480, 360). Test with a non-standard size to
        # confirm arbitrary values pass through, not just the two
        # legacy sizes.
        label = ScalingImageLabel(minimum_size=(600, 450))
        self.assertEqual(label.minimumWidth(), 600)
        self.assertEqual(label.minimumHeight(), 450)


if __name__ == "__main__":
    unittest.main(verbosity=2)
