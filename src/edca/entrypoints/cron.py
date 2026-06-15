"""Heartbeat entrypoint: check freshness -> deterministic runbook -> escalate.

Run locally:  python -m edca.entrypoints.cron --once
On Cloud Run:  triggered by Cloud Scheduler hitting this as a Job, or served as
an HTTP endpoint (see handler.py) that Scheduler invokes.

This is the ONLY place concrete clients are constructed. Core stays pure.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from edca.agent.agent import build_agent
from edca.client.auth import fetch_id_token
from edca.client.control_center import ControlCenterClient
from edca.core.runbook import plan_recovery
from edca.settings import Settings


def _build_client(settings: Settings) -> ControlCenterClient:
    http = httpx.Client(timeout=settings.request_timeout_s)
    return ControlCenterClient(
        base_url=settings.control_center_url,
        http=http,
        token_provider=fetch_id_token,
    )


async def _escalate(client: ControlCenterClient, settings: Settings, unique_id: str) -> str:
    """Run the LLM agent for one stale model and return its summary text."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    agent = build_agent(client, model=settings.model)
    runner = Runner(
        agent=agent,
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


async def run_once(settings: Settings) -> None:
    client = _build_client(settings)
    for model in client.list_models():
        if model.get("overall_is_fresh", True):
            continue
        unique_id = model["model_unique_id"]
        status = client.get_model_status(unique_id)
        result = plan_recovery(status)

        for action in result.actions:
            if action.kind == "retrigger_loader":
                # TODO: consult core.policy with recent attempts before firing.
                client.trigger_loader(action.loader_type, action.loader_id)
                print(f"[runbook] re-triggered {action.loader_type}:{action.loader_id}")

        if result.escalate:
            summary = await _escalate(client, settings, unique_id)
            print(f"[agent] {unique_id}: {summary}")
            # TODO: post `summary` to Slack / alerting.


def main() -> None:
    settings = Settings.from_env()
    if "--once" in sys.argv:
        asyncio.run(run_once(settings))
    else:
        asyncio.run(run_once(settings))


if __name__ == "__main__":
    main()
