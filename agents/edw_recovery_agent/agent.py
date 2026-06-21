"""The recovery agent — exposes `root_agent` for ADK.

`adk web` discovers an agent by importing this module and reading the
module-level `root_agent`. The clients are constructed here at import (this file
IS the edge for the dev UI); the same `control_center`/`slack` objects are reused
by the entrypoints in main.py.

The model is Claude served from Vertex AI via ADK's native `Claude` class
(model id pinned in settings.MODEL). Auth is GCP ADC
(GOOGLE_APPLICATION_CREDENTIALS); GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION
must be set at runtime (the `Claude` class reads them to build the Vertex client).
"""
from __future__ import annotations

import traceback
from typing import Any, Callable

import requests
from google.adk.agents import LlmAgent
from slack_sdk import WebClient

from src.settings import (
    CONTROL_CENTER_URL,
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL,
)
from src.auth import make_iap_jwt
from src.data_client import ControlCenterClient
from src.slack import SlackClient, build_alert_blocks

SYSTEM_INSTRUCTION = """\
You are the data-platform recovery agent for Tecovas' EDW.

You are invoked when a watched dbt model is stale. You own the entire decision:
diagnose why the model or its sources are stale, decide on the safest corrective
action, take it, and report clearly. There is no deterministic runbook ahead of
you — start by inspecting the model's status before doing anything.

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
  re-run did not resolve the staleness), call `alert_humans` to post a rich
  stale-pipeline alert with a clear diagnosis. Do not alert for routine,
  self-resolved cases.

You can also communicate directly when asked:
- `message_channel` posts a plain message to any Slack channel (by name like
  "data_devs" or by ID). Use it for general or ad-hoc messages and tests.
- `dm_human` sends a direct message to one person (resolve the name first with
  `find_slack_user`).
Use `alert_humans` specifically for structured stale-pipeline alerts; use
`message_channel`/`dm_human` for everything else.

Be concise and factual. You are talking to data engineers.
"""

# --- concrete clients (constructed at the edge) -----------------------------

control_center = ControlCenterClient(
    base_url=CONTROL_CENTER_URL,
    http=requests.Session(),
    token_provider=make_iap_jwt,
    # IAP self-signed JWT audience: the service URL with a path wildcard.
    token_audience=f"{CONTROL_CENTER_URL.rstrip('/')}/*",
)
slack = SlackClient(
    web=WebClient(token=SLACK_BOT_TOKEN),
    default_channel=SLACK_CHANNEL,
)


# --- tools (plain functions; ADK reads name/signature/docstring) ------------
#
# Every tool returns an ADK-style envelope so the model can reason about
# outcomes: {"status": "success", ...payload} or {"status": "error",
# "error_message": "..."}. `_guard` wraps control-center calls (which raise on
# HTTP errors); `_from_slack` translates the Slack client's {"ok": ...} shape.


def _guard(payload_key: str, fn: Callable[[], Any]) -> dict[str, Any]:
    """Run a control-center call, returning a success/error envelope.

    On failure the FULL traceback is printed to stderr (so it lands in Cloud Run
    logs for troubleshooting), and the error message is enriched with the HTTP
    status + response body when the exception carries an HTTP response.
    """
    try:
        return {"status": "success", payload_key: fn()}
    except Exception as e:  # network / HTTP / decode — surface to the model
        traceback.print_exc()  # full stack -> stderr -> Cloud Run logs
        msg = f"{type(e).__name__}: {e}"
        resp = getattr(e, "response", None)
        if resp is not None:  # requests.HTTPError etc. — add status + body
            body = (resp.text or "")[:1000]
            msg = f"{msg} | HTTP {resp.status_code} from {resp.url}: {body}"
        print(f"[tool-error] {msg}", flush=True)
        return {"status": "error", "error_message": msg}


def _from_slack(result: dict[str, Any]) -> dict[str, Any]:
    """Translate a Slack client result ({"ok": bool, ...}) to the envelope."""
    if result.get("ok"):
        return {"status": "success", **result}
    return {
        "status": "error",
        "error_message": result.get("error", "Slack call failed"),
        **result,
    }


