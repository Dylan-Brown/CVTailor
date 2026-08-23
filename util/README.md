# CVTailor Utility Scripts

This directory contains optional Windows PowerShell scripts designed to automate the CVTailor pipeline and manage its always-on background processes. 

## 1. File Watcher Management

These scripts control the background watcher that automatically queues new job descriptions and syncs your generated PDFs.

*   **`Register-FileWatcher.ps1`**: Run this **once as Administrator**. It registers the file watcher as a Windows Scheduled Task so it automatically starts when you boot your PC, survives crashes, and runs quietly in the background indefinitely.
*   **`Test-FileWatcherRunning.ps1`**: Run this to verify the watcher is actually alive. It doesn't just check the Windows Task status—it actively pings the local HTTP server to guarantee the process hasn't stalled.
*   **`Restart-FileWatcher.ps1`**: A quick utility to restart the background task. This is perfect for when you make edits to your `file_watcher.json` config and need them to take effect immediately without re-registering everything.

## 2. The "One-Click" Pipeline Runner

*   **`Run-FullPipeline.ps1`**: The ultimate end-to-end automation script. Instead of running the pipeline in separate, manual stages, this script checks your configuration, automatically warms up your local LM Studio model, and runs the entire pipeline straight through (including unattended AI review, document assembly, and opening the application URLs).
    *   *Prerequisites:* Because this is a zero-touch script, your `pipeline.yaml` must be set to `Agent` (not User), and your `GEMINI_API_KEY` must be configured in your environment.

**Usage Examples:**
```powershell
.\Run-FullPipeline.ps1                 # Run all pending applications end-to-end
.\Run-FullPipeline.ps1 -App <app_id>   # Run the pipeline for one specific job
.\Run-FullPipeline.ps1 -DryRun         # Validate your configuration without spending API calls
.\Run-FullPipeline.ps1 -Model <name>   # Temporarily override the default LM Studio model