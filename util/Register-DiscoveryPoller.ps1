<#
Registers Run-DiscoverTailorApply.ps1 (discovery + tailoring + assembly +
gated apply, end to end) as a Windows Scheduled Task, running every 15
minutes indefinitely -- the same cadence alpha-hunt's own GitHub Actions
cron uses (see docs/ALPHA_HUNT_GH_ACTIONS_FALLBACK.md), just local instead
of cloud-scheduled.

Updated 2026-09-30: repointed from Run-DiscoveryAndNotify.ps1 (discovery +
readiness-check only -- it never actually tailored anything, so postings
just piled up in applications/ with nothing turning them into real,
submittable materials) to Run-DiscoverTailorApply.ps1, which runs the full
chain. Also bumped ExecutionTimeLimit (tailoring is much slower than a
plain API poll) and added MultipleInstances=IgnoreNew so a slow run can't
overlap with the next trigger.

Run ONCE from a PowerShell prompt:
    powershell -ExecutionPolicy Bypass -File .\Register-DiscoveryPoller.ps1

Paths are derived from this script's own location ($PSScriptRoot =
jobs\util\), same convention as Register-FileWatcher.ps1.
#>

$ErrorActionPreference = "Stop"

$TaskName    = "CVTailorDiscoveryPoller"
$JobsRoot    = Resolve-Path (Join-Path $PSScriptRoot "..")
$WrapperPath = Join-Path $PSScriptRoot "Run-DiscoverTailorApply.ps1"

if (-not (Test-Path $WrapperPath)) {
    throw "Wrapper not found at $WrapperPath -- this script expects to live at jobs\util\Register-DiscoveryPoller.ps1, next to Run-DiscoverTailorApply.ps1."
}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WrapperPath`"" `
    -WorkingDirectory $JobsRoot

# "Once, repeating every 15 min, forever" -- the standard Task Scheduler
# idiom for an interval trigger (there's no dedicated -Interval trigger type).
# [TimeSpan]::MaxValue serializes to a duration string
# (P99999999DT23H59M59S) that Task Scheduler's XML schema rejects outright
# -- 10 years is effectively "forever" for this and round-trips fine.
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Runs as you, but "whether logged on or not" so it survives logoff.
# You'll be prompted for your Windows password once during registration.
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Every 15 min: polls CVTailor's curated ATS boards (+ Adzuna if configured), tailors + assembles materials for pending applications, checks the Definition-of-Done gate, gated-applies where enabled, and Telegram-notifies on new discoveries / newly-ready applications." `
    -Force

Write-Host "Registered task '$TaskName'. Running it once now to verify..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime, LastTaskResult, NextRunTime

$LogPath = Join-Path $JobsRoot "log\discovery_poller.log"
Write-Host "`nCheck $LogPath for output. If Telegram isn't configured yet, you'll see [telegram] warnings there instead of a hard failure -- everything else still runs."
