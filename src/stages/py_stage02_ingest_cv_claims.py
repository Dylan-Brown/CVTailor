#!/usr/bin/env python3
"""
py_stage02_ingest_cv_claims.py — Stage 2: Resume Ingestion
==========================================================
Extracts atomic facts from your base documents to produce the `ClaimsLedger` 
and `StyleProfile`. 

This is a strict extraction pass; the LLM is constrained to pull exactly what 
is written without summarizing or inferring.

Usage:
    python py_stage02_ingest_cv_claims.py \
        --resume path/to/resume.pdf \
        --cover-letter-sample path/to/sample.txt \
        --out-dir applications/...
"""
import argparse
import sys
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/stages/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
sys.path.append(str(_JOBS_ROOT / "src" / "prompt_library"))
from llm_dispatch import call_llm_structured, default_provider
from io_utils import save_model, log_event
from schemas import ClaimsLedger, StyleProfile, Claim, StyleLeakCheck
from py_prompt_template_handler import load_prompt


def extract_text_from_resume(path: str) -> str:
    if path.lower().endswith(".pdf"):
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    elif path.lower().endswith(".docx"):
        import docx
        d = docx.Document(path)
        return "\n".join(p.text for p in d.paragraphs)
    else:
        return Path(path).read_text(encoding="utf-8")


EXTRACTION_SYSTEM = load_prompt("stages/stage02_ingest_cv_claims/extraction_system.txt")
STYLE_EXTRACTION_SYSTEM = load_prompt("stages/stage02_ingest_cv_claims/style_extraction_system.txt")
STYLE_LEAK_CHECK_SYSTEM = load_prompt("stages/stage02_ingest_cv_claims/style_leak_check_system.txt")


def _redact_leaked_items(style: StyleProfile, items: list) -> tuple[StyleProfile, list, list]:
    """Removes exactly the flagged list entries from a StyleProfile.
    Uses token-overlap fuzzy matching to ensure minor quoting or truncation 
    differences don't allow leaked items to survive redaction."""
    import re
    data = style.model_dump()
    actually_redacted, not_found = [], []
    
    def get_word_set(text: str) -> set[str]:
        return set(w.lower() for w in re.findall(r"[A-Za-z0-9]+", text))

    for item in items:
        field_list = data.get(item.field, [])
        target = item.exact_value.strip()
        target_words = get_word_set(target)
        
        kept = []
        redacted_this_item = False
        
        for v in field_list:
            v_words = get_word_set(v)
            # If 80%+ of the target's words are in the field string, it's a match
            if target_words and len(target_words.intersection(v_words)) / len(target_words) >= 0.80:
                redacted_this_item = True
                continue
            # Fallback to strict substring check
            if target in v or v.strip() in target:
                redacted_this_item = True
                continue
                
            kept.append(v)
            
        if redacted_this_item:
            actually_redacted.append(item.exact_value)
        else:
            not_found.append(item.exact_value)
        data[item.field] = kept
        
    return StyleProfile.model_validate(data), actually_redacted, not_found


