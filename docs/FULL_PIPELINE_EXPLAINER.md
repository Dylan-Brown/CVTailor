# CVTailor Full Pipeline Explainer

Quick reference, in order. See `../README.md` for the "why" behind any of this.

## Pipeline execution order — what runs, in what sequence, and where it lives

```
BATCH FLOW (run_pipeline.py runs these per-JD automatically):

  Stage 0    src/stages/py_stage00_normalize_jd_input.py       Raw JD JSON -> validated jd_input.json
             (only for hand-dropped files; the Chrome extension skips this)

  Stage 1    src/stages/py_stage01_score_jd_input.py           Score the JD + suggest resume variant
             (deterministic, no LLM call — picks among config/resume_variants/*.json)

  Stage 2    src/stages/py_stage02_ingest_cv_claims.py          Resume + cover letter sample -> claims_ledger.json + style_profile.json
             (runs once per resume VARIANT, cached, not once per JD)

  Stage 3    src/stages/py_stage03_research_jd_company.py       JD + claims -> company_brief.json
             (web research on the company — one LLM call)

  Stage 4    src/stages/py_stage04_analyze_requirement_gaps.py  JD + claims -> edit_brief.json
             (coverage scoring, gap identification — one LLM call)

  Stage 5    src/stages/py_stage05_propose_updates.py           Ledger + brief + baseline cover letter -> candidate_edits.json
             (proposes targeted swaps for resume AND cover letter — one LLM call, reasoning tier)

  Stage 6    src/stages/py_stage06_fact_check_updates.py        Edits + claims -> verified_edits.json
             (fact-check gate: is this edit grounded in a real claim? — one LLM call per edit)

  Stage 7    src/stages/py_stage07_evaluate_update_utility.py   Verified edits + JD -> critiqued_edits.json
             (utility gate: is this edit actually USEFUL for THIS job? — one LLM call per edit)

MANUAL STEP (you run this yourself, interactively):

  Stage 8    src/stages/py_stage08_agentic_update_review.py     Critiqued edits -> review_decisions.json
             (approve/reject/edit each surviving edit — the one human checkpoint)
             or src/ai_wrappers/py_stage08_reviewer_interface.py to let pipeline.yaml
             pick between you and an AI reviewer (src/ai_wrappers/py_stage08_reviewer_gemini.py)

BATCH ASSEMBLY (src/controls/py_pipeline_assemble.py runs this per-JD automatically):

  Stage 9    src/stages/py_stage09_assemble_materials.py        Decisions + templates + baseline letter -> generated_materials/
             (applies approved edits to resume template and cover letter, builds docx/pdf)

BATCH MANAGEMENT (run any time, not part of the per-JD pipeline):

  src/checkpoints/py_pipeline_status.py           Where is everything right now
  src/controls/py_pipeline_reset.py               Reset a JD's downstream state for re-processing
  src/controls/py_pipeline_store_archive.py       Move a fully-done JD to applications/archived/
  src/controls/py_post_pipeline_store_applied.py  Archive a JD you've actually sent
  src/controls/py_post_pipeline_open_app_urls.py  Open every live app's source_url in Chrome
  src/checkpoints/py_pipeline_precheck.py         Validate config/ before spending anything
  src/checkpoints/py_post_pipeline_verify_materials.py  Definition of Done check -- run before
                               sending anything (see DEFINITION_OF_DONE.md, this folder)
  src/checkpoints/py_pipeline_prereview_summary.py      Funnel/quality summary across all
                               prepared apps -- run BEFORE reviewing a large batch, to spot
                               thin ledgers, zero-edit or zero-resume-edit apps before
                               spending review time on them
  run_pipeline.py              Also supports single-JD end-to-end mode (pass --jd-json;
                               all stages in one command)
```

## One-time setup

1. `pip install -r requirements.txt` — install dependencies.
2. `$env:ANTHROPIC_API_KEY = "sk-ant-..."` — set your API key (Claude is the default provider), or copy `config/credentials/anthropic_key.txt.example` to `anthropic_key.txt` and fill it in (see that file's own comments — the env var is what's actually read today).
3. Put your resume in `config/resume/` — exactly one file, `.pdf` or `.docx`.
4. Put your resume template in `config/resume_template/` — exactly one `.docx`.
5. Put a sample cover letter in `config/cover_letter_sample/` — exactly one `.txt`, genericized (no real employer names/facts).
6. Copy `config/applicant_info.example.json` to `config/applicant_info.json` and fill in your name (email/phone optional).
7. `python src/checkpoints/py_pipeline_precheck.py` — validates everything above before you spend anything.

## Per batch

