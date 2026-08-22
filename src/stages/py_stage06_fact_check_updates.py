#!/usr/bin/env python3
"""
py_stage06_fact_check_updates.py — the fact-check gate. Runs BETWEEN generation and
human review. Nothing reaches you in stage 8 without passing through
here first.

Two-tier check per candidate edit:
  Tier 1 (deterministic, free): claim_ids_referenced is empty -> instant
  discard. No LLM call needed — an edit that cites nothing to verify
  against is definitionally unverifiable.

  Tier 2 (semantic, one LLM call per edit): given the edit's suggested
  text and the FULL TEXT of every claim_id it cites, judge whether the
  edit's factual content is actually supported.
    - supported   -> passes through untouched
    - embellished -> passes through but flagged, human decides
    - fabricated  -> hard discard, never reaches review

Everything discarded is logged with reasoning in discarded_edits.jsonl
— nothing disappears silently.

Usage:
    python py_stage06_fact_check_updates.py \
        --claims-ledger applications/.../claims_ledger.json \
        --candidate-edits applications/.../candidate_edits.json \
        --out-dir applications/... [--dry-run]
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
from schemas import ClaimsLedger, VerificationResult, VerificationReport
from py_prompt_template_handler import load_prompt
from pydantic import BaseModel


class CandidateEditBatch(BaseModel):
    edits: list  # loaded loosely here; validated against schemas.CandidateEdit on read


VERIFY_SYSTEM = load_prompt("stages/stage06_fact_check_updates/verify_system.txt")


def verify_one(edit: dict, ledger_by_id: dict, provider: str, cover_letter_baseline_text: str = "") -> VerificationResult:
    if is_company_specific_edit(edit, cover_letter_baseline_text):
        # About the company/role, not the candidate -- there's nothing
        # in the claims ledger to check this against, and there never
        # will be, so this isn't "no LLM call needed this time," it's
        # "this category of edit is permanently out of scope for
        # claims-ledger fact-checking." See core/prose.py.
        return VerificationResult(
            edit_id=edit["edit_id"],
            verdict="supported",
            unsupported_spans=[],
            reasoning="Company/role-specific content (fills <COMPANY_NAME>/<ROLE_NAME> or is "
                      "the mandatory company-paragraph/closing-sentence rewrite) -- not a "
                      "candidate claim, not checked against the claims ledger.",
        )

    if edit.get("target") == "resume" and (
        edit.get("edit_type") != "reword" or not edit.get("original_text")
    ):
        # Stage 9 only knows how to find original_text verbatim in the
        # template and replace it — it has no way to move, delete, or
        # insert a resume bullet. An edit that isn't a reword-in-place
        # would silently never make it into the resume regardless of
        # approval, so catch it here rather than wasting the reviewer's
        # time on something that was always going to be a no-op.
        return VerificationResult(
            edit_id=edit["edit_id"],
            verdict="fabricated",
            unsupported_spans=[edit["suggested_text"]],
            reasoning=(
                f"target=resume with edit_type={edit.get('edit_type')!r} and "
                f"original_text={edit.get('original_text')!r} — stage 9 can only apply "
                f"in-place rewording of existing text, not reordering, insertion, or "
                f"deletion. This edit would never reach the resume even if approved."
            ),
        )

    if not edit.get("claim_ids_referenced"):
        if edit.get("target") == "cover_letter":
            # Company-specific edits have no candidate claims — pass them
            # through as "supported" since there's nothing to fact-check.
            return VerificationResult(
                edit_id=edit["edit_id"],
                verdict="supported",
                unsupported_spans=[],
                reasoning="Cover letter edit with no claim_ids_referenced (company-specific content) — no candidate facts to verify.",
            )
        return VerificationResult(
            edit_id=edit["edit_id"],
            verdict="fabricated",
            unsupported_spans=[edit["suggested_text"]],
            reasoning="No claim_ids_referenced — nothing to verify against.",
        )

    source_claims = [
        ledger_by_id[cid].text for cid in edit["claim_ids_referenced"] if cid in ledger_by_id
    ]
    if not source_claims:
        return VerificationResult(
            edit_id=edit["edit_id"],
            verdict="fabricated",
            unsupported_spans=[edit["suggested_text"]],
            reasoning="Referenced claim_ids don't exist in the ledger.",
        )

    # For cover letter swaps: the suggested_text contains a mix of the
    # candidate's OWN pre-existing words (carried over from original_text)
    # and the actual changes. Only the changes need fact-checking — the
    # candidate's own biographical details (employer names, years of
    # experience, city, etc.) are true by definition and shouldn't be
    # re-verified against the resume ledger (which doesn't contain them,
    # since the ledger was extracted from the resume, not the cover letter).
    if edit.get("original_text") and edit.get("target") == "cover_letter":
        user = (
            f"Source claims:\n" + "\n".join(f"- {c}" for c in source_claims) +
            f"\n\nORIGINAL text (the candidate's own existing words — these are NOT up for fact-checking, "
            f"they are true by definition since the candidate wrote them about themselves):\n{edit['original_text']}"
            f"\n\nPROPOSED replacement text (check ONLY the parts that differ from the original above — "
            f"words carried over verbatim from the original are the candidate's own and must not be flagged):"
            f"\n{edit['suggested_text']}"
        )
    else:
        user = (
            f"Source claims:\n" + "\n".join(f"- {c}" for c in source_claims) +
            f"\n\nEdit's suggested text:\n{edit['suggested_text']}"
        )
    result = call_llm_structured(
        system=VERIFY_SYSTEM,
        user=user,
        response_model=VerificationResult,
        tier="standard",
        provider=provider,
    )
    result.edit_id = edit["edit_id"]  # don't trust the model to echo this correctly
    return result


def _tier1_only(edit: dict, cover_letter_baseline_text: str = "") -> VerificationResult | None:
    """Runs just the free, deterministic Tier-1 checks from verify_one
    (no LLM call) — used in --dry-run so wiring can be smoke-tested with
    real discard logic instead of a generic placeholder. Returns a
    VerificationResult if Tier 1 alone is decisive (a real discard),
    or None if the edit would need a Tier-2 LLM call to judge."""
    if is_company_specific_edit(edit, cover_letter_baseline_text):
        return VerificationResult(
            edit_id=edit["edit_id"],
            verdict="supported",
            unsupported_spans=[],
            reasoning="Company/role-specific content -- not a candidate claim, not checked "
                      "against the claims ledger.",
        )

    if edit.get("target") == "resume" and (
        edit.get("edit_type") != "reword" or not edit.get("original_text")
    ):
        return VerificationResult(
            edit_id=edit["edit_id"],
            verdict="fabricated",
            unsupported_spans=[edit["suggested_text"]],
            reasoning=(
                f"target=resume with edit_type={edit.get('edit_type')!r} and "
                f"original_text={edit.get('original_text')!r} — stage 9 can only apply "
                f"in-place rewording of existing text, not reordering, insertion, or "
                f"deletion. This edit would never reach the resume even if approved."
            ),
        )
    if not edit.get("claim_ids_referenced"):
        # Company-specific cover letter edits are already handled above
        # via is_company_specific_edit(). This remaining case is a
        # cover letter edit with empty claim_ids for some OTHER reason
        # (e.g. pure connective/transitional text) -- let Tier 2 judge
        # it rather than auto-discarding, since "no claims referenced"
        # doesn't necessarily mean "unverifiable" for a cover letter
        # the way it does for a resume claim.
        if edit.get("target") == "cover_letter":
            return None  # let Tier 2 judge it on its own merits
        return VerificationResult(
            edit_id=edit["edit_id"],
            verdict="fabricated",
            unsupported_spans=[edit["suggested_text"]],
            reasoning="No claim_ids_referenced — nothing to verify against.",
        )
    return None


def run(claims_ledger_path, candidate_edits_path, out_dir, dry_run, provider, cover_letter_baseline=None):
    ledger = load_model(claims_ledger_path, ClaimsLedger)
    ledger_by_id = {c.claim_id: c for c in ledger.claims}
    raw_batch = json.loads(Path(candidate_edits_path).read_text(encoding="utf-8"))
    edits = raw_batch["edits"]
    # Optional -- older callers/apps without it still get the pattern +
    # section-label signals in is_company_specific_edit(), just not the
    # strongest one. See that function's docstring for why this matters.
    cover_letter_baseline_text = (
        Path(cover_letter_baseline).read_text(encoding="utf-8") if cover_letter_baseline else ""
    )

    results: list[VerificationResult] = []
    passed, flagged, discarded = [], [], []

    if dry_run:
        # Tier 1 is free (no LLM call) — run it for real so a dry-run
        # chain gives real signal and produces a real verified_edits.json
        # for stage 8 to smoke-test against, same as every other stage's
        # dry-run leaving something for the next stage to validate wiring
        # with. Only Tier 2 (the actual fact-check call) is stubbed —
        # and stubbed edits go to `flagged`, never `passed`, so nothing
        # unverified can ever look trustworthy even in a dry-run artifact.
        for edit in edits:
            r = _tier1_only(edit, cover_letter_baseline_text)
            if r is not None:
                results.append(r)
                discarded.append({**edit, "verification": r.model_dump()})
            else:
                r = VerificationResult(
                    edit_id=edit["edit_id"],
                    verdict="embellished",
                    unsupported_spans=[],
                    reasoning="[DRY-RUN] Tier 2 (LLM fact-check) skipped — not actually "
                              "verified. Placeholder for wiring test only; do not review "
                              "as if this passed a real check.",
                )
                results.append(r)
                flagged.append({**edit, "verification": r.model_dump()})
        print(f"[dry-run] {len(discarded)}/{len(edits)} discarded for free via real Tier-1 "
              f"checks (no API call). The remaining {len(flagged)} would need a Tier-2 LLM "
              f"call via {provider} — written to 'flagged' as unverified placeholders "
              f"instead, so stage 8 has real files to test against without spending "
              f"anything. Skipping the actual API calls.")
    else:
        for edit in edits:
            r = verify_one(edit, ledger_by_id, provider, cover_letter_baseline_text)
            results.append(r)
            if r.verdict == "supported":
                passed.append(edit)
            elif r.verdict == "embellished":
                flagged.append({**edit, "verification": r.model_dump()})
            else:
                discarded.append({**edit, "verification": r.model_dump()})

    out = Path(out_dir)
    report = VerificationReport(
        results=results,
        discarded_count=len(discarded),
        flagged_count=len(flagged),
        passed_count=len(passed),
    )
    # Written for real even in --dry-run (like every other stage's
    # placeholder output) so stage 8 has an actual verified_edits.json to
    # read — it has no dry-run mode of its own and would otherwise dead-
    # end a full dry-run chain at this stage. discarded_edits.jsonl is
    # skipped in dry-run since it's an append-only audit log, not
    # something downstream reads.
    save_model(out / "verification_report.json", report, dry_run=False)
    verified_payload = {"passed": passed, "flagged": flagged, "dry_run": dry_run}
    out.mkdir(parents=True, exist_ok=True)
    (out / "verified_edits.json").write_text(json.dumps(verified_payload, indent=2))
    if dry_run:
        print(f"[dry-run] wrote verification_report.json/verified_edits.json to {out} "
              f"({len(discarded)} real Tier-1 discards, {len(flagged)} flagged as "
              f"dry-run placeholders for what Tier-2 would check — 0 passed, since "
              f"nothing is actually verified in a dry-run). discarded_edits.jsonl not "
              f"appended in dry-run (append-only audit log, not read downstream).",
              file=sys.stderr)
    else:
        with open(out / "discarded_edits.jsonl", "a", encoding="utf-8") as f:
            for d in discarded:
                f.write(json.dumps(d) + "\n")

    log_event("stage6_verify", {
        "passed": len(passed), "flagged": len(flagged), "discarded": len(discarded),
        "dry_run": dry_run, "provider": provider,
    })
    print(f"Verified: {len(passed)} passed, {len(flagged)} flagged for review, "
          f"{len(discarded)} discarded as fabricated -> {out / 'verified_edits.json'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--claims-ledger", required=True)
    ap.add_argument("--candidate-edits", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--provider", default=default_provider(), choices=["lmstudio", "claude", "gemini"],
                     help="Defaults to $CVTAILOR_LLM_PROVIDER, or 'lmstudio' (a local model, no cloud spend) if that's unset.")
    ap.add_argument("--cover-letter-baseline", default=None,
                     help="Optional -- path to the baseline cover letter sample. Strengthens "
                          "the mandatory-company-paragraph exemption (see "
                          "is_company_specific_edit() in core/prose.py) with a ground-truth "
                          "substring check against the real baseline text, on top of the "
                          "pattern/section-label signals used when this is omitted.")
    args = ap.parse_args()
    run(args.claims_ledger, args.candidate_edits, args.out_dir, args.dry_run, args.provider,
        cover_letter_baseline=args.cover_letter_baseline)