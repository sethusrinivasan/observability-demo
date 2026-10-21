"""
telemetry-receiver/receiver.py — High-Throughput OpenTelemetry Receiver
with In-Memory Ring Buffer, Local Columnar Compaction (10MB Threshold),
Nanosecond Granularity, and Asynchronous S3 Parquet Exporter.
"""

import os
import time
import json
import uuid
import gzip
import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify
import boto3
from botocore.client import Config
import pyarrow as pa
import pyarrow.parquet as pq

# Protobuf parsers from opentelemetry-proto
try:
    from opentelemetry.proto.trace.v1.trace_pb2 import TracesData
    from opentelemetry.proto.metrics.v1.metrics_pb2 import MetricsData
    from opentelemetry.proto.logs.v1.logs_pb2 import LogsData
    HAS_PROTO = True
except ImportError:
    HAS_PROTO = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s'
)
logger = logging.getLogger("telemetry-receiver")

app = Flask(__name__)

# Configuration
BUFFER_DIR = Path(os.getenv("BUFFER_DIR", "/tmp/telemetry_buffer"))
MAX_PARQUET_FILE_SIZE_BYTES = int(os.getenv("MAX_PARQUET_FILE_SIZE_BYTES", str(10 * 1024 * 1024)))  # 10 MB
DRAIN_BATCH_SIZE = int(os.getenv("DRAIN_BATCH_SIZE", "1000"))
DRAIN_INTERVAL_SECONDS = float(os.getenv("DRAIN_INTERVAL_SECONDS", "0.2"))  # 200 ms batch compaction
MAX_RETENTION_TIME_SECONDS = float(os.getenv("MAX_RETENTION_TIME_SECONDS", "300"))  # 5 min flush safeguard

S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://s3-storage:9090")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "telemetry-lake")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "test")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "test")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

BUFFER_DIR.mkdir(parents=True, exist_ok=True)
COMPACTING_PATH = BUFFER_DIR / "compacting_current.parquet"

# Columnar Schema with Nanosecond Precision
PARQUET_SCHEMA = pa.schema([
    ("event_id", pa.string()),
    ("timestamp_ns", pa.int64()),
    ("timestamp_iso", pa.string()),
    ("telemetry_type", pa.string()),
    ("service_name", pa.string()),
    ("event_name", pa.string()),
    ("attributes", pa.string()),
    ("raw_payload", pa.string()),
])

# S3 Thread Pool for Asynchronous Uploads
s3_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="S3AsyncUploader")

def get_s3_client():
    kwargs = {"region_name": AWS_REGION}
    is_aws = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("AWS_SESSION_TOKEN") or (S3_ENDPOINT_URL and "amazonaws.com" in S3_ENDPOINT_URL))
    if is_aws:
        if S3_ENDPOINT_URL and "amazonaws.com" not in S3_ENDPOINT_URL and "s3-storage" not in S3_ENDPOINT_URL:
            kwargs["endpoint_url"] = S3_ENDPOINT_URL
    else:
        kwargs["endpoint_url"] = S3_ENDPOINT_URL
        kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
        kwargs["config"] = Config(signature_version="s3v4", s3={"addressing_style": "path"})
    return boto3.client("s3", **kwargs)

def ensure_bucket_exists():
    if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        logger.info("Running in AWS Lambda - S3 bucket '%s' managed by Terraform.", S3_BUCKET_NAME)
        return
    s3 = get_s3_client()
    try:
        s3.head_bucket(Bucket=S3_BUCKET_NAME)
    except Exception:
        try:
            s3.create_bucket(Bucket=S3_BUCKET_NAME)
            logger.info("Created S3 bucket: %s at %s", S3_BUCKET_NAME, S3_ENDPOINT_URL)
        except Exception as e:
            logger.warning("Could not auto-create bucket '%s': %s", S3_BUCKET_NAME, e)

