@echo off
setlocal
set "ROOT=%~dp0"
if "%~1"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%deploy\launch-windows.ps1" start
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%deploy\launch-windows.ps1" %*
)
exit /b %ERRORLEVEL%
