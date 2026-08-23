#!/usr/bin/env python3
"""
py_answer_question.py
=====================
Generates grounded answers to free-form application screening questions 
(e.g., "Why are you interested in this role?").

Forces the LLM to base its answer entirely on your claims ledger.

Usage:
    python src/util/py_answer_question.py --question "..." --app-id <app_id>
"""
import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
sys.path.append(str(Path(__file__).resolve().parent.parent / "prompt_library"))
from batch_common import APPLICATIONS_ROOT, NON_APPLICATION_DIR_NAMES
from llm_dispatch import call_llm_structured, default_provider
from io_utils import load_model
from schemas import ClaimsLedger, StyleProfile, CompanyBrief, JDInput
from py_prompt_template_handler import load_prompt
from pydantic import BaseModel


class QuestionAnswer(BaseModel):
    answer: str


def _resolve_app_dir(app_id: str) -> Path:
    """Resolves --app-id by exact match first (so a real folder name
    always wins even on the off chance it's also a substring of
    another), then falls back to a case-insensitive substring match
    against folders directly under applications/ (never a
    NON_APPLICATION_DIR_NAMES entry) -- lets you type a short fragment instead of the
    full company_role_date app_id. Same "list and ask, don't guess"
    behavior as py_mark_applied.py's prefix match when more than one
    folder qualifies, so a short or ambiguous fragment can't silently
    resolve to the wrong application. Same "list and ask, don't guess"
    behavior as py_post_pipeline_store_applied.py's prefix match when
    more than one folder qualifies."""
    exact = APPLICATIONS_ROOT / app_id
    if exact.is_dir():
        return exact

    if not APPLICATIONS_ROOT.is_dir():
        print(f"No applications/{app_id}/ folder found (and {APPLICATIONS_ROOT}/ doesn't exist).", file=sys.stderr)
        sys.exit(1)

    needle = app_id.lower()
    matches = sorted(
        d for d in APPLICATIONS_ROOT.iterdir()
        if d.is_dir()
        and d.name not in NON_APPLICATION_DIR_NAMES
        and needle in d.name.lower()
    )
    if not matches:
        print(f"No applications/{app_id}/ folder found (tried exact and substring match).", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"{app_id!r} matches {len(matches)} folders -- be more specific:", file=sys.stderr)
        for m in matches:
            print(f"  {m.name}", file=sys.stderr)
        sys.exit(1)
    return matches[0]


ANSWER_SYSTEM = load_prompt("util/answer_question_system.txt")


def run(question: str, variant: str | None, app_id: str | None, provider: str, out: str | None):
    company_brief = None
    jd = None

    if app_id:
        app_dir = _resolve_app_dir(app_id)
        app_id = app_dir.name  # normalize a substring match to the real folder name for the paths below
        routing_path = app_dir / "routing.json"
        if routing_path.is_file():
            import json
            variant = json.loads(routing_path.read_text(encoding="utf-8")).get("resume_variant", variant)
        jd_path = app_dir / "jd_input.processed.json"
        if not jd_path.is_file():
            jd_path = app_dir / "jd_input.json"
        if jd_path.is_file():
            jd = load_model(jd_path, JDInput)
        brief_path = app_dir / "company_brief.json"
        if brief_path.is_file():
            company_brief = load_model(brief_path, CompanyBrief)

    if not variant:
        variant = "Backend"
        print(f"No --variant or --app-id given -- defaulting to {variant!r}. "
              f"Pass --variant Backend|\"Full Stack\" or --app-id to be explicit.", file=sys.stderr)

    slug = "".join(c if c.isalnum() else "_" for c in variant.strip().lower()).strip("_")
    ledger_path = APPLICATIONS_ROOT / "_shared" / "variants" / slug / "claims_ledger.json"
    if not ledger_path.is_file():
        print(f"No cached claims ledger for variant {variant!r} at {ledger_path} -- "
              f"run run_pipeline.py at least once first (it builds these).", file=sys.stderr)
        sys.exit(1)
    ledger = load_model(ledger_path, ClaimsLedger)

    style_path = APPLICATIONS_ROOT / "_shared" / "style_profile.json"
    style = load_model(style_path, StyleProfile) if style_path.is_file() else None

    claims_blob = "\n".join(f"[{c.claim_id}] {c.text}" for c in ledger.claims)
    user_parts = [f"Claims ledger:\n{claims_blob}"]
    if style:
        user_parts.append(f"Style profile: {style.model_dump_json()}")
    if jd:
        user_parts.append(f"Role: {jd.role_title} at {jd.company}")
    if company_brief:
        user_parts.append(f"Company brief: {company_brief.model_dump_json()}")
    user_parts.append(f"Question: {question}")
    user_prompt = "\n\n".join(user_parts)

    result = call_llm_structured(
        system=ANSWER_SYSTEM,
        user=user_prompt,
        response_model=QuestionAnswer,
        tier="standard",
        provider=provider,
    )

    print(f"\n{result.answer}\n")

    if out:
        Path(out).write_text(result.answer, encoding="utf-8")
        print(f"Saved -> {out}", file=sys.stderr)
    elif app_id:
        out_path = APPLICATIONS_ROOT / app_id / "extra_question_answers.txt"
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(f"Q: {question}\nA: {result.answer}\n\n")
        print(f"Appended -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True, help="The application-form question to answer")
    ap.add_argument("--variant", default=None, choices=["Backend", "Full Stack"],
                     help="Which resume's claims ledger to draw from. Auto-detected if --app-id is given.")
    ap.add_argument("--app-id", default=None,
                     help="Optional -- pulls in company/JD context and auto-detects --variant from "
                          "routing.json. A unique substring of the full app_id is enough; ambiguous "
                          "or no matches list the candidates instead of guessing.")
    ap.add_argument("--out", default=None, help="Optional -- save the answer to this file")
    ap.add_argument("--provider", default=default_provider(), choices=["lmstudio", "claude", "gemini"],
                     help="Defaults to $CVTAILOR_LLM_PROVIDER, or 'lmstudio' if that's unset.")
    args = ap.parse_args()
    run(args.question, args.variant, args.app_id, args.provider, args.out)