<#
Full unattended chain: discover new postings, tailor+assemble real
application materials for them, then check/gated-apply. Supersedes
Run-DiscoveryAndNotify.ps1, which only ever did discovery + a submission
readiness check -- it never actually ran the tailoring pipeline, so
nothing dropped by the poller ever became a real, submittable application
on its own. This script closes that gap:

  1. src\util\py_check_telegram_filter_commands.py -- apply any pending
     "/filter <text>" reply before this cycle's discovery runs.
  2. src\sourcing\py_poll_ats_boards.py -- curated ATS discovery.
  3. src\sourcing\py_poll_adzuna.py -- optional broad-market lane, only
     if config\adzuna.yaml exists.
  4. run_pipeline.py -- batch mode (stages 0-7): drains raw intake and
     tailors resume/cover-letter content for every pending application.
     Uses the local LM Studio model by default (no cloud spend) unless
     $env:CVTAILOR_LLM_PROVIDER says otherwise.
  5. src\ai_wrappers\py_stage08_reviewer_interface.py -- stage 8 review.
     Non-blocking here because config\pipeline.yaml's review_stage_assignee
     is 'Agent' (Gemini) -- if that's ever set back to 'User', this step
     will hang forever waiting for interactive input, so don't schedule
     this script unattended while it's set to 'User'.
  6. src\controls\py_pipeline_assemble.py -- stage 9 assemble. This is
     what actually produces applications\<id>\generated_materials\ --
     nothing before this step creates real, submittable output.
  7. src\submission\py_submit_application.py -- gated apply. Actually
     submitting anything still requires config\submission.yaml's
     `enabled: true` (global kill-switch, default false) AND that
     company's `auto_submit: true` in target_companies.yaml AND a fully
     green Definition-of-Done gate -- otherwise this step only Telegram-
     notifies that the application is ready and leaves it for you.

Deliberately EXCLUDES src\controls\py_post_pipeline_open_app_urls.py --
that step opens a Chrome tab per live application with NO de-dup, so
running it every cycle from a 15-min scheduled task would re-open a
browser window full of tabs every 15 minutes. That step stays a manual,
interactive-only action (via `python run_pipeline.py --full` by hand, or
until a de-duped version exists).

Invoked on a schedule by Register-DiscoveryPoller.ps1; can also be run by
hand:
    powershell -ExecutionPolicy Bypass -File .\Run-DiscoverTailorApply.ps1
#>

$JobsRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogDir   = Join-Path $JobsRoot "log"
$LogPath  = Join-Path $LogDir "discovery_poller.log"

# Same interpreter fix as Run-DiscoveryAndNotify.ps1 (2026-09-29): NOT
# config\file_watcher.json's applications.python_exe (Python 3.12, missing
# CVTailor's requirements.txt deps) -- this is the Python 3.14 install
# that actually has them (bs4, playwright, requests, yaml, ...).
$PythonExe = "C:\Users\Thesis\AppData\Local\Programs\Python\Python314\python.exe"

if (-not (Test-Path $PythonExe)) {
    throw "Python interpreter not found at $PythonExe -- update this path in Run-DiscoverTailorApply.ps1 to wherever CVTailor's requirements.txt is actually installed (check with: python -c 'import bs4, playwright')."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Invoke-Step {
    param([string]$ScriptRelPath, [string[]]$ScriptArgs = @())
    $ScriptPath = Join-Path $JobsRoot $ScriptRelPath
    "`n[$(Get-Date -Format o)] === $ScriptRelPath $($ScriptArgs -join ' ') ===" | Add-Content -Path $LogPath
    & $PythonExe $ScriptPath @ScriptArgs *>&1 | Add-Content -Path $LogPath
}

Invoke-Step "src\util\py_check_telegram_filter_commands.py"

Invoke-Step "src\sourcing\py_poll_ats_boards.py"

if (Test-Path (Join-Path $JobsRoot "config\adzuna.yaml")) {
    Invoke-Step "src\sourcing\py_poll_adzuna.py"
}

# run_pipeline.py doesn't warm this up itself -- without it, stage 1+
# generation calls fail outright if LM Studio's server/model isn't already
# up (connection refused to localhost:1234).
Invoke-Step "src\auto_queue\ensure_lm_studio_ready.py"

Invoke-Step "run_pipeline.py"
Invoke-Step "src\ai_wrappers\py_stage08_reviewer_interface.py"
Invoke-Step "src\controls\py_pipeline_assemble.py"
Invoke-Step "src\submission\py_submit_application.py"
