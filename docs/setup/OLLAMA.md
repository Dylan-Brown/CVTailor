# Ollama

Not currently wired up. `src/core/llm_dispatch.py` supports exactly
three providers today — `lmstudio` (local, OpenAI-compatible server),
`claude`, and `gemini` — see `call_llm_structured()`'s provider
dispatch. Ollama also exposes an OpenAI-compatible endpoint
(`http://localhost:11434/v1` by default), so adding it would likely
mean pointing `LMSTUDIO_BASE_URL` (or a new, more accurately-named env
var) at Ollama's endpoint rather than writing a fourth
`_call_*_structured()` path from scratch — worth checking whether
Ollama's tool-calling support is reliable enough for this pipeline's
structured-output requirements before doing that, since local models
vary widely there (see LM_STUDIO.md's note on the same issue).

This file exists as a placeholder for that decision, not as working
setup instructions yet.
