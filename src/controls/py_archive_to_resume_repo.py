#!/usr/bin/env python3
"""
py_archive_to_resume_repo.py
=============================
Mirrors a just-applied CVTailor application into the standalone resume/
cover-letter git history repo (E:\\Resumes-CoverLetters, GitHub:
ThesisW/Resumes-CoverLetters) as a `job/<date>-<slug>` branch, forked
from `main`'s current tip for that resume-variant track.

Called automatically by py_post_pipeline_store_applied.py right after an
application is archived with the "applied" outcome -- never blocks that
archiving on failure here, since this is a convenience mirror, not part
of the pipeline's own data. Can also be run by hand against an
already-archived app folder (e.g. to retry after a network failure).

What it does, given an archived app folder:
  1. Reads jd_input(.processed).json for company/role, routing.json for
     which resume variant (Backend / Full Stack / AI Solutions Architect)
     was used.
  2. If the live template in config/resume_template/ for that variant has
     changed since main's last recorded copy, commits that update to
     `main` first -- so `main` always reflects "what's current."
  3. Creates `job/<date>-<slug>` off main's tip (skips if that branch
     already exists -- idempotent against a retry), copies in the
     application's generated_materials/ (final resume + cover letter)
     and its pipeline metadata (jd_input.json, routing.json, etc., under
     a `pipeline/` subfolder), writes a NOTE.md, commits.
  4. Pushes main (if updated) and the new job branch to GitHub.

Matches the shape build_resume_git_history.py already established for
historical branches (same job/<date>-<slug> naming, same NOTE.md
convention, same pipeline/ subfolder for metadata) so `git log`/`git
branch -a` reads as one consistent history regardless of which script
added a given branch.

Usage:
    Imported: from py_archive_to_resume_repo import archive_application
              archive_application(app_dir)
    Standalone: python src/controls/py_archive_to_resume_repo.py <app_id_or_path>
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import APPLICATIONS_ROOT, find_jd_input  # noqa: E402

JOBS_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_RESUME_TEMPLATE_DIR = JOBS_ROOT / "config" / "resume_template"

RESUME_REPO = Path(r"E:\Resumes-CoverLetters")

AUTHOR_NAME = "Dylan Brown"
AUTHOR_EMAIL = "dylbr@proton.me"

# routing.json's "resume_variant" -> (track folder in the repo, live/main filename).
# Matches py_stage09_assemble_materials.py's own variant_display fallback:
# unrecognized/missing variant defaults to Backend.
VARIANT_TRACKS = {
    "Backend": ("Backend", "Dylan Brown Resume Backend 2026.docx"),
    "Full Stack": ("Full-Stack", "Dylan Brown Resume Full Stack 2026.docx"),
    "AI Solutions Architect": ("AI-Solutions-Architect", "Dylan Brown Resume AI Solutions Architect 2026.docx"),
}
DEFAULT_VARIANT = "Backend"

FOLDER_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})$")


class ArchiveSkipped(Exception):
    """Raised for expected no-ops (branch already exists, repo missing) --
    caught and printed as a warning, never lets a caller treat this as a
    hard failure."""


def _run_git(args, date_iso=None, check=True):
    env = os.environ.copy()
    if date_iso:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{date_iso}T12:00:00"
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = AUTHOR_NAME
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = AUTHOR_EMAIL
    result = subprocess.run(["git", "-C", str(RESUME_REPO)] + args,
                             env=env, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result


def _branch_exists(branch: str) -> bool:
    local = _run_git(["rev-parse", "--verify", "--quiet", branch], check=False)
    if local.returncode == 0:
        return True
    remote = _run_git(["rev-parse", "--verify", "--quiet", f"origin/{branch}"], check=False)
    return remote.returncode == 0


def _sync_main():
    _run_git(["checkout", "main"])
    _run_git(["fetch", "origin"], check=False)
    ff = _run_git(["merge", "--ff-only", "origin/main"], check=False)
    if ff.returncode != 0:
        raise RuntimeError(
            f"local main and origin/main have diverged -- resolve by hand in {RESUME_REPO} "
            f"before archiving more applications:\n{ff.stdout}\n{ff.stderr}"
        )


def _maybe_update_main_template(variant: str, today_iso: str) -> bool:
    """If the live CVTailor template for this variant differs from what's
    committed on main, copies it in and commits. Returns True if main got
    a new commit (caller needs to push it)."""
    track_dir, filename = VARIANT_TRACKS[variant]
    live_template = CONFIG_RESUME_TEMPLATE_DIR / filename
    if not live_template.is_file():
        print(f"  (warning: live template not found at {live_template}, skipping drift check)",
              file=sys.stderr)
        return False

    repo_path = RESUME_REPO / track_dir / filename
    if repo_path.is_file() and repo_path.read_bytes() == live_template.read_bytes():
        return False

    repo_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(live_template, repo_path)
    _run_git(["add", str(repo_path.relative_to(RESUME_REPO))])
    _run_git(["commit", "-m", f"{track_dir.replace('-', ' ')}: template update (auto-synced from CVTailor)"],
              date_iso=today_iso)
    print(f"  main: committed updated {track_dir} template")
    return True


def _load_app_context(app_dir: Path) -> dict:
    jd_path = find_jd_input(app_dir)
    jd = json.loads(jd_path.read_text(encoding="utf-8")) if jd_path else {}

    variant = DEFAULT_VARIANT
    routing_path = app_dir / "routing.json"
    if routing_path.is_file():
        try:
            candidate = json.loads(routing_path.read_text(encoding="utf-8")).get("resume_variant")
            if candidate in VARIANT_TRACKS:
                variant = candidate
            elif candidate:
                print(f"  (warning: unrecognized resume_variant {candidate!r} in routing.json, "
                      f"defaulting to {DEFAULT_VARIANT})", file=sys.stderr)
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "company": jd.get("company", "Unknown Company"),
        "role_title": jd.get("role_title", ""),
        "variant": variant,
    }


def _collect_files(app_dir: Path, dest_base: str) -> dict[str, Path]:
    """{dest_path_in_repo: src_path} -- generated_materials/ contents go
    directly under dest_base/ (matching the historical convention),
    pipeline metadata (jd_input*.json, routing.json, review_decisions.json,
    verified_edits.json) goes under dest_base/pipeline/."""
    files = {}
    materials_dir = app_dir / "generated_materials"
    if materials_dir.is_dir():
        for f in materials_dir.iterdir():
            if f.is_file() and not f.name.startswith(("~$", ".~lock.")):
                files[f"{dest_base}/{f.name}"] = f

    for name in ("jd_input.json", "jd_input.processed.json", "routing.json",
                 "review_decisions.json", "verified_edits.json"):
        f = app_dir / name
        if f.is_file():
            files[f"{dest_base}/pipeline/{name}"] = f

    return files


def archive_application(app_dir: Path, push: bool = True) -> None:
    """Entry point called from py_post_pipeline_store_applied.py. Prints
    warnings and returns (never raises) on any expected/soft failure --
    archiving the real application must never be blocked by this mirror
    step. Raises only on a programming error worth surfacing loudly."""
    app_dir = Path(app_dir)
    try:
        _archive_application(app_dir, push=push)
    except ArchiveSkipped as e:
        print(f"  (resume-repo mirror skipped: {e})", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 -- convenience mirror, never fatal to the caller
        print(f"  (resume-repo mirror failed, continuing anyway: {e})", file=sys.stderr)


def _archive_application(app_dir: Path, push: bool = True) -> None:
    if not (RESUME_REPO / ".git").is_dir():
        raise ArchiveSkipped(f"{RESUME_REPO} is not a git repo")

    app_id = app_dir.name
    m = FOLDER_DATE_RE.search(app_id)
    if m:
        date_iso = m.group(1)
        slug = app_id[: m.start()]
    else:
        from datetime import date
        date_iso = date.today().isoformat()
        slug = app_id
        print(f"  (warning: {app_id!r} has no trailing _YYYY-MM-DD, using today's date)",
              file=sys.stderr)

    branch = f"job/{date_iso}-{slug}"
    ctx = _load_app_context(app_dir)
    company_title = ctx["company"]

    _sync_main()

    if _branch_exists(branch):
        raise ArchiveSkipped(f"{branch} already exists -- not re-archiving")

    from datetime import date as _date
    today_iso = _date.today().isoformat()
    main_updated = _maybe_update_main_template(ctx["variant"], today_iso)
    if main_updated and push:
        _run_git(["push", "origin", "main"])

    fork_sha = _run_git(["rev-parse", "main"]).stdout.strip()
    _run_git(["checkout", "-b", branch, "main"])

    note = (
        f"# {company_title}\n\n"
        f"Application date: {date_iso}\n"
        f"Date source: live capture (py_archive_to_resume_repo.py, at submission time)\n"
        f"Resume variant: {ctx['variant']}\n"
        f"Forked from main @ {fork_sha[:10]} "
        f"(the template state current as of this application).\n"
    )
    note_path = RESUME_REPO / "NOTE.md"
    note_path.write_text(note, encoding="utf-8")
    _run_git(["add", "NOTE.md"])
    _run_git(["commit", "-m", "Application notes"], date_iso=date_iso)

    files = _collect_files(app_dir, slug)
    if not files:
        print(f"  (warning: no generated_materials/ or pipeline metadata found for {app_id})",
              file=sys.stderr)
    for dest_rel, src in files.items():
        dest = RESUME_REPO / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        _run_git(["add", dest_rel])
    if files:
        _run_git(["commit", "-m", f"Tailored resume/cover letter for {company_title}"], date_iso=date_iso)

    if push:
        _run_git(["push", "-u", "origin", branch])

    _run_git(["checkout", "main"])
    print(f"  resume-repo: archived {app_id} as {branch}"
          + (" (pushed)" if push else " (local only, not pushed)"))


def _resolve_app_dir(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        return p
    candidates = sorted(
        d for d in (APPLICATIONS_ROOT / "archive" / "applied").glob(f"{arg}*") if d.is_dir()
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        sys.exit(f"No archived application matching {arg!r} under applications/archive/applied/.")
    sys.exit(f"{arg!r} matches {len(candidates)} folders -- be more specific:\n"
              + "\n".join(f"  {c.name}" for c in candidates))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("app", help="Full path to an archived app folder, or an app_id prefix under "
                                 "applications/archive/applied/")
    ap.add_argument("--no-push", action="store_true", help="Commit locally only, skip the GitHub push.")
    args = ap.parse_args()
    archive_application(_resolve_app_dir(args.app), push=not args.no_push)
