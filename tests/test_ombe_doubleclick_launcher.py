"""Offline checks for the relocatable O-MBE Windows launcher."""

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
LAUNCHER = ROOT / "scripts" / "windows" / "start_ombe.ps1"


def test_ombe_entrypoint_forces_chamber_without_launching(monkeypatch):
    monkeypatch.setenv("AIQM_CHAMBER", "chmbe")
    sys.modules.pop("growth_monitor_ombe", None)
    module = importlib.import_module("growth_monitor_ombe")

    assert os.environ["AIQM_CHAMBER"] == "chmbe"
    module._configure_chamber()
    assert os.environ["AIQM_CHAMBER"] == "ombe"
    assert module._MUTEX_NAME == r"Local\AI4MBE.OMBE.GrowthMonitor"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_ombe_launcher_dry_run_forces_expected_profile():
    environment = os.environ.copy()
    environment["AI4MBE_GUI_PYTHON"] = sys.executable
    environment["AIQM_CHAMBER"] = "chmbe"
    completed = subprocess.run(
        [
            POWERSHELL_EXE, "-NoLogo", "-NoProfile", "-ExecutionPolicy",
            "Bypass", "-File", str(LAUNCHER), "-DryRun",
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
    assert payload["application"] == "ombe"
    assert payload["chamber"] == "ombe"
    assert payload["arguments"] == ["growth_monitor_ombe.py"]
    assert Path(payload["repository_root"]).resolve() == ROOT.resolve()
    assert payload["required_production_modules"] == [
        "vmbpy", "pyads", "serial", "pymodbus",
    ]
    assert payload["dependency_probe"] == "skipped_dry_run"


def test_ombe_cmd_wrapper_is_relocatable_and_hidden():
    text = (ROOT / "Start O-MBE Growth Monitor.cmd").read_text(encoding="utf-8")
    assert '"%~dp0scripts\\windows\\start_ombe.ps1"' in text
    assert "-WindowStyle Hidden" in text
