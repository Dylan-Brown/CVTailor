#!/usr/bin/env python3
"""
py_check_telegram_filter_commands.py -- checks for a Telegram reply
starting with "/filter <free text>" and, if found, uses an LLM to turn
that free text into new hard title-exclusion keywords (appended to
config/applicant_info.json's job_scoring.excluded_title_keywords),
re-scans every not-yet-processed applications/ posting against the
updated filter, prunes anything that now matches, and replies on
Telegram confirming what changed.

Intended to run every cycle alongside the discovery pollers (see
util/Run-DiscoveryAndNotify.ps1) -- NOT a standalone always-on listener,
so a /filter reply can take up to one poll interval to be picked up.

Usage:
    python src/util/py_check_telegram_filter_commands.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import requests
from pydantic import BaseModel, Field

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
sys.path.append(str(Path(__file__).resolve().parent.parent))
from batch_common import (
    APPLICANT_INFO_PATH, APPLICATIONS_ROOT, CONFIG_ROOT, SHARED_DIR,
    app_dirs, find_jd_input, is_processed, load_applicant_info,
)
from io_utils import load_model, log_event
from schemas import JDInput
from llm_dispatch import call_llm_structured
from py_notify_telegram import load_config as load_telegram_config, send_telegram_message
from sourcing.scorer import check_disqualifiers, job_dict_from_posting, job_dict_from_jd_input
from sourcing.config import ScoringConfig

LAST_UPDATE_ID_PATH = SHARED_DIR / "telegram_filter_last_update_id.json"
_CREDENTIALS_DIR = CONFIG_ROOT / "credentials"
_PLACEHOLDER_PREFIX = "PLACEHOLDER_REPLACE_WITH_YOUR_REAL_"


def _load_credential(env_var: str, filename: str) -> str | None:
    """Same convention as py_poll_adzuna.py/py_notify_telegram.py: $env:<var>
    wins if set, otherwise the first non-comment, non-blank,
    non-placeholder line of config/credentials/<filename>."""
    if os.environ.get(env_var):
        return os.environ[env_var]
    path = _CREDENTIALS_DIR / filename
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return None if line.startswith(_PLACEHOLDER_PREFIX) else line
    return None

FILTER_SYSTEM = """You turn a job-seeker's short, informal instruction about job postings they
don't want to see into a list of hard title-exclusion keywords.

Rules:
- Only extract keywords that clearly describe a JOB TITLE or ROLE TYPE to exclude
  (e.g. "Networking", "Marketing", "Recruiter", "Sales"). Never extract company
  names, locations, salary figures, or seniority numbers -- those aren't handled
  by this mechanism.
- A keyword should be a single word or very short phrase that would appear
  in a real job title, matched case-insensitively as a whole word against
  the title. Prefer the most specific short word (e.g. "Marketing" not
  "Marketing roles").
- Skip anything already in the current excluded list below -- only return
  NEW keywords.
- If the message doesn't actually ask to exclude any role/title, return an
  empty list -- don't invent something to return.
- summary must be one short plain sentence describing what you did (or
  that nothing changed), written as a direct reply to the person, e.g.
  "Added Recruiter and Sales as excluded titles." or "Didn't find a
  title/role to exclude in that message -- no changes made."
