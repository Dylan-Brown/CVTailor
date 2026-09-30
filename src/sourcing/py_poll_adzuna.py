#!/usr/bin/env python3
"""
py_poll_adzuna.py -- optional, notify-only broad-market lane. Separate
from the curated Greenhouse/Lever/Ashby discovery lane
(py_poll_ats_boards.py): this one searches Adzuna's aggregated listings
across whatever companies post there, and never auto-submits or feeds
run_pipeline.py directly -- it just appends matches to
log/adzuna_matches.jsonl for you to look at.

Usage:
    python src/sourcing/py_poll_adzuna.py [--max-results N]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
sys.path.append(str(Path(__file__).resolve().parent.parent / "util"))
sys.path.append(str(Path(__file__).resolve().parent.parent))
from batch_common import CONFIG_ROOT, SHARED_DIR, JOBS_ROOT, load_applicant_info
from io_utils import log_event
from py_notify_telegram import send_telegram_message
from sourcing.scorer import check_disqualifiers
from sourcing.config import ScoringConfig

ADZUNA_CONFIG_PATH = CONFIG_ROOT / "adzuna.yaml"
SEEN_ADZUNA_PATH = SHARED_DIR / "seen_adzuna.json"
CREDENTIALS_DIR = CONFIG_ROOT / "credentials"
_PLACEHOLDER_PREFIX = "PLACEHOLDER_REPLACE_WITH_YOUR_REAL_"


def _load_credential(env_var: str, filename: str) -> str | None:
    """$env:<env_var> wins if set; otherwise the first non-comment,
    non-blank, non-placeholder line of config/credentials/<filename>."""
    if os.environ.get(env_var):
        return os.environ[env_var]
    path = CREDENTIALS_DIR / filename
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return None if line.startswith(_PLACEHOLDER_PREFIX) else line
    return None


def load_config() -> dict:
    if not ADZUNA_CONFIG_PATH.is_file():
        print(f"{ADZUNA_CONFIG_PATH} not found -- copy config/adzuna.example.yaml "
              f"to adzuna.yaml first.", file=sys.stderr)
        sys.exit(1)
    return yaml.safe_load(ADZUNA_CONFIG_PATH.read_text(encoding="utf-8")) or {}


def load_seen() -> set[str]:
    if not SEEN_ADZUNA_PATH.is_file():
        return set()
    try:
        return set(json.loads(SEEN_ADZUNA_PATH.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    SEEN_ADZUNA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_ADZUNA_PATH.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def run(max_results: int) -> None:
    app_id = _load_credential("ADZUNA_APP_ID", "adzuna_app_id.txt")
    app_key = _load_credential("ADZUNA_APP_KEY", "adzuna_app_key.txt")
    if not app_id or not app_key:
        print("Adzuna credentials not set -- fill in config/credentials/"
              "adzuna_app_id.txt and adzuna_app_key.txt (or set $env:ADZUNA_APP_ID / "
              "$env:ADZUNA_APP_KEY).", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    country = cfg.get("country", "us")
    where = cfg.get("where", "")

    job_scoring = load_applicant_info().get("job_scoring", {})
    keywords = list(job_scoring.get("target_titles", []))
    if not keywords:
        print("job_scoring.target_titles is empty in config/applicant_info.json -- "
              "nothing to search Adzuna for.", file=sys.stderr)
        sys.exit(1)
    min_salary = job_scoring.get("min_salary")
    cfg = ScoringConfig.from_dict(job_scoring)

    seen = load_seen()
    log_path = JOBS_ROOT / "log" / "adzuna_matches.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    new_matches = 0
    new_labels = []
    for keyword in keywords:
        params = {
            "app_id": app_id, "app_key": app_key,
            "what": keyword, "results_per_page": max_results,
            "content-type": "application/json",
        }
        if where:
            params["where"] = where
        if min_salary:
            params["salary_min"] = min_salary

        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as e:
            print(f"[FAILED] Adzuna search for {keyword!r}: {e}", file=sys.stderr)
            continue

        for job in results:
            job_id = job.get("id")
            if not job_id or job_id in seen:
                continue

            title = job.get("title") or ""
            company_name = (job.get("company") or {}).get("display_name") or ""
            location_name = (job.get("location") or {}).get("display_name") or ""

            disq = check_disqualifiers(
                {"title": title, "company": company_name, "location": location_name,
                 "description": job.get("description") or ""},
                cfg,
            )
            if disq:
                seen.add(job_id)  # still mark seen -- no point re-checking a bad-fit match every poll
                _, reason = disq
                print(f"[skip] {title} @ {company_name}: {reason}", file=sys.stderr)
                continue

            seen.add(job_id)
            new_matches += 1
            new_labels.append(f"{title} @ {company_name}")
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "keyword": keyword,
                "title": title,
                "company": company_name,
                "location": location_name,
                "salary_min": job.get("salary_min"),
                "salary_max": job.get("salary_max"),
                "url": job.get("redirect_url"),
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(f"[match] {entry['title']} @ {entry['company']} -- {entry['url']}")

    match_word = "match" if new_matches == 1 else "matches"
    save_seen(seen)
    log_event("adzuna_poll", {"new_matches": new_matches})
    print(f"\n{new_matches} new {match_word} appended to {log_path}")

    if new_matches:
        listing = "\n".join(f"- {label}" for label in new_labels[:20])
        more = f"\n...and {new_matches - 20} more" if new_matches > 20 else ""
        send_telegram_message(
            f"(Adzuna) {new_matches} new {match_word}\n{listing}{more}"
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-results", type=int, default=20)
    args = ap.parse_args()
    run(args.max_results)


if __name__ == "__main__":
    main()
