<#
Run-FullPipeline.ps1

The "just run it" entry point: makes sure LM Studio is up and serving
the right model, makes sure pipeline.yaml is actually wired for
unattended (Agent/Gemini) review rather than the interactive human CLI
-- which would otherwise just hang waiting on stdin in a script meant
to need nothing from you -- then runs prepare (stages 0-7, LM Studio)
straight through review (stage 8, Gemini), assembly (stage 9), and
opening every live application's URL in Chrome, via
`run_pipeline.py --full`.

Every check below fails loudly and stops BEFORE spending any API calls
if something's actually wrong, rather than discovering it three stages
deep -- same philosophy as py_pipeline_precheck.py, which this also
runs.

Lives in jobs/util/ (alongside Register-FileWatcher.ps1) -- resolves
the actual jobs/ root as its own parent directory rather than assuming
$PSScriptRoot IS jobs/ root, so it isn't tied to living at any one
specific location.

Usage (from anywhere -- paths below are relative to jobs/util/):
    .\Run-FullPipeline.ps1                       # the whole point: no args needed
    .\Run-FullPipeline.ps1 -App acme_2026-08-22   # scope to one application
    .\Run-FullPipeline.ps1 -Force                 # re-run even if already verified
    .\Run-FullPipeline.ps1 -DryRun                # validate wiring, no API calls, no LM Studio/Gemini needed
    .\Run-FullPipeline.ps1 -Model qwen2.5-14b-instruct   # a different LM Studio model
#>
param(
    [string]$App = $null,
    [switch]$Force,
    [switch]$DryRun,
    [string]$Model = "qwen2.5-coder-14b-instruct"
)

$JobsRoot = Split-Path $PSScriptRoot -Parent
Set-Location $JobsRoot

function Fail($msg) {
    Write-Host $msg -ForegroundColor Red
    exit 1
}

# ---- 1. pipeline.yaml must actually be wired for unattended review --
#         otherwise stage 8 silently falls back to the interactive
#         human CLI (see py_stage08_reviewer_interface.py) and this
#         "just run it" script would hang on stdin instead of finishing. ----
if (-not $DryRun) {
    $yaml = Get-Content (Join-Path $JobsRoot "pipeline.yaml") -Raw
    $assigneeLine = ($yaml -split "`n" | Where-Object { $_ -match "^\s*review_stage_assignee\s*:" }) -join ""
    $agentLine = ($yaml -split "`n" | Where-Object { $_ -match "^\s*selected_agent\s*:" }) -join ""
    $assignee = ($assigneeLine -split ":", 2)[1].Trim().Trim("'", '"')
    $agent = ($agentLine -split ":", 2)[1].Trim().Trim("'", '"')

    if ($assignee -ne "Agent") {
        Fail ("pipeline.yaml has review_stage_assignee: $assignee -- this script is for " +
              "unattended runs, which needs `"Agent`", not a human sitting at the interactive " +
              "CLI. Set it to Agent in pipeline.yaml, or run the normal 3-step flow by hand " +
              "instead (see docs/QUICKSTART.md).")
    }
    if ($agent -ne "Gemini") {
        Fail ("pipeline.yaml has selected_agent: $agent -- only `"Gemini`" has a registered " +
              "reviewer backend right now (see AGENT_BACKENDS in " +
              "src/ai_wrappers/py_stage08_reviewer_interface.py). Set selected_agent: Gemini.")
    }
    Write-Host "[check] pipeline.yaml: Agent / Gemini confirmed." -ForegroundColor Green

    # ---- 2. GEMINI_API_KEY -- the Gemini reviewer backend always needs
    #         this regardless of the LM Studio default for everything else. ----
    if (-not $env:GEMINI_API_KEY) {
        Fail ("GEMINI_API_KEY isn't set in this session's environment -- required by the Gemini " +
              "reviewer backend (src/ai_wrappers/py_stage08_reviewer_gemini.py). Set it with " +
              '$env:GEMINI_API_KEY = "..." before running this script, or set it persistently ' +
              "in your PowerShell profile.")
    }
    Write-Host "[check] GEMINI_API_KEY: set." -ForegroundColor Green

    # ---- 3. Pin the model BEFORE calling the readiness check --
    #         src/auto_queue/ensure_lm_studio_ready.py reads LMSTUDIO_MODEL
    #         itself (no CLI arg), and the same env var has to still be set
    #         when run_pipeline.py runs later in this same session so the
    #         pipeline actually talks to the model just loaded, not
    #         whatever /v1/models lists first out of however many models
    #         are downloaded. ----
    $env:LMSTUDIO_MODEL = $Model
    Write-Host "[check] Ensuring LM Studio is serving $Model ..." -ForegroundColor Cyan
    python src/auto_queue/ensure_lm_studio_ready.py
    if ($LASTEXITCODE -ne 0) {
        Fail "LM Studio isn't ready (see output above) -- fix that first, or launch/load it yourself."
    }

    # ---- 4. Everything else (resume/template/cover-letter files present,
    #         applicant_info.json valid, dependencies installed, at least
    #         one JD actually queued) -- fail before spending anything. ----
    Write-Host "`n[check] Running py_pipeline_precheck.py ..." -ForegroundColor Cyan
    python src/checkpoints/py_pipeline_precheck.py
    if ($LASTEXITCODE -ne 0) {
        Fail "`npy_pipeline_precheck.py failed -- fix the items above before this can run for real."
    }
}

# ---- 5. The actual run: prepare -> Gemini review -> assemble -> open URLs. ----
$pipelineArgs = @("run_pipeline.py", "--full")
if ($App)    { $pipelineArgs += @("--app", $App) }
if ($Force)  { $pipelineArgs += "--force" }
if ($DryRun) { $pipelineArgs += "--dry-run" }

Write-Host "`n=== python $($pipelineArgs -join ' ') ===" -ForegroundColor Cyan
python @pipelineArgs
exit $LASTEXITCODE
