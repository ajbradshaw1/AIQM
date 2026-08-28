"""Focused tests for shared Classifier2 repository discovery."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui.classifier_repository import (
    _layout_candidates,
    classifier2_directory,
    resolve_ai_repo_root,
)
from gui.events_tab import EventsTab
from gui.growth_app import GrowthApp, _resolve_ai_repo_root
from gui.state import RheedQcState


def _classifier_repo(path: Path, *, nested: bool = False) -> Path:
    classifier = (
        path / "src" / "classifiers" / "classifier2"
        if nested
        else path / "Classifier2"
    )
    classifier.mkdir(parents=True)
    (classifier / "evaluate.py").write_text("# test fixture\n", encoding="utf-8")
    return path


class SharedResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_environment_override_is_authoritative(self) -> None:
        override = _classifier_repo(self.root / "override", nested=True)
        migrated = _classifier_repo(
            self.root / "workspace" / "repos" / "rheed-perception"
        )
        instrument = self.root / "workspace" / "repos" / "aiqm-instrument"
        instrument.mkdir(parents=True)

        resolved = resolve_ai_repo_root(
            environ={"AI_REPO_ROOT": str(override)},
            instrument_root=instrument,
            legacy_roots=(migrated,),
        )

        self.assertEqual(resolved, override.resolve())

    def test_invalid_environment_override_does_not_fall_through(self) -> None:
        invalid = self.root / "explicit-but-invalid"
        invalid.mkdir()
        fallback = _classifier_repo(self.root / "legacy")

        with self.assertRaisesRegex(FileNotFoundError, "AI_REPO_ROOT points"):
            resolve_ai_repo_root(
                environ={"AI_REPO_ROOT": str(invalid)},
                instrument_root=self.root / "instrument",
                legacy_roots=(fallback,),
            )

    def test_blank_environment_override_is_authoritative(self) -> None:
        fallback = _classifier_repo(self.root / "legacy")

        with self.assertRaisesRegex(FileNotFoundError, "set but empty"):
            resolve_ai_repo_root(
                environ={"AI_REPO_ROOT": "  \t"},
                instrument_root=self.root / "instrument",
                legacy_roots=(fallback,),
            )

    def test_migrated_workspace_layout_is_discovered(self) -> None:
        workspace = self.root / "AI4MBE"
        instrument = workspace / "repos" / "aiqm-instrument"
        expected = _classifier_repo(workspace / "repos" / "rheed-perception")
        instrument.mkdir(parents=True)

        resolved = resolve_ai_repo_root(
            environ={}, instrument_root=instrument, legacy_roots=()
        )

        self.assertEqual(resolved, expected.resolve())

    def test_linked_worktree_discovers_workspace_repository(self) -> None:
        workspace = self.root / "AI4MBE"
        worktree = workspace / "worktrees" / "aiqm-instrument" / "feature"
        expected = _classifier_repo(workspace / "repos" / "rheed-perception")
        worktree.mkdir(parents=True)
        marker = workspace / "workspace" / "repos.lock.yaml"
        marker.parent.mkdir()
        marker.write_text("repositories: {}\n", encoding="utf-8")

        resolved = resolve_ai_repo_root(
            environ={}, instrument_root=worktree, legacy_roots=()
        )

        self.assertEqual(resolved, expected.resolve())

    def test_unmarked_ancestor_repos_tree_is_not_selected(self) -> None:
        # Keep this synthetic checkout outside the real D:\AI4MBE ancestry.
        # The full suite intentionally redirects TEMP into that workspace,
        # where upward discovery should find the real repos.lock.yaml.
        checkout = Path(r"Z:\unrelated\worktrees\instrument")
        unrelated_repo = Path(r"Z:\unrelated\repos\rheed-perception")

        self.assertNotIn(unrelated_repo, _layout_candidates(checkout))

        with self.assertRaisesRegex(FileNotFoundError, "Could not locate"):
            resolve_ai_repo_root(
                environ={}, instrument_root=checkout, legacy_roots=()
            )

    def test_existing_legacy_checkout_is_the_final_fallback(self) -> None:
        legacy = _classifier_repo(self.root / "legacy-ai-for-quantum")

        resolved = resolve_ai_repo_root(
            environ={},
            instrument_root=Path(r"Z:\standalone-instrument"),
            legacy_roots=(self.root / "missing", legacy),
        )

        self.assertEqual(resolved, legacy.resolve())

    def test_no_candidate_fails_with_checked_paths_and_remediation(self) -> None:
        legacy = self.root / "missing-legacy"

        with self.assertRaises(FileNotFoundError) as caught:
            resolve_ai_repo_root(
                environ={},
                instrument_root=Path(r"Z:\standalone-instrument"),
                legacy_roots=(legacy,),
            )

        message = str(caught.exception)
        self.assertIn(str(legacy), message)
        self.assertIn("Set AI_REPO_ROOT", message)

    def test_incomplete_preferred_layout_does_not_shadow_complete_root_layout(
        self,
    ) -> None:
        repository = self.root / "dual-layout"
        (repository / "src" / "classifiers" / "classifier2").mkdir(
            parents=True
        )
        complete = _classifier_repo(repository) / "Classifier2"

        self.assertEqual(classifier2_directory(repository), complete.resolve())

    def test_classifier_bridge_uses_complete_layout_selector(self) -> None:
        repository = self.root / "bridge-dual-layout"
        (repository / "src" / "classifiers" / "classifier2").mkdir(
            parents=True
        )
        complete = _classifier_repo(repository) / "Classifier2"
        selected_paths = []

        evaluate = types.ModuleType("evaluate")

        def load_model(_path):
            selected_paths.append(Path(sys.path[0]))
            return object(), "cpu"

        evaluate.load_model = load_model
        evaluate.load_all_ideal_scores = lambda *_args: {}
        evaluate.load_bad_image_scores = lambda *_args: []
        evaluate.classify_winrate = lambda *_args: {}
        fake_torch = types.ModuleType("torch")

        from gui.classifier_bridge import ClassifierBridge

        original_sys_path = list(sys.path)
        try:
            with patch.dict(
                sys.modules,
                {"evaluate": evaluate, "torch": fake_torch},
            ):
                ClassifierBridge(repository)
        finally:
            sys.path[:] = original_sys_path

        self.assertEqual(selected_paths, [complete.resolve()])


class ResolverCallSiteTests(unittest.TestCase):
    def test_growth_app_uses_shared_resolver(self) -> None:
        expected = Path("resolved-perception-root")
        with patch("gui.growth_app.resolve_ai_repo_root", return_value=expected):
            self.assertEqual(_resolve_ai_repo_root(), str(expected))

    def test_events_tab_uses_shared_resolver(self) -> None:
        expected = Path("resolved-perception-root")
        with patch("gui.events_tab.resolve_ai_repo_root", return_value=expected):
            self.assertIs(EventsTab._default_ai_repo_root(), expected)


class _Signal:
    def __init__(self) -> None:
        self.connections: list[tuple] = []

    def connect(self, *args) -> None:
        self.connections.append(args)


class _Worker:
    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs
        self.state_updated = _Signal()
        self.running = False

    def start(self) -> None:
        self.running = True

    def isRunning(self) -> bool:
        return self.running

    def on_rheed_state(self, _state) -> None:
        pass


class _Value:
    def __init__(self, value) -> None:
        self.value = value

    def currentText(self) -> str:
        return str(self.value)

    def text(self) -> str:
        return str(self.value)

    def isChecked(self) -> bool:
        return bool(self.value)


class _Monitor:
    def __init__(self) -> None:
        self.config_camera_mode = _Value("dummy")
        self.config_camera_exposure_ms = SimpleNamespace(value=lambda: 0.0)
        self.config_pyrometer_mode = _Value("dummy")
        self.config_mistral_mode = _Value("dummy")
        self.config_evap_mode = _Value("dummy")
        self.config_exactus_port = _Value("COM_TEST")
        self.config_exactus_baud = _Value("115200")
        self.config_classifier_enabled = _Value(True)
        self.config_weak_primary_shadow_enabled = _Value(True)
        self.classifier_states = []
        self.state = "idle"

    def clear_camera_provenance(self) -> None:
        pass

    def update_classifier_state(self, state) -> None:
        self.classifier_states.append(state)

    def set_state(self, state: str) -> None:
        self.state = state


class _StatusBar:
    def __init__(self) -> None:
        self.messages: list[tuple] = []

    def showMessage(self, *args) -> None:
        self.messages.append(args)


class _ArmHarness:
    def __init__(self) -> None:
        self.monitor = _Monitor()
        self.camera_worker = None
        self.classifier_worker = None
        self.weak_primary_shadow_worker = None
        self.pyrometer_worker = None
        self.mistral_worker = None
        self.evap_worker = None
        self._latest_classifier = None
        self._rheed_qc_state = RheedQcState()
        self.growth_log = SimpleNamespace(active=False)
        self._status_bar = _StatusBar()
        self._chamber_config = SimpleNamespace(
            camera_index=0,
            camera_fps=1.0,
            pyrometer_port="COM_TEST",
            pyrometer_device_id=1,
            pyrometer_rts=False,
            pyrometer_modbus_backend="raw_serial",
        )

    def statusBar(self):
        return self._status_bar

    def _on_classifier_state(self, state) -> None:
        GrowthApp._on_classifier_state(self, state)

    def _on_camera_state(self, _state) -> None:
        pass

    def _on_weak_primary_shadow_state(self, _state) -> None:
        pass

    def _on_pyrometer_state(self, _state) -> None:
        pass

    def _on_mistral_state(self, _state) -> None:
        pass

    def _on_evap_state(self, _state) -> None:
        pass


class ArmDegradationTests(unittest.TestCase):
    def test_resolver_failure_does_not_abort_other_worker_startup(self) -> None:
        app = _ArmHarness()
        worker_names = (
            "RheedCameraWorker",
            "WeakPrimaryShadowWorker",
            "PyrometerWorker",
            "MistralWorker",
            "EvapControlWorker",
        )
        worker_patches = [
            patch(f"gui.growth_app.{name}", _Worker) for name in worker_names
        ]
        for worker_patch in worker_patches:
            worker_patch.start()
            self.addCleanup(worker_patch.stop)

        with (
            patch(
                "gui.growth_app._resolve_ai_repo_root",
                side_effect=FileNotFoundError("no classifier checkout"),
            ),
            patch(
                "gui.growth_app._resolve_weak_primary_ai_repo_root",
                return_value="bundled-shadow",
            ),
            patch("gui.growth_app.ClassifierWorker") as classifier_worker,
        ):
            GrowthApp._on_arm(app)

        classifier_worker.assert_not_called()
        self.assertIsNone(app.classifier_worker)
        for attribute in (
            "camera_worker",
            "weak_primary_shadow_worker",
            "pyrometer_worker",
            "mistral_worker",
            "evap_worker",
        ):
            self.assertTrue(getattr(app, attribute).isRunning(), attribute)
        self.assertEqual(app.monitor.state, "armed")
        self.assertEqual(len(app.monitor.classifier_states), 1)
        self.assertIn(
            "no classifier checkout", app.monitor.classifier_states[0].error
        )
        self.assertIn("classifier unavailable", app._status_bar.messages[-1][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
