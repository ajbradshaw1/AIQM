@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\launch_ai4mbe.ps1" -Application labeler
set "rc=%ERRORLEVEL%"
if not "%rc%"=="0" (
  echo.
  echo Launch failed. See the error dialog and launcher log.
  pause
)
exit /b %rc%
