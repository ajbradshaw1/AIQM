"""Visible-tab contract for both chamber Growth Monitor profiles."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from drivers.config import CHALCOGENIDE_MBE, OXIDE_MBE  # noqa: E402
from gui.growth_monitor import GrowthMonitor  # noqa: E402
from gui.live_equalizer_tab import LiveEqualizerTab  # noqa: E402


_app = QApplication.instance() or QApplication(sys.argv)


class GrowthMonitorVisibleTabTests(unittest.TestCase):
    """Live Equalizer stays compatible in code but absent from the UI."""

    def _monitor(self, config) -> GrowthMonitor:
        # Loading simulator bases is unrelated to the tab-visibility contract
        # and makes this focused test needlessly dependent on image assets.
        loader = patch.object(LiveEqualizerTab, "_load_basis")
        loader.start()
        self.addCleanup(loader.stop)
        monitor = GrowthMonitor(config=config)
        self.addCleanup(monitor.deleteLater)
        return monitor

    def test_both_chambers_show_events_without_live_equalizer_tab(self) -> None:
        for config in (CHALCOGENIDE_MBE, OXIDE_MBE):
            with self.subTest(chamber=config.chamber_id):
                monitor = self._monitor(config)
                titles = [
                    monitor._tabs.tabText(index)
                    for index in range(monitor._tabs.count())
                ]

                self.assertEqual(
                    titles,
                    ["Monitor", "Direct-read", "Events", "Scrubber", "Session"],
                )
                self.assertGreaterEqual(
                    monitor._tabs.indexOf(monitor.events_tab), 0,
                )

    def test_hidden_equalizer_object_is_not_a_tab(self) -> None:
        monitor = self._monitor(CHALCOGENIDE_MBE)

        self.assertIsInstance(monitor.live_equalizer_tab, LiveEqualizerTab)
        self.assertEqual(
            monitor._tabs.indexOf(monitor.live_equalizer_tab), -1,
        )
        self.assertTrue(monitor.live_equalizer_tab.isHidden())

        monitor.show()
        _app.processEvents()
        self.assertTrue(monitor.live_equalizer_tab.isHidden())

        monitor._on_blind_labeling_mode_changed(True)
        monitor._on_blind_labeling_mode_changed(False)
        self.assertEqual(
            monitor._tabs.indexOf(monitor.live_equalizer_tab), -1,
        )
        self.assertTrue(monitor.live_equalizer_tab.isHidden())


if __name__ == "__main__":
    unittest.main()
