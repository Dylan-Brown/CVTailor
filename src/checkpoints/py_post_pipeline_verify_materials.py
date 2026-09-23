#!/usr/bin/env python3
"""
py_post_pipeline_verify_materials.py -- the automated Tier 1 "Definition
of Done" gate: missing keywords, leaked placeholders, page limits.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import APPLICATIONS_ROOT, app_dirs
from docx_utils import get_pdf_page_count
from prose import extract_required_keywords, KNOWN_BAD_PHRASES

RESUME_MAX_PAGES_DEFAULT = 2
COVER_LETTER_MAX_PAGES_DEFAULT = 1

# The keyword check's "check" name is dynamic (embeds the live x/y
# count -- see check_ats_simulation), so --keywords-only matches it by
# this fixed prefix rather than an exact string.
_KEYWORD_CHECK_PREFIX = "required JD keywords present in resume text"

# Worth-a-look signals, never printed in red -- not in the same class
# as a placeholder leak, unreadable document, or broken edit.
_SOFT_CHECK_PREFIXES = (_KEYWORD_CHECK_PREFIX, "at least one RESUME edit was approved")

# ANSI font colors: green/orange for the app header, red for ✗ lines,
# yellow for soft ✗ (see _soft_passed). Only applied on a real terminal.
_RESET = "\033[0m"
_GREEN = "\033[32m"
_ORANGE = "\033[38;5;208m"
_RED = "\033[31m"
_YELLOW = "\033[33m"


def _colorize(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}" if sys.stdout.isatty() else text

def _extract_docx_text(path: Path) -> str | None:
    try:
        import docx
        d = docx.Document(path)
        return "\n".join(p.text for p in d.paragraphs)
    except Exception:
        return None


def _extract_pdf_text(path: Path) -> str | None:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return None


def _read_material_text(materials_dir: Path, suffix: str) -> tuple[str | None, Path | None]:
    """suffix is ' - Resume' or ' - Cover Letter'. Prefers .docx (more
    reliable extraction) over .pdf, falling back to whichever exists."""
    docx_files = list(materials_dir.glob(f"*{suffix}.docx"))
    pdf_files = list(materials_dir.glob(f"*{suffix}.pdf"))
    if docx_files:
        return _extract_docx_text(docx_files[0]), docx_files[0]
    if pdf_files:
        return _extract_pdf_text(pdf_files[0]), pdf_files[0]
    return None, None


def _check_no_angle_brackets(text: str, record, label: str) -> None:
    """Backstop under the fixed-enumeration KNOWN_BAD_PHRASES check --
    catches any placeholder token not on that list. A bare '<'/'>' had
    zero legitimate occurrences across 40 real generated documents."""
    found = sorted(set(ch for ch in "<>" if ch in text))
    record(f"no unresolved '<'/'>' template syntax in the {label}", not found,
           f"found {', '.join(found)!r} in the assembled {label} -- almost certainly a "
           f"placeholder token that didn't get substituted" if found else "")


def _find_generated_pdf(materials_dir: Path, suffix: str) -> Path | None:
    """suffix is ' - Resume' or ' - Cover Letter'. Independent of
    _read_material_text()'s docx-preferred lookup -- page count is a
    PDF-only property; reusing that path used to silently skip both checks."""
    pdf_files = list(materials_dir.glob(f"*{suffix}.pdf"))
    return pdf_files[0] if pdf_files else None


_BLIP_TAG = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"


def check_signature_spacing(materials_dir: Path, record) -> None:
    """compress_docx_spacing() can shrink the signature paragraph's line
    box below the image's height, causing visual overlap with "Best,".
    Checked against .docx formatting -- skipped if only a PDF survives."""
    cover_docx = list(materials_dir.glob("* - Cover Letter.docx"))
    if not cover_docx:
        return
    import docx
    paras = docx.Document(cover_docx[0]).paragraphs
    img_idx = next((i for i, p in enumerate(paras)
                     if any(r._element.findall(".//" + _BLIP_TAG) for r in p.runs)), None)
    if img_idx is None:
        return  # no signature image in this template -- nothing to check
    pf = paras[img_idx].paragraph_format
    ls = pf.line_spacing
    sb = pf.space_before.pt if pf.space_before else None
    sa = pf.space_after.pt if pf.space_after else None
    squeezed = (ls is not None and ls < 1.0) or (sb is not None and sb < 11) or (sa is not None and sa < 11)
    record("signature image not squeezed by page-fit compression", not squeezed,
           f"line_spacing={ls}, space_before={sb}pt, space_after={sa}pt on the image's "
           f"paragraph -- page-fit compression likely pulled the signature image up into "
           f"the sign-off line. Run src/util/py_fix_signature_overlap.py to fix." if squeezed else "")


def check_ats_simulation(app_dir: Path, materials_dir: Path, record) -> None:
    """Simulates two vendor-agnostic ATS failure modes, not any specific
    ATS's score: primitive PDF parsing, and literal keyword coverage."""
    resume_pdfs = list(materials_dir.glob("* - Resume.pdf"))
    if not resume_pdfs:
        return  # no PDF to simulate against -- docx-only, skip silently rather than fail
    resume_pdf = resume_pdfs[0]

    try:
        import pdfplumber
        with pdfplumber.open(resume_pdf) as pdf:
            raw_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        record("ATS-style PDF text extraction succeeds", False, f"couldn't extract: {e}")
        return

    if not raw_text.strip():
        record("ATS-style PDF text extraction succeeds", False,
               "extracted empty text -- likely an image-based PDF, invisible to most ATS parsers")
        return
    record("ATS-style PDF text extraction succeeds", True, f"{len(raw_text)} characters extracted")

    # Low alphabetic ratio usually means a font/encoding issue mangled the text.
    alpha_count = sum(1 for c in raw_text if c.isalpha())
    alpha_ratio = alpha_count / max(1, len(raw_text))
    record("extracted text isn't garbled", alpha_ratio > 0.5,
           f"{alpha_ratio:.0%} alphabetic characters (want > 50%)")

    applicant_info_path = Path("config/applicant_info.json")
    if applicant_info_path.is_file():
        try:
            applicant = json.loads(applicant_info_path.read_text(encoding="utf-8"))
            name = applicant.get("name", "")
            email = applicant.get("email", "")
            if name:
                record("applicant name is extractable from the PDF", name in raw_text,
                       "" if name in raw_text else f"{name!r} not found in extracted text")
            if email:
                record("applicant email is extractable from the PDF", email in raw_text,
                       "" if email in raw_text else f"{email!r} not found in extracted text")
        except Exception:
            pass

    edit_brief_path = app_dir / "edit_brief.json"
    if edit_brief_path.is_file():
        try:
            edit_brief = json.loads(edit_brief_path.read_text(encoding="utf-8"))
            keywords = extract_required_keywords(edit_brief)
            if keywords:
                # Case-insensitive, matching classify_keyword_gaps' own
                # convention -- real ATS lexical search isn't case-sensitive either.
                raw_text_lower = raw_text.lower()
                missing = [kw for kw in keywords if kw.lower() not in raw_text_lower]
                coverage = 1 - (len(missing) / len(keywords))
                record(f"{_KEYWORD_CHECK_PREFIX} ({len(keywords) - len(missing)}/{len(keywords)})",
                       coverage >= 0.7,
                       f"missing: {missing}" if missing else "all present")
        except Exception:
            pass


