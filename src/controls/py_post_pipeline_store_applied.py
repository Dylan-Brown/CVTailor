#!/usr/bin/env python3
"""
py_post_pipeline_store_applied.py
=================================
Records the outcome of a fully processed application and archives its
folder out of the active queue into `applications/archive/<outcome>/`.

Absorbs what used to be a separate py_pipeline_store_archive.py -- that
script did the exact same "move a fully-done application folder"
operation, just without recording WHICH outcome, into a different
(`applications/archived/`, no per-outcome split) destination. The two
existed side by side largely by accident of history; once every
outcome is representable here (including a generic "done, no further
categorization" one via --done), there was nothing left for a separate
script to do. Both destinations are equally permanent -- neither is
ever touched again by py_pipeline_reset.py or run_pipeline.py once an
application lands there, so unifying them was safe: nothing downstream
depended on the two being different scripts or different folders.

Matches folders by a case-insensitive prefix. If multiple folders
match, prompts for clarification rather than guessing. The move is
resumable -- if interrupted by a locked file, re-running picks up
where it left off.

Outcomes (default: applied):
    (none)      archive/applied/<app_id>   -- adds a row to
                Application Progress.md if pipeline.yaml's
                application_progress_md_path is configured
    --cutoff    archive/cut_off/<app_id>   -- opens a
                cutoff_explanation.txt in Notepad (single-app mode only;
                --all writes an empty one per app instead of opening N
                Notepad windows)
    --revisit   archive/revisit/<app_id>
    --test      archive/test/<app_id>
    --done      archive/done/<app_id>      -- fully finished, nothing
                further to record; no Application Progress.md row
    --custom NAME
                archive/custom/NAME/<app_id> -- NAME is any directory
                name you choose (no Application Progress.md row).
                Unlike every other outcome, this one COPIES rather
                than moves: the live folder under applications/ is
                left in place so you can re-run the whole pipeline
                against it later (e.g. after switching the local
                model), while archive/custom/NAME/ keeps a snapshot
                of what was generated this time.

Usage:
    python src/controls/py_post_pipeline_store_applied.py valon_software_engineer [--cutoff | --revisit | --test | --done | --custom NAME]
    python src/controls/py_post_pipeline_store_applied.py --all [--cutoff | --revisit | --test | --done | --custom NAME]
        Applies to every live application under applications/ instead
        of one matched by prefix. Lists every application that would
        be affected and requires typing "yes" to confirm before
        touching anything -- bulk-declaring every open application
        "applied" (or any other outcome) is exactly the kind of
        one-shot action that shouldn't happen from a stray Enter key.
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
from batch_common import APPLICATIONS_ROOT, NON_APPLICATION_DIR_NAMES, app_dirs

JOBS_ROOT = Path(__file__).resolve().parent.parent.parent
PIPELINE_YAML = JOBS_ROOT / "pipeline.yaml"
ARCHIVE_ROOT = APPLICATIONS_ROOT / "archive"
APPLIED_DIR = ARCHIVE_ROOT / "applied"
CUTOFF_DIR = ARCHIVE_ROOT / "cut_off"
REVISIT_DIR = ARCHIVE_ROOT / "revisit"
TEST_DIR = ARCHIVE_ROOT / "test"
DONE_DIR = ARCHIVE_ROOT / "done"
CUSTOM_ROOT = ARCHIVE_ROOT / "custom"
LOG_PATH = ARCHIVE_ROOT / "apps_moved.log"

DEST_DIRS = {"applied": APPLIED_DIR, "cutoff": CUTOFF_DIR, "revisit": REVISIT_DIR, "test": TEST_DIR, "done": DONE_DIR}
LOG_LABELS = {"applied": "applied", "cutoff": "cut_off", "revisit": "revisit", "test": "test", "done": "done"}


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
    if CUSTOM_ROOT.is_dir():
        for custom_dir in CUSTOM_ROOT.iterdir():
            if custom_dir.is_dir():
                matches.extend(
                    d for d in custom_dir.iterdir()
                    if d.is_dir() and d.name.lower().startswith(prefix_lower)
                )
    return sorted(matches)


def _has_output(app_dir: Path) -> bool:
    materials_dir = app_dir / "generated_materials"
    return materials_dir.is_dir() and (any(materials_dir.glob("*.pdf")) or any(materials_dir.glob("*.docx")))


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
    across the archive/ destinations, ordered and timestamped by each
    folder's own mtime, so history isn't lost just because logging is
    new. A no-op once the log already exists -- this never re-scans on
    later runs."""
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
    if CUSTOM_ROOT.is_dir():
        for custom_dir in CUSTOM_ROOT.iterdir():
            if not custom_dir.is_dir():
                continue
            for d in custom_dir.iterdir():
                if d.is_dir():
                    entries.append((datetime.fromtimestamp(d.stat().st_mtime),
                                     f"custom/{custom_dir.name}", d.name))
    entries.sort(key=lambda e: e[0])
    with LOG_PATH.open("w", encoding="utf-8") as f:
        for ts, label, name in entries:
            f.write(f"{ts:%Y-%m-%d %H:%M:%S}  {label:<8} {name}\n")


