@echo off
setlocal
powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start_rheed_labeler.ps1"
set "rc=%ERRORLEVEL%"
if not "%rc%"=="0" (
  echo.
  echo RHEED Post-processing Labeler launch failed. See the error dialog and launcher log.
  pause
)
exit /b %rc%
