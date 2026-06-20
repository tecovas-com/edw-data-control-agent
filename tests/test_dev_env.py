"""Manual dev-environment smoke check (NOT part of the pytest suite).

Run after `cp .env.template .env` and dropping the SA key in `gcp/` to confirm
local credentials actually work end to end:

    python test_dev_env.py

It loads `.env`, mints a self-signed service-account JWT for the control-center
audience, and decodes the JWT payload so you can eyeball the claims (issuer `iss`,
audience `aud`, service-account `email`, expiry `exp`). Decoding only base64url-
unpacks the payload — it does NOT verify the signature (that needs Google's
public keys); it's just to confirm the token targets the right audience/SA.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


def find_env(filename: str = ".env") -> Path:
    """Walk up from this file to find the repo-root `.env`."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / filename
        if candidate.exists():
            return candidate
    raise SystemExit(f"no {filename} found — run: cp .env.template .env")


def load_env() -> None:
    """Load KEY=VALUE lines from `.env` into os.environ (real env wins)."""
    env = find_env()
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def decode_jwt(token: str) -> dict:
    """Decode (NOT verify) a JWT's payload. Signature is not checked."""
    payload_b64 = token.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def main() -> None:
    load_env()

    audience = os.environ.get("CLOUD_RUN_DATA_CONTROL_URL")
    if not audience:
        raise SystemExit(
            "set CLOUD_RUN_DATA_CONTROL_URL (or CONTROL_CENTER_URL) in .env"
        )
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise SystemExit("set GOOGLE_APPLICATION_CREDENTIALS in .env (path to SA key)")

    # Ensure the repo root is importable when run as `python tests/test_dev_env.py`
    # (Python only puts the script's own dir on sys.path, not the repo root).
    repo_root = find_env().parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from src.auth import make_iap_jwt

    # IAP with the Google-managed OAuth client wants `aud` = URL + path wildcard.
    audience = audience.rstrip("/") + "/*"

    print(f"audience: {audience}")
    print(f"credentials: {os.environ['GOOGLE_APPLICATION_CREDENTIALS']}\n")

    token = make_iap_jwt(audience)
    print("\nDecoded claims (signature NOT verified):")
    print(json.dumps(decode_jwt(token), indent=2))


if __name__ == "__main__":
    main()
