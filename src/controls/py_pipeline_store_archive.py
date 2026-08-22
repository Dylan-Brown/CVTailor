#!/usr/bin/env python3
"""
py_pipeline_store_archive.py — the "pack it up" operation: moves a
fully-done application (applications/<app_id>/, generated_materials/
and all) into applications/archived/<app_id>/.

Archived applications are never touched by py_pipeline_reset.py or
run_pipeline.py — this is for JDs you're truly finished with
(interviewed, rejected, withdrawn, sent and just done referencing) and
never want resurfacing.

Usage:
    python src/controls/py_pipeline_store_archive.py --all
    python src/controls/py_pipeline_store_archive.py --jd <app_id>
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import APPLICATIONS_ROOT, ARCHIVED_DIR, app_dirs, is_processed


def archive_one(app_id: str) -> Path:
    src = APPLICATIONS_ROOT / app_id
    if not src.is_dir():
        raise FileNotFoundError(f"No {APPLICATIONS_ROOT}/{app_id}/ folder found.")
    ARCHIVED_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVED_DIR / app_id
    if dest.exists():
        raise FileExistsError(f"{dest} already exists — already archived?")
    shutil.move(str(src), str(dest))
    return dest


def _is_done(app_dir: Path) -> bool:
    materials_dir = app_dir / "generated_materials"
    has_output = materials_dir.is_dir() and (any(materials_dir.glob("*.pdf")) or any(materials_dir.glob("*.docx")))
    return has_output or is_processed(app_dir)


def run(jd: str | None, all_: bool):
    if not jd and not all_:
        print("Specify --jd <app_id> or --all.", file=sys.stderr)
        sys.exit(2)

    if all_:
        targets = [d.name for d in app_dirs() if _is_done(d)]
        if not targets:
            print("Nothing fully done (has generated_materials/, or marked processed) to archive.")
            return
    else:
        app_dir = APPLICATIONS_ROOT / jd
        if not app_dir.is_dir():
            print(f"No applications/{jd}/ folder found.", file=sys.stderr)
            sys.exit(1)
        if not _is_done(app_dir):
            print(f"applications/{jd}/ doesn't look done yet (no generated_materials/, not "
                  f"marked processed) — archive anyway? Re-run with --force-incomplete if so.",
                  file=sys.stderr)
            if "--force-incomplete" not in sys.argv:
                sys.exit(1)
        targets = [jd]

    archived = []
    for app_id in targets:
        try:
            dest = archive_one(app_id)
            print(f"Archived {app_id} -> {dest}/")
            archived.append(app_id)
        except Exception as e:
            print(f"[FAILED] {app_id}: {e}", file=sys.stderr)

    if archived:
        print(f"\n{len(archived)} application(s) moved to {ARCHIVED_DIR}/ — permanent, "
              f"won't be touched by py_pipeline_reset.py or run_pipeline.py.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd", default=None, help="app_id to archive")
    ap.add_argument("--all", action="store_true", help="Archive every fully-done application")
    ap.add_argument("--force-incomplete", action="store_true",
                     help="Allow archiving a single --jd that isn't fully done yet")
    args = ap.parse_args()
    run(args.jd, args.all)
