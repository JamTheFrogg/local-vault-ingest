# Installs vault-ingest into a private Python environment (.venv) next to this folder.
# Run from PowerShell:   powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Find-Python {
    # py/python write to stderr when a version is missing; under "Stop" that aborts PowerShell 5.1
    $ErrorActionPreference = "Continue"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in "3.13", "3.12", "3.11") {
            & py "-$v" -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { return @("py", "-$v") }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return @("python") }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.11 or newer was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
    exit 1
}
$exe = $py[0]
$rest = @(); if ($py.Length -gt 1) { $rest = $py[1..($py.Length - 1)] }

Write-Host "Creating .venv with $($py -join ' ') ..."
& $exe @rest -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -e ".[dev]"

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env - open it and fill in VAULT_PATH and the AI settings." -ForegroundColor Yellow
}

Write-Host "Running self-tests..."
& .\.venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -eq 0) { Write-Host "Installed OK." -ForegroundColor Green }
