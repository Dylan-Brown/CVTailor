#!/usr/bin/env python3
"""
py_notify_telegram.py -- shared Telegram push helper for CVTailor's
discovery and submission lanes. Sends a plain-text message via a
dedicated bot (see config/telegram.yaml + config/credentials/
telegram_bot_token.txt) to your own chat -- a bot created specifically
for CVTailor, deliberately separate from unrelated personal Telegram
bots elsewhere on this machine.

Usage (manual test):
    python src/util/py_notify_telegram.py "test message"

Import usage (other CVTailor scripts):
    from py_notify_telegram import send_telegram_message
    send_telegram_message("3 new postings found")
"""
import os
import sys
from pathlib import Path

import requests
import yaml

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
from batch_common import CONFIG_ROOT

TELEGRAM_CONFIG_PATH = CONFIG_ROOT / "telegram.yaml"
CREDENTIALS_DIR = CONFIG_ROOT / "credentials"
_PLACEHOLDER_PREFIX = "PLACEHOLDER_REPLACE_WITH_YOUR_REAL_"


def _load_credential(env_var: str, filename: str) -> str | None:
    """Same convention as py_poll_adzuna.py's _load_credential: $env:<var>
    wins if set, otherwise the first non-comment, non-blank,
    non-placeholder line of config/credentials/<filename>."""
    if os.environ.get(env_var):
        return os.environ[env_var]
    path = CREDENTIALS_DIR / filename
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return None if line.startswith(_PLACEHOLDER_PREFIX) else line
    return None


def load_config() -> dict:
    if not TELEGRAM_CONFIG_PATH.is_file():
        return {}
    return yaml.safe_load(TELEGRAM_CONFIG_PATH.read_text(encoding="utf-8")) or {}


def send_telegram_message(text: str) -> bool:
    """Best-effort push: returns False (and prints a warning to stderr)
    instead of raising, so a missing/misconfigured Telegram setup never
    breaks the poller or submission run that's calling this."""
    cfg = load_config()
    if not cfg.get("enabled", True):
        return False
    token = _load_credential("TELEGRAM_BOT_TOKEN", "telegram_bot_token.txt")
    chat_id = cfg.get("chat_id")
    if not token or not chat_id:
        print("[telegram] not configured (missing bot token or chat_id) -- "
              "skipping notification. See config/telegram.example.yaml and "
              "config/credentials/telegram_bot_token.example.txt.", file=sys.stderr)
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[telegram] send failed: {e}", file=sys.stderr)
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python py_notify_telegram.py <message text>", file=sys.stderr)
        sys.exit(1)
    ok = send_telegram_message(" ".join(sys.argv[1:]))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
