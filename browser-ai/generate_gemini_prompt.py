#!/usr/bin/env python3
"""
generate_gemini_prompt.py -- regenerates "Gemini JD Eval Prompt.txt"
from config/applicant_info.json's "job_scoring" section. Run after
editing that section, so the web prompt never drifts from real config.
"""

import sys
from pathlib import Path

JOBS_ROOT = Path(__file__).resolve().parent.parent  # browser-ai/ -> jobs/
sys.path.insert(0, str(JOBS_ROOT / "src"))
sys.path.insert(0, str(JOBS_ROOT / "src" / "core"))

from sourcing.config import ScoringConfig
from batch_common import load_applicant_info

OUTPUT_PATH = Path(__file__).resolve().parent / "Gemini JD Eval Prompt.txt"


def _load_job_scoring() -> dict:
    applicant = load_applicant_info()
    job_scoring = applicant.get("job_scoring")
    if not job_scoring:
        raise SystemExit(
            "config/applicant_info.json has no \"job_scoring\" section -- this generator "
            "needs it (target roles, target firms, salary range, disqualifiers) to build "
            "the prompt. See config/applicant_info.example.json for the shape."
        )
    return job_scoring


def build_prompt(cfg: ScoringConfig, target_roles: list[str], not_good_fit: list[str]) -> str:
    roles_block = "\n".join(f"   {i}. {r}" for i, r in enumerate(target_roles, start=1))
    not_fit_block = ", ".join(not_good_fit)

    return f"""You are screening a single job posting against my personal criteria before I
decide whether to spend time tailoring a resume and applying. Give me a
direct verdict and reasoning, not a summary of the posting.

MY CRITERIA, IN ROUGH PRIORITY ORDER:

1. SALARY: I require a minimum of ${cfg.min_salary:,}/year base salary. If a range is
   listed, tell me where my requirement falls in it and whether negotiating
   up into it is realistic if the range starts below ${cfg.min_salary:,}.

2. REMOTE WORK: Fully remote is strongly preferred. Hybrid is acceptable
   ONLY if the office is in or near Philadelphia, PA with a reasonable
   commute -- otherwise treat hybrid/on-site as a hard disqualifier.

3. AUTONOMY & FLEXIBLE SCHEDULE: I need genuine autonomy over how and when
   I work -- no close oversight, no rigid clock-in/out culture, no
   always-on-camera or constant-check-in expectations. Flag any language
   suggesting micromanagement or rigid hour-tracking.

4. NO DRUG TESTING: Pre-employment or random drug testing is a hard
   disqualifier. If not stated explicitly in the posting, infer likelihood
   from the industry/role (e.g. transportation, healthcare, government
   contractors, and some regulated financial roles commonly require it --
   flag this even if the JD itself is silent on it).

5. TARGET ROLE TYPE, IN PRIORITY ORDER:
{roles_block}
   I am NOT a good fit for: {not_fit_block} -- flag if the posting is
   actually one of these despite its title.

6. AUTOMATION-FRIENDLY CULTURE: I want to automate repetitive parts of my
   own job wherever possible. Flag if the posting's tone suggests a culture
   resistant to that (rigid manual-process adherence, heavy compliance
   restrictions on tooling) versus one that would likely welcome it.

7. LIKELIHOOD OF BEING OFFERED: Compare the JD's stated requirements against
   my resume below and give me an honest, calibrated read on how
   competitive I'd actually be to a hiring manager reading both side by
   side -- not just "am I technically qualified," but strong/moderate/weak
   candidate.

MY RESUME:
[paste resume text here]

THE JOB POSTING:
[paste full job posting text here]

GIVE ME:
- A single verdict: APPLY / MAYBE / SKIP
- 3-5 sentences of reasoning, addressing hard disqualifiers first (salary
  floor, non-Philadelphia hybrid/on-site, drug testing) before softer
  factors
- If MAYBE: state specifically what would need to be true to make it APPLY
  (e.g. "if the hybrid requirement turns out to be negotiable")
"""


def main():
    job_scoring = _load_job_scoring()
    cfg = ScoringConfig.from_dict(job_scoring)
    prompt = build_prompt(cfg, job_scoring.get("target_roles", []), job_scoring.get("not_good_fit", []))
    OUTPUT_PATH.write_text(prompt, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
