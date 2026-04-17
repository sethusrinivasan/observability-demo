from flask import Flask, request, jsonify
import logging
import resource
import requests
import time
import os
import json
from datetime import datetime, timezone
import psycopg2

# Replace the hardcoded endpoints with environment variables
OTLP_ENDPOINT = os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://localhost:4317')
DB_HOST = os.getenv('POSTGRES_HOST', 'postgres')
DB_PORT = int(os.getenv('POSTGRES_PORT', 5432))
DB_NAME = os.getenv('POSTGRES_DB', 'observability')
DB_USER = os.getenv('POSTGRES_USER', 'observability')
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD', 'observability')
DB_RETRY_COUNT = int(os.getenv('DB_RETRY_COUNT', 10))
DB_RETRY_DELAY = float(os.getenv('DB_RETRY_DELAY', 1.0))


# Similarly for metrics and logs


# OpenTelemetry imports
from opentelemetry import trace, metrics
from opentelemetry.metrics import Observation
from opentelemetry.sdk.resources import SERVICE_NAME, DEPLOYMENT_ENVIRONMENT, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.instrumentation.flask import FlaskInstrumentor

app = Flask(__name__)


def get_db_connection():
    """
    Open and return a new Postgres connection.

    A fresh connection is created on every call — no pooling.
    Callers are responsible for closing it.  Use the helper
    with_db() context manager below to ensure the connection
    is always closed even if an exception is raised.
    """
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


from contextlib import contextmanager

@contextmanager
def with_db():
    """
    Context manager that opens a connection, yields it, then
    unconditionally closes it — guaranteeing no idle connections
    are left open after each request.

    Usage:
        with with_db() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
            conn.commit()
    """
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