def run(resume_path: str, cover_letter_path: str, out_dir: str, dry_run: bool, provider: str):
    resume_text = extract_text_from_resume(resume_path)
    cover_text = Path(cover_letter_path).read_text(encoding="utf-8")

    if dry_run:
        placeholder_ledger = ClaimsLedger(
            source_file=resume_path,
            claims=[Claim(claim_id="DRYRUN", section="dry_run_placeholder",
                          text="[DRY-RUN PLACEHOLDER — no real extraction performed]",
                          category="skill", has_metric=False)],
        )
        placeholder_style = StyleProfile(
            avg_sentence_length=0.0,
            tone_descriptors=["DRY-RUN PLACEHOLDER"],
            structural_notes=["DRY-RUN PLACEHOLDER — no real style analysis performed"],
        )
        save_model(Path(out_dir) / "claims_ledger.json", placeholder_ledger, dry_run=False)
        save_model(Path(out_dir) / "style_profile.json", placeholder_style, dry_run=False)
        print(f"[dry-run] wrote PLACEHOLDER claims_ledger.json/style_profile.json to {out_dir} "
              f"— no API calls made (inputs read fine: {len(resume_text)} chars resume, "
              f"{len(cover_text)} chars cover letter). This lets stages 3-5 validate their "
              f"wiring without spending anything; it is not real extraction — don't review it.")
        return

    ledger = call_llm_structured(
        system=EXTRACTION_SYSTEM,
        user=f"Extract every claim from this resume:\n\n{resume_text}",
        response_model=ClaimsLedger,
        tier="cheap",
        provider=provider,
    )
    ledger.source_file = resume_path

    style = call_llm_structured(
        system=STYLE_EXTRACTION_SYSTEM,
        user=f"Sample cover letter:\n\n{cover_text}",
        response_model=StyleProfile,
        tier="cheap",
        provider=provider,
    )

    leak_check = call_llm_structured(
        system=STYLE_LEAK_CHECK_SYSTEM,
        user=(
            f"Original cover letter:\n{cover_text}\n\n"
            f"Extracted style profile:\n{style.model_dump_json(indent=2)}"
        ),
        response_model=StyleLeakCheck,
        tier="standard",  # deliberately a more capable tier than the cheap extraction it's auditing
        provider=provider,
    )

    actually_redacted, not_found = [], []
    if leak_check.has_leakage:
        style, actually_redacted, not_found = _redact_leaked_items(style, leak_check.items)
        if actually_redacted:
            print(f"Auto-redacted {len(actually_redacted)} flagged item(s) from the style profile: "
                  f"{actually_redacted}")
        if not_found:
            # A flagged item that didn't match any list entry verbatim is a
            # real, partial safety-net failure -- surfaced loudly and
            # separately from the successful redactions above, not folded
            # into one number that can't tell the two apart.
            print(f"WARNING: {len(not_found)} flagged item(s) could NOT be auto-redacted "
                  f"(no verbatim match found in the style profile): {not_found}\n"
                  f"  This may mean the flagged text is still present in some form -- "
                  f"check style_profile.json (and style_profile_LEAK_WARNING.json, if "
                  f"written below) before trusting it.", file=sys.stderr)
        if actually_redacted or not_found:
            # Confirm redaction actually worked rather than trusting it blindly —
            # one more cheap check, bounded to a single retry either way.
            leak_check = call_llm_structured(
                system=STYLE_LEAK_CHECK_SYSTEM,
                user=(
                    f"Original cover letter:\n{cover_text}\n\n"
                    f"Extracted style profile (after auto-redaction):\n{style.model_dump_json(indent=2)}"
                ),
                response_model=StyleLeakCheck,
                tier="standard",
                provider=provider,
            )

    out = Path(out_dir)
    save_model(out / "claims_ledger.json", ledger, dry_run)
    save_model(out / "style_profile.json", style, dry_run)

    if leak_check.has_leakage:
        save_model(out / "style_profile_LEAK_WARNING.json", leak_check, dry_run)
        print(
            "\n"
            "!!! STYLE PROFILE MAY STILL CONTAIN LEAKED CONTENT (after auto-redaction attempt) !!!\n"
            f"Remaining: {[i.exact_value for i in leak_check.items]}\n"
            f"Reasoning: {leak_check.reasoning}\n"
            f"Auto-redaction couldn't fully resolve this — review and hand-edit "
            f"{out / 'style_profile.json'} before running run_pipeline.py on more JDs, "
            f"or the flagged content risks surfacing in every cover letter this batch produces.\n",
            file=sys.stderr,
        )
    else:
        suffix = " (clean after auto-redaction)" if actually_redacted else ""
        print(f"Style leak check: clean{suffix}")

    log_event("stage2_ingest", {
        "resume": resume_path, "n_claims": len(ledger.claims), "dry_run": dry_run,
        "provider": provider, "style_leak_detected": leak_check.has_leakage,
        "style_items_auto_redacted": len(actually_redacted),
        "style_items_not_found": len(not_found),
    })
    print(f"Extracted {len(ledger.claims)} claims -> {out / 'claims_ledger.json'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", required=True)
    ap.add_argument("--cover-letter-sample", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--provider", default=default_provider(), choices=["lmstudio", "claude", "gemini"],
                     help="Defaults to $CVTAILOR_LLM_PROVIDER, or 'lmstudio' (a local model, no cloud spend) if that's unset.")
    args = ap.parse_args()
    run(args.resume, args.cover_letter_sample, args.out_dir, args.dry_run, args.provider)