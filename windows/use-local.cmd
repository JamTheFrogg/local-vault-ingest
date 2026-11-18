@echo off
rem Switch vault-ingest to local mode (LM Studio) and restart the background watcher so it applies now.
setlocal
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m vault_ingest mode local
set "RC=%ERRORLEVEL%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-ScheduledTask -TaskName 'Vault ingest watcher' -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName 'Vault ingest watcher'; Start-ScheduledTask -TaskName 'Vault ingest watcher'; Write-Host 'Restarted the Vault ingest watcher.' } else { Write-Host 'The Vault ingest watcher task is not registered (see windows\register-startup.ps1). The mode was still changed.' }"
rem keep the window open so the result is readable when double-clicked; pass /nopause to skip
if /i not "%~1"=="/nopause" pause
exit /b %RC%
