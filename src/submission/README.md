# src/submission/

The auto-submit lane -- fills and submits ATS application forms via
Playwright, adapted from [alpha-hunt](https://github.com/mr-ravi26/alpha-hunt)'s
`src/apply.py`. Kept separate from `src/controls/` since this is the one
script in the pipeline that can take an irreversible external action
(actually submitting a real application) rather than just mutating local
state.

* **`py_submit_application.py`** — only ever submits when an application
  passes `src/checkpoints/py_post_pipeline_verify_materials.py`'s
  Definition-of-Done gate completely AND its company is marked
  `auto_submit: true` in `config/target_companies.yaml`. Anything else
  is silently left for the existing manual path
  (`src/controls/py_post_pipeline_open_app_urls.py`) — this script never
  lowers that bar, and never guesses when a form field's association
  with a generated answer is unclear (skips it -- and thus the whole
  application -- to `needs_review` instead).

Requires `playwright install chromium` once (see repo root
`requirements.txt` for the Python package itself). Run with `--dry-run`
first, and with `config/submission.yaml`'s `headless: false` for your
first real run against any new company/ATS combo -- watch the browser
actually fill the form before trusting it unattended.
