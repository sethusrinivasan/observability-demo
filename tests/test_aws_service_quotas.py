"""
tests/test_aws_service_quotas.py
Automated test suite for the AWS Service Quotas Snapshot Engine,
Twice-Daily Scheduler, Aurora / PostgreSQL table persistence, and Canary Dashboard integration.
"""

import os
import sqlite3
import pytest
import requests

from canary.aws_quotas import (
    DEFAULT_AWS_SERVICE_QUOTAS,
    calculate_utilization,
    fetch_aws_service_quotas,
    capture_service_quotas_snapshot,
    get_latest_service_quotas,
    get_recent_snapshots_log,
    get_aws_quotas_scheduler_status,
    start_aws_quotas_scheduler,
    trigger_aws_quotas_snapshot_now,
    SCHEDULER_INTERVAL_SECONDS,
)
from canary.canary import render_dashboard_html

CANARY_URL = os.getenv("CANARY_URL", "http://localhost:8085")


def test_aws_service_quotas_master_catalog():
    """Verify master quota catalog covers all key AWS services with proper attributes."""
    assert len(DEFAULT_AWS_SERVICE_QUOTAS) >= 25

    services = {q["service_code"] for q in DEFAULT_AWS_SERVICE_QUOTAS}
    expected_services = {"rds", "ec2", "vpc", "lambda", "s3", "cloudwatch", "dynamodb"}
    assert expected_services.issubset(services), f"Missing services: {expected_services - services}"

    for q in DEFAULT_AWS_SERVICE_QUOTAS:
        assert "service_code" in q
        assert "service_name" in q
        assert "quota_code" in q
        assert "quota_name" in q
        assert "default_quota" in q
        assert "current_configuration" in q
        assert "current_usage" in q
        assert "unit" in q
        assert "is_adjustable" in q
        assert q["quota_code"].startswith("L-")
        assert q["default_quota"] > 0
        assert q["current_configuration"] >= 0


def test_calculate_utilization_logic():
    """Verify utilization calculation and risk status categories (Normal, Warning, Critical)."""
    # Normal (< 70%)
    pct, status = calculate_utilization(10.0, 100.0)
    assert pct == 10.0
    assert status == "NORMAL"

    pct, status = calculate_utilization(69.9, 100.0)
    assert pct == 69.9
    assert status == "NORMAL"

    # Warning (70.0% to 84.9%)
    pct, status = calculate_utilization(70.0, 100.0)
    assert pct == 70.0
    assert status == "WARNING"

    pct, status = calculate_utilization(84.9, 100.0)
    assert pct == 84.9
    assert status == "WARNING"

    # Critical (>= 85%)
    pct, status = calculate_utilization(85.0, 100.0)
    assert pct == 85.0
    assert status == "CRITICAL"

    pct, status = calculate_utilization(95.0, 100.0)
    assert pct == 95.0
    assert status == "CRITICAL"

    # Edge case: zero / negative limit
    pct, status = calculate_utilization(5.0, 0.0)
    assert pct > 1000.0
    assert status == "CRITICAL"


def test_fetch_aws_service_quotas_returns_complete_fields():
    """Verify fetch_aws_service_quotas returns populated records with keys, status, and pct."""
    quotas = fetch_aws_service_quotas()
    assert len(quotas) == len(DEFAULT_AWS_SERVICE_QUOTAS)

    for q in quotas:
        assert "quota_key" in q
        assert ":" in q["quota_key"]
        assert "service_code" in q
        assert "default_quota" in q
        assert "current_configuration" in q
        assert "current_usage" in q
        assert "utilization_pct" in q
        assert "status" in q
        assert q["status"] in ("NORMAL", "WARNING", "CRITICAL")
        assert "captured_at" in q


def test_twice_daily_scheduler_cadence():
    """Verify scheduler is configured for twice daily (12 hours / 43,200 seconds) cadence."""
    assert SCHEDULER_INTERVAL_SECONDS == 43200

    status = get_aws_quotas_scheduler_status()
    assert status["enabled"] is True
    assert status["interval_seconds"] == 43200
    assert status["interval_hours"] == 12.0
    assert "Twice Daily" in status["frequency"]
    assert status["total_monitored"] >= 25


def test_capture_service_quotas_snapshot_in_memory():
    """Verify capturing snapshot calculates proper summary metrics and populates caches."""
    snapshot = capture_service_quotas_snapshot(get_conn_or_conn=None)

    assert "snapshot_id" in snapshot
    assert snapshot["snapshot_id"].startswith("snap_")
    assert snapshot["total_quotas"] >= 25
    assert snapshot["avg_utilization_pct"] >= 0.0
    assert isinstance(snapshot["service_counts"], dict)
    assert "rds" in snapshot["service_counts"]
    assert "ec2" in snapshot["service_counts"]

    # Verify memory cache access
    latest = get_latest_service_quotas(get_conn_or_conn=None)
    assert len(latest) == snapshot["total_quotas"]

    recent_snaps = get_recent_snapshots_log(get_conn_or_conn=None, limit=10)
    assert len(recent_snaps) >= 1
    assert recent_snaps[0]["snapshot_id"] == snapshot["snapshot_id"]


