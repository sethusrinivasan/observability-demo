"""
tests/test_query_fingerprints_and_plan.py
Automated validation for:
1. AST-based SQL Query Fingerprinting (stripping parameters/constants, canonical skeletons, unique IDs).
2. pg_stat_statements correlation and master fingerprint registry.
3. 30-minute automated background scheduling and on-demand trigger.
4. Graphical query plan UX: node-type color categories, meaningful icons, and glowing Top 3 costliest operations.
"""

import json
import os
import requests
import pytest
from canary.sql_ast import (
    strip_sql_parameters,
    parse_sql_to_ast,
    compute_query_fingerprint,
    correlate_pg_stat_statements,
    get_master_fingerprints,
    get_fingerprint_scheduler_status,
    trigger_fingerprint_analysis_now,
)
from canary.canary import (
    _flatten_explain,
    _get_node_visual_meta,
    get_rds_cost_breakdown,
    render_dashboard_html,
)

CANARY_URL = os.getenv("CANARY_URL", "http://localhost:8085").rstrip("/")


def test_sql_ast_strips_parameters_and_produces_identical_fingerprints():
    """Verify AST normalizer strips literal parameters, strings, binds, and IN lists."""
    # 1. Parameterized SELECT queries with different literals must produce identical fingerprint IDs
    q1 = "SELECT id, username, email FROM users WHERE id = 42 AND status = 'active' AND age > 21 LIMIT 10 OFFSET 0;"
    q2 = "select id, username, email from users where id = 9999 and status = 'banned' and age > 65 limit 100 offset 50;"
    ast1 = parse_sql_to_ast(q1)
    ast2 = parse_sql_to_ast(q2)

    assert ast1["fingerprint_id"] == ast2["fingerprint_id"], "Parameterized SELECT queries must produce identical fingerprint IDs"
    assert ast1["statement_type"] == "SELECT"
    assert ast1["primary_table"] == "users"
    assert "?" in ast1["canonical_skeleton"]
    assert "42" not in ast1["canonical_skeleton"]
    assert "9999" not in ast1["canonical_skeleton"]
    assert "'active'" not in ast1["canonical_skeleton"]

    # 2. IN lists of differing lengths must normalize to IN (?)
    q_in1 = "SELECT * FROM orders WHERE status IN ('placed', 'confirmed', 'shipped');"
    q_in2 = "SELECT * FROM orders WHERE status IN ('cancelled');"
    ast_in1 = parse_sql_to_ast(q_in1)
    ast_in2 = parse_sql_to_ast(q_in2)
    assert ast_in1["fingerprint_id"] == ast_in2["fingerprint_id"]
    assert "IN (?)" in ast_in1["canonical_skeleton"]

    # 3. INSERT queries with different VALUES must match
    q_ins1 = "INSERT INTO audit_logs (endpoint, status_code, details) VALUES ('/api/login', 200, '{\"ok\":true}');"
    q_ins2 = "INSERT INTO audit_logs (endpoint, status_code, details) VALUES ('/api/checkout', 500, '{\"err\":\"timeout\"}');"
    ast_ins1 = parse_sql_to_ast(q_ins1)
    ast_ins2 = parse_sql_to_ast(q_ins2)
    assert ast_ins1["fingerprint_id"] == ast_ins2["fingerprint_id"]
    assert ast_ins1["statement_type"] == "INSERT"
    assert ast_ins1["primary_table"] == "audit_logs"

    # 4. UPDATE queries
    q_up1 = "UPDATE accounts SET balance = balance + 100.50, updated_at = '2026-01-01' WHERE user_id = 10;"
    q_up2 = "update accounts set balance = balance + 999.99, updated_at = '2026-12-31' where user_id = 88;"
    ast_up1 = parse_sql_to_ast(q_up1)
    ast_up2 = parse_sql_to_ast(q_up2)
    assert ast_up1["fingerprint_id"] == ast_up2["fingerprint_id"]
    assert ast_up1["statement_type"] == "UPDATE"

    # 5. Distinct query skeletons must produce DIFFERENT fingerprint IDs
    assert ast1["fingerprint_id"] != ast_ins1["fingerprint_id"]
    assert ast_ins1["fingerprint_id"] != ast_up1["fingerprint_id"]


