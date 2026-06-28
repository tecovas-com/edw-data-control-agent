"""Tiny stub clients for tests. No pytest fixtures anywhere."""
from __future__ import annotations

from typing import Any


class StubControlCenterClient:
    """Stand-in for ControlCenterClient: canned responses, records actions."""

    def __init__(
        self,
        models: list[dict[str, Any]] | None = None,
        statuses: dict[str, dict[str, Any]] | None = None,
        refusing: bool = False,
    ) -> None:
        self._models = models or []
        self._statuses = statuses or {}
        self._refusing = refusing  # refresh_model refuses (server-side rate limit)
        self.triggered: list[tuple[str, str]] = []
        self.refreshed: list[str] = []

    def list_models(self) -> list[dict[str, Any]]:
        return self._models

    def models_status(self, filter: str = "all") -> dict[str, Any]:
        models = list(self._statuses.values())
        if filter == "stale":
            models = [m for m in models if not m.get("overall_is_fresh", False)]
        return {"checked_at": "2026-06-20T00:00:00Z", "models": models}

    def get_model_status(self, unique_id: str) -> dict[str, Any]:
        return self._statuses[unique_id]

    def refresh_model(self, unique_id: str) -> dict[str, Any]:
        self.refreshed.append(unique_id)
        if self._refusing:
            return {"ok": False, "refused": True, "reason": "rate limit: re-run at most once per hour"}
        return {"ok": True, "run_id": "run-stub-123", "unique_id": unique_id}

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


class _FakeRef:
    """A referenced-table ref exposing just `.dataset_id` (what the client reads)."""

    def __init__(self, dataset_id: str) -> None:
        self.dataset_id = dataset_id


class _FakeRowIterator:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.total_rows = len(rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeBQJob:
    """A query job: dry runs expose statement_type/estimate/refs; real runs yield rows."""

    def __init__(
        self,
        *,
        dry: bool,
        rows: list[dict[str, Any]] | None = None,
        statement_type: str = "SELECT",
        total_bytes_processed: int = 0,
        total_bytes_billed: int = 0,
        referenced_datasets: tuple[str, ...] = (),
        timeout: bool = False,
        job_id: str = "job-fake",
    ) -> None:
        self._rows = rows or []
        self._timeout = timeout
        self.statement_type = statement_type
        self.total_bytes_processed = total_bytes_processed
        self.total_bytes_billed = 0 if dry else total_bytes_billed
        self.referenced_tables = [_FakeRef(d) for d in referenced_datasets] if dry else []
        self.job_id = None if dry else job_id
        self.cancelled = False

    def result(self, timeout: float | None = None) -> _FakeRowIterator:
        if self._timeout:
            from concurrent.futures import TimeoutError as _T
            raise _T()
        return _FakeRowIterator(self._rows)

    def cancel(self) -> None:
        self.cancelled = True


class FakeBQClient:
    """Fake google.cloud.bigquery.Client for testing BigQueryClient without the network.

    Configure the canned dry-run verdict (statement_type, byte estimate, referenced
    datasets) and the real-run rows; records every `query()` call (sql, dry_run,
    labels) and every job created (to assert cancellation on timeout).
    """

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        statement_type: str = "SELECT",
        estimate_bytes: int = 0,
        billed_bytes: int = 0,
        referenced_datasets: tuple[str, ...] = (),
        timeout: bool = False,
    ) -> None:
        self._rows = rows or []
        self._statement_type = statement_type
        self._estimate_bytes = estimate_bytes
        self._billed_bytes = billed_bytes
        self._referenced_datasets = referenced_datasets
        self._timeout = timeout
        self.queries: list[dict[str, Any]] = []
        self.jobs: list[_FakeBQJob] = []

    def query(self, sql: str, job_config: Any = None, location: str | None = None) -> _FakeBQJob:
        dry = bool(getattr(job_config, "dry_run", False))
        self.queries.append(
            {"sql": sql, "dry_run": dry, "labels": dict(getattr(job_config, "labels", {}) or {})}
        )
        job = _FakeBQJob(
            dry=dry,
            rows=self._rows,
            statement_type=self._statement_type,
            total_bytes_processed=self._estimate_bytes,
            total_bytes_billed=self._billed_bytes,
            referenced_datasets=self._referenced_datasets,
            timeout=self._timeout,
        )
        self.jobs.append(job)
        return job


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
        members: list[dict[str, Any]] | None = None,
        users_by_email: dict[str, str] | None = None,
        channels: list[dict[str, Any]] | None = None,
    ) -> None:
        self._bot_user_id = bot_user_id
        self._bot_id = bot_id
        self._history = history or []
        self._replies = replies or {}
        self._members = members or []
        self._users_by_email = users_by_email or {}
        self._channels = channels or []
        self.deleted: list[str] = []
        self.posted: list[dict[str, Any]] = []
        self.opened: list[str] = []

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

    def conversations_open(self, users: str) -> dict[str, Any]:
        self.opened.append(users)
        return {"ok": True, "channel": {"id": f"D_{users}"}}

    def users_lookupByEmail(self, email: str) -> dict[str, Any]:
        return {"ok": True, "user": {"id": self._users_by_email[email]}}

    def conversations_list(
        self, limit: int = 1000, cursor: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        # One channel per page so tests exercise the cursor loop.
        idx = int(cursor) if cursor else 0
        chans = self._channels[idx : idx + 1]
        next_idx = idx + 1
        next_cursor = str(next_idx) if next_idx < len(self._channels) else ""
        return {
            "ok": True,
            "channels": chans,
            "response_metadata": {"next_cursor": next_cursor},
        }

    def users_list(self, limit: int = 1000, cursor: str | None = None) -> dict[str, Any]:
        # Paginate one member per page so tests exercise the cursor loop.
        idx = int(cursor) if cursor else 0
        member = self._members[idx : idx + 1]
        next_idx = idx + 1
        next_cursor = str(next_idx) if next_idx < len(self._members) else ""
        return {
            "ok": True,
            "members": member,
            "response_metadata": {"next_cursor": next_cursor},
        }
