"""
jobs/cvtailor_score.py

Thin wrapper so `score-job` works as a standalone console_script
command (pyproject.toml's [project.scripts]). Previously this entry
pointed at a module (jobs.cvtailor_score -- and before that
jobs.cvtailor.score) that was never built -- silently broken since the
moment it was written, worked around all along by running the real
script directly instead. Recovered 2026-08-17 after being accidentally
deleted as "duplicate legacy code" in an earlier pass.

Shells out to the real script rather than solving the sys.path-hack
import complexity -- same pattern the `cvtailor` console command
(now backed by run_pipeline.py directly) used to use for exactly
this reason.
"""

import subprocess
import sys
from pathlib import Path

CVTAILOR_ROOT = Path(__file__).resolve().parent
SCORE_SCRIPT = CVTAILOR_ROOT / "src" / "stages" / "py_stage01_score_jd_input.py"


def main():
    if not SCORE_SCRIPT.exists():
        print(f"Expected script not found: {SCORE_SCRIPT}", file=sys.stderr)
        sys.exit(1)
    # cwd=CVTAILOR_ROOT matters: py_stage01_score_jd_input.py's
    # --applications-root default ("applications") is relative to wherever
    # it runs from, and every other invocation all along has assumed
    # CVTailor's own root as that anchor. Setting cwd here means
    # `score-job` works correctly regardless of which directory you
    # actually run it from.
    result = subprocess.run(
        [sys.executable, str(SCORE_SCRIPT), *sys.argv[1:]],
        cwd=str(CVTAILOR_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
