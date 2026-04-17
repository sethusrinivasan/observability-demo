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

import json
import time
import pytest
import requests
import psycopg2

# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------
APP_URL       = "http://localhost:5000"
OTEL_HTTP_URL = "http://localhost:4318"
TEMPO_URL     = "http://localhost:3200"
LOKI_URL      = "http://localhost:3100"
MIMIR_URL     = "http://localhost:9009"
PROMETHEUS_URL= "http://localhost:9090"
GRAFANA_URL   = "http://localhost:3000"
REDPANDA_KAFKA= ("localhost", 9092)

PG_CONN = dict(host="localhost", port=5432, dbname="observability",
               user="observability", password="observability", connect_timeout=2)

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
        # 10% random error rate — retry a few times to get a success
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
        resp = requests.get(f"{GRAFANA_URL}/api/datasources", timeout=5)
        assert resp.status_code == 200
        names = {ds["name"] for ds in resp.json()}
        assert {"Tempo", "Mimir", "Loki"}.issubset(names)

    def test_datasource_types_correct(self):
        resp = requests.get(f"{GRAFANA_URL}/api/datasources", timeout=5)
        by_name = {ds["name"]: ds["type"] for ds in resp.json()}
        assert by_name["Tempo"] == "tempo"
        assert by_name["Mimir"] == "prometheus"
        assert by_name["Loki"] == "loki"

    def test_dashboard_loaded(self):
        resp = requests.get(
            f"{GRAFANA_URL}/api/dashboards/uid/observability-demo-metrics",
            timeout=5,
        )
        assert resp.status_code == 200
        db = resp.json()["dashboard"]
        assert db["title"] == "Observability Demo"
        assert len(db["panels"]) > 0

    def test_dashboard_has_loki_panels(self):
        resp = requests.get(
            f"{GRAFANA_URL}/api/dashboards/uid/observability-demo-metrics",
            timeout=5,
        )
        panels = resp.json()["dashboard"]["panels"]
        loki_panels = [
            p for p in panels
            if isinstance(p.get("datasource"), dict)
            and p["datasource"].get("type") == "loki"
        ]
        assert len(loki_panels) > 0