def test_compute_query_fingerprint_helper():
    """Verify compute_query_fingerprint returns 3-tuple (fp_id, skeleton, ast_dict)."""
    sql = "SELECT count(*) FROM audit_logs WHERE status_code >= 400;"
    fp_id, skeleton, ast = compute_query_fingerprint(sql)

    assert fp_id.startswith("fp_")
    assert len(fp_id) >= 15
    assert "SELECT count(?)" in skeleton or "SELECT count(*)" in skeleton or "SELECT count" in skeleton
    assert isinstance(ast, dict)
    assert ast["fingerprint_id"] == fp_id


def test_correlate_pg_stat_statements_and_master_registry():
    """Verify correlation merges parameterized queries and computes aggregated metrics."""
    # Synthesize simulated entries
    simulated_entries = [
        {
            "query": "SELECT u.id, u.name FROM users u WHERE u.id = 101 AND u.status = 'active';",
            "db_user": "app_user",
            "calls": 50,
            "total_time_ms": 250.0,
            "rows": 50,
            "blks_read": 10,
            "blks_hit": 190,
            "temp_blks": 0,
            "wal_bytes": 0,
        },
        {
            "query": "SELECT u.id, u.name FROM users u WHERE u.id = 999 AND u.status = 'banned';",
            "db_user": "admin_user",
            "calls": 70,
            "total_time_ms": 350.0,
            "rows": 70,
            "blks_read": 15,
            "blks_hit": 265,
            "temp_blks": 0,
            "wal_bytes": 0,
        },
    ]

    correlated = correlate_pg_stat_statements(None, additional_raw_queries=simulated_entries)
    assert len(correlated) >= 1

    # Both simulated queries share the exact same AST skeleton, so they must be merged!
    sample_ast = parse_sql_to_ast(simulated_entries[0]["query"])
    target_fp = sample_ast["fingerprint_id"]

    matched = next((item for item in correlated if item["fingerprint_id"] == target_fp), None)
    assert matched is not None, f"Expected merged entry for fingerprint {target_fp}"
    assert matched["calls"] == 120, f"Expected 50 + 70 = 120 calls, got {matched['calls']}"
    assert matched["total_time_ms"] == 600.0, f"Expected 250 + 350 = 600.0 ms, got {matched['total_time_ms']}"
    assert matched["rows"] == 120
    assert matched["correlated_variants_count"] == 2
    assert "app_user" in matched["db_user"]
    assert "admin_user" in matched["db_user"]
    assert matched["cache_hit_pct"] > 90.0
    assert "total_cost" in matched
    assert matched["total_cost"] > 0.0

    # Master list check
    master = get_master_fingerprints()
    assert len(master) >= 1
    assert any(m["fingerprint_id"] == target_fp for m in master)


def test_fingerprint_scheduler_state_and_trigger():
    """Verify 30-minute scheduler state and on-demand trigger behavior."""
    sched = get_fingerprint_scheduler_status()
    assert sched["enabled"] is True
    assert sched["interval_seconds"] == 1800  # Exactly 30 minutes
    assert sched["interval_minutes"] == 30

    # On-demand trigger immediately updates run count
    initial_runs = sched["run_count"]
    triggered = trigger_fingerprint_analysis_now()
    assert isinstance(triggered, list)

    updated_sched = get_fingerprint_scheduler_status()
    assert updated_sched["run_count"] >= initial_runs + 1
    assert updated_sched["last_run_at"] is not None
    assert updated_sched["next_run_at"] is not None


