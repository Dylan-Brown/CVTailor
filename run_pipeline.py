#!/usr/bin/env python3
"""
run_pipeline.py
===============
The primary entrypoint for executing the CVTailor pipeline.

Modes:
  1. BATCH MODE (Default): Processes all pending applications in the `applications/` 
     directory through Stages 0-7. Stops before Stage 8 (manual review) unless 
     the `--full` flag is provided (which runs end-to-end using the configured AI Agent).
  2. SINGLE-JD MODE (`--jd-json`): Intakes a brand new job description from a file path 
     and runs it through Stages 0-7 in one shot.

Usage:
    python run_pipeline.py [--app <id_or_prefix>] [--force] [--dry-run] [--provider claude]
    python run_pipeline.py --full
    python run_pipeline.py --jd-json path/to/jd.json --resume path/to/resume.pdf ...
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

JOBS_ROOT = Path(__file__).resolve().parent
sys.path.append(str(JOBS_ROOT / "src" / "core"))
from batch_common import (
    APPLICATIONS_ROOT, app_dirs, find_jd_input, is_processed,
    ensure_variant_ingest, pick_ledger_for_jd, safe_id_from_filename,
)
from py_run_stage import sh, sh_capture, sh_optional, stage_script
from llm_dispatch import default_provider
from io_utils import load_model
from schemas import JDInput

STAGE8_DISPATCHER = JOBS_ROOT / "src" / "ai_wrappers" / "py_stage08_reviewer_interface.py"
ASSEMBLE_SCRIPT = JOBS_ROOT / "src" / "controls" / "py_pipeline_assemble.py"
PIPELINE_YAML = JOBS_ROOT / "pipeline.yaml"


def _stage1_enabled() -> bool:
    """Reads pipeline.yaml's run_stage1_scoring (Yes/No, default No) --
    same minimal line-scan py_stage08_reviewer_interface.py already uses
    for its own pipeline.yaml keys; not worth a YAML dependency for one
    more setting. Stage 1 stays runnable by hand regardless of this --
    it only gates whether run_single_jd() launches it automatically."""
    if not PIPELINE_YAML.is_file():
        return False
    for line in PIPELINE_YAML.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("run_stage1_scoring:"):
            return stripped.split(":", 1)[1].strip().strip("'\"").lower() == "yes"
    return False
OPEN_URLS_SCRIPT = JOBS_ROOT / "src" / "controls" / "py_post_pipeline_open_app_urls.py"
APPLICATIONS_STATS_SCRIPT = JOBS_ROOT / "src" / "util" / "py_applications_stats.py"


def drain_raw_intake() -> list[str]:
    """Finds every raw (not-yet-validated) JD JSON sitting under
    applications/ and runs stage 0 on it, turning it into a real,
    canonical jd_input.json. Two cases:

    1. A stray *.json sitting directly at applications/ root (not in
       any subfolder) -- gets a fresh applications/<name>/ subfolder
       created for it (name derived from the file), stage 0 runs with
       that as --app-id, then the root-level file is deleted.
    2. An existing applications/<name>/ subfolder that has some *.json
       file but no jd_input.json yet (you pre-made the folder and
       dropped a raw export inside it) -- stage 0 runs in place using
       the folder's own name as --app-id, then the raw file is
       deleted. Ambiguous cases (2+ json files, no jd_input.json yet)
       are skipped with a warning rather than guessed at.

    Either way the raw file's content lives on as that folder's
    jd_input.json, so nothing is lost by deleting the raw copy.
    Returns the list of app_ids created/updated. A JD failing stage 0
    (malformed JSON, fails the JDInput contract) is left in place
    rather than deleted, so you don't lose track of it.
    """
    if not APPLICATIONS_ROOT.is_dir():
        return []
    drained = []

    root_files = sorted(p for p in APPLICATIONS_ROOT.iterdir() if p.is_file() and p.suffix.lower() == ".json")
    for raw_file in root_files:
        app_id = safe_id_from_filename(raw_file)
        try:
            sh([sys.executable, stage_script(0),
                "--jd-json", str(raw_file),
                "--app-id", app_id,
                "--applications-root", str(APPLICATIONS_ROOT)])
            raw_file.unlink()
            drained.append(app_id)
        except Exception as e:
            print(f"[FAILED] {raw_file.name}: {e} -- left at {APPLICATIONS_ROOT}/, not deleted.",
                  file=sys.stderr)

    for d in app_dirs():
        if find_jd_input(d) is not None:
            continue
        raw_files = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".json")
        if not raw_files:
            continue
        if len(raw_files) > 1:
            print(f"[skip] {d.name}/: {len(raw_files)} JSON files present and no jd_input.json "
                  f"yet -- ambiguous which is the JD, leaving alone: "
                  f"{[p.name for p in raw_files]}", file=sys.stderr)
            continue
        raw_file = raw_files[0]
        try:
            sh([sys.executable, stage_script(0),
                "--jd-json", str(raw_file),
                "--app-id", d.name,
                "--applications-root", str(APPLICATIONS_ROOT)])
            raw_file.unlink()
            drained.append(d.name)
        except Exception as e:
            print(f"[FAILED] {d.name}/{raw_file.name}: {e} -- left in place, not deleted.",
                  file=sys.stderr)

    if drained:
        print(f"Drained {len(drained)} raw JD file(s) into applications/: {', '.join(drained)}")
    return drained


def _is_really_verified(app_dir: Path) -> bool:
    """True only if verified_edits.json exists AND wasn't written by a
    --dry-run pass. Dry-run deliberately writes real placeholder files
    to this exact path so downstream stages can smoke-test wiring
    without spending anything -- a plain existence check can't tell
    "really prepared" apart from "someone ran --dry-run first"."""
    p = app_dir / "verified_edits.json"
    if not p.is_file():
        return False
    try:
        return not json.loads(p.read_text(encoding="utf-8")).get("dry_run", False)
    except Exception:
        return True  # unreadable/corrupt -- don't silently reprocess, surface it via status instead


def _resolve_app_filter(app_filter: str | None) -> list[Path]:
    """Resolves --app to a single application dir by exact match, then
    case-insensitive prefix (same convention every other pipeline
    script uses for --app-id). Returns every pending app dir if
    app_filter is None."""
    if app_filter is None:
        return app_dirs()
    exact = APPLICATIONS_ROOT / app_filter
    if exact.is_dir():
        return [exact]
    matches = [d for d in app_dirs() if d.name.lower().startswith(app_filter.lower())]
    if not matches:
        print(f"No applications/{app_filter}* folder found.", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"{app_filter!r} matches {len(matches)} folders -- be more specific:", file=sys.stderr)
        for m in matches:
            print(f"  {m.name}", file=sys.stderr)
        sys.exit(1)
    return matches


def run_full_chain(app_filter: str | None) -> None:
    """--full: continues past stage 7 into review (stage 8, via the
    dispatcher so pipeline.yaml's Review Stage Assignee decides human
    vs. Agent), assembly (stage 9), opening every live app's source_url
    in Chrome, and a final applications-stats checkpoint -- the rest of
    what a fully-automated Agent review setup no longer needs a human
    to trigger by hand.

    Each step runs via sh_optional() rather than sh() -- same
    philosophy as "one JD failing during prepare doesn't stop the
    batch": a review-step hiccup (e.g. a missing GEMINI_API_KEY)
    shouldn't prevent assemble from at least processing whatever DID
    get reviewed, assemble failing shouldn't prevent opening tabs for
    whatever's already live, and none of the above should prevent the
    stats checkpoint from at least reporting whatever state actually
    resulted. Assembly, opening URLs, and the stats checkpoint are all
    already unscoped by design (assemble processes every JD with
    review_decisions.json; open_app_urls opens every live app's
    source_url; py_applications_stats.py --all reports across all of
    applications/) -- only the review step itself understands
    --app-id, so only it gets `app_filter` forwarded."""
    print("\n=== Stage 8: review ===")
    review_cmd = [sys.executable, str(STAGE8_DISPATCHER)]
    if app_filter:
        review_cmd += ["--app-id", app_filter]
    sh_optional(review_cmd)

    print("\n=== Stage 9: assemble ===")
    sh_optional([sys.executable, str(ASSEMBLE_SCRIPT)])

    print("\n=== Opening application URLs ===")
    sh_optional([sys.executable, str(OPEN_URLS_SCRIPT)])

    print("\n=== Applications stats ===")
    sh_optional([sys.executable, str(APPLICATIONS_STATS_SCRIPT), "--all"])


def run_batch(app_filter: str | None, force: bool, dry_run: bool, provider: str, full: bool) -> None:
    drain_raw_intake()

    try:
        ledger_paths, style_path, cover_letter_path = ensure_variant_ingest(force=force, dry_run=dry_run, provider=provider)
    except Exception as e:
        print(f"Can't proceed — variant ingest failed: {e}", file=sys.stderr)
        sys.exit(1)

    candidates = _resolve_app_filter(app_filter)
    pending = [
        d for d in candidates
        if find_jd_input(d) is not None
        and not is_processed(d)
        and (force or not _is_really_verified(d))
    ]
    if not pending:
        print("Nothing pending — every applications/<id>/ folder is either already "
              "verified/reviewed/assembled, or has no jd_input.json yet. Drop a raw JD "
              "JSON straight into applications/ (or capture one via the Chrome extension), "
              "then re-run.")
        # "Nothing pending" only means stages 0-7 have nothing left to
        # prepare -- it says nothing about stage 8. An app fully
        # verified in an earlier run (or one whose review got cut off
        # partway through last time) is exactly the case --full exists
        # for: still returning here unconditionally would silently skip
        # review/assemble/open-urls/stats every time prepare alone has
        # nothing new to do, which defeats the entire point of --full
        # after a resumed or partial review session.
        if full and not dry_run:
            run_full_chain(app_filter)
        return

    dry = ["--dry-run"] if dry_run else []
    prov = ["--provider", provider]
    prepared, failed = [], []

    for out_dir in pending:
        app_id = out_dir.name
        jd_path = find_jd_input(out_dir)
        jd = load_model(jd_path, JDInput)
        ledger_path, variant_used, reasoning = pick_ledger_for_jd(jd, ledger_paths)

        print(f"\n=== Preparing {app_id} (resume variant: {variant_used}) ===")
        print(f"  {reasoning}")
        (out_dir / "routing.json").write_text(
            json.dumps({"resume_variant": variant_used, "reasoning": reasoning}, indent=2))
        try:
            sh([sys.executable, stage_script(3),
                "--jd-input", str(jd_path),
                "--claims-ledger", str(ledger_path),
                "--out-dir", str(out_dir)] + prov + dry)
            sh([sys.executable, stage_script(4),
                "--jd-input", str(jd_path),
                "--claims-ledger", str(ledger_path),
                "--out-dir", str(out_dir)] + prov + dry)
            sh([sys.executable, stage_script(5),
                "--claims-ledger", str(ledger_path),
                "--style-profile", str(style_path),
                "--edit-brief", str(out_dir / "edit_brief.json"),
                "--company-brief", str(out_dir / "company_brief.json"),
                "--cover-letter-baseline", str(cover_letter_path),
                "--out-dir", str(out_dir)] + prov + dry)
            sh([sys.executable, stage_script(6),
                "--claims-ledger", str(ledger_path),
                "--candidate-edits", str(out_dir / "candidate_edits.json"),
                "--out-dir", str(out_dir),
                "--cover-letter-baseline", str(cover_letter_path)] + prov + dry)
            sh([sys.executable, stage_script(7),
                "--jd-input", str(jd_path),
                "--verified-edits", str(out_dir / "verified_edits.json"),
                "--out-dir", str(out_dir),
                "--cover-letter-baseline", str(cover_letter_path)] + prov + dry)
            prepared.append(app_id)
        except Exception as e:
            print(f"[FAILED] {app_id}: {e}", file=sys.stderr)
            failed.append((app_id, str(e)))

    print(f"\n{len(prepared)} prepared, {len(failed)} failed")
    if failed:
        print("\nFailed (fix the cause and re-run run_pipeline.py — "
              "already-succeeded JDs are skipped automatically):")
        for a, err in failed:
            print(f"  {a}: {err}")

    if dry_run:
        if full:
            print("\n--full has nothing to continue into on a --dry-run pass "
                  "(stage 6 writes no real verified_edits.json in dry-run mode) "
                  "-- skipping review/assemble/open-urls.")
        return

    if full:
        run_full_chain(app_filter)
    elif prepared:
        print("\nReady for review — run:")
        print("  python src/ai_wrappers/py_stage08_reviewer_interface.py")
        print(f"(auto-discovers all {len(prepared)} JD(s) above, and honors pipeline.yaml's "
              f"Review Stage Assignee; pass --app-id <id> to review just one, or pass --full "
              f"to run_pipeline.py next time to chain review + assemble + open-urls automatically)")


def run_single_jd(args) -> None:
    """Intake-and-prepare a brand new JD not yet in applications/,
    through stages 0-7 (1 is optional, run right after 0 if
    --resume-text was given AND pipeline.yaml's run_stage1_scoring is
    Yes). Stops at 7 -- stage 8 (review) is interactive and stage 9
    (assembly) is its own checkpoint, same reasoning as batch mode."""
    dry = ["--dry-run"] if args.dry_run else []
    prov = ["--provider", args.provider]

    intake_cmd = [sys.executable, stage_script(0),
                  "--jd-json", args.jd_json,
                  "--applications-root", str(APPLICATIONS_ROOT)]
    if args.app:
        intake_cmd += ["--app-id", args.app]
    if args.source_url:
        intake_cmd += ["--source-url", args.source_url]
    stage0_output = sh_capture(intake_cmd)

    app_id = args.app
    if not app_id:
        for line in stage0_output.splitlines():
            if "(app_id:" in line:
                app_id = line.split("(app_id:")[1].strip(" )")
    if not app_id:
        print("Could not determine app_id from stage 0 output.", file=sys.stderr)
        sys.exit(1)

    out = APPLICATIONS_ROOT / app_id
    jd_input = json.loads((out / "jd_input.json").read_text())
    company = jd_input["company"]
    role_title = jd_input["role_title"]

    if args.resume_text and _stage1_enabled():
        sh_optional([sys.executable, stage_script(1),
                     "--app-id", app_id,
                     "--resume-text", args.resume_text,
                     "--applications-root", str(APPLICATIONS_ROOT)] + prov)
    elif args.resume_text:
        print("(skipping stage1 scoring -- pipeline.yaml's run_stage1_scoring is No)")
    else:
        print("(skipping stage1 scoring -- pass --resume-text and set pipeline.yaml's "
              "run_stage1_scoring: Yes to enable)")

    stages = {
        2: [sys.executable, stage_script(2),
            "--resume", args.resume,
            "--cover-letter-sample", args.cover_letter_sample,
            "--out-dir", str(out)] + prov + dry,
        3: [sys.executable, stage_script(3),
            "--jd-input", str(out / "jd_input.json"),
            "--claims-ledger", str(out / "claims_ledger.json"),
            "--out-dir", str(out)] + prov + dry,
        4: [sys.executable, stage_script(4),
            "--jd-input", str(out / "jd_input.json"),
            "--claims-ledger", str(out / "claims_ledger.json"),
            "--out-dir", str(out)] + prov + dry,
        5: [sys.executable, stage_script(5),
            "--claims-ledger", str(out / "claims_ledger.json"),
            "--style-profile", str(out / "style_profile.json"),
            "--edit-brief", str(out / "edit_brief.json"),
            "--company-brief", str(out / "company_brief.json"),
            "--cover-letter-baseline", args.cover_letter_sample,
            "--out-dir", str(out)] + prov + dry,
        6: [sys.executable, stage_script(6),
            "--claims-ledger", str(out / "claims_ledger.json"),
            "--candidate-edits", str(out / "candidate_edits.json"),
            "--out-dir", str(out),
            "--cover-letter-baseline", args.cover_letter_sample] + prov + dry,
        7: [sys.executable, stage_script(7),
            "--jd-input", str(out / "jd_input.json"),
            "--verified-edits", str(out / "verified_edits.json"),
            "--out-dir", str(out),
            "--cover-letter-baseline", args.cover_letter_sample] + prov + dry,
    }

    for n in range(2, 8):
        if args.stop_before and n >= args.stop_before:
            print(f"Stopping before stage {n} (--stop-before {args.stop_before})")
            break
        sh(stages[n])

    print(f"\n{app_id} prepared through stage 7 ({company} / {role_title}).")
    print(f"Ready for review — run: python src/stages/py_stage08_agentic_update_review.py --app-id {app_id}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", default=None,
                     help="Scope to one application (exact app_id, or a unique prefix). "
                          "Batch mode only -- for single-JD mode this instead sets the "
                          "app_id for the new application being created.")
    ap.add_argument("--force", action="store_true",
                     help="Batch mode: re-run even if verified_edits.json already exists for a JD.")
    ap.add_argument("--dry-run", action="store_true",
                     help="Skip actual API calls; just validate inputs and show what would run.")
    ap.add_argument("--provider", default=default_provider(), choices=["lmstudio", "claude", "gemini"],
                     help="Defaults to $CVTAILOR_LLM_PROVIDER, or 'lmstudio' (a local model, no cloud spend) if that's unset.")
    ap.add_argument("--full", action="store_true",
                     help="Batch mode: after stages 0-7, continue straight into stage 8 review "
                          "(via src/ai_wrappers/py_stage08_reviewer_interface.py, honoring "
                          "pipeline.yaml's Review Stage Assignee -- User still stops and waits "
                          "for you, Agent doesn't), stage 9 assemble, and opening every live "
                          "app's source_url in Chrome. No effect combined with --dry-run (there's "
                          "nothing real yet to review).")

    single_jd = ap.add_argument_group("single-JD end-to-end mode (pass --jd-json to enable)")
    single_jd.add_argument("--jd-json", default=None,
                            help="JSON exported by the Chrome extension (or hand-built, matching "
                                 "JDInput in src/core/schemas.py). Switches run_pipeline.py from "
                                 "batch mode into single-JD end-to-end mode.")
    single_jd.add_argument("--source-url", default=None)
    single_jd.add_argument("--resume", default=None)
    single_jd.add_argument("--resume-text", default=None,
                            help="Plain-text resume snapshot for stage1 scoring -- optional, "
                                 "runs the pre-screen automatically right after stage 0 if given.")
    single_jd.add_argument("--cover-letter-sample", default=None)
    single_jd.add_argument("--stop-before", type=int, default=None)
    args = ap.parse_args()

    if args.jd_json:
        missing = [name for name, val in [("--resume", args.resume), ("--cover-letter-sample", args.cover_letter_sample)] if not val]
        if missing:
            ap.error(f"single-JD mode (--jd-json given) also requires: {', '.join(missing)}")
        run_single_jd(args)
    else:
        run_batch(args.app, args.force, args.dry_run, args.provider, args.full)


if __name__ == "__main__":
    main()
