# src/sourcing/

Optional job-fit scoring, used only by `src/stages/py_stage01_score_jd_input.py`
(itself optional -- not run automatically by `run_pipeline.py`). Salvaged
from an earlier, archived project (RemoteJobApplier) -- the scraping/
submission machinery from that project is gone; only the scoring rubric
was kept.

* **`scorer.py`** — `JobScorer`: scores a JD 0-10 against your configured
  targets (resume fit, role-type match, salary, remote, target-firm bonus).
* **`config.py`** — `ScoringConfig`/`ScoringWeights` dataclasses, built via
  `.from_dict()` from `config/applicant_info.json`'s optional `job_scoring`
  section. No target companies/titles hardcoded here by design -- see that
  JSON section instead.
* **`candidate_profile.py`** — renders the same `job_scoring` section as
  the prose block the scorer's LLM prompt embeds as candidate context.
* **`ats_boards.py`** — currently unused by anything (not imported outside
  this folder); a leftover from the salvaged project.
