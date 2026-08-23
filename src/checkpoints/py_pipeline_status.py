#!/usr/bin/env python3
"""
py_pipeline_status.py
=====================
Reports the current pipeline stage for all active applications directly from the filesystem state.

Usage:
    python src/checkpoints/py_pipeline_status.py
"""
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import APPLICATIONS_ROOT, ARCHIVED_DIR, app_dirs, is_processed


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _effective_edits_file(app_dir: Path) -> Path:
    """critiqued_edits.json (stage 7's output) when it exists, else
    verified_edits.json (stage 6's) -- the SAME precedence
    src/stages/py_stage08_agentic_update_review.py's own edits_file_for()
    uses. Getting this wrong here was a real bug: an app whose stage 7
    critique cut every single edit stage 6 had passed still showed as
    "verified, run stage 8" even though stage 8 would report nothing
    waiting on review for it -- misleading, since there was nothing
    left to review."""
    critiqued = app_dir / "critiqued_edits.json"
    return critiqued if critiqued.is_file() else app_dir / "verified_edits.json"


def _effective_edit_counts(app_dir: Path) -> tuple[int, int] | None:
    """(passed, flagged) counts from whichever file is actually
    effective for review -- see _effective_edits_file. Returns None if
    that file doesn't exist or was written by --dry-run (a placeholder,
    not real state)."""
    path = _effective_edits_file(app_dir)
    if not path.is_file():
        return None
    data = _read_json(path)
    if data is None or data.get("dry_run", False):
        return None
    return len(data.get("passed", [])), len(data.get("flagged", []))


def run():
    shared_leak_warning = APPLICATIONS_ROOT / "_shared" / "style_profile_LEAK_WARNING.json"
    if shared_leak_warning.is_file():
        print("\n!!! style_profile.json was flagged for possible employer-specific content leakage !!!")
        print(f"    See {shared_leak_warning} — this affects every JD prepared since the last "
              f"stage 2 run. Review applications/_shared/style_profile.json before continuing.\n")

    raw_intake = sorted(p for p in APPLICATIONS_ROOT.iterdir() if p.is_file() and p.suffix.lower() == ".json") \
        if APPLICATIONS_ROOT.is_dir() else []
    archived = sorted(p for p in ARCHIVED_DIR.iterdir() if p.is_dir()) if ARCHIVED_DIR.is_dir() else []
    dirs = app_dirs()

    rows = []
    for app_dir in dirs:
        app_id = app_dir.name
        materials_dir = app_dir / "generated_materials"
        has_output = materials_dir.is_dir() and (any(materials_dir.glob("*.pdf")) or any(materials_dir.glob("*.docx")))
        counts = _effective_edit_counts(app_dir)

        if has_output or is_processed(app_dir):
            stage, action = "done", "-" if not archived else "python src/controls/py_pipeline_store_archive.py --jd " + app_id
        elif (app_dir / "review_decisions.json").is_file():
            stage, action = "reviewed", "python src/controls/py_pipeline_assemble.py"
        elif counts is not None and (counts[0] + counts[1]) == 0:
            # Effective edits file exists and is real, but critique (or
            # verify) left zero surviving edits -- NOT "verified, go
            # review", since there's nothing to review. See
            # _effective_edits_file's docstring for the real incident
            # this distinction fixes.
            stage, action = "critiqued: 0 edits survived", (
                f"check {app_dir}/critiqued_out.jsonl or candidate_edits.json -- "
                f"nothing left to review; requeue with --force to regenerate"
            )
        elif counts is not None:
            stage, action = "verified", (
                f"python src/stages/py_stage08_agentic_update_review.py --app-id {app_id}"
            )
        elif (app_dir / "candidate_edits.json").is_file():
            stage, action = "generated (unverified)", "python run_pipeline.py --force"
        elif (app_dir / "jd_input.json").is_file():
            stage, action = "intake only", "python run_pipeline.py"
        else:
            stage, action = "unknown", f"check {app_dir}/"
        rows.append((app_id, stage, action))

    rows.sort(key=lambda r: r[0])

    if not rows and not raw_intake and not archived:
        print("Nothing in the pipeline yet — drop a raw JD JSON straight into "
              f"{APPLICATIONS_ROOT}/, or capture one via the Chrome extension, "
              "then run run_pipeline.py.")
        return

    if rows:
        id_w = max(len(r[0]) for r in rows) + 2
        stage_w = max(len(r[1]) for r in rows) + 2
        print(f"\n{'app_id':<{id_w}}{'stage':<{stage_w}}next action")
        print("-" * (id_w + stage_w + 40))
        for app_id, stage, action in rows:
            print(f"{app_id:<{id_w}}{stage:<{stage_w}}{action}")

    print(f"\n{len(raw_intake)} raw JD file(s) waiting at {APPLICATIONS_ROOT}/ root "
          f"(will be organized into their own applications/ folders on the next "
          f"run_pipeline.py run), {len(rows)} live application folder(s), "
          f"{len(archived)} archived.")


if __name__ == "__main__":
    run()
