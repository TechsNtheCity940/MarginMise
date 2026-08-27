@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo MarginMise Windows Build (memory-safe)
echo ========================================
echo.

REM Create virtual environment
echo [1/5] Creating build environment...
python -m venv .buildvenv
call .buildvenv\Scripts\activate

REM Install dependencies
echo [2/5] Installing build dependencies...
pip install --disable-pip-version-check --no-input --upgrade pip
pip install --disable-pip-version-check --no-input -r requirements.txt
pip install --disable-pip-version-check --no-input pyinstaller pillow

REM Check available memory
echo.
echo [3/5] Checking system memory...
python -c "import psutil; mem = psutil.virtual_memory(); print(f'Available RAM: {mem.available / 1024**2:.0f} MB'); print(f'Total RAM: {mem.total / 1024**2:.0f} MB')" 2>nul
if errorlevel 1 (
    echo WARNING: psutil not available, skipping memory check
)

REM Build with UPX disabled to reduce memory pressure
echo.
echo [4/5] Building executable (UPX disabled, this may take 10-20 minutes)...
echo NOTE: If the build freezes for more than 30 minutes, press Ctrl+C and try build_exe_dir.bat instead
echo.

pyinstaller marginmise.spec --upx-dir=none

REM Test the executable
echo.
echo [5/5] Build complete.
if exist "dist\MarginMise.exe" (
    echo.
    echo ========================================
    echo BUILD SUCCESSFUL!
    echo ========================================
    echo Output: dist\MarginMise.exe
    echo.
    echo To distribute:
    echo   1. Copy dist\MarginMise.exe to any Windows PC
    echo   2. On first run, it will:
    echo      - Install Python if needed
    echo      - Create a virtual environment
    echo      - Install all dependencies
    echo      - Download Tesseract OCR (silent)
    echo      - Download the local AI model
    echo   3. No Python installation needed on target machine
    echo.
) else (
    echo.
    echo ========================================
    echo BUILD FAILED!
    echo ========================================
    echo Check the PyInstaller output above for errors.
    echo.
    echo TIP: If the build keeps freezing, try build_exe_dir.bat instead
    echo      (creates a folder-based build instead of single EXE)
)

REM Cleanup
echo Cleaning up build environment...
deactivate
rmdir /s /q .buildvenv 2>nul

echo.
echo Done.
pause
endlocal
