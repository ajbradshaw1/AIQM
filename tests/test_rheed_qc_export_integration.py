"""Cross-repository smoke test for GUI QC logs -> Classifier2 frame export."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

import numpy as np

GUI_ROOT = Path(__file__).resolve().parent.parent
if str(GUI_ROOT) not in sys.path:
    sys.path.insert(0, str(GUI_ROOT))

from gui.classifier_repository import (  # noqa: E402
    classifier2_directory,
    resolve_ai_repo_root,
)
from gui.growth_logger import GrowthLogger  # noqa: E402


def _workspace_root(gui_root: Path) -> Path | None:
    """Return the marked workspace containing *gui_root*, if one exists."""
    for candidate in (gui_root, *gui_root.parents):
        if (candidate / "workspace" / "repos.lock.yaml").is_file():
            return candidate
    return None


def _resolve_classifier2_root(
    gui_root: Path = GUI_ROOT,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Locate Classifier2 without hiding broken configured workspaces.

    ``AI_REPO_ROOT`` names the perception repository itself and is therefore
    explicit configuration that fails closed.
    The migrated multi-repository workspace is detected by its lock file and
    likewise fails if its canonical perception repository is incomplete.
    Only a genuinely standalone GUI checkout may skip this cross-repo test.
    """
    environment = os.environ if environ is None else environ
    workspace_root = _workspace_root(gui_root)
    try:
        repository = resolve_ai_repo_root(
            environ=environment,
            instrument_root=gui_root,
            legacy_roots=(gui_root.parent / "RHEEDClassify",),
        )
        classifier2_root = classifier2_directory(repository)
    except FileNotFoundError:
        # Explicit configuration and marked workspaces are operational
        # contracts, so a broken checkout must fail instead of hiding behind
        # a skip. Only a genuinely standalone checkout may omit Classifier2.
        if "AI_REPO_ROOT" in environment or workspace_root is not None:
            raise
        raise unittest.SkipTest(
            "Classifier2 is unavailable for this standalone GUI checkout; "
            "set AI_REPO_ROOT to enable the cross-repository integration test"
        ) from None
    exporter = classifier2_root / "global_qc_data.py"
    if not exporter.is_file():
        raise FileNotFoundError(
            f"Resolved Classifier2 checkout has no global_qc_data.py: "
            f"{classifier2_root}"
        )
    return classifier2_root


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


class Classifier2RootLocatorTests(unittest.TestCase):
    @staticmethod
    def _create_classifier2(repo_root: Path) -> Path:
        classifier2_root = repo_root / "Classifier2"
        classifier2_root.mkdir(parents=True)
        (classifier2_root / "evaluate.py").write_text(
            "# resolver sentinel\n",
            encoding="utf-8",
        )
        (classifier2_root / "global_qc_data.py").write_text(
            "# locator sentinel\n",
            encoding="utf-8",
        )
        return classifier2_root.resolve()

    def test_explicit_ai_repo_root_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "perception"
            expected = self._create_classifier2(repo_root)
            actual = _resolve_classifier2_root(
                Path(tmp) / "standalone-gui",
                {"AI_REPO_ROOT": str(repo_root)},
            )
            self.assertEqual(actual, expected)

    def test_missing_explicit_ai_repo_root_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                _resolve_classifier2_root(
                    Path(tmp) / "standalone-gui",
                    {"AI_REPO_ROOT": str(Path(tmp) / "missing")},
                )

    def test_explicit_workspace_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp)
            self._create_classifier2(
                workspace_root / "repos" / "rheed-perception"
            )
            with self.assertRaises(FileNotFoundError):
                _resolve_classifier2_root(
                    workspace_root / "repos" / "aiqm-instrument",
                    {"AI_REPO_ROOT": str(workspace_root)},
                )

    def test_migrated_workspace_layout_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp)
            lock = workspace_root / "workspace" / "repos.lock.yaml"
            lock.parent.mkdir(parents=True)
            lock.write_text("schema_version: 1\n", encoding="utf-8")
            expected = self._create_classifier2(
                workspace_root / "repos" / "rheed-perception"
            )
            gui_root = (
                workspace_root
                / "worktrees"
                / "aiqm-instrument"
                / "deployed-labeling"
            )
            actual = _resolve_classifier2_root(gui_root, {})
            self.assertEqual(actual, expected)

    def test_incomplete_migrated_workspace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp)
            lock = workspace_root / "workspace" / "repos.lock.yaml"
            lock.parent.mkdir(parents=True)
            lock.write_text("schema_version: 1\n", encoding="utf-8")
            gui_root = workspace_root / "repos" / "aiqm-instrument"
            with self.assertRaises(FileNotFoundError):
                _resolve_classifier2_root(gui_root, {})

    def test_unconfigured_standalone_checkout_skips(self):
        # Use a virtual drive path so redirecting TEMP under D:\AI4MBE does
        # not make this synthetic standalone checkout inherit the real
        # workspace marker from one of its ancestors.
        with self.assertRaises(unittest.SkipTest):
            _resolve_classifier2_root(Path(r"Z:\standalone-gui"), {})


class GuiToClassifierQcExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        classifier2_root = _resolve_classifier2_root()
        if str(classifier2_root) not in sys.path:
            sys.path.insert(0, str(classifier2_root))
        from global_qc_data import build_session_qc_rows

        cls.build_session_qc_rows = staticmethod(build_session_qc_rows)

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

            rows, summary = self.build_session_qc_rows(
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
