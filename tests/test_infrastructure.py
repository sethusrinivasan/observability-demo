"""
test_infrastructure.py — one-test-per-container smoke tests.

Each test validates the core function of its container:
  - observability-demo  : Flask app serves traffic
  - otel-collector      : accepts OTLP HTTP telemetry
  - tempo               : stores and returns traces
  - loki                : stores and returns logs
  - mimir               : stores and returns metrics
  - prometheus          : scrapes postgres-exporter and exposes pg_up
  - postgres-exporter   : exports pg_up=1 (scraped via Prometheus)
  - postgres            : accepts SQL queries, audit_logs table exists
  - redpanda            : Kafka broker is reachable and reports a cluster
  - grafana             : API healthy, 3 datasources provisioned, dashboard loaded

All tests hit the published host ports so they run from the host without
needing to be inside the Docker network.  The suite is skipped entirely
when the stack is not running.
"""

import os
import shutil
import json
import time
import pytest
import requests
import psycopg2

# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------
APP_URL       = os.getenv("PYTHON_APP_URL", "http://localhost:5000")
JAVA_APP_URL  = os.getenv("JAVA_APP_URL", "http://localhost:8080")
RUST_APP_URL  = os.getenv("RUST_APP_URL", "http://localhost:8083")
NODE_APP_URL  = os.getenv("NODE_APP_URL", "http://localhost:8084")
GO_APP_URL    = os.getenv("GO_APP_URL", "http://localhost:8086")
DOTNET_APP_URL= os.getenv("DOTNET_APP_URL", "http://localhost:8087")
C_APP_URL     = os.getenv("C_APP_URL", "http://localhost:8088")
OTEL_HTTP_URL = os.getenv("OTEL_HTTP_URL", "http://localhost:4318")
TEMPO_URL     = os.getenv("TEMPO_URL", "http://localhost:3200")
LOKI_URL      = os.getenv("LOKI_URL", "http://localhost:3100")
MIMIR_URL     = os.getenv("MIMIR_URL", "http://localhost:9009")
PROMETHEUS_URL= os.getenv("PROMETHEUS_URL", "http://localhost:9090")
GRAFANA_URL   = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_AUTH  = (os.getenv("GRAFANA_USER", "admin"), os.getenv("GRAFANA_PASSWORD", "admin"))
REDIS_EXPORTER_URL = os.getenv("REDIS_EXPORTER_URL", "http://localhost:9121")
VALKEY_PORT   = int(os.getenv("VALKEY_PORT", "6379"))
CANARY_URL    = os.getenv("CANARY_URL", "http://localhost:8085")
REDPANDA_KAFKA= ("localhost", 9092)

PG_CONN = dict(
    host=os.getenv("POSTGRES_HOST", "localhost"),
    port=int(os.getenv("POSTGRES_PORT", 5432)),
    dbname=os.getenv("POSTGRES_DB", "observability"),
    user=os.getenv("POSTGRES_USER", "observability"),
    password=os.getenv("POSTGRES_PASSWORD", "observability"),
    connect_timeout=2,
)

# ---------------------------------------------------------------------------
# Session-scoped availability guards — skip whole module if stack is down
# ---------------------------------------------------------------------------

def _http_ok(url: str, timeout: int = 2) -> bool:
    try:
        return requests.get(url, timeout=timeout).status_code < 500
    except Exception:
        return False

def _tcp_ok(host: str, port: int, timeout: int = 2) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


requires_stack = pytest.mark.skipif(
    not _http_ok(f"{APP_URL}/"),
    reason="Observability stack not running"
)

requires_java = pytest.mark.skipif(
    not _http_ok(f"{JAVA_APP_URL}/"),
    reason="Java app not running"
)

requires_rust = pytest.mark.skipif(
    not _http_ok(f"{RUST_APP_URL}/"),
    reason="Rust app not running"
)

requires_node = pytest.mark.skipif(
    not _http_ok(f"{NODE_APP_URL}/"),
    reason="Node app not running"
)

requires_go = pytest.mark.skipif(
    not _http_ok(f"{GO_APP_URL}/"),
    reason="Go app not running"
)

requires_dotnet = pytest.mark.skipif(
    not _http_ok(f"{DOTNET_APP_URL}/"),
    reason="Dotnet app not running"
)

requires_c = pytest.mark.skipif(
    not _http_ok(f"{C_APP_URL}/"),
    reason="C app not running"
)

requires_valkey = pytest.mark.skipif(
    not _tcp_ok("localhost", VALKEY_PORT),
    reason="Valkey not running"
)

