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

After the script reports services ready:

- **Python app**: http://localhost:5000
- **Java app**: http://localhost:8080
- **Rust app**: http://localhost:8083
- **Grafana**: http://localhost:3000
- **Prometheus**: http://localhost:9090
- **Tempo**: http://localhost:3200
- **Loki**: http://localhost:3100
- **Mimir**: http://localhost:9009

Grafana login is **admin** / **admin**. Open http://localhost:3000/login and use those fields. Anonymous access is off.

On Kubernetes the Rust app is http://localhost:8081. Docker publishes Rust on 8083 because 8081 is the Grafana image renderer.

### Kubernetes (Kind)

```bash
./launch_k8s.sh --run              # install deps, create cluster, deploy
./launch_k8s.sh --teardown-only    # delete cluster
```

Forwarding set up automatically. Access same as Docker Compose.

## What This Does

**Each app exposes:**
- `/` — greeting page
- `/version` — name, language, and version
- `/selftest` — in-process Fibonacci and evaluator checks
- `/compute/<n>` — Fibonacci(n). Values above 25 are rejected
- `/auditlog` — write a row to PostgreSQL
- `/auditlog/stats` — audit-log stats
- `/eval?expr=...` — math expression evaluator (no `eval()`, no external parser libs)

**Observability:**
- Traces via OTLP → Tempo
- Metrics via OTLP → Mimir, plus Prometheus scrapes of postgres-exporter and redis-exporter
- Logs via OTLP → Loki from the Python, Java, and Rust apps
- PostgreSQL version widget in the Grafana dashboard
- Valkey (Redis-compatible) for canary metrics, scraped by redis-exporter

**Canary:** Continuous synthetic load at 6 TPS in Docker Compose and on Kubernetes.


## Stack Overview

| Component | Port | Purpose | Version |
|-----------|------|---------|---------|
| [Python Flask](./app.py) | 5000 | WSGI app: Fibonacci compute, Postgres audit log + pooling, math expression parser (Shunting-Yard) | 1.0.1 |
| [Java Spring Boot](./java-app) | 8080 | Spring MVC app: Fibonacci compute, JDBC audit log, recursive descent expression parser, Actuator health/info | 1.0.1 |
| [Rust Axum](./rust-app) | 8083 (Docker), 8081 (Kind) | Async web server: Fibonacci compute, sqlx async Postgres, tokenizer-based expression evaluator, OTLP logs | 1.0.1 |
| Grafana | 3000 | Dashboards + data sources. Login `admin` / `admin` | `latest` |
| Grafana image renderer | 8081 | PNG render sidecar for Grafana | `latest` |
| Prometheus | 9090 | Metrics scraper | `latest` |
| Mimir | 9009 | Long-term metrics storage | `latest` |
| Tempo | 3200 | Trace storage | `latest` |
| Loki | 3100 | Log storage | `latest` |
| OpenTelemetry Collector | 4317 | OTLP ingestion point | `latest` |
| PostgreSQL | 5432 | Audit log persistence | 18.6 |
| postgres-exporter | 9187 | PG metrics (version, connections, etc.) | `latest` |
| Valkey | 6379 | Redis-compatible store for canary metrics | `latest` |
| redis-exporter | 9121 | Prometheus metrics for Valkey | `latest` |
| Redpanda | 9092 | Kafka API used by Tempo | `latest` |

Compose pins these images to `latest` except PostgreSQL, which is pinned to 18.6, the current stable release. PostgreSQL 19 is still in beta.

## Testing

See the [consolidated test results](tests/test_results/results.md) for recent self-test executions.

```bash
# Unit tests (no stack required)
python -m pytest tests/test_evaluator.py tests/test_app.py -v

# Infrastructure tests (requires running stack)
python -m pytest tests/test_infrastructure.py -v

# All tests
python -m pytest tests/ -v

# Shell smoke test of the Python app (docker compose must already be up)
./test.sh
```

## Project Layout

```
.
├── app.py / java-app/ / rust-app/      Apps + Dockerfile
├── canary/                               Synthetic load generator
├── config/                               YAML configs (otel, tempo, loki, mimir, prometheus, grafana)
│   └── dashboards/
│       ├── observability-demo-metrics.json
│       └── valkey-metrics.json
├── k8s/                                  Kubernetes manifests + deploy script
├── tests/                                Test suite
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

## Spring Boot only

The Java service is the only app that exposes [Spring Boot Actuator](https://docs.spring.io/spring-boot/reference/actuator/index.html). Python and Rust keep the shared demo routes.

```bash
curl http://localhost:8080/actuator/health
curl http://localhost:8080/actuator/health/liveness
curl http://localhost:8080/actuator/health/readiness
curl http://localhost:8080/actuator/info
```

Liveness ignores the database. Readiness includes the database check and a custom `demo` health indicator. Docker Compose and the Kind manifest probe those paths.

## URLs to try

Dashboards:

- http://localhost:3000/
- http://localhost:3000/d/observability-demo-metrics/observability-demo
- http://localhost:3000/d/valkey-metrics/valkey-canary-metrics

In a browser, write `+` in the eval query as `%2B`.

| | Python | Java | Rust (Docker) |
|---|---|---|---|
| Home | http://localhost:5000/ | http://localhost:8080/ | http://localhost:8083/ |
| Version | http://localhost:5000/version | http://localhost:8080/version | http://localhost:8083/version |
| Self-test | http://localhost:5000/selftest | http://localhost:8080/selftest | http://localhost:8083/selftest |
| Fibonacci | http://localhost:5000/compute/10 | http://localhost:8080/compute/10 | http://localhost:8083/compute/10 |
| Audit log | http://localhost:5000/auditlog | http://localhost:8080/auditlog | http://localhost:8083/auditlog |
| Audit stats | http://localhost:5000/auditlog/stats | http://localhost:8080/auditlog/stats | http://localhost:8083/auditlog/stats |
| Eval `2+3*4` | http://localhost:5000/eval?expr=2%2B3*4 | http://localhost:8080/eval?expr=2%2B3*4 | http://localhost:8083/eval?expr=2%2B3*4 |

Java only:

- http://localhost:8080/actuator/health
- http://localhost:8080/actuator/health/liveness
- http://localhost:8080/actuator/health/readiness
- http://localhost:8080/actuator/info

Backends:

- http://localhost:9090/ and http://localhost:9090/targets
- http://localhost:3200/ready
- http://localhost:3100/ready
- http://localhost:9009/ready
- http://localhost:9187/metrics
- http://localhost:9121/metrics

## PostgreSQL Additions

- **Version widget** in Grafana dashboard (requires `PG_EXPORTER_DISABLE_SETTINGS_METRICS=false`)
- **Audit table** auto-created on app startup
- **Connection pooling** in all app services
- **postgres-exporter** scrapes version, connections, cache hit rate, etc. The Bitnami image needs `GODEBUG=fips140=off`, or the version widget stays empty.

---
*Note: AI was used to learn, build, test, and deploy this project.*
