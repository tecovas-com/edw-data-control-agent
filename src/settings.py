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

# --- BigQuery (read-only query tools; see src/bigquery.py) -------------------
# No defaults here — values come from the environment (`.env` locally, see
# `.env.template`; real env on Cloud Run). Unset vars read as None/empty, matching
# the SLACK_* pattern above, so the package still imports without a populated env.
# The dataset allowlist is a FRIENDLY pre-check only — the real boundary is the
# SA's IAM/ACL grants (see scripts/provision_bq_readonly_sa.py). Authorized-source
# datasets are the raw datasets reachable *through* authorized views (e.g. the
# stg_wfx__* views in dbt_views read raw_wfx).


def _csv(name: str) -> frozenset[str]:
    return frozenset(d.strip() for d in os.environ.get(name, "").split(",") if d.strip())


def _opt_int(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value else None


def _opt_float(name: str) -> float | None:
    value = os.environ.get(name)
    return float(value) if value else None


BQ_PROJECT = os.environ.get("BQ_PROJECT")
BQ_ALLOWED_DATASETS = _csv("BQ_ALLOWED_DATASETS")
BQ_AUTHORIZED_SOURCE_DATASETS = _csv("BQ_AUTHORIZED_SOURCE_DATASETS")
BQ_MAX_BYTES_BILLED = _opt_int("BQ_MAX_BYTES_BILLED")
BQ_MAX_ROWS = _opt_int("BQ_MAX_ROWS")
BQ_MAX_RESULT_BYTES = _opt_int("BQ_MAX_RESULT_BYTES")
BQ_QUERY_TIMEOUT_S = _opt_float("BQ_QUERY_TIMEOUT_S")
BQ_LOCATION = os.environ.get("BQ_LOCATION")
