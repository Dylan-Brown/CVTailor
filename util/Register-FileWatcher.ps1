<#
Registers jobs\src\auto_queue\py_file_watcher.py as a Windows Scheduled Task:
  - Starts at boot AND at logon
  - Restarts automatically if it crashes (3x, 1 min apart)
  - Runs indefinitely, whether logged in or not

Run ONCE from an elevated (Administrator) PowerShell prompt:
    powershell -ExecutionPolicy Bypass -File .\Register-FileWatcher.ps1

Paths are derived, not hardcoded twice:
  - $ScriptPath / $WorkingDir come from this script's own location
    ($PSScriptRoot = jobs\util\), so moving the whole jobs\ folder doesn't
    require editing this file.
  - The Python interpreter is read directly out of config\file_watcher.json
    (applications.python_exe) rather than duplicated here - pythonw.exe
    (no console window) is derived from the same folder as that python.exe,
    since a standard CPython Windows install ships both side by side.
#>

$TaskName   = "ThesisToolkitFileWatcher"
$JobsRoot   = Resolve-Path (Join-Path $PSScriptRoot "..")
$ConfigPath = Join-Path $JobsRoot "config\file_watcher.json"
$ScriptPath = Join-Path $JobsRoot "src\auto_queue\py_file_watcher.py"
$WorkingDir = Join-Path $JobsRoot "src\auto_queue"

if (-not (Test-Path $ConfigPath)) {
    throw "Config not found at $ConfigPath - this script expects to live at jobs\util\Register-FileWatcher.ps1, next to a sibling config\file_watcher.json."
}
$Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json

$PythonExe  = $Config.applications.python_exe
$PythonwExe = Join-Path (Split-Path $PythonExe -Parent) "pythonw.exe"

if (-not (Test-Path $PythonwExe)) {
    Write-Warning "pythonw.exe not found at $PythonwExe (derived from applications.python_exe in config) - update that config value or install location, then re-run."
}
if (-not (Test-Path $ScriptPath)) {
    Write-Warning "py_file_watcher.py not found at $ScriptPath - check the jobs\ folder layout."
}

$Action = New-ScheduledTaskAction -Execute $PythonwExe -Argument "`"$ScriptPath`"" -WorkingDirectory $WorkingDir

$TriggerBoot  = New-ScheduledTaskTrigger -AtStartup
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # 0 = no time limit

# Runs as you, but "whether logged on or not" so it survives logoff.
# You'll be prompted for your Windows password once during registration.
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger @($TriggerBoot, $TriggerLogon) `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Watches intake/ and applications/ for CVTailor auto-run; exports generated materials to Proton Drive." `
    -Force

Write-Host "Registered task '$TaskName'. Starting it now..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime, LastTaskResult, NextRunTime

$LogPath = Join-Path $JobsRoot "log\file_watcher.json"
Write-Host "`nCheck $LogPath for output."
Write-Host "Verify it's alive: curl http://127.0.0.1:8765/health"
