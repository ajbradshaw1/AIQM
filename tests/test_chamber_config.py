#!/usr/bin/env python3
"""Headless tests for the chamber config system (drivers/config.py).

No GUI or hardware required. Verifies get_active_config() env-var dispatch,
field values for both chamber presets, and cell_display list structure.
"""
from __future__ import annotations
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from drivers.config import (
    OXIDE_MBE, CHALCOGENIDE_MBE, SYSTEMS, get_active_config,
)


class TestGetActiveConfig(unittest.TestCase):

    def _with_chamber(self, value):
        """Context manager: set AIQM_CHAMBER env var and restore after."""
        import contextlib
        @contextlib.contextmanager
        def _ctx():
            old = os.environ.get("AIQM_CHAMBER")
            os.environ["AIQM_CHAMBER"] = value
            try:
                yield
            finally:
                if old is None:
                    os.environ.pop("AIQM_CHAMBER", None)
                else:
                    os.environ["AIQM_CHAMBER"] = old
        return _ctx()

    def test_default_is_ombe(self):
        os.environ.pop("AIQM_CHAMBER", None)
        cfg = get_active_config()
        self.assertEqual(cfg.chamber_id, "ombe")

    def test_ombe_explicit(self):
        with self._with_chamber("ombe"):
            cfg = get_active_config()
        self.assertEqual(cfg.chamber_id, "ombe")

    def test_chmbe_explicit(self):
        with self._with_chamber("chmbe"):
            cfg = get_active_config()
        self.assertEqual(cfg.chamber_id, "chmbe")

    def test_legacy_oxide_alias(self):
        with self._with_chamber("oxide"):
            cfg = get_active_config()
        self.assertEqual(cfg.chamber_id, "ombe")

    def test_legacy_chalcogenide_alias(self):
        with self._with_chamber("chalcogenide"):
            cfg = get_active_config()
        self.assertEqual(cfg.chamber_id, "chmbe")

    def test_unknown_falls_back_to_ombe(self):
        with self._with_chamber("nonexistent"):
            cfg = get_active_config()
        self.assertEqual(cfg.chamber_id, "ombe")

    def test_case_insensitive(self):
        with self._with_chamber("CHMBE"):
            cfg = get_active_config()
        self.assertEqual(cfg.chamber_id, "chmbe")


class TestOmbConfig(unittest.TestCase):

    def test_live_reader_mode_defaults(self):
        self.assertEqual(OXIDE_MBE.camera_mode_default, "vimba")
        self.assertEqual(OXIDE_MBE.pyrometer_mode_default, "modbus")

    def test_mistral_mode_default(self):
        # Switched from "screengrab" to "ads" Jul 27 2026 after
        # direct pyads to Bulbasaur PLC validated. Fallback modes
        # still selectable via the sidebar dropdown.
        self.assertEqual(OXIDE_MBE.mistral_mode_default, "ads")

    def test_evap_mode_default(self):
        self.assertEqual(OXIDE_MBE.evap_mode_default, "elog")

    def test_ombe_evap_log_dir_is_explicit(self):
        self.assertTrue(OXIDE_MBE.evap_log_dir)
        self.assertIn("1.2.0.51", OXIDE_MBE.evap_log_dir)

    def test_five_cells(self):
        self.assertEqual(len(OXIDE_MBE.cell_display), 5)

    def test_all_ombe_cells_have_state_field(self):
        for cell in OXIDE_MBE.cell_display:
            self.assertIsNotNone(cell["state_field"],
                                 f"O-MBE cell {cell['label']} missing state_field")

    def test_cell_labels_not_empty(self):
        for cell in OXIDE_MBE.cell_display:
            self.assertTrue(cell["label"])

    def test_ombe_ads_config(self):
        # Bulbasaur PLC per Jul 27 2026 lab validation.
        self.assertEqual(OXIDE_MBE.ads_netid, "10.0.42.111.1.1")
        self.assertEqual(OXIDE_MBE.ads_port_main, 851)
        self.assertEqual(OXIDE_MBE.ads_port_pid, 852)
        self.assertEqual(OXIDE_MBE.ads_cell_count, 6)

    def test_ombe_ads_display_not_confirmed(self):
        # cell_display uses material labels (Sr, Eu, Er, HTEC2, Y);
        # ADS Cell{N} → material mapping is not yet confirmed by growers,
        # so widgets should NOT be populated from ADS. Data still flows
        # to CSV via update_mistral_state → sensor_log.
        self.assertFalse(OXIDE_MBE.ads_display_confirmed)


class TestChMbeConfig(unittest.TestCase):

    def test_live_reader_mode_defaults(self):
        self.assertEqual(CHALCOGENIDE_MBE.camera_mode_default, "vimba")
        self.assertEqual(CHALCOGENIDE_MBE.pyrometer_mode_default, "modbus")

    def test_mistral_mode_default(self):
        self.assertEqual(CHALCOGENIDE_MBE.mistral_mode_default, "ads")

    def test_evap_mode_default(self):
        self.assertEqual(CHALCOGENIDE_MBE.evap_mode_default, "elog")

    def test_seven_cells(self):
        self.assertEqual(len(CHALCOGENIDE_MBE.cell_display), 7)

    def test_chmbe_cells_have_no_state_field(self):
        for cell in CHALCOGENIDE_MBE.cell_display:
            self.assertIsNone(cell["state_field"],
                              f"Ch-MBE cell {cell['label']} should have state_field=None")

    def test_evap_log_dir_set(self):
        self.assertTrue(CHALCOGENIDE_MBE.evap_log_dir)
        self.assertIn("1.2.0.48", CHALCOGENIDE_MBE.evap_log_dir)

    def test_cell_labels_not_empty(self):
        for cell in CHALCOGENIDE_MBE.cell_display:
            self.assertTrue(cell["label"])

    def test_chmbe_ads_config(self):
        # Ch-MBE PLC per Jul 22 2026 discovery (Task #191).
        self.assertEqual(CHALCOGENIDE_MBE.ads_netid, "10.0.42.112.1.1")
        self.assertEqual(CHALCOGENIDE_MBE.ads_port_main, 851)
        self.assertEqual(CHALCOGENIDE_MBE.ads_port_pid, 852)
        self.assertEqual(CHALCOGENIDE_MBE.ads_cell_count, 7)

    def test_chmbe_ads_display_confirmed(self):
        # cell_display uses numeric labels (Cell1..Cell7) aligned with
        # ADS Cell{N}, so widget-populate-from-ADS is safe.
        self.assertTrue(CHALCOGENIDE_MBE.ads_display_confirmed)


class TestSystems(unittest.TestCase):

    def test_all_keys_present(self):
        for key in ("ombe", "oxide", "chmbe", "chalcogenide"):
            self.assertIn(key, SYSTEMS)

    def test_no_mutation_between_instances(self):
        # cell_display uses field(default_factory=...) so instances are independent
        OXIDE_MBE.cell_display.append({"label": "TEST", "state_field": None})
        # The default_factory config should not be affected (SYSTEMS uses pre-built instances)
        self.assertNotIn(
            {"label": "TEST", "state_field": None},
            CHALCOGENIDE_MBE.cell_display,
        )
        OXIDE_MBE.cell_display.pop()  # restore


if __name__ == "__main__":
    print("Running chamber config tests...\n")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().discover(
        start_dir=str(Path(__file__).parent),
        pattern="test_chamber_config.py",
    ))
    sys.exit(0 if result.wasSuccessful() else 1)
