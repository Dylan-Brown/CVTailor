#!/usr/bin/env python3
"""
py_stage03_research_jd_company.py -- Claude web_search research call on
the target company, scoped to this specific JD. Findings carry
source_url citations forward for review-time verification.
"""

import argparse
import sys
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/stages/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
sys.path.append(str(_JOBS_ROOT / "src" / "prompt_library"))
from llm_dispatch import call_llm_research_structured, UNTRUSTED_CONTENT_PREAMBLE, default_provider
from io_utils import save_model, load_model, log_event
from schemas import CompanyBrief, ClaimsLedger, JDInput
from py_prompt_template_handler import load_prompt

RESEARCH_SYSTEM = load_prompt("stages/stage03_research_jd_company/research_system.txt")

# Only used for Gemini's second call (structuring free-text research notes
# into CompanyBrief) — Claude does search + structuring in one call, so it
# never touches this prompt. See call_llm_research_structured.
STRUCTURE_SYSTEM = load_prompt("stages/stage03_research_jd_company/structure_system.txt")


def run(jd_input_path: str, claims_ledger_path: str, out_dir: str, dry_run: bool, provider: str):
    jd = load_model(jd_input_path, JDInput)
    ledger = load_model(claims_ledger_path, ClaimsLedger)
    skills = [c.text for c in ledger.claims if c.category == "skill"]

    structured_facts = (
        f"Company: {jd.company}\nRole: {jd.role_title}\n"
        f"Location: {jd.location or 'unspecified'} ({jd.remote_type})\n"
        f"Employment type: {jd.employment_type or 'unspecified'}\n"
        f"Salary: {jd.salary_min}-{jd.salary_max} {jd.salary_currency or ''} "
        f"per {jd.salary_period or 'unspecified'}\n"
        f"Posting URL: {jd.source_url}"
    )

    user_prompt = (
        f"{structured_facts}\n\n"
        f"{UNTRUSTED_CONTENT_PREAMBLE}\n"
        f"--- BEGIN SCRAPED JOB DESCRIPTION ---\n"
        f"{jd.full_description_text}\n"
        f"--- END SCRAPED JOB DESCRIPTION ---\n\n"
        f"Applicant's current skill set (for relevance filtering, not "
        f"for you to comment on directly):\n{skills}\n\n"
        f"Research this company and role. Prioritize anything that "
        f"would change how someone tailors a resume for THIS opening "
        f"specifically, not general company facts."
    )

    if dry_run:
        placeholder = CompanyBrief(
            company=jd.company,
            role_title=jd.role_title,
            stack_signals=["DRY-RUN PLACEHOLDER — no research performed"],
            open_questions=["DRY-RUN PLACEHOLDER"],
        )
        save_model(Path(out_dir) / "company_brief.json", placeholder, dry_run=False)
        print(f"[dry-run] wrote PLACEHOLDER company_brief.json to {out_dir} — no API call made "
              f"(inputs loaded fine: {jd.company} / {jd.role_title}, "
              f"{len(ledger.claims)} ledger claims). This lets stage 5 validate its wiring "
              f"without spending anything; it is not real research.")
        return

    brief = call_llm_research_structured(
        research_system=RESEARCH_SYSTEM,
        research_user=user_prompt,
        structure_system=STRUCTURE_SYSTEM,
        response_model=CompanyBrief,
        company=jd.company,
        role_title=jd.role_title,
        provider=provider,
    )
    brief.company = brief.company or jd.company
    brief.role_title = brief.role_title or jd.role_title

    save_model(Path(out_dir) / "company_brief.json", brief, dry_run)
    log_event("stage3_research", {
        "company": brief.company,
        "n_findings": len(brief.culture_notes) + len(brief.recent_news) + len(brief.compensation_notes),
        "dry_run": dry_run, "provider": provider,
    })
    print(f"Research complete -> {Path(out_dir) / 'company_brief.json'}")


def main(app_id: str | None, jd_input: str | None, claims_ledger: str | None, out_dir: str | None,
         force: bool, dry_run: bool, provider: str):
    """Two ways to invoke this script, not one replacing the other:

    1. EXPLICIT PATHS (--jd-input + --claims-ledger + --out-dir together)
       -- unchanged from before, this is what run_pipeline.py's own
       subprocess calls use internally. Still required to work exactly
       as it always has; removing it would break batch orchestration.
    2. SIMPLIFIED (--app-id, or nothing at all for every live app) --
       auto-derives jd_input/out_dir from the app folder itself and the
       claims ledger via the same ensure_variant_ingest() +
       pick_ledger_for_jd() routing every other stage already uses, so
       there's no real reason this needed manual paths for standalone
       use in the first place. Skips apps that already have a
       company_brief.json unless --force is passed, matching the
       skip-if-done convention the rest of this pipeline follows.
    """
    if jd_input or claims_ledger or out_dir:
        if not (jd_input and claims_ledger and out_dir):
            print("--jd-input, --claims-ledger, and --out-dir must be given together "
                  "(this is the explicit-path mode run_pipeline.py uses internally). "
                  "For standalone use, pass --app-id instead, or nothing at all to run "
                  "every application.", file=sys.stderr)
            sys.exit(1)
        run(jd_input, claims_ledger, out_dir, dry_run, provider)
        return

    from batch_common import app_dirs, find_jd_input, ensure_variant_ingest, pick_ledger_for_jd, APPLICATIONS_ROOT

    targets = [APPLICATIONS_ROOT / app_id] if app_id else app_dirs()
    if app_id and not targets[0].is_dir():
        print(f"No applications/{app_id}/ folder found.", file=sys.stderr)
        sys.exit(1)

    ledger_paths, _style_path, _cover_letter_path = ensure_variant_ingest(
        force=False, dry_run=dry_run, provider=provider)

    ran, skipped = 0, 0
    for app_dir in targets:
        jd_path = find_jd_input(app_dir)
        if jd_path is None:
            continue  # stage 0 never ran for this folder -- nothing to research yet
        if not force and (app_dir / "company_brief.json").is_file():
            skipped += 1
            continue
        jd = load_model(jd_path, JDInput)
        ledger_path, variant_used, _reasoning = pick_ledger_for_jd(jd, ledger_paths)
        print(f"\n=== {app_dir.name} (resume variant: {variant_used}) ===")
        try:
            run(str(jd_path), str(ledger_path), str(app_dir), dry_run, provider)
            ran += 1
        except Exception as e:
            print(f"[FAILED] {app_dir.name}: {e}", file=sys.stderr)

    print(f"\n{ran} researched, {skipped} already had a company_brief.json (use --force to redo)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-id", default=None,
                     help="Research just this one application. Omit to research every live application.")
    ap.add_argument("--jd-input", default=None, help="Explicit path -- used internally by run_pipeline.py")
    ap.add_argument("--claims-ledger", default=None, help="Explicit path -- used internally by run_pipeline.py")
    ap.add_argument("--out-dir", default=None, help="Explicit path -- used internally by run_pipeline.py")
    ap.add_argument("--force", action="store_true", help="Re-research even if company_brief.json already exists")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--provider", default=default_provider(), choices=["lmstudio", "claude", "gemini"],
                     help="Defaults to $CVTAILOR_LLM_PROVIDER, or 'lmstudio' (a local model, no cloud spend) if that's unset.")
    args = ap.parse_args()
    main(args.app_id, args.jd_input, args.claims_ledger, args.out_dir, args.force, args.dry_run, args.provider)
