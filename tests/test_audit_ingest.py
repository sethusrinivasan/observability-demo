"""
tests/test_audit_ingest.py — Unit tests for the Lakehouse Audit Ingestion and Summarizer
"""

import os
import json
import uuid
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from canary.audit_ingest import sync_audit_lake_to_postgres, SUMMARIZED_TABLES_DDL


@pytest.fixture
def temp_parquet_dir(tmp_path):
    """Generate a realistic sample Parquet lake directory."""
    lake_dir = tmp_path / "telemetry_lake"
    lake_dir.mkdir(parents=True)

    schema = pa.schema([
        ("event_id", pa.string()),
        ("timestamp_ns", pa.int64()),
        ("timestamp_iso", pa.string()),
        ("telemetry_type", pa.string()),
        ("service_name", pa.string()),
        ("event_name", pa.string()),
        ("attributes", pa.string()),
        ("raw_payload", pa.string()),
    ])

    now_iso = datetime.now(timezone.utc).isoformat()
    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)

    data = {
        "event_id": [str(uuid.uuid4()) for _ in range(6)],
        "timestamp_ns": [now_ns + i * 1000 for i in range(6)],
        "timestamp_iso": [now_iso for _ in range(6)],
        "telemetry_type": ["log", "log", "log", "log", "metric", "trace"],
        "service_name": [
            "observability-python-app",
            "observability-python-app",
            "observability-go-app",
            "observability-node-app",
            "observability-canary",
            "observability-python-app",
        ],
        "event_name": ["audit_log", "audit_log", "audit_log", "audit_error", "metric.count", "span.call"],
        "attributes": [
            json.dumps({"endpoint": "/auditlog"}),
            json.dumps({"endpoint": "/auditlog"}),
            json.dumps({"endpoint": "/compute/10"}),
            json.dumps({"endpoint": "/eval", "error": "division by zero"}),
            json.dumps({"unit": "count"}),
            json.dumps({"span_id": "123"}),
        ],
        "raw_payload": [
            json.dumps({"endpoint": "/auditlog", "status_code": 200, "response_time_seconds": 0.012, "user_agent": "canary"}),
            json.dumps({"endpoint": "/auditlog", "status_code": 200, "response_time_seconds": 0.015, "user_agent": "canary"}),
            json.dumps({"endpoint": "/compute/10", "status_code": 200, "response_time_seconds": 0.045, "user_agent": "canary"}),
            json.dumps({"endpoint": "/eval", "status_code": 500, "response_time_seconds": 0.120, "user_agent": "canary"}),
            json.dumps({"value": 42}),
            json.dumps({"trace": "abc"}),
        ],
    }

    table = pa.Table.from_pydict(data, schema=schema)
    pq.write_table(table, lake_dir / "audit_test.parquet")
    return lake_dir


def test_summarized_tables_ddl_validity():
    """Verify DDL contains required tables and columns."""
    assert "audit_log_hourly_summary" in SUMMARIZED_TABLES_DDL
    assert "audit_anomalies" in SUMMARIZED_TABLES_DDL
    assert "hour_bucket" in SUMMARIZED_TABLES_DDL
    assert "p95_duration_sec" in SUMMARIZED_TABLES_DDL


def test_audit_sync_with_mocked_postgres(temp_parquet_dir):
    """Test DuckDB extraction and Postgres upsert execution with mocked DB."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.connection.encoding = "UTF8"
    mock_cursor.mogrify.side_effect = lambda sql, args: b"(" + b", ".join(str(x).encode() for x in args) + b")"
    mock_conn.cursor.return_value = mock_cursor

    # Mock table check
    mock_cursor.fetchone.return_value = (1,)

    with patch("psycopg2.connect", return_value=mock_conn):
        parquet_glob = f"{temp_parquet_dir}/**/*.parquet"
        result = sync_audit_lake_to_postgres(
            parquet_path=parquet_glob,
            pg_conn_params={"host": "localhost", "port": 5432, "dbname": "observability"},
            lookback_hours=24
        )

    assert result["status"] == "success"
    assert result["hourly_summaries"] >= 1
    assert result["dimensions_upserted"] >= 1
    assert result["anomalies_recorded"] >= 1
    assert mock_conn.commit.called


def test_audit_sync_empty_lake(tmp_path):
    """Test graceful skip when lake has no parquet files."""
    empty_dir = tmp_path / "empty_lake"
    empty_dir.mkdir()

    result = sync_audit_lake_to_postgres(
        parquet_path=f"{empty_dir}/**/*.parquet",
        pg_conn_params={"host": "localhost"}
    )
    assert result["status"] == "skipped"
