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
   (`core/runbook.py`): re-trigger the loader once, re-check.
4. **Escalate** — if the runbook can't resolve it, or the situation is
   ambiguous, hand it to the LLM agent (`agent/loop.py`), which diagnoses,
   decides within policy (`core/policy.py`), and posts a rich alert.

## Guardrails

Recovery actions cost money and compute. **The hard guardrails (e.g. "re-run a
loader at most once per hour") live server-side in the control center's MCP tool
layer**, not in this agent's prompt. `core/policy.py` here is a *second*,
client-side check so the agent fails fast and reasons about limits — but it is
not the source of truth. Never let the agent issue an unbounded re-run loop.

## Conventions (inherited from control-center)

- **Python ≥ 3.11**, type hints everywhere.
- `dataclasses` in `core/`. `pydantic` only at boundaries (config, API payloads).
- **`datetime.now()` is forbidden in `core/`** — always accept `now: datetime`.
- **No I/O in `core/`** — the MCP/HTTP client, LLM client, and Slack client are
  injected. Concrete clients are constructed only in `entrypoints/`.
- **No pytest fixtures.** Plain test functions; stub clients in `tests/stubs.py`.
- **Fail loud** on misconfiguration at startup.
- `core/` is pure decision logic and must be testable with no network, no GCP,
  no LLM — stub everything.

## Layout

```
src/edca/
├── client/
│   ├── auth.py          # mint GCP IAM ID tokens (audience = control-center URL)
│   └── control_center.py# httpx wrapper over the freshness API/MCP
├── core/                # PURE decision logic — no I/O, no datetime.now()
│   ├── runbook.py       # deterministic recovery decisions
│   └── policy.py        # client-side guardrail checks
├── agent/
│   ├── loop.py          # LLM reasoning loop (escalation path)
│   └── prompts.py       # system prompt + templates
└── entrypoints/
    ├── cron.py          # scheduled heartbeat: check → recover → escalate
    └── handler.py       # event-driven (Pub/Sub) entry
config/agent.yaml        # which models to watch, thresholds, escalation targets
tests/                   # plain functions + stubs.py
```

## Build order (TDD, green before next step)

1. `client/auth.py` + `client/control_center.py` — talk to the API (stub httpx).
2. `core/policy.py` — guardrail checks (pure).
3. `core/runbook.py` — deterministic recovery decisions (pure).
4. `entrypoints/cron.py` — wire check → runbook against stub client.
5. `agent/loop.py` — escalation path with injected LLM client.
6. `entrypoints/handler.py` + deploy (Cloud Run + Scheduler/Pub-Sub).
