"""Mint GCP IAM ID tokens for service-to-service calls to the control center.

The audience must be the control-center Cloud Run service URL. On Cloud Run the
ambient (workload) service account is used automatically — no keys, no secrets.
Locally, `gcloud auth application-default login` provides the credentials.
"""
from __future__ import annotations

import google.auth.transport.requests
from google.oauth2 import id_token


def fetch_id_token(audience: str) -> str:
    """Return a Google-signed ID token whose audience is `audience`.

    `audience` is the control-center service URL (e.g. https://...run.app).
    """
    request = google.auth.transport.requests.Request()
    return id_token.fetch_id_token(request, audience)
