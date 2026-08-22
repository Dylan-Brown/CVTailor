#!/usr/bin/env python3
"""
py_stage08_reviewer_interface.py — thin dispatcher for stage 8. Reads
pipeline.yaml's "Review Stage Assignee" (User | Agent) and execs the
matching backend, forwarding every other argument unchanged:

  - User  -> src/stages/py_stage08_agentic_update_review.py, the real
             interactive human-review CLI. This is where the actual
             review logic lives -- same as every other stage script,
             a standalone CLI, not something only reachable through
             this dispatcher. Running it directly always works.
  - Agent -> whichever wrapper pipeline.yaml's "Selected Agent" names
             (currently only "Gemini" ->
             src/ai_wrappers/py_stage08_reviewer_gemini.py; more
             wrappers can be added here as they're built).

This split exists so a human and an AI reviewer are choosing between
the SAME two backends through the SAME flags (--app-id, --force,
--max-edits, ...) -- the only difference is which one drives, decided
once in pipeline.yaml rather than by which script you happened to
type.

Usage: identical to py_stage08_agentic_update_review.py -- every
argument is forwarded unchanged to whichever backend is selected.
    python src/ai_wrappers/py_stage08_reviewer_interface.py [--app-id <id>] [--force] [...]
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/ai_wrappers/ -> src/ -> jobs/
STAGES_DIR = JOBS_ROOT / "src" / "stages"
AI_WRAPPERS_DIR = JOBS_ROOT / "src" / "ai_wrappers"
PIPELINE_YAML = JOBS_ROOT / "pipeline.yaml"

AGENT_BACKENDS = {
    "gemini": AI_WRAPPERS_DIR / "py_stage08_reviewer_gemini.py",
}


def _read_pipeline_config() -> dict:
    """Minimal, dependency-free YAML read for just the two keys this
    dispatcher needs -- avoids requiring PyYAML as a hard dependency
    for something this small. Missing file or missing keys both fall
    back to the same defaults a fresh clone should have: a human
    reviews, by default."""
    config = {"review_stage_assignee": "User", "selected_agent": "Gemini"}
    if not PIPELINE_YAML.is_file():
        return config
    for line in PIPELINE_YAML.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("review stage assignee:"):
            config["review_stage_assignee"] = stripped.split(":", 1)[1].strip().strip("'\"")
        elif stripped.lower().startswith("selected agent:"):
            config["selected_agent"] = stripped.split(":", 1)[1].strip().strip("'\"")
    return config


def main():
    passthrough = sys.argv[1:]
    config = _read_pipeline_config()
    assignee = config["review_stage_assignee"].strip().lower()

    if assignee == "agent":
        agent = config["selected_agent"].strip().lower()
        backend = AGENT_BACKENDS.get(agent)
        if backend is None:
            print(f"pipeline.yaml selects Agent={config['selected_agent']!r}, but no wrapper is "
                  f"registered for it (known: {sorted(AGENT_BACKENDS)}). Falling back to the "
                  f"human reviewer.", file=sys.stderr)
            backend = STAGES_DIR / "py_stage08_agentic_update_review.py"
    else:
        backend = STAGES_DIR / "py_stage08_agentic_update_review.py"

    cmd = [sys.executable, str(backend)] + passthrough
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
