"""Hardware-free tests for the strict Vimba commissioning probe."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from scripts.vimba_streaming_demo import (
    JsonlEvidenceWriter,
    _save_pair,
    evaluate_accounting,
)


class AccountingTests(unittest.TestCase):
    def test_exact_trigger_callback_save_chain_passes(self) -> None:
        result = evaluate_accounting(
            triggers_attempted=15,
            triggers_sent=15,
            callbacks_received=15,
            pairs_saved=15,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["unexplained_missing_callbacks"], 0)

    def test_fourteen_of_fifteen_fails_by_default(self) -> None:
        result = evaluate_accounting(
            triggers_attempted=15,
            triggers_sent=15,
            callbacks_received=14,
            pairs_saved=14,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["unexplained_missing_callbacks"], 1)

    def test_initial_loss_requires_explicit_allowance(self) -> None:
        result = evaluate_accounting(
            triggers_attempted=15,
            triggers_sent=15,
            callbacks_received=14,
            pairs_saved=14,
            allow_initial_missing=1,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["missing_callbacks"], 1)
        self.assertEqual(result["allowed_initial_missing"], 1)

    def test_callback_without_saved_pair_fails(self) -> None:
        result = evaluate_accounting(
            triggers_attempted=15,
            triggers_sent=15,
            callbacks_received=15,
            pairs_saved=14,
            save_failures=1,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["unsaved_callbacks"], 1)

    def test_callback_processing_failure_fails_even_when_counts_match(self) -> None:
        result = evaluate_accounting(
            triggers_attempted=15,
            triggers_sent=15,
            callbacks_received=15,
            pairs_saved=15,
            callback_failures=1,
        )
        self.assertFalse(result["passed"])

    def test_extra_callback_and_trigger_failure_fail(self) -> None:
        extra = evaluate_accounting(
            triggers_attempted=15,
            triggers_sent=15,
            callbacks_received=16,
            pairs_saved=16,
        )
        failed_trigger = evaluate_accounting(
            triggers_attempted=15,
            triggers_sent=14,
            callbacks_received=14,
            pairs_saved=14,
            trigger_failures=1,
        )
        self.assertFalse(extra["passed"])
        self.assertEqual(extra["extra_callbacks"], 1)
        self.assertFalse(failed_trigger["passed"])

    def test_negative_or_non_integer_counts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_accounting(
                triggers_attempted=1,
                triggers_sent=-1,
                callbacks_received=0,
                pairs_saved=0,
            )


class EvidenceTests(unittest.TestCase):
    def test_uint16_raw_round_trip_preserves_pixels_and_orientation(self) -> None:
        source = np.array(
            [[0, 1, 2], [255, 1024, 4095]], dtype=np.uint16,
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = _save_pair(source, Path(tmp), "stamp", 0, 12)
            restored = np.asarray(Image.open(result["raw_path"]))
            bgw = np.asarray(Image.open(result["bgw_path"]))
        np.testing.assert_array_equal(restored, source)
        self.assertEqual(bgw.shape, (2, 3, 3))
        self.assertEqual(result["shape"], [2, 3])
        self.assertEqual(result["dtype"], "uint16")
        self.assertEqual(
            result["orientation_transform"], "identity (no flip or transpose)",
        )
        self.assertEqual(len(result["raw_sha256"]), 64)
        self.assertEqual(len(result["bgw_sha256"]), 64)

    def test_jsonl_writer_flushes_complete_parseable_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            writer = JsonlEvidenceWriter(path)
            writer.append("trigger_sent", index=0)
            first = path.read_text(encoding="utf-8").splitlines()
            writer.append("callback_received", callback_index=0)
            writer.close()
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(len(first), 1)
        self.assertEqual(
            [record["event"] for record in records],
            ["trigger_sent", "callback_received"],
        )
        self.assertTrue(all(record["recorded_monotonic_ns"] > 0 for record in records))


if __name__ == "__main__":
    unittest.main()
