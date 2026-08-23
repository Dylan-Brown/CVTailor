#!/usr/bin/env python3
"""
py_stage04_analyze_requirement_gaps.py — Stage 4: Gap Analysis
==============================================================
Diffs the job description requirements against your claims ledger to produce 
an `EditBrief`. Identifies which requirements are covered, missing, or over-indexed.

Usage:
    python py_stage04_analyze_requirement_gaps.py \
        --jd-input path/to/jd_input.json \
        --claims-ledger path/to/claims_ledger.json \
        --out-dir applications/...
"""
import argparse
import sys
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/stages/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
sys.path.append(str(_JOBS_ROOT / "src" / "prompt_library"))
from llm_dispatch import call_llm_structured, UNTRUSTED_CONTENT_PREAMBLE, default_provider
from io_utils import save_model, load_model, log_event
from prose import extract_required_keywords, classify_keyword_gaps
from schemas import EditBrief, ClaimsLedger, JDInput
from py_prompt_template_handler import load_prompt

GAP_SYSTEM = load_prompt("stages/stage04_analyze_requirement_gaps/gap_system.txt")


def _format_list_section(label: str, items: list[str]) -> str:
    if items:
        return f"{label}:\n" + "\n".join(f"- {i}" for i in items)
    return f"{label}: none provided"


def run(jd_input_path: str, claims_ledger_path: str, out_dir: str, dry_run: bool, provider: str):
    jd = load_model(jd_input_path, JDInput)
    ledger = load_model(claims_ledger_path, ClaimsLedger)
    claims_blob = "\n".join(f"[{c.claim_id}] ({c.section}) {c.text}" for c in ledger.claims)

    structured_sections = "\n\n".join([
        _format_list_section("Pre-extracted Requirements (primary)", jd.requirements_raw),
        _format_list_section("Pre-extracted Responsibilities (primary, equal weight)", jd.responsibilities_raw),
        _format_list_section("Pre-extracted Nice-to-have (primary, lower priority)", jd.nice_to_have_raw),
    ])

    user_prompt = (
        f"Role: {jd.role_title} at {jd.company}\n\n{structured_sections}\n\n"
        f"{UNTRUSTED_CONTENT_PREAMBLE}\n"
        f"--- BEGIN SCRAPED JOB DESCRIPTION (context only — see weighting rules) ---\n"
        f"{jd.full_description_text}\n"
        f"--- END SCRAPED JOB DESCRIPTION ---\n\n"
        f"Claims ledger:\n{claims_blob}"
    )

    if dry_run:
        placeholder = EditBrief(
            requirements=[],
            coverage_score=0.0,
            gaps=["DRY-RUN PLACEHOLDER — no gap analysis performed"],
        )
        save_model(Path(out_dir) / "edit_brief.json", placeholder, dry_run=False)
        print(f"[dry-run] wrote PLACEHOLDER edit_brief.json to {out_dir} — no API call made "
              f"(inputs loaded fine: {jd.company} / {jd.role_title}, "
              f"{len(ledger.claims)} ledger claims). This lets stage 5 validate its wiring "
              f"without spending anything; it is not a real gap analysis.")
        return

    brief = call_llm_structured(
        system=GAP_SYSTEM,
        user=user_prompt,
        response_model=EditBrief,
        tier="standard",
        provider=provider,
    )

    # Deterministic, no LLM call -- as early in the pipeline as this can
    # happen, right here where JD requirements and the claims ledger are
    # both already loaded. See core/prose.classify_keyword_gaps for
    # why these two cases are handled completely differently downstream.
    keywords = extract_required_keywords(brief.model_dump())
    actionable, unsupported = classify_keyword_gaps(keywords, ledger.claims)
    brief.keyword_gaps_actionable = actionable
    brief.keyword_gaps_unsupported = unsupported

    save_model(Path(out_dir) / "edit_brief.json", brief, dry_run)
    log_event("stage4_gap_analysis", {
        "coverage_score": brief.coverage_score, "n_gaps": len(brief.gaps), "dry_run": dry_run, "provider": provider,
        "keyword_gaps_actionable": len(actionable), "keyword_gaps_unsupported": len(unsupported),
    })
    print(f"Coverage {brief.coverage_score:.0%}, {len(brief.gaps)} gaps, "
          f"{len(actionable)} keyword(s) to surface, {len(unsupported)} unsupported -> "
          f"{Path(out_dir) / 'edit_brief.json'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd-input", required=True)
    ap.add_argument("--claims-ledger", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--provider", default=default_provider(), choices=["lmstudio", "claude", "gemini"],
                     help="Defaults to $CVTAILOR_LLM_PROVIDER, or 'lmstudio' (a local model, no cloud spend) if that's unset.")
    args = ap.parse_args()
    run(args.jd_input, args.claims_ledger, args.out_dir, args.dry_run, args.provider)