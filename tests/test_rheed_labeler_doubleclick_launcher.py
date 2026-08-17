"""Offline checks for the relocatable RHEED labeler launcher."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCH_SCRIPT = ROOT / "scripts" / "windows" / "start_rheed_labeler.ps1"
WRAPPER = ROOT / "Start RHEED Post-processing Labeler.cmd"
POWERSHELL_EXE = shutil.which("powershell.exe") or "powershell.exe"


def test_doubleclick_wrapper_uses_relocatable_hidden_powershell() -> None:
    text = WRAPPER.read_text(encoding="utf-8")

    assert '"%~dp0scripts\\windows\\start_rheed_labeler.ps1"' in text
    assert "-NoProfile" in text
    assert "-WindowStyle Hidden" in text
    assert "exit /b %rc%" in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell launcher")
def test_labeler_dry_run_is_relocatable_and_sanitized(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["AI4MBE_GUI_PYTHON"] = sys.executable
    environment["AIQM_CHAMBER"] = "chmbe"
    environment["PYTHONPATH"] = r"C:\stale-python"
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["QT_PLUGIN_PATH"] = r"C:\stale-qt"

    completed = subprocess.run(
        [
            POWERSHELL_EXE,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCH_SCRIPT),
            "-DryRun",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        encoding="utf-8-sig",
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["dry_run"] is True
    assert payload["application"] == "labeler"
    assert Path(payload["repository_root"]).resolve() == ROOT.resolve()
    assert Path(payload["working_directory"]).resolve() == ROOT.resolve()
    assert Path(payload["entry_point"]).is_file()
    assert Path(payload["python"]).resolve() == Path(sys.executable).resolve()
    assert payload["python_source"] == "AI4MBE_GUI_PYTHON"
    assert payload["arguments"] == [
        "-m",
        "tools.rheed_postprocessing_labeling",
        "desktop",
    ]
    assert payload["chamber"] is None
    assert payload["python_no_user_site"] == "1"
    assert "PYTHONPATH" in payload["sanitized_variables"]
    assert "QT_QPA_PLATFORM" in payload["sanitized_variables"]


def test_documented_labeler_doubleclick_entry_exists() -> None:
    documentation = (
        ROOT / "tools" / "rheed_postprocessing_labeling" / "README.md",
        ROOT / "docs" / "RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.md",
        ROOT
        / "tools"
        / "rheed_postprocessing_labeling"
        / "manual"
        / "RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.tex",
    )
    for document in documentation:
        assert document.is_file()
        assert "Start RHEED Post-processing Labeler.cmd" in document.read_text(
            encoding="utf-8"
        )

    assert WRAPPER.is_file()
    assert LAUNCH_SCRIPT.is_file()
