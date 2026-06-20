"""Mint credentials for calling the IAP-protected control center.

The control center sits behind Cloud IAP configured with the **Google-managed
OAuth client**. With that client, standard Google-issued OIDC ID tokens are NOT
accepted for programmatic access — service accounts must instead present a
**self-signed JWT** (iss = sub = SA email), with `aud` set to the IAP-secured URL
plus a path wildcard (`/*`), sent as `Authorization: Bearer`.

See: https://cloud.google.com/iap/docs/authentication-howto
     ("Authenticate with a service account" -> self-signed JWT)

Two signing backends, chosen by environment:

* Local dev — GOOGLE_APPLICATION_CREDENTIALS points to an SA key file, so we sign
  the JWT locally with that private key.
* Cloud Run — no key file; the attached SA's private key is never exposed. We ask
  the IAM Credentials API to sign the JWT *as the SA* (keyless). This needs:
    - the SA attached to the service (`gcloud run services update --service-account`)
    - the SA able to sign as itself:
        gcloud iam service-accounts add-iam-policy-binding SA_EMAIL \
            --member="serviceAccount:SA_EMAIL" \
            --role="roles/iam.serviceAccountTokenCreator"
    - the API enabled: `gcloud services enable iamcredentials.googleapis.com`

In both cases the SA must hold roles/iap.httpsResourceAccessor on the IAP resource.
"""
from __future__ import annotations

import json
import os
import time

import google.auth
from google.auth import crypt, iam, jwt
from google.auth.transport.requests import Request

# IAP rejects tokens older than ~1h; this is the JWT's validity window.
_JWT_LIFETIME_S = 3600
_TOKEN_CREATOR_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _local_key_signer() -> tuple[crypt.Signer, str] | None:
    """Signer backed by a local SA key file, or None if no usable key file."""
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key_path or not os.path.exists(key_path):
        return None
    with open(key_path) as f:
        info = json.load(f)
    if "private_key" not in info or "client_email" not in info:
        return None
    return crypt.RSASigner.from_service_account_info(info), info["client_email"]


def _iam_api_signer() -> tuple[iam.Signer, str]:
    """Keyless signer: the IAM Credentials API signs as the ambient SA.

    Used on Cloud Run, where no key file exists. Requires the ambient SA to hold
    roles/iam.serviceAccountTokenCreator on itself and the iamcredentials API.
    """
    source_credentials, _ = google.auth.default(scopes=[_TOKEN_CREATOR_SCOPE])
    email = os.environ.get("EDCA_SERVICE_ACCOUNT_EMAIL") or getattr(
        source_credentials, "service_account_email", None
    )
    if not email or email == "default":
        raise RuntimeError(
            "could not determine the service account email for IAM-API signing; "
            "set EDCA_SERVICE_ACCOUNT_EMAIL"
        )
    request = Request()
    return iam.Signer(request, source_credentials, email), email


def make_iap_jwt(audience: str) -> str:
    """Return a self-signed service-account JWT accepted by Cloud IAP.

    `audience` is the IAP-secured URL with a path wildcard, e.g.
    "https://<service>.run.app/*". Signs locally with the SA key when one is
    available (dev), otherwise via the IAM Credentials API (Cloud Run).
    """
    signer, email = _local_key_signer() or _iam_api_signer()
    now = int(time.time())
    payload = {
        "iss": email,
        "sub": email,
        "aud": audience,
        "iat": now,
        "exp": now + _JWT_LIFETIME_S,
        "email": email,
    }
    token = jwt.encode(signer, payload)
    return token.decode("utf-8") if isinstance(token, bytes) else token
