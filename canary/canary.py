"""
canary.py — Standalone synthetic canary for the observability-demo stack.
Generates synthetic traffic across Python, Java, Rust, Node, Go, .NET, and C microservices,
exports OpenTelemetry telemetry, logs to Valkey, supports Fault Injection Testing
with language-specific color-coded event tagging, dynamic TPS override controls,
container lifecycle/power management, dependency health telemetry, an SSMS/pgAdmin-style
SQL query editor, and a compact, high-efficiency dashboard with collapsible controls.
"""

from collections import deque
import html
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import math
import os
import random
import socket
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
            return {"status": "success", "action": "stop", "language": lang, "docker_code": code}
        elif action_clean == "start":
            code, data = self._request("POST", f"/containers/{cid}/start")
            state.set_app_active(lang.lower(), True)
            state.mark_unreachable(lang.lower())
            logger.info("Power Control: Started container %s (%s). Canary workload queued for warmup.", container_name, cid)
            return {"status": "success", "action": "start", "language": lang, "docker_code": code}
        elif action_clean == "restart":
            code, data = self._request("POST", f"/containers/{cid}/restart?t=3")
            state.set_app_active(lang.lower(), True)
            state.mark_unreachable(lang.lower())
            logger.info("Power Control: Restarted container %s (%s). Canary workload queued for warmup.", container_name, cid)
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
        "name": "📜 Recent Audit Logs (All Languages)",
        "description": "Fetches the 50 most recent synthetic audit log entries across all microservices.",
        "sql": "SELECT id, created_at, endpoint, status_code, coalesce(details->>'language', 'python') AS language, coalesce(details->>'service', 'python-app') AS service FROM audit_logs ORDER BY id DESC LIMIT 50;"
    },
    {
        "id": "lang_audit_counts",
        "name": "📊 Audit Log Volume by Language",
        "description": "Rolls up total audit entries and timestamps grouped by microservice language.",
        "sql": "SELECT coalesce(details->>'language', 'python') AS language, count(*) AS total_entries, min(created_at) AS first_entry, max(created_at) AS latest_entry FROM audit_logs GROUP BY 1 ORDER BY total_entries DESC;"
    },
    {
        "id": "action_breakdown",
        "name": "⚡ Endpoint Hit & Duration Breakdown",
        "description": "Analyzes the frequency of routes, status codes, and average response times.",
        "sql": "SELECT endpoint, status_code, count(*) AS total_calls, round(avg(response_time_seconds)::numeric, 4) AS avg_duration_sec FROM audit_logs GROUP BY endpoint, status_code ORDER BY total_calls DESC;"
    },
    {
        "id": "hourly_activity",
        "name": "⏱️ Hourly Activity (Past 24 Hours)",
        "description": "Aggregates total audit activity by 1-hour time windows.",
        "sql": "SELECT date_trunc('hour', created_at) AS hour_window, count(*) AS total_records FROM audit_logs GROUP BY 1 ORDER BY hour_window DESC LIMIT 24;"
    },
    {
        "id": "table_sizes",
        "name": "💾 Postgres Table Sizes & Row Estimates",
        "description": "Inspects relational table sizes, live tuples, and vacuum stats from pg_stat_user_tables.",
        "sql": "SELECT relname AS table_name, n_live_tup AS estimated_rows, pg_size_pretty(pg_total_relation_size(relid)) AS total_size, last_vacuum, last_autovacuum FROM pg_stat_user_tables ORDER BY n_live_tup DESC;"
    },
    {
        "id": "db_connections",
        "name": "🔌 Active Database Connections",
        "description": "Lists current client connections and queries from pg_stat_activity.",
        "sql": "SELECT pid, datname, usename, client_addr, state, query_start, wait_event_type, wait_event, left(query, 60) AS query_preview FROM pg_stat_activity WHERE datname = 'observability' LIMIT 20;"
    },
    {
        "id": "schema_tables",
        "name": "🗄️ Information Schema Table Catalog",
        "description": "Queries database catalog tables and types in the observability schema.",
        "sql": "SELECT table_schema, table_name, table_type FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY table_schema, table_name;"
    }
]