8. Get a JD into `applications/`, any of three ways: the Chrome extension writes straight into `applications/<app_id>/jd_input.json` on its own; or drop a raw exported JD JSON straight into `applications/` (any filename) and the next step organizes it into its own `applications/<name>/` subfolder automatically; or pre-make `applications/<name>/` yourself and drop the raw JSON inside it, if you want to choose the folder name precisely. `applications/` itself is the queue **and** the drop zone — no separate config folder anywhere.
9. `python run_pipeline.py --dry-run` — free validation pass, no API calls, confirms the wiring.
10. `python run_pipeline.py` — organizes any raw JD JSON sitting under `applications/` into its own folder, then runs stage 2 (resume-variant ingest) and stages 3-7 for every JD there that isn't already verified. Stage 2 runs once **per resume variant** defined in `config/resume_variants/*.json` (not once per JD), cached until you change that resume or the cover letter — each JD is automatically routed to the matching variant based on real requirements/responsibilities keywords (see that variant's `keywords` list), not guessed. Assembly (step 12) routes to that variant's `resume_template` too, if one's set.
11. `python src/stages/py_stage08_agentic_update_review.py` — the one manual step. Run with **no arguments** — it auto-discovers every JD that has `verified_edits.json` (or `critiqued_edits.json`, if stage 7 ran) but no `review_decisions.json` yet and walks you through them one after another. (`--app-id <jd_name>` reviews just one; `--verified-edits`/`--out-dir` still work if you want to point at an exact path.) Each edit shows as `REWORD` (a diff against existing resume text), `COVER LETTER SENTENCE` (new content, assembled fresh — nothing to diff against), or a `⚠ UNUSUAL EDIT` warning (shouldn't normally appear — see troubleshooting below if it does). **`q` quits immediately and records nothing** — not just for remaining edits, for the one on screen too. If you want anything applied, approve/edit at least one thing before quitting, or let it run to the end.
12. `python src/controls/py_pipeline_assemble.py` — assembles every reviewed JD into `applications/<jd_name>/generated_materials/`, converts to PDF, and marks it processed in place (`jd_input.json` -> `jd_input.processed.json` inside its `applications/<jd_name>/` folder — visible at a glance, no file to move between folders).
13. `python src/checkpoints/py_pipeline_status.py` — check where everything stands, any time.

## Maintenance / re-runs

- `python run_pipeline.py --force` — reprocess every live JD under `applications/`, ignoring anything already done for them (including a forced fresh resume/cover-letter ingest for every variant). Add `--app <id_or_prefix>` to scope to one application.
- `python src/controls/py_pipeline_reset.py --jd <app_id>` — reset one JD's downstream state (undoes the processed marker, clears generated/verified/reviewed artifacts and `generated_materials/`) so it starts clean on the next `run_pipeline.py --force`. Add `--all` for every live JD, `--keep-artifacts` to skip the reset and just undo the processed marker.
- `python src/controls/py_pipeline_store_archive.py --jd <app_id>` — move one fully-done application (its whole `applications/<app_id>/` folder, `generated_materials/` and all) into `applications/archived/<app_id>/`, permanently. Add `--all` for every fully-done application.

## Flags that work everywhere they apply

- `--dry-run` — validates inputs and prints what would happen; stages 2-5 write clearly-labeled placeholder files so the whole chain can be checked for free, stage 6 writes nothing (it's the step right before human review).
- `--provider lmstudio|claude|gemini` — defaults to `$CVTAILOR_LLM_PROVIDER`, itself defaulting to `lmstudio` (a local model via LM Studio, no cloud spend). Switching providers triggers a fresh shared ingest automatically.
- `--force` — ignore "already done" checks and redo it (meaning varies slightly by script — see each script's `--help`).

## If something's stuck

- **`py_pipeline_assemble.py` reports "0/0 resume edits applied"**: check `applications/<jd_name>/review_decisions.json` — if it's an empty list, nothing was approved during stage 8 (most often: you hit `q` before approving/editing anything). Re-run stage 8 for that JD and actually approve or edit at least one thing.
- **An edit never shows up at review at all, or you see fewer resume edits than expected**: stage 6 automatically discards resume edits that stage 9 has no way to apply — anything that isn't a straight in-place reword of real existing text (e.g. a proposed reorder, insertion, or deletion) gets caught and logged to `discarded_edits.jsonl` before it ever reaches you. This is intentional, not a bug — those edit types would silently do nothing even if approved. Separately, stage 7's critique can legitimately cut every edit stage 6 passed, leaving 0 real edits — `py_pipeline_status.py` reports this distinctly (`critiqued: 0 edits survived`), not as "verified, go review."
- **A JD never leaves intake**: it hasn't been fully assembled yet. Check `python src/checkpoints/py_pipeline_status.py` for what stage it's actually at.
- **`style_profile_LEAK_WARNING.json` exists** in `applications/_shared/`: auto-redaction couldn't fully resolve a flagged item — hand-edit `applications/_shared/style_profile.json`, delete the warning file, re-run.
- **A stage fails with a schema/validation error**: retried automatically once with corrective feedback to the model; if it still fails after that, check `logs/` for the stage's JSONL entry and re-run — transient model errors don't usually repeat twice.
- **`run_pipeline.py` hangs for many minutes on one JD, `--provider lmstudio`**: check LM Studio's own log (Developer tab) for `tg = X t/s` — if it's single digits, the loaded model doesn't fit your GPU's VRAM and is decoding on CPU; switch to a smaller model that fully fits, and pin it with `$env:LMSTUDIO_MODEL` (see `docs/setup/LM_STUDIO.md`). If it's stuck before generation even starts, it may be the stage 3 DuckDuckGo search step instead — that has its own 10s timeout per query, so it shouldn't hang, but a `WARNING: search failed` in the output is expected/harmless if a query just came back empty.