def ensure_audit_table():
    with with_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    endpoint TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_time_seconds DOUBLE PRECISION,
                    process_memory_rss BIGINT,
                    process_cpu_seconds DOUBLE PRECISION,
                    system_loadavg_1m DOUBLE PRECISION,
                    container_memory_current BIGINT,
                    container_memory_limit BIGINT,
                    container_memory_percent DOUBLE PRECISION,
                    container_cpu_usage_ns BIGINT,
                    details JSONB
                );
                """
            )
            cursor.execute(
                """
                ALTER TABLE audit_logs
                ADD COLUMN IF NOT EXISTS response_time_seconds DOUBLE PRECISION;
                """
            )
            conn.commit()


def wait_for_postgres():
    logger = logging.getLogger(__name__)
    for attempt in range(1, DB_RETRY_COUNT + 1):
        try:
            conn = get_db_connection()
            conn.close()
            logger.info("Connected to Postgres on attempt %d", attempt)
            return
        except Exception as exc:
            logger.warning("Postgres connection attempt %d/%d failed: %s", attempt, DB_RETRY_COUNT, exc)
            if attempt == DB_RETRY_COUNT:
                raise
            time.sleep(DB_RETRY_DELAY)


# 1. Create resource
service_resource = Resource(attributes={
    SERVICE_NAME: "observability-python-app",
    DEPLOYMENT_ENVIRONMENT: "local-dev"
})

# 2. Setup Tracing
trace_provider = TracerProvider(resource=service_resource)
span_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
trace_provider.add_span_processor(span_processor)
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer(__name__)

# 3. Setup Metrics
metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    export_interval_millis=5000
)
meter_provider = MeterProvider(resource=service_resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(__name__)

# Create metrics
request_counter = meter.create_counter(
    "app.requests.total",
    description="Total number of requests"
)
request_success_counter = meter.create_counter(
    "app.requests.success",
    description="Total number of successful requests"
)
request_error_counter = meter.create_counter(
    "app.requests.errors",
    description="Total number of failed requests"
)
request_duration = meter.create_histogram(
    "app.request.duration",
    description="Request duration in seconds"
)
def get_process_memory_rss() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_maxrss * 1024


def get_system_loadavg() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def get_process_cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def get_cgroup_memory_current() -> int:
    if os.path.exists("/sys/fs/cgroup/memory.current"):
        with open("/sys/fs/cgroup/memory.current", "r") as f:
            return int(f.read().strip() or 0)
    path = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
    if os.path.exists(path):
        with open(path, "r") as f:
            return int(f.read().strip() or 0)
    return 0


def get_cgroup_memory_limit() -> int:
    if os.path.exists("/sys/fs/cgroup/memory.max"):
        with open("/sys/fs/cgroup/memory.max", "r") as f:
            value = f.read().strip()
            return int(value) if value.isdigit() else 0
    path = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    if os.path.exists(path):
        with open(path, "r") as f:
            return int(f.read().strip() or 0)
    return 0


def get_cgroup_memory_percent() -> float:
    limit = get_cgroup_memory_limit()
    current = get_cgroup_memory_current()
    return (current / limit * 100.0) if limit > 0 else 0.0


def get_cgroup_cpu_usage_ns() -> int:
    if os.path.exists("/sys/fs/cgroup/cpu.stat"):
        with open("/sys/fs/cgroup/cpu.stat", "r") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return int(line.split()[1]) * 1000
    path = "/sys/fs/cgroup/cpuacct/cpuacct.usage"
    if os.path.exists(path):
        with open(path, "r") as f:
            return int(f.read().strip() or 0)
    return 0


def observe_process_memory(options):
    return [Observation(get_process_memory_rss(), {"process": "python"})]


def observe_process_cpu_seconds(options):
    return [Observation(get_process_cpu_seconds(), {"process": "python"})]


def observe_system_loadavg(options):
    return [Observation(get_system_loadavg(), {"process": "python"})]


def observe_container_memory_current(options):
    return [Observation(get_cgroup_memory_current(), {"container": "self"})]


def observe_container_memory_limit(options):
    return [Observation(get_cgroup_memory_limit(), {"container": "self"})]


def observe_container_memory_percent(options):
    return [Observation(get_cgroup_memory_percent(), {"container": "self"})]


def observe_container_cpu_usage_ns(options):
    return [Observation(get_cgroup_cpu_usage_ns(), {"container": "self"})]

meter.create_observable_gauge(
    "app.process.memory.rss",
    callbacks=[observe_process_memory],
    description="Process RSS memory usage in bytes"
)
meter.create_observable_gauge(
    "app.process.cpu.seconds",
    callbacks=[observe_process_cpu_seconds],
    description="Process CPU time in seconds"
)
meter.create_observable_gauge(
    "app.system.loadavg.1m",
    callbacks=[observe_system_loadavg],
    description="System load average (1 minute)"
)
meter.create_observable_gauge(
    "app.container.memory.current",
    callbacks=[observe_container_memory_current],
    description="Container memory usage in bytes"
)
meter.create_observable_gauge(
    "app.container.memory.limit",
    callbacks=[observe_container_memory_limit],
    description="Container memory limit in bytes"
)
meter.create_observable_gauge(
    "app.container.memory.percent",
    callbacks=[observe_container_memory_percent],
    description="Container memory usage as a percentage of limit"
)
meter.create_observable_gauge(
    "app.container.cpu.usage.ns",
    callbacks=[observe_container_cpu_usage_ns],
    description="Container CPU usage in nanoseconds"
)

@app.before_request
def start_timer():
    request.start_time = time.time()


@app.after_request
def record_request_metrics(response):
    duration = time.time() - getattr(request, "start_time", time.time())
    endpoint = request.path
    status_code = response.status_code
    status = "success" if status_code < 400 else "error"
    route = request.endpoint or endpoint
    journey = "compute" if endpoint.startswith("/compute") else "home" if endpoint == "/" else "other"

    labels = {
        "endpoint": endpoint,
        "route": route,
        "status": status,
        "status_code": str(status_code),
        "journey": journey,
    }

    request_counter.add(1, labels)
    request_duration.record(duration, labels)

    if status == "error":
        request_error_counter.add(1, labels)
    else:
        request_success_counter.add(1, labels)

    return response

# 4. Setup Logging with Trace correlation
logger_provider = LoggerProvider(resource=service_resource)
log_exporter = OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
set_logger_provider(logger_provider)

# Configure Python logging
handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger(__name__)

# Auto-instrument Flask
FlaskInstrumentor().instrument_app(app)

HOME_EXAMPLE_INPUTS = [5, 10, 20]

def get_compute_example_links() -> str:
    return "\n".join(
        f"      <li><a href='/compute/{n}'>Compute {n}</a></li>"
        for n in HOME_EXAMPLE_INPUTS
    )

def get_home_html() -> str:
    example_links = get_compute_example_links()
    return f"""<!doctype html>
