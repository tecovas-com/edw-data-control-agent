"""Runtime configuration, read from the environment at the edges.

Plain module-level constants — read once at import. Dev-safe defaults let
`adk web` and tests import the package without a fully-populated environment;
production sets the real values via env (Cloud Run) or a local `.env`.
"""
from __future__ import annotations

import os

CONTROL_CENTER_URL = os.environ.get("CLOUD_RUN_DATA_CONTROL_URL", "http://localhost:8080")
MODEL = "claude-opus-4-8"

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL")
