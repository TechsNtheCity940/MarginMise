@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "Logs" mkdir "Logs"
set "STARTUP_LOG=%~dp0Logs\startup.log"
set "ERROR_LOG=%~dp0Logs\startup_error.log"

>"%STARTUP_LOG%" echo [%date% %time%] Starting MarginMise v3.5 CostPilot Review Automation
>>"%STARTUP_LOG%" echo Application folder: %~dp0

set "MM_INSTALL=%LOCALAPPDATA%\MarginMise"
set "MM_PYTHON=%MM_INSTALL%\.venv\Scripts\python.exe"

if not exist "%MM_PYTHON%" (
  echo Python environment not found. Preparing MarginMise...
  >>"%STARTUP_LOG%" echo Python environment not found. Running installer.
  call "%~dp0install_windows.bat" --silent
  if errorlevel 1 goto :failed
)

if not exist "%MM_PYTHON%" (
  >>"%STARTUP_LOG%" echo Installer completed but virtual environment is still missing: %MM_PYTHON%
  goto :failed
)

>>"%STARTUP_LOG%" echo Launching GUI with %MM_PYTHON% from source directory.
cd /d "%~dp0"
"%MM_PYTHON%" "%~dp0launch_gui.py" >>"%STARTUP_LOG%" 2>&1
if errorlevel 1 goto :failed

exit /b 0

:failed
echo.
echo MarginMise could not start.
echo A diagnostic log was written to:
echo   %STARTUP_LOG%
if exist "%ERROR_LOG%" echo   %ERROR_LOG%
echo.
if exist "%STARTUP_LOG%" type "%STARTUP_LOG%"
echo.
pause
exit /b 1
