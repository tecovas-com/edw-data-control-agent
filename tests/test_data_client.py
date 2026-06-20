"""LIVE tests for ControlCenterClient — they hit the real control-center API.

No fakes, no stubs, no pytest fixtures: these build a real client, mint a real
IAM ID token, and call the deployed freshness API. They ERROR (fail loud) if
CLOUD_RUN_DATA_CONTROL_URL / GOOGLE_APPLICATION_CREDENTIALS are unset.

Only READ endpoints are exercised — `trigger_loader` is intentionally NOT called
here because a re-run costs money/compute.

Each response is written to tests/output/ (git-ignored) for manual inspection.

    pytest tests/test_data_client.py
    pytest -k "not (live or data_client)"      # skip all network tests
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from src.auth import make_iap_jwt
from src.data_client import ControlCenterClient

OUTPUT_DIR = Path(__file__).parent / "output"


def _live_client() -> ControlCenterClient:
    base_url = os.environ.get("CLOUD_RUN_DATA_CONTROL_URL")
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not base_url or not creds:
        raise RuntimeError(
            "set CLOUD_RUN_DATA_CONTROL_URL and GOOGLE_APPLICATION_CREDENTIALS "
            "to run live data-client tests"
        )
    base_url = base_url.rstrip("/")
    # Behind IAP (Google-managed OAuth client): present a self-signed SA JWT whose
    # audience is the service URL with a path wildcard.
    return ControlCenterClient(
        base_url=base_url,
        http=requests.Session(),
        token_provider=make_iap_jwt,
        timeout=float(os.environ.get("EDCA_TIMEOUT_S", "60")),
        token_audience=f"{base_url}/*",
    )


def _write_output(name: str, data: object) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / name
    path.write_text(json.dumps(data, indent=2, default=str))
    return path


def test_live_list_models_returns_models():
    client = _live_client()

    models = client.list_models()
    _write_output("list_models.json", models)

    assert isinstance(models, list), models
    assert models, "control center returned zero watched models"
    first = models[0]
    assert "unique_id" in first, first
    assert "name" in first, first


def test_live_get_model_status_for_first_model():
    client = _live_client()

    models = client.list_models()
    assert models, "no models to fetch status for"
    unique_id = models[0]["unique_id"]

    status = client.get_model_status(unique_id)
    safe_name = unique_id.replace(".", "_").replace("/", "_")
    _write_output(f"model_status_{safe_name}.json", status)

    assert isinstance(status, dict), status
    assert status, "status payload was empty"
