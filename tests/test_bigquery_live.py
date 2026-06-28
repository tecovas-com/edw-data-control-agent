"""LIVE tests for BigQueryClient — they hit real BigQuery.

No fakes, no stubs, no pytest fixtures: these build a real client (ADC, via
`google.cloud.bigquery.Client()`) and run read-only queries against the warehouse.
They ERROR (fail loud) if the BQ_* env (see `.env.template`) is unset.

Only READ paths are exercised — schema inspection and small SELECTs. Each response
is written to tests/output/ (git-ignored) for manual inspection.

    pytest tests/test_bigquery_live.py
    pytest -k "not live"                # skip all network tests
"""
from __future__ import annotations

import json
from pathlib import Path

from google.cloud import bigquery

from src import settings
from src.bigquery import BigQueryClient

OUTPUT_DIR = Path(__file__).parent / "output"


def _live_client() -> BigQueryClient:
    if not settings.BQ_PROJECT or not settings.BQ_ALLOWED_DATASETS:
        raise RuntimeError(
            "set BQ_PROJECT and the other BQ_* env (see .env.template) to run "
            "live BigQuery tests"
        )
    return BigQueryClient(
        bigquery.Client(project=settings.BQ_PROJECT),
        project=settings.BQ_PROJECT,
        allowed_datasets=settings.BQ_ALLOWED_DATASETS,
        authorized_source_datasets=settings.BQ_AUTHORIZED_SOURCE_DATASETS,
        max_bytes_billed=settings.BQ_MAX_BYTES_BILLED,
        max_rows=settings.BQ_MAX_ROWS,
        max_result_bytes=settings.BQ_MAX_RESULT_BYTES,
        default_timeout_s=settings.BQ_QUERY_TIMEOUT_S,
        agent_name="wfx-live-test",
        location=settings.BQ_LOCATION or "US",
    )


def _write_output(name: str, data: object) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / name
    path.write_text(json.dumps(data, indent=2, default=str))
    return path


def test_live_list_tables_core():
    client = _live_client()

    tables = client.list_tables("core")
    _write_output("bq_list_tables_core.json", tables)

    assert isinstance(tables, list) and tables, tables
    assert {"table_name", "table_type"} <= set(tables[0]), tables[0]


def test_live_get_schema_base_products_wfx():
    client = _live_client()

    columns = client.get_schema("base", "base_products__wfx")
    _write_output("bq_get_schema_base_products__wfx.json", columns)

    assert isinstance(columns, list) and columns, columns
    assert {"column", "data_type"} <= set(columns[0]), columns[0]


def test_live_run_query_base_products_wfx():
    client = _live_client()

    out = client.run_query(
        "SELECT * FROM `tecovas-prod-edw.base`.base_products__wfx LIMIT 5",
        caller="live-test",
    )
    _write_output("bq_run_query_base_products__wfx.json", out)

    assert out["returned_rows"] >= 1, out
    assert "rows" in out and "bytes_processed" in out, out


def test_live_run_query_staging_view_over_raw():
    # A staging VIEW (dbt_views) reading raw_wfx — exercises the authorized-source
    # path: raw_wfx may appear in referenced tables but is not directly readable.
    client = _live_client()

    out = client.run_query(
        "SELECT item_code, item_name, item_active "
        "FROM `tecovas-prod-edw.dbt_views`.stg_wfx__items LIMIT 5",
        caller="live-test",
    )
    _write_output("bq_run_query_stg_wfx__items.json", out)

    assert out["returned_rows"] >= 1, out
