#!/usr/bin/env python3
"""
prose.py — job-title simplification for sentence context, and the
shared "is this cover-letter edit about the company/role rather than
the candidate" detector used by both stage 6 (fact-check) and stage 7
(utility critique). Split out of pipeline_common.py.
"""
from __future__ import annotations
import re

from text_matching import _normalize_quotes

# ---------- Job title simplification for prose ----------
#
# A job title with a trailing qualifier -- comma ("Staff Software
# Engineer, Backend (Search)"), parenthetical ("Senior Software Engineer
# (Cloud Native, React/Node.js)"), or spaced dash ("Full Stack Staff
# Software Engineer - Logistics, Operations & Tooling") -- reads as a
# run-on when repeated verbatim mid-sentence ("applying for the Staff
# Software Engineer, Backend (Search) role at Affirm"). Real, not
# hypothetical: 8 of the applications in this project's own
# applications/ folder as of 2026-08 have a title matching one of these
# shapes. Requires WHITESPACE on both sides of a dash specifically so a
# hyphenated compound word ("Full-Stack Engineer") isn't mistaken for a
# qualifier separator.
_TITLE_QUALIFIER_SEPARATOR = re.compile(r"\s*,\s*|\s+[-–—]\s+|\s*\(")

# "Sr" / "Sr." as posted -- real, not hypothetical: 4 of this project's
# own live JDs use one or the other. Expanded to "Senior" rather than
# just given a period ("Sr.") because the candidate's own resume never
# abbreviates it (always "Senior Full-Stack Engineer", "Senior Associate
# Software Engineer", etc.) -- prose that's about to sit next to that
# voice should match it, not read like a copied job-board label. The
# verbatim Re: line still shows the exact posted "Sr"/"Sr." unaffected,
# same reasoning as the qualifier trim above.
_SR_ABBREVIATION = re.compile(r"\bSr\.?(?=\s|$)", re.IGNORECASE)

# "SSE"/"SE" as posted job-title acronyms ("Senior Software Engineer" /
# "Software Engineer"). Deliberately case-SENSITIVE (unlike "Sr" above)
# -- these are only meaningful as title-case abbreviations; a
# case-insensitive match would risk catching a stray lowercase "se" or
# "sse" that isn't this acronym at all, and job postings that actually
# use this abbreviation always post it in caps. \b on both sides means
# this can't fire mid-word (won't touch "USE" or "BASE").
_SSE_SE_ABBREVIATION = re.compile(r"\b(SSE|SE)\b")
_ABBREVIATION_EXPANSIONS = {"SSE": "Senior Software Engineer", "SE": "Software Engineer"}


def simplify_role_title_for_prose(role_title: str) -> str:
    """Trims a job title down to its base role, for use INSIDE a
    sentence (the cover letter's mandatory opening line, a suggested
    outreach message) -- NOT for header/subject-style lines ("Re:
    <title> at <company>", a follow-up doc's title line), which should
    stay verbatim: those read fine punctuation and all, and exact
    wording there helps a recruiter route the application to the right
    req. Returns everything before the FIRST qualifier separator,
    trimmed -- still accurate (you genuinely are applying for a "Staff
    Software Engineer" role; the qualifier is a specialization detail,
    not a different job), just without the clause a sentence can't
    carry gracefully. A title with no qualifier separator is returned
    unchanged (stripped). Also expands "Sr"/"Sr." to "Senior" and
    "SSE"/"SE" to their full titles -- see _SR_ABBREVIATION and
    _SSE_SE_ABBREVIATION above."""
    expanded = _SR_ABBREVIATION.sub("Senior", role_title)
    expanded = _SSE_SE_ABBREVIATION.sub(lambda m: _ABBREVIATION_EXPANSIONS[m.group(0)], expanded)
    m = _TITLE_QUALIFIER_SEPARATOR.search(expanded)
    return expanded[:m.start()].strip() if m else expanded.strip()


# ---------- Cover-letter company/role-specific edit detection ----------
#
# Shared by stage 6 (fact-check) and stage 7 (utility critique) so the
# two can't drift apart on what counts as "company-specific, not a
# candidate claim." An edit that fills in <COMPANY_NAME>/<ROLE_NAME>,
# or rewrites the "What draws me to..." / sign-off sentences, is
# inherently ABOUT the company or role, not about the candidate's own
# background -- fact-checking it against the claims ledger (which only
# has candidate facts) will always incorrectly flag it, and stage 7
# shouldn't be free to cut it either, since these are mandatory per
# stage 5's own prompt, not optional suggestions.
COMPANY_SPECIFIC_EDIT_PATTERNS = [
    "<COMPANY_NAME>",
    "<ROLE_NAME>",
    "What draws me to",       # the mandatory company-specific paragraph
    "I'd welcome the chance", # the mandatory closing sentence
]


