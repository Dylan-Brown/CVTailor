"""
sourcing/config.py

Scoring configuration, ported from the archived RemoteJobApplier project's
config.py. Trimmed to only what scoring needs -- the ATS-credential and
browser-automation config from the original is gone along with the rest
of that project, since it's not needed here.

Edit target_firms / target_titles / min_salary directly as your targets
shift -- these are the actual levers, no need to touch scorer.py itself
for most tuning.
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


@dataclass
class ScoringConfig:
    min_salary: int = 150_000
    max_salary: int = 400_000
    required_remote: bool = True        # filter out non-remote
    weights: ScoringWeights = field(default_factory=ScoringWeights)

    target_firms: list[str] = field(default_factory=lambda: [
        # Cloud/infra-heavy companies with strong remote culture
        "Stripe", "Cloudflare", "HashiCorp", "Datadog", "Snowflake",
        "Confluent", "MongoDB", "Grafana Labs", "PlanetScale",
        "Pulumi", "Temporal", "Vercel", "Fly.io", "Render",
        "AWS", "Google Cloud", "Microsoft Azure",
        # Fintech (leverages Capital One background)
        "Plaid", "Brex", "Mercury", "Ramp", "Marqeta",
        "Affirm", "Chime", "Betterment", "Wealthfront",
        # Remote-first engineering orgs
        "GitLab", "Automattic", "Basecamp", "Buffer", "Zapier",
        "Netlify", "Supabase", "PagerDuty", "Incident.io",
    ])

    target_titles: list[str] = field(default_factory=lambda: [
        "Cloud Architect", "Solutions Architect", "Principal Engineer",
        "Staff Engineer", "Senior Software Engineer", "Senior SWE",
    ])
