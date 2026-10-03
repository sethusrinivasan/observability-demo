"""
canary.py — Standalone synthetic canary for the observability-demo stack.
Generates synthetic traffic across Python, Java, Rust, Node, Go, .NET, and C microservices,
exports OpenTelemetry telemetry, logs to Valkey, supports Fault Injection Testing
with language-specific color-coded event tagging, dynamic TPS override controls,
container lifecycle/power management, dependency health telemetry, an SSMS/pgAdmin-style
SQL query editor, and a compact, high-efficiency dashboard with collapsible controls.
"""

from collections import deque
from datetime import datetime
import hashlib
import hmac
import html
import re
import secrets
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
import random
import socket
import sys
import threading
import time
import urllib.parse

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

try:
    import qrcode
    import qrcode.image.svg
except ImportError:
    qrcode = None

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
import redis
import requests

from sql_guard import SqlGuardError, guard_readonly_sql

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------
APP_BASE_URL         = os.getenv("APP_BASE_URL",        "http://observability-python-app:5000")
JAVA_APP_BASE_URL    = os.getenv("JAVA_APP_BASE_URL",   "http://observability-java-app:8080")
RUST_APP_BASE_URL    = os.getenv("RUST_APP_BASE_URL",   "http://observability-rust-app:8081")
NODE_APP_BASE_URL    = os.getenv("NODE_APP_BASE_URL",   "http://observability-node-app:8080")
GO_APP_BASE_URL      = os.getenv("GO_APP_BASE_URL",     "http://observability-go-app:8080")
DOTNET_APP_BASE_URL  = os.getenv("DOTNET_APP_BASE_URL", "http://observability-dotnet-app:8080")
C_APP_BASE_URL       = os.getenv("C_APP_BASE_URL",      "http://observability-c-app:8080")
OTLP_ENDPOINT        = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
MIMIR_URL            = os.getenv("MIMIR_URL",           "http://mimir:9009")
VALKEY_HOST          = os.getenv("VALKEY_HOST",         "valkey")
CANARY_TPS           = float(os.getenv("CANARY_TPS",    "6"))
CANARY_PORT          = int(os.getenv("CANARY_PORT",     "8085"))
REFRESH_INTERVAL_SEC = int(os.getenv("CANARY_REFRESH_INTERVAL", "3"))

POSTGRES_HOST        = os.getenv("POSTGRES_HOST",       "postgres")
POSTGRES_PORT        = int(os.getenv("POSTGRES_PORT",   "5432"))
POSTGRES_DB          = os.getenv("POSTGRES_DB",         "observability")
POSTGRES_USER        = os.getenv("POSTGRES_USER",       "observability")
POSTGRES_PASSWORD    = os.getenv("POSTGRES_PASSWORD",   "observability")
DOCKER_SOCKET_PATH   = os.getenv("DOCKER_SOCKET_PATH",  "/var/run/docker.sock")
CANARY_DEMO_USER     = os.getenv("CANARY_DEMO_USER",     "demouser")
CANARY_DEMO_PASSWORD = os.getenv("CANARY_DEMO_PASSWORD", "demo")
TEMPO_URL            = os.getenv("TEMPO_URL",           "http://tempo:3200")
LOKI_URL             = os.getenv("LOKI_URL",            "http://loki:3100")
PROMETHEUS_URL       = os.getenv("PROMETHEUS_URL",      "http://prometheus:9090")
GRAFANA_URL          = os.getenv("GRAFANA_URL",         "http://grafana:3000")
REDPANDA_ADMIN_URL   = os.getenv("REDPANDA_ADMIN_URL",  "http://redpanda:9644")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("canary")

# ---------------------------------------------------------------------------
# OTel metrics setup
# ---------------------------------------------------------------------------
resource = Resource(attributes={
    SERVICE_NAME: "observability-canary",
    DEPLOYMENT_ENVIRONMENT: "local-dev",
})

reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    export_interval_millis=5000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("canary")

request_counter         = meter.create_counter(
    "app.synthetic.requests.total",
    description="Total canary requests")
request_success_counter = meter.create_counter(
    "app.synthetic.requests.success",
    description="Successful canary requests")
request_error_counter   = meter.create_counter(
    "app.synthetic.requests.errors",
    description="Failed canary requests")
request_duration        = meter.create_histogram(
    "app.synthetic.request.duration",
    description="Canary request duration in seconds")
schedule_behind_counter = meter.create_counter(
    "app.synthetic.schedule.behind",
    description="Loops where a slow request pushed the canary behind its target rate")

TARGETS = [
    {"path": "/",                                          "method": "GET"},
    {"path": "/compute/5",                                 "method": "GET"},
    {"path": "/compute/10",                                "method": "GET"},
    {"path": "/compute/20",                                "method": "GET"},
    {"path": "/auditlog",                                  "method": "GET"},
    {"path": "/auditlog",                                  "method": "POST",
     "data": {"source": "canary"}},
    {"path": "/auditlog/stats",                            "method": "GET"},
    {"path": "/eval?expr=3%2B4",                          "method": "GET"},
    {"path": "/eval?expr=10-3",                           "method": "GET"},
    {"path": "/eval?expr=6*7",                            "method": "GET"},
    {"path": "/eval?expr=22%2F4",                         "method": "GET"},
    {"path": "/eval?expr=2%5E10",                         "method": "GET"},
    {"path": "/eval?expr=(2%2B3)*4",                      "method": "GET"},
    {"path": "/eval?expr=((3%2B4)*6%5E2%2F5)%2B1-7",     "method": "GET"},
    {"path": "/eval?expr=-5%2B8",                         "method": "GET"},
    {"path": "/eval?expr=2%5E3%5E2",                      "method": "GET"},
    {"path": "/eval",                                      "method": "POST",
     "json": {"expr": "(10+5)*2^2-3"}},
    {"path": "/eval?expr=5%2F0",                          "method": "GET",
     "expected_4xx": True},
    {"path": "/eval?expr=3%244",                          "method": "GET",
     "expected_4xx": True},
    {"path": "/eval",                                     "method": "GET",
     "expected_4xx": True},
]

# ---------------------------------------------------------------------------
# Unix Domain Socket HTTP Connection for Docker Engine API
# ---------------------------------------------------------------------------
class UnixHTTPConnection(http.client.HTTPConnection):
    """Connects to the Docker daemon over /var/run/docker.sock"""
    def __init__(self, socket_path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)

class DockerManager:
    """Manages container lifecycle via Docker Engine API over /var/run/docker.sock"""
    CONTAINER_MAP = {
        "python": "observability-python-app",
        "java":   "observability-java-app",
        "rust":   "observability-rust-app",
        "node":   "observability-node-app",
        "go":     "observability-go-app",
        "dotnet": "observability-dotnet-app",
        "c":      "observability-c-app",
    }

    def __init__(self, socket_path: str = DOCKER_SOCKET_PATH):
        self.socket_path = socket_path

    def _request(self, method: str, path: str, body: bytes = b"") -> tuple[int, dict | list | str]:
        if not os.path.exists(self.socket_path):
            return 503, {"error": f"Docker socket {self.socket_path} not found"}
        conn = None
        try:
            conn = UnixHTTPConnection(self.socket_path, timeout=5.0)
            headers = {"Host": "localhost"}
            if body:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            data = {}
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    data = raw.decode("utf-8", errors="ignore")
            return resp.status, data
        except Exception as exc:
            return 500, {"error": str(exc)}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def container_started_at(self, container_id: str) -> float | None:
        """Unix time the container process last started, from the Docker API."""
        if not container_id or container_id in ("unknown", "not_found"):
            return None
        code, data = self._request("GET", f"/containers/{container_id}/json")
        if code != 200 or not isinstance(data, dict):
            return None
        raw = ((data.get("State") or {}).get("StartedAt") or "")
        if not raw or raw.startswith("0001"):
            return None
        match = re.match(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?",
            raw,
        )
        if not match:
            return None
        frac = (match.group(2) or "0")[:6].ljust(6, "0")
        tz = match.group(3) or "Z"
        if tz == "Z":
            tz = "+00:00"
        try:
            parsed = datetime.fromisoformat(f"{match.group(1)}.{frac}{tz}")
            return parsed.timestamp()
        except Exception:
            return None

    def get_containers_status(self) -> dict:
        status_code, data = self._request("GET", "/containers/json?all=1")
        if status_code != 200 or not isinstance(data, list):
            # Fallback if docker socket not available
            return {
                lang: {
                    "id": "unknown",
                    "name": name,
                    "state": "running" if state.is_app_active(lang) else "stopped",
                    "status": "Running (Local State)" if state.is_app_active(lang) else "Stopped",
                    "is_running": state.is_app_active(lang),
                    "active_in_canary": state.is_app_active(lang),
                }
                for lang, name in self.CONTAINER_MAP.items()
            }

        found_containers = {}
        for c in data:
            for n in c.get("Names", []):
                clean_n = n.lstrip("/")
                found_containers[clean_n] = c

        res = {}
        for lang, container_name in self.CONTAINER_MAP.items():
            c = found_containers.get(container_name)
            if c:
                st = c.get("State", "unknown")
                is_run = (st == "running")
                res[lang] = {
                    "id": c.get("Id", "")[:12],
                    "name": container_name,
                    "state": st,
                    "status": c.get("Status", ""),
                    "is_running": is_run,
                    "active_in_canary": state.is_app_active(lang)
                }
            else:
                res[lang] = {
                    "id": "not_found",
                    "name": container_name,
                    "state": "not_found",
                    "status": "Container Not Found",
                    "is_running": False,
                    "active_in_canary": False
                }
        return res

    def container_action(self, lang: str, action: str) -> dict:
        container_name = self.CONTAINER_MAP.get(lang.lower())
        if not container_name:
            return {"status": "error", "message": f"Unknown language: {lang}"}

        statuses = self.get_containers_status()
        c_info = statuses.get(lang.lower())
        if not c_info or c_info["id"] in ("unknown", "not_found"):
            return {"status": "error", "message": f"Container for {lang} not found"}

        cid = c_info["id"]
        action_clean = action.lower().strip()
        if action_clean == "stop":
            code, data = self._request("POST", f"/containers/{cid}/stop?t=3")
            state.set_app_active(lang.lower(), False)
            state.mark_unreachable(lang.lower())
            logger.info("Power Control: Stopped container %s (%s). Canary workload suspended.", container_name, cid)
            if code < 400:
                deploy_markers.record("stop", lang.lower(), f"stop {lang.lower()}")
            return {"status": "success", "action": "stop", "language": lang, "docker_code": code}
        elif action_clean == "start":
            code, data = self._request("POST", f"/containers/{cid}/start")
            state.set_app_active(lang.lower(), True)
            state.mark_unreachable(lang.lower())
            logger.info("Power Control: Started container %s (%s). Canary workload queued for warmup.", container_name, cid)
            if code < 400:
                deploy_markers.record("start", lang.lower(), f"start {lang.lower()}")
            return {"status": "success", "action": "start", "language": lang, "docker_code": code}
        elif action_clean == "restart":
            code, data = self._request("POST", f"/containers/{cid}/restart?t=3")
            state.set_app_active(lang.lower(), True)
            state.mark_unreachable(lang.lower())
            logger.info("Power Control: Restarted container %s (%s). Canary workload queued for warmup.", container_name, cid)
            if code < 400:
                deploy_markers.record("restart", lang.lower(), f"restart {lang.lower()}")
            return {"status": "success", "action": "restart", "language": lang, "docker_code": code}
        else:
            return {"status": "error", "message": f"Unsupported action: {action}"}

docker_manager = DockerManager()

# ---------------------------------------------------------------------------
# Curated Saved SQL Queries for Postgres
# ---------------------------------------------------------------------------
SAVED_QUERIES = [
    {
        "id": "recent_audit_logs",
        "name": "📜 Recent audit rows",
        "description": "The latest calls, with language and service taken from the JSON details.",
        "sql": "SELECT id, created_at, endpoint, status_code, round(response_time_seconds::numeric, 4) AS duration_sec, coalesce(details->>'language', 'python') AS language, coalesce(details->>'service', 'python-app') AS service FROM audit_logs ORDER BY id DESC LIMIT 50;"
    },
    {
        "id": "recent_failures",
        "name": "🚨 Recent failures",
        "description": "Calls that returned 4xx or 5xx, newest first.",
        "sql": "SELECT id, created_at, endpoint, status_code, round(response_time_seconds::numeric, 4) AS duration_sec, coalesce(details->>'language', 'python') AS language, left(coalesce(details->>'user_agent', ''), 80) AS user_agent FROM audit_logs WHERE status_code >= 400 ORDER BY id DESC LIMIT 50;"
    },
    {
        "id": "lang_audit_counts",
        "name": "📊 Volume by language",
        "description": "How many audit rows each language has written, and the time span covered.",
        "sql": "SELECT coalesce(details->>'language', 'python') AS language, count(*) AS calls, min(created_at) AS first_seen, max(created_at) AS last_seen FROM audit_logs GROUP BY 1 ORDER BY calls DESC;"
    },
    {
        "id": "endpoint_coverage",
        "name": "🧭 Endpoint coverage",
        "description": "Which routes show up, how often, and when they were first and last seen.",
        "sql": "SELECT endpoint, count(*) AS calls, min(created_at) AS first_seen, max(created_at) AS last_seen, round(avg(response_time_seconds)::numeric, 4) AS avg_sec FROM audit_logs GROUP BY endpoint ORDER BY calls DESC;"
    },
    {
        "id": "action_breakdown",
        "name": "⚡ Calls by endpoint and status",
        "description": "Volume and average duration for each route and status code.",
        "sql": "SELECT endpoint, status_code, count(*) AS calls, round(avg(response_time_seconds)::numeric, 4) AS avg_sec FROM audit_logs GROUP BY endpoint, status_code ORDER BY calls DESC;"
    },
    {
        "id": "status_classes",
        "name": "🚦 Status class mix",
        "description": "Share of 2xx, 4xx, and 5xx responses and the average duration of each class.",
        "sql": "SELECT CASE WHEN status_code < 300 THEN '2xx' WHEN status_code < 400 THEN '3xx' WHEN status_code < 500 THEN '4xx' ELSE '5xx' END AS status_class, count(*) AS calls, round(avg(response_time_seconds)::numeric, 4) AS avg_sec FROM audit_logs GROUP BY 1 ORDER BY 1;"
    },
    {
        "id": "error_rate_by_route",
        "name": "📉 Error rate by route",
        "description": "Percentage of calls at status 400 or above, by language and endpoint.",
        "sql": "SELECT coalesce(details->>'language', 'python') AS language, endpoint, count(*) AS calls, count(*) FILTER (WHERE status_code >= 400) AS errors, round((100.0 * count(*) FILTER (WHERE status_code >= 400) / nullif(count(*), 0))::numeric, 2) AS error_pct FROM audit_logs GROUP BY 1, 2 ORDER BY error_pct DESC, calls DESC;"
    },
    {
        "id": "latency_percentiles",
        "name": "📏 Latency percentiles by endpoint",
        "description": "p50, p95, and p99 response time for each route. This is the SLO view.",
        "sql": "SELECT endpoint, count(*) AS timed_calls, round(percentile_disc(0.50) WITHIN GROUP (ORDER BY response_time_seconds)::numeric, 4) AS p50_sec, round(percentile_disc(0.95) WITHIN GROUP (ORDER BY response_time_seconds)::numeric, 4) AS p95_sec, round(percentile_disc(0.99) WITHIN GROUP (ORDER BY response_time_seconds)::numeric, 4) AS p99_sec, round(max(response_time_seconds)::numeric, 4) AS max_sec FROM audit_logs WHERE response_time_seconds IS NOT NULL GROUP BY endpoint ORDER BY p95_sec DESC;"
    },
    {
        "id": "latency_by_language",
        "name": "🏁 Latency percentiles by language",
        "description": "Compare p50 and p95 across the services that record a response time.",
        "sql": "SELECT coalesce(details->>'language', 'python') AS language, count(*) AS timed_calls, round(avg(response_time_seconds)::numeric, 4) AS avg_sec, round(percentile_disc(0.50) WITHIN GROUP (ORDER BY response_time_seconds)::numeric, 4) AS p50_sec, round(percentile_disc(0.95) WITHIN GROUP (ORDER BY response_time_seconds)::numeric, 4) AS p95_sec FROM audit_logs WHERE response_time_seconds IS NOT NULL GROUP BY 1 ORDER BY p95_sec DESC;"
    },
    {
        "id": "latency_spread",
        "name": "📐 Latency spread by endpoint",
        "description": "Average versus standard deviation, so a route with a wild tail stands out.",
        "sql": "SELECT endpoint, count(*) AS timed_calls, round(avg(response_time_seconds)::numeric, 4) AS avg_sec, round(stddev_samp(response_time_seconds)::numeric, 4) AS stddev_sec, round(max(response_time_seconds)::numeric, 4) AS max_sec FROM audit_logs WHERE response_time_seconds IS NOT NULL GROUP BY endpoint ORDER BY stddev_sec DESC NULLS LAST;"
    },
    {
        "id": "slowest_calls",
        "name": "🐢 Slowest recent calls",
        "description": "The individual audit rows with the highest response time.",
        "sql": "SELECT id, created_at, endpoint, status_code, round(response_time_seconds::numeric, 4) AS duration_sec, coalesce(details->>'language', 'python') AS language FROM audit_logs WHERE response_time_seconds IS NOT NULL ORDER BY response_time_seconds DESC LIMIT 25;"
    },
    {
        "id": "latency_histogram",
        "name": "📊 Latency histogram",
        "description": "Ten buckets from 0 to 1 second, plus a bucket for anything slower.",
        "sql": "SELECT width_bucket(response_time_seconds, 0, 1, 10) AS bucket, count(*) AS calls, round(min(response_time_seconds)::numeric, 4) AS min_sec, round(max(response_time_seconds)::numeric, 4) AS max_sec FROM audit_logs WHERE response_time_seconds IS NOT NULL GROUP BY 1 ORDER BY 1;"
    },
    {
        "id": "hourly_activity",
        "name": "⏱️ Hourly volume and latency",
        "description": "Call count, error count, and average duration for each hour.",
        "sql": "SELECT date_trunc('hour', created_at) AS hour_window, count(*) AS calls, count(*) FILTER (WHERE status_code >= 400) AS errors, round(avg(response_time_seconds)::numeric, 4) AS avg_sec FROM audit_logs GROUP BY 1 ORDER BY hour_window DESC LIMIT 48;"
    },
    {
        "id": "five_minute_traffic",
        "name": "⏱️ Five-minute traffic",
        "description": "A finer volume and latency trend for the last six hours.",
        "sql": "SELECT date_bin('5 minutes', created_at, timestamp '2000-01-01') AS bucket, count(*) AS calls, count(*) FILTER (WHERE status_code >= 400) AS errors, round(avg(response_time_seconds)::numeric, 4) AS avg_sec FROM audit_logs WHERE created_at >= now() - interval '6 hours' GROUP BY 1 ORDER BY bucket DESC;"
    },
    {
        "id": "hour_of_day",
        "name": "🗓️ Volume by weekday and hour",
        "description": "When traffic clusters. Day 0 is Sunday, matching PostgreSQL extract(dow).",
        "sql": "SELECT extract(dow from created_at) AS day_of_week, extract(hour from created_at) AS hour_of_day, count(*) AS calls, round(avg(response_time_seconds)::numeric, 4) AS avg_sec FROM audit_logs GROUP BY 1, 2 ORDER BY 1, 2;"
    },
    {
        "id": "resource_vs_latency",
        "name": "🖥️ Load and memory beside latency",
        "description": "Per-minute average load, container memory percent, and response time.",
        "sql": "SELECT date_trunc('minute', created_at) AS minute, count(*) AS calls, round(avg(response_time_seconds)::numeric, 4) AS avg_sec, round(avg(system_loadavg_1m)::numeric, 2) AS avg_load, round(avg(container_memory_percent)::numeric, 1) AS avg_mem_pct FROM audit_logs WHERE response_time_seconds IS NOT NULL GROUP BY 1 ORDER BY minute DESC LIMIT 60;"
    },
    {
        "id": "memory_headroom",
        "name": "🧠 Container memory headroom",
        "description": "Average and peak container memory use against the cgroup limit, by language.",
        "sql": "SELECT coalesce(details->>'language', 'python') AS language, count(*) AS samples, round(avg(container_memory_percent)::numeric, 1) AS avg_mem_pct, round(max(container_memory_percent)::numeric, 1) AS max_mem_pct, round(avg(container_memory_current)::numeric, 0) AS avg_bytes, round(avg(container_memory_limit)::numeric, 0) AS limit_bytes FROM audit_logs WHERE container_memory_percent IS NOT NULL GROUP BY 1 ORDER BY max_mem_pct DESC;"
    },
    {
        "id": "instrumentation_coverage",
        "name": "🔎 Which fields are actually filled",
        "description": "Shows which languages record timing and container samples, and which leave them null.",
        "sql": "SELECT coalesce(details->>'language', 'python') AS language, count(*) AS calls, count(response_time_seconds) AS timed_calls, count(process_memory_rss) AS process_memory_samples, count(container_memory_percent) AS container_mem_samples, count(container_cpu_usage_ns) AS container_cpu_samples FROM audit_logs GROUP BY 1 ORDER BY calls DESC;"
    },
    {
        "id": "caller_mix",
        "name": "🌐 Caller addresses",
        "description": "Remote addresses stored in details, and how many of those calls failed.",
        "sql": "SELECT coalesce(details->>'remote_addr', '(none)') AS remote_addr, count(*) AS calls, count(*) FILTER (WHERE status_code >= 400) AS errors FROM audit_logs GROUP BY 1 ORDER BY calls DESC LIMIT 20;"
    },
    {
        "id": "load_latency_corr",
        "name": "🔗 Load versus latency",
        "description": "Correlation of host load and container memory with response time. Near 0 means they move independently.",
        "sql": "SELECT count(*) AS samples, round(corr(system_loadavg_1m, response_time_seconds)::numeric, 4) AS load_latency_corr, round(corr(container_memory_percent, response_time_seconds)::numeric, 4) AS memory_latency_corr FROM audit_logs WHERE response_time_seconds IS NOT NULL;"
    },
    {
        "id": "recent_editor_queries",
        "name": "📝 Recent SQL editor runs",
        "description": "Statements submitted from this editor, including ones the guard rejected.",
        "sql": "SELECT id, created_at, username, status, row_count, execution_time_ms, left(query_text, 120) AS query_preview FROM sql_query_audit ORDER BY id DESC LIMIT 50;"
    },
    {
        "id": "editor_outcomes",
        "name": "🧪 Editor allow, deny, and error counts",
        "description": "How often editor statements succeed, get rejected, or fail in Postgres.",
        "sql": "SELECT status, count(*) AS attempts, round(avg(execution_time_ms)::numeric, 2) AS avg_ms, max(created_at) AS latest FROM sql_query_audit GROUP BY status ORDER BY attempts DESC;"
    },
    {
        "id": "slow_editor_queries",
        "name": "🐢 Slowest editor statements",
        "description": "Successful editor queries ordered by how long they took.",
        "sql": "SELECT id, created_at, username, row_count, round(execution_time_ms::numeric, 2) AS ms, left(query_text, 160) AS query_preview FROM sql_query_audit WHERE status = 'success' ORDER BY execution_time_ms DESC LIMIT 20;"
    },
    {
        "id": "saved_query_catalog",
        "name": "🗂️ Saved query catalog",
        "description": "Queries stored for reuse in the SQL editor.",
        "sql": "SELECT id, name, description, created_by, updated_at FROM sql_saved_queries ORDER BY name;"
    }
]

def _pg_connect(read_only: bool = False):
    options = "-c statement_timeout=5000"
    if read_only:
        options += " -c default_transaction_read_only=on"
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        connect_timeout=4,
        options=options,
    )