def list_watched_models() -> dict[str, Any]:
    """List all watched dbt models and whether each is currently fresh."""
    return _guard("models", control_center.list_models)


def get_models_status(filter: str = "all") -> dict[str, Any]:
    """Batch freshness for every watched model in ONE call -> {checked_at, models}.

    Prefer this over calling get_model_status per model — it avoids the N+1 and
    is the efficient way to survey the whole fleet. It is expensive server-side
    (fans out BigQuery + loader calls), so call it once and reason over the result.

    Args:
        filter: which models to return — "all", "stale" (only stale ones), or
            "behind_sources" (fresh model but stale upstream sources).
    """
    return _guard("result", lambda: control_center.models_status(filter))


def get_stale_models() -> dict[str, Any]:
    """List only the currently stale watched models -> {checked_at, models}.

    Shorthand for get_models_status(filter="stale"). Use this as the first step
    when diagnosing what needs recovery.
    """
    return _guard("result", lambda: control_center.models_status("stale"))


def get_model_status(unique_id: str) -> dict[str, Any]:
    """Get the full freshness status for one model, including every source.

    Args:
        unique_id: the dbt unique_id, e.g. "model.tecovas.fct_sales".
    """
    return _guard("model", lambda: control_center.get_model_status(unique_id))


def refresh_model(unique_id: str) -> dict[str, Any]:
    """Request a re-run for a stale model. May be refused by server-side limits.

    The control center maps the model to its loader and enforces rate limits.

    Args:
        unique_id: the dbt unique_id, e.g. "model.tecovas.fct_sales".
    """
    return _guard("result", lambda: control_center.refresh_model(unique_id))


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
    return _from_slack(slack.post_alert(text=f"Stale pipeline: {unique_id}", blocks=blocks))


def message_channel(channel: str, message: str) -> dict[str, Any]:
    """Post a plain-text message to a Slack channel.

    Use for general or ad-hoc messages (not structured stale-pipeline alerts —
    use `alert_humans` for those).

    Args:
        channel: channel name (e.g. "data_devs" or "#data_devs") or ID
            (e.g. "C0123ABCD").
        message: plain-text message to post.
    """
    target = channel
    if not channel.startswith(("C", "G", "D")) or " " in channel:
        found = slack.find_channel_by_name(channel)
        if not found.get("ok"):
            return _from_slack(found)
        target = found["id"]
    return _from_slack(slack.post_alert(text=message, channel=target))


def find_slack_user(name: str) -> dict[str, Any]:
    """Find Slack users by display/real name (case-insensitive substring match).

    Use this to resolve a person's name to a Slack user ID before DMing them
    with `dm_human`. On success returns ``{"status": "success", "users":
    [{"id", "name", "real_name", "display_name", "email"}, ...]}``. If more than
    one user matches, disambiguate before messaging.

    Args:
        name: full or partial name to search for, e.g. "Lorenzo Peve".
    """
    return _from_slack(slack.find_users_by_name(name))


def dm_human(user: str, message: str) -> dict[str, Any]:
    """Send a direct message to a specific Slack user (not the team channel).

    Use this to reach a named owner privately — e.g. the engineer on-call for a
    loader — when an alert is targeted rather than for the whole team. For
    broad, team-wide alerts use `alert_humans` instead. To message someone by
    name, first resolve them with `find_slack_user`.

    Args:
        user: the recipient's Slack user ID (e.g. "U123456") or email address.
        message: plain-text message to send.
    """
    return _from_slack(slack.send_dm(user=user, text=message))


root_agent = LlmAgent(
    name="edw_recovery_agent",
    model='gemini-2.5-flash', # todo
    instruction=SYSTEM_INSTRUCTION,
    tools=[
        list_watched_models,
        get_models_status,
        get_stale_models,
        get_model_status,
        refresh_model,
        alert_humans,
        message_channel,
        find_slack_user,
        dm_human,
    ],
)