# High-Throughput Compaction & Ingestion Engine
class HighThroughputCompactor:
    def __init__(self):
        self.queue = deque()
        self.queue_lock = threading.Lock()
        self.writer = None
        self.writer_lock = threading.Lock()
        self.current_rows_in_file = 0
        self.file_opened_time = None

        self.total_ingested = 0
        self.total_compacted_rows = 0
        self.total_parquet_files_uploaded = 0
        self.total_bytes_uploaded = 0
        self.last_upload_time = None

    def enqueue_events(self, events):
        """Ultra-fast lock-protected append to in-memory deque."""
        if not events:
            return 0
        with self.queue_lock:
            self.queue.extend(events)
            self.total_ingested += len(events)
            return len(self.queue)

    def drain_queue(self, max_items):
        """Drain a batch of events from in-memory queue."""
        batch = []
        with self.queue_lock:
            while self.queue and len(batch) < max_items:
                batch.append(self.queue.popleft())
        return batch

    def _get_or_create_writer(self):
        """Ensure active ParquetWriter is open for local compaction."""
        if self.writer is None:
            self.writer = pq.ParquetWriter(
                str(COMPACTING_PATH),
                PARQUET_SCHEMA,
                compression="snappy",
                use_dictionary=True
            )
            self.current_rows_in_file = 0
            self.file_opened_time = time.time()
            logger.info("Initialized local compaction file: %s", COMPACTING_PATH)
        return self.writer

    def compact_batch(self, batch):
        """Append a batch of rows as a row-group into local Parquet file."""
        if not batch:
            return

        with self.writer_lock:
            writer = self._get_or_create_writer()

            data_dict = {
                "event_id": [r["event_id"] for r in batch],
                "timestamp_ns": [r["timestamp_ns"] for r in batch],
                "timestamp_iso": [r["timestamp_iso"] for r in batch],
                "telemetry_type": [r["telemetry_type"] for r in batch],
                "service_name": [r["service_name"] for r in batch],
                "event_name": [r["event_name"] for r in batch],
                "attributes": [r["attributes"] for r in batch],
                "raw_payload": [r["raw_payload"] for r in batch],
            }

            table = pa.Table.from_pydict(data_dict, schema=PARQUET_SCHEMA)
            writer.write_table(table)
            self.current_rows_in_file += len(batch)
            self.total_compacted_rows += len(batch)

            # Check if file exceeded the 10 MB size limit
            current_size = COMPACTING_PATH.stat().st_size if COMPACTING_PATH.exists() else 0
            if current_size >= MAX_PARQUET_FILE_SIZE_BYTES:
                logger.info(
                    "Local Parquet file reached %.2f MB (threshold: %.2f MB) with %d rows — closing for S3 upload",
                    current_size / (1024 * 1024), MAX_PARQUET_FILE_SIZE_BYTES / (1024 * 1024), self.current_rows_in_file
                )
                self._rotate_and_upload_async()

    def _rotate_and_upload_async(self):
        """Close active writer, rename to unique upload file, and submit async upload task."""
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception as e:
                logger.warning("Error closing writer: %s", e)
            self.writer = None

        if not COMPACTING_PATH.exists():
            return

        rows = self.current_rows_in_file
        self.current_rows_in_file = 0
        self.file_opened_time = None

        now = datetime.now(timezone.utc)
        ts_ns = time.time_ns()
        rotate_id = f"telemetry_{ts_ns}_{uuid.uuid4().hex[:8]}.parquet"
        rotate_path = BUFFER_DIR / rotate_id

        COMPACTING_PATH.rename(rotate_path)
        file_size = rotate_path.stat().st_size

        s3_key = (
            f"telemetry/year={now.year:04d}/month={now.month:02d}/day={now.day:02d}/"
            f"hour={now.hour:02d}/{rotate_id}"
        )

        logger.info(
            "Submitting upload to S3: s3://%s/%s (Size: %.1f KB, Rows: %d)",
            S3_BUCKET_NAME, s3_key, file_size / 1024.0, rows
        )
        if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
            self._async_upload_task(rotate_path, s3_key, file_size, rows)
        else:
            s3_executor.submit(self._async_upload_task, rotate_path, s3_key, file_size, rows)

    def _async_upload_task(self, local_path, s3_key, file_size, rows):
        """Background worker thread: uploads Parquet file to S3 and purges from disk."""
        t0 = time.time()
        try:
            s3 = get_s3_client()
            s3.upload_file(str(local_path), S3_BUCKET_NAME, s3_key)
            duration = time.time() - t0

            self.total_parquet_files_uploaded += 1
            self.total_bytes_uploaded += file_size
            self.last_upload_time = datetime.now(timezone.utc).isoformat()

            logger.info(
                "ASYNC S3 UPLOAD COMPLETE: s3://%s/%s (%.1f KB, %d rows in %.2fs)",
                S3_BUCKET_NAME, s3_key, file_size / 1024.0, rows, duration
            )
        except Exception as e:
            logger.error("Async S3 upload failed for %s: %s", local_path, e, exc_info=True)
        finally:
            local_path.unlink(missing_ok=True)

    def force_flush(self):
        """Manual flush: drain remaining queue, compact, and close active file for upload."""
        # 1. Drain all queued events
        drained_count = 0
        while True:
            batch = self.drain_queue(DRAIN_BATCH_SIZE)
            if not batch:
                break
            drained_count += len(batch)
            self.compact_batch(batch)

        # 2. Rotate and upload if any rows exist in the file
        with self.writer_lock:
            rows_flushed = self.current_rows_in_file
            if rows_flushed > 0 or (COMPACTING_PATH.exists() and COMPACTING_PATH.stat().st_size > 0):
                self._rotate_and_upload_async()

        # 3. Ensure any pending or leftover parquet files in buffer dir are pushed to S3
        for p in list(BUFFER_DIR.glob("telemetry_*.parquet")):
            try:
                now = datetime.now(timezone.utc)
                s3_key = f"telemetry/year={now.year:04d}/month={now.month:02d}/day={now.day:02d}/hour={now.hour:02d}/{p.name}"
                self._async_upload_task(p, s3_key, p.stat().st_size, 1)
            except Exception as e:
                logger.warning("Leftover parquet upload failed: %s", e)

        return rows_flushed or drained_count or self.total_compacted_rows

    def background_loop(self):
        """Continuous compactor daemon."""
        while True:
            time.sleep(DRAIN_INTERVAL_SECONDS)
            try:
                # 1. Drain and compact
                batch = self.drain_queue(DRAIN_BATCH_SIZE)
                if batch:
                    self.compact_batch(batch)

                # 2. Time-based flush safeguard (5 minutes) if rows are waiting
                with self.writer_lock:
                    if (
                        self.writer is not None
                        and self.current_rows_in_file > 0
                        and self.file_opened_time is not None
                        and (time.time() - self.file_opened_time) >= MAX_RETENTION_TIME_SECONDS
                    ):
                        logger.info(
                            "Time retention limit reached (%.1fs >= %.1fs) with %d rows — closing for S3 upload",
                            time.time() - self.file_opened_time, MAX_RETENTION_TIME_SECONDS, self.current_rows_in_file
                        )
                        self._rotate_and_upload_async()
            except Exception as e:
                logger.error("Error in compactor background loop: %s", e)

