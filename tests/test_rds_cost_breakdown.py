"""
tests/test_rds_cost_breakdown.py
Automated validation for Amazon RDS Frugal Cost Attribution (Method A).
Validates Just-In-Time engine telemetry, call counts, compute attribution,
and Canary Dashboard UI elements.
"""

import os
import requests
import pytest

CANARY_URL = os.getenv("CANARY_URL", "http://localhost:8085").rstrip("/")


def test_rds_cost_breakdown_endpoint_structure():
    """Verify GET /api/rds/cost-breakdown returns expected schema and Method A metadata."""
    res = requests.get(f"{CANARY_URL}/api/rds/cost-breakdown", timeout=5)
    assert res.status_code == 200, f"Expected 200 OK, got {res.status_code}"
    data = res.json()

    assert data["status"] == "success"
    assert "Method A" in data["method"]
    assert "rds_host" in data
    assert "database" in data
    assert data["monthly_budget_usd"] == 100.0
    assert data["attribution_basis"] == "compute"
    assert data["elapsed_ms"] < 500.0  # Ultra-fast JIT query (<500ms even under load)

    # Summary structure
    summary = data["summary"]
    assert "total_calls" in summary
    assert "total_time_ms" in summary
    assert "active_connections" in summary
    assert summary["total_calls"] >= 0

    # Lists
    assert isinstance(data["app_breakdown"], list)
    assert isinstance(data["top_queries"], list)
    assert isinstance(data["active_sessions"], list)


def test_rds_cost_breakdown_custom_budget_and_basis():
    """Verify custom monthly_budget and attribution_basis query parameters."""
    res = requests.get(
        f"{CANARY_URL}/api/rds/cost-breakdown?monthly_budget=250.00&attribution_basis=calls",
        timeout=5,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["monthly_budget_usd"] == 250.00
    assert data["attribution_basis"] == "calls"

    # Attributed costs should scale to $250.00
    apps = data["app_breakdown"]
    if apps:
        total_attributed = sum(app["attributed_cost_usd"] for app in apps)
        assert abs(total_attributed - 250.00) <= 1.0  # Accounts for minor rounding cents


def test_rds_cost_breakdown_ui_elements_in_html():
    """Verify Canary HTML contains the RDS Cost Breakdown tab and container sections."""
    res = requests.get(CANARY_URL, timeout=5)
    assert res.status_code == 200
    html = res.text

    # Tab button
    assert "Database Cost Breakdown" in html
    assert "switchView('cost_breakdown', this)" in html

    # Section ID
    assert 'id="cost-breakdown-section"' in html

    # Critical UI controls
    assert 'id="rds-budget-input"' in html
    assert 'id="rds-basis-select"' in html
    assert 'id="rds-app-breakdown-tbody"' in html
    assert 'id="rds-sessions-tbody"' in html
    assert 'id="rds-top-queries-tbody"' in html
