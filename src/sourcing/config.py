#!/usr/bin/env python3
"""
sourcing/config.py -- ScoringConfig/ScoringWeights dataclasses for job
scoring. target_firms/min_salary/etc. come from config/applicant_info.json's
"job_scoring" section via from_dict(), not hardcoded here.
"""


from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScoringWeights:
    resume_match: float = 0.40       # LLM resume <-> JD skill fit
    role_type_match: float = 0.20    # Domain classification vs priority hierarchy
    salary_match: float = 0.15       # min_salary threshold (strict when unlisted)
    remote_confirmed: float = 0.15   # Remote type (strict on ambiguous)
    firm_boost: float = 0.10         # Target company bonus

    @classmethod
    def from_dict(cls, data: dict) -> "ScoringWeights":
        defaults = cls()
        return cls(**{f: data.get(f, getattr(defaults, f)) for f in
                       ("resume_match", "role_type_match", "salary_match",
                        "remote_confirmed", "firm_boost")})


@dataclass
class ScoringConfig:
    min_salary: int = 150_000
    max_salary: int = 400_000
    required_remote: bool = True        # filter out non-remote
    weights: ScoringWeights = field(default_factory=ScoringWeights)

    # Empty by default -- deliberately no baked-in target companies/titles.
    # These are personal preferences, not a sensible shared default; see
    # from_dict() below, the actual way this gets populated.
    target_firms: list[str] = field(default_factory=list)
    target_titles: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "ScoringConfig":
        """Builds a ScoringConfig from config/applicant_info.json's
        "job_scoring" section (see load_applicant_info() in
        batch_common.py). Any field the section omits falls back to this
        dataclass's own defaults."""
        defaults = cls()
        return cls(
            min_salary=data.get("min_salary", defaults.min_salary),
            max_salary=data.get("max_salary", defaults.max_salary),
            required_remote=data.get("required_remote", defaults.required_remote),
            weights=ScoringWeights.from_dict(data.get("weights", {})),
            target_firms=list(data.get("target_firms", [])),
            target_titles=list(data.get("target_titles", [])),
        )
