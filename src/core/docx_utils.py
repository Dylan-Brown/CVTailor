#!/usr/bin/env python3
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
    run-level formatting -- walks the runs spanning the match, writes
    into the first one, clears the rest. Returns False if not found."""
    runs = paragraph.runs
    full_text = "".join(r.text for r in runs)
    # Normalizes curly/straight quotes for the search only (1:1 substitution,
    # so indices don't shift) and slices the original text, not the normalized copy.
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
    nested inside table cells -- doc.paragraphs alone misses table-layout content."""
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from iter_all_paragraphs(cell)  # cell also has .paragraphs and .tables


def convert_docx_to_pdf(docx_path: str | Path, pdf_path: str | Path) -> bool:
    """Best-effort docx -> pdf conversion via MS Word COM automation
    (docx2pdf, Windows/Mac only). Returns False on any failure instead
    of raising, so callers can fall back to shipping the .docx."""
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
    """Applies progressively more aggressive layout compression to a docx
    IN PLACE (level 1-3). Each level is STANDALONE, meant to run against
    a fresh copy, not stacked on an already-compressed file."""
    import docx
    from docx.shared import Inches, Pt

    _BLIP_TAG = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"

    doc = docx.Document(docx_path)

    # Scale relative to whatever margin is already there, with a floor --
    # a fixed absolute target would WIDEN margins on an already-tight template.
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
        # Skip any paragraph containing an inline image -- a line_spacing
        # below 1.0 can shrink its line box below the image's real height,
        # causing visual overlap with the line above (see fix_signature_overlap.py).
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
        # bottom-border. Last resort: flat 8.5pt for everything past that first rule.
        first_rule_idx = next((i for i, p in enumerate(paras) if _has_bottom_border(p)), None)
        if first_rule_idx is not None:
            for para in paras[first_rule_idx + 1:]:
                for run in para.runs:
                    run.font.size = Pt(8.5)

        # Trims trailing blank paragraphs after the sign-off. Only the
        # LAST paragraph matching the applicant's name is the sign-off --
        # a single match (e.g. the resume, which never repeats the name) is left alone.
        if applicant_name:
            name_idxs = [i for i, p in enumerate(paras) if p.text.strip() == applicant_name]
            if len(name_idxs) >= 2:
                for para in paras[name_idxs[-1] + 1:]:
                    para._p.getparent().remove(para._p)

    doc.save(docx_path)
