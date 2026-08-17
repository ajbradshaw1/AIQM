"""Offline contract tests for the three AI4MBE desktop shortcuts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL_EXE = shutil.which("powershell.exe") or "powershell.exe"
INSTALLER = ROOT / "scripts" / "windows" / "install_ai4mbe_shortcuts.ps1"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_shortcut_installer_dry_run_lists_all_three_without_writing():
    completed = subprocess.run(
        [
            POWERSHELL_EXE, "-NoLogo", "-NoProfile", "-ExecutionPolicy",
            "Bypass", "-File", str(INSTALLER), "-DryRun",
        ],
        cwd=Path(os.environ.get("TEMP") or ROOT),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["dry_run"] is True
    assert {item["name"] for item in payload["shortcuts"]} == {
        "O-MBE Growth Monitor",
        "Ch-MBE Growth Monitor",
        "RHEED Post-processing Labeler",
    }
    for item in payload["shortcuts"]:
        assert Path(item["target"]).is_file()
        assert Path(item["working_directory"]).resolve() == ROOT.resolve()


def test_installer_wrapper_is_relocatable_and_never_installs_packages():
    wrapper = (ROOT / "Install AI4MBE Desktop Shortcuts.cmd").read_text(
        encoding="utf-8"
    )
    assert '"%~dp0scripts\\windows\\install_ai4mbe_shortcuts.ps1"' in wrapper
    script = INSTALLER.read_text(encoding="utf-8").lower()
    assert "pip install" not in script
    assert "conda install" not in script
