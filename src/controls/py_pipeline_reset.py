#!/usr/bin/env python3
"""
py_pipeline_reset.py
====================
Resets an active application's downstream artifacts (Stages 1-9) so it can be re-run fresh. 
Never touches the permanent archive.

Usage:
    python src/controls/py_pipeline_reset.py --jd <app_id> [--keep-artifacts]
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import (
    APPLICATIONS_ROOT, NON_APPLICATION_DIR_NAMES, app_dirs, PROCESSED_JD_NAME, UNPROCESSED_JD_NAME,
)

_DOWNSTREAM_FILES = [
    "edit_brief.json", "company_brief.json", "candidate_edits.json",
    "verified_edits.json", "verification_report.json", "review_decisions.json",
    "discarded_edits.jsonl",
]


def requeue_one(app_dir: Path, keep_artifacts: bool) -> str:
    app_id = app_dir.name

    processed_path = app_dir / PROCESSED_JD_NAME
    if processed_path.is_file():
        processed_path.rename(app_dir / UNPROCESSED_JD_NAME)

    if not keep_artifacts:
        for name in _DOWNSTREAM_FILES:
            f = app_dir / name
            if f.is_file():
                f.unlink()
        materials_dir = app_dir / "generated_materials"
        if materials_dir.is_dir():
            shutil.rmtree(materials_dir)

    return app_id


def run(jd: str | None, all_: bool, keep_artifacts: bool):
    if not jd and not all_:
        print("Specify --jd <app_id> or --all.", file=sys.stderr)
        sys.exit(2)

    if all_:
        targets = app_dirs()
        if not targets:
            print("Nothing under applications/ to requeue.")
            return
    else:
        app_dir = APPLICATIONS_ROOT / jd
        if not app_dir.is_dir() or app_dir.name in NON_APPLICATION_DIR_NAMES:
            print(f"No live applications/{jd}/ folder found (or it's archived — "
                  f"requeue never touches applications/archived/).", file=sys.stderr)
            sys.exit(1)
        targets = [app_dir]

    for app_dir in targets:
        app_id = requeue_one(app_dir, keep_artifacts)
        note = "" if keep_artifacts else " (reset downstream artifacts and generated_materials/)"
        print(f"Requeued {app_id}{note}")

    print(f"\n{len(targets)} application(s) requeued. Run 'python run_pipeline.py --force' "
          f"to reprocess.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd", default=None, help="app_id to requeue")
    ap.add_argument("--all", action="store_true", help="Requeue every live application")
    ap.add_argument("--keep-artifacts", action="store_true",
                     help="Don't reset downstream artifacts or generated_materials/ -- just undo the processed marker")
    args = ap.parse_args()
    run(args.jd, args.all, args.keep_artifacts)
