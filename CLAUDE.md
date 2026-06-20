# CLAUDE.md — `edw-data-control-agent`

Read this before writing code. It defines what this service is, how it relates to
`edw-data-control-center`, and the conventions you must follow.

## What this is

A **consumer** of the `edw-data-control-center` freshness API. It watches pipeline
freshness, and when something is stale it either runs a deterministic recovery
runbook or escalates to an LLM agent that diagnoses and decides what to do
(re-run a loader, wait, or alert a human).

This repo is intentionally **separate** from `edw-data-control-center`:

| | control-center | this repo (agent) |
|---|---|---|
| Role | System of record for freshness | Consumer / actor |
| I/O | Forbidden in core, injected at edges | Its whole job |
| Determinism | Pure input→output | LLM calls, side effects |
| Failure mode | Wrong answer = bug | Wrong action = re-ran a job, cost money |

It reaches the control center **only over the network**, through that service's
MCP tools / REST API, authenticated with a **GCP IAM ID token** from a dedicated
service account. It never imports `edc.core` directly — the API is the contract.

## The recovery flow (hybrid)

1. **Trigger** — a periodic heartbeat (cron / Cloud Scheduler) or an event
   (Pub/Sub from a finished dbt/loader run) fires the entrypoint.
2. **Check** — call the control center for stale models/sources.
3. **Deterministic recovery** — for known failure modes, run the runbook
   (`plan_recovery` in `core.py`): re-trigger the loader once, re-check.
4. **Escalate** — if the runbook can't resolve it, or the situation is
   ambiguous, hand it to the LLM agent (`root_agent` in `agent.py`), which
   diagnoses, decides within policy (`may_retrigger` in `core.py`), and posts a
   rich alert.

## Guardrails

Recovery actions cost money and compute. **The hard guardrails (e.g. "re-run a
loader at most once per hour") live server-side in the control center's MCP tool
layer**, not in this agent's prompt. `may_retrigger` in `core.py` is a *second*,
client-side check so the agent fails fast and reasons about limits — but it is
not the source of truth. Never let the agent issue an unbounded re-run loop.

## Conventions (inherited from control-center)

- **Python ≥ 3.11**, type hints everywhere.
- `dataclasses` for the pure types in `core.py`. `pydantic` only at boundaries.
- **`datetime.now()` is forbidden in `core.py`** — always accept `now: datetime`.
- **No I/O in `core.py`** — the HTTP, LLM, and Slack clients live in `clients.py`
  and are constructed at the edge (`agent.py` / `main.py`), never in `core.py`.
- **No pytest fixtures.** Plain test functions; stub clients in `tests/stubs.py`.
- `core.py` is pure decision logic and must be testable with no network, no GCP,
  no LLM — stub everything.
- Config is plain module-level constants in `settings.py`, read from env with
  dev-safe defaults so `adk web` and tests import the package cleanly.

## Layout

Flat modules — one file per layer (pure ↔ I/O ↔ edge), no nested packages.

```
src/
├── __init__.py     # marks src as a package (needed for adk web + imports)
├── settings.py     # env-read config constants
├── core.py         # PURE decision logic: plan_recovery, may_retrigger,
│                   #   verify_slack_signature, build_alert_blocks
├── clients.py      # I/O adapters: fetch_id_token, ControlCenterClient, SlackClient
├── agent.py        # the LLM agent (escalation path): tools + `root_agent`
└── main.py         # entrypoints: run_once heartbeat + FastAPI app (/run, /healthz)
config/agent.yaml   # which models to watch, thresholds, escalation targets
tests/              # plain functions + stubs.py
```

## Running

- **Dev UI:** `adk web` from the repo root — it loads `src.agent.root_agent`.
- **HTTP (Cloud Run):** `uvicorn src.main:app` (Scheduler/Pub-Sub hits POST /run).
- **One heartbeat locally:** `python -m src.main --once`.
- **Tests:** `pytest -k "not live"` (live Slack tests need SLACK_BOT_TOKEN/CHANNEL).

## Build order (TDD, green before next step)

1. `clients.py` — talk to the API and Slack (stub httpx / WebClient).
2. `core.py` — guardrail checks + deterministic recovery decisions (pure).
3. `main.py` — wire check → runbook against a stub client.
4. `agent.py` — escalation path: tools + `root_agent`.
5. Deploy (Cloud Run + Scheduler/Pub-Sub).
