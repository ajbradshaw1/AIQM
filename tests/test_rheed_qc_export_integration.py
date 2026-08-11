"""Cross-repository smoke test for GUI QC logs -> Classifier2 frame export."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

GUI_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = GUI_ROOT.parent
CLASSIFIER2_ROOT = WORKSPACE_ROOT / "RHEEDClassify" / "Classifier2"
if not (CLASSIFIER2_ROOT / "global_qc_data.py").is_file():
    raise unittest.SkipTest(
        "RHEEDClassify/Classifier2 is not available beside the GUI repository"
    )
for root in (GUI_ROOT, CLASSIFIER2_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

from global_qc_data import build_session_qc_rows  # noqa: E402
from gui.growth_logger import GrowthLogger  # noqa: E402


def _frame(value: int) -> np.ndarray:
    return np.full((24, 32, 3), value, dtype=np.uint8)


def _state(
    segment,
    aligned,
    *,
    count: int = 0,
    ready: bool = False,
    reject="",
    reason: str = "",
) -> dict:
    return {
        "realignment_id": 0,
        "view_segment_id": segment,
        "gun_aligned": aligned,
        "history_frame_count": count,
        "history_required": 2,
        "history_ready": ready,
        "qc_reject": reject,
        "qc_reason": reason,
        "prediction_actionable": bool(aligned and ready and reject is not True),
    }


class GuiToClassifierQcExportTests(unittest.TestCase):
    def test_real_logger_schema_exports_without_semantic_coercion(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = GrowthLogger(base_dir=tmp)
            logger.start_session("QC_EXPORT")
            session_dir = logger.session_dir

            logger.record_rheed_view_event(
                "session_start",
                0.0,
                state_snapshot=_state(None, None),
            )
            logger.record_rheed_view_event(
                "alignment_confirmed",
                1.0,
                state_snapshot=_state(0, True),
                frame_role="initial_aligned",
                frame=_frame(30),
            )
            logger.record_rheed_view_event(
                "qc_pass",
                2.0,
                state_snapshot=_state(0, True, reject=False),
                frame_role="qc_pass",
                frame=_frame(60),
            )
            logger.record_rheed_view_event(
                "realign_start",
                3.0,
                state_snapshot=_state(0, False),
                realignment_id=1,
                previous_view_segment_id=0,
                frame_role="pre_realign",
                frame=_frame(90),
            )
            logger.record_rheed_view_event(
                "qc_reject",
                4.0,
                state_snapshot=_state(
                    0,
                    False,
                    reject=True,
                    reason="defocused",
                ),
                realignment_id=1,
                previous_view_segment_id=0,
                frame_role="qc_reject",
                frame=_frame(120),
            )
            logger.record_rheed_view_event(
                "realign_end",
                5.0,
                state_snapshot=_state(1, True),
                realignment_id=1,
                previous_view_segment_id=0,
                frame_role="post_realign",
                frame=_frame(150),
            )
            logger.end_session()

            rows, summary = build_session_qc_rows(
                session_dir,
                history_frames=2,
            )
            self.assertTrue(summary["event_file_present"])
            labels = {
                Path(row["frame_path"]).name: row["qc_label"]
                for row in rows
            }
            self.assertEqual(
                labels[
                    next(name for name in labels if "_qc_pass_" in name)
                ],
                "PASS",
            )
            self.assertEqual(
                labels[
                    next(name for name in labels if "_qc_reject_" in name)
                ],
                "REJECT",
            )

            pre = next(
                row for row in rows
                if "_realign_start_" in Path(row["frame_path"]).name
            )
            post = next(
                row for row in rows
                if "_realign_end_" in Path(row["frame_path"]).name
            )
            self.assertEqual(pre["qc_label"], "UNKNOWN")
            self.assertEqual(pre["gun_aligned"], "false")
            self.assertEqual(post["view_segment_id"], "1")
            self.assertEqual(post["history_ready"], "false")
            self.assertNotIn(pre["frame_path"], post["history_paths_json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
