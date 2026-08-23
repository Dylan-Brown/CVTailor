#!/usr/bin/env python3
"""
cvtailor_score.py -- thin wrapper so `score-job` works as a standalone
console_script command (see pyproject.toml's [project.scripts]).
Shells out to src/stages/py_stage01_score_jd_input.py, the real script,
rather than importing it directly -- avoids a sys.path hack, at the
cost of one extra subprocess hop.

Usage: identical to py_stage01_score_jd_input.py -- every argument is
forwarded unchanged.
    score-job --app-id <jd_name_or_prefix> --resume-text <path>
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