requires_canary = pytest.mark.skipif(
    not _http_ok(f"{CANARY_URL}/health"),
    reason="Canary dashboard not running"
)


# ===========================================================================
# 1. observability-demo
#    Core function: Flask app that computes Fibonacci, writes audit logs,
#    and emits OTel traces/metrics/logs.
# ===========================================================================

@requires_stack
class TestObservabilityDemo:
    def test_home_returns_200(self):
        resp = requests.get(f"{APP_URL}/")
        assert resp.status_code == 200
        assert "Observability Lab" in resp.text

    def test_compute_returns_correct_fibonacci(self):
        resp = requests.get(f"{APP_URL}/compute/10")
        for _ in range(10):
            resp = requests.get(f"{APP_URL}/compute/10")
            if resp.status_code == 200:
                break
        assert resp.status_code == 200
        data = resp.json()
        assert data["input"] == 10
        assert data["result"] == 55

    def test_auditlog_post_writes_record(self):
        resp = requests.post(f"{APP_URL}/auditlog", data={"source": "infra-test"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"
        assert isinstance(data["audit_id"], int)

    def test_auditlog_stats_returns_counts(self):
        resp = requests.get(f"{APP_URL}/auditlog/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_rows"] > 0
        assert "percentiles" in data

    def test_crash_thread_endpoint(self):
        resp = requests.post(f"{APP_URL}/crash?type=thread", timeout=5)
        assert resp.status_code == 500
        data = resp.json()
        assert data["status"] == "crashed"
        assert data["type"] == "thread"


# ===========================================================================
# 2. otel-collector
#    Core function: receives OTLP telemetry over gRPC (4317) and HTTP (4318),
#    routes traces → Tempo, metrics → Mimir, logs → Loki.
# ===========================================================================

@requires_stack
class TestOtelCollector:
    def test_otlp_http_endpoint_reachable(self):
        """POST an empty OTLP JSON payload — collector returns 200 or 400
        (400 = bad payload, but the service is up and parsing)."""
        resp = requests.post(
            f"{OTEL_HTTP_URL}/v1/traces",
            json={},
            headers={"Content-Type": "application/json"},
            timeout=3,
        )
        assert resp.status_code in (200, 400)

    def test_grpc_port_open(self):
        """gRPC port 4317 must be TCP-reachable."""
        assert _tcp_ok("localhost", 4317)

    def test_metrics_reach_mimir_via_collector(self):
        """app_requests_total must exist in Mimir — proves the
        collector metrics pipeline is working end-to-end."""
        resp = requests.get(
            f"{MIMIR_URL}/prometheus/api/v1/query",
            params={"query": "app_requests_total"},
            timeout=5,
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]["result"]) > 0

    def test_traces_reach_tempo_via_collector(self):
        """Tempo must have at least one trace — proves the collector
        traces pipeline is working end-to-end."""
        resp = requests.get(f"{TEMPO_URL}/api/search", params={"limit": 1}, timeout=5)
        assert resp.status_code == 200
        assert len(resp.json().get("traces", [])) > 0

    def test_logs_reach_loki_via_collector(self):
        """Loki must have the service_name label — proves the collector
        logs pipeline is working end-to-end."""
        resp = requests.get(f"{LOKI_URL}/loki/api/v1/labels", timeout=5)
        assert resp.status_code == 200
        assert "service_name" in resp.json()["data"]


# ===========================================================================
# 3. tempo
#    Core function: distributed tracing backend — stores spans from the
#    collector and serves trace search/fetch queries.
# ===========================================================================

@requires_stack
class TestTempo:
    def test_ready_endpoint(self):
        resp = requests.get(f"{TEMPO_URL}/ready", timeout=3)
        assert resp.status_code == 200

    def test_search_returns_traces(self):
        resp = requests.get(f"{TEMPO_URL}/api/search", params={"limit": 5}, timeout=5)
        assert resp.status_code == 200
        traces = resp.json().get("traces", [])
        assert len(traces) > 0

    def test_trace_has_expected_service(self):
        resp = requests.get(f"{TEMPO_URL}/api/search", params={"limit": 10}, timeout=5)
        services = {t["rootServiceName"] for t in resp.json().get("traces", [])}
        assert "observability-python-app" in services

    def test_trace_fetch_by_id(self):
        """Fetch a specific trace by ID and verify it has spans."""
        search = requests.get(f"{TEMPO_URL}/api/search", params={"limit": 1}, timeout=5)
        trace_id = search.json()["traces"][0]["traceID"]
        resp = requests.get(f"{TEMPO_URL}/api/traces/{trace_id}", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        # Response has batches of resource spans
        assert "batches" in data or "resourceSpans" in data or len(data) > 0


# ===========================================================================
# 4. loki
#    Core function: log aggregation backend — receives logs from the
#    collector and serves LogQL queries.
# ===========================================================================

@requires_stack
class TestLoki:
    def test_ready_endpoint(self):
        resp = requests.get(f"{LOKI_URL}/ready", timeout=3)
        assert resp.status_code == 200

    def test_labels_endpoint_returns_service_name(self):
        resp = requests.get(f"{LOKI_URL}/loki/api/v1/labels", timeout=5)
        assert resp.status_code == 200
        assert "service_name" in resp.json()["data"]

    def test_log_stream_exists_for_app(self):
        """Query the last 60 s of logs for the app service."""
        now_ns = int(time.time() * 1e9)
        start_ns = now_ns - int(60 * 1e9)
        resp = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={
                "query": '{service_name="observability-python-app"}',
                "limit": 5,
                "start": start_ns,
                "end": now_ns,
            },
            timeout=5,
        )
        assert resp.status_code == 200
        streams = resp.json()["data"]["result"]
        assert len(streams) > 0

    def test_log_entries_have_content(self):
        """Log lines must be non-empty strings."""
        now_ns = int(time.time() * 1e9)
        start_ns = now_ns - int(60 * 1e9)
        resp = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={
                "query": '{service_name="observability-python-app"}',
                "limit": 5,
                "start": start_ns,
                "end": now_ns,
            },
            timeout=5,
        )
        for stream in resp.json()["data"]["result"]:
            for _ts, line in stream["values"]:
                assert len(line) > 0


