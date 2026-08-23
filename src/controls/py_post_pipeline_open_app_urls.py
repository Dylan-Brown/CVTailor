#!/usr/bin/env python3
"""
py_post_pipeline_open_app_urls.py
=================================
Opens the source URL for every active application in a Chrome tab.

Usage:
    python src/controls/py_post_pipeline_open_app_urls.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import app_dirs

CHROME_CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
]


def collect_urls() -> list[str]:
    urls = []
    seen = set()
    for app_dir in app_dirs():
        for name in ("jd_input.json", "jd_input.processed.json"):
            jd_path = app_dir / name
            if not jd_path.is_file():
                continue
            try:
                data = json.loads(jd_path.read_text(encoding="utf-8"))
            except Exception:
                print(f"WARNING: failed to parse {jd_path}", file=sys.stderr)
                continue
            url = data.get("source_url")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
            break  # don't double-count both filenames for the same app
    return urls


def main():
    urls = collect_urls()
    if not urls:
        print("No source_url fields found in any applications/*/jd_input*.json.")
        return

    print(f"Found {len(urls)} URL(s). Opening in Google Chrome...")
    chrome_path = next((p for p in CHROME_CANDIDATES if p.is_file()), None)
    if chrome_path is None:
        print("ERROR: Google Chrome executable could not be found.", file=sys.stderr)
        sys.exit(1)

    subprocess.Popen([str(chrome_path), *urls])


if __name__ == "__main__":
    main()
