#!/usr/bin/env python3
"""Tests for scripts/precheck_mistral_ads.py.

Covers the cell-summary invariant logic, the verdict decision tree, the
evidence bundle writer, and CLI argparse. Does NOT touch pyads / real
TwinCAT — that's on-Bulbasaur territory.

Runs on Mac dev env with no lab dependencies:
    python -m pytest -q tests/test_precheck_mistral_ads.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.precheck_mistral_ads import (  # noqa: E402
    ADS_CELL_SUFFIXES,
    _extract_cell_number,
    build_verdict,
    main,
    phase_cell_summary,
    write_evidence_bundle,
)


def _make_populated_cell(i: int) -> dict:
    """Return a dict with all 8 ADS suffixes populated for cell i."""
    return {
        f"cell{i}_T": 100.0 + i,
        f"cell{i}_T_set": 200.0 + i,
        f"cell{i}_active_setpoint": 200.0 + i,
        f"cell{i}_V": 0.1 * i,
        f"cell{i}_I": 0.01 * i,
        f"cell{i}_prog_V": 0.2 * i,
        f"cell{i}_prog_A": 0.02 * i,
        f"cell{i}_power": 0.005 * i,
    }


def _make_read_dict(populated_cells: list[int]) -> dict:
    """Compose a fake MistralAdsClient.read() dict with the given cells populated.

    All other cells (up to 7) will have their keys set to None to mimic the
    real driver's shape (it always emits cell1..cell7 keys, with values that
    may be None if the underlying ADS read failed).
    """
    d: dict = {}
    for i in range(1, 8):
        if i in populated_cells:
            d.update(_make_populated_cell(i))
        else:
            for sfx in ADS_CELL_SUFFIXES:
                d[f"cell{i}_{sfx}"] = None
    return d


# ---------------------------------------------------------------------------
# _extract_cell_number helper
# ---------------------------------------------------------------------------

class ExtractCellNumberTests(unittest.TestCase):

    def test_standard_key(self):
        self.assertEqual(_extract_cell_number("cell3_T"), 3)
        self.assertEqual(_extract_cell_number("cell7_power"), 7)
        self.assertEqual(_extract_cell_number("cell1_active_setpoint"), 1)

    def test_non_cell_key(self):
        self.assertIsNone(_extract_cell_number("service_mode"))
        self.assertIsNone(_extract_cell_number("turbo1_rpm"))
        self.assertIsNone(_extract_cell_number("ion_gauge_a_P"))

    def test_malformed_cell_key(self):
        # Not a number after "cell"
        self.assertIsNone(_extract_cell_number("cellFoo_T"))
        # No underscore separator
        self.assertIsNone(_extract_cell_number("cell3T"))


# ---------------------------------------------------------------------------
# phase_cell_summary logic
# ---------------------------------------------------------------------------

class CellSummaryTests(unittest.TestCase):

    def test_ombe_happy_path_six_cells(self):
        """O-MBE: 6 populated cells 1-6, cell7 all None, no invariant violation."""
        read_dict = _make_read_dict([1, 2, 3, 4, 5, 6])
        s = phase_cell_summary(read_dict, cell_count=6)
        self.assertEqual(s["populated_cells"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(s["unpopulated_cells"], [])
        self.assertEqual(s["unexpected_cell_keys"], [])
        # cell7 not counted in per_cell dict (only 1..cell_count)
        self.assertNotIn(7, s["per_cell"])

    def test_chmbe_happy_path_seven_cells(self):
        """Ch-MBE: all 7 cells populated, no invariant violation."""
        read_dict = _make_read_dict([1, 2, 3, 4, 5, 6, 7])
        s = phase_cell_summary(read_dict, cell_count=7)
        self.assertEqual(s["populated_cells"], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(s["unpopulated_cells"], [])
        self.assertEqual(s["unexpected_cell_keys"], [])

    def test_ombe_provenance_violation_when_cell7_has_data(self):
        """O-MBE (cell_count=6) with populated cell7 → invariant violation."""
        # All 7 cells populated but chamber says only 6 exist
        read_dict = _make_read_dict([1, 2, 3, 4, 5, 6, 7])
        s = phase_cell_summary(read_dict, cell_count=6)
        # cell1..6 correctly identified as populated
        self.assertEqual(s["populated_cells"], [1, 2, 3, 4, 5, 6])
        # cell7 keys flagged as unexpected
        self.assertEqual(len(s["unexpected_cell_keys"]), 8)  # all 8 suffixes
        for key in s["unexpected_cell_keys"]:
            self.assertTrue(key.startswith("cell7_"))

    def test_empty_read_dict(self):
        s = phase_cell_summary({}, cell_count=7)
        self.assertEqual(s["populated_cells"], [])
        self.assertEqual(s["unpopulated_cells"], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(s["unexpected_cell_keys"], [])
        self.assertEqual(s["per_cell"], {})

    def test_all_none_cell_goes_to_unpopulated(self):
        """A cell where every suffix is None counts as unpopulated."""
        read_dict = _make_read_dict([1, 2])  # cells 3-7 all None
        s = phase_cell_summary(read_dict, cell_count=7)
        self.assertEqual(s["populated_cells"], [1, 2])
        self.assertEqual(s["unpopulated_cells"], [3, 4, 5, 6, 7])

    def test_partial_cell_population_still_counts_as_populated(self):
        """A cell with even ONE non-None suffix is 'populated'."""
        # Only cell3_T is populated; the other 7 suffixes are None.
        read_dict = _make_read_dict([])  # all None
        read_dict["cell3_T"] = 425.5
        s = phase_cell_summary(read_dict, cell_count=7)
        self.assertIn(3, s["populated_cells"])
        self.assertEqual(s["per_cell"][3]["populated_suffixes"], ["T"])
        self.assertEqual(s["per_cell"][3]["sample_values"], {"T": 425.5})

    def test_ignores_non_cell_keys(self):
        """service_mode, turbo1_rpm, ion_gauge_a_P are not confused with cell keys."""
        read_dict = _make_read_dict([1])
        # Add some non-cell driver keys that shouldn't trip up the parser
        read_dict.update({
            "service_mode": False,
            "turbo1_rpm": 24000,
            "ion_gauge_a_P": 3.2e-9,
            "pirani_backing_P": 1.5e-3,
        })
        s = phase_cell_summary(read_dict, cell_count=6)
        self.assertEqual(s["populated_cells"], [1])
        self.assertEqual(s["unexpected_cell_keys"], [])


# ---------------------------------------------------------------------------
# Verdict logic (6 states)
# ---------------------------------------------------------------------------

class VerdictLogicTests(unittest.TestCase):

    def _base_config(self, ads_netid: str = "10.0.42.111.1.1", cell_count: int = 6) -> dict:
        return {
            "chamber_id": "ombe",
            "ads_netid": ads_netid,
            "ads_port_main": 851,
            "ads_port_pid": 852,
            "ads_cell_count": cell_count,
            "ads_display_confirmed": False,
            "mistral_mode_default": "ads",
        }

    def _cell_summary(
        self, populated: list[int], cell_count: int,
        unexpected: list[str] = None,
    ) -> dict:
        return {
            "populated_cells": populated,
            "unpopulated_cells": [
                i for i in range(1, cell_count + 1) if i not in populated
            ],
            "unexpected_cell_keys": unexpected or [],
            "per_cell": {},
        }

    def test_chamber_no_ads_verdict(self):
        cfg = self._base_config(ads_netid="")
        state, hint = build_verdict("some_chamber", cfg, {}, {})
        self.assertEqual(state, "chamber_no_ads")
        self.assertIn("no ads_netid", hint)

    def test_connect_failed_verdict(self):
        cfg = self._base_config()
        ads = {"connected": False, "read_ok": False,
               "error": "AdsConnectionError: route not found", "duration_s": 0.1}
        state, hint = build_verdict("ombe", cfg, ads, {})
        self.assertEqual(state, "connect_failed")
        self.assertIn("TwinCAT", hint)
        self.assertIn("route not found", hint)

    def test_read_failed_verdict(self):
        cfg = self._base_config()
        ads = {"connected": True, "read_ok": False,
               "error": "AdsError: variable not found", "duration_s": 0.5}
        state, hint = build_verdict("ombe", cfg, ads, {})
        self.assertEqual(state, "read_failed")
        self.assertIn("variable not found", hint)

    def test_provenance_violation_verdict(self):
        cfg = self._base_config(cell_count=6)  # O-MBE
        ads = {"connected": True, "read_ok": True, "error": "", "duration_s": 0.5}
        cs = self._cell_summary(
            populated=[1, 2, 3, 4, 5, 6],
            cell_count=6,
            unexpected=["cell7_T", "cell7_V"],
        )
        state, hint = build_verdict("ombe", cfg, ads, cs)
        self.assertEqual(state, "provenance_violation")
        self.assertIn("cell7_T", hint)

    def test_no_populated_cells_verdict(self):
        cfg = self._base_config()
        ads = {"connected": True, "read_ok": True, "error": "", "duration_s": 0.3}
        cs = self._cell_summary(populated=[], cell_count=6)
        state, hint = build_verdict("ombe", cfg, ads, cs)
        self.assertEqual(state, "no_populated_cells")
        self.assertIn("idle", hint.lower())

    def test_ok_verdict(self):
        cfg = self._base_config(cell_count=6)
        ads = {"connected": True, "read_ok": True, "error": "", "duration_s": 0.4}
        cs = self._cell_summary(populated=[1, 4, 6], cell_count=6)
        state, hint = build_verdict("ombe", cfg, ads, cs)
        self.assertEqual(state, "ok")
        self.assertIn("3/6", hint)
        self.assertIn("0.4", hint)


# ---------------------------------------------------------------------------
# Evidence bundle
# ---------------------------------------------------------------------------

class EvidenceBundleTests(unittest.TestCase):

    def _standard_fixtures(self):
        return {
            "args": {"output_dir": "/tmp"},
            "env": {"AIQM_CHAMBER": "ombe"},
            "config": {"chamber_id": "ombe", "ads_netid": "10.0.42.111.1.1",
                       "ads_port_main": 851, "ads_port_pid": 852,
                       "ads_cell_count": 6, "ads_display_confirmed": False,
                       "mistral_mode_default": "ads"},
            "ads": {"connected": True, "read_ok": True, "error": "",
                    "duration_s": 0.5, "keys": [],
                    "read_dict": _make_read_dict([1, 2, 3])},
            "cells": {"populated_cells": [1, 2, 3],
                      "unpopulated_cells": [4, 5, 6],
                      "unexpected_cell_keys": [], "per_cell": {}},
            "state": "ok",
            "hint": "ok hint",
        }

    def test_writes_result_json(self):
        import json
        import tempfile
        f = self._standard_fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence"
            written = write_evidence_bundle(
                out, f["args"], f["env"], f["config"],
                f["ads"], f["cells"], f["state"], f["hint"],
            )
            self.assertIn("result.json", written)
            data = json.loads(written["result.json"].read_text())
            self.assertEqual(data["verdict"]["state"], "ok")
            self.assertEqual(data["phase_1_config"]["chamber_id"], "ombe")

    def test_read_dict_stripped_from_json(self):
        """Full read_dict is trimmed; sample + key-count preserved."""
        import json
        import tempfile
        f = self._standard_fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            written = write_evidence_bundle(
                out, f["args"], f["env"], f["config"],
                f["ads"], f["cells"], f["state"], f["hint"],
            )
            data = json.loads(written["result.json"].read_text())
            phase_2 = data["phase_2_ads"]
            self.assertNotIn("read_dict", phase_2)
            self.assertIn("read_dict_key_count", phase_2)
            self.assertIn("read_dict_sample", phase_2)
            self.assertGreater(phase_2["read_dict_key_count"], 0)

    def test_ads_result_never_mutated(self):
        """The writer should not mutate the caller's ads_result dict."""
        import tempfile
        f = self._standard_fixtures()
        # Snapshot the read_dict before the call
        original_read_dict = dict(f["ads"]["read_dict"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_evidence_bundle(
                out, f["args"], f["env"], f["config"],
                f["ads"], f["cells"], f["state"], f["hint"],
            )
        # The caller's ads_result must still have its read_dict intact
        self.assertEqual(f["ads"]["read_dict"], original_read_dict)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CLITests(unittest.TestCase):

    def test_help_does_not_crash(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_output_dir_accepted(self):
        """--help still works when --output-dir is supplied (parse-only test)."""
        with self.assertRaises(SystemExit) as ctx:
            main(["--output-dir", "/tmp/evidence", "--help"])
        self.assertEqual(ctx.exception.code, 0)


# ---------------------------------------------------------------------------
# ADS_CELL_SUFFIXES sanity — the constant should match what growth_logger uses.
# ---------------------------------------------------------------------------

class AdsCellSuffixesConsistencyTests(unittest.TestCase):

    def test_suffixes_match_driver_docstring(self):
        """The 8 suffixes here must match the MistralAdsClient contract."""
        expected = ("T", "T_set", "active_setpoint",
                    "V", "I", "prog_V", "prog_A", "power")
        self.assertEqual(ADS_CELL_SUFFIXES, expected)


if __name__ == "__main__":
    print("Running precheck_mistral_ads tests...\n")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().discover(
        start_dir=str(Path(__file__).parent),
        pattern="test_precheck_mistral_ads.py",
    ))
    sys.exit(0 if result.wasSuccessful() else 1)
