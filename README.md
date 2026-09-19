# Observability Demo

> **Note:** This is an experimental demo project.

An experimental observability playground demonstrating end-to-end telemetry across Python (Flask), Java (Spring Boot), and Rust (Axum) microservices. It utilizes the OpenTelemetry Collector to pipeline distributed traces, custom metrics, and structured logs into the Grafana LGTM stack (Loki, Grafana, Tempo, Mimir), complete with a PostgreSQL audit layer and synthetic load generation.

Run via Docker Compose or Kubernetes (Kind).

## Quick Deploy

Deploy this stack to cloud:

<a href="https://console.aws.amazon.com/cloudformation/home?#/stacks/new?stackName=observability-demo&templateURL=https://raw.githubusercontent.com/sethusrinivasan/observability-demo/main/docker-compose.yaml"><img src="https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png" height="32" alt="Deploy to AWS"></a>
<a href="https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fsethusrinivasan%2Fobservability-demo%2Fmain%2Fdocker-compose.yaml"><img src="https://aka.ms/deploytoazurebutton" height="32" alt="Deploy to Azure"></a>
<a href="https://console.cloud.google.com/cloudshell/editor?cloudshell_git_repo=https://github.com/sethusrinivasan/observability-demo"><img src="https://gstatic.com/cloudssh/images/open-btn.svg" height="32" alt="Deploy to Google Cloud"></a>
<a href="https://render.com/deploy?repo=https://github.com/sethusrinivasan/observability-demo"><img src="https://render.com/images/deploy-to-render-button.svg" height="32" alt="Deploy to Render"></a>
<a href="https://railway.app/new?template=https://github.com/sethusrinivasan/observability-demo"><img src="https://railway.app/button.svg" height="32" alt="Deploy on Railway"></a>

## Quick Start

### Docker Compose

```bash
./launch_docker.sh --run          # full teardown + deploy
./launch_docker.sh --run --keep-data   # keep DB data
./launch_docker.sh --teardown-only     # cleanup
```

After ~30s:
- **Python app**: http://localhost:5000
- **Java app**: http://localhost:8080
- **Rust app**: http://localhost:8083
- **Grafana**: http://localhost:3000 (auto-provisioned dashboard)
- **Prometheus**: http://localhost:9090

### Kubernetes (Kind)

```bash
./launch_k8s.sh --run              # install deps, create cluster, deploy
./launch_k8s.sh --teardown-only    # delete cluster
```

Forwarding set up automatically. Access same as Docker Compose.

## What This Does

**Apps have 5 endpoints each:**
- `/` — greeting page
- `/compute/<n>` — Fibonacci(n) with 10% error rate (test error handling)
- `/auditlog` — log system metrics to PostgreSQL
- `/auditlog/stats` — response time stats (p50/p90/p95/p99)
- `/eval?expr=...` — math expression evaluator (no external libs, Shunting-Yard algorithm)

**Observability:**
- Traces via OTLP → Tempo
- Metrics via OTLP → Mimir (with Prometheus scrape from postgres-exporter)
- Logs via OTLP + structured logging → Loki
- PostgreSQL version widget in Grafana dashboard

**Canary:** Continuous synthetic load (24 TPS docker / 2 TPS k8s)


## Stack Overview

| Component | Port | Purpose | Version |
|-----------|------|---------|---------|
| [Python Flask](./app.py) | 5000 | WSGI app: Fibonacci compute, Postgres audit log + pooling, math expression parser (Shunting-Yard) | 1.0.1 |
| [Java Spring Boot](./java-app) | 8080 | Spring MVC app: Fibonacci compute, JDBC audit log, recursive descent expression parser | 1.0.1 |
| [Rust Axum](./rust-app) | 8083 | Async web server: Fibonacci compute, sqlx async Postgres, tokenizer-based expression evaluator | 1.0.1 |
| Grafana | 3000 | Dashboards + data sources | [12.4.2](https://hub.docker.com/r/grafana/grafana) |
| Prometheus | 9090 | Metrics scraper | [3.11.2](https://hub.docker.com/r/prom/prometheus) |
| Mimir | 9009 | Long-term metrics storage | [2.17.9](https://hub.docker.com/r/grafana/mimir) |
| Tempo | 3200 | Trace storage | [2.10.3](https://hub.docker.com/r/grafana/tempo) |
| Loki | 3100 | Log storage | [3.7.1](https://hub.docker.com/r/grafana/loki) |
| OpenTelemetry Collector | 4317 | OTLP ingestion point | [0.149.0](https://hub.docker.com/r/otel/opentelemetry-collector-contrib) |
| PostgreSQL | 5432 | Audit log persistence | [16](https://hub.docker.com/_/postgres) |
| postgres-exporter | 9187 | PG metrics (version, connections, etc.) | [0.19.1](https://hub.docker.com/r/bitnami/postgres-exporter) |
| Valkey | 6379 | Redis fork for storing custom metrics | [latest](https://hub.docker.com/r/valkey/valkey) |

## Testing

See the [consolidated test results](tests/test_results/results.md) for recent self-test executions.

```bash
# Unit tests (no stack required)
python -m pytest tests/test_evaluator.py tests/test_app.py -v

# Infrastructure tests (requires running stack)
python -m pytest tests/test_infrastructure.py -v

# All tests
python -m pytest tests/ -v

# Shell smoke test (docker compose)
./test.sh
```

## Project Layout

```
.
├── app.py / java-app/ / rust-app/      Apps + Dockerfile
├── canary/                               Synthetic load generator
├── config/                               YAML configs (otel, tempo, loki, mimir, prometheus)
│   └── dashboards/
│       └── observability-demo-metrics.json
├── k8s/                                  Kubernetes manifests + deploy script
├── tests/                                Test suite
├── docs/                                 Diagrams, screenshots
├── docker-compose.yaml                   Full stack definition
├── launch_docker.sh / launch_k8s.sh      Entry points for deployment
└── pytest.ini / requirements.txt         Python config
```

## Expression Evaluator

Parses math expressions (no `eval()`, no external libs). Implements Shunting-Yard algorithm:

```bash
# Python
curl "http://localhost:5000/eval?expr=2+3*4"
# Java
curl "http://localhost:8080/eval?expr=2+3*4"
# Rust
curl "http://localhost:8083/eval?expr=2+3*4"
```

Supports: `+`, `-`, `*`, `/`, `^` (exponent), parentheses, floating point.

## PostgreSQL Additions

- **Version widget** in Grafana dashboard (requires `PG_EXPORTER_DISABLE_SETTINGS_METRICS=false`)
- **Audit table** auto-created on app startup
- **Connection pooling** in all app services
- **postgres-exporter** scrapes version, connections, cache hit rate, etc.

---
*Note: AI was used to learn, build, test, and deploy this project.*