def _mandatory_cover_letter_spans(baseline_text: str) -> tuple[str, str]:
    """Splits the baseline cover letter into paragraphs and returns
    (company_paragraph, closing_sentence) -- the two spans stage 5's
    own prompt (rules 9/10) requires a mandatory reword for, pulled
    directly from the baseline itself rather than guessed at, so a
    match against these is ground truth, not another heuristic. Empty
    string for either span not found (e.g. a hand-edited baseline that
    dropped one of them) -- callers already have other signals to fall
    back on, this isn't the only check."""
    paragraphs = [p.strip() for p in baseline_text.split("\n\n") if p.strip()]
    company_para = next((p for p in paragraphs if p.startswith("What draws me to")), "")
    closing = next((p for p in paragraphs if p.startswith("I'd welcome the chance")), "")
    return company_para, closing


def is_company_specific_edit(edit: dict, cover_letter_baseline_text: str = "") -> bool:
    """True if this cover-letter edit's original_text is about the
    company/role rather than the candidate's own background -- see
    COMPANY_SPECIFIC_EDIT_PATTERNS above.

    Checks THREE independent signals, not just one: original_text
    containing a known pattern (exact copy of the whole paragraph),
    the edit's own section label naming it as the company/closing
    paragraph, OR (if cover_letter_baseline_text is given) original_text
    being a substring of the ACTUAL mandatory paragraph/sentence pulled
    live from the baseline file. That third signal is the one that
    actually caught the real failure mode: stage 5's prompt (rule 9)
    requires original_text to be the FULL mandatory paragraph copied
    verbatim, but a real run instead captured only its closing clause
    ("That's the kind of system I want to be building.") as
    original_text, under a section label ("What draws me to
    <COMPANY_NAME>") that didn't contain "why" either -- so neither of
    the first two signals fired, stage 7's utility critique judged it
    as generic filler and cut it, and the actual boilerplate sentence
    it should have protected was never even proposed for a reword.
    Substring-against-the-live-baseline catches this regardless of
    what fragment the model captured or how it happened to label the
    section, since the baseline paragraph is fixed, known text, not
    something to pattern-match around.

    Relying on any ONE signal alone was already known-fragile -- a real
    batch of 14 applications showed a 36% failure rate on exactly this
    exemption before the section-label fallback was added, and the
    labeling case above shows the label fallback alone isn't enough
    either. Passing cover_letter_baseline_text is optional (existing
    callers that don't have it yet still get signals 1 and 2), but
    strongly recommended -- it's the only signal immune to both
    paraphrasing AND labeling drift at once."""
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
    """Pulls likely skill/tool names out of the JD's REQUIRED
    requirement phrases -- capitalized tokens (proper nouns, acronyms,
    tech names like 'TypeScript', 'AWS', 'Kubernetes', 'Node') are a
    reasonable, if imperfect, heuristic for what a recruiter would
    actually type into an ATS keyword search. Deliberately restricted
    to priority=required (not preferred) -- these are the terms most
    likely to be used as a hard keyword filter, not just a nice-to-have
    scan. Shared by stage 4 (which classifies these into actionable vs.
    unsupported, as early in the pipeline as possible) and
    py_post_pipeline_verify_materials.py (which checks literal presence
    in the final assembled resume) -- kept in one place so the two
    can't drift apart on what counts as a "required keyword."
    """
    keywords = set()
    for req in edit_brief.get("requirements", []):
        if req.get("priority") != "required":
            continue
        for token in re.findall(r"\b[A-Z][a-zA-Z0-9+.#]*\b", req.get("text", "")):
            if token not in KEYWORD_STOPWORDS and len(token) > 1:
                keywords.add(token)
    return sorted(keywords)


def classify_keyword_gaps(keywords: list[str], claims: list) -> tuple[dict[str, list[str]], list[str]]:
    """Splits required JD keywords into two genuinely different cases,
    checked deterministically (no LLM call) against the claims ledger
    -- as early in the pipeline as this can happen, right where stage 4
    already has both the JD requirements and the claims ledger loaded
    together:

      - ACTIONABLE: the keyword already appears somewhere in a real
        claim's text, but may not have been surfaced into an actual
        edit yet. This is a retrieval gap, not a truth gap -- safe to
        hand to stage 5 as a targeted instruction, since the fact is
        already real and verified-by-construction (it's sitting in the
        ledger, which only ever contains verbatim resume text).
      - UNSUPPORTED: no claim mentions the keyword at all. This is NOT
        something to feed back into generation -- there is nothing
        genuine to build an edit from, and asking a model to "find a
        way to include it anyway" is exactly the kind of embellishment
        risk the whole verification/critique architecture exists to
        prevent. These are surfaced to the candidate directly (stage 9
        appends them to the follow-up-steps file) as a real, honest
        signal -- either you have real undocumented experience worth
        adding to your actual resume, or the JD is asking for
        something you genuinely don't have. Both are useful to know;
        neither is the generation pipeline's decision to make.

    Returns (actionable: {keyword: [claim_id, ...]}, unsupported: [keyword, ...]).
    """
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
    "Core Technology team",  # real incident: a leftover claim about a
                              # team at a PREVIOUS employer, nonsensically
                              # applied to whatever company was being
                              # applied to. Fixed in the baseline sample
                              # twice now -- this check exists specifically
                              # because it silently came back once already.
    "<COMPANY_NAME>", "<ROLE_NAME>", "<COMPANY>", "<ROLE_TITLE>",
    "{{", "}}",  # any unresolved template placeholder syntax at all
]
