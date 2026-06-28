"""Read-only BigQuery client + pure helpers — shared infra for agent tools.

Two capabilities, behind hard guardrails: inspect schema (list tables / columns)
and run a read-only SQL query. Agents wrap these methods as their own tools.

Layered like the rest of `src/`:
- pure helpers at the top (no I/O, no clock — `is_read_only`,
  `validate_dataset_allowed`, `build_information_schema_query`, `apply_caps`).
- `BigQueryClient`, the I/O wrapper over an injected `google.cloud.bigquery.Client`
  (tests pass a fake — see `tests/stubs.FakeBQClient`).

Guardrails (the app-layer belt; the SA's IAM/ACL grants are the real boundary —
see `scripts/provision_bq_readonly_sa.py`):
- `is_read_only` rejects anything but a single SELECT/WITH (friendly, fast).
- a dry-run gates on `statement_type == "SELECT"` (blocks EXPORT DATA / DML / CALL
  / multi-statement that slip past the text check) and on the byte estimate vs.
  `max_bytes_billed` (cost) before the real run.
- referenced datasets are checked against the allowlist (∪ authorized sources).
- the real job carries `maximum_bytes_billed`, query-cache, a pinned location and
  `{agent, caller}` labels; on client timeout the job is cancelled (stops billing).
- results are capped by row count AND serialized bytes (`truncated` flags it).
- every call emits one structured audit record via the injected `audit_log`.

The clock is injected (`now`) so audit timestamps/durations are testable.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable

from google.cloud import bigquery

# --- exceptions -------------------------------------------------------------


class QueryNotAllowed(ValueError):
    """A query was rejected by an app-layer guardrail (IAM is the real boundary)."""


class QueryTooLarge(QueryNotAllowed):
    """The dry-run byte estimate exceeded the configured cap."""


class QueryTimedOut(RuntimeError):
    """The query ran longer than the timeout and was cancelled."""


# --- pure helpers (no I/O, no clock) ----------------------------------------

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip_sql_comments(sql: str) -> str:
    """Remove block (/* */) and line (--) comments so they can't hide statements."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def is_read_only(sql: str) -> bool:
    """True only for a single SELECT/WITH statement.

    A friendly, fast pre-check — not the security boundary. Strips comments,
    rejects multiple statements, and rejects anything whose first keyword is not
    SELECT or WITH (so EXPORT DATA, INSERT/UPDATE/DELETE/MERGE, CREATE/DROP, CALL,
    etc. are all refused). The dry-run `statement_type` gate is the stronger check.
    """
    s = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not s or ";" in s:  # empty, or more than one statement
        return False
    parts = s.split(None, 1)
    return bool(parts) and parts[0].upper() in {"SELECT", "WITH"}


def validate_dataset_allowed(dataset: str, allowed: frozenset[str]) -> None:
    """Raise QueryNotAllowed unless `dataset` is in the allowlist."""
    if dataset not in allowed:
        raise QueryNotAllowed(
            f"dataset not in allowlist: {dataset} (allowed: {sorted(allowed)})"
        )


def _validate_identifier(name: str, kind: str) -> str:
    """Guard a dataset/table identifier before inlining it into metadata SQL."""
    if not _IDENT.match(name):
        raise QueryNotAllowed(f"invalid {kind} identifier: {name!r}")
    return name


def build_information_schema_query(
    project: str, dataset: str, table: str | None = None
) -> str:
    """Compose the INFORMATION_SCHEMA query for a dataset's tables or one table's columns.

    Columns come from COLUMN_FIELD_PATHS (not COLUMNS) so nested STRUCT/ARRAY paths
    and dbt-persisted descriptions both surface.
    """
    _validate_identifier(project.replace("-", "_"), "project")  # projects allow hyphens
    _validate_identifier(dataset, "dataset")
    src = f"`{project}.{dataset}`.INFORMATION_SCHEMA"
    if table is None:
        return f"SELECT table_name, table_type FROM {src}.TABLES ORDER BY table_name"
    _validate_identifier(table, "table")
    return (
        f"SELECT field_path AS column, data_type, description "
        f"FROM {src}.COLUMN_FIELD_PATHS "
        f"WHERE table_name = '{table}' ORDER BY field_path"
    )


