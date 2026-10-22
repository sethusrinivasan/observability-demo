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

    # Ensure every single app in the on-demand benchmark cycle was hit with the exact same fair endpoint & method
    for res in cycle["results"]:
        assert res["path"] == "/eval"
        assert res["method"] == "POST"


def test_3h_query_activity_retention_and_ui():
    """Verify 3-hour query retention tables, saved queries, and UI elements exist."""
    from canary.canary import SAVED_QUERIES, get_rds_cost_breakdown
    from canary.sql_guard import ALLOWED_TABLES

    # 1. Verify query_activity_snapshots is permitted in SQL guard
    assert "query_activity_snapshots" in ALLOWED_TABLES
    assert "pg_stat_statements" in ALLOWED_TABLES

    # 2. Verify 3-hour saved queries are present
    saved_ids = {q["id"] for q in SAVED_QUERIES}
    assert "recent_3h_query_activity" in saved_ids
    assert "top_3h_service_breakdown" in saved_ids

    # 3. Verify HTML contains the 3-hour query activity table and tbody
    html = render_dashboard_html()
    assert "⏱️ Past 3 Hours Query Activity Log (Rolling Window)" in html
    assert 'id="rds-3h-queries-tbody"' in html

    # 4. Verify get_rds_cost_breakdown returns past_3h_queries list
    data = get_rds_cost_breakdown()
    assert "past_3h_queries" in data
    assert isinstance(data["past_3h_queries"], list)


def test_top_query_fingerprints_io_network_storage_and_costs():
    """Verify top query fingerprints include compute, IO, storage, network, and cost drivers."""
    from canary.canary import get_rds_cost_breakdown, render_dashboard_html
    data = get_rds_cost_breakdown()
    assert data["status"] == "success"
    assert "top_queries" in data
    assert isinstance(data["top_queries"], list)

    # Verify HTML labels state "Database Cost Breakdown"
    html = render_dashboard_html()
    assert "💰 Database Cost Breakdown" in html
    assert "🔍 Top Query Fingerprints &amp; Compute Drivers (pg_stat_statements)" in html
    assert "Disk &amp; Buffer I/O" in html
    assert "Temp &amp; WAL" in html
    assert "Est. Network" in html
    assert "Est. Cost ($)" in html


def test_s3_browser_resilience_and_dashboard_error_logging():
    """Verify S3 browser handles errors gracefully and logs to dashboard error subsystem."""
    from canary.canary import s3_list_objects, log_dashboard_error, DASHBOARD_ERROR_RING
    from canary.sql_guard import ALLOWED_TABLES

    # 1. s3_list_objects should never raise UnboundLocalError even when boto3 or AWS credentials fail
    res = s3_list_objects("test-bucket", "some/prefix")
    assert isinstance(res, dict)
    assert res["status"] in ("success", "error")
    assert "folders" in res
    assert "files" in res

    # 2. Verify dashboard error logging
    entry = log_dashboard_error(
        component="s3_browser",
        error_type="TestError",
        message="Simulated non-JSON response from gateway",
        details={"status_code": 502}
    )
    assert entry["component"] == "s3_browser"
    assert entry["error_type"] == "TestError"
    assert len(DASHBOARD_ERROR_RING) > 0
    assert DASHBOARD_ERROR_RING[0]["component"] == "s3_browser"

    # 3. Verify dashboard_error_logs is in SQL guard
    assert "dashboard_error_logs" in ALLOWED_TABLES


def test_audit_logs_deduplication_and_views():
    """Verify audit_dimensions and deconstructed views are whitelisted and saved queries are present."""
    from canary.sql_guard import ALLOWED_TABLES
    from canary.canary import SAVED_QUERIES

    assert "audit_dimensions" in ALLOWED_TABLES
    assert "v_audit_logs" in ALLOWED_TABLES
    assert "v_audit_dimension_analytics" in ALLOWED_TABLES

    saved_ids = {q["id"] for q in SAVED_QUERIES}
    assert "audit_dimensions_summary" in saved_ids
    assert "v_audit_logs_deconstructed" in saved_ids
    assert "v_audit_dimension_analytics" in saved_ids
    assert "recent_dashboard_errors" in saved_ids


