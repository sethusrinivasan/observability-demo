"""
canary.py — Standalone synthetic canary for the observability-demo stack.
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
APP_BASE_URL        = os.getenv("APP_BASE_URL",        "http://observability-python-app:5000")
JAVA_APP_BASE_URL   = os.getenv("JAVA_APP_BASE_URL",   "http://observability-java-app:8080")
RUST_APP_BASE_URL   = os.getenv("RUST_APP_BASE_URL",   "http://observability-rust-app:8081")
OTLP_ENDPOINT       = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
CANARY_TPS          = float(os.getenv("CANARY_TPS",    "12"))
INTER_ARRIVAL       = 1.0 / CANARY_TPS

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

def wait_for_apps(session: requests.Session) -> None:
    """Block until all apps are reachable."""
    for name, url in [("Python", APP_BASE_URL), ("Java", JAVA_APP_BASE_URL), ("Rust", RUST_APP_BASE_URL)]:
        base = f"{url}/"
        while True:
            try:
                resp = session.get(base, timeout=3)
                if resp.status_code < 500:
                    logger.info("%s App is reachable at %s", name, url)
                    break
            except Exception as exc:
                logger.warning("%s App not ready yet (%s), retrying in 3 s...", name, exc)
            time.sleep(3)

def run() -> None:
    session = requests.Session()
    logger.info("Canary starting — Python: %s, Java: %s, Rust: %s, TPS: %.0f",
                APP_BASE_URL, JAVA_APP_BASE_URL, RUST_APP_BASE_URL, CANARY_TPS)

    wait_for_apps(session)
    logger.info("Starting canary loop")
    
    next_tick = time.time()

    while True:
        for app_info in [("python", APP_BASE_URL), ("java", JAVA_APP_BASE_URL), ("rust", RUST_APP_BASE_URL)]:
            app_lang, base_url = app_info
            for target in TARGETS:
                path         = target["path"]
                method       = target["method"]
                expected_4xx = target.get("expected_4xx", False)
                url          = f"{base_url}{path}"

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

                    if expected_4xx:
                        status = "success" if 400 <= resp.status_code < 500 else "error"
                    else:
                        status = "success" if resp.status_code < 400 else "error"

                except Exception as exc:
                    logger.debug("Request failed %s %s: %s", method, path, exc)
                    status = "error"

                response_time = time.time() - t_start
                base_path = path.split("?")[0]
                labels = {"path": base_path, "method": method,
                          "status": status, "client": "canary", "language": app_lang}

                request_counter.add(1, labels)
                request_duration.record(response_time, labels)
                
                next_tick += INTER_ARRIVAL
                sleep_time = max(0.0, next_tick - time.time())
                time.sleep(sleep_time)

if __name__ == "__main__":
    run()
