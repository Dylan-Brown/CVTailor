"""
harvester_adapter.py — normalizes Job Data Harvester's 13-key output
into the pipeline's JDInput contract.

The extension runs fully on-device (Gemini Nano, no network calls),
which is why source_url and scraped_at need adapter-side handling
rather than model extraction:

  - source_url: the extension stamps this deterministically from
    chrome.tabs (as "SourceURL" in the raw payload) rather than having
    the model guess at it from page text, which usually doesn't even
    contain its own URL. If a raw payload predates that change and has
    no SourceURL, falls back to the --source-url CLI flag if given,
    else stays None.
  - scraped_at: extension doesn't stamp one, so this sets it to "now"
    at adapter-run time. Slightly later than the actual scrape, but
    close enough for the pipeline's purposes (dedup, audit trail).
  - Salary: the extension gives one free-text string
    ("$170,000 - $190,000 USD Annually", "$142,800—$184,800 CAD", ...).
    parse_salary() pulls min/max/currency/period out of it. Tested
    against all three of your sample files' actual salary strings.
  - full_description_text: the extension already split the JD into
    RoleDescription/Responsibilities/Requirements/NiceToHave/Benefits
    rather than keeping one blob — arguably better structured than raw
    scraped prose. synthesize_full_text() reassembles a labeled version
    of it so stages 3-4 still get one coherent text to reason over,
    without losing the section boundaries.
  - Requirements/Responsibilities/NiceToHave map straight into
    requirements_raw/responsibilities_raw/nice_to_have_raw — exactly
    the fields they were designed for, no LLM re-derivation needed.
    Stage 4 weights all three as primary/authoritative signal, ranked
    Requirements+Responsibilities above NiceToHave, with the prose in
    full_description_text (RoleDescription/CompanyDescription) treated
    as context rather than a coverage-scoring input.
  - WarmApplication (point-of-contact for a warm outreach instead of a
    cold submission): best-effort model extraction, mapped into
    JDInput.warm_contact. All 4 sub-fields are optional and commonly
    absent — empty strings from the model are normalized to None here
    so downstream code can do a simple truthiness check rather than
    comparing against "".
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
    }