# Observability Demo

A full-stack observability reference implementation built on the Grafana LGTM stack (Loki, Grafana, Tempo, Mimir). A Python Flask application emits traces, metrics, and logs via OpenTelemetry. A dedicated canary container continuously exercises every endpoint at a calibrated rate. Everything runs locally with a single command.

---

## Table of Contents

- [Architecture](#architecture)
- [Data Flow](#data-flow)
- [Services](#services)
- [Endpoints](#endpoints)
- [Quick Start — Existing Machine](#quick-start--existing-machine)
- [Setup — Brand New Ubuntu Machine](#setup--brand-new-ubuntu-machine)
- [Running Tests](#running-tests)
- [Scripts Reference](#scripts-reference)
- [Project Structure](#project-structure)
- [Canary Workload](#canary-workload)
- [Expression Evaluator](#expression-evaluator)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        observability-network (Docker bridge)            │
│                                                                         │
│  ┌──────────────┐   HTTP/gRPC    ┌─────────────────────────────────┐   │
│  │    canary    │ ─────────────► │       observability-demo         │   │
│  │  (Python)    │                │         (Flask app)              │   │
│  │  24 TPS      │                │  GET /                           │   │
│  │  20 targets  │                │  GET /compute/<n>                │   │
│  └──────────────┘                │  GET|POST /auditlog              │   │
│                                  │  GET /auditlog/stats             │   │
│                                  │  GET|POST /eval                  │   │
│                                  └──────────┬──────────────────────┘   │
│                                             │ OTLP gRPC (4317)         │
│                                             ▼                           │
│                                  ┌──────────────────┐                  │
│                                  │  otel-collector  │                  │
│                                  │  (OTel Contrib)  │                  │
│                                  └──┬───────┬───────┘                  │
│                    traces ──────────┘       │ metrics    logs           │
│                       ▼                     ▼              ▼            │
│              ┌────────────────┐  ┌────────────────┐  ┌──────────┐     │
│              │     tempo      │  │     mimir      │  │   loki   │     │
│              │  (tracing)     │  │  (metrics)     │  │  (logs)  │     │
│              └───────┬────────┘  └───────┬────────┘  └────┬─────┘     │
│                      │ Kafka ingest       │ remote write   │           │
│              ┌───────▼────────┐  ┌───────▼────────┐       │           │
│              │   redpanda     │  │   prometheus   │       │           │
│              │  (Kafka broker)│  │  (scrape+fwd)  │       │           │
│              └────────────────┘  └───────┬────────┘       │           │
│                                          │ pg metrics      │           │
│                                  ┌───────▼────────┐        │           │
│                                  │postgres-export │        │           │
│                                  └───────┬────────┘        │           │
│                                          │                  │           │
│                                  ┌───────▼────────┐        │           │
│                                  │   postgres     │        │           │
│                                  │  (audit_logs)  │        │           │
│                                  └────────────────┘        │           │
│                                                             │           │
│  ┌──────────────────────────────────────────────────────────▼────────┐ │
│  │                          grafana                                   │ │
│  │   datasources: Tempo · Mimir · Loki    dashboard: auto-provisioned │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow

### Telemetry pipeline

```
Flask app
    │
    ├─ Traces ──► OTel Collector ──► Tempo ──────────────────► Grafana
    │                                    └─► Redpanda (Kafka)
    │
    ├─ Metrics ─► OTel Collector ──► Mimir ◄── Prometheus ◄── postgres-exporter ◄── Postgres
    │                                               └──────────────────────────────► Grafana
    │
    └─ Logs ───► OTel Collector ──► Loki ───────────────────► Grafana
```

### Request flow — /eval endpoint

```
Client
  │  GET /eval?expr=((3+4)*6^2/5)+1-7
  ▼
Flask /eval route
  │
  ├─ tokenise()        "((3+4)*6^2/5)+1-7"
  │    └─► [LEFT_PAREN, LEFT_PAREN, 3, +, 4, RIGHT_PAREN, *, 6, ^, 2, /, 5, ...]
  │
  ├─ to_rpn()          Shunting-Yard algorithm
  │    └─► [3, 4, +, 6, 2, ^, *, 5, /, 1, +, 7, -]
  │
  ├─ evaluate_rpn()    operand stack
  │    └─► 44.4
  │
  └─► {"expression": "((3+4)*6^2/5)+1-7", "result": 44.4}
```

### Canary flow

```
canary container
  │
  ├─ wait_for_app()    polls http://observability-demo:5000/ until 200
  │
  └─ loop (24 TPS, paced inter-arrival = 1/24 ≈ 42 ms)
       │
       ├─ fire request ──► observability-demo
       ├─ measure response_time
       ├─ emit OTel metrics (client="canary") ──► otel-collector ──► mimir
       └─ sleep max(0, 0.042 - response_time)
```

### Audit log write flow

```
POST /auditlog
  │
  ├─ collect system snapshot
  │    process RSS, CPU seconds, system load avg
  │    cgroup memory current/limit/%, CPU ns
  │
  ├─ with_db() ──► INSERT INTO audit_logs ──► Postgres
  │    └─ conn.close() guaranteed in finally block
  │
  └─► {"status": "ok", "audit_id": <id>, ...}
```

---

## Grafana Dashboard

The pre-provisioned dashboard is available at **http://localhost:3000** immediately after `./launch.sh` completes. No login required (anonymous admin).

![Grafana Dashboard](docs/grafana-dashboard.png)

The dashboard is organised into collapsible rows:

| Row | What it shows |
|---|---|
| SLO / Health | Overall success rate, compute journey success rate, error budget burn, total RPS, P95 latency, alert indicator |
| Traffic, Latency & Errors (RED) | Request rate by route, latency percentiles (p50/p95/p99), error rate + error % |
| Saturation | Process RSS memory, system load average (1m), process CPU rate |
| Container Resources | Memory used vs limit, memory %, CPU cores |
| Synthetic Workload | Canary request rate by path, canary P95 latency by path |
| PostgreSQL | Active connections, cache hit rate, commit vs rollback rate, rows fetched/inserted/updated |
| Logs (Loki) | Live application log stream, errors-only filtered view |

---

## Services

| Container | Image | Port (DC) | Port (K8s) | Role |
|---|---|---|---|---|
| `observability-python-app` | local build | 5000 | 30001 | Flask app — compute, audit log, expression eval |
| `observability-java-app` | local build | 8080 | 30005 | Spring Boot app — compute, audit log |
| `observability-rust-app` | local build | 8083 | 30006 | Axum app — compute, audit log |
| `canary` | local build | — | — | Synthetic canary, TPS varies, all endpoints |
| `otel-collector` | otel-contrib | 4317 (gRPC), 4318 (HTTP) | — | Receives OTLP, routes to backends |
| `tempo` | grafana/tempo | 3200 | 30003 | Distributed tracing backend |
| `redpanda` | redpandadata/redpanda | 9092 | — | Kafka broker for Tempo ingest |
| `mimir` | grafana/mimir | 9009 | 30004 | Long-term metrics storage |
| `loki` | grafana/loki | 3100 | 30002 | Log aggregation |
| `grafana` | grafana/grafana | 3000 | 30000 | Dashboards — Tempo + Mimir + Loki |
| `prometheus` | prom/prometheus | 9090 | — | Scrapes postgres-exporter, remote-writes to Mimir |
| `postgres-exporter` | wrouesnel/postgres_exporter | 9187 (internal) | — | Exports pg_* metrics |
| `postgres` | postgres:16 | 5432 | — | Audit log persistence |

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Home page with compute links |
| `GET` | `/compute/<n>` | Compute Fibonacci(n). 10% random 500 error rate by design |
| `GET\|POST` | `/auditlog` | Write system snapshot to Postgres, return audit record |
| `GET` | `/auditlog/stats` | Response time percentiles (p50/p90/p95/p99) + histogram |
| `GET\|POST` | `/eval?expr=<expression>` | Evaluate a mathematical expression (PEMDAS, no eval()) |

### /eval examples

```bash
# Simple arithmetic
curl "http://localhost:5000/eval?expr=3+4"
# {"expression":"3+4","result":7.0}

# Nested PEMDAS
curl "http://localhost:5000/eval?expr=((3%2B4)*6%5E2%2F5)%2B1-7"
# {"expression":"((3+4)*6^2/5)+1-7","result":44.4}

# POST with JSON
curl -X POST http://localhost:5000/eval \
  -H 'Content-Type: application/json' \
  -d '{"expr": "(2+3)^2"}'
# {"expression":"(2+3)^2","result":25.0}

# Error handling
curl "http://localhost:5000/eval?expr=5/0"
# {"error":"Division by zero"}  HTTP 400
```

---

## Quick Start

### Docker Compose (Python + Java + Rust apps)

Requires: Docker 20+, Docker Compose v2+.

```bash
git clone https://github.com/sethusrinivasan/observability-demo.git
cd observability-demo
./launch.sh
```

### Kubernetes (Kind cluster)

Requires: Docker 20+, kubectl, kind.

```bash
git clone https://github.com/sethusrinivasan/observability-demo.git
cd observability-demo
./k8s/deploy-k8s.sh
```

When deployment completes:

| Service | Docker Compose URL | Kubernetes URL |
|---|---|---|
| Python App | http://localhost:5000 | http://localhost:30001 |
| Java App | http://localhost:8080 | http://localhost:30005 |
| Rust App | http://localhost:8083 | http://localhost:30006 |
| Grafana | http://localhost:3000 | http://localhost:3000 |
| Prometheus | http://localhost:9090 | http://localhost:9090 |
| Tempo | http://localhost:3200 | http://localhost:3200 |
| Loki | http://localhost:3100 | http://localhost:3100 |
| Mimir | http://localhost:9009 | http://localhost:9009 |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        observability-network (Docker/K8s)               │
│                                                                         │
│  ┌──────────────┐   HTTP/gRPC    ┌─────────────────────────────────┐   │
│  │    canary    │ ─────────────► │   Multi-language apps           │   │
│  │  (Python)    │                │   (Python/Flask, Java/Spring,   │   │
│  │  TPS: 24 DC  │                │    Rust/Axum)                   │   │
│  │       2 K8s  │                │  GET /                           │   │
│  │  20 targets  │                │  GET /compute/<n>                │   │
│  └──────────────┘                │  GET|POST /auditlog              │   │
│                                  │  GET /auditlog/stats             │   │
│                                  │  GET|POST /eval                  │   │
│                                  └──────────┬──────────────────────┘   │
│                                             │ OTLP gRPC (4317)         │
│                                             ▼                           │
│                                  ┌──────────────────┐                  │
│                                  │  otel-collector  │                  │
│                                  │  (OTel Contrib)  │                  │
│                                  └──┬───────┬───────┘                  │
│                    traces ──────────┘       │ metrics    logs           │
│                       ▼                     ▼              ▼            │
│              ┌────────────────┐  ┌────────────────┐  ┌──────────┐     │
│              │     tempo      │  │     mimir      │  │   loki   │     │
│              │  (tracing)     │  │  (metrics)     │  │  (logs)  │     │
│              └───────┬────────┘  └───────┬────────┘  └────┬─────┘     │
│                      │ Kafka ingest       │ remote write   │           │
│              ┌───────▼────────┐  ┌───────▼────────┐       │           │
│              │   redpanda     │  │   prometheus   │       │           │
│              │  (Kafka broker)│  │  (scrape+fwd)  │       │           │
│              └────────────────┘  └───────┬────────┘       │           │
│                                          │ pg metrics      │           │
│                                  ┌───────▼────────┐        │           │
│                                  │postgres-export │        │           │
│                                  └───────┬────────┘        │           │
│                                          │                  │           │
│                                  ┌───────▼────────┐        │           │
│                                  │   postgres     │        │           │
│                                  │  (audit_logs)  │        │           │
│                                  └────────────────┘        │           │
│                                                             │           │
│  ┌──────────────────────────────────────────────────────────▼────────┐ │
│  │                          grafana                                   │ │
│  │   datasources: Tempo · Mimir · Loki    dashboard: auto-provisioned │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Running Tests

```bash
# All tests (unit + evaluator + infrastructure smoke tests)
.venv/bin/python -m pytest tests/ -v

# Unit tests only (no stack needed)
.venv/bin/python -m pytest tests/test_app.py tests/test_evaluator.py -v

# Infrastructure tests (requires running stack)
.venv/bin/python -m pytest tests/test_infrastructure.py -v

# Shell smoke test against live stack
./test.sh
```

Test breakdown:

| File | Tests | Requires stack |
|---|---|---|
| `tests/test_app.py` | 48 | Postgres only |
| `tests/test_evaluator.py` | 69 | No |
| `tests/test_infrastructure.py` | 41 | Yes (all services) |

---

## Scripts Reference

| Script | Purpose |
|---|---|
| `./launch.sh` | Full teardown + fresh deploy (Docker Compose). Use on first run or after config changes. Accepts `--keep-data` to preserve volumes |
| `./start.sh` | Start Docker Compose stack without teardown (faster, keeps existing containers) |
| `./test.sh` | Smoke test Python app endpoints against the running Docker Compose stack |
| `./k8s/deploy-k8s.sh` | Deploy full observability stack to Kind Kubernetes cluster |

---

## Project Structure

```
observability-demo/
├── app.py                          # Flask application
├── evaluator.py                    # Shunting-Yard expression evaluator
├── Dockerfile                      # App container image
├── requirements.txt                # Python dependencies
├── docker-compose.yaml             # Full stack definition (11 services)
│
├── canary/
│   ├── canary.py                   # Standalone synthetic canary
│   ├── Dockerfile                  # Canary container image
│   └── requirements.txt            # Minimal deps (requests + OTel only)
│
├── config/
│   ├── otel-collector.yaml         # OTLP receiver → Tempo/Mimir/Loki routing
│   ├── tempo.yaml                  # Trace storage + Kafka ingest
│   ├── loki.yaml                   # Log storage (filesystem, schema v13)
│   ├── mimir.yaml                  # Metrics storage (filesystem)
│   ├── prometheus.yaml             # Scrape postgres-exporter, remote-write to Mimir
│   ├── grafana-datasources.yaml    # Auto-provision Tempo, Mimir, Loki datasources
│   ├── grafana-dashboard-provider.yaml
│   └── dashboards/
│       └── observability-demo-metrics.json   # Pre-built Grafana dashboard
│
├── tests/
│   ├── conftest.py                 # Python 3.13 compat shims, host/Docker DB resolution
│   ├── test_app.py                 # Flask route + DB unit tests (48 tests)
│   ├── test_evaluator.py           # Evaluator pipeline unit tests (69 tests)
│   └── test_infrastructure.py     # End-to-end container smoke tests (41 tests)
│
├── pytest.ini                      # Test config (no cache, suppress OTel warnings)
├── launch.sh                       # Full teardown + deploy script
├── start.sh                        # Quick start script
├── redeploy.sh                     # Rebuild app + canary only
├── build.sh                        # Build app image only
└── test.sh                         # Shell smoke test
```

---

## Canary Workload

The canary runs in its own container (`canary/`) completely isolated from the app. It exercises all 20 endpoint targets at a uniform rate.

**Rate calibration:**
- Target: 24 TPS → ~50% saturation of one CPU core on a 4-core host
- Pacing: `sleep = max(0, 1/TPS - response_time)` — uniform inter-arrival regardless of endpoint latency
- Tune via env var: `CANARY_TPS=10 docker compose up`

**Coverage:**

| Endpoint | Cases |
|---|---|
| `GET /` | 1 |
| `GET /compute/<n>` | 3 (n=5, 10, 20) |
| `GET /auditlog` | 1 |
| `POST /auditlog` | 1 |
| `GET /auditlog/stats` | 1 |
| `GET /eval` | 9 (one per operator/feature) |
| `POST /eval` (JSON) | 1 |
| `GET /eval` error paths | 3 (÷0, invalid char, missing param) |

Error-path canaries (expected 4xx) are counted as `success` in metrics so they don't inflate the error rate.

View canary logs:
```bash
docker logs -f canary
```

---

## Expression Evaluator

`/eval` implements a mathematical expression parser from scratch using the **Shunting-Yard algorithm** (Dijkstra, 1961). No `eval()`, no third-party parsing libraries.

**Pipeline:**
```
expression string
    │
    ▼ tokenise()
[NUMBER, OPERATOR, LEFT_PAREN, ...]
    │
    ▼ to_rpn()   ← Shunting-Yard
[RPN token list]
    │
    ▼ evaluate_rpn()   ← operand stack
float result
```

**PEMDAS operator table:**

| Operator | Precedence | Associativity | Example |
|---|---|---|---|
| `^` | 4 (highest) | Right | `2^3^2` = `2^(3^2)` = 512 |
| `*` `/` | 3 | Left | `12/4*3` = `(12/4)*3` = 9 |
| `+` `-` | 2 (lowest) | Left | `10-3+2` = `(10-3)+2` = 9 |

**Supported:**
- Integers and decimals: `3`, `3.14`
- All five operators: `+` `-` `*` `/` `^`
- Nested parentheses: `((3+4)*6^2/5)+1-7` → `44.4`
- Unary minus: `-5+8` → `3.0`
- Error handling: division by zero, mismatched parentheses, invalid characters

---

## Acknowledgements

This project was co-authored with **[Kiro](https://kiro.dev)**, an AI-powered IDE built to assist developers.

Kiro contributed to the design, implementation, debugging, and documentation of this project — including the observability pipeline configuration, the expression evaluator, the canary workload, the test suite, the Grafana dashboard, and the setup scripts.
