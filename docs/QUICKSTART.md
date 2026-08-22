# Quickstart — everyday commands

All commands run from `jobs/` (the folder this file's parent sits in).

## Reset an app to re-run it

    python src\controls\py_pipeline_reset.py --jd <app_id>

## Take unprocessed apps and prepare edits (resume + cover letter)

Runs stage 0 intake for anything new, stage 2 ingest (once per resume
variant), then stages 3-7 (research, gap analysis, generate, verify,
critique) per app internally — no need to call any of those separately.

    python run_pipeline.py --force

## Check where every app currently sits in the pipeline

    python src\checkpoints\py_pipeline_status.py

## Sanity-check what prepare produced BEFORE spending review time on it

Edit counts, zero-edit apps, thin ledgers, low JD coverage.

    python src\checkpoints\py_pipeline_prereview_summary.py

## Review edits one by one

    python src\stages\py_stage08_agentic_update_review.py

(or `python src\ai_wrappers\py_stage08_reviewer_interface.py` to let
pipeline.yaml's Review Stage Assignee pick between you and an AI
reviewer)

## Output final resume and cover letter to use when applying

Runs stage 9 assemble internally.

    python src\controls\py_pipeline_assemble.py

Already assembled and just hand-edited a `.docx` in
`generated_materials/`? Pass `--app-id <prefix>[,<prefix2>,...]`
instead to skip straight to reconverting that app's EXISTING `.docx`
to a fresh PDF (never re-runs stage 9, so your edits aren't
overwritten):

    python src\controls\py_pipeline_assemble.py --app-id <app_id_prefix>

## Final sanity check before sending anything

`--app-id <app_id>` for one app, `--all` for every app.

    python src\checkpoints\py_post_pipeline_verify_materials.py --app-id <app_id>

## Answer a screening question during the application itself

`--app-id` accepts a unique substring of the full app_id, not just the
exact folder name.

    python src\util\py_answer_question.py --question "<question>" --app-id "<ex: hims_hers>"
