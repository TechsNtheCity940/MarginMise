@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo MarginMise Windows Build (small onefile)
echo ========================================
echo.
echo This build uses a minimal spec to avoid memory/DLL errors.
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

REM Build with minimal spec
echo.
echo [3/5] Building small onefile executable...
echo Using PyInstaller 6.13.0 with minimal hooks.
echo.

pyinstaller --onefile --windowed ^
  --name "MarginMise" ^
  --icon assets/app_icon_256.png ^
  --add-data "assets;assets" ^
  --hidden-import invoice_pipeline ^
  --hidden-import bulk_ingestion ^
  --hidden-import recipe_costing ^
  --hidden-import margin_memory ^
  --hidden-import manager_chat ^
  --hidden-import local_ai ^
  --hidden-import local_ocr ^
  --hidden-import inventory_planning ^
  --hidden-import phase2_features ^
  --hidden-import phase3_features ^
  --hidden-import operational_controls ^
  --hidden-import excel_io ^
  --hidden-import dashboard_service ^
  --hidden-import dashboard_widgets ^
  --hidden-import review_copilot ^
  --hidden-import launch_gui ^
  --hidden-import restaurant_cost_gui ^
  --hidden-import manager_first_gui ^
  --hidden-import events ^
  --hidden-import shift_reports ^
  --hidden-import weekly_invoice_log ^
  --hidden-import src.theme ^
  --hidden-import bootstrap ^
  --exclude-module hermes_agent ^
  --exclude-module hermes_backend ^
  --exclude-module hermes ^
  --exclude-module tkinter ^
  --exclude-module matplotlib ^
  --exclude-module PyQt5 ^
  --exclude-module PyQt6 ^
  --exclude-module PySide2 ^
  --exclude-module PySide6 ^
  --exclude-module numpy ^
  --exclude-module pandas ^
  --exclude-module scipy ^
  --exclude-module sklearn ^
  --exclude-module torch ^
  --exclude-module tensorflow ^
  --clean ^
  bootstrap.py

REM Test the executable
echo.
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
