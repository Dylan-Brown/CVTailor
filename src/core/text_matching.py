"""
text_matching.py — quote normalization and fuzzy text-span lookup,
split out of pipeline_common.py. Used by docx_utils.py and prose.py
(is_company_specific_edit) so both can locate edit text against a
baseline document without duplicating the same matching logic.
"""
from __future__ import annotations
import re

_QUOTE_NORMALIZE_MAP = str.maketrans({
    "‘": "'", "’": "'",  # left/right single smart quotes -> straight
    "“": '"', "”": '"',  # left/right double smart quotes -> straight
})


def _normalize_quotes(s: str) -> str:
    return s.translate(_QUOTE_NORMALIZE_MAP)


def find_edit_match(body: str, original: str) -> tuple[int, int] | None:
    """Finds `original` inside `body`, trying progressively more
    permissive strategies, and returns (start, end) character indices
    into the REAL, unmodified body -- never a separately-normalized
    copy, so callers can always slice/replace directly against what
    they were given. Returns None if nothing matches at any level.

    Two real, confirmed failure modes motivated the extra strategies,
    found by diffing an app's actual candidate_edits.json against the
    real baseline file:
      - a genuine double-space typo sitting in the baseline text
        ("defining  contract test cases") that the model silently
        normalized to a single space when copying it into
        original_text -- an exact match against the real (still
        double-spaced) baseline then fails on that one character.
      - the model prepending a bullet marker ("* ") to a paragraph
        that was never actually a bulleted list item in the baseline
        (only the "quantified highlights" section uses real bullets;
        the model over-generalized that pattern onto the "why this
        company" and closing paragraphs too).

    Strategies are tried in order, exact first, so a real exact match
    is always preferred over a looser one -- this never makes matching
    MORE permissive than necessary for a given edit.
    """
    candidates = [original]
    stripped = original.strip()
    for marker in ("* ", "*\t", "• ", "•\t"):
        if stripped.startswith(marker):
            candidates.append(stripped[len(marker):])

    for candidate in candidates:
        if not candidate:
            continue

        # 1. Exact match.
        idx = body.find(candidate)
        if idx != -1:
            return idx, idx + len(candidate)

        # 2. Quote-normalized match (curly vs. straight quotes).
        norm_body = _normalize_quotes(body)
        norm_cand = _normalize_quotes(candidate)
        idx = norm_body.find(norm_cand)
        if idx != -1:
            return idx, idx + len(candidate)

        # 3. Whitespace-tolerant match -- any run of whitespace in the
        # candidate matches any run of whitespace in the body,
        # catching double-space typos in either direction without
        # needing to know which side has the extra space. A real
        # regex search (not a separately-normalized copy), so start()/
        # end() are already correct indices into the real body.
        pattern = re.compile(r"\s+".join(re.escape(part) for part in candidate.split()))
        m = pattern.search(body)
        if m:
            return m.start(), m.end()

    return None
