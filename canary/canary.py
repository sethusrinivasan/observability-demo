"""
canary.py — Standalone synthetic canary for the observability-demo stack.

Runs in its own container, completely isolated from the Flask app.
Exercises every endpoint at a calibrated uniform rate and emits OTel
metrics to the collector so Grafana can track canary health independently
from real traffic.

Rate design
-----------
CANARY_TPS = 24 req/s  (one request at a time, paced inter-arrival)
Inter-arrival = 1/24 ≈ 0.042 s between the START of each request.
sleep_time = max(0, inter_arrival - response_time)

Calibrated on a 4-core / 15 GiB host:
  App CPU per TPS ≈ 0.52% host CPU
  50% saturation of one core = 12.5% host CPU → 24 TPS

Coverage (20 targets)
---------------------
GET  /                          home page
GET  /compute/5,10,20           Fibonacci small / medium / large
GET  /auditlog                  audit log write via GET
POST /auditlog  (form)          audit log write via POST
GET  /auditlog/stats            stats + histogram

GET  /eval?expr=...             one case per operator / feature:
  3+4          → 7.0            addition
  10-3         → 7.0            subtraction
  6*7          → 42.0           multiplication
  22/4         → 5.5            division
  2^10         → 1024.0         exponentiation
  (2+3)*4      → 20.0           parentheses
  ((3+4)*6^2/5)+1-7 → 44.4     full nested PEMDAS
  -5+8         → 3.0            unary minus
  2^3^2        → 512.0          right-associative exponentiation
POST /eval  (JSON)   → 57.0     POST + JSON content-type
GET  /eval?expr=5/0  → 400      error path: division by zero
GET  /eval?expr=3$4  → 400      error path: invalid character
GET  /eval           → 400      error path: missing parameter
"""

import os
import time
import logging

import requests
from opentelemetry import metrics
from opentelemetry.sdk.resources import SERVICE_NAME, DEPLOYMENT_ENVIRONMENT, Resource
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------
APP_BASE_URL   = os.getenv("APP_BASE_URL",       "http://observability-python-app:5000")
OTLP_ENDPOINT  = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
CANARY_TPS     = float(os.getenv("CANARY_TPS",   "24"))
INTER_ARRIVAL  = 1.0 / CANARY_TPS

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

# Counters mirror the names used by the app's synthetic metrics so existing
# Grafana panels continue to work — only the "client" label differs.
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

# ---------------------------------------------------------------------------
# Target list
# ---------------------------------------------------------------------------
TARGETS = [
    # --- home ---
    {"path": "/",                                          "method": "GET"},

    # --- compute ---
    {"path": "/compute/5",                                 "method": "GET"},
    {"path": "/compute/10",                                "method": "GET"},
    {"path": "/compute/20",                                "method": "GET"},

    # --- auditlog ---
    {"path": "/auditlog",                                  "method": "GET"},
    {"path": "/auditlog",                                  "method": "POST",
     "data": {"source": "canary"}},
    {"path": "/auditlog/stats",                            "method": "GET"},

    # --- eval: one case per operator / feature ---
    {"path": "/eval?expr=3%2B4",                          "method": "GET"},   # + → 7.0
    {"path": "/eval?expr=10-3",                           "method": "GET"},   # - → 7.0
    {"path": "/eval?expr=6*7",                            "method": "GET"},   # * → 42.0
    {"path": "/eval?expr=22%2F4",                         "method": "GET"},   # / → 5.5
    {"path": "/eval?expr=2%5E10",                         "method": "GET"},   # ^ → 1024.0
    {"path": "/eval?expr=(2%2B3)*4",                      "method": "GET"},   # parens → 20.0
    {"path": "/eval?expr=((3%2B4)*6%5E2%2F5)%2B1-7",     "method": "GET"},   # PEMDAS → 44.4
    {"path": "/eval?expr=-5%2B8",                         "method": "GET"},   # unary - → 3.0
    {"path": "/eval?expr=2%5E3%5E2",                      "method": "GET"},   # right-assoc → 512.0

    # --- eval: POST with JSON body ---
    {"path": "/eval",                                      "method": "POST",
     "json": {"expr": "(10+5)*2^2-3"}},                                       # → 57.0

    # --- eval: error paths (4xx IS the correct response) ---
    {"path": "/eval?expr=5%2F0",                          "method": "GET",
     "expected_4xx": True},                                                    # division by zero
    {"path": "/eval?expr=3%244",                          "method": "GET",
     "expected_4xx": True},                                                    # invalid char $
    {"path": "/eval",                                     "method": "GET",
     "expected_4xx": True},                                                    # missing param
]


def wait_for_app(session: requests.Session) -> None:
    """Block until the app is reachable, retrying every 3 s."""
    url = f"{APP_BASE_URL}/"
    while True:
        try:
            resp = session.get(url, timeout=3)
            if resp.status_code < 500:
                logger.info("App is reachable at %s", APP_BASE_URL)
                return
        except Exception as exc:
            logger.warning("App not ready yet (%s), retrying in 3 s...", exc)
        time.sleep(3)


def run() -> None:
    session = requests.Session()

    logger.info("Canary starting — target: %s, TPS: %.0f, inter-arrival: %.3f s",
                APP_BASE_URL, CANARY_TPS, INTER_ARRIVAL)

    # Wait until the app is up before firing requests
    wait_for_app(session)
    logger.info("Starting canary loop")

    while True:
        for target in TARGETS:
            path         = target["path"]
            method       = target["method"]
            expected_4xx = target.get("expected_4xx", False)
            url          = f"{APP_BASE_URL}{path}"

            t_start = time.time()
            status  = "error"

            try:
                if method == "POST":
                    if "json" in target:
                        resp = session.post(url, json=target["json"], timeout=5)
                    else:
                        resp = session.post(url, data=target.get("data", {}), timeout=5)
                else:
                    resp = session.get(url, timeout=5)

                # 4xx on an error-path canary is the expected/correct outcome
                if expected_4xx:
                    status = "success" if 400 <= resp.status_code < 500 else "error"
                else:
                    status = "success" if resp.status_code < 400 else "error"

            except Exception as exc:
                logger.debug("Request failed %s %s: %s", method, path, exc)
                status = "error"

            response_time = time.time() - t_start

            # Strip query string for clean Grafana labels
            base_path = path.split("?")[0]
            labels = {"path": base_path, "method": method,
                      "status": status, "client": "canary"}

            request_counter.add(1, labels)
            request_duration.record(response_time, labels)
            if status == "success":
                request_success_counter.add(
                    1, {"path": base_path, "method": method, "client": "canary"})
            else:
                request_error_counter.add(
                    1, {"path": base_path, "method": method, "client": "canary"})

            # Pace to CANARY_TPS — sleep only the remaining inter-arrival budget
            sleep_time = max(0.0, INTER_ARRIVAL - response_time)
            time.sleep(sleep_time)


if __name__ == "__main__":
    run()
