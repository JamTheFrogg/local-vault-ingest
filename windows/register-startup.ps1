# Registers four background tasks that start when you log in to Windows:
#   1. "Obsidian MCP server"  - the shared search/read/write server for all your AIs
#   2. "LM Studio server"     - `lms server start`, hidden; search in the AI apps uses it too
#   3. "Vault ingest watcher" - processes transcripts dropped into 00_Inbox
#   4. "Meeting companion"    - detects meetings, offers Meetily, collects screenshots, exports Meetily transcripts
# Run once from PowerShell:  powershell -ExecutionPolicy Bypass -File .\windows\register-startup.ps1
# Remove later with:         Unregister-ScheduledTask -TaskName "Obsidian MCP server","LM Studio server","Vault ingest watcher","Meeting companion"
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$vbs = Join-Path $PSScriptRoot "hidden.vbs"
$mcpCmd = Join-Path $PSScriptRoot "start-obsidian-mcp.cmd"
$lmsCmd = Join-Path $PSScriptRoot "start-lm-studio.cmd"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"

if (Select-String -Path $mcpCmd -Pattern "__VAULT_PATH__" -Quiet) {
    Write-Host "Edit windows\start-obsidian-mcp.cmd first (set OBSIDIAN_VAULT_PATH)." -ForegroundColor Red; exit 1
}
if (-not (Test-Path (Join-Path $root "bin\obsidian-mcp.exe")) -and -not (Test-Path "$env:USERPROFILE\.cargo\bin\obsidian-mcp.exe")) {
    Write-Host "obsidian-mcp.exe not found. Put it in the 'bin' folder (see SETUP.md step 2)." -ForegroundColor Red; exit 1
}
if (-not (Test-Path $pythonw)) {
    Write-Host "Run windows\install.ps1 first." -ForegroundColor Red; exit 1
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

$a1 = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`" `"$mcpCmd`""
Register-ScheduledTask -TaskName "Obsidian MCP server" -Action $a1 -Trigger $trigger -Settings $settings -Force | Out-Null

$aLms = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`" `"$lmsCmd`""
Register-ScheduledTask -TaskName "LM Studio server" -Action $aLms -Trigger $trigger -Settings $settings -Force | Out-Null

$a2 = New-ScheduledTaskAction -Execute $pythonw -Argument "-m vault_ingest watch" -WorkingDirectory $root
Register-ScheduledTask -TaskName "Vault ingest watcher" -Action $a2 -Trigger $trigger -Settings $settings -Force | Out-Null

# pythonw has no console window, so the companion runs hidden; it logs to <vault>\_System\companion.log
$a3 = New-ScheduledTaskAction -Execute $pythonw -Argument "-m vault_ingest companion" -WorkingDirectory $root
Register-ScheduledTask -TaskName "Meeting companion" -Action $a3 -Trigger $trigger -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskName "Obsidian MCP server"
Start-ScheduledTask -TaskName "LM Studio server"
Start-ScheduledTask -TaskName "Vault ingest watcher"
Start-ScheduledTask -TaskName "Meeting companion"
Start-Sleep -Seconds 5
try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:37842/health" -TimeoutSec 5
    Write-Host "Obsidian MCP server is up ($($r.StatusCode))." -ForegroundColor Green
} catch {
    Write-Host "Obsidian MCP server did not answer yet. Check %LOCALAPPDATA%\obsidian-mcp\obsidian-mcp.log" -ForegroundColor Yellow
}
Write-Host "All four tasks registered. They start automatically at every login."
