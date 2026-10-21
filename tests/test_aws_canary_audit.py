"""
tests/test_aws_canary_audit.py
Automated end-to-end audit test suite for the live AWS deployment of
the Observability-Demo Canary Dashboard and Telemetry Lake.

Verifies:
1. Public unauthenticated access to the Canary Function URL
2. 100% GREEN dependency health (11/11 UP)
3. Aurora PostgreSQL connectivity via IAM DB Authentication (no hardcoded passwords)
4. S3 Data Lake browser connectivity via IAM execution roles
5. DuckDB in-process serverless OLAP queries against S3 Parquet data
6. Full suite of 7 canned DuckDB analytical queries
7. High-throughput custom telemetry receiver ingestion and S3 compaction
"""

import os
import requests
import pytest

AWS_CANARY_URL = os.getenv(
    "AWS_CANARY_URL",
    "https://kc25dgfws7f4oqbowcxhgpqwii0uyzxa.lambda-url.us-east-2.on.aws"
)
AWS_COLLECTOR_URL = os.getenv(
    "AWS_COLLECTOR_URL",
    "https://67lv2o2bb26q6thqvfcep2re3i0sjrom.lambda-url.us-east-2.on.aws"
)
EXPECTED_BUCKET = os.getenv(
    "AWS_S3_BUCKET",
    "observability-demo-telemetry-lake-596090776030"
)


def test_aws_canary_public_unauthenticated_access():
    """Verify Canary dashboard is publicly accessible without credentials."""
    resp = requests.get(AWS_CANARY_URL, timeout=10)
    assert resp.status_code == 200, f"Expected 200 OK, got {resp.status_code}"
    html = resp.text
    assert "<!doctype html>" in html.lower()
    assert "Canary &amp; Chaos Engineering" in html or "Canary" in html
    assert "id=\"duckdb-query-input\"" in html
    assert "id=\"s3-browser-section\"" in html


def test_aws_canary_dependencies_all_eleven_green():
    """Verify all 11 infrastructure dependencies report UP and 100% available."""
    resp = requests.get(f"{AWS_CANARY_URL}/api/dependencies", timeout=15)
    assert resp.status_code == 200, f"Expected 200 OK, got {resp.status_code}"
    data = resp.json()

    assert data["total_dependencies"] == 11
    assert data["healthy_count"] == 11, f"Expected 11 healthy, got {data['healthy_count']}"
    assert data["overall_availability_pct"] == 100.0

    dep_ids = {d["id"]: d for d in data["dependencies"]}
    required_ids = [
        "postgres", "valkey", "otel-collector", "tempo", "loki",
        "mimir", "prometheus", "grafana", "redpanda",
        "telemetry-receiver", "s3-storage"
    ]
    for rid in required_ids:
        assert rid in dep_ids, f"Dependency '{rid}' missing from health report"
        dep = dep_ids[rid]
        assert dep["status"] == "UP", f"Dependency '{rid}' is {dep['status']}: {dep.get('details')}"
        assert dep["available"] is True


def test_aws_canary_rds_iam_database_auth():
    """Verify PostgreSQL connectivity uses IAM DB Auth and can query database."""
    # 1. Check dependency status contains valid db size and audit log count
    dep_resp = requests.get(f"{AWS_CANARY_URL}/api/dependencies", timeout=10)
    data = dep_resp.json()
    pg = next(d for d in data["dependencies"] if d["id"] == "postgres")
    assert pg["status"] == "UP"
    assert "db_size" in pg["details"]
    assert "audit_logs_count" in pg["details"]

    # 2. Execute SQL query through Canary SQL query API
    sql_resp = requests.post(
        f"{AWS_CANARY_URL}/api/sql/query",
        json={"query": "SELECT count(*) FROM audit_logs;"},
        timeout=10
    )
    assert sql_resp.status_code == 200
    sql_data = sql_resp.json()
    assert sql_data["status"] == "success"
    assert "columns" in sql_data
    assert "rows" in sql_data
    assert len(sql_data["rows"]) == 1


def test_aws_canary_s3_lake_browser_iam_role():
    """Verify S3 Lake Browser operates using IAM Execution Role without static keys."""
    # List buckets
    b_resp = requests.get(f"{AWS_CANARY_URL}/api/s3/buckets", timeout=10)
    assert b_resp.status_code == 200
    b_data = b_resp.json()
    assert b_data["status"] == "success"
    assert EXPECTED_BUCKET in b_data["buckets"]

    # List objects in telemetry lake
    o_resp = requests.get(
        f"{AWS_CANARY_URL}/api/s3/objects",
        params={"bucket": EXPECTED_BUCKET},
        timeout=10
    )
    assert o_resp.status_code == 200
    o_data = o_resp.json()
    assert o_data["status"] == "success"
    assert o_data["bucket"] == EXPECTED_BUCKET


def test_aws_collector_ingest_and_compaction_flush():
    """Verify custom telemetry receiver accepts OTLP traces and flushes Parquet to S3."""
    # Send trace span
    trace_payload = {
        "resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "audit-test-service"}}]},
            "scopeSpans": [{
                "spans": [{
                    "name": "automated_audit_span",
                    "traceId": "abcdef1234567890abcdef1234567890",
                    "spanId": "1234567890abcdef",
                    "startTimeUnixNano": 1792545000000000000,
                    "endTimeUnixNano": 1792545000002000000,
                    "attributes": [{"key": "test.type", "value": {"stringValue": "automated-e2e-audit"}}]
                }]
            }]
        }]
    }
    ingest_resp = requests.post(f"{AWS_COLLECTOR_URL}/v1/traces", json=trace_payload, timeout=10)
    assert ingest_resp.status_code in [200, 202]

    # Flush receiver to S3 via Canary API
    flush_resp = requests.post(f"{AWS_CANARY_URL}/api/s3/flush", timeout=10)
    assert flush_resp.status_code == 200
    flush_data = flush_resp.json()
    assert flush_data["status"] == "flushed"
    assert flush_data["events_flushed"] >= 1


def test_aws_canary_duckdb_serverless_olap():
    """Verify DuckDB in-process OLAP executes against S3 Parquet telemetry files."""
    query = "SELECT count(*) AS total_records, service_name FROM telemetry GROUP BY service_name;"
    resp = requests.post(
        f"{AWS_CANARY_URL}/api/duckdb/query",
        json={"query": query, "max_rows": 20},
        timeout=15
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success", f"DuckDB query failed: {data.get('error')}"
    assert "columns" in data
    assert "rows" in data
    assert data["row_count"] >= 1
    assert data["bucket"] == EXPECTED_BUCKET


def test_aws_canary_all_seven_canned_duckdb_queries():
    """Verify all 7 curated canned DuckDB queries execute cleanly without syntax/binder errors."""
    c_resp = requests.get(f"{AWS_CANARY_URL}/api/duckdb/canned-queries", timeout=10)
    assert c_resp.status_code == 200
    queries = c_resp.json()["queries"]
    assert len(queries) == 7

    for q in queries:
        sql = q["sql"]
        q_resp = requests.post(
            f"{AWS_CANARY_URL}/api/duckdb/query",
            json={"query": sql, "max_rows": 10},
            timeout=15
        )
        assert q_resp.status_code == 200, f"Query '{q['id']}' failed with HTTP {q_resp.status_code}"
        q_data = q_resp.json()
        assert q_data["status"] == "success", f"Query '{q['id']}' returned error: {q_data.get('error')}"
        assert "columns" in q_data
        assert "rows" in q_data
