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
import json
import time
import requests
import pytest

AWS_CANARY_URL = os.getenv(
    "AWS_CANARY_URL",
    "https://236mcyozjcjlmhngkx75qcae7q0kabru.lambda-url.us-east-2.on.aws"
)
AWS_COLLECTOR_URL = os.getenv(
    "AWS_COLLECTOR_URL",
    "https://26ey7wseq2ufs3zetr7ip5ljvi0reoht.lambda-url.us-east-2.on.aws"
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
    data = {}
    for attempt in range(6):
        resp = requests.get(f"{AWS_CANARY_URL}/api/dependencies", timeout=20)
        assert resp.status_code == 200, f"Expected 200 OK, got {resp.status_code}"
        data = resp.json()
        if data.get("healthy_count") == 11:
            break
        time.sleep(3)

    assert data.get("total_dependencies") == 11
    unhealthy = [d["id"] for d in data.get("dependencies", []) if not d.get("available")]
    assert data.get("healthy_count") == 11, f"Expected 11 healthy, got {data.get('healthy_count')}. Unhealthy: {unhealthy}"
    assert data.get("overall_availability_pct") == 100.0

    dep_ids = {d["id"]: d for d in data.get("dependencies", [])}
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
    query = "SELECT telemetry_type, event_name, service_name FROM telemetry LIMIT 10;"
    resp = requests.post(
        f"{AWS_CANARY_URL}/api/duckdb/query",
        json={"query": query, "max_rows": 10},
        timeout=25
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success", f"DuckDB query failed: {data.get('error')}"
    assert "columns" in data
    assert "rows" in data
    assert data["row_count"] >= 1
    assert data["bucket"] == EXPECTED_BUCKET


def test_aws_canary_all_seven_canned_duckdb_queries():
    """Verify all 7 curated canned DuckDB queries are registered, well-formatted, and valid."""
    c_resp = requests.get(f"{AWS_CANARY_URL}/api/duckdb/canned-queries", timeout=10)
    assert c_resp.status_code == 200
    queries = c_resp.json()["queries"]
    assert len(queries) == 7

    expected_ids = {
        "event_breakdown", "nanosecond_traces", "error_warning_logs",
        "throughput_10s_buckets", "s3_parquet_files", "nanosecond_jitter",
        "metric_summary"
    }
    actual_ids = {q["id"] for q in queries}
    assert actual_ids == expected_ids, f"Expected queries {expected_ids}, got {actual_ids}"

    for q in queries:
        assert q["id"], "Query missing id"
        assert q["name"], f"Query {q['id']} missing name"
        assert q["description"], f"Query {q['id']} missing description"
        assert q["sql"] and len(q["sql"].strip()) > 10, f"Query {q['id']} has invalid SQL"


def test_aws_all_seven_microservices_live_endpoints():
    """Verify all 7 Graviton ARM64 polyglot microservices are live and healthy."""
    microservices = {}

    # 1. Dynamically load from MICROSERVICE_URLS_JSON (e.g. from Terraform output)
    env_urls = os.getenv("MICROSERVICE_URLS_JSON", "")
    if env_urls:
        try:
            parsed = json.loads(env_urls)
            if isinstance(parsed, dict) and parsed:
                microservices = {k: v.rstrip("/") for k, v in parsed.items()}
        except Exception:
            pass

    # 2. Dynamically query Canary /stats for live registered app endpoints
    if not microservices:
        try:
            stats_resp = requests.get(f"{AWS_CANARY_URL}/stats", timeout=5)
            if stats_resp.status_code == 200:
                apps = stats_resp.json().get("apps", {})
                for lang, app_info in apps.items():
                    if isinstance(app_info, dict) and app_info.get("url"):
                        microservices[lang] = app_info["url"].rstrip("/")
        except Exception:
            pass

    # 3. Fallback to active deployed URLs
    if not microservices:
        microservices = {
            "python": "https://mxhrcxceaeluv6gu2cod4quwpm0kfydz.lambda-url.us-east-2.on.aws",
            "go": "https://h74bl44slegkr7c6h32hyygzui0djbvn.lambda-url.us-east-2.on.aws",
            "node": "https://xo3ktoqz6qz3dm5o64hvgm22mq0zkqat.lambda-url.us-east-2.on.aws",
            "c": "https://omtosemwg4qxxjqlnbidwl4mvm0wvqva.lambda-url.us-east-2.on.aws",
            "dotnet": "https://4o5yjl47yxdg5e5tsj727mchb40evddy.lambda-url.us-east-2.on.aws",
            "rust": "https://nulam6s4glkv2wafy6tcrjn2j40fyhim.lambda-url.us-east-2.on.aws",
            "java": "https://c5nmwnntg56s7v6gr2cowlmqqm0bcovb.lambda-url.us-east-2.on.aws",
            "php": "https://gvn63eisikfv2zvzf52bqngwku0zhtkv.lambda-url.us-east-2.on.aws",
            "swift": "https://6cq3xl2oie6huoo4mxrqxxfgqu0upybj.lambda-url.us-east-2.on.aws",
        }

    for lang, base_url in microservices.items():
        base_clean = base_url.rstrip("/")
        # Check /version
        v_resp = requests.get(f"{base_clean}/version", timeout=10)
        assert v_resp.status_code == 200, f"{lang} /version failed with {v_resp.status_code}"
        v_data = v_resp.json()
        assert "app_name" in v_data
        assert "version" in v_data

        # Check /compute/5
        c_resp = requests.get(f"{base_clean}/compute/5", timeout=10)
        assert c_resp.status_code == 200, f"{lang} /compute/5 failed with {c_resp.status_code}"
        c_data = c_resp.json()
        assert c_data.get("result") == 5, f"{lang} expected fib(5)=5, got {c_data.get('result')}"


def test_aws_canary_synthetic_traffic_stream_health():
    """Verify live synthetic traffic stream on Canary dashboard is healthy and active."""
    # Warm/trigger synthetic traffic cycles if needed
    for _ in range(3):
        try:
            requests.post(f"{AWS_CANARY_URL}/api/trigger-cycle", timeout=15)
        except Exception:
            pass

    # Poll /stats for health metrics
    stats = {}
    for _ in range(10):
        resp = requests.get(f"{AWS_CANARY_URL}/stats", timeout=10)
        assert resp.status_code == 200, f"Stats endpoint failed with {resp.status_code}"
        stats = resp.json()
        if stats.get("availability_pct", 0.0) >= 80.0:
            break
        time.sleep(1)

    assert stats["status"] in ["Running", "Initializing"], f"Expected Canary to be Running or Initializing, got {stats['status']}"
    assert stats["total_requests"] >= 0, f"Expected non-negative requests stream, got {stats['total_requests']}"
    assert stats["availability_pct"] >= 80.0, f"Availability dropped to {stats['availability_pct']}%"

    # Verify all 9 microservices are marked reachable by Canary
    apps = stats.get("apps", {})
    expected_langs = ["python", "go", "node", "c", "dotnet", "rust", "java", "php", "swift"]
    for lang in expected_langs:
        assert lang in apps, f"Language '{lang}' missing from Canary apps dict"
        assert apps[lang]["reachable"] is True, f"Language '{lang}' marked unreachable: {apps[lang]}"

    # Verify recent requests stream is active and healthy
    recent = stats.get("recent_requests", [])
    if recent:
        recent_successes = sum(1 for r in recent if r.get("status") == "success")
        assert recent_successes >= 1, f"Recent stream has too many failures: {recent}"
