"""Tiny stub clients for tests. No pytest fixtures anywhere."""
from __future__ import annotations

from typing import Any


class StubControlCenterClient:
    """Stand-in for ControlCenterClient: canned responses, records actions."""

    def __init__(
        self,
        models: list[dict[str, Any]] | None = None,
        statuses: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._models = models or []
        self._statuses = statuses or {}
        self.triggered: list[tuple[str, str]] = []

    def list_models(self) -> list[dict[str, Any]]:
        return self._models

    def get_model_status(self, unique_id: str) -> dict[str, Any]:
        return self._statuses[unique_id]

    def trigger_loader(self, loader_type: str, loader_id: str) -> dict[str, Any]:
        self.triggered.append((loader_type, loader_id))
        return {"ok": True}


class StubSlackClient:
    """Stand-in for SlackClient: records posts instead of hitting the network."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.alerts: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.dms: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def post_alert(
        self,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        if self.fail:
            return {"ok": False, "error": "stub failure"}
        self.alerts.append({"text": text, "blocks": blocks, "channel": channel})
        return {"ok": True, "ts": "111.222", "channel": channel or "C_STUB"}

    def reply_in_thread(
        self, thread_ts: str, text: str, channel: str | None = None
    ) -> dict[str, Any]:
        self.replies.append({"thread_ts": thread_ts, "text": text})
        return {"ok": True, "ts": "333.444"}

    def send_dm(self, user: str, text: str) -> dict[str, Any]:
        self.dms.append((user, text))
        return {"ok": True, "ts": "555.666", "channel": "D_STUB", "user_id": user}

    def delete_message(self, ts: str, channel: str | None = None) -> dict[str, Any]:
        self.deleted.append(ts)
        return {"ok": True, "ts": ts}

    def delete_bot_messages(
        self, channel: str | None = None, limit: int = 200, pause_s: float = 0.0
    ) -> dict[str, Any]:
        deleted = [a["ts"] for a in self.alerts if "ts" in a] + [r["ts"] for r in self.replies if "ts" in r]
        self.deleted.extend(deleted)
        return {"ok": True, "deleted": deleted, "errors": [], "scanned": len(deleted)}

    def self_check(self) -> dict[str, Any]:
        return {"ok": True, "team": "stub", "user": "bot", "bot_id": "B_STUB"}


class FakeWebClient:
    """Fake slack_sdk.WebClient for testing SlackClient I/O without the network.

    Canned auth.test + conversations history/replies; records chat_delete calls so
    `delete_bot_messages` can be exercised deterministically.
    """

    def __init__(
        self,
        bot_user_id: str = "U_BOT",
        bot_id: str = "B_BOT",
        history: list[dict[str, Any]] | None = None,
        replies: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._bot_user_id = bot_user_id
        self._bot_id = bot_id
        self._history = history or []
        self._replies = replies or {}
        self.deleted: list[str] = []
        self.posted: list[dict[str, Any]] = []

    def auth_test(self) -> dict[str, Any]:
        return {"ok": True, "user_id": self._bot_user_id, "bot_id": self._bot_id, "team": "fake"}

    def conversations_history(self, channel: str, limit: int = 200) -> dict[str, Any]:
        return {"messages": self._history[:limit]}

    def conversations_replies(self, channel: str, ts: str, limit: int = 1000) -> dict[str, Any]:
        return {"messages": self._replies.get(ts, [])}

    def chat_delete(self, channel: str, ts: str) -> dict[str, Any]:
        self.deleted.append(ts)
        return {"ok": True, "ts": ts}

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ts": "999.000", "channel": kwargs.get("channel", "C_FAKE")}