def test_graphical_plan_exclusive_cost_and_top_3_highlighting():
    """Verify _flatten_explain computes exclusive costs, ranks top 3, and identifies related nodes."""
    plan_payload = {
        "Plan": {
            "Node Type": "Limit",
            "Total Cost": 500.0,
            "Actual Total Time": 200.0,
            "Plan Rows": 10,
            "Actual Rows": 10,
            "Plans": [
                {
                    "Node Type": "Sort",
                    "Total Cost": 490.0,
                    "Actual Total Time": 195.0,
                    "Plan Rows": 10,
                    "Actual Rows": 10,
                    "Plans": [
                        {
                            "Node Type": "Hash Join",
                            "Total Cost": 450.0,
                            "Actual Total Time": 180.0,
                            "Plan Rows": 100,
                            "Actual Rows": 80,
                            "Plans": [
                                {
                                    "Node Type": "Seq Scan",
                                    "Relation Name": "audit_logs",
                                    "Total Cost": 350.0,
                                    "Actual Total Time": 140.0,
                                    "Plan Rows": 10000,
                                    "Actual Rows": 1000,
                                },
                                {
                                    "Node Type": "Hash",
                                    "Total Cost": 80.0,
                                    "Actual Total Time": 35.0,
                                    "Plan Rows": 50,
                                    "Actual Rows": 50,
                                    "Plans": [
                                        {
                                            "Node Type": "Index Scan",
                                            "Relation Name": "audit_dimensions",
                                            "Total Cost": 75.0,
                                            "Actual Total Time": 30.0,
                                            "Plan Rows": 50,
                                            "Actual Rows": 50,
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }

    flat = _flatten_explain(plan_payload)
    assert "nodes" in flat
    assert "top_costliest" in flat
    nodes = flat["nodes"]
    assert len(nodes) == 6

    # Verify exclusive costs:
    # Seq Scan: 350.0 (no children) -> Rank #1 (350 / 500 = 70.0%)
    # Index Scan: 75.0 (no children) -> Rank #2 (75 / 500 = 15.0%)
    # Sort: 490 - 450 = 40.0 -> Rank #3 (40 / 500 = 8.0%)
    seq_node = next(n for n in nodes if n["node_type"] == "Seq Scan")
    idx_node = next(n for n in nodes if n["node_type"] == "Index Scan")
    sort_node = next(n for n in nodes if n["node_type"] == "Sort")
    join_node = next(n for n in nodes if n["node_type"] == "Hash Join")

    assert seq_node["top_cost_rank"] == 1, "Seq Scan must be Rank #1 costliest"
    assert seq_node["cost_share_pct"] == 70.0
    assert seq_node["icon"] == "⚠️"
    assert seq_node["category"] == "scan-seq"

    assert idx_node["top_cost_rank"] == 2, "Index Scan must be Rank #2 costliest"
    assert idx_node["cost_share_pct"] == 15.0
    assert idx_node["icon"] == "⚡"
    assert idx_node["category"] == "scan-index"

    assert sort_node["top_cost_rank"] == 3, "Sort must be Rank #3 costliest"
    assert sort_node["cost_share_pct"] == 8.0
    assert sort_node["icon"] == "📶"
    assert sort_node["category"] == "sort"

    # Hash Join is direct parent of Seq Scan (Rank #1) -> must be marked is_related_to_top_cost
    assert join_node["top_cost_rank"] is None
    assert join_node["is_related_to_top_cost"] is True
    assert join_node["related_to_rank"] == 1

    # Estimation drift on Seq Scan: planned 10,000 vs actual 1,000 -> 10.0x drift
    assert seq_node["drift_ratio"] == 10.0


def test_node_visual_meta_categories_and_icons():
    """Verify distinct icons and themes for all major PostgreSQL node types."""
    scans = [
        ("Seq Scan", "⚠️", "scan-seq"),
        ("Index Scan", "⚡", "scan-index"),
        ("Index Only Scan", "💎", "scan-index-only"),
        ("Bitmap Heap Scan", "📑", "scan-bitmap-heap"),
        ("Bitmap Index Scan", "🗺️", "scan-bitmap-idx"),
    ]
    for nt, icon, cat in scans:
        meta = _get_node_visual_meta(nt)
        assert meta["icon"] == icon, f"Expected {icon} for {nt}"
        assert meta["category"] == cat

    joins = [
        ("Hash Join", "🔀", "join-hash"),
        ("Nested Loop", "🔄", "join-nested"),
        ("Merge Join", "⛓️", "join-merge"),
    ]
    for nt, icon, cat in joins:
        meta = _get_node_visual_meta(nt)
        assert meta["icon"] == icon, f"Expected {icon} for {nt}"
        assert meta["category"] == cat

    aggregates_and_sorts = [
        ("Aggregate", "📊", "aggregate"),
        ("HashAggregate", "📊", "aggregate"),
        ("Sort", "📶", "sort"),
        ("Limit", "⏹️", "limit"),
        ("Materialize", "💾", "materialize"),
        ("Memoize", "💡", "memoize"),
    ]
    for nt, icon, cat in aggregates_and_sorts:
        meta = _get_node_visual_meta(nt)
        assert meta["icon"] == icon
        assert meta["category"] == cat


def test_ui_contains_fingerprint_controls_and_plan_visual_styles():
    """Verify dashboard HTML contains all fingerprinting controls, 30m scheduler badge, modal, and plan CSS."""
    html = render_dashboard_html()

    # 1. Query Fingerprinting Buttons
    assert 'id="run-ast-fingerprint-btn"' in html
    assert 'id="run-ast-fingerprint-btn-2"' in html
    assert "Run Query Fingerprinting" in html
    assert 'onclick="runAstQueryFingerprinting()"' in html

    # 2. 30-Minute Scheduler Badge
    assert 'id="rds-fingerprint-sched-badge"' in html
    assert "⏱️ Auto-Analysis: Every 30m" in html

    # 3. AST Inspector Modal
    assert 'id="ast-inspect-modal"' in html
    assert 'id="ast-modal-fp-badge"' in html
    assert 'id="ast-modal-skeleton"' in html
    assert 'id="ast-modal-json"' in html
    assert "copyAstJson()" in html
    assert "closeAstModal()" in html

    # 4. Graphical Plan Elements
    assert 'id="sql-plan-cost-drivers"' in html
    assert "plan-cost-rank-1" in html
    assert "plan-cost-rank-2" in html
    assert "plan-cost-rank-3" in html
    assert "plan-cost-related" in html
    assert "node-theme-scan-seq" in html
    assert "node-theme-scan-index" in html
    assert "node-theme-join-hash" in html


def test_rds_cost_breakdown_endpoint_includes_ast_fingerprints():
    """Verify GET /api/rds/cost-breakdown response includes AST fingerprint IDs and scheduler metadata."""
    res = requests.get(f"{CANARY_URL}/api/rds/cost-breakdown", timeout=5)
    assert res.status_code == 200
    data = res.json()

    assert "master_fingerprints" in data
    assert "fingerprint_scheduler" in data
    sched = data["fingerprint_scheduler"]
    assert sched["interval_seconds"] == 1800
    assert sched["interval_minutes"] == 30

    # Top queries should contain fingerprint_id and skeleton_sql
    top_q = data.get("top_queries", [])
    if top_q:
        q0 = top_q[0]
        assert "fingerprint_id" in q0
        assert q0["fingerprint_id"].startswith("fp_")
        assert "skeleton_sql" in q0


def test_manual_trigger_endpoint_via_http():
    """Verify POST /api/rds/fingerprint executes analysis and returns updated fingerprints."""
    res = requests.post(f"{CANARY_URL}/api/rds/fingerprint", timeout=5)
    assert res.status_code == 200
    data = res.json()

    assert data["status"] == "success"
    assert "total_fingerprints" in data
    assert "scheduler" in data
    assert data["scheduler"]["interval_minutes"] == 30
    assert isinstance(data["fingerprints"], list)
