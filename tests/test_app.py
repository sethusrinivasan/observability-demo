"""
Test suite for observability-demo Flask app.

DB-dependent tests (auditlog, auditlog_stats) are skipped automatically
when Postgres is not reachable, so the suite runs cleanly in CI without
a live database.
"""
import json
import os
import unittest.mock as mock

import pytest

from app import (
    app,
    fibonacci,
    get_cgroup_memory_percent,
    get_compute_example_links,
    get_home_html,
    get_process_cpu_seconds,
    get_process_memory_rss,
    get_system_loadavg,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _postgres_available() -> bool:
    """Return True only when a real Postgres is reachable."""
    try:
        import psycopg2
        from app import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASSWORD, connect_timeout=2,
        )
        conn.close()
        return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _postgres_available(), reason="Postgres not reachable"
)


# ===========================================================================
# fibonacci
# ===========================================================================

class TestFibonacci:
    def test_base_cases(self):
        assert fibonacci(0) == 0
        assert fibonacci(1) == 1

    def test_small_values(self):
        assert fibonacci(2) == 1
        assert fibonacci(5) == 5
        assert fibonacci(10) == 55

    def test_known_sequence(self):
        expected = [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]
        assert [fibonacci(i) for i in range(10)] == expected


# ===========================================================================
# HTML helpers
# ===========================================================================

class TestGetComputeExampleLinks:
    def test_contains_all_example_inputs(self):
        links = get_compute_example_links()
        for n in [5, 10, 20]:
            assert f"/compute/{n}" in links

    def test_returns_list_items(self):
        links = get_compute_example_links()
        assert "<li>" in links
        assert "<a href=" in links


class TestGetHomeHtml:
    def test_heading_present(self):
        assert "Hello from Observability Lab!" in get_home_html()

    def test_compute_links_present(self):
        html = get_home_html()
        for n in [5, 10, 20]:
            assert f"/compute/{n}" in html
            assert f"Compute {n}" in html

    def test_form_elements_present(self):
        html = get_home_html()
        assert "id='compute-value'" in html
        assert "goCompute()" in html

    def test_valid_html_structure(self):
        html = get_home_html()
        assert html.strip().startswith("<!doctype html>")
        assert "</html>" in html

    def test_charset_declared(self):
        assert "utf-8" in get_home_html()


# ===========================================================================
# GET /
# ===========================================================================

class TestHomeRoute:
    def test_status_200(self, client):
        assert client.get("/").status_code == 200

    def test_content_type_html(self, client):
        assert client.get("/").headers["Content-Type"].startswith("text/html")

    def test_body_contains_heading(self, client):
        assert "Hello from Observability Lab!" in client.get("/").get_data(as_text=True)

    def test_body_contains_compute_links(self, client):
        body = client.get("/").get_data(as_text=True)
        for n in [5, 10, 20]:
            assert f"/compute/{n}" in body


# ===========================================================================
# GET /compute/<n>
# ===========================================================================

