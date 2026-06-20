"""Make `.env` available to the test environment.

`pytest` (unlike `adk web`) does not auto-load `.env`, so live tests that read
SLACK_BOT_TOKEN / SLACK_CHANNEL would otherwise fail. This loads the repo-root
`.env` at collection time without adding a python-dotenv dependency. Real
environment variables always win over `.env` values.
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV_FILE = Path(__file__).parent / ".env"


def _load_env() -> None:
    if not _ENV_FILE.exists():
        return
    for raw in _ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip an inline ` # comment` (e.g. SLACK_CHANNEL=C123 # notes).
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        value = value.strip('"').strip("'")
        os.environ.setdefault(key, value)  # don't clobber a real env var


_load_env()
