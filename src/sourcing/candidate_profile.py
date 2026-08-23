#!/usr/bin/env python3
"""
sourcing/candidate_profile.py

Renders config/applicant_info.json's optional "job_scoring" section
(target-role priority, "not a good fit" exclusions, core strengths) as
the prose block src/stages/py_stage01_score_jd_input.py's scorer embeds
in its LLM prompt. Used to be hardcoded module-level constants here --
moved to that JSON config so editing your targets is a config edit, not
a source edit, and so a fresh clone doesn't inherit someone else's
targets as a silent default.
"""


from __future__ import annotations


def as_prompt_block(job_scoring: dict, applicant_name: str = "") -> str:
    """Renders the profile as the prose block the scorer's LLM prompt
    embeds as candidate context. Reads live from whatever `job_scoring`
    dict is passed in (see py_stage01_score_jd_input.py, which loads it
    fresh from config/applicant_info.json on every run) -- edits there
    take effect on the next stage 1 run with nothing else to touch."""
    lines = []
    if applicant_name:
        lines.append(f"Name: {applicant_name}")
    if job_scoring.get("experience_summary"):
        lines.append(f"Experience: {job_scoring['experience_summary']}")
    if job_scoring.get("core_strengths"):
        lines.append(f"Core strengths: {', '.join(job_scoring['core_strengths'])}")
    if job_scoring.get("does_not_have"):
        lines.append(f"Does NOT have: {', '.join(job_scoring['does_not_have'])}")
    if job_scoring.get("education"):
        lines.append(f"Education: {job_scoring['education']}")

    target_roles = job_scoring.get("target_roles", [])
    if target_roles:
        lines.append("")
        lines.append("Target roles (priority order):")
        for i, role in enumerate(target_roles, start=1):
            lines.append(f"  {i}. {role}")

    if job_scoring.get("not_good_fit"):
        lines.append("")
        lines.append(f"NOT a good fit for: {', '.join(job_scoring['not_good_fit'])}")

    return "\n".join(lines)