"""


class FilterUpdate(BaseModel):
    new_excluded_keywords: list[str] = Field(default_factory=list)
    summary: str


def _get_updates(token: str, offset: int | None) -> list[dict]:
    params = {"timeout": 5}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("result", [])


def _load_last_update_id() -> int | None:
    if not LAST_UPDATE_ID_PATH.is_file():
        return None
    try:
        return json.loads(LAST_UPDATE_ID_PATH.read_text(encoding="utf-8")).get("last_update_id")
    except Exception:
        return None


def _save_last_update_id(update_id: int) -> None:
    LAST_UPDATE_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_UPDATE_ID_PATH.write_text(json.dumps({"last_update_id": update_id}), encoding="utf-8")


def _apply_keywords(new_keywords: list[str], dry_run: bool) -> list[str]:
    """Merges new_keywords into applicant_info.json's excluded_title_keywords
    (case-insensitive dedup). Returns the ones actually added (i.e. not
    already present)."""
    if not new_keywords:
        return []
    data = json.loads(APPLICANT_INFO_PATH.read_text(encoding="utf-8"))
    job_scoring = data.setdefault("job_scoring", {})
    existing = job_scoring.setdefault("excluded_title_keywords", [])
    existing_lower = {k.lower() for k in existing}

    added = []
    for kw in new_keywords:
        if kw.lower() not in existing_lower:
            existing.append(kw)
            existing_lower.add(kw.lower())
            added.append(kw)

    if added and not dry_run:
        APPLICANT_INFO_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return added


def _prune_matching_pending(cfg: ScoringConfig, dry_run: bool) -> list[str]:
    """Re-scans everything not yet processed against the updated cfg and
    deletes anything that now hard-disqualifies. Only touches: (a)
    un-drained raw JD JSON files sitting at applications/ root, and (b)
    applications/<id>/ folders with a jd_input.json but no
    verified_edits.json yet -- i.e. nothing built on top of them yet.
    Never touches an already-processed or already-in-progress app."""
    removed = []

    if APPLICATIONS_ROOT.is_dir():
        for raw_file in sorted(p for p in APPLICATIONS_ROOT.iterdir()
                                if p.is_file() and p.suffix.lower() == ".json"):
            try:
                posting = json.loads(raw_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            disq = check_disqualifiers(job_dict_from_posting(posting), cfg)
            if disq:
                _, reason = disq
                label = f"{posting.get('company')} / {posting.get('role_title')}"
                removed.append(f"{label} ({reason})")
                if not dry_run:
                    raw_file.unlink()

    for d in app_dirs():
        if is_processed(d) or (d / "verified_edits.json").is_file():
            continue
        jd_path = find_jd_input(d)
        if jd_path is None:
            continue
        jd = load_model(jd_path, JDInput)
        disq = check_disqualifiers(job_dict_from_jd_input(jd), cfg)
        if disq:
            _, reason = disq
            removed.append(f"{jd.company} / {jd.role_title} ({reason})")
            if not dry_run:
                shutil.rmtree(d)

    return removed


def run(dry_run: bool) -> None:
    cfg_tg = load_telegram_config()
    if not cfg_tg.get("enabled", True):
        return
    token = _load_credential("TELEGRAM_BOT_TOKEN", "telegram_bot_token.txt")
    chat_id = cfg_tg.get("chat_id")
    if not token or not chat_id:
        return  # same "not configured" case py_notify_telegram.py already warns about elsewhere

    last_id = _load_last_update_id()
    offset = (last_id + 1) if last_id is not None else None
    try:
        updates = _get_updates(token, offset)
    except Exception as e:
        print(f"[telegram-filter] getUpdates failed: {e}", file=sys.stderr)
        return
    if not updates:
        return

    highest_id = max(u["update_id"] for u in updates)

    filter_texts = []
    for u in updates:
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue
        text = (msg.get("text") or "").strip()
        if text.lower().startswith("/filter"):
            filter_texts.append(text[len("/filter"):].strip())

    if not dry_run:
        _save_last_update_id(highest_id)

    for filter_text in filter_texts:
        if not filter_text:
            continue
        applicant = load_applicant_info()
        job_scoring = applicant.get("job_scoring", {})
        cfg = ScoringConfig.from_dict(job_scoring)

        prompt = (
            f"Current excluded title keywords: {cfg.excluded_title_keywords}\n"
            f"Current not_good_fit categories: {job_scoring.get('not_good_fit', [])}\n\n"
            f"The person just sent this /filter instruction:\n\"{filter_text}\""
        )
        try:
            result = call_llm_structured(system=FILTER_SYSTEM, user=prompt,
                                          response_model=FilterUpdate, tier="cheap")
        except Exception as e:
            print(f"[telegram-filter] LLM call failed: {e}", file=sys.stderr)
            send_telegram_message(f"Couldn't apply that /filter -- LLM call failed: {e}")
            continue

        added = _apply_keywords(result.new_excluded_keywords, dry_run)
        cfg.excluded_title_keywords = list(cfg.excluded_title_keywords) + added
        removed = _prune_matching_pending(cfg, dry_run) if added else []

        log_event("telegram_filter", {
            "filter_text": filter_text, "added_keywords": added,
            "removed_count": len(removed), "dry_run": dry_run,
        })

        reply = result.summary
        if removed:
            posting_word = "posting" if len(removed) == 1 else "postings"
            listing = "\n".join(f"- {label}" for label in removed[:20])
            more = f"\n...and {len(removed) - 20} more" if len(removed) > 20 else ""
            reply += f"\nRemoved {len(removed)} already-pending {posting_word}:\n{listing}{more}"
        send_telegram_message(reply)
        print(f"[telegram-filter] {filter_text!r} -> added {added}, removed {len(removed)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                     help="Don't write config, delete files, or advance the update-id marker.")
    args = ap.parse_args()
    run(args.dry_run)


if __name__ == "__main__":
    main()