def log_move(label: str, name: str):
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {label:<8} {name}\n")


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


def _resolve_dest(kind: str, custom_name: str | None) -> tuple[Path, str]:
    """(dest_root, label) for kind -- the fixed archive/<outcome>/ roots
    for every built-in kind, or archive/custom/<custom_name>/ when kind
    is "custom". Kept out of DEST_DIRS/LOG_LABELS since those are fixed
    at import time and custom_name is only known once argparse runs."""
    if kind == "custom":
        assert custom_name, "kind == 'custom' requires a custom_name"
        return CUSTOM_ROOT / custom_name, f"custom/{custom_name}"
    return DEST_DIRS[kind], LOG_LABELS[kind].replace("_", " ")


def _move_one(src: Path, kind: str, dest_root: Path, label: str, open_notepad: bool = True) -> Path:
    """Does the actual move for one application folder -- copy,
    cleanup, log, and kind-specific side effects (Application
    Progress.md row for applied, explanation file for cutoff). Shared
    by both the single-app path and --all's bulk path; open_notepad is
    False in bulk mode so marking dozens of applications cutoff
    doesn't pop open dozens of Notepad windows -- each still gets an
    empty cutoff_explanation.txt to fill in by hand.

    Raises RuntimeError (not sys.exit) on a partial failure -- lets
    --all's bulk loop report one failed app and keep going instead of
    the whole batch dying on the first locked file.

    kind == "custom" is copy-only: src is left exactly as-is under
    applications/ (never cleaned up) specifically so the whole pipeline
    can be re-run against it later -- e.g. after switching which local
    model is loaded -- while archive/custom/NAME/ keeps a snapshot of
    what was generated this time."""
    copy_only = kind == "custom"
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / src.name
    resuming = dest.exists()
    if resuming:
        verb = "copy" if copy_only else "move"
        print(f"{dest} already exists -- resuming an interrupted {verb}.", file=sys.stderr)
    dest.mkdir(parents=True, exist_ok=True)

    copy_failures = merge_copy(src, dest)
    for f in copy_failures:
        print(f"  could not copy (still open elsewhere?): {f}", file=sys.stderr)

    remove_failures = [] if copy_only else cleanup_src(src)
    for f in remove_failures:
        print(f"  could not remove from source (still open elsewhere?): {f}", file=sys.stderr)

    if copy_failures or remove_failures:
        verb = "copied" if copy_only else "marked"
        raise RuntimeError(
            f"partially {verb} {label}: {src.name} -> {dest} "
            f"({len(set(copy_failures) | set(remove_failures))} file(s) still locked) -- "
            f"close the file(s) above and re-run to finish."
        )

    action = "Copied" if copy_only else "Marked"
    print(f"{action} {label}: {src.name} -> {dest}"
          + ("  (original left in place under applications/ -- re-run the pipeline on it "
             "any time)" if copy_only else ""))
    log_move(label, src.name)

    if kind == "applied":
        update_application_progress_md(dest)

    if kind == "cutoff":
        explanation_path = dest / "cutoff_explanation.txt"
        if not explanation_path.exists():
            explanation_path.write_text("", encoding="utf-8")
        if open_notepad:
            # Absolute path, not relative -- Windows 11's packaged/MSIX
            # Notepad resolves a relative argument against its own
            # sandboxed working directory rather than this process's
            # cwd, so it can't find the file and errors instead of
            # opening it.
            subprocess.Popen(["notepad.exe", str(explanation_path.resolve())])

    return dest


