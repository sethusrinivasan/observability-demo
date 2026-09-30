/**
 * index.js — Node.js (Express) Observability Microservice
 * Full parity with Java (Spring Boot), Python (Flask), and Rust (Axum) services.
 * Implements OpenTelemetry Tracing & Metrics, PostgreSQL Audit Log, Actuator Probes,
 * Fibonacci Compute, and Zero-Dependency Math Expression Evaluator.
 */

const fs = require("fs");
const os = require("os");
const express = require("express");
const { Pool } = require("pg");
const { trace, context } = require("@opentelemetry/api");
const { Resource } = require("@opentelemetry/resources");
const { SEMRESATTRS_SERVICE_NAME, SEMRESATTRS_DEPLOYMENT_ENVIRONMENT, SEMRESATTRS_SERVICE_VERSION } = require("@opentelemetry/semantic-conventions");
const { NodeTracerProvider } = require("@opentelemetry/sdk-trace-node");
const { BatchSpanProcessor } = require("@opentelemetry/sdk-trace-base");
const { OTLPTraceExporter } = require("@opentelemetry/exporter-trace-otlp-grpc");
const { MeterProvider, PeriodicExportingMetricReader } = require("@opentelemetry/sdk-metrics");
const { OTLPMetricExporter } = require("@opentelemetry/exporter-metrics-otlp-grpc");

const { evaluate } = require("./evaluator");
const { fibonacci } = require("./fibonacci");

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------
const PORT = parseInt(process.env.PORT || "8080", 10);
const OTLP_ENDPOINT = process.env.OTEL_EXPORTER_OTLP_ENDPOINT || "http://otel-collector:4317";
const APP_NAME = "observability-node-app";
const VERSION = "1.0.1";
const LANGUAGE = "nodejs";

const resource = new Resource({
  [SEMRESATTRS_SERVICE_NAME]: APP_NAME,
  [SEMRESATTRS_DEPLOYMENT_ENVIRONMENT]: "local-dev",
  [SEMRESATTRS_SERVICE_VERSION]: VERSION,
  language: LANGUAGE,
});

// ---------------------------------------------------------------------------
// OpenTelemetry Tracing Setup
// ---------------------------------------------------------------------------
const tracerProvider = new NodeTracerProvider({ resource });
const traceExporter = new OTLPTraceExporter({ url: OTLP_ENDPOINT });
tracerProvider.addSpanProcessor(new BatchSpanProcessor(traceExporter));
tracerProvider.register();
const tracer = trace.getTracer(APP_NAME, VERSION);

// ---------------------------------------------------------------------------
// OpenTelemetry Metrics Setup
// ---------------------------------------------------------------------------
const metricExporter = new OTLPMetricExporter({ url: OTLP_ENDPOINT });
const metricReader = new PeriodicExportingMetricReader({
  exporter: metricExporter,
  exportIntervalMillis: 5000,
});
const meterProvider = new MeterProvider({ resource });
meterProvider.addMetricReader(metricReader);
const meter = meterProvider.getMeter(APP_NAME, VERSION);

const requestCounter = meter.createCounter("app.requests.total", {
  description: "Total number of requests",
});
const requestSuccessCounter = meter.createCounter("app.requests.success", {
  description: "Total number of successful requests",
});
const requestErrorCounter = meter.createCounter("app.requests.errors", {
  description: "Total number of failed requests",
});
const requestDuration = meter.createHistogram("app.request.duration", {
  description: "Request duration in seconds",
  unit: "s",
});

// System CGroup helpers
function readCgroupLong(v2path, v1path) {
  try {
    if (fs.existsSync(v2path)) {
      const val = fs.readFileSync(v2path, "utf-8").trim();
      if (/^\d+$/.test(val)) return parseInt(val, 10);
    }
    if (fs.existsSync(v1path)) {
      const val = fs.readFileSync(v1path, "utf-8").trim();
      if (/^\d+$/.test(val)) return parseInt(val, 10);
    }
  } catch (ignored) {}
  return 0;
}

function getCgroupMemoryCurrent() {
  return readCgroupLong("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes");
}
function getCgroupMemoryLimit() {
  return readCgroupLong("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes");
}
function getCgroupMemoryPercent() {
  const limit = getCgroupMemoryLimit();
  const current = getCgroupMemoryCurrent();
  return limit > 0 ? (current * 100.0) / limit : 0.0;
}

// System Gauges
const nodeAttrs = { language: LANGUAGE, "service.version": VERSION };

meter.createObservableGauge("app.system.loadavg.1m", {
  description: "System load average (1 minute)",
}).addCallback((observable) => {
  observable.observe(os.loadavg()[0] || 0, nodeAttrs);
});

