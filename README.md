# Observability Demo

<div align="center">

[![Built with Google Antigravity](https://img.shields.io/badge/Built%20with-Google%20Antigravity-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://deepmind.google/technologies/)
[![AI Assisted: Gemini](https://img.shields.io/badge/AI%20Assisted-Gemini%20CLI-8E75C4?style=for-the-badge&logo=google-gemini&logoColor=white)](https://deepmind.google/technologies/gemini/)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Java](https://img.shields.io/badge/Java-Spring%20Boot-ED8B00?style=for-the-badge&logo=springboot&logoColor=white)](https://spring.io/projects/spring-boot)
[![Rust](https://img.shields.io/badge/Rust-Axum-000000?style=for-the-badge&logo=rust&logoColor=white)](https://github.com/tokio-rs/axum)
[![Node.js](https://img.shields.io/badge/Node.js-Express-339933?style=for-the-badge&logo=node.js&logoColor=white)](https://nodejs.org/)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-Traces%20%7C%20Metrics%20%7C%20Logs-F5A800?style=for-the-badge&logo=opentelemetry&logoColor=white)](https://opentelemetry.io/)
[![Grafana LGTM](https://img.shields.io/badge/Grafana-LGTM%20Stack-F46800?style=for-the-badge&logo=grafana&logoColor=white)](https://grafana.com/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)

</div>

---

An end-to-end polyglot observability playground demonstrating production-grade telemetry across **Python (Flask)**, **Java (Spring Boot)**, **Rust (Axum)**, and **Node.js (Express)** microservices. Telemetry (traces, metrics, logs) is collected via OpenTelemetry Collector and exported to the **Grafana LGTM stack** (Loki, Grafana, Tempo, Mimir), alongside a PostgreSQL audit layer and an interactive synthetic **Canary Traffic & Fault Injection Dashboard**.

Deployable locally via **Docker Compose** or **Kubernetes (Kind)**.

---

## Architecture & Dashboards

### Canary Load & Telemetry Dashboard (`http://localhost:8085`)
Interactive control center featuring real-time & historical trend graphs across 6 time ranges, language-color-coded fault injection testing, and dynamic load pacing.

![Canary Load & Telemetry Dashboard](docs/canary-dashboard.png)

### Grafana LGTM Observability Dashboard (`http://localhost:3000`)
Consolidated distributed tracing (Tempo), metrics (Mimir/Prometheus), logs (Loki), and infrastructure health.

![Grafana Dashboard](docs/grafana-dashboard.png)

---

## Features

* **Multi-Language Telemetry Parity**: Identical endpoint semantics, custom OTel histograms/counters, and tracing context across Python, Java, Rust, and Node.js.
* **Canary Load & Fault Injection Station**:
  * **Historical Trend Graphs**: Availability (%), Error Rate (err/s), Response Latency (ms), and Throughput (TPS) queried directly from Mimir.
  * **6 Time Horizons**: `Past 5 Min`, `Past 30 Min`, `Past Hour`, `Past 6 Hours`, `Past Day`, and `Past Month`.
  * **Fault Injection Testing (Chaos Drills)**: Induce `Error Spikes`, `High Latency (+1000ms)`, `Service Outage (Drops)`, or `Intermittent Errors` with custom event tagging.
  * **Language-Specific Event Color Coding**: Fault events are visually tagged and overlaid on trend charts using distinct language color palettes (🟠 Java, 🔵 Python, 🟣 Rust, 🟢 Node, 🔴 All).
  * **Dynamic Load Controller**: Bump up Canary TPS from baseline (6 TPS) to higher rates (e.g., 12, 24, 48 TPS) with automatic expiration and reset controls.
  * **Dual View Architecture**: Toggle between `Both Views`, `Trends Only`, and `Current Raw Numbers`.
* **Distributed Tracing**: OTLP traces streamed from microservices through OpenTelemetry Collector into Grafana Tempo.
* **Metrics & Aggregations**: Standardized RED metrics (Rate, Errors, Duration) stored in Grafana Mimir.
* **Structured Logging**: Unified OTLP log pipeline shipping structured JSON logs to Grafana Loki.
* **PostgreSQL Audit Log**: Thread-safe connection pooling and automatic schema initialization recording audit events.
* **Mathematical Expression Evaluator**: Clean zero-dependency AST parser implementing the Shunting-Yard / recursive-descent algorithm across all four languages.

---

## Quick Start

### Docker Compose

```bash
./launch_docker.sh --run               # Full teardown, build & deploy
./launch_docker.sh --run --keep-data   # Deploy while preserving database volumes
./launch_docker.sh --teardown-only     # Stop and clean up containers
```

Once deployment completes, access the services:

| Component | URL | Credentials / Notes |
|-----------|-----|---------------------|
| **Canary Dashboard** | [http://localhost:8085](http://localhost:8085) | Trend graphs, fault injection & TPS controls |
| **Grafana** | [http://localhost:3000](http://localhost:3000) | `admin` / `admin` (Anonymous login disabled) |
| **Python App** | [http://localhost:5000](http://localhost:5000) | Flask + Gunicorn WSGI |
| **Java App** | [http://localhost:8080](http://localhost:8080) | Spring Boot 3 + Spring MVC |
| **Rust App** | [http://localhost:8083](http://localhost:8083) | Tokio + Axum async runtime (8081 on Kind) |
| **Node.js App** | [http://localhost:8084](http://localhost:8084) | Node.js 20 + Express (30007 on Kind) |
| **Prometheus** | [http://localhost:9090](http://localhost:9090) | Target scrapers & metrics |
| **Grafana Mimir** | [http://localhost:9009](http://localhost:9009) | Long-term PromQL metrics engine |
| **Grafana Tempo** | [http://localhost:3200](http://localhost:3200) | Distributed trace backend |
| **Grafana Loki** | [http://localhost:3100](http://localhost:3100) | Log aggregation engine |

### Kubernetes (Kind)

```bash
./launch_k8s.sh --run              # Spin up Kind cluster, build images, and apply manifests
./launch_k8s.sh --teardown-only    # Delete Kind cluster
```

*Port-forwarding is established automatically by the launch script.*

---

## Stack Components

| Component | Port | Purpose | Version |
|---|---|---|---|
| [Python Service](./app.py) | `5000` | Flask WSGI service, DB connection pool, Shunting-Yard evaluator | `1.0.1` |
| [Java Service](./java-app) | `8080` | Spring Boot service, Spring Data JDBC, Actuator health probes | `1.0.1` |
| [Rust Service](./rust-app) | `8083` (Docker) / `8081` (Kind) | Axum asynchronous service, SQLx Postgres pool, tokenizer evaluator | `1.0.1` |
| [Node.js Service](./node-app) | `8084` (Docker) / `30007` (Kind) | Express asynchronous service, pg connection pool, recursive-descent evaluator | `1.0.1` |
| [Canary Dashboard](./canary) | `8085` | Synthetic traffic generator, historical trend graphs, chaos fault injection | `1.0.1` |
| Grafana | `3000` | Provisioned dashboards and data source integration (`admin`/`admin`) | `latest` |
| Grafana Image Renderer | `8081` | Headless Chromium sidecar for automated PNG panel rendering | `latest` |
| Prometheus | `9090` | Exporter scraper and metrics buffer | `latest` |
| Grafana Mimir | `9009` | Distributed long-term metrics storage | `latest` |
| Grafana Tempo | `3200` | Distributed tracing backend (backed by Redpanda) | `latest` |
| Grafana Loki | `3100` | Log aggregation backend | `latest` |
| OTel Collector | `4317` (gRPC) / `4318` (HTTP) | Telemetry ingestion and routing pipeline | `latest` |
| PostgreSQL | `5432` | Audit log database with persistence | `18.6` |
| Postgres Exporter | `9187` | Database runtime metrics (connections, locks, buffers) | `latest` |
| Valkey | `6379` | High-performance Redis fork for canary counters and event logs | `latest` |
| Redis Exporter | `9121` | Prometheus scraper for Valkey cache telemetry | `latest` |
| Redpanda | `9092` | Kafka-compatible distributed event log for Tempo traces | `latest` |

---

## API Endpoints Matrix

Every microservice implements identical API contracts:

| Route | Method | Description | Sample Query |
|---|---|---|---|
| `/` | `GET` | Service index & environment info | `curl http://localhost:5000/` |
| `/version` | `GET` | Service metadata, language, and version | `curl http://localhost:8080/version` |
| `/selftest` | `GET` | In-process internal unit verification | `curl http://localhost:8083/selftest` |
| `/compute/<n>` | `GET` | Fibonacci calculation (rejects `n > 25`) | `curl http://localhost:5000/compute/10` |
| `/auditlog` | `GET`, `POST` | Insert/query persistent audit log record | `curl -X POST http://localhost:8080/auditlog -d "source=cli"` |
| `/auditlog/stats` | `GET` | Aggregated audit log row statistics | `curl http://localhost:8083/auditlog/stats` |
| `/eval?expr=...` | `GET`, `POST` | Custom zero-eval arithmetic expression parser | `curl "http://localhost:5000/eval?expr=2%2B3*4"` |

*Note: For the `/eval` endpoint, operators like `+` must be URL-encoded (`%2B`).*

---

## Testing & Verification

Comprehensive multi-layer test suite covering unit tests, evaluator logic, infrastructure health, and end-to-end integration:

```bash
# Multi-language integration smoke tests
python3 tests/smoke_test.py --docker

# Run unit and infrastructure test suite
pytest tests/ -v

# Quick shell smoke test against Flask endpoints
./test.sh
```

---

## AI Tooling & Attribution

This project was developed, debugged, and enhanced with the assistance of modern agentic AI systems:

* **[Google Antigravity](https://deepmind.google/technologies/)**: Autonomous engineering pair programmer used for end-to-end telemetry architecture, root-cause diagnosis of microservice anomalies, chaos engineering design, and full-stack dashboard implementation.
* **[Gemini CLI](https://deepmind.google/technologies/gemini/)**: Advanced agentic coding workflows for polyglot code synchronization across Python, Java, Rust, and Node.js, PromQL query optimization, and test automation.

---

## License

This project is licensed under the Apache License 2.0.
