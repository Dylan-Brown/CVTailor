#!/usr/bin/env python3
"""
py_applications_archive_cleanup.py
==================================
Reclaims disk space by deleting intermediate JSON artifacts from archived applications. 
Always preserves the final generated materials, the original job posting, and your review decisions.

Usage:
    python src/util/py_applications_archive_cleanup.py [--force]
"""
import argparse
import sys
from pathlib import Path

KEEP_EXACT = {
    "jd_input.json",
    "jd_input.processed.json",
    "review_decisions.json",
    "routing.json",
}
KEEP_DIRS = {"generated_materials"}

DEFAULT_SUBFOLDERS = ["applied", "test"]


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def plan_app_dir(app_dir: Path) -> tuple[list[Path], list[Path], int]:
    """Returns (keep, delete, bytes_to_reclaim) for one <app_id> folder."""
    keep, delete = [], []
    reclaimed = 0
    for item in app_dir.iterdir():
        if item.is_dir():
            if item.name in KEEP_DIRS:
                keep.append(item)
            else:
                # __pycache__, or anything else unexpected -- delete
                delete.append(item)
                reclaimed += _dir_size(item)
        else:
            if item.name in KEEP_EXACT:
                keep.append(item)
            else:
                delete.append(item)
                reclaimed += item.stat().st_size
    return keep, delete, reclaimed


def run(root: Path, subfolders: list[str], force: bool):
    if not root.is_dir():
        print(f"No {root}/ found — nothing to clean up.", file=sys.stderr)
        sys.exit(1)

    app_dirs = []
    empty_dirs = []
    for sub in subfolders:
        sub_dir = root / sub
        if sub_dir.is_dir():
            for d in sub_dir.iterdir():
                if not d.is_dir():
                    continue
                # Genuinely empty leftover folders (created during
                # experimentation, then abandoned) are real, recurring
                # clutter -- found several in a live tree -- distinct
                # from a folder that just has nothing WORTH keeping;
                # an empty dir has nothing to plan against at all.
                if not any(d.iterdir()):
                    empty_dirs.append(d)
                else:
                    app_dirs.append(d)

    if empty_dirs:
        print(f"\nEmpty folder(s) found ({len(empty_dirs)}) -- {'would remove' if not force else 'removing'}:")
        for d in sorted(empty_dirs):
            print(f"  {d.parent.name}/{d.name}")
            if force:
                d.rmdir()

    if not app_dirs:
        if not empty_dirs:
            print(f"No application folders found under {root}/{{{','.join(subfolders)}}}/.")
        return

    total_reclaimed = 0
    for app_dir in sorted(app_dirs):
        keep, delete, reclaimed = plan_app_dir(app_dir)
        total_reclaimed += reclaimed
        label = f"{app_dir.parent.name}/{app_dir.name}"
        print(f"\n{label} — {'would reclaim' if not force else 'reclaiming'} {_human_size(reclaimed)}")
        for k in sorted(keep):
            print(f"  keep:   {k.name}")
        for d in sorted(delete):
            print(f"  delete: {d.name}")
            if force:
                if d.is_dir():
                    import shutil
                    shutil.rmtree(d)
                else:
                    d.unlink()

    print(f"\n{'Would reclaim' if not force else 'Reclaimed'} {_human_size(total_reclaimed)} "
          f"across {len(app_dirs)} application folder(s).")
    if not force:
        print("This was a dry run — nothing was deleted. Re-run with --force to actually delete.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="applications/archive",
                     help="Folder containing the subfolders to clean (default: applications/archive)")
    ap.add_argument("--subfolders", nargs="+", default=DEFAULT_SUBFOLDERS,
                     help=f"Subfolders under --root to process (default: {DEFAULT_SUBFOLDERS})")
    ap.add_argument("--force", action="store_true",
                     help="Actually delete. Without this, only shows what would be deleted.")
    args = ap.parse_args()
    run(Path(args.root), args.subfolders, args.force)
