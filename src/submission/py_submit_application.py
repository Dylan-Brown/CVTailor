#!/usr/bin/env python3
"""
py_submit_application.py -- the submission lane: fills and submits ATS
application forms with Playwright. Adapted from alpha-hunt
(https://github.com/mr-ravi26/alpha-hunt)'s src/apply.py -- same
selector-based fill and same "confidently answerable, or don't submit"
gate, with three substitutions:

  - resume comes from that application's own generated_materials/, not
    one static resume_path.
  - unrecognized required questions fall through to
    src/util/py_answer_question.py's grounded, per-JD answer generator
    (backed by your claims ledger) instead of a static qa.yaml lookup --
    and, unlike alpha-hunt's own apply.py, the matched/generated answer
    actually gets filled into its associated field rather than only
    gating whether to submit.
  - static, genuinely non-per-JD questions (work authorization, etc.)
    check config/applicant_info.json's static_answers first, before
    ever calling an LLM.

Every assembled application gets its Definition-of-Done gate checked on
every run (see py_post_pipeline_verify_materials.py's check_app()), and
the first time one goes fully green a Telegram notification fires --
regardless of auto_submit -- so nothing sits "ready" without you knowing.

Actually auto-submitting a given application additionally requires ALL of:
  1. config/submission.yaml's `enabled: true` -- a global kill-switch
     checked before per-company allowlisting, off by default.
  2. Belongs to a company marked `auto_submit: true` in
     config/target_companies.yaml.
  3. Passes the Definition-of-Done gate completely (see above).
Anything short of all three is left alone for the existing manual path
(py_post_pipeline_open_app_urls.py) -- this script never lowers that bar,
and never guesses when a field's association with a value is unclear.

Usage:
    python src/submission/py_submit_application.py [--app-id <id>] [--dry-run]
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
sys.path.append(str(Path(__file__).resolve().parent.parent / "util"))
sys.path.append(str(Path(__file__).resolve().parent.parent / "checkpoints"))
from batch_common import APPLICATIONS_ROOT, CONFIG_ROOT, SHARED_DIR, app_dirs, find_jd_input, load_applicant_info
from io_utils import log_event
from py_answer_question import get_answer
from py_notify_telegram import send_telegram_message
from py_post_pipeline_verify_materials import (
    check_app, RESUME_MAX_PAGES_DEFAULT, COVER_LETTER_MAX_PAGES_DEFAULT,
)
from py_run_stage import sh_optional

TARGET_COMPANIES_PATH = CONFIG_ROOT / "target_companies.yaml"
SUBMISSION_CONFIG_PATH = CONFIG_ROOT / "submission.yaml"
NOTIFIED_READY_PATH = SHARED_DIR / "notified_ready.json"
STORE_APPLIED_SCRIPT = Path(__file__).resolve().parent.parent / "controls" / "py_post_pipeline_store_applied.py"


def load_allowlist() -> dict[str, bool]:
    """{company_name: auto_submit} across every board type in
    config/target_companies.yaml. Missing file/company -> not
    allowlisted, never assume yes."""
    if not TARGET_COMPANIES_PATH.is_file():
        return {}
    raw = yaml.safe_load(TARGET_COMPANIES_PATH.read_text(encoding="utf-8")) or {}
    allowlist = {}
    for board_companies in raw.values():
        for c in board_companies:
            allowlist[c["name"]] = bool(c.get("auto_submit", False))
    return allowlist


def load_submission_config() -> dict:
    if not SUBMISSION_CONFIG_PATH.is_file():
        return {"enabled": False, "max_applications_per_run": 5, "headless": True}
    return yaml.safe_load(SUBMISSION_CONFIG_PATH.read_text(encoding="utf-8")) or {}


def load_notified_ready() -> set[str]:
    """app_ids already Telegram-notified as DoD-green, so a re-run (e.g.
    every 15 min via Task Scheduler) doesn't re-ping the same app."""
    if not NOTIFIED_READY_PATH.is_file():
        return set()
    try:
        return set(json.loads(NOTIFIED_READY_PATH.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_notified_ready(notified: set[str]) -> None:
    NOTIFIED_READY_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTIFIED_READY_PATH.write_text(json.dumps(sorted(notified), indent=2), encoding="utf-8")


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) > 1 else (parts[0], "")


def find_resume_pdf(app_dir: Path) -> Path | None:
    matches = list((app_dir / "generated_materials").glob("* - Resume.pdf"))
    return matches[0] if matches else None


def match_static_answer(question_text: str, applicant: dict) -> str | None:
    """Zero-LLM path for genuinely static/factual recurring questions
    (work authorization, sponsorship) -- see applicant_info.example.json's
    static_answers. Checked before ever calling get_answer()."""
    q = question_text.lower()
    for phrase, answer in applicant.get("static_answers", {}).items():
        if phrase.lower() in q:
            return answer
    return None


def _fill_matched_field(page, label_el, answer: str) -> bool:
    """Finds the field associated with a matched label and fills it.
    Only trusts an explicit <label for=...> id association, or a
    textarea/input/select inside the same immediate container -- never
    a page-wide '.first' guess, which would route every question's
    answer into whichever field happens to come first on the page."""
    field = None
    try:
        for_id = label_el.get_attribute("for")
    except Exception:
        for_id = None
    if for_id:
        candidate = page.locator(f'#{for_id}')
        if candidate.count() > 0:
            field = candidate.first
    if field is None:
        try:
            container = label_el.locator("xpath=ancestor::*[self::div or self::fieldset][1]")
            if container.count() > 0:
                nearby = container.first.locator("textarea, input[type='text'], select")
                if nearby.count() > 0:
                    field = nearby.first
        except Exception:
            field = None
    if field is None or field.count() == 0:
        return False
    try:
        tag = field.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            field.select_option(label=answer)
        else:
            field.fill(answer)
        return True
    except Exception:
        return False


def apply_to_job(app_dir: Path, jd: dict, applicant: dict, headless: bool) -> tuple[str, str]:
    """Adapted from alpha-hunt's src/apply.py:apply_to_job() -- see the
    module docstring for what changed. Returns (status, detail) where
    status is one of: "submitted", "needs_review", "error"."""
    resume_pdf = find_resume_pdf(app_dir)
    first_name, last_name = split_name(applicant["name"])

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page()
            page.goto(jd["source_url"], timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)

            # Some ATS pages (Ashby, some Greenhouse boards) show the job
            # description first and only reveal the form after an "Apply"
            # trigger click.
            apply_trigger = page.get_by_role("link", name=re.compile(r"^apply", re.I)).first
            if apply_trigger.count() == 0:
                apply_trigger = page.get_by_role("button", name=re.compile(r"^apply", re.I)).first
            if apply_trigger.count() > 0:
                try:
                    apply_trigger.click(timeout=5000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass  # already on the form, or trigger wasn't real

            unanswered = []

            def fill_if_present(selectors: list[str], value: str):
                if not value:
                    return
                for sel in selectors:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            el.fill(value)
                            return
                    except Exception:
                        continue

            fill_if_present(['input[name*="first_name" i]', 'input[id*="first_name" i]'], first_name)
            fill_if_present(['input[name*="last_name" i]', 'input[id*="last_name" i]'], last_name)
            fill_if_present(['input[type="email"]', 'input[name*="email" i]'], applicant.get("email", ""))
            fill_if_present(['input[type="tel"]', 'input[name*="phone" i]'], applicant.get("phone", ""))
            fill_if_present(['input[name*="linkedin" i]', 'input[id*="linkedin" i]'], applicant.get("linkedin_url", ""))
            fill_if_present(['input[name*="website" i]', 'input[name*="portfolio" i]'], applicant.get("portfolio_url", ""))

            if resume_pdf:
                try:
                    file_input = page.locator('input[type="file"]').first
                    if file_input.count() > 0:
                        file_input.set_input_files(str(resume_pdf))
                    else:
                        unanswered.append("resume upload field not found")
                except Exception:
                    unanswered.append("resume upload field not found")
            else:
                unanswered.append("no generated_materials/*-Resume.pdf found for this application")

            # --- Custom questions: match, generate if needed, and fill ---
            question_blocks = page.locator("label, .application-question, [class*='question' i]")
            count = min(question_blocks.count(), 60)  # safety cap
            for i in range(count):
                label_el = question_blocks.nth(i)
                try:
                    text = label_el.inner_text(timeout=1000).strip()
                except Exception:
                    continue
                if not text or len(text) > 200:
                    continue
                if not ("?" in text or re.search(r"\brequired\b|\*", text, re.I)):
                    continue

                answer = match_static_answer(text, applicant)
                if answer is None:
                    try:
                        answer, _ = get_answer(text, app_id=app_dir.name)
                    except SystemExit:
                        answer = None  # grounding data unavailable -- don't guess
                    except Exception as e:
                        print(f"  [warn] couldn't generate an answer for {text!r}: {e}", file=sys.stderr)
                        answer = None

                if answer is None or not _fill_matched_field(page, label_el, answer):
                    unanswered.append(text)

            if unanswered:
                browser.close()
                return "needs_review", f"Unanswered fields: {'; '.join(unanswered[:5])}"

            # --- Submit ---
            submit_btn = page.locator('button[type="submit"], input[type="submit"]').first
            if submit_btn.count() == 0:
                submit_btn = page.get_by_role(
                    "button", name=re.compile(r"submit application|submit|apply now", re.I)
                ).first
            if submit_btn.count() == 0:
                browser.close()
                return "needs_review", "Could not find submit button"

            submit_btn.scroll_into_view_if_needed(timeout=5000)
            submit_btn.click(timeout=10000)
            page.wait_for_timeout(3000)  # let confirmation page load

            browser.close()
            return "submitted", "ok"

    except PWTimeout:
        return "error", "Page load timed out"
    except Exception as e:
        return "error", str(e)


def run(app_filter: str | None, dry_run: bool) -> None:
    allowlist = load_allowlist()
    submission_cfg = load_submission_config()
    enabled = submission_cfg.get("enabled", False)
    max_apps = submission_cfg.get("max_applications_per_run", 5)
    headless = submission_cfg.get("headless", True)
    applicant = load_applicant_info()
    notified_ready = load_notified_ready()

    candidates = [APPLICATIONS_ROOT / app_filter] if app_filter else app_dirs()
    submitted_count = 0

    for app_dir in candidates:
        if not app_dir.is_dir() or not (app_dir / "generated_materials").is_dir():
            continue  # not fully assembled yet -- nothing to submit

        jd_path = find_jd_input(app_dir)
        if not jd_path:
            continue
        jd = json.loads(jd_path.read_text(encoding="utf-8"))
        company = jd.get("company")

        # DoD gate is checked for every assembled app regardless of
        # auto_submit/enabled -- notification shouldn't depend on whether
        # this app was ever going to be allowed to auto-submit.
        result = check_app(app_dir, RESUME_MAX_PAGES_DEFAULT, COVER_LETTER_MAX_PAGES_DEFAULT)
        (app_dir / "definition_of_done_report.json").write_text(json.dumps(result, indent=2))
        if not result["all_passed"]:
            failed = [c["check"] for c in result["checks"] if not c["passed"]]
            print(f"[not ready] {app_dir.name}: Definition-of-Done gate not fully green "
                  f"({', '.join(failed)}).")
            continue

        if app_dir.name not in notified_ready:
            notified_ready.add(app_dir.name)
            save_notified_ready(notified_ready)
            send_telegram_message(
                f"{app_dir.name} ({company}) is ready to submit -- "
                f"Definition-of-Done gate green."
            )

        if not enabled:
            print(f"[ready, gated] {app_dir.name}: auto-submit disabled globally "
                  f"(config/submission.yaml enabled: false) -- leaving for manual apply.")
            continue
        if not allowlist.get(company, False):
            print(f"[ready, manual] {app_dir.name}: {company!r} isn't auto_submit: true "
                  f"in config/target_companies.yaml -- leaving for manual apply.")
            continue
        if submitted_count >= max_apps:
            print(f"[ready, capped] {app_dir.name}: hit max_applications_per_run "
                  f"({max_apps}) already this run -- leaving for manual apply.")
            continue

        if dry_run:
            print(f"[dry-run] would submit {app_dir.name} ({company}) now.")
            continue

        print(f"\n=== Submitting {app_dir.name} ({company}) ===")
        status, detail = apply_to_job(app_dir, jd, applicant, headless)
        log_event("submission", {"app_id": app_dir.name, "company": company,
                                  "status": status, "detail": detail})

        if status == "submitted":
            submitted_count += 1
            print(f"[submitted] {app_dir.name}")
            sh_optional([sys.executable, str(STORE_APPLIED_SCRIPT), app_dir.name])
            send_telegram_message(f"Submitted {app_dir.name} ({company}).")
        elif status == "needs_review":
            print(f"[needs review] {app_dir.name}: {detail}")
        else:
            print(f"[error] {app_dir.name}: {detail}", file=sys.stderr)

    print(f"\n{submitted_count} application(s) submitted this run.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app-id", default=None, help="Scope to one application; default is every eligible one.")
    ap.add_argument("--dry-run", action="store_true",
                     help="Check the allowlist and Definition-of-Done gate, but never open a "
                          "browser or submit anything.")
    args = ap.parse_args()
    run(args.app_id, args.dry_run)


if __name__ == "__main__":
    main()
