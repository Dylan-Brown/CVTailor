#!/usr/bin/env python3
"""
py_post_pipeline_store_applied.py — you sent it, so archive it: moves
a live applications/<app_id>/ folder into applications/archive/applied/,
identified by a prefix of its name rather than the full app_id.

Usage:
    python src/controls/py_post_pipeline_store_applied.py valon
    python src/controls/py_post_pipeline_store_applied.py valon_software_engineer
    python src/controls/py_post_pipeline_store_applied.py valon --cutoff
    python src/controls/py_post_pipeline_store_applied.py valon --revisit
    python src/controls/py_post_pipeline_store_applied.py valon --test

Matches by prefix (case-insensitive) against folders directly under
applications/ (never touches archive/ or _shared/ -- those aren't
live applications). If more than one folder matches the prefix, does
NOT guess -- lists the matches and asks for something more specific,
since silently picking the wrong one would misfile a real application.

Warns (doesn't block) if the matched folder has no generated_materials/
yet -- that usually means nothing was actually sent, so marking it
"applied" is probably a mistake, but you may have a real reason (e.g.
you hand-sent a manually-edited copy and the folder's own materials/
got cleaned up since), so this doesn't stop the move.

Destination is picked by exactly one of these (default: applied):
    (none)      archive/applied/<app_id>
    --cutoff    archive/cut_off/<app_id> -- also creates (if not already
                present) a cutoff_explanation.txt inside, opened in
                Notepad so you can jot down why. The other three
                destinations don't get an explanation file.
    --revisit   archive/revisit/<app_id>
    --test      archive/test/<app_id>

The move is resumable: it copies file-by-file rather than one atomic
rename, so if a re-run finds the destination already partially
populated (e.g. a previous run got interrupted because a generated
file was open in another program, like a PDF open in Adobe), it just
fills in whatever's still missing and retries removing the source,
instead of bailing out with "already exists". Any file still locked
is reported and left in place -- close it and re-run to finish. This
means running the same prefix twice, or with a different-length
prefix that resolves to the same folder, is always safe.

Every successful move is appended to archive/apps_moved.log (created
empty the first time it's needed). The very first time that log is
created, it's backfilled with one entry per folder already sitting in
archive/{applied,cut_off,revisit,test}/, timestamped from each
folder's own filesystem mtime (the time it was created at its
destination, i.e. when it was moved there) rather than left blank --
so the log reflects full history from the start, not just moves made
after this feature existed.
"""
import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import APPLICATIONS_ROOT, NON_APPLICATION_DIR_NAMES

ARCHIVE_ROOT = APPLICATIONS_ROOT / "archive"
APPLIED_DIR = ARCHIVE_ROOT / "applied"
CUTOFF_DIR = ARCHIVE_ROOT / "cut_off"
REVISIT_DIR = ARCHIVE_ROOT / "revisit"
TEST_DIR = ARCHIVE_ROOT / "test"
LOG_PATH = ARCHIVE_ROOT / "apps_moved.log"

DEST_DIRS = {"applied": APPLIED_DIR, "cutoff": CUTOFF_DIR, "revisit": REVISIT_DIR, "test": TEST_DIR}
LOG_LABELS = {"applied": "applied", "cutoff": "cut_off", "revisit": "revisit", "test": "test"}


def find_matches(prefix: str) -> list[Path]:
    if not APPLICATIONS_ROOT.is_dir():
        return []
    prefix_lower = prefix.lower()
    return sorted(
        d for d in APPLICATIONS_ROOT.iterdir()
        if d.is_dir()
        and d.name not in NON_APPLICATION_DIR_NAMES
        and d.name.lower().startswith(prefix_lower)
    )


def find_already_archived(prefix: str) -> list[Path]:
    """Folders under any of the archive/ destinations matching prefix
    -- used to give a clear "already handled" message instead of a
    bare "not found" when a live folder's already been fully moved
    out."""
    prefix_lower = prefix.lower()
    matches = []
    for root in DEST_DIRS.values():
        if root.is_dir():
            matches.extend(
                d for d in root.iterdir()
                if d.is_dir() and d.name.lower().startswith(prefix_lower)
            )
    return sorted(matches)


def merge_copy(src: Path, dest: Path) -> list[Path]:
    """Copies everything from src into dest, creating dest's
    subdirectories as needed and overwriting any same-named file
    already there (cheap and safe -- src is always the source of
    truth mid-move). Skips (and reports) any file that fails to copy,
    e.g. because it's still open elsewhere, rather than aborting the
    whole operation."""
    failures = []
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dest / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(item, target)
        except OSError:
            failures.append(item)
    return failures


def cleanup_src(src: Path) -> list[Path]:
    """Removes everything under src (deepest paths first, so files
    are gone before their parent dirs), then src itself if it ended
    up empty. Skips (and reports) anything still locked instead of
    raising, so a single open file doesn't block cleanup of
    everything else."""
    failures = []
    for item in sorted(src.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if item.is_dir():
                item.rmdir()
            else:
                item.unlink()
        except OSError:
            failures.append(item)
    try:
        src.rmdir()
    except OSError:
        pass
    return failures


def ensure_log_backfilled():
    """Creates apps_moved.log the first time it's needed. If it
    doesn't exist yet, backfills it with every folder already present
    across the four archive/ destinations, ordered and timestamped by
    each folder's own mtime, so history isn't lost just because
    logging is new. A no-op once the log already exists -- this never
    re-scans on later runs."""
    if LOG_PATH.exists():
        return
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    entries = []
    for kind, root in DEST_DIRS.items():
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if d.is_dir():
                entries.append((datetime.fromtimestamp(d.stat().st_mtime), LOG_LABELS[kind], d.name))
    entries.sort(key=lambda e: e[0])
    with LOG_PATH.open("w", encoding="utf-8") as f:
        for ts, label, name in entries:
            f.write(f"{ts:%Y-%m-%d %H:%M:%S}  {label:<8} {name}\n")


def log_move(kind: str, name: str):
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {LOG_LABELS[kind]:<8} {name}\n")


def run(prefix: str, kind: str):
    ensure_log_backfilled()
    matches = find_matches(prefix)

    if not matches:
        already = find_already_archived(prefix)
        if already:
            print(f"Already archived: {', '.join(d.relative_to(APPLICATIONS_ROOT).as_posix() for d in already)}",
                  file=sys.stderr)
        else:
            print(f"No application folder found starting with {prefix!r} under {APPLICATIONS_ROOT}/.", file=sys.stderr)
        sys.exit(1)

    if len(matches) > 1:
        print(f"{prefix!r} matches {len(matches)} folders -- be more specific:", file=sys.stderr)
        for m in matches:
            print(f"  {m.name}", file=sys.stderr)
        sys.exit(1)

    src = matches[0]
    materials_dir = src / "generated_materials"
    has_output = materials_dir.is_dir() and (any(materials_dir.glob("*.pdf")) or any(materials_dir.glob("*.docx")))
    if not has_output:
        print(f"WARNING: {src.name}/ has no generated_materials/ (or it's empty) -- "
              f"this usually means nothing was actually assembled/sent for this application. "
              f"Moving it anyway, since you may have a real reason.", file=sys.stderr)

    dest_root = DEST_DIRS[kind]
    label = LOG_LABELS[kind].replace("_", " ")
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / src.name
    resuming = dest.exists()
    if resuming:
        print(f"{dest} already exists -- resuming an interrupted move.", file=sys.stderr)
    dest.mkdir(parents=True, exist_ok=True)

    copy_failures = merge_copy(src, dest)
    for f in copy_failures:
        print(f"  could not copy (still open elsewhere?): {f}", file=sys.stderr)

    remove_failures = cleanup_src(src)
    for f in remove_failures:
        print(f"  could not remove from source (still open elsewhere?): {f}", file=sys.stderr)

    if copy_failures or remove_failures:
        print(f"\nPartially marked {label}: {src.name} -> {dest} "
              f"({len(set(copy_failures) | set(remove_failures))} file(s) still locked). "
              f"Close the file(s) above and re-run to finish.", file=sys.stderr)
        sys.exit(1)

    print(f"Marked {label}: {src.name} -> {dest}")
    log_move(kind, src.name)

    if kind == "cutoff":
        explanation_path = dest / "cutoff_explanation.txt"
        if not explanation_path.exists():
            explanation_path.write_text("", encoding="utf-8")
        # Absolute path, not relative -- Windows 11's packaged/MSIX
        # Notepad resolves a relative argument against its own
        # sandboxed working directory rather than this process's cwd,
        # so it can't find the file and errors instead of opening it.
        subprocess.Popen(["notepad.exe", str(explanation_path.resolve())])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", help="Prefix (or full name) of the application folder to mark, e.g. 'valon'")
    dest_group = ap.add_mutually_exclusive_group()
    dest_group.add_argument("--cutoff", action="store_true",
                             help="Archive to archive/cut_off/ instead of archive/applied/, and open a "
                                  "cutoff_explanation.txt in Notepad")
    dest_group.add_argument("--revisit", action="store_true",
                             help="Archive to archive/revisit/ instead of archive/applied/")
    dest_group.add_argument("--test", action="store_true",
                             help="Archive to archive/test/ instead of archive/applied/")
    args = ap.parse_args()
    kind = "cutoff" if args.cutoff else "revisit" if args.revisit else "test" if args.test else "applied"
    run(args.prefix, kind)