def check_app(app_dir: Path, resume_max_pages: int, cover_letter_max_pages: int) -> dict:
    app_id = app_dir.name
    materials_dir = app_dir / "generated_materials"
    checks = []

    def record(name: str, passed: bool, detail: str = ""):
        checks.append({"check": name, "passed": passed, "detail": detail})

    if not materials_dir.is_dir():
        record("generated_materials/ exists", False, "app hasn't been assembled yet")
        return {"app_id": app_id, "checks": checks, "all_passed": False}
    record("generated_materials/ exists", True)

    # --- Company/JD context, for checks that need to know what "correct" looks like ---
    jd_path = app_dir / "jd_input.processed.json"
    if not jd_path.is_file():
        jd_path = app_dir / "jd_input.json"
    company = role_title = None
    if jd_path.is_file():
        try:
            jd = json.loads(jd_path.read_text(encoding="utf-8"))
            company, role_title = jd.get("company"), jd.get("role_title")
        except Exception:
            pass

    # Defense in depth -- catches ledgers prepared before schemas.py
    # enforced min_length=10 (a real run once silently extracted only 5 claims).
    routing_path = app_dir / "routing.json"
    if routing_path.is_file():
        try:
            variant = json.loads(routing_path.read_text(encoding="utf-8")).get("resume_variant")
            slug = re.sub(r"[^\w]+", "_", (variant or "").strip().lower()).strip("_")
            ledger_path = APPLICATIONS_ROOT / "_shared" / "variants" / slug / "claims_ledger.json"
            if ledger_path.is_file():
                n_claims = len(json.loads(ledger_path.read_text(encoding="utf-8")).get("claims", []))
                record(f"claims ledger has enough content ({variant} variant)",
                       n_claims >= 10, f"{n_claims} claims (want >= 10, ideally 30+)")
        except Exception as e:
            record("claims ledger has enough content", False, f"couldn't check: {e}")

    # Defense in depth -- a real run once returned 60.0 meaning "60%"
    # where a 0.60 fraction was expected, silently printed as "6000%".
    edit_brief_path = app_dir / "edit_brief.json"
    if edit_brief_path.is_file():
        try:
            score = json.loads(edit_brief_path.read_text(encoding="utf-8")).get("coverage_score")
            record("coverage_score is a real 0-1 value", 0.0 <= score <= 1.0, f"got {score}")
        except Exception as e:
            record("coverage_score is a real 0-1 value", False, f"couldn't check: {e}")

    # Checked PER DOCUMENT, not aggregate -- a run once generated only
    # cover-letter edits, zero resume edits, which an aggregate check would miss.
    decisions_path = app_dir / "review_decisions.json"
    edits_path = app_dir / "critiqued_edits.json"
    if not edits_path.is_file():
        edits_path = app_dir / "verified_edits.json"
    if decisions_path.is_file() and edits_path.is_file():
        try:
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
            edits_data = json.loads(edits_path.read_text(encoding="utf-8"))
            target_by_id = {e["edit_id"]: e.get("target") for e in
                            edits_data.get("passed", []) + edits_data.get("flagged", [])}
            approved_ids = [d["edit_id"] for d in decisions if d["decision"] in ("approved", "edited")]
            n_resume = sum(1 for eid in approved_ids if target_by_id.get(eid) == "resume")
            n_cover = sum(1 for eid in approved_ids if target_by_id.get(eid) == "cover_letter")
            record("at least one edit was approved (any document)", len(approved_ids) > 0,
                   f"{len(approved_ids)} approved/edited out of {len(decisions)} reviewed")
            record("at least one RESUME edit was approved", n_resume > 0,
                   f"{n_resume} resume edit(s) approved -- 0 means the resume is completely "
                   f"untailored for this application" if n_resume == 0 else f"{n_resume} approved")
        except Exception as e:
            record("at least one edit was approved", False, f"couldn't check: {e}")
    elif decisions_path.is_file():
        try:
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
            n_approved = sum(1 for d in decisions if d["decision"] in ("approved", "edited"))
            record("at least one edit was approved", n_approved > 0,
                   f"{n_approved} approved/edited out of {len(decisions)} reviewed "
                   f"(couldn't split resume vs. cover letter -- edits source file missing)")
        except Exception as e:
            record("at least one edit was approved", False, f"couldn't check: {e}")

    # --- Cover letter checks ---
    cover_text, cover_path = _read_material_text(materials_dir, " - Cover Letter")
    if cover_text is None:
        record("cover letter readable", False, "no .docx or .pdf found, or text extraction failed")
    else:
        record("cover letter readable", True)

        # A real run once had two bullets independently centered on the
        # same dollar figure -- this catches that regardless of stage 5's own self-check.
        dollar_figures = re.findall(r"\$[\d,.]+\s?[MmBbKk]?(?:illion)?", cover_text)
        seen_figures = {}
        for fig in dollar_figures:
            norm = re.sub(r"[,\s]", "", fig).lower().rstrip("illion")
            seen_figures.setdefault(norm, []).append(fig)
        repeated = {k: v for k, v in seen_figures.items() if len(v) > 1}
        record("no dollar figure repeated across separate bullets", not repeated,
               f"{dict(repeated)} -- likely two bullets independently centered on the "
               f"same metric" if repeated else "")

        for phrase in KNOWN_BAD_PHRASES:
            if phrase in cover_text:
                record(f"no leftover placeholder/known-bad text: {phrase!r}", False,
                       "found verbatim in the assembled cover letter")
        if not any(phrase in cover_text for phrase in KNOWN_BAD_PHRASES):
            record("no leftover placeholders or known-bad phrases", True)
        _check_no_angle_brackets(cover_text, record, "cover letter")

        if company:
            # Re:/salutation trivially contain the company name -- 3+
            # mentions is a cheap proxy that the free-written paragraph actually engaged.
            mentions = cover_text.count(company)
            record(f"company name ({company!r}) appears beyond just Re:/salutation",
                   mentions >= 3, f"found {mentions} mention(s), want >= 3")

        # Catches the mandatory company-paragraph rewrite silently not firing.
        generic_tell = "keeps pushing on developer experience by accelerating and streamlining"
        record("mandatory company paragraph was actually rewritten",
               generic_tell not in cover_text,
               "found the generic template phrasing verbatim -- the company-specific "
               "rewrite did not happen" if generic_tell in cover_text else "")

        # Flags the OLD redundant 2nd-paragraph phrasing specifically
        # (trimmed from the baseline sample) -- firing means this letter predates that fix.
        redundant_tell = "reimagining fragmented legacy financial applications"
        record("2nd body paragraph doesn't restate the opening/bullets",
               redundant_tell not in cover_text,
               "found the old redundant phrasing verbatim -- this letter predates the "
               "baseline trim (see config/cover_letter_sample/*.txt) and should be "
               "regenerated or hand-edited" if redundant_tell in cover_text else "")

        cover_pdf = _find_generated_pdf(materials_dir, " - Cover Letter")
        if cover_pdf:
            pages = get_pdf_page_count(cover_pdf)
            if pages is not None:
                record(f"cover letter <= {cover_letter_max_pages} page(s)",
                       pages <= cover_letter_max_pages, f"{pages} page(s)")

        check_signature_spacing(materials_dir, record)

    # --- Resume checks ---
    resume_text, resume_path = _read_material_text(materials_dir, " - Resume")
    if resume_text is None:
        record("resume readable", False, "no .docx or .pdf found, or text extraction failed")
    else:
        record("resume readable", True)
        for phrase in KNOWN_BAD_PHRASES:
            if phrase in resume_text:
                record(f"no leftover placeholder/known-bad text: {phrase!r}", False,
                       "found verbatim in the assembled resume")
        _check_no_angle_brackets(resume_text, record, "resume")
        resume_pdf = _find_generated_pdf(materials_dir, " - Resume")
        if resume_pdf:
            pages = get_pdf_page_count(resume_pdf)
            if pages is not None:
                record(f"resume <= {resume_max_pages} page(s)",
                       pages <= resume_max_pages, f"{pages} page(s)")

        check_ats_simulation(app_dir, materials_dir, record)

    all_passed = all(c["passed"] for c in checks)
    return {"app_id": app_id, "company": company, "role_title": role_title,
            "checks": checks, "all_passed": all_passed}


