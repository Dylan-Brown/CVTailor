<#
Runs CVTailor's discovery + submission-readiness check once: first checks
for a Telegram "/filter <text>" reply and applies it if found
(src\util\py_check_telegram_filter_commands.py -- so a new exclusion
takes effect before this same cycle's discovery), then the curated
ATS poller (src\sourcing\py_poll_ats_boards.py), the optional Adzuna lane
(src\sourcing\py_poll_adzuna.py -- only if config\adzuna.yaml exists),
then the submission script (src\submission\py_submit_application.py,
which -- while config\submission.yaml's `enabled` is false -- only checks
the Definition-of-Done gate and Telegram-pings newly-ready apps, never
opens a browser or submits anything).

Invoked on a schedule by Register-DiscoveryPoller.ps1 (every 15 min); can
also be run by hand:
    powershell -ExecutionPolicy Bypass -File .\Run-DiscoveryAndNotify.ps1
#>

$JobsRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogDir   = Join-Path $JobsRoot "log"
$LogPath  = Join-Path $LogDir "discovery_poller.log"

# Deliberately NOT config\file_watcher.json's applications.python_exe --
# that points at a separate Python 3.12 install (the file watcher's own
# interpreter, which needs no third-party packages) that doesn't have
# CVTailor's actual requirements.txt deps (bs4, playwright, ...)
# installed. Those live under this Python 3.14 install instead (found
# 2026-09-29 the hard way: a scheduled run crashed on ModuleNotFoundError
# for both bs4 and playwright until this was hardcoded).
$PythonExe = "C:\Users\Thesis\AppData\Local\Programs\Python\Python314\python.exe"

if (-not (Test-Path $PythonExe)) {
    throw "Python interpreter not found at $PythonExe -- update this path in Run-DiscoveryAndNotify.ps1 to wherever CVTailor's requirements.txt is actually installed (check with: python -c 'import bs4, playwright')."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Invoke-Step {
    param([string]$ScriptRelPath)
    $ScriptPath = Join-Path $JobsRoot $ScriptRelPath
    "`n[$(Get-Date -Format o)] === $ScriptRelPath ===" | Add-Content -Path $LogPath
    & $PythonExe $ScriptPath *>&1 | Add-Content -Path $LogPath
}

Invoke-Step "src\util\py_check_telegram_filter_commands.py"

Invoke-Step "src\sourcing\py_poll_ats_boards.py"

if (Test-Path (Join-Path $JobsRoot "config\adzuna.yaml")) {
    Invoke-Step "src\sourcing\py_poll_adzuna.py"
}

Invoke-Step "src\submission\py_submit_application.py"