def execute_sql_query(query_str: str, max_rows: int = 100) -> dict:
    """Executes SQL query against PostgreSQL with safety timeout and formatting."""
    if not psycopg2:
        return {
            "status": "error",
            "error": "psycopg2 driver not installed in canary runtime",
            "query": query_str
        }

    clean_q = query_str.strip()
    if not clean_q:
        return {"status": "error", "error": "Query cannot be empty", "query": query_str}

    t0 = time.time()
    conn = None
    try:
        conn = psycopg2.connect(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            dbname=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            connect_timeout=4,
            options="-c statement_timeout=5000"
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(clean_q)

        if cur.description:
            columns = [desc[0] for desc in cur.description]
            raw_rows = cur.fetchmany(max_rows)
            formatted_rows = []
            for row in raw_rows:
                formatted_row = []
                for val in row:
                    if val is None:
                        formatted_row.append(None)
                    elif isinstance(val, (dict, list)):
                        formatted_row.append(json.dumps(val))
                    elif hasattr(val, "isoformat"):
                        formatted_row.append(val.isoformat())
                    else:
                        formatted_row.append(str(val))
                formatted_rows.append(formatted_row)

            elapsed_ms = round((time.time() - t0) * 1000, 2)
            return {
                "status": "success",
                "columns": columns,
                "rows": formatted_rows,
                "row_count": len(formatted_rows),
                "execution_time_ms": elapsed_ms,
                "truncated": len(raw_rows) >= max_rows,
                "query": clean_q
            }
        else:
            elapsed_ms = round((time.time() - t0) * 1000, 2)
            rowcount = cur.rowcount
            return {
                "status": "success",
                "columns": ["status", "rows_affected"],
                "rows": [["Command Executed Successfully", str(rowcount)]],
                "row_count": 1,
                "execution_time_ms": elapsed_ms,
                "query": clean_q
            }
    except Exception as exc:
        elapsed_ms = round((time.time() - t0) * 1000, 2)
        return {
            "status": "error",
            "error": str(exc),
            "execution_time_ms": elapsed_ms,
            "query": clean_q
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

            self.recent_requests.append({
                "time": time.strftime("%H:%M:%S", time.localtime(now)),
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
    def sync_loop():
        while True:
            try:
                statuses = docker_manager.get_containers_status()
                for lang, info in statuses.items():
                    is_run = info.get("is_running", False)
                    if not is_run:
                        if state.is_app_active(lang):
                            state.set_app_active(lang, False)
                            state.mark_unreachable(lang)
                    else:
                        if not state.is_app_active(lang):
                            state.set_app_active(lang, True)
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

    return {
        "range": range_str,
        "service": service,
        "start_time": start,
        "end_time": now,
        "step": step,
        "metrics": results,
        "fault_events": fault_events
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
# Compact, High-Efficiency HTML Dashboard Rendering
# ---------------------------------------------------------------------------
def render_dashboard_html() -> str:
    return f"""<!doctype html>
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
    .sql-textarea {{
      width: 100%;
      background: #090e17;
      border: 1px solid var(--card-border);
      border-radius: 4px;
      color: #7dd3fc;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.84rem;
      padding: 8px 10px;
      line-height: 1.4;
      resize: vertical;
      min-height: 85px;
      outline: none;
      margin-bottom: 10px;
    }}
    .sql-textarea:focus {{ border-color: var(--accent); }}
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
    .dep-meta-row {{ display: flex; justify-content: space-between; font-size: 0.74rem; }}

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
          <span style="font-size:0.75rem; color:var(--muted); font-weight:normal;">(Availability, Errors, Latency, Throughput)</span>
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
              <th style="width: 80px;">Time</th>
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
          <span class="badge badge-success">Target: postgres:5432 (observability)</span>
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
            <button class="btn btn-accent" onclick="runSqlQuery()">▶ Run Query (Ctrl+Enter)</button>
            <button class="btn btn-secondary" onclick="clearSqlQuery()">🧹 Clear</button>
            <button class="btn btn-outline" onclick="exportSqlResults('csv')">📥 Export CSV</button>
            <button class="btn btn-outline" onclick="exportSqlResults('json')">📥 Export JSON</button>
          </div>
        </div>

        <textarea id="sql-query-input" class="sql-textarea" rows="4" spellcheck="false" placeholder="Enter SQL query here... e.g. SELECT * FROM audit_logs ORDER BY id DESC LIMIT 50;"></textarea>

        <div class="sql-meta-bar" id="sql-status-bar">
          <span id="sql-status-text">Ready &bull; Press Ctrl+Enter or click Run Query</span>
          <span id="sql-timing-text">-- ms</span>
        </div>

        <!-- First-Principles Data Grid -->
        <div class="sql-grid-wrapper" id="sql-grid-wrapper">
          <div id="sql-grid-empty" style="padding: 30px; text-align: center; color: var(--muted);">
            No query results to display. Select a saved query above or run your own SQL.
          </div>
          <table class="sql-grid-table" id="sql-grid-table" style="display: none;">
            <thead id="sql-grid-thead"></thead>
            <tbody id="sql-grid-tbody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <footer>
      <div>Canary Load Generator &bull; Polyglot Telemetry &bull; Color-Coded Fault Injection &bull; Dynamic TPS Override &bull; Container Power Control &bull; SQL Data Grid</div>
      <div><a href="/stats" target="_blank">JSON Snapshot</a> &bull; <a href="/api/trends?range=5m" target="_blank">Trends API</a> &bull; <a href="/api/dependencies" target="_blank">Dependencies API</a></div>
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
          <td>${{r.time}}</td>
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

          cardsHtml += `<div class="dep-card" style="border-left: 3px solid ${{isUp ? '#10b981' : '#ef4444'}};">
            <div class="dep-card-header">
              <div>
                <div class="dep-name">${{dep.name}}</div>
                <div class="dep-role">${{dep.role}}</div>
              </div>
              <div>${{statusBadge}}</div>
            </div>
            <div class="dep-meta-row">
              <span style="color:var(--muted);">Endpoint / Protocol:</span>
              <code>${{dep.endpoint}}</code>
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
        const textarea = document.getElementById('sql-query-input');
        if (!textarea.value.trim() && list.length > 0) {{
          textarea.value = list[0].sql;
          sel.value = list[0].id;
        }}
      }} catch (err) {{
        console.error("Error loading saved queries:", err);
      }}
    }}

    function onSelectSavedQuery(qid) {{
      if (window._savedQueriesMap && window._savedQueriesMap[qid]) {{
        document.getElementById('sql-query-input').value = window._savedQueriesMap[qid];
      }}
    }}

    function clearSqlQuery() {{
      document.getElementById('sql-query-input').value = '';
      document.getElementById('sql-saved-select').value = '';
      document.getElementById('sql-grid-empty').style.display = 'block';
      document.getElementById('sql-grid-table').style.display = 'none';
      document.getElementById('sql-status-text').innerText = 'Ready &bull; Cleared';
      document.getElementById('sql-timing-text').innerText = '-- ms';
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
        }} else {{
          statusText.innerHTML = `❌ Error: <span style="color:#ef4444;">${{data.error}}</span>`;
          timingText.innerText = `${{data.execution_time_ms || 0}} ms`;
          document.getElementById('sql-grid-empty').innerHTML = `<div style="color:#ef4444; font-family:monospace; padding:20px;">SQL Error: ${{data.error}}</div>`;
          document.getElementById('sql-grid-empty').style.display = 'block';
          document.getElementById('sql-grid-table').style.display = 'none';
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

        showTooltip(ev.pageX, ev.pageY, closest, options, faultEvent);
      }};

      canvas.onmouseleave = () => hideTooltip();
    }}

    const tooltipEl = document.getElementById('chart-tooltip');
    function showTooltip(pageX, pageY, pt, options, faultEvent) {{
      const timeStr = new Date(pt.t * 1000).toLocaleTimeString();
      const metricLabel = options.metricLabel ? ` &bull; ${{options.metricLabel}}` : '';
      let html = `<div style="color:var(--muted); font-size:0.7rem;">${{timeStr}}${{metricLabel}}</div>
        <div style="font-weight:700; font-size:0.88rem; color:${{options.strokeColor}};">${{pt.v}} ${{options.unit || ''}}</div>`;
      if (faultEvent) {{
        const tgt = (faultEvent.target || 'all').toLowerCase();
        const col = LANG_COLORS[tgt] || LANG_COLORS['all'];
        html += `<div style="margin-top:3px; color:${{col.stroke}}; font-weight:700; font-size:0.72rem;">🏷️ [${{tgt.toUpperCase()}}] ${{faultEvent.tag}} (${{faultEvent.fault_type}})</div>`;
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
        formatY: (v) => v.toFixed(0) + '%'
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
        formatY: (v) => v.toFixed(1)
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
        formatY: (v) => v.toFixed(0) + 'ms'
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
        formatY: (v) => v.toFixed(1)
      }});
    }}

    window.addEventListener('resize', () => {{
      if (latestTrendsData) renderAllCharts(latestTrendsData);
    }});

    function init() {{
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

# ---------------------------------------------------------------------------
# HTTP Server (Standard Library)
# ---------------------------------------------------------------------------
def start_dashboard_server(port: int) -> HTTPServer:
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send_cors_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

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
                data = json.dumps(SAVED_QUERIES, indent=2).encode("utf-8")
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
            post_data = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""

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

            elif path == "/api/sql/query":
                query_str = body.get("query", "")
                limit = int(body.get("limit", 100))
                res = execute_sql_query(query_str, max_rows=limit)
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

    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
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
    start_dashboard_server(CANARY_PORT)

    session = requests.Session()
    try:
        vk = redis.Redis(host=VALKEY_HOST, port=6379, decode_responses=True)
    except Exception as e:
        logger.warning("Valkey connection error: %s", e)
        vk = None

    logger.info("Canary starting — Base TPS: %.0f, Mimir: %s", CANARY_TPS, MIMIR_URL)

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