class TestComputeRoute:
    def test_correct_fibonacci_result(self, client):
        resp = client.get("/compute/10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["input"] == 10
        assert data["result"] == 55

    def test_zero_input(self, client):
        resp = client.get("/compute/0")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == 0

    def test_one_input(self, client):
        resp = client.get("/compute/1")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == 1

    def test_random_error_returns_500(self, client):
        # Artificial random errors have been removed — /compute always succeeds
        # for valid input. This test is replaced by the non-integer 404 test below.
        pass

    def test_non_integer_returns_404(self, client):
        assert client.get("/compute/abc").status_code == 404

    def test_response_is_json(self, client):
        resp = client.get("/compute/5")
        assert resp.content_type.startswith("application/json")

    @pytest.mark.parametrize("n,expected", [(0, 0), (1, 1), (5, 5), (7, 13), (10, 55)])
    def test_parametrized_fibonacci_values(self, client, n, expected):
        resp = client.get(f"/compute/{n}")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == expected


# ===========================================================================
# GET|POST /auditlog  (requires Postgres)
# ===========================================================================

class TestAuditlogRoute:
    @requires_postgres
    def test_get_returns_201(self, client):
        resp = client.get("/auditlog")
        assert resp.status_code == 201

    @requires_postgres
    def test_post_returns_201(self, client):
        resp = client.post("/auditlog", data={"source": "test"})
        assert resp.status_code == 201

    @requires_postgres
    def test_response_schema(self, client):
        data = client.get("/auditlog").get_json()
        required = {
            "status", "audit_id", "response_time_seconds",
            "process_memory_rss", "process_cpu_seconds",
            "system_loadavg_1m", "container_memory_current",
            "container_memory_limit", "container_memory_percent",
            "container_cpu_usage_ns", "details",
        }
        assert required.issubset(data.keys())

    @requires_postgres
    def test_status_field_is_ok(self, client):
        assert client.get("/auditlog").get_json()["status"] == "ok"

    @requires_postgres
    def test_audit_id_is_integer(self, client):
        assert isinstance(client.get("/auditlog").get_json()["audit_id"], int)

    @requires_postgres
    def test_response_time_is_non_negative(self, client):
        assert client.get("/auditlog").get_json()["response_time_seconds"] >= 0

    @requires_postgres
    def test_details_contains_remote_addr(self, client):
        details = client.get("/auditlog").get_json()["details"]
        assert "remote_addr" in details

    def test_db_failure_returns_500(self, client):
        with mock.patch("app.get_db_connection", side_effect=Exception("db down")):
            resp = client.get("/auditlog")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["status"] == "error"
        assert "message" in data


# ===========================================================================
# GET /auditlog/stats  (requires Postgres)
# ===========================================================================

class TestAuditlogStatsRoute:
    @requires_postgres
    def test_status_200(self, client):
        assert client.get("/auditlog/stats").status_code == 200

    @requires_postgres
    def test_response_schema(self, client):
        data = client.get("/auditlog/stats").get_json()
        assert "total_rows" in data
        assert "success_count" in data
        assert "failure_count" in data
        assert "percentiles" in data
        assert "response_time_histogram" in data

    @requires_postgres
    def test_percentiles_keys(self, client):
        percentiles = client.get("/auditlog/stats").get_json()["percentiles"]
        for key in ["p50_seconds", "p90_seconds", "p95_seconds", "p99_seconds"]:
            assert key in percentiles

    @requires_postgres
    def test_histogram_structure(self, client):
        hist = client.get("/auditlog/stats").get_json()["response_time_histogram"]
        assert hist["bucket_count"] == 10
        assert hist["max_seconds"] == 5.0
        assert isinstance(hist["buckets"], list)

    @requires_postgres
    def test_counts_are_non_negative(self, client):
        data = client.get("/auditlog/stats").get_json()
        assert data["total_rows"] >= 0
        assert data["success_count"] >= 0
        assert data["failure_count"] >= 0

    @requires_postgres
    def test_success_plus_failure_equals_total(self, client):
        data = client.get("/auditlog/stats").get_json()
        assert data["success_count"] + data["failure_count"] == data["total_rows"]

    def test_db_failure_returns_500(self, client):
        with mock.patch("app.get_db_connection", side_effect=Exception("db down")):
            resp = client.get("/auditlog/stats")
        assert resp.status_code == 500
        assert resp.get_json()["status"] == "error"


# ===========================================================================
# 404 / unknown routes
# ===========================================================================

class TestUnknownRoutes:
    def test_unknown_route_returns_404(self, client):
        assert client.get("/does-not-exist").status_code == 404

    def test_post_to_compute_returns_405(self, client):
        assert client.post("/compute/5").status_code == 405


# ===========================================================================
# System / cgroup metric helpers
# ===========================================================================

class TestSystemMetricHelpers:
    def test_get_process_memory_rss_positive(self):
        assert get_process_memory_rss() > 0

    def test_get_process_cpu_seconds_non_negative(self):
        assert get_process_cpu_seconds() >= 0

    def test_get_system_loadavg_non_negative(self):
        assert get_system_loadavg() >= 0

    def test_get_cgroup_memory_percent_no_limit(self):
        # When limit is 0 (no cgroup limit set), percent should be 0.0
        with mock.patch("app.get_cgroup_memory_limit", return_value=0):
            assert get_cgroup_memory_percent() == 0.0

    def test_get_cgroup_memory_percent_with_limit(self):
        with mock.patch("app.get_cgroup_memory_limit", return_value=1_000_000):
            with mock.patch("app.get_cgroup_memory_current", return_value=500_000):
                assert get_cgroup_memory_percent() == pytest.approx(50.0)

    def test_get_system_loadavg_oserror_returns_zero(self):
        with mock.patch("os.getloadavg", side_effect=OSError):
            assert get_system_loadavg() == 0.0
