#!/usr/bin/env python3
"""
py_poll_ats_boards.py -- Discovery lane: polls the curated companies in
config/target_companies.yaml via ats_boards.py's public Greenhouse/Lever/
Ashby APIs, and drops any not-already-seen posting into applications/ as
a ready-to-drain raw JD JSON. run_pipeline.py's drain_raw_intake() picks
these up automatically on its next run -- no new intake plumbing needed.

Usage:
    python src/sourcing/py_poll_ats_boards.py [--dry-run] [--max-jobs-per-board N]

Intended to run on a schedule (Windows Task Scheduler, same mechanism
util/Register-FileWatcher.ps1 sets up for the file watcher). See
docs/ALPHA_HUNT_GH_ACTIONS_FALLBACK.md for a cloud-scheduled fallback if a
target ATS starts blocking the local poller's IP.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
sys.path.append(str(Path(__file__).resolve().parent.parent / "util"))
sys.path.append(str(Path(__file__).resolve().parent.parent))
from batch_common import APPLICATIONS_ROOT, SHARED_DIR, CONFIG_ROOT, load_applicant_info
from io_utils import save_model, log_event
from schemas import JDInput
from py_notify_telegram import send_telegram_message
from sourcing.scorer import check_disqualifiers, job_dict_from_posting
from sourcing.config import ScoringConfig

from ats_boards import fetch_greenhouse_jobs, fetch_lever_jobs, fetch_ashby_jobs

TARGET_COMPANIES_PATH = CONFIG_ROOT / "target_companies.yaml"
SEEN_POSTINGS_PATH = SHARED_DIR / "seen_postings.json"

_BOARD_FETCHERS = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
}


def slugify(text: str) -> str:
    """Same rule py_stage00_normalize_jd_input.py uses for app_ids --
    kept consistent so a posting dropped here lands in an
    applications/<id>/ folder that looks like every other intake path."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "_", text).strip("_")


def load_target_companies() -> dict[str, list[tuple[str, str]]]:
    """{"greenhouse": [(name, token), ...], ...} from
    config/target_companies.yaml. auto_submit flags are read separately
    by py_submit_application.py at submission time -- discovery doesn't
    need them."""
    if not TARGET_COMPANIES_PATH.is_file():
        print(f"{TARGET_COMPANIES_PATH} not found -- copy "
              f"config/target_companies.example.yaml to target_companies.yaml "
              f"and curate your own company list first.", file=sys.stderr)
        sys.exit(1)
    raw = yaml.safe_load(TARGET_COMPANIES_PATH.read_text(encoding="utf-8")) or {}
    return {
        board: [(c["name"], c["token"]) for c in raw.get(board, [])]
        for board in _BOARD_FETCHERS
    }


def load_keywords() -> list[str]:
    """job_scoring.target_titles from config/applicant_info.json, reused
    rather than a second hardcoded keyword list. Errors loudly on empty
    rather than silently polling and matching nothing -- ats_boards.py's
    _matches_keywords() with an empty list matches no posting at all."""
    job_scoring = load_applicant_info().get("job_scoring", {})
    keywords = list(job_scoring.get("target_titles", []))
    if not keywords:
        print("config/applicant_info.json's job_scoring.target_titles is empty -- "
              "the poller has no keywords to match postings against and would "
              "silently discover nothing. Add at least one target title first.",
              file=sys.stderr)
        sys.exit(1)
    return keywords


def load_seen() -> dict[str, str]:
    if not SEEN_POSTINGS_PATH.is_file():
        return {}
    try:
        return json.loads(SEEN_POSTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def mark_seen(seen: dict[str, str], source_url: str) -> None:
    seen[source_url] = time.strftime("%Y-%m-%dT%H:%M:%S")
    SEEN_POSTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_POSTINGS_PATH.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def drop_posting(posting: dict, dry_run: bool, cfg: ScoringConfig) -> str | None:
    """Validates against JDInput and writes a raw JD JSON straight into
    applications/ root. Returns the posting's source_url on success (the
    dedup key), or None if it failed validation or is hard-disqualified
    (location, title keyword, or another DISQUALIFIER_PATTERNS hit --
    checked here so a bad-fit posting never gets a file written at all,
    rather than relying on stage 1's advisory-only score later)."""
    posting = dict(posting)
    posting.setdefault("extraction_method", "ats_api")

    disq = check_disqualifiers(job_dict_from_posting(posting), cfg)
    if disq:
        _, reason = disq
        print(f"[skip] {posting.get('company')} / {posting.get('role_title')}: {reason}",
              file=sys.stderr)
        return None

    try:
        jd = JDInput.model_validate(posting)
    except Exception as e:
        print(f"[skip] {posting.get('company')} / {posting.get('role_title')}: "
              f"failed JDInput validation: {e}", file=sys.stderr)
        return None

    company_slug, role_slug = slugify(jd.company), slugify(jd.role_title)
    out_path = APPLICATIONS_ROOT / f"{company_slug}_{role_slug}.json"
    if dry_run:
        print(f"[dry-run] would write {out_path}")
        return jd.source_url

    n = 1
    while out_path.exists():
        # Same company+role already dropped and not yet drained -- suffix
        # rather than silently overwriting a not-yet-processed file.
        out_path = APPLICATIONS_ROOT / f"{company_slug}_{role_slug}_{n}.json"
        n += 1

    save_model(out_path, jd)
    print(f"[new] {jd.company} / {jd.role_title} -> {out_path.name}")
    return jd.source_url


def load_scoring_config() -> ScoringConfig:
    return ScoringConfig.from_dict(load_applicant_info().get("job_scoring", {}))


def run(max_jobs_per_board: int, dry_run: bool) -> None:
    companies = load_target_companies()
    keywords = load_keywords()
    seen = load_seen()
    cfg = load_scoring_config()

    new_count = 0
    new_labels = []
    for board, fetcher in _BOARD_FETCHERS.items():
        board_companies = companies.get(board, [])
        if not board_companies:
            continue
        try:
            postings = list(fetcher(keywords, max_jobs_per_board, companies=board_companies))
        except Exception as e:
            print(f"[FAILED] {board}: {e}", file=sys.stderr)
            continue

        for posting in postings:
            source_url = posting.get("source_url")
            if not source_url or source_url in seen:
                continue
            written_url = drop_posting(posting, dry_run, cfg)
            if written_url:
                new_count += 1
                new_labels.append(f"{posting.get('company')} / {posting.get('role_title')}")
                if not dry_run:
                    mark_seen(seen, written_url)

    posting_word = "posting" if new_count == 1 else "postings"
    log_event("ats_poll", {"new_postings": new_count, "dry_run": dry_run})
    print(f"\n{new_count} new {posting_word} dropped into {APPLICATIONS_ROOT}/"
          f"{' (dry-run, nothing written)' if dry_run else ''}")

    if new_count and not dry_run:
        listing = "\n".join(f"- {label}" for label in new_labels[:20])
        more = f"\n...and {new_count - 20} more" if new_count > 20 else ""
        send_telegram_message(
            f"{new_count} new {posting_word} discovered\n{listing}{more}"
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-jobs-per-board", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true",
                     help="Fetch and validate, but don't write files or update seen_postings.json.")
    args = ap.parse_args()
    run(args.max_jobs_per_board, args.dry_run)


if __name__ == "__main__":
    main()
