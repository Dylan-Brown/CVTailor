# src/stages/

The 10 numbered pipeline stages, each a standalone CLI script. Stages
never import each other directly -- they only agree on the JSON shapes
in `src/core/schemas.py`, reading one stage's output as the next
stage's input. `run_pipeline.py` chains them for you; each still runs
fine on its own for debugging one JD in isolation.

* **`py_stage00_normalize_jd_input.py`** — validates untrusted JD JSON
  (from the Chrome extension or a manual drop) into `jd_input.json`.
* **`py_stage01_score_jd_input.py`** — optional pre-screen score against
  your configured target roles/firms/salary range. Not run automatically.
* **`py_stage02_ingest_cv_claims.py`** — extracts your resume into an
  atomic, ID-addressed claims ledger (runs once per resume variant, not
  once per JD).
* **`py_stage03_research_jd_company.py`** — web research on the target
  company, scoped to this specific JD.
* **`py_stage04_analyze_requirement_gaps.py`** — compares JD requirements
  against your claims, flags coverage gaps.
* **`py_stage05_propose_updates.py`** — the LLM generates candidate edits,
  strictly constrained to citing a real claim ID.
* **`py_stage06_fact_check_updates.py`** — the fact-check gate: verifies
  every cited edit against the actual claim text, discards fabrications.
* **`py_stage07_evaluate_update_utility.py`** — discards edits that are
  true but not actually useful for this specific job.
* **`py_stage08_agentic_update_review.py`** — the one manual checkpoint:
  approve/reject/edit each surviving edit. Interactive by design; see
  `src/ai_wrappers/` for the automated Agent-reviewer alternative.
* **`py_stage09_assemble_materials.py`** — applies approved edits to the
  resume template and cover letter, builds the final `.docx`/`.pdf`.
