#!/usr/bin/env python3
"""
py_post_pipeline_verify_materials.py
====================================
The automated Tier 1 "Definition of Done" gate. 
Scans final assembled documents for missing keywords, leaked placeholders, and page limit violations.

Usage:
    python src/checkpoints/py_post_pipeline_verify_materials.py --all
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

# Checks that are worth-a-look signals, not hard failures -- never
# printed in red, regardless of what else did or didn't pass. A missed
# keyword or a resume that got zero approved edits are both things
# you'd want to notice, but neither is in the same class as a
# placeholder leak, an unreadable document, or a broken edit.
_SOFT_CHECK_PREFIXES = (_KEYWORD_CHECK_PREFIX, "at least one RESUME edit was approved")

# ANSI font-color codes (not background highlight) -- green/orange for
# the app header (overall pass/fail), red for individual ✗ lines, yellow
# for a ✗ keywords line specifically (see _soft_passed). Only applied
# when stdout is a real terminal, so piping/redirecting output to a
# file doesn't end up full of escape-code noise.
_RESET = "\033[0m"
_GREEN = "\033[32m"
_ORANGE = "\033[38;5;208m"
_RED = "\033[31m"
_YELLOW = "\033[33m"


def _colorize(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}" if sys.stdout.isatty() else text

# Known-bad leftover strings this project has actually shipped in real
# output at least once. Not a general profanity/placeholder filter --
# specific phrases from specific incidents, kept here so they can never
# silently recur without at least a loud failure.
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
    reliable text extraction) over .pdf if both exist; falls back to
    whichever one is actually present."""
    docx_files = list(materials_dir.glob(f"*{suffix}.docx"))
    pdf_files = list(materials_dir.glob(f"*{suffix}.pdf"))
    if docx_files:
        return _extract_docx_text(docx_files[0]), docx_files[0]
    if pdf_files:
        return _extract_pdf_text(pdf_files[0]), pdf_files[0]
    return None, None


def _check_no_angle_brackets(text: str, record, label: str) -> None:
    """Backstop underneath the named-token KNOWN_BAD_PHRASES check --
    that list is a fixed enumeration (<COMPANY_NAME>, <ROLE_NAME>,
    etc.) and silently misses any placeholder token that isn't on it
    yet (a new one added to a template, a typo'd variant, a renamed
    one). Checked empirically before adding this: scanned every
    paragraph of all 40 real generated .docx files (20 resumes + 20
    cover letters) for a bare '<' or '>' -- zero legitimate occurrences
    anywhere (no "<100ms"-style technical phrasing in this candidate's
    actual writing), so a bare angle bracket is a safe, general signal
    of a leaked template token, not just the specific ones already
    named."""
    found = sorted(set(ch for ch in "<>" if ch in text))
    record(f"no unresolved '<'/'>' template syntax in the {label}", not found,
           f"found {', '.join(found)!r} in the assembled {label} -- almost certainly a "
           f"placeholder token that didn't get substituted" if found else "")


def _find_generated_pdf(materials_dir: Path, suffix: str) -> Path | None:
    """suffix is ' - Resume' or ' - Cover Letter'. Deliberately
    independent of _read_material_text()'s own docx-preferred lookup --
    with --keep-docx defaulting on, a .docx and a .pdf normally sit side
    by side in generated_materials/, and page COUNT is a property of
    the rendered PDF specifically, not the docx. Real incident: the
    page-count checks below used to reuse _read_material_text()'s
    returned path, which is the .docx whenever one exists -- silently
    skipping both checks on every app with --keep-docx on, with no
    failure or skipped-check line printed, just two checks that quietly
    never ran."""
    pdf_files = list(materials_dir.glob(f"*{suffix}.pdf"))
    return pdf_files[0] if pdf_files else None


_BLIP_TAG = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"


