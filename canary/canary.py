"""
canary.py — Standalone synthetic canary for the observability-demo stack.
Generates synthetic traffic across Python, Java, and Rust microservices,
exports OpenTelemetry telemetry, logs to Valkey, supports Fault Injection Testing
with language-specific color-coded event tagging, dynamic TPS override controls,
historical Mimir trend queries, and a compact, high-efficiency dashboard with
both Trend Graphs and Current Raw Numbers views.
"""

from collections import deque
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import math
import os
import random
import threading
import time
import urllib.parse

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
OTLP_ENDPOINT        = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
MIMIR_URL            = os.getenv("MIMIR_URL",           "http://mimir:9009")
VALKEY_HOST          = os.getenv("VALKEY_HOST",         "valkey")
CANARY_TPS           = float(os.getenv("CANARY_TPS",    "6"))
CANARY_PORT          = int(os.getenv("CANARY_PORT",     "8085"))
REFRESH_INTERVAL_SEC = int(os.getenv("CANARY_REFRESH_INTERVAL", "3"))

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
# Fault Injection Manager (Language-specific color coding & tagging)
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
            if target_clean not in ("all", "python", "java", "rust", "node", "go", "dotnet"):
                target_clean = "all"

            fault = {
                "id": f"fault_{int(now)}_{random.randint(1000, 9999)}",
                "tag": clean_tag,
                "fault_type": fault_type,      # error_spike, high_latency, service_outage, intermittent_errors
                "target": target_clean,         # all, python, java, rust
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
        self.apps = {
            "python": {"url": APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "java":   {"url": JAVA_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "rust":   {"url": RUST_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "node":   {"url": NODE_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "go":     {"url": GO_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
            "dotnet": {"url": DOTNET_APP_BASE_URL, "reachable": False, "total": 0, "success": 0, "error": 0, "duration_sum": 0.0},
        }
        self.endpoints = {}
        self.recent_requests = deque(maxlen=30)
        self.durations_window = deque(maxlen=1000)
        self.last_target = ""

        # 5-second bucket in-memory time series ring buffer (past 60 minutes)
        self.ts_buckets = {}

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
            if lang in self.apps:
                self.apps[lang]["reachable"] = True

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
                    "apps": {"python": {"t": 0, "s": 0, "e": 0, "ds": 0.0},
                             "java":   {"t": 0, "s": 0, "e": 0, "ds": 0.0},
                             "rust":   {"t": 0, "s": 0, "e": 0, "ds": 0.0},
                             "node":   {"t": 0, "s": 0, "e": 0, "ds": 0.0},
                             "go":     {"t": 0, "s": 0, "e": 0, "ds": 0.0},
                             "dotnet": {"t": 0, "s": 0, "e": 0, "ds": 0.0}}
                }
            b = self.ts_buckets[bucket_ts]
            b["total"] += 1
            b["dur_sum"] += duration
            b["dur_cnt"] += 1
            if is_success:
                b["success"] += 1
            else:
                b["error"] += 1

            if lang in b["apps"]:
                ba = b["apps"][lang]
                ba["t"] += 1
                ba["ds"] += duration
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

            # Check override status
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

            for b_ts in sorted(self.ts_buckets.keys()):
                if b_ts < start_ts:
                    continue
                b = self.ts_buckets[b_ts]
                if service in ("python", "java", "rust", "node", "go", "dotnet"):
                    ba = b["apps"].get(service, {"t": 0, "s": 0, "e": 0, "ds": 0.0})
                    tot = ba["t"]
                    succ = ba["s"]
                    err = ba["e"]
                    dur_s = ba["ds"]
                else:
                    tot = b["total"]
                    succ = b["success"]
                    err = b["error"]
                    dur_s = b["dur_sum"]

                tps = round(tot / 5.0, 2)
                err_rate = round(err / 5.0, 2)
                avail = round((succ / tot * 100.0), 2) if tot > 0 else 100.0
                avg_l = round((dur_s / tot * 1000.0), 1) if tot > 0 else 0.0

                pts_throughput.append({"t": b_ts, "v": tps})
                pts_errors.append({"t": b_ts, "v": err_rate})
                pts_availability.append({"t": b_ts, "v": avail})
                pts_latency.append({"t": b_ts, "v": avg_l})

            return {
                "throughput": pts_throughput,
                "errors": pts_errors,
                "availability": pts_availability,
                "latency_ms": pts_latency
            }

state = CanaryState()

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

    service_filter = f'language="{service}",' if service in ("python", "java", "rust", "node", "go", "dotnet") else ""

    queries = {
        "throughput": f'sum(rate(app_synthetic_requests_total{{{service_filter}}}[{rate_win}])) or vector(0)',
        "errors": f'sum(rate(app_synthetic_requests_total{{{service_filter}status="error"}}[{rate_win}])) or vector(0)',
        "availability": f'clamp_max(clamp_min((sum(rate(app_synthetic_requests_total{{{service_filter}status="success"}}[{rate_win}])) / sum(rate(app_synthetic_requests_total{{{service_filter}}}[{rate_win}]))) * 100, 0), 100) or vector(100)',
        "latency_ms": f'(sum(rate(app_synthetic_request_duration_sum{{{service_filter}}}[{rate_win}])) / sum(rate(app_synthetic_request_duration_count{{{service_filter}}}[{rate_win}]))) * 1000 or vector(0)'
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

    if not mimir_success or (seconds <= 3600 and (not results.get("throughput") or len(results["throughput"]) < 5)):
        mem_data = state.get_inmemory_trends(seconds, service)
        for k in ("throughput", "errors", "availability", "latency_ms"):
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
    .header-controls {{ display: flex; align-items: center; gap: 8px; }}

    /* View Switcher Tabs */
    .view-tabs {{
      display: inline-flex;
      background: #101726;
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 2px;
      gap: 2px;
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

    /* Compact 3-Column Control Panels (Fault Injection + Crash Trigger + TPS Override) */
    .controls-grid {{
      display: grid;
      grid-template-columns: 1.15fr 1.15fr 1fr;
      gap: 10px;
      margin-bottom: 14px;
    }}
    @media (max-width: 1280px) {{
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
    .badge-python {{ background: #1e3a8a; color: #93c5fd; }}
    .badge-java {{ background: #7c2d12; color: #fdba74; }}
    .badge-rust {{ background: #701a75; color: #f0abfc; }}
    .badge-node {{ background: #064e3b; color: #86efac; }}
    .badge-go   {{ background: #083344; color: #67e8f9; }}
    .badge-dotnet {{ background: #2e1065; color: #c4b5fd; }}

    /* Language-specific Fault Badges */
    .badge-fault-java   {{ background: #7c2d12; color: #fed7aa; border: 1px solid #f97316; }}
    .badge-fault-python {{ background: #1e3a8a; color: #bfdbfe; border: 1px solid #3b82f6; }}
    .badge-fault-rust   {{ background: #701a75; color: #f5d0fe; border: 1px solid #d946ef; }}
    .badge-fault-node   {{ background: #064e3b; color: #bbf7d0; border: 1px solid #22c55e; }}
    .badge-fault-go     {{ background: #083344; color: #a5f3fc; border: 1px solid #06b6d4; }}
    .badge-fault-dotnet {{ background: #2e1065; color: #ddd6fe; border: 1px solid #8b5cf6; }}
    .badge-fault-all    {{ background: #7f1d1d; color: #fecaca; border: 1px solid #ef4444; }}

    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.76rem;
      background: #0d1422;
      padding: 1px 4px;
      border-radius: 3px;
    }}

    .scroll-table-container {{
      max-height: 220px;
      overflow-y: auto;
    }}

    footer {{
      margin-top: 14px;
      padding-top: 8px;
      border-top: 1px solid var(--card-border);
      display: flex;
      justify-content: space-between;
      color: var(--muted);
      font-size: 0.74rem;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <div id="chart-tooltip"></div>
  <div id="toast-notice" class="toast-notice"></div>
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
        </div>
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

    <!-- Compact Dual Controls: Fault Injection Testing & TPS Override -->
    <div class="controls-grid" id="controls-section">
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
              <option value="all">🔴 All (Python/Java/Rust/Node/Go/.NET)</option>
              <option value="java">🟠 Java (Spring Boot)</option>
              <option value="python">🔵 Python (Flask)</option>
              <option value="rust">🟣 Rust (Axum)</option>
              <option value="node">🟢 Node.js (Express)</option>
              <option value="go">🩵 Go (net/http)</option>
              <option value="dotnet">💜 .NET (C#)</option>
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
          </div>
        </div>
      </div>

      <!-- 2x2 Charts Grid -->
      <div class="charts-grid">
        <!-- 1. Availability Chart -->
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title">
              <span style="color:#10b981;">●</span> Availability Trend (%)
            </div>
            <div class="chart-current" id="cur-avail" style="color:#10b981;">--%</div>
          </div>
          <div class="canvas-container">
            <canvas id="chart-avail"></canvas>
          </div>
        </div>

        <!-- 2. Errors Chart -->
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title">
              <span style="color:#ef4444;">●</span> Error Rate (Errors / sec)
            </div>
            <div class="chart-current" id="cur-errors" style="color:#ef4444;">-- err/s</div>
          </div>
          <div class="canvas-container">
            <canvas id="chart-errors"></canvas>
          </div>
        </div>

        <!-- 3. Latencies Chart -->
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title">
              <span style="color:#38bdf8;">●</span> Latency Trend (Average ms)
            </div>
            <div class="chart-current" id="cur-lat" style="color:#38bdf8;">-- ms</div>
          </div>
          <div class="canvas-container">
            <canvas id="chart-latency"></canvas>
          </div>
        </div>

        <!-- 4. Throughput Chart -->
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title">
              <span style="color:#a855f7;">●</span> Throughput Trend (TPS / sec)
            </div>
            <div class="chart-current" id="cur-tps" style="color:#a855f7;">-- req/s</div>
          </div>
          <div class="canvas-container">
            <canvas id="chart-tps"></canvas>
          </div>
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
              <th>Health</th>
              <th style="text-align:right;">Requests</th>
              <th style="text-align:right;">Success</th>
              <th style="text-align:right;">Errors</th>
              <th style="text-align:right;">Availability</th>
              <th style="text-align:right;">Avg Latency</th>
            </tr>
          </thead>
          <tbody id="apps-table-body">
            <tr><td colspan="8" style="text-align:center; color:var(--muted);">Loading raw numbers...</td></tr>
          </tbody>
        </table>
      </div>

      <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px;">
        <!-- Endpoint breakdown -->
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

        <!-- Fault injection history -->
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

    <footer>
      <div>Canary Load Generator &bull; Color-Coded Fault Injection &bull; Dynamic TPS Override &bull; <a href="/stats" target="_blank">JSON Snapshot</a> &bull; <a href="/api/trends?range=5m" target="_blank">Trends API</a></div>
      <div>OTel Collector &rarr; Tempo, Loki, Mimir &bull; Valkey</div>
    </footer>
  </div>

  <script>
    let currentRange = '5m';
    let currentService = 'all';
    let currentView = 'both';
    let latestTrendsData = null;

    // Language color definitions
    const LANG_COLORS = {{
      'java':   {{ stroke: '#f97316', fill: 'rgba(249, 115, 22, 0.22)', badge: '#ea580c', border: '#f97316', text: '#fff' }},
      'python': {{ stroke: '#3b82f6', fill: 'rgba(59, 130, 246, 0.22)', badge: '#2563eb', border: '#3b82f6', text: '#fff' }},
      'rust':   {{ stroke: '#d946ef', fill: 'rgba(217, 70, 239, 0.22)', badge: '#c026d3', border: '#d946ef', text: '#fff' }},
      'node':   {{ stroke: '#22c55e', fill: 'rgba(34, 197, 94, 0.22)', badge: '#16a34a', border: '#22c55e', text: '#fff' }},
      'go':     {{ stroke: '#06b6d4', fill: 'rgba(6, 182, 212, 0.22)', badge: '#0891b2', border: '#06b6d4', text: '#fff' }},
      'dotnet': {{ stroke: '#8b5cf6', fill: 'rgba(139, 92, 246, 0.22)', badge: '#7c3aed', border: '#8b5cf6', text: '#fff' }},
      'all':    {{ stroke: '#ef4444', fill: 'rgba(239, 68, 68, 0.22)', badge: '#dc2626', border: '#ef4444', text: '#fff' }}
    }};

    function switchView(mode, btn) {{
      currentView = mode;
      document.querySelectorAll('.view-tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      const graphsSection = document.getElementById('graphs-section');
      const rawSection = document.getElementById('raw-numbers-section');
      const kpiSection = document.getElementById('kpi-section');
      const ctrlSection = document.getElementById('controls-section');

      if (mode === 'graphs') {{
        graphsSection.style.display = 'block';
        rawSection.style.display = 'none';
        kpiSection.style.display = 'grid';
        ctrlSection.style.display = 'grid';
      }} else if (mode === 'raw') {{
        graphsSection.style.display = 'none';
        rawSection.style.display = 'block';
        kpiSection.style.display = 'grid';
        ctrlSection.style.display = 'grid';
      }} else {{
        graphsSection.style.display = 'block';
        rawSection.style.display = 'block';
        kpiSection.style.display = 'grid';
        ctrlSection.style.display = 'grid';
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
      if (isOverride) {{
        const remStr = data.tps_remaining_sec ? ` (${{data.tps_remaining_sec}}s left)` : ' (Perm)';
        document.getElementById('kpi-tps-status').innerText = `🔥 OVERRIDE${{remStr}} (Base: ${{data.base_tps}})`;
        document.getElementById('kpi-tps-status').style.color = '#f59e0b';
        tpsBadge.className = 'badge badge-warning';
        tpsBadge.innerText = `OVERRIDE: ${{effTps}} TPS${{remStr}}`;
      }} else {{
        document.getElementById('kpi-tps-status').innerText = `Target: ${{data.base_tps}} TPS (Default)`;
        document.getElementById('kpi-tps-status').style.color = 'var(--muted)';
        tpsBadge.className = 'badge badge-success';
        tpsBadge.innerText = `DEFAULT: ${{data.base_tps}} TPS`;
      }}

      // Active Fault Banner with Language-Specific Colors
      const fSnap = data.fault_snapshot || {{}};
      const activeFault = fSnap.active_fault;
      const pulseEl = document.getElementById('live-pulse');
      const bannerEl = document.getElementById('active-fault-banner');

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
      }} else {{
        pulseEl.classList.remove('fault-active');
        bannerEl.classList.remove('show');
      }}

      // Apps table
      const tbody = document.getElementById('apps-table-body');
      let appRows = '';
      for (const [lang, app] of Object.entries(data.apps || {{}})) {{
        const reachBadge = app.reachable ? '<span class="badge badge-success">UP</span>' : '<span class="badge badge-warning">WAIT</span>';
        const rateColor = (app.availability_pct >= 99.0) ? '#10b981' : ((app.availability_pct >= 95.0) ? '#f59e0b' : '#ef4444');
        appRows += `<tr>
          <td><span class="badge badge-${{lang}}">${{lang.toUpperCase()}}</span></td>
          <td><code>${{app.url}}</code></td>
          <td>${{reachBadge}}</td>
          <td style="text-align:right;">${{Number(app.total).toLocaleString()}}</td>
          <td style="text-align:right; color:#10b981; font-weight:600;">${{Number(app.success).toLocaleString()}}</td>
          <td style="text-align:right; color:#ef4444; font-weight:600;">${{Number(app.error).toLocaleString()}}</td>
          <td style="text-align:right; color:${{rateColor}}; font-weight:700;">${{app.availability_pct}}%</td>
          <td style="text-align:right; font-family:monospace;">${{app.avg_latency_ms}} ms</td>
        </tr>`;
      }}
      tbody.innerHTML = appRows;

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

      // Fault history table with Language-Specific Color Badges
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

      // Recent requests with Language-Specific Fault Tagging
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
        const url = `/api/trends?range=${{encodeURIComponent(currentRange)}}&service=${{encodeURIComponent(currentService)}}`;
        const res = await fetch(url);
        if (!res.ok) return;
        const trendPayload = await res.json();
        latestTrendsData = trendPayload;
        renderAllCharts(trendPayload);
      }} catch (e) {{
        console.error("Error fetching trends:", e);
      }}
    }}

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

    // Toast Notification
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
          document.getElementById('tps-rate-input').value = 6;
          fetchStats();
          fetchTrendsAndRefresh();
        }}
      }} catch (err) {{
        console.error(err);
      }}
    }}

    // -------------------------------------------------------------------------
    // High-Performance Interactive Canvas Chart Engine (Language Color-Coded)
    // -------------------------------------------------------------------------
    function drawLineChart(canvasId, series, faultEvents, options) {{
      const canvas = document.getElementById(canvasId);
      if (!canvas) return;
      const rect = canvas.parentElement.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = rect.width;
      const h = rect.height;

      canvas.width = w * dpr;
      canvas.height = h * dpr;
      const ctx = canvas.getContext('2d');
      ctx.scale(dpr, dpr);

      ctx.clearRect(0, 0, w, h);

      const padLeft = 38;
      const padRight = 12;
      const padTop = 14;
      const padBottom = 20;
      const plotW = w - padLeft - padRight;
      const plotH = h - padTop - padBottom;

      if (!series || series.length < 2) {{
        ctx.fillStyle = '#64748b';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('Collecting trend telemetry...', w / 2, h / 2);
        return;
      }}

      const tMin = series[0].t;
      const tMax = series[series.length - 1].t;
      const tSpan = Math.max(1, tMax - tMin);

      let vMin = (options.yMin !== undefined) ? options.yMin : Math.min(...series.map(p => p.v));
      let vMax = (options.yMax !== undefined) ? options.yMax : Math.max(...series.map(p => p.v));
      if (vMin === vMax) {{ vMin = 0; vMax = vMax ? vMax * 1.2 : 1; }}
      if (options.yMin === undefined && vMin > 0) vMin = 0;
      const vSpan = Math.max(0.0001, vMax - vMin);

      const getX = (t) => padLeft + ((t - tMin) / tSpan) * plotW;
      const getY = (v) => padTop + plotH - ((v - vMin) / vSpan) * plotH;

      // Draw Fault Event Regions with Language-Specific Color Coding
      if (faultEvents && faultEvents.length > 0) {{
        for (const f of faultEvents) {{
          const fStart = Math.max(tMin, f.start_time);
          const fEnd = Math.min(tMax, f.end_time || (Date.now() / 1000));
          if (fEnd >= tMin && fStart <= tMax) {{
            const x1 = getX(fStart);
            const x2 = Math.max(x1 + 3, getX(fEnd));

            const tgt = (f.target || 'all').toLowerCase();
            const col = LANG_COLORS[tgt] || LANG_COLORS['all'];

            // Shaded fault region
            ctx.fillStyle = col.fill;
            ctx.fillRect(x1, padTop, x2 - x1, plotH);

            // Dashed marker boundary lines
            ctx.strokeStyle = col.stroke;
            ctx.lineWidth = 1.5;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(x1, padTop);
            ctx.lineTo(x1, padTop + plotH);
            ctx.moveTo(x2, padTop);
            ctx.lineTo(x2, padTop + plotH);
            ctx.stroke();
            ctx.setLineDash([]);

            // Colored Event Tag Pill
            const tagText = `${{tgt.toUpperCase()}}: ${{f.tag || 'DRILL'}}`;
            ctx.font = 'bold 8.5px sans-serif';
            const tagW = ctx.measureText(tagText).width + 8;
            const drawX = Math.min(w - tagW - 2, Math.max(padLeft, x1));
            ctx.fillStyle = col.badge;
            ctx.fillRect(drawX, padTop + 2, tagW, 13);
            ctx.fillStyle = col.text;
            ctx.textAlign = 'left';
            ctx.fillText(tagText, drawX + 4, padTop + 11);
          }}
        }}
      }}

      // Horizontal grid lines
      ctx.strokeStyle = '#1a2638';
      ctx.lineWidth = 1;
      const yTicks = 3;
      ctx.fillStyle = '#64748b';
      ctx.font = '9px monospace';
      ctx.textAlign = 'right';

      for (let i = 0; i <= yTicks; i++) {{
        const yVal = vMin + (vSpan * i) / yTicks;
        const yPos = getY(yVal);
        ctx.beginPath();
        ctx.moveTo(padLeft, yPos);
        ctx.lineTo(w - padRight, yPos);
        ctx.stroke();
        ctx.fillText(options.formatY ? options.formatY(yVal) : yVal.toFixed(1), padLeft - 4, yPos + 3);
      }}

      // Target threshold line (e.g. 99% availability)
      if (options.threshold && options.threshold >= vMin && options.threshold <= vMax) {{
        const thY = getY(options.threshold);
        ctx.strokeStyle = 'rgba(245, 158, 11, 0.45)';
        ctx.setLineDash([3, 2]);
        ctx.beginPath();
        ctx.moveTo(padLeft, thY);
        ctx.lineTo(w - padRight, thY);
        ctx.stroke();
        ctx.setLineDash([]);
      }}

      // Time X axis labels
      ctx.fillStyle = '#64748b';
      ctx.textAlign = 'center';
      const xTicks = Math.min(6, Math.floor(plotW / 75));
      for (let i = 0; i <= xTicks; i++) {{
        const tVal = tMin + (tSpan * i) / xTicks;
        const xPos = getX(tVal);
        const dt = new Date(tVal * 1000);
        let timeLabel = '';
        if (tSpan <= 1800) {{
          timeLabel = dt.toTimeString().split(' ')[0];
        }} else if (tSpan <= 86400) {{
          timeLabel = dt.toTimeString().substring(0, 5);
        }} else {{
          timeLabel = (dt.getMonth() + 1) + '/' + dt.getDate() + ' ' + dt.toTimeString().substring(0, 5);
        }}
        ctx.fillText(timeLabel, xPos, h - 5);
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
      let html = `<div style="color:var(--muted); font-size:0.7rem;">${{timeStr}}</div>
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

      // 3. Latency Chart
      const latPts = m.latency_ms || [];
      if (latPts.length) {{
        document.getElementById('cur-lat').innerText = latPts[latPts.length - 1].v + ' ms';
      }}
      drawLineChart('chart-latency', latPts, faults, {{
        yMin: 0,
        strokeColor: '#38bdf8',
        fillColor: 'rgba(56, 189, 248, 0.2)',
        unit: 'ms',
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
            # Process crash may abort TCP connection immediately
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
# HTTP Server (Standard Library)
# ---------------------------------------------------------------------------
def start_dashboard_server(port: int) -> HTTPServer:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            if path == "/" or path == "/index.html":
                body = render_dashboard_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif path in ("/stats", "/json"):
                data = json.dumps(state.get_snapshot(), indent=2).encode("utf-8")
                self.send_response(200)
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
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/trends":
                range_str = query.get("range", ["5m"])[0]
                service = query.get("service", ["all"])[0]
                trend_data = query_trend_metrics(range_str, service)
                data = json.dumps(trend_data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            elif path == "/api/faults":
                data = json.dumps(fault_manager.get_snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            elif path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(404)
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
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/fault/stop":
                stopped = fault_manager.stop_fault()
                resp = json.dumps({"status": "stopped", "fault": stopped}).encode("utf-8")
                self.send_response(200)
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
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif path == "/api/tps/reset":
                res = state.clear_tps_override()
                resp = json.dumps({"status": "reset", **res}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info("Compact Canary dashboard listening at http://0.0.0.0:%d/", port)
    return server

# ---------------------------------------------------------------------------
# Wait for Apps & Main Execution
# ---------------------------------------------------------------------------
def wait_for_apps(session: requests.Session) -> None:
    for name, lang, url in [("Python", "python", APP_BASE_URL),
                            ("Java", "java", JAVA_APP_BASE_URL),
                            ("Rust", "rust", RUST_APP_BASE_URL),
                            ("Node", "node", NODE_APP_BASE_URL),
                            ("Go", "go", GO_APP_BASE_URL),
                            ("Dotnet", "dotnet", DOTNET_APP_BASE_URL)]:
        base = f"{url}/"
        while True:
            try:
                resp = session.get(base, timeout=3)
                if resp.status_code < 500:
                    logger.info("%s App is reachable at %s", name, url)
                    state.mark_reachable(lang)
                    break
            except Exception as exc:
                logger.warning("%s App not ready yet (%s), retrying in 3 s...", name, exc)
            time.sleep(3)

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
    logger.info("Starting canary loop with dynamic TPS and language color-coded fault injection")

    next_tick = time.time()

    while True:
        for app_info in [("python", APP_BASE_URL), ("java", JAVA_APP_BASE_URL), ("rust", RUST_APP_BASE_URL), ("node", NODE_APP_BASE_URL), ("go", GO_APP_BASE_URL), ("dotnet", DOTNET_APP_BASE_URL)]:
            app_lang, base_url = app_info
            for target in TARGETS:
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
                    next_tick = now_t
                sleep_time = max(0.0, next_tick - now_t)
                time.sleep(sleep_time)

if __name__ == "__main__":
    run()
