#!/usr/bin/env python3
"""
prose.py -- job-title simplification for sentence context, and the
shared company/role-vs-candidate edit detector used by stage 6 and
stage 7. Split out of pipeline_common.py.
"""
from __future__ import annotations
import re

from text_matching import _normalize_quotes

# ---------- Job title simplification for prose ----------
# Trims a trailing qualifier (comma/parenthetical/spaced-dash) that
# reads as a run-on mid-sentence; dash requires surrounding whitespace.
_TITLE_QUALIFIER_SEPARATOR = re.compile(r"\s*,\s*|\s+[-–—]\s+|\s*\(")

# "Sr"/"Sr." expands to "Senior" to match the resume's own voice,
# which never abbreviates it. The verbatim Re: line is unaffected.
_SR_ABBREVIATION = re.compile(r"\bSr\.?(?=\s|$)", re.IGNORECASE)

# "SSE"/"SE" job-title acronyms, expanded to their full titles.
# Case-sensitive on purpose -- avoids catching "USE" or lowercase text.
_SSE_SE_ABBREVIATION = re.compile(r"\b(SSE|SE)\b")
_ABBREVIATION_EXPANSIONS = {"SSE": "Senior Software Engineer", "SE": "Software Engineer"}


def simplify_role_title_for_prose(role_title: str) -> str:
    """Trims a job title to its base role for use INSIDE a sentence
    (not for header/subject-style lines like "Re: <title>", which stay
    verbatim). Also expands "Sr"/"SSE"/"SE" abbreviations."""
    expanded = _SR_ABBREVIATION.sub("Senior", role_title)
    expanded = _SSE_SE_ABBREVIATION.sub(lambda m: _ABBREVIATION_EXPANSIONS[m.group(0)], expanded)
    m = _TITLE_QUALIFIER_SEPARATOR.search(expanded)
    return expanded[:m.start()].strip() if m else expanded.strip()


# ---------- Cover-letter company/role-specific edit detection ----------
# Shared by stage 6 and stage 7 so both agree on what counts as
# company/role-specific text rather than a candidate claim.
COMPANY_SPECIFIC_EDIT_PATTERNS = [
    "<COMPANY_NAME>",
    "<ROLE_NAME>",
    "What draws me to",       # the mandatory company-specific paragraph
    "I'd welcome the chance", # the mandatory closing sentence
]


def _mandatory_cover_letter_spans(baseline_text: str) -> tuple[str, str]:
    """Splits the baseline cover letter into (company_paragraph,
    closing_sentence) -- the two spans stage 5 requires a mandatory
    reword for. Empty string for either span not found."""
    paragraphs = [p.strip() for p in baseline_text.split("\n\n") if p.strip()]
    company_para = next((p for p in paragraphs if p.startswith("What draws me to")), "")
    closing = next((p for p in paragraphs if p.startswith("I'd welcome the chance")), "")
    return company_para, closing


def is_company_specific_edit(edit: dict, cover_letter_baseline_text: str = "") -> bool:
    """True if this cover-letter edit is about the company/role rather
    than the candidate's own background. Checks pattern match, section
    label, and (if given) substring match against the live baseline."""
    if edit.get("target") != "cover_letter":
        return False
    original = (edit.get("original_text") or "").strip()
    if any(pat in original for pat in COMPANY_SPECIFIC_EDIT_PATTERNS):
        return True
    section = (edit.get("section") or "").lower().replace("_", " ").replace("-", " ")
    if "why" in section and ("company" in section or "this" in section):
        return True
    if "draws me" in section:
        return True
    if section.strip() in ("closing", "closing paragraph", "closing sentence", "sign off", "sign-off", "signoff"):
        return True
    if cover_letter_baseline_text and original:
        company_para, closing = _mandatory_cover_letter_spans(cover_letter_baseline_text)
        normalized = _normalize_quotes(original)
        if company_para and normalized in _normalize_quotes(company_para):
            return True
        if closing and normalized in _normalize_quotes(closing):
            return True
    return False


# Words that capitalize but aren't actual skill/tool names -- filtered
# out of the requirement-keyword extraction so a sentence-leading "The"
# or a pronoun doesn't get treated as a required technical term.
KEYWORD_STOPWORDS = {
    "The", "This", "That", "These", "Those", "You", "Your", "We", "Our",
    "I", "A", "An", "In", "On", "For", "With", "And", "Or", "To", "Of",
    "Experience", "Years", "Strong", "Deep", "Track", "Record",
}


def extract_required_keywords(edit_brief: dict) -> list[str]:
    """Pulls likely skill/tool names out of required JD requirement
    phrases via a capitalized-token heuristic. Shared by stage 4 and
    py_post_pipeline_verify_materials.py so both agree on the definition."""
    keywords = set()
    for req in edit_brief.get("requirements", []):
        if req.get("priority") != "required":
            continue
        for token in re.findall(r"\b[A-Z][a-zA-Z0-9+.#]*\b", req.get("text", "")):
            if token not in KEYWORD_STOPWORDS and len(token) > 1:
                keywords.add(token)
    return sorted(keywords)


def classify_keyword_gaps(keywords: list[str], claims: list) -> tuple[dict[str, list[str]], list[str]]:
    """Splits required JD keywords into ACTIONABLE (already in a claim,
    just not yet surfaced into an edit) vs UNSUPPORTED (no matching
    claim -- surfaced to the candidate, never fed back into generation)."""
    actionable: dict[str, list[str]] = {}
    unsupported: list[str] = []
    for kw in keywords:
        kw_lower = kw.lower()
        matching_claims = [c.claim_id for c in claims if kw_lower in c.text.lower()]
        if matching_claims:
            actionable[kw] = matching_claims
        else:
            unsupported.append(kw)
    return actionable, unsupported


KNOWN_BAD_PHRASES = [
    "Core Technology team",  # leftover claim from a previous employer,
                              # applied nonsensically to any company
    "<COMPANY_NAME>", "<ROLE_NAME>", "<COMPANY>", "<ROLE_TITLE>",
    "{{", "}}",  # unresolved template placeholder syntax
]
