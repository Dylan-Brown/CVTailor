#!/usr/bin/env python3
"""
py_pipeline_reset.py — reset a live application's downstream state so
`run_pipeline.py --force` (or, if it's already past stage 6,
`py_pipeline_assemble.py --force`) picks it up fresh.

By default this resets everything past stage 0 (jd_input.processed.json
is renamed back to jd_input.json if it had been marked processed;
edit_brief.json, company_brief.json, candidate_edits.json,
verified_edits.json, verification_report.json, review_decisions.json,
and generated_materials/ are all cleared) — a requeue almost always means
"something changed, start this one over," and leaving old
review_decisions.json in place would sit there with edit_ids that
won't match anything freshly generated. Pass --keep-artifacts to skip
that and only undo the processed marker.

Never touches applications/archived/ — that's a separate, permanent
state (see py_pipeline_store_archive.py). Move it out of archived/ by
hand first if you genuinely want to revive one.

Usage:
    python src/controls/py_pipeline_reset.py --jd <app_id>
    python src/controls/py_pipeline_reset.py --all
    python src/controls/py_pipeline_reset.py --jd <app_id> --keep-artifacts
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
