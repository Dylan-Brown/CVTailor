#!/usr/bin/env python3
"""
py_stage08_agentic_update_review.py -- the interactive human review CLI
for stage 8. Shows each surviving edit; you approve/reject/edit it.
"""
import argparse
import difflib
import json
import re
import sys
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/stages/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
from batch_common import APPLICATIONS_ROOT, CONFIG_ROOT, NON_APPLICATION_DIR_NAMES, is_example_file
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
    """edit_brief.json's (stage 4) requirement priorities, lowercased.
    Returns {} if missing -- ranking then falls back to edit_type alone."""
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
    """Deterministic impact score (no LLM call): edit_type base score,
    +2/+1 bonus if the text overlaps a "required"/"preferred" JD requirement."""
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
    """critiqued_edits.json (stage 7) if present, else verified_edits.json (stage 6)."""
    critiqued = app_dir / "critiqued_edits.json"
    return critiqued if critiqued.is_file() else app_dir / "verified_edits.json"


def find_matches_by_prefix(prefix: str) -> list[Path]:
    """Case-insensitive startswith match against live applications/
    folders -- same convention every other --app-id script uses."""
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
    """Exact match first, then case-insensitive prefix match. Zero or
    2+ matches is a hard error -- never guess which application you meant."""
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
    """edit_ids already decided, or {} if the file is missing/corrupt.
    Lets a partially-reviewed app (quit early, --max-edits) resume correctly."""
    decisions_path = app_dir / "review_decisions.json"
    if not decisions_path.is_file():
        return set()
    try:
        data = json.loads(decisions_path.read_text(encoding="utf-8"))
        return {d["edit_id"] for d in data}
    except Exception:
        return set()


def find_pending_reviews() -> list[Path]:
    """Apps with a real (non-dry-run) edits file that still has at
    least one undecided edit. Excludes dry-run placeholder files and
    edits already decided (use --app-id to revisit those)."""
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
    """Same candidate set as find_pending_reviews(), minus the undecided-
    edit filter -- used by --force's whole-batch revisit mode."""
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
    """Every word from the claims ledger(s), so real resume terms don't
    get flagged as typos. Merges current per-variant and legacy flat ledgers."""
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
    """Likely-misspelled words in `text`, or [] if pyspellchecker is
    missing. Exists because hand-edited text skips every other check."""
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
    """Echoes hand-typed text back and always requires confirmation --
    the one path nothing else in the pipeline checks (a past stdin-
    buffer mixup once produced coherent but wrong text, so this can't be typo-gated)."""
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
    """Numbers and capitalized proper-noun-ish tokens -- specific,
    checkable content whose repetition usually signals real duplication.
    Generic sentence-starters ("What", "Why"...) are filtered to avoid false positives."""
    tokens = set()
    tokens.update(re.findall(r"\$[\d,.]+\s?[MmBbKk]?", text))
    tokens.update(re.findall(r"\b\d{2,}\+?%?\b", text))
    tokens.update(re.findall(r"\b[A-Z][a-zA-Z0-9+.#]{2,}\b", text))
    return tokens - _GENERIC_CAPITALIZED_WORDS


def _find_duplicate_signal(edit: dict, other_edits: list[dict], baseline_text: str,
                            company_name: str = "") -> str | None:
    """No LLM call -- checks suggested_text against every other edit of
    the SAME target in this batch, and (new cover-letter content only)
    against the baseline letter. Company name excluded since it legitimately repeats."""
    my_tokens = _extract_distinctive_tokens(edit["suggested_text"])
    if company_name:
        my_tokens -= _extract_distinctive_tokens(company_name)
    if not my_tokens:
        return None

    for other in other_edits:
        if other["edit_id"] == edit["edit_id"]:
            continue
        if other.get("target") != edit.get("target"):
            continue  # only compare within the same document -- a resume
                       # skill also appearing in the cover letter is normal
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
    """Flattens company_brief.json into one search blob + the company
    name. Returns ("", "") if stage 3 never ran -- callers treat that as "nothing to check"."""
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
    """Only for cover-letter edits actually about the company. Flags
    named claims (work-mode terms, capitalized tech tokens) that stage
    3's own research never found -- "worth a second look," not "definitely wrong"."""
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
    """Mechanical backstop under stage 6's holistic judgment: for a
    resume reword adding a new technical term, does the cited claim's
    own text actually contain it? Catches scope creep a real-but-irrelevant citation hides."""
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
    """Same KNOWN_BAD_PHRASES check py_post_pipeline_verify_materials.py
    runs post-assembly, done here too so a leak is caught before assembly."""
    for phrase in KNOWN_BAD_PHRASES:
        if phrase in edit["suggested_text"]:
            return f"contains {phrase!r} verbatim -- an unresolved placeholder or leftover artifact"
    return None


