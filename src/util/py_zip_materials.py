#!/usr/bin/env python3
"""
py_zip_materials.py — zips every generated document (resumes, cover
letters, follow-up-steps files) across all live applications/ into one
archive for quick review/sharing. Python rewrite of the old
zip_cover_letters.ps1 -- deliberately widened scope from "cover
letters only" to everything in generated_materials/, since that's
what the new name promises; pass --cover-letters-only to get the old
narrower behavior back.

Excludes applications/archived/ -- same reasoning as everywhere else
in the pipeline, archived apps are done and don't need re-bundling
every time.

Usage:
    python src/util/py_zip_materials.py
    python src/util/py_zip_materials.py --cover-letters-only
    python src/util/py_zip_materials.py --out materials_review.zip
"""
import argparse
import sys
import zipfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import APPLICATIONS_ROOT, app_dirs

DEFAULT_OUT = "materials_review.zip"


def collect_files(cover_letters_only: bool) -> list[Path]:
    pattern = "*Cover Letter.*" if cover_letters_only else "*"
    files = []
    for app_dir in app_dirs():
        materials_dir = app_dir / "generated_materials"
        if not materials_dir.is_dir():
            continue
        files.extend(p for p in materials_dir.glob(pattern) if p.is_file())
    return sorted(files)


def run(cover_letters_only: bool, out_name: str) -> None:
    files = collect_files(cover_letters_only)
    if not files:
        print("No generated materials found under applications/*/generated_materials/.")
        return

    out_path = Path(out_name)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            # app_id/filename inside the zip, so files from different
            # applications with the same suffix don't collide.
            app_id = f.parent.parent.name
            zf.write(f, arcname=f"{app_id}/{f.name}")

    print(f"Done: {out_path} ({len(files)} file(s) across "
          f"{len({f.parent.parent.name for f in files})} application(s))")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cover-letters-only", action="store_true",
                     help="Old zip_cover_letters.ps1 behavior -- cover letters only, not the full materials set.")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"Output zip filename (default: {DEFAULT_OUT})")
    args = ap.parse_args()
    run(args.cover_letters_only, args.out)
