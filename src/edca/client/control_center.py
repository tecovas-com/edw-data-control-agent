"""Thin httpx wrapper over the edw-data-control-center freshness API.

This is the ONLY way this repo reaches the control center. It never imports
`edc.core`. Every request carries an IAM ID token (see `auth.py`).

The token-minting callable is injected so tests can stub it and never touch GCP.
"""
from __future__ import annotations

from typing import Any, Callable

import httpx


class ControlCenterClient:
    def __init__(
        self,
        base_url: str,
        http: httpx.Client,
        token_provider: Callable[[str], str],
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._token_provider = token_provider

    def _headers(self) -> dict[str, str]:
        token = self._token_provider(self._base_url)
        return {"Authorization": f"Bearer {token}"}

    # --- reads -------------------------------------------------------------
    def list_models(self) -> list[dict[str, Any]]:
        """GET /models -> watched models with overall_is_fresh."""
        r = self._http.get(
            f"{self._base_url}/models", headers=self._headers()
        )
        r.raise_for_status()
        return r.json()

    def get_model_status(self, unique_id: str) -> dict[str, Any]:
        """GET /models/{unique_id}/status -> full PipelineStatus."""
        r = self._http.get(
            f"{self._base_url}/models/{unique_id}/status",
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    # --- recovery actions (guardrails enforced server-side) ----------------
    def trigger_loader(self, loader_type: str, loader_id: str) -> dict[str, Any]:
        """POST a re-run request. The control center enforces rate limits."""
        r = self._http.post(
            f"{self._base_url}/loaders/{loader_type}/{loader_id}/trigger",
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()
