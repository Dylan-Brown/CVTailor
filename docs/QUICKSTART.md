# Quickstart, Pipeline Explainer & Benchmarks

## Everyday Commands (Quickstart)
All commands run from the `jobs/` root directory.
*   **Reset an app:** `python src\controls\py_pipeline_reset.py --jd <app_id>`.
*   **Prepare pending apps:** `python run_pipeline.py --force` (Runs stages 0-7 automatically).
*   **Check status:** `python src\checkpoints\py_pipeline_status.py`.
*   **Pre-review sanity check:** `python src\checkpoints\py_pipeline_prereview_summary.py`.
*   **Review edits:** `python src\stages\py_stage08_agentic_update_review.py` (The one manual step).
*   **Assemble PDFs:** `python src\controls\py_pipeline_assemble.py`.
*   **Verify before sending:** `python src\checkpoints\py_post_pipeline_verify_materials.py --app-id <app_id>`.

## Full Pipeline Explainer
The batch flow executes in this sequence automatically via `run_pipeline.py`:
*   **Stage 0:** Normalize raw JD JSON into `jd_input.json`.
*   **Stage 1:** Score the JD and suggest a resume variant.
*   **Stage 2:** Ingest CV claims (runs once per resume variant, cached).
*   **Stage 3:** Research the JD company via web search.
*   **Stage 4:** Analyze requirement gaps.
*   **Stage 5:** Propose updates constrained to the claims ledger.
*   **Stage 6:** Fact-check gate (discards fabrications).
*   **Stage 7:** Evaluate update utility (discards irrelevant edits).

## Definition of Done (DoD)
A concrete, checkable gate for "ready to submit" in two tiers.
**Tier 1 — Automated:** Run `py_post_pipeline_verify_materials.py`. It checks:
*   Claims ledger has ≥10 claims.
*   `coverage_score` is a real 0-1 value.
*   At least one edit was approved.
*   No leftover placeholders (`<COMPANY_NAME>`, `{{...}}`).
*   No known-bad leftover phrases.
*   Company name appears ≥3 times in the cover letter.
*   Mandatory company paragraph was actually rewritten.
*   Resume and cover letter meet target page limits.
*   Resume PDF text-extracts cleanly for ATS parsers.
*   Required JD keywords are present in resume text.

**Tier 2 — Human (~60 seconds):**
1. Does the company paragraph name something *true and specific*?
2. Does at least one bullet read as genuinely tailored?
3. Does the closing reference the actual role?
4. Is contact info correct?
5. Check for voice/tense drift (especially present-perfect tense).

## Goals & Benchmarks
*   **Throughput:** Track apps prepared per week using `python src/util/py_applications_stats.py --archive`.
*   **Quality:** Tier 1 DoD pass rate should be 100% before send. Zero resume edits is a red flag.