<html lang='en'>
  <head>
    <meta charset='utf-8'>
    <title>Observability Lab</title>
  </head>
  <body>
    <h1>Hello from Observability Lab!</h1>
    <p>Discover the compute endpoint with a number:</p>
    <ul>
{example_links}
    </ul>
    <form id='compute-form'>
      <label for='compute-value'>Enter a number:</label>
      <input type='number' id='compute-value' name='n' min='0' value='5'>
      <button type='button' onclick='goCompute()'>Compute</button>
    </form>
    <p>Or call the endpoint directly at <code>/compute/&lt;number&gt;</code>.</p>
    <script>
      function goCompute() {{
        const n = document.getElementById('compute-value').value;
        if (n === '') {{
          return;
        }}
        window.location = '/compute/' + encodeURIComponent(n);
      }}
    </script>
  </body>
</html>"""

@app.route('/')
def home():
    """Simple endpoint that returns a greeting"""
    with tracer.start_as_current_span("home-endpoint") as span:
        span.set_attribute("http.method", "GET")
        logger.info("Home endpoint called")
        return get_home_html(), 200, {"Content-Type": "text/html"}

@app.route('/compute/<int:n>')
def compute(n):
    """Endpoint that does some CPU work"""
    with tracer.start_as_current_span("compute-endpoint") as span:
        span.set_attribute("compute.value", n)
        logger.info(f"Computing Fibonacci for {n}")

        result = fibonacci(n)

        logger.info(f"Computed fibonacci({n}) = {result}")
        return jsonify({"input": n, "result": result})


@app.route('/auditlog', methods=['GET', 'POST'])
def auditlog():
    with tracer.start_as_current_span("auditlog-endpoint") as span:
        span.set_attribute("http.method", request.method)
        logger.info("Audit log endpoint called")

        start_time = time.time()
        endpoint = request.path
        status_code = 200
        process_memory = get_process_memory_rss()
        process_cpu = get_process_cpu_seconds()
        load_avg = get_system_loadavg()
        container_memory_current = get_cgroup_memory_current()
        container_memory_limit = get_cgroup_memory_limit()
        container_memory_percent = get_cgroup_memory_percent()
        container_cpu_ns = get_cgroup_cpu_usage_ns()

        details = {
            "remote_addr": request.remote_addr,
            "user_agent": request.headers.get("User-Agent"),
            "query_params": request.args.to_dict(flat=True),
            "form_data": request.form.to_dict(flat=True),
        }

        response_time = time.time() - start_time

        try:
            with with_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO audit_logs (
                            endpoint,
                            status_code,
                            response_time_seconds,
                            process_memory_rss,
                            process_cpu_seconds,
                            system_loadavg_1m,
                            container_memory_current,
                            container_memory_limit,
                            container_memory_percent,
                            container_cpu_usage_ns,
                            details
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id;
                        """,
                        (
                            endpoint,
                            status_code,
                            response_time,
                            process_memory,
                            process_cpu,
                            load_avg,
                            container_memory_current,
                            container_memory_limit,
                            container_memory_percent,
                            container_cpu_ns,
                            json.dumps(details),
                        ),
                    )
                    record_id = cursor.fetchone()[0]
                    conn.commit()
        except Exception as exc:
            logger.exception("Failed to write audit log to Postgres")
            return jsonify({"status": "error", "message": "Failed to write audit log", "error": str(exc)}), 500

        return jsonify({
            "status": "ok",
            "audit_id": record_id,
            "response_time_seconds": response_time,
            "process_memory_rss": process_memory,
            "process_cpu_seconds": process_cpu,
            "system_loadavg_1m": load_avg,
            "container_memory_current": container_memory_current,
            "container_memory_limit": container_memory_limit,
            "container_memory_percent": container_memory_percent,
            "container_cpu_usage_ns": container_cpu_ns,
            "details": details,
        }), 201


