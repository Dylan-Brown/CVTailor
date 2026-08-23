# Local Models Setup

## Choosing a Local Model
Every stage's structured output depends on the model reliably honoring forced tool/function calls.
1. **Tool/function calling support:** If a stage repeatedly fails with "Model never called return_data", the model is the issue, not the pipeline.
2. **VRAM constraints:** The model must fit entirely in your GPU's VRAM.
3. **Context length:** Stage 5 is heavy (ledger + style profile + edit brief + company brief + full cover letter) and requires a sufficient context window.

## LM Studio (Default)
The default provider (`CVTAILOR_LLM_PROVIDER` unset, or `lmstudio`) costs nothing and keeps your data local.
1. Install LM Studio and load a compatible model.
2. Start the local server (Default: `http://localhost:1234/v1`).
3. Run `python src/checkpoints/py_pipeline_precheck.py` to verify connection.

**Environment Variables:**
*   `LMSTUDIO_BASE_URL`: Override if on a different host/port.
*   `LMSTUDIO_MODEL`: Pin an exact model ID. Without this, the pipeline uses whichever model `/v1/models` lists first, which is unreliable.

**Troubleshooting:**
If a stage hangs, check LM Studio's log for `tg = X t/s`. Single digits indicate CPU decoding; switch to a smaller model.

## Ollama
Not currently wired up. Ollama exposes an OpenAI-compatible endpoint (default `http://localhost:11434/v1`), so future support requires pointing `LMSTUDIO_BASE_URL` to it and verifying its tool-calling reliability. This serves as a placeholder for that decision.