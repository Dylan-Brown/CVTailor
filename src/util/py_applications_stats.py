#!/usr/bin/env python3
"""
py_applications_stats.py — throughput at a glance: how many live
applications are pending, how many were actually applied/cut-off/
revisited/test-run each week, and the resume-variant split (see
config/resume_variants/) across everything generated so far. Python
rewrite of the old count_apps_pending.ps1, combining what used to be
three separate manual checks (pending count, --archive weekly
breakdown, and a commented-out "bonus" Get-ChildItem snippet for the
resume-variant split) into one script.

Usage:
    python src/util/py_applications_stats.py              # pending count
    python src/util/py_applications_stats.py --archive     # weekly velocity breakdown
    python src/util/py_applications_stats.py --variants     # resume-variant split
    python src/util/py_applications_stats.py --all          # all three
"""
import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import APPLICATIONS_ROOT, ARCHIVED_DIR, app_dirs, load_resume_variants

LOG_PATH = APPLICATIONS_ROOT / "archive" / "apps_moved.log"
_LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\s+(\S+)\s+(.+)$")
_LABELS = ("applied", "cut_off", "revisit", "test")

# This job search's own week-of-unemployment counter, tracked by hand
# outside this script -- the week containing the earliest logged move
# is week 20; every following week increments by 1 from there. Same
# anchor the old PowerShell version used.
_FIRST_WEEK_NUMBER = 20


def print_pending_count() -> None:
    print(f"Pending: {len(app_dirs())}")


def print_archive_breakdown() -> None:
    if not LOG_PATH.is_file():
        print(f"No log file found at {LOG_PATH} -- run "
              f"src/controls/py_post_pipeline_store_applied.py at least once to create it.",
              file=sys.stderr)
        return

    entries = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        m = _LOG_LINE_RE.match(line)
        if m:
            entries.append((datetime.strptime(m.group(1), "%Y-%m-%d"), m.group(2)))

    if not entries:
        print("Log file is empty -- nothing to summarize.")
        return

    first_date = min(e[0] for e in entries)
    last_date = max(e[0] for e in entries)
    # Weeks run Sunday-to-Saturday. Python's weekday(): Monday=0..Sunday=6,
    # so days-since-Sunday is (weekday + 1) % 7.
    days_since_sunday = (first_date.weekday() + 1) % 7
    anchor_week_start = first_date - timedelta(days=days_since_sunday)
    max_offset = (last_date - anchor_week_start).days // 7

    counts = Counter()
    for date, label in entries:
        offset = (date - anchor_week_start).days // 7
        counts[(offset, label)] += 1

    for offset in range(max_offset + 1):
        week_num = _FIRST_WEEK_NUMBER + offset
        week_start = anchor_week_start + timedelta(days=offset * 7)
        week_end = week_start + timedelta(days=6)
        print(f"Week {week_num} ({week_start:%Y-%m-%d} to {week_end:%Y-%m-%d}):")
        week_total = 0
        for label in _LABELS:
            c = counts.get((offset, label), 0)
            week_total += c
            print(f"  {label:<8}: {c}")
        print(f"  {'total':<8}: {week_total}")
        print()


def print_variant_split() -> None:
    """Resume variant split across every generated resume so far (live
    applications/ plus applications/archived/) -- a rough throughput
    signal for which variant is actually getting used, not a precise
    count (a resume filename not matching any configured variant label
    is simply not counted). Labels come from config/resume_variants/
    (see load_resume_variants()), not hardcoded, so this stays correct
    however many variants are configured -- two, three, or one."""
    labels = sorted(load_resume_variants().keys(), key=len, reverse=True)  # longest first, so
    # e.g. "AI Engineer" doesn't get shadowed by a shorter label that's also a substring match.
    counts = Counter()
    roots = [r for r in (APPLICATIONS_ROOT, ARCHIVED_DIR) if r.is_dir()]
    for root in roots:
        for resume in root.glob("*/generated_materials/*Resume.*"):
            name_lower = resume.stem.lower()
            for label in labels:
                if label.lower() in name_lower:
                    counts[label] += 1
                    break

    if not counts:
        print("No generated resumes found under applications/ or applications/archived/.")
        return
    print("Resume variant split (generated_materials/*Resume.*):")
    for label, n in counts.most_common():
        print(f"  {label:<12}: {n}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", action="store_true", help="Weekly applied/cut_off/revisit/test breakdown")
    ap.add_argument("--variants", action="store_true", help="Backend vs. Full Stack resume split")
    ap.add_argument("--all", action="store_true", help="Print all three sections")
    args = ap.parse_args()

    if args.all:
        print_pending_count()
        print()
        print_archive_breakdown()
        print_variant_split()
    elif args.archive:
        print_archive_breakdown()
    elif args.variants:
        print_variant_split()
    else:
        print_pending_count()