meter.createObservableGauge("app.process.memory.rss", {
  description: "Process memory usage in bytes",
  unit: "bytes",
}).addCallback((observable) => {
  observable.observe(process.memoryUsage().rss, nodeAttrs);
});

meter.createObservableGauge("app.process.cpu.seconds", {
  description: "Process CPU time in seconds",
  unit: "seconds",
}).addCallback((observable) => {
  const cpu = process.cpuUsage();
  observable.observe((cpu.user + cpu.system) / 1000000.0, nodeAttrs);
});

meter.createObservableGauge("app.container.memory.current", {
  description: "Container memory current usage in bytes",
  unit: "bytes",
}).addCallback((observable) => {
  observable.observe(getCgroupMemoryCurrent(), nodeAttrs);
});

meter.createObservableGauge("app.container.memory.limit", {
  description: "Container memory limit in bytes",
  unit: "bytes",
}).addCallback((observable) => {
  observable.observe(getCgroupMemoryLimit(), nodeAttrs);
});

meter.createObservableGauge("app.container.memory.percent", {
  description: "Container memory usage as a percentage of limit",
}).addCallback((observable) => {
  observable.observe(getCgroupMemoryPercent(), nodeAttrs);
});

// ---------------------------------------------------------------------------
// PostgreSQL Database Pool
// ---------------------------------------------------------------------------
const pool = new Pool({
  host: process.env.POSTGRES_HOST || "postgres",
  port: parseInt(process.env.POSTGRES_PORT || "5432", 10),
  database: process.env.POSTGRES_DB || "observability",
  user: process.env.POSTGRES_USER || "observability",
  password: process.env.POSTGRES_PASSWORD || "observability",
  max: 5,
});

async function initDb(retries = 10, delayMs = 3000) {
  for (let i = 1; i <= retries; i++) {
    try {
      const client = await pool.connect();
      try {
        await client.query(`
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
          ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS response_time_seconds DOUBLE PRECISION;
        `);
        console.log(`[DB] Successfully connected to PostgreSQL on attempt ${i}`);
        return;
      } finally {
        client.release();
      }
    } catch (err) {
      console.warn(`[DB] Attempt ${i}/${retries} failed to connect to Postgres (${err.message}). Retrying in ${delayMs / 1000}s...`);
      await new Promise((res) => setTimeout(res, delayMs));
    }
  }
}

// ---------------------------------------------------------------------------
// Express Web Application
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

function withContext(data = {}) {
  return {
    app_name: APP_NAME,
    version: VERSION,
    language: LANGUAGE,
    timestamp: new Date().toISOString(),
    ...data,
  };
}

// Telemetry Metrics Middleware
app.use((req, res, next) => {
  const start = process.hrtime();
  const endpoint = req.path;

  res.on("finish", () => {
    const diff = process.hrtime(start);
    const durationSec = diff[0] + diff[1] / 1e9;
    const statusCode = res.statusCode;
    const status = statusCode < 400 ? "success" : "error";

    let route = endpoint;
    if (endpoint === "/") route = "home";
    else if (endpoint.startsWith("/compute")) route = "compute";
    else if (endpoint === "/auditlog") route = "auditlog";
    else if (endpoint === "/auditlog/stats") route = "auditlog_stats";
    else if (endpoint === "/eval") route = "eval_expression";

    const journey = endpoint.startsWith("/compute") ? "compute" : endpoint === "/" ? "home" : "other";

    const labels = {
      endpoint,
      route,
      status,
      status_code: String(statusCode),
      journey,
      language: LANGUAGE,
    };

    requestCounter.add(1, labels);
    if (status === "error") {
      requestErrorCounter.add(1, labels);
    } else {
      requestSuccessCounter.add(1, labels);
    }
    requestDuration.record(durationSec, labels);
  });

  next();
});

// ---------------------------------------------------------------------------
// Microservice Endpoints
// ---------------------------------------------------------------------------

// 1. Home endpoint
app.get("/", (req, res) => {
  const span = tracer.startSpan("home-endpoint");
  try {
    const html = `<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Observability Lab (Node.js)</title></head><body><h1>Hello from Observability Lab (Node.js)!</h1><p><strong>App:</strong> ${APP_NAME} | <strong>Version:</strong> ${VERSION} | <strong>Language:</strong> ${LANGUAGE} | <strong>Framework:</strong> Express</p><p>Discover the compute endpoint with a number:</p><ul><li><a href='/compute/5'>Compute 5</a></li><li><a href='/compute/10'>Compute 10</a></li><li><a href='/compute/20'>Compute 20</a></li></ul><p>Actuator Probes:</p><ul><li><a href='/actuator/health'>Health</a></li><li><a href='/actuator/health/liveness'>Liveness</a></li><li><a href='/actuator/health/readiness'>Readiness</a></li><li><a href='/actuator/info'>Info</a></li></ul></body></html>`;
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.send(html);
  } finally {
    span.end();
  }
});

