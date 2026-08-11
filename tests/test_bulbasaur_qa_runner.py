"""Unit tests for scripts/bulbasaur_qa_runner.py.

Interactive prompt path is deliberately NOT tested — that's the
grower's UX. This file covers the state machinery that would
silently break if refactored: queue integrity, report format,
resume parser.

Run:
    python -m pytest -q tests/test_bulbasaur_qa_runner.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.bulbasaur_qa_runner import (  # noqa: E402
    VALIDATION_QUEUE,
    ItemResult,
    ValidationItem,
    append_result,
    initialize_report,
    load_prior_results,
)


class ValidationQueueTests(unittest.TestCase):
    """Queue integrity — unique ids, valid areas, no empty steps."""

    def test_queue_has_expected_size(self):
        # If this drops below 13 something got removed accidentally.
        # If it grows way past 15 we've probably duplicated an item.
        self.assertGreaterEqual(len(VALIDATION_QUEUE), 13)
        self.assertLessEqual(len(VALIDATION_QUEUE), 20)

    def test_ids_are_unique(self):
        # Duplicate ids would break resume parsing (later result
        # would silently overwrite earlier).
        ids = [item.id for item in VALIDATION_QUEUE]
        self.assertEqual(len(ids), len(set(ids)))

    def test_all_items_have_required_fields(self):
        for item in VALIDATION_QUEUE:
            self.assertIsInstance(item, ValidationItem)
            self.assertTrue(item.id, msg=f"empty id in {item}")
            self.assertTrue(item.title, msg=f"empty title in {item.id}")
            self.assertIn(item.area, ("gui", "drivers", "scripts"),
                          msg=f"invalid area in {item.id}")
            self.assertTrue(item.shipped_ref,
                            msg=f"empty shipped_ref in {item.id}")
            self.assertTrue(item.description,
                            msg=f"empty description in {item.id}")
            self.assertTrue(len(item.steps) > 0,
                            msg=f"no steps in {item.id}")
            self.assertTrue(item.expected,
                            msg=f"empty expected in {item.id}")


class ReportIOTests(unittest.TestCase):
    """append_result + initialize_report round-trip."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.report_path = Path(self.tmp.name) / "report.md"

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize_creates_header(self):
        initialize_report(self.report_path)
        self.assertTrue(self.report_path.exists())
        content = self.report_path.read_text(encoding="utf-8")
        self.assertIn("Bulbasaur validation report", content)
        self.assertIn(str(len(VALIDATION_QUEUE)), content)

    def test_append_result_writes_item_section(self):
        initialize_report(self.report_path)
        item = VALIDATION_QUEUE[0]
        result = ItemResult(
            item_id=item.id, verdict="pass",
            notes="worked cleanly, no issues",
            tested_iso="2026-07-25T14:00:00",
        )
        append_result(self.report_path, item, result)
        content = self.report_path.read_text(encoding="utf-8")
        self.assertIn(f"`{item.id}`", content)
        self.assertIn("PASS", content)
        self.assertIn("worked cleanly, no issues", content)

    def test_appends_do_not_clobber(self):
        # Two appends → both items appear in the report.
        initialize_report(self.report_path)
        for i, item in enumerate(VALIDATION_QUEUE[:2]):
            append_result(self.report_path, item, ItemResult(
                item_id=item.id, verdict="pass",
                notes=f"note {i}",
                tested_iso="2026-07-25T14:00:00",
            ))
        content = self.report_path.read_text(encoding="utf-8")
        self.assertIn(f"`{VALIDATION_QUEUE[0].id}`", content)
        self.assertIn(f"`{VALIDATION_QUEUE[1].id}`", content)


class ResumeParserTests(unittest.TestCase):
    """load_prior_results reads back what append_result wrote."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.report_path = Path(self.tmp.name) / "report.md"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_report_yields_empty_dict(self):
        # Non-existent path → empty dict (fail-open resume).
        result = load_prior_results(self.report_path)
        self.assertEqual(result, {})

    def test_round_trip_preserves_ids_and_verdicts(self):
        initialize_report(self.report_path)
        for verdict, item in zip(
            ["pass", "fail", "skip"], VALIDATION_QUEUE[:3],
        ):
            append_result(self.report_path, item, ItemResult(
                item_id=item.id, verdict=verdict,
                notes=f"note for {verdict}",
                tested_iso="2026-07-25T14:00:00",
            ))
        parsed = load_prior_results(self.report_path)
        self.assertEqual(len(parsed), 3)
        for i, verdict in enumerate(["pass", "fail", "skip"]):
            item_id = VALIDATION_QUEUE[i].id
            self.assertIn(item_id, parsed)
            self.assertEqual(parsed[item_id].verdict, verdict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