def check_signature_spacing(materials_dir: Path, record) -> None:
    """Real incident: compress_docx_spacing() (the page-fit typography
    compressor in src/core/docx_utils.py) rewrites line_spacing/
    space_before/space_after on every paragraph in a cover letter,
    including the signature image's own paragraph. A line_spacing
    multiple below 1.0 shrinks that paragraph's line box below the
    image's actual height, so the image visually overlaps the "Best,"
    line above it -- confirmed on 13 of 20 real generated cover letters
    (see src/util/py_fix_signature_overlap.py, the remediation script
    for existing files). Checked directly against the .docx paragraph formatting --
    a PDF's already-rendered layout can't be introspected this way, so
    this is skipped silently if only a PDF survives (--no-keep-docx
    already ran)."""
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
    """Two things a real ATS is actually likely to get wrong or
    filter on, simulated locally -- NOT a prediction of any specific
    commercial ATS's real score, which is a proprietary black box
    nobody outside that company can replicate. This tests the two
    well-documented, vendor-agnostic failure modes instead:

    1. Parsing fidelity -- most ATS use fairly primitive, non-layout-
       aware PDF text extraction (similar to what pdfplumber does
       here). Multi-column layouts get scrambled, text in tables/
       headers/footers is often dropped, image-based content is
       invisible. Tested by extracting the PDF the same primitive way
       and checking basic parseability (name/email present, output
       isn't garbled or empty).
    2. Literal keyword coverage -- most ATS keyword search is lexical,
       not semantic. A recruiter searching "Kubernetes" won't find you
       if your resume only says "container orchestration." Tested
       against the JD's own REQUIRED terms (from edit_brief.json,
       already computed by stage 4), not invented independently.
    """
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

    # Garbled-output check -- a low alphabetic ratio usually means a
    # font/encoding issue produced mangled characters, not real content.
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
                # Case-insensitive on purpose, matching classify_keyword_gaps'
                # own convention (src/core/prose.py) -- a keyword extracted
                # as "Backend" just because it started a JD sentence
                # shouldn't register as missing when the resume genuinely
                # says "...backend services...", and real ATS lexical search
                # isn't case-sensitive either.
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

    # --- Claims ledger sanity (defense in depth -- schemas.py now enforces
    # min_length=10 going forward, but this catches anything prepared
    # before that fix landed). Real incident: a local-model run silently
    # extracted only 5 claims from a full resume (should be 30-50+),
    # and every downstream stage treated that impoverished ledger as
    # complete. ---
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

    # --- coverage_score sanity (defense in depth, same reasoning as above).
    # Real incident: a local model returned 60.0 meaning "60%" where a
    # 0.60 fraction was expected, silently accepted, printed as "6000%". ---
    edit_brief_path = app_dir / "edit_brief.json"
    if edit_brief_path.is_file():
        try:
            score = json.loads(edit_brief_path.read_text(encoding="utf-8")).get("coverage_score")
            record("coverage_score is a real 0-1 value", 0.0 <= score <= 1.0, f"got {score}")
        except Exception as e:
            record("coverage_score is a real 0-1 value", False, f"couldn't check: {e}")

    # --- At least one edit actually made it through review -- checked
    # PER DOCUMENT, not just in aggregate. Real incident: a run generated
    # only cover-letter edits, zero resume edits, for two separate
    # applications in a row -- an aggregate "at least one edit" check
    # would have passed both, silently missing that the resume (the
    # document most hiring processes and ATS systems actually scan
    # first) went out completely untailored. ---
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

        # Real incident: two consecutive bullets both centered on the
        # same "$6.8M" figure -- one untouched baseline, one a reworded
        # edit that independently drifted back onto the same metric
        # instead of the different fact its own bullet was supposed to
        # be about. Cheap, deterministic, catches this class of
        # redundancy regardless of whether stage 5's own self-check
        # (comparing proposed edits against EACH OTHER, not against
        # untouched baseline bullets sitting nearby) catches it.
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
            # Re:/salutation lines trivially contain the company name --
            # real incident was the mandatory "What draws me to X..."
            # paragraph staying completely generic (Stripe-era boilerplate)
            # while Re:/salutation still correctly showed the real company.
            # Requiring 3+ mentions is a cheap proxy for "the free-written
            # paragraph actually engaged with the company," not proof of it.
            mentions = cover_text.count(company)
            record(f"company name ({company!r}) appears beyond just Re:/salutation",
                   mentions >= 3, f"found {mentions} mention(s), want >= 3")

        # Real incident: the mandatory company-paragraph rewrite silently
        # not firing, leaving the exact Stripe-era boilerplate in place.
        generic_tell = "keeps pushing on developer experience by accelerating and streamlining"
        record("mandatory company paragraph was actually rewritten",
               generic_tell not in cover_text,
               "found the generic template phrasing verbatim -- the company-specific "
               "rewrite did not happen" if generic_tell in cover_text else "")

        # Real incident: the baseline's 2nd body paragraph ("I've spent
        # my entire career at Capital One reimagining...") restated the
        # opening sentence's "modernizing financial infrastructure at
        # Capital One" claim, then closed with a vague "integrating new
        # API endpoints...enterprise customer servicing platform" line
        # that duplicated ground the bullets below already cover more
        # concretely -- present, unedited, in 18 of 20 real generated
        # cover letters. Trimmed out of config/cover_letter_sample/
        # dylan_brown_cover_letter_new.txt (the only sentence in that
        # paragraph carrying real, non-redundant information -- the
        # Python/Node/Java/Angular stack line -- was kept). This flags
        # the OLD redundant phrasing specifically, the same pattern as
        # the mandatory-paragraph check above, not a general redundancy
        # detector -- if it fires, the baseline fix didn't make it into
        # this particular letter (an app generated before the fix, or a
        # future baseline edit reintroducing it).
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
        # keywords_only doesn't skip any checks -- check_app() is fast/
        # deterministic and its full result still gets written to
        # definition_of_done_report.json below either way. It's purely a
        # display filter, for reading/manually editing a .docx without
        # the rest of the checks scrolling the keyword miss list off screen.
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