def apply_caps(
    rows: list[dict[str, Any]], max_rows: int, max_result_bytes: int
) -> tuple[list[dict[str, Any]], bool]:
    """Trim rows to the row-count and serialized-byte caps; return (rows, truncated)."""
    truncated = False
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        truncated = True
    while rows and len(json.dumps(rows, default=str).encode()) > max_result_bytes:
        rows = rows[:-1]
        truncated = True
    return rows, truncated


def _label_safe(value: str | None) -> str:
    """Coerce a value into a valid BigQuery label (lowercase, [a-z0-9_-], <=63)."""
    v = (value or "none").lower()
    v = re.sub(r"[^a-z0-9_-]", "_", v)
    return v[:63] or "none"


def log_to_stdout(record: dict[str, Any]) -> None:
    """Default audit sink: one JSON line to stdout (lands in Cloud Run logs)."""
    print(json.dumps(record, default=str), flush=True)


# --- client (I/O edge) ------------------------------------------------------


class BigQueryClient:
    def __init__(
        self,
        client: bigquery.Client,
        *,
        project: str,
        allowed_datasets: frozenset[str],
        max_bytes_billed: int,
        max_rows: int,
        max_result_bytes: int,
        default_timeout_s: float,
        agent_name: str,
        authorized_source_datasets: frozenset[str] = frozenset(),
        audit_log: Callable[[dict[str, Any]], None] = log_to_stdout,
        location: str = "US",
        now: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._project = project
        self._allowed = allowed_datasets
        # datasets that may appear in referenced tables via authorized views
        self._readable = allowed_datasets | authorized_source_datasets
        self._max_bytes_billed = max_bytes_billed
        self._max_rows = max_rows
        self._max_result_bytes = max_result_bytes
        self._timeout_s = default_timeout_s
        self._agent_name = agent_name
        self._audit_log = audit_log
        self._location = location
        self._now = now

    # -- public tools --------------------------------------------------------

    def list_tables(self, dataset: str) -> list[dict[str, Any]]:
        """List tables/views in an allowlisted dataset: [{table_name, table_type}]."""
        validate_dataset_allowed(dataset, self._allowed)
        sql = build_information_schema_query(self._project, dataset)
        return self._execute(sql, caller=None, label="list_tables")["rows"]

    def get_schema(self, dataset: str, table: str) -> list[dict[str, Any]]:
        """Columns of one table: [{column, data_type, description}] (incl. nested paths)."""
        validate_dataset_allowed(dataset, self._allowed)
        sql = build_information_schema_query(self._project, dataset, table)
        return self._execute(sql, caller=None, label="get_schema")["rows"]

    def run_query(self, sql: str, *, caller: str | None = None) -> dict[str, Any]:
        """Run a read-only SELECT; return rows + metadata.

        Returns {rows, returned_rows, total_rows, truncated, bytes_processed,
        bytes_billed, job_id, statement_type}. Raises QueryNotAllowed /
        QueryTooLarge / QueryTimedOut on a guardrail trip.
        """
        return self._execute(sql, caller=caller, label="run_query")

    # -- internals -----------------------------------------------------------

    def _job_config(self, *, dry_run: bool, caller: str | None = None) -> bigquery.QueryJobConfig:
        cfg = bigquery.QueryJobConfig(
            dry_run=dry_run,
            use_query_cache=not dry_run,
            maximum_bytes_billed=self._max_bytes_billed,
        )
        if not dry_run:
            cfg.labels = {"agent": _label_safe(self._agent_name), "caller": _label_safe(caller)}
        return cfg

    def _execute(self, sql: str, *, caller: str | None, label: str) -> dict[str, Any]:
        started = self._now()

        if not is_read_only(sql):
            self._audit(label, caller, sql, status="rejected", started=started,
                        error="not a single read-only SELECT/WITH statement")
            raise QueryNotAllowed("only a single read-only SELECT/WITH statement is allowed")

        # Dry run: validate statement type + cost + referenced datasets before billing.
        dry = self._client.query(sql, job_config=self._job_config(dry_run=True), location=self._location)
        statement_type = getattr(dry, "statement_type", None)
        if statement_type not in (None, "SELECT"):
            self._audit(label, caller, sql, status="rejected", started=started,
                        statement_type=statement_type, error="non-SELECT statement type")
            raise QueryNotAllowed(f"only SELECT statements are allowed (got {statement_type})")

        estimate = getattr(dry, "total_bytes_processed", 0) or 0
        if estimate > self._max_bytes_billed:
            self._audit(label, caller, sql, status="rejected", started=started,
                        statement_type=statement_type,
                        error=f"estimate {estimate} > cap {self._max_bytes_billed}")
            raise QueryTooLarge(
                f"query would scan ~{estimate} bytes, over the {self._max_bytes_billed}-byte cap"
            )

        for ref in getattr(dry, "referenced_tables", []) or []:
            ds = getattr(ref, "dataset_id", None)
            if ds is not None and ds not in self._readable:
                self._audit(label, caller, sql, status="rejected", started=started,
                            statement_type=statement_type, error=f"references off-allowlist dataset: {ds}")
                raise QueryNotAllowed(f"query references dataset not in allowlist: {ds}")

        # Real run.
        job = self._client.query(
            sql, job_config=self._job_config(dry_run=False, caller=caller), location=self._location
        )
        try:
            iterator = job.result(timeout=self._timeout_s)
        except FuturesTimeout:
            job.cancel()  # stop billing; don't leave it running
            self._audit(label, caller, sql, status="timeout", started=started, job=job,
                        statement_type=statement_type, error=f"exceeded {self._timeout_s}s")
            raise QueryTimedOut(f"query exceeded {self._timeout_s}s and was cancelled")

        all_rows = [dict(r) for r in iterator]
        total_rows = getattr(iterator, "total_rows", None)
        if total_rows is None:
            total_rows = len(all_rows)
        rows, truncated = apply_caps(all_rows, self._max_rows, self._max_result_bytes)

        result = {
            "rows": rows,
            "returned_rows": len(rows),
            "total_rows": total_rows,
            "truncated": truncated,
            "bytes_processed": getattr(job, "total_bytes_processed", None),
            "bytes_billed": getattr(job, "total_bytes_billed", None),
            "job_id": getattr(job, "job_id", None),
            "statement_type": statement_type,
        }
        self._audit(label, caller, sql, status="success", started=started, job=job,
                    statement_type=statement_type, returned_rows=len(rows),
                    total_rows=total_rows, truncated=truncated)
        return result

    def _audit(
        self,
        label: str,
        caller: str | None,
        sql: str,
        *,
        status: str,
        started: float,
        job: Any = None,
        statement_type: str | None = None,
        returned_rows: int | None = None,
        total_rows: int | None = None,
        truncated: bool | None = None,
        error: str | None = None,
    ) -> None:
        ts = self._now()
        record = {
            "timestamp": ts,
            "agent": self._agent_name,
            "caller": caller,
            "label": label,
            "sql": sql,
            "job_id": getattr(job, "job_id", None),
            "statement_type": statement_type,
            "bytes_processed": getattr(job, "total_bytes_processed", None),
            "bytes_billed": getattr(job, "total_bytes_billed", None),
            "total_rows": total_rows,
            "returned_rows": returned_rows,
            "truncated": truncated,
            "duration_ms": round((ts - started) * 1000, 1),
            "status": status,
            "error": error,
        }
        try:
            self._audit_log(record)
        except Exception:  # never let audit logging break a query
            pass
