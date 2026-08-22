#!/usr/bin/env python3
"""
py_stage08_agentic_update_review.py — the hard gate. Shows a diff for every edit that
passed (or was flagged by) stage 6, one at a time, and you decide.
Fabricated edits never make it here — they were already discarded.

Interactive by design. This is the one stage that isn't meant to run
unattended.

Usage:
    # explicit paths
    python py_stage08_agentic_update_review.py \
        --verified-edits applications/.../verified_edits.json \
        --out-dir applications/...

    # or just point at the app id (a unique prefix of it works too) --
    # this also switches on revisit mode: every edit is shown, even
    # ones you already approved/rejected/edited, so you can change
    # your mind on a past decision
    python py_stage08_agentic_update_review.py --app-id <jd_name_or_prefix>

    # or nothing at all — reviews every app that still has at least one
    # undecided edit, one app after another. Edits already decided in a
    # prior session are always skipped here (use --app-id or --force
    # to revisit)
    python py_stage08_agentic_update_review.py

    # --force re-reviews already-decided edits instead of skipping them.
    # Alone, it revisits EVERY app with an edits file (not just apps
    # with something new pending); combined with --app-id it's the same
    # revisit --app-id already does; combined with explicit
    # --verified-edits/--out-dir it's the only way to revisit those,
    # since that path otherwise never shows an already-decided edit.
    python py_stage08_agentic_update_review.py --force
"""
import argparse
import difflib
import json
import re
import sys
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/stages/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
from batch_common import APPLICATIONS_ROOT, CONFIG_ROOT, NON_APPLICATION_DIR_NAMES
from io_utils import log_event
from prose import KNOWN_BAD_PHRASES
from schemas import ReviewDecision

# Force UTF-8 encoding for Windows terminals to prevent UnicodeEncodeError
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')


# A few common tech terms/acronyms a general-English dictionary won't know —
# kept short and additive to the claims-ledger vocabulary below, not a
# substitute for it.
_TECH_ALLOWLIST = {
    "aws", "iam", "api", "apis", "sql", "nosql", "tdd", "cicd", "ci", "cd",
    "json", "yaml", "backend", "frontend", "onboarded", "onboarding",
}


def _load_requirement_priorities(out_dir: Path) -> dict[str, str]:
    """Reads edit_brief.json (stage 4's output, if present) and returns
    {requirement_text_lower: priority}, where priority is "required" or
    "preferred" -- stage 4 already classified every JD requirement this
    way. Returns {} if edit_brief.json isn't there (ranking then falls
    back to edit_type alone, still deterministic, just less informed)."""
    brief_path = out_dir / "edit_brief.json"
    if not brief_path.exists():
        return {}
    try:
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        return {r["text"].lower(): r["priority"] for r in brief.get("requirements", [])}
    except Exception:
        return {}


# Impact proxy by edit type: a new_sentence fills an actual coverage gap
# (highest-leverage kind of edit); reword is pure polish (lowest --
# exactly the "over-optimizing" you don't want to spend review time on).
_EDIT_TYPE_IMPACT = {
    "new_sentence": 3,
    "reweight": 2,
    "reorder": 1,
    "reword": 0,
}


def score_edit(edit: dict, requirement_priorities: dict[str, str]) -> int:
    """Deterministic impact score, no LLM call -- higher means more worth
    your limited review time. Base score is edit_type (a real proxy for
    how substantive the change is, not just wording). Bonus if the
    edit's text overlaps a JD requirement stage 4 already flagged as
    "required" (+2) or "preferred" (+1) -- this is a text-overlap
    heuristic, not a true link (CandidateEdit doesn't carry a reference
    to which specific gap it addresses), so treat ties/near-ties as
    equivalent rather than trusting single-point differences."""
    score = _EDIT_TYPE_IMPACT.get(edit.get("edit_type"), 0)

    suggested = (edit.get("suggested_text") or "").lower()
    suggested_words = set(suggested.split())
    for req_text, priority in requirement_priorities.items():
        req_words = {w for w in req_text.split() if len(w) > 3}  # skip short/common words
        if req_words and len(req_words & suggested_words) >= 2:
            score += 2 if priority == "required" else 1
            break  # one match is enough -- don't stack bonuses across multiple requirements

    return score


def edits_file_for(app_dir: Path) -> Path:
    """critiqued_edits.json (stage 7's output) when it exists, else
    verified_edits.json (stage 6's). Stage 7 is an optional gate -- an
    app prepared before it existed, or a run that deliberately skipped
    it, still reviews fine straight off stage 6's output."""
    critiqued = app_dir / "critiqued_edits.json"
    return critiqued if critiqued.is_file() else app_dir / "verified_edits.json"


def find_matches_by_prefix(prefix: str) -> list[Path]:
    """Case-insensitive startswith match against live applications/
    folders -- same convention py_post_pipeline_store_applied.py and py_pipeline_assemble.py
    already use for their own prefix matching, reused here so --app-id
    behaves consistently across the project instead of introducing a
    second matching rule."""
    if not APPLICATIONS_ROOT.is_dir():
        return []
    prefix_lower = prefix.lower()
    return sorted(
        d for d in APPLICATIONS_ROOT.iterdir()
        if d.is_dir()
        and d.name not in NON_APPLICATION_DIR_NAMES
        and d.name.lower().startswith(prefix_lower)
    )


