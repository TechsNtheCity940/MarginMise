@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo MarginMise Windows Installer Build
echo ========================================
echo.
echo This creates a professional Windows installer.
echo.

REM Check if NSIS is installed
where makensis >nul 2>&1
if errorlevel 1 (
    echo NSIS not found. Installing NSIS...
    winget install --id NSIS.NSIS --exact --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo ERROR: Could not install NSIS automatically.
        echo Please install NSIS from https://nsis.sourceforge.io/
        pause
        exit /b 1
    )
)

REM Build folder-based EXE first
echo [1/4] Building application...
call build_exe_dir.bat
if errorlevel 1 (
    echo Build failed!
    pause
    exit /b 1
)

REM Create installer
echo.
echo [2/4] Creating Windows installer...
makensis /V2 installer\marginmise.nsi
if errorlevel 1 (
    echo Installer build failed!
    pause
    exit /b 1
)

REM Verify output
echo.
echo [3/4] Verifying output...
if exist "MarginMise-Installer.exe" (
    echo.
    echo ========================================
    echo INSTALLER BUILD SUCCESSFUL!
    echo ========================================
    echo.
    echo Output: MarginMise-Installer.exe
    echo.
    echo This installer:
    echo   - Creates a professional Windows installer
    echo   - Installs to %%LOCALAPPDATA%%\MarginMise
    echo   - Creates desktop shortcut
    echo   - Creates Start Menu entry
    echo   - Adds Add/Remove Programs entry
    echo   - Includes uninstaller
    echo.
    echo To deploy:
    echo   1. Copy MarginMise-Installer.exe to any Windows PC
    echo   2. Double-click to install
    echo   3. Launch from desktop or Start Menu
    echo.
    for %%F in ("MarginMise-Installer.exe") do echo File size: %%~zF bytes (%%~zF / 1024 / 1024 MB)
    echo.
) else (
    echo.
    echo ========================================
    echo INSTALLER BUILD FAILED!
    echo ========================================
    echo Check the NSIS output above for errors.
)

echo.
echo [4/4] Done.
pause
endlocal