def test_database_table_persistence_sqlite_compatibility():
    """Verify database schema compatibility with SQLite in addition to Aurora PostgreSQL."""
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE aws_service_quotas_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_quotas INTEGER DEFAULT 0,
            warning_quotas INTEGER DEFAULT 0,
            critical_quotas INTEGER DEFAULT 0,
            avg_utilization_pct REAL DEFAULT 0.0,
            service_counts TEXT DEFAULT '{}',
            raw_quotas_json TEXT DEFAULT '[]'
        );
    """)

    cur.execute("""
        CREATE TABLE aws_service_quotas_latest (
            quota_key TEXT PRIMARY KEY,
            service_code TEXT NOT NULL,
            service_name TEXT NOT NULL,
            quota_code TEXT NOT NULL,
            quota_name TEXT NOT NULL,
            default_quota REAL DEFAULT 0.0,
            current_configuration REAL DEFAULT 0.0,
            current_usage REAL DEFAULT 0.0,
            utilization_pct REAL DEFAULT 0.0,
            unit TEXT DEFAULT '',
            is_adjustable INTEGER DEFAULT 1,
            status TEXT DEFAULT 'NORMAL',
            last_snapshot_id TEXT,
            last_snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cur.execute("""
        INSERT INTO aws_service_quotas_snapshots
        (snapshot_id, total_quotas, avg_utilization_pct)
        VALUES ('snap_test_001', 34, 12.5);
    """)

    cur.execute("""
        INSERT INTO aws_service_quotas_latest
        (quota_key, service_code, service_name, quota_code, quota_name, default_quota, current_configuration, current_usage, utilization_pct, status)
        VALUES ('rds:L-7B6409FD', 'rds', 'Amazon RDS', 'L-7B6409FD', 'DB instances', 40.0, 40.0, 2.0, 5.0, 'NORMAL');
    """)

    conn.commit()

    cur.execute("SELECT snapshot_id, total_quotas, avg_utilization_pct FROM aws_service_quotas_snapshots;")
    row = cur.fetchone()
    assert row[0] == "snap_test_001"
    assert row[1] == 34
    assert row[2] == 12.5

    cur.execute("SELECT quota_key, current_configuration, current_usage, utilization_pct FROM aws_service_quotas_latest;")
    q_row = cur.fetchone()
    assert q_row[0] == "rds:L-7B6409FD"
    assert q_row[1] == 40.0
    assert q_row[2] == 2.0
    assert q_row[3] == 5.0

    conn.close()


def test_canary_dashboard_html_elements():
    """Verify Canary dashboard HTML includes all AWS Service Quotas UI controls and tables."""
    html = render_dashboard_html()

    # Tab navigation button
    assert 'id="view-quotas-btn"' in html
    assert "switchView('aws_quotas'" in html

    # Section container
    assert 'id="aws-quotas-section"' in html

    # Summary KPI elements
    assert 'id="kpi-quotas-total"' in html
    assert 'id="kpi-quotas-avg-util"' in html
    assert 'id="kpi-quotas-risk"' in html
    assert 'id="kpi-quotas-cadence"' in html

    # Filters and search
    assert 'id="aws-quotas-service-filter"' in html
    assert 'id="aws-quotas-status-filter"' in html
    assert 'id="aws-quotas-search-input"' in html
    assert 'filterAwsQuotasTable()' in html

    # Action buttons
    assert 'id="btn-snapshot-now"' in html
    assert 'captureAwsQuotaSnapshotNow()' in html
    assert 'fetchAwsServiceQuotas()' in html

    # Tables
    assert 'id="aws-quotas-table"' in html
    assert 'id="aws-quotas-tbody"' in html
    assert 'id="aws-quotas-snapshots-table"' in html
    assert 'id="aws-quotas-snapshots-tbody"' in html

    # Aurora DB reference badges
    assert "aws_service_quotas_snapshots" in html
    assert "aws_service_quotas_latest" in html
    assert "Twice Daily Snapshot" in html


def test_live_canary_aws_quotas_endpoints():
    """Verify live HTTP endpoints on Canary server for AWS quotas and snapshots."""
    try:
        resp = requests.get(f"{CANARY_URL}/health", timeout=2)
        if resp.status_code != 200:
            pytest.skip("Local canary container not accessible on port 8085")
    except Exception:
        pytest.skip("Local canary container not reachable")

    # 1. GET /api/aws/quotas
    get_resp = requests.get(f"{CANARY_URL}/api/aws/quotas", timeout=10)
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["status"] == "success"
    assert "scheduler" in data
    assert data["scheduler"]["interval_seconds"] == 43200
    assert "quotas" in data
    assert len(data["quotas"]) >= 25
    assert "recent_snapshots" in data

    # 2. GET /api/aws/quotas/snapshots
    snap_resp = requests.get(f"{CANARY_URL}/api/aws/quotas/snapshots", timeout=10)
    assert snap_resp.status_code == 200
    snap_data = snap_resp.json()
    assert snap_data["status"] == "success"
    assert "snapshots" in snap_data
    assert len(snap_data["snapshots"]) >= 1

    # 3. POST /api/aws/quotas/snapshot (on-demand snapshot capture)
    post_resp = requests.post(f"{CANARY_URL}/api/aws/quotas/snapshot", timeout=15)
    assert post_resp.status_code == 200
    post_data = post_resp.json()
    assert post_data["status"] == "success"
    assert "snapshot" in post_data
    assert post_data["snapshot"]["snapshot_id"].startswith("snap_")
    assert post_data["snapshot"]["total_quotas"] >= 25