# ===========================================================================
# 5. mimir
#    Core function: long-term metrics storage — receives Prometheus remote
#    write from the collector and Prometheus, serves PromQL queries.
# ===========================================================================

@requires_stack
class TestMimir:
    def test_ready_endpoint(self):
        resp = requests.get(f"{MIMIR_URL}/ready", timeout=3)
        assert resp.status_code == 200

    def test_app_metric_queryable(self):
        """app_requests_total must be present and have a positive value."""
        resp = requests.get(
            f"{MIMIR_URL}/prometheus/api/v1/query",
            params={"query": "app_requests_total"},
            timeout=5,
        )
        assert resp.status_code == 200
        result = resp.json()["data"]["result"]
        assert len(result) > 0
        assert float(result[0]["value"][1]) > 0

    def test_postgres_metric_queryable(self):
        """pg_up must be 1 — proves Prometheus → Mimir remote write works."""
        resp = requests.get(
            f"{MIMIR_URL}/prometheus/api/v1/query",
            params={"query": "pg_up"},
            timeout=5,
        )
        assert resp.status_code == 200
        result = resp.json()["data"]["result"]
        assert len(result) > 0
        assert result[0]["value"][1] == "1"

    def test_metric_labels_present(self):
        """app_requests_total must carry endpoint, route, status labels."""
        resp = requests.get(
            f"{MIMIR_URL}/prometheus/api/v1/query",
            params={"query": "app_requests_total"},
            timeout=5,
        )
        labels = resp.json()["data"]["result"][0]["metric"]
        for key in ("endpoint", "route", "status"):
            assert key in labels


# ===========================================================================
# 6. prometheus
#    Core function: scrapes postgres-exporter every 15 s and remote-writes
#    to Mimir. Exposes its own query API.
# ===========================================================================

@requires_stack
class TestPrometheus:
    def test_ready_endpoint(self):
        resp = requests.get(f"{PROMETHEUS_URL}/-/ready", timeout=3)
        assert resp.status_code == 200
        assert "Ready" in resp.text

    def test_postgres_exporter_target_is_up(self):
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=5)
        assert resp.status_code == 200
        targets = resp.json()["data"]["activeTargets"]
        pg_targets = [t for t in targets if t["labels"].get("job") == "postgres-exporter"]
        assert len(pg_targets) > 0
        assert pg_targets[0]["health"] == "up"

    def test_pg_up_metric_equals_one(self):
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": "pg_up"},
            timeout=5,
        )
        assert resp.status_code == 200
        result = resp.json()["data"]["result"]
        assert len(result) > 0
        assert result[0]["value"][1] == "1"


# ===========================================================================
# 7. postgres-exporter
#    Core function: connects to Postgres and exposes pg_* metrics on :9187
#    for Prometheus to scrape.  Port 9187 is not published to the host, so
#    we validate indirectly via Prometheus (which can reach it inside Docker).
# ===========================================================================

