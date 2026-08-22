"""
docx_utils.py — python-docx helpers: paragraph iteration, in-place
text replacement, PDF conversion/page-count, and layout compression.
Split out of pipeline_common.py.
"""
from __future__ import annotations
import sys
from pathlib import Path

from text_matching import _normalize_quotes


def docx_replace_text(paragraph, old: str, new: str) -> bool:
    """Replace `old` with `new` inside a python-docx Paragraph, preserving
    run-level formatting (bold, italic, etc.) instead of collapsing the
    paragraph to one uniformly-formatted run.

    Word fragments visible text across multiple <w:r> runs (revision
    ids, spell-check boundaries), so a phrase you can see on the page
    often isn't a contiguous string in any single run. This walks the
    runs, finds which ones the match spans, writes the replacement into
    the FIRST run touched (keeping that run's formatting), and clears
    the matched portion out of any runs after it — non-matched text
    inside a partially-overlapping run is preserved untouched.

    Returns True if a replacement was made, False if `old` wasn't found
    in this paragraph's concatenated text.
    """
    runs = paragraph.runs
    full_text = "".join(r.text for r in runs)
    # Curly vs. straight quotes between the source resume and the separately-
    # maintained template docx is a real, observed failure mode — normalize
    # for the search only (1:1 character substitution, so this doesn't shift
    # indices) and slice the original text, not the normalized copy.
    idx = _normalize_quotes(full_text).find(_normalize_quotes(old))
    if idx == -1:
        return False
    end_idx = idx + len(old)

    offsets = []
    pos = 0
    for r in runs:
        offsets.append((pos, pos + len(r.text)))
        pos += len(r.text)

    replaced_first = False
    for r, (start, end) in zip(runs, offsets):
        if end <= idx or start >= end_idx:
            continue  # this run is entirely outside the match — untouched
        pre = r.text[: idx - start] if start < idx else ""
        post = r.text[end_idx - start:] if end > end_idx else ""
        r.text = (pre + new + post) if not replaced_first else (pre + post)
        replaced_first = True
    return True


def iter_all_paragraphs(doc):
    """Yield every paragraph in a python-docx Document, including ones
    nested inside table cells (and cells nested inside tables inside
    cells). Plenty of resume templates use tables for column layout, so
    doc.paragraphs alone silently misses most of the content."""
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from iter_all_paragraphs(cell)  # cell also has .paragraphs and .tables


def convert_docx_to_pdf(docx_path: str | Path, pdf_path: str | Path) -> bool:
    """Best-effort docx -> pdf conversion via MS Word COM automation
    (the docx2pdf package — Windows/Mac only, requires Word installed).
    Returns False rather than raising on any failure, so callers can
    fall back to shipping the .docx — a missing/broken PDF converter
    shouldn't block getting a resume out the door."""
    try:
        from docx2pdf import convert
        Path(pdf_path).parent.mkdir(parents=True, exist_ok=True)
        convert(str(docx_path), str(pdf_path))
        return Path(pdf_path).is_file()
    except Exception as e:
        print(f"PDF conversion unavailable/failed ({e}) — keeping .docx", file=sys.stderr)
        return False


