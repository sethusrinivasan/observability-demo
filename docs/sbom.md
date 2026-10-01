# Software Bill of Materials (SBOM) & Architectural Inspirations

This document provides a comprehensive Software Bill of Materials (SBOM) and detailed architectural acknowledgments for the **Observability Demo** ecosystem, covering all 7 polyglot microservice applications, synthetic canary operations platform, mobile clients, and dependent stack infrastructure.

The machine-readable CycloneDX 1.5 standard SBOM is maintained at [`sbom/sbom-cyclonedx.json`](file:///home/home/github/observability-demo/sbom/sbom-cyclonedx.json).

---

## 📋 Component Inventory & Bill of Materials

| Component Name | Type | Version | Ecosystem / Runtime | License | PURL | Primary Function |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`observability-python-app`** | Application | 1.0.1 | Python 3.11 / Flask | MIT | `pkg:docker/observability-python-app@1.0.1` | Python REST microservice, recursive descent evaluator, OTel tracing |
| **`observability-java-app`** | Application | 1.0.1 | JDK 21 / Spring Boot 3.2 | Apache-2.0 | `pkg:maven/com.observability/observability-java-app@1.0.1` | Spring Boot enterprise microservice, Actuator metrics, JavaAgent OTel |
| **`observability-rust-app`** | Application | 1.0.1 | Rust 1.77 / Tokio / Axum | MIT | `pkg:cargo/observability-rust-app@1.0.1` | High-performance async microservice, memory safety, tracing OTel |
| **`observability-node-app`** | Application | 1.0.1 | Node.js 22 LTS / Express 4.19 | MIT | `pkg:npm/observability-node-app@1.0.1` | Event-driven microservice, non-blocking I/O, Node OTel SDK |
| **`observability-go-app`** | Application | 1.0.1 | Go 1.22 / net/http | BSD-3-Clause | `pkg:golang/github.com/observability-demo/go-app@1.0.1` | Concurrent Goroutine service, standard library minimalism, Go OTel SDK |
| **`observability-dotnet-app`** | Application | 1.0.1 | .NET 8.0 / C# Minimal API | MIT | `pkg:nuget/ObservabilityDotnetApp@1.0.1` | Modern Linux containerized C# service, Kestrel engine, .NET OTel SDK |
| **`observability-c-app`** | Application | 1.0.1 | POSIX C99 / pthreads / libpq | MIT | `pkg:generic/observability-c-app@1.0.1` | Pure C socket microservice, zero external framework, raw memory efficiency |
| **`observability-canary`** | Application | 1.0.1 | Python 3.11 / Canvas / Docker API | MIT | `pkg:docker/observability-canary@1.0.1` | Synthetic load generator, Chaos Fault Injection, Docker power manager, SQL data grid |
| **`canary-observability-mobile`** | Application | 1.0.0 | React Native 0.74 / Expo 51 | MIT | `pkg:npm/canary-observability-mobile@1.0.0` | Cross-platform mobile/desktop client for Android, iOS, Windows, and Web |
| **`postgres`** | Container | 16.1-alpine | PostgreSQL Relational DB | PostgreSQL | `pkg:docker/postgres@16.1-alpine` | Relational audit log persistence, transactional integrity, JSONB metadata |
| **`valkey`** | Container | 7.2.5 | C / RESP In-Memory Store | BSD-3-Clause | `pkg:docker/valkey/valkey@7.2.5` | High-throughput in-memory cache for canary counters and latency sums |
| **`otel-collector`** | Container | 0.88.0 | Go / OpenTelemetry Pipeline | Apache-2.0 | `pkg:docker/otel/opentelemetry-collector-contrib@0.88.0` | Telemetry router receiving OTLP gRPC/HTTP and dispatching to Tempo, Mimir, Loki |
| **`tempo`** | Container | 2.3.1 | Go / Grafana Tracing | AGPL-3.0 | `pkg:docker/grafana/tempo@2.3.1` | Distributed trace storage with correlation to logs and metrics |
| **`loki`** | Container | 2.9.2 | Go / Grafana Logs | AGPL-3.0 | `pkg:docker/grafana/loki@2.9.2` | Horizontally scalable log aggregator indexing stream labels via LogQL |
| **`mimir`** | Container | 2.11.0 | Go / Grafana Metrics | AGPL-3.0 | `pkg:docker/grafana/mimir@2.11.0` | Long-term high-availability Prometheus metric store with PromQL query engine |
| **`prometheus`** | Container | 2.48.0 | Go / CNCF Monitoring | Apache-2.0 | `pkg:docker/prom/prometheus@v2.48.0` | Pull-based telemetry scraper collecting host, container, and app metrics |
| **`grafana`** | Container | 10.2.2 | Go + TypeScript / Grafana | AGPL-3.0 | `pkg:docker/grafana/grafana@10.2.2` | Unified observability dashboards visualizing metrics, traces, logs, and database tables |
| **`redpanda`** | Container | 23.3.3 | C++20 / Seastar Engine | BSL-1.1 | `pkg:docker/redpandadata/redpanda@v23.3.3` | Kafka-compatible distributed event stream bus using Raft consensus |

---

## 🏛️ Implementation Details & Architectural Inspirations

### 1. Python Microservice (`observability-python-app`)
* **How It Was Implemented**:
  * Written in Python 3.11 with the lightweight **Flask** WSGI framework.
  * Features a custom, zero-dependency recursive-descent math tokenizer and AST parser handling operator precedence (`^`, `*`, `/`, `+`, `-`), unary operators, and parentheses without relying on unsafe `eval()`.
  * Integrates `psycopg2-binary` for transactional PostgreSQL audit logging into the `audit_logs` table.
  * Instrumented via `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-instrumentation-flask`, and `opentelemetry-exporter-otlp`, pushing metrics and spans via gRPC directly to `otel-collector:4317`.
* **Where the Inspiration Was From**:
  * **Armin Ronacher's Flask**: Clean, explicit routing and WSGI pipeline simplicity.
  * **Edsger W. Dijkstra's Shunting-yard Algorithm & Vaughan Pratt's Top-Down Operator Precedence**: Formed the mathematical foundation for safe arithmetic expression parsing.
  * **Google SRE Probing Patterns**: Inspired the `/selftest` and `/version` actuator endpoints.

---

### 2. Java Enterprise Microservice (`observability-java-app`)
* **How It Was Implemented**:
  * Built using **Spring Boot 3.2.0** on **Eclipse Temurin OpenJDK 21** Alpine Linux.
  * Employs Spring MVC controllers, HikariCP connection pooling, and Spring Boot Actuator for `/actuator/health` and `/actuator/prometheus` metrics.
  * Utilizes the **OpenTelemetry JavaAgent 1.32.0** for automatic bytecode-level instrumentation of HTTP requests, JDBC database calls, and thread execution contexts.
  * Implements mathematical expression parsing using an object-oriented recursive-descent syntax tree.
* **Where the Inspiration Was From**:
  * **Spring Boot PetClinic & Enterprise Java Standards**: Industry standard reference patterns for dependency injection, component scanning, and decoupled data access layers.
  * **Netflix OSS (Hystrix & Resilience4j)**: Fault tolerance paradigms and thread isolation concepts.
  * **Oracle JVM Specification**: Utilizing Java 21's latest Garbage Collection optimizations on containerized environments.

---

### 3. Rust Async Microservice (`observability-rust-app`)
* **How It Was Implemented**:
  * Constructed using **Rust 1.77** with **Tokio** multi-threaded async runtime and **Axum 0.7** web framework.
  * Implements zero-allocation parsing with Rust pattern matching, `Result<T, E>` algebraic types, and strict borrow-checker safety guarantees.
  * Leverages `sqlx` with asynchronous connection pooling for non-blocking PostgreSQL audit inserts.
  * Employs the `tracing` and `tracing-opentelemetry` subscriber crates with OTLP gRPC telemetry streaming.
* **Where the Inspiration Was From**:
  * **Carl Lerche & Tokio Team**: Work-stealing async scheduler design and asynchronous I/O abstractions.
  * **Rust Zero-Cost Abstraction Philosophy**: Guarantees maximum performance without memory safety hazards or garbage collection pauses.

---

### 4. Node.js Microservice (`observability-node-app`)
* **How It Was Implemented**:
  * Built in **Node.js 22 LTS** utilizing **Express 4.19** and native async/await syntax.
  * Integrates the official `@opentelemetry/sdk-node` with gRPC trace and metric exporters, paired with `@opentelemetry/instrumentation-express` and `@opentelemetry/instrumentation-pg`.
  * Implements recursive mathematical AST evaluation in JavaScript with rigorous syntax verification and zero dependencies.
* **Where the Inspiration Was From**:
  * **Ryan Dahl's Node.js Event Loop**: Non-blocking single-threaded event loop architecture handling concurrent I/O.
  * **TJ Holowaychuk's Connect / Express Middleware Pipeline**: Linear request-response lifecycle interceptors.

---

### 5. Go Microservice (`observability-go-app`)
* **How It Was Implemented**:
  * Written in **Go 1.22** following idiomatic standard library conventions with `net/http` and `http.ServeMux`.
  * Uses Goroutines and Go channels for concurrent task scheduling and `pgx/v5` connection pool for high-throughput database interactions.
  * Integrates `go.opentelemetry.io/otel` with automated span propagation and Prometheus metric registration.
* **Where the Inspiration Was From**:
  * **Rob Pike & Ken Thompson**: Communicating Sequential Processes (CSP) concurrency model.
  * **The Go Standard Library (`net/http`)**: "Clear is better than clever" philosophy and clean interface composition.

---

### 6. C# .NET Microservice (`observability-dotnet-app`)
* **How It Was Implemented**:
  * Built on **.NET 8.0 SDK** with **ASP.NET Core Minimal APIs** hosted on Kestrel in an Alpine Linux container.
  * Employs `Npgsql` for native ADO.NET connection pooling and parameterized SQL queries.
  * Uses the official `OpenTelemetry.Extensions.Hosting` SDK with OTLP gRPC exporter.
* **Where the Inspiration Was From**:
  * **David Fowler & ASP.NET Core Architecture Team**: Ultra-lightweight Minimal APIs and high-performance pipeline design on Linux.
  * **Modern Cross-Platform .NET**: Microsoft's open-source pivot delivering high throughput and low memory footprint in microservice containers.

---

### 7. POSIX C Microservice (`observability-c-app`)
* **How It Was Implemented**:
  * Implemented in pure **C99** with **POSIX Berkeley sockets**, multi-threading via `pthread`, and raw HTTP/1.1 parsing without external web servers.
  * Includes a zero-dependency lexical scanner and recursive-descent math expression parser written entirely from scratch in C.
  * Interacts with PostgreSQL directly using the official `libpq` C client library.
* **Where the Inspiration Was From**:
  * **W. Richard Stevens' 'UNIX Network Programming'**: Foundational networking architecture, socket multiplexing, and robust network programming.
  * **Dennis Ritchie & Brian Kernighan**: The enduring power of straightforward, transparent C systems programming.

---

### 8. Canary Load Generator, Fault Injector & Operations Center (`observability-canary`)
* **How It Was Implemented**:
  * Implemented in Python 3.11 with Python's standard library `HTTPServer`, multi-threading, and non-blocking sockets.
  * **Container Power Management**: Connects directly to the Docker Engine daemon over the Unix domain socket `/var/run/docker.sock` using standard library `http.client.HTTPConnection`. Shuts down, starts, and restarts unique language containers on demand, automatically adjusting synthetic traffic loops to prevent false failure alarms.
  * **SSMS & pgAdmin Style Data Grid**: Implemented from first principles in vanilla JavaScript and CSS with row numbering (`#`), column sorting, execution timing, JSON/CSV exports, and curated saved queries querying PostgreSQL `audit_logs` and catalog tables.
  * **Collapsible UI Controls**: Encapsulates load generators, chaos crash stations, TPS override sliders, and container power controls in a collapsible drawer that is **collapsed by default**, maximizing monitoring screen real estate.
  * **Dependencies Health Telemetry**: Automatically probes all 9 backend infrastructure components (PostgreSQL, Valkey, OTel Collector, Tempo, Loki, Mimir, Prometheus, Grafana, Redpanda), computing availability, roundtrip latency, and resource capacity.
  * **Offline QR Code Generator**: Generates SVG QR codes directly via Python's `qrcode` library and client-side helpers, allowing instant pairing with mobile devices.
* **Where the Inspiration Was From**:
  * **Netflix Chaos Monkey & Simian Army / AWS Fault Injection Simulator (FIS)**: Color-coded, language-specific fault injection and targeted process crash drills.
  * **Microsoft SQL Server Management Studio (SSMS), pgAdmin 4, and JetBrains DataGrip**: First-principles tabular query results grid, sortable column headers, and execution metrics.
  * **Google Site Reliability Engineering (SRE) Handbooks**: Synthetic canary probing, error budgeting, and active availability tracking.

---

### 9. Cross-Platform Mobile Client (`canary-observability-mobile`)
* **How It Was Implemented**:
  * Built using **React Native 0.74** and **Expo 51** with responsive Flexbox design.
  * Supports simultaneous deployment to **Android**, **iOS**, **Windows (React Native for Windows)**, and **Web**.
  * Provides remote monitoring of KPIs, polyglot container power toggles, chaos trigger buttons, and SQL query execution from physical mobile devices.
* **Where the Inspiration Was From**:
  * **Modern DevOps On-Call Mobile Apps (Datadog, PagerDuty, AWS Console Mobile)**: Providing on-call engineers with immediate visibility and mitigation controls from anywhere.
