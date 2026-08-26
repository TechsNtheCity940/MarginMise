@echo off
REM ============================================================
REM MarginMise Windows Bootstrapper
REM ============================================================
REM This installer handles the complete setup process:
REM 1. Find or install Python 3.11+
REM 2. Create virtual environment
REM 3. Install all Python dependencies
REM 4. Silently install Tesseract OCR
REM 5. Download llama.cpp + LFM2.5 model
REM 6. Create shortcuts
REM ============================================================

setlocal
cd /d "%~dp0"

echo ========================================
echo MarginMise Setup
echo ========================================
echo.

REM Create Logs directory
if not exist "Logs" mkdir "Logs"

REM Run the bootstrapper
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
