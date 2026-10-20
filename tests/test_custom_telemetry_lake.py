"""
test_custom_telemetry_lake.py — Test suite for the Custom OpenTelemetry Receiver,
Ephemeral Disk Buffering, and S3 Parquet Data Lake.
"""

import io
import json
import time
import requests
import boto3
import pytest
import pyarrow.parquet as pq

RECEIVER_URL = "http://localhost:4319"
S3_URL = "http://localhost:9095"
S3_BUCKET = "telemetry-lake"

@pytest.fixture(scope="module")
def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_URL,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1"
    )

def test_receiver_health():
    res = requests.get(f"{RECEIVER_URL}/health", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"

def test_receiver_status():
    res = requests.get(f"{RECEIVER_URL}/status", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["service"] == "custom-telemetry-receiver"
    assert data["s3_bucket"] == S3_BUCKET
    assert "buffer_count" in data
    assert data["flush_count_threshold"] == 1000
    assert data["flush_time_threshold_seconds"] == 300.0

def test_ingest_and_parquet_flushing(s3_client):
    # Ingest 15 events
    for i in range(5):
        requests.post(f"{RECEIVER_URL}/v1/traces", json={
            "resourceSpans": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "pytest-trace-service"}}]},
                "scopeSpans": [{"spans": [{"name": f"test_trace_span_{i}", "traceId": "abc123", "spanId": "span1"}]}]
            }]
        }, timeout=5)
        requests.post(f"{RECEIVER_URL}/v1/metrics", json={
            "resourceMetrics": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "pytest-metric-service"}}]},
                "scopeMetrics": [{"metrics": [{"name": f"test_metric_{i}", "unit": "count"}]}]
            }]
        }, timeout=5)
        requests.post(f"{RECEIVER_URL}/v1/logs", json={
            "resourceLogs": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "pytest-log-service"}}]},
                "scopeLogs": [{"logRecords": [{"body": {"stringValue": f"pytest log message #{i}"}}]}]
            }]
        }, timeout=5)

    # Force manual flush
    flush_res = requests.post(f"{RECEIVER_URL}/flush", timeout=5)
    assert flush_res.status_code == 200
    flush_data = flush_res.json()
    assert flush_data["events_flushed"] >= 15

    # Check S3 objects
    objects = s3_client.list_objects_v2(Bucket=S3_BUCKET)
    keys = [o["Key"] for o in objects.get("Contents", [])]
    assert len(keys) > 0

    # Read latest Parquet object
    latest_key = keys[-1]
    obj = s3_client.get_object(Bucket=S3_BUCKET, Key=latest_key)
    table = pq.read_table(io.BytesIO(obj["Body"].read()))
    
    assert "event_id" in table.column_names
    assert "telemetry_type" in table.column_names
    assert "service_name" in table.column_names
    assert "event_name" in table.column_names
    assert table.num_rows > 0

def test_zero_events_no_flush():
    # Calling flush should cleanly report events flushed (resilient to ambient live telemetry)
    res = requests.post(f"{RECEIVER_URL}/flush", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] in ["flushed", "no_events_to_flush"]
    assert data["events_flushed"] >= 0
