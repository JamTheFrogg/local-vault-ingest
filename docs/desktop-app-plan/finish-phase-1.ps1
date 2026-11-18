# Finish the open steps on branch feat/desktop-app in vault-ingest-public.
# Run in PowerShell:
#   powershell -ExecutionPolicy Bypass -File "$HOME\Desktop\Projects\vault-ingest\docs\desktop-app-plan\finish-phase-1.ps1"
# It stops at the first problem and changes nothing on GitHub until the tests pass.

$ErrorActionPreference = "Stop"
$repo   = "$HOME\Desktop\Projects\vault-ingest-public"
$python = "$HOME\AppData\Local\Programs\Python\Python312\python.exe"
Set-Location $repo

# 1. Right branch
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne "feat/desktop-app") { throw "On branch '$branch', expected feat/desktop-app. Run: git switch feat/desktop-app" }

# 2. Own environment for this repo (.venv is git-ignored)
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv ..." -ForegroundColor Cyan
    & $python -m venv .venv
}
Write-Host "Installing (this takes a minute) ..." -ForegroundColor Cyan
& .venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# 3. Tests
Write-Host "Running tests ..." -ForegroundColor Cyan
& .venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed. Nothing was committed. Copy the failures to Claude." }

# 4. The workflow file Claude's file tools may not write
$wf = ".github\workflows\phase0.yml"
New-Item -ItemType Directory -Force (Split-Path $wf) | Out-Null
$yaml = @'
name: Phase 0 kit

# Runs on every push to feat/desktop-app (a manual-only workflow is not offered until it is on main),
# and by hand: Actions > Phase 0 kit > Run workflow. See docs/phase-0-checks.md.
on:
  workflow_dispatch:
  push:
    branches: [feat/desktop-app]

permissions:
  contents: read

jobs:
  recorder-probe:
    runs-on: windows-latest
    defaults:
      run:
        working-directory: tools/recorder-probe
    env:
      # Static C runtime: the .exe runs on a PC without the Visual C++ redistributable.
      RUSTFLAGS: -C target-feature=+crt-static
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - run: cargo test --release --locked
      - run: cargo build --release --locked
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with:
          name: recorder-probe-windows
          path: tools/recorder-probe/target/release/recorder-probe.exe
          if-no-files-found: error

  engine:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - run: python scripts/build_engine.py --out engine
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with:
          name: engine-windows
          path: engine
          if-no-files-found: error
'@
[IO.File]::WriteAllText((Join-Path $repo $wf), ($yaml -replace "`r`n", "`n") + "`n")   # UTF-8 without BOM, LF

# 5. Commit and push (only files git does not ignore)
git add -A
Write-Host "`nFiles in the commit:" -ForegroundColor Cyan
git status --short
git commit -q -m "Desktop app groundwork: engine API (serve), cross-platform companion, phase 0 kit"
if ($LASTEXITCODE -ne 0) { throw "git commit failed" }
git push -u origin feat/desktop-app
if ($LASTEXITCODE -ne 0) { throw "git push failed (the commit is saved locally)" }

Write-Host "`nDone. The push starts the 'Phase 0 kit' workflow: GitHub > Actions. When it is green, download the two artifacts and follow docs\phase-0-checks.md." -ForegroundColor Green
