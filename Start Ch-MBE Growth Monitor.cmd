@echo off
setlocal
powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start_chmbe.ps1"
exit /b %ERRORLEVEL%
