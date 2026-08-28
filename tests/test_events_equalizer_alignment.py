"""Regression guard: visual fitting is absent from point-event labeling."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

from gui.events_tab import EventsTab  # noqa: E402


class PointEventLabelingIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tab = EventsTab()

    def tearDown(self) -> None:
        self.tab.deleteLater()

    def test_no_visual_fitting_control_or_popup_exists(self) -> None:
        for attribute in (
            "_equalizer_button", "_equalizer_window", "_on_open_equalizer",
            "_save_retrospective_equalizer_label",
            "retrospective_calibration_accept_requested",
            "retrospective_calibration_invalidation_requested",
            "retrospective_calibration_resolve_requested",
        ):
            self.assertFalse(hasattr(self.tab, attribute), attribute)

    def test_completion_ui_only_mentions_human_labels_and_anchor(self) -> None:
        help_text = self.tab._completion_help.text().lower()
        source = inspect.getsource(EventsTab._complete_event).lower()
        self.assertNotIn("equalizer", help_text)
        self.assertNotIn("equalizer", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
