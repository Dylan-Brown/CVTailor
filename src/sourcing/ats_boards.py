#!/usr/bin/env python3
"""
sourcing/ats_boards.py -- fetches postings from Greenhouse/Lever/Ashby's
public JSON APIs (no scraping, no auth needed). Yields dicts already
shaped to JDInput's schema. Not currently imported by anything else.
"""


from __future__ import annotations

import logging
import re
import time
from html import unescape
from typing import Iterator, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_DELAY_SEC = 1.0  # be a good citizen against these public APIs


# ─── Company registries ───────────────────────────────────────────────────────
# Format: (display_name, ats_token). Add more any time -- find the token
# from the company's careers page URL (e.g. boards.greenhouse.io/<token>).

GREENHOUSE_COMPANIES: list[tuple[str, str]] = [
    ("Stripe", "stripe"), ("Plaid", "plaid"), ("Brex", "brex"),
    ("Marqeta", "marqeta"), ("Affirm", "affirm"), ("Chime", "chime"), ("Ramp", "ramp"),
    ("Cloudflare", "cloudflare"), ("Datadog", "datadoghq"), ("Snowflake", "snowflake"),
    ("MongoDB", "mongodb"), ("PagerDuty", "pagerduty"), ("Confluent", "confluent"),
    ("GitLab", "gitlab"), ("Figma", "figma"), ("Notion", "notion"), ("Zapier", "zapier"),
    ("Netlify", "netlify"), ("Supabase", "supabase"),
    ("Anthropic", "anthropic"), ("Scale AI", "scaleai"), ("Cohere", "cohere"),
    ("HashiCorp", "hashicorp"), ("Grafana Labs", "grafanalabs"),
    ("Temporal", "temporal"), ("Incident.io", "incidentio"),
]

LEVER_COMPANIES: list[tuple[str, str]] = [
    ("Betterment", "betterment"), ("Wealthfront", "wealthfront"), ("Mercury", "mercury"),
    ("Vercel", "vercel"), ("Render", "render"), ("Fly.io", "fly"),
    ("PlanetScale", "planetscale"), ("Pulumi", "pulumi"),
    ("Automattic", "automattic"), ("Buffer", "buffer"), ("Basecamp", "basecamp"),
    ("Carta", "carta"), ("Rippling", "rippling"), ("Gusto", "gusto"),
]

ASHBY_COMPANIES: list[tuple[str, str]] = [
    ("Linear", "linear"), ("Retool", "retool"), ("Loom", "loom"), ("Coda", "coda"),
    ("Descript", "descript"), ("Buildkite", "buildkite"), ("Clerk", "clerk"),
    ("Resend", "resend"), ("Neon", "neon"), ("Turso", "turso"), ("Cal.com", "calcom"),
    ("Trigger.dev", "triggerdev"), ("Mintlify", "mintlify"),
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _strip_html(html_str: str) -> str:
    """Strip HTML tags and unescape entities."""
    if not html_str:
        return ""
    text = BeautifulSoup(html_str, "html.parser").get_text(separator="\n", strip=True)
    return unescape(text)[:5000]


def _infer_remote(location: str, description: str) -> str:
    """Returns a value matching JDInput.remote_type's literal options."""
    text = f"{location} {description}".lower()
    if re.search(r"\b(fully remote|100% remote|remote.first|remote only|work from anywhere)\b", text):
        return "remote"
    if re.search(r"\bremote\b", text):
        return "remote"
    if re.search(r"\bhybrid\b", text):
        return "hybrid"
    return "unspecified"


def _matches_keywords(title: str, description: str, keywords: list[str]) -> bool:
    text = f"{title} {description or ''}".lower()
    return any(kw.lower() in text for kw in keywords)


def _to_jd_input_dict(*, source_url: str, company: str, role_title: str, location: str,
                       remote_type: str, salary_min: Optional[int], salary_max: Optional[int],
                       salary_raw: Optional[str], description: str) -> dict:
    """Shapes a parsed posting into CVTailor's JDInput schema fields."""
    return {
        "source_url": source_url,
        "company": company,
        "role_title": role_title,
        "location": location,
        "remote_type": remote_type,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": "USD" if (salary_min or salary_max) else None,
        "salary_period": "year" if (salary_min or salary_max) else None,
        "full_description_text": description,
        "requirements_raw": [],
        "_sourcing_note": salary_raw,  # human-readable salary string, informational only
    }


# ─── Greenhouse ───────────────────────────────────────────────────────────────

def fetch_greenhouse_jobs(
    keywords: list[str],
    max_jobs: int = 100,
    companies: Optional[list[tuple[str, str]]] = None,
    delay: float = DEFAULT_DELAY_SEC,
) -> Iterator[dict]:
    companies = companies or GREENHOUSE_COMPANIES
    seen: set[str] = set()
    count = 0

    for company_name, token in companies:
        if count >= max_jobs:
            break
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        logger.info(f"Greenhouse: fetching {company_name} ({token})")
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"Greenhouse: {company_name} failed: {e}")
            time.sleep(delay)
            continue

        for job in data.get("jobs", []):
            if count >= max_jobs:
                break
            job_id = job.get("id")
            if not job_id or job_id in seen:
                continue

            title = job.get("title", "")
            description = _strip_html(job.get("content", ""))
            if not _matches_keywords(title, description, keywords):
                continue

            location_obj = job.get("location") or {}
            location = location_obj.get("name", "") if isinstance(location_obj, dict) else str(location_obj)

            salary_min, salary_max, salary_raw = None, None, None
            pay_ranges = job.get("pay_input_ranges", [])
            if pay_ranges:
                r = pay_ranges[0]
                salary_min = r.get("min_cents", 0) // 100 if r.get("min_cents") else None
                salary_max = r.get("max_cents", 0) // 100 if r.get("max_cents") else None
                if salary_min and salary_max:
                    salary_raw = f"${salary_min:,} - ${salary_max:,} {r.get('currency_type', 'USD')}"

            seen.add(job_id)
            count += 1
            yield _to_jd_input_dict(
                source_url=job.get("absolute_url", f"https://boards.greenhouse.io/{token}/jobs/{job_id}"),
                company=company_name, role_title=title, location=location,
                remote_type=_infer_remote(location, description),
                salary_min=salary_min, salary_max=salary_max, salary_raw=salary_raw,
                description=description,
            )
        time.sleep(delay)


