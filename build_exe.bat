@echo off
REM ============================================================
REM MarginMise Windows .exe Build Script
REM ============================================================
REM Prerequisites: Python 3.12, pip
REM This script builds a low-memory folder-based .exe in dist/MarginMise/
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
python -m PyInstaller marginmise_dir.spec --noconfirm --clean

REM Test the executable
echo [4/5] Running packaged startup smoke test...
if exist "dist\MarginMise\MarginMise.exe" (
    "dist\MarginMise\MarginMise.exe" --startup-check
    if errorlevel 1 (
        echo.
        echo ========================================
        echo BUILD FAILED: STARTUP SMOKE TEST FAILED!
        echo ========================================
        echo The executable was created but could not initialize the GUI runtime.
        exit /b 1
    )
    echo.
    echo ========================================
    echo BUILD SUCCESSFUL!
    echo ========================================
    echo Output: dist\MarginMise\MarginMise.exe
    echo.
    echo To distribute:
    echo   1. Copy the entire dist\MarginMise folder to any Windows PC
    echo   2. The packaged GUI starts directly; it does not install Python or create a venv.
    echo   3. OCR and CostPilot runtimes start only when those features are used.
    echo.
) else (
    echo.
    echo ========================================
    echo BUILD FAILED!
    echo ========================================
    echo Check the PyInstaller output above for errors.
    exit /b 1
)

REM Cleanup
echo [5/5] Cleaning up build venv...
deactivate
rmdir /s /q .buildvenv 2>nul

echo.
echo Done.
pause
