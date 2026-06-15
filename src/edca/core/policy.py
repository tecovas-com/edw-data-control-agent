"""Client-side guardrail checks. PURE — no I/O, no datetime.now().

These are a *second* line of defense so the agent fails fast and can reason
about limits. The authoritative guardrails live server-side in the control
center's MCP tool layer. Never rely on this alone to prevent a runaway re-run.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


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
