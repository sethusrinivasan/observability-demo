# Observability Demo

A full-stack observability reference implementation built on the **Grafana LGTM stack** (Loki, Grafana, Tempo, Mimir). Three application services — Python/Flask, Java/Spring Boot, and Rust/Axum — each emit traces, metrics, and logs via OpenTelemetry. A dedicated canary container continuously exercises every endpoint. Runs locally via Docker Compose or Kubernetes (Kind).

---

## Table of Contents

- [Architecture](#architecture)
- [Services](#services)
- [Endpoints](#endpoints)
- [Quick Start — Docker Compose](#quick-start--docker-compose)
- [Quick Start — Kubernetes (Kind)](#quick-start--kubernetes-kind)
- [Running Tests](#running-tests)
- [Scripts Reference](#scripts-reference)
- [Project Structure](#project-structure)
- [Grafana Dashboard](#grafana-dashboard)
- [Canary Workload](#canary-workload)
- [Expression Evaluator](#expression-evaluator)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        observability-network                            │
│                                                                         │
│  ┌──────────────┐   HTTP     ┌───────────────────────────────────────┐  │
│  │    canary    │ ─────────► │  Multi-language apps                  │  │
│  │  (Python)    │            │  Python/Flask · Java/Spring · Rust/Axum│ │
│  └──────────────┘            └──────────────┬────────────────────────┘  │
│                                             │ OTLP gRPC (4317)          │
│                                             ▼                            │
│                                  ┌──────────────────┐                   │
│                                  │  otel-collector  │                   │
│                                  └──┬───────┬───────┘                   │
│                    traces ──────────┘       │ metrics + logs             │
│                       ▼                     ▼              ▼             │
│              ┌────────────────┐  ┌────────────────┐  ┌──────────┐      │
│              │     tempo      │  │     mimir      │  │   loki   │      │
│              └───────┬────────┘  └───────┬────────┘  └────┬─────┘      │
│                      │ Kafka             │ remote write    │            │
│              ┌───────▼────────┐  ┌───────▼────────┐       │            │
│              │   redpanda     │  │   prometheus   │       │            │
│              └────────────────┘  └───────┬────────┘       │            │
│                                          │                 │            │
│                                  ┌───────▼────────┐        │            │
│                                  │    postgres    │        │            │
│                                  └────────────────┘        │            │
│  ┌──────────────────────────────────────────────────────────▼─────────┐ │
│  │                           grafana                                   │ │
│  │     datasources: Tempo · Mimir · Loki   dashboard: auto-provisioned │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Services

| Container | Image | Port (Docker) | Port (K8s) | Role |
|---|---|---|---|---|
| `observability-python-app` | local build | 5000 | 30001 | Flask app — compute, audit log, expression eval |
| `observability-java-app` | local build | 8080 | 30005 | Spring Boot app — compute, audit log |
| `observability-rust-app` | local build | 8083 | 30006 | Axum app — compute, audit log |
| `canary` | local build | — | — | Synthetic load generator |
| `otel-collector` | otel-contrib | 4317/4318 | — | Receives OTLP, routes to backends |
| `tempo` | grafana/tempo | 3200 | 30003 | Distributed tracing backend |
| `redpanda` | redpandadata/redpanda | 9092 | — | Kafka broker for Tempo ingest |
| `mimir` | grafana/mimir | 9009 | 30004 | Long-term metrics storage |
| `loki` | grafana/loki | 3100 | 30002 | Log aggregation |
| `grafana` | grafana/grafana | 3000 | 30000 | Dashboards |
| `prometheus` | prom/prometheus | 9090 | — | Scrapes postgres-exporter, remote-writes to Mimir |
| `postgres-exporter` | wrouesnel/postgres_exporter | 9187 | — | Exports pg_* metrics |
| `postgres` | postgres:16 | 5432 | — | Audit log persistence |

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Home page |
| `GET` | `/compute/<n>` | Fibonacci(n). 10% random 500 error rate by design |
| `GET\|POST` | `/auditlog` | Write system snapshot to Postgres |
| `GET` | `/auditlog/stats` | Response time percentiles (p50/p90/p95/p99) + histogram |
| `GET\|POST` | `/eval?expr=<expression>` | Evaluate a mathematical expression (PEMDAS) |

---

## Quick Start — Docker Compose

Requires: Docker 20+, Docker Compose v2+.

```bash
git clone https://github.com/sethusrinivasan/observability-demo.git
cd observability-demo
./launch_docker.sh --run
```

When complete:

| Service | URL |
|---|---|
| Python App | http://localhost:5000 |
| Java App | http://localhost:8080 |
| Rust App | http://localhost:8083 |
| Grafana | http://localhost:3000 |
| Prometheus | http://localhost:9090 |
| Tempo | http://localhost:3200 |
| Loki | http://localhost:3100 |
| Mimir | http://localhost:9009 |

---

## Quick Start — Kubernetes (Kind)

Requires: Docker 20+. `kubectl` and `kind` are installed automatically if missing.

```bash
git clone https://github.com/sethusrinivasan/observability-demo.git
cd observability-demo
./launch_k8s.sh --run
```

When complete:

| Service | URL |
|---|---|
| Python App | http://localhost:5000 |
| Java App | http://localhost:8080 |
| Rust App | http://localhost:8081 |
| Grafana | http://localhost:3000 |
| Tempo | http://localhost:3200 |
| Loki | http://localhost:3100 |
| Mimir | http://localhost:9009 |

---

## Running Tests

```bash
# Pure unit tests — no stack required
.venv/bin/python -m pytest tests/test_evaluator.py tests/test_app.py -v

# Infrastructure smoke tests — requires running stack
.venv/bin/python -m pytest tests/test_infrastructure.py -v

# All tests
.venv/bin/python -m pytest tests/ -v

# Shell smoke test against live stack
./test.sh
```

Test breakdown:

| File | Tests | Requires stack |
|---|---|---|
| `tests/test_evaluator.py` | 69 | No |
| `tests/test_app.py` | 48 | Postgres only |
| `tests/test_infrastructure.py` | 41 | Yes (all services) |

---

## Scripts Reference

### `launch_docker.sh` — Docker Compose stack

```bash
./launch_docker.sh              # show help
./launch_docker.sh --run        # full teardown + fresh deploy (wipes volumes)
./launch_docker.sh --run --keep-data   # teardown + redeploy, preserve volumes
./launch_docker.sh --teardown-only     # stop everything, no redeploy
./launch_docker.sh --help       # show help
```

### `launch_k8s.sh` — Kubernetes (Kind) stack

```bash
./launch_k8s.sh                 # show help
./launch_k8s.sh --run           # install deps, create cluster, build & deploy
./launch_k8s.sh --teardown-only # delete the Kind cluster entirely
./launch_k8s.sh --help          # show help
```

### Other scripts

| Script | Purpose |
|---|---|
| `./start.sh` | Start Docker Compose stack without teardown (faster restart) |
| `./test.sh` | Shell smoke test against the running Docker Compose stack |
| `./tests/run_all_tests.sh` | Python unit tests + K8s multi-language smoke tests |

---

## Project Structure

```
observability-demo/
├── app.py                          # Flask application (Python)
├── evaluator.py                    # Shunting-Yard expression evaluator
├── Dockerfile                      # Python app container image
├── requirements.txt                # Python dependencies
├── docker-compose.yaml             # Full Docker Compose stack (13 services)
│
├── java-app/                       # Spring Boot application
│   ├── src/
│   ├── pom.xml
│   └── Dockerfile
│
├── rust-app/                       # Axum application
│   ├── src/main.rs
│   ├── Cargo.toml
│   └── Dockerfile
│
├── canary/
│   ├── canary.py                   # Synthetic load generator
│   ├── Dockerfile
│   └── requirements.txt
│
├── config/
│   ├── otel-collector.yaml         # OTLP receiver → Tempo/Mimir/Loki routing
│   ├── tempo.yaml
│   ├── loki.yaml
│   ├── mimir.yaml
│   ├── prometheus.yaml
│   ├── grafana-datasources.yaml
│   ├── grafana-dashboard-provider.yaml
│   └── dashboards/
│       └── observability-demo-metrics.json
│
├── k8s/
│   ├── manifests/
│   │   ├── app.yaml                # App deployments + services
│   │   └── infrastructure.yaml    # Infra deployments + services
│   └── deploy-k8s.sh              # Low-level K8s deploy helper (called by launch_k8s.sh)
│
├── tests/
│   ├── conftest.py                 # OTel stubs, Postgres host resolution
│   ├── test_app.py                 # Flask route + DB unit tests (48 tests)
│   ├── test_evaluator.py           # Evaluator pipeline unit tests (69 tests)
│   ├── test_infrastructure.py      # End-to-end container smoke tests (41 tests)
│   ├── smoke_test.py               # Multi-language K8s smoke test
│   └── run_all_tests.sh            # Runs unit + K8s smoke tests
│
├── docs/
│   └── grafana-dashboard.png
│
├── launch_docker.sh                # Docker Compose lifecycle (--run / --teardown-only)
├── launch_k8s.sh                   # Kind Kubernetes lifecycle (--run / --teardown-only)
├── start.sh                        # Quick Docker Compose start (no teardown)
├── test.sh                         # Shell smoke test
└── pytest.ini                      # Pytest config
```

---

## Grafana Dashboard

Available at **http://localhost:3000** immediately after launch. No login required (anonymous admin).

![Grafana Dashboard](docs/grafana-dashboard.png)

| Row | What it shows |
|---|---|
| SLO / Health | Success rate, error budget burn, total RPS, P95 latency |
| Traffic, Latency & Errors (RED) | Request rate by route, latency percentiles, error rate |
| Saturation | Process RSS, system load average, CPU rate |
| Container Resources | Memory used vs limit, CPU cores |
| Synthetic Workload | Canary request rate and P95 latency by path |
| PostgreSQL | Connections, cache hit rate, commit/rollback, rows fetched |
| Logs (Loki) | Live log stream, errors-only filtered view |

---

## Canary Workload

The canary runs in its own container and exercises all endpoints at a uniform rate.

**Rate:** 24 TPS (Docker Compose) / 2 TPS (Kubernetes, tunable via `CANARY_TPS` env var)

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

---

## Expression Evaluator

`/eval` implements a mathematical expression parser from scratch using the **Shunting-Yard algorithm** (Dijkstra, 1961) — no `eval()`, no third-party parsing libraries.

**Pipeline:** `tokenise()` → `to_rpn()` → `evaluate_rpn()`

| Operator | Precedence | Associativity |
|---|---|---|
| `^` | 4 (highest) | Right |
| `*` `/` | 3 | Left |
| `+` `-` | 2 (lowest) | Left |

**Examples:**

```bash
curl "http://localhost:5000/eval?expr=3+4"
# {"expression":"3+4","result":7.0}

curl "http://localhost:5000/eval?expr=((3%2B4)*6%5E2%2F5)%2B1-7"
# {"expression":"((3+4)*6^2/5)+1-7","result":44.4}

curl -X POST http://localhost:5000/eval \
  -H 'Content-Type: application/json' \
  -d '{"expr": "(2+3)^2"}'
# {"expression":"(2+3)^2","result":25.0}
```
