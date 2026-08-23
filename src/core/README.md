# src/core/

Shared helpers every stage, checkpoint, and control script imports.
Split out of one large `pipeline_common.py` file when it grew too big to
navigate; each module here has one clear job.

* **`batch_common.py`** — the biggest one: `config/` folder conventions,
  resume-variant discovery/routing, the `applications/`-as-queue model.
* **`schemas.py`** — every Pydantic model the pipeline passes between
  stages as JSON (`JDInput`, `ClaimsLedger`, `CandidateEdit`, etc.).
* **`llm_dispatch.py`** — multi-provider LLM calling (LM Studio / Claude /
  Gemini), structured-output helpers, and API key resolution.
* **`io_utils.py`** — JSON model load/save and JSONL audit logging.
* **`docx_utils.py`** — python-docx helpers: paragraph iteration, in-place
  text replacement, PDF conversion, typography compression.
* **`text_matching.py`** — quote normalization and fuzzy text-span lookup,
  used by `docx_utils.py` and `prose.py` to locate edit text in a document.
* **`prose.py`** — job-title simplification, and the shared "is this edit
  about the company/role rather than the candidate" detector (used by
  stages 6 and 7).
* **`py_logger.py`** — one consistent console log format across every
  script, so a batch run reads as one stream.
* **`py_run_stage.py`** — the "given one stage + one app, run it as a
  subprocess" primitive every orchestrating script builds on.

Nothing in here is pipeline-*stage* logic -- that stays in `src/stages/`.
This is orchestration/plumbing only.
