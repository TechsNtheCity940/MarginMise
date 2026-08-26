param(
    [switch]$RunSetup,
    [switch]$ForceProfile,
    [switch]$Silent
)

$ErrorActionPreference = 'Stop'
if ($Silent) { $ProgressPreference = 'SilentlyContinue' }
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir

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

# Build the application environment independently — no external AI provider
# or cloud service is required. OCR and CostPilot both run locally.
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
    Write-Host '  WARNING: Tesseract could not be installed. RapidOCR will be used for OCR.'
}

Write-Host 'Preparing local CostPilot (pinned LFM2.5 Q4 and llama.cpp runtime)...'
& '.venv\Scripts\python.exe' local_ai.py ensure
if ($LASTEXITCODE -ne 0) {
    Write-Host '  WARNING: Local CostPilot runtime could not be downloaded (offline or network issue).'
    Write-Host '  The app will use deterministic computed answers until CostPilot is installed.'
    Write-Host '  Run python local_ai.py ensure later to retry the download.'
}

Write-Host ''
Write-Host 'MarginMise installation completed.'
Write-Host 'RapidOCR is installed locally and runs only while a scan is being processed.'
Write-Host 'CostPilot uses the local LFM2.5 Q4 model and loads it only while answering.'
Write-Host ''
Write-Host 'Run run_gui.bat to open MarginMise.'
