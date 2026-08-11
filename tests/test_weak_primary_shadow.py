"""Offline contract tests for the brightness-robust GUI shadow integration."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gui.growth_monitor import GrowthMonitor
from gui.growth_app import (
    _BUNDLED_WEAK_PRIMARY_AI_ROOT,
    _resolve_weak_primary_ai_repo_root,
)
from gui.state import WeakPrimaryShadowState
from gui.weak_primary_shadow import (
    WeakPrimaryShadowBridge,
    _require_available_memory,
    discover_lambda_point_one_checkpoints,
    load_release_manifest,
)

_app = QApplication.instance() or QApplication(sys.argv)


class CollectionDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for fold in range(4):
            for pair in ("2022-02-04", "2022-02-06", "2022-04-11"):
                for seed in (17, 29, 43):
                    directory = self.root / (
                        f"fold_{fold}__pair_{pair}__seed_{seed}__lambda_0.1"
                    )
                    directory.mkdir()
                    (directory / "model.pth").touch()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_requires_all_36_cells_without_selecting_one_checkpoint(self) -> None:
        paths = discover_lambda_point_one_checkpoints(self.root)
        self.assertEqual(len(paths), 36)
        paths[0].unlink()
        with self.assertRaisesRegex(ValueError, "exactly 36"):
            discover_lambda_point_one_checkpoints(self.root)

    def test_checkpoint_contract_rejects_deployable_claim(self) -> None:
        payload = {
            "checkpoint_family": "brightness_robust_weak_primary_v1",
            "execution_scope": "weak_shadow_only",
            "deployment_eligible": True,
            "lambda_pair": 0.1,
            "classes": ["twinned_2x1", "c_6x2", "rt13", "htr"],
            "output_semantics": (
                "conditional_primary_probability_not_surface_fraction"
            ),
            "brightness_policy": "all_extreme",
            "embedding_dim": 512,
            "temperature": 1.0,
            "model_state_dict": {},
            "cache": {},
        }
        with self.assertRaisesRegex(ValueError, "shadow contract"):
            WeakPrimaryShadowBridge._validate_checkpoint(
                payload, Path("model.pth")
            )

    def test_low_memory_preflight_fails_before_model_load(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"AIQM_WEAK_PRIMARY_MIN_AVAILABLE_MB": "1536"},
            ),
            mock.patch(
                "gui.weak_primary_shadow._available_system_memory_mb",
                return_value=900.0,
            ),
        ):
            with self.assertRaisesRegex(MemoryError, "was not loaded"):
                _require_available_memory()

    def test_bundled_package_is_complete_and_is_the_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_REPO_ROOT", None)
            root = Path(_resolve_weak_primary_ai_repo_root())
        self.assertEqual(root.resolve(), _BUNDLED_WEAK_PRIMARY_AI_ROOT.resolve())
        artifact_root = root / "brightness_robust_four_output_all_extreme"
        self.assertTrue((root / "backbones.py").is_file())
        self.assertTrue((root / "Classifier2" / "weak_primary_model.py").is_file())
        self.assertTrue((root / "Classifier2" / "davidson_pairwise.py").is_file())
        manifest, paths = load_release_manifest(artifact_root)
        self.assertEqual(manifest["brightness_policy"], "all_extreme")
        self.assertNotIn("five_output", manifest["contracts"])
        self.assertFalse((artifact_root / "five_output_1x1_gates").exists())
        self.assertTrue(
            (
                artifact_root / "shared_encoder"
                / "dinov2_vits14_pretrained_zeropad512.pth"
            ).is_file()
        )
        self.assertEqual(
            len(paths),
            36,
        )


class ShadowDisplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = GrowthMonitor()

    def tearDown(self) -> None:
        self.monitor.close()

    def test_display_is_explicitly_shadow_and_does_not_touch_sliders(self) -> None:
        before = {
            name: slider.value()
            for name, slider in self.monitor._recon_sliders.items()
        }
        state = WeakPrimaryShadowState(
            loading=False,
            ready=True,
            last_frame_number=7,
            checkpoint_count=36,
            ensemble_id="brightness-robust-all-extreme-cv36-test",
            brightness_policy="all_extreme",
            output_classes=("Twinned (2x1)", "c(6x2)", "rt13xrt13", "HTR"),
            conditional_probabilities={
                "Twinned (2x1)": 0.1,
                "c(6x2)": 0.2,
                "rt13xrt13": 0.3,
                "HTR": 0.4,
            },
            checkpoint_disagreement=0.05,
            inference_ms=12.0,
            actionable=False,
        )
        self.monitor._classifier_output_exposure_recorded = True
        self.monitor.update_weak_primary_shadow_state(state)
        text = self.monitor._weak_primary_shadow_label.text()
        self.assertIn("SHADOW ONLY", text)
        self.assertIn("all_extreme", text)
        self.assertIn("no 1x1", text)
        self.assertIn("HTR 40.0%", text)
        self.assertEqual(
            before,
            {
                name: slider.value()
                for name, slider in self.monitor._recon_sliders.items()
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