def resolve_app_dir(app_id: str) -> Path:
    """Resolves --app-id by exact match first (so a real folder name
    always wins even on the off chance it's also a prefix of another),
    then falls back to a case-insensitive PREFIX match against folders
    directly under applications/ -- lets you type a short leading
    fragment instead of the full company_role_date app_id. A prefix
    matching zero or more than one folder is a hard error, not a
    guess -- picking the wrong application silently would be worse
    than making you retype it."""
    exact = APPLICATIONS_ROOT / app_id
    if exact.is_dir():
        return exact

    matches = find_matches_by_prefix(app_id)
    if not matches:
        print(f"No applications/{app_id}* folder found under {APPLICATIONS_ROOT}/.", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"{app_id!r} matches {len(matches)} folders -- be more specific:", file=sys.stderr)
        for m in matches:
            print(f"  {m.name}", file=sys.stderr)
        sys.exit(1)
    return matches[0]


def _decided_edit_ids(app_dir: Path) -> set[str]:
    """edit_ids that already have a recorded decision for this app, or
    the empty set if review_decisions.json doesn't exist / is corrupt.
    Used to tell "already reviewed" apart from "not reviewed yet" at
    the individual-edit level rather than the whole-file level -- an
    app can have SOME edits decided (e.g. a prior session was quit
    early, or --max-edits truncated it) and still have real work left."""
    decisions_path = app_dir / "review_decisions.json"
    if not decisions_path.is_file():
        return set()
    try:
        data = json.loads(decisions_path.read_text(encoding="utf-8"))
        return {d["edit_id"] for d in data}
    except Exception:
        return set()


def find_pending_reviews() -> list[Path]:
    """Application dirs with REAL (non-dry-run) edits that still have
    at least one undecided edit -- the same "verified" state
    py_pipeline_status.py reports, but at edit granularity rather than
    whole-file: an app whose review_decisions.json only covers SOME of
    its edits (quit early, or a prior --max-edits truncation) is still
    pending, not done. Reads whichever of critiqued_edits.json /
    verified_edits.json applies (see edits_file_for). Explicitly
    excludes dry-run placeholder files (stages 5 and 5b both write real
    files to those paths in --dry-run so downstream wiring can be
    smoke-tested without spending anything -- see their own comments)
    -- otherwise auto-discover would surface placeholder/unverified
    content as if it were ready for review, right after someone ran a
    dry-run sanity check before their real batch.

    Deliberately does NOT surface edits already decided -- that's what
    --app-id is for (see run()'s revisit parameter). Auto-discover
    should only ever hand you fresh, undecided work."""
    if not APPLICATIONS_ROOT.is_dir():
        return []
    candidates = sorted(
        p for p in APPLICATIONS_ROOT.iterdir()
        if p.is_dir() and p.name not in NON_APPLICATION_DIR_NAMES
        and edits_file_for(p).is_file()
    )
    result = []
    for p in candidates:
        try:
            data = json.loads(edits_file_for(p).read_text(encoding="utf-8"))
            if data.get("dry_run", False):
                continue
            all_ids = {e["edit_id"] for e in data.get("passed", []) + data.get("flagged", [])}
        except Exception:
            result.append(p)  # unreadable/corrupt -- surface it for review rather than silently hide it
            continue
        if all_ids - _decided_edit_ids(p):
            result.append(p)
    return result


def find_all_apps_with_edits() -> list[Path]:
    """Same candidate set as find_pending_reviews() -- every live
    applications/<id>/ folder with a real (non-dry-run) edits file --
    but WITHOUT filtering down to only apps that still have an
    undecided edit. Used by --force's whole-batch mode: with no
    --app-id given, --force means "revisit every app's decisions,"
    not just apps that still have something new to decide."""
    if not APPLICATIONS_ROOT.is_dir():
        return []
    candidates = sorted(
        p for p in APPLICATIONS_ROOT.iterdir()
        if p.is_dir() and p.name not in NON_APPLICATION_DIR_NAMES
        and edits_file_for(p).is_file()
    )
    result = []
    for p in candidates:
        try:
            data = json.loads(edits_file_for(p).read_text(encoding="utf-8"))
            if data.get("dry_run", False):
                continue
        except Exception:
            result.append(p)  # unreadable/corrupt -- surface it rather than silently hide it
            continue
        result.append(p)
    return result


def _load_known_words() -> set[str]:
    """Pulls every word out of the shared claims ledger(s) so real names/terms
    that already appear in your actual resume don't get flagged as typos —
    only genuinely new, dictionary-unknown words in freshly typed text do.
    Scans both the current per-variant ledgers (applications/_shared/
    variants/*/claims_ledger.json) and the legacy flat single-ledger path
    (applications/_shared/claims_ledger.json, from before variant routing
    existed) -- whichever are actually present, merged together."""
    words = set()
    ledger_paths = list((APPLICATIONS_ROOT / "_shared" / "variants").glob("*/claims_ledger.json"))
    legacy_ledger = APPLICATIONS_ROOT / "_shared" / "claims_ledger.json"
    if legacy_ledger.is_file():
        ledger_paths.append(legacy_ledger)
    for ledger_path in ledger_paths:
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
            for claim in data.get("claims", []):
                words.update(w.lower() for w in re.findall(r"[A-Za-z']+", claim.get("text", "")))
        except Exception:
            pass
    return words


