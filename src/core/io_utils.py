#!/usr/bin/env python3
"""
io_utils.py — JSON model load/save, JSONL audit logging, and the
applications/<app_id>/ directory helper. Split out of pipeline_common.py.
"""

from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path
from typing import Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

LOG_DIR = Path(os.environ.get("PIPELINE_LOG_DIR", "./logs"))


def log_event(stage_name: str, event: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "stage": stage_name, **event}
    with open(LOG_DIR / f"{stage_name}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_model(path: str | Path, model: Type[T]) -> T:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return model.model_validate(data)


def save_model(path: str | Path, obj: BaseModel, dry_run: bool = False) -> None:
    payload = obj.model_dump_json(indent=2)
    if dry_run:
        print(f"[dry-run] would write {path} ({len(payload)} bytes)", file=sys.stderr)
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(payload, encoding="utf-8")


def append_jsonl(path: str | Path, obj: BaseModel, dry_run: bool = False) -> None:
    """Append one record to a .jsonl audit file (as opposed to
    save_model, which overwrites a single JSON document)."""
    line = obj.model_dump_json()
    if dry_run:
        print(f"[dry-run] would append to {path}: {line[:80]}...", file=sys.stderr)
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def application_dir(applications_root: str | Path, app_id: str) -> Path:
    d = Path(applications_root) / app_id
    d.mkdir(parents=True, exist_ok=True)
    return d
