"""Entrypoints — the composition root that drives the service end to end.

This is the one module that depends on BOTH sides of the codebase: the core
utilities in ``src/`` and the LLM agent in ``agents/``. It lives at the repo
root (outside both) so the dependency graph stays one-directional:
``main -> {src, agents}`` and ``agents -> src``.

Two ways in, one body:
- HTTP (Cloud Run): Cloud Scheduler / Pub/Sub push hits POST /run.
- CLI (local / Cloud Run Job):  python main.py --once

The flow per heartbeat: hand the agent one procedural prompt and let it drive.
The agent finds the stale models itself (via its tools), then for each one
diagnoses, decides within server-side policy, acts, and alerts humans.

    uvicorn main:app --port 8080      # serve the HTTP entrypoint
    python main.py --once             # run one heartbeat locally
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from agents.edw_recovery_agent.agent import root_agent

# The per-heartbeat task. All decision policy lives in the agent's system
# instruction; this is just the ordered procedure for one run.
HEARTBEAT_PROMPT = """\
Run the freshness recovery procedure now. This run started at {run_time}.

1. Call get_stale_models to find every currently stale watched model.
2. If none are stale, send slack message to lorenzo.peve@tecovas.com (Lorenzo Peve)
3. For EACH stale model, in turn:
   a. Call get_model_status to see why it (or its sources) is stale.
   b. Decide the safest corrective action. Re-trigger a loader only when that
      is likely to help and is within policy; never re-run a loader that already
      succeeded — that points upstream.
   c. Take the action (refresh_model) if warranted, then re-check status.
   d. If a human needs to act (upstream problem, re-run refused, or staleness
      persists), call alert_humans with a clear diagnosis.
4. End with a concise summary: which models were stale, what you did for each,
   and which still need a human.

Use emoji generously in every Slack message so the status is readable at a
glance:
- Start the message with a single overall status emoji: ✅ all healthy,
  ⚠️ recovered / needs attention soon, 🚨 a human must act now.
- Prefix per-item lines too, e.g. ✅ healthy/recovered, 🔄 re-ran a loader,
  ⏳ waiting, 🚨 still stale / needs a human, 🔍 investigated.
For the "no models are stale" case, lead with ✅, e.g.
"✅ All clear — no dbt models are currently stale. The freshness recovery
procedure found no issues. 🎉".

Always include the run time ({run_time}) in the Slack message, e.g. a trailing
line like "🕒 Run time: {run_time}".
"""


async def _run_agent(prompt: str) -> str:
    """Drive the LLM agent for one run and return its final summary text."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    runner = Runner(
        agent=root_agent,
        app_name="edw_recovery_agent",
        session_service=InMemorySessionService(),
    )
    await runner.session_service.create_session(
        app_name="edw_recovery_agent", user_id="cron", session_id="heartbeat"
    )
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    final = ""
    async for event in runner.run_async(
        user_id="cron", session_id="heartbeat", new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final = event.content.parts[0].text or ""
    return final


async def run_once() -> None:
    """One heartbeat: run the agent against the procedural prompt and log it."""
    run_time = datetime.now(ZoneInfo("America/Chicago")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )
    summary = await _run_agent(HEARTBEAT_PROMPT.format(run_time=run_time))
    print(f"[agent] {summary}")


app = FastAPI(title="edw-data-control-agent")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
async def run() -> dict[str, str]:
    await run_once()
    return {"status": "completed"}


def main() -> None:
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