def run(prefix: str, kind: str, custom_name: str | None = None):
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
    verb = "Copying" if kind == "custom" else "Moving"
    if not _has_output(src):
        print(f"WARNING: {src.name}/ has no generated_materials/ (or it's empty) -- "
              f"this usually means nothing was actually assembled/sent for this application. "
              f"{verb} it anyway, since you may have a real reason.", file=sys.stderr)

    dest_root, label = _resolve_dest(kind, custom_name)
    try:
        _move_one(src, kind, dest_root, label)
    except RuntimeError as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(1)


def run_all(kind: str, custom_name: str | None = None):
    ensure_log_backfilled()
    targets = app_dirs()
    if not targets:
        print("No live applications under applications/ to mark.")
        return

    dest_root, label = _resolve_dest(kind, custom_name)
    no_output = {d.name for d in targets if not _has_output(d)}

    verb = "copy" if kind == "custom" else "mark"
    print(f"\nThis will {verb} ALL {len(targets)} live application(s) {label!r}:")
    for d in targets:
        flag = "  (no generated_materials/ yet)" if d.name in no_output else ""
        print(f"  {d.name}{flag}")
    if no_output:
        print(f"\n{len(no_output)} of these have no generated output -- usually means nothing was "
              f"actually assembled/sent for them. They'll be marked {label!r} anyway if you confirm.")

    confirm = input(f"\nType 'yes' to mark all {len(targets)} application(s) {label!r}: ").strip().lower()
    if confirm != "yes":
        print("Aborted -- nothing changed.")
        return

    marked = []
    for app_dir in targets:
        try:
            _move_one(app_dir, kind, dest_root, label, open_notepad=False)
            marked.append(app_dir.name)
        except RuntimeError as e:
            print(f"[FAILED] {app_dir.name}: {e}", file=sys.stderr)

    print(f"\n{len(marked)}/{len(targets)} marked {label!r}.")
    if kind == "cutoff" and marked:
        print(f"Each got an empty cutoff_explanation.txt under archive/cut_off/<app_id>/ -- fill "
              f"those in by hand (skipped auto-opening {len(marked)} Notepad windows in bulk mode).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", nargs="?", default=None,
                     help="Prefix (or full name) of the application folder to mark, e.g. 'valon'. "
                          "Omit when using --all.")
    dest_group = ap.add_mutually_exclusive_group()
    dest_group.add_argument("--cutoff", action="store_true",
                             help="Archive to archive/cut_off/ instead of archive/applied/, and open a "
                                  "cutoff_explanation.txt in Notepad (single-app mode only)")
    dest_group.add_argument("--revisit", action="store_true",
                             help="Archive to archive/revisit/ instead of archive/applied/")
    dest_group.add_argument("--test", action="store_true",
                             help="Archive to archive/test/ instead of archive/applied/")
    dest_group.add_argument("--done", action="store_true",
                             help="Archive to archive/done/ instead of archive/applied/ -- fully "
                                  "finished, nothing further to record (no Application Progress.md "
                                  "row, unlike --applied)")
    dest_group.add_argument("--custom", metavar="NAME", default=None,
                             help="Archive to archive/custom/NAME/ instead of archive/applied/, "
                                  "where NAME is any directory name you choose")
    ap.add_argument("--all", action="store_true",
                     help="Apply to every live application instead of one matched by prefix. Lists "
                          "everything that would be affected and requires typing 'yes' to confirm "
                          "before touching anything.")
    args = ap.parse_args()

    if args.all and args.prefix:
        ap.error("--all and a prefix are mutually exclusive -- --all applies to every live application.")
    if not args.all and not args.prefix:
        ap.error("provide a prefix, or pass --all to apply to every live application.")

    custom_name = None
    if args.custom is not None:
        custom_name = args.custom.strip()
        if not custom_name:
            ap.error("--custom requires a non-empty directory name.")
        if any(c in custom_name for c in ("/", "\\")) or custom_name in (".", ".."):
            ap.error(f"--custom {args.custom!r} isn't a valid single directory name.")

    kind = ("cutoff" if args.cutoff else "revisit" if args.revisit else "test" if args.test
            else "done" if args.done else "custom" if custom_name else "applied")

    if args.all:
        run_all(kind, custom_name)
    else:
        run(args.prefix, kind, custom_name)
