#!/usr/bin/env python3
"""
py_prompt_template_handler.py -- load_prompt() reads every system
prompt from prompt_library/*.txt (mirrors src/'s subfolder layout).
render() adds {placeholder} substitution for the rare prompt that needs it.
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
