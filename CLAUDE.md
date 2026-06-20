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
   (`plan_recovery` in `recovery.py`): re-trigger the loader once, re-check.
4. **Escalate** — if the runbook can't resolve it, or the situation is
   ambiguous, hand it to the LLM agent (`root_agent` in `agent.py`), which
   diagnoses, decides within policy (`may_retrigger` in `recovery.py`), and posts a
   rich alert.

## Guardrails

Recovery actions cost money and compute. **The hard guardrails (e.g. "re-run a
loader at most once per hour") live server-side in the control center's MCP tool
layer**, not in this agent's prompt. `may_retrigger` in `recovery.py` is a *second*,
client-side check so the agent fails fast and reasons about limits — but it is
not the source of truth. Never let the agent issue an unbounded re-run loop.

## Conventions (inherited from control-center)

- **Python ≥ 3.11**, type hints everywhere.
- `dataclasses` for the pure types in `recovery.py`. `pydantic` only at boundaries.
- **`datetime.now()` is forbidden in `recovery.py`** — always accept `now: datetime`.
- **No I/O in `recovery.py`** — the HTTP, LLM, and Slack clients live in their own
  modules and are constructed at the edge (`agent.py` / `main.py`), never in
  `recovery.py`. (`slack.py` also holds pure Slack helpers alongside its client.)
- **No pytest fixtures.** Plain test functions; stub clients in `tests/stubs.py`.
- `recovery.py` is pure decision logic and must be testable with no network, no
  GCP, no LLM — stub everything.
- Config is plain module-level constants in `settings.py`, read from env with
  dev-safe defaults so `adk web` and tests import the package cleanly.

## Layout

Three concerns, one-directional deps: `main.py -> {src, agents}` and
`agents -> src`. `src/` is pure core (imports nothing internal); the agent lives
in `agents/`; the entrypoint sits at the root, above both.

```
main.py             # composition root / entrypoints: run_once heartbeat + FastAPI app (/run, /healthz)
src/                # core utilities — flat modules, one file per layer (pure ↔ I/O ↔ edge)
├── __init__.py     # marks src as a package
├── settings.py     # env-read config constants
├── recovery.py     # PURE decision logic: plan_recovery (runbook), may_retrigger (policy)
├── auth.py         # mint GCP IAM ID tokens (audience = control-center URL)
├── data_client.py  # requests wrapper over the freshness API/MCP
└── slack.py        # Slack client + pure helpers (build_alert_blocks, verify_slack_signature)
agents/             # ADK agents dir (adk web scans this); folder name = app name
└── edw_recovery_agent/
    ├── __init__.py # exposes root_agent
    └── agent.py    # the LLM agent (escalation path): tools + `root_agent` + client wiring
config/agent.yaml   # which models to watch, thresholds, escalation targets
tests/              # plain functions + stubs.py
```

## Running

- **Dev UI:** `adk web agents` from the repo root — app shows as `edw_recovery_agent`.
- **HTTP (Cloud Run):** `uvicorn main:app` (Scheduler/Pub-Sub hits POST /run).
- **One heartbeat locally:** `python main.py --once`.
- **Tests:** `pytest -k "not live"` (live Slack tests need SLACK_BOT_TOKEN/CHANNEL).

## Build order (TDD, green before next step)

1. `auth.py` + `data_client.py` + `slack.py` — talk to the API and Slack
   (stub requests.Session / WebClient).
2. `recovery.py` — guardrail checks + deterministic recovery decisions (pure).
3. `main.py` (repo root) — wire check → runbook against a stub client.
4. `agents/edw_recovery_agent/agent.py` — escalation path: tools + `root_agent`.
5. Deploy (Cloud Run + Scheduler/Pub-Sub).
