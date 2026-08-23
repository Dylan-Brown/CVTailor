# src/adapters/

Translation layers between an external data source and the pipeline's
own schemas -- kept separate from `src/stages/` since these aren't
pipeline stages themselves, just format-normalization glue.

* **`harvester_adapter.py`** — normalizes the raw JSON the Chrome
  extension ("Job Data Harvester") produces into the strict `JDInput`
  schema stage 0 expects: parses free-text salary strings into
  structured min/max/currency/period, reassembles the extension's
  split text fields into one `full_description_text` block, and stamps
  metadata (`scraped_at`, `source_url`, intake timing).
