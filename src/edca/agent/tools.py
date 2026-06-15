"""ADK tools — plain functions the agent can call.

Each tool is a thin wrapper over the injected ControlCenterClient. ADK discovers
the function name, signature, and docstring as the tool contract, so docstrings
here are part of the agent's instructions — keep them accurate.

The client is bound at startup via `build_tools(client)` so these stay testable
and free of module-level globals.
"""
from __future__ import annotations

from typing import Any, Callable

from edca.client.control_center import ControlCenterClient


def build_tools(client: ControlCenterClient) -> list[Callable[..., Any]]:
    """Return ADK tool callables bound to a ControlCenterClient."""

    def list_watched_models() -> list[dict[str, Any]]:
        """List all watched dbt models and whether each is currently fresh."""
        return client.list_models()

    def get_model_status(unique_id: str) -> dict[str, Any]:
        """Get the full freshness status for one model, including every source.

        Args:
            unique_id: the dbt unique_id, e.g. "model.tecovas.fct_sales".
        """
        return client.get_model_status(unique_id)

    def retrigger_loader(loader_type: str, loader_id: str) -> dict[str, Any]:
        """Request a re-run of a loader. May be refused by server-side limits.

        Args:
            loader_type: one of "fivetran", "airflow", "cloud_run_jobs",
                "dbt_artifacts".
            loader_id: the loader's id as configured in the control center.
        """
        return client.trigger_loader(loader_type, loader_id)

    return [list_watched_models, get_model_status, retrigger_loader]
