# Two-Tier Lakehouse Architecture: Frugal Audit Telemetry & Postgres Summarization

## 1. Executive Summary

This document specifies the architectural solution for capturing canary audit logs cost-effectively while retaining rich relational insights in PostgreSQL.

| Dimension | Legacy Direct-to-DB Anti-Pattern | Two-Tier Lakehouse Architecture |
| :--- | :--- | :--- |
| **Write Path** | 10 MicroVMs open synchronous DB connections every 60s | MicroVMs emit OTLP logs to Telemetry Receiver & S3 Parquet Lake |
| **Storage Engine** | Aurora PostgreSQL table (`audit_logs`) | Amazon S3 / Local Object Storage (Snappy Columnar Parquet) |
| **Storage Cost** | High ($0.10/GB-month + Aurora IOPS + table bloat) | Dirt Cheap (**$0.023/GB-month**, < $0.0001/month for audit logs) |
| **Aurora Compute** | **0.5 ACU continuous 24/7** ($1.502/day = $45.06/month) | **0.000 ACU ~23.5 hours/day** (~$0.05 - $0.07/day compute) |
| **Relational Insights** | Raw unstructured row bloat | Vectorized rollups (`audit_log_hourly_summary`, `audit_anomalies`, `audit_dimensions`) |
| **Query Latency** | Full table scans over 100k+ rows | Indexed summary lookups (< 5 ms) or serverless DuckDB OLAP |
| **Local Docker Parity**| Required live DB for every request | Identical local Docker compose with local S3Mock / Parquet buffer |

---

## 2. Root Cause Analysis: The Auto-Pause Conflict

1. **Aurora Serverless v2 Auto-Pause Mechanics:**
   - The shared Aurora PostgreSQL cluster (`reality-check-cost-tracker`) is configured with `MinCapacity: 0.0` and `SecondsUntilAutoPause: 300` (5 minutes).
   - Compute drops to **0.000 ACU** ($0.00 compute charge) only after 300 contiguous seconds of complete TCP/idle silence.

2. **The Canary Pulse Contention:**
   - EventBridge rule `observability-canary-1min-pulse` triggers synthetic workloads across 10 polyglot microservices every 1 minute (`rate(1 minute)` = 1,440 executions/day).
   - In the legacy code, every call to `/auditlog` caused the application to synchronously connect to PostgreSQL on port 5432 and execute `INSERT INTO audit_logs ...`.
   - This constantly reset the 300-second idle countdown, preventing Aurora from ever pausing and racking up ~$1.44/day in unnecessary compute fees.
   - Furthermore, serverless Lambda cold starts caused frequent SSL connection drops (`could not accept SSL connection: EOF detected`) and connection timeouts.

---

## 3. The Two-Tier Architecture

```
[ EventBridge 1-min Pulse ]
          │
          ▼
[ 10 Polyglot MicroVMs ]  (Python, Go, Node, Java, .NET, Rust, PHP, C, Swift)
          │  Asynchronous OTLP HTTP / Protobuf (/v1/logs)
          ▼
[ Custom Telemetry Receiver ] (In-memory ring buffer & local 10MB columnar compactor)
          │  Asynchronous Parquet Upload
          ▼
[ Amazon S3 Telemetry Lake ] (s3://telemetry-lake/telemetry/**/*.parquet)
  • Columnar Snappy compression (10x smaller)
  • Storage cost: $0.023/GB/month (~$0.0001/month)
  • ZERO compute cost when idle
          │
          │  Daily Scheduled Batch at 06:15 UTC (or On-Demand via POST /api/audit/sync-postgres)
          ▼
[ Vectorized DuckDB Engine ] (In-process inside Canary Lambda or Docker container)
  • Reads S3 Parquet directly via httpfs
  • Vectorized aggregation & anomaly detection
          │
          │  Single batch upsert in ONE transaction (< 2 seconds)
          ▼
[ Aurora PostgreSQL (Relational Serving) ]
  ├── audit_log_hourly_summary   (Traffic volume, success/error distribution, p95 latency)
  ├── audit_dimensions           (MD5-deduplicated dimensions, hit counts, first/last seen)
  ├── audit_anomalies            (4xx/5xx failures, latency anomalies with error payloads)
  └── audit_logs                 (Curated rolling sample of recent records)
          │
   (Transaction ends)
          ▼
[ Aurora Serverless v2 Auto-Pauses to 0.000 ACU ]
```

