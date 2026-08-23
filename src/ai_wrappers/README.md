# src/ai_wrappers/

Automated ("Agent") alternatives to stage 8's interactive human review,
selected by `pipeline.yaml`'s `review_stage_assignee` / `selected_agent`.

* **`py_stage08_reviewer_interface.py`** — the dispatcher. Reads
  `pipeline.yaml` and execs either the real human CLI
  (`src/stages/py_stage08_agentic_update_review.py`) or one of the Agent
  backends below. This is what `run_pipeline.py` actually calls.
* **`_agent_reviewer_common.py`** — shared driver logic for every Agent
  backend: launches the human review CLI as a subprocess, reads its
  prompts off stdout, approves whatever's already clean, and for flagged
  edits generates + verifies a replacement (through stage 6's own
  fact-checker) before ever submitting it.
* **`py_stage08_reviewer_gemini.py`** / **`_claude.py`** / **`_local.py`**
  — thin ~15-line backends, each just naming which provider generates
  replacement text. All the actual logic lives in `_agent_reviewer_common.py`.

Every backend is still just forwarding to the real interactive CLI under
the hood -- running `py_stage08_agentic_update_review.py` directly always
works too, regardless of what's configured here.
