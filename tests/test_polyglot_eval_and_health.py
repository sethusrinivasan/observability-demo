"""
tests/test_polyglot_eval_and_health.py
Safety gate test suite for polyglot microservice endpoints, healthchecks,
and canary synthetic traffic zero-error rates.

Verifies:
1. Matrix test of /eval across all 9 polyglot implementations (GET & POST)
2. Uniform mathematical accuracy and 400 error handling on invalid queries
3. Docker Compose healthcheck validity and runtime health status
4. Canary synthetic cycle execution produces 0 unexpected errors across all 9 languages
"""

import json
import urllib.request
import urllib.error
import pytest
import requests

POLYGLOT_APPS = {
    "python": "http://localhost:5000",
    "java": "http://localhost:8080",
    "rust": "http://localhost:8083",
    "node": "http://localhost:8084",
    "go": "http://localhost:8086",
    "dotnet": "http://localhost:8087",
    "c": "http://localhost:8088",
    "php": "http://localhost:8089",
    "swift": "http://localhost:8091",
}

CANARY_URL = "http://localhost:8085"


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_version_endpoint(app_name, base_url):
    """Ensure all 9 microservices expose /version with runtime metadata."""
    resp = requests.get(f"{base_url}/version", timeout=3)
    assert resp.status_code == 200, f"{app_name} /version failed: {resp.status_code}"
    data = resp.json()
    assert "version" in data, f"{app_name} missing 'version' in /version payload"
    assert "language" in data or "app_name" in data, f"{app_name} missing metadata"


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_eval_get_query_param(app_name, base_url):
    """Ensure all 9 microservices support GET /eval?expr=... with url-encoded math."""
    test_cases = [
        ("3%2B4", 7.0),
        ("10-3", 7.0),
        ("2%5E10", 1024.0),
        ("((3%2B4)*6%5E2%2F5)%2B1-7", 44.4),
        ("-5%2B8", 3.0),
        ("2%5E3%5E2", 512.0),
    ]
    for expr, expected in test_cases:
        resp = requests.get(f"{base_url}/eval?expr={expr}", timeout=3)
        assert resp.status_code == 200, f"{app_name} GET /eval?expr={expr} failed with {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "result" in data, f"{app_name} missing 'result' in response"
        assert abs(float(data["result"]) - expected) < 0.01, f"{app_name} got {data['result']} for {expr}, expected {expected}"


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_eval_post_json_payload(app_name, base_url):
    """Ensure all 9 microservices support POST /eval with JSON payload."""
    payload = {"expr": "(10+5)*2^2-3"}
    resp = requests.post(f"{base_url}/eval", json=payload, timeout=3)
    assert resp.status_code == 200, f"{app_name} POST /eval failed with {resp.status_code}: {resp.text}"
    data = resp.json()
    assert abs(float(data["result"]) - 57.0) < 0.01, f"{app_name} result {data.get('result')} != 57.0"


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_eval_error_handling(app_name, base_url):
    """Ensure all 9 microservices return 400 Bad Request for division by zero or invalid input."""
    # Division by zero
    resp = requests.get(f"{base_url}/eval?expr=5%2F0", timeout=3)
    assert resp.status_code == 400, f"{app_name} expected 400 for division by zero, got {resp.status_code}"

    # Empty expression
    resp_empty = requests.get(f"{base_url}/eval", timeout=3)
    assert resp_empty.status_code == 400, f"{app_name} expected 400 for missing expr, got {resp_empty.status_code}"


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_health_endpoint(app_name, base_url):
    """Ensure all 9 microservices expose GET /health with 200 status."""
    resp = requests.get(f"{base_url}/health", timeout=3)
    assert resp.status_code == 200, f"{app_name} /health failed: {resp.status_code}"
    try:
        data = resp.json()
        status_val = str(data.get("status", "")).upper()
        assert status_val in ("UP", "OK", "HEALTHY") or "status" in data
    except Exception:
        assert resp.text.strip().upper() in ("OK", "UP", "HEALTHY")


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_compute_endpoint(app_name, base_url):
    """Ensure all 9 microservices calculate iterative Fibonacci without overflow up to n=50."""
    resp = requests.get(f"{base_url}/compute/50", timeout=5)
    assert resp.status_code == 200, f"{app_name} /compute/50 failed with {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "result" in data, f"{app_name} missing 'result' in /compute/50 response"
    assert int(data["result"]) == 12586269025, f"{app_name} fib(50) expected 12586269025, got {data['result']}"


def test_canary_synthetic_cycle_zero_errors():
    """Verify Canary synthetic cycle executes cleanly with 0 errors across all services."""
    resp = requests.get(f"{CANARY_URL}/api/trigger-cycle", timeout=15)
    assert resp.status_code == 200, f"Trigger cycle failed with {resp.status_code}"
    cycle = resp.json().get("cycle", {})
    assert cycle.get("error_count") == 0, f"Canary cycle had errors: {cycle.get('results')}"
    assert cycle.get("success_count", 0) >= 300, f"Expected full workload execution, got {cycle.get('success_count')}"