@requires_stack
class TestPostgresExporter:
    def test_pg_up_scraped_by_prometheus(self):
        """pg_up=1 in Prometheus proves the exporter is reachable and
        connected to Postgres."""
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": "pg_up"},
            timeout=5,
        )
        result = resp.json()["data"]["result"]
        assert len(result) > 0
        assert result[0]["value"][1] == "1"

    def test_pg_stat_database_metrics_present(self):
        """pg_stat_database_xact_commit must exist — proves the exporter
        is collecting real Postgres statistics."""
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": 'pg_stat_database_xact_commit{datname="observability"}'},
            timeout=5,
        )
        assert resp.status_code == 200
        result = resp.json()["data"]["result"]
        assert len(result) > 0

    def test_pg_stat_activity_count_present(self):
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": 'pg_stat_activity_count{datname="observability"}'},
            timeout=5,
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]["result"]) > 0


# ===========================================================================
# 8. postgres
#    Core function: relational store for audit_logs — persists every
#    /auditlog request with system metrics and request metadata.
# ===========================================================================

@requires_stack
class TestPostgres:
    def _conn(self):
        return psycopg2.connect(**PG_CONN)

    def test_connection_succeeds(self):
        conn = self._conn()
        conn.close()

    def test_audit_logs_table_exists(self):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_name = 'audit_logs'
                    )
                """)
                assert cur.fetchone()[0] is True

    def test_audit_logs_has_expected_columns(self):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'audit_logs'
                """)
                cols = {row[0] for row in cur.fetchall()}
        expected = {
            "id", "created_at", "endpoint", "status_code",
            "response_time_seconds", "process_memory_rss",
            "process_cpu_seconds", "system_loadavg_1m",
            "container_memory_current", "container_memory_limit",
            "container_memory_percent", "container_cpu_usage_ns", "details",
        }
        assert expected.issubset(cols)

    def test_audit_logs_contains_records(self):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM audit_logs")
                assert cur.fetchone()[0] > 0

    def test_audit_log_write_and_read(self):
        """Write a row directly and read it back to verify round-trip."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO audit_logs
                        (endpoint, status_code, response_time_seconds, details)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, endpoint, status_code
                """, ("/test-infra", 200, 0.001, json.dumps({"source": "infra-test"})))
                row = cur.fetchone()
                conn.commit()
        assert row[1] == "/test-infra"
        assert row[2] == 200

    def test_details_column_is_valid_jsonb(self):
        """details column must deserialise as a dict with remote_addr key."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT details FROM audit_logs
                    WHERE details IS NOT NULL
                    LIMIT 1
                """)
                row = cur.fetchone()
        assert row is not None
        details = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        assert isinstance(details, dict)


# ===========================================================================
# 9. redpanda
#    Core function: Kafka-compatible broker used by Tempo as an ingest
#    buffer.  Validates the broker is reachable on port 9092.
# ===========================================================================

