#!/usr/bin/env python3
"""
py_pipeline_assemble.py
=======================
Assembles reviewed edits into the final `.docx` and `.pdf` files inside `generated_materials/`. 
Marks the application as processed on success.

Usage:
    python src/controls/py_pipeline_assemble.py [--force] [--keep-docx] [--app-id <id>]
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import (
    APPLICATIONS_ROOT, NON_APPLICATION_DIR_NAMES, app_dirs, find_jd_input, mark_processed,
    COVER_LETTER_TEMPLATE_DIR, COVER_LETTER_SAMPLE_DIR,
    discover_resume_template_variants, pick_template_for_variant,
    discover_single_file, discover_single_file_optional,
    load_applicant_info, sh, stage_script,
)
from docx_utils import convert_docx_to_pdf, get_pdf_page_count, compress_docx_spacing


def _convert_materials_to_pdf(materials_dir: Path, app_id: str, keep_docx: bool,
                               max_page_fit_level: int, resume_max_pages: int,
                               cover_letter_max_pages: int, applicant_name: str = "") -> bool:
    """Converts every resume/cover-letter .docx sitting in materials_dir
    to PDF, with the same typography-compression page-fit fallback --
    shared by both a normal full assemble and --app-id's PDF-only
    regeneration mode so the fit logic can't drift between the two.
    Returns False if there was nothing to convert (no matching .docx
    filenames in materials_dir), so callers can report that distinctly
    from every file actually failing conversion."""
    resume_files = sorted(materials_dir.glob("* - Resume.docx"))
    cover_files = sorted(materials_dir.glob("* - Cover Letter.docx"))
    if not resume_files and not cover_files:
        return False

    for src in resume_files + cover_files:
        is_resume = src in resume_files
        doc_label = "resume" if is_resume else "cover letter"
        target_pages = resume_max_pages if is_resume else cover_letter_max_pages
        pdf_path = src.with_suffix(".pdf")
        pdf_ok = convert_docx_to_pdf(src, pdf_path)

        if pdf_ok and max_page_fit_level > 0 and target_pages > 0:
            page_count = get_pdf_page_count(pdf_path)
            if page_count and page_count > target_pages:
                print(f"  {src.name} is {page_count} page(s), over the "
                      f"{target_pages}-page {doc_label} target — trying typography "
                      f"compression to fit (content itself is never changed)")
                working_copy = Path(tempfile.gettempdir()) / f"cvtailor_compress_{app_id}_{doc_label.replace(' ', '_')}.docx"
                fit = False
                for level in range(1, max_page_fit_level + 1):
                    # Fresh copy of the ORIGINAL, uncompressed src before every
                    # attempt -- not the previous level's already-reduced output.
                    # Calling compress_docx_spacing repeatedly on the same
                    # progressively-mutated file compounds each level's
                    # reduction on top of the last (0.9 x 0.75 x 0.6 = 0.405,
                    # not the 0.6 "level 3" alone implies), which is how a real
                    # run ended up with margins pinned at the hard 0.35in floor
                    # and unreadably small body text. Each level is meant to be
                    # its own standalone aggressiveness, tried in increasing
                    # order until one works -- not a stack of all of them.
                    shutil.copy(src, working_copy)
                    compress_docx_spacing(working_copy, level, applicant_name)
                    if not convert_docx_to_pdf(working_copy, pdf_path):
                        break
                    new_count = get_pdf_page_count(pdf_path)
                    print(f"    level {level}: {new_count} page(s)")
                    if new_count and new_count <= target_pages:
                        shutil.copy(working_copy, src)  # keep the fitted version
                        fit = True
                        break
                if not fit:
                    print(f"    still over {target_pages} page(s) at maximum "
                          f"compression (level {max_page_fit_level}) — this needs "
                          f"actual content trimmed from the template, typography "
                          f"alone can't fix it. See README's page-limit section.")
                    convert_docx_to_pdf(src, pdf_path)  # regenerate from the uncompressed original
                working_copy.unlink(missing_ok=True)

        if pdf_ok and not keep_docx:
            src.unlink()  # PDF succeeded and .docx wasn't explicitly requested to stay

    return True


def find_matches_by_prefix(prefix: str) -> list[Path]:
    """Case-insensitive startswith match against live applications/
    folders -- same convention py_post_pipeline_store_applied.py already
    uses for its own prefix matching, reused here so --app-id behaves consistently
    across the project instead of introducing a second matching rule."""
    if not APPLICATIONS_ROOT.is_dir():
        return []
    prefix_lower = prefix.lower()
    return sorted(
        d for d in APPLICATIONS_ROOT.iterdir()
        if d.is_dir()
        and d.name not in NON_APPLICATION_DIR_NAMES
        and d.name.lower().startswith(prefix_lower)
    )


def regenerate_pdfs(app_id_arg: str, keep_docx: bool, max_page_fit_level: int,
                     resume_max_pages: int, cover_letter_max_pages: int):
    """--app-id mode: skips assembly (stage 9) entirely and just
    re-converts each matching app's EXISTING generated_materials/*.docx
    to a fresh PDF. For the "I hand-edited the docx after assembly,
    just need the PDF redone" case -- running the normal assemble path
    would re-run stage 9 from verified_edits.json/review_decisions.json
    and overwrite those hand edits with the original generated text.

    Each entry in the comma-separated --app-id list is resolved by
    prefix (see find_matches_by_prefix) against live applications/
    folders. A prefix matching zero or more than one folder is a hard
    error, not a guess -- this flag goes straight to overwriting a PDF
    in place, so silently picking the wrong app is worse than making
    you retype it."""
    prefixes = [p.strip() for p in app_id_arg.split(",") if p.strip()]
    if not prefixes:
        print("--app-id given but empty after splitting on commas.", file=sys.stderr)
        sys.exit(1)

    applicant_name = load_applicant_info().get("name", "")

    resolved: list[Path] = []
    for prefix in prefixes:
        matches = find_matches_by_prefix(prefix)
        if not matches:
            print(f"No application folder found starting with {prefix!r} under {APPLICATIONS_ROOT}/.",
                  file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"{prefix!r} matches {len(matches)} folders -- be more specific:", file=sys.stderr)
            for m in matches:
                print(f"  {m.name}", file=sys.stderr)
            sys.exit(1)
        resolved.append(matches[0])

    for app_dir in resolved:
        app_id = app_dir.name
        materials_dir = app_dir / "generated_materials"
        if not materials_dir.is_dir():
            print(f"[skip] {app_id} — no generated_materials/ yet, nothing to regenerate "
                  f"(run a full assemble first)", file=sys.stderr)
            continue
        print(f"\n=== Regenerating PDF(s) for {app_id} from existing .docx ===")
        if not _convert_materials_to_pdf(materials_dir, app_id, keep_docx, max_page_fit_level,
                                          resume_max_pages, cover_letter_max_pages, applicant_name):
            print(f"[skip] {app_id} — no .docx in {materials_dir} to convert "
                  f"(already pdf-only, or a prior --no-keep-docx run already cleared them)",
                  file=sys.stderr)


def run(force: bool, keep_docx: bool, dry_run: bool, max_page_fit_level: int, allow_empty_review: bool,
        resume_max_pages: int = 2, cover_letter_max_pages: int = 1):
    try:
        applicant = load_applicant_info()
        templates = discover_resume_template_variants()
        cover_letter_template = discover_single_file_optional(
            COVER_LETTER_TEMPLATE_DIR, (".docx",), "cover letter template",
        )
        cover_letter_sample = discover_single_file(
            COVER_LETTER_SAMPLE_DIR, (".txt",), "cover letter sample",
        )
    except Exception as e:
        print(f"Can't proceed: {e}", file=sys.stderr)
        sys.exit(1)

    if not APPLICATIONS_ROOT.is_dir():
        print(f"No {APPLICATIONS_ROOT}/ found — nothing to assemble yet.")
        return
    dirs = app_dirs()
    if not dirs:
        print(f"No applications found under {APPLICATIONS_ROOT}/.")
        return

    dry = ["--dry-run"] if dry_run else []
    assembled, skipped, awaiting_review, failed = [], [], [], []

    for app_dir in dirs:
        app_id = app_dir.name
        decisions_path = app_dir / "review_decisions.json"
        if not decisions_path.is_file():
            awaiting_review.append(app_id)
            continue

        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
        non_rejected = sum(d.get("decision") in ("approved", "edited") for d in decisions)
        if non_rejected == 0 and not allow_empty_review:
            print(f"[not reviewed] {app_id} — review_decisions.json has 0 approved/edited "
                  f"edits (usually means stage 8 was quit before deciding anything). "
                  f"Treating as still awaiting review, not assembling. Re-run: "
                  f"python src/stages/py_stage08_agentic_update_review.py --app-id {app_id}")
            awaiting_review.append(app_id)
            continue

        materials_dir = app_dir / "generated_materials"
        already_done = materials_dir.is_dir() and (any(materials_dir.glob("*.pdf")) or any(materials_dir.glob("*.docx")))
        if already_done and not force:
            print(f"[skip] {app_id} — already in generated_materials/ (use --force to redo)")
            skipped.append(app_id)
            continue

        jd_input_path = find_jd_input(app_dir)
        if jd_input_path is None:
            print(f"[FAILED] {app_id}: missing jd_input.json — was stage 0 ever run for this?",
                  file=sys.stderr)
            failed.append((app_id, "missing jd_input.json"))
            continue
        jd = json.loads(jd_input_path.read_text(encoding="utf-8"))

        # Route to the same resume-template variant run_pipeline.py
        # picked for this app's claims ledger (persisted in routing.json).
        # Missing routing.json (e.g. an app prepared before this feature
        # existed) falls back to whatever pick_template_for_variant()'s
        # own fallback does -- doesn't block assembly over it.
        routing_path = app_dir / "routing.json"
        variant_used = "*"
        if routing_path.is_file():
            try:
                variant_used = json.loads(routing_path.read_text(encoding="utf-8")).get("resume_variant", "*")
            except Exception:
                pass
        resume_template = pick_template_for_variant(variant_used, templates)

        print(f"\n=== Assembling {app_id} ===")
        try:
            sh([sys.executable, stage_script("py_stage09_assemble_materials.py"),
                "--resume-template", str(resume_template),
                "--review-decisions", str(decisions_path),
                "--verified-edits", str(app_dir / "verified_edits.json"),
                "--out-dir", str(app_dir),
                "--company", jd["company"], "--role-title", jd["role_title"],
                "--applicant-name", applicant["name"]]
               + (["--applicant-email", applicant["email"]] if applicant.get("email") else [])
               + (["--applicant-phone", applicant["phone"]] if applicant.get("phone") else [])
               + (["--applicant-linkedin", applicant["linkedin"]] if applicant.get("linkedin") else [])
               + (["--applicant-github", applicant["github"]] if applicant.get("github") else [])
               + (["--applicant-address-line-1", applicant["address_line_1"]] if applicant.get("address_line_1") else [])
               + (["--applicant-address-line-2", applicant["address_line_2"]] if applicant.get("address_line_2") else [])
               + (["--applicant-city", applicant["address_city"]] if applicant.get("address_city") else [])
               + (["--applicant-state", applicant["address_state"]] if applicant.get("address_state") else [])
               + (["--applicant-zipcode", applicant["zipcode"]] if applicant.get("zipcode") else [])
               + (["--cover-letter-template", str(cover_letter_template)] if cover_letter_template else [])
               + ["--skip-cover-letter-review"]
               + ["--cover-letter-baseline", str(cover_letter_sample)]
               + (["--resume-variant", variant_used] if variant_used and variant_used != "*" else [])
               + dry)

            if dry_run:
                assembled.append(app_id)
                continue

            # Real docx files already live at their final home
            # (generated_materials/, written directly there by stage 9)
            # -- no separate copy-to-output-folder step needed anymore.
            # Glob by the deterministic " - Resume.docx"/" - Cover
            # Letter.docx" suffix rather than reconstructing the exact
            # filename independently (company/role/name safe-string
            # logic living in two places was exactly how this drifted
            # out of sync and silently no-op'd for a long time before
            # being caught).
            _convert_materials_to_pdf(materials_dir, app_id, keep_docx, max_page_fit_level,
                                       resume_max_pages, cover_letter_max_pages, applicant["name"])

            # Only mark this JD processed once real output exists — see
            # module docstring. Renames jd_input.json -> jd_input.processed.json
            # in place, a visible at-a-glance marker; no separate queue
            # folder to move a file between.
            mark_processed(app_dir)
            print(f"Marked {app_id} processed (jd_input.json -> jd_input.processed.json).")

            assembled.append(app_id)
        except Exception as e:
            print(f"[FAILED] {app_id}: {e}", file=sys.stderr)
            failed.append((app_id, str(e)))

    print(f"\n{len(assembled)} assembled, {len(skipped)} skipped, "
          f"{len(awaiting_review)} awaiting review, {len(failed)} failed")
    if awaiting_review:
        print("\nAwaiting review (run stage 8 for these first):")
        for a in awaiting_review:
            print(f"  python src/stages/py_stage08_agentic_update_review.py --app-id {a}")
    if failed:
        print("\nFailed:")
        for a, err in failed:
            print(f"  {a}: {err}")

    if assembled:
        print("\nBefore sending anything, run the Definition of Done check "
              "(see DEFINITION_OF_DONE.md):")
        for a in assembled:
            print(f"  python src/checkpoints/py_post_pipeline_verify_materials.py --app-id {a}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Re-assemble even if generated_materials/ already has this app")
    ap.add_argument("--keep-docx", action="store_true", default=True,
                     help="(default: on) Keep the .docx alongside the converted .pdf. Flipped "
                          "to default-on after real use showed the deleted-by-default .docx "
                          "meant a hand-editable copy wasn't available when a formatting bug "
                          "(a missing paragraph break, in one real case) needed a quick manual "
                          "fix -- storage cost is trivial next to that. Pass --no-keep-docx to "
                          "go back to pdf-only; src/util/py_applications_archive_cleanup.py is the "
                          "right place to reclaim space once an application is truly done, not this flag.")
    ap.add_argument("--no-keep-docx", dest="keep_docx", action="store_false",
                     help="Delete the .docx once PDF conversion succeeds (the old default).")
    ap.add_argument("--resume-max-pages", type=int, default=2,
                     help="Target page count for the resume (default: 2). Typography "
                          "compression tries to fit within this before giving up and "
                          "leaving the uncompressed original in place.")
    ap.add_argument("--cover-letter-max-pages", type=int, default=1,
                     help="Target page count for the cover letter (default: 1). Same "
                          "compression behavior as --resume-max-pages.")
    ap.add_argument("--max-page-fit-level", type=int, default=3, choices=[0, 1, 2, 3],
                     help="If the resume/cover letter PDF comes out over its page target "
                          "(--resume-max-pages / --cover-letter-max-pages), try up to this "
                          "many levels of typography compression (margins/spacing/font) "
                          "before giving up. 0 disables page-fit entirely. Never touches "
                          "actual content.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-empty-review", action="store_true",
                     help="Assemble even if review_decisions.json has 0 approved/edited "
                          "edits. Off by default — this is almost always an accidentally "
                          "skipped review, not a real intent to submit unedited content.")
    ap.add_argument("--app-id", default=None,
                     help="Comma-separated app-id prefix(es), e.g. 'valon' or "
                          "'valon,toast_senior,evolve'. When given, skips full assembly "
                          "entirely and just re-converts each matching app's EXISTING "
                          "generated_materials/*.docx to a fresh .pdf -- for after you've "
                          "hand-edited the docx and only need the PDF regenerated. Ignores "
                          "--force/--dry-run/--allow-empty-review in this mode.")
    args = ap.parse_args()
    if args.app_id:
        regenerate_pdfs(args.app_id, args.keep_docx, args.max_page_fit_level,
                         args.resume_max_pages, args.cover_letter_max_pages)
    else:
        run(args.force, args.keep_docx, args.dry_run, args.max_page_fit_level, args.allow_empty_review,
            resume_max_pages=args.resume_max_pages, cover_letter_max_pages=args.cover_letter_max_pages)