"""Tiny stub clients for tests. No pytest fixtures anywhere."""
from __future__ import annotations

from typing import Any


class StubControlCenterClient:
    """Stand-in for ControlCenterClient: canned responses, records actions."""

    def __init__(
        self,
        models: list[dict[str, Any]] | None = None,
        statuses: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._models = models or []
        self._statuses = statuses or {}
        self.triggered: list[tuple[str, str]] = []

    def list_models(self) -> list[dict[str, Any]]:
        return self._models

    def get_model_status(self, unique_id: str) -> dict[str, Any]:
        return self._statuses[unique_id]

    def trigger_loader(self, loader_type: str, loader_id: str) -> dict[str, Any]:
        self.triggered.append((loader_type, loader_id))
        return {"ok": True}
