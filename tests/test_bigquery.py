"""Unit tests for src/bigquery.py — pure helpers + the guarded client.

Plain functions, no fixtures; the client is driven by tests/stubs.FakeBQClient so
nothing touches the network. (Live BigQuery tests would live in a `*_live` file
gated on credentials, like test_data_client.py.)

    pytest tests/test_bigquery.py
"""
from __future__ import annotations

import pytest

from src.bigquery import (
    BigQueryClient,
    QueryNotAllowed,
    QueryTimedOut,
    QueryTooLarge,
    apply_caps,
    build_information_schema_query,
    is_read_only,
    validate_dataset_allowed,
)
from tests.stubs import FakeBQClient

ALLOWED = frozenset({"core", "base", "dbt_views"})
SOURCES = frozenset({"raw_wfx"})


def _client(fake: FakeBQClient, *, audit=None, **overrides):
    kwargs = dict(
        project="tecovas-prod-edw",
        allowed_datasets=ALLOWED,
        authorized_source_datasets=SOURCES,
        max_bytes_billed=1_000_000,
        max_rows=100,
        max_result_bytes=10_000,
        default_timeout_s=30,
        agent_name="wfx-test",
        audit_log=audit if audit is not None else (lambda r: None),
        now=lambda: 1_000.0,
    )
    kwargs.update(overrides)
    return BigQueryClient(fake, **kwargs)


# --- pure helpers -----------------------------------------------------------


def test_is_read_only_accepts_select_and_with():
    assert is_read_only("SELECT 1")
    assert is_read_only("  with t as (select 1) select * from t  ")
    assert is_read_only("SELECT 1; ")  # trailing semicolon is fine


def test_is_read_only_rejects_writes_and_multistatement():
    assert not is_read_only("EXPORT DATA OPTIONS(uri='gs://x') AS SELECT 1")
    assert not is_read_only("INSERT INTO t VALUES (1)")
    assert not is_read_only("CREATE TABLE t AS SELECT 1")
    assert not is_read_only("CALL my_proc()")
    assert not is_read_only("SELECT 1; DROP TABLE t")
    assert not is_read_only("")
    # a comment must not smuggle a second statement past the check
    assert not is_read_only("SELECT 1; -- comment\nDELETE FROM t")


def test_validate_dataset_allowed():
    validate_dataset_allowed("core", ALLOWED)  # no raise
    with pytest.raises(QueryNotAllowed):
        validate_dataset_allowed("net_suite", ALLOWED)


def test_build_information_schema_query():
    tables = build_information_schema_query("p", "core")
    assert "INFORMATION_SCHEMA.TABLES" in tables
    cols = build_information_schema_query("p", "core", "core__products")
    assert "COLUMN_FIELD_PATHS" in cols  # nested paths + descriptions
    assert "core__products" in cols
    with pytest.raises(QueryNotAllowed):
        build_information_schema_query("p", "bad-dataset")  # invalid identifier


def test_apply_caps_row_and_byte_limits():
    rows = [{"n": i} for i in range(10)]
    capped, truncated = apply_caps(rows, max_rows=5, max_result_bytes=10_000)
    assert len(capped) == 5 and truncated
    capped, truncated = apply_caps(rows, max_rows=100, max_result_bytes=20)
    assert truncated and len(capped) < 10  # byte cap kicked in


# --- client guardrails ------------------------------------------------------


def test_run_query_rejects_non_read_only():
    client = _client(FakeBQClient())
    with pytest.raises(QueryNotAllowed):
        client.run_query("DELETE FROM core.core__products")


def test_run_query_rejects_non_select_statement_type():
    # passes the text check but the dry-run reports a non-SELECT statement type
    fake = FakeBQClient(statement_type="SCRIPT")
    with pytest.raises(QueryNotAllowed):
        _client(fake).run_query("SELECT 1")


def test_run_query_rejects_off_allowlist_referenced_dataset():
    fake = FakeBQClient(referenced_datasets=("net_suite",))
    with pytest.raises(QueryNotAllowed):
        _client(fake).run_query("SELECT * FROM net_suite.t")


def test_run_query_allows_authorized_source_dataset():
    # raw_wfx is reachable via authorized views, so a reference to it is fine
    fake = FakeBQClient(rows=[{"x": 1}], referenced_datasets=("dbt_views", "raw_wfx"))
    out = _client(fake).run_query("SELECT * FROM dbt_views.stg_wfx__items")
    assert out["returned_rows"] == 1


def test_run_query_rejects_over_byte_cap():
    fake = FakeBQClient(estimate_bytes=5_000_000)  # > 1_000_000 cap
    with pytest.raises(QueryTooLarge):
        _client(fake).run_query("SELECT * FROM core.core__sales")


def test_run_query_truncates_rows():
    fake = FakeBQClient(rows=[{"n": i} for i in range(250)])
    out = _client(fake, max_rows=100).run_query("SELECT n FROM core.t")
    assert out["returned_rows"] == 100
    assert out["truncated"] is True
    assert out["total_rows"] == 250


def test_run_query_timeout_cancels_job():
    fake = FakeBQClient(timeout=True)
    with pytest.raises(QueryTimedOut):
        _client(fake).run_query("SELECT 1")
    # the real-run job must have been cancelled (not left billing)
    assert any(j.cancelled for j in fake.jobs)


def test_run_query_emits_audit_record_with_caller_and_labels():
    captured: list[dict] = []
    fake = FakeBQClient(rows=[{"x": 1}], estimate_bytes=42, billed_bytes=99)
    out = _client(fake, audit=captured.append).run_query("SELECT 1", caller="U123")
    assert out["returned_rows"] == 1

    assert len(captured) == 1
    rec = captured[0]
    assert rec["status"] == "success"
    assert rec["caller"] == "U123"
    assert rec["agent"] == "wfx-test"
    assert rec["bytes_billed"] == 99
    assert rec["label"] == "run_query"

    # the real (non-dry) job carries sanitized {agent, caller} labels
    real = [q for q in fake.queries if not q["dry_run"]][0]
    assert real["labels"] == {"agent": "wfx-test", "caller": "u123"}


def test_rejected_query_is_audited():
    captured: list[dict] = []
    with pytest.raises(QueryNotAllowed):
        _client(FakeBQClient(), audit=captured.append).run_query("DROP TABLE t")
    assert captured and captured[0]["status"] == "rejected"


# --- schema inspection ------------------------------------------------------


def test_list_tables_and_get_schema():
    fake = FakeBQClient(rows=[{"table_name": "core__products", "table_type": "BASE TABLE"}])
    assert _client(fake).list_tables("core")[0]["table_name"] == "core__products"

    with pytest.raises(QueryNotAllowed):
        _client(FakeBQClient()).list_tables("net_suite")  # off-allowlist

    fake2 = FakeBQClient(rows=[{"column": "item_id", "data_type": "STRING", "description": "id"}])
    cols = _client(fake2).get_schema("base", "base_products__wfx")
    assert cols[0]["column"] == "item_id"
