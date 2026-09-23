# src/controls/

Scripts that mutate pipeline state -- assembling final output, resetting
an application, or moving it between folders. Run these deliberately,
usually as the next explicit step after a checkpoint script tells you
where things stand.

* **`py_pipeline_assemble.py`** — assembles every reviewed application
  into `applications/<jd_name>/generated_materials/`, converts to PDF,
  and marks it processed.
* **`py_pipeline_reset.py`** — resets one (or every) JD's downstream
  state so it starts clean on the next `run_pipeline.py --force`.
* **`py_post_pipeline_store_applied.py`** — archives one application (by
  prefix) or every live application (`--all`, warns and confirms first)
  into `applications/archive/<outcome>/` -- applied (default), cut_off,
  revisit, test, or done (fully finished, no specific outcome). Logs
  every move for `py_applications_stats.py`'s weekly breakdown.
* **`py_post_pipeline_open_app_urls.py`** — opens every live application's
  `source_url` in your browser, for a final human look before sending.