def _check_for_typos(text: str) -> list[str]:
    """Returns likely-misspelled words in `text`, or [] if pyspellchecker
    isn't installed / nothing looks off. This exists because hand-edited
    text skips every other check in this pipeline — stage 6 never sees it,
    it goes straight into the final document."""
    try:
        from spellchecker import SpellChecker
    except ImportError:
        return []  # optional dependency — degrade silently, don't block review over it

    spell = SpellChecker()
    known = _load_known_words() | _TECH_ALLOWLIST
    words = [w.strip(".,;:()\"'").lower() for w in text.split()]
    words = [w for w in words if w and w.isalpha()]
    return [w for w in words if w not in known and w not in spell]


def _confirm_edited_text(text: str) -> str:
    """Echoes hand-typed text back and ALWAYS requires an explicit
    confirmation before accepting it — this is the one path in the
    whole pipeline nothing else checks; stage 6's fact-check never
    sees text you type here, so there is no other safety net.

    Real incident that made this unconditional: a KeyboardInterrupt
    mid-edit left stale text sitting in the terminal's stdin buffer:
    the NEXT script invocation's input() calls silently consumed that
    leftover buffered text instead of live keystrokes, and a LATER,
    unrelated edit ended up committed with a DIFFERENT edit's text --
    perfectly well-formed prose, zero typos, so the old typo-gated
    confirmation never fired and it was only caught by chance (the
    wrong text happened to also fail stage 9's verbatim-match check).
    A stdin mixup produces coherent, typo-free text almost by
    definition, so confirmation can never be conditional on finding a
    typo -- that's exactly the case it needs to catch."""
    while True:
        typos = _check_for_typos(text)
        print(f"  You entered: {text}")
        if typos:
            print(f"  Possible typo(s): {', '.join(typos)}")
        confirm = input("  Use this text? (y)es / (r)e-type: ").strip().lower()
        if confirm in ("y", "yes"):
            return text
        text = input("  New text: ").strip()


_GENERIC_CAPITALIZED_WORDS = {
    "What", "Why", "How", "The", "This", "That", "These", "Those",
    "I", "My", "Your", "Our", "We", "You", "It", "Its",
}


def _extract_distinctive_tokens(text: str) -> set[str]:
    """Numbers and capitalized proper-noun-ish tokens -- the same kind
    of specific, checkable content that (if repeated) usually signals
    real duplication rather than coincidental word overlap. Same idea
    as py_post_pipeline_verify_materials.py's post-assembly repeated-dollar-figure check,
    generalized slightly and moved earlier in the pipeline -- catching
    this at review time, not just after the document is already built.

    Generic sentence-starting capitals ("What draws me to...", "Why
    I...") are filtered out -- every "why this company" edit legitimately
    starts the same way, and without this filter that shared boilerplate
    alone triggered a false duplicate flag between two edits that
    actually made two different points, which masked a real
    contradiction signal on one of them in testing."""
    tokens = set()
    tokens.update(re.findall(r"\$[\d,.]+\s?[MmBbKk]?", text))
    tokens.update(re.findall(r"\b\d{2,}\+?%?\b", text))
    tokens.update(re.findall(r"\b[A-Z][a-zA-Z0-9+.#]{2,}\b", text))
    return tokens - _GENERIC_CAPITALIZED_WORDS


def _find_duplicate_signal(edit: dict, other_edits: list[dict], baseline_text: str,
                            company_name: str = "") -> str | None:
    """Deterministic, no LLM call -- checks this edit's suggested_text
    against every OTHER edit of the SAME document type in the same
    review batch, and (for new cover-letter content only, not reword-
    in-place edits, which are SUPPOSED to overlap with their own
    original bullet) against the untouched baseline document. Returns
    a plain-English warning naming exactly what's shared and where, or
    None if nothing distinctive overlaps.

    The company's own name is excluded from comparison -- it
    legitimately appears in nearly every company-specific edit
    (every "why this company" sentence says the company's name at
    least once), so without this exclusion it registered as "shared
    duplicate content" between two edits that actually made two
    completely different points, which masked a real contradiction
    signal on one of them in testing."""
    my_tokens = _extract_distinctive_tokens(edit["suggested_text"])
    if company_name:
        my_tokens -= _extract_distinctive_tokens(company_name)
    if not my_tokens:
        return None

    for other in other_edits:
        if other["edit_id"] == edit["edit_id"]:
            continue
        if other.get("target") != edit.get("target"):
            continue  # resume mentioning a skill AND the cover letter also
                       # mentioning it is normal and expected -- only compare
                       # within the same document, where repeating a point
                       # actually costs you space and reads as padding
        overlap = my_tokens & _extract_distinctive_tokens(other["suggested_text"])
        if overlap:
            return (f"shares {', '.join(sorted(overlap))} with [{other['edit_id']}] "
                    f"({other['section']}), also in this batch — likely duplicating "
                    f"a point that edit already makes")

    is_new_cover_letter_content = edit.get("target") == "cover_letter" and not edit.get("original_text")
    if is_new_cover_letter_content and baseline_text:
        overlap = my_tokens & _extract_distinctive_tokens(baseline_text)
        if overlap:
            return (f"shares {', '.join(sorted(overlap))} with content already in the "
                    f"existing cover letter — likely restating something already said")
    return None


_WORK_MODE_TERMS = {"remote", "hybrid", "onsite", "on-site", "in-office"}


