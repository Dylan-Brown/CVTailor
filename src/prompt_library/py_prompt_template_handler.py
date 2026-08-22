"""
py_prompt_template_handler.py — one generalized loader for every system
prompt in the pipeline, replacing what used to be a big triple-quoted
string constant re-invented per stage file. Why this matters (not just
tidiness): modularity (a prompt can be read/edited/reviewed without
touching the Python around it), readability (a stage file's own logic
isn't buried under 150 lines of prompt text), and standardizability
(every stage loads a prompt exactly the same way, instead of each
inventing its own f-string-interpolation convention).

Prompt files live under prompt_library/, mirroring src/'s own
subfolders (prompt_library/stages/stage05_propose_updates/gen_system.txt
backs src/stages/py_stage05_propose_updates.py, etc.) so it's always
obvious which prompt belongs to which script.

Usage:
    from py_prompt_template_handler import load_prompt
    GEN_SYSTEM = load_prompt("stages/stage05_propose_updates/gen_system.txt")

Supports simple {placeholder} substitution via render() for the rare
prompt that genuinely needs a value baked in at load time (most of
this pipeline's prompts are static and just use load_prompt() directly
-- stage-specific per-call values are interpolated into the USER
message elsewhere, not the system prompt, so render() exists for
future use more than current need).
"""
from __future__ import annotations
from pathlib import Path

PROMPT_LIBRARY_ROOT = Path(__file__).resolve().parent
_cache: dict[str, str] = {}


def load_prompt(relative_path: str) -> str:
    """Reads prompt_library/<relative_path> as UTF-8 text, trailing
    newline stripped (a template file naturally ends with one; the
    system prompt constants this replaces never did). Cached per
    process -- a stage script loading the same prompt more than once
    (unlikely, but free to guard against) doesn't re-hit disk."""
    if relative_path in _cache:
        return _cache[relative_path]
    path = PROMPT_LIBRARY_ROOT / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"No prompt template at {path} (looked for prompt_library/{relative_path})")
    text = path.read_text(encoding="utf-8").rstrip("\n")
    _cache[relative_path] = text
    return text


def render(relative_path: str, **values) -> str:
    """load_prompt() plus str.format(**values) -- for a template that
    uses {placeholder} syntax and needs values baked in at load time
    rather than interpolated into the user message."""
    return load_prompt(relative_path).format(**values)
