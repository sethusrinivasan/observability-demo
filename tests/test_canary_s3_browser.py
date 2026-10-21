"""
test_canary_s3_browser.py — Integration tests for Canary S3 Browser UI Endpoints
and Adobe S3Mock storage integration.
"""

import requests
import pytest

CANARY_URL = "http://localhost:8085"

def test_s3_buckets_endpoint():
    res = requests.get(f"{CANARY_URL}/api/s3/buckets", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "telemetry-lake" in data["buckets"]
    assert "Apache 2.0" in data["license"]

def test_s3_objects_root_listing():
    res = requests.get(f"{CANARY_URL}/api/s3/objects?bucket=telemetry-lake&prefix=", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["bucket"] == "telemetry-lake"
    folder_names = [f["name"] for f in data.get("folders", [])]
    assert "telemetry/" in folder_names

def test_s3_parquet_preview():
    # Find first parquet file in lake
    res = requests.get(f"{CANARY_URL}/api/s3/objects?bucket=telemetry-lake&prefix=telemetry/year=2026/month=10/day=20/hour=22/", timeout=5)
    assert res.status_code == 200
    data = res.json()
    files = data.get("files", [])
    if files:
        key = files[0]["key"]
        prev_res = requests.get(f"{CANARY_URL}/api/s3/preview?bucket=telemetry-lake&key={key}", timeout=5)
        assert prev_res.status_code == 200
        prev_data = prev_res.json()
        assert prev_data["status"] == "success"
        assert prev_data["type"] == "parquet"
        assert "event_id" in prev_data["columns"]
        assert "telemetry_type" in prev_data["columns"]
        assert len(prev_data["rows"]) > 0

def test_s3_crud_operations():
    # 1. Create bucket
    b_res = requests.post(f"{CANARY_URL}/api/s3/buckets", json={"bucket": "pytest-crud-bkt"}, timeout=5)
    assert b_res.status_code == 200
    assert b_res.json()["status"] == "success"

    # 2. Upload file
    u_res = requests.post(
        f"{CANARY_URL}/api/s3/upload?bucket=pytest-crud-bkt&key=test/note.txt",
        data=b"pytest S3 content payload",
        timeout=5
    )
    assert u_res.status_code == 200
    assert u_res.json()["status"] == "success"

    # 3. Download file
    d_res = requests.get(f"{CANARY_URL}/api/s3/download?bucket=pytest-crud-bkt&key=test/note.txt", timeout=5)
    assert d_res.status_code == 200
    assert d_res.content == b"pytest S3 content payload"

    # 4. Delete file
    del_res = requests.post(
        f"{CANARY_URL}/api/s3/delete",
        json={"bucket": "pytest-crud-bkt", "key": "test/note.txt"},
        timeout=5
    )
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "success"


def test_s3_foreign_bucket_access_denied_recovery():
    """Verify querying an unauthorized/foreign bucket returns a clean error and suggested_bucket."""
    res = requests.get(
        f"{CANARY_URL}/api/s3/objects?bucket=cdk-hnb659fds-assets-596090776030-us-east-2",
        timeout=5,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "error"
    assert "suggested_bucket" in data
    assert data["suggested_bucket"] == "telemetry-lake"
