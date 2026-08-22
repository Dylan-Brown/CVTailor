# Choosing a local model

Every stage's structured output depends on the loaded model reliably
honoring forced tool/function calls (see `src/core/llm_dispatch.py`'s
`_call_lmstudio_structured()` — it retries with corrective feedback on
a schema mismatch, but a model that can't follow the schema at all
will exhaust those retries and fail the stage).

What matters, in order:

1. **Tool/function calling support** — not every local model has this,
   and support quality varies even among models that claim it. If a
   stage repeatedly fails with "Model never called return_data" (see
   `_call_lmstudio_structured`'s error message), the loaded model is
   the likely cause, not a bug in the pipeline.
2. **Fits your GPU's VRAM entirely** — see LM_STUDIO.md's note on
   `tg = X t/s` in LM Studio's log. A model that spills onto CPU
   decoding is usually too slow to be worth using here, even if it's
   otherwise a better model.
3. **Context length** — stage 5 (`py_stage05_propose_updates.py`) is
   the heaviest single call (claims ledger + style profile + edit
   brief + company brief + full cover letter baseline, all in one
   prompt). A short context window will truncate or fail on a large
   resume/claims ledger before a smaller-but-longer-context model
   would.

No specific model is recommended here deliberately — this changes
faster than a doc can track. Whatever you pick, run
`python src/checkpoints/py_pipeline_precheck.py` and a `--dry-run`
batch first to confirm it's actually usable before spending real time
on a full run.
