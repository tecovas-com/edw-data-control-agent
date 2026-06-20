"""Entrypoints — the composition root that drives the service end to end.

This is the one module that depends on BOTH sides of the codebase: the core
utilities in ``src/`` and the LLM agent in ``agents/``. It lives at the repo
root (outside both) so the dependency graph stays one-directional:
``main -> {src, agents}`` and ``agents -> src``.

Two ways in, one body:
- HTTP (Cloud Run): Cloud Scheduler / Pub/Sub push hits POST /run.
- CLI (local / Cloud Run Job):  python main.py --once

The flow per heartbeat: check freshness -> run the deterministic runbook ->
escalate ambiguous cases to the LLM agent (which owns Slack alerting).

    uvicorn main:app --port 8080      # serve the HTTP entrypoint
    python main.py --once             # run one heartbeat locally
"""
from __future__ import annotations

import asyncio
import sys

from fastapi import FastAPI

from agents.edw_recovery_agent.agent import control_center, root_agent
from src.recovery import plan_recovery


async def _escalate(unique_id: str) -> str:
    """Run the LLM agent for one stale model and return its summary text."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    runner = Runner(
        agent=root_agent,
        app_name="edw_recovery_agent",
        session_service=InMemorySessionService(),
    )
    await runner.session_service.create_session(
        app_name="edw_recovery_agent", user_id="cron", session_id=unique_id
    )
    prompt = f"Model {unique_id} is stale and the runbook could not resolve it. Investigate and act."
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    final = ""
    async for event in runner.run_async(
        user_id="cron", session_id=unique_id, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final = event.content.parts[0].text or ""
    return final


async def run_once() -> None:
    """One heartbeat: check -> deterministic runbook -> escalate."""
    for model in control_center.list_models():
        if model.get("overall_is_fresh", True):
            continue
        unique_id = model["model_unique_id"]
        status = control_center.get_model_status(unique_id)
        result = plan_recovery(status)

        for action in result.actions:
            if action.kind == "retrigger_loader":
                # TODO: consult core.may_retrigger with recent attempts first.
                control_center.trigger_loader(action.loader_type, action.loader_id)
                print(f"[runbook] re-triggered {action.loader_type}:{action.loader_id}")

        if result.escalate:
            # The agent owns alerting: it calls `alert_humans` (Slack) when a
            # human needs to act. We just log the final summary here.
            summary = await _escalate(unique_id)
            print(f"[agent] {unique_id}: {summary}")


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