def _company_brief_text(app_dir: Path) -> tuple[str, str]:
    """Flattens company_brief.json's actual findings into one search
    blob -- stack_signals plus every finding's claim text, across all
    three finding categories -- and returns the company's own name
    alongside it, since callers need both. Returns ("", "") if stage 3
    never ran or found nothing (missing file, empty brief) -- callers
    should treat that as "nothing to check against," not an error."""
    brief_path = app_dir / "company_brief.json"
    if not brief_path.is_file():
        return "", ""
    try:
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    parts = list(brief.get("stack_signals", []))
    for key in ("culture_notes", "recent_news", "compensation_notes"):
        parts += [f.get("claim", "") for f in brief.get(key, [])]
    return " ".join(parts), brief.get("company", "")


def _find_company_contradiction(edit: dict, company_brief_text: str) -> str | None:
    """Only checked for cover-letter edits that are actually ABOUT the
    company (why-this-company / culture / context sections, by name --
    a resume reword doesn't make company-specific claims, so it's
    never relevant there). Flags specific, named claims about the
    company -- a work-mode term, or a capitalized tech-sounding token
    -- that don't appear anywhere in what stage 3's own research
    actually found. Deterministic, no new LLM call, same class of risk
    as claims-ledger fabrication checking, just pointed at the
    company's facts instead of the candidate's own.

    Real heuristic limits, same caveat as the keyword/duplicate checks:
    generic capitalized words can false-positive (a company_brief that
    happens not to mention "Kubernetes" by name doesn't prove an edit
    mentioning it is WRONG, only that our own research didn't confirm
    it) -- treat this as "worth a second look," not "definitely wrong."
    """
    if edit.get("target") != "cover_letter" or not company_brief_text:
        return None
    section = (edit.get("section") or "").lower()
    if not any(kw in section for kw in ("why", "company", "culture")):
        return None

    text = edit["suggested_text"]
    text_lower = text.lower()
    brief_lower = company_brief_text.lower()

    unsupported = []
    claimed_modes = _WORK_MODE_TERMS & set(re.findall(r"[a-z-]+", text_lower))
    unsupported += [m for m in claimed_modes if m not in brief_lower]

    tech_tokens = {t for t in _extract_distinctive_tokens(text) if not t[0].isdigit() and "$" not in t}
    unsupported += [t for t in tech_tokens if t not in company_brief_text]

    if unsupported:
        return (f"claims {', '.join(sorted(set(unsupported)))} about the company -- not found "
                f"anywhere in stage 3's research (may be right, just unconfirmed by what we found)")
    return None


def _find_unsupported_new_term(edit: dict, claims_by_id: dict[str, str]) -> str | None:
    """A hard, mechanical backstop underneath stage 6's own LLM
    judgment -- not a replacement for it. Stage 6 makes a holistic call
    on whether an edit sounds true; this checks something narrower and
    checkable: for a resume reword that ADDS a specific technical term
    not present in the original bullet, does the text of at least one
    of the claims it actually cites contain that term? A real gap this
    closes: an edit can cite a real, valid claim_id while still
    introducing a term that specific claim never mentions -- subtle
    scope creep that a holistic "does this sound plausible" judgment
    can miss, since the cited claim IS real, just not actually
    supporting the NEW part.

    Only checked for resume rewords with a non-empty original_text to
    diff against (cover-letter "new sentence" edits have no original
    to compare against, so there's nothing to call "newly introduced"
    versus what was already there) and only when claim_ids_referenced
    is non-empty (an edit with zero claims cited is already stage 6's
    problem, not this check's)."""
    if edit.get("target") != "resume" or edit.get("edit_type") != "reword":
        return None
    original = edit.get("original_text")
    claim_ids = edit.get("claim_ids_referenced") or []
    if not original or not claim_ids:
        return None

    new_terms = _extract_distinctive_tokens(edit["suggested_text"]) - _extract_distinctive_tokens(original)
    if not new_terms:
        return None

    cited_claims_text = " ".join(claims_by_id.get(cid, "") for cid in claim_ids)
    unsupported = [t for t in new_terms if t not in cited_claims_text]
    if unsupported:
        return (f"adds {', '.join(sorted(unsupported))}, not found in the text of the "
                f"claim(s) this edit cites ({', '.join(claim_ids)}) -- worth confirming "
                f"that claim actually supports this specific addition")
    return None


def _find_placeholder_leak(edit: dict) -> str | None:
    """Currently only checked by py_post_pipeline_verify_materials.py, AFTER assembly --
    if that check is ever skipped (forgotten, or run against the wrong
    app), a literal "<COMPANY_NAME>" or unresolved "{{...}}" ships in a
    real document. Checking the same KNOWN_BAD_PHRASES list here too,
    at review time, means it's caught before assembly even happens,
    not just before sending."""
    for phrase in KNOWN_BAD_PHRASES:
        if phrase in edit["suggested_text"]:
            return f"contains {phrase!r} verbatim -- an unresolved placeholder or leftover artifact"
    return None


def _find_degenerate_text(edit: dict) -> str | None:
    """A real sanity floor, not a hypothetical -- the same class of
    silent-corruption risk as the blank-Enter-wipes-a-bullet bug fixed
    earlier (that one was YOUR input; this is the MODEL's output, same
    danger). Empty or near-empty generated text should never be
    silently approved."""
    text = edit["suggested_text"].strip()
    if len(text) < 10:
        return f"only {len(text)} character(s) after trimming whitespace -- likely a generation failure"
    return None