def ensure_sql_editor_schema() -> None:
    """Create the editor's saved-query and audit tables, then seed the curated reads."""
    if not psycopg2:
        return
    conn = _pg_connect()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sql_saved_queries (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                sql TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT 'demouser',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sql_query_audit (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                username TEXT NOT NULL,
                query_text TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                row_count INTEGER,
                execution_time_ms DOUBLE PRECISION,
                client_addr TEXT
            )
            """
        )
        for query in SAVED_QUERIES:
            guard_readonly_sql(query["sql"])
            cur.execute(
                """
                INSERT INTO sql_saved_queries (id, name, description, sql, created_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    sql = EXCLUDED.sql,
                    updated_at = now()
                """,
                (query["id"], query["name"], query["description"], query["sql"], CANARY_DEMO_USER),
            )
    finally:
        conn.close()


def list_saved_queries() -> list[dict]:
    ensure_sql_editor_schema()
    conn = _pg_connect(read_only=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, description, sql, created_by FROM sql_saved_queries ORDER BY name"
        )
        return [
            {"id": row[0], "name": row[1], "description": row[2], "sql": row[3], "created_by": row[4]}
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def save_saved_query(name: str, sql: str, description: str, username: str) -> dict:
    guard_readonly_sql(sql)
    ensure_sql_editor_schema()
    query_id = "user-" + secrets.token_hex(4)
    conn = _pg_connect()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO sql_saved_queries (id, name, description, sql, created_by)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (query_id, name[:80], (description or "")[:240], sql[:4000], username or CANARY_DEMO_USER),
        )
    finally:
        conn.close()
    return {"id": query_id, "name": name[:80], "description": description or "", "sql": sql[:4000]}


def _record_sql_audit(username, query_text, status, error_message, row_count, elapsed_ms, client_addr) -> None:
    if not psycopg2:
        return
    try:
        conn = _pg_connect()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO sql_query_audit
                (username, query_text, status, error_message, row_count, execution_time_ms, client_addr)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                username or CANARY_DEMO_USER,
                (query_text or "")[:4000],
                status,
                (error_message or None),
                row_count,
                elapsed_ms,
                client_addr or None,
            ),
        )
        conn.close()
    except Exception as exc:
        logger.warning("Could not write SQL editor audit row: %s", exc)


_PLAN_COLUMNS = [
    "node", "relation", "startup_cost", "total_cost", "plan_rows",
    "actual_rows", "actual_ms", "loops", "shared_hit", "shared_read", "detail",
]


def _sql_cell(val):
    if val is None:
        return None
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, float):
        return str(round(val, 4))
    if isinstance(val, (dict, list)):
        return json.dumps(val)
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _flatten_explain(payload) -> dict:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    rows = []

    explanations = []

    def walk(node, depth, parent_type=None):
        if parent_type:
            node["_parent_type"] = parent_type
        detail_parts = _plan_detail_parts(node)
        rows.append([
            ("  " * depth) + str(node.get("Node Type") or ""),
            node.get("Relation Name"),
            node.get("Startup Cost"),
            node.get("Total Cost"),
            node.get("Plan Rows"),
            node.get("Actual Rows"),
            node.get("Actual Total Time"),
            node.get("Actual Loops"),
            node.get("Shared Hit Blocks"),
            node.get("Shared Read Blocks"),
            "; ".join(detail_parts) or None,
        ])
        explanations.append(_explain_plan_node(node))
        for child in node.get("Plans") or []:
            walk(child, depth + 1, node.get("Node Type"))

    walk(payload.get("Plan") or {}, 0)
    return {
        "columns": list(_PLAN_COLUMNS),
        "rows": [[_sql_cell(cell) for cell in row] for row in rows],
        "explanations": explanations,
        "planning_time_ms": payload.get("Planning Time"),
        "execution_time_ms": payload.get("Execution Time"),
    }


def _plan_list(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _plan_detail_parts(node: dict) -> list[str]:
    parts = []
    if node.get("Index Name"):
        parts.append("Index: " + str(node["Index Name"]))
    if node.get("Scan Direction"):
        parts.append("Direction: " + str(node["Scan Direction"]))
    if node.get("Parent Relationship"):
        parts.append("Role: " + str(node["Parent Relationship"]))
    for label, key in (
        ("Filter", "Filter"),
        ("Index Cond", "Index Cond"),
        ("Hash Cond", "Hash Cond"),
        ("Join Filter", "Join Filter"),
        ("Recheck Cond", "Recheck Cond"),
    ):
        if node.get(key):
            parts.append(f"{label}: {node[key]}")
    if node.get("Sort Key"):
        parts.append("Sort Key: " + _plan_list(node["Sort Key"]))
    if node.get("Group Key"):
        parts.append("Group Key: " + _plan_list(node["Group Key"]))
    if node.get("Workers Launched") is not None:
        parts.append(
            f"Workers: {node.get('Workers Launched')} launched of {node.get('Workers Planned')} planned"
        )
    return parts


_PLAN_NODE_TEXT = {
    "Seq Scan": "Reads the table heap from start to finish. No index skips pages.",
    "Index Scan": "Searches an index for matching entries, then fetches those rows from the heap. The index condition is applied in the index. A filter is checked only after the heap row is fetched.",
    "Index Only Scan": "Searches an index that already holds every column this step needs. The heap is skipped when the visibility map says the page is all-visible, and visited when it does not.",
    "Bitmap Index Scan": "Builds a bitmap of matching row locations from an index. It does not fetch heap rows. The bitmap heap scan above it does that.",
    "Bitmap Heap Scan": "Fetches heap pages from a bitmap of row locations, in physical page order, so a page is read once. A recheck means the bitmap was not exact and each candidate row is tested again.",
    "BitmapAnd": "Keeps row locations that appear in every child bitmap. This is how several indexes are combined with AND.",
    "BitmapOr": "Unions row locations from the child bitmaps. This is how several indexes are combined with OR.",
    "Nested Loop": "For each row from the outer child, runs the inner child once. This stays cheap when the outer side is small and the inner side is an index lookup.",
    "Hash Join": "Builds a hash table from one side and probes it with the other. The hash condition is the equality used for the match.",
    "Merge Join": "Walks two inputs that are already ordered on the join key and merges matches. A sort underneath exists when nothing else supplied that order.",
    "Hash": "Builds the hash table that the hash join above it probes.",
    "Sort": "Orders its input by the sort key before the parent can use the rows. The parent is typically ORDER BY, a merge join, a group aggregate, or a unique step.",
    "Incremental Sort": "Sorts groups that are already ordered on a prefix of the sort key, instead of sorting the whole input again.",
    "Aggregate": "Computes aggregates over the whole input. With no GROUP BY, one row comes out.",
    "GroupAggregate": "Computes aggregates for groups that arrive already sorted on the group key. One row comes out per group.",
    "HashAggregate": "Puts groups in a hash table keyed by the GROUP BY columns, then emits one row per group. Memory grows with the number of distinct groups.",
    "MixedAggregate": "Computes some aggregates from a hash table and others from sorted input.",
    "Limit": "Stops after the requested number of rows, so work above this step can finish early. A sort underneath still has to order its input before the first row can leave, unless that input was already ordered.",
    "Gather": "Collects rows from parallel workers into the leader. It does not keep the workers' sort order. The child plan is what each worker ran.",
    "Gather Merge": "Collects ordered streams from parallel workers and merges them so the combined output stays sorted.",
    "Materialize": "Stores the child output so a parent can read it again without running the child another time.",
    "Memoize": "Remembers inner results of a nested loop, keyed by the current parameter values, so a repeated lookup is not executed again.",
    "CTE Scan": "Reads a WITH query that was materialized rather than inlined. The CTE ran on its own, and this step scans that stored result.",
    "Subquery Scan": "Runs a subquery as its own plan and scans that output. The subquery was not flattened into the outer query.",
    "Result": "Produces rows from expressions, with no table read. A constant select or a projection shows up this way.",
    "Unique": "Drops duplicates that sit next to each other, so the input has to arrive sorted. This is one way DISTINCT is executed.",
    "WindowAgg": "Computes window functions over the ordered frame and keeps the rows in that order.",
    "Append": "Runs each child in turn and concatenates the rows. UNION ALL and a scan of several partitions use this. It does not remove duplicates.",
    "MergeAppend": "Merges several already-ordered children so the combined output stays ordered.",
    "Recursive Union": "Runs a WITH RECURSIVE query, feeding each round's new rows back in until a round adds nothing.",
    "WorkTable Scan": "Reads the working table that the recursive step is filling for the current round.",
    "Function Scan": "Calls a set-returning function and scans the rows it returns.",
    "Values Scan": "Scans the rows written in a VALUES list. No table is read.",
    "Tid Scan": "Fetches rows by ctid, their physical location.",
    "Sample Scan": "Reads a sample of the table rather than every row.",
    "Foreign Scan": "Asks a foreign-data wrapper for rows stored outside this database. The time includes waiting on that source.",
    "LockRows": "Locks the qualifying rows, as SELECT FOR UPDATE does, before they are returned.",
    "ModifyTable": "Writes rows with INSERT, UPDATE, DELETE, or MERGE.",
    "ProjectSet": "Expands a set-returning function in the select list into one output row per returned value.",
}


def _fmt_plan_num(value, digits: int = 2):
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits == 0 or number.is_integer():
        return str(int(number)) if number.is_integer() and abs(number) < 1e15 else f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _describe_plan_node(node: dict, node_type: str) -> str:
    if node_type == "Aggregate":
        partial = node.get("Partial Mode")
        strategy = node.get("Strategy")
        if partial == "Partial":
            return (
                "Each parallel worker aggregates only the rows it read. "
                "What comes out is a partial aggregate state for each group, not the final totals. "
                "A finalize step above this one combines those states."
            )
        if partial == "Finalize":
            return "Combines the partial aggregate states from the workers into one finished row per group."
        if strategy == "Sorted":
            return _PLAN_NODE_TEXT["GroupAggregate"]
        if strategy == "Hashed" or node.get("Group Key"):
            return _PLAN_NODE_TEXT["HashAggregate"]
        return _PLAN_NODE_TEXT["Aggregate"]
    return _PLAN_NODE_TEXT.get(node_type, "PostgreSQL used this step while producing the query result.")


def _explain_plan_node(node: dict) -> str:
    node_type = str(node.get("Node Type") or "Plan node")
    what = _describe_plan_node(node, node_type)
    if node.get("Parallel Aware"):
        what = "Parallel workers ran this step. " + what
    relation = node.get("Relation Name")
    if relation:
        alias = node.get("Alias")
        if alias and alias != relation:
            what += f" The table is {relation}, aliased as {alias}."
        else:
            what += f" The table is {relation}."
    if node.get("Index Name"):
        what += f" The index is {node['Index Name']}."
        if node.get("Scan Direction"):
            what += f" The scan direction is {node['Scan Direction']}."

    paragraphs = [what]
    startup = node.get("Startup Cost")
    total = node.get("Total Cost")
    if startup is not None or total is not None:
        paragraphs.append(
            f"Startup cost {_fmt_plan_num(startup)} is the planner's estimate of effort before the first row. "
            f"Total cost {_fmt_plan_num(total)} is the estimate to produce the rows it expected. "
            "Costs are arbitrary units, where one sequential page read is about 1. They are not milliseconds."
        )
    actual_ms = node.get("Actual Total Time")
    loops = node.get("Actual Loops")
    if actual_ms is not None:
        timing = f"One run took {_fmt_plan_num(actual_ms, 3)} ms, and that time includes the steps underneath this one."
        try:
            loop_count = float(loops or 1)
        except (TypeError, ValueError):
            loop_count = 1
        if loop_count > 1:
            timing += (
                f" The node ran {_fmt_plan_num(loop_count, 0)} times, and the time above is the average of those runs "
                f"(about {_fmt_plan_num(float(actual_ms) * loop_count, 3)} ms added together)."
            )
        if node.get("Plans"):
            timing += " Adding this time to the times of its children double-counts those children."
        paragraphs.append(timing)

    plan_rows = node.get("Plan Rows")
    actual_rows = node.get("Actual Rows")
    if plan_rows is not None and actual_rows is not None:
        try:
            expected = float(plan_rows)
            actual = float(actual_rows)
            loop_count = float(loops or 1)
        except (TypeError, ValueError):
            expected = actual = loop_count = None
        if expected is not None:
            estimate = (
                f"The planner expected about {_fmt_plan_num(expected)} rows from one run of this step. "
                f"This run returned about {_fmt_plan_num(actual)} rows"
            )
            if loop_count and loop_count > 1:
                estimate += (
                    f" each time it ran, about {_fmt_plan_num(round(actual * loop_count))} rows across all "
                    f"{_fmt_plan_num(loop_count, 0)} runs"
                )
            estimate += "."
            if expected > 0 and (actual >= expected * 10 or actual * 10 <= expected):
                estimate += " The estimate is far from the measured count. That usually means the column statistics do not describe the rows this predicate selects."
            paragraphs.append(estimate)

    hit = node.get("Shared Hit Blocks")
    read = node.get("Shared Read Blocks")
    if hit is not None or read is not None:
        buffer_bits = []
        if hit:
            buffer_bits.append(f"{_fmt_plan_num(hit, 0)} pages were already in PostgreSQL shared buffers")
        if read:
            buffer_bits.append(
                f"{_fmt_plan_num(read, 0)} pages were not in shared buffers and were requested from the operating system. "
                "That read does not prove the page came from disk; the operating system cache may still have held it"
            )
        if not hit and not read:
            buffer_bits.append("This step did not report shared-buffer reads of its own")
        buffer_text = ". ".join(buffer_bits) + "."
        if node.get("Plans"):
            buffer_text += " The counts include pages touched by the steps underneath this one."
        paragraphs.append(buffer_text)

    notes = []
    if node.get("Filter"):
        notes.append(
            f"Filter ({node['Filter']}): rows are read and then dropped when they fail this test. "
            "Dropped rows are not included in the actual row count, so the step can read more than that count shows."
        )
    if node.get("Index Cond"):
        notes.append(
            f"Index condition ({node['Index Cond']}): the index is searched with this predicate, so non-matching index entries are not fetched from the heap."
        )
    if node.get("Recheck Cond"):
        notes.append(
            f"Recheck ({node['Recheck Cond']}): the bitmap did not name exact rows, often because it switched to page granularity after it grew. Each candidate heap row is tested again."
        )
    if node.get("Hash Cond"):
        notes.append(f"Hash condition ({node['Hash Cond']}): this equality chooses the hash bucket a row is built into or probed against.")
    if node.get("Join Filter"):
        notes.append(
            f"Join filter ({node['Join Filter']}): applied after the join method has a candidate pair. It is a predicate that method could not use as its main match condition."
        )
    if node.get("Sort Key"):
        notes.append(f"Sort key ({_plan_list(node['Sort Key'])}): rows are ordered by this list for the step above.")
    if node.get("Group Key"):
        notes.append(f"Group key ({_plan_list(node['Group Key'])}): one aggregate result is produced for each distinct value of this list.")
    role = node.get("Parent Relationship")
    parent_type = str((node.get("_parent_type") or ""))
    if role in ("Outer", "Inner") and parent_type in ("Nested Loop", "Hash Join", "Merge Join"):
        notes.append(f"This step is the {role.lower()} input of the {parent_type} above it.")
    if node.get("Workers Launched") is not None:
        notes.append(
            f"{node.get('Workers Launched')} parallel workers launched, out of {node.get('Workers Planned')} the planner asked for."
        )
    if notes:
        paragraphs.append(" ".join(notes))
    return "\n\n".join(paragraphs)


def _capture_plan(cur, sql: str) -> dict:
    """Run EXPLAIN ANALYZE on a statement the guard has already accepted."""
    explained = sql.strip().rstrip(";").strip()
    cur.execute("SAVEPOINT sql_plan")
    try:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + explained)
        payload = cur.fetchone()[0]
        cur.execute("RELEASE SAVEPOINT sql_plan")
        return _flatten_explain(payload)
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT sql_plan")
        return {"columns": [], "rows": [], "error": str(exc)}


def execute_sql_query(query_str: str, max_rows: int = 100, username: str = "", client_addr: str = "") -> dict:
    """Run one read-only SELECT against the application tables and audit the attempt."""
    if not psycopg2:
        return {
            "status": "error",
            "error": "psycopg2 driver not installed in canary runtime",
            "query": query_str,
        }

    clean_q = (query_str or "").strip()
    max_rows = min(max(int(max_rows or 100), 1), 200)
    t0 = time.time()
    try:
        guard_readonly_sql(clean_q)
    except SqlGuardError as exc:
        elapsed_ms = round((time.time() - t0) * 1000, 2)
        _record_sql_audit(username, clean_q, "denied", str(exc), None, elapsed_ms, client_addr)
        return {
            "status": "error",
            "error": str(exc),
            "execution_time_ms": elapsed_ms,
            "query": clean_q,
        }

    conn = None
    try:
        conn = _pg_connect(read_only=True)
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("BEGIN READ ONLY")
        cur.execute(clean_q)
        if not cur.description:
            raise SqlGuardError("Only read-only SELECT statements are allowed")
        columns = [desc[0] for desc in cur.description]
        raw_rows = cur.fetchmany(max_rows)
        formatted_rows = [[_sql_cell(val) for val in row] for row in raw_rows]
        plan = _capture_plan(cur, clean_q)
        conn.rollback()
        elapsed_ms = round((time.time() - t0) * 1000, 2)
        _record_sql_audit(username, clean_q, "success", None, len(formatted_rows), elapsed_ms, client_addr)
        return {
            "status": "success",
            "columns": columns,
            "rows": formatted_rows,
            "row_count": len(formatted_rows),
            "execution_time_ms": elapsed_ms,
            "truncated": len(raw_rows) >= max_rows,
            "query": clean_q,
            "plan": plan,
        }
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        elapsed_ms = round((time.time() - t0) * 1000, 2)
        message = str(exc)
        _record_sql_audit(username, clean_q, "error", message, None, elapsed_ms, client_addr)
        return {
            "status": "error",
            "error": message,
            "execution_time_ms": elapsed_ms,
            "query": clean_q,
        }
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Dependencies Health Prober (9 Stack Components)
# ---------------------------------------------------------------------------
def check_dependencies() -> dict:
    """Probes all 9 stack dependencies and computes observed health, latency, usage, and error rate."""
    deps = []

    # 1. PostgreSQL
    pg_ok = False
    pg_lat = 0.0
    pg_details = {}
    t0 = time.time()
    try:
        if psycopg2:
            conn = psycopg2.connect(
                host=POSTGRES_HOST, port=POSTGRES_PORT, dbname=POSTGRES_DB,
                user=POSTGRES_USER, password=POSTGRES_PASSWORD, connect_timeout=2
            )
            cur = conn.cursor()
            cur.execute("SELECT pg_size_pretty(pg_database_size('observability')), count(*) FROM pg_stat_activity WHERE datname='observability';")
            row = cur.fetchone()
            cur.execute("SELECT count(*) FROM audit_logs;")
            log_cnt = cur.fetchone()[0]
            conn.close()
            pg_lat = round((time.time() - t0) * 1000, 1)
            pg_ok = True
            pg_details = {
                "db_size": row[0] if row else "unknown",
                "active_connections": row[1] if row else 0,
                "audit_logs_count": log_cnt
            }
        else:
            s = socket.create_connection((POSTGRES_HOST, POSTGRES_PORT), timeout=1.5)
            s.close()
            pg_lat = round((time.time() - t0) * 1000, 1)
            pg_ok = True
            pg_details = {"port_check": "TCP 5432 open"}
    except Exception as exc:
        pg_lat = round((time.time() - t0) * 1000, 1)
        pg_details = {"error": str(exc)}

    deps.append({
        "id": "postgres",
        "name": "PostgreSQL",
        "role": "Relational Data & Audit Store",
        "endpoint": f"{POSTGRES_HOST}:{POSTGRES_PORT}",
        "protocol": "PostgreSQL Wire Protocol",
        "status": "UP" if pg_ok else "DOWN",
        "available": pg_ok,
        "availability_pct": 100.0 if pg_ok else 0.0,
        "latency_ms": pg_lat,
        "error_rate_pct": 0.0 if pg_ok else 100.0,
        "capacity_usage": f"DB Size: {pg_details.get('db_size', 'N/A')} | Conns: {pg_details.get('active_connections', 0)} | Logs: {pg_details.get('audit_logs_count', 'N/A')}" if pg_ok else "Unreachable",
        "details": pg_details
    })

    # 2. Valkey
    vk_ok = False
    vk_lat = 0.0
    vk_details = {}
    t0 = time.time()
    try:
        r = redis.Redis(host=VALKEY_HOST, port=6379, socket_timeout=1.5, decode_responses=True)
        r.ping()
        info = r.info()
        vk_lat = round((time.time() - t0) * 1000, 1)
        vk_ok = True
        vk_details = {
            "used_memory_human": info.get("used_memory_human", "N/A"),
            "connected_clients": info.get("connected_clients", 0),
            "total_commands": info.get("total_commands_processed", 0)
        }
    except Exception as exc:
        vk_lat = round((time.time() - t0) * 1000, 1)
        vk_details = {"error": str(exc)}

    deps.append({
        "id": "valkey",
        "name": "Valkey",
        "role": "In-Memory Telemetry & Cache",
        "endpoint": f"{VALKEY_HOST}:6379",
        "protocol": "RESP / Redis Protocol",
        "status": "UP" if vk_ok else "DOWN",
        "available": vk_ok,
        "availability_pct": 100.0 if vk_ok else 0.0,
        "latency_ms": vk_lat,
        "error_rate_pct": 0.0 if vk_ok else 100.0,
        "capacity_usage": f"Memory: {vk_details.get('used_memory_human', 'N/A')} | Clients: {vk_details.get('connected_clients', 0)} | Cmds: {vk_details.get('total_commands', 0)}" if vk_ok else "Unreachable",
        "details": vk_details
    })

    # 3. OpenTelemetry Collector
    otel_ok = False
    otel_lat = 0.0
    otel_details = {}
    t0 = time.time()
    try:
        try:
            resp = requests.get("http://otel-collector:13133/", timeout=1.5)
            if resp.status_code == 200:
                otel_ok = True
                otel_details = {"health_endpoint": "HTTP 13133 OK"}
        except Exception:
            s = socket.create_connection(("otel-collector", 4317), timeout=1.5)
            s.close()
            otel_ok = True
            otel_details = {"grpc_port": "4317 Open"}
        otel_lat = round((time.time() - t0) * 1000, 1)
    except Exception as exc:
        otel_lat = round((time.time() - t0) * 1000, 1)
        otel_details = {"error": str(exc)}

    deps.append({
        "id": "otel-collector",
        "name": "OTel Collector",
        "role": "Telemetry Pipeline (OTLP gRPC/HTTP)",
        "endpoint": "otel-collector:4317 / 4318",
        "protocol": "OTLP / gRPC / Protobuf",
        "status": "UP" if otel_ok else "DOWN",
        "available": otel_ok,
        "availability_pct": 100.0 if otel_ok else 0.0,
        "latency_ms": otel_lat,
        "error_rate_pct": 0.0 if otel_ok else 100.0,
        "capacity_usage": "Pipelines: Traces (Tempo), Metrics (Mimir), Logs (Loki)" if otel_ok else "Pipeline Inactive",
        "details": otel_details
    })

    # 4. Tempo
    tempo_ok = False
    tempo_lat = 0.0
    t0 = time.time()
    try:
        resp = requests.get(f"{TEMPO_URL}/ready", timeout=2.0)
        tempo_lat = round((time.time() - t0) * 1000, 1)
        tempo_ok = (resp.status_code == 200)
    except Exception:
        tempo_lat = round((time.time() - t0) * 1000, 1)

    deps.append({
        "id": "tempo",
        "name": "Grafana Tempo",
        "role": "Distributed Tracing Backend",
        "endpoint": f"{TEMPO_URL}/ready",
        "protocol": "HTTP / gRPC Trace Ingestion",
        "status": "UP" if tempo_ok else "DOWN",
        "available": tempo_ok,
        "availability_pct": 100.0 if tempo_ok else 0.0,
        "latency_ms": tempo_lat,
        "error_rate_pct": 0.0 if tempo_ok else 100.0,
        "capacity_usage": "Trace Storage: Local Block Store | WAL Active" if tempo_ok else "Unreachable",
        "details": {"status_code": 200 if tempo_ok else 500}
    })

    # 5. Loki
    loki_ok = False
    loki_lat = 0.0
    t0 = time.time()
    try:
        resp = requests.get(f"{LOKI_URL}/ready", timeout=2.0)
        loki_lat = round((time.time() - t0) * 1000, 1)
        loki_ok = (resp.status_code == 200)
    except Exception:
        loki_lat = round((time.time() - t0) * 1000, 1)

    deps.append({
        "id": "loki",
        "name": "Grafana Loki",
        "role": "High-Volume Log Aggregation",
        "endpoint": f"{LOKI_URL}/ready",
        "protocol": "HTTP / LogQL API",
        "status": "UP" if loki_ok else "DOWN",
        "available": loki_ok,
        "availability_pct": 100.0 if loki_ok else 0.0,
        "latency_ms": loki_lat,
        "error_rate_pct": 0.0 if loki_ok else 100.0,
        "capacity_usage": "Log Engine: TSDB Chunks | Ingestion Active" if loki_ok else "Unreachable",
        "details": {"status_code": 200 if loki_ok else 500}
    })

    # 6. Mimir
    mimir_ok = False
    mimir_lat = 0.0
    t0 = time.time()
    try:
        resp = requests.get(f"{MIMIR_URL}/ready", timeout=2.0)
        mimir_lat = round((time.time() - t0) * 1000, 1)
        mimir_ok = (resp.status_code == 200)
    except Exception:
        mimir_lat = round((time.time() - t0) * 1000, 1)

    deps.append({
        "id": "mimir",
        "name": "Grafana Mimir",
        "role": "Long-Term Prometheus Metrics Store",
        "endpoint": f"{MIMIR_URL}/ready",
        "protocol": "HTTP / PromQL Query Range",
        "status": "UP" if mimir_ok else "DOWN",
        "available": mimir_ok,
        "availability_pct": 100.0 if mimir_ok else 0.0,
        "latency_ms": mimir_lat,
        "error_rate_pct": 0.0 if mimir_ok else 100.0,
        "capacity_usage": "TSDB Compactor & Ingester Active" if mimir_ok else "Unreachable",
        "details": {"status_code": 200 if mimir_ok else 500}
    })

    # 7. Prometheus
    prom_ok = False
    prom_lat = 0.0
    t0 = time.time()
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/-/ready", timeout=2.0)
        prom_lat = round((time.time() - t0) * 1000, 1)
        prom_ok = (resp.status_code == 200)
    except Exception:
        prom_lat = round((time.time() - t0) * 1000, 1)

    deps.append({
        "id": "prometheus",
        "name": "Prometheus",
        "role": "Scraper & Alert Manager",
        "endpoint": f"{PROMETHEUS_URL}/-/ready",
        "protocol": "HTTP / Scraping Engine",
        "status": "UP" if prom_ok else "DOWN",
        "available": prom_ok,
        "availability_pct": 100.0 if prom_ok else 0.0,
        "latency_ms": prom_lat,
        "error_rate_pct": 0.0 if prom_ok else 100.0,
        "capacity_usage": "Scrape Targets: 8+ Jobs (Apps, Exporters, Infra)" if prom_ok else "Unreachable",
        "details": {"status_code": 200 if prom_ok else 500}
    })

    # 8. Grafana
    graf_ok = False
    graf_lat = 0.0
    t0 = time.time()
    try:
        resp = requests.get(f"{GRAFANA_URL}/api/health", timeout=2.0)
        graf_lat = round((time.time() - t0) * 1000, 1)
        graf_ok = (resp.status_code == 200)
    except Exception:
        graf_lat = round((time.time() - t0) * 1000, 1)

    deps.append({
        "id": "grafana",
        "name": "Grafana",
        "role": "Unified Observability Dashboards",
        "endpoint": f"{GRAFANA_URL}/api/health",
        "protocol": "HTTP / REST API",
        "status": "UP" if graf_ok else "DOWN",
        "available": graf_ok,
        "availability_pct": 100.0 if graf_ok else 0.0,
        "latency_ms": graf_lat,
        "error_rate_pct": 0.0 if graf_ok else 100.0,
        "capacity_usage": "Datasources: Mimir, Tempo, Loki, Postgres" if graf_ok else "Unreachable",
        "details": {"status_code": 200 if graf_ok else 500}
    })

    # 9. Redpanda
    rp_ok = False
    rp_lat = 0.0
    t0 = time.time()
    try:
        resp = requests.get(f"{REDPANDA_ADMIN_URL}/v1/status/ready", timeout=2.0)
        rp_lat = round((time.time() - t0) * 1000, 1)
        rp_ok = (resp.status_code == 200)
    except Exception:
        try:
            s = socket.create_connection(("redpanda", 9092), timeout=1.5)
            s.close()
            rp_lat = round((time.time() - t0) * 1000, 1)
            rp_ok = True
        except Exception:
            rp_lat = round((time.time() - t0) * 1000, 1)

    deps.append({
        "id": "redpanda",
        "name": "Redpanda",
        "role": "Kafka-Compatible Event Streaming",
        "endpoint": "redpanda:9092 / 9644",
        "protocol": "Kafka Binary Protocol & Admin REST",
        "status": "UP" if rp_ok else "DOWN",
        "available": rp_ok,
        "availability_pct": 100.0 if rp_ok else 0.0,
        "latency_ms": rp_lat,
        "error_rate_pct": 0.0 if rp_ok else 100.0,
        "capacity_usage": "Raft Consensus | Event Bus Active" if rp_ok else "Unreachable",
        "details": {"status": "ready" if rp_ok else "down"}
    })

    # Host ports published by docker-compose. The dashboard builds the URL
    # from the browser's hostname so the link works on localhost and on the LAN.
    browser_links = {
        "postgres": {"port": 9187, "path": "/metrics", "label": "Open metrics"},
        "valkey": {"port": 9121, "path": "/metrics", "label": "Open metrics"},
        "otel-collector": {"port": 55679, "path": "/debug/tracez", "label": "Open"},
        "tempo": {"port": 3200, "path": "/status", "label": "Open"},
        "loki": {"port": 3100, "path": "/services", "label": "Open"},
        "mimir": {"port": 9009, "path": "/", "label": "Open"},
        "prometheus": {"port": 9090, "path": "/query", "label": "Open"},
        "grafana": {
            "port": 3000,
            "path": "/d/observability-demo-metrics/observability-demo",
            "label": "Open",
        },
        "redpanda": {"port": 9644, "path": "/v1/cluster/health_overview", "label": "Open"},
    }
    for dep in deps:
        link = browser_links.get(dep["id"])
        if link:
            dep["browser"] = link

    total_cnt = len(deps)
    healthy_cnt = sum(1 for d in deps if d["available"])
    avg_latency = round(sum(d["latency_ms"] for d in deps) / total_cnt, 1) if total_cnt > 0 else 0.0
    overall_avail = round((healthy_cnt / total_cnt) * 100.0, 1) if total_cnt > 0 else 100.0

    return {
        "timestamp": time.time(),
        "total_dependencies": total_cnt,
        "healthy_count": healthy_cnt,
        "overall_availability_pct": overall_avail,
        "avg_latency_ms": avg_latency,
        "dependencies": deps
    }

# ---------------------------------------------------------------------------
# Fault Injection Manager
# ---------------------------------------------------------------------------
class FaultManager:
    """Manages active and historical Fault Injection Testing drills with event tags."""
    def __init__(self):
        self._lock = threading.Lock()
        self.active_fault = None
        self.history = deque(maxlen=50)

    def start_fault(self, tag: str, fault_type: str, target: str,
                    duration_sec: int = 60, rate: int = 100, delay_ms: int = 1000) -> dict:
        with self._lock:
            now = time.time()
            if self.active_fault:
                self._finalize_active(now)

            clean_tag = tag.strip() if tag and tag.strip() else f"DRILL-{int(now)}"
            target_clean = target.lower().strip()
            if target_clean not in ("all", "python", "java", "rust", "node", "go", "dotnet", "c"):
                target_clean = "all"

            fault = {
                "id": f"fault_{int(now)}_{random.randint(1000, 9999)}",
                "tag": clean_tag,
                "fault_type": fault_type,
                "target": target_clean,
                "rate": max(1, min(100, int(rate))),
                "delay_ms": max(100, int(delay_ms)),
                "duration_sec": max(0, int(duration_sec)),
                "start_time": now,
                "end_time": (now + duration_sec) if duration_sec > 0 else None,
                "status": "active",
                "affected_requests": 0,
                "induced_errors": 0,
            }
            self.active_fault = fault
            logger.info("Fault Injection Started: [%s] Type=%s Target=%s Duration=%ss",
                        clean_tag, fault_type, target_clean, duration_sec)
            return dict(fault)

    def stop_fault(self) -> dict:
        with self._lock:
            now = time.time()
            if not self.active_fault:
                return {}
            fault = self._finalize_active(now)
            logger.info("Fault Injection Stopped: [%s] Affected=%d Errors=%d",
                        fault.get("tag"), fault.get("affected_requests", 0), fault.get("induced_errors", 0))
            return fault

    def _finalize_active(self, now: float) -> dict:
        if not self.active_fault:
            return {}
        f = self.active_fault
        f["status"] = "completed"
        f["end_time"] = now
        f["actual_duration_sec"] = round(now - f["start_time"], 1)
        self.history.appendleft(dict(f))
        self.active_fault = None
        return dict(f)

    def should_inject(self, app_lang: str) -> tuple[bool, dict | None]:
        with self._lock:
            if not self.active_fault:
                return False, None

            now = time.time()
            if self.active_fault["end_time"] and now >= self.active_fault["end_time"]:
                self._finalize_active(now)
                return False, None

            target = self.active_fault["target"]
            if target != "all" and target != app_lang:
                return False, None

            rate = self.active_fault["rate"]
            if rate < 100 and random.uniform(0, 100) > rate:
                return False, None

            return True, dict(self.active_fault)

    def record_affected(self, is_error: bool):
        with self._lock:
            if self.active_fault:
                self.active_fault["affected_requests"] += 1
                if is_error:
                    self.active_fault["induced_errors"] += 1

    def get_snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            if self.active_fault and self.active_fault["end_time"] and now >= self.active_fault["end_time"]:
                self._finalize_active(now)

            active = dict(self.active_fault) if self.active_fault else None
            if active and active.get("end_time"):
                active["remaining_sec"] = max(0, int(active["end_time"] - now))
            return {
                "active_fault": active,
                "history": list(self.history)
            }

    def get_events_for_range(self, start_time: float, end_time: float) -> list[dict]:
        with self._lock:
            now = time.time()
            events = []
            all_list = []
            if self.active_fault:
                all_list.append(dict(self.active_fault))
            all_list.extend(list(self.history))

            for f in all_list:
                f_start = f["start_time"]
                f_end = f.get("end_time") or now
                if f_end >= start_time and f_start <= end_time:
                    events.append({
                        "id": f["id"],
                        "tag": f["tag"],
                        "fault_type": f["fault_type"],
                        "target": f["target"],
                        "start_time": f_start,
                        "end_time": f_end,
                        "active": (f.get("status") == "active")
                    })
            return sorted(events, key=lambda x: x["start_time"])

fault_manager = FaultManager()

# ---------------------------------------------------------------------------
# Deploy and restart markers for the trend graphs
# ---------------------------------------------------------------------------
class DeployMarkers:
    """Point-in-time tags for stack deploys and container restarts.

    Stored in Valkey so the lines remain after the canary process restarts.
    """

    KEY = "canary:deploy_markers"

    def __init__(self):
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._vk = None

    def attach(self, client) -> None:
        self._vk = client
        if client is None:
            return
        try:
            raw = client.lrange(self.KEY, 0, -1)
        except Exception as exc:
            logger.warning("Could not load deploy markers: %s", exc)
            return
        loaded = []
        for item in raw or []:
            try:
                loaded.append(json.loads(item))
            except Exception:
                continue
        with self._lock:
            self._events = loaded[-400:]

    def record(self, kind: str, target: str, tag: str, when: float | None = None) -> dict:
        when = float(time.time() if when is None else when)
        target = (target or "stack").lower()
        kind = kind if kind in ("deploy", "restart", "start", "stop") else "restart"
        event = {
            "kind": kind,
            "target": target,
            "tag": tag or kind,
            "time": round(when, 3),
        }
        with self._lock:
            for existing in self._events:
                if (
                    existing.get("kind") == kind
                    and existing.get("target") == target
                    and abs(float(existing.get("time", 0)) - when) < 45
                ):
                    return existing
            self._events.append(event)
            self._events = self._events[-400:]
        if self._vk is not None:
            try:
                self._vk.rpush(self.KEY, json.dumps(event))
                self._vk.ltrim(self.KEY, -400, -1)
            except Exception as exc:
                logger.debug("Could not store deploy marker: %s", exc)
        logger.info("Graph marker %s %s at %s", kind, target, event["tag"])
        return event

    def ingest_starts(self, starts: list[tuple[str, float]]) -> None:
        """Record container start times. A tight burst becomes one deploy marker."""
        if not starts:
            return
        ordered = sorted(starts, key=lambda item: item[1])
        span = ordered[-1][1] - ordered[0][1]
        if len(ordered) >= 3 and span <= 180:
            self.record("deploy", "stack", "deploy", when=ordered[0][1])
            return
        for lang, started in ordered:
            self.record("restart", lang, f"restart {lang}", when=started)

    def for_range(self, start_time: float, end_time: float, service: str = "all") -> list[dict]:
        with self._lock:
            events = []
            for event in self._events:
                stamp = float(event.get("time", 0))
                if stamp < start_time or stamp > end_time:
                    continue
                target = event.get("target", "stack")
                if service not in ("all", "", None) and target not in (service, "stack"):
                    continue
                events.append(dict(event))
            return sorted(events, key=lambda item: item["time"])


deploy_markers = DeployMarkers()

# ---------------------------------------------------------------------------
# Thread-safe Canary State & Dynamic TPS Override
# ---------------------------------------------------------------------------
class CanaryState:
    def __init__(self):
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.status = "Initializing"
        self.base_tps = CANARY_TPS
        self.override_tps = None
        self.tps_override_end = None
        self.total_requests = 0
        self.success_count = 0
        self.error_count = 0
        self.active_apps = {
            "python": True,
            "java":   True,
            "rust":   True,
            "node":   True,
            "go":     True,
            "dotnet": True,
            "c":      True,
        }
        self.apps = {
            "python": {"url": APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "java":   {"url": JAVA_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "rust":   {"url": RUST_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "node":   {"url": NODE_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "go":     {"url": GO_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "dotnet": {"url": DOTNET_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "c":      {"url": C_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
        }
        self.endpoints = {}
        self.recent_requests = deque(maxlen=30)
        self.durations_window = deque(maxlen=1000)
        self.last_target = ""
        self.ts_buckets = {}
        self.last_external_host = ""

    def is_app_active(self, lang: str) -> bool:
        with self._lock:
            return self.active_apps.get(lang.lower(), True)

    def set_app_active(self, lang: str, active: bool) -> None:
        with self._lock:
            l = lang.lower()
            self.active_apps[l] = bool(active)
            if not active and l in self.apps:
                self.apps[l]["reachable"] = False

    def get_effective_tps(self) -> float:
        with self._lock:
            now = time.time()
            if self.override_tps is not None and self.tps_override_end is not None:
                if now >= self.tps_override_end:
                    self.override_tps = None
                    self.tps_override_end = None
                    return self.base_tps
                return self.override_tps
            elif self.override_tps is not None:
                return self.override_tps
            return self.base_tps

    def set_tps_override(self, tps: float, duration_sec: int = 60) -> dict:
        with self._lock:
            now = time.time()
            self.override_tps = max(0.5, min(100.0, float(tps)))
            self.tps_override_end = (now + duration_sec) if duration_sec > 0 else None
            logger.info("Canary TPS Override activated: %.1f TPS for %d seconds", self.override_tps, duration_sec)
            return {
                "base_tps": self.base_tps,
                "effective_tps": self.override_tps,
                "duration_sec": duration_sec,
                "remaining_sec": duration_sec if duration_sec > 0 else None
            }

    def clear_tps_override(self) -> dict:
        with self._lock:
            self.override_tps = None
            self.tps_override_end = None
            logger.info("Canary TPS Override cleared, restored base TPS %.1f", self.base_tps)
            return {"effective_tps": self.base_tps}

    def mark_reachable(self, lang: str) -> None:
        with self._lock:
            l = lang.lower()
            if l in self.apps:
                self.apps[l]["reachable"] = True

    def mark_unreachable(self, lang: str) -> None:
        with self._lock:
            l = lang.lower()
            if l in self.apps:
                self.apps[l]["reachable"] = False

    def is_reachable(self, lang: str) -> bool:
        with self._lock:
            return self.apps.get(lang.lower(), {}).get("reachable", False)

    def set_running(self) -> None:
        with self._lock:
            self.status = "Running"

    def record_request(self, lang: str, method: str, path: str, status: str, duration: float, fault_tag: str | None = None) -> None:
        with self._lock:
            now = time.time()
            self.total_requests += 1
            is_success = (status == "success")
            if is_success:
                self.success_count += 1
            else:
                self.error_count += 1

            if lang in self.apps:
                self.apps[lang]["total"] += 1
                self.apps[lang]["duration_sum"] += duration
                if is_success:
                    self.apps[lang]["success"] += 1
                else:
                    self.apps[lang]["error"] += 1

            ep_key = f"{method} {path.split('?')[0]}"
            if ep_key not in self.endpoints:
                self.endpoints[ep_key] = {"hits": 0, "errors": 0, "duration_sum": 0.0}
            self.endpoints[ep_key]["hits"] += 1
            self.endpoints[ep_key]["duration_sum"] += duration
            if not is_success:
                self.endpoints[ep_key]["errors"] += 1

            self.durations_window.append(duration)
            self.last_target = f"{lang} {method} {path}"

            bucket_ts = int(now // 5) * 5
            if bucket_ts not in self.ts_buckets:
                self.ts_buckets[bucket_ts] = {
                    "total": 0, "success": 0, "error": 0, "dur_sum": 0.0, "dur_cnt": 0,
                    "durs": [],
                    "apps": {"python": {"t": 0, "s": 0, "e": 0, "ds": 0.0, "durs": []},
                             "java":   {"t": 0, "s": 0, "e": 0, "ds": 0.0, "durs": []},
                             "rust":   {"t": 0, "s": 0, "e": 0, "ds": 0.0, "durs": []},
                             "node":   {"t": 0, "s": 0, "e": 0, "ds": 0.0, "durs": []},
                             "go":     {"t": 0, "s": 0, "e": 0, "ds": 0.0, "durs": []},
                             "dotnet": {"t": 0, "s": 0, "e": 0, "ds": 0.0, "durs": []},
                             "c":      {"t": 0, "s": 0, "e": 0, "ds": 0.0, "durs": []}}
                }
            b = self.ts_buckets[bucket_ts]
            b["total"] += 1
            b["dur_sum"] += duration
            b["dur_cnt"] += 1
            dur_ms = round(duration * 1000.0, 2)
            b.setdefault("durs", []).append(dur_ms)
            if is_success:
                b["success"] += 1
            else:
                b["error"] += 1

            if lang in b["apps"]:
                ba = b["apps"][lang]
                ba["t"] += 1
                ba["ds"] += duration
                ba.setdefault("durs", []).append(dur_ms)
                if is_success:
                    ba["s"] += 1
                else:
                    ba["e"] += 1

            cutoff = now - 3600
            for old_k in [k for k in self.ts_buckets if k < cutoff]:
                del self.ts_buckets[old_k]

            wall_ns = time.time_ns()
            secs, nanos = divmod(wall_ns, 1_000_000_000)
            clock = time.strftime("%H:%M:%S", time.localtime(secs))
            self.recent_requests.append({
                "time": f"{clock}.{nanos:09d}",
                "timestamp": now,
                "lang": lang,
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": round(duration * 1000, 1),
                "fault_tag": fault_tag
            })

    def get_snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            uptime = max(0.0, now - self.start_time)
            sorted_durs = sorted(self.durations_window) if self.durations_window else [0.0]
            n = len(sorted_durs)
            p50 = sorted_durs[int(n * 0.50)] * 1000
            p90 = sorted_durs[min(n - 1, int(n * 0.90))] * 1000
            p95 = sorted_durs[min(n - 1, int(n * 0.95))] * 1000
            p99 = sorted_durs[min(n - 1, int(n * 0.99))] * 1000
            avg_lat = (sum(sorted_durs) / n * 1000) if n else 0.0

            eff_tps = self.base_tps
            is_override = False
            override_rem = None
            if self.override_tps is not None:
                if self.tps_override_end is not None:
                    if now < self.tps_override_end:
                        eff_tps = self.override_tps
                        is_override = True
                        override_rem = max(0, int(self.tps_override_end - now))
                    else:
                        self.override_tps = None
                        self.tps_override_end = None
                else:
                    eff_tps = self.override_tps
                    is_override = True

            app_stats = {}
            for k, v in self.apps.items():
                app_total = v["total"]
                app_succ = v["success"]
                app_err = v["error"]
                app_avg_lat = (v["duration_sum"] / app_total * 1000) if app_total > 0 else 0.0
                app_avail = (app_succ / app_total * 100.0) if app_total > 0 else 100.0
                app_stats[k] = {
                    **v,
                    "is_active_workload": self.active_apps.get(k, True),
                    "avg_latency_ms": round(app_avg_lat, 1),
                    "availability_pct": round(app_avail, 2)
                }

            endpoint_list = []
            for ep, d in self.endpoints.items():
                h = d["hits"]
                e = d["errors"]
                endpoint_list.append({
                    "endpoint": ep,
                    "hits": h,
                    "errors": e,
                    "availability_pct": round((h - e) / h * 100.0, 1) if h else 100.0,
                    "avg_latency_ms": round((d["duration_sum"] / h * 1000), 1) if h else 0.0
                })
            endpoint_list.sort(key=lambda x: x["hits"], reverse=True)

            return {
                "status": self.status,
                "base_tps": self.base_tps,
                "effective_tps": eff_tps,
                "is_tps_override": is_override,
                "tps_remaining_sec": override_rem,
                "uptime_seconds": round(uptime, 1),
                "total_requests": self.total_requests,
                "success_count": self.success_count,
                "error_count": self.error_count,
                "availability_pct": round((self.success_count / self.total_requests * 100.0), 2) if self.total_requests > 0 else 100.0,
                "latency_p50_ms": round(p50, 1),
                "latency_p90_ms": round(p90, 1),
                "latency_p95_ms": round(p95, 1),
                "latency_p99_ms": round(p99, 1),
                "avg_latency_ms": round(avg_lat, 1),
                "active_apps": dict(self.active_apps),
                "apps": app_stats,
                "top_endpoints": endpoint_list[:10],
                "recent_requests": list(self.recent_requests),
                "last_target": self.last_target,
                "fault_snapshot": fault_manager.get_snapshot()
            }

    def get_inmemory_trends(self, seconds: int, service: str = "all") -> dict:
        with self._lock:
            now = time.time()
            start_ts = now - seconds
            pts_throughput = []
            pts_errors = []
            pts_availability = []
            pts_latency = []
            pts_p50 = []
            pts_p90 = []
            pts_p95 = []
            pts_p99 = []
            pts_p100 = []

            start_bucket = int(start_ts // 5) * 5
            end_bucket = int(now // 5) * 5

            for b_ts in range(start_bucket, end_bucket + 1, 5):
                b = self.ts_buckets.get(b_ts)
                if not b:
                    pts_throughput.append({"t": b_ts, "v": 0.0})
                    pts_errors.append({"t": b_ts, "v": 0.0})
                    pts_availability.append({"t": b_ts, "v": 100.0})
                    pts_latency.append({"t": b_ts, "v": 0.0})
                    pts_p50.append({"t": b_ts, "v": 0.0})
                    pts_p90.append({"t": b_ts, "v": 0.0})
                    pts_p95.append({"t": b_ts, "v": 0.0})
                    pts_p99.append({"t": b_ts, "v": 0.0})
                    pts_p100.append({"t": b_ts, "v": 0.0})
                    continue

                if service in ("python", "java", "rust", "node", "go", "dotnet", "c"):
                    ba = b["apps"].get(service, {"t": 0, "s": 0, "e": 0, "ds": 0.0, "durs": []})
                    tot = ba["t"]
                    succ = ba["s"]
                    err = ba["e"]
                    dur_s = ba["ds"]
                    durs = ba.get("durs", [])
                else:
                    tot = b["total"]
                    succ = b["success"]
                    err = b["error"]
                    dur_s = b["dur_sum"]
                    durs = b.get("durs", [])

                tps = round(tot / 5.0, 2)
                err_rate = round(err / 5.0, 2)
                avail = round((succ / tot * 100.0), 2) if tot > 0 else 100.0
                avg_l = round((dur_s / tot * 1000.0), 1) if tot > 0 else 0.0

                if durs:
                    s_durs = sorted(durs)
                    n_d = len(s_durs)
                    p50_v = round(s_durs[int(n_d * 0.50)], 1)
                    p90_v = round(s_durs[min(n_d - 1, int(n_d * 0.90))], 1)
                    p95_v = round(s_durs[min(n_d - 1, int(n_d * 0.95))], 1)
                    p99_v = round(s_durs[min(n_d - 1, int(n_d * 0.99))], 1)
                    p100_v = round(s_durs[-1], 1)
                else:
                    p50_v = p90_v = p95_v = p99_v = p100_v = avg_l

                pts_throughput.append({"t": b_ts, "v": tps})
                pts_errors.append({"t": b_ts, "v": err_rate})
                pts_availability.append({"t": b_ts, "v": avail})
                pts_latency.append({"t": b_ts, "v": avg_l})
                pts_p50.append({"t": b_ts, "v": p50_v})
                pts_p90.append({"t": b_ts, "v": p90_v})
                pts_p95.append({"t": b_ts, "v": p95_v})
                pts_p99.append({"t": b_ts, "v": p99_v})
                pts_p100.append({"t": b_ts, "v": p100_v})

            return {
                "throughput": pts_throughput,
                "errors": pts_errors,
                "availability": pts_availability,
                "latency_ms": pts_latency,
                "latency_p50": pts_p50,
                "latency_p90": pts_p90,
                "latency_p95": pts_p95,
                "latency_p99": pts_p99,
                "latency_p100": pts_p100
            }

state = CanaryState()

def start_docker_sync_thread() -> None:
    last_seen: dict[str, tuple] = {}

    def sync_loop():
        while True:
            try:
                statuses = docker_manager.get_containers_status()
                fresh_starts = []
                for lang, info in statuses.items():
                    is_run = info.get("is_running", False)
                    cid = info.get("id")
                    if not is_run:
                        if state.is_app_active(lang):
                            state.set_app_active(lang, False)
                            state.mark_unreachable(lang)
                    else:
                        if not state.is_app_active(lang):
                            state.set_app_active(lang, True)
                    signature = (cid, is_run)
                    if is_run and cid not in ("unknown", "not_found", "", None) and last_seen.get(lang) != signature:
                        started = docker_manager.container_started_at(cid)
                        if started:
                            fresh_starts.append((lang, started))
                    last_seen[lang] = signature
                deploy_markers.ingest_starts(fresh_starts)
            except Exception as e:
                logger.debug("Docker sync background error: %s", e)
            time.sleep(2.0)
    t = threading.Thread(target=sync_loop, daemon=True)
    t.start()

def get_detected_host_ip() -> str:
    """Detects the host machine's reachable LAN IP address for remote/mobile client access."""
    env_ip = os.getenv("CANARY_HOST_IP") or os.getenv("HOST_IP")
    if env_ip and env_ip.strip():
        return env_ip.strip()
    if state.last_external_host:
        return state.last_external_host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "localhost"

# ---------------------------------------------------------------------------
# Mimir Historical Trend Querier with In-Memory Merging
# ---------------------------------------------------------------------------
def query_trend_metrics(range_str: str = "5m", service: str = "all") -> dict:
    now = time.time()
    range_configs = {
        "5m":  {"seconds": 300,     "step": "5s",   "rate_win": "30s"},
        "30m": {"seconds": 1800,    "step": "15s",  "rate_win": "1m"},
        "1h":  {"seconds": 3600,    "step": "30s",  "rate_win": "2m"},
        "6h":  {"seconds": 21600,   "step": "2m",   "rate_win": "5m"},
        "1d":  {"seconds": 86400,   "step": "10m",  "rate_win": "15m"},
        "24h": {"seconds": 86400,   "step": "10m",  "rate_win": "15m"},
        "30d": {"seconds": 2592000, "step": "2h",   "rate_win": "6h"},
        "1mo": {"seconds": 2592000, "step": "2h",   "rate_win": "6h"}
    }
    cfg = range_configs.get(range_str, range_configs["5m"])
    seconds = cfg["seconds"]
    step = cfg["step"]
    rate_win = cfg["rate_win"]
    start = now - seconds

    service_filter = f'language="{service}",' if service in ("python", "java", "rust", "node", "go", "dotnet", "c") else ""

    queries = {
        "throughput": f'sum(rate(app_synthetic_requests_total{{{service_filter}}}[{rate_win}])) or vector(0)',
        "errors": f'sum(rate(app_synthetic_requests_total{{{service_filter}status="error"}}[{rate_win}])) or vector(0)',
        "availability": f'clamp_max(clamp_min((sum(rate(app_synthetic_requests_total{{{service_filter}status="success"}}[{rate_win}])) / sum(rate(app_synthetic_requests_total{{{service_filter}}}[{rate_win}]))) * 100, 0), 100) or vector(100)',
        "latency_ms": f'(sum(rate(app_synthetic_request_duration_sum{{{service_filter}}}[{rate_win}])) / sum(rate(app_synthetic_request_duration_count{{{service_filter}}}[{rate_win}]))) * 1000 or vector(0)',
        "latency_p50": f'histogram_quantile(0.50, sum(rate(app_synthetic_request_duration_bucket{{{service_filter}}}[{rate_win}])) by (le)) or vector(0)',
        "latency_p90": f'histogram_quantile(0.90, sum(rate(app_synthetic_request_duration_bucket{{{service_filter}}}[{rate_win}])) by (le)) or vector(0)',
        "latency_p95": f'histogram_quantile(0.95, sum(rate(app_synthetic_request_duration_bucket{{{service_filter}}}[{rate_win}])) by (le)) or vector(0)',
        "latency_p99": f'histogram_quantile(0.99, sum(rate(app_synthetic_request_duration_bucket{{{service_filter}}}[{rate_win}])) by (le)) or vector(0)',
        "latency_p100": f'histogram_quantile(1.0, sum(rate(app_synthetic_request_duration_bucket{{{service_filter}}}[{rate_win}])) by (le)) or vector(0)'
    }

    results = {}
    mimir_success = False

    try:
        session = requests.Session()
        for k, q in queries.items():
            r = session.get(f"{MIMIR_URL}/prometheus/api/v1/query_range", params={
                "query": q,
                "start": start,
                "end": now,
                "step": step
            }, timeout=3.5)
            if r.status_code == 200:
                matrix = r.json().get("data", {}).get("result", [])
                vals = matrix[0]["values"] if matrix else []
                pts = []
                for item in vals:
                    try:
                        v = float(item[1])
                        if not math.isnan(v) and not math.isinf(v):
                            pts.append({"t": int(item[0]), "v": round(v, 2)})
                    except Exception:
                        pass
                results[k] = pts
                mimir_success = True
            else:
                results[k] = []
    except Exception as exc:
        logger.debug("Mimir query error: %s", exc)
        mimir_success = False

    trend_keys = ("throughput", "errors", "availability", "latency_ms", "latency_p50", "latency_p90", "latency_p95", "latency_p99", "latency_p100")
    if seconds <= 1800:
        # High-resolution real-time responsiveness for 5m and 30m views
        mem_data = state.get_inmemory_trends(seconds, service)
        for k in trend_keys:
            if mem_data.get(k):
                results[k] = mem_data[k]
    elif not mimir_success or (seconds <= 3600 and (not results.get("throughput") or len(results["throughput"]) < 5)):
        mem_data = state.get_inmemory_trends(seconds, service)
        for k in trend_keys:
            if not results.get(k) or len(results[k]) < len(mem_data.get(k, [])):
                results[k] = mem_data.get(k, [])

    fault_events = fault_manager.get_events_for_range(start, now)
    markers = deploy_markers.for_range(start, now, service)

    return {
        "range": range_str,
        "service": service,
        "start_time": start,
        "end_time": now,
        "step": step,
        "metrics": results,
        "fault_events": fault_events,
        "deploy_markers": markers
    }

# ---------------------------------------------------------------------------
# Chaos Crash Invocation (Thread / Process Crash)
# ---------------------------------------------------------------------------
def trigger_target_crash(target: str, crash_type: str = "process", tag: str = None) -> dict:
    app_urls = {
        "python": APP_BASE_URL,
        "java": JAVA_APP_BASE_URL,
        "rust": RUST_APP_BASE_URL,
        "node": NODE_APP_BASE_URL,
        "go": GO_APP_BASE_URL,
        "dotnet": DOTNET_APP_BASE_URL,
        "c": C_APP_BASE_URL,
    }
    tgt_clean = (target or "java").lower().strip()
    crash_type_clean = "thread" if (crash_type or "").lower().strip() == "thread" else "process"
    targets = [tgt_clean] if tgt_clean in app_urls else list(app_urls.keys())

    clean_tag = tag.strip() if tag and tag.strip() else f"CRASH-{tgt_clean.upper()}-{crash_type_clean.upper()}"

    logger.warning("Triggering %s crash on target environment %s (tag: %s)",
                   crash_type_clean, tgt_clean, clean_tag)

    results = []
    for tgt in targets:
        base = app_urls[tgt]
        url = f"{base}/crash?type={crash_type_clean}"
        try:
            resp = requests.post(url, json={"type": crash_type_clean, "tag": clean_tag}, timeout=2.0)
            status_code = resp.status_code
            try:
                resp_data = resp.json()
            except Exception:
                resp_data = resp.text
            results.append({
                "target": tgt,
                "url": url,
                "status_code": status_code,
                "response": resp_data
            })
        except Exception as exc:
            results.append({
                "target": tgt,
                "url": url,
                "status": "connection_terminated",
                "message": f"Connection reset / process terminated: {exc}"
            })

    fault_manager.start_fault(
        tag=clean_tag,
        fault_type=f"{crash_type_clean}_crash",
        target=tgt_clean if tgt_clean in app_urls else "all",
        duration_sec=30
    )

    return {
        "status": "success",
        "tag": clean_tag,
        "crash_type": crash_type_clean,
        "target": tgt_clean,
        "results": results
    }

# ---------------------------------------------------------------------------
# Public showcase URL and same-origin service proxy
# A TryCloudflare quick tunnel publishes only the canary. Other ports on that
# hostname do not reach Mimir, Grafana, and the rest, so those links stay on
# this origin and are proxied to the service inside the compose network.
# ---------------------------------------------------------------------------
OPEN_UPSTREAMS = {
    "postgres": "http://postgres-exporter:9187",
    "valkey": "http://redis-exporter:9121",
    "otel-collector": "http://otel-collector:55679",
    "tempo": "http://tempo:3200",
    "loki": "http://loki:3100",
    "mimir": "http://mimir:9009",
    "prometheus": "http://prometheus:9090",
    "grafana": "http://grafana:3000",
    "redpanda": "http://redpanda:9644",
}
_PROXY_DROP_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "upgrade",
}


def read_showcase_url() -> str:
    path = os.getenv("SHOWCASE_URL_FILE", "/app/.showcase/url")
    try:
        line = open(path, encoding="utf-8").read().strip().splitlines()[0].strip()
    except Exception:
        return ""
    parsed = urllib.parse.urlparse(line)
    if parsed.scheme == "https" and parsed.netloc and " " not in line and '"' not in line:
        return line.rstrip("/")
    return ""


def _showcase_footer_html() -> str:
    url = read_showcase_url()
    if not url:
        return ""
    safe = html.escape(url, quote=True)
    return f'<a href="{safe}" target="_blank" rel="noopener noreferrer">Public showcase: {safe}</a> &bull; '


def _prefix_absolute_urls(text: str, prefix: str) -> str:
    for attr in ("href", "src", "action"):
        text = text.replace(f'{attr}="/', f'{attr}="{prefix}/')
        text = text.replace(f"{attr}='/", f"{attr}='{prefix}/")
    return text.replace("url(/", f"url({prefix}/")


def proxy_open_request(method: str, target: str, body: bytes, req_headers) -> tuple[int, list[tuple[str, str]], bytes]:
    """Forward /open/<service>/... to that service. Returns status, headers, body."""
    parsed = urllib.parse.urlparse(target)
    pieces = [part for part in parsed.path.split("/") if part]
    if len(pieces) < 2 or pieces[0] != "open" or pieces[1] not in OPEN_UPSTREAMS:
        payload = b'{"error":"unknown service"}'
        return 404, [("Content-Type", "application/json")], payload
    service = pieces[1]
    rest = "/" + "/".join(pieces[2:])
    if parsed.path.endswith("/") and not rest.endswith("/"):
        rest += "/"
    if len(pieces) == 2 and not parsed.path.endswith("/"):
        location = f"/open/{service}/"
        if parsed.query:
            location += "?" + parsed.query
        return 302, [("Location", location)], b""
    upstream = OPEN_UPSTREAMS[service] + rest
    if parsed.query:
        upstream += "?" + parsed.query
    forwarded = {}
    for key, value in req_headers.items():
        if key.lower() in ("host", "content-length", "connection", "transfer-encoding"):
            continue
        forwarded[key] = value
    try:
        upstream_resp = requests.request(
            method, upstream, data=body or None, headers=forwarded,
            timeout=20, allow_redirects=False,
        )
    except Exception as exc:
        payload = json.dumps({"error": f"upstream unavailable: {exc}"}).encode("utf-8")
        return 502, [("Content-Type", "application/json")], payload

    prefix = f"/open/{service}"
    content_type = upstream_resp.headers.get("Content-Type", "")
    raw = upstream_resp.content or b""
    lowered = content_type.lower()
    if any(kind in lowered for kind in ("text/html", "text/css", "javascript")) and len(raw) <= 8_000_000:
        text = raw.decode(upstream_resp.encoding or "utf-8", errors="replace")
        raw = _prefix_absolute_urls(text, prefix).encode("utf-8")
        content_type = content_type.split(";")[0] + "; charset=utf-8"

    headers = []
    for key, value in upstream_resp.headers.items():
        if key.lower() in _PROXY_DROP_HEADERS:
            continue
        if key.lower() == "location":
            if value.startswith("/"):
                value = prefix + value
            else:
                for base in (OPEN_UPSTREAMS[service],):
                    if value.startswith(base):
                        value = prefix + value[len(base):]
                        break
        if key.lower() == "content-type":
            value = content_type or value
        headers.append((key, value))
    return upstream_resp.status_code, headers, raw


# ---------------------------------------------------------------------------
# Compact, High-Efficiency HTML Dashboard Rendering
# ---------------------------------------------------------------------------
def render_dashboard_html() -> str:
    html_page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <noscript><meta http-equiv="refresh" content="5"></noscript>
  <title>Canary Load Generator & Fault Injection Dashboard</title>
  <style>
    :root {{
      --bg: #090e17;
      --card-bg: #151f30;
      --card-border: #24344d;
      --text: #f1f5f9;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --purple: #a855f7;
      /* Language-specific branding */
      --lang-java: #f97316;     /* Java: Orange */
      --lang-python: #3b82f6;   /* Python: Blue */
      --lang-rust: #d946ef;     /* Rust: Fuchsia */
      --lang-node: #22c55e;     /* Node: Emerald */
      --lang-go: #06b6d4;       /* Go: Cyan */
      --lang-dotnet: #8b5cf6;   /* .NET: Violet */
      --lang-c: #64748b;        /* C: Steel Slate */
      --lang-all: #ef4444;      /* All: Red */
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 12px 18px;
      line-height: 1.4;
      font-size: 0.85rem;
    }}
    .container {{ max-width: 1440px; margin: 0 auto; }}

    /* Compact Header */
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--card-border);
      flex-wrap: wrap;
      gap: 10px;
    }}
    .title-group {{ display: flex; align-items: center; gap: 10px; }}
    h1 {{ font-size: 1.25rem; font-weight: 700; display: flex; align-items: center; gap: 6px; letter-spacing: -0.02em; }}
    .pulse {{
      width: 9px; height: 9px; border-radius: 50%;
      background-color: var(--success);
      box-shadow: 0 0 8px var(--success);
      display: inline-block;
    }}
    .pulse.fault-active {{
      background-color: var(--danger);
      box-shadow: 0 0 12px var(--danger);
      animation: pulse-ring 1.2s infinite;
    }}
    @keyframes pulse-ring {{
      0% {{ transform: scale(0.9); opacity: 1; }}
      50% {{ transform: scale(1.4); opacity: 0.5; }}
      100% {{ transform: scale(0.9); opacity: 1; }}
    }}
    .header-controls {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}

    /* View Switcher Tabs */
    .view-tabs {{
      display: inline-flex;
      background: #101726;
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 2px;
      gap: 2px;
      flex-wrap: wrap;
    }}
    .view-tab-btn {{
      background: transparent;
      border: none;
      color: var(--muted);
      padding: 4px 10px;
      border-radius: 4px;
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s ease;
    }}
    .view-tab-btn.active {{
      background: var(--accent);
      color: #041226;
      box-shadow: 0 1px 2px rgba(0,0,0,0.3);
    }}

    /* Compact Active Fault Banner */
    .fault-banner {{
      display: none;
      border-radius: 6px;
      padding: 8px 14px;
      margin-bottom: 12px;
      align-items: center;
      justify-content: space-between;
      animation: flash-border 2s infinite alternate;
    }}
    .fault-banner.show {{ display: flex; }}
    @keyframes flash-border {{
      0% {{ box-shadow: 0 0 6px rgba(239,68,68,0.3); }}
      100% {{ box-shadow: 0 0 14px rgba(239,68,68,0.7); }}
    }}
    .fault-banner-title {{ font-weight: 700; font-size: 0.88rem; display: flex; align-items: center; gap: 8px; }}
    .fault-banner-tag {{
      padding: 2px 7px;
      border-radius: 4px;
      font-weight: 800;
      font-family: monospace;
      font-size: 0.8rem;
    }}

    /* Ultra-Compact KPI Row */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 10px;
      margin-bottom: 12px;
    }}
    @media (max-width: 900px) {{
      .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
    }}
    .kpi-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 8px 12px;
      position: relative;
    }}
    .kpi-title {{ font-size: 0.7rem; text-transform: uppercase; color: var(--muted); font-weight: 700; letter-spacing: 0.04em; }}
    .kpi-value {{ font-size: 1.35rem; font-weight: 800; margin: 2px 0; line-height: 1.1; }}
    .kpi-subtext {{ font-size: 0.72rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}

    /* Collapsible Controls Drawer */
    .collapsible-drawer {{
      background: #0f172a;
      border: 1px solid var(--card-border);
      border-radius: 6px;
      margin-bottom: 14px;
      overflow: hidden;
    }}
    .drawer-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 12px;
      background: #111a29;
      cursor: pointer;
      user-select: none;
      border-bottom: 1px solid transparent;
    }}
    .drawer-header:hover {{ background: #162236; }}
    .drawer-header.open {{ border-bottom: 1px solid var(--card-border); }}
    .drawer-title-group {{ display: flex; align-items: center; gap: 8px; }}
    .drawer-title {{ font-size: 0.85rem; font-weight: 700; display: flex; align-items: center; gap: 6px; }}
    .drawer-badges {{ display: flex; gap: 6px; align-items: center; }}

    /* Compact 4-Column Control Panels (Fault Injection + Crash Trigger + TPS + Container Power) */
    .controls-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      padding: 12px;
    }}
    @media (max-width: 1024px) {{
      .controls-grid {{ grid-template-columns: 1fr; }}
    }}
    .toast-notice {{
      position: fixed;
      top: 15px;
      right: 20px;
      background: #111a29;
      border: 1px solid var(--accent);
      color: #f1f5f9;
      padding: 9px 16px;
      border-radius: 6px;
      box-shadow: 0 6px 20px rgba(0,0,0,0.6);
      font-size: 0.8rem;
      font-weight: 600;
      z-index: 9999;
      display: none;
    }}
    .ctrl-box {{
      background: #111a29;
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 10px 14px;
    }}
    .ctrl-box-title {{
      font-size: 0.82rem;
      font-weight: 700;
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }}
    .form-inline-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)) 100px;
      gap: 8px;
      align-items: flex-end;
    }}
    .form-field {{ display: flex; flex-direction: column; gap: 3px; }}
    .form-field label {{ font-size: 0.68rem; font-weight: 700; color: var(--muted); text-transform: uppercase; }}
    .ctrl-input {{
      background: #182234;
      border: 1px solid var(--card-border);
      border-radius: 4px;
      color: var(--text);
      padding: 5px 8px;
      font-size: 0.8rem;
      outline: none;
      height: 30px;
    }}
    .ctrl-input:focus {{ border-color: var(--accent); }}

    /* Buttons */
    .btn {{
      padding: 5px 12px;
      border-radius: 4px;
      font-size: 0.78rem;
      font-weight: 700;
      cursor: pointer;
      border: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      height: 30px;
      transition: all 0.15s ease;
    }}
    .btn-danger {{ background: #dc2626; color: white; }}
    .btn-danger:hover {{ background: #ef4444; }}
    .btn-warning {{ background: #d97706; color: white; }}
    .btn-warning:hover {{ background: #f59e0b; }}
    .btn-accent {{ background: var(--accent); color: #041226; }}
    .btn-accent:hover {{ background: #7dd3fc; }}
    .btn-secondary {{ background: #24344d; color: var(--text); }}
    .btn-secondary:hover {{ background: #334668; }}
    .btn-outline {{ background: transparent; border: 1px solid var(--card-border); color: var(--muted); }}
    .btn-outline:hover {{ color: var(--text); border-color: var(--muted); }}
    .btn-sm {{ height: 24px; padding: 2px 7px; font-size: 0.72rem; }}

    /* Quick Preset Chips */
    .chips-row {{ display: flex; gap: 5px; margin-top: 8px; align-items: center; flex-wrap: wrap; }}
    .chip-btn {{
      background: #162236;
      border: 1px solid var(--card-border);
      color: var(--muted);
      border-radius: 12px;
      padding: 2px 8px;
      font-size: 0.7rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.1s ease;
    }}
    .chip-btn:hover {{ color: var(--text); border-color: var(--accent); background: #1c2b42; }}
    .chip-btn.chip-java:hover {{ border-color: var(--lang-java); color: var(--lang-java); }}
    .chip-btn.chip-python:hover {{ border-color: var(--lang-python); color: var(--lang-python); }}
    .chip-btn.chip-rust:hover {{ border-color: var(--lang-rust); color: var(--lang-rust); }}
    .chip-btn.chip-node:hover {{ border-color: var(--lang-node); color: var(--lang-node); }}
    .chip-btn.chip-go:hover {{ border-color: var(--lang-go); color: var(--lang-go); }}
    .chip-btn.chip-dotnet:hover {{ border-color: var(--lang-dotnet); color: var(--lang-dotnet); }}
    .chip-btn.chip-c:hover {{ border-color: var(--lang-c); color: var(--lang-c); }}

    /* Section Subheaders */
    .section-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin: 10px 0 8px;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .section-title {{ font-size: 0.95rem; font-weight: 700; display: flex; align-items: center; gap: 6px; }}

    /* Filter Toolbars */
    .btn-group {{
      display: inline-flex;
      background: #101726;
      border: 1px solid var(--card-border);
      border-radius: 4px;
      overflow: hidden;
    }}
    .filter-btn {{
      background: transparent;
      border: none;
      color: var(--muted);
      padding: 3px 9px;
      font-size: 0.74rem;
      font-weight: 600;
      cursor: pointer;
      border-right: 1px solid var(--card-border);
    }}
    .filter-btn:last-child {{ border-right: none; }}
    .filter-btn.active {{ background: #38bdf8; color: #041226; font-weight: 700; }}
    .filter-btn:hover:not(.active) {{ color: var(--text); background: rgba(255,255,255,0.05); }}

    /* Latency Percentile Picker */
    .percentile-picker {{
      display: inline-flex;
      background: #090e17;
      border: 1px solid var(--card-border);
      border-radius: 4px;
      overflow: hidden;
      margin-left: 6px;
      vertical-align: middle;
    }}
    .percentile-btn {{
      background: transparent;
      border: none;
      color: #94a3b8;
      padding: 1px 6px;
      font-size: 0.67rem;
      font-weight: 700;
      cursor: pointer;
      border-right: 1px solid var(--card-border);
      transition: all 0.15s ease;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .percentile-btn:last-child {{ border-right: none; }}
    .percentile-btn.active {{
      background: #38bdf8;
      color: #041226;
      font-weight: 800;
    }}
    .percentile-btn:hover:not(.active) {{
      color: #fff;
      background: rgba(56, 189, 248, 0.15);
    }}

    /* Compact 2x2 Charts Grid */
    .charts-grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      margin-bottom: 12px;
    }}
    @media (max-width: 900px) {{
      .charts-grid {{ grid-template-columns: 1fr; }}
    }}
    .chart-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 8px 12px;
      position: relative;
    }}
    .chart-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 4px;
    }}
    .chart-title {{ font-size: 0.8rem; font-weight: 700; display: flex; align-items: center; gap: 5px; }}
    .chart-current {{ font-size: 0.95rem; font-weight: 800; font-family: monospace; }}
    .canvas-container {{
      position: relative;
      width: 100%;
      height: 160px;
    }}
    canvas {{ width: 100% !important; height: 100% !important; display: block; }}

    /* Floating Tooltip */
    #chart-tooltip {{
      position: absolute;
      background: rgba(15, 23, 42, 0.96);
      border: 1px solid #475569;
      border-radius: 4px;
      padding: 5px 9px;
      font-size: 0.74rem;
      pointer-events: none;
      display: none;
      z-index: 100;
      box-shadow: 0 4px 12px rgba(0,0,0,0.6);
      white-space: nowrap;
    }}

    /* Compact Tables */
    .table-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      overflow: hidden;
      margin-bottom: 10px;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; }}
    th, td {{ padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--card-border); }}
    th {{
      background: #111a29;
      font-size: 0.68rem;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--muted);
      letter-spacing: 0.05em;
    }}
    tr:hover td {{ background: #1a273d; }}

    /* Badges */
    .badge {{
      display: inline-block;
      padding: 1px 6px;
      border-radius: 3px;
      font-size: 0.7rem;
      font-weight: 700;
    }}
    .badge-success {{ background: #064e3b; color: #34d399; }}
    .badge-error {{ background: #7f1d1d; color: #f87171; }}
    .badge-warning {{ background: #78350f; color: #fbbf24; }}
    .badge-secondary {{ background: #1e293b; color: #94a3b8; }}
    .badge-python {{ background: #1e3a8a; color: #93c5fd; }}
    .badge-java {{ background: #7c2d12; color: #fdba74; }}
    .badge-rust {{ background: #701a75; color: #f0abfc; }}
    .badge-node {{ background: #064e3b; color: #86efac; }}
    .badge-go   {{ background: #083344; color: #67e8f9; }}
    .badge-dotnet {{ background: #2e1065; color: #c4b5fd; }}
    .badge-c      {{ background: #1e293b; color: #94a3b8; }}

    /* Language-specific Fault Badges */
    .badge-fault-java   {{ background: #7c2d12; color: #fed7aa; border: 1px solid #f97316; }}
    .badge-fault-python {{ background: #1e3a8a; color: #bfdbfe; border: 1px solid #3b82f6; }}
    .badge-fault-rust   {{ background: #701a75; color: #f5d0fe; border: 1px solid #d946ef; }}
    .badge-fault-node   {{ background: #064e3b; color: #bbf7d0; border: 1px solid #22c55e; }}
    .badge-fault-go     {{ background: #083344; color: #a5f3fc; border: 1px solid #06b6d4; }}
    .badge-fault-dotnet {{ background: #2e1065; color: #ddd6fe; border: 1px solid #8b5cf6; }}
    .badge-fault-c      {{ background: #1e293b; color: #cbd5e1; border: 1px solid #64748b; }}
    .badge-fault-all    {{ background: #7f1d1d; color: #fecaca; border: 1px solid #ef4444; }}

    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.76rem;
      background: #0d1422;
      padding: 1px 4px;
      border-radius: 3px;
    }}

    .scroll-table-container {{
      max-height: 240px;
      overflow-y: auto;
    }}

    /* SSMS / pgAdmin Tabular Data Grid */
    .sql-editor-container {{
      background: #0f172a;
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 12px;
      margin-bottom: 12px;
    }}
    .sql-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .sql-editor-box {{
      position: relative;
      margin-bottom: 10px;
      border: 1px solid var(--card-border);
      border-radius: 4px;
      background: #090e17;
    }}
    .sql-editor-box:focus-within {{ border-color: var(--accent); }}
    .sql-highlight,
    .sql-textarea {{
      margin: 0;
      padding: 10px 12px 10px 12px;
      border: 0;
      width: 100%;
      min-height: 168px;
      box-sizing: border-box;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.84rem;
      line-height: 1.45;
      tab-size: 2;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .sql-highlight {{
      position: absolute;
      inset: 0;
      overflow: hidden;
      pointer-events: none;
      color: #e2e8f0;
      padding-right: 28px;
    }}
    .sql-textarea {{
      position: relative;
      display: block;
      resize: vertical;
      background: transparent;
      color: transparent;
      caret-color: #f8fafc;
      outline: none;
      overflow: auto;
      scrollbar-gutter: stable;
    }}
    .sql-textarea::placeholder {{ color: #64748b; }}
    .sql-kw {{ color: #7dd3fc; font-weight: 650; }}
    .sql-fn {{ color: #c4b5fd; }}
    .sql-str {{ color: #86efac; }}
    .sql-num {{ color: #fdba74; }}
    .sql-cmt {{ color: #64748b; font-style: italic; }}
    .sql-op {{ color: #f9a8d4; }}
    .sql-id {{ color: #e2e8f0; }}
    .sql-meta-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #111a29;
      border: 1px solid var(--card-border);
      padding: 5px 10px;
      border-radius: 4px;
      font-size: 0.74rem;
      color: var(--muted);
      margin-bottom: 8px;
    }}
    .sql-grid-wrapper {{
      max-height: 420px;
      overflow-x: auto;
      overflow-y: auto;
      border: 1px solid var(--card-border);
      border-radius: 4px;
      background: #0b111e;
      width: 100%;
      position: relative;
      scrollbar-width: thin;
      scrollbar-color: #3b82f6 #0f172a;
    }}
    .sql-grid-wrapper::-webkit-scrollbar {{
      height: 10px;
      width: 10px;
    }}
    .sql-grid-wrapper::-webkit-scrollbar-track {{
      background: #0b111e;
      border-radius: 4px;
    }}
    .sql-grid-wrapper::-webkit-scrollbar-thumb {{
      background: #2563eb;
      border-radius: 4px;
      border: 2px solid #0b111e;
    }}
    .sql-grid-wrapper::-webkit-scrollbar-thumb:hover {{
      background: #60a5fa;
    }}
    .sql-grid-table {{
      min-width: 100%;
      width: max-content;
      border-collapse: collapse;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.76rem;
      table-layout: auto;
    }}
    .sql-grid-table th {{
      position: sticky;
      top: 0;
      background: #152238;
      color: #94a3b8;
      border-right: 1px solid #24344d;
      border-bottom: 2px solid #334e77;
      padding: 6px 12px;
      cursor: pointer;
      user-select: none;
      white-space: nowrap;
      font-size: 0.72rem;
      z-index: 2;
    }}
    .sql-grid-table th:hover {{ background: #1c2e4c; color: var(--text); }}
    .sql-grid-table th:first-child {{
      position: sticky;
      left: 0;
      top: 0;
      z-index: 3;
      background: #152238;
    }}
    .sql-grid-table td {{
      padding: 6px 12px;
      border-bottom: 1px solid #1e2c42;
      border-right: 1px solid #182436;
      white-space: nowrap;
      max-width: 380px;
      min-width: 90px;
      overflow: hidden;
      text-overflow: ellipsis;
      cursor: cell;
      user-select: text;
    }}
    .sql-grid-table td:hover {{
      background: #1c2e4c;
      color: #fff;
    }}
    .sql-grid-table td.row-num {{
      background: #0e1726;
      color: #64748b;
      font-weight: 700;
      text-align: right;
      width: 42px;
      min-width: 42px;
      max-width: 42px;
      user-select: none;
      cursor: default;
      position: sticky;
      left: 0;
      z-index: 1;
    }}
    .sql-grid-table tr:hover td {{ background: #16243b; }}
    .sql-grid-table tr:hover td.row-num {{ background: #121c2e; }}
    .sql-null {{ color: #64748b; font-style: italic; }}
    .sql-grid-table.sql-resizable {{
      table-layout: fixed;
      width: max-content;
    }}
    .sql-grid-table.sql-resizable th,
    .sql-grid-table.sql-resizable td {{
      max-width: none;
      min-width: 0;
      position: relative;
    }}
    .col-resize {{
      position: absolute;
      top: 0;
      right: -3px;
      width: 8px;
      height: 100%;
      cursor: col-resize;
      z-index: 4;
    }}
    .col-resize:hover {{ background: rgba(96, 165, 250, 0.45); }}
    .sql-cell-tip {{
      position: fixed;
      z-index: 80;
      max-width: 440px;
      max-height: 220px;
      overflow: auto;
      padding: 8px 10px;
      background: #0b111e;
      color: #e2e8f0;
      border: 1px solid #334e77;
      border-radius: 6px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.72rem;
      line-height: 1.4;
      white-space: pre-wrap;
      word-break: break-word;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
      pointer-events: none;
    }}
    .plan-view-toggle {{ display: inline-flex; gap: 4px; }}
    .plan-view-toggle .active {{
      color: var(--text);
      border-color: #60a5fa;
      background: #152238;
    }}
    .plan-graph {{
      padding: 12px 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .plan-branch {{ display: flex; flex-direction: column; gap: 8px; }}
    .plan-card {{
      border: 1px solid #334e77;
      background: #121c2e;
      border-radius: 6px;
      padding: 8px 10px;
      max-width: 460px;
    }}
    .plan-card-type {{ font-weight: 700; color: #e2e8f0; }}
    .plan-card-rel {{ color: #93c5fd; margin-left: 6px; }}
    .plan-card-meta {{ color: #94a3b8; font-size: 0.72rem; margin-top: 3px; }}
    .plan-card-detail {{
      color: #cbd5e1;
      font-size: 0.72rem;
      margin-top: 4px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .plan-children {{
      margin-left: 18px;
      padding-left: 12px;
      border-left: 1px solid #334e77;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    #sql-plan-table tbody tr {{ cursor: pointer; }}
    #sql-plan-table tr.plan-selected td {{ background: #1e3a5f !important; }}
    .plan-card.plan-selected {{ border-color: #60a5fa; }}
    .plan-explain {{
      margin-top: 8px;
      padding: 10px 12px;
      border: 1px solid #334e77;
      border-radius: 4px;
      background: #111a29;
      color: #e2e8f0;
      font-size: 0.82rem;
      line-height: 1.5;
      white-space: pre-wrap;
    }}

    /* Cell Value Inspector Modal */
    .cell-modal-box {{
      max-width: 780px !important;
      width: 92% !important;
      max-height: 86vh;
      display: flex;
      flex-direction: column;
      text-align: left !important;
      background: #111a29 !important;
      border: 1px solid var(--accent) !important;
      border-radius: 8px;
      padding: 16px !important;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.95);
    }}
    .cell-modal-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 10px;
    }}
    .cell-modal-title {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 0.92rem;
    }}
    .cell-modal-toolbar {{
      display: flex;
      gap: 6px;
      margin-bottom: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .cell-modal-content-wrapper {{
      flex: 1;
      max-height: 58vh;
      overflow: auto;
      background: #080d16;
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 12px;
    }}
    .cell-modal-text {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.8rem;
      color: #7dd3fc;
      white-space: pre;
      line-height: 1.45;
      margin: 0;
      user-select: text;
    }}
    .cell-modal-text.wrapped {{
      white-space: pre-wrap !important;
      word-break: break-all;
    }}
    .cell-modal-footer {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 10px;
      padding-top: 8px;
      border-top: 1px solid var(--card-border);
    }}

    /* Dependencies Grid */
    .dep-cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .dep-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .dep-card-header {{ display: flex; justify-content: space-between; align-items: center; }}
    .dep-name {{ font-weight: 700; font-size: 0.88rem; display: flex; align-items: center; gap: 6px; }}
    .dep-role {{ font-size: 0.72rem; color: var(--muted); }}
    .dep-meta-row {{ display: flex; justify-content: space-between; font-size: 0.74rem; gap: 8px; }}
    a.dep-open {{
      color: #7dd3fc;
      font-weight: 700;
      text-decoration: underline;
      cursor: pointer;
      word-break: break-all;
    }}
    a.dep-open:hover {{ color: #e0f2fe; }}

    /* QR Code Modal */
    .modal-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.75);
      z-index: 1000;
      align-items: center;
      justify-content: center;
      backdrop-filter: blur(3px);
    }}
    .modal-overlay.open {{ display: flex; }}
    .modal-box {{
      background: #111a29;
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 20px;
      max-width: 420px;
      width: 90%;
      text-align: center;
      position: relative;
      box-shadow: 0 10px 30px rgba(0,0,0,0.8);
    }}
    .modal-close-btn {{
      position: absolute;
      top: 10px;
      right: 12px;
      background: transparent;
      border: none;
      color: var(--muted);
      font-size: 1.2rem;
      cursor: pointer;
    }}
    .qr-container {{
      background: white;
      padding: 14px;
      border-radius: 8px;
      display: inline-block;
      margin: 12px 0;
      box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    }}
    .qr-container svg, .qr-container img {{
      display: block;
      width: 180px;
      height: 180px;
    }}

    footer {{
      margin-top: 14px;
      padding-top: 8px;
      border-top: 1px solid var(--card-border);
      display: flex;
      justify-content: space-between;
      color: var(--muted);
      font-size: 0.74rem;
      flex-wrap: wrap;
      gap: 6px;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <div id="chart-tooltip"></div>
  <div id="toast-notice" class="toast-notice"></div>

  <!-- QR Code Modal -->
  <div id="qr-modal" class="modal-overlay">
    <div class="modal-box">
      <button class="modal-close-btn" onclick="closeQrModal()">&times;</button>
      <h3 style="font-size: 1.1rem; font-weight: 700; margin-bottom: 4px;">📱 Mobile Dashboard & Connect</h3>
      <p style="font-size: 0.75rem; color: var(--muted); margin-bottom: 8px;">
        Scan to connect Android, iOS, or Windows devices to the Canary Telemetry feed.
      </p>
      <div class="qr-container" id="qr-target">
        <img id="qr-img" src="/api/qrcode" alt="Canary QR Code" />
      </div>
      <div style="margin-top: 6px; display: flex; flex-direction: column; gap: 6px; text-align: left;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <label style="font-size: 0.7rem; font-weight: 700; color: var(--muted);">SERVER LAN HOST / IP:</label>
          <span id="qr-ip-detected-badge" class="badge badge-secondary" style="font-size:0.65rem; display:none;">Auto-detected IP</span>
        </div>
        <div style="display: flex; gap: 6px;">
          <input type="text" id="qr-host-input" class="ctrl-input" style="flex: 1;" placeholder="e.g. 192.168.1.100 or 10.0.0.x" oninput="updateQrUrl()" />
          <button class="btn btn-secondary" onclick="copyQrUrl()">📋 Copy</button>
        </div>
        <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:2px;">
          <button type="button" class="btn btn-outline btn-sm" id="qr-preset-lan-btn" style="display:none; font-size:0.68rem; padding:2px 8px;" onclick="setQrHostPreset(this.dataset.ip)">Use Detected IP</button>
          <button type="button" class="btn btn-outline btn-sm" id="qr-preset-current-btn" style="font-size:0.68rem; padding:2px 8px;" onclick="setQrHostPreset(window.location.hostname)">Use Current URL Host</button>
        </div>
        <div id="qr-full-url" style="font-family: monospace; font-size: 0.72rem; color: var(--accent); word-break: break-all;"></div>
      </div>
      <p style="font-size: 0.7rem; color: var(--muted); margin-top: 10px;">
        💡 Use the React Native app in <code style="color:var(--text);">mobile/</code> or open in mobile Safari / Chrome.
      </p>
    </div>
  </div>

  <!-- Cell Value Inspector Modal (Full-screen readable popup for cell text/JSON) -->
  <div id="cell-modal" class="modal-overlay" onclick="if(event.target===this) closeCellModal()">
    <div class="modal-box cell-modal-box">
      <div class="cell-modal-header">
        <div class="cell-modal-title">
          <span>🔍</span>
          <span id="cell-modal-col-name" style="font-weight: 700; color: var(--accent);">Column</span>
          <span id="cell-modal-row-badge" class="badge badge-secondary">Row #1</span>
          <span id="cell-modal-len-badge" class="badge badge-secondary" style="font-family: monospace;">0 chars</span>
        </div>
        <button class="modal-close-btn" onclick="closeCellModal()">&times;</button>
      </div>

      <div class="cell-modal-toolbar">
        <button class="btn btn-secondary btn-sm" onclick="copyCellValue()">📋 Copy Value</button>
        <button class="btn btn-secondary btn-sm" id="cell-format-json-btn" onclick="formatCellJson()">✨ Format JSON</button>
        <button class="btn btn-secondary btn-sm" onclick="wrapCellTextToggle()">↩️ Toggle Wrap</button>
        <span style="font-size:0.7rem; color:var(--muted); margin-left:auto;">Double-click any cell to inspect</span>
      </div>

      <div class="cell-modal-content-wrapper">
        <pre id="cell-modal-text" class="cell-modal-text wrapped"></pre>
      </div>

      <div class="cell-modal-footer">
        <span style="font-size: 0.72rem; color: var(--muted);">💡 Press <kbd style="background:#1e293b; padding:1px 5px; border-radius:3px; border:1px solid #334155;">Esc</kbd> to close.</span>
        <button class="btn btn-accent btn-sm" onclick="closeCellModal()">Close</button>
      </div>
    </div>
  </div>

  <div class="container">
    <header>
      <div class="title-group">
        <h1><span class="pulse" id="live-pulse"></span> Canary Load & Telemetry Control</h1>
        <span class="badge badge-success" id="overall-status-pill">RUNNING</span>
      </div>
      <div class="header-controls">
        <div class="view-tabs">
          <button class="view-tab-btn active" onclick="switchView('both', this)">📊 Both Views</button>
          <button class="view-tab-btn" onclick="switchView('graphs', this)">📈 Trends</button>
          <button class="view-tab-btn" onclick="switchView('raw', this)">🔢 Raw Numbers</button>
          <button class="view-tab-btn" onclick="switchView('dependencies', this)">🔌 Dependencies</button>
          <button class="view-tab-btn" onclick="switchView('sql', this)">💾 SQL Editor</button>
        </div>
        <button class="btn btn-outline" style="height:26px; padding:2px 8px; font-size:0.72rem;" onclick="openQrModal()" title="Mobile App & QR Code">📱 Connect / QR</button>
        <button class="btn btn-outline" style="height:26px; padding:2px 8px; font-size:0.72rem;" onclick="fetchTrendsAndRefresh()" title="Refresh now">🔄</button>
        <button class="btn btn-outline" style="height:26px; padding:2px 8px; font-size:0.72rem;" onclick="logoutCanary()" title="Sign out">Log out</button>
      </div>
    </header>

    <!-- Active Fault Warning Banner -->
    <div id="active-fault-banner" class="fault-banner">
      <div class="fault-banner-title">
        <span>🚨 ACTIVE FAULT DRILL:</span>
        <span class="fault-banner-tag" id="banner-tag"></span>
        <span id="banner-details" style="font-size: 0.78rem; font-weight: normal; color: #fecaca; margin-left: 6px;"></span>
      </div>
      <div style="display:flex; align-items:center; gap: 8px;">
        <span id="banner-timer" style="font-family: monospace; font-size: 0.85rem; font-weight: 700; color: #fde047;"></span>
        <button class="btn btn-warning" style="height:26px; padding:2px 10px; font-size:0.74rem;" onclick="stopActiveFault()">⏹️ Abort Drill</button>
      </div>
    </div>

    <!-- Ultra-Compact KPI Row -->
    <div id="kpi-section" class="kpi-grid">
      <div class="kpi-card" style="border-top: 2px solid var(--success);">
        <div class="kpi-title">Availability</div>
        <div class="kpi-value" id="kpi-avail" style="color: #10b981;">--%</div>
        <div class="kpi-subtext" id="kpi-avail-sub">Ratio: 0/0</div>
      </div>
      <div class="kpi-card" style="border-top: 2px solid var(--danger);">
        <div class="kpi-title">Total Errors</div>
        <div class="kpi-value" id="kpi-errors" style="color: #ef4444;">--</div>
        <div class="kpi-subtext" id="kpi-errors-sub">0.00 err/s</div>
      </div>
      <div class="kpi-card" style="border-top: 2px solid var(--accent);">
        <div class="kpi-title">Synthetic Requests</div>
        <div class="kpi-value" id="kpi-total">--</div>
        <div class="kpi-subtext" id="kpi-uptime">Uptime: --</div>
      </div>
      <div class="kpi-card" style="border-top: 2px solid var(--purple);">
        <div class="kpi-title">Latency (Avg / P95)</div>
        <div class="kpi-value" id="kpi-latency">-- ms</div>
        <div class="kpi-subtext" id="kpi-latency-p95">P95: -- ms</div>
      </div>
      <div class="kpi-card" style="border-top: 2px solid #38bdf8;">
        <div class="kpi-title">Canary Throughput</div>
        <div class="kpi-value" id="kpi-tps">-- TPS</div>
        <div class="kpi-subtext" id="kpi-tps-status">Target: 6 TPS</div>
      </div>
    </div>

    <!-- Expandable / Collapsible Controls Drawer (COLLAPSED BY DEFAULT FOR UNCLUTTERED VIEW) -->
    <div class="collapsible-drawer" id="controls-drawer">
      <div class="drawer-header" id="drawer-toggle-header" onclick="toggleControlsDrawer()">
        <div class="drawer-title-group">
          <span class="drawer-title">
            <span id="drawer-icon">▶</span> ⚙️ Chaos & Load Generation Controls (Fault Injection, Crashes, TPS Override & Container Power)
          </span>
        </div>
        <div class="drawer-badges">
          <span id="drawer-tps-chip" class="badge badge-success">6 TPS (Default)</span>
          <span id="drawer-fault-chip" class="badge badge-secondary">No Active Fault</span>
          <span id="drawer-power-chip" class="badge badge-secondary">7/7 Containers Active</span>
          <button class="btn btn-outline btn-sm" id="drawer-toggle-btn" style="pointer-events: none;">▶ Expand</button>
        </div>
      </div>

      <!-- Collapsible Body (Hidden by default) -->
      <div id="drawer-content" style="display: none;">
        <div class="controls-grid">
          <!-- 1. Fault Injection Testing Station -->
          <div class="ctrl-box">
            <div class="ctrl-box-title">
              <span>⚡ Fault Injection Testing (Color-Coded by Language)</span>
              <span style="font-size:0.7rem; color:var(--muted); font-weight:normal;">Tags events on graph trends</span>
            </div>
            <form id="fault-form" class="form-inline-grid" onsubmit="startFaultInjection(event)">
              <div class="form-field">
                <label>Event Tag Name</label>
                <input type="text" id="fault-tag-input" class="ctrl-input" placeholder="e.g. CHAOS-JAVA-ERR" required />
              </div>
              <div class="form-field">
                <label>Fault Mode</label>
                <select id="fault-type-select" class="ctrl-input">
                  <option value="error_spike">💥 Error Spike (5xx Storm)</option>
                  <option value="high_latency">⏱️ High Latency (+1000ms)</option>
                  <option value="service_outage">🛑 Service Outage (Drops)</option>
                  <option value="intermittent_errors">🎲 Intermittent (50% Err)</option>
                  <option value="process_crash">💥 Process Crash (Kill Process)</option>
                  <option value="thread_crash">🧵 Thread Crash (Worker Fault)</option>
                </select>
              </div>
              <div class="form-field">
                <label>Target Language</label>
                <select id="fault-target-select" class="ctrl-input">
                  <option value="all">🔴 All (Python/Java/Rust/Node/Go/.NET/C)</option>
                  <option value="java">🟠 Java (Spring Boot)</option>
                  <option value="python">🔵 Python (Flask)</option>
                  <option value="rust">🟣 Rust (Axum)</option>
                  <option value="node">🟢 Node.js (Express)</option>
                  <option value="go">🩵 Go (net/http)</option>
                  <option value="dotnet">💜 .NET (C#)</option>
                  <option value="c">⚙️ C (POSIX)</option>
                </select>
              </div>
              <div class="form-field">
                <label>Duration</label>
                <select id="fault-duration-select" class="ctrl-input">
                  <option value="30">30s</option>
                  <option value="60" selected>1 min</option>
                  <option value="120">2 min</option>
                  <option value="300">5 min</option>
                  <option value="0">Manual</option>
                </select>
              </div>
              <div>
                <button type="submit" class="btn btn-danger" style="width:100%;">⚡ Induce</button>
              </div>
            </form>
            <div class="chips-row">
              <span style="font-size:0.68rem; color:var(--muted); font-weight:700;">PRESETS:</span>
              <button class="chip-btn chip-java" onclick="quickDrill('JAVA-ERR-SPIKE', 'error_spike', 'java', 60)">🟠 Java Errors (60s)</button>
              <button class="chip-btn chip-python" onclick="quickDrill('PY-LATENCY-SURGE', 'high_latency', 'python', 60)">🔵 Py Latency (60s)</button>
              <button class="chip-btn chip-rust" onclick="quickDrill('RUST-CHAOS-DRILL', 'intermittent_errors', 'rust', 60)">🟣 Rust Chaos (60s)</button>
              <button class="chip-btn chip-node" onclick="quickDrill('NODE-ERR-SPIKE', 'error_spike', 'node', 60)">🟢 Node Errors (60s)</button>
              <button class="chip-btn chip-go" onclick="quickDrill('GO-ERR-SPIKE', 'error_spike', 'go', 60)">🩵 Go Errors (60s)</button>
              <button class="chip-btn chip-dotnet" onclick="quickDrill('DOTNET-ERR-SPIKE', 'error_spike', 'dotnet', 60)">💜 .NET Errors (60s)</button>
              <button class="chip-btn chip-c" onclick="quickDrill('C-ERR-SPIKE', 'error_spike', 'c', 60)">⚙️ C Errors (60s)</button>
              <button class="chip-btn" onclick="quickDrill('ALL-OUTAGE-DRILL', 'service_outage', 'all', 30)">🔴 Full Outage (30s)</button>
            </div>
          </div>

          <!-- 2. Target Crash Trigger Station -->
          <div class="ctrl-box">
            <div class="ctrl-box-title">
              <span>💥 Target Crash Injection (Process / Thread)</span>
              <span style="font-size:0.7rem; color:var(--muted); font-weight:normal;">Issues crash to target process</span>
            </div>
            <form id="crash-form" class="form-inline-grid" style="grid-template-columns: 1fr 1fr 1fr 95px;" onsubmit="submitCrashTrigger(event)">
              <div class="form-field">
                <label>Target Environment</label>
                <select id="crash-target-select" class="ctrl-input">
                  <option value="java">🟠 Java (Spring Boot)</option>
                  <option value="python">🔵 Python (Flask)</option>
                  <option value="rust">🟣 Rust (Axum)</option>
                  <option value="node">🟢 Node.js (Express)</option>
                  <option value="go">🩵 Go (net/http)</option>
                  <option value="dotnet">💜 .NET (C#)</option>
                  <option value="c">⚙️ C (POSIX)</option>
                  <option value="all">🔴 All Environments</option>
                </select>
              </div>
              <div class="form-field">
                <label>Crash Scope</label>
                <select id="crash-type-select" class="ctrl-input">
                  <option value="process">💥 Process Crash (Kill)</option>
                  <option value="thread">🧵 Thread Crash (Worker)</option>
                </select>
              </div>
              <div class="form-field">
                <label>Event Tag Name</label>
                <input type="text" id="crash-tag-input" class="ctrl-input" placeholder="e.g. CRASH-JAVA-PROC" />
              </div>
              <div>
                <button type="submit" class="btn btn-danger" style="width:100%; background:#b91c1c;">💥 Crash</button>
              </div>
            </form>
            <div class="chips-row">
              <span style="font-size:0.68rem; color:var(--muted); font-weight:700;">QUICK CRASH:</span>
              <button class="chip-btn chip-java" onclick="quickCrash('java', 'process', 'CRASH-JAVA-PROC')">🟠 Java Proc</button>
              <button class="chip-btn chip-java" onclick="quickCrash('java', 'thread', 'CRASH-JAVA-THREAD')">🟠 Java Thread</button>
              <button class="chip-btn chip-python" onclick="quickCrash('python', 'process', 'CRASH-PY-PROC')">🔵 Py Proc</button>
              <button class="chip-btn chip-rust" onclick="quickCrash('rust', 'process', 'CRASH-RUST-PROC')">🟣 Rust Proc</button>
              <button class="chip-btn chip-node" onclick="quickCrash('node', 'process', 'CRASH-NODE-PROC')">🟢 Node Proc</button>
              <button class="chip-btn chip-go" onclick="quickCrash('go', 'process', 'CRASH-GO-PROC')">🩵 Go Proc</button>
              <button class="chip-btn chip-dotnet" onclick="quickCrash('dotnet', 'process', 'CRASH-DOTNET-PROC')">💜 .NET Proc</button>
              <button class="chip-btn chip-c" onclick="quickCrash('c', 'process', 'CRASH-C-PROC')">⚙️ C Proc</button>
            </div>
          </div>

          <!-- 3. Dynamic TPS Load Controller -->
          <div class="ctrl-box">
            <div class="ctrl-box-title">
              <span>🚀 Load Rate Controller (TPS Override)</span>
              <span id="tps-active-badge" class="badge badge-success">DEFAULT: 6 TPS</span>
            </div>
            <form id="tps-form" class="form-inline-grid" style="grid-template-columns: 1fr 1fr 100px 70px;" onsubmit="applyTpsOverride(event)">
              <div class="form-field">
                <label>Target TPS Rate</label>
                <input type="number" id="tps-rate-input" class="ctrl-input" min="1" max="100" step="1" value="18" required />
              </div>
              <div class="form-field">
                <label>Override Duration</label>
                <select id="tps-duration-select" class="ctrl-input">
                  <option value="30">30s</option>
                  <option value="60" selected>1 min</option>
                  <option value="120">2 min</option>
                  <option value="300">5 min</option>
                  <option value="900">15 min</option>
                  <option value="0">Permanent</option>
                </select>
              </div>
              <div>
                <button type="submit" class="btn btn-accent" style="width:100%;">🚀 Set TPS</button>
              </div>
              <div>
                <button type="button" class="btn btn-secondary" style="width:100%;" onclick="resetTps()" title="Reset to base 6 TPS">Reset</button>
              </div>
            </form>
            <div class="chips-row">
              <span style="font-size:0.68rem; color:var(--muted); font-weight:700;">QUICK TPS:</span>
              <button class="chip-btn" onclick="quickTps(6, 0)">6 TPS (Default)</button>
              <button class="chip-btn" onclick="quickTps(12, 60)">12 TPS (2x, 60s)</button>
              <button class="chip-btn" onclick="quickTps(24, 60)">24 TPS (4x, 60s)</button>
              <button class="chip-btn" onclick="quickTps(48, 60)">48 TPS (8x, 60s)</button>
            </div>
          </div>

          <!-- 4. Container Power & Resource Manager -->
          <div class="ctrl-box">
            <div class="ctrl-box-title">
              <span>🔌 Container Power & Resource Manager</span>
              <span style="font-size:0.7rem; color:var(--muted);">Saves resources & adapts canary workload</span>
            </div>
            <div style="font-size:0.73rem; color:var(--muted); margin-bottom:8px;">
              Shutting down containers suspends synthetic canary traffic to avoid false failure spikes.
            </div>
            <div id="container-power-list" style="display:flex; flex-direction:column; gap:6px;">
              <div style="color:var(--muted); text-align:center; padding:10px;">Loading container states...</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Trend Graphs Section -->
    <div id="graphs-section">
      <div class="section-bar">
        <div class="section-title">
          <span>📈 Metric Trend Graphs</span>
          <span style="font-size:0.75rem; color:var(--muted); font-weight:normal;">(Availability, Errors, Latency, Throughput) · dashed lines mark deploys and container restarts</span>
        </div>
        <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
          <div class="btn-group">
            <span style="font-size:0.7rem; padding:3px 7px; color:var(--muted); background:#111a29; display:flex; align-items:center;">RANGE:</span>
            <button class="filter-btn active" onclick="setTimeRange('5m', this)">5 Min</button>
            <button class="filter-btn" onclick="setTimeRange('30m', this)">30 Min</button>
            <button class="filter-btn" onclick="setTimeRange('1h', this)">1 Hour</button>
            <button class="filter-btn" onclick="setTimeRange('6h', this)">6 Hours</button>
            <button class="filter-btn" onclick="setTimeRange('1d', this)">Past Day</button>
            <button class="filter-btn" onclick="setTimeRange('30d', this)">Past Month</button>
          </div>
          <div class="btn-group">
            <span style="font-size:0.7rem; padding:3px 7px; color:var(--muted); background:#111a29; display:flex; align-items:center;">FILTER:</span>
            <button class="filter-btn active" onclick="setServiceFilter('all', this)">All</button>
            <button class="filter-btn" onclick="setServiceFilter('python', this)">Python</button>
            <button class="filter-btn" onclick="setServiceFilter('java', this)">Java</button>
            <button class="filter-btn" onclick="setServiceFilter('rust', this)">Rust</button>
            <button class="filter-btn" onclick="setServiceFilter('node', this)">Node</button>
            <button class="filter-btn" onclick="setServiceFilter('go', this)">Go</button>
            <button class="filter-btn" onclick="setServiceFilter('dotnet', this)">.NET</button>
            <button class="filter-btn" onclick="setServiceFilter('c', this)">C</button>
          </div>
        </div>
      </div>

      <!-- 2x2 Charts Grid -->
      <div class="charts-grid">
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title"><span style="color:#10b981;">●</span> Availability Trend (%)</div>
            <div class="chart-current" id="cur-avail" style="color:#10b981;">--%</div>
          </div>
          <div class="canvas-container"><canvas id="chart-avail"></canvas></div>
        </div>

        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title"><span style="color:#ef4444;">●</span> Error Rate (Errors / sec)</div>
            <div class="chart-current" id="cur-errors" style="color:#ef4444;">-- err/s</div>
          </div>
          <div class="canvas-container"><canvas id="chart-errors"></canvas></div>
        </div>

        <div class="chart-card">
          <div class="chart-header" style="flex-wrap: wrap; gap: 4px;">
            <div style="display:flex; align-items:center; gap:4px; flex-wrap:wrap;">
              <div class="chart-title"><span style="color:#38bdf8;">●</span> <span id="lat-chart-title">Latency Trend (P95 ms)</span></div>
              <div class="percentile-picker" id="latency-percentile-picker">
                <button type="button" class="percentile-btn" data-p="p50" onclick="setLatencyPercentile('p50', this)" title="50th Percentile (Median Latency)">P50</button>
                <button type="button" class="percentile-btn" data-p="p90" onclick="setLatencyPercentile('p90', this)" title="90th Percentile Latency">P90</button>
                <button type="button" class="percentile-btn active" data-p="p95" onclick="setLatencyPercentile('p95', this)" title="95th Percentile Latency (Default)">P95</button>
                <button type="button" class="percentile-btn" data-p="p99" onclick="setLatencyPercentile('p99', this)" title="99th Percentile Latency">P99</button>
                <button type="button" class="percentile-btn" data-p="p100" onclick="setLatencyPercentile('p100', this)" title="100th Percentile (Max Latency)">P100</button>
              </div>
            </div>
            <div class="chart-current" id="cur-lat" style="color:#38bdf8;">-- ms</div>
          </div>
          <div class="canvas-container"><canvas id="chart-latency"></canvas></div>
        </div>

        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title"><span style="color:#a855f7;">●</span> Throughput Trend (TPS / sec)</div>
            <div class="chart-current" id="cur-tps" style="color:#a855f7;">-- req/s</div>
          </div>
          <div class="canvas-container"><canvas id="chart-tps"></canvas></div>
        </div>
      </div>
    </div>

    <!-- Current Raw Numbers Section -->
    <div id="raw-numbers-section">
      <div class="section-title" style="margin: 12px 0 6px;">🎯 Target Microservices — Current Raw Numbers</div>
      <div class="table-card">
        <table>
          <thead>
            <tr>
              <th>Service</th>
              <th>Endpoint URL</th>
              <th>Workload</th>
              <th>Container</th>
              <th style="text-align:right;">Requests</th>
              <th style="text-align:right;">Success</th>
              <th style="text-align:right;">Errors</th>
              <th style="text-align:right;">Availability</th>
              <th style="text-align:right;">Avg Latency</th>
            </tr>
          </thead>
          <tbody id="apps-table-body">
            <tr><td colspan="9" style="text-align:center; color:var(--muted);">Loading raw numbers...</td></tr>
          </tbody>
        </table>
      </div>

      <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px;">
        <div>
          <div class="section-title" style="margin-bottom: 6px;">📊 Endpoint Hit & Error Breakdown</div>
          <div class="table-card scroll-table-container">
            <table>
              <thead>
                <tr>
                  <th>Route</th>
                  <th style="text-align:right;">Hits</th>
                  <th style="text-align:right;">Errors</th>
                  <th style="text-align:right;">Avail %</th>
                  <th style="text-align:right;">Latency</th>
                </tr>
              </thead>
              <tbody id="endpoints-table-body">
                <tr><td colspan="5" style="text-align:center; color:var(--muted);">Loading endpoints...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <div>
          <div class="section-title" style="margin-bottom: 6px;">🏷️ Fault Injection Event Ledger</div>
          <div class="table-card scroll-table-container">
            <table>
              <thead>
                <tr>
                  <th>Tag</th>
                  <th>Target</th>
                  <th>Mode</th>
                  <th>Duration</th>
                  <th>Impact</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody id="fault-history-table-body">
                <tr><td colspan="6" style="text-align:center; color:var(--muted);">No fault drills logged yet.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Live Recent Activity Feed -->
      <div class="section-title" style="margin-bottom: 6px;">⚡ Live Synthetic Request Stream (Last 30 Requests)</div>
      <div class="table-card scroll-table-container">
        <table>
          <thead>
            <tr>
              <th style="white-space:nowrap;">Time</th>
              <th style="width: 75px;">App</th>
              <th style="width: 60px;">Method</th>
              <th>Endpoint Path</th>
              <th style="width: 70px;">Status</th>
              <th style="width: 90px; text-align:right;">Latency</th>
              <th>Fault Event Tag</th>
            </tr>
          </thead>
          <tbody id="recent-table-body">
            <tr><td colspan="7" style="text-align:center;">Waiting for requests...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Dependencies Health Section (Tabbed View) -->
    <div id="dependencies-section" style="display: none;">
      <div class="section-bar">
        <div class="section-title">
          <span>🔌 Stack Dependencies & Infrastructure Health</span>
          <span style="font-size:0.75rem; color:var(--muted); font-weight:normal;">(PostgreSQL, Valkey, OTel Collector, Tempo, Loki, Mimir, Prometheus, Grafana, Redpanda)</span>
        </div>
        <div>
          <button class="btn btn-outline btn-sm" onclick="fetchDependencies()">🔄 Re-probe Dependencies</button>
        </div>
      </div>

      <!-- Dependencies Summary Bar -->
      <div class="kpi-grid" style="grid-template-columns: repeat(4, 1fr); margin-bottom: 12px;">
        <div class="kpi-card" style="border-top: 2px solid var(--success);">
          <div class="kpi-title">Stack Availability</div>
          <div class="kpi-value" id="dep-overall-avail" style="color: #10b981;">100%</div>
          <div class="kpi-subtext" id="dep-overall-ratio">9 of 9 Dependencies Healthy</div>
        </div>
        <div class="kpi-card" style="border-top: 2px solid var(--accent);">
          <div class="kpi-title">Average Probe Latency</div>
          <div class="kpi-value" id="dep-avg-lat">-- ms</div>
          <div class="kpi-subtext">Direct network round-trip</div>
        </div>
        <div class="kpi-card" style="border-top: 2px solid var(--danger);">
          <div class="kpi-title">Observed Error Rate</div>
          <div class="kpi-value" id="dep-error-rate" style="color: #10b981;">0.0%</div>
          <div class="kpi-subtext">Infrastructure fault rate</div>
        </div>
        <div class="kpi-card" style="border-top: 2px solid var(--purple);">
          <div class="kpi-title">Telemetry Pipelines</div>
          <div class="kpi-value" style="color: #a855f7;">ACTIVE</div>
          <div class="kpi-subtext">OTLP gRPC &bull; Metrics &bull; Traces &bull; Logs</div>
        </div>
      </div>

      <!-- Dependency Cards Grid -->
      <div class="dep-cards-grid" id="dep-cards-container">
        <div style="color:var(--muted); text-align:center; grid-column: 1 / -1; padding:20px;">Probing stack dependencies...</div>
      </div>
    </div>

    <!-- SQL Query Editor Section (Tabbed View: SSMS / pgAdmin Style) -->
    <div id="sql-editor-section" style="display: none;">
      <div class="section-bar">
        <div class="section-title">
          <span>💾 PostgreSQL SQL Query Editor & Data Grid</span>
        </div>
        <div>
          <span class="badge badge-success">Read-only · audit_logs, sql_saved_queries, sql_query_audit</span>
        </div>
      </div>

      <div class="sql-editor-container">
        <div class="sql-toolbar">
          <div style="display:flex; align-items:center; gap:8px; flex:1; min-width:280px;">
            <label style="font-size:0.72rem; font-weight:700; color:var(--muted);">SAVED QUERIES:</label>
            <select id="sql-saved-select" class="ctrl-input" style="flex:1;" onchange="onSelectSavedQuery(this.value)">
              <option value="">-- Choose a curated saved query --</option>
            </select>
          </div>
          <div style="display:flex; align-items:center; gap:6px;">
            <input id="sql-save-name" class="ctrl-input" placeholder="Name to save" style="width:140px;">
            <button class="btn btn-outline" onclick="formatSqlEditor()">Format</button>
            <button class="btn btn-outline" onclick="saveSqlQuery()">Save</button>
            <button class="btn btn-accent" onclick="runSqlQuery()">▶ Run Query (Ctrl+Enter)</button>
            <button class="btn btn-secondary" onclick="clearSqlQuery()">🧹 Clear</button>
            <button class="btn btn-outline" onclick="exportSqlResults('csv')">📥 Export CSV</button>
            <button class="btn btn-outline" onclick="exportSqlResults('json')">📥 Export JSON</button>
          </div>
        </div>

        <div class="sql-editor-box" id="sql-editor-box">
          <pre class="sql-highlight" id="sql-highlight" aria-hidden="true"></pre>
          <textarea id="sql-query-input" class="sql-textarea" spellcheck="false" placeholder="Enter SQL query here... e.g. SELECT * FROM audit_logs ORDER BY id DESC LIMIT 50;"></textarea>
        </div>

        <div class="sql-meta-bar" id="sql-status-bar">
          <span id="sql-status-text">Ready &bull; Press Ctrl+Enter or click Run Query</span>
          <span id="sql-timing-text">-- ms</span>
        </div>

        <!-- First-Principles Data Grid -->
        <div class="sql-grid-wrapper" id="sql-grid-wrapper">
          <div id="sql-grid-empty" style="padding: 30px; text-align: center; color: var(--muted);">
            No query results to display. Select a saved query above or run your own SQL.
          </div>
          <table class="sql-grid-table sql-resizable" id="sql-grid-table" style="display: none;">
            <colgroup id="sql-grid-cols"></colgroup>
            <thead id="sql-grid-thead"></thead>
            <tbody id="sql-grid-tbody"></tbody>
          </table>
        </div>

        <div id="sql-plan-panel" style="display: none; margin-top: 12px;">
          <div class="sql-meta-bar">
            <span>Execution plan</span>
            <span style="display:inline-flex; align-items:center; gap:10px;">
              <span id="sql-plan-summary">--</span>
              <span class="plan-view-toggle">
                <button type="button" class="btn btn-outline btn-sm active" id="plan-view-grid" onclick="setPlanView('grid')">Grid</button>
                <button type="button" class="btn btn-outline btn-sm" id="plan-view-graph" onclick="setPlanView('graph')">Graph</button>
              </span>
            </span>
          </div>
          <div class="sql-grid-wrapper" id="sql-plan-wrapper">
            <table class="sql-grid-table sql-resizable" id="sql-plan-table">
              <colgroup id="sql-plan-cols"></colgroup>
              <thead id="sql-plan-thead"></thead>
              <tbody id="sql-plan-tbody"></tbody>
            </table>
          </div>
          <div class="sql-grid-wrapper" id="sql-plan-graph" style="display: none;"></div>
          <div id="sql-plan-explain" class="plan-explain" hidden>Click a row in the plan. The note here describes that step.</div>
        </div>
        <div id="sql-cell-tip" class="sql-cell-tip" hidden></div>
      </div>
    </div>

    <footer>
      <div>Canary Load Generator &bull; Polyglot Telemetry &bull; Color-Coded Fault Injection &bull; Dynamic TPS Override &bull; Container Power Control &bull; SQL Data Grid</div>
      <div>__SHOWCASE_FOOTER__<a href="/stats" target="_blank">JSON Snapshot</a> &bull; <a href="/api/trends?range=5m" target="_blank">Trends API</a> &bull; <a href="/api/dependencies" target="_blank">Dependencies API</a></div>
    </footer>
  </div>

  <script>
    let currentRange = '5m';
    let currentService = 'all';
    let currentView = 'both';
    let currentLatencyPercentile = 'p95';
    let latestTrendsData = null;
    let latestSqlResult = null;
    let controlsDrawerOpen = false;

    // Language color definitions
    const LANG_COLORS = {{
      'java':   {{ stroke: '#f97316', fill: 'rgba(249, 115, 22, 0.22)', badge: '#ea580c', border: '#f97316', text: '#fff' }},
      'python': {{ stroke: '#3b82f6', fill: 'rgba(59, 130, 246, 0.22)', badge: '#2563eb', border: '#3b82f6', text: '#fff' }},
      'rust':   {{ stroke: '#d946ef', fill: 'rgba(217, 70, 239, 0.22)', badge: '#c026d3', border: '#d946ef', text: '#fff' }},
      'node':   {{ stroke: '#22c55e', fill: 'rgba(34, 197, 94, 0.22)', badge: '#16a34a', border: '#22c55e', text: '#fff' }},
      'go':     {{ stroke: '#06b6d4', fill: 'rgba(6, 182, 212, 0.22)', badge: '#0891b2', border: '#06b6d4', text: '#fff' }},
      'dotnet': {{ stroke: '#8b5cf6', fill: 'rgba(139, 92, 246, 0.22)', badge: '#7c3aed', border: '#8b5cf6', text: '#fff' }},
      'c':      {{ stroke: '#64748b', fill: 'rgba(100, 116, 139, 0.22)', badge: '#475569', border: '#64748b', text: '#fff' }},
      'all':    {{ stroke: '#ef4444', fill: 'rgba(239, 68, 68, 0.22)', badge: '#dc2626', border: '#ef4444', text: '#fff' }}
    }};

    // Collapsible Controls Drawer Logic (Collapsed by Default)
    function toggleControlsDrawer() {{
      controlsDrawerOpen = !controlsDrawerOpen;
      const content = document.getElementById('drawer-content');
      const header = document.getElementById('drawer-toggle-header');
      const icon = document.getElementById('drawer-icon');
      const btn = document.getElementById('drawer-toggle-btn');

      if (controlsDrawerOpen) {{
        content.style.display = 'block';
        header.classList.add('open');
        icon.innerText = '▼';
        btn.innerText = '▼ Collapse';
        fetchContainers();
      }} else {{
        content.style.display = 'none';
        header.classList.remove('open');
        icon.innerText = '▶';
        btn.innerText = '▶ Expand';
      }}
    }}

    function switchView(mode, btn) {{
      currentView = mode;
      document.querySelectorAll('.view-tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      const graphsSection = document.getElementById('graphs-section');
      const rawSection = document.getElementById('raw-numbers-section');
      const kpiSection = document.getElementById('kpi-section');
      const drawerSection = document.getElementById('controls-drawer');
      const depSection = document.getElementById('dependencies-section');
      const sqlSection = document.getElementById('sql-editor-section');

      // Reset all display
      graphsSection.style.display = 'none';
      rawSection.style.display = 'none';
      kpiSection.style.display = 'none';
      drawerSection.style.display = 'none';
      depSection.style.display = 'none';
      sqlSection.style.display = 'none';

      if (mode === 'graphs') {{
        graphsSection.style.display = 'block';
        kpiSection.style.display = 'grid';
        drawerSection.style.display = 'block';
      }} else if (mode === 'raw') {{
        rawSection.style.display = 'block';
        kpiSection.style.display = 'grid';
        drawerSection.style.display = 'block';
      }} else if (mode === 'dependencies') {{
        depSection.style.display = 'block';
        fetchDependencies();
      }} else if (mode === 'sql') {{
        sqlSection.style.display = 'block';
        loadSavedQueriesList();
      }} else {{
        // 'both'
        graphsSection.style.display = 'block';
        rawSection.style.display = 'block';
        kpiSection.style.display = 'grid';
        drawerSection.style.display = 'block';
      }}
      if (latestTrendsData) renderAllCharts(latestTrendsData);
    }}

    function setTimeRange(range, elem) {{
      currentRange = range;
      elem.parentNode.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      elem.classList.add('active');
      fetchTrendsAndRefresh();
    }}

    function setServiceFilter(svc, elem) {{
      currentService = svc;
      elem.parentNode.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      elem.classList.add('active');
      fetchTrendsAndRefresh();
    }}

    function setLatencyPercentile(p, elem) {{
      currentLatencyPercentile = p.toLowerCase();
      const picker = document.getElementById('latency-percentile-picker');
      if (picker) {{
        picker.querySelectorAll('.percentile-btn').forEach(btn => btn.classList.remove('active'));
      }}
      if (elem) elem.classList.add('active');
      const titleEl = document.getElementById('lat-chart-title');
      if (titleEl) {{
        titleEl.innerText = `Latency Trend (${{p.toUpperCase()}} ms)`;
      }}
      if (latestTrendsData) {{
        renderAllCharts(latestTrendsData);
      }} else {{
        fetchTrendsAndRefresh();
      }}
    }}

    async function fetchStats() {{
      try {{
        const res = await fetch('/stats');
        if (!res.ok) return;
        const data = await res.json();
        updateRawNumbers(data);
      }} catch (e) {{
        console.error("Error fetching stats:", e);
      }}
    }}

    function updateRawNumbers(data) {{
      const avail = data.availability_pct || 100.0;
      const availEl = document.getElementById('kpi-avail');
      availEl.innerText = avail.toFixed(2) + '%';
      availEl.style.color = (avail >= 99.0) ? '#10b981' : ((avail >= 95.0) ? '#f59e0b' : '#ef4444');
      document.getElementById('kpi-avail-sub').innerText = `Success: ${{Number(data.success_count || 0).toLocaleString()}} / ${{Number(data.total_requests || 0).toLocaleString()}}`;

      document.getElementById('kpi-errors').innerText = Number(data.error_count || 0).toLocaleString();
      document.getElementById('kpi-total').innerText = Number(data.total_requests || 0).toLocaleString();
      const uptimeSec = data.uptime_seconds || 0;
      const m = Math.floor(uptimeSec / 60) % 60;
      const h = Math.floor(uptimeSec / 3600);
      const s = Math.floor(uptimeSec % 60);
      document.getElementById('kpi-uptime').innerText = `Uptime: ${{h}}h ${{m}}m ${{s}}s`;

      document.getElementById('kpi-latency').innerText = `${{data.avg_latency_ms || 0}} ms`;
      document.getElementById('kpi-latency-p95').innerText = `P95: ${{data.latency_p95_ms || 0}} ms | P99: ${{data.latency_p99_ms || 0}} ms`;

      // TPS Status & Override Display
      const effTps = data.effective_tps || data.base_tps || 6;
      const isOverride = data.is_tps_override;
      document.getElementById('kpi-tps').innerText = `${{effTps}} TPS`;
      const tpsBadge = document.getElementById('tps-active-badge');
      const drawerTpsChip = document.getElementById('drawer-tps-chip');

      if (isOverride) {{
        const remStr = data.tps_remaining_sec ? ` (${{data.tps_remaining_sec}}s left)` : ' (Perm)';
        document.getElementById('kpi-tps-status').innerText = `🔥 OVERRIDE${{remStr}} (Base: ${{data.base_tps}})`;
        document.getElementById('kpi-tps-status').style.color = '#f59e0b';
        tpsBadge.className = 'badge badge-warning';
        tpsBadge.innerText = `OVERRIDE: ${{effTps}} TPS${{remStr}}`;
        drawerTpsChip.className = 'badge badge-warning';
        drawerTpsChip.innerText = `🔥 ${{effTps}} TPS${{remStr}}`;
      }} else {{
        document.getElementById('kpi-tps-status').innerText = `Target: ${{data.base_tps}} TPS (Default)`;
        document.getElementById('kpi-tps-status').style.color = 'var(--muted)';
        tpsBadge.className = 'badge badge-success';
        tpsBadge.innerText = `DEFAULT: ${{data.base_tps}} TPS`;
        drawerTpsChip.className = 'badge badge-success';
        drawerTpsChip.innerText = `${{data.base_tps}} TPS (Default)`;
      }}

      // Active Fault Banner with Language-Specific Colors
      const fSnap = data.fault_snapshot || {{}};
      const activeFault = fSnap.active_fault;
      const pulseEl = document.getElementById('live-pulse');
      const bannerEl = document.getElementById('active-fault-banner');
      const drawerFaultChip = document.getElementById('drawer-fault-chip');

      if (activeFault) {{
        pulseEl.classList.add('fault-active');
        bannerEl.classList.add('show');
        const tgt = (activeFault.target || 'all').toLowerCase();
        const col = LANG_COLORS[tgt] || LANG_COLORS['all'];
        bannerEl.style.background = `linear-gradient(90deg, ${{col.fill}} 0%, #111a29 100%)`;
        bannerEl.style.border = `1px solid ${{col.border}}`;

        const bTag = document.getElementById('banner-tag');
        bTag.innerText = activeFault.tag || 'DRILL';
        bTag.style.background = col.badge;
        bTag.style.color = col.text;

        document.getElementById('banner-details').innerText = `Target: ${{tgt.toUpperCase()}} | Mode: ${{activeFault.fault_type}} | Affected: ${{activeFault.affected_requests || 0}}`;
        document.getElementById('banner-timer').innerText = activeFault.remaining_sec ? `Timer: ${{activeFault.remaining_sec}}s` : 'Continuous';

        drawerFaultChip.className = 'badge badge-error';
        drawerFaultChip.innerText = `🚨 FAULT: ${{activeFault.tag}}`;
      }} else {{
        pulseEl.classList.remove('fault-active');
        bannerEl.classList.remove('show');
        drawerFaultChip.className = 'badge badge-secondary';
        drawerFaultChip.innerText = `No Active Fault`;
      }}

      // Apps table
      const tbody = document.getElementById('apps-table-body');
      let appRows = '';
      let activeContainerCount = 0;
      const activeAppsMap = data.active_apps || {{}};

      for (const [lang, app] of Object.entries(data.apps || {{}})) {{
        const isWorkloadActive = (activeAppsMap[lang] !== false);
        if (isWorkloadActive) activeContainerCount++;
        const reachBadge = app.reachable ? '<span class="badge badge-success">UP</span>' : '<span class="badge badge-warning">WAIT</span>';
        const workloadBadge = isWorkloadActive ? '<span class="badge badge-success">ACTIVE</span>' : '<span class="badge badge-secondary">SUSPENDED</span>';
        const rateColor = (app.availability_pct >= 99.0) ? '#10b981' : ((app.availability_pct >= 95.0) ? '#f59e0b' : '#ef4444');

        appRows += `<tr>
          <td><span class="badge badge-${{lang}}">${{lang.toUpperCase()}}</span></td>
          <td><code>${{app.url}}</code></td>
          <td>${{workloadBadge}}</td>
          <td>${{reachBadge}}</td>
          <td style="text-align:right;">${{Number(app.total).toLocaleString()}}</td>
          <td style="text-align:right; color:#10b981; font-weight:600;">${{Number(app.success).toLocaleString()}}</td>
          <td style="text-align:right; color:#ef4444; font-weight:600;">${{Number(app.error).toLocaleString()}}</td>
          <td style="text-align:right; color:${{rateColor}}; font-weight:700;">${{app.availability_pct}}%</td>
          <td style="text-align:right; font-family:monospace;">${{app.avg_latency_ms}} ms</td>
        </tr>`;
      }}
      tbody.innerHTML = appRows;

      const drawerPowerChip = document.getElementById('drawer-power-chip');
      if (drawerPowerChip) {{
        drawerPowerChip.innerText = `${{activeContainerCount}}/7 Containers Active`;
        drawerPowerChip.className = (activeContainerCount === 7) ? 'badge badge-secondary' : 'badge badge-warning';
      }}

      // Endpoints table
      const epBody = document.getElementById('endpoints-table-body');
      let epRows = '';
      for (const ep of (data.top_endpoints || [])) {{
        const rateColor = (ep.availability_pct >= 99.0) ? '#10b981' : ((ep.availability_pct >= 95.0) ? '#f59e0b' : '#ef4444');
        epRows += `<tr>
          <td><code>${{ep.endpoint}}</code></td>
          <td style="text-align:right;">${{Number(ep.hits).toLocaleString()}}</td>
          <td style="text-align:right; color:#ef4444;">${{Number(ep.errors).toLocaleString()}}</td>
          <td style="text-align:right; color:${{rateColor}}; font-weight:600;">${{ep.availability_pct}}%</td>
          <td style="text-align:right; font-family:monospace;">${{ep.avg_latency_ms}} ms</td>
        </tr>`;
      }}
      epBody.innerHTML = epRows || '<tr><td colspan="5" style="text-align:center;">No data</td></tr>';

      // Fault history table
      const fBody = document.getElementById('fault-history-table-body');
      let fRows = '';
      const historyList = [];
      if (activeFault) historyList.push(activeFault);
      historyList.push(...(fSnap.history || []));

      for (const f of historyList) {{
        const tgt = (f.target || 'all').toLowerCase();
        const badgeCls = `badge-fault-${{tgt}}`;
        const isAct = (f.status === 'active');
        const stBadge = isAct ? '<span class="badge badge-error">RUNNING</span>' : '<span class="badge badge-success">DONE</span>';
        const dur = f.actual_duration_sec ? `${{f.actual_duration_sec}}s` : (f.duration_sec ? `${{f.duration_sec}}s` : 'manual');
        const impact = `${{f.affected_requests || 0}} reqs (${{f.induced_errors || 0}} err)`;
        fRows += `<tr>
          <td><span class="badge ${{badgeCls}}">🏷️ ${{f.tag}}</span></td>
          <td><span class="badge badge-${{tgt}}">${{tgt.toUpperCase()}}</span></td>
          <td><code>${{f.fault_type}}</code></td>
          <td>${{dur}}</td>
          <td>${{impact}}</td>
          <td>${{stBadge}}</td>
        </tr>`;
      }}
      fBody.innerHTML = fRows || '<tr><td colspan="6" style="text-align:center; color:var(--muted);">No fault drills logged yet.</td></tr>';

      // Recent requests
      const recBody = document.getElementById('recent-table-body');
      let recRows = '';
      for (const r of (data.recent_requests || []).slice().reverse()) {{
        const stBadge = (r.status === 'success') ? '<span class="badge badge-success">OK</span>' : '<span class="badge badge-error">ERR</span>';
        let tagBadge = '<span style="color:var(--muted); font-size:0.7rem;">—</span>';
        if (r.fault_tag) {{
          const bCls = `badge-fault-${{r.lang}}`;
          tagBadge = `<span class="badge ${{bCls}}">🏷️ ${{r.fault_tag}}</span>`;
        }}
        recRows += `<tr>
          <td style="font-family:monospace; white-space:nowrap;">${{r.time}}</td>
          <td><span class="badge badge-${{r.lang}}">${{r.lang.toUpperCase()}}</span></td>
          <td><code style="color:var(--accent);">${{r.method}}</code></td>
          <td><code>${{r.path}}</code></td>
          <td>${{stBadge}}</td>
          <td style="text-align:right; font-family:monospace;">${{r.duration_ms}} ms</td>
          <td>${{tagBadge}}</td>
        </tr>`;
      }}
      recBody.innerHTML = recRows || '<tr><td colspan="7" style="text-align:center;">Waiting for requests...</td></tr>';
    }}

    async function fetchTrendsAndRefresh() {{
      try {{
        const url = `/api/trends?range=${{encodeURIComponent(currentRange)}}&service=${{encodeURIComponent(currentService)}}&percentile=${{encodeURIComponent(currentLatencyPercentile)}}`;
        const res = await fetch(url);
        if (!res.ok) return;
        const trendPayload = await res.json();
        latestTrendsData = trendPayload;
        renderAllCharts(trendPayload);
      }} catch (e) {{
        console.error("Error fetching trends:", e);
      }}
    }}

    // Container Power Manager Operations
    async function fetchContainers() {{
      try {{
        const res = await fetch('/api/containers');
        if (!res.ok) return;
        const data = await res.json();
        renderContainerPowerList(data);
      }} catch (err) {{
        console.error("Error fetching containers:", err);
      }}
    }}

    function renderContainerPowerList(containers) {{
      const containerEl = document.getElementById('container-power-list');
      if (!containerEl) return;
      let html = '';
      for (const [lang, info] of Object.entries(containers)) {{
        const isRun = info.is_running;
        const stPill = isRun ? '<span class="badge badge-success">RUNNING</span>' : '<span class="badge badge-secondary">STOPPED</span>';
        const workPill = info.active_in_canary ? '<span style="color:#10b981; font-weight:700;">● Active</span>' : '<span style="color:#94a3b8;">○ Skipped</span>';
        const actBtn = isRun
          ? `<button class="btn btn-danger btn-sm" onclick="containerAction('${{lang}}', 'stop')">🛑 Stop</button>`
          : `<button class="btn btn-accent btn-sm" onclick="containerAction('${{lang}}', 'start')">▶ Start</button>`;
        const restartBtn = `<button class="btn btn-secondary btn-sm" onclick="containerAction('${{lang}}', 'restart')">🔄</button>`;

        html += `<div style="display:flex; justify-content:space-between; align-items:center; background:#182234; padding:6px 10px; border-radius:4px; border:1px solid var(--card-border);">
          <div style="display:flex; align-items:center; gap:8px;">
            <span class="badge badge-${{lang}}">${{lang.toUpperCase()}}</span>
            <span style="font-size:0.75rem; font-family:monospace; color:var(--text);">${{info.name}}</span>
            ${{stPill}}
            <span style="font-size:0.7rem; color:var(--muted);">Workload: ${{workPill}}</span>
          </div>
          <div style="display:flex; gap:5px;">
            ${{actBtn}}
            ${{restartBtn}}
          </div>
        </div>`;
      }}
      containerEl.innerHTML = html;
    }}

    async function containerAction(lang, action) {{
      try {{
        showToast(`Executing ${{action.toUpperCase()}} on ${{lang.toUpperCase()}} container...`);
        const res = await fetch('/api/containers/action', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ language: lang, action: action }})
        }});
        const data = await res.json();
        if (res.ok) {{
          showToast(`✅ Container ${{lang.toUpperCase()}} ${{action}}ed successfully!`);
          setTimeout(fetchContainers, 800);
          fetchStats();
        }} else {{
          showToast(`❌ Error: ${{data.message || 'Operation failed'}}`, true);
        }}
      }} catch (err) {{
        showToast(`❌ Network error: ${{err}}`, true);
      }}
    }}

    // Dependencies Health Telemetry View
    async function fetchDependencies() {{
      const container = document.getElementById('dep-cards-container');
      if (!container) return;
      try {{
        const res = await fetch('/api/dependencies');
        if (!res.ok) return;
        const data = await res.json();

        document.getElementById('dep-overall-avail').innerText = `${{data.overall_availability_pct}}%`;
        document.getElementById('dep-overall-ratio').innerText = `${{data.healthy_count}} of ${{data.total_dependencies}} Dependencies Healthy`;
        document.getElementById('dep-avg-lat').innerText = `${{data.avg_latency_ms}} ms`;
        const errRate = (100.0 - data.overall_availability_pct).toFixed(1);
        const errEl = document.getElementById('dep-error-rate');
        errEl.innerText = `${{errRate}}%`;
        errEl.style.color = (data.overall_availability_pct === 100) ? '#10b981' : '#ef4444';

        let cardsHtml = '';
        for (const dep of data.dependencies) {{
          const isUp = dep.available;
          const statusBadge = isUp ? '<span class="badge badge-success">HEALTHY / UP</span>' : '<span class="badge badge-error">UNAVAILABLE</span>';
          const latColor = (dep.latency_ms < 10) ? '#10b981' : ((dep.latency_ms < 50) ? '#38bdf8' : '#f59e0b');

          let browserHref = '';
          if (dep.browser && dep.browser.port) {{
            const host = window.location.hostname || 'localhost';
            const path = dep.browser.path || '/';
            const localHost = !host || host === 'localhost' || host === '127.0.0.1' || host === '::1'
              || /^10\\./.test(host) || /^192\\.168\\./.test(host)
              || /^172\\.(1[6-9]|2\\d|3[0-1])\\./.test(host);
            if (localHost) {{
              browserHref = `http://${{host}}:${{dep.browser.port}}${{path}}`;
            }} else {{
              const suffix = path.startsWith('/') ? path : `/${{path}}`;
              browserHref = `${{window.location.origin}}/open/${{dep.id}}${{suffix}}`;
            }}
          }}
          const nameHtml = browserHref
            ? `<a class="dep-open" href="${{browserHref}}" target="_blank" rel="noopener noreferrer">${{dep.name}}</a>`
            : dep.name;
          const endpointHtml = browserHref
            ? `<a class="dep-open" href="${{browserHref}}" target="_blank" rel="noopener noreferrer">${{browserHref}}</a>`
            : `<code>${{dep.endpoint}}</code>`;

          cardsHtml += `<div class="dep-card" style="border-left: 3px solid ${{isUp ? '#10b981' : '#ef4444'}};">
            <div class="dep-card-header">
              <div>
                <div class="dep-name">${{nameHtml}}</div>
                <div class="dep-role">${{dep.role}}</div>
              </div>
              <div>${{statusBadge}}</div>
            </div>
            <div class="dep-meta-row">
              <span style="color:var(--muted);">Open in browser:</span>
              ${{endpointHtml}}
            </div>
            <div class="dep-meta-row">
              <span style="color:var(--muted);">Observed Latency:</span>
              <span style="font-weight:700; color:${{latColor}};">${{dep.latency_ms}} ms</span>
            </div>
            <div class="dep-meta-row">
              <span style="color:var(--muted);">Capacity & Usage:</span>
              <span style="font-size:0.72rem; color:var(--text);">${{dep.capacity_usage}}</span>
            </div>
          </div>`;
        }}
        container.innerHTML = cardsHtml;
      }} catch (err) {{
        container.innerHTML = `<div style="color:#ef4444; padding:20px; grid-column:1/-1;">Error probing dependencies: ${{err}}</div>`;
      }}
    }}

    // SSMS & pgAdmin Style SQL Editor & First-Principles Data Grid
    const SQL_KEYWORDS = new Set(`
    select from where group order having limit offset fetch union except intersect with recursive
    as on join inner left right full cross natural and or not null is in like ilike between
    case when then else end distinct all asc desc nulls last first filter over within using
    by cast interval timestamp true false window lateral only
    `.trim().split(/\\s+/));

    const CLAUSES = new Set(['select','from','where','group','order','having','limit','offset','fetch','union','except','intersect','with','window']);

    function tokenizeSql(sql) {{
      const tokens = [];
      let i = 0;
      const n = sql.length;
      while (i < n) {{
        const c = sql[i];
        if (/\\s/.test(c)) {{ i++; continue; }}
        if (c === '-' && sql[i + 1] === '-') {{
          let j = i + 2;
          while (j < n && sql[j] !== '\\n' && sql[j] !== '\\r') j++;
          tokens.push({{ type: 'comment', value: sql.slice(i, j) }});
          i = j;
          continue;
        }}
        if (c === '/' && sql[i + 1] === '*') {{
          const end = sql.indexOf('*/', i + 2);
          const j = end < 0 ? n : end + 2;
          tokens.push({{ type: 'comment', value: sql.slice(i, j) }});
          i = j;
          continue;
        }}
        if (c === "'") {{
          let j = i + 1;
          while (j < n) {{
            if (sql[j] === "'" && sql[j + 1] === "'") {{ j += 2; continue; }}
            if (sql[j] === "'") {{ j++; break; }}
            j++;
          }}
          tokens.push({{ type: 'string', value: sql.slice(i, j) }});
          i = j;
          continue;
        }}
        if (/[0-9]/.test(c)) {{
          let j = i + 1;
          while (j < n && /[0-9.]/.test(sql[j])) j++;
          tokens.push({{ type: 'number', value: sql.slice(i, j) }});
          i = j;
          continue;
        }}
        if (/[A-Za-z_]/.test(c)) {{
          let j = i + 1;
          while (j < n && /[A-Za-z0-9_]/.test(sql[j])) j++;
          tokens.push({{ type: 'word', value: sql.slice(i, j) }});
          i = j;
          continue;
        }}
        const ops = ['->>', '->', '::', '>=', '<=', '<>', '!=', '||'];
        const op = ops.find(item => sql.startsWith(item, i));
        if (op) {{
          tokens.push({{ type: 'op', value: op }});
          i += op.length;
          continue;
        }}
        tokens.push({{ type: 'punct', value: c }});
        i++;
      }}
      return tokens;
    }}

    function nextWord(tokens, i) {{
      for (let j = i + 1; j < tokens.length; j++) {{
        if (tokens[j].type === 'word') return tokens[j].value.toLowerCase();
        if (tokens[j].type !== 'comment') return '';
      }}
      return '';
    }}

    function beautifySql(sql) {{
      const tokens = tokenizeSql(sql || '');
      if (!tokens.length) return '';
      const lines = [];
      let buf = '';
      let paren = 0;
      const stmt = [0];
      let selectDepth = -1;
      const caseIndents = [];
      let lineIndent = 0;

      function atStmt() {{ return paren === stmt[stmt.length - 1]; }}

      function flush() {{
        const text = buf.replace(/[ \\t]+$/g, '').replace(/^[ \\t]+/g, '');
        if (text) lines.push('  '.repeat(Math.max(0, lineIndent)) + text);
        buf = '';
      }}
      function startLine(indent, token) {{
        flush();
        lineIndent = indent;
        buf = token ? token.value : '';
      }}

      function tightBefore(token) {{
        if (!buf) return true;
        const prev = buf[buf.length - 1];
        if ('(.'.includes(prev)) return true;
        if (token.value === '(') {{
          const named = buf.match(/([A-Za-z_]+)$/);
          if (named && ['filter', 'over', 'within', 'group'].includes(named[1].toLowerCase())) return false;
          return true;
        }}
        if (token.value === '.' || token.value === ',' || token.value === ')' || token.value === ';') return true;
        if (token.type === 'op' && (token.value === '->>' || token.value === '->' || token.value === '::')) return true;
        if (buf.endsWith('->>') || buf.endsWith('->') || buf.endsWith('::')) return true;
        return false;
      }}

      function add(token) {{
        if (buf && !tightBefore(token)) buf += ' ';
        buf += token.value;
      }}

      for (let i = 0; i < tokens.length; i++) {{
        const t = tokens[i];
        const word = t.type === 'word' ? t.value.toLowerCase() : '';
        const upcoming = nextWord(tokens, i);

        if (t.type === 'comment') {{
          flush();
          lines.push('  '.repeat(lineIndent) + t.value);
          continue;
        }}

        if (t.value === '(') {{
          add(t);
          paren++;
          if (upcoming === 'select' || upcoming === 'with') stmt.push(paren);
          continue;
        }}
        if (t.value === ')') {{
          if (stmt[stmt.length - 1] === paren) stmt.pop();
          if (selectDepth === paren) selectDepth = -1;
          paren = Math.max(0, paren - 1);
          if (!buf.trim()) {{
            lineIndent = paren;
            buf = ')';
            flush();
          }} else {{
            add(t);
          }}
          continue;
        }}

        const joinHead = /^(inner|left|right|full|cross|natural)$/i.test(buf.trim());
        if (word === 'join' && joinHead) {{
          add(t);
          continue;
        }}
        const startsJoin = atStmt() && (word === 'join' || ((word === 'inner' || word === 'left' || word === 'right' || word === 'full' || word === 'cross' || word === 'natural') && upcoming === 'join'));
        const startsClause = atStmt() && CLAUSES.has(word) && word !== 'by';
        if (startsJoin || startsClause) {{
          if (word === 'select') {{
            flush();
            lineIndent = paren;
            buf = t.value;
            flush();
            selectDepth = paren;
            lineIndent = paren + 1;
          }} else {{
            if (selectDepth === paren) selectDepth = -1;
            startLine(paren, t);
          }}
          continue;
        }}
        if ((word === 'and' || word === 'or') && atStmt()) {{
          startLine(paren + 1, t);
          continue;
        }}
        if (word === 'case') {{
          const inSelect = selectDepth === paren;
          if (buf.trim()) flush();
          const base = paren + (inSelect ? 1 : 0);
          caseIndents.push(base);
          lineIndent = base;
          buf = t.value;
          continue;
        }}
        if (word === 'when' || word === 'else') {{
          const base = caseIndents.length ? caseIndents[caseIndents.length - 1] : paren;
          if (buf.trim().toLowerCase() === 'case') flush();
          else flush();
          lineIndent = base + 1;
          buf = t.value;
          continue;
        }}
        if (word === 'end' && caseIndents.length) {{
          const base = caseIndents.pop();
          flush();
          lineIndent = base;
          buf = t.value;
          continue;
        }}
        if (t.value === ',' && selectDepth === paren) {{
          buf += ',';
          flush();
          lineIndent = paren + 1;
          continue;
        }}
        add(t);
      }}
      flush();
      return lines.join('\\n').replace(/\\n{{3,}}/g, '\\n\\n').trim();
    }}

    function highlightSql(sql) {{
      let html = '';
      let i = 0;
      const text = sql || '';
      const n = text.length;
      function esc(s) {{
        return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      }}
      while (i < n) {{
        const c = text[i];
        if (c === '\\n' || c === ' ' || c === '\\t') {{
          let j = i + 1;
          while (j < n && (text[j] === ' ' || text[j] === '\\t')) j++;
          html += esc(text.slice(i, j));
          i = j;
          continue;
        }}
        if (c === '-' && text[i + 1] === '-') {{
          let j = i + 2;
          while (j < n && text[j] !== '\\n') j++;
          html += '<span class="sql-cmt">' + esc(text.slice(i, j)) + '</span>';
          i = j;
          continue;
        }}
        if (c === '/' && text[i + 1] === '*') {{
          const end = text.indexOf('*/', i + 2);
          const j = end < 0 ? n : end + 2;
          html += '<span class="sql-cmt">' + esc(text.slice(i, j)) + '</span>';
          i = j;
          continue;
        }}
        if (c === "'") {{
          let j = i + 1;
          while (j < n) {{
            if (text[j] === "'" && text[j + 1] === "'") {{ j += 2; continue; }}
            if (text[j] === "'") {{ j++; break; }}
            j++;
          }}
          html += '<span class="sql-str">' + esc(text.slice(i, j)) + '</span>';
          i = j;
          continue;
        }}
        if (/[0-9]/.test(c)) {{
          let j = i + 1;
          while (j < n && /[0-9.]/.test(text[j])) j++;
          html += '<span class="sql-num">' + esc(text.slice(i, j)) + '</span>';
          i = j;
          continue;
        }}
        if (/[A-Za-z_]/.test(c)) {{
          let j = i + 1;
          while (j < n && /[A-Za-z0-9_]/.test(text[j])) j++;
          const word = text.slice(i, j);
          let k = j;
          while (k < n && (text[k] === ' ' || text[k] === '\\t')) k++;
          const isFn = text[k] === '(' && !SQL_KEYWORDS.has(word.toLowerCase());
          const cls = SQL_KEYWORDS.has(word.toLowerCase()) ? 'sql-kw' : (isFn ? 'sql-fn' : 'sql-id');
          html += '<span class="' + cls + '">' + esc(word) + '</span>';
          i = j;
          continue;
        }}
        const ops = ['->>', '->', '::', '>=', '<=', '<>', '!=', '||'];
        const op = ops.find(item => text.startsWith(item, i));
        if (op) {{
          html += '<span class="sql-op">' + esc(op) + '</span>';
          i += op.length;
          continue;
        }}
        html += esc(c);
        i++;
      }}
      return html;
    }}

    function setSqlEditorText(sql, format) {{
      const el = document.getElementById('sql-query-input');
      if (!el) return;
      el.value = format ? beautifySql(sql || '') : (sql || '');
      paintSqlEditor();
      fitSqlEditor();
    }}

    function paintSqlEditor() {{
      const el = document.getElementById('sql-query-input');
      const pre = document.getElementById('sql-highlight');
      if (!el || !pre) return;
      pre.innerHTML = highlightSql(el.value);
    }}

    function fitSqlEditor() {{
      const el = document.getElementById('sql-query-input');
      if (!el) return;
      el.style.height = 'auto';
      const next = Math.min(380, Math.max(168, el.scrollHeight + 2));
      el.style.height = next + 'px';
      syncSqlScroll();
    }}

    function syncSqlScroll() {{
      const el = document.getElementById('sql-query-input');
      const pre = document.getElementById('sql-highlight');
      if (!el || !pre) return;
      pre.scrollTop = el.scrollTop;
      pre.scrollLeft = el.scrollLeft;
    }}

    function formatSqlEditor() {{
      const el = document.getElementById('sql-query-input');
      if (!el) return;
      const start = el.selectionStart;
      setSqlEditorText(el.value, true);
      el.focus();
      const pos = Math.min(start, el.value.length);
      el.setSelectionRange(pos, pos);
    }}

    function initSqlEditor() {{
      const el = document.getElementById('sql-query-input');
      if (!el || el.dataset.ready) return;
      el.dataset.ready = '1';
      el.addEventListener('input', () => {{
        paintSqlEditor();
        fitSqlEditor();
      }});
      el.addEventListener('scroll', syncSqlScroll);
      paintSqlEditor();
      fitSqlEditor();
    }}
    async function loadSavedQueriesList() {{
      try {{
        const res = await fetch('/api/sql/saved-queries');
        if (!res.ok) return;
        const list = await res.json();
        const sel = document.getElementById('sql-saved-select');
        sel.innerHTML = '<option value="">-- Choose a curated saved query --</option>';
        for (const q of list) {{
          sel.innerHTML += `<option value="${{q.id}}">${{q.name}}</option>`;
        }}
        window._savedQueriesMap = {{}};
        for (const q of list) {{
          window._savedQueriesMap[q.id] = q.sql;
        }}
        // Default populate if empty
        initSqlEditor();
        const textarea = document.getElementById('sql-query-input');
        if (!textarea.value.trim() && list.length > 0) {{
          setSqlEditorText(list[0].sql, true);
          sel.value = list[0].id;
        }} else {{
          paintSqlEditor();
        }}
      }} catch (err) {{
        console.error("Error loading saved queries:", err);
      }}
    }}

    function onSelectSavedQuery(qid) {{
      if (window._savedQueriesMap && window._savedQueriesMap[qid]) {{
        setSqlEditorText(window._savedQueriesMap[qid], true);
      }}
    }}

    function clearSqlQuery() {{
      setSqlEditorText('', false);
      document.getElementById('sql-saved-select').value = '';
      document.getElementById('sql-grid-empty').style.display = 'block';
      document.getElementById('sql-grid-table').style.display = 'none';
      hideSqlPlan();
      document.getElementById('sql-status-text').innerText = 'Ready &bull; Cleared';
      document.getElementById('sql-timing-text').innerText = '-- ms';
    }}

    async function logoutCanary() {{
      await fetch('/api/logout', {{ method: 'POST' }});
      window.location.href = '/login';
    }}

    async function saveSqlQuery() {{
      const name = document.getElementById('sql-save-name').value.trim();
      const query = document.getElementById('sql-query-input').value.trim();
      if (!name || !query) {{
        showToast('Enter a name and a query to save', true);
        return;
      }}
      const res = await fetch('/api/sql/saved-queries', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ name: name, sql: query }})
      }});
      const data = await res.json();
      if (!res.ok) {{
        showToast(data.error || 'Could not save query', true);
        return;
      }}
      showToast('Saved query');
      loadSavedQueriesList();
    }}

    async function runSqlQuery() {{
      const query = document.getElementById('sql-query-input').value.trim();
      if (!query) {{
        showToast("Please enter a SQL query", true);
        return;
      }}
      const statusText = document.getElementById('sql-status-text');
      const timingText = document.getElementById('sql-timing-text');
      statusText.innerHTML = 'Executing query on PostgreSQL...';

      try {{
        const res = await fetch('/api/sql/query', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ query: query }})
        }});
        const data = await res.json();
        latestSqlResult = data;

        if (data.status === 'success') {{
          statusText.innerHTML = `✅ Query Succeeded &bull; ${{data.row_count}} row(s) returned`;
          timingText.innerText = `${{data.execution_time_ms}} ms`;
          renderSqlGrid(data);
          renderSqlPlan(data.plan);
        }} else {{
          statusText.innerHTML = `❌ Error: <span style="color:#ef4444;">${{data.error}}</span>`;
          timingText.innerText = `${{data.execution_time_ms || 0}} ms`;
          document.getElementById('sql-grid-empty').innerHTML = `<div style="color:#ef4444; font-family:monospace; padding:20px;">SQL Error: ${{data.error}}</div>`;
          document.getElementById('sql-grid-empty').style.display = 'block';
          document.getElementById('sql-grid-table').style.display = 'none';
          hideSqlPlan();
        }}
      }} catch (err) {{
        statusText.innerHTML = `❌ Network Error: ${{err}}`;
      }}
    }}

    function renderSqlGrid(data) {{
      const emptyEl = document.getElementById('sql-grid-empty');
      const tableEl = document.getElementById('sql-grid-table');
      const thead = document.getElementById('sql-grid-thead');
      const tbody = document.getElementById('sql-grid-tbody');

      if (!data.columns || data.columns.length === 0 || !data.rows || data.rows.length === 0) {{
        emptyEl.innerHTML = 'Query returned 0 rows.';
        emptyEl.style.display = 'block';
        tableEl.style.display = 'none';
        return;
      }}

      emptyEl.style.display = 'none';
      tableEl.style.display = 'table';

      // Header with Row Number # and column names with sort handler
      let thHtml = '<tr><th style="width:40px;">#</th>';
      for (let i = 0; i < data.columns.length; i++) {{
        const col = data.columns[i];
        thHtml += `<th onclick="sortSqlGridColumn(${{i}})">${{col}} <span id="col-sort-${{i}}" style="color:var(--muted); font-size:0.65rem;">⇅</span></th>`;
      }}
      thHtml += '</tr>';
      thead.innerHTML = thHtml;

      // Rows
      let trHtml = '';
      for (let r = 0; r < data.rows.length; r++) {{
        const row = data.rows[r];
        trHtml += `<tr><td class="row-num">${{r + 1}}</td>`;
        for (let c = 0; c < row.length; c++) {{
          const cell = row[c];
          if (cell === null || cell === undefined) {{
            trHtml += `<td class="sql-null" ondblclick="openCellModal(${{r}}, ${{c}})" title="Double-click to inspect (&lt;NULL&gt;)">&lt;NULL&gt;</td>`;
          }} else {{
            const safeCell = String(cell).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
            trHtml += `<td ondblclick="openCellModal(${{r}}, ${{c}})" title="Double-click to inspect full content">${{safeCell}}</td>`;
          }}
        }}
        trHtml += '</tr>';
      }}
      tbody.innerHTML = trHtml;
    }}

    function hideSqlPlan() {{
      const panel = document.getElementById('sql-plan-panel');
      if (panel) panel.style.display = 'none';
      const explain = document.getElementById('sql-plan-explain');
      if (explain) explain.hidden = true;
    }}

    function sqlEscape(value) {{
      return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }}

    let latestSqlPlan = null;
    let planViewMode = 'grid';

    function setPlanView(mode) {{
      planViewMode = mode === 'graph' ? 'graph' : 'grid';
      const grid = document.getElementById('sql-plan-wrapper');
      const graph = document.getElementById('sql-plan-graph');
      const gridBtn = document.getElementById('plan-view-grid');
      const graphBtn = document.getElementById('plan-view-graph');
      if (grid) grid.style.display = planViewMode === 'grid' ? 'block' : 'none';
      if (graph) graph.style.display = planViewMode === 'graph' ? 'block' : 'none';
      if (gridBtn) gridBtn.classList.toggle('active', planViewMode === 'grid');
      if (graphBtn) graphBtn.classList.toggle('active', planViewMode === 'graph');
      if (planViewMode === 'graph' && latestSqlPlan) renderPlanGraph(latestSqlPlan);
    }}

    function showPlanStep(index) {{
      if (!latestSqlPlan || !latestSqlPlan.rows || !latestSqlPlan.rows[index]) return;
      document.querySelectorAll('#sql-plan-table tbody tr').forEach((tr, i) => {{
        tr.classList.toggle('plan-selected', i === index);
      }});
      document.querySelectorAll('#sql-plan-graph .plan-card').forEach(card => {{
        card.classList.toggle('plan-selected', Number(card.dataset.index) === index);
      }});
      const explain = document.getElementById('sql-plan-explain');
      if (!explain) return;
      explain.hidden = false;
      const text = (latestSqlPlan.explanations && latestSqlPlan.explanations[index]) || 'No explanation for this step.';
      explain.textContent = text;
    }}

    function renderPlanGraph(plan) {{
      const host = document.getElementById('sql-plan-graph');
      if (!host) return;
      const roots = [];
      const stack = [];
      (plan.rows || []).forEach((row, index) => {{
        const label = String(row[0] || '');
        const depth = Math.floor((label.match(/^ */) || [''])[0].length / 2);
        const node = {{ index: index, label: label.trim(), relation: row[1], ms: row[6], rows: row[5], children: [] }};
        while (stack.length > depth) stack.pop();
        if (!stack.length) roots.push(node);
        else stack[stack.length - 1].children.push(node);
        stack[depth] = node;
      }});
      function draw(node) {{
        const rel = node.relation ? `<span class="plan-card-rel">${{sqlEscape(node.relation)}}</span>` : '';
        const meta = `${{sqlEscape(node.rows == null ? '' : node.rows + ' rows')}} · ${{sqlEscape(node.ms == null ? '' : node.ms + ' ms')}}`;
        const kids = node.children.map(draw).join('');
        return `<div class="plan-branch"><button type="button" class="plan-card" data-index="${{node.index}}" onclick="showPlanStep(${{node.index}})"><div class="plan-card-type">${{sqlEscape(node.label)}}${{rel}}</div><div class="plan-card-meta">${{meta}}</div></button>${{kids ? `<div class="plan-children">${{kids}}</div>` : ''}}</div>`;
      }}
      host.innerHTML = `<div class="plan-graph">${{roots.map(draw).join('')}}</div>`;
    }}

    function renderSqlPlan(plan) {{
      const panel = document.getElementById('sql-plan-panel');
      const summary = document.getElementById('sql-plan-summary');
      const thead = document.getElementById('sql-plan-thead');
      const tbody = document.getElementById('sql-plan-tbody');
      const explain = document.getElementById('sql-plan-explain');
      if (!panel) return;
      if (!plan || !plan.rows || plan.rows.length === 0) {{
        if (plan && plan.error) {{
          panel.style.display = 'block';
          summary.textContent = plan.error;
          thead.innerHTML = '';
          tbody.innerHTML = '';
          if (explain) explain.hidden = true;
        }} else {{
          hideSqlPlan();
        }}
        return;
      }}
      latestSqlPlan = plan;
      panel.style.display = 'block';
      const bits = [];
      if (plan.planning_time_ms != null) bits.push(plan.planning_time_ms + ' ms planning');
      if (plan.execution_time_ms != null) bits.push(plan.execution_time_ms + ' ms execution');
      summary.textContent = bits.join(' · ');
      let thHtml = '<tr>';
      (plan.columns || []).forEach(col => {{
        thHtml += `<th>${{sqlEscape(col)}}</th>`;
      }});
      thHtml += '</tr>';
      thead.innerHTML = thHtml;
      let trHtml = '';
      plan.rows.forEach((row, r) => {{
        trHtml += `<tr onclick="showPlanStep(${{r}})">`;
        row.forEach((cell, idx) => {{
          if (cell === null || cell === undefined) {{
            trHtml += '<td class="sql-null">&lt;NULL&gt;</td>';
          }} else {{
            const style = idx === 0 ? ' style="white-space:pre; font-family:ui-monospace,monospace;"' : '';
            trHtml += `<td${{style}}>${{sqlEscape(cell)}}</td>`;
          }}
        }});
        trHtml += '</tr>';
      }});
      tbody.innerHTML = trHtml;
      if (explain) {{
        explain.hidden = false;
        explain.textContent = 'Click a row in the plan. The note here describes that step: what it reads, how the estimate compares with this run, and what a filter or sort is doing.';
      }}
      renderPlanGraph(plan);
      setPlanView(planViewMode);
    }}

    let _sortDir = {{}};
    function sortSqlGridColumn(colIndex) {{
      if (!latestSqlResult || !latestSqlResult.rows) return;
      _sortDir[colIndex] = !_sortDir[colIndex];
      const asc = _sortDir[colIndex];

      latestSqlResult.rows.sort((a, b) => {{
        const valA = a[colIndex];
        const valB = b[colIndex];
        if (valA === valB) return 0;
        if (valA === null) return asc ? -1 : 1;
        if (valB === null) return asc ? 1 : -1;
        const numA = Number(valA);
        const numB = Number(valB);
        if (!isNaN(numA) && !isNaN(numB)) {{
          return asc ? (numA - numB) : (numB - numA);
        }}
        return asc ? String(valA).localeCompare(String(valB)) : String(valB).localeCompare(String(valA));
      }});

      renderSqlGrid(latestSqlResult);
      const arrow = asc ? '▲' : '▼';
      const iconEl = document.getElementById(`col-sort-${{colIndex}}`);
      if (iconEl) iconEl.innerText = arrow;
    }}

    function exportSqlResults(format) {{
      if (!latestSqlResult || !latestSqlResult.rows || latestSqlResult.rows.length === 0) {{
        showToast("No query results to export", true);
        return;
      }}
      if (format === 'json') {{
        const jsonRows = latestSqlResult.rows.map(row => {{
          const obj = {{}};
          latestSqlResult.columns.forEach((col, idx) => {{
            obj[col] = row[idx];
          }});
          return obj;
        }});
        const blob = new Blob([JSON.stringify(jsonRows, null, 2)], {{ type: 'application/json' }});
        downloadBlob(blob, `query_results_${{Date.now()}}.json`);
      }} else {{
        // CSV export
        const header = latestSqlResult.columns.map(c => `"${{c.replace(/"/g, '""')}}"`).join(',');
        const lines = latestSqlResult.rows.map(row => {{
          return row.map(val => (val === null ? '' : `"${{String(val).replace(/"/g, '""')}}"`)).join(',');
        }});
        const csvContent = [header, ...lines].join('\\n');
        const blob = new Blob([csvContent], {{ type: 'text/csv' }});
        downloadBlob(blob, `query_results_${{Date.now()}}.csv`);
      }}
    }}

    function downloadBlob(blob, filename) {{
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      showToast(`Exported ${{filename}} successfully!`);
    }}

    // QR Code Modal Operations
    function openQrModal() {{
      const modal = document.getElementById('qr-modal');
      modal.classList.add('open');
      const hostInput = document.getElementById('qr-host-input');
      const currentHost = window.location.hostname || 'localhost';

      // Auto-detect server LAN IP for cross-device mobile scanning
      fetch('/api/network/host-ip')
        .then(res => res.json())
        .then(data => {{
          const detectedIp = data.ip;
          if (detectedIp && detectedIp !== 'localhost' && detectedIp !== '127.0.0.1') {{
            const lanBtn = document.getElementById('qr-preset-lan-btn');
            if (lanBtn) {{
              lanBtn.dataset.ip = detectedIp;
              lanBtn.innerText = `📡 Use LAN IP (${{detectedIp}})`;
              lanBtn.style.display = 'inline-block';
            }}
            const badge = document.getElementById('qr-ip-detected-badge');
            if (badge) badge.style.display = 'inline-block';

            // If user opened on localhost/127.0.0.1, auto-populate with LAN IP so phone scan works!
            if (!hostInput.value || hostInput.value === 'localhost' || hostInput.value === '127.0.0.1') {{
              hostInput.value = detectedIp;
              updateQrUrl();
              return;
            }}
          }}
          if (!hostInput.value) {{
            hostInput.value = currentHost;
          }}
          updateQrUrl();
        }})
        .catch(() => {{
          if (!hostInput.value) {{
            hostInput.value = currentHost;
          }}
          updateQrUrl();
        }});
    }}

    function setQrHostPreset(h) {{
      if (!h) return;
      document.getElementById('qr-host-input').value = h;
      updateQrUrl();
    }}

    function closeQrModal() {{
      document.getElementById('qr-modal').classList.remove('open');
    }}

    function updateQrUrl() {{
      let val = document.getElementById('qr-host-input').value.trim();
      let proto = window.location.protocol || 'http:';

      if (!val) {{
        val = window.location.hostname || 'localhost';
      }}

      // Strip any leading protocol
      if (val.startsWith('http://')) {{
        proto = 'http:';
        val = val.substring(7);
      }} else if (val.startsWith('https://')) {{
        proto = 'https:';
        val = val.substring(8);
      }}

      // Strip any port suffix (:8085, :80, :443, etc.) — port suffix is not needed!
      if (val.includes(':')) {{
        val = val.split(':')[0];
      }}

      // Strip any trailing slashes
      val = val.replace(/[\\/]+$/, '');

      const fullUrl = `${{proto}}//${{val}}`;
      document.getElementById('qr-full-url').innerText = fullUrl;
      const img = document.getElementById('qr-img');
      img.src = `/api/qrcode?url=${{encodeURIComponent(fullUrl)}}`;
    }}

    function copyQrUrl() {{
      const text = document.getElementById('qr-full-url').innerText;
      navigator.clipboard.writeText(text).then(() => {{
        showToast("✅ Copied URL to clipboard!");
      }});
    }}

    // Cell Value Inspector Modal Operations
    let _activeCellRaw = '';
    let _activeCellPretty = '';
    let _activeCellIsJson = false;

    function openCellModal(rowIdx, colIdx) {{
      if (!latestSqlResult || !latestSqlResult.rows) return;
      const row = latestSqlResult.rows[rowIdx];
      if (!row) return;
      const colName = (latestSqlResult.columns && latestSqlResult.columns[colIdx]) ? latestSqlResult.columns[colIdx] : `Col #${{colIdx + 1}}`;
      const rawVal = row[colIdx];

      document.getElementById('cell-modal-col-name').innerText = colName;
      document.getElementById('cell-modal-row-badge').innerText = `Row #${{rowIdx + 1}}`;

      const textEl = document.getElementById('cell-modal-text');
      const formatBtn = document.getElementById('cell-format-json-btn');
      const lenBadge = document.getElementById('cell-modal-len-badge');

      if (rawVal === null || rawVal === undefined) {{
        _activeCellRaw = '';
        _activeCellPretty = '';
        _activeCellIsJson = false;
        lenBadge.innerText = 'NULL';
        lenBadge.className = 'badge badge-secondary';
        textEl.innerText = '<NULL>';
        formatBtn.style.display = 'none';
      }} else {{
        let strVal = typeof rawVal === 'object' ? JSON.stringify(rawVal) : String(rawVal);
        _activeCellRaw = strVal;
        lenBadge.innerText = `${{strVal.length.toLocaleString()}} chars`;
        lenBadge.className = 'badge badge-secondary';

        // Check if string is valid JSON
        _activeCellIsJson = false;
        try {{
          const parsed = typeof rawVal === 'object' ? rawVal : JSON.parse(strVal);
          if (parsed && typeof parsed === 'object') {{
            _activeCellIsJson = true;
            _activeCellPretty = JSON.stringify(parsed, null, 2);
          }}
        }} catch (_) {{
          _activeCellIsJson = false;
        }}

        if (_activeCellIsJson) {{
          formatBtn.style.display = 'inline-block';
          formatBtn.innerText = '📄 Raw View';
          textEl.innerText = _activeCellPretty;
        }} else {{
          formatBtn.style.display = 'none';
          textEl.innerText = _activeCellRaw;
        }}
      }}

      document.getElementById('cell-modal').classList.add('open');
    }}

    function closeCellModal() {{
      document.getElementById('cell-modal').classList.remove('open');
    }}

    function copyCellValue() {{
      const text = document.getElementById('cell-modal-text').innerText;
      if (!text || text === '<NULL>') {{
        showToast("Cell is NULL or empty");
        return;
      }}
      const valToCopy = (_activeCellRaw !== '') ? _activeCellRaw : text;
      navigator.clipboard.writeText(valToCopy).then(() => {{
        showToast("✅ Cell value copied to clipboard!");
      }}).catch(() => {{
        const ta = document.createElement('textarea');
        ta.value = valToCopy;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast("✅ Cell value copied to clipboard!");
      }});
    }}

    function formatCellJson() {{
      const textEl = document.getElementById('cell-modal-text');
      const formatBtn = document.getElementById('cell-format-json-btn');
      if (textEl.innerText === _activeCellPretty) {{
        textEl.innerText = _activeCellRaw;
        formatBtn.innerText = '✨ Format JSON';
      }} else {{
        textEl.innerText = _activeCellPretty;
        formatBtn.innerText = '📄 Raw View';
      }}
    }}

    function wrapCellTextToggle() {{
      const textEl = document.getElementById('cell-modal-text');
      textEl.classList.toggle('wrapped');
    }}

    // Shortcuts: Esc closes modals, Ctrl+Enter executes SQL Query
    document.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape') {{
        closeCellModal();
        closeQrModal();
      }}
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {{
        if (currentView === 'sql') {{
          runSqlQuery();
        }}
      }}
    }});

    // Fault Injection Handlers
    async function startFaultInjection(e) {{
      e.preventDefault();
      const tag = document.getElementById('fault-tag-input').value.trim();
      const fault_type = document.getElementById('fault-type-select').value;
      const target = document.getElementById('fault-target-select').value;
      const duration_sec = parseInt(document.getElementById('fault-duration-select').value, 10);

      try {{
        const res = await fetch('/api/fault/start', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ tag, fault_type, target, duration_sec, rate: 100, delay_ms: 1000 }})
        }});
        if (res.ok) {{
          document.getElementById('fault-tag-input').value = '';
          fetchStats();
          fetchTrendsAndRefresh();
        }}
      }} catch (err) {{
        alert("Error starting fault drill: " + err);
      }}
    }}

    function quickDrill(tag, fault_type, target, duration_sec) {{
      document.getElementById('fault-tag-input').value = tag;
      document.getElementById('fault-type-select').value = fault_type;
      document.getElementById('fault-target-select').value = target;
      document.getElementById('fault-duration-select').value = duration_sec;
      document.getElementById('fault-form').dispatchEvent(new Event('submit'));
    }}

    async function stopActiveFault() {{
      try {{
        const res = await fetch('/api/fault/stop', {{ method: 'POST' }});
        if (res.ok) {{
          fetchStats();
          fetchTrendsAndRefresh();
        }}
      }} catch (err) {{
        console.error(err);
      }}
    }}

    function showToast(msg, isError) {{
      const toast = document.getElementById('toast-notice');
      if (!toast) return;
      toast.innerText = msg;
      toast.style.borderColor = isError ? '#ef4444' : '#10b981';
      toast.style.display = 'block';
      setTimeout(() => {{ toast.style.display = 'none'; }}, 4000);
    }}

    // Target Crash Handlers
    async function submitCrashTrigger(e) {{
      e.preventDefault();
      const target = document.getElementById('crash-target-select').value;
      const type = document.getElementById('crash-type-select').value;
      let tag = document.getElementById('crash-tag-input').value.trim();
      if (!tag) tag = `CRASH-${{target.toUpperCase()}}-${{type.toUpperCase()}}`;

      try {{
        const res = await fetch('/api/crash', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ target, type, tag }})
        }});
        const data = await res.json();
        showToast(`💥 Crash issued to ${{target.toUpperCase()}} (${{type.toUpperCase()}} crash)!`);
        document.getElementById('crash-tag-input').value = '';
        fetchStats();
        fetchTrendsAndRefresh();
      }} catch (err) {{
        showToast("Error triggering crash: " + err, true);
      }}
    }}

    function quickCrash(target, type, tag) {{
      document.getElementById('crash-target-select').value = target;
      document.getElementById('crash-type-select').value = type;
      document.getElementById('crash-tag-input').value = tag;
      document.getElementById('crash-form').dispatchEvent(new Event('submit'));
    }}

    // TPS Override Handlers
    async function applyTpsOverride(e) {{
      e.preventDefault();
      const tps = parseFloat(document.getElementById('tps-rate-input').value);
      const duration_sec = parseInt(document.getElementById('tps-duration-select').value, 10);

      try {{
        const res = await fetch('/api/tps', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ tps, duration_sec }})
        }});
        if (res.ok) {{
          fetchStats();
          fetchTrendsAndRefresh();
        }}
      }} catch (err) {{
        alert("Error setting TPS: " + err);
      }}
    }}

    function quickTps(rate, duration_sec) {{
      document.getElementById('tps-rate-input').value = rate;
      document.getElementById('tps-duration-select').value = duration_sec;
      document.getElementById('tps-form').dispatchEvent(new Event('submit'));
    }}

    async function resetTps() {{
      try {{
        const res = await fetch('/api/tps/reset', {{ method: 'POST' }});
        if (res.ok) {{
          fetchStats();
          fetchTrendsAndRefresh();
        }}
      }} catch (err) {{
        alert("Error resetting TPS: " + err);
      }}
    }}

    // High-Efficiency Canvas Chart Engine
    function drawLineChart(canvasId, series, faultEvents, options) {{
      const canvas = document.getElementById(canvasId);
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      const w = rect.width;
      const h = rect.height;

      canvas.width = w * dpr;
      canvas.height = h * dpr;
      ctx.resetTransform();
      ctx.scale(dpr, dpr);

      ctx.clearRect(0, 0, w, h);

      if (!series || series.length === 0) {{
        ctx.fillStyle = '#64748b';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('No telemetry data available for this range', w / 2, h / 2);
        return;
      }}

      const padLeft = 42;
      const padRight = 12;
      const padTop = 12;
      const padBottom = 22;
      const plotW = w - padLeft - padRight;
      const plotH = h - padTop - padBottom;

      const tMin = series[0].t;
      const tMax = series[series.length - 1].t;
      const tSpan = Math.max(1, tMax - tMin);

      let vMin = (options.yMin !== undefined) ? options.yMin : Math.min(...series.map(p => p.v));
      let vMax = (options.yMax !== undefined) ? options.yMax : Math.max(...series.map(p => p.v));
      if (vMin === vMax) {{ vMin -= 1; vMax += 1; }}
      const vSpan = vMax - vMin;

      function getX(t) {{ return padLeft + ((t - tMin) / tSpan) * plotW; }}
      function getY(v) {{ return padTop + plotH - ((v - vMin) / vSpan) * plotH; }}

      // Grid lines
      ctx.strokeStyle = '#1e293b';
      ctx.lineWidth = 1;
      ctx.fillStyle = '#64748b';
      ctx.font = '9px sans-serif';
      ctx.textAlign = 'right';

      const yTicks = 4;
      for (let i = 0; i <= yTicks; i++) {{
        const val = vMin + (vSpan / yTicks) * i;
        const y = getY(val);
        ctx.beginPath();
        ctx.moveTo(padLeft, y);
        ctx.lineTo(w - padRight, y);
        ctx.stroke();
        const lbl = options.formatY ? options.formatY(val) : val.toFixed(1);
        ctx.fillText(lbl, padLeft - 6, y + 3);
      }}

      // Fault Event Shading Bands
      if (faultEvents && faultEvents.length > 0) {{
        for (const f of faultEvents) {{
          const fStart = Math.max(tMin, f.start_time);
          const fEnd = Math.min(tMax, f.end_time || (Date.now() / 1000));
          if (fEnd >= tMin && fStart <= tMax) {{
            const x1 = getX(fStart);
            const x2 = getX(fEnd);
            const tgt = (f.target || 'all').toLowerCase();
            const col = LANG_COLORS[tgt] || LANG_COLORS['all'];

            ctx.fillStyle = col.fill;
            ctx.fillRect(x1, padTop, Math.max(3, x2 - x1), plotH);

            ctx.strokeStyle = col.stroke;
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(x1, padTop);
            ctx.lineTo(x1, padTop + plotH);
            ctx.stroke();

            ctx.fillStyle = col.text;
            ctx.font = 'bold 8px monospace';
            ctx.textAlign = 'left';
            ctx.fillText(`🏷️ ${{f.tag}}`, x1 + 3, padTop + 10);
          }}
        }}
      }}

      // Area gradient
      const grad = ctx.createLinearGradient(0, padTop, 0, padTop + plotH);
      grad.addColorStop(0, options.fillColor || 'rgba(56, 189, 248, 0.25)');
      grad.addColorStop(1, 'rgba(56, 189, 248, 0.0)');

      ctx.beginPath();
      ctx.moveTo(getX(series[0].t), getY(series[0].v));
      for (let i = 1; i < series.length; i++) {{
        ctx.lineTo(getX(series[i].t), getY(series[i].v));
      }}
      ctx.lineTo(getX(series[series.length - 1].t), padTop + plotH);
      ctx.lineTo(getX(series[0].t), padTop + plotH);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      // Line plot
      ctx.beginPath();
      ctx.strokeStyle = options.strokeColor || '#38bdf8';
      ctx.lineWidth = 2.0;
      ctx.lineJoin = 'round';
      ctx.moveTo(getX(series[0].t), getY(series[0].v));
      for (let i = 1; i < series.length; i++) {{
        ctx.lineTo(getX(series[i].t), getY(series[i].v));
      }}
      ctx.stroke();

      // Deploy and restart markers
      const markers = options.markers || [];
      for (const marker of markers) {{
        if (marker.time < tMin || marker.time > tMax) continue;
        const x = getX(marker.time);
        const isDeploy = marker.kind === 'deploy';
        const tgt = (marker.target || 'stack').toLowerCase();
        const col = isDeploy ? '#fbbf24' : ((LANG_COLORS[tgt] || LANG_COLORS['all']).stroke);
        ctx.save();
        ctx.strokeStyle = col;
        ctx.fillStyle = col;
        ctx.lineWidth = isDeploy ? 2 : 1.5;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(x, padTop);
        ctx.lineTo(x, padTop + plotH);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.font = 'bold 8px monospace';
        ctx.textAlign = 'left';
        ctx.fillText(marker.tag || marker.kind, x + 3, padTop + plotH - 4);
        ctx.restore();
      }}

      // Mousemove tooltip
      canvas.onmousemove = (ev) => {{
        const mRect = canvas.getBoundingClientRect();
        const mx = ev.clientX - mRect.left;
        if (mx < padLeft || mx > w - padRight) {{
          hideTooltip();
          return;
        }}
        const hoverT = tMin + ((mx - padLeft) / plotW) * tSpan;
        let closest = series[0];
        let minDist = Math.abs(series[0].t - hoverT);
        for (const pt of series) {{
          const dist = Math.abs(pt.t - hoverT);
          if (dist < minDist) {{ minDist = dist; closest = pt; }}
        }}

        let faultEvent = null;
        if (faultEvents) {{
          for (const f of faultEvents) {{
            const fEnd = f.end_time || (Date.now() / 1000);
            if (closest.t >= f.start_time && closest.t <= fEnd) {{
              faultEvent = f;
              break;
            }}
          }}
        }}

        let markerEvent = null;
        let markerDist = 12;
        for (const marker of (options.markers || [])) {{
          const dist = Math.abs(closest.t - marker.time);
          if (dist < markerDist) {{
            markerDist = dist;
            markerEvent = marker;
          }}
        }}

        showTooltip(ev.pageX, ev.pageY, closest, options, faultEvent, markerEvent);
      }};

      canvas.onmouseleave = () => hideTooltip();
    }}

    const tooltipEl = document.getElementById('chart-tooltip');
    function showTooltip(pageX, pageY, pt, options, faultEvent, markerEvent) {{
      const timeStr = new Date(pt.t * 1000).toLocaleTimeString();
      const metricLabel = options.metricLabel ? ` &bull; ${{options.metricLabel}}` : '';
      let html = `<div style="color:var(--muted); font-size:0.7rem;">${{timeStr}}${{metricLabel}}</div>
        <div style="font-weight:700; font-size:0.88rem; color:${{options.strokeColor}};">${{pt.v}} ${{options.unit || ''}}</div>`;
      if (faultEvent) {{
        const tgt = (faultEvent.target || 'all').toLowerCase();
        const col = LANG_COLORS[tgt] || LANG_COLORS['all'];
        html += `<div style="margin-top:3px; color:${{col.stroke}}; font-weight:700; font-size:0.72rem;">🏷️ [${{tgt.toUpperCase()}}] ${{faultEvent.tag}} (${{faultEvent.fault_type}})</div>`;
      }}
      if (markerEvent) {{
        const col = markerEvent.kind === 'deploy' ? '#fbbf24' : '#e2e8f0';
        html += `<div style="margin-top:3px; color:${{col}}; font-weight:700; font-size:0.72rem;">⚑ ${{markerEvent.tag}}</div>`;
      }}
      tooltipEl.innerHTML = html;
      tooltipEl.style.display = 'block';
      tooltipEl.style.left = (pageX + 10) + 'px';
      tooltipEl.style.top = (pageY - 28) + 'px';
    }}
    function hideTooltip() {{
      tooltipEl.style.display = 'none';
    }}

    function renderAllCharts(payload) {{
      if (!payload || !payload.metrics) return;
      const m = payload.metrics;
      const faults = payload.fault_events || [];
      const markers = payload.deploy_markers || [];

      // 1. Availability Chart
      const availPts = m.availability || [];
      if (availPts.length) {{
        const lastV = availPts[availPts.length - 1].v;
        document.getElementById('cur-avail').innerText = lastV + '%';
        document.getElementById('cur-avail').style.color = (lastV >= 99) ? '#10b981' : ((lastV >= 95) ? '#f59e0b' : '#ef4444');
      }}
      drawLineChart('chart-avail', availPts, faults, {{
        yMin: Math.min(85, Math.min(...availPts.map(p => p.v), 100)),
        yMax: 100,
        threshold: 99.0,
        strokeColor: '#10b981',
        fillColor: 'rgba(16, 185, 129, 0.2)',
        unit: '%',
        formatY: (v) => v.toFixed(0) + '%',
        markers: markers
      }});

      // 2. Error Rate Chart
      const errPts = m.errors || [];
      if (errPts.length) {{
        document.getElementById('cur-errors').innerText = errPts[errPts.length - 1].v + ' err/s';
      }}
      drawLineChart('chart-errors', errPts, faults, {{
        yMin: 0,
        strokeColor: '#ef4444',
        fillColor: 'rgba(239, 68, 68, 0.2)',
        unit: 'err/s',
        formatY: (v) => v.toFixed(1),
        markers: markers
      }});

      // 3. Latency Chart (Selected Percentile: p50, p90, p95, p99, p100)
      const pKey = `latency_${{currentLatencyPercentile}}`;
      const latPts = (m[pKey] && m[pKey].length) ? m[pKey] : (m.latency_ms || []);
      const pLabel = currentLatencyPercentile.toUpperCase();
      if (latPts.length) {{
        document.getElementById('cur-lat').innerText = latPts[latPts.length - 1].v + ' ms';
      }}
      drawLineChart('chart-latency', latPts, faults, {{
        yMin: 0,
        strokeColor: '#38bdf8',
        fillColor: 'rgba(56, 189, 248, 0.2)',
        unit: 'ms',
        metricLabel: `${{pLabel}} Latency`,
        formatY: (v) => v.toFixed(0) + 'ms',
        markers: markers
      }});

      // 4. Throughput Chart
      const tpsPts = m.throughput || [];
      if (tpsPts.length) {{
        document.getElementById('cur-tps').innerText = tpsPts[tpsPts.length - 1].v + ' req/s';
      }}
      drawLineChart('chart-tps', tpsPts, faults, {{
        yMin: 0,
        strokeColor: '#a855f7',
        fillColor: 'rgba(168, 85, 247, 0.2)',
        unit: 'req/s',
        formatY: (v) => v.toFixed(1),
        markers: markers
      }});
    }}

    window.addEventListener('resize', () => {{
      if (latestTrendsData) renderAllCharts(latestTrendsData);
    }});

    function init() {{
      initSqlEditor();
      fetchStats();
      fetchTrendsAndRefresh();
      setInterval(fetchStats, 3000);
      setInterval(fetchTrendsAndRefresh, 5000);
    }}

    window.addEventListener('DOMContentLoaded', init);
  </script>
</body>
</html>
"""
    return html_page.replace("__SHOWCASE_FOOTER__", _showcase_footer_html())

# ---------------------------------------------------------------------------
# Demo login. One shared account until real authentication exists.
# ---------------------------------------------------------------------------
_sessions: dict[str, str] = {}
_sessions_lock = threading.Lock()


def _secret_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(left.encode("utf-8")).digest(),
        hashlib.sha256(right.encode("utf-8")).digest(),
    )


def issue_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        if len(_sessions) > 200:
            _sessions.clear()
        _sessions[token] = username
    return token


def session_user(token: str) -> str | None:
    if not token:
        return None
    with _sessions_lock:
        return _sessions.get(token)


def drop_session(token: str) -> None:
    with _sessions_lock:
        _sessions.pop(token, None)


def render_login_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Canary login</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center;
           background: #090e17; color: #f1f5f9; font-family: system-ui, sans-serif; }
    form { width: min(360px, calc(100% - 32px)); background: #151f30; border: 1px solid #24344d;
           border-radius: 8px; padding: 22px; display: grid; gap: 10px; }
    h1 { margin: 0; font-size: 1.15rem; }
    p { margin: 0; color: #94a3b8; font-size: 0.85rem; }
    input { background: #0b1220; color: #f1f5f9; border: 1px solid #334155; border-radius: 4px; padding: 8px; }
    button { background: #38bdf8; color: #041226; border: 0; border-radius: 4px; padding: 8px; font-weight: 700; cursor: pointer; }
    .err { color: #fca5a5; min-height: 1.1em; font-size: 0.82rem; }
  </style>
</head>
<body>
  <form id="login-form">
    <h1>Canary dashboard</h1>
    <p>Demo account: <strong>demouser</strong> / <strong>demo</strong>. This is a placeholder login, not real authentication.</p>
    <input id="username" name="username" autocomplete="username" placeholder="Username" required>
    <input id="password" name="password" type="password" autocomplete="current-password" placeholder="Password" required>
    <button type="submit">Sign in</button>
    <div class="err" id="login-error"></div>
  </form>
  <script>
    document.getElementById('login-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: document.getElementById('username').value,
          password: document.getElementById('password').value
        })
      });
      if (res.ok) {
        window.location.href = '/';
        return;
      }
      document.getElementById('login-error').textContent = 'Invalid username or password';
    });
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP Server (Standard Library)
# ---------------------------------------------------------------------------
def start_dashboard_server(port: int) -> ThreadingHTTPServer:
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send_cors_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

        def _cookie_token(self) -> str:
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                part = part.strip()
                if part.startswith("canary_session="):
                    return part.split("=", 1)[1]
            return ""

        def _user(self) -> str | None:
            return session_user(self._cookie_token())

        def _send_bytes(self, status: int, payload: bytes, content_type: str, extra_headers=None) -> None:
            self.send_response(status)
            self._send_cors_headers()
            for key, value in extra_headers or []:
                self.send_header(key, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _session_cookie(self, token: str, clear: bool = False) -> str:
            host = self.headers.get("Host", "")
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" or "trycloudflare.com" in host else ""
            if clear:
                return f"canary_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0{secure}"
            return f"canary_session={token}; HttpOnly; SameSite=Lax; Path=/{secure}"

        def _send_login_page(self) -> None:
            self._send_bytes(200, render_login_html().encode("utf-8"), "text/html; charset=utf-8")

        def _require_user(self) -> str | None:
            user = self._user()
            if user:
                return user
            self._send_bytes(401, b'{"error":"login required"}', "application/json")
            return None

        def do_OPTIONS(self):
            self.send_response(204)
            self._send_cors_headers()
            self.end_headers()

        def do_GET(self):
            # Capture incoming external host if non-localhost
            host_header = self.headers.get("Host", "")
            if host_header:
                host_clean = host_header.split(":")[0].strip()
                if host_clean and host_clean not in ("localhost", "127.0.0.1", "0.0.0.0"):
                    state.last_external_host = host_clean

            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            if path in ("/login",) or (path in ("/", "/index.html") and not self._user()):
                self._send_login_page()
                return
            if path != "/health" and not self._user():
                self._send_bytes(401, b'{"error":"login required"}', "application/json")
                return

            if path.startswith("/open/"):
                status, headers, payload = proxy_open_request("GET", self.path, b"", self.headers)
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if path == "/api/network/host-ip":
                detected_ip = get_detected_host_ip()
                data = json.dumps({
                    "ip": detected_ip,
                    "port": CANARY_PORT,
                    "url": f"http://{detected_ip}:{CANARY_PORT}"
                }).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/" or path == "/index.html":
                body = render_dashboard_html().encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif path in ("/stats", "/json"):
                data = json.dumps(state.get_snapshot(), indent=2).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            elif path == "/api/crash":
                target = query.get("target", ["java"])[0]
                crash_type = query.get("type", ["process"])[0]
                tag = query.get("tag", [""])[0]
                res = trigger_target_crash(target, crash_type, tag)
                resp = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/trends":
                range_str = query.get("range", ["5m"])[0]
                service = query.get("service", ["all"])[0]
                percentile = query.get("percentile", ["p95"])[0].lower()
                trend_data = query_trend_metrics(range_str, service)
                if percentile in ("p50", "p90", "p95", "p99", "p100"):
                    trend_data["selected_percentile"] = percentile
                    p_key = f"latency_{percentile}"
                    if p_key in trend_data.get("metrics", {}):
                        trend_data["metrics"]["latency_ms"] = trend_data["metrics"][p_key]
                data = json.dumps(trend_data).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            elif path == "/api/faults":
                data = json.dumps(fault_manager.get_snapshot()).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            elif path == "/api/containers":
                data = json.dumps(docker_manager.get_containers_status(), indent=2).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            elif path == "/api/dependencies":
                data = json.dumps(check_dependencies(), indent=2).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            elif path == "/api/sql/saved-queries":
                try:
                    data = json.dumps(list_saved_queries(), indent=2).encode("utf-8")
                except Exception as exc:
                    data = json.dumps({"error": str(exc)}).encode("utf-8")
                    self._send_bytes(500, data, "application/json")
                    return
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            elif path == "/api/qrcode":
                default_host = get_detected_host_ip()
                is_https = (self.headers.get("X-Forwarded-Proto") == "https" or "trycloudflare.com" in default_host)
                proto = "https" if is_https else "http"
                default_url = f"{proto}://{default_host}"
                raw_target = query.get("url", [default_url])[0].strip()
                if not raw_target:
                    raw_target = default_url

                # Strip any unwanted port suffix (:8085, :80, :443) — port suffix is not needed!
                if "://" not in raw_target:
                    target_proto = "https" if "trycloudflare.com" in raw_target else "http"
                    raw_target = f"{target_proto}://{raw_target}"
                parsed_u = urllib.parse.urlsplit(raw_target)
                scheme = parsed_u.scheme or "http"
                host = parsed_u.hostname or (parsed_u.netloc.split(":")[0] if parsed_u.netloc else default_host)
                clean_target = urllib.parse.urlunsplit((scheme, host, parsed_u.path, parsed_u.query, parsed_u.fragment))

                if qrcode:
                    factory = qrcode.image.svg.SvgPathImage
                    img = qrcode.make(clean_target, image_factory=factory, box_size=10)
                    svg_bytes = img.to_string()
                else:
                    # Fallback clean SVG
                    svg_bytes = f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200"><rect width="200" height="200" fill="#ffffff"/><text x="100" y="100" font-family="sans-serif" font-size="12" text-anchor="middle" fill="#000000">{html.escape(clean_target)}</text></svg>'.encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("X-Encoded-Target", clean_target)
                self.send_header("Content-Length", str(len(svg_bytes)))
                self.end_headers()
                self.wfile.write(svg_bytes)

            elif path == "/health":
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(404)
                self._send_cors_headers()
                self.end_headers()

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""
            if path == "/api/login":
                try:
                    creds = json.loads(raw_body.decode("utf-8") or "{}")
                except Exception:
                    creds = {}
                username = str(creds.get("username") or "")
                password = str(creds.get("password") or "")
                if _secret_eq(username, CANARY_DEMO_USER) and _secret_eq(password, CANARY_DEMO_PASSWORD):
                    token = issue_session(username)
                    payload = json.dumps({"status": "ok", "username": username}).encode("utf-8")
                    self._send_bytes(200, payload, "application/json", [
                        ("Set-Cookie", self._session_cookie(token)),
                    ])
                else:
                    self._send_bytes(401, b'{"error":"invalid username or password"}', "application/json")
                return
            if path == "/api/logout":
                drop_session(self._cookie_token())
                self._send_bytes(200, b'{"status":"logged out"}', "application/json", [
                    ("Set-Cookie", self._session_cookie("", clear=True)),
                ])
                return
            user = self._require_user()
            if not user:
                return
            if path.startswith("/open/"):
                status, headers, payload = proxy_open_request("POST", self.path, raw_body, self.headers)
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            post_data = raw_body.decode("utf-8") if raw_body else ""

            body = {}
            if post_data:
                try:
                    body = json.loads(post_data)
                except Exception:
                    body = urllib.parse.parse_qs(post_data)
                    body = {k: v[0] for k, v in body.items()}

            if path == "/api/fault/start":
                tag = body.get("tag", "FAULT-DRILL")
                fault_type = body.get("fault_type", "error_spike")
                target = body.get("target", "all")
                duration = int(body.get("duration_sec", 60))
                rate = int(body.get("rate", 100))
                delay_ms = int(body.get("delay_ms", 1000))

                active = fault_manager.start_fault(tag, fault_type, target, duration, rate, delay_ms)
                resp = json.dumps({"status": "started", "fault": active}).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/fault/stop":
                stopped = fault_manager.stop_fault()
                resp = json.dumps({"status": "stopped", "fault": stopped}).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/tps":
                tps = float(body.get("tps", 12))
                duration = int(body.get("duration_sec", 60))
                res = state.set_tps_override(tps, duration)
                resp = json.dumps({"status": "applied", **res}).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/crash":
                target = body.get("target", "java")
                crash_type = body.get("type", "process")
                tag = body.get("tag", "")
                res = trigger_target_crash(target, crash_type, tag)
                resp = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/tps/reset":
                res = state.clear_tps_override()
                resp = json.dumps({"status": "reset", **res}).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/markers":
                kind = (body.get("kind") or "deploy").lower()
                target = body.get("target") or "stack"
                tag = body.get("tag") or kind
                event = deploy_markers.record(kind, target, tag)
                resp = json.dumps({"status": "recorded", "marker": event}).encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/containers/action":
                lang = body.get("language", "")
                action = body.get("action", "")
                res = docker_manager.container_action(lang, action)
                status_code = 200 if res.get("status") == "success" else 400
                resp = json.dumps(res).encode("utf-8")
                self.send_response(status_code)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/sql/saved-queries":
                name = (body.get("name") or "").strip()
                sql = body.get("sql") or body.get("query") or ""
                if not name or not str(sql).strip():
                    self._send_bytes(400, b'{"error":"name and sql are required"}', "application/json")
                    return
                try:
                    saved = save_saved_query(name, str(sql), body.get("description") or "", user)
                except SqlGuardError as exc:
                    payload = json.dumps({"status": "error", "error": str(exc)}).encode("utf-8")
                    self._send_bytes(400, payload, "application/json")
                    return
                except Exception as exc:
                    payload = json.dumps({"status": "error", "error": str(exc)}).encode("utf-8")
                    self._send_bytes(500, payload, "application/json")
                    return
                self._send_bytes(200, json.dumps({"status": "saved", "query": saved}).encode("utf-8"), "application/json")
                return

            elif path == "/api/sql/query":
                query_str = body.get("query", "")
                limit = int(body.get("limit", 100))
                client_addr = self.client_address[0] if self.client_address else ""
                res = execute_sql_query(query_str, max_rows=limit, username=user, client_addr=client_addr)
                status_code = 200 if res.get("status") == "success" else 400
                resp = json.dumps(res).encode("utf-8")
                self.send_response(status_code)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            else:
                self.send_response(404)
                self._send_cors_headers()
                self.end_headers()

        def log_message(self, format, *args):
            pass

    class DashboardServer(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            exc = sys.exception()
            if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
                return
            super().handle_error(request, client_address)

    server = DashboardServer(("0.0.0.0", port), DashboardHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info("Canary dashboard listening at http://0.0.0.0:%d/", port)
    return server

# ---------------------------------------------------------------------------
# Wait for Apps & Main Execution
# ---------------------------------------------------------------------------
def wait_for_apps(session: requests.Session) -> None:
    """Non-blocking discovery probe at startup. Probes each container with a short timeout.
    Does NOT block startup if any container is stopped or still warming up."""
    logger.info("Performing startup probe of available microservices...")
    statuses = docker_manager.get_containers_status()
    for name, lang, url in [("Python", "python", APP_BASE_URL),
                            ("Java", "java", JAVA_APP_BASE_URL),
                            ("Rust", "rust", RUST_APP_BASE_URL),
                            ("Node", "node", NODE_APP_BASE_URL),
                            ("Go", "go", GO_APP_BASE_URL),
                            ("Dotnet", "dotnet", DOTNET_APP_BASE_URL),
                            ("C", "c", C_APP_BASE_URL)]:
        c_info = statuses.get(lang, {})
        is_running = c_info.get("is_running", True)
        if not is_running:
            state.set_app_active(lang, False)
            state.mark_unreachable(lang)
            logger.info("%s container is STOPPED in Docker. Canary workload will remain paused.", name)
            continue

        try:
            resp = session.get(f"{url}/", timeout=1.0)
            if resp.status_code < 500:
                logger.info("%s App is reachable at %s", name, url)
                state.mark_reachable(lang)
                state.set_app_active(lang, True)
            else:
                logger.warning("%s App returned %d, will warm up in background.", name, resp.status_code)
                state.mark_unreachable(lang)
                state.set_app_active(lang, True)
        except Exception as exc:
            logger.warning("%s App not responding yet (%s), will warm up in background.", name, exc)
            state.mark_unreachable(lang)
            state.set_app_active(lang, True)

def run() -> None:
    try:
        ensure_sql_editor_schema()
    except Exception as exc:
        logger.warning("SQL editor tables are not ready yet: %s", exc)
    start_dashboard_server(CANARY_PORT)

    session = requests.Session()
    try:
        vk = redis.Redis(host=VALKEY_HOST, port=6379, decode_responses=True)
    except Exception as e:
        logger.warning("Valkey connection error: %s", e)
        vk = None

    logger.info("Canary starting — Base TPS: %.0f, Mimir: %s", CANARY_TPS, MIMIR_URL)
    deploy_markers.attach(vk)

    wait_for_apps(session)
    state.set_running()
    start_docker_sync_thread()
    logger.info("Starting canary loop with dynamic TPS, power management, and color-coded fault injection")

    next_tick = time.time()
    last_warmup_check = 0.0

    app_list = [
        ("python", APP_BASE_URL),
        ("java", JAVA_APP_BASE_URL),
        ("rust", RUST_APP_BASE_URL),
        ("node", NODE_APP_BASE_URL),
        ("go", GO_APP_BASE_URL),
        ("dotnet", DOTNET_APP_BASE_URL),
        ("c", C_APP_BASE_URL)
    ]
    app_idx = 0
    target_idx = 0

    while True:
        now_loop = time.time()

        # 1. Warmup probe for active but unreachable containers (e.g. newly started JVM or Go container)
        if now_loop - last_warmup_check >= 1.5:
            last_warmup_check = now_loop
            for app_lang, base_url in app_list:
                if state.is_app_active(app_lang) and not state.is_reachable(app_lang):
                    try:
                        probe_resp = session.get(f"{base_url}/", timeout=1.0)
                        if probe_resp.status_code < 500:
                            state.mark_reachable(app_lang)
                            logger.info("Container %s is READY at %s! Synthetic traffic resumed.", app_lang.upper(), base_url)
                    except Exception:
                        pass

        # 2. Gather active and ready containers
        ready_apps = [
            (lang, url) for lang, url in app_list
            if state.is_app_active(lang) and state.is_reachable(lang)
        ]

        if not ready_apps:
            # All containers stopped or still warming up — sleep cleanly without CPU spin
            time.sleep(0.3)
            next_tick = time.time()
            continue

        # 3. Interleaved round-robin across ready apps
        app_lang, base_url = ready_apps[app_idx % len(ready_apps)]
        app_idx += 1

        target = TARGETS[target_idx % len(TARGETS)]
        target_idx += 1

        path         = target["path"]
        method       = target["method"]
        expected_4xx = target.get("expected_4xx", False)
        url          = f"{base_url}{path}"

        t_start = time.time()
        status  = "error"
        fault_tag = None

        # Check Fault Injection
        inject_fault, active_fault = fault_manager.should_inject(app_lang)

        if inject_fault and active_fault:
            fault_tag = active_fault["tag"]
            f_type = active_fault["fault_type"]

            if f_type == "error_spike":
                try:
                    resp = session.get(f"{base_url}/fault-injected-error-drill", timeout=3)
                except Exception:
                    pass
                status = "error"
                fault_manager.record_affected(is_error=True)

            elif f_type == "service_outage":
                time.sleep(0.04)
                status = "error"
                fault_manager.record_affected(is_error=True)

            elif f_type == "high_latency":
                delay_s = active_fault.get("delay_ms", 1000) / 1000.0
                time.sleep(delay_s)
                try:
                    resp = session.get(url, timeout=5)
                    status = "success" if (expected_4xx and 400 <= resp.status_code < 500) or (not expected_4xx and resp.status_code < 400) else "error"
                except Exception:
                    status = "error"
                fault_manager.record_affected(is_error=(status == "error"))

            elif f_type == "intermittent_errors":
                if random.random() < 0.5:
                    status = "error"
                    fault_manager.record_affected(is_error=True)
                else:
                    try:
                        resp = session.get(url, timeout=5)
                        status = "success" if resp.status_code < 400 else "error"
                    except Exception:
                        status = "error"
                    fault_manager.record_affected(is_error=(status == "error"))

            elif f_type in ("process_crash", "thread_crash"):
                status = "error"
                fault_manager.record_affected(is_error=True)
        else:
            try:
                if method == "POST":
                    if "json" in target:
                        resp = session.post(url, json=target["json"], timeout=5)
                    else:
                        resp = session.post(url, data=target.get("data", {}), timeout=5)
                else:
                    resp = session.get(url, timeout=5)

                if expected_4xx:
                    status = "success" if 400 <= resp.status_code < 500 else "error"
                else:
                    status = "success" if resp.status_code < 400 else "error"

            except Exception as exc:
                logger.debug("Request failed %s %s: %s", method, path, exc)
                status = "error"

        response_time = time.time() - t_start
        base_path = path.split("?")[0]
        labels = {
            "path": base_path,
            "method": method,
            "status": status,
            "client": "canary",
            "language": app_lang
        }

        request_counter.add(1, labels)
        request_duration.record(response_time, labels)

        state.record_request(app_lang, method, path, status, response_time, fault_tag=fault_tag)

        if vk:
            try:
                field = f"{app_lang}:{base_path}:{status}"
                vk.hincrby("canary:total_requests", field, 1)

                duration_field = f"{app_lang}:{base_path}"
                vk.hincrbyfloat("canary:duration_sum", duration_field, response_time)
                vk.hincrby("canary:duration_count", duration_field, 1)
            except Exception as vk_exc:
                logger.debug("Failed to report to Valkey: %s", vk_exc)

        # Dynamic TPS inter-arrival
        current_tps = state.get_effective_tps()
        inter_arrival = 1.0 / max(0.5, current_tps)
        next_tick += inter_arrival
        now_t = time.time()
        if next_tick < now_t - 1.0:
            schedule_behind_counter.add(1, {"language": app_lang})
            next_tick = now_t
        sleep_time = max(0.0, next_tick - now_t)
        time.sleep(sleep_time)

if __name__ == "__main__":
    run()
