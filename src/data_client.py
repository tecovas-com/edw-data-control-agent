"""Thin requests wrapper over the edw-data-control-center freshness API.

This is the ONLY way this repo reaches the data control center. It never imports
`edc.core` — the network is the contract. Every request carries an OIDC ID token
(see auth.py); the token-minting callable is injected so tests can stub it and
never touch GCP.

The control center sits behind Cloud IAP, so the token's audience is NOT the
service URL — it is the IAP OAuth client ID. `token_audience` is therefore
decoupled from `base_url`: requests go to `base_url`, but the token is minted for
`token_audience` (defaults to `base_url` for plain Cloud Run IAM).
"""
from __future__ import annotations

from typing import Any, Callable

import requests


class ControlCenterClient:
    def __init__(
        self,
        base_url: str,
        http: requests.Session,
        token_provider: Callable[[str], str],
        timeout: float = 30,
        token_audience: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._token_provider = token_provider
        self._timeout = timeout
        self._token_audience = token_audience or self._base_url

    def _headers(self) -> dict[str, str]:
        token = self._token_provider(self._token_audience)
        return {"Authorization": f"Bearer {token}"}

    def list_models(self) -> list[dict[str, Any]]:
        """GET /api/models -> watched models (unique_id, name, max_age_hours, ...)."""
        r = self._http.get(
            f"{self._base_url}/api/models",
            headers=self._headers(),
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()["models"]

    def get_model_status(self, unique_id: str) -> dict[str, Any]:
        """GET /api/models/{unique_id} -> full freshness detail for one model."""
        r = self._http.get(
            f"{self._base_url}/api/models/{unique_id}",
            headers=self._headers(),
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    def refresh_model(self, unique_id: str) -> dict[str, Any]:
        """POST /api/models/{unique_id}/refresh -> request a re-run for a model.

        The control center maps the model to its loader and enforces rate limits.
        """
        r = self._http.post(
            f"{self._base_url}/api/models/{unique_id}/refresh",
            headers=self._headers(),
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()
