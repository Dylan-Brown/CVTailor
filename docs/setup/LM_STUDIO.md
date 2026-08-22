# LM Studio setup

The default provider (`CVTAILOR_LLM_PROVIDER` unset, or `lmstudio`) —
no API key, no cloud spend, nothing leaves your machine.

1. Install [LM Studio](https://lmstudio.ai/) and load a model that
   supports tool/function calling reliably — structured output here
   depends on it (see `src/core/llm_dispatch.py`'s tool-forcing
   pattern). A model that ignores or mangles tool calls will fail
   validation repeatedly and burn retries.
2. Developer tab -> Start Server. Default: `http://localhost:1234/v1`.
3. Run `python src/checkpoints/py_pipeline_precheck.py` — it checks the
   server is reachable and a model is actually loaded, not just that
   the process is running.

## Env vars

- `LMSTUDIO_BASE_URL` — override if LM Studio is on a different
  host/port. Default `http://localhost:1234/v1`.
- `LMSTUDIO_MODEL` — pin an exact model id if you're serving more than
  one. Without this, the pipeline uses whichever model `/v1/models`
  lists first — not reliable if your catalog has more than one model
  downloaded but only one actually loaded.

## If a stage hangs or times out

Check LM Studio's own log (Developer tab) for `tg = X t/s`. Single
digits usually means the loaded model doesn't fit your GPU's VRAM and
is decoding on CPU — switch to a smaller model that fully fits.
