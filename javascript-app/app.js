const express = require('express');
const os = require('os');
const fs = require('fs');
const { Pool } = require('pg');

const { context, trace, metrics } = require('@opentelemetry/api');
const { Resource } = require('@opentelemetry/resources');
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { BatchSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-grpc');
const { MeterProvider, PeriodicExportingMetricReader } = require('@opentelemetry/sdk-metrics');
const { OTLPMetricExporter } = require('@opentelemetry/exporter-metrics-otlp-grpc');
const { LoggerProvider, BatchLogRecordProcessor } = require('@opentelemetry/sdk-logs');
const { OTLPLogExporter } = require('@opentelemetry/exporter-logs-otlp-grpc');

const PORT = Number(process.env.PORT || 8082);
const OTLP_ENDPOINT = process.env.OTEL_EXPORTER_OTLP_ENDPOINT || 'http://otel-collector:4317';

const DB_HOST = process.env.POSTGRES_HOST || 'postgres';
const DB_PORT = Number(process.env.POSTGRES_PORT || 5432);
const DB_NAME = process.env.POSTGRES_DB || 'observability';
const DB_USER = process.env.POSTGRES_USER || 'observability';
const DB_PASSWORD = process.env.POSTGRES_PASSWORD || 'observability';

const APP_NAME = 'observability-javascript-app';
const APP_VERSION = '1.0.1';
const LANGUAGE = 'javascript';

const resource = new Resource({
  'service.name': APP_NAME,
  'service.version': APP_VERSION,
  language: LANGUAGE,
  environment: 'local-dev',
});

const tracerProvider = new NodeTracerProvider({ resource });
tracerProvider.addSpanProcessor(new BatchSpanProcessor(new OTLPTraceExporter({ url: OTLP_ENDPOINT })));
tracerProvider.register();
const tracer = trace.getTracer(APP_NAME);

const metricReader = new PeriodicExportingMetricReader({
  exporter: new OTLPMetricExporter({ url: OTLP_ENDPOINT }),
  exportIntervalMillis: 5000,
});
const meterProvider = new MeterProvider({ resource, readers: [metricReader] });
metrics.setGlobalMeterProvider(meterProvider);
const meter = metrics.getMeter(APP_NAME);

const loggerProvider = new LoggerProvider({ resource });
loggerProvider.addLogRecordProcessor(new BatchLogRecordProcessor(new OTLPLogExporter({ url: OTLP_ENDPOINT })));
const appLogger = loggerProvider.getLogger(APP_NAME);

const requestCounter = meter.createCounter('app.requests.total', { description: 'Total number of requests' });
const requestSuccessCounter = meter.createCounter('app.requests.success', { description: 'Total number of successful requests' });
const requestErrorCounter = meter.createCounter('app.requests.errors', { description: 'Total number of failed requests' });
const requestDuration = meter.createHistogram('app.request.duration', {
  description: 'Request duration in seconds',
  unit: 's',
});

function safeRead(path) {
  try {
    return fs.readFileSync(path, 'utf8').trim();
  } catch (_e) {
    return '';
  }
}

function readCgroupInt(v2Path, v1Path) {
  const v2 = safeRead(v2Path);
  if (/^\d+$/.test(v2)) return Number(v2);
  const v1 = safeRead(v1Path);
  if (/^\d+$/.test(v1)) return Number(v1);
  return 0;
}

function getCgroupMemoryCurrent() {
  return readCgroupInt('/sys/fs/cgroup/memory.current', '/sys/fs/cgroup/memory/memory.usage_in_bytes');
}

function getCgroupMemoryLimit() {
  return readCgroupInt('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes');
}

function getCgroupMemoryPercent() {
  const limit = getCgroupMemoryLimit();
  const current = getCgroupMemoryCurrent();
  return limit > 0 ? (current * 100.0) / limit : 0.0;
}

function getCgroupCpuUsageNs() {
  const cpuStat = safeRead('/sys/fs/cgroup/cpu.stat');
  if (cpuStat) {
    for (const line of cpuStat.split('\n')) {
      if (line.startsWith('usage_usec')) {
        const parts = line.trim().split(/\s+/);
        if (parts.length > 1 && /^\d+$/.test(parts[1])) {
          return Number(parts[1]) * 1000;
        }
      }
    }
  }
  const v1 = safeRead('/sys/fs/cgroup/cpuacct/cpuacct.usage');
  return /^\d+$/.test(v1) ? Number(v1) : 0;
}

function getProcessMemoryRss() {
  return Number(process.memoryUsage().rss || 0);
}

function getProcessCpuSeconds() {
  const usage = process.cpuUsage();
  return (usage.user + usage.system) / 1_000_000;
}

function getSystemLoadAvg() {
  const [oneMin] = os.loadavg();
  return Number.isFinite(oneMin) ? oneMin : 0.0;
}

const gaugeAttrs = { language: LANGUAGE };
meter.createObservableGauge('app.process.memory.rss', {
  description: 'Process RSS memory usage in bytes',
}, (obs) => obs.observe(getProcessMemoryRss(), gaugeAttrs));

meter.createObservableGauge('app.process.cpu.seconds', {
  description: 'Process CPU time in seconds',
}, (obs) => obs.observe(getProcessCpuSeconds(), gaugeAttrs));

meter.createObservableGauge('app.system.loadavg.1m', {
  description: 'System load average (1 minute)',
}, (obs) => obs.observe(getSystemLoadAvg(), gaugeAttrs));

meter.createObservableGauge('app.container.memory.current', {
  description: 'Container memory usage in bytes',
}, (obs) => obs.observe(getCgroupMemoryCurrent(), gaugeAttrs));

meter.createObservableGauge('app.container.memory.limit', {
  description: 'Container memory limit in bytes',
}, (obs) => obs.observe(getCgroupMemoryLimit(), gaugeAttrs));

meter.createObservableGauge('app.container.memory.percent', {
  description: 'Container memory usage as a percentage of limit',
}, (obs) => obs.observe(getCgroupMemoryPercent(), gaugeAttrs));

meter.createObservableGauge('app.container.cpu.usage.ns', {
  description: 'Container CPU usage in nanoseconds',
}, (obs) => obs.observe(getCgroupCpuUsageNs(), gaugeAttrs));

const dbPool = new Pool({
  host: DB_HOST,
  port: DB_PORT,
  database: DB_NAME,
  user: DB_USER,
  password: DB_PASSWORD,
  max: 20,
});

async function ensureAuditTable() {
  await dbPool.query(`
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
  `);
  await dbPool.query(`
    ALTER TABLE audit_logs
    ADD COLUMN IF NOT EXISTS response_time_seconds DOUBLE PRECISION;
  `);
}

function withContextPayload(payload) {
  return {
    ...payload,
    app_name: APP_NAME,
    version: APP_VERSION,
    language: LANGUAGE,
    timestamp: new Date().toISOString(),
  };
}

function logInfo(message, attributes = {}) {
  appLogger.emit({
    body: message,
    severityText: 'INFO',
    attributes: {
      language: LANGUAGE,
      ...attributes,
    },
  });
}

function mapRoute(pathname) {
  if (pathname === '/') return 'home';
  if (pathname.startsWith('/compute')) return 'compute';
  if (pathname === '/auditlog') return 'auditlog';
  if (pathname === '/auditlog/stats') return 'auditlog_stats';
  if (pathname === '/eval') return 'eval_expression';
  return pathname;
}

function mapJourney(pathname) {
  if (pathname.startsWith('/compute')) return 'compute';
  if (pathname === '/') return 'home';
  return 'other';
}

function recordRequestMetrics(pathname, statusCode, durationSec) {
  const status = statusCode < 400 ? 'success' : 'error';
  const labels = {
    endpoint: pathname,
    route: mapRoute(pathname),
    status,
    status_code: String(statusCode),
    journey: mapJourney(pathname),
    language: LANGUAGE,
  };

  requestCounter.add(1, labels);
  requestDuration.record(durationSec, labels);

  if (status === 'error') {
    requestErrorCounter.add(1, labels);
  } else {
    requestSuccessCounter.add(1, labels);
  }
}

function fibonacci(n) {
  if (n <= 1) return n;
  return fibonacci(n - 1) + fibonacci(n - 2);
}

const OPERATORS = {
  '+': { prec: 1, assoc: 'L' },
  '-': { prec: 1, assoc: 'L' },
  '*': { prec: 2, assoc: 'L' },
  '/': { prec: 2, assoc: 'L' },
  '^': { prec: 3, assoc: 'R' },
};

function tokenise(expr) {
  const tokens = [];
  const s = String(expr || '');
  let i = 0;

  while (i < s.length) {
    const c = s[i];
    if (/\s/.test(c)) {
      i += 1;
      continue;
    }

    const prev = tokens[tokens.length - 1];
    const unaryMinus = c === '-' && (!prev || prev.type === 'op' || prev.type === 'lparen');
    if (/\d|\./.test(c) || unaryMinus) {
      let j = i;
      if (unaryMinus) j += 1;
      let seenDot = false;
      while (j < s.length && /\d|\./.test(s[j])) {
        if (s[j] === '.') {
          if (seenDot) break;
          seenDot = true;
        }
        j += 1;
      }
      const numText = s.slice(i, j);
      if (!/^-?\d*\.?\d+$/.test(numText)) {
        throw new Error(`Invalid number: ${numText}`);
      }
      tokens.push({ type: 'num', value: numText });
      i = j;
      continue;
    }

    if (Object.prototype.hasOwnProperty.call(OPERATORS, c)) {
      tokens.push({ type: 'op', value: c });
      i += 1;
      continue;
    }

    if (c === '(') {
      tokens.push({ type: 'lparen', value: c });
      i += 1;
      continue;
    }

    if (c === ')') {
      tokens.push({ type: 'rparen', value: c });
      i += 1;
      continue;
    }

    throw new Error(`Unrecognised character: ${c}`);
  }

  return tokens;
}

function toRpn(tokens) {
  const out = [];
  const stack = [];

  for (const token of tokens) {
    if (token.type === 'num') {
      out.push(token);
    } else if (token.type === 'op') {
      while (stack.length > 0) {
        const top = stack[stack.length - 1];
        if (top.type !== 'op') break;
        const a = OPERATORS[token.value];
        const b = OPERATORS[top.value];
        const shouldPop = (a.assoc === 'L' && a.prec <= b.prec) || (a.assoc === 'R' && a.prec < b.prec);
        if (!shouldPop) break;
        out.push(stack.pop());
      }
      stack.push(token);
    } else if (token.type === 'lparen') {
      stack.push(token);
    } else if (token.type === 'rparen') {
      while (stack.length > 0 && stack[stack.length - 1].type !== 'lparen') {
        out.push(stack.pop());
      }
      if (stack.length === 0) {
        throw new Error('Mismatched parentheses');
      }
      stack.pop();
    }
  }

  while (stack.length > 0) {
    const t = stack.pop();
    if (t.type === 'lparen' || t.type === 'rparen') {
      throw new Error('Mismatched parentheses');
    }
    out.push(t);
  }

  return out;
}

function evalRpn(rpn) {
  const stack = [];
  for (const token of rpn) {
    if (token.type === 'num') {
      stack.push(Number(token.value));
      continue;
    }

    if (stack.length < 2) {
      throw new Error('Invalid expression');
    }

    const right = stack.pop();
    const left = stack.pop();

    switch (token.value) {
      case '+':
        stack.push(left + right);
        break;
      case '-':
        stack.push(left - right);
        break;
      case '*':
        stack.push(left * right);
        break;
      case '/':
        if (right === 0) throw new Error('Division by zero');
        stack.push(left / right);
        break;
      case '^':
        stack.push(left ** right);
        break;
      default:
        throw new Error(`Unknown operator: ${token.value}`);
    }
  }

  if (stack.length !== 1) {
    throw new Error('Invalid expression');
  }

  return stack[0];
}

function evaluateExpression(expr) {
  const trimmed = String(expr || '').trim();
  if (!trimmed) {
    throw new Error("Missing 'expr' parameter");
  }
  return evalRpn(toRpn(tokenise(trimmed)));
}

function getHomeHtml() {
  return "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Observability Lab (JavaScript)</title></head><body><h1>Hello from Observability Lab (JavaScript)!</h1><p><strong>App:</strong> observability-javascript-app | <strong>Version:</strong> 1.0.1 | <strong>Language:</strong> javascript</p><p>Discover the compute endpoint with a number:</p><ul><li><a href='/compute/5'>Compute 5</a></li><li><a href='/compute/10'>Compute 10</a></li><li><a href='/compute/20'>Compute 20</a></li></ul></body></html>";
}

function runSelfTests() {
  const tests = [
    () => fibonacci(5) === 5,
    () => fibonacci(10) === 55,
    () => evaluateExpression('2+3*4') === 14,
    () => evaluateExpression('2^10') === 1024,
  ];

  let failures = 0;
  for (const test of tests) {
    try {
      if (!test()) failures += 1;
    } catch (_e) {
      failures += 1;
    }
  }

  return {
    tests_run: tests.length,
    success: failures === 0,
    failures,
  };
}

const app = express();
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use((req, res, next) => {
  const startNs = process.hrtime.bigint();
  res.on('finish', () => {
    const durationSec = Number(process.hrtime.bigint() - startNs) / 1_000_000_000;
    recordRequestMetrics(req.path, res.statusCode, durationSec);
  });
  next();
});

app.get('/', (_req, res) => {
  tracer.startActiveSpan('home-endpoint', (span) => {
    try {
      logInfo('Home endpoint called (javascript)');
      res.status(200).set('Content-Type', 'text/html').send(getHomeHtml());
    } finally {
      span.end();
    }
  });
});

app.get('/version', (_req, res) => {
  res.status(200).json(withContextPayload({ version: APP_VERSION, language: LANGUAGE }));
});

app.get('/selftest', (_req, res) => {
  const result = runSelfTests();
  const status = result.success ? 200 : 500;
  res.status(status).json(withContextPayload(result));
});

app.get('/compute/:n', (req, res) => {
  tracer.startActiveSpan('compute-endpoint', (span) => {
    try {
      const n = Number(req.params.n);
      if (!Number.isInteger(n) || n < 0) {
        res.status(400).json(withContextPayload({ error: 'Invalid input. Use a non-negative integer.' }));
        return;
      }
      if (n > 25) {
        res.status(400).json(withContextPayload({ error: 'Value too large, Max is 25 to prevent DoS' }));
        return;
      }
      span.setAttribute('compute.value', n);
      const result = fibonacci(n);
      res.status(200).json(withContextPayload({ input: n, result }));
    } finally {
      span.end();
    }
  });
});

app.all('/auditlog', async (req, res) => {
  await tracer.startActiveSpan('auditlog-endpoint', async (span) => {
    try {
      const start = process.hrtime.bigint();
      const processMemory = getProcessMemoryRss();
      const processCpu = getProcessCpuSeconds();
      const loadAvg = getSystemLoadAvg();
      const containerMemoryCurrent = getCgroupMemoryCurrent();
      const containerMemoryLimit = getCgroupMemoryLimit();
      const containerMemoryPercent = getCgroupMemoryPercent();
      const containerCpuNs = getCgroupCpuUsageNs();

      const details = {
        remote_addr: req.ip,
        user_agent: req.get('user-agent'),
        query_params: req.query,
        form_data: req.body,
      };

      const responseTime = Number(process.hrtime.bigint() - start) / 1_000_000_000;

      const insertResult = await dbPool.query(
        `INSERT INTO audit_logs (
          endpoint, status_code, response_time_seconds, process_memory_rss,
          process_cpu_seconds, system_loadavg_1m, container_memory_current,
          container_memory_limit, container_memory_percent, container_cpu_usage_ns,
          details
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        RETURNING id`,
        [
          req.path,
          200,
          responseTime,
          processMemory,
          processCpu,
          loadAvg,
          containerMemoryCurrent,
          containerMemoryLimit,
          containerMemoryPercent,
          containerCpuNs,
          JSON.stringify(details),
        ]
      );

      res.status(201).json(withContextPayload({
        status: 'ok',
        audit_id: insertResult.rows[0].id,
        response_time_seconds: responseTime,
        process_memory_rss: processMemory,
        process_cpu_seconds: processCpu,
        system_loadavg_1m: loadAvg,
        container_memory_current: containerMemoryCurrent,
        container_memory_limit: containerMemoryLimit,
        container_memory_percent: containerMemoryPercent,
        container_cpu_usage_ns: containerCpuNs,
        details,
      }));
    } catch (err) {
      res.status(500).json(withContextPayload({
        status: 'error',
        message: 'Failed to write audit log',
        error: String(err.message || err),
      }));
    } finally {
      span.end();
    }
  });
});

app.get('/auditlog/stats', async (req, res) => {
  await tracer.startActiveSpan('auditlog-stats-endpoint', async (span) => {
    try {
      span.setAttribute('http.method', req.method);
      const histogramBucketCount = 10;
      const histogramMaxSeconds = 5.0;

      const totalsQuery = await dbPool.query(`
        SELECT
          count(*) AS total_rows,
          sum((status_code < 400)::int) AS success_count,
          sum((status_code >= 400)::int) AS failure_count,
          percentile_disc(0.5) WITHIN GROUP (ORDER BY response_time_seconds) AS p50_seconds,
          percentile_disc(0.9) WITHIN GROUP (ORDER BY response_time_seconds) AS p90_seconds,
          percentile_disc(0.95) WITHIN GROUP (ORDER BY response_time_seconds) AS p95_seconds,
          percentile_disc(0.99) WITHIN GROUP (ORDER BY response_time_seconds) AS p99_seconds
        FROM audit_logs;
      `);

      const histogramQuery = await dbPool.query(
        `SELECT width_bucket(response_time_seconds, 0, $1, $2) AS bucket,
                count(*) AS bucket_count
         FROM audit_logs
         WHERE response_time_seconds IS NOT NULL
         GROUP BY bucket
         ORDER BY bucket`,
        [histogramMaxSeconds, histogramBucketCount]
      );

      const totals = totalsQuery.rows[0] || {};
      const buckets = histogramQuery.rows.map((row) => {
        const bucket = Number(row.bucket);
        const count = Number(row.bucket_count);

        if (bucket === 0) {
          return {
            bucket,
            lower_bound_seconds: 0.0,
            upper_bound_seconds: 0.0,
            count,
          };
        }

        if (bucket > histogramBucketCount) {
          return {
            bucket,
            lower_bound_seconds: histogramMaxSeconds,
            upper_bound_seconds: null,
            count,
          };
        }

        const width = histogramMaxSeconds / histogramBucketCount;
        return {
          bucket,
          lower_bound_seconds: (bucket - 1) * width,
          upper_bound_seconds: bucket * width,
          count,
        };
      });

      res.status(200).json(withContextPayload({
        total_rows: Number(totals.total_rows || 0),
        success_count: Number(totals.success_count || 0),
        failure_count: Number(totals.failure_count || 0),
        percentiles: {
          p50_seconds: totals.p50_seconds == null ? null : Number(totals.p50_seconds),
          p90_seconds: totals.p90_seconds == null ? null : Number(totals.p90_seconds),
          p95_seconds: totals.p95_seconds == null ? null : Number(totals.p95_seconds),
          p99_seconds: totals.p99_seconds == null ? null : Number(totals.p99_seconds),
        },
        response_time_histogram: {
          bucket_count: histogramBucketCount,
          max_seconds: histogramMaxSeconds,
          buckets,
        },
      }));
    } catch (err) {
      res.status(500).json(withContextPayload({
        status: 'error',
        message: 'Failed to read audit log statistics',
        error: String(err.message || err),
      }));
    } finally {
      span.end();
    }
  });
});

app.all('/eval', (req, res) => {
  tracer.startActiveSpan('eval-endpoint', (span) => {
    try {
      const expr = req.query.expr || req.body?.expr || '';
      span.setAttribute('eval.expression', String(expr));
      const result = evaluateExpression(expr);
      res.status(200).json(withContextPayload({ expression: expr, result }));
    } catch (err) {
      res.status(400).json(withContextPayload({ error: String(err.message || err) }));
    } finally {
      span.end();
    }
  });
});

(async () => {
  try {
    await ensureAuditTable();
    logInfo('JavaScript app starting', { service: APP_NAME });
    app.listen(PORT, '0.0.0.0', () => {
      logInfo(`JavaScript app listening on ${PORT}`);
    });
  } catch (err) {
    console.error('Failed to start application:', err);
    process.exit(1);
  }
})();