def _find_degenerate_text(edit: dict) -> str | None:
    """Empty or near-empty generated text should never be silently
    approved -- same class of risk as the blank-input bug this pipeline already guards against."""
    text = edit["suggested_text"].strip()
    if len(text) < 10:
        return f"only {len(text)} character(s) after trimming whitespace -- likely a generation failure"
    return None


def _find_length_explosion(edit: dict, max_ratio: float = 2.5) -> str | None:
    """Resume rewords must stay roughly reword-IN-PLACE (stage 5 rule
    2) -- several-times-longer than the original is a real violation
    even if the verbatim match still technically holds."""
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
    """Narrow, specific pattern from a real incident: a sentence
    starting with a bare contraction suffix ('m/'d/'ve/'ll/'re/'s) and
    no leading pronoun usually means the subject went missing."""
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

    # Reuses stage 6's own stored verification reasoning -- no new cost,
    # no invented numeric confidence score. Three honest tiers: clean-
    # with-claims > clean-no-claims (mandatory paragraphs) > flagged.
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

    # Checked for EVERY edit, not just hand-edits -- previously only ran
    # on typed text, so approved AI-generated text never got checked.
    typos = _check_for_typos(suggested)
    if typos:
        print(f"  ⚠ Possible typo(s) in the AI-generated text: {', '.join(typos)}")

    # Blank line between "suggested action" and "given edit" sections.
    print()

    if target == "cover_letter":
        print(f"  COVER LETTER SENTENCE (assembled fresh — no existing text to compare):")
        print(f"    {suggested}")
        return

    if target == "resume" and edit_type == "reword" and original:
        print(f"  REWORD (existing bullet, in place):")
        diff = difflib.unified_diff(original.split(), suggested.split(), lineterm="", n=999)
        print("    " + " ".join(list(diff)[3:]))  # skip the --- / +++ / @@ header lines
        # Repeats the result on its own line so a minor tweak can be
        # copy/pasted straight into (e)dit instead of retyped.
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
    """revisit=True shows every edit including already-decided ones (so
    you can change your mind); default skips decided edits. Either way,
    decisions merge into review_decisions.json, never replace it wholesale."""
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

    # Used by _find_duplicate_signal for new cover-letter content only.
    # Missing baseline just skips that check, not a fatal error.
    baseline_text = ""
    cl_sample = CONFIG_ROOT / "cover_letter_sample"
    if cl_sample.is_dir():
        txt_files = [p for p in cl_sample.glob("*.txt") if not is_example_file(p)]
        if txt_files:
            baseline_text = txt_files[0].read_text(encoding="utf-8")

    # Used by _find_unsupported_new_term. Missing routing/ledger just
    # skips that check -- older apps predate routing.json.
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
        """Eligible for bulk-accept only if none of show_edit's checks
        would flag it. Mirrors show_edit's own check order exactly so
        the two can't quietly drift apart."""
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

    # Opt-in, never silent -- nothing is approved without you seeing it.
    # Offers to bulk-accept clean edits only when some are flagged too,
    # so manual a/r/e/q review only covers what actually needs judgment.
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
        # Repeated per-edit, not once per batch -- edit_ids aren't
        # unique across JDs, so [cl-03] can look identical in two
        # different companies' reviews back-to-back in one session.
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

        # Only a/r/e/q accepted -- anything else (including blank Enter)
        # re-prompts rather than guessing. A stray Enter used to get
        # silently recorded as an approved edit with empty final_text.
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

    # Merge, not replace -- an earlier session's decision (or one this
    # run's --max-edits window skipped) survives unless re-decided here.
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

    return quit_requested


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
            quit_requested = run(str(edits_file_for(app_dir)), str(app_dir),
                                  max_edits=args.max_edits, revisit=args.force)
            if quit_requested:
                remaining = len(pending) - i
                if remaining:
                    print(f"\nQuit requested -- stopping here. {remaining} app(s) not reviewed "
                          f"this session: {', '.join(p.name for p in pending[i:])}")
                break
