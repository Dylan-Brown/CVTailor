"""
py_run_stage.py — the "given one stage + one app, execute it" primitive.

Every stage script is a standalone subprocess CLI (see schemas.py's own
docstring on why: stages never import each other, they only agree on
file shapes). This module is the one place that knows how to invoke
one of those CLIs as a subprocess, given a stage number and an argv
list — run_pipeline.py's batch-prepare loop and its single-app
end-to-end mode both call into this rather than each building their
own subprocess.run() wrapper, and batch_common.py's sh()/stage_script()
delegate here too so there's exactly one subprocess-invocation
convention in the whole codebase.

Also directly runnable as its own CLI, for running one stage against
one app by hand without going through run_pipeline.py at all:

    python src/core/py_run_stage.py --stage 3 --app-id acme_2026-07-27 -- \\
        --jd-input applications/acme_2026-07-27/jd_input.json \\
        --claims-ledger applications/_shared/variants/backend/claims_ledger.json \\
        --out-dir applications/acme_2026-07-27
"""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/core/ -> src/ -> jobs/
STAGES_DIR = JOBS_ROOT / "src" / "stages"

# Numeric stage -> script filename. The single source of truth for
# "which file is stage N" -- run_pipeline.py and every other caller
# look up scripts through this dict rather than hardcoding filenames.
STAGE_SCRIPTS = {
    0: "py_stage00_normalize_jd_input.py",
    1: "py_stage01_score_jd_input.py",
    2: "py_stage02_ingest_cv_claims.py",
    3: "py_stage03_research_jd_company.py",
    4: "py_stage04_analyze_requirement_gaps.py",
    5: "py_stage05_propose_updates.py",
    6: "py_stage06_fact_check_updates.py",
    7: "py_stage07_evaluate_update_utility.py",
    8: "py_stage08_agentic_update_review.py",
    9: "py_stage09_assemble_materials.py",
}


def stage_script(name_or_number: str | int) -> str:
    """Accepts either a stage number (0-9) or an exact script filename
    -- the numeric form is preferred for new code, the filename form
    stays supported since batch_common.py's callers already pass exact
    filenames (e.g. "py_stage02_ingest_cv_claims.py") and rewriting
    every call site to the numeric form isn't worth the churn."""
    if isinstance(name_or_number, int):
        return str(STAGES_DIR / STAGE_SCRIPTS[name_or_number])
    return str(STAGES_DIR / name_or_number)


def sh(cmd: list[str]) -> None:
    """Run a stage as a subprocess, streaming its output live. Raises
    on a non-zero exit -- callers that want to collect per-app failures
    instead of stopping the whole batch (see run_pipeline.py) catch
    this themselves rather than this function swallowing it."""
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"command failed (exit {result.returncode}): {' '.join(cmd)}")


def sh_capture(cmd: list[str]) -> str:
    """Like sh(), but captures output instead of letting it stream to
    the terminal, and returns stdout -- for callers that need to parse
    a stage's own printed output (stage 0's printed app_id)."""
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"command failed (exit {result.returncode}): {' '.join(cmd)}")
    print(result.stdout)
    return result.stdout


def sh_optional(cmd: list[str]) -> bool:
    """Like sh(), but a non-zero exit doesn't raise -- for stages that
    are decision aids, not gates (stage 1's scoring: a failed/unavailable
    score shouldn't block the rest of the pipeline). Returns True on
    success, False on a non-fatal failure."""
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"(non-fatal) {Path(cmd[1]).name} exited with code {result.returncode} "
              f"-- continuing anyway", file=sys.stderr)
        return False
    return True


def run_stage(n: int, argv: list[str]) -> None:
    """The primitive itself: run stage N against whatever app/paths
    `argv` specifies. Raises on failure -- see sh()."""
    sh([sys.executable, stage_script(n)] + argv)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Run one pipeline stage by number, passing the rest of argv through unchanged.")
    ap.add_argument("--stage", type=int, required=True, choices=sorted(STAGE_SCRIPTS))
    ap.add_argument("stage_args", nargs=argparse.REMAINDER,
                     help="Everything after -- is passed through to the stage script as-is.")
    args = ap.parse_args()
    passthrough = args.stage_args[1:] if args.stage_args[:1] == ["--"] else args.stage_args
    run_stage(args.stage, passthrough)
