#!/usr/bin/env python3
"""
py_stage01_score_jd_input.py — Stage 1: JD Scoring
==================================================
Scores a parsed job posting before you spend time on Stages 2-7.

Evaluates the `jd_input.json` against your configured targets (roles, firms, salary) 
and hard disqualifiers. This stage acts as a decision aid; it writes a score 
report and prints a verdict, but it does not block the pipeline from continuing.

Usage:
    python py_stage01_score_jd_input.py \
        --app-id acme_cloud_architect_2026-07-27 \
        --resume-text /path/to/resume.txt \
        [--applications-root applications] \
        [--provider claude]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/stages/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
from io_utils import log_event, load_model
from schemas import JDInput
from batch_common import suggest_resume_variant, APPLICATIONS_ROOT, load_applicant_info

sys.path.append(str(_JOBS_ROOT / "src"))
from sourcing.scorer import JobScorer
from sourcing.config import ScoringConfig
from sourcing.candidate_profile import as_prompt_block


def _load_scoring_setup() -> tuple[ScoringConfig, str]:
    """Reads config/applicant_info.json's optional "job_scoring" section
    -- target roles/firms/salary range/disqualifiers -- and builds the
    ScoringConfig + candidate-profile prose block stage 1 needs. Errors
    clearly if the section is missing rather than scoring against an
    empty profile, since a score with no target info behind it isn't a
    useful signal -- same reasoning as load_resume_variants() refusing
    to silently guess when config/resume_variants/ is empty."""
    applicant = load_applicant_info()
    job_scoring = applicant.get("job_scoring")
    if not job_scoring:
        raise SystemExit(
            "config/applicant_info.json has no \"job_scoring\" section -- stage 1 needs "
            "it (target roles, target firms, salary range, disqualifiers) to score "
            "against. See config/applicant_info.example.json for the shape. Stage 1 "
            "is optional -- stages 2-7 run fine without it."
        )
    return ScoringConfig.from_dict(job_scoring), as_prompt_block(job_scoring, applicant.get("name", ""))


def _jd_input_to_job_dict(jd: JDInput) -> dict:
    """Maps JDInput's field names onto what the ported scorer expects."""
    salary_raw = None
    if jd.salary_min and jd.salary_max:
        salary_raw = f"${jd.salary_min:,.0f} - ${jd.salary_max:,.0f} {jd.salary_currency or 'USD'}"
    return {
        "title": jd.role_title,
        "company": jd.company,
        "location": jd.location or "",
        "description": jd.full_description_text,
        "remote_type": jd.remote_type,
        "salary_min": jd.salary_min,
        "salary_max": jd.salary_max,
        "salary_raw": salary_raw,
    }


def run(app_id: str, resume_text_path: str, applications_root: str, provider: str | None) -> None:
    app_dir = Path(applications_root) / app_id
    jd_path = app_dir / "jd_input.json"
    if not jd_path.exists():
        print(f"No jd_input.json found at {jd_path}. Run py_stage00_normalize_jd_input.py first.", file=sys.stderr)
        sys.exit(1)

    jd = load_model(jd_path, JDInput)
    resume_text = Path(resume_text_path).read_text(encoding="utf-8")

    # Runs independent of the scorer/LLM entirely -- works even if the
    # LLM call fails, and even for disqualified jobs (which previously
    # got no suggestion at all, since the old version only ran after a
    # role_type existed in the scoring breakdown).
    resume_suggestion, resume_suggestion_reason = suggest_resume_variant(jd)

    scoring_config, candidate_profile = _load_scoring_setup()
    scorer = JobScorer(scoring_config, resume_text=resume_text,
                        candidate_profile=candidate_profile, provider=provider)
    job = _jd_input_to_job_dict(jd)
    score, breakdown, rationale = scorer.score_job(job)
    breakdown["suggested_resume_variant"] = resume_suggestion
    breakdown["suggested_resume_variant_reason"] = resume_suggestion_reason

    report = {"app_id": app_id, "score": score, "breakdown": breakdown, "rationale": rationale,
              "status": "failed" if score is None else "scored"}
    report_path = app_dir / "score_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    log_event("stage1_score", {
        "app_id": app_id, "company": jd.company, "role_title": jd.role_title,
        "score": score, "disqualified": "disqualified" in breakdown,
        "llm_call_failed": breakdown.get("llm_call_failed", False),
        "suggested_resume_variant": resume_suggestion,
    })

    print(f"\n{jd.role_title} @ {jd.company}")
    print(f"Suggested resume: {resume_suggestion}  ({resume_suggestion_reason})")

    if score is None:
        # LLM call failed -- no fabricated number, no verdict. Loud and
        # unambiguous rather than a plausible-looking score that wasn't real.
        print("SCORE UNAVAILABLE -- the LLM call failed, so no score was computed.")
        print(f"  {rationale}")
        print(f"\nFull breakdown (including the error) written to: {report_path}")
        print("\nFix the underlying issue (commonly: ANTHROPIC_API_KEY not set, "
              "or --provider gemini without GEMINI_API_KEY set) and re-run.")
        return

    verdict = (
        "DISQUALIFIED" if "disqualified" in breakdown else
        "STRONG (proceed to stage2)" if score >= 7.0 else
        "MODERATE (worth a look, verify the gaps)" if score >= 5.0 else
        "WEAK (probably not worth stage2-7 effort)"
    )
    print(f"Score: {score}/10 -- {verdict}")
    print(f"  {rationale}")
    print(f"\nFull breakdown written to: {report_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app-id", required=True)
    ap.add_argument("--resume-text", required=True,
                     help="Plain-text resume file to score against (maintain a current .txt "
                          "snapshot alongside your .docx for this purpose)")
    ap.add_argument("--applications-root", default=str(APPLICATIONS_ROOT))
    ap.add_argument("--provider", default=None, choices=["lmstudio", "claude", "gemini"],
                     help="Defaults to CVTAILOR_LLM_PROVIDER env var, then 'claude'")
    args = ap.parse_args()
    run(args.app_id, args.resume_text, args.applications_root, args.provider)
