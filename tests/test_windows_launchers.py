"""Offline checks for relocatable Windows launch and shortcut scripts."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_SCRIPTS = ROOT / "scripts" / "windows"
POWERSHELL_EXE = shutil.which("powershell.exe") or "powershell.exe"


def _powershell_json(script: Path, *arguments: str, env=None) -> dict:
    completed = subprocess.run(
        [
            POWERSHELL_EXE,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        # Deliberately launch away from the checkout: Explorer shortcuts do
        # not guarantee a useful current working directory.
        cwd=Path(os.environ.get("TEMP") or ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def test_chmbe_entrypoint_forces_chamber_without_launching(monkeypatch):
    monkeypatch.setenv("AIQM_CHAMBER", "ombe")
    sys.modules.pop("growth_monitor_chmbe", None)
    module = importlib.import_module("growth_monitor_chmbe")

    # Import is side-effect free; only the explicit launch function changes it.
    assert os.environ["AIQM_CHAMBER"] == "ombe"
    module._configure_chamber()
    assert os.environ["AIQM_CHAMBER"] == "chmbe"
    assert module._MUTEX_NAME == r"Local\AI4MBE.ChMBE.GrowthMonitor"


def test_ombe_entrypoint_forces_chamber_without_launching(monkeypatch):
    monkeypatch.setenv("AIQM_CHAMBER", "chmbe")
    sys.modules.pop("growth_monitor_ombe", None)
    module = importlib.import_module("growth_monitor_ombe")

    # Import is side-effect free; only the explicit launch function changes it.
    assert os.environ["AIQM_CHAMBER"] == "chmbe"
    module._configure_chamber()
    assert os.environ["AIQM_CHAMBER"] == "ombe"
    assert module._MUTEX_NAME == r"Local\AI4MBE.OMBE.GrowthMonitor"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
@pytest.mark.parametrize(
    ("module_name", "opposite_chamber", "display_name"),
    [
        ("growth_monitor_ombe", "chmbe", "O-MBE"),
        ("growth_monitor_chmbe", "ombe", "Ch-MBE"),
    ],
)
def test_live_entrypoint_rejects_a_second_process_before_gui_import(
    module_name, opposite_chamber, display_name
):
    """Exercise the real cross-process mutex without opening Qt or hardware."""
    module = importlib.import_module(module_name)
    try:
        handle = module._acquire_windows_mutex()
    except RuntimeError:
        pytest.skip(f"An {display_name} Growth Monitor is already running")
    try:
        environment = os.environ.copy()
        environment["AIQM_CHAMBER"] = opposite_chamber
        completed = subprocess.run(
            [sys.executable, str(ROOT / f"{module_name}.py")],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    finally:
        module._release_windows_mutex(handle)

    assert completed.returncode == 2
    assert "already running" in completed.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell launcher")
@pytest.mark.parametrize(
    ("application", "expected_arguments", "expected_chamber"),
    [
        ("ombe", ["growth_monitor_ombe.py"], "ombe"),
        ("chmbe", ["growth_monitor_chmbe.py"], "chmbe"),
        (
            "labeler",
            ["-m", "tools.rheed_postprocessing_labeling", "desktop"],
            None,
        ),
    ],
)
def test_launcher_dry_run_is_relocatable_and_sanitized(
    application, expected_arguments, expected_chamber
):
    environment = os.environ.copy()
    environment["AI4MBE_GUI_PYTHON"] = sys.executable
    environment["AIQM_CHAMBER"] = "ombe"
    environment["PYTHONPATH"] = r"C:\stale-python"
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["QT_QPA_PLATFORM_PLUGIN_PATH"] = r"C:\stale-qt"

    payload = _powershell_json(
        WINDOWS_SCRIPTS / "launch_ai4mbe.ps1",
        "-Application",
        application,
        "-DryRun",
        env=environment,
    )

    assert payload["dry_run"] is True
    assert Path(payload["repository_root"]).resolve() == ROOT.resolve()
    assert Path(payload["working_directory"]).resolve() == ROOT.resolve()
    assert Path(payload["python"]).resolve() == Path(sys.executable).resolve()
    assert payload["python_source"] == "AI4MBE_GUI_PYTHON"
    assert "git_branch" in payload
    assert "git_commit" in payload
    assert payload["arguments"] == expected_arguments
    assert payload["chamber"] == expected_chamber
    assert payload["python_no_user_site"] == "1"
    assert "PYTHONPATH" in payload["sanitized_variables"]
    assert "QT_QPA_PLATFORM" in payload["sanitized_variables"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcut installer")
def test_shortcut_installer_dry_run_never_writes_lnk(tmp_path):
    payload = _powershell_json(
        WINDOWS_SCRIPTS / "install_shortcuts.ps1",
        "-DesktopPath",
        str(tmp_path),
        "-DryRun",
    )

    assert payload["dry_run"] is True
    assert Path(payload["repository_root"]).resolve() == ROOT.resolve()
    assert Path(payload["desktop"]).resolve() == tmp_path.resolve()
    assert {item["Name"] for item in payload["shortcuts"]} == {
        "O-MBE Growth Monitor",
        "Ch-MBE Growth Monitor",
        "RHEED Post-processing Labeler",
    }
    applications = {
        item["Name"]: item["Arguments"] for item in payload["shortcuts"]
    }
    for item in payload["shortcuts"]:
        assert Path(item["Target"]).name.lower() == "powershell.exe"
        assert "-NoProfile" in item["Arguments"]
        assert "-WindowStyle Hidden" in item["Arguments"]
        assert "-ExecutionPolicy Bypass" in item["Arguments"]
        assert str(WINDOWS_SCRIPTS / "launch_ai4mbe.ps1") in item["Arguments"]
        assert Path(item["TroubleshootingWrapper"]).is_file()
    assert "-Application chmbe" in applications["Ch-MBE Growth Monitor"]
    assert "-Application ombe" in applications["O-MBE Growth Monitor"]
    assert (
        "-Application labeler"
        in applications["RHEED Post-processing Labeler"]
    )
    assert not list(tmp_path.glob("*.lnk"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell launcher")
def test_rejected_fast_candidate_falls_through_to_conda_list(tmp_path):
    environment_root = Path(sys.executable).resolve().parent
    assert environment_root.name.lower() == "ai4mbe-gui"

    fake_conda = tmp_path / "fake-conda.cmd"
    conda_payload = json.dumps({"envs": [str(environment_root)]})
    fake_conda.write_text(
        f"@echo off\necho {conda_payload}\nexit /b 0\n",
        encoding="ascii",
    )
    profile = tmp_path / "empty-profile"
    profile.mkdir()
    system_directory = Path(os.environ["SystemRoot"]) / "System32"
    stale_candidate = system_directory / "where.exe"
    assert stale_candidate.is_file()

    environment = os.environ.copy()
    environment["AI4MBE_GUI_PYTHON"] = str(stale_candidate)
    environment["USERPROFILE"] = str(profile)
    environment["PATH"] = str(system_directory)
    environment.pop("CONDA_PREFIX", None)

    payload = _powershell_json(
        WINDOWS_SCRIPTS / "launch_ai4mbe.ps1",
        "-Application",
        "labeler",
        "-DryRun",
        "-ProbeCandidates",
        "-CondaExecutable",
        str(fake_conda),
        "-ManagedEnvironmentPath",
        str(tmp_path / "missing-managed" / "python.exe"),
        env=environment,
    )

    assert Path(payload["python"]).resolve() == Path(sys.executable).resolve()
    assert payload["python_source"] == "conda env list"


def test_cmd_wrappers_quote_their_relocatable_script_path():
    expected = {
        "Start O-MBE Growth Monitor.cmd": "-Application ombe",
        "Start Ch-MBE Growth Monitor.cmd": "-Application chmbe",
        "Start RHEED Post-processing Labeler.cmd": "-Application labeler",
    }
    for name, application_argument in expected.items():
        text = (ROOT / name).read_text(encoding="utf-8")
        assert '"%~dp0scripts\\windows\\launch_ai4mbe.ps1"' in text
        assert application_argument in text

    installer = (ROOT / "Install AI4MBE Desktop Shortcuts.cmd").read_text(
        encoding="utf-8"
    )
    assert '"%~dp0scripts\\windows\\install_shortcuts.ps1"' in installer


def test_live_gui_dependency_probe_imports_torch_before_pyqt6():
    launcher = (WINDOWS_SCRIPTS / "launch_ai4mbe.ps1").read_text(
        encoding="utf-8"
    )
    probe = '"import torch; import PyQt6, numpy, PIL"'
    assert probe in launcher
    assert "import PyQt6, numpy, PIL, torch" not in launcher
    assert '$ApplicationName -in @("ombe", "chmbe")' in launcher