def _find_length_explosion(edit: dict, max_ratio: float = 2.5) -> str | None:
    """Resume rewords are supposed to be reword-IN-PLACE (stage 5's own
    rule 2) -- roughly comparable length to what they replace, not a
    restructuring. A bullet that mechanically satisfies the verbatim-
    match requirement while ballooning several times longer than the
    original is a real rule violation worth flagging, even though the
    match itself is technically valid."""
    if edit.get("target") != "resume" or edit.get("edit_type") != "reword":
        return None
    original = edit.get("original_text")
    if not original:
        return None
    ratio = len(edit["suggested_text"]) / max(1, len(original))
    if ratio > max_ratio:
        return (f"suggested text is {ratio:.1f}x longer than the original bullet -- "
                f"reads more like a restructure than a reword-in-place")
    return None


_BARE_CONTRACTION_START = re.compile(r"^\s*['\u2019](m|d|ve|ll|re|s)\b", re.IGNORECASE)


def _find_broken_fragment(edit: dict) -> str | None:
    """The exact mechanical shape of a real incident this session --
    "'m drawn to Arcadia..." shipped with its subject missing, visible
    immediately to anyone reading the first line. A sentence starting
    with a bare contraction suffix ('m, 'd, 've, 'll, 're, 's) and no
    leading pronoun is a specific, narrow, checkable pattern -- not a
    general grammar checker, just this one recognizable failure shape."""
    text = edit["suggested_text"]
    if _BARE_CONTRACTION_START.match(text):
        return "starts with a bare contraction (e.g. \"'m\", \"'d\") -- likely missing its subject (\"I\")"
    return None


def show_edit(edit: dict, other_edits: list[dict] | None = None, baseline_text: str = "",
              company_brief_text: str = "", company_name: str = "", claims_by_id: dict[str, str] | None = None):
    target = edit.get("target")
    edit_type = edit.get("edit_type")
    original = edit.get("original_text")
    suggested = edit["suggested_text"]

    # The suggestion below costs nothing new -- it's built entirely
    # from reasoning stage 6 (fact-check) already generated and stored
    # directly on the edit dict, previously computed but never actually
    # shown here. An edit with no "verification" key cleared fact-check
    # AND stage 7's utility critique without a flag, which is a real,
    # already-earned signal, not a guess -- shown so a clean edit can
    # be confidently approved quickly instead of re-derived from
    # scratch every time. A flagged edit still gets the SAME full
    # manual a/r/e/q flow as before; this only makes the reason for
    # the flag visible instead of forcing you to guess why it was cut.
    # No numeric confidence score exists anywhere in this pipeline, and
    # this doesn't invent one -- an LLM self-rating its own confidence
    # (especially a local model) tends to be poorly calibrated, so a
    # fabricated number would look more precise than it actually is.
    # What's real: the verdict itself is already a coarse confidence
    # tier, sharpened here into three honest levels instead of a fake
    # score -- clean-with-real-claims is the strongest signal, clean-
    # with-no-claims (mostly the mandatory company-specific paragraphs,
    # legitimately exempt from claim-tracing) is a notch weaker, and
    # flagged is "read this yourself."
    placeholder_signal = _find_placeholder_leak(edit)
    degenerate_signal = _find_degenerate_text(edit)
    fragment_signal = _find_broken_fragment(edit)
    length_signal = _find_length_explosion(edit)
    duplicate_signal = _find_duplicate_signal(edit, other_edits or [], baseline_text, company_name)
    contradiction_signal = _find_company_contradiction(edit, company_brief_text)
    new_term_signal = _find_unsupported_new_term(edit, claims_by_id or {})
    if "verification" in edit:
        reason = edit["verification"].get("reasoning", "")
        print(f"  ⚠ Suggested: REVIEW CAREFULLY — flagged during fact-check: {reason}")
    elif placeholder_signal:
        print(f"  🛑 Suggested: REJECT — {placeholder_signal}")
    elif degenerate_signal:
        print(f"  🛑 Suggested: REJECT — {degenerate_signal}")
    elif fragment_signal:
        print(f"  ⚠ Suggested: REVIEW CAREFULLY — {fragment_signal}")
    elif length_signal:
        print(f"  ⚠ Suggested: REVIEW CAREFULLY — {length_signal}")
    elif duplicate_signal:
        print(f"  ⚠ Suggested: REVIEW CAREFULLY — possible duplicate: {duplicate_signal}")
    elif contradiction_signal:
        print(f"  ⚠ Suggested: REVIEW CAREFULLY — unconfirmed company claim: {contradiction_signal}")
    elif new_term_signal:
        print(f"  ⚠ Suggested: REVIEW CAREFULLY — possible unsupported addition: {new_term_signal}")
    elif edit.get("claim_ids_referenced"):
        print(f"  💡 Suggested: APPROVE (high confidence) — cleared fact-check and utility "
              f"review, and traces to a specific claim in the ledger")
    else:
        print(f"  💡 Suggested: APPROVE (moderate confidence) — cleared fact-check and "
              f"utility review with no flags, though this edit references no specific "
              f"ledger claim (expected for the mandatory company-specific paragraphs)")

    # Typo-checked and shown for EVERY edit, not just ones you end up
    # hand-editing -- _check_for_typos previously only ran on text you
    # personally typed, so AI-generated text you simply approved as-is
    # never got checked at all. Real incident: "secirty", "conbtribute",
    # and "distribute systems" (should be "distributed") all sat in a
    # real, approved, assembled cover letter -- caught only by chance on
    # a manual read after the fact, not by anything in this pipeline.
    typos = _check_for_typos(suggested)
    if typos:
        print(f"  ⚠ Possible typo(s) in the AI-generated text: {', '.join(typos)}")

    # Blank line between the "suggested action" section above and the
    # "given edit" section below -- run() adds the matching blank lines
    # for the other two section breaks (title -> suggested action, and
    # given edit -> the a/r/e/q prompt).
    print()

    if target == "cover_letter":
        print(f"  COVER LETTER SENTENCE (assembled fresh — no existing text to compare):")
        print(f"    {suggested}")
        return

    if target == "resume" and edit_type == "reword" and original:
        print(f"  REWORD (existing bullet, in place):")
        diff = difflib.unified_diff(original.split(), suggested.split(), lineterm="", n=999)
        print("    " + " ".join(list(diff)[3:]))  # skip the --- / +++ / @@ header lines
        # The diff above shows WHAT changed; this repeats the resulting
        # sentence on its own line with nothing else on it, so a minor
        # tweak can be copy/pasted straight out of the terminal into
        # (e)dit instead of retyping the whole bullet from scratch.
        print()
        print(f"    {suggested}")
        print()
        return

    # Shouldn't normally reach here — stage 5/5 are supposed to prevent this
    # combination — but if one slips through, say so plainly rather than
    # silently mislabeling it as ordinary new content.
    print(f"  ⚠ UNUSUAL EDIT (target={target!r}, edit_type={edit_type!r}, "
          f"original_text={original!r}) — this doesn't match a pattern stage 9 "
          f"knows how to apply. Approving it will likely have no effect on the "
          f"final resume. Proposed text:")
    print(f"    {suggested}")