// 2. Version endpoint
app.get("/version", (req, res) => {
  res.json(withContext());
});

// 3. Self-test endpoint
app.get("/selftest", (req, res) => {
  let testsRun = 0;
  let failures = 0;

  try {
    testsRun++;
    if (fibonacci(5) !== 5 || fibonacci(10) !== 55) failures++;

    testsRun++;
    if (evaluate("2+3*4") !== 14 || evaluate("2^10") !== 1024) failures++;

    testsRun++;
    let threw = false;
    try {
      evaluate("5/0");
    } catch {
      threw = true;
    }
    if (!threw) failures++;
  } catch {
    failures++;
  }

  const success = failures === 0;
  res.status(success ? 200 : 500).json(
    withContext({
      tests_run: testsRun,
      success,
      failures,
    })
  );
});

// 4. Fibonacci Compute endpoint
app.get("/compute/:n", (req, res) => {
  const n = parseInt(req.params.n, 10);
  if (isNaN(n) || n > 25) {
    return res.status(400).json(
      withContext({
        error: "Value too large, Max is 25 to prevent DoS",
      })
    );
  }

  const span = tracer.startSpan("compute-endpoint");
  span.setAttribute("compute.value", n);
  try {
    const result = fibonacci(n);
    res.json(withContext({ input: n, result }));
  } finally {
    span.end();
  }
});

// 5. Audit Log (GET & POST)
async function handleAuditLog(req, res) {
  const span = tracer.startSpan("auditlog-endpoint");
  try {
    const endpoint = req.originalUrl || req.path;
    const statusCode = 201;
    const details = JSON.stringify({ language: LANGUAGE, service: APP_NAME });

    await pool.query(
      "INSERT INTO audit_logs (endpoint, status_code, details) VALUES ($1, $2, $3::jsonb)",
      [endpoint, statusCode, details]
    );

    res.status(201).json(withContext({ status: "ok" }));
  } catch (err) {
    res.status(500).json(withContext({ status: "error", message: err.message }));
  } finally {
    span.end();
  }
}
app.get("/auditlog", handleAuditLog);
app.post("/auditlog", handleAuditLog);

// 6. Audit Log Stats
app.get("/auditlog/stats", async (req, res) => {
  const span = tracer.startSpan("auditlog-stats-endpoint");
  try {
    const { rows } = await pool.query("SELECT count(*) AS total_rows FROM audit_logs");
    const totalRows = rows.length ? parseInt(rows[0].total_rows, 10) : 0;
    res.json(withContext({ total_rows: totalRows }));
  } catch (err) {
    res.status(500).json(withContext({ status: "error", message: err.message }));
  } finally {
    span.end();
  }
});

// 7. Math Expression Evaluator (GET & POST)
function handleEval(req, res) {
  let expr = req.query.expr;
  if (!expr && req.body && req.body.expr) {
    expr = req.body.expr;
  }

  if (!expr || typeof expr !== "string" || !expr.trim()) {
    return res.status(400).json(withContext({ error: "Missing 'expr' parameter" }));
  }

  const span = tracer.startSpan("eval-endpoint");
  span.setAttribute("eval.expression", expr);
  try {
    const result = evaluate(expr);
    res.json(withContext({ expression: expr, result }));
  } catch (err) {
    res.status(400).json(withContext({ expression: expr, error: err.message }));
  } finally {
    span.end();
  }
}
app.get("/eval", handleEval);
app.post("/eval", handleEval);

// 8. Actuator & Health Probes (Parity with Java Spring Boot)
app.get("/actuator/health", (req, res) => {
  res.json({ status: "UP" });
});
app.get("/actuator/health/liveness", (req, res) => {
  res.json({ status: "UP" });
});
app.get("/actuator/health/readiness", async (req, res) => {
  try {
    await pool.query("SELECT 1");
    res.json({ status: "UP", components: { db: { status: "UP" } } });
  } catch (err) {
    res.status(503).json({ status: "DOWN", error: err.message });
  }
});
app.get("/actuator/info", (req, res) => {
  res.json({ app: { name: APP_NAME, version: VERSION, language: LANGUAGE } });
});
app.get("/health", (req, res) => {
  res.send("OK");
});

// ---------------------------------------------------------------------------
// Server Bootstrap
// ---------------------------------------------------------------------------
async function start() {
  await initDb();
  app.listen(PORT, "0.0.0.0", () => {
    console.log(`[HTTP] ${APP_NAME} listening on http://0.0.0.0:${PORT}`);
  });
}

start();
