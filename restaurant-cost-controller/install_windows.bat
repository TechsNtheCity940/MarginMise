@echo off
setlocal
cd /d "%~dp0"
if /I "%~1"=="--silent" (
  if not exist "Logs" mkdir "Logs"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1" -Silent >>"%~dp0Logs\install.log" 2>&1
  exit /b %errorlevel%
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1"
if errorlevel 1 (
  echo.
  echo Installation failed. Review the error above.
  pause
  exit /b 1
)
echo.
pause
endlocal
