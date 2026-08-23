#!/usr/bin/env python3
"""
native_host.py -- native messaging host for the Chrome extension; reads
one JSON message over stdin, hands it to stage 0, exits. NOT TESTED
end-to-end -- see extension/native_host/README.md for the manual test.
"""

import json
import struct
import subprocess
import sys
import uuid
from pathlib import Path

# native_host.py -> extension/ -> jobs/
CVTAILOR_ROOT = Path(__file__).resolve().parent.parent.parent
STAGE0 = CVTAILOR_ROOT / "src" / "stages" / "py_stage00_normalize_jd_input.py"
STAGING_DIR = Path(__file__).resolve().parent / "_incoming"


def read_message() -> dict:
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) == 0:
        sys.exit(0)  # Chrome closed the pipe with nothing to send -- exit quietly
    length = struct.unpack("<I", raw_length)[0]
    message_bytes = sys.stdin.buffer.read(length)
    return json.loads(message_bytes.decode("utf-8"))


def send_message(obj: dict) -> None:
    encoded = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> None:
    try:
        payload = read_message()
    except Exception as e:
        send_message({"ok": False, "error": f"Could not read message from extension: {e}"})
        return

    STAGING_DIR.mkdir(exist_ok=True)
    # Unique per invocation, not a fixed shared filename -- Chrome spawns
    # a SEPARATE native_host.py process per tab's extraction (confirmed:
    # background.js opens its own connectNative() per call, no shared
    # state), so multiple tabs extracting concurrently means multiple
    # native_host.py processes running at once. A fixed filename here
    # was a real, confirmed race: one process's write/unlink could stomp
    # on or delete another's in-flight file, causing either an outright
    # failure or, worse, one tab silently processing a DIFFERENT tab's
    # job data with no error at all.
    tmp_path = STAGING_DIR / f"incoming_jd_{uuid.uuid4().hex}.json"
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        result = subprocess.run(
            [sys.executable, str(STAGE0), "--jd-json", str(tmp_path)],
            capture_output=True, text=True, cwd=str(CVTAILOR_ROOT),
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    if result.returncode != 0:
        error_tail = (result.stderr or result.stdout).strip()[-500:]
        send_message({"ok": False, "error": error_tail or "stage0_intake.py failed with no output"})
        return

    app_id = None
    for line in result.stdout.splitlines():
        if "(app_id:" in line:
            app_id = line.split("(app_id:")[1].strip(" )")

    send_message({"ok": True, "app_id": app_id, "raw_output": result.stdout.strip()})


if __name__ == "__main__":
    main()