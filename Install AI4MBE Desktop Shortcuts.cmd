@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\install_shortcuts.ps1"
set "rc=%ERRORLEVEL%"
if not "%rc%"=="0" (
  echo.
  echo Shortcut installation failed.
  pause
)
exit /b %rc%
