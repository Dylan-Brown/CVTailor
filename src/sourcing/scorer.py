"""
sourcing/scorer.py

Job scoring engine, ported from the archived RemoteJobApplier project's
scoring/scorer.py. All the deterministic logic -- hard disqualifiers,
role-type classification, skill-gap penalties, salary/remote/firm/
seniority scoring, and the weighted-total math -- is kept as-is, since
that's the part encoding real accumulated judgment about what's worth
your time and none of it depended on the scraping/submission machinery
that got RemoteJobApplier retired.

What changed from the original:
  - The LLM call now goes through CVTailor's own core/llm_dispatch.
    call_llm_structured(...) instead of a raw anthropic.Anthropic()
    client + JSON-fence-stripping. This gets you the same structured-
    output reliability (forced tool call, not "please reply with only
    JSON") and multi-provider support (--provider claude|gemini) that
    every other CVTailor stage already has, instead of a third,
    less-robust LLM-calling convention living in a scoring-only corner.
  - Resume text is passed in directly as a string by the caller
    (stage1_score.py) instead of being loaded through RemoteJobApplier's
    AppConfig.get_resume_text(). Point the stage script at whatever
    resume text you want scored against -- typically the same resume
    stage2_ingest already extracted from, or the current claims ledger
    rendered back to prose.

Usage (called from src/stages/py_stage01_score_jd_input.py, not run directly):

    from sourcing.scorer import JobScorer
    from sourcing.config import ScoringConfig

    scorer = JobScorer(ScoringConfig(), resume_text=resume_text)
    score, breakdown, rationale = scorer.score_job(job_dict)
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.append(str(Path(__file__).resolve().parent.parent.parent / "core"))  # src/sourcing/ -> src/ -> jobs/, then src/core/
from llm_dispatch import call_llm_structured

from .config import ScoringConfig

logger = logging.getLogger(__name__)


# ─── Hard disqualifier patterns ───────────────────────────────────────────────

DISQUALIFIER_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("contract", "Contract/1099/C2C role",
     re.compile(
         r"\b(contract(or)?|1099|c2c|corp.?to.?corp|freelance|independent contractor"
         r"|w2 contract|contract.?to.?hire|contract position)\b",
         re.I
     )),
    ("clearance", "Active security clearance required",
     re.compile(
         r"\b(active|current|must have|require[sd]?).{0,20}"
         r"(secret|top secret|ts\/sci|security clearance|dod clearance|clearance required)\b"
         r"|clearance required|must be clearable|clearance is required",
         re.I
     )),
    ("experience_10plus", "Requires 10+ years experience",
     re.compile(
         r"\b(10\+|10 or more|ten \(10\)|minimum 10|at least 10)"
         r".{0,30}years?.{0,20}(experience|exp)\b"
         r"|\b(12|13|14|15)\+?\s*years?.{0,20}(experience|exp)\b",
         re.I
     )),
    ("drug_test", "Drug testing required",
     re.compile(
         r"\b(drug test(ing)?|pre.?employment drug|substance (abuse )?test(ing)?|"
         r"drug.?free workplace policy)\b",
         re.I
     )),
    ("international_only", "International/non-US only",
     re.compile(
         r"\b(canada only|uk only|europe only|eu only|emea only|apac only|"
         r"australia only|germany only|uk residents only|must be based in (canada|uk|europe|"
         r"australia|germany|france|netherlands|ireland)|not available (in|for) (the )?us[a]?\b|"
         r"outside (the )?us[a]?|non.?us|excluding (the )?us[a]?)\b",
         re.I
     )),
]


# ─── Role type classifier ────────────────────────────────────────────────────
# Maps job content to your priority hierarchy.
# Returns (role_type_name, score_0_to_1, explanation)

ROLE_TYPE_RULES: list[tuple[str, float, list[re.Pattern], list[re.Pattern]]] = [
    # (name, base_score, title_patterns, description_patterns)

    # Tier 1: Cloud Architecture — highest priority
    ("cloud_architect", 1.0,
     [re.compile(r"\b(cloud architect|solutions architect|enterprise architect|"
                 r"principal architect|aws architect|azure architect|gcp architect)\b", re.I)],
     [re.compile(r"\b(cloud architecture|design.*cloud|architect.*solution|"
                 r"technical.*strategy|well.architected)\b", re.I)]),

    # Tier 2: Full-stack product engineering
    ("fullstack_product", 0.85,
     [re.compile(r"\b(full.?stack|fullstack|software engineer|product engineer|"
                 r"application engineer|backend engineer|senior engineer)\b", re.I)],
     [re.compile(r"\b(react|next\.?js|typescript|node\.?js|django|fastapi|flask|"
                 r"rest api|graphql|python.*api|api.*python|aws lambda|dynamodb|"
                 r"frontend.*backend|backend.*frontend|web application)\b", re.I)]),

    # Tier 3: Platform / infrastructure engineering
    ("platform_infra", 0.75,
     [re.compile(r"\b(platform engineer|infrastructure engineer|staff engineer|"
                 r"site reliability|sre|devops engineer)\b", re.I)],
     [re.compile(r"\b(terraform|pulumi|ci.?cd|github actions|observability|"
                 r"distributed systems|microservices|service mesh|aws cdk)\b", re.I)]),

    # Tier 4: Fintech / payments domain
    ("fintech", 0.70,
     [re.compile(r"\b(payments|fintech|banking|financial.*engineer)\b", re.I)],
     [re.compile(r"\b(payment processing|financial infrastructure|banking api|"
                 r"transaction|ledger|compliance|regulatory|fraud)\b", re.I)]),

    # Tier 5: ML engineering / AI systems
    ("ml_engineering", 0.60,
     [re.compile(r"\b(ml engineer|ai engineer|machine learning engineer|"
                 r"applied.*scientist)\b", re.I)],
     [re.compile(r"\b(model training|inference|llm|fine.?tuning|pytorch|tensorflow|"
                 r"mlflow|model deployment|feature engineering)\b", re.I)]),
]

# Penalty domains — these reduce the role type score significantly
PENALTY_ROLE_PATTERNS: list[tuple[str, float, re.Pattern, re.Pattern]] = [
    # (name, score_cap, title_pattern, description_pattern)

    ("pure_devops_sre", 0.35,
     re.compile(r"\b(devops engineer|sre engineer|site reliability engineer|"
                r"systems administrator|sysadmin|cloud operations)\b", re.I),
     re.compile(r"\b(incident response|on.?call|monitoring|alerting|"
                r"deployment pipeline|infrastructure.*only|ops.*team)\b", re.I)),

    ("mlops_data_eng", 0.35,
     re.compile(r"\b(mlops|data engineer|data pipeline|etl developer|"
                r"analytics engineer)\b", re.I),
     re.compile(r"\b(airflow|spark|hadoop|kafka|data warehouse|dbt|"
                r"snowflake.*engineer|pipeline.*data)\b", re.I)),
]


def classify_role_type(job: dict) -> tuple[str, float, str]:
    """
    Classify job into your role priority hierarchy.
    Returns (role_type, score_0_to_1, explanation).

    Title signals are weighted 2x description signals. Penalty roles
    (pure DevOps, MLOps) are checked first on title -- a "DevOps
    Engineer" title is penalized unless the description shows clear
    engineering/platform scope beyond ops.
    """
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()

    for pen_name, score_cap, title_pat, desc_pat in PENALTY_ROLE_PATTERNS:
        if not title_pat.search(title):
            continue

        high_tier_title = any(
            any(p.search(title) for p in title_patterns)
            for name, base_score, title_patterns, desc_patterns in ROLE_TYPE_RULES
            if base_score >= 0.80
        )
        if high_tier_title:
            break

        engineering_scope_signals = re.compile(
            r"\b(software engineer|product engineer|build.*feature|ship.*code|"
            r"develop.*service|write.*software|application development|"
            r"full.?stack|terraform|pulumi|aws cdk|infrastructure as code|"
            r"backend (service|api|system)|api development|feature development)\b",
            re.I
        )
        has_engineering_scope = bool(engineering_scope_signals.search(desc))

        if has_engineering_scope:
            score = 0.60
            return "platform_infra_devops", round(score, 3), "DevOps+engineering scope"
        else:
            return pen_name, score_cap, f"Penalized: {pen_name} (ops-only DevOps title)"

    best_score = 0.0
    best_name = "unknown"
    best_explanation = "No domain match"

    for name, base_score, title_patterns, desc_patterns in ROLE_TYPE_RULES:
        title_hit = any(p.search(title) for p in title_patterns)
        desc_hits = sum(1 for p in desc_patterns if p.search(desc))

        if title_hit:
            score = base_score
        elif desc_hits >= 2:
            score = base_score * 0.75
        elif desc_hits == 1:
            score = base_score * 0.50
        else:
            continue

        if score > best_score:
            best_score = score
            best_name = name
            hit_type = "title" if title_hit else f"{desc_hits} desc keywords"
            best_explanation = f"{name} ({hit_type})"

    if best_score < 0.45:
        for pen_name, score_cap, title_pat, desc_pat in PENALTY_ROLE_PATTERNS:
            if desc_pat.search(desc):
                best_score = min(best_score or 0.25, score_cap)
                best_explanation = f"Penalized: {pen_name} (desc match)"
                best_name = pen_name
                break

    if best_score == 0.0:
        best_score = 0.20
        best_explanation = "Unknown domain"

    return best_name, round(best_score, 3), best_explanation


# ─── Skill gap penalties ─────────────────────────────────────────────────────

SKILL_GAP_PENALTIES: list[tuple[str, float, re.Pattern, bool]] = [
    ("golang", 1.5, re.compile(r"\b(go|golang)\b", re.I), True),
    ("kubernetes_primary", 1.0, re.compile(r"\b(kubernetes|k8s)\b", re.I), True),
    ("cpp", 1.5, re.compile(r"\b(c\+\+|cpp|systems programming)\b", re.I), True),
]

PRIMARY_SIGNALS = re.compile(
    r"\b(required|must have|must.know|must be|primary|core|essential|"
    r"strong experience|expert|proficient in|hands.on experience with|"
    r"you will use|you.ll use|day.to.day|primarily)\b",
    re.I
)


def check_disqualifiers(job: dict) -> Optional[tuple[str, str]]:
    text = " ".join([
        job.get("title") or "",
        job.get("description") or "",
        job.get("location") or "",
    ])
    for name, reason, pattern in DISQUALIFIER_PATTERNS:
        if pattern.search(text):
            if name == "clearance":
                if not re.search(r"\b(security|government|dod|federal|classified)\b", text, re.I):
                    continue
            logger.info(f"Disqualified '{job.get('title')}' @ '{job.get('company')}': {reason}")
            return name, reason

    location = (job.get("location") or "").strip()
    INTERNATIONAL_ONLY_LOCATIONS = re.compile(
        r"\b(canada|uk|united kingdom|europe|germany|france|netherlands|ireland|"
        r"australia|new zealand|singapore|india|brazil|mexico)\b",
        re.I
    )
    USA_SIGNALS = re.compile(
        r"\b(usa?|united states|remote us|us remote|anywhere|worldwide|global)\b",
        re.I
    )
    if (INTERNATIONAL_ONLY_LOCATIONS.search(location)
            and not USA_SIGNALS.search(text)
            and not USA_SIGNALS.search(location)):
        reason = f"International only: {location}"
        logger.info(f"Disqualified '{job.get('title')}' @ '{job.get('company')}': {reason}")
        return "international_only", reason

    return None


def check_skill_gaps(job: dict, llm_primary_reqs: list[str]) -> list[tuple[str, float]]:
    penalties = []
    primary_text = " ".join(llm_primary_reqs).lower()
    full_text = (job.get("description") or "").lower()

    for name, penalty, pattern, primary_only in SKILL_GAP_PENALTIES:
        if not pattern.search(full_text):
            continue
        if primary_only:
            is_primary = bool(pattern.search(primary_text))
            if not is_primary:
                for m in pattern.finditer(full_text):
                    start = max(0, m.start() - 100)
                    end = min(len(full_text), m.end() + 100)
                    context = full_text[start:end]
                    if PRIMARY_SIGNALS.search(context):
                        is_primary = True
                        break
            if not is_primary:
                continue
        penalties.append((name, penalty))
        logger.info(f"Skill gap penalty: {name} ({penalty} pts) for '{job.get('title')}'")
    return penalties


# ─── LLM structured-output contract ──────────────────────────────────────────
# Replaces the original's raw-JSON-parse-with-fence-stripping. Forced tool
# call via call_llm_structured means this can't come back malformed.

class ScorerLLMOutput(BaseModel):
    resume_match: float = Field(ge=0.0, le=1.0)
    ambiguous_jd: bool = False
    scope_match: float = Field(ge=0.0, le=1.0)
    primary_requirements: list[str] = Field(default_factory=list)
    missing_primary: list[str] = Field(default_factory=list)
    reskilling_required: bool = False
    rationale: str = ""
    seniority_match: float = Field(ge=0.0, le=1.0, default=0.5)
    likely_salary_ok: bool = False
    red_flags: list[str] = Field(default_factory=list)
    green_flags: list[str] = Field(default_factory=list)


SCORER_SYSTEM = """You are a precise, strict job application scoring assistant.
You evaluate job descriptions against a candidate resume. Be conservative — err toward lower scores."""

SCORER_USER_TEMPLATE = """Evaluate this job against the candidate's resume. Be strict.

