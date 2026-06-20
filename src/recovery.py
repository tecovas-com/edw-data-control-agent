"""Recovery decision logic — PURE: NO I/O, NO datetime.now().

Everything here is testable with no network, no GCP, no LLM, no clock. It is the
heart of the service:

- `may_retrigger`      — client-side guardrail check (a second line of defense;
                         the authoritative limits live server-side).
- `plan_recovery`      — deterministic recovery decisions from a PipelineStatus.

The concrete clients (control center, Slack, LLM) are constructed at the edge
(agent.py / main.py) and never imported here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

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
