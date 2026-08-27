@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo MarginMise Windows Installer Build
echo ========================================

REM --- Step 1: Build folder-based EXE ---
echo [1/3] Building application folder...
if not exist ".buildvenv\Scripts\activate.bat" (
    echo Creating build venv...
    python -m venv .buildvenv
    .buildvenv\Scripts\activate.bat
    pip install -r requirements.txt pyinstaller==6.13.0
)
.\.buildvenv\Scripts\activate.bat
pyinstaller marginmise_dir.spec --clean -y
if errorlevel 1 (
    echo BUILD FAILED
    pause
    exit /b 1
)

REM --- Step 2: Copy logo and assets ---
echo [2/3] Packaging installer assets...
if not exist "deploy\MarginMise" mkdir "deploy\MarginMise"
xcopy /E /I /Y "dist\MarginMise\*" "deploy\MarginMise\"
xcopy /E /I /Y "assets\*" "deploy\MarginMise\assets\"

REM --- Step 3: Build NSIS installer ---
echo [3/3] Building NSIS installer...
set NSIS="C:\Program Files (x86)\NSIS\makensis.exe"
if not exist %NSIS% set NSIS="C:\Program Files\NSIS\makensis.exe"
if not exist %NSIS% (
    echo NSIS not found, installing via winget...
    winget install NSIS.NSIS --accept-source-agreements --accept-package-agreements
    timeout /t 10 /nobreak >nul
    set NSIS="C:\Program Files (x86)\NSIS\makensis.exe"
    if not exist %NSIS% set NSIS="C:\Program Files\NSIS\makensis.exe"
)
%NSIS% /V2 installer\marginmise.nsi
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