@app.route('/auditlog/stats', methods=['GET'])
def auditlog_stats():
    with tracer.start_as_current_span("auditlog-stats-endpoint") as span:
        span.set_attribute("http.method", request.method)
        logger.info("Audit log stats endpoint called")

        histogram_bucket_count = 10
        histogram_max_seconds = 5.0

        try:
            with with_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            count(*) AS total_rows,
                            sum((status_code < 400)::int) AS success_count,
                            sum((status_code >= 400)::int) AS failure_count,
                            percentile_disc(0.5) WITHIN GROUP (ORDER BY response_time_seconds) AS p50_seconds,
                            percentile_disc(0.9) WITHIN GROUP (ORDER BY response_time_seconds) AS p90_seconds,
                            percentile_disc(0.95) WITHIN GROUP (ORDER BY response_time_seconds) AS p95_seconds,
                            percentile_disc(0.99) WITHIN GROUP (ORDER BY response_time_seconds) AS p99_seconds
                        FROM audit_logs;
                        """
                    )
                    totals = cursor.fetchone()

                    cursor.execute(
                        """
                        SELECT width_bucket(response_time_seconds, 0, %s, %s) AS bucket,
                               count(*) AS bucket_count
                        FROM audit_logs
                        WHERE response_time_seconds IS NOT NULL
                        GROUP BY bucket
                        ORDER BY bucket;
                        """,
                        (histogram_max_seconds, histogram_bucket_count),
                    )
                    histogram_rows = cursor.fetchall()

            buckets = []
            for bucket, bucket_count in histogram_rows:
                if bucket == 0:
                    lower = 0.0
                    upper = 0.0
                elif bucket > histogram_bucket_count:
                    lower = histogram_max_seconds
                    upper = None
                else:
                    bucket_width = histogram_max_seconds / histogram_bucket_count
                    lower = (bucket - 1) * bucket_width
                    upper = bucket * bucket_width
                buckets.append({
                    "bucket": bucket,
                    "lower_bound_seconds": lower,
                    "upper_bound_seconds": upper,
                    "count": bucket_count,
                })

            return jsonify({
                "total_rows": totals[0] or 0,
                "success_count": totals[1] or 0,
                "failure_count": totals[2] or 0,
                "percentiles": {
                    "p50_seconds": totals[3],
                    "p90_seconds": totals[4],
                    "p95_seconds": totals[5],
                    "p99_seconds": totals[6],
                },
                "response_time_histogram": {
                    "bucket_count": histogram_bucket_count,
                    "max_seconds": histogram_max_seconds,
                    "buckets": buckets,
                },
            }), 200
        except Exception as exc:
            logger.exception("Failed to read audit log statistics from Postgres")
            return jsonify({"status": "error", "message": "Failed to read audit log statistics", "error": str(exc)}), 500


@app.route('/eval', methods=['GET', 'POST'])
def eval_expression():
    """
    Evaluate a mathematical expression without any helper libraries.

    Accepts the expression via:
      GET  ?expr=<expression>
      POST JSON body  {"expr": "<expression>"}
      POST form data  expr=<expression>

    Returns JSON:
      {"expression": "...", "result": <float>}   on success  (HTTP 200)
      {"error": "..."}                            on failure  (HTTP 400)

    Internally delegates to evaluator.evaluate() which implements the
    full Shunting-Yard pipeline: tokenise → RPN → evaluate.
    """
    from evaluator import evaluate as eval_expr

    with tracer.start_as_current_span("eval-endpoint") as span:
        # --- extract the expression from whichever input method was used ---
        if request.method == "POST":
            if request.is_json:
                expr = (request.get_json() or {}).get("expr", "")
            else:
                expr = request.form.get("expr", "")
        else:
            expr = request.args.get("expr", "")

        span.set_attribute("eval.expression", expr)
        logger.info("Eval endpoint called with expression: %s", expr)

        if not expr:
            return jsonify({"error": "Missing 'expr' parameter"}), 400

        try:
            result = eval_expr(expr)
            logger.info("Eval result: %s = %s", expr, result)
            span.set_attribute("eval.result", result)
            return jsonify({"expression": expr, "result": result}), 200
        except ZeroDivisionError as exc:
            logger.warning("Eval division by zero: %s", expr)
            return jsonify({"error": str(exc)}), 400
        except ValueError as exc:
            logger.warning("Eval error for '%s': %s", expr, exc)
            return jsonify({"error": str(exc)}), 400


def fibonacci(n):
    """Recursive Fibonacci (inefficient on purpose)"""
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)


if __name__ == '__main__':
    wait_for_postgres()
    ensure_audit_table()
    # Synthetic canary runs in its own container (canary/) — no thread needed here
    app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False)
    """
    Background canary that continuously exercises every Flask endpoint at a
    controlled, uniform rate.

    Rate design
    -----------
    Target: CANARY_TPS = 10 requests/second across all targets.
    Inter-arrival time = 1 / CANARY_TPS = 0.100 s between the START of each
    consecutive request.

    The pacing loop subtracts actual response time from the inter-arrival
    budget so that slow endpoints don't cause the next request to fire late
    and fast endpoints don't bunch up and spike CPU.

      sleep_time = max(0, inter_arrival - response_time)

    This keeps throughput stable regardless of individual endpoint latency.

    Saturation budget (4-core host, 15 GiB RAM)
    --------------------------------------------
    Measured baseline at ~16 TPS (old fixed-sleep loop):
      App CPU  ≈ 10%   →  ~0.6% per TPS
      Memory   ≈ 57 MiB (stable, not TPS-sensitive)
    At 10 TPS:
      App CPU  ≈ 6%    — well under 60% saturation target
      Stack total CPU ≈ 15% — leaves ample headroom for spikes

    Coverage map (20 targets, all at equal inter-arrival time)
    -----------------------------------------------------------
    GET  /                          — home page
    GET  /compute/5,10,20           — Fibonacci (small / medium / large)
    GET  /auditlog                  — audit log write via GET
    POST /auditlog  (form)          — audit log write via POST
    GET  /auditlog/stats            — stats + response-time histogram

    GET  /eval?expr=...             — one canary per operator / feature:
      3+4          → 7.0            •  addition
      10-3         → 7.0            •  subtraction
      6*7          → 42.0           •  multiplication
      22/4         → 5.5            •  division (decimal result)
      2^10         → 1024.0         •  exponentiation
      (2+3)*4      → 20.0           •  parentheses override precedence
      ((3+4)*6^2/5)+1-7 → 44.4     •  full nested PEMDAS (spec example)
      -5+8         → 3.0            •  unary minus
      2^3^2        → 512.0          •  right-associative exponentiation
    POST /eval  (JSON body)  → 57.0 •  POST + JSON content-type path
    GET  /eval?expr=5/0      → 400  •  error path: division by zero
    GET  /eval?expr=3$4      → 400  •  error path: invalid character
    GET  /eval               → 400  •  error path: missing parameter
    """
    # ------------------------------------------------------------------
    # Rate control: target TPS and derived inter-arrival interval.
    #
    # Calibration (4-core host, measured at 9.44 TPS baseline):
    #   App CPU per TPS ≈ 0.52% host CPU
    #   50% saturation of one core = 12.5% host CPU
    #   → target TPS = 12.5 / 0.52 ≈ 24 req/s
    #
    # At 24 TPS:
    #   App CPU  ≈ 12.5% host  (50% of one core — target saturation)
    #   Stack total CPU ≈ 16%  — well within host capacity
    #   Inter-arrival = 1/24 ≈ 0.042 s between request starts
    # ------------------------------------------------------------------
    CANARY_TPS     = 24
    INTER_ARRIVAL  = 1.0 / CANARY_TPS   # ~0.042 s between request starts

    session = requests.Session()

    # ------------------------------------------------------------------
    # Target list — every endpoint and every operator/feature of /eval.
    # Fields:
    #   path         : URL path (may include query string)
    #   method       : "GET" or "POST"
    #   data         : form data dict for POST (optional)
    #   json         : JSON body dict for POST (optional)
    #   expected_4xx : True when a 4xx response IS the correct outcome
    #                  (error-path canaries) — counted as success so they
    #                  don't inflate the error-rate metric
    # ------------------------------------------------------------------
    targets = [
        # --- home ---
        {"path": "/",                                          "method": "GET"},

        # --- compute: small / medium / large Fibonacci ---
        {"path": "/compute/5",                                 "method": "GET"},
        {"path": "/compute/10",                                "method": "GET"},
        {"path": "/compute/20",                                "method": "GET"},

        # --- auditlog ---
        {"path": "/auditlog",                                  "method": "GET"},
        {"path": "/auditlog",                                  "method": "POST",
         "data": {"source": "synthetic"}},
        {"path": "/auditlog/stats",                            "method": "GET"},

        # --- eval: GET, one case per operator / language feature ---
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

        # --- eval: error paths (4xx is the CORRECT response here) ---
        {"path": "/eval?expr=5%2F0",                          "method": "GET",
         "expected_4xx": True},                                                    # division by zero
        {"path": "/eval?expr=3%244",                          "method": "GET",
         "expected_4xx": True},                                                    # invalid char $
        {"path": "/eval",                                     "method": "GET",
         "expected_4xx": True},                                                    # missing param
    ]

    while True:
        for target in targets:
            path         = target["path"]
            method       = target["method"]
            expected_4xx = target.get("expected_4xx", False)
            url          = f'http://127.0.0.1:5000{path}'

            # Record wall-clock start for both response timing and pacing
            t_start = time.time()
            status  = 'error'

            try:
                if method == 'POST':
                    if "json" in target:
                        resp = session.post(url, json=target["json"], timeout=5)
                    else:
                        resp = session.post(url, data=target.get("data", {}), timeout=5)
                else:
                    resp = session.get(url, timeout=5)

                # Error-path canaries: 4xx is the expected/correct outcome
                if expected_4xx:
                    status = 'success' if 400 <= resp.status_code < 500 else 'error'
                else:
                    status = 'success' if resp.status_code < 400 else 'error'

            except Exception:
                status = 'error'

            response_time = time.time() - t_start

            # Emit OTel metrics — strip query string from path label so
            # Grafana can group all /eval variants under one series
            base_path = path.split("?")[0]
            labels = {"path": base_path, "method": method,
                      "status": status, "client": "synthetic"}
            synthetic_request_counter.add(1, labels)
            synthetic_request_duration.record(response_time, labels)
            if status == 'success':
                synthetic_request_success_counter.add(
                    1, {"path": base_path, "method": method, "client": "synthetic"})
            else:
                synthetic_request_error_counter.add(
                    1, {"path": base_path, "method": method, "client": "synthetic"})

            # Pace to CANARY_TPS: sleep only the remaining budget after the
            # response arrived.  If the response took longer than the full
            # inter-arrival window we skip the sleep entirely (no negative
            # sleep) so we never fall further behind.
            sleep_time = max(0.0, INTER_ARRIVAL - response_time)
            time.sleep(sleep_time)