---

## 4. PostgreSQL Relational Schema

PostgreSQL is leveraged for its greatest strengths: relational integrity, B-Tree indexing, rapid dashboard queries, and structured aggregations.

### A. Hourly Rollups (`audit_log_hourly_summary`)
```sql
CREATE TABLE IF NOT EXISTS audit_log_hourly_summary (
    hour_bucket TIMESTAMPTZ NOT NULL,
    service_name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'unknown',
    total_calls BIGINT NOT NULL DEFAULT 0,
    success_calls BIGINT NOT NULL DEFAULT 0,
    error_calls BIGINT NOT NULL DEFAULT 0,
    avg_duration_sec DOUBLE PRECISION,
    p95_duration_sec DOUBLE PRECISION,
    max_duration_sec DOUBLE PRECISION,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hour_bucket, service_name, endpoint)
);
CREATE INDEX IF NOT EXISTS idx_audit_hourly_svc_time
    ON audit_log_hourly_summary (service_name, hour_bucket DESC);
```

### B. Anomaly Insights (`audit_anomalies`)
```sql
CREATE TABLE IF NOT EXISTS audit_anomalies (
    id SERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    service_name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    duration_sec DOUBLE PRECISION,
    error_details JSONB,
    raw_payload JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_anomalies_time
    ON audit_anomalies (occurred_at DESC);
```

---

## 5. Maintenance Window Alignment

To maximize frugality, all batch tasks that touch Aurora are aligned within a single contiguous 35-minute maintenance window every morning:

1. **06:00 UTC:** `RealityCheckCostTracker` collects daily Cost Explorer metrics (Aurora runs for 2 min).
2. **06:15 UTC:** `observability-audit-lake-daily-sync` executes vectorized DuckDB summarization from S3 Parquet into Aurora (Aurora runs for 30s).
3. **06:30 UTC:** `RealityCheckCdc` replicates changed data snapshots (Aurora runs for 30s).
4. **06:35 UTC:** All batch jobs complete; Aurora's 300-second idle countdown starts.
5. **06:40 UTC:** Aurora enters **0.000 ACU** auto-pause and stays asleep for the remaining ~23.5 hours of the day.

---

## 6. Retention Policies & Lifecycle Enforcement

### CloudWatch Logs
- All 11 Lambda log groups (`/aws/lambda/observability-*`) are configured with an explicit **3-day retention** policy (`retention_in_days = 3`).
- Prevents indefinite log accumulation (~80 MB accumulated historically) and eliminates stale storage fees.

### S3 Object Storage
- The foundational telemetry lake bucket (`observability-demo-telemetry-lake-596090776030`) enforces:
  - Transition to **Standard-IA** after 30 days.
  - Transition to **Glacier Flexible Retrieval** after 90 days.
  - Permanent expiration after **365 days**.
  - **Abort incomplete multipart uploads** after 1 day.

---

## 7. Local Docker Parity

The solution runs identically in the local developer environment:
- `docker-compose.yaml` provisions `postgres:16`, `s3-storage` (Adobe S3Mock), `custom-telemetry-receiver`, and `canary`.
- Microservices route OTLP logs to `http://custom-telemetry-receiver:4318`.
- The telemetry receiver uploads to `http://s3-storage:9090` (or local volume `/tmp/telemetry_buffer`).
- DuckDB queries local files or S3Mock seamlessly, and ingests summaries into local `postgres:5432`.
- Verification can be executed anytime via `pytest tests/test_audit_ingest.py`.
