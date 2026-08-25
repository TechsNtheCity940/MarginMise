param(
    [switch]$RunSetup,
    [switch]$ForceProfile,
    [switch]$Silent
)

$ErrorActionPreference = 'Stop'
if ($Silent) { $ProgressPreference = 'SilentlyContinue' }
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir

function Find-Hermes {
    $command = Get-Command hermes -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\hermes.exe",
        "$env:LOCALAPPDATA\hermes\bin\hermes.cmd"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Find-Python {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { return @($launcher.Source, '-3') }
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { return @($pythonCommand.Source) }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return @($candidate) }
    }
    return $null
}

function Install-Python {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { return }
    Write-Host 'Python was not found. Installing the supported per-user Python runtime...'
    & $winget.Source install --id Python.Python.3.11 --exact --scope user --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)] [array] $Python,
        [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Arguments
    )
    if ($Python.Count -eq 2) {
        & $Python[0] $Python[1] @Arguments
    } else {
        & $Python[0] @Arguments
    }
}

# Build the application environment independently of Hermes. OCR and all
# operational workflows must remain usable even when an AI provider is down.
$Python = Find-Python
if (-not $Python) {
    Install-Python
    $Python = Find-Python
}
if (-not $Python) {
    throw 'Python 3.11 or newer is required and could not be installed automatically.'
}

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    Write-Host 'Creating the MarginMise Python environment...'
    Invoke-Python -Python $Python -Arguments @('-m', 'venv', '.venv')
}

& '.venv\Scripts\python.exe' -m pip install --disable-pip-version-check --no-input --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'The MarginMise package installer could not be updated.' }
& '.venv\Scripts\python.exe' -m pip install --disable-pip-version-check --no-input -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'MarginMise Python dependencies could not be installed.' }

Write-Host 'Preparing on-demand local OCR...'
& '.venv\Scripts\python.exe' local_ocr.py ensure --install-tesseract
if ($LASTEXITCODE -ne 0) {
    throw 'Neither RapidOCR nor Tesseract could be prepared. Review Logs\install.log.'
}

Write-Host 'Preparing local CostPilot (pinned LFM2.5 Q4 and llama.cpp runtime)...'
& '.venv\Scripts\python.exe' local_ai.py ensure
if ($LASTEXITCODE -ne 0) {
    throw 'The local CostPilot model could not be installed or verified. Review Logs\install.log.'
}

# Existing Hermes installations remain available only for an explicitly
# configured OpenRouter cloud fallback. New installations do not require it.
$Hermes = Find-Hermes

Write-Host ''
Write-Host 'MarginMise installation completed.'
Write-Host 'RapidOCR is installed locally and runs only while a scan is being processed.'
Write-Host 'CostPilot uses the local LFM2.5 Q4 model and loads it only while answering.'
Write-Host 'OpenRouter is optional and is not required for OCR or normal CostPilot use.'

if ($RunSetup -and -not $Silent -and $Hermes) {
    Write-Host ''
    Write-Host 'Opening one-time provider authorization.'
    & $Hermes -p restaurant-cost-controller model
}

Write-Host ''
Write-Host 'Run run_gui.bat to open MarginMise.'
