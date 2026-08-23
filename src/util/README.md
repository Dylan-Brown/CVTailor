# src/util/

Standalone Python scripts you run by hand, outside the normal per-JD
pipeline flow. None of these run automatically as part of `run_pipeline.py`.

* **`py_answer_question.py`** — Generates a grounded answer to a free-form
  application screening question (e.g. "Why are you interested in this
  role?"), sourced entirely from your claims ledger.
* **`py_applications_stats.py`** — Throughput/velocity metrics: pending
  count, weekly applied/cut-off/revisit breakdown, resume-variant split,
  file-watcher run timings. See `python src/util/py_applications_stats.py --all`.
* **`py_applications_archive_cleanup.py`** — Reclaims disk space by deleting
  intermediate JSON artifacts from `applications/archived/`, keeping the
  final generated materials, the original job posting, and your review
  decisions.
* **`py_zip_materials.py`** — Bundles every generated resume/cover letter
  across the live `applications/` queue into one ZIP for review or sharing.
* **`py_export_jd_input_schema.py`** — Regenerates `contracts/jd_input.schema.json`
  from the `JDInput` model in `src/core/schemas.py`, so the two can't drift
  apart. Run after changing that model.
* **`py_fix_signature_overlap.py`** — One-off remediation for cover letters
  where typography compression shrank spacing around the signature image;
  fixes just the paragraphs around the signature without touching the rest
  of the document's compression.

See `util/README.md` (top-level, not this folder) for the PowerShell
automation scripts -- file watcher registration and the one-click pipeline
runner. Those are a separate, Windows-specific concern from the Python
utilities here.
