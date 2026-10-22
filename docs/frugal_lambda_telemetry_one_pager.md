# Executive One-Pager: Ultra-Frugal Serverless Polyglot Observability

**Objective**: Host the polyglot microservices, synthetic canary, custom telemetry collector, and S3 Parquet analytical lake on **AWS Lambda Firecracker MicroVMs** (**128 MB Graviton `arm64`**) sharing an existing **Aurora Serverless DB** with maximum frugality (**$0.00 on Free Tier / < $4.00/mo at scale**).

---

## 1. System Architecture

```mermaid
flowchart LR
    subgraph Clients ["Ingress & Triggers"]
        Browser["Canary UI"]
        Cron["EventBridge\n(5-min Flush / 1-min Canary)"]
    end

    subgraph LambdaMicroVMs ["Lambda MicroVMs (128 MB Graviton arm64 - Function URLs)"]
        Canary["Canary Dashboard\n+ In-Process DuckDB (256MB)"]
        Apps["7 Polyglot Apps\n(Rust, Go, C, Node, Python, .NET, Java)"]
        Collector["Custom OTel Collector Lambda\n(Reserved Concurrency = 1)"]
    end

    subgraph DataStorage ["Data & Storage Tier"]
        Aurora[("Shared Aurora Serverless\n(Max 1-2 pooled conns)")]
        S3[("Amazon S3 (telemetry-lake)\n*.parquet (>= 10MB Compaction)")]
    end

    Browser & Cron --> Canary
    Canary -->|Function URLs (Free)| Apps
    Apps -->|Pooled Conns| Aurora
    Apps -->|OTLP HTTP| Collector
    Cron -->|POST /flush| Collector
    Collector -->|PutObject (>= 10MB)| S3
    Canary -->|DuckDB httpfs Parquet Scan| S3
```

---

## 2. Core Architectural Pillars

| Layer | Technology | Frugal Design Choice | Why It Saves Money |
| :--- | :--- | :--- | :--- |
| **Ingress** | **Lambda Function URLs** | Direct HTTPS with built-in CORS | **$0.00** — Replaces API Gateway ($1.00–$3.50/M) & ALB ($16+/mo). |
| **Compute** | **128 MB Graviton (`arm64`)** | AWS Lambda Web Adapter (LWA) | **$0.00** — Uses AWS's permanent **400k GB-sec / 3.2M free seconds/mo**. 20% cheaper than x86. |
| **Database** | **Existing Aurora Serverless** | 1–2 pooled conns per MicroVM | **$0.00** — Reuses existing cluster; prevents connection exhaustion & ACU scaling. |
| **Collector** | **Lambda MicroVM + `/tmp`** | Reserved Concurrency = 1 | **$0.00** — Scales to zero; routes traffic to same warm MicroVM to compact up to 10 MB. |
| **S3 Lake** | **Amazon S3 Standard** | 10 MB Parquet Snappy Compaction | **$0.01/mo** — Slashes S3 `PUT` requests by **99.7%** ($0.005/1k requests). |
| **Analytics** | **In-Process DuckDB** | Direct S3 zero-copy `httpfs` scan | **$0.00** — Runs in Canary Lambda; zero Athena ($5/TB) or Redshift ($180+/mo) fees. |

---

## 3. Microservice Runtime Sizing Matrix

