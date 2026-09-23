<#
Confirms the file watcher is actually running. Checks two independent
things because they can disagree: Task Scheduler can report a task as
"Running" while the Python process inside it has crashed after its
initial launch (e.g. a config error a few seconds in) - the HTTP check
is the one that actually proves the watcher is doing its job.

Run: powershell -ExecutionPolicy Bypass -File .\Test-FileWatcherRunning.ps1
#>

$TaskName = "ThesisToolkitFileWatcher"

Write-Host "--- Scheduled Task state ---"
try {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $Info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Task state:   $($Task.State)"
    Write-Host "Last run:     $($Info.LastRunTime)"
    Write-Host "Last result:  $($Info.LastTaskResult)  (0 = success; still 0 while running)"
} catch {
    Write-Warning "Task '$TaskName' not found - it hasn't been registered yet (see Register-FileWatcher.ps1)."
}

Write-Host "`n--- Local trigger server (the real check) ---"
try {
    $Response = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 5
    Write-Host "Reachable. intake enabled=$($Response.intake)  applications enabled=$($Response.applications)"
    Write-Host "`nThe watcher is genuinely up."
} catch {
    Write-Warning "Server not reachable at http://127.0.0.1:8765/health."
    Write-Warning "Even if the Task above shows 'Running', the watcher process itself is not responding -"
    Write-Warning "check jobs\log\file_watcher.json for what happened after launch."
}