def _is_soft_check(check_name: str) -> bool:
    return check_name.startswith(_SOFT_CHECK_PREFIXES)


def _soft_passed(checks: list[dict]) -> bool:
    """True if every check EXCEPT the soft ones (_SOFT_CHECK_PREFIXES)
    passed -- those alone shouldn't read as red/orange the way a
    placeholder leak, an unreadable document, or a broken edit does."""
    return all(c["passed"] for c in checks if not _is_soft_check(c["check"]))


def _print_check_line(c: dict) -> None:
    mark = "✓" if c["passed"] else "✗"
    line = f"  {mark} {c['check']}"
    if c["detail"]:
        line += f" — {c['detail']}"
    if not c["passed"]:
        line = _colorize(line, _YELLOW if _is_soft_check(c["check"]) else _RED)
    print(line)


def run(app_id: str | None, all_apps: bool, resume_max_pages: int, cover_letter_max_pages: int,
        keywords_only: bool = False):
    if not app_id and not all_apps:
        print("Specify --app-id <id> or --all.", file=sys.stderr)
        sys.exit(2)

    targets = [APPLICATIONS_ROOT / app_id] if app_id else app_dirs()
    if app_id and not targets[0].is_dir():
        print(f"No applications/{app_id}/ folder found.", file=sys.stderr)
        sys.exit(1)

    any_failed = False
    for app_dir in targets:
        # keywords_only is a display filter only -- every check still runs
        # and the full result still gets written to definition_of_done_report.json.
        result = check_app(app_dir, resume_max_pages, cover_letter_max_pages)
        status = "PASS" if result["all_passed"] else "FAIL"
        # Header is green either on a true all-clear, or when the ONLY
        # failure(s) are the keywords check -- see _soft_passed.
        soft_passed = _soft_passed(result["checks"])
        header_color = _GREEN if soft_passed else _ORANGE
        print(f"\n\n=== {_colorize(result['app_id'], header_color)} — {status} ===")

        if keywords_only:
            keyword_checks = [c for c in result["checks"] if c["check"].startswith(_KEYWORD_CHECK_PREFIX)]
            if not keyword_checks:
                print("  (no keyword check available -- edit_brief.json missing, or resume text couldn't be extracted)")
            for c in keyword_checks:
                _print_check_line(c)
        else:
            for c in result["checks"]:
                _print_check_line(c)

        if not result["all_passed"]:
            any_failed = True

        report_path = app_dir / "definition_of_done_report.json"
        report_path.write_text(json.dumps(result, indent=2))

    print()
    if any_failed:
        print(_colorize(
            "One or more applications failed automated checks — read the ✗ lines above, "
            "then see DEFINITION_OF_DONE.md's Tier 2 for what to check by hand regardless.",
            _ORANGE))
        sys.exit(1)
    else:
        print(_colorize(
            "All automated checks passed. Still do the Tier 2 human skim in "
            "DEFINITION_OF_DONE.md before sending anything.",
            _GREEN))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-id", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--resume-max-pages", type=int, default=RESUME_MAX_PAGES_DEFAULT)
    ap.add_argument("--cover-letter-max-pages", type=int, default=COVER_LETTER_MAX_PAGES_DEFAULT)
    ap.add_argument("--keywords-only", action="store_true",
                     help="Skip printing every other check -- just the app header and the "
                          "required-JD-keywords-missing line, for a quick read while manually "
                          "editing a .docx. Still runs every check and still writes the full "
                          "definition_of_done_report.json; this only trims what's printed.")
    args = ap.parse_args()
    run(args.app_id, args.all, args.resume_max_pages, args.cover_letter_max_pages,
        keywords_only=args.keywords_only)
