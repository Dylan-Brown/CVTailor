#!/usr/bin/env python3
"""
harvester_adapter.py — Stage 0 Translation Layer

Normalizes the raw 13-key JSON output from the Job Data Harvester (Chrome extension) 
into the strict `JDInput` schema expected by the CVTailor pipeline.

Key Transformations:
  1. Metadata Enrichment: Automatically stamps `scraped_at`, resolves `source_url`, 
     and preserves performance and intake metrics (e.g., `intake_enqueued_at`).
  2. Salary Parsing: Converts free-text salary strings (e.g., "$170k - $190k USD") 
     into structured numeric boundaries, currency, and pay periods.
  3. Text Synthesis: Reassembles the extension's split text fields (Role Description, 
     Responsibilities, Benefits, etc.) into a single, cohesive `full_description_text` 
     block while preserving labeled section boundaries for downstream LLM context.
  4. Direct Mapping: Passes pre-parsed arrays (Requirements, Responsibilities, NiceToHave) 
     directly into the canonical schema to bypass redundant LLM extraction.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone

CURRENCY_RE = re.compile(r"\b(USD|CAD|EUR|GBP|AUD|NZD)\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\$?\s?([\d][\d,]*(?:\.\d+)?)")


def parse_salary(salary_str: str | None) -> tuple[float | None, float | None, str | None, str | None]:
    """'$170,000 - $190,000 USD Annually' -> (170000.0, 190000.0, 'USD', 'year')
    Handles hyphen/en-dash/em-dash separators (doesn't actually split on
    the dash at all — just pulls every number-like token in order, which
    sidesteps the dash-character problem entirely)."""
    if not salary_str or not salary_str.strip():
        return None, None, None, None

    nums = [float(n.replace(",", "")) for n in NUMBER_RE.findall(salary_str)]
    salary_min = nums[0] if len(nums) >= 1 else None
    salary_max = nums[1] if len(nums) >= 2 else None

    currency_match = CURRENCY_RE.search(salary_str)
    currency = currency_match.group(1).upper() if currency_match else None

    lower = salary_str.lower()
    if "hour" in lower or "/hr" in lower or "hourly" in lower:
        period = "hour"
    elif "month" in lower:
        period = "month"
    else:
        period = "year"  # reasonable default for salaried ranges in this bracket

    return salary_min, salary_max, currency, period


def map_remote_type(workplace_type: str | None) -> str:
    if not workplace_type:
        return "unspecified"
    wt = workplace_type.strip().lower()
    if "remote" in wt:
        return "remote"
    if "hybrid" in wt:
        return "hybrid"
    if "onsite" in wt or "on-site" in wt or "on site" in wt:
        return "onsite"
    return "unspecified"


def synthesize_full_text(raw: dict) -> str:
    """Reassemble the extension's split fields into one labeled text
    blob, preserving section boundaries instead of flattening them."""
    parts = []
    company = raw.get("Company", "the company")
    if raw.get("CompanyDescription"):
        parts.append(f"About {company}:\n{raw['CompanyDescription']}")
    if raw.get("RoleDescription"):
        parts.append(f"Role overview:\n{raw['RoleDescription']}")
    if raw.get("Responsibilities"):
        parts.append("Responsibilities:\n" + "\n".join(f"- {r}" for r in raw["Responsibilities"]))
    if raw.get("Requirements"):
        parts.append("Requirements:\n" + "\n".join(f"- {r}" for r in raw["Requirements"]))
    if raw.get("NiceToHave"):
        parts.append("Nice to have:\n" + "\n".join(f"- {n}" for n in raw["NiceToHave"]))
    if raw.get("Benefits"):
        parts.append("Benefits:\n" + "\n".join(f"- {b}" for b in raw["Benefits"]))
    return "\n\n".join(parts)


def is_harvester_format(raw: dict) -> bool:
    """Detect the extension's schema vs. an already-canonical JDInput
    dict, so stage 0 can accept either without a CLI flag to distinguish
    them."""
    return "JobTitle" in raw and "role_title" not in raw


def extract_warm_contact(raw: dict) -> dict | None:
    """Maps the extension's WarmApplication sub-object into JDInput's
    warm_contact shape, treating empty strings the model was told to
    use for "not found" as None. Returns None entirely (not an
    all-None dict) if the extension didn't send a WarmApplication
    object at all -- payloads captured before this field existed."""
    wa = raw.get("WarmApplication")
    if not isinstance(wa, dict):
        return None
    contact = {
        "name": wa.get("PointOfContactName") or None,
        "contact_method": wa.get("PointOfContactMethod") or None,
        "email": wa.get("PointOfContactEmail") or None,
        "position": wa.get("PointOfContactPosition") or None,
    }
    return contact if any(contact.values()) else None


def normalize_harvester_json(raw: dict, source_url: str | None = None) -> dict:
    salary_min, salary_max, currency, period = parse_salary(raw.get("Salary"))
    return {
        "source_url": raw.get("SourceURL") or source_url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "company": raw.get("Company") or "Unknown",
        "role_title": raw.get("JobTitle") or "Unknown",
        "location": raw.get("Location"),
        "remote_type": map_remote_type(raw.get("WorkplaceType")),
        "employment_type": raw.get("EmploymentType"),
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": currency,
        "salary_period": period,
        "full_description_text": synthesize_full_text(raw),
        "requirements_raw": raw.get("Requirements", []),
        "responsibilities_raw": raw.get("Responsibilities", []),
        "nice_to_have_raw": raw.get("NiceToHave", []),
        "extraction_method": "dom_heuristic",
        "extension_version": "job_data_harvester_1.0.0",
        "warm_contact": extract_warm_contact(raw),
        "extraction_runtime_ms": raw.get("ExtractionRuntimeMs"),
        "intake_enqueued_at": raw.get("IntakeEnqueuedAt"),
        "intake_trigger_id": raw.get("IntakeTriggerId"),
    }