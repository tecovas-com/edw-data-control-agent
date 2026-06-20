"""Pure decision logic — NO I/O, NO datetime.now().

Everything here is testable with no network, no GCP, no LLM, no clock. It is the
heart of the service:

- `may_retrigger`      — client-side guardrail check (a second line of defense;
                         the authoritative limits live server-side).
- `plan_recovery`      — deterministic recovery decisions from a PipelineStatus.
- `verify_slack_signature` / `build_alert_blocks` — pure Slack helpers.

The concrete clients (control center, Slack, LLM) are constructed at the edge
(agent.py / main.py) and never imported here.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# --- policy: client-side guardrails -----------------------------------------


@dataclass(frozen=True)
class RecoveryAttempt:
    loader_type: str
    loader_id: str
    at: datetime


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def may_retrigger(
    loader_type: str,
    loader_id: str,
    recent_attempts: list[RecoveryAttempt],
    now: datetime,
    *,
    min_interval_hours: int = 1,
    max_attempts_per_day: int = 4,
) -> PolicyDecision:
    """Decide whether re-triggering a loader is within client-side policy."""
    mine = [
        a for a in recent_attempts
        if a.loader_type == loader_type and a.loader_id == loader_id
    ]
    last = max((a.at for a in mine), default=None)
    if last is not None and now - last < timedelta(hours=min_interval_hours):
        return PolicyDecision(
            False,
            f"last attempt {now - last} ago < {min_interval_hours}h min interval",
        )

    in_last_day = [a for a in mine if now - a.at < timedelta(days=1)]
    if len(in_last_day) >= max_attempts_per_day:
        return PolicyDecision(
            False,
            f"{len(in_last_day)} attempts in last 24h >= cap {max_attempts_per_day}",
        )

    return PolicyDecision(True, "within policy")


# --- runbook: deterministic recovery decisions ------------------------------


@dataclass(frozen=True)
class RecoveryAction:
    kind: str          # "retrigger_loader" | "alert" | "wait"
    loader_type: str | None = None
    loader_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class RunbookResult:
    actions: list[RecoveryAction] = field(default_factory=list)
    escalate: bool = False   # hand off to the LLM agent
    reason: str = ""


def plan_recovery(status: dict) -> RunbookResult:
    """Given a PipelineStatus dict, return deterministic recovery actions.

    Known case handled here: a stale source whose loader is identifiable ->
    re-trigger that loader once. Anything ambiguous (model fresh but sources
    stale with succeeded loaders, unknown loader types, etc.) -> escalate.
    """
    if status.get("overall_is_fresh", False):
        return RunbookResult(reason="already fresh; nothing to do")

    actions: list[RecoveryAction] = []
    ambiguous = False

    for src in status.get("sources", []):
        if src.get("is_fresh", True) or src.get("is_static", False):
            continue
        loader = src.get("loader") or {}
        loader_type = loader.get("loader_type")
        loader_id = loader.get("loader_id")
        # The loader ran and succeeded but data is still stale -> not a simple
        # re-run; let the agent investigate upstream.
        if loader.get("succeeded") and loader_type and loader_id:
            ambiguous = True
            continue
        if loader_type and loader_id:
            actions.append(
                RecoveryAction(
                    kind="retrigger_loader",
                    loader_type=loader_type,
                    loader_id=loader_id,
                    reason=f"source {src.get('name')} stale and loader not succeeding",
                )
            )
        else:
            ambiguous = True

    if ambiguous or not actions:
        return RunbookResult(
            actions=actions,
            escalate=True,
            reason="ambiguous or no confident deterministic action",
        )
    return RunbookResult(actions=actions, reason="deterministic re-trigger")


# --- slack: pure helpers (HMAC verification + Block Kit composition) ---------

# Slack rejects requests whose timestamp is more than 5 minutes off (replay guard).
_MAX_SKEW_S = 60 * 5


def verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
    now: float,
    max_skew_s: int = _MAX_SKEW_S,
) -> bool:
    """Verify the X-Slack-Signature header per Slack's HMAC scheme.

    Rejects replays whose timestamp is more than `max_skew_s` from `now` (unix
    seconds, injected — never read the clock in core). See:
    https://api.slack.com/authentication/verifying-requests-from-slack
    """
    if not timestamp or not signature or not signing_secret:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(now - ts) > max_skew_s:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def build_alert_blocks(
    unique_id: str,
    summary: str,
    *,
    failing_sources: list[str] | None = None,
    actions_taken: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compose the Block Kit payload for a stale-pipeline alert.

    `summary` is the recovery agent's plain-text diagnosis. The action buttons
    carry the model `unique_id` as their value so the interactivity handler can
    correlate a click back to the model.
    """
    sources = ", ".join(f"`{s}`" for s in failing_sources) if failing_sources else "—"
    taken = "\n".join(f"• {a}" for a in actions_taken) if actions_taken else "(none)"
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Stale pipeline needs attention"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Model:*\n`{unique_id}`"},
                {"type": "mrkdwn", "text": f"*Stale sources:*\n{sources}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Diagnosis:*\n{summary or '(no summary)'}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Actions taken:*\n{taken}"},
        },
        {
            "type": "actions",
            "block_id": "edca_actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Re-run loader"},
                    "action_id": "retrigger_loader",
                    "value": unique_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Acknowledge"},
                    "action_id": "acknowledge",
                    "value": unique_id,
                },
            ],
        },
    ]
