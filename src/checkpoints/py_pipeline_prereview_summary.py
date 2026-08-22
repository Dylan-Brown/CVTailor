#!/usr/bin/env python3
"""
py_pipeline_prereview_summary.py — the funnel, at a glance, across every
prepared application: how many claims in the ledger it drew from, how
many edits stage 5 proposed, how many survived stage 6's fact-check
and stage 7's utility critique, and — the number that actually
matters for what you're about to spend review time on — how many
edits will actually be SHOWN to you in stage 8, split by resume vs.
cover letter.

py_pipeline_status.py tells you WHICH STAGE each app is at. This tells
you whether what's sitting at that stage looks healthy, before you
spend review time on it. Doesn't replace py_post_pipeline_verify_materials.py
(that runs post-assembly, against the real generated documents) — this is
the earlier check, for the ~20-JD-at-once situation where reviewing
every single one by hand first is the wrong order of operations.

Flags, not just numbers:
    ZERO EDITS      -- nothing survived to show in review at all
    NO RESUME EDITS -- cover letter got tailored, resume didn't
    THIN LEDGER     -- the resume variant's claims ledger looks weak
    LOW COVERAGE     -- edit_brief's coverage_score is low

Usage:
    python src/checkpoints/py_pipeline_prereview_summary.py
    python src/checkpoints/py_pipeline_prereview_summary.py --app-id <id>
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import APPLICATIONS_ROOT, app_dirs

THIN_LEDGER_THRESHOLD = 20  # matches schemas.py's own floor
LOW_COVERAGE_THRESHOLD = 0.30


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def summarize_app(app_dir: Path) -> dict | None:
    """None if this app isn't far enough along to summarize yet (no
    verified_edits.json) -- distinct from a real 0-edit result, which
    IS summarizable and gets flagged."""
    verified_path = app_dir / "verified_edits.json"
    if not verified_path.is_file():
        return None
    verified = _read_json(verified_path)
    if verified is None or verified.get("dry_run", False):
        return None

    jd_path = app_dir / "jd_input.processed.json"
    if not jd_path.is_file():
        jd_path = app_dir / "jd_input.json"
    jd = _read_json(jd_path) or {}
    company = jd.get("company", "?")

    routing = _read_json(app_dir / "routing.json") or {}
    variant = routing.get("resume_variant", "?")
    slug = re.sub(r"[^\w]+", "_", variant.strip().lower()).strip("_") if variant != "?" else None
    ledger_size = None
    if slug:
        ledger = _read_json(APPLICATIONS_ROOT / "_shared" / "variants" / slug / "claims_ledger.json")
        if ledger:
            ledger_size = len(ledger.get("claims", []))

    edit_brief = _read_json(app_dir / "edit_brief.json") or {}
    coverage = edit_brief.get("coverage_score")

    candidate_edits = (_read_json(app_dir / "candidate_edits.json") or {}).get("edits", [])
    n_candidate = len(candidate_edits)
    # Resume/cover split at the EARLIEST possible point -- what stage 5
    # actually proposed, before anything downstream has a chance to
    # touch it. If this is already 0, the fix belongs in stage 5's
    # prompt, not in stage 6/5b/6 -- there was never a resume edit to
    # lose in the first place.
    n_candidate_resume = sum(1 for e in candidate_edits if e.get("target") == "resume")

    verified_all = verified.get("passed", []) + verified.get("flagged", [])
    n_passed = len(verified.get("passed", []))
    n_flagged = len(verified.get("flagged", []))
    n_discarded_stage6 = n_candidate - n_passed - n_flagged
    # Resume/cover split AFTER stage 6 -- if this dropped to 0 while
    # n_candidate_resume was > 0, stage 6's fact-check is disproportionately
    # killing resume edits specifically (likely the verbatim original_text
    # matching, or embellishment flags on resume rewords).
    n_verified_resume = sum(1 for e in verified_all if e.get("target") == "resume")

    critiqued_path = app_dir / "critiqued_edits.json"
    critiqued = _read_json(critiqued_path) if critiqued_path.is_file() else None
    if critiqued and not critiqued.get("dry_run", False):
        kept = critiqued.get("passed", []) + critiqued.get("flagged", [])
        n_cut_stage7 = (n_passed + n_flagged) - len(kept)
    else:
        # 5b hasn't run (or was skipped) -- everything stage 6 passed is
        # what would reach review.
        kept = verified_all
        n_cut_stage7 = 0

    n_resume = sum(1 for e in kept if e.get("target") == "resume")
    n_cover = sum(1 for e in kept if e.get("target") == "cover_letter")

    # Where in the funnel resume edits actually disappeared, if they did --
    # this is the whole point of tracking the split at each stage instead
    # of just the final one.
    resume_loss_point = None
    if n_candidate_resume == 0:
        resume_loss_point = "never proposed (stage 5)"
    elif n_verified_resume == 0:
        resume_loss_point = "lost in stage 6 (fact-check)"
    elif n_resume == 0:
        resume_loss_point = "lost in stage 7 (critique)"

    flags = []
    if len(kept) == 0:
        flags.append("ZERO EDITS")
    if n_resume == 0 and len(kept) > 0:
        flags.append(f"NO RESUME EDITS ({resume_loss_point})")
    if ledger_size is not None and ledger_size < THIN_LEDGER_THRESHOLD:
        flags.append(f"THIN LEDGER ({ledger_size})")
    if coverage is not None and coverage < LOW_COVERAGE_THRESHOLD:
        flags.append(f"LOW COVERAGE ({coverage:.0%})")

    return {
        "app_id": app_dir.name, "company": company, "variant": variant,
        "ledger_size": ledger_size, "coverage": coverage,
        "n_candidate": n_candidate, "n_candidate_resume": n_candidate_resume,
        "n_passed": n_passed, "n_flagged": n_flagged, "n_verified_resume": n_verified_resume,
        "n_discarded_stage6": n_discarded_stage6, "n_cut_stage7": n_cut_stage7,
        "n_kept_total": len(kept), "n_resume": n_resume, "n_cover": n_cover,
        "resume_loss_point": resume_loss_point, "flags": flags,
    }


def run(app_id: str | None):
    targets = [APPLICATIONS_ROOT / app_id] if app_id else app_dirs()
    if app_id and not targets[0].is_dir():
        print(f"No applications/{app_id}/ folder found.", file=sys.stderr)
        sys.exit(1)

    rows = []
    not_ready = []
    for d in sorted(targets):
        summary = summarize_app(d)
        if summary is None:
            not_ready.append(d.name)
        else:
            rows.append(summary)

    if not rows:
        print("Nothing has real verified_edits.json yet -- nothing to summarize.")
        if not_ready:
            print(f"({len(not_ready)} app(s) not far enough along: {', '.join(not_ready)})")
        return

    id_w = max(len(r["app_id"]) for r in rows) + 2
    print(f"\n{'app_id':<{id_w}}{'ledger':>7}{'cov':>6}{'gen':>5}{'pass':>6}{'flag':>6}"
          f"{'d5':>4}{'cut5b':>7}{'kept':>6}{'res':>5}{'cov.l':>6}  flags")
    print("-" * (id_w + 60))

    flagged_count = 0
    for r in rows:
        if r["flags"]:
            flagged_count += 1
        ledger_str = str(r["ledger_size"]) if r["ledger_size"] is not None else "?"
        cov_str = f"{r['coverage']:.0%}" if r["coverage"] is not None else "?"
        flags_str = ", ".join(r["flags"])
        print(f"{r['app_id']:<{id_w}}{ledger_str:>7}{cov_str:>6}{r['n_candidate']:>5}"
              f"{r['n_passed']:>6}{r['n_flagged']:>6}{r['n_discarded_stage6']:>4}"
              f"{r['n_cut_stage7']:>7}{r['n_kept_total']:>6}{r['n_resume']:>5}"
              f"{r['n_cover']:>6}  {flags_str}")

    print(f"\nColumns: ledger=claims in the routed variant's ledger, cov=stage 4 "
          f"coverage_score, gen=stage 5 candidates, pass/flag=stage 6 verdicts, "
          f"d5=stage 6 discards, cut5b=stage 7 cuts, kept=what stage 8 will "
          f"actually show, res/cov.l=that split by resume vs. cover letter.")
    print(f"\n{len(rows)} app(s) summarized, {flagged_count} with at least one flag worth a look.")
    if not_ready:
        print(f"{len(not_ready)} app(s) not far enough along yet to summarize: {', '.join(not_ready)}")

    # The actual diagnostic payoff -- WHERE in the funnel resume edits
    # are disappearing, tallied across every app, not just flagged per-
    # app. If this is dominated by one stage, that's where the real fix
    # belongs, not a guess.
    loss_points = [r["resume_loss_point"] for r in rows if r.get("resume_loss_point")]
    if loss_points:
        from collections import Counter
        counts = Counter(loss_points)
        print(f"\nOf {len(rows)} apps, {len(loss_points)} lost resume edits somewhere in the funnel:")
        for point, n in counts.most_common():
            print(f"  {n:>2} app(s) -- {point}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-id", default=None, help="Summarize just one app instead of everything")
    args = ap.parse_args()
    run(args.app_id)
