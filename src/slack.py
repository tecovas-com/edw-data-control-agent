"""Everything Slack.

Two pure helpers (no I/O — testable with no secrets, no clock, no network):
- `verify_slack_signature`: HMAC check for inbound interactivity webhooks.
- `build_alert_blocks`: Block Kit composition for a stale-pipeline alert.

Plus `SlackClient`, the I/O wrapper over the Slack Web API: it returns plain
dicts, never raises on a Slack-side error (returns ``{"ok": False, ...}``), and
takes an injected `WebClient` so tests stub it and never touch the network.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Slack rejects requests whose timestamp is more than 5 minutes off (replay guard).
_MAX_SKEW_S = 60 * 5


def verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
    now: float,
    max_skew_s: int = _MAX_SKEW_S,
) -> bool:
    """Verify the X-Slack-Signature header per Slack's HMAC scheme.

    Rejects replays whose timestamp is more than `max_skew_s` from `now` (unix
    seconds, injected — never read the clock here). See:
    https://api.slack.com/authentication/verifying-requests-from-slack
    """
    if not timestamp or not signature or not signing_secret:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(now - ts) > max_skew_s:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def build_alert_blocks(
    unique_id: str,
    summary: str,
    *,
    failing_sources: list[str] | None = None,
    actions_taken: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compose the Block Kit payload for a stale-pipeline alert.

    `summary` is the recovery agent's plain-text diagnosis. The action buttons
    carry the model `unique_id` as their value so the interactivity handler can
    correlate a click back to the model.
    """
    sources = ", ".join(f"`{s}`" for s in failing_sources) if failing_sources else "—"
    taken = "\n".join(f"• {a}" for a in actions_taken) if actions_taken else "(none)"
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Stale pipeline needs attention"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Model:*\n`{unique_id}`"},
                {"type": "mrkdwn", "text": f"*Stale sources:*\n{sources}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Diagnosis:*\n{summary or '(no summary)'}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Actions taken:*\n{taken}"},
        },
        {
            "type": "actions",
            "block_id": "edca_actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Re-run loader"},
                    "action_id": "retrigger_loader",
                    "value": unique_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Acknowledge"},
                    "action_id": "acknowledge",
                    "value": unique_id,
                },
            ],
        },
    ]


class SlackClient:
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
        """Send a direct message to a Slack user (ID or email).

        Opens the IM channel with `conversations.open` and posts to that channel
        ID. Posting to a raw user ID is unreliable — always resolve the DM
        channel first.
        """
        try:
            user_id = user
            if "@" in user:
                lookup = self._web.users_lookupByEmail(email=user)
                user_id = lookup["user"]["id"]
            opened = self._web.conversations_open(users=user_id)
            dm_channel = opened["channel"]["id"]
            resp = self._web.chat_postMessage(channel=dm_channel, text=text)
            return {
                "ok": True,
                "ts": resp["ts"],
                "channel": resp["channel"],
                "user_id": user_id,
            }
        except SlackApiError as e:
            return {"ok": False, "error": str(e)}

    def find_channel_by_name(
        self, name: str, limit: int = 1000
    ) -> dict[str, Any]:
        """Resolve a channel NAME (e.g. "data_devs") to its channel ID.

        Slack's post APIs want a channel ID, not a name. This scans
        `conversations.list` (public + private) and returns the first exact,
        case-insensitive name match as ``{"ok": True, "id", "name"}``. Leading
        ``#`` is ignored.
        """
        needle = name.strip().lstrip("#").lower()
        if not needle:
            return {"ok": False, "error": "empty name"}
        cursor: str | None = None
        try:
            while True:
                resp = self._web.conversations_list(
                    limit=limit,
                    cursor=cursor,
                    types="public_channel,private_channel",
                    exclude_archived=True,
                )
                for c in resp.get("channels", []):
                    if (c.get("name") or "").lower() == needle:
                        return {"ok": True, "id": c["id"], "name": c["name"]}
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            return {"ok": False, "error": f"channel not found: {name}"}
        except SlackApiError as e:
            return {"ok": False, "error": str(e)}

    def find_users_by_name(
        self, name: str, limit: int = 1000
    ) -> dict[str, Any]:
        """Find Slack users whose name matches `name` (case-insensitive substring).

        Slack has no name-lookup API (only email), so this scans `users.list`
        and matches against real_name, display_name, and the handle. Paginates
        through the workspace until exhausted. Returns the matching users as a
        list of ``{"id", "name", "real_name", "display_name", "email"}`` dicts.
        """
        needle = name.strip().lower()
        if not needle:
            return {"ok": False, "error": "empty name", "users": []}
        matches: list[dict[str, Any]] = []
        cursor: str | None = None
        try:
            while True:
                resp = self._web.users_list(limit=limit, cursor=cursor)
                for m in resp.get("members", []):
                    if m.get("deleted") or m.get("is_bot"):
                        continue
                    profile = m.get("profile", {})
                    real_name = m.get("real_name") or profile.get("real_name", "")
                    display_name = profile.get("display_name", "")
                    handle = m.get("name", "")
                    haystacks = (real_name, display_name, handle)
                    if any(needle in (h or "").lower() for h in haystacks):
                        matches.append(
                            {
                                "id": m["id"],
                                "name": handle,
                                "real_name": real_name,
                                "display_name": display_name,
                                "email": profile.get("email"),
                            }
                        )
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            return {"ok": True, "users": matches}
        except SlackApiError as e:
            return {"ok": False, "error": str(e), "users": []}

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
