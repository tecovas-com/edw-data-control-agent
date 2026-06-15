# edw-data-control-agent

Alerting + recovery agent for the data platform. It is a **consumer** of
[`edw-data-control-center`](../edw-data-control-center): it polls that service's
freshness API, runs a deterministic recovery runbook for known failures, and
escalates ambiguous cases to an LLM agent that diagnoses, acts within policy,
and alerts a human.

This repo never imports the control center's core — it talks to it over the
network with a GCP IAM ID token. See `CLAUDE.md` for architecture and conventions.

## Quick start

```bash
pip install -e ".[dev]"
pytest -q

# point at the control center
export CONTROL_CENTER_URL="https://edw-data-control-center-xxxx.run.app"

# run one heartbeat locally (uses ambient GCP credentials)
python -m edca.entrypoints.cron --once
```

## Layout

See `CLAUDE.md`. Decision logic is pure and lives in `src/edca/core/`; all I/O
(HTTP, IAM, LLM) is injected and constructed only in `src/edca/entrypoints/`.
