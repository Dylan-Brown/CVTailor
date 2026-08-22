"""
native_host.py

Native messaging host for the "Job Data Harvester" extension. Chrome
launches this as a subprocess and talks to it over stdin/stdout using
the native messaging protocol (4-byte little-endian length prefix +
UTF-8 JSON, both directions). This replaces the old flow of downloading
a JSON file to the Downloads folder and manually moving it -- the extension now
hands the parsed job data straight to this script, which writes it to
a small scratch file only stage0_intake.py itself touches, then calls
stage0_intake.py directly and reports the resulting app_id back to the
extension for the toast notification.

Reads exactly one message, responds once, exits -- Chrome starts a new
process per connection from the extension side (background.js calls
connectNative() + postMessage() once, then disconnects), so there's no
need for this to loop or stay resident.

NOT TESTED end-to-end -- no Chrome/Windows native messaging pipe
available in the sandbox this was built in. See extension/native_host/
README.md for the manual test procedure to run once this is installed.
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