compactor = HighThroughputCompactor()

# Request Decoders with Nanosecond Precision
def extract_body():
    raw = request.get_data()
    encoding = request.headers.get("Content-Encoding", "").lower()
    if "gzip" in encoding:
        raw = gzip.decompress(raw)
    return raw

def parse_telemetry_request(telemetry_type):
    raw = extract_body()
    content_type = request.headers.get("Content-Type", "").lower()
    now_ns = time.time_ns()
    now_iso = datetime.now(timezone.utc).isoformat()
    events = []

    # Protobuf parsing
    if HAS_PROTO and ("protobuf" in content_type or "octet-stream" in content_type):
        try:
            if telemetry_type == "trace":
                td = TracesData()
                td.ParseFromString(raw)
                for rs in td.resource_spans:
                    svc_name = "unknown"
                    for attr in rs.resource.attributes:
                        if attr.key == "service.name":
                            svc_name = attr.value.string_value
                    for ss in rs.scope_spans:
                        for span in ss.spans:
                            span_time_ns = span.start_time_unix_nano or span.end_time_unix_nano or now_ns
                            attrs = {a.key: str(a.value.string_value or a.value.int_value or "") for a in span.attributes}
                            events.append({
                                "event_id": str(uuid.uuid4()),
                                "timestamp_ns": span_time_ns,
                                "timestamp_iso": datetime.fromtimestamp(span_time_ns / 1e9, tz=timezone.utc).isoformat(),
                                "telemetry_type": "trace",
                                "service_name": svc_name,
                                "event_name": span.name,
                                "attributes": json.dumps(attrs),
                                "raw_payload": json.dumps({"trace_id": span.trace_id.hex(), "span_id": span.span_id.hex(), "name": span.name})
                            })
                return events

            elif telemetry_type == "metric":
                md = MetricsData()
                md.ParseFromString(raw)
                for rm in md.resource_metrics:
                    svc_name = "unknown"
                    for attr in rm.resource.attributes:
                        if attr.key == "service.name":
                            svc_name = attr.value.string_value
                    for sm in rm.scope_metrics:
                        for m in sm.metrics:
                            events.append({
                                "event_id": str(uuid.uuid4()),
                                "timestamp_ns": now_ns,
                                "timestamp_iso": now_iso,
                                "telemetry_type": "metric",
                                "service_name": svc_name,
                                "event_name": m.name,
                                "attributes": json.dumps({"description": m.description, "unit": m.unit}),
                                "raw_payload": json.dumps({"name": m.name, "description": m.description})
                            })
                return events

            elif telemetry_type == "log":
                ld = LogsData()
                ld.ParseFromString(raw)
                for rl in ld.resource_logs:
                    svc_name = "unknown"
                    for attr in rl.resource.attributes:
                        if attr.key == "service.name":
                            svc_name = attr.value.string_value
                    for sl in rl.scope_logs:
                        for log in sl.log_records:
                            log_time_ns = log.time_unix_nano or now_ns
                            attrs = {a.key: str(a.value.string_value or "") for a in log.attributes}
                            events.append({
                                "event_id": str(uuid.uuid4()),
                                "timestamp_ns": log_time_ns,
                                "timestamp_iso": datetime.fromtimestamp(log_time_ns / 1e9, tz=timezone.utc).isoformat(),
                                "telemetry_type": "log",
                                "service_name": svc_name,
                                "event_name": str(log.body.string_value or "log"),
                                "attributes": json.dumps(attrs),
                                "raw_payload": json.dumps({"body": str(log.body.string_value)})
                            })
                return events

        except Exception as e:
            logger.warning("Protobuf decode failed for %s: %s; falling back", telemetry_type, e)

    # JSON or Fallback parsing
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except Exception:
        payload = {"raw": raw.decode("utf-8", errors="replace")}

    svc_name = "unknown"
    if isinstance(payload, dict):
        for key in ["resourceSpans", "resourceMetrics", "resourceLogs"]:
            if key in payload and isinstance(payload[key], list) and len(payload[key]) > 0:
                attrs = payload[key][0].get("resource", {}).get("attributes", [])
                for a in attrs:
                    if a.get("key") == "service.name":
                        svc_name = a.get("value", {}).get("stringValue", "unknown")

    events.append({
        "event_id": str(uuid.uuid4()),
        "timestamp_ns": now_ns,
        "timestamp_iso": now_iso,
        "telemetry_type": telemetry_type,
        "service_name": svc_name,
        "event_name": f"OTLP {telemetry_type.upper()}",
        "attributes": json.dumps({"content_type": content_type}),
        "raw_payload": json.dumps(payload)
    })
    return events


