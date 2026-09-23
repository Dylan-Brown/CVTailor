# CVTailor Applications Queue & Archive

This directory serves two distinct purposes: it is the **active queue** where new job descriptions are dropped for processing, and the **permanent archive** where your historical applications are stored.

## Directory Structure

### 1. The Active Queue (`/<app_id>`)
When the Chrome extension or manual intaker drops a raw job description JSON here, the pipeline automatically creates a dedicated subdirectory (e.g., `company_role_date/`). 
* Holds all intermediate artifacts (`edit_brief.json`, `candidate_edits.json`, etc.).
* Assembled PDFs and Word documents appear inside nested `generated_materials/`.

### 2. The Shared Cache (`/_shared/`)
Holds extracted data for your base materials, organized by resume variant:
* **`claims_ledger.json`**: Verified atomic resume facts.
* **`style_profile.json`**: Writing style extracted from your cover letter sample.
* **`.source_hashes.json`**: Tracks base document state to trigger re-extraction when resumes change.

### 3. The Archive (`/archive/`)
Processed applications are moved here by `py_post_pipeline_store_applied.py`
-- one app at a time by prefix, or every live app at once via `--all`
(warns and requires typing `yes` before touching anything):
* **`/applied`**: Successfully submitted applications (default outcome; adds
  a row to your configured Application Progress.md, if any).
* **`/cut_off`**: Abandoned or filtered-out opportunities (each gets a `cutoff_explanation.txt`).
* **`/revisit`**: Applications queued for manual attention or later follow-up.
* **`/test`**: Sandbox for testing pipeline prompts or local models.
* **`/done`**: Fully finished, nothing further to record -- no outcome
  category, no Application Progress.md row. This absorbed what used to
  be a separate `/archived/` folder and `py_pipeline_store_archive.py`
  script; the two were doing the same "move a finished application
  folder" operation with less nuance, so they were merged in.
* **`apps_moved.log`**: Timestamped move history, used by `py_applications_stats.py --archive` for weekly velocity.
* **`definition_of_done_report.json`**: Written by `py_post_pipeline_verify_materials.py`.

All five outcome folders are equally permanent -- never touched by
`py_pipeline_reset.py` or `run_pipeline.py` once an application lands
in one of them.