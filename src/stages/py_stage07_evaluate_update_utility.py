#!/usr/bin/env python3
"""
py_stage07_evaluate_update_utility.py — the adversarial utility gate. Runs BETWEEN
stage 6 (fact-check) and stage 8 (human review).

Stage 6 asks "is this factually true?". This asks the separate question
nothing in the pipeline previously owned: "is this edit actually USEFUL
for THIS specific job?" Those come apart constantly -- a perfectly
truthful, perfectly grounded edit can still be a pure synonym swap that
changes nothing a hiring manager would notice.

Why this exists: stage 8 ranks edits with a deterministic heuristic
(edit-type weights + keyword overlap against JD requirements), which
systematically floats weak-but-keyword-matchy edits to the top. Since
stage 8 only shows you the top N (default 5), a weak edit surviving
into that slot costs you a genuinely good one. This stage culls before
that ranking happens.

DELIBERATELY BIASED TOWARD CUTTING. The premise is that the existing
resume and cover letter are already good -- an edit has to earn its
place, not merely avoid being wrong. A cut edit isn't lost: it's
written to critiqued_out.jsonl with the critic's reasoning, same
never-silently-disappear principle as stage 6's discarded_edits.jsonl.

This stage JUDGES ONLY -- it never rewrites an edit. Adding a second
generation pass would mean more LLM-generated prose competing with the
candidate's own voice, which is the opposite of what's wanted; the
whole point here is subtraction.

Usage:
    python py_stage07_evaluate_update_utility.py \
        --jd-input applications/.../jd_input.json \
        --verified-edits applications/.../verified_edits.json \
        --out-dir applications/... [--dry-run] [--keep-flagged]
"""
import argparse
import json
import sys
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/stages/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
sys.path.append(str(_JOBS_ROOT / "src" / "prompt_library"))
from llm_dispatch import call_llm_structured, default_provider
from io_utils import save_model, load_model, log_event
from prose import is_company_specific_edit
from schemas import JDInput, CritiqueResult, CritiqueReport
from py_prompt_template_handler import load_prompt


CRITIQUE_SYSTEM = load_prompt("stages/stage07_evaluate_update_utility/critique_system.txt")


def critique_one(edit: dict, jd: JDInput, provider: str | None) -> CritiqueResult:
    requirements = "\n".join(f"- {r}" for r in jd.requirements_raw) or "(none listed)"
    responsibilities = "\n".join(f"- {r}" for r in jd.responsibilities_raw) or "(none listed)"

    if edit.get("original_text"):
        current = f"TEXT THIS WOULD REPLACE:\n{edit['original_text']}"
    else:
        current = ("TEXT THIS WOULD REPLACE:\n(nothing -- this is an additive new cover "
                   "letter sentence, so it must justify occupying space in a letter that "
                   "is already complete and coherent without it)")

    user = (
        f"JOB: {jd.role_title} at {jd.company}\n\n"
        f"JD REQUIREMENTS:\n{requirements}\n\n"
        f"JD RESPONSIBILITIES:\n{responsibilities}\n\n"
        f"{current}\n\n"
        f"PROPOSED NEW TEXT:\n{edit['suggested_text']}\n\n"
        f"Keep or cut?"
    )
    result = call_llm_structured(
        system=CRITIQUE_SYSTEM, user=user, response_model=CritiqueResult,
        tier="standard", provider=provider,
    )
    # The model sets edit_id from its own reading of the prompt, which it
    # has no reliable basis for -- overwrite with the real one rather
    # than trusting it to echo an identifier it was never given.
    result.edit_id = edit["edit_id"]
    return result


