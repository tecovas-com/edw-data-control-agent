"""Deterministic recovery decisions. PURE — no I/O, no datetime.now().

Maps a model's PipelineStatus (as returned by the control center) to a list of
recommended actions. The entrypoint executes them; the LLM agent is only invoked
when this runbook returns NO confident action (escalate=True).
"""
from __future__ import annotations

from dataclasses import dataclass, field


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
