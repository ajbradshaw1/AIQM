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
        "AI4MBE Operator Manual",
        "Uninstall AI4MBE Growth Monitor",
    }
    applications = {
        item["Name"]: item["Arguments"] for item in payload["shortcuts"]
    }
    application_items = [
        item for item in payload["shortcuts"]
        if item["Name"] in applications and item["Name"] not in {
            "AI4MBE Operator Manual", "Uninstall AI4MBE Growth Monitor"
        }
    ]
    for item in application_items:
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
    manual = next(
        item for item in payload["shortcuts"]
        if item["Name"] == "AI4MBE Operator Manual"
    )
    assert Path(manual["Target"]).suffix.lower() == ".pdf"
    uninstall = next(
        item for item in payload["shortcuts"]
        if item["Name"] == "Uninstall AI4MBE Growth Monitor"
    )
    assert Path(uninstall["Target"]).name.lower() == "powershell.exe"
    assert "uninstall_ai4mbe.ps1" in uninstall["Arguments"]
    assert "-RemoveApplicationFiles" in uninstall["Arguments"]
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

    full_installer = (ROOT / "Install AI4MBE Growth Monitor.cmd").read_text(
        encoding="utf-8"
    )
    assert '"%~dp0scripts\\windows\\install_ai4mbe.ps1"' in full_installer
    uninstaller = (ROOT / "Uninstall AI4MBE Growth Monitor.cmd").read_text(
        encoding="utf-8"
    )
    assert 'set "uninstaller=%~dp0scripts\\windows\\uninstall_ai4mbe.ps1"' in uninstaller
    assert '-File "%uninstaller%"' in uninstaller


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
def test_full_installer_dry_run_separates_program_and_data(tmp_path):
    payload = _powershell_json(
        WINDOWS_SCRIPTS / "install_ai4mbe.ps1",
        "-SourceRoot",
        str(ROOT),
        "-InstallRoot",
        str(tmp_path / "program"),
        "-DataRoot",
        str(tmp_path / "sessions"),
        "-PythonPath",
        sys.executable,
        "-DryRun",
    )

    assert payload["product_id"] == "AI4MBE.GrowthMonitor.Windows"
    assert payload["architecture"] == "x64"
    assert Path(payload["install_root"]) == tmp_path / "program"
    assert Path(payload["data_root"]) == tmp_path / "sessions"
    assert Path(payload["python"]).resolve() == Path(sys.executable).resolve()
    assert payload["create_environment"] is False
    assert str(tmp_path / "sessions") in payload["preserve_on_uninstall"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
def test_full_installer_accepts_selected_parent_without_dialog(tmp_path):
    parent = tmp_path / "custom install parent"
    payload = _powershell_json(
        WINDOWS_SCRIPTS / "install_ai4mbe.ps1",
        "-SourceRoot",
        str(ROOT),
        "-InstallParent",
        str(parent),
        "-DataRoot",
        str(tmp_path / "sessions"),
        "-PythonPath",
        sys.executable,
        "-DryRun",
    )

    expected = parent / "AI4MBE-Growth-Monitor"
    assert Path(payload["install_root"]) == expected
    assert Path(payload["selected_install_parent"]) == parent


def test_installer_uses_windows_folder_picker_for_double_click():
    text = (WINDOWS_SCRIPTS / "install_ai4mbe.ps1").read_text(encoding="utf-8")
    assert "System.Windows.Forms.FolderBrowserDialog" in text
    assert "Select-InstallParent" in text
    assert 'Join-Path $selectedInstallParent "AI4MBE-Growth-Monitor"' in text


def test_inno_setup_is_x64_per_user_and_uses_existing_runtime_installer():
    text = (WINDOWS_SCRIPTS / "ai4mbe_growth_monitor.iss").read_text(
        encoding="utf-8"
    )
    assert "ArchitecturesAllowed=x64compatible" in text
    assert "PrivilegesRequired=lowest" in text
    assert "DefaultDirName={localappdata}\\Programs\\AI4MBE-Growth-Monitor" in text
    assert "install_ai4mbe.ps1" in text
    assert "-ManagedUninstallerPath" in text
    assert "{userdocs}\\AI4MBE\\GrowthSessions" in text
    assert "[UninstallDelete]" in text
    assert 'Type: filesandordirs; Name: "{app}"' in text


def test_windows_release_workflow_builds_exe_and_keeps_zip_fallback():
    text = (ROOT / ".github" / "workflows" / "windows-release.yml").read_text(
        encoding="utf-8"
    )
    assert "choco install innosetup" in text
    assert "ISCC.exe" in text
    assert "Windows-x64-Setup.exe" in text
    assert "gh release create" in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows uninstaller")
def test_uninstaller_requires_marker_and_preserves_data(tmp_path):
    install_root = tmp_path / "program"
    data_root = tmp_path / "sessions"
    desktop = tmp_path / "desktop"
    install_root.mkdir()
    data_root.mkdir()
    desktop.mkdir()
    (data_root / "do-not-delete.txt").write_text("experiment", encoding="utf-8")
    marker = {
        "schema_version": 1,
        "product_id": "AI4MBE.GrowthMonitor.Windows",
        "architecture": "x64",
        "install_root": str(install_root),
        "data_root": str(data_root),
    }
    (install_root / ".ai4mbe-install.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )
    (install_root / "application.txt").write_text("remove", encoding="utf-8")
    for name in (
        "O-MBE Growth Monitor.lnk",
        "Uninstall AI4MBE Growth Monitor.lnk",
    ):
        (desktop / name).write_text("shortcut", encoding="utf-8")

    completed = subprocess.run(
        [
            POWERSHELL_EXE,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WINDOWS_SCRIPTS / "uninstall_ai4mbe.ps1"),
            "-InstallRoot",
            str(install_root),
            "-DesktopPath",
            str(desktop),
            "-RemoveApplicationFiles",
            "-Quiet",
        ],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert not install_root.exists()
    assert (data_root / "do-not-delete.txt").read_text(encoding="utf-8") == "experiment"
    assert not list(desktop.glob("*.lnk"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows uninstaller")
def test_script_uninstaller_delegates_to_registered_exe(tmp_path):
    install_root = tmp_path / "program"
    data_root = tmp_path / "sessions"
    managed_uninstaller = tmp_path / "unins000.exe"
    install_root.mkdir()
    data_root.mkdir()
    managed_uninstaller.write_bytes(b"placeholder")
    marker = {
        "schema_version": 1,
        "product_id": "AI4MBE.GrowthMonitor.Windows",
        "architecture": "x64",
        "install_root": str(install_root),
        "data_root": str(data_root),
        "managed_uninstaller": str(managed_uninstaller),
    }
    (install_root / ".ai4mbe-install.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )

    payload = _powershell_json(
        WINDOWS_SCRIPTS / "uninstall_ai4mbe.ps1",
        "-InstallRoot",
        str(install_root),
        "-DryRun",
    )

    assert Path(payload["delegated_to"]) == managed_uninstaller
    assert Path(payload["install_root"]) == install_root
    assert install_root.is_dir()


def test_live_gui_dependency_probe_imports_torch_before_pyqt6():
    launcher = (WINDOWS_SCRIPTS / "launch_ai4mbe.ps1").read_text(
        encoding="utf-8"
    )
    probe = '"import struct; assert struct.calcsize(\'P\') == 8; import torch; import PyQt6, numpy, PIL"'
    assert probe in launcher
    assert "import PyQt6, numpy, PIL, torch" not in launcher
    assert '$ApplicationName -in @("ombe", "chmbe")' in launcher
