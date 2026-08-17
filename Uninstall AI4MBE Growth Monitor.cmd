@echo off
setlocal
set "uninstaller=%~dp0scripts\windows\uninstall_ai4mbe.ps1"
cd /d "%TEMP%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%uninstaller%"
set "rc=%ERRORLEVEL%"
if not "%rc%"=="0" (
  echo.
  echo Uninstallation failed. See the error dialog and uninstaller log.
  pause
)
exit /b %rc%