| Service | Language & Framework | Binary / Packaging | Memory | Cold Start | Adaptation Needed |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **C** | POSIX C99 Sockets | Musl static binary | **128 MB** | ~8 ms | Alpine `arm64` build + LWA |
| **Rust** | Axum 0.7 + Tokio | Musl static binary | **128 MB** | ~15 ms | `aarch64-unknown-linux-musl` + LWA |
| **Go** | `net/http` standard library | Pure Go static binary | **128 MB** | ~18 ms | `CGO_ENABLED=0 GOARCH=arm64` + LWA |
| **Node.js** | Express on Node 20 | Distroless Node 20 | **128 MB** | ~110 ms | Node 20 `arm64` base + LWA |
| **Python** | Flask 3.1 + WSGI | Single-worker WSGI | **128 MB** | ~140 ms | Single-worker (strip Gunicorn forks) + LWA |
| **.NET** | ASP.NET Core (.NET 8) | Native AOT compiled | **128 MB** | ~25 ms | `<PublishAot>true</PublishAot>` targeting `linux-arm64` |
| **Java** | Spring Boot 3.2 | GraalVM Native Image | **128 MB** | ~65 ms | GraalVM native profile *(or 256MB if JVM)* |
| **Canary UI** | Flask + DuckDB OLAP | Python 3.12 + `httpfs` | **256 MB** | ~180 ms | Needs 256MB for DuckDB in-memory buffers |
| **Collector** | Python OTel Receiver | `/tmp` ParquetWriter | **128 MB** | ~120 ms | Reserved Concurrency = 1 + EventBridge cron |

---

## 4. Frugality Cost Model: Monthly Bill Breakdown

> [!WARNING]
> **AWS Cost Advisory**: All figures below represent an illustrative engineering estimate and benchmark. Deploying to AWS provisions live infrastructure and incurs real billable costs ($$). Users must independently evaluate their own account requirements, billing tiers, and traffic patterns.

| Line Item | Resource / Metric | Unit Pricing | Estimated Monthly Cost |
| :--- | :--- | :--- | :--- |
| **Lambda Invocations** | 5,000,000 requests | 1M free, then $0.20/1M | **$0.80** |
| **Compute Duration** | 250,000 GB-seconds | First 400,000 GB-sec free | **$0.00** (Free Tier) |
| **HTTPS Ingress** | Lambda Function URLs | Included free with Lambda | **$0.00** |
| **Aurora Serverless** | Shared existing cluster | Shared usage (no new ACUs) | **$0.00** |
| **S3 Parquet Storage** | 10 GB telemetry lake | $0.023 / GB / month | **$0.23** |
| **S3 API Operations** | ~1,500 PUTs (10MB files) | $0.005 / 1,000 PUTs | **$0.01** |
| **DuckDB Analytics** | In-process in Canary | Transient Lambda compute | **$0.00** |
| **Total Monthly Bill** | **Zero Idle Waste** | **All-Inclusive** | **~$1.04 to $4.00 / month** *(or $0.00 on Free Tier)* |

---

## 5. Deployment Roadmap (5 Simple Phases)

```
[Phase 1: Build] -----> [Phase 2: S3 & IAM] -----> [Phase 3: Deploy Functions] -----> [Phase 4: Collector & Cron] -----> [Phase 5: Validate]
Multi-arch arm64        Create telemetry-lake      Deploy 8 Web Functions            Deploy Collector (Conc=1)          Run DuckDB queries
images with LWA         + Lifecycle policies       with Function URLs                + EventBridge 5-min ping           in Canary UI
```

1. **Phase 1 (Container Images)**: Add LWA binary (`COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4-aarch64 /lambda-adapter /opt/extensions/lambda-adapter`) to each Dockerfile and push `linux/arm64` images to AWS ECR.
2. **Phase 2 (S3 Lake & Security)**: Provision `s3://telemetry-lake` with SSE-S3 encryption and 30-day Infrequent Access lifecycle transition. Create IAM roles for S3 Read/Write.
3. **Phase 3 (Deploy Web Functions)**: Deploy 8 Lambda functions on `arm64` at 128 MB (Canary at 256 MB) with Function URLs (`AuthType: NONE`).
4. **Phase 4 (Collector Lambda & EventBridge)**: Deploy the Collector Lambda with `ReservedConcurrency = 1` and attach an EventBridge cron rule (`rate(5 minutes)`) to invoke `POST /flush`.
5. **Phase 5 (End-to-End Validation)**: Trigger canary load pulses, inspect files via Canary S3 Browser, and run analytical queries in the DuckDB Query Editor.
