#!/usr/bin/env python3
"""
py_fix_signature_overlap.py
===========================
One-off remediation script for legacy cover letters.

Fixes an issue where progressive typography compression shrank line spacing 
around the signature image, causing visual overlap. It resets the spacing 
strictly for the three paragraphs surrounding the signature block without 
affecting the rest of the document's compression.

Usage (Run from jobs/ root):
    python src/util/py_fix_signature_overlap.py [--dry-run]
"""
import argparse
import glob
import sys
from pathlib import Path

import docx
from docx.shared import Pt

_BLIP_TAG = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_PRISTINE_SPACE = Pt(12)  # matches the template's own space_before/space_after


def _has_image(paragraph) -> bool:
    return any(run._element.findall(".//" + _BLIP_TAG) for run in paragraph.runs)


def fix_one(docx_path: Path, dry_run: bool) -> bool:
    """Returns True if this file needed (and got, unless dry_run) a fix."""
    doc = docx.Document(docx_path)
    paras = doc.paragraphs
    img_idx = next((i for i, p in enumerate(paras) if _has_image(p)), None)
    if img_idx is None:
        return False  # no signature image in this doc -- nothing to fix

    changed = False
    for i in (img_idx - 1, img_idx, img_idx + 1):
        if i < 0 or i >= len(paras):
            continue
        pf = paras[i].paragraph_format
        if pf.line_spacing != 1.0:
            changed = True
            if not dry_run:
                pf.line_spacing = 1.0
        if pf.space_before is None or abs(pf.space_before.pt - 12) > 0.5:
            changed = True
            if not dry_run:
                pf.space_before = _PRISTINE_SPACE
        if pf.space_after is None or abs(pf.space_after.pt - 12) > 0.5:
            changed = True
            if not dry_run:
                pf.space_after = _PRISTINE_SPACE

    if changed and not dry_run:
        doc.save(docx_path)
    return changed


def run(dry_run: bool):
    paths = sorted(Path(p) for p in glob.glob("applications/*/generated_materials/*Cover Letter.docx"))
    if not paths:
        print("No cover letter .docx files found under applications/*/generated_materials/.")
        return

    fixed = []
    for path in paths:
        try:
            if fix_one(path, dry_run):
                fixed.append(path)
        except Exception as e:
            print(f"[FAILED] {path}: {e}", file=sys.stderr)

    verb = "Would fix" if dry_run else "Fixed"
    print(f"{verb} {len(fixed)}/{len(paths)} cover letter(s):")
    for p in fixed:
        print(f"  {p}")
    if fixed and not dry_run:
        print("\nRegenerate PDFs for these with:")
        print("  python src/controls/py_pipeline_assemble.py --app-id " + ",".join(p.parts[1] for p in fixed))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Report what would change without writing anything")
    args = ap.parse_args()
    run(args.dry_run)
