"""The recovery agent (the escalation path) — exposes `root_agent` for ADK.

`adk web` discovers an agent by importing this module and reading the
module-level `root_agent`. The clients are constructed here at import (this file
IS the edge for the dev UI); the same `control_center`/`slack` objects are reused
by the entrypoints in main.py.

The model runs through ADK's LiteLlm wrapper so we can use Claude. Override with
EDCA_MODEL (e.g. a Gemini id for native ADK). ANTHROPIC_API_KEY (or Vertex
creds) must be present at runtime.
"""
from __future__ import annotations

from typing import Any

import httpx
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from slack_sdk import WebClient

from . import settings
from .clients import ControlCenterClient, SlackClient, fetch_id_token
from .core import build_alert_blocks

SYSTEM_INSTRUCTION = """\
You are the data-platform recovery agent for Tecovas' EDW.

You are invoked only when the deterministic runbook could NOT confidently resolve
a stale pipeline. Your job: diagnose why a watched dbt model or its sources are
stale, decide on the safest corrective action, and report clearly.

Available tools let you inspect freshness and re-trigger loaders. Rules:
- Re-running a loader costs money and compute. Prefer the smallest action.
- Before re-triggering, check the model status to confirm it is still stale and
  the loader is not already running.
- If a loader ran and SUCCEEDED but data is still stale, do NOT blindly re-run —
  the problem is likely upstream. Investigate and escalate to a human instead.
- The re-trigger tool may refuse you (server-side rate limit). Respect it; never
  loop trying to force a re-run.
- Always end by summarizing: what was stale, what you did (or chose not to do),
  and whether a human needs to act.
- When a human needs to act (the problem is upstream, a re-run was refused, or a
  re-run did not resolve the staleness), call `alert_humans` to post a Slack
  alert with a clear diagnosis. Do not alert for routine, self-resolved cases.

Be concise and factual. You are talking to data engineers.
"""

# --- concrete clients (constructed at the edge) -----------------------------

control_center = ControlCenterClient(
    base_url=settings.CONTROL_CENTER_URL,
    http=httpx.Client(timeout=settings.REQUEST_TIMEOUT_S),
    token_provider=fetch_id_token,
)
slack = SlackClient(
    web=WebClient(token=settings.SLACK_BOT_TOKEN),
    default_channel=settings.SLACK_CHANNEL,
)


# --- tools (plain functions; ADK reads name/signature/docstring) ------------


def list_watched_models() -> list[dict[str, Any]]:
    """List all watched dbt models and whether each is currently fresh."""
    return control_center.list_models()


def get_model_status(unique_id: str) -> dict[str, Any]:
    """Get the full freshness status for one model, including every source.

    Args:
        unique_id: the dbt unique_id, e.g. "model.tecovas.fct_sales".
    """
    return control_center.get_model_status(unique_id)


def retrigger_loader(loader_type: str, loader_id: str) -> dict[str, Any]:
    """Request a re-run of a loader. May be refused by server-side limits.

    Args:
        loader_type: one of "fivetran", "airflow", "cloud_run_jobs",
            "dbt_artifacts".
        loader_id: the loader's id as configured in the control center.
    """
    return control_center.trigger_loader(loader_type, loader_id)


def alert_humans(
    unique_id: str,
    summary: str,
    failing_sources: list[str] | None = None,
    actions_taken: list[str] | None = None,
) -> dict[str, Any]:
    """Post a stale-pipeline alert to the team's Slack channel.

    Use this when a human needs to act — e.g. the problem is upstream, or a
    re-run was refused or did not resolve the staleness.

    Args:
        unique_id: the stale model's dbt unique_id.
        summary: plain-text diagnosis: what is stale, what you did or chose not
            to do, and what the human should do next.
        failing_sources: optional list of stale source identifiers.
        actions_taken: optional list of actions you already took.
    """
    blocks = build_alert_blocks(
        unique_id,
        summary,
        failing_sources=failing_sources,
        actions_taken=actions_taken,
    )
    return slack.post_alert(text=f"Stale pipeline: {unique_id}", blocks=blocks)


root_agent = LlmAgent(
    name="edw_recovery_agent",
    model=LiteLlm(model=settings.MODEL),
    instruction=SYSTEM_INSTRUCTION,
    tools=[list_watched_models, get_model_status, retrigger_loader, alert_humans],
)
