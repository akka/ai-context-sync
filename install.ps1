# install.ps1 — Install claude-context-sync on Windows
#
# Usage (run as your normal user in PowerShell):
#   .\install.ps1 -Key "YOUR_API_KEY"
#   .\install.ps1 -Key "YOUR_API_KEY" -Url "https://claude-contexts.akka.io"
#
# The API key is provided by IT/DevEx — it is NOT a GitHub token.
#
# Requirements: Python 3.8+  (https://python.org/downloads)

param(
    [string]$Key = "",
    [string]$Url = "https://claude-contexts.akka.io"
)

$ErrorActionPreference = "Stop"

$ScriptName   = "sync_claude_contexts.py"
$WatcherName  = "watch_cowork_sessions.py"
$ClaudeDir    = Join-Path $env:USERPROFILE ".claude"
$InstallPath  = Join-Path $ClaudeDir $ScriptName
$WatcherPath  = Join-Path $ClaudeDir $WatcherName
$ConfigFile   = Join-Path $ClaudeDir "context-sync.conf"
$LogFile      = Join-Path $ClaudeDir "context-sync.log"
$WatcherLog   = Join-Path $ClaudeDir "cowork-watcher.log"
$TaskName     = "ClaudeContextSync"
$WatcherTask  = "ClaudeCoworkWatcher"

function Info  { param($m) Write-Host "  [INFO]  $m" -ForegroundColor Cyan }
function Warn  { param($m) Write-Host "  [WARN]  $m" -ForegroundColor Yellow }
function Fail  { param($m) Write-Host "  [ERROR] $m" -ForegroundColor Red; exit 1 }

# ── Python check ──────────────────────────────────────────────────────────────
function Find-Python {
    foreach ($cmd in @("python", "python3", "py")) {
        try {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 8) {
                    return (Get-Command $cmd).Source
                }
            }
        } catch {}
    }
    Fail "Python 3.8+ is required. Download from https://python.org/downloads"
}

Write-Host ""
Write-Host "══════════════════════════════════════════" -ForegroundColor Green
Write-Host "  Claude Context Sync — Windows Installer" -ForegroundColor Green
Write-Host "══════════════════════════════════════════" -ForegroundColor Green
Write-Host ""

$PythonPath = Find-Python
Info "Using Python: $PythonPath"

# ── Create ~/.claude ──────────────────────────────────────────────────────────
if (-not (Test-Path $ClaudeDir)) {
    New-Item -ItemType Directory -Path $ClaudeDir | Out-Null
}

# ── Copy or download script ───────────────────────────────────────────────────
$LocalScript = Join-Path $PSScriptRoot $ScriptName
if (Test-Path $LocalScript) {
    Info "Copying $ScriptName from current directory…"
    Copy-Item $LocalScript $InstallPath -Force
} else {
    Info "Downloading $ScriptName from GitHub…"
    $RawUrl = "https://raw.githubusercontent.com/akka/ai-context-sync/main/$ScriptName"
    try {
        Invoke-WebRequest -Uri $RawUrl -OutFile $InstallPath -UseBasicParsing
    } catch {
        Fail "Download failed: $_"
    }
}
Info "Installed script to $InstallPath"

# ── Copy or download cowork watcher ──────────────────────────────────────────
$LocalWatcher = Join-Path $PSScriptRoot $WatcherName
if (Test-Path $LocalWatcher) {
    Info "Copying $WatcherName from current directory…"
    Copy-Item $LocalWatcher $WatcherPath -Force
} else {
    Info "Downloading $WatcherName from GitHub…"
    $WatcherUrl = "https://raw.githubusercontent.com/akka/ai-context-sync/main/$WatcherName"
    try {
        Invoke-WebRequest -Uri $WatcherUrl -OutFile $WatcherPath -UseBasicParsing
        Info "Installed watcher to $WatcherPath"
    } catch {
        Warn "Could not download ${WatcherName} — cowork session sync unavailable."
    }
}

# ── Write config file ─────────────────────────────────────────────────────────
if ($Key -ne "") {
    Set-Content -Path $ConfigFile -Value @"
SOURCE_URL=$Url
CONTEXT_API_KEY=$Key
"@ -Encoding UTF8
    Info "Saved config to $ConfigFile"
} elseif (-not (Test-Path $ConfigFile)) {
    Set-Content -Path $ConfigFile -Value @"
# Claude Context Sync configuration
# Contact IT for your CONTEXT_API_KEY — do NOT share it.
SOURCE_URL=$Url
CONTEXT_API_KEY=YOUR_KEY_HERE
"@ -Encoding UTF8
    Warn "API key not provided — edit $ConfigFile and set CONTEXT_API_KEY."
}

# Restrict config file permissions to current user only
try {
    $acl = Get-Acl $ConfigFile
    $acl.SetAccessRuleProtection($true, $false)
    $acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $currentUser, "FullControl", "Allow"
    )
    $acl.AddAccessRule($rule)
    Set-Acl -Path $ConfigFile -AclObject $acl
} catch {
    Warn "Could not restrict config file permissions: $_"
}

# ── Task Scheduler — daily at 08:00 ──────────────────────────────────────────
Info "Creating Windows Task Scheduler entry ($TaskName)…"

$Action  = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$InstallPath`" >> `"$LogFile`" 2>&1"

$Trigger = New-ScheduledTaskTrigger -Daily -At "08:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action   $Action `
    -Trigger  $Trigger `
    -Settings $Settings `
    -Description "Daily sync of Claude AI context files from akka.io" `
    -RunLevel  Limited | Out-Null

Info "Scheduled task created — runs daily at 08:00."

# ── Task Scheduler — cowork watcher (persistent, restarts on failure) ─────────
if (Test-Path $WatcherPath) {
    Info "Creating cowork watcher task ($WatcherTask)…"

    $WatcherAction = New-ScheduledTaskAction `
        -Execute $PythonPath `
        -Argument "`"$WatcherPath`" >> `"$WatcherLog`" 2>&1"

    # Trigger: at logon, runs indefinitely
    $WatcherTrigger = New-ScheduledTaskTrigger -AtLogOn

    $WatcherSettings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([System.TimeSpan]::Zero) `
        -RestartCount 10 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable

    Unregister-ScheduledTask -TaskName $WatcherTask -Confirm:$false -ErrorAction SilentlyContinue

    Register-ScheduledTask `
        -TaskName    $WatcherTask `
        -Action      $WatcherAction `
        -Trigger     $WatcherTrigger `
        -Settings    $WatcherSettings `
        -Description "Watches for new Claude cowork sessions and injects org context" `
        -RunLevel    Limited | Out-Null

    # Start it immediately
    Start-ScheduledTask -TaskName $WatcherTask -ErrorAction SilentlyContinue
    Info "Cowork watcher task created and started."
}

# ── First run ─────────────────────────────────────────────────────────────────
if ($Key -ne "") {
    Write-Host ""
    Info "Running initial sync…"
    & $PythonPath $InstallPath
} else {
    Write-Host ""
    Write-Host "  Next steps:" -ForegroundColor Yellow
    Write-Host "  1. Edit $ConfigFile and set CONTEXT_API_KEY=<key from IT>"
    Write-Host "  2. Run manually:  python `"$InstallPath`""
}

Write-Host ""
Write-Host "  Done!  Logs: $LogFile" -ForegroundColor Green
Write-Host ""
