@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PYTHON=python"
set "VENV=.buildvenv"
set "DIST=%CD%\dist\MarginMise"
set "NSIS_EXE=C:\Program Files (x86)\NSIS\makensis.exe"
if not exist "%NSIS_EXE%" set "NSIS_EXE=C:\Program Files\NSIS\makensis.exe"
if not exist "%VENV%\Scripts\python.exe" set "PYTHON=%VENV%\Scripts\python.exe"

echo ========================================
echo MarginMise Windows Installer Build
echo ========================================
echo Project folder: %CD%
echo Expected dist:  %DIST%

REM --- Step 1: Build folder-based EXE ---
echo [1/3] Building application folder...
if not exist "%VENV%\Scripts\python.exe" (
    echo Creating build venv...
    "%PYTHON%" -m venv "%VENV%"
)
call "%VENV%\Scripts\activate.bat"
echo Installing build deps...
pip install -q --disable-pip-version-check pyinstaller==6.13.0 Pillow
echo Running PyInstaller...
pyinstaller --distpath "%CD%\dist" marginmise_dir.spec --clean -y
if errorlevel 1 (
    echo BUILD FAILED at PyInstaller
    pause
    exit /b 1
)

REM --- Step 2: Verify dist folder exists ---
echo [2/3] Verifying build output...
echo Contents of dist folder:
dir /b "%CD%\dist\" 2>nul || echo (dist folder missing)
if exist "%DIST%" (
    echo Contents of %DIST%:
    dir /b "%DIST%" 2>nul || echo (empty or missing)
) else (
    echo ERROR: %DIST% not found
    echo PyInstaller may have built to a different path
    pause
    exit /b 1
)
if not exist "%DIST%\MarginMise.exe" (
    echo ERROR: %DIST%\MarginMise.exe not found
    echo Please check PyInstaller output above
    pause
    exit /b 1
)
echo Build output verified: %DIST%

REM --- Step 3: Build NSIS installer ---
echo [3/3] Building NSIS installer...
if not exist "%NSIS_EXE%" (
    echo NSIS not found, installing via winget...
    winget install NSIS.NSIS --accept-source-agreements --accept-package-agreements --silent
    timeout /t 15 /nobreak >nul
    if not exist "%NSIS_EXE%" set "NSIS_EXE=C:\Program Files\NSIS\makensis.exe"
)
if not exist "%NSIS_EXE%" (
    echo NSIS install failed
    pause
    exit /b 1
)
"%NSIS_EXE%" /V2 installer\marginmise.nsi
if errorlevel 1 (
    echo INSTALLER BUILD FAILED
    pause
    exit /b 1
)

echo.
echo ========================================
echo BUILD SUCCESSFUL!
echo ========================================
echo Output: MarginMise-Installer.exe
echo.
echo To distribute:
echo   1. Copy MarginMise-Installer.exe to any Windows PC
echo   2. Double-click to install
echo   3. Creates desktop shortcut automatically
echo.
pause
