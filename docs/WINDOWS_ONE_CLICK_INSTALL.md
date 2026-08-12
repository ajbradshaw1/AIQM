# AI4MBE Windows one-click installation

## Install

1. Download the `AI4MBE-Growth-Monitor-*-Windows-x64.zip` asset from the GitHub
   Release. Do not download GitHub's automatically generated source archive.
2. Extract the ZIP completely.
3. Double-click **Install AI4MBE Growth Monitor.cmd**.
4. Choose an installation parent folder. The installer creates an
   `AI4MBE-Growth-Monitor` folder inside it. Cancel leaves the computer
   unchanged.
5. Start the correct chamber from the new Desktop shortcut:
   **O-MBE Growth Monitor** or **Ch-MBE Growth Monitor**.

This release supports **64-bit Windows and 64-bit Python only**. The installer
fails closed on a 32-bit operating system or interpreter. It is per-user and
does not require administrator privileges when the selected location is
user-writable. The folder picker initially suggests `%LOCALAPPDATA%\Programs`,
but another writable folder or drive can be selected. It first reuses a compatible
`ai4mbe-gui` Python/Conda environment. If none exists, it creates an isolated
`.venv` in the application directory and installs the pinned Windows
requirements. The fallback requires Python 3.10–3.12 and internet access.

For unattended use, `-InstallRoot` specifies the exact absolute destination.
Alternatively, `-InstallParent` specifies a parent directory and the installer
appends `AI4MBE-Growth-Monitor`. These parameters bypass the folder picker.

The validated application stack requires **64-bit Windows and 64-bit Python**.
If a 64-bit computer currently has only 32-bit Python, the installer ignores
that interpreter and uses another 64-bit environment. A genuinely 32-bit
Windows installation is rejected before files are copied because supported
32-bit builds of the PyTorch/PyQt6/WGC combination are not available.

Experiment sessions are stored separately under
`Documents\AI4MBE\GrowthSessions`. Launcher diagnostics remain under
`%LOCALAPPDATA%\AI4MBE\LauncherLogs`.

## Uninstall

Double-click the Desktop shortcut **Uninstall AI4MBE Growth Monitor** or the
same-named `.cmd` file in the installation directory. The uninstaller removes
the installed application and its Desktop shortcuts. It intentionally keeps:

- `Documents\AI4MBE\GrowthSessions`
- `%LOCALAPPDATA%\AI4MBE\LauncherLogs`
- `%LOCALAPPDATA%\AI4MBE\InstallerLogs`

Delete those data directories manually only after making and verifying the
required experiment backup.

## Updating

Download and extract the newer Release, then run its installer. The installer
replaces only the program directory and leaves the data/log directories in
place. It does not change instrument setpoints or Windows machine-wide
environment variables.

## Offline and instrument-driver limits

The package contains the GUI runtime, manuals, dummy images, and the current
brightness-robust four-output shadow model. Historical experiment logs,
development tests, and duplicated pre-release model bundles are excluded.
Vendor components are not redistributed. Vimba
direct-camera mode still requires the vendor Vimba X SDK and matching `vmbpy`;
ADS operation requires TwinCAT System Service; OCR requires a compatible local
Tesseract installation. The launcher reports missing optional Python drivers
without silently changing acquisition mode.
