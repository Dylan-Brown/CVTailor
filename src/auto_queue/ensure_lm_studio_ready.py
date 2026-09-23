#!/usr/bin/env python3
"""
ensure_lm_studio_ready.py — Auto-starts LM Studio and loads the required model.

Ensures the LM Studio application is running, the local server is active, 
and the specified model is fully loaded into memory *before* the CVTailor 
pipeline triggers. This prevents the pipeline from failing due to cold starts.

Uses the native `lms` CLI tool (which ships with LM Studio) for execution.
Configuration is driven by `jobs/config/file_watcher.json`.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_LMSTUDIO_BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
DEFAULT_MODEL_ID = os.environ.get("LMSTUDIO_MODEL", "qwen2.5-14b-instruct")
DEFAULT_EXE_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "LM Studio" / "LM Studio.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "LM-Studio" / "LM Studio.exe",
]
DEFAULT_APP_LAUNCH_TIMEOUT = 90     # seconds to wait for the app to boot / `lms server start` to succeed
DEFAULT_MODEL_LOAD_TIMEOUT = 600    # seconds -- a 14B model loading cold from disk is not fast
DEFAULT_POLL_INTERVAL = 2


def _server_reachable(base_url: str, timeout: float = 3.0) -> bool:
    """The one check that actually matches what the pipeline depends on --
    the same /v1/models endpoint llm_dispatch.py's _lmstudio_resolve_model()
    hits."""
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _app_process_running() -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq LM Studio.exe"],
        capture_output=True, text=True,
    )
    return "LM Studio.exe" in result.stdout


def _launch_app(exe_candidates: list[Path]) -> None:
    exe = next((p for p in exe_candidates if p.is_file()), None)
    if exe is None:
        raise RuntimeError(
            "LM Studio.exe not found in the configured install locations "
            f"({[str(p) for p in exe_candidates]}). Install it from https://lmstudio.ai/ "
            "or update lm_studio.exe_candidates in file_watcher.json."
        )
    print(f"[lm_studio] launching {exe}...")
    subprocess.Popen([str(exe)], creationflags=subprocess.DETACHED_PROCESS)


def _lms_server_start() -> bool:
    """Idempotent -- exits 0 whether or not a server was already running."""
    try:
        result = subprocess.run(["lms", "server", "start"], capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _loaded_model_identifiers() -> set[str]:
    """`lms ps --json` -- models actually loaded into memory right now.
    Deliberately NOT /v1/models: with JIT model loading on, that
    endpoint lists every downloaded model, not just what's loaded."""
    try:
        result = subprocess.run(["lms", "ps", "--json"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    try:
        loaded = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    return {m.get("identifier", "") for m in loaded}


def _lms_unload_model(model_id: str) -> bool:
    try:
        result = subprocess.run(["lms", "unload", model_id], capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _evict_other_models(model_id: str) -> None:
    """`lms load` never evicts anything else -- models pile up in memory
    across runs made with different model_id values (e.g. -Model) until
    they're competing for VRAM/RAM and inference stalls. Best-effort:
    a failed unload just gets logged, never blocks loading model_id."""
    for loaded_id in _loaded_model_identifiers():
        if loaded_id == model_id:
            continue
        print(f"[lm_studio] unloading stray model {loaded_id!r} to free VRAM/RAM...")
        if not _lms_unload_model(loaded_id):
            print(f"[lm_studio] `lms unload {loaded_id}` failed -- continuing anyway.")


def _lms_load_model(model_id: str, timeout: int) -> bool:
    print(f"[lm_studio] loading {model_id!r} (this can take a while for a 14B model)...")
    try:
        result = subprocess.run(
            ["lms", "load", model_id, "--yes"],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "`lms` isn't on PATH even though the app is running -- install LM Studio from "
            "https://lmstudio.ai/ and run it once manually; the first launch bootstraps the "
            "`lms` CLI onto PATH."
        )
    except subprocess.TimeoutExpired:
        print(f"[lm_studio] `lms load {model_id}` didn't finish within {timeout}s.")
        return False
    if result.returncode != 0:
        print(f"[lm_studio] `lms load {model_id}` failed:\n{result.stdout}\n{result.stderr}")
        return False
    return True


def ensure_lm_studio_ready(
    model_id: str = DEFAULT_MODEL_ID,
    base_url: str = DEFAULT_LMSTUDIO_BASE_URL,
    exe_candidates: list[Path] | None = None,
    app_launch_timeout: int = DEFAULT_APP_LAUNCH_TIMEOUT,
    model_load_timeout: int = DEFAULT_MODEL_LOAD_TIMEOUT,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
) -> bool:
    """Makes sure LM Studio's server is up AND `model_id` is loaded,
    launching/starting/loading whatever piece is missing. Returns True
    once actually ready, False if it gave up.

    IMPORTANT for whoever spawns the actual pipeline subprocess: set
    LMSTUDIO_MODEL=<model_id> in its environment before running
    run_pipeline.py -- loading the right model here doesn't make the
    pipeline USE it.
    """
    exe_candidates = exe_candidates or DEFAULT_EXE_CANDIDATES

    if _server_reachable(base_url):
        _evict_other_models(model_id)
        if model_id in _loaded_model_identifiers():
            print(f"[lm_studio] already serving {model_id!r} -- nothing to do.")
            return True

    if not _app_process_running():
        _launch_app(exe_candidates)

    deadline = time.monotonic() + app_launch_timeout
    while not _lms_server_start():
        if time.monotonic() >= deadline:
            print(f"[lm_studio] `lms server start` still failing after {app_launch_timeout}s -- giving up.")
            return False
        time.sleep(poll_interval)

    deadline = time.monotonic() + app_launch_timeout
    while not _server_reachable(base_url):
        if time.monotonic() >= deadline:
            print(f"[lm_studio] server accepted `lms server start` but {base_url} never became "
                  f"reachable within {app_launch_timeout}s -- giving up.")
            return False
        time.sleep(poll_interval)
    print("[lm_studio] server is up.")

    _evict_other_models(model_id)

    if model_id not in _loaded_model_identifiers():
        if not _lms_load_model(model_id, model_load_timeout):
            return False

    ready = _server_reachable(base_url) and model_id in _loaded_model_identifiers()
    print(f"[lm_studio] {'ready' if ready else 'NOT ready'} -- model={model_id!r}")
    return ready


if __name__ == "__main__":
    import sys
    sys.exit(0 if ensure_lm_studio_ready() else 1)