def run(verified_edits_path: str, out_dir: str, max_edits: int = 5, revisit: bool = False):
    """revisit=True (only ever set for an explicit --app-id run) shows
    EVERY edit, including ones that already have a recorded decision,
    so you can change your mind on something you already approved or
    rejected -- each edit's prior decision is shown right before you're
    asked again. revisit=False (the default -- explicit --verified-edits/
    --out-dir, or auto-discover) skips edits that already have a
    decision entirely; they're left exactly as they were, and only
    genuinely new/undecided edits get shown. Either way, decisions are
    merged into the existing review_decisions.json rather than replacing
    it wholesale, so a decision made in an earlier session (or on an
    edit outside this run's --max-edits window) is never silently lost."""
    data = json.loads(Path(verified_edits_path).read_text(encoding="utf-8"))
    all_edits = data["passed"] + data["flagged"]

    decisions_out_path = Path(out_dir) / "review_decisions.json"
    existing_decisions: dict[str, dict] = {}
    if decisions_out_path.is_file():
        try:
            existing_decisions = {d["edit_id"]: d for d in json.loads(decisions_out_path.read_text(encoding="utf-8"))}
        except Exception:
            existing_decisions = {}

    if revisit:
        reviewable_edits = all_edits
    else:
        reviewable_edits = [e for e in all_edits if e["edit_id"] not in existing_decisions]
        already_decided = len(all_edits) - len(reviewable_edits)
        if already_decided and reviewable_edits:
            print(f"\n{Path(out_dir).name}: skipping {already_decided} edit(s) already decided "
                  f"in a prior review session (pass --app-id {Path(out_dir).name} to revisit them).")

    if not reviewable_edits:
        if existing_decisions:
            print(f"\n{Path(out_dir).name}: all {len(all_edits)} edit(s) already reviewed -- "
                  f"nothing new to decide. Pass --app-id {Path(out_dir).name} to revisit prior "
                  f"approve/reject/edit decisions.")
        return

    # Loaded once, used by _find_duplicate_signal for any NEW cover-
    # letter content (reword-in-place edits are expected to overlap
    # with their own original bullet, so this only matters for the
    # "assembled fresh" kind). Missing baseline just means duplicate
    # checks against it are skipped, not a fatal error.
    baseline_text = ""
    cl_sample = CONFIG_ROOT / "cover_letter_sample"
    if cl_sample.is_dir():
        txt_files = list(cl_sample.glob("*.txt"))
        if txt_files:
            baseline_text = txt_files[0].read_text(encoding="utf-8")

    # Loaded once, used by _find_unsupported_new_term -- reads
    # routing.json (written during prepare) to find which variant's
    # ledger this app used, same lookup every other stage relies on.
    # Missing routing/ledger just means that check is skipped, not a
    # fatal error -- an older app prepared before routing.json existed
    # shouldn't break review.
    claims_by_id: dict[str, str] = {}
    routing_path = Path(out_dir) / "routing.json"
    if routing_path.is_file():
        try:
            from batch_common import slugify_variant
            variant = json.loads(routing_path.read_text(encoding="utf-8")).get("resume_variant", "")
            slug = slugify_variant(variant) if variant else ""
            ledger_path = APPLICATIONS_ROOT / "_shared" / "variants" / slug / "claims_ledger.json"
            if slug and ledger_path.is_file():
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                claims_by_id = {c["claim_id"]: c["text"] for c in ledger.get("claims", [])}
        except Exception:
            pass

    company_brief_text, company_name = _company_brief_text(Path(out_dir))

    def is_clean(edit: dict) -> bool:
        """Eligible for bulk-accept only if NONE of the checks
        show_edit displays would flag it -- fact-check, placeholder
        leak, degenerate/near-empty text, broken sentence fragment,
        length explosion, duplicate content, company-research
        contradiction, or an unsupported new term on a cited claim.
        Kept as one explicit list mirroring show_edit's own check
        order, rather than re-deriving it, so the two can't quietly
        drift apart and bulk-accept something the display would have
        flagged a moment later."""
        if "verification" in edit:
            return False
        if _find_placeholder_leak(edit) is not None:
            return False
        if _find_degenerate_text(edit) is not None:
            return False
        if _find_broken_fragment(edit) is not None:
            return False
        if _find_length_explosion(edit) is not None:
            return False
        if _find_duplicate_signal(edit, all_edits, baseline_text, company_name) is not None:
            return False
        if _find_company_contradiction(edit, company_brief_text) is not None:
            return False
        return _find_unsupported_new_term(edit, claims_by_id) is None

    requirement_priorities = _load_requirement_priorities(Path(out_dir))
    scored = sorted(
        ((score_edit(e, requirement_priorities), e) for e in reviewable_edits),
        key=lambda pair: pair[0], reverse=True,
    )

    if len(scored) > max_edits:
        selected = scored[:max_edits]
        skipped = scored[max_edits:]
        print(f"\n{len(reviewable_edits)} candidate edits total -- reviewing the {max_edits} "
              f"highest-impact (ranked by edit type + JD-requirement priority overlap, "
              f"no LLM call, no extra cost).")
        print(f"Not reviewing (and therefore not applied) -- {len(skipped)} lower-impact "
              f"edit(s): {', '.join(e['edit_id'] for _, e in skipped)}")
        log_event("stage8_review_skipped", {
            "app_id": Path(out_dir).name,
            "skipped_edit_ids": [e["edit_id"] for _, e in skipped],
            "skipped_count": len(skipped),
            "max_edits": max_edits,
        })
    else:
        selected = scored

    decisions: list[ReviewDecision] = []

    # Opt-in, explicit, and never silent -- nothing gets approved
    # without you seeing it and choosing to. When SOME edits in this
    # app are clean (cleared fact-check and utility review with no
    # flags -- see show_edit's suggestion line for what that means)
    # and at least one is flagged, offer to bulk-accept just the clean
    # ones so the manual a/r/e/q flow below only has to cover the
    # edits that actually need judgment. Declining (or there being
    # nothing clean to offer) falls through to reviewing every edit
    # individually, exactly as before -- this changes nothing about
    # the default, careful path.
    clean = [(s, e) for s, e in selected if is_clean(e)]
    flagged_selected = [(s, e) for s, e in selected if not is_clean(e)]
    app_label_preview = Path(out_dir).name
    if clean and flagged_selected:
        print(f"\n=== {app_label_preview}: {len(clean)} of {len(selected)} edit(s) cleared "
              f"fact-check and utility review with no flags. ===")
        bulk_choice = input(
            f"  Bulk-accept those {len(clean)} clean edit(s) and go straight to the "
            f"{len(flagged_selected)} flagged one(s)? (y)es / (n)o, review all individually: "
        ).strip().lower()
        if bulk_choice == "y":
            for _score, edit in clean:
                print(f"  ✓ auto-approved (clean): [{edit['edit_id']}] {edit['section']}")
                decisions.append(ReviewDecision(edit_id=edit["edit_id"], decision="approved"))
            selected = flagged_selected
            print()
    quit_requested = False

    app_label = Path(out_dir).name
    for _score, edit in selected:
        flag = " [FLAGGED: embellished — check the numbers]" if "verification" in edit else ""
        # app_label repeated on every edit, not just once at the top of
        # the batch -- edit_ids aren't unique across JDs (each stage 5
        # run numbers its own edits fresh), so [cl-03] in one company's
        # review looks identical to [cl-03] in another's a few edits
        # later, especially in an auto-discover session walking through
        # several JDs back-to-back. Found via real confusion in real use.
        print(f"\n\n[{app_label} / {edit['edit_id']}] {edit['section']}{flag}")
        prior = existing_decisions.get(edit["edit_id"])
        if prior:
            prior_desc = prior.get("decision", "?")
            if prior_desc == "edited" and prior.get("final_text"):
                prior_desc += f" -> {prior['final_text']!r}"
            print(f"  (Previously: {prior_desc} -- deciding again will overwrite that.)")
        print()  # blank line between the title above and the suggested action below
        show_edit(edit, other_edits=all_edits, baseline_text=baseline_text,
                  company_brief_text=company_brief_text, company_name=company_name,
                  claims_by_id=claims_by_id)
        print()  # blank line between the given edit above and the response prompt below

        # Strict re-prompt loop: only a/r/e/q are ever accepted. Anything
        # else -- including an accidental blank Enter -- re-prompts for
        # THIS SAME edit rather than guessing what you meant. The
        # previous behavior treated any unrecognized input (including
        # empty input) as replacement text, which meant a stray Enter
        # press got silently recorded as an APPROVED edit with an EMPTY
        # final_text -- wiping that bullet from the resume entirely,
        # not just skipping it. That's worse than a rejection, and easy
        # to never notice until the final document.
        while True:
            choice = input("  (a)pprove / (r)eject / (e)dit / (q)uit: ").strip()
            choice_lower = choice.lower()

            if choice_lower == "q":
                quit_requested = True
                break
            elif choice_lower == "a":
                decisions.append(ReviewDecision(edit_id=edit["edit_id"], decision="approved"))
                break
            elif choice_lower == "e":
                new_text = input("  New text: ").strip()
                if not new_text:
                    print("  Empty text isn't a valid replacement. Re-enter the text, "
                          "or 'q' at the main prompt to quit reviewing.")
                    continue
                new_text = _confirm_edited_text(new_text)
                decisions.append(ReviewDecision(
                    edit_id=edit["edit_id"], decision="edited", final_text=new_text,
                ))
                break
            elif choice_lower == "r":
                decisions.append(ReviewDecision(edit_id=edit["edit_id"], decision="rejected"))
                break
            else:
                print(f"  {choice!r} isn't a recognized command -- enter a, r, e, or q.")
                continue

        if quit_requested:
            break

    # Merge, not replace -- a decision recorded in an earlier session
    # (or on an edit this run's --max-edits window didn't reach) stays
    # in place unless THIS run explicitly re-decided that same edit_id
    # (only possible in revisit mode; non-revisit mode never touches an
    # edit_id already in existing_decisions in the first place).
    merged_by_id = dict(existing_decisions)
    for d in decisions:
        merged_by_id[d.edit_id] = d.model_dump()

    out = decisions_out_path
    out.write_text(json.dumps(list(merged_by_id.values()), indent=2))
    log_event("stage8_review", {
        "approved": sum(d.decision == "approved" for d in decisions),
        "edited": sum(d.decision == "edited" for d in decisions),
        "rejected": sum(d.decision == "rejected" for d in decisions),
    })
    print(f"\nSaved {len(decisions)} new decision(s) this session -> {out} "
          f"({len(merged_by_id)} total decided for this app)")

    non_rejected = sum(d.get("decision") in ("approved", "edited") for d in merged_by_id.values())
    if non_rejected == 0:
        print(
            f"\n!!! 0 edits approved or edited — nothing will change in the final "
            f"resume/cover letter !!!\n"
            f"If you quit early (q) or rejected everything, py_pipeline_assemble.py "
            f"will refuse to treat this as reviewed and won't produce output for "
            f"it. Re-run this review if that wasn't intentional:\n"
            f"  python src/stages/py_stage08_agentic_update_review.py --app-id {Path(out_dir).name}\n",
            file=sys.stderr,
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verified-edits", help="Path to verified_edits.json. Omit to auto-discover.")
    ap.add_argument("--out-dir", help="Where to write review_decisions.json. Omit to auto-discover.")
    ap.add_argument("--app-id", help="Shorthand for --verified-edits/--out-dir under applications/<app-id>/ "
                                      "(a unique prefix of the folder name works too). Also switches on "
                                      "revisit mode: every edit is shown, including ones you already "
                                      "approved/rejected/edited, so you can change your mind. Without "
                                      "--app-id (auto-discover, or explicit --verified-edits/--out-dir), "
                                      "already-decided edits are always skipped unless --force is given.")
    ap.add_argument("--force", action="store_true",
                     help="Re-review edits that already have a recorded decision instead of "
                          "skipping them -- i.e. revisit mode, without needing --app-id. Alone "
                          "(no --app-id/--verified-edits/--out-dir), revisits EVERY app with an "
                          "edits file, not just ones with something new pending. Combined with "
                          "--app-id it's a no-op (that already implies revisit). Combined with "
                          "explicit --verified-edits/--out-dir, it's the only way to revisit "
                          "that specific pair, since that path otherwise never shows an "
                          "already-decided edit.")
    ap.add_argument("--max-edits", type=int, default=5,
                     help="Review at most this many highest-impact edits (default: 5). "
                          "The rest are simply not applied -- not rejected, just skipped, "
                          "so re-running with a higher --max-edits later would include them.")
    args = ap.parse_args()

    if args.verified_edits or args.out_dir:
        if not (args.verified_edits and args.out_dir):
            ap.error("--verified-edits and --out-dir must be given together")
        run(args.verified_edits, args.out_dir, max_edits=args.max_edits, revisit=args.force)
    elif args.app_id:
        out_dir = resolve_app_dir(args.app_id)
        run(str(edits_file_for(out_dir)), str(out_dir), max_edits=args.max_edits, revisit=True)
    else:
        pending = find_all_apps_with_edits() if args.force else find_pending_reviews()
        if not pending:
            if args.force:
                print("No applications with an edits file found to revisit.")
            else:
                print("Nothing waiting on review — every app either has no verified_edits.json yet "
                      "or every one of its edits already has a decision. Run src/checkpoints/py_pipeline_status.py to "
                      "check, or pass --app-id <id> (or --force) to revisit decisions you already made.")
            sys.exit(0)
        label = "to revisit (--force)" if args.force else "pending review"
        print(f"Auto-discovered {len(pending)} app(s) {label}: "
              f"{', '.join(p.name for p in pending)}")
        for i, app_dir in enumerate(pending, 1):
            verb = "Revisiting" if args.force else "Reviewing"
            print(f"\n=== {verb} {app_dir.name} ({i}/{len(pending)}) ===")
            run(str(edits_file_for(app_dir)), str(app_dir), max_edits=args.max_edits, revisit=args.force)
