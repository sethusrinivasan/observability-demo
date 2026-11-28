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

## 6. Root-Cause Analysis: Why Aurora Serverless v2 Keeps Waking Up

During continuous CloudWatch log auditing on `/aws/rds/cluster/reality-check-cost-tracker/postgresql`:

### Observed Connection Frequency (24-Hour Sample)
- **Total connection attempts**: 765
- **`cost_reader` user attempts**: **743 attempts** (97.1% of total)
- **`postgres` user attempts**: 16 attempts
- **`cost_collector` user attempts**: 4 attempts
- **`cdc_sync_worker` user attempts**: 2 attempts
- **Hourly cadence**: **26 to 36 connection attempts every single hour** (~1 attempt every 1.5 to 2 minutes).

### The Auto-Pause Cancellation Cycle
1. Aurora Serverless v2 auto-pause policy specifies `SecondsUntilAutoPause: 300` (5 minutes of complete inactivity).
2. `cost_reader` (originating from `reality-check-cost-web` in external workload `box_hackathon`) and canary healthchecks initiate connections on every poll / request.
3. Every TCP SYN / handshake resets Aurora's 300-second countdown timer.
4. Because incoming attempts occur every 90–120 seconds, the 300-second threshold is **never reached during normal hours**, locking Aurora at 0.50–3.00 ACU 24/7.
5. **Financial Impact**: Continuous 0.5 ACU compute generates ~$1.424/day ($42.71/month), representing **83.4% of total AWS account spend**.

---

## 7. Frugal Buffer-and-Batch Consolidation Specification

To achieve the account target of **< $0.70/day daily spend**, the database write and read paths must be completely decoupled from synchronous request lifecycles:

```
[ Monitoring Tools / Polyglot Lambdas / Canaries ]
                    │
                    │  Asynchronous OTLP HTTP / JSON (/v1/logs)
                    ▼
       [ Custom Telemetry Receiver ]
                    │  Snappy Columnar Compaction (10MB Chunks)
                    ▼
     [ Amazon S3 Parquet Lakehouse ]  <─── Zero Idle Compute Cost ($0.023/GB-mo)
         ├── /telemetry/traces/
         ├── /telemetry/logs/
         ├── /telemetry/metrics/
         └── /telemetry/errors/
                    │
                    ├──► [ Web Portals / DuckDB OLAP ] (In-Process, < 5ms, 0 DB Wakes)
                    │
                    └──► [ Daily 06:15 UTC Consolidated Sync ]
                                  │
                                  │  Single Batch Transaction (< 2s)
                                  ▼
                     [ Aurora PostgreSQL ]
                                  │
                                  ▼ (300s Idle Countdown)
                     [ Auto-Pauses to 0.000 ACU ]
```

### Key Architectural Tenets:
1. **Zero Synchronous DB Connections in Request Paths**:
   All microservices and canaries configure `MAX_DB_CONNECTIONS=0` and buffer telemetry directly to S3.
2. **In-Process Serverless OLAP for Read Queries**:
   The Canary Dashboard and Cloud Frugality portals use in-process DuckDB reading S3 Parquet directly. No database connections are opened for analytics, dashboards, or routine traffic checks.
3. **Consolidated Bulk Sync**:
   Once per day at 06:15 UTC, the sync worker executes a single DuckDB aggregation query and writes bulk records to `audit_log_hourly_summary`, `audit_anomalies`, and `audit_dimensions` in **one transaction**, allowing Aurora to return to 0.000 ACU within 5 minutes.

---

## 8. Failure Roll-Up & Frugal CloudWatch Alarms

To prevent observability and monitoring costs from driving up account spend:

### Frugal Metric Strategy (Embedded Metric Format)
- Applications log structured JSON to stdout containing EMF headers (`_aws.CloudWatchMetrics`).
- CloudWatch automatically extracts the metric `Errors` without requiring costly log metric filters or custom high-resolution publishing.

### Alarm Design & Cost Guardrails
- **Standard Resolution Only**: Use 60-second or 300-second periods (`Period = 60`, `EvaluationPeriods = 1`). High-resolution (10-second) alarms ($0.30/month) are strictly prohibited.
- **Rollup Metrics**: Alarms trigger on `Sum(Errors) > 0` at the service level rather than maintaining per-route alarms.
- **Detailed Forensic Storage**: Full error stack traces and payloads are routed to S3 Snappy Parquet (`telemetry/errors/`), where they can be queried on-demand via DuckDB without incurring CloudWatch Logs Insights query charges ($0.005/GB scanned).

---

## 9. Cost Impact & Target Verification

| Cost Component | Current Unoptimized Spend | Optimized Buffer-and-Batch Spend | Net Reduction |
| :--- | :--- | :--- | :--- |
| **Aurora Serverless v2** | $1.424 / day | **$0.012 / day** (10 min active/day) | -99.1% (-$1.412/day) |
| **CloudWatch Metrics & Alarms** | $0.170 / day | **$0.150 / day** (Standard resolution) | -11.8% (-$0.020/day) |
| **Cost Explorer API Calls** | $0.100 / day | **$0.020 / day** (Cached collectors) | -80.0% (-$0.080/day) |
| **S3 Storage & Requests** | $0.004 / day | **$0.006 / day** (Parquet lakehouse) | +$0.002/day |
| **Lambda Compute** | $0.008 / day | **$0.008 / day** | $0.000/day |
| **Total Daily Spend** | **$1.706 / day** | **$0.196 / day** | **-$1.510 / day (-88.5%)** |
| **Target Budget (< $0.70/day)**| **FAIL (Red / $1.71)** | **PASS (Green / $0.20)** | **Margin: $0.50/day below limit** |

---

## 10. Retention Policies & Lifecycle Enforcement

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
