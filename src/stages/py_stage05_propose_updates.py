#!/usr/bin/env python3
"""
py_stage05_propose_updates.py — propose targeted edits to both the resume and
the candidate's existing cover letter, constrained to the claims ledger.

The cover letter is treated as a BASELINE DOCUMENT — the candidate's
own writing, not a template to be rebuilt. Edits are targeted swaps
(verbatim original_text → suggested_text), same mechanics as resume
edits. The letter's narrative structure, tense, and voice are
preserved by default; only the parts that need to change for THIS
specific job get touched.

This stage is intentionally NOT trusted on its own — every edit it
produces goes through stage 6's verifier and stage 7's utility
critic before a human ever sees it. The system prompt below is a
guardrail, not a guarantee.

Usage:
    python py_stage05_propose_updates.py \
        --claims-ledger applications/.../claims_ledger.json \
        --style-profile applications/.../style_profile.json \
        --edit-brief applications/.../edit_brief.json \
        --company-brief applications/.../company_brief.json \
        --cover-letter-baseline config/cover_letter_sample/... \
        --out-dir applications/... [--dry-run]
"""
import argparse
import sys
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/stages/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
sys.path.append(str(_JOBS_ROOT / "src" / "prompt_library"))
from llm_dispatch import call_llm_structured, default_provider
from io_utils import save_model, load_model, log_event
from schemas import ClaimsLedger, StyleProfile, EditBrief, CompanyBrief, CandidateEdit
from py_prompt_template_handler import load_prompt
from pydantic import BaseModel


class CandidateEditBatch(BaseModel):
    edits: list[CandidateEdit]


GEN_SYSTEM = load_prompt("stages/stage05_propose_updates/gen_system.txt")


def _extract_cover_letter_body(sample_path: str) -> str:
    """Extracts just the letter body — everything between the salutation
    and the sign-off. The header (name, address, title, contact info,
    date, Re: line) and footer (Best, / signature / name) are handled
    by the template, not by stage 5's edits. Also strips layout
    annotations like ((Left Side...)) and template placeholders in the
    header section like {{DATE ...}}, {{Backend | Fullstack}}, etc."""
    text = Path(sample_path).read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    body_start = None
    body_end = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Salutation can be "Dear ...", "Good ...", or the template
        # placeholder form "{{Good (Morning | ..."
        if (stripped.startswith("Dear ") or stripped.startswith("Good ")
                or stripped.startswith("{{Good")):
            body_start = i + 1
        if stripped in ("Best,", "Sincerely,", "Regards,", "Thank you,"):
            body_end = i
            break
    if body_start is None:
        body_start = 0
    if body_end is None:
        body_end = len(lines)
    while body_start < body_end and not lines[body_start].strip():
        body_start += 1
    while body_end > body_start and not lines[body_end - 1].strip():
        body_end -= 1
    return "\n".join(lines[body_start:body_end])


def run(claims_ledger_path, style_profile_path, edit_brief_path,
        company_brief_path, cover_letter_baseline_path, out_dir, dry_run, provider):
    ledger = load_model(claims_ledger_path, ClaimsLedger)
    style = load_model(style_profile_path, StyleProfile)
    edit_brief = load_model(edit_brief_path, EditBrief)
    company = load_model(company_brief_path, CompanyBrief)

    cover_letter_body = _extract_cover_letter_body(cover_letter_baseline_path)

    claims_blob = "\n".join(f"[{c.claim_id}] {c.text}" for c in ledger.claims)

    user_prompt = f"""Claims ledger (the ONLY source of factual content you may draw from):
{claims_blob}

Style profile: {style.model_dump_json()}
Edit brief (gaps to address, sections to de-emphasize): {edit_brief.model_dump_json()}
Company brief (for framing/language, not for new facts): {company.model_dump_json()}

COVER_LETTER_BASELINE (the candidate's existing letter — propose targeted swaps against this text):
{cover_letter_body}

Propose resume edits and cover letter swaps as CandidateEditBatch."""

    if dry_run:
        placeholder = CandidateEditBatch(edits=[])
        save_model(Path(out_dir) / "candidate_edits.json", placeholder, dry_run=False)
        print(f"[dry-run] wrote PLACEHOLDER candidate_edits.json (empty edit list) to "
              f"{out_dir} — no API call made ({len(ledger.claims)} ledger claims, "
              f"{len(edit_brief.gaps)} gaps loaded fine). This lets stage 6 validate its "
              f"wiring without spending anything; it is not a real draft.")
        return

    batch = call_llm_structured(
        system=GEN_SYSTEM,
        user=user_prompt,
        response_model=CandidateEditBatch,
        tier="reasoning",
        provider=provider,
        max_tokens=8192,
    )

    save_model(Path(out_dir) / "candidate_edits.json", batch, dry_run)
    log_event("stage5_generate", {"n_candidates": len(batch.edits), "dry_run": dry_run, "provider": provider})
    print(f"Drafted {len(batch.edits)} candidate edits -> "
          f"{Path(out_dir) / 'candidate_edits.json'} (unverified — stage 6 next)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--claims-ledger", required=True)
    ap.add_argument("--style-profile", required=True)
    ap.add_argument("--edit-brief", required=True)
    ap.add_argument("--company-brief", required=True)
    ap.add_argument("--cover-letter-baseline", required=True,
                     help="Path to the candidate's existing cover letter sample (.txt)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--provider", default=default_provider(), choices=["lmstudio", "claude", "gemini"],
                     help="Defaults to $CVTAILOR_LLM_PROVIDER, or 'lmstudio' (a local model, no cloud spend) if that's unset.")
    args = ap.parse_args()
    run(args.claims_ledger, args.style_profile, args.edit_brief,
        args.company_brief, args.cover_letter_baseline,
        args.out_dir, args.dry_run, args.provider)
