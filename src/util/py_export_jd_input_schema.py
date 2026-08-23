#!/usr/bin/env python3
"""
py_export_jd_input_schema.py
============================
Regenerates `contracts/jd_input.schema.json` from the `JDInput` Pydantic model. 
Run this after making changes in `src/core/schemas.py` to keep the public extension contract synced.

Usage:
    python src/util/py_export_jd_input_schema.py [--check]
"""
import argparse
import json
import sys
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/util/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
from schemas import JDInput

SCHEMA_PATH = _JOBS_ROOT / "contracts" / "jd_input.schema.json"


def render_schema() -> str:
    schema = JDInput.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def run(check: bool) -> None:
    rendered = render_schema()
    current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.is_file() else None

    if current == rendered:
        print(f"{SCHEMA_PATH} is already up to date with JDInput.")
        return

    if check:
        print(f"{SCHEMA_PATH} is out of date with JDInput -- run without --check to regenerate it.",
              file=sys.stderr)
        sys.exit(1)

    SCHEMA_PATH.write_text(rendered, encoding="utf-8")
    print(f"Wrote {SCHEMA_PATH} from JDInput.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                     help="Don't write -- just exit non-zero if the schema file is stale (for CI/pre-commit use).")
    args = ap.parse_args()
    run(args.check)
