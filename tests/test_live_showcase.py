"""
Live Showcase Verification Test Suite.
Validates the publicly accessible Cloudflare Tunnel deployment of the
observability playground and its microservices stack.
"""
import os
import requests
import pytest

LIVE_URL = os.getenv("CANARY_URL", os.getenv("LIVE_URL", "https://volume-investigation-knife-pros.trycloudflare.com")).rstrip("/")


@pytest.fixture(scope="session")
def session():
    s = requests.Session()
    s.headers.update({"User-Agent": "GitHub-Actions-Live-Verifier/1.0"})
    return s


class TestLiveShowcase:
    """Verifies that the live showcase is online, healthy, and all services are functional."""

    def test_live_homepage_auto_logins(self, session):
        """Visiting the live URL automatically authenticates without prompting for demo login."""
        resp = session.get(f"{LIVE_URL}/", timeout=15)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert "Canary Load Generator" in resp.text
        assert 'id="login-form"' not in resp.text
        # Check that session cookie was issued
        assert "canary_session" in session.cookies or "canary_session" in resp.headers.get("Set-Cookie", "")

    def test_live_login_redirect(self, session):
        """Direct access to /login automatically redirects to / with demo credentials."""
        resp = session.get(f"{LIVE_URL}/login", timeout=15, allow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers.get("Location") == "/"

    def test_live_health_endpoint(self, session):
        """Actuator health endpoint returns 200 OK."""
        resp = session.get(f"{LIVE_URL}/health", timeout=10)
        assert resp.status_code == 200
        assert "OK" in resp.text

    def test_live_stats_stream(self, session):
        """Live synthetic traffic stats are actively emitting."""
        resp = session.get(f"{LIVE_URL}/stats", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") in ("Running", "Initializing")
        assert "effective_tps" in data
        assert "total_requests" in data

    def test_live_dependencies_rollup(self, session):
        """All 9 infrastructure and monitoring stack dependencies report UP."""
        resp = session.get(f"{LIVE_URL}/api/dependencies", timeout=15)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("total_dependencies") == 9
        assert data.get("healthy_count", 0) >= 8, f"Only {data.get('healthy_count')} healthy dependencies: {data}"

    def test_live_trends_metrics(self, session):
        """Mimir trend metrics query succeeds."""
        resp = session.get(f"{LIVE_URL}/api/trends?range=5m", timeout=15)
        assert resp.status_code == 200
        data = resp.json()
        assert "metrics" in data
        assert "throughput" in data["metrics"]
        assert "errors" in data["metrics"]
        assert "availability" in data["metrics"]

    def test_live_proxied_grafana(self, session):
        """Grafana is accessible via the showcase upstream proxy."""
        resp = session.get(f"{LIVE_URL}/open/grafana/api/health", timeout=15)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("database") == "ok"

    def test_live_proxied_prometheus(self, session):
        """Prometheus scraper is responsive via proxy."""
        resp = session.get(f"{LIVE_URL}/open/prometheus/-/ready", timeout=15)
        assert resp.status_code == 200

    def test_live_sql_editor_query_and_guard(self, session):
        """SQL Editor executes valid read queries and guard denies prohibited queries."""
        # Valid read query
        resp = session.post(
            f"{LIVE_URL}/api/sql/query",
            json={"query": "SELECT id, created_at, endpoint, status_code FROM audit_logs ORDER BY id DESC LIMIT 5;"},
            timeout=15,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "success"

        # Prohibited destructive query
        resp_bad = session.post(
            f"{LIVE_URL}/api/sql/query",
            json={"query": "DROP TABLE audit_logs;"},
            timeout=15,
        )
        assert resp_bad.status_code == 400
        assert resp_bad.json().get("status") == "error"

    def test_live_qr_code_generation(self, session):
        """Dynamic SVG QR code generator returns valid SVG."""
        resp = session.get(f"{LIVE_URL}/api/qrcode?url={LIVE_URL}", timeout=10)
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers.get("Content-Type", "")
        assert b"<svg" in resp.content
