@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\install_ai4mbe_shortcuts.ps1"
set "rc=%ERRORLEVEL%"
if not "%rc%"=="0" pause
exit /b %rc%
