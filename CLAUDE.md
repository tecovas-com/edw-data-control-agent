# CLAUDE.md — `edw-data-control-agent`

Read this before writing code. It defines what this service is, how it relates to
`edw-data-control-center`, and the conventions you must follow.

## What this is

A **consumer** of the `edw-data-control-center` freshness API. It watches pipeline
freshness, and when something is stale it hands the case to an LLM agent that
diagnoses and decides what to do (re-run a loader, wait, or alert a human).

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

## The recovery flow (agentic)

1. **Trigger** — a periodic heartbeat (cron / Cloud Scheduler) or an event
   (Pub/Sub from a finished dbt/loader run) fires the entrypoint.
2. **Hand the agent one procedural prompt** — `run_once` runs the LLM agent
   (`root_agent` in `agent.py`) once against `HEARTBEAT_PROMPT`, the ordered
   procedure for a run.
3. **Agent owns recovery** — there is no deterministic runbook and no Python
   orchestration loop: the agent finds the stale models itself (its tools),
   then for each inspects status, decides the safest corrective action,
   re-triggers loaders when warranted, and posts a rich alert when a human
   needs to act.

## Guardrails

Recovery actions cost money and compute. **The hard guardrails (e.g. "re-run a
loader at most once per hour") live server-side in the control center's MCP tool
layer**, not in this agent's prompt. The agent's system prompt instructs it to
prefer the smallest action, confirm staleness before re-triggering, respect a
server-side refusal, and never loop trying to force a re-run — but the server is
the source of truth. Never let the agent issue an unbounded re-run loop.

## Conventions (inherited from control-center)

- **Python ≥ 3.11**, type hints everywhere.
- `pydantic` only at boundaries.
- **No I/O in pure helpers** — the HTTP, LLM, and Slack clients live in their own
  modules and are constructed at the edge (`agent.py` / `main.py`). `slack.py`
  holds pure helpers (`build_alert_blocks`, `verify_slack_signature`) alongside
  its client; keep those testable with no network, no clock — inject `now`.
- **No pytest fixtures.** Plain test functions; stub clients in `tests/stubs.py`.
- Config is plain module-level constants in `settings.py`, read from env with
  dev-safe defaults so `adk web` and tests import the package cleanly.
- **No YAML config files.** All configuration is env-read constants in
  `settings.py` — do not add `config/*.yaml` or a YAML loader.

## Layout

Three concerns, one-directional deps: `main.py -> {src, agents}` and
`agents -> src`. `src/` is pure core (imports nothing internal); the agent lives
in `agents/`; the entrypoint sits at the root, above both.

```
main.py             # composition root / entrypoints: run_once heartbeat + FastAPI app (/run, /healthz)
src/                # core utilities — flat modules, one file per layer (pure ↔ I/O ↔ edge)
├── __init__.py     # marks src as a package
├── settings.py     # env-read config constants
├── auth.py         # mint GCP IAM ID tokens (audience = control-center URL)
├── data_client.py  # requests wrapper over the freshness API/MCP
└── slack.py        # Slack client + pure helpers (build_alert_blocks, verify_slack_signature)
agents/             # ADK agents dir (adk web scans this); folder name = app name
└── edw_recovery_agent/
    ├── __init__.py # exposes root_agent
    └── agent.py    # the LLM agent: tools + `root_agent` + client wiring
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
2. `agents/edw_recovery_agent/agent.py` — the agent: tools + `root_agent`.
3. `main.py` (repo root) — run the agent once against `HEARTBEAT_PROMPT`.
4. Deploy (Cloud Run + Scheduler/Pub-Sub).
