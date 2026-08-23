#!/usr/bin/env python3
"""
py_stage00_normalize_jd_input.py — Stage 0: Intake & Normalization
==================================================================
The validation boundary for incoming job descriptions. 

Automatically detects whether the input is raw Job Data Harvester output or 
canonical JSON, normalizes it to the strict `JDInput` schema, and writes it 
to `applications/{app_id}/jd_input.json`.

Usage:
    python py_stage00_normalize_jd_input.py --jd-json path/to/jd.json [--app-id <id>]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/stages/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
sys.path.append(str(_JOBS_ROOT / "src" / "adapters"))
from io_utils import log_event, save_model
from schemas import JDInput
from harvester_adapter import is_harvester_format, normalize_harvester_json
from batch_common import APPLICATIONS_ROOT

# Force UTF-8 encoding for Windows terminals to prevent UnicodeEncodeError on emojis
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "_", text).strip("_")


def run(jd_json_path: str, source_url: str | None, app_id: str | None,
        applications_root: str, dry_run: bool = False) -> str | None:
    raw = json.loads(Path(jd_json_path).read_text(encoding="utf-8"))

    if is_harvester_format(raw):
        raw = normalize_harvester_json(raw, source_url=source_url)
        detected_format = "job_data_harvester"
    else:
        if source_url and not raw.get("source_url"):
            raw["source_url"] = source_url
        detected_format = "canonical_jdinput"

    try:
        jd = JDInput.model_validate(raw)
    except Exception as e:
        print(f"REJECTED: {jd_json_path} does not satisfy the JDInput contract "
              f"(detected format: {detected_format}).\n{e}", file=sys.stderr)
        log_event("stage0_intake", {
            "source": jd_json_path, "detected_format": detected_format,
            "status": "rejected", "error": str(e),
        })
        sys.exit(1)

    if not app_id:
        date = time.strftime("%Y-%m-%d")
        app_id = f"{slugify(jd.company)}_{slugify(jd.role_title)}_{date}"

    out_dir = Path(applications_root) / app_id
    readable_name = f"{slugify(jd.company)}_{slugify(jd.role_title)}_jd_input.json"

    if dry_run:
        print(f"[dry-run] would validate and write -> {out_dir / 'jd_input.json'} "
              f"(app_id: {app_id}, detected format: {detected_format}) -- no files written.")
        print(f"[dry-run] would also save readable duplicate as {readable_name}")
        return app_id

    save_model(out_dir / "jd_input.json", jd, dry_run=False)

    # Human-readable duplicate, for browsing the applications/ folder
    # yourself -- every stage past this point reads the canonical
    # "jd_input.json" name above, so that one's untouched; this is
    # purely so you're not stuck reading a generic filename to figure
    # out which folder is which application.
    save_model(out_dir / readable_name, jd, dry_run=False)

    log_event("stage0_intake", {
        "source": jd_json_path, "detected_format": detected_format, "app_id": app_id,
        "company": jd.company, "role_title": jd.role_title,
        "extraction_method": jd.extraction_method,
        "requirements_provided": len(jd.requirements_raw),
        "has_source_url": jd.source_url is not None,
        "status": "accepted",
    })
    print(f"Accepted ({detected_format}) -> {out_dir / 'jd_input.json'}  (app_id: {app_id})")
    print(f"  (also saved as {readable_name} for easier browsing)")
    if jd.source_url is None:
        print("  note: no source_url captured — pass --source-url next time if you "
              "want it in research citations.", file=sys.stderr)
    return app_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd-json", required=True)
    ap.add_argument("--source-url", default=None,
                     help="The extension doesn't capture this — pass it manually if you want "
                          "it available for research citations / dedup.")
    ap.add_argument("--app-id", default=None)
    ap.add_argument("--applications-root", default=str(APPLICATIONS_ROOT))
    ap.add_argument("--dry-run", action="store_true",
                     help="Validate and show what would be written, without writing anything.")
    args = ap.parse_args()
    run(args.jd_json, args.source_url, args.app_id, args.applications_root, args.dry_run)
