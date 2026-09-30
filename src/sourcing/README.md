# src/sourcing/

Job-fit scoring (used only by `src/stages/py_stage01_score_jd_input.py`,
itself optional -- not run automatically by `run_pipeline.py`), plus the
ATS-board discovery lane. Both were salvaged/extended from an earlier,
archived project (RemoteJobApplier).

* **`scorer.py`** — `JobScorer`: scores a JD 0-10 against your configured
  targets (resume fit, role-type match, salary, remote, target-firm bonus).
* **`config.py`** — `ScoringConfig`/`ScoringWeights` dataclasses, built via
  `.from_dict()` from `config/applicant_info.json`'s optional `job_scoring`
  section. No target companies/titles hardcoded here by design -- see that
  JSON section instead.
* **`candidate_profile.py`** — renders the same `job_scoring` section as
  the prose block the scorer's LLM prompt embeds as candidate context.
* **`ats_boards.py`** — fetches postings from Greenhouse/Lever/Ashby's
  public JSON APIs, already shaped to `JDInput`. The company lists baked
  in here are just placeholders/fallback defaults -- `py_poll_ats_boards.py`
  is the real entrypoint and supplies your curated companies from
  `config/target_companies.yaml` instead.
* **`py_poll_ats_boards.py`** — the discovery lane's CLI entrypoint: polls
  `ats_boards.py` against your curated company list + `applicant_info.json`'s
  `job_scoring.target_titles` keywords, dedupes against
  `applications/_shared/seen_postings.json`, and drops any new posting
  straight into `applications/` for `run_pipeline.py` to pick up on its
  next run. Meant to run on a schedule -- see the module docstring.
