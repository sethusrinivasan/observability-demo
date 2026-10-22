"""
tests/test_canary_features.py - Automated validation for Canary Enhancements.

Validates:
1. Postgres SQL Query Editor categorization (Schema & Data Dictionary, Health & Admin, App & Audit).
2. Postgres IAM DB Auth role selection (observability_reader, observability_admin, observability).
3. DuckDB Query Editor keyword syntax highlighting and query cancellation support.
4. DuckDB query 1-day time-bound filters.
5. Frugal load unit configuration (TPM vs TPS scaling).
6. Synthetic request cycle execution.
"""

import pytest
import re
from canary.canary import (
    SAVED_QUERIES,
    CANNED_DUCKDB_QUERIES,
    CanaryState,
    render_dashboard_html,
    run_synthetic_cycle,
)


def test_saved_queries_categorization():
    """Verify saved queries are categorized into the 3 requested groups."""
    categories = {q.get("category") for q in SAVED_QUERIES}
    assert "📚 Schema & Data Dictionary" in categories
    assert "🩺 Postgres Health & Admin Queries" in categories
    assert "🔍 Application & Audit Queries" in categories

    # Verify Schema & Data Dictionary contains size and catalog queries
    schema_queries = [q for q in SAVED_QUERIES if q.get("category") == "📚 Schema & Data Dictionary"]
    assert len(schema_queries) >= 3
    query_sqls = " ".join(q["sql"] for q in schema_queries)
    assert "pg_database_size" in query_sqls or "pg_size_pretty" in query_sqls
    assert "information_schema.columns" in query_sqls or "pg_tables" in query_sqls

    # Verify Health & Admin queries contain pg_stat queries
    admin_queries = [q for q in SAVED_QUERIES if q.get("category") == "🩺 Postgres Health & Admin Queries"]
    assert len(admin_queries) >= 3
    admin_sqls = " ".join(q["sql"] for q in admin_queries)
    assert "pg_stat_activity" in admin_sqls
    assert "pg_stat_database" in admin_sqls


def test_duckdb_canned_queries_time_bound_filter():
    """Verify all canned DuckDB queries contain 1-day time bound filters."""
    for q in CANNED_DUCKDB_QUERIES:
        sql = q["sql"]
        assert "INTERVAL 1 DAY" in sql, f"Query '{q['name']}' missing 1-day time bound filter"


def test_canary_state_tpm_and_tps_scaling():
    """Verify CanaryState supports TPM and TPS rate units."""
    state = CanaryState()
    rate_info = state.get_rate_info()
    assert "rate_unit" in rate_info
    assert "effective_tps" in rate_info
    assert "effective_tpm" in rate_info
    assert "display" in rate_info

    # Test setting 6 TPM override
    override_res = state.set_tps_override(rate=6, duration_sec=60, unit="TPM")
    assert override_res["rate_unit"] == "TPM"
    assert override_res["effective_tpm"] == 6.0
    assert abs(override_res["effective_tps"] - 0.1) < 0.02
    assert abs(state.get_effective_tps() - 0.1) < 0.02

    # Test setting 12 TPS override
    override_res_tps = state.set_tps_override(rate=12, duration_sec=60, unit="TPS")
    assert override_res_tps["rate_unit"] == "TPS"
    assert override_res_tps["effective_tps"] == 12.0
    assert override_res_tps["effective_tpm"] == 720.0

    state.clear_tps_override()


def test_dashboard_html_elements_and_features():
    """Verify HTML rendering contains all required controls, syntax highlighters, and roles."""
    html = render_dashboard_html()

    # DuckDB keyword syntax highlighter and cancel button
    assert 'id="duckdb-highlight"' in html
    assert 'id="duckdb-cancel-btn"' in html
    assert 'paintDuckDBEditor' in html
    assert 'cancelDuckDBQuery' in html
    assert 'sortDuckDBGrid' in html

    # SQL Editor IAM roles and categories
    assert 'id="sql-role-select"' in html
    assert 'observability_reader' in html
    assert 'observability_admin' in html
    assert 'observability' in html

    # Dynamic Load unit controls
    assert 'id="tps-unit-select"' in html
    assert 'TPM (Tx/min)' in html
    assert 'TPS (Tx/sec)' in html

    # On-Demand canary run controls and modal
    assert 'id="btn-ondemand-pulse"' in html
    assert '⚡ Run Once (On-Demand)' in html
    assert 'id="btn-drawer-ondemand"' in html
    assert 'id="ondemand-modal"' in html
    assert 'ondemand-results-body' in html
    assert 'ondemand-summary-bar' in html
    assert 'triggerOnDemandPulse' in html
    assert 'closeOnDemandModal' in html


def test_on_demand_synthetic_cycle():
    """Verify run_synthetic_cycle(on_demand=True) executes across fleet and returns details."""
    import unittest.mock as mock

    # Mock requests.Session to simulate successful responses without real microservices
    mock_session = mock.MagicMock()
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_session.get.return_value = mock_resp
    mock_session.post.return_value = mock_resp

    cycle = run_synthetic_cycle(on_demand=True, session=mock_session)

    assert cycle["status"] == "success"
    assert cycle["on_demand"] is True
    assert cycle["requests_executed"] >= 8
    assert cycle["success_count"] == cycle["requests_executed"]
    assert cycle["error_count"] == 0
    assert "total_duration_ms" in cycle
    assert len(cycle["results"]) == cycle["requests_executed"]

    # Verify every result has required metadata for the results modal
    apps_tested = set()
    for res in cycle["results"]:
        assert "app" in res
        assert "method" in res
        assert "path" in res
        assert "status" in res
        assert res["status"] == "success"
        assert res["status_code"] == 200
        assert "duration_ms" in res
        apps_tested.add(res["app"])

    # Ensure all polyglot microservices were tested in the on-demand cycle
    expected_apps = {"python", "java", "rust", "node", "go", "dotnet", "c", "php"}
    assert expected_apps.issubset(apps_tested)
