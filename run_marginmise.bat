@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM MarginMise Portable Launcher for Restaurant PCs
REM ============================================================
REM Lightweight launcher - no PyInstaller, no big EXE
REM Uses system Python if available, otherwise installs silently
REM ============================================================

set APP_NAME=MarginMise
set VENV_DIR=.venv
set PYTHON_MIN=3.11

echo ========================================
echo %APP_NAME% Launcher
echo ========================================
echo.

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Installing silently...
    winget install --id Python.Python.3.12 --exact --scope user --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo ERROR: Could not install Python automatically.
        echo Please install Python %PYTHON_MIN%+ from python.org and try again.
        pause
        exit /b 1
    )
    echo Python installed successfully.
    echo.
)

REM Create virtual environment if needed
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Setting up %APP_NAME% environment...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment.
        pause
        exit /b 1
    )
    
    REM Install dependencies
    echo Installing dependencies (this may take a few minutes)...
    "%VENV_DIR%\Scripts\python.exe" -m pip install --disable-pip-version-check --no-input --upgrade pip
    "%VENV_DIR%\Scripts\python.exe" -m pip install --disable-pip-version-check --no-input -r requirements.txt
    if errorlevel 1 (
        echo WARNING: Some dependencies failed to install.
        echo The app may run with reduced functionality.
    )
    
    REM Install Tesseract OCR
    echo.
    echo Installing Tesseract OCR...
    "%VENV_DIR%\Scripts\python.exe" local_ocr.py ensure --install-tesseract
    
    REM Install local AI model
    echo.
    echo Installing AI model (this may take a few minutes)...
    "%VENV_DIR%\Scripts\python.exe" local_ai.py ensure
    
    echo.
    echo ========================================
    echo Setup Complete!
    echo ========================================
    echo.
) else (
    echo %APP_NAME% is already set up.
    echo.
)

REM Launch the application
echo Starting %APP_NAME%...
"%VENV_DIR%\Scripts\python.exe" launch_gui.py

endlocal
