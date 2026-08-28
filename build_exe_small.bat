@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo MarginMise Windows Build (low-memory folder mode)
echo ========================================
echo.
echo This build uses the onedir spec to avoid onefile extraction overhead.
echo.

REM Create virtual environment
echo [1/5] Creating build environment...
python -m venv .buildvenv
call .buildvenv\Scripts\activate

REM Install dependencies
echo [2/5] Installing build dependencies...
pip install --disable-pip-version-check --no-input --upgrade pip
pip install --disable-pip-version-check --no-input -r requirements.txt
pip install --disable-pip-version-check --no-input pyinstaller==6.13.0 pillow

REM Build with the production onedir spec. The executable entry point is launch_gui.py.
echo.
echo [3/5] Building folder-based executable...
echo Using PyInstaller 6.13.0 with the low-memory onedir layout.
echo.

python -m PyInstaller marginmise_dir.spec --noconfirm --clean

REM Test the executable
echo.
echo [4/5] Build complete.
if exist "dist\MarginMise\MarginMise.exe" (
    echo.
    echo ========================================
    echo BUILD SUCCESSFUL!
    echo ========================================
    echo Output: dist\MarginMise\MarginMise.exe
    echo.
    echo To distribute:
    echo   1. Copy dist\MarginMise.exe to any Windows PC
    echo   2. On first run, it will install prerequisites automatically
    echo   3. No Python installation needed on target machine
    echo.
) else (
    echo.
    echo ========================================
    echo BUILD FAILED!
    echo ========================================
    echo Check the PyInstaller output above for errors.
)

REM Cleanup
echo [5/5] Cleaning up build environment...
deactivate
rmdir /s /q .buildvenv 2>nul

echo.
echo Done.
pause
endlocal
