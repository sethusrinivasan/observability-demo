"""
tests/test_duckdb_telemetry_query.py
Integration tests for DuckDB OLAP query engine querying S3 Parquet Data Lake
via Canary API (/api/duckdb/canned-queries, /api/duckdb/query).
"""

import pytest
import requests

CANARY_URL = "http://localhost:8085"
RECEIVER_URL = "http://localhost:4319"


@pytest.fixture(scope="module", autouse=True)
def prepare_telemetry_data():
    """Ensure receiver has flushed at least one batch to S3 before querying."""
    # Send test trace, metric, log
    requests.post(f"{RECEIVER_URL}/v1/traces", json={
        "resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "duckdb-test-service"}}]},
            "scopeSpans": [{"spans": [{
                "name": "duckdb_test_span",
                "traceId": "1122334455667788",
                "spanId": "8877665544332211",
                "startTimeUnixNano": 1792536506058000000,
                "endTimeUnixNano": 1792536506060000000
            }]}]
        }]
    }, timeout=5)

    requests.post(f"{RECEIVER_URL}/v1/logs", json={
        "resourceLogs": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "duckdb-test-service"}}]},
            "scopeLogs": [{"logRecords": [{
                "timeUnixNano": 1792536506058000000,
                "body": {"stringValue": "DuckDB test log entry: simulated fault test"}
            }]}]
        }]
    }, timeout=5)

    # Flush receiver to S3
    requests.post(f"{CANARY_URL}/api/s3/flush", timeout=5)


def test_duckdb_canned_queries_list():
    """Test GET /api/duckdb/canned-queries returns full suite of curated queries."""
    res = requests.get(f"{CANARY_URL}/api/duckdb/canned-queries", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    queries = data["queries"]
    assert len(queries) >= 7

    # Verify query IDs and structure
    ids = [q["id"] for q in queries]
    assert "event_breakdown" in ids
    assert "nanosecond_traces" in ids
    assert "error_warning_logs" in ids
    assert "throughput_10s_buckets" in ids
    assert "s3_parquet_files" in ids
    assert "nanosecond_jitter" in ids
    assert "metric_summary" in ids

    for q in queries:
        assert "name" in q and len(q["name"]) > 0
        assert "description" in q and len(q["description"]) > 0
        assert "sql" in q and "FROM " in q["sql"].upper()


def test_duckdb_run_all_canned_queries():
    """Execute each canned query and ensure it returns valid data without SQL/binder errors."""
    res = requests.get(f"{CANARY_URL}/api/duckdb/canned-queries", timeout=5)
    queries = res.json()["queries"]

    for q in queries:
        sql = q["sql"]
        post_res = requests.post(f"{CANARY_URL}/api/duckdb/query", json={"query": sql, "max_rows": 20}, timeout=10)
        assert post_res.status_code == 200, f"Query '{q['name']}' returned HTTP {post_res.status_code}"
        res_data = post_res.json()
        assert res_data["status"] == "success", f"Query '{q['name']}' failed with error: {res_data.get('error')}"
        assert "columns" in res_data
        assert "rows" in res_data
        assert res_data["row_count"] >= 0
        assert res_data["execution_time_ms"] > 0
        assert res_data["bucket"] == "telemetry-lake"


def test_duckdb_nanosecond_precision():
    """Verify nanosecond timestamps (timestamp_ns) are preserved as 64-bit integers."""
    sql = "SELECT service_name, event_name, timestamp_ns, timestamp_iso FROM telemetry WHERE timestamp_ns IS NOT NULL ORDER BY timestamp_ns DESC LIMIT 5;"
    res = requests.post(f"{CANARY_URL}/api/duckdb/query", json={"query": sql}, timeout=10)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert len(data["rows"]) > 0

    col_idx = data["columns"].index("timestamp_ns")
    for row in data["rows"]:
        ts_ns = row[col_idx]
        assert isinstance(ts_ns, int), f"timestamp_ns should be int64, got {type(ts_ns)}"
        # Nanosecond epoch in 2026 is > 1.7e18
        assert ts_ns > 1_700_000_000_000_000_000, f"Timestamp {ts_ns} not in expected nanosecond range"


def test_duckdb_error_handling():
    """Verify invalid SQL returns a clean error message and not 500 crash."""
    # 1. Empty query
    res = requests.post(f"{CANARY_URL}/api/duckdb/query", json={"query": ""}, timeout=5)
    assert res.status_code == 200
    assert res.json()["status"] == "error"
    assert "empty" in res.json()["error"].lower()

    # 2. Syntax error
    res2 = requests.post(f"{CANARY_URL}/api/duckdb/query", json={"query": "SELEC NONEXISTENT FROM BAD_SYNTAX"}, timeout=5)
    assert res2.status_code == 200
    assert res2.json()["status"] == "error"
    assert "error" in res2.json()["error"].lower() or "syntax" in res2.json()["error"].lower()


def test_canary_html_tab_present():
    """Verify the Canary dashboard HTML includes the 'Telemetry view - Query editor' tab and section."""
    res = requests.get(CANARY_URL, timeout=5)
    assert res.status_code == 200
    html = res.text
    assert "Telemetry view - Query editor" in html
    assert 'switchView(\'telemetry_query\', this)' in html
    assert 'id="telemetry-query-section"' in html
    assert 'id="duckdb-canned-select"' in html
    assert 'id="duckdb-query-input"' in html
    assert 'id="duckdb-grid-table"' in html
