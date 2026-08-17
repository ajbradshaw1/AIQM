"""Offline checks for the relocatable Ch-MBE Windows launcher."""

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
POWERSHELL_EXE = shutil.which("powershell.exe") or "powershell.exe"
LAUNCHER = ROOT / "scripts" / "windows" / "start_chmbe.ps1"


def test_chmbe_entrypoint_forces_chamber_without_launching(monkeypatch):
    monkeypatch.setenv("AIQM_CHAMBER", "ombe")
    sys.modules.pop("growth_monitor_chmbe", None)
    module = importlib.import_module("growth_monitor_chmbe")

    # Import is side-effect free; only the explicit launch function changes it.
    assert os.environ["AIQM_CHAMBER"] == "ombe"
    module._configure_chamber()
    assert os.environ["AIQM_CHAMBER"] == "chmbe"
    assert module._MUTEX_NAME == r"Local\AI4MBE.ChMBE.GrowthMonitor"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_launcher_dry_run_is_relocatable_and_sanitized():
    environment = os.environ.copy()
    environment["AI4MBE_GUI_PYTHON"] = sys.executable
    environment["AIQM_CHAMBER"] = "ombe"
    environment["PYTHONPATH"] = r"C:\stale-python"
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["QT_QPA_PLATFORM_PLUGIN_PATH"] = r"C:\stale-qt"

    completed = subprocess.run(
        [
            POWERSHELL_EXE,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            "-DryRun",
        ],
        cwd=Path(os.environ.get("TEMP") or ROOT),
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["dry_run"] is True
    assert payload["application"] == "chmbe"
    assert payload["chamber"] == "chmbe"
    assert Path(payload["repository_root"]).resolve() == ROOT.resolve()
    assert Path(payload["working_directory"]).resolve() == ROOT.resolve()
    assert Path(payload["python"]).resolve() == Path(sys.executable).resolve()
    assert payload["python_source"] == "AI4MBE_GUI_PYTHON"
    assert payload["arguments"] == ["growth_monitor_chmbe.py"]
    assert payload["python_no_user_site"] == "1"
    assert payload["required_production_modules"] == [
        "vmbpy", "pyads", "serial",
    ]
    assert payload["dependency_probe"] == "skipped_dry_run"
    assert payload["dependency_status"] == {
        "vmbpy": "not_probed",
        "pyads": "not_probed",
        "serial": "not_probed",
    }
    assert "PYTHONPATH" in payload["sanitized_variables"]
    assert "QT_QPA_PLATFORM" in payload["sanitized_variables"]


def test_cmd_wrapper_quotes_relocatable_hidden_launcher():
    text = (ROOT / "Start Ch-MBE Growth Monitor.cmd").read_text(
        encoding="utf-8"
    )
    assert '"%~dp0scripts\\windows\\start_chmbe.ps1"' in text
    assert "-WindowStyle Hidden" in text


def test_launcher_never_installs_or_changes_persistent_environment():
    text = LAUNCHER.read_text(encoding="utf-8").lower()
    assert "pip install" not in text
    assert "conda install" not in text
    assert "setenvironmentvariable" not in text
    assert "Get-RequiredProductionModules".lower() in text
    assert "$driverImports".lower() in text
