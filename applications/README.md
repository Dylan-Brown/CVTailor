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
Processed applications are moved here by `py_post_pipeline_store_applied.py`:
* **`/applied`**: Successfully submitted applications.
* **`/cut_off`**: Abandoned or filtered-out opportunities (each gets a `cutoff_explanation.txt`).
* **`/revisit`**: Applications queued for manual attention or later follow-up.
* **`/test`**: Sandbox for testing pipeline prompts or local models.
* **`apps_moved.log`**: Timestamped move history, used by `py_applications_stats.py --archive` for weekly velocity.
* **`definition_of_done_report.json`**: Written by `py_post_pipeline_verify_materials.py`.

### 4. `/archived/` (note: distinct from `/archive/` above)
Where `py_pipeline_store_archive.py` moves a fully-done application as one
packed-up folder (jd folder + its `generated_materials/`). Permanent --
never touched by `py_pipeline_reset.py`.