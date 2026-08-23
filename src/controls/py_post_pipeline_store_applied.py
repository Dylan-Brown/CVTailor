#!/usr/bin/env python3
"""
py_post_pipeline_store_applied.py
=================================
Archives a fully processed application folder from the active queue into 
the permanent `applications/archive/` directory.

Matches folders by a provided prefix (case-insensitive). If multiple folders match, 
it prompts for clarification. The move is resumable; if interrupted by a locked file, 
it will safely retry on the next run.

Destinations (Defaults to `applied`):
    --cutoff    -> archive/cut_off/<app_id> (Prompts for an explanation note)
    --revisit   -> archive/revisit/<app_id>
    --test      -> archive/test/<app_id>

Usage:
    python src/controls/py_post_pipeline_store_applied.py valon_software_engineer [--cutoff | --revisit | --test]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import APPLICATIONS_ROOT, NON_APPLICATION_DIR_NAMES

JOBS_ROOT = Path(__file__).resolve().parent.parent.parent
PIPELINE_YAML = JOBS_ROOT / "pipeline.yaml"
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


def _application_progress_md_path() -> Path | None:
    """Reads pipeline.yaml's application_progress_md_path -- same
    minimal line-scan convention used for its other settings. None/
    missing/unset means the feature is off, not an error."""
    if not PIPELINE_YAML.is_file():
        return None
    for line in PIPELINE_YAML.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("application_progress_md_path:"):
            value = stripped.split(":", 1)[1].strip()
            return None if value.lower() in ("none", "") else Path(value)
    return None


def _load_row_data(app_dir: Path) -> tuple[str, str, str]:
    """(role_title, company, resume_version_cell) for app_dir, read from
    jd_input.json/.processed.json and routing.json. Missing/unreadable
    routing.json just means an empty resume-version cell, not an error."""
    jd_path = app_dir / "jd_input.processed.json"
    if not jd_path.is_file():
        jd_path = app_dir / "jd_input.json"
    jd = json.loads(jd_path.read_text(encoding="utf-8")) if jd_path.is_file() else {}

    variant_cell = ""
    routing_path = app_dir / "routing.json"
    if routing_path.is_file():
        try:
            variant = json.loads(routing_path.read_text(encoding="utf-8")).get("resume_variant", "")
            if variant:
                variant_cell = f"{variant.replace(' ', '-')}-specific resume (CVTailor)"
        except (json.JSONDecodeError, OSError):
            pass

    return jd.get("role_title", ""), jd.get("company", ""), variant_cell


def update_application_progress_md(app_dir: Path):
    """Inserts one new row at the top of the "Job Applications" table
    (the first row right after its header separator) in the configured
    Obsidian note. Silently does nothing if the path isn't configured,
    the file doesn't exist, or the table can't be found -- this is a
    convenience mirror, never something that should block archiving."""
    md_path = _application_progress_md_path()
    if md_path is None or not md_path.is_file():
        return

    role_title, company, resume_cell = _load_row_data(app_dir)
    today = datetime.now()
    row = (f"| {today:%m-%d} | {role_title} | {company} | {resume_cell} | "
           f"- Initial application: {today.month}/{today.day} | "
           f'<font color="#FFFF00">Awaiting response</font> | |\n')

    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    header_idx = next((i for i, l in enumerate(lines) if l.strip() == "# Job Applications"), None)
    if header_idx is None:
        print(f"  (skipped Application Progress.md update -- no \"# Job Applications\" heading found)",
              file=sys.stderr)
        return
    sep_idx = next(
        (i for i in range(header_idx + 1, len(lines))
         if lines[i].lstrip().startswith("|") and re.fullmatch(r"[|:\-\s]+", lines[i].strip())),
        None,
    )
    if sep_idx is None:
        print(f"  (skipped Application Progress.md update -- no table found under that heading)",
              file=sys.stderr)
        return

    lines.insert(sep_idx + 1, row)
    md_path.write_text("".join(lines), encoding="utf-8")
    print(f"  Added row to Application Progress.md: {company} -- {role_title}")


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

    if kind == "applied":
        update_application_progress_md(dest)

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
