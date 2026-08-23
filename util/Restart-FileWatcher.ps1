<#
Quick stop+start of the already-registered file watcher task - for fast
iteration during testing (e.g. after editing config\file_watcher.json)
without re-running the full Register-FileWatcher.ps1 registration.

Run: powershell -ExecutionPolicy Bypass -File .\Restart-FileWatcher.ps1
#>

$TaskName = "ThesisToolkitFileWatcher"

try {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
} catch {
    throw "Task '$TaskName' isn't registered yet - run Register-FileWatcher.ps1 first."
}

Write-Host "Stopping $TaskName..."
Stop-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

Write-Host "Starting $TaskName..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime, LastTaskResult, NextRunTime

Write-Host "`nVerify with Test-FileWatcherRunning.ps1, or: curl http://127.0.0.1:8765/health"