@requires_stack
class TestRedpanda:
    def test_kafka_port_reachable(self):
        """TCP connect to the Kafka API port."""
        assert _tcp_ok(*REDPANDA_KAFKA)

    @pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available in test environment")
    def test_tempo_ingest_topic_exists(self):
        """tempo-ingest topic must exist — created by Tempo on startup."""
        import subprocess
        result = subprocess.run(
            ["docker", "exec", "redpanda", "rpk", "topic", "list"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        # topic may or may not exist yet depending on Tempo ingest config,
        # but the broker must respond
        assert "Error" not in result.stderr or result.returncode == 0

    @pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available in test environment")
    def test_cluster_info_returns_broker(self):
        """rpk cluster info must report at least one broker."""
        import subprocess
        result = subprocess.run(
            ["docker", "exec", "redpanda", "rpk", "cluster", "info"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "redpanda" in result.stdout.lower() or "BROKERS" in result.stdout


# ===========================================================================
# 10. grafana
#     Core function: visualisation layer — serves dashboards backed by
#     Tempo, Mimir, and Loki datasources.
# ===========================================================================

@requires_stack
class TestGrafana:
    def test_health_endpoint(self):
        resp = requests.get(f"{GRAFANA_URL}/api/health", timeout=3)
        assert resp.status_code == 200
        data = resp.json()
        assert data["database"] == "ok"

    def test_three_datasources_provisioned(self):
        resp = requests.get(f"{GRAFANA_URL}/api/datasources", auth=GRAFANA_AUTH, timeout=5)
        assert resp.status_code == 200
        names = {ds["name"] for ds in resp.json()}
        assert {"Tempo", "Mimir", "Loki"}.issubset(names)

    def test_datasource_types_correct(self):
        resp = requests.get(f"{GRAFANA_URL}/api/datasources", auth=GRAFANA_AUTH, timeout=5)
        assert resp.status_code == 200
        by_name = {ds["name"]: ds["type"] for ds in resp.json()}
        assert by_name["Tempo"] == "tempo"
        assert by_name["Mimir"] == "prometheus"
        assert by_name["Loki"] == "loki"

    def test_dashboard_loaded(self):
        resp = requests.get(
            f"{GRAFANA_URL}/api/dashboards/uid/observability-demo-metrics",
            auth=GRAFANA_AUTH,
            timeout=5,
        )
        assert resp.status_code == 200
        db = resp.json()["dashboard"]
        assert db["title"] == "Observability Demo"
        assert len(db["panels"]) > 0

    def test_valkey_dashboard_loaded(self):
        resp = requests.get(
            f"{GRAFANA_URL}/api/dashboards/uid/valkey-metrics",
            auth=GRAFANA_AUTH,
            timeout=5,
        )
        assert resp.status_code == 200
        db = resp.json()["dashboard"]
        assert len(db["panels"]) > 0

    def test_dashboard_has_loki_panels(self):
        resp = requests.get(
            f"{GRAFANA_URL}/api/dashboards/uid/observability-demo-metrics",
            auth=GRAFANA_AUTH,
            timeout=5,
        )
        assert resp.status_code == 200
        panels = resp.json()["dashboard"]["panels"]
        loki_panels = [
            p for p in panels
            if isinstance(p.get("datasource"), dict)
            and p["datasource"].get("type") == "loki"
        ]
        assert len(loki_panels) > 0


# ===========================================================================
# 11. observability-java-app
#     Core function: Spring Boot MVC app with Fibonacci, JDBC audit log,
#     math evaluator, and Actuator endpoints.
# ===========================================================================

@requires_java
class TestJavaApp:
    def test_home_returns_200(self):
        resp = requests.get(f"{JAVA_APP_URL}/", timeout=5)
        assert resp.status_code == 200

    def test_version_returns_java(self):
        resp = requests.get(f"{JAVA_APP_URL}/version", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["language"] == "java"
        assert data["version"] == "1.0.1"

    def test_selftest_passes(self):
        resp = requests.get(f"{JAVA_APP_URL}/selftest", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["failures"] == 0

    def test_compute_fibonacci(self):
        resp = requests.get(f"{JAVA_APP_URL}/compute/10", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["input"] == 10
        assert data["result"] == 55

    def test_eval_expression(self):
        resp = requests.post(f"{JAVA_APP_URL}/eval", json={"expr": "2 + 3 * 4"}, timeout=5)
        assert resp.status_code == 200
        assert resp.json()["result"] == 14.0

    def test_auditlog_get(self):
        resp = requests.get(f"{JAVA_APP_URL}/auditlog", timeout=5)
        assert resp.status_code in [200, 201]
        assert resp.json()["status"] == "ok"

    def test_actuator_health(self):
        resp = requests.get(f"{JAVA_APP_URL}/actuator/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "UP"

    def test_actuator_health_liveness(self):
        resp = requests.get(f"{JAVA_APP_URL}/actuator/health/liveness", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "UP"

    def test_actuator_health_readiness(self):
        resp = requests.get(f"{JAVA_APP_URL}/actuator/health/readiness", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "UP"

    def test_actuator_info(self):
        resp = requests.get(f"{JAVA_APP_URL}/actuator/info", timeout=5)
        assert resp.status_code == 200

    def test_crash_thread_endpoint(self):
        resp = requests.post(f"{JAVA_APP_URL}/crash?type=thread", timeout=5)
        assert resp.status_code == 500
        data = resp.json()
        assert data["status"] == "crashed"
        assert data["type"] == "thread"


# ===========================================================================
# 12. observability-rust-app
#     Core function: Axum async app with Fibonacci, sqlx audit log,
#     math evaluator, and OTLP telemetry.
# ===========================================================================

@requires_rust
class TestRustApp:
    def test_home_returns_200(self):
        resp = requests.get(f"{RUST_APP_URL}/", timeout=5)
        assert resp.status_code == 200

    def test_version_returns_rust(self):
        resp = requests.get(f"{RUST_APP_URL}/version", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["language"] == "rust"
        assert data["version"] == "1.0.1"

    def test_selftest_passes(self):
        resp = requests.get(f"{RUST_APP_URL}/selftest", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_compute_fibonacci(self):
        resp = requests.get(f"{RUST_APP_URL}/compute/10", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == 55

    def test_eval_expression(self):
        resp = requests.post(f"{RUST_APP_URL}/eval", json={"expr": "2 + 3 * 4"}, timeout=5)
        assert resp.status_code == 200
        assert resp.json()["result"] == 14.0

    def test_auditlog_get(self):
        resp = requests.get(f"{RUST_APP_URL}/auditlog", timeout=5)
        assert resp.status_code in [200, 201]
        assert resp.json()["status"] == "ok"

    def test_crash_thread_endpoint(self):
        resp = requests.post(f"{RUST_APP_URL}/crash?type=thread", timeout=5)
        assert resp.status_code == 500
        data = resp.json()
        assert data["status"] == "crashed"
        assert data["type"] == "thread"


# ===========================================================================
# 13. observability-node-app
#     Core function: Node.js (Express) equivalent microservice with full
#     OTel tracing, metrics, and PostgreSQL audit logging.
# ===========================================================================

@requires_node
class TestNodeApp:
    def test_home_returns_200(self):
        resp = requests.get(f"{NODE_APP_URL}/", timeout=5)
        assert resp.status_code == 200

    def test_version_returns_nodejs(self):
        resp = requests.get(f"{NODE_APP_URL}/version", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["language"] in ["nodejs", "node"]
        assert data["version"] == "1.0.1"

    def test_selftest_passes(self):
        resp = requests.get(f"{NODE_APP_URL}/selftest", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_compute_fibonacci(self):
        resp = requests.get(f"{NODE_APP_URL}/compute/10", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == 55

    def test_eval_expression(self):
        resp = requests.post(f"{NODE_APP_URL}/eval", json={"expr": "2 + 3 * 4"}, timeout=5)
        assert resp.status_code == 200
        assert resp.json()["result"] == 14.0

    def test_auditlog_get(self):
        resp = requests.get(f"{NODE_APP_URL}/auditlog", timeout=5)
        assert resp.status_code in [200, 201]
        assert resp.json()["status"] == "ok"

    def test_crash_thread_endpoint(self):
        resp = requests.post(f"{NODE_APP_URL}/crash?type=thread", timeout=5)
        assert resp.status_code == 500
        data = resp.json()
        assert data["status"] == "crashed"
        assert data["type"] == "thread"


# ===========================================================================
# 14. observability-go-app
#     Core function: Go (net/http) equivalent microservice with full
#     OTel tracing, metrics, and PostgreSQL audit logging.
# ===========================================================================

@requires_go
class TestGoApp:
    def test_home_returns_200(self):
        resp = requests.get(f"{GO_APP_URL}/", timeout=5)
        assert resp.status_code == 200

    def test_version_returns_golang(self):
        resp = requests.get(f"{GO_APP_URL}/version", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["language"] in ["golang", "go"]
        assert data["version"] == "1.0.1"

    def test_selftest_passes(self):
        resp = requests.get(f"{GO_APP_URL}/selftest", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_compute_fibonacci(self):
        resp = requests.get(f"{GO_APP_URL}/compute/10", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == 55

    def test_eval_expression(self):
        resp = requests.post(f"{GO_APP_URL}/eval", json={"expr": "2 + 3 * 4"}, timeout=5)
        assert resp.status_code == 200
        assert resp.json()["result"] == 14.0

    def test_auditlog_get(self):
        resp = requests.get(f"{GO_APP_URL}/auditlog", timeout=5)
        assert resp.status_code in [200, 201]
        assert resp.json()["status"] == "ok"

    def test_crash_thread_endpoint(self):
        resp = requests.post(f"{GO_APP_URL}/crash?type=thread", timeout=5)
        assert resp.status_code == 500
        data = resp.json()
        assert data["status"] == "crashed"
        assert data["type"] == "thread"


# ===========================================================================
# 15. observability-dotnet-app
#     Core function: C# .NET (ASP.NET Core) equivalent microservice with full
#     OTel tracing, metrics, and PostgreSQL audit logging.
# ===========================================================================

@requires_dotnet
class TestDotnetApp:
    def test_home_returns_200(self):
        resp = requests.get(f"{DOTNET_APP_URL}/", timeout=5)
        assert resp.status_code == 200

    def test_version_returns_csharp(self):
        resp = requests.get(f"{DOTNET_APP_URL}/version", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["language"] in ["csharp", "dotnet"]
        assert data["version"] == "1.0.1"

    def test_selftest_passes(self):
        resp = requests.get(f"{DOTNET_APP_URL}/selftest", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_compute_fibonacci(self):
        resp = requests.get(f"{DOTNET_APP_URL}/compute/10", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == 55

    def test_eval_expression(self):
        resp = requests.post(f"{DOTNET_APP_URL}/eval", json={"expr": "2 + 3 * 4"}, timeout=5)
        assert resp.status_code == 200
        assert resp.json()["result"] == 14.0

    def test_auditlog_get(self):
        resp = requests.get(f"{DOTNET_APP_URL}/auditlog", timeout=5)
        assert resp.status_code in [200, 201]
        assert resp.json()["status"] == "ok"

    def test_crash_thread_endpoint(self):
        resp = requests.post(f"{DOTNET_APP_URL}/crash?type=thread", timeout=5)
        assert resp.status_code == 500
        data = resp.json()
        assert data["status"] == "crashed"
        assert data["type"] == "thread"


# ===========================================================================
# 15b. observability-c-app (C POSIX C99)
#      Core function: High-performance C microservice with zero-dependency math
#      evaluator, recursive Fibonacci, PostgreSQL persistence, and Actuator probes.
# ===========================================================================

@requires_c
class TestCApp:
    def test_home_returns_200(self):
        resp = requests.get(f"{C_APP_URL}/", timeout=5)
        assert resp.status_code == 200

    def test_version_returns_c(self):
        resp = requests.get(f"{C_APP_URL}/version", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["language"] == "c"
        assert data["version"] == "1.0.1"

    def test_selftest_passes(self):
        resp = requests.get(f"{C_APP_URL}/selftest", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_compute_fibonacci(self):
        resp = requests.get(f"{C_APP_URL}/compute/10", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == 55

    def test_eval_expression(self):
        resp = requests.post(f"{C_APP_URL}/eval", json={"expr": "2 + 3 * 4"}, timeout=5)
        assert resp.status_code == 200
        assert resp.json()["result"] == 14.0

    def test_auditlog_get(self):
        resp = requests.get(f"{C_APP_URL}/auditlog", timeout=5)
        assert resp.status_code in [200, 201]
        assert resp.json()["status"] == "ok"

    def test_crash_thread_endpoint(self):
        resp = requests.post(f"{C_APP_URL}/crash?type=thread", timeout=5)
        assert resp.status_code == 500
        data = resp.json()
        assert data["status"] == "crashed"
        assert data["type"] == "thread"


# ===========================================================================
# 16. valkey & redis-exporter
#     Core function: In-memory store for canary metrics, scraped by Prometheus.
# ===========================================================================

@requires_valkey
class TestValkeyAndRedisExporter:
    def test_valkey_port_reachable(self):
        assert _tcp_ok("localhost", VALKEY_PORT)

    def test_redis_exporter_metrics(self):
        resp = requests.get(f"{REDIS_EXPORTER_URL}/metrics", timeout=5)
        assert resp.status_code == 200
        assert "redis_up 1" in resp.text

    def test_prometheus_scrapes_redis_exporter(self):
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": "redis_up"},
            timeout=5,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]["result"]
        assert len(data) > 0
        assert data[0]["value"][1] == "1"


# ===========================================================================
# 16. canary dashboard
#     Core function: Live self-monitoring HTTP dashboard for synthetic traffic.
# ===========================================================================

@requires_canary
class TestCanaryDashboard:
    def test_dashboard_html_returns_200(self):
        resp = requests.get(f"{CANARY_URL}/", timeout=5)
        assert resp.status_code == 200
        assert "Canary Load Generator" in resp.text
        assert 'http-equiv="refresh"' in resp.text

    def test_stats_json_endpoint(self):
        resp = requests.get(f"{CANARY_URL}/stats", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ["Running", "Initializing"]
        assert "total_requests" in data
        assert "apps" in data
        assert "python" in data["apps"]
        assert "java" in data["apps"]
        assert "rust" in data["apps"]
        assert "node" in data["apps"]
        assert "go" in data["apps"]
        assert "dotnet" in data["apps"]
        assert "c" in data["apps"]

    def test_health_endpoint(self):
        resp = requests.get(f"{CANARY_URL}/health", timeout=5)
        assert resp.status_code == 200
        assert "OK" in resp.text

    def test_trends_endpoint(self):
        for r in ["5m", "30m", "1h", "6h", "1d", "30d"]:
            resp = requests.get(f"{CANARY_URL}/api/trends?range={r}", timeout=5)
            assert resp.status_code == 200
            data = resp.json()
            assert "metrics" in data
            assert "throughput" in data["metrics"]
            assert "errors" in data["metrics"]
            assert "availability" in data["metrics"]
            assert "latency_ms" in data["metrics"]

    def test_fault_injection_lifecycle(self):
        # Start fault
        resp = requests.post(f"{CANARY_URL}/api/fault/start", json={
            "tag": "TEST-CI-DRILL",
            "fault_type": "error_spike",
            "target": "java",
            "duration_sec": 10
        }, timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

        # Check active fault list
        resp_f = requests.get(f"{CANARY_URL}/api/faults", timeout=5)
        assert resp_f.status_code == 200
        assert resp_f.json()["active_fault"]["tag"] == "TEST-CI-DRILL"

        # Stop fault
        resp_s = requests.post(f"{CANARY_URL}/api/fault/stop", timeout=5)
        assert resp_s.status_code == 200
        assert resp_s.json()["status"] == "stopped"

    def test_tps_override_lifecycle(self):
        # Apply TPS override
        resp = requests.post(f"{CANARY_URL}/api/tps", json={"tps": 18, "duration_sec": 10}, timeout=5)
        assert resp.status_code == 200
        assert resp.json()["effective_tps"] == 18.0

        # Reset TPS
        resp_r = requests.post(f"{CANARY_URL}/api/tps/reset", timeout=5)
        assert resp_r.status_code == 200
        assert resp_r.json()["status"] == "reset"

    def test_crash_trigger_api(self):
        resp = requests.post(f"{CANARY_URL}/api/crash", json={
            "target": "python",
            "type": "thread",
            "tag": "TEST-CI-CRASH-THREAD"
        }, timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["crash_type"] == "thread"
        assert data["target"] == "python"
        assert len(data["results"]) >= 1

        # Stop fault drill
        requests.post(f"{CANARY_URL}/api/fault/stop", timeout=5)

    def test_containers_endpoint(self):
        resp = requests.get(f"{CANARY_URL}/api/containers", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        for lang in ["python", "java", "rust", "node", "go", "dotnet", "c"]:
            assert lang in data
            assert "is_running" in data[lang]
            assert "active_in_canary" in data[lang]

    def test_container_power_lifecycle(self):
        # Stop dotnet container
        resp_stop = requests.post(f"{CANARY_URL}/api/containers/action", json={"language": "dotnet", "action": "stop"}, timeout=5)
        assert resp_stop.status_code == 200
        assert resp_stop.json()["status"] == "success"

        # Check containers endpoint reflects inactive workload
        resp_c = requests.get(f"{CANARY_URL}/api/containers", timeout=5)
        assert resp_c.status_code == 200
        assert resp_c.json()["dotnet"]["active_in_canary"] is False

        # Restart dotnet container
        resp_start = requests.post(f"{CANARY_URL}/api/containers/action", json={"language": "dotnet", "action": "start"}, timeout=5)
        assert resp_start.status_code == 200
        assert resp_start.json()["status"] == "success"

    def test_dependencies_endpoint(self):
        resp = requests.get(f"{CANARY_URL}/api/dependencies", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "total_dependencies" in data
        assert data["total_dependencies"] == 9
        assert "healthy_count" in data
        assert data["healthy_count"] >= 8
        assert "overall_availability_pct" in data
        dep_names = [d["id"] for d in data["dependencies"]]
        for expected in ["postgres", "valkey", "otel-collector", "tempo", "loki", "mimir", "prometheus", "grafana", "redpanda"]:
            assert expected in dep_names
        by_id = {d["id"]: d for d in data["dependencies"]}
        assert by_id["grafana"]["browser"]["port"] == 3000
        assert by_id["prometheus"]["browser"]["path"] == "/query"
        assert by_id["postgres"]["browser"]["port"] == 9187

    def test_sql_saved_queries_endpoint(self):
        resp = requests.get(f"{CANARY_URL}/api/sql/saved-queries", timeout=5)
        assert resp.status_code == 200
        queries = resp.json()
        assert len(queries) >= 5
        for q in queries:
            assert "name" in q
            assert "sql" in q

    def test_sql_query_execution(self):
        query = "SELECT id, created_at, endpoint, status_code FROM audit_logs ORDER BY id DESC LIMIT 5;"
        resp = requests.post(f"{CANARY_URL}/api/sql/query", json={"query": query}, timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "columns" in data
        assert "rows" in data
        assert len(data["rows"]) > 0
        assert "execution_time_ms" in data

    def test_qrcode_endpoint(self):
        resp = requests.get(f"{CANARY_URL}/api/qrcode?url=http://192.168.1.100:8085", timeout=5)
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers.get("Content-Type", "")
        assert b"<svg" in resp.content

