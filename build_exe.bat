@echo off
REM ============================================================
REM MarginMise Windows .exe Build Script
REM ============================================================
REM Prerequisites: Python 3.12, pip
REM This script builds a standalone .exe in dist/
REM ============================================================

cd /d "%~dp0"

echo ========================================
echo MarginMise Windows Build
echo ========================================
echo.

REM Create virtual environment
echo [1/5] Creating virtual environment...
python -m venv .buildvenv
call .buildvenv\Scripts\activate

REM Install dependencies
echo [2/5] Installing dependencies...
pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller pillow

REM Build with PyInstaller
echo [3/5] Building executable with PyInstaller...
pyinstaller marginmise.spec

REM Test the executable
echo [4/5] Build complete.
if exist "dist\MarginMise.exe" (
    echo.
    echo ========================================
    echo BUILD SUCCESSFUL!
    echo ========================================
    echo Output: dist\MarginMise.exe
    echo.
    echo To distribute:
    echo   1. Copy dist\MarginMise.exe to any Windows PC
    echo   2. The packaged GUI starts directly; it does not install Python or create a venv.
    echo   3. OCR and CostPilot runtimes start only when those features are used.
    echo.
) else (
    echo.
    echo ========================================
    echo BUILD FAILED!
    echo ========================================
    echo Check the PyInstaller output above for errors.
)

REM Cleanup
echo [5/5] Cleaning up build venv...
deactivate
rmdir /s /q .buildvenv 2>nul

echo.
echo Done.
pause