## CANDIDATE PROFILE
{candidate_profile}

## RESUME
{resume}

## JOB POSTING
Title: {title}
Company: {company}
Location: {location}
Salary: {salary}
Description:
{description}

## INSTRUCTIONS
Evaluate strictly. A job that requires significant re-skilling (new primary tech stack,
ops-only scope with no engineering, or 3+ missing primary requirements) should score
resume_match <= 0.45 even if there is partial overlap.

Calibration for resume_match:
  0.85+ = near-perfect fit, could start day one
  0.70-0.84 = strong fit, minor gaps only
  0.55-0.69 = moderate fit, one meaningful gap
  0.40-0.54 = weak fit, multiple gaps or wrong domain
  0.00-0.39 = poor fit, significant re-skilling needed

scope_match measures whether the SCOPE of the role (not just skills) matches the
candidate's target roles, not just their skills.

Title flexibility: the candidate markets themselves as backend, full-stack, OR
frontend depending on the role -- their resume's self-chosen title is not a
ceiling or a fixed category. A JD titled differently from the candidate's
resume title (e.g. JD says "Backend Engineer", resume says "Full-Stack
Engineer") is NOT itself a scope mismatch. Judge scope_match on actual
overlapping responsibilities and required skills, not on title-string
similarity. Only treat it as a real scope mismatch if the day-to-day work
itself is a stretch (e.g. a role that's overwhelmingly frontend-only UI/CSS
work when the candidate's real experience is backend-heavy, or vice versa).

Calibration rules (enforce these hard):
- If missing_primary has 1 item AND it's a learnable tool/framework: resume_match <= 0.72
- If missing_primary has 1 item AND it's a language or domain shift: resume_match <= 0.65
- If missing_primary has 2+ items: resume_match <= 0.50
- If reskilling_required is true: resume_match <= 0.50, scope_match <= 0.50
- If scope is pure DevOps/SRE with no software engineering: scope_match <= 0.40
- If scope is MLOps/data engineering: scope_match <= 0.45
- "learnable on the job" gaps (a specific scheduler, a specific DB) should NOT
  set reskilling_required=true"""


# ─── Scorer ───────────────────────────────────────────────────────────────────

class JobScorer:
    def __init__(self, cfg: ScoringConfig, resume_text: str, candidate_profile: str,
                 provider: str | None = None):
        self.cfg = cfg
        self.weights = cfg.weights
        self.resume_text = resume_text
        self.candidate_profile = candidate_profile
        self.provider = provider

    def score_job(self, job: dict) -> tuple[float, dict, str]:
        """
        Score a job. Returns (score: float, breakdown: dict, rationale: str).
        Score is 0.0-10.0.
        """
        breakdown: dict = {}

        disq = check_disqualifiers(job)
        if disq:
            disq_name, disq_reason = disq
            breakdown["disqualified"] = disq_reason
            breakdown["final"] = 0.0
            return 0.0, breakdown, f"DISQUALIFIED: {disq_reason}"

        role_type, role_score, role_explanation = classify_role_type(job)
        breakdown["role_type"] = role_type
        breakdown["role_type_score"] = role_score
        breakdown["role_type_explanation"] = role_explanation

        try:
            llm = self._llm_score(job)
        except Exception as e:
            # Previously this fell back to hardcoded resume_match=0.35/
            # scope_match=0.35 and kept going -- producing a plausible-
            # looking numeric score that was actually fabricated, not a
            # real assessment. That's worse than no score at all. Abort
            # here instead: no blended number, no final score, an
            # unmistakable failure state the caller has to handle
            # explicitly rather than one that silently looks legitimate.
            breakdown["llm_call_failed"] = True
            breakdown["error"] = str(e)
            return None, breakdown, f"SCORING FAILED -- LLM call errored, no score computed: {e}"

        resume_match = llm.resume_match
        scope_match = llm.scope_match
        red_flags = llm.red_flags
        green_flags = llm.green_flags
        primary_reqs = llm.primary_requirements
        missing_primary = llm.missing_primary
        reskilling = llm.reskilling_required
        ambiguous_jd = llm.ambiguous_jd
        llm_rationale = llm.rationale

        red_text_lower = " ".join(red_flags).lower()
        if not ambiguous_jd and any(s in red_text_lower for s in [
            "truncated", "incomplete", "missing description", "no description",
            "cannot fully assess", "insufficient information", "job posting incomplete"
        ]):
            ambiguous_jd = True

        breakdown["ambiguous_jd"] = ambiguous_jd

        if reskilling and not ambiguous_jd:
            resume_match = min(resume_match, 0.50)
            scope_match = min(scope_match, 0.50)
        elif reskilling and ambiguous_jd:
            resume_match = min(resume_match, 0.62)
            scope_match = min(scope_match, 0.62)

        if ambiguous_jd:
            if len(missing_primary) >= 2:
                resume_match = min(resume_match, 0.65)
            elif len(missing_primary) == 1:
                resume_match = min(resume_match, 0.75)
        else:
            if len(missing_primary) >= 2:
                resume_match = min(resume_match, 0.50)
            elif len(missing_primary) == 1:
                learnable_gap_signals = re.compile(
                    r"\b(kubernetes|k8s|eks|terraform|ansible|specific|scheduler|"
                    r"tasktiger|celery|particular|platform|tooling|framework)\b", re.I
                )
                gap_text = " ".join(missing_primary).lower()
                is_learnable = bool(learnable_gap_signals.search(gap_text))
                resume_match = min(resume_match, 0.72 if is_learnable else 0.65)

        blended_role_score = (role_score * 0.6) + (scope_match * 0.4)
        breakdown["role_type_score_blended"] = round(blended_role_score, 3)

        breakdown["resume_match"] = resume_match
        breakdown["scope_match"] = scope_match
        breakdown["primary_requirements"] = primary_reqs
        breakdown["missing_primary"] = missing_primary
        breakdown["reskilling_required"] = reskilling
        breakdown["red_flags"] = red_flags
        breakdown["green_flags"] = green_flags

        salary_score = self._score_salary(job, llm)
        breakdown["salary_match"] = salary_score

        remote_score = self._score_remote(job)
        breakdown["remote_confirmed"] = remote_score

        firm_score = self._score_firm(job)
        breakdown["firm_boost"] = firm_score

        seniority_score = self._score_seniority(job, llm.seniority_match)
        breakdown["seniority_match"] = seniority_score

        w = self.weights
        weighted = (
            resume_match       * w.resume_match +
            blended_role_score * w.role_type_match +
            salary_score       * w.salary_match +
            remote_score        * w.remote_confirmed +
            firm_score          * w.firm_boost
        )
        final = round(weighted * 10.0, 2)

        seniority_delta = (seniority_score - 0.5) * 0.6
        final = round(final + seniority_delta, 2)

        ENGINEERING_ROLE_TYPES = {
            "cloud_architect", "fullstack_product", "platform_infra",
            "platform_infra_devops", "fintech", "ml_engineering"
        }
        gap_penalties = check_skill_gaps(job, primary_reqs + missing_primary)
        if gap_penalties:
            adjusted_penalties = []
            for gap_name, penalty in gap_penalties:
                adjusted = penalty * 0.5
                if role_type in ENGINEERING_ROLE_TYPES and not reskilling:
                    adjusted = adjusted * 0.5
                adjusted = round(adjusted, 2)
                adjusted_penalties.append((gap_name, adjusted))
            total_penalty = sum(p for _, p in adjusted_penalties)
            breakdown["skill_gap_penalties"] = {n: p for n, p in adjusted_penalties}
            final = max(0.0, final - total_penalty)

        if remote_score == 0.0:
            final = 0.0
            breakdown["hard_penalty"] = "not_remote"

        red_text = " ".join(red_flags).lower()
        if any(w_ in red_text for w_ in ["contract", "1099", "c2c", "clearance required"]):
            final = 0.0
            breakdown["hard_penalty"] = "disqualifier_in_red_flags"

        if reskilling and role_score <= 0.35:
            final = min(final, 5.0)
            breakdown["reskilling_cap"] = True

        final = round(max(0.0, final), 2)
        breakdown["final"] = final

        parts = [f"Resume match: {resume_match:.0%}. Role type: {role_type} ({role_score:.0%})."]
        if missing_primary:
            parts.append(f"Missing: {', '.join(missing_primary[:3])}.")
        if reskilling:
            parts.append("Significant re-skilling required.")
        if gap_penalties:
            parts.append(f"Skill gap penalties: {', '.join(n for n, _ in gap_penalties)}.")
        parts.append(llm_rationale)
        if red_flags:
            parts.append(f"[flag] {'; '.join(red_flags[:2])}")
        rationale = " ".join(p for p in parts if p).strip()

        return final, breakdown, rationale

    def _llm_score(self, job: dict) -> ScorerLLMOutput:
        description = (job.get("description") or "")[:3000]
        salary_str = ""
        if job.get("salary_raw"):
            salary_str = job["salary_raw"]
        elif job.get("salary_min"):
            sal_max = job.get("salary_max")
            salary_str = f"${job['salary_min']:,}" + (f" - ${sal_max:,}" if sal_max else "+")

        prompt = SCORER_USER_TEMPLATE.format(
            candidate_profile=self.candidate_profile,
            resume=self.resume_text[:4000],
            title=job.get("title", ""),
            company=job.get("company", ""),
            location=job.get("location", ""),
            salary=salary_str or "Not listed",
            description=description or "Not provided",
        )

        # No try/except here -- let a failed call propagate up to
        # score_job(), which aborts scoring outright rather than
        # substituting a fabricated-looking fallback score. Silently
        # returning a plausible number on failure is worse than a loud
        # error: it looked like a real assessment when it wasn't one.
        return call_llm_structured(
            system=SCORER_SYSTEM,
            user=prompt,
            response_model=ScorerLLMOutput,
            tier="standard",
            provider=self.provider,
        )

    def _score_salary(self, job: dict, llm_result: ScorerLLMOutput) -> float:
        sal_min = job.get("salary_min")
        target = self.cfg.min_salary

        if sal_min:
            if sal_min >= target:           return 1.0
            elif sal_min >= target * 0.90:  return 0.7
            elif sal_min >= target * 0.80:  return 0.4
            elif sal_min >= target * 0.70:  return 0.2
            return 0.05

        title = (job.get("title") or "").lower()
        if llm_result.likely_salary_ok:
            if any(t in title for t in ["staff", "principal", "architect", "director"]):
                return 0.65
            if any(t in title for t in ["senior", "sr "]):
                return 0.50
            return 0.40

        if any(t in title for t in ["staff", "principal", "architect", "director"]):
            return 0.45
        if any(t in title for t in ["senior", "sr "]):
            return 0.35
        return 0.20

    def _score_remote(self, job: dict) -> float:
        rt = (job.get("remote_type") or "").lower()
        if rt == "remote":   return 1.0
        if rt == "hybrid":   return 0.0 if self.cfg.required_remote else 0.4
        if rt == "onsite":   return 0.0

        desc = " ".join([
            job.get("description") or "",
            job.get("location") or "",
        ]).lower()

        if re.search(r"\b(fully remote|100% remote|remote.first|remote only|"
                     r"work from anywhere|wfa)\b", desc):
            return 0.95
        if re.search(r"\bremote\b", desc):
            return 0.50
        return 0.15

    def _score_firm(self, job: dict) -> float:
        company = (job.get("company") or "").lower()
        for firm in self.cfg.target_firms:
            if firm.lower() in company:
                return 1.0
        return 0.0

    def _score_seniority(self, job: dict, llm_seniority: float) -> float:
        title = (job.get("title") or "").lower()
        keyword_score = 0.0
        for t in self.cfg.target_titles:
            if t.lower() in title:
                keyword_score = 1.0
                break
        if not keyword_score:
            if any(w_ in title for w_ in ["lead", "principal", "staff", "architect"]):
                keyword_score = 0.9
            elif "senior" in title or "sr " in title:
                keyword_score = 0.8
            elif "junior" in title or "jr " in title:
                keyword_score = 0.3
            elif "intern" in title:
                keyword_score = 0.0
        return round((keyword_score + llm_seniority) / 2, 3)