def run(jd_input_path, verified_edits_path, out_dir, dry_run, provider, keep_flagged, cover_letter_baseline=None):
    jd = load_model(jd_input_path, JDInput)
    verified = json.loads(Path(verified_edits_path).read_text(encoding="utf-8"))
    cover_letter_baseline_text = (
        Path(cover_letter_baseline).read_text(encoding="utf-8") if cover_letter_baseline else ""
    )

    # Stage 6 splits into passed (clean) and flagged (embellished --
    # human decides). By default both go through the critic, since a
    # flagged edit that's ALSO low-utility is doubly not worth a review
    # slot. --keep-flagged passes flagged edits through uncritiqued if
    # you'd rather always see everything stage 6 was unsure about.
    passed = verified.get("passed", [])
    flagged = verified.get("flagged", [])
    to_critique = passed + ([] if keep_flagged else flagged)

    out = Path(out_dir)

    if dry_run:
        print(f"[dry-run] would critique {len(to_critique)} edit(s) via {provider} "
              f"(one call each) and write critique_report.json/critiqued_edits.json to "
              f"{out}. Skipping the actual API calls; writing pass-through placeholders "
              f"so stage 8 has real files to test wiring against.", file=sys.stderr)
        out.mkdir(parents=True, exist_ok=True)
        (out / "critiqued_edits.json").write_text(json.dumps(
            {"passed": passed, "flagged": flagged, "dry_run": True}, indent=2))
        return

    results: list[CritiqueResult] = []
    kept_passed, kept_flagged, cut = [], [], []

    # Mandatory/company-specific edits — the prompt explicitly requires
    # these for every job, and cutting them defeats the whole point of
    # per-company tailoring. Pass them through uncritiqued rather than
    # asking the critic to judge something that isn't optional. Uses
    # the same is_company_specific_edit() check as stage 6, so the two
    # stages can't drift apart on what counts as exempt.
    for edit in to_critique:
        if is_company_specific_edit(edit, cover_letter_baseline_text):
            r = CritiqueResult(
                edit_id=edit["edit_id"],
                verdict="keep",
                reasoning="Mandatory per-company edit (company paragraph or closing sentence) — exempt from utility critique.",
            )
            results.append(r)
            if edit in flagged:
                kept_flagged.append(edit)
            else:
                kept_passed.append(edit)
            continue

        r = critique_one(edit, jd, provider)
        results.append(r)
        if r.verdict == "keep":
            if edit in flagged:
                kept_flagged.append(edit)
            else:
                kept_passed.append(edit)
        else:
            cut.append({**edit, "critique": r.model_dump()})

    if keep_flagged:
        kept_flagged = flagged

    report = CritiqueReport(
        results=results,
        kept_count=len(kept_passed) + len(kept_flagged),
        cut_count=len(cut),
    )
    save_model(out / "critique_report.json", report, dry_run=False)

    out.mkdir(parents=True, exist_ok=True)
    (out / "critiqued_edits.json").write_text(json.dumps(
        {"passed": kept_passed, "flagged": kept_flagged, "dry_run": False}, indent=2))

    # Never silently disappear -- same principle as stage 6's
    # discarded_edits.jsonl. A cut edit is recoverable and its reasoning
    # is auditable.
    if cut:
        with open(out / "critiqued_out.jsonl", "a", encoding="utf-8") as f:
            for c in cut:
                f.write(json.dumps(c) + "\n")

    log_event("stage7_critique", {
        "app_id": out.name,
        "kept": report.kept_count, "cut": report.cut_count, "provider": provider,
    })
    print(f"Critique: {report.kept_count} kept, {report.cut_count} cut "
          f"-> {out / 'critiqued_edits.json'}")
    if cut:
        print(f"  (cut edits + reasoning logged to {out / 'critiqued_out.jsonl'})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd-input", required=True)
    ap.add_argument("--verified-edits", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-flagged", action="store_true",
                     help="Pass stage 6's 'flagged/embellished' edits through uncritiqued, "
                          "so you always see everything stage 6 was unsure about.")
    ap.add_argument("--provider", default=None, choices=["lmstudio", "claude", "gemini"])
    ap.add_argument("--cover-letter-baseline", default=None,
                     help="Optional -- path to the baseline cover letter sample. Same "
                          "ground-truth exemption strengthening as stage 6's own "
                          "--cover-letter-baseline; see is_company_specific_edit() in "
                          "core/prose.py.")
    args = ap.parse_args()
    provider = args.provider or default_provider()
    run(args.jd_input, args.verified_edits, args.out_dir, args.dry_run, provider, args.keep_flagged,
        cover_letter_baseline=args.cover_letter_baseline)
