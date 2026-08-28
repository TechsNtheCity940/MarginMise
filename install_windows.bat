@echo off
REM ============================================================
REM MarginMise Windows Bootstrapper
REM ============================================================
REM This source installer is for developers. Packaged users should run
REM MarginMise.exe from the PyInstaller build or NSIS installer.
REM ============================================================

setlocal
cd /d "%~dp0"

echo ========================================
echo MarginMise Setup
echo ========================================
echo.

REM Create Logs directory
if not exist "Logs" mkdir "Logs"

REM Run source bootstrap setup (never used by the packaged GUI executable)
echo [Setup] Starting installation...
python -m bootstrap

if errorlevel 1 (
    echo.
    echo Installation encountered an error. Check Logs\bootstrap.log for details.
    pause
    exit /b 1
)

echo.
echo ========================================
echo Setup Complete!
echo ========================================
echo.
echo MarginMise is ready to use.
echo.
echo Run MarginMise.exe to start the application.
echo.
pause
endlocal