# ─── Lever ────────────────────────────────────────────────────────────────────

def fetch_lever_jobs(
    keywords: list[str],
    max_jobs: int = 100,
    companies: Optional[list[tuple[str, str]]] = None,
    delay: float = DEFAULT_DELAY_SEC,
) -> Iterator[dict]:
    companies = companies or LEVER_COMPANIES
    seen: set[str] = set()
    count = 0

    for company_name, slug in companies:
        if count >= max_jobs:
            break
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        logger.info(f"Lever: fetching {company_name} ({slug})")
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            jobs_data = resp.json()
            if not isinstance(jobs_data, list):
                logger.warning(f"Lever: unexpected response for {company_name}")
                time.sleep(delay)
                continue
        except Exception as e:
            logger.warning(f"Lever: {company_name} failed: {e}")
            time.sleep(delay)
            continue

        for job in jobs_data:
            if count >= max_jobs:
                break
            job_id = job.get("id", "")
            if not job_id or job_id in seen:
                continue

            title = job.get("text", "")
            categories = job.get("categories") or {}
            location = categories.get("location", "")
            desc_html = job.get("description", "") + " " + " ".join(
                li.get("content", "") for li in job.get("lists", [])
            )
            description = _strip_html(desc_html)
            if not _matches_keywords(title, description, keywords):
                continue

            salary_raw, salary_min, salary_max = None, None, None
            salary_range = job.get("salaryRange")
            if salary_range:
                sal_min, sal_max = salary_range.get("min"), salary_range.get("max")
                interval = salary_range.get("interval", "per year")
                if sal_min and sal_max:
                    salary_raw = f"${sal_min:,} - ${sal_max:,} {salary_range.get('currency', 'USD')} {interval}"
                    if "year" in interval.lower():
                        salary_min, salary_max = sal_min, sal_max

            seen.add(job_id)
            count += 1
            yield _to_jd_input_dict(
                source_url=job.get("applyUrl") or job.get("hostedUrl") or "",
                company=company_name, role_title=title, location=location,
                remote_type=_infer_remote(location, description),
                salary_min=salary_min, salary_max=salary_max, salary_raw=salary_raw,
                description=description,
            )
        time.sleep(delay)


# ─── Ashby ────────────────────────────────────────────────────────────────────

def fetch_ashby_jobs(
    keywords: list[str],
    max_jobs: int = 100,
    companies: Optional[list[tuple[str, str]]] = None,
    delay: float = DEFAULT_DELAY_SEC,
) -> Iterator[dict]:
    companies = companies or ASHBY_COMPANIES
    seen: set[str] = set()
    count = 0

    for company_name, slug in companies:
        if count >= max_jobs:
            break
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
        logger.info(f"Ashby: fetching {company_name} ({slug})")
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"Ashby: {company_name} failed: {e}")
            time.sleep(delay)
            continue

        for job in data.get("jobs", []):
            if count >= max_jobs:
                break
            job_id = job.get("id", "")
            if not job_id or job_id in seen:
                continue

            title = job.get("title", "")
            location = job.get("location", "")
            is_remote = job.get("isRemote", False)
            workplace_type = (job.get("workplaceType") or "").lower()

            if is_remote or workplace_type == "remote":
                remote_type = "remote"
            elif workplace_type == "hybrid":
                remote_type = "hybrid"
            else:
                remote_type = _infer_remote(location, "")

            desc_raw = job.get("descriptionHtml") or job.get("descriptionPlain", "")
            description = _strip_html(desc_raw) if "<" in desc_raw else desc_raw[:5000]
            if not _matches_keywords(title, description, keywords):
                continue

            salary_raw, salary_min, salary_max = None, None, None
            compensation = job.get("compensation")
            if compensation:
                summary = (compensation.get("scrapeableCompensationSalarySummary")
                           or compensation.get("compensationTierSummary", ""))
                if summary:
                    salary_raw = summary
                    nums = re.findall(r"[\d,]+", summary)
                    if len(nums) >= 2:
                        salary_min = int(nums[0].replace(",", ""))
                        salary_max = int(nums[1].replace(",", ""))

            seen.add(job_id)
            count += 1
            yield _to_jd_input_dict(
                source_url=job.get("applyUrl") or job.get("jobUrl") or "",
                company=company_name, role_title=title, location=location,
                remote_type=remote_type,
                salary_min=salary_min, salary_max=salary_max, salary_raw=salary_raw,
                description=description,
            )
        time.sleep(delay)


# ─── Convenience: all boards, all target companies ───────────────────────────

def fetch_all_target_companies(keywords: list[str], max_jobs_per_board: int = 50) -> Iterator[dict]:
    """Queries Greenhouse, Lever, and Ashby in sequence for the curated
    target-company lists above. Each yielded dict is ready for
    `py_stage0_intake.py --jd-json`."""
    yield from fetch_greenhouse_jobs(keywords, max_jobs_per_board)
    yield from fetch_lever_jobs(keywords, max_jobs_per_board)
    yield from fetch_ashby_jobs(keywords, max_jobs_per_board)
