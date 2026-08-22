"""
sourcing/candidate_profile.py

Single source of truth for target-role priority, "not a good fit"
exclusions, and core strengths -- previously hardcoded directly inside
src/stages/py_stage01_score_jd_input.py's CANDIDATE_PROFILE string, which meant
anything else wanting the same list (like the Gemini pre-screen prompt
generator) would've had to duplicate it by hand and drift over time.

Edit TARGET_ROLES / NOT_GOOD_FIT / CORE_STRENGTHS / DOES_NOT_HAVE here
as your targets shift -- both the scorer's LLM prompt and the
Gemini-prompt generator (jobs/generate_gemini_prompt.py) pick up
changes automatically, no need to edit them separately.
"""

from __future__ import annotations

NAME = 'Dylan "Thesis" Brown'

EXPERIENCE_SUMMARY = (
    "8+ years full-stack software engineering, most recently senior engineer "
    "in FinTech infrastructure at Capital One (2018-2026)"
)

CORE_STRENGTHS = [
    "AWS (Lambda, DynamoDB, S3, EC2, CDK)", "Python", "TypeScript", "React",
    "distributed systems", "microservices", "REST APIs", "ML background",
    "fintech domain",
]

DOES_NOT_HAVE = [
    "hands-on Kubernetes", "Go/Golang", "C++", "pure ops/SRE experience",
]

EDUCATION = "University of Pennsylvania"

# Ordered by priority -- index 0 is the most-wanted role type.
TARGET_ROLES = [
    "Cloud Architecture / AWS Solutions Architect",
    "Senior Full-Stack Product Engineer (React + Python/Node backend)",
    "Platform / Infrastructure Engineer (Terraform, CI/CD, observability)",
    "Fintech / Payments Engineer",
    "ML Engineer / AI Systems Engineer",
]

NOT_GOOD_FIT = [
    "pure DevOps/SRE (ops-only)",
    "MLOps/data pipelines",
    "roles requiring significant re-skilling in technologies never used",
]


def as_prompt_block() -> str:
    """Renders the profile as the prose block stage1_score.py's
    CANDIDATE_PROFILE used to hardcode -- kept as a function (not a
    module-level constant) so it always reflects current edits above."""
    lines = [
        f"Name: {NAME}",
        f"Experience: {EXPERIENCE_SUMMARY}",
        f"Core strengths: {', '.join(CORE_STRENGTHS)}",
        f"Does NOT have: {', '.join(DOES_NOT_HAVE)}",
        f"Education: {EDUCATION}",
        "",
        "Target roles (priority order):",
    ]
    for i, role in enumerate(TARGET_ROLES, start=1):
        lines.append(f"  {i}. {role}")
    lines.append("")
    lines.append(f"NOT a good fit for: {', '.join(NOT_GOOD_FIT)}")
    return "\n".join(lines)
