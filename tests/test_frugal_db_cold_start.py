"""
tests/test_frugal_db_cold_start.py
Verifies that microservices initialize and operate cleanly without cold-start crashes
or DNS delays when running under the Frugal Lakehouse Audit architecture
(MAX_DB_CONNECTIONS="0", POSTGRES_HOST="", DATABASE_URL="").
"""

import requests
import pytest

LIVE_APPS = {
    "rust": "https://nulam6s4glkv2wafy6tcrjn2j40fyhim.lambda-url.us-east-2.on.aws",
    "go": "https://h74bl44slegkr7c6h32hyygzui0djbvn.lambda-url.us-east-2.on.aws",
    "dotnet": "https://4o5yjl47yxdg5e5tsj727mchb40evddy.lambda-url.us-east-2.on.aws",
    "node": "https://xo3ktoqz6qz3dm5o64hvgm22mq0zkqat.lambda-url.us-east-2.on.aws",
    "python": "https://mxhrcxceaeluv6gu2cod4quwpm0kfydz.lambda-url.us-east-2.on.aws",
}


@pytest.mark.parametrize("service,base_url", LIVE_APPS.items())
def test_frugal_cold_start_version_and_health(service, base_url):
    """Verify each microservice returns 200 OK on /version without init crashes."""
    resp = requests.get(f"{base_url}/version", timeout=15)
    assert resp.status_code == 200, f"{service} /version failed: {resp.status_code}"
    data = resp.json()
    assert data.get("app_name") or data.get("language"), f"{service} returned empty context"

    health_resp = requests.get(f"{base_url}/health", timeout=15)
    assert health_resp.status_code in (200, 204), f"{service} /health failed: {health_resp.status_code}"


@pytest.mark.parametrize("service,base_url", LIVE_APPS.items())
def test_frugal_compute_and_eval(service, base_url):
    """Verify compute and math evaluation succeed reliably without DB dependency."""
    compute_resp = requests.get(f"{base_url}/compute/10", timeout=15)
    assert compute_resp.status_code == 200, f"{service} /compute/10 failed: {compute_resp.status_code}"

    eval_resp = requests.get(f"{base_url}/eval?expr=2%2B3*4", timeout=15)
    assert eval_resp.status_code == 200, f"{service} /eval failed: {eval_resp.status_code}"
    assert eval_resp.json().get("result") == 14.0, f"{service} /eval wrong result"
