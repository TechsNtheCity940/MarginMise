@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo MarginMise Windows Installer Build
echo ========================================
echo.

REM Ensure NSIS is on PATH for this session
set "PATH=%PATH%;C:\Program Files (x86)\NSIS;C:\Program Files\NSIS"

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
    set "PATH=%PATH%;C:\Program Files (x86)\NSIS;C:\Program Files\NSIS"
    where makensis >nul 2>&1
    if errorlevel 1 (
        echo ERROR: NSIS installed but makensis still not found.
        echo Please open a NEW command prompt and run build_installer.bat again.
        pause
        exit /b 1
    )
)

REM Build folder-based EXE
echo [1/4] Building application...
call build_exe_dir.bat
if errorlevel 1 (
    echo Build failed!
    pause
    exit /b 1
)

REM Verify build output
echo.
echo [2/4] Verifying build output...
if not exist "dist\MarginMise\MarginMise.exe" (
    echo ERROR: dist\MarginMise\MarginMise.exe not found
    pause
    exit /b 1
)

REM Create installer output folder
echo.
echo [3/4] Creating installer package...
if not exist "deploy" mkdir deploy
if exist "deploy\MarginMise" rmdir /s /q deploy\MarginMise

REM Copy build output and launcher files
xcopy /E /I /Y "dist\MarginMise\*" "deploy\MarginMise\"
copy /Y "run_marginmise.bat" "deploy\MarginMise\" >nul 2>&1
copy /Y "README.md" "deploy\MarginMise\" >nul 2>&1

REM Build NSIS installer from the correct folder
echo.
echo [4/4] Building installer EXE...
makensis /V2 installer\marginmise.nsi
if errorlevel 1 (
    echo.
    echo Installer build failed!
    pause
    exit /b 1
)

REM Verify installer output
echo.
echo Verifying installer...
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
echo Done.
pause
endlocal
