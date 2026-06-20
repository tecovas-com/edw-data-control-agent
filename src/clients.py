"""I/O adapters — the only place this service talks to the outside world.

Three concrete clients, all small and testable (stub them in tests/stubs.py):

- `fetch_id_token`       — mint a GCP IAM ID token for service-to-service auth.
- `ControlCenterClient`  — httpx wrapper over the freshness API/MCP. Never
                           imports `edc.core`; the network is the contract.
- `SlackClient`          — wrapper over the Slack Web API for alerts.

Pure logic (recovery decisions, Block Kit composition, signature checks) lives
in core.py and is injected/consumed here, never the other way around.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import google.auth.transport.requests
import httpx
from google.oauth2 import id_token
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def fetch_id_token(audience: str) -> str:
    """Return a Google-signed ID token whose audience is `audience`.

    `audience` is the control-center service URL (e.g. https://...run.app). On
    Cloud Run the ambient service account is used automatically; locally,
    `gcloud auth application-default login` provides the credentials.
    """
    request = google.auth.transport.requests.Request()
    return id_token.fetch_id_token(request, audience)


class ControlCenterClient:
    """Thin httpx wrapper over the edw-data-control-center freshness API.

    Every request carries an IAM ID token. The token-minting callable is
    injected so tests can stub it and never touch GCP.
    """

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

    def list_models(self) -> list[dict[str, Any]]:
        """GET /models -> watched models with overall_is_fresh."""
        r = self._http.get(f"{self._base_url}/models", headers=self._headers())
        r.raise_for_status()
        return r.json()

    def get_model_status(self, unique_id: str) -> dict[str, Any]:
        """GET /models/{unique_id}/status -> full PipelineStatus."""
        r = self._http.get(
            f"{self._base_url}/models/{unique_id}/status", headers=self._headers()
        )
        r.raise_for_status()
        return r.json()

    def trigger_loader(self, loader_type: str, loader_id: str) -> dict[str, Any]:
        """POST a re-run request. The control center enforces rate limits."""
        r = self._http.post(
            f"{self._base_url}/loaders/{loader_type}/{loader_id}/trigger",
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()


class SlackClient:
    """Wrapper over the Slack Web API. Returns plain dicts; never raises on a
    Slack-side error (returns ``{"ok": False, ...}``). The `WebClient` is
    injected so tests stub it and never touch the network."""

    def __init__(self, web: WebClient, default_channel: str) -> None:
        self._web = web
        self._channel = default_channel

    def post_alert(
        self,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Post a message (optionally Block Kit) to the configured channel."""
        try:
            resp = self._web.chat_postMessage(
                channel=channel or self._channel, text=text, blocks=blocks
            )
            return {"ok": True, "ts": resp["ts"], "channel": resp["channel"]}
        except SlackApiError as e:
            return {"ok": False, "error": str(e)}

    def reply_in_thread(
        self, thread_ts: str, text: str, channel: str | None = None
    ) -> dict[str, Any]:
        """Reply to an existing thread in the configured channel."""
        try:
            resp = self._web.chat_postMessage(
                channel=channel or self._channel, thread_ts=thread_ts, text=text
            )
            return {"ok": True, "ts": resp["ts"]}
        except SlackApiError as e:
            return {"ok": False, "error": str(e)}

    def send_dm(self, user: str, text: str) -> dict[str, Any]:
        """Send a direct message to a Slack user (ID or email)."""
        try:
            user_id = user
            if "@" in user:
                lookup = self._web.users_lookupByEmail(email=user)
                user_id = lookup["user"]["id"]
            resp = self._web.chat_postMessage(channel=user_id, text=text)
            return {
                "ok": True,
                "ts": resp["ts"],
                "channel": resp["channel"],
                "user_id": user_id,
            }
        except SlackApiError as e:
            return {"ok": False, "error": str(e)}

    def delete_message(self, ts: str, channel: str | None = None) -> dict[str, Any]:
        """Delete a single message (or thread reply) by its ts."""
        try:
            self._web.chat_delete(channel=channel or self._channel, ts=ts)
            return {"ok": True, "ts": ts}
        except SlackApiError as e:
            return {"ok": False, "ts": ts, "error": str(e)}

    def delete_bot_messages(
        self,
        channel: str | None = None,
        limit: int = 200,
        pause_s: float = 0.0,
    ) -> dict[str, Any]:
        """Delete every message and thread reply authored by THIS bot in `channel`.

        Scans up to `limit` top-level messages plus their thread replies, keeps
        only the ones this bot posted (matched by user_id or bot_id from
        auth.test), and deletes them. `pause_s` sleeps between deletes to stay
        under Slack's tier-3 rate limit (~50/min) for large cleanups.
        """
        channel = channel or self._channel
        try:
            auth = self._web.auth_test()
            history = self._web.conversations_history(channel=channel, limit=limit)
        except SlackApiError as e:
            return {"ok": False, "deleted": [], "errors": [str(e)], "scanned": 0}

        bot_user_id = auth.get("user_id")
        bot_id = auth.get("bot_id")

        def _is_ours(m: dict[str, Any]) -> bool:
            return m.get("user") == bot_user_id or (
                bot_id is not None and m.get("bot_id") == bot_id
            )

        # conversations.history only returns top-level messages; fetch replies too.
        by_ts: dict[str, dict[str, Any]] = {}
        for m in history.get("messages", []):
            by_ts[m["ts"]] = m
            if m.get("reply_count", 0) > 0 or m.get("thread_ts"):
                thread_ts = m.get("thread_ts") or m["ts"]
                try:
                    replies = self._web.conversations_replies(
                        channel=channel, ts=thread_ts, limit=1000
                    )
                    for r in replies.get("messages", []):
                        by_ts[r["ts"]] = r
                except SlackApiError:
                    pass  # best-effort; keep cleaning what we can

        targets = [m for m in by_ts.values() if _is_ours(m)]
        deleted: list[str] = []
        errors: list[str] = []
        for m in targets:
            try:
                self._web.chat_delete(channel=channel, ts=m["ts"])
                deleted.append(m["ts"])
                if pause_s:
                    time.sleep(pause_s)
            except SlackApiError as e:
                errors.append(f"{m['ts']}: {e}")

        return {
            "ok": not errors,
            "deleted": deleted,
            "errors": errors,
            "scanned": len(by_ts),
        }

    def self_check(self) -> dict[str, Any]:
        """Auth check (auth.test) for startup/integration. Returns an error dict."""
        try:
            resp = self._web.auth_test()
            return {
                "ok": True,
                "team": resp.get("team"),
                "user": resp.get("user"),
                "bot_id": resp.get("bot_id"),
            }
        except SlackApiError as e:
            return {"ok": False, "error": str(e)}
