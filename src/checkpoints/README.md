# src/checkpoints/

Read-only validation/reporting scripts. None of these mutate pipeline
state -- run any of them any time to check where things stand.

* **`py_pipeline_precheck.py`** — validates `config/` and your environment
  (API keys, dependencies, LM Studio reachability) *before* you spend
  anything. Run this first on a fresh setup.
* **`py_pipeline_status.py`** — reports the current stage of every active
  application, straight from filesystem state.
* **`py_pipeline_prereview_summary.py`** — a funnel summary across all
  prepared applications, run before a big stage 8 review session to spot
  red flags (zero surviving edits, thin ledgers, low JD coverage) before
  spending review time on them.
* **`py_post_pipeline_verify_materials.py`** — the automated Tier 1
  "Definition of Done" gate: scans assembled documents for missing
  keywords, leaked placeholders, and page-limit violations before you
  actually send anything.
