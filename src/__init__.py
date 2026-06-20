"""Runtime configuration, read from the environment at the edges.

Plain module-level constants — read once at import. Dev-safe defaults let
`adk web` and tests import the package without a fully-populated environment;
production sets the real values via env (Cloud Run) or a local `.env`.
"""
from __future__ import annotations

import os

CONTROL_CENTER_URL = os.environ.get("CONTROL_CENTER_URL", "http://localhost:8080")
MODEL = os.environ.get("EDCA_MODEL", "anthropic/claude-opus-4-8")
REQUEST_TIMEOUT_S = float(os.environ.get("EDCA_TIMEOUT_S", "30"))

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "#data-alerts")
