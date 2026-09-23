#!/usr/bin/env python3
"""
py_applications_stats.py
========================
Generates throughput and velocity metrics for your pipeline. 

Calculates pending applications, weekly velocity, resume-variant splits, 
and file-watcher timings. Appends a structured record of every run to 
`log/applications_stats.jsonl`.

Usage:
    python src/util/py_applications_stats.py [--archive | --variants | --timings | --extraction | --all]
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import APPLICATIONS_ROOT, ARCHIVE_ROOT, CONFIG_ROOT, app_dirs, load_applicant_info, load_resume_variants
from io_utils import log_event

LOG_PATH = APPLICATIONS_ROOT / "archive" / "apps_moved.log"
FILE_WATCHER_CONFIG_PATH = CONFIG_ROOT / "file_watcher.json"
_LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\s+(\S+)\s+(.+)$")
_LABELS = ("applied", "cut_off", "revisit", "test", "done")


def _job_search_week1_sunday() -> datetime:
    """Week 1 of the archive breakdown is the week containing
    applicant_info.json's job_search_start_date -- replaces the old
    hardcoded _FIRST_WEEK_NUMBER = 20 constant, so both "which week is
    this" and "where does the calendar start" come from one configured
    date instead of a number tracked by hand outside the script."""
    raw = load_applicant_info().get("job_search_start_date")
    if not raw:
        raise SystemExit(
            f"{CONFIG_ROOT / 'applicant_info.json'} is missing 'job_search_start_date' "
            f"(e.g. \"2026-01-01\", the Sunday-or-any-day your job search began -- "
            f"see config/applicant_info.example.json). --archive needs it to number weeks."
        )
    try:
        start = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"applicant_info.json's job_search_start_date {raw!r} isn't YYYY-MM-DD.")
    # Weeks run Sunday-to-Saturday. Python's weekday(): Monday=0..Sunday=6,
    # so days-since-Sunday is (weekday + 1) % 7.
    days_since_sunday = (start.weekday() + 1) % 7
    return start - timedelta(days=days_since_sunday)


def print_pending_count() -> int:
    count = len(app_dirs())
    print(f"Pending: {count}")
    return count


def print_archive_breakdown() -> list[dict]:
    if not LOG_PATH.is_file():
        print(f"No log file found at {LOG_PATH} -- run "
              f"src/controls/py_post_pipeline_store_applied.py at least once to create it.",
              file=sys.stderr)
        return []

    entries = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        m = _LOG_LINE_RE.match(line)
        if m:
            entries.append((datetime.strptime(m.group(1), "%Y-%m-%d"), m.group(2)))

    if not entries:
        print("Log file is empty -- nothing to summarize.")
        return []

    last_date = max(e[0] for e in entries)
    anchor_week_start = _job_search_week1_sunday()  # week 1's Sunday, from applicant_info.json
    min_offset = min(0, min((e[0] - anchor_week_start).days // 7 for e in entries))
    max_offset = (last_date - anchor_week_start).days // 7

    counts = Counter()
    for date, label in entries:
        offset = (date - anchor_week_start).days // 7
        counts[(offset, label)] += 1

    weeks = []
    for offset in range(min_offset, max_offset + 1):
        week_num = offset + 1
        week_start = anchor_week_start + timedelta(days=offset * 7)
        week_end = week_start + timedelta(days=6)
        print(f"Week {week_num} ({week_start:%Y-%m-%d} to {week_end:%Y-%m-%d}):")
        week_row = {"week": week_num, "start": f"{week_start:%Y-%m-%d}", "end": f"{week_end:%Y-%m-%d}"}
        week_total = 0
        for label in _LABELS:
            c = counts.get((offset, label), 0)
            week_row[label] = c
            week_total += c
            print(f"  {label:<8}: {c}")
        week_row["total"] = week_total
        print(f"  {'total':<8}: {week_total}")
        print()
        weeks.append(week_row)
    return weeks


def _scan_roots() -> list[Path]:
    """applications/ itself, plus every outcome subfolder under
    applications/archive/ (applied/cut_off/revisit/test/done) --
    each one directly holds app_id folders, same shape as
    APPLICATIONS_ROOT, so callers can glob "*/..." against each with
    no special-casing for which root it came from."""
    roots = [APPLICATIONS_ROOT]
    if ARCHIVE_ROOT.is_dir():
        roots.extend(d for d in ARCHIVE_ROOT.iterdir() if d.is_dir())
    return [r for r in roots if r.is_dir()]


def print_variant_split() -> dict:
    """Resume variant split across every generated resume so far (live
    applications/ plus every applications/archive/<outcome>/) -- a
    rough throughput signal for which variant is actually getting
    used, not a precise count (a resume filename not matching any
    configured variant label is simply not counted). Labels come from
    config/resume_variants/ (see load_resume_variants()), not
    hardcoded, so this stays correct however many variants are
    configured -- two, three, or one."""
    labels = sorted(load_resume_variants().keys(), key=len, reverse=True)  # longest first, so
    # e.g. "AI Engineer" doesn't get shadowed by a shorter label that's also a substring match.
    counts = Counter()
    for root in _scan_roots():
        for resume in root.glob("*/generated_materials/*Resume.*"):
            name_lower = resume.stem.lower()
            for label in labels:
                if label.lower() in name_lower:
                    counts[label] += 1
                    break

    if not counts:
        print("No generated resumes found under applications/ or applications/archived/.")
        return {}
    print("Resume variant split (generated_materials/*Resume.*):")
    for label, n in counts.most_common():
        print(f"  {label:<12}: {n}")
    return dict(counts)


# ---------- Pipeline-run timings, from the file watcher's trigger server ----------
#
# Folded in from a standalone collect_pipeline_timings.py rather than kept as
# its own separate module/import target -- this file's own report-per-section
# structure (print_pending_count / print_archive_breakdown / print_variant_split)
# is the one place run/report logic for this project lives; a second
# independently-importable "library" script for one more section would just
# be a second convention to remember.


def _seconds_between(start_iso: str | None, end_iso: str | None) -> float | None:
    if not start_iso or not end_iso:
        return None
    return (datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)).total_seconds()


def _file_watcher_base_url() -> str:
    """Reads server.host/port from config/file_watcher.json rather than
    hardcoding 127.0.0.1:8765 a second time -- that config is already
    the single source of truth for where the watcher's trigger server
    listens (see src/auto_queue/py_file_watcher.py). Falls back to the
    documented default if the config is missing or unreadable, since
    the file watcher itself is optional -- no config file at all is a
    normal state, not an error here."""
    try:
        cfg = json.loads(FILE_WATCHER_CONFIG_PATH.read_text(encoding="utf-8"))
        server = cfg.get("server", {})
        return f"http://{server.get('host', '127.0.0.1')}:{server.get('port', 8765)}"
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return "http://127.0.0.1:8765"


def _fetch_json(base_url: str, path: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def print_pipeline_timings() -> list[dict] | None:
    """Pulls recorded runtimes + timing metrics from the file watcher's
    local trigger server (src/auto_queue/py_file_watcher.py's /runs and
    /metrics routes) and prints one line per application: total
    pipeline runtime (started_at -> finished_at, computed here -- /runs
    doesn't return a pre-computed duration) plus the watcher's own
    two-stage breakdown (intake -> jd_input.json landing, jd_input.json
    -> materials export, both already computed server-side into
    /metrics' intake_to_jd_seconds / jd_to_materials_seconds).

    Returns None (not []) if the watcher isn't reachable -- distinct
    from "reachable but zero runs recorded yet" -- so the caller (and
    the logged record) can tell "watcher not running" apart from
    "watcher running, nothing through it yet." Best-effort and
    non-fatal: the watcher not running is a completely normal state
    (it's opt-in automation), not an error condition."""
    base_url = _file_watcher_base_url()
    try:
        runs = _fetch_json(base_url, "/runs")["runs"]
        metrics = _fetch_json(base_url, "/metrics")["metrics"]
    except (urllib.error.URLError, OSError, ValueError):
        print(f"File watcher not reachable at {base_url} -- skipping pipeline timings "
              f"(normal if it isn't running).")
        return None

    metrics_by_app = {m["application_id"]: m for m in metrics}
    merged = []
    for run in runs:
        app_id = run["application_id"]
        m = metrics_by_app.get(app_id, {})
        merged.append({
            "application_id": app_id,
            "status": run["status"],
            "pipeline_runtime_seconds": _seconds_between(run.get("started_at"), run.get("finished_at")),
            "intake_to_jd_seconds": m.get("intake_to_jd_seconds"),
            "jd_to_materials_seconds": m.get("jd_to_materials_seconds"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "log_file": run.get("log_file"),
        })

    if not merged:
        print("File watcher is running but no runs recorded yet.")
        return merged

    def fmt(v):
        return f"{v:.1f}s" if v is not None else "n/a"

    print("Pipeline run timings (from the file watcher's trigger server):")
    for row in merged:
        print(f"  {row['application_id']:<50s} status={row['status']:<8s} "
              f"runtime={fmt(row['pipeline_runtime_seconds'])} "
              f"intake->jd={fmt(row['intake_to_jd_seconds'])} "
              f"jd->materials={fmt(row['jd_to_materials_seconds'])}")
    return merged


# ---------- Extension extraction-runtime aggregation ----------

def print_extraction_runtimes() -> dict:
    """Aggregates JDInput.extraction_runtime_ms (see schemas.py) across
    every applications/*/jd_input*.json, live and archived. This field
    is stamped by the extension itself (background.js) -- only present
    on applications captured after that field was added, so anything
    older is simply skipped, not counted as a zero. Reports how many
    had the field at all, since that count itself is informative early
    on (most existing applications won't)."""
    values: list[float] = []
    total_seen = 0
    for root in _scan_roots():
        for jd_path in list(root.glob("*/jd_input.json")) + list(root.glob("*/jd_input.processed.json")):
            total_seen += 1
            try:
                data = json.loads(jd_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            ms = data.get("extraction_runtime_ms")
            if isinstance(ms, (int, float)):
                values.append(float(ms))

    if not values:
        print(f"No extraction_runtime_ms found on any of {total_seen} application(s) -- "
              f"expected until the extension update that stamps it is actually in use.")
        return {"applications_seen": total_seen, "with_runtime": 0}

    result = {
        "applications_seen": total_seen,
        "with_runtime": len(values),
        "avg_ms": sum(values) / len(values),
        "min_ms": min(values),
        "max_ms": max(values),
    }
    print(f"Extraction runtime: {result['with_runtime']}/{result['applications_seen']} application(s) "
          f"have extraction_runtime_ms -- avg {result['avg_ms']:.0f}ms, "
          f"min {result['min_ms']:.0f}ms, max {result['max_ms']:.0f}ms")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", action="store_true", help="Weekly applied/cut_off/revisit/test breakdown")
    ap.add_argument("--variants", action="store_true", help="Resume variant split (see config/resume_variants/)")
    ap.add_argument("--timings", action="store_true", help="Pipeline-run timings from the file watcher (if running)")
    ap.add_argument("--extraction", action="store_true", help="Extension extraction-runtime stats")
    ap.add_argument("--all", action="store_true", help="Print every section")
    args = ap.parse_args()

    record: dict = {}
    if args.all:
        record["pending"] = print_pending_count()
        print()
        record["archive_weeks"] = print_archive_breakdown()
        record["variant_split"] = print_variant_split()
        record["pipeline_timings"] = print_pipeline_timings()
        record["extraction_runtimes"] = print_extraction_runtimes()
    elif args.archive:
        record["archive_weeks"] = print_archive_breakdown()
    elif args.variants:
        record["variant_split"] = print_variant_split()
    elif args.timings:
        record["pipeline_timings"] = print_pipeline_timings()
    elif args.extraction:
        record["extraction_runtimes"] = print_extraction_runtimes()
    else:
        record["pending"] = print_pending_count()

    # log/applications_stats.jsonl -- same log_event() convention every
    # stage script already uses (auto-prepends a "ts" field), just one
    # record per checkpoint run instead of one per pipeline stage.
    log_event("applications_stats", record)