def test_duckdb_s3_calls_and_bandwidth_tracking():
    """Verify DuckDB queries return execution duration, s3 calls count, and bandwidth transferred."""
    from canary.canary import execute_duckdb_query
    res = execute_duckdb_query("SELECT 100 AS num;")
    assert res["status"] == "success"
    assert "s3_calls_count" in res
    assert "bandwidth_transferred_bytes" in res
    assert "bandwidth_transferred_display" in res
    assert isinstance(res["s3_calls_count"], int)
    assert isinstance(res["bandwidth_transferred_bytes"], int)


def test_s3_browser_folder_approximation_zero_cost():
    """Verify S3 folder listing provides approximate object count & size with 0 S3 request cost."""
    from canary.canary import s3_list_objects
    res = s3_list_objects("telemetry-lake", "")
    assert res["status"] == "success"
    assert "folders" in res
    for f in res["folders"]:
        assert "approx_objects" in f
        assert "approx_size_formatted" in f
        assert "0 S3 API calls" in f.get("cost_note", "")


def test_ondemand_custom_target_and_swift_service():
    """Verify On-Demand runs execute across all 9 services including Swift, and UI contains percentile controls."""
    from canary.canary import run_synthetic_cycle, render_dashboard_html

    # Execute on-demand cycle with custom target
    custom_target = {"path": "/version", "method": "GET"}
    res = run_synthetic_cycle(on_demand=True, custom_target=custom_target)
    assert res["status"] == "success"
    assert res["on_demand"] is True
    assert len(res["results"]) >= 8
    apps_tested = {r["app"] for r in res["results"]}
    assert "swift" in apps_tested
    assert "python" in apps_tested
    assert "java" in apps_tested

    # Verify UI contains API selector dropdown, copy button, compute edit box (default 50), and p50/p95/p99/p100 latencies
    html = render_dashboard_html()
    assert 'id="ondemand-api-select"' in html
    assert 'POST /eval' in html
    assert 'GET /compute' in html
    assert 'id="ondemand-compute-input-wrapper"' in html
    assert 'id="ondemand-compute-n"' in html
    assert 'value="50"' in html
    assert 'GET /version' in html
    assert 'GET /health' in html
    assert 'ondemand-p50' in html
    assert 'ondemand-p95' in html
    assert 'ondemand-p99' in html
    assert 'ondemand-p100' in html
    assert 'copyOnDemandResults()' in html
    assert 'resetOnDemandMetrics()' in html


def test_fault_ledger_timestamp_and_sort_desc():
    """Verify Fault Injection Event Ledger keeps timestamps and sorts drills by timestamp descending."""
    from canary.canary import fault_manager, render_dashboard_html
    import time

    # Start and stop 2 distinct drills
    f1 = fault_manager.start_fault(tag="DRILL-EARLIER", fault_type="error_spike", target="swift", duration_sec=10)
    assert "timestamp" in f1
    assert "start_time" in f1
    assert f1["target"] == "swift"
    time.sleep(0.05)
    fault_manager.stop_fault()

    f2 = fault_manager.start_fault(tag="DRILL-LATER", fault_type="high_latency", target="python", duration_sec=10)
    assert "timestamp" in f2
    assert "start_time" in f2
    assert f2["start_time"] >= f1["start_time"]
    fault_manager.stop_fault()

    # Check that UI renders Timestamp column and sort desc logic
    html = render_dashboard_html()
    assert 'Timestamp</th>' in html
    assert 'Fault Injection Event Ledger' in html
    assert '(b.start_time || 0) - (a.start_time || 0)' in html


def test_container_power_manager_includes_swift():
    """Verify Container Power & Resource Manager includes Swift across container map, status, and controls."""
    from canary.canary import DockerManager, docker_manager, render_dashboard_html

    # Check CONTAINER_MAP has Swift
    assert "swift" in DockerManager.CONTAINER_MAP
    assert DockerManager.CONTAINER_MAP["swift"] == "observability-swift-app"

    # Check get_containers_status includes swift
    statuses = docker_manager.get_containers_status()
    assert "swift" in statuses
    assert statuses["swift"]["name"] == "observability-swift-app"

    # Check HTML has swift badge, fault target option, and crash target option
    html = render_dashboard_html()
    assert '.badge-swift' in html
    assert '.badge-fault-swift' in html
    assert '<option value="swift">🕊️ Swift</option>' in html
    assert "CRASH-SWIFT-PROC" in html