def get_pdf_page_count(pdf_path: str | Path) -> int | None:
    """Returns a PDF's page count, or None if it can't be read. This is
    the only reliable way to know actual page count — python-docx never
    renders/paginates a document, it just holds the XML."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            return len(pdf.pages)
    except Exception:
        return None


def compress_docx_spacing(docx_path: str | Path, level: int, applicant_name: str = "") -> None:
    """Applies progressively more aggressive typography compression to a
    docx IN PLACE — margins and paragraph spacing at every level. Levels
    1-2 leave font sizes and paragraph count alone entirely; level 3
    (the last resort) also flattens every run past the header/body
    divider to 8.5pt and trims the cover letter's trailing blank
    paragraphs after the sign-off name. Never touches the actual
    wording of the letter/resume at any level, only layout.

    level: 1 (mild) to 3 (aggressive) -- each level is a STANDALONE
    aggressiveness, meant to be applied to a fresh copy of the original
    file, not called repeatedly on the same already-modified file with
    increasing level numbers. The caller (py_pipeline_assemble.py) tries
    level 1 first and stops at the lightest level that fits, but always
    against a fresh copy -- calling this repeatedly on the same mutated
    file compounds each level's reduction on top of the last, producing
    much more aggressive results than the level number alone suggests.
    """
    import docx
    from docx.shared import Inches, Pt

    _BLIP_TAG = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"

    doc = docx.Document(docx_path)

    # Scale relative to whatever margin is already there, with a floor —
    # fixed absolute targets would actually WIDEN margins on a template
    # that's already tighter than the target (confirmed: the real resume
    # this was built against already uses 0.5in margins).
    margin_scale = {1: 0.9, 2: 0.75, 3: 0.6}[level]
    min_margin = Inches(0.35)
    for section in doc.sections:
        for attr in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
            current = getattr(section, attr)
            scaled = Inches(current.inches * margin_scale)
            setattr(section, attr, scaled if scaled > min_margin else min_margin)

    spacing_scale = {1: 0.85, 2: 0.65, 3: 0.5}[level]
    line_spacing = {1: 1.0, 2: 0.95, 3: 0.9}[level]
    for para in iter_all_paragraphs(doc):
        # Real incident: the cover letter template's signature image sits
        # in its own paragraph. A line_spacing multiple below 1.0 shrinks
        # that paragraph's line box below the image's actual height, so
        # the image visually overlaps the "Best," line above it -- 13 of
        # 20 real generated cover letters had exactly this from a level-3
        # compression pass before this exemption existed (see
        # fix_signature_overlap.py, the one-off remediation for the ones
        # already generated, and py_post_pipeline_verify_materials.py's matching check).
        # Skip any paragraph containing an inline image entirely --
        # font/text paragraphs compress fine, an image doesn't reflow
        # with its container the same way.
        if any(run._element.findall(".//" + _BLIP_TAG) for run in para.runs):
            continue
        pf = para.paragraph_format
        if pf.space_after is not None:
            pf.space_after = Pt(pf.space_after.pt * spacing_scale)
        if pf.space_before is not None:
            pf.space_before = Pt(pf.space_before.pt * spacing_scale)
        pf.line_spacing = line_spacing

    if level >= 3:
        from docx.oxml.ns import qn

        def _has_bottom_border(p) -> bool:
            pPr = p._p.find(qn("w:pPr"))
            if pPr is None:
                return False
            pBdr = pPr.find(qn("w:pBdr"))
            return pBdr is not None and pBdr.find(qn("w:bottom")) is not None

        paras = doc.paragraphs

        # Both templates draw their header/body divider as a paragraph
        # bottom-border, not a real drawn shape (resume: under
        # "PROFESSIONAL SUMMARY"; cover letter: a blank rule paragraph
        # right after the contact line) -- last resort before giving up,
        # so a flat 8.5pt for everything past that first rule beats the
        # milder proportional shrink this level used before, including
        # on subsequent section-header borders further down.
        first_rule_idx = next((i for i, p in enumerate(paras) if _has_bottom_border(p)), None)
        if first_rule_idx is not None:
            for para in paras[first_rule_idx + 1:]:
                for run in para.runs:
                    run.font.size = Pt(8.5)

        # Trailing cleanup: the cover letter template's sign-off name
        # line is followed by 9+ blank paragraphs and a second, trailing
        # horizontal-rule paragraph that serve no purpose and just eat
        # page-fit budget. Only the LAST paragraph matching the
        # applicant's name is the sign-off -- the FIRST is the header
        # name line, and a document with only that one match (e.g. the
        # resume, which never repeats the name) is left alone rather
        # than risking deleting real content after a false single match.
        if applicant_name:
            name_idxs = [i for i, p in enumerate(paras) if p.text.strip() == applicant_name]
            if len(name_idxs) >= 2:
                for para in paras[name_idxs[-1] + 1:]:
                    para._p.getparent().remove(para._p)

    doc.save(docx_path)
