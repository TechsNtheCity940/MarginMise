@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo MarginMise Windows Build (folder mode)
echo ========================================
echo.
echo This creates a folder-based build instead of a single EXE.
echo It uses less memory and is less likely to freeze.
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

REM Build folder-based version
echo.
echo [3/5] Building folder-based executable...
echo This mode uses less memory than onefile mode.
echo.

pyinstaller marginmise_dir.spec

REM Test the executable
echo.
echo [4/5] Build complete.
if exist "dist\MarginMise\MarginMise.exe" (
    echo.
    echo ========================================
    echo BUILD SUCCESSFUL!
    echo ========================================
    echo Output: dist\MarginMise\ folder
    echo.
    echo To distribute:
    echo   1. Copy the entire dist\MarginMise\ folder to any Windows PC
    echo   2. Run MarginMise.exe from that folder
    echo   3. On first run, it will install prerequisites automatically
    echo.
    echo TIP: You can zip the folder for easy distribution
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