# API Endpoints
@app.route("/v1/traces", methods=["POST"])
def ingest_traces():
    events = parse_telemetry_request("trace")
    q_len = compactor.enqueue_events(events)
    return jsonify({"status": "accepted", "queued_events": q_len}), 200

@app.route("/v1/metrics", methods=["POST"])
def ingest_metrics():
    events = parse_telemetry_request("metric")
    q_len = compactor.enqueue_events(events)
    return jsonify({"status": "accepted", "queued_events": q_len}), 200

@app.route("/v1/logs", methods=["POST"])
def ingest_logs():
    events = parse_telemetry_request("log")
    q_len = compactor.enqueue_events(events)
    return jsonify({"status": "accepted", "queued_events": q_len}), 200

@app.route("/v1/telemetry", methods=["POST"])
def ingest_generic():
    events = parse_telemetry_request("generic")
    q_len = compactor.enqueue_events(events)
    return jsonify({"status": "accepted", "queued_events": q_len}), 200

@app.route("/flush", methods=["POST"])
def manual_flush():
    flushed_rows = compactor.force_flush()
    return jsonify({
        "status": "flushed" if flushed_rows > 0 else "no_events_to_flush",
        "events_flushed": flushed_rows,
        "total_compacted_rows": compactor.total_compacted_rows,
        "total_parquet_files_uploaded": compactor.total_parquet_files_uploaded
    }), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "queued_events": len(compactor.queue),
        "current_compacting_rows": compactor.current_rows_in_file,
        "total_parquet_files_uploaded": compactor.total_parquet_files_uploaded
    }), 200

@app.route("/status", methods=["GET"])
def status():
    current_size = COMPACTING_PATH.stat().st_size if COMPACTING_PATH.exists() else 0
    return jsonify({
        "service": "custom-telemetry-receiver",
        "granularity": "nanoseconds (int64)",
        "buffer_count": len(compactor.queue),
        "queue_length": len(compactor.queue),
        "flush_count_threshold": 1000,
        "flush_time_threshold_seconds": 300,
        "local_compacting_rows": compactor.current_rows_in_file,
        "local_compacting_size_bytes": current_size,
        "max_parquet_file_size_bytes": MAX_PARQUET_FILE_SIZE_BYTES,
        "max_parquet_file_size_mb": MAX_PARQUET_FILE_SIZE_BYTES / (1024 * 1024),
        "compaction_percent": round((current_size / MAX_PARQUET_FILE_SIZE_BYTES) * 100, 2),
        "total_ingested_events": compactor.total_ingested,
        "total_compacted_rows": compactor.total_compacted_rows,
        "total_parquet_files_uploaded": compactor.total_parquet_files_uploaded,
        "total_bytes_uploaded": compactor.total_bytes_uploaded,
        "last_upload_time": compactor.last_upload_time,
        "s3_bucket": S3_BUCKET_NAME,
        "s3_endpoint": S3_ENDPOINT_URL
    }), 200

# Start Compactor Daemon Thread
compactor_thread = threading.Thread(target=compactor.background_loop, daemon=True, name="CompactorDaemon")
compactor_thread.start()

# Initialize bucket in background
threading.Thread(target=ensure_bucket_exists, daemon=True, name="BucketInit").start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "4318"))
    logger.info("Starting Telemetry Receiver on port %d...", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
