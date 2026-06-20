"""Mint credentials for calling the IAP-protected control center.

The control center sits behind Cloud IAP configured with the **Google-managed
OAuth client**. With that client, standard Google-issued OIDC ID tokens are NOT
accepted for programmatic access — service accounts must instead present a
**self-signed JWT** (iss = sub = SA email, RS256-signed with the SA's own private
key), with `aud` set to the IAP-secured URL plus a path wildcard (`/*`).

See: https://cloud.google.com/iap/docs/authentication-howto
     ("Authenticate with a service account" -> self-signed JWT)

The SA key is read from GOOGLE_APPLICATION_CREDENTIALS. The agent's SA must hold
roles/iap.httpsResourceAccessor on the control-center IAP resource.
"""
from __future__ import annotations

import json
import os
import time

from google.auth import crypt, jwt

# IAP rejects tokens older than ~1h; this is the JWT's validity window.
_JWT_LIFETIME_S = 3600


def make_iap_jwt(audience: str) -> str:
    """Return a self-signed service-account JWT accepted by Cloud IAP.

    `audience` is the IAP-secured URL with a path wildcard, e.g.
    "https://<service>.run.app/*". The token is signed locally with the private
    key in GOOGLE_APPLICATION_CREDENTIALS and sent as `Authorization: Bearer`.
    """
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key_path:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is not set")
    with open(key_path) as f:
        info = json.load(f)

    signer = crypt.RSASigner.from_service_account_info(info)
    email = info["client_email"]
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
