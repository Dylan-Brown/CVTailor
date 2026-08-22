<#
Install-NativeHost.ps1

Registers the CVTailor intake native messaging host with Chrome, so
the "Job Data Harvester" extension can talk to native_host.py directly
instead of downloading a file. Run this ONCE after editing
com.thesistoolkit.cvtailor_intake.json's allowed_origins with your
actual extension ID (see README.md in this folder for how to find it).

Rewrites the manifest's "path" field to wherever native_host.bat
*actually* lives (derived from $PSScriptRoot) every time this runs,
rather than trusting whatever's checked into the committed JSON --
that field is an absolute path, so it goes stale the moment this
folder is cloned somewhere else or moved. Re-run this script any time
you move the project; it's idempotent.

Run from PowerShell (no admin needed -- this only touches HKCU):
    .\Install-NativeHost.ps1
#>

$ManifestPath = Join-Path $PSScriptRoot "com.thesistoolkit.cvtailor_intake.json"
$RegistryKey = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.thesistoolkit.cvtailor_intake"
$ActualHostPath = Join-Path $PSScriptRoot "native_host.bat"

if (-not (Test-Path $ManifestPath)) {
    Write-Host "Could not find $ManifestPath -- run this script from the native_host folder." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $ActualHostPath)) {
    Write-Host "Could not find $ActualHostPath -- run this script from the native_host folder." -ForegroundColor Red
    exit 1
}

$manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
if ($manifest.allowed_origins -join "," -match "REPLACE_WITH_YOUR_EXTENSION_ID") {
    Write-Host "com.thesistoolkit.cvtailor_intake.json still has the placeholder extension ID." -ForegroundColor Yellow
    Write-Host "Edit allowed_origins with your real extension ID first -- see README.md." -ForegroundColor Yellow
    exit 1
}

if ($manifest.path -ne $ActualHostPath) {
    Write-Host "Updating manifest path: $($manifest.path) -> $ActualHostPath" -ForegroundColor Cyan
    $manifest.path = $ActualHostPath
    ($manifest | ConvertTo-Json) | Set-Content -Path $ManifestPath -Encoding UTF8
}

New-Item -Path $RegistryKey -Force | Out-Null
Set-ItemProperty -Path $RegistryKey -Name "(Default)" -Value $ManifestPath

Write-Host "Registered native messaging host:" -ForegroundColor Green
Write-Host "  Registry key: $RegistryKey"
Write-Host "  Manifest:     $ManifestPath"
Write-Host "  Host script:  $ActualHostPath"
Write-Host ""
Write-Host "Reload the extension at chrome://extensions and try it on a job posting." -ForegroundColor Cyan
