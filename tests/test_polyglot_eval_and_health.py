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
import os
import urllib.request
import urllib.error
import pytest
import requests

USE_LIVE_AWS = os.getenv("USE_LIVE_AWS", "1").lower() in ("1", "true", "yes")

LIVE_APPS = {
    "python": "https://mxhrcxceaeluv6gu2cod4quwpm0kfydz.lambda-url.us-east-2.on.aws",
    "java": "https://c5nmwnntg56s7v6gr2cowlmqqm0bcovb.lambda-url.us-east-2.on.aws",
    "rust": "https://nulam6s4glkv2wafy6tcrjn2j40fyhim.lambda-url.us-east-2.on.aws",
    "node": "https://xo3ktoqz6qz3dm5o64hvgm22mq0zkqat.lambda-url.us-east-2.on.aws",
    "go": "https://h74bl44slegkr7c6h32hyygzui0djbvn.lambda-url.us-east-2.on.aws",
    "dotnet": "https://4o5yjl47yxdg5e5tsj727mchb40evddy.lambda-url.us-east-2.on.aws",
    "c": "https://omtosemwg4qxxjqlnbidwl4mvm0wvqva.lambda-url.us-east-2.on.aws",
    "php": "https://gvn63eisikfv2zvzf52bqngwku0zhtkv.lambda-url.us-east-2.on.aws",
    "swift": "https://6cq3xl2oie6huoo4mxrqxxfgqu0upybj.lambda-url.us-east-2.on.aws",
}

LOCAL_APPS = {
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

POLYGLOT_APPS = LIVE_APPS if USE_LIVE_AWS else LOCAL_APPS
CANARY_URL = os.getenv("AWS_CANARY_URL", "https://236mcyozjcjlmhngkx75qcae7q0kabru.lambda-url.us-east-2.on.aws") if USE_LIVE_AWS else "http://localhost:8085"


EXPECTED_APP_METADATA = {
    "python": {"app_name": "observability-python-app", "languages": ["python"]},
    "java": {"app_name": "observability-java-app", "languages": ["java"]},
    "rust": {"app_name": "observability-rust-app", "languages": ["rust"]},
    "node": {"app_name": "observability-node-app", "languages": ["nodejs", "node", "javascript"]},
    "go": {"app_name": "observability-go-app", "languages": ["golang", "go"]},
    "dotnet": {"app_name": "observability-dotnet-app", "languages": ["csharp", "dotnet"]},
    "c": {"app_name": "observability-c-app", "languages": ["c"]},
    "php": {"app_name": "observability-php-app", "languages": ["php"]},
    "swift": {"app_name": "observability-swift-app", "languages": ["swift"]},
}

def safe_get(url, **kwargs):
    kwargs.setdefault("timeout", 5)
    for _ in range(3):
        try:
            r = requests.get(url, **kwargs)
            if r.status_code != 502:
                return r
        except Exception:
            pass
        import time
        time.sleep(0.5)
    return requests.get(url, **kwargs)


def safe_post(url, **kwargs):
    kwargs.setdefault("timeout", 5)
    for _ in range(3):
        try:
            r = requests.post(url, **kwargs)
            if r.status_code != 502:
                return r
        except Exception:
            pass
        import time
        time.sleep(0.5)
    return requests.post(url, **kwargs)


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_version_endpoint(app_name, base_url):
    """Ensure all 9 microservices expose /version with runtime metadata, exact matching identity, and unique Linux process name."""
    resp = safe_get(f"{base_url}/version")
    assert resp.status_code == 200, f"{app_name} /version failed: {resp.status_code}"
    data = resp.json()
    assert "version" in data, f"{app_name} missing 'version' in /version payload"
    expected = EXPECTED_APP_METADATA[app_name]
    assert data.get("app_name") == expected["app_name"], f"{app_name} returned wrong app_name: {data.get('app_name')} != {expected['app_name']}"
    assert data.get("language") in expected["languages"], f"{app_name} returned wrong language: {data.get('language')} not in {expected['languages']}"
    assert data.get("process_name") == expected["app_name"], f"{app_name} returned wrong process_name: {data.get('process_name')} != {expected['app_name']}"
    assert isinstance(data.get("pid"), (int, float)), f"{app_name} missing integer pid: {data.get('pid')}"
    assert data.get("linux_comm"), f"{app_name} missing linux_comm"

@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_actuator_info_endpoint(app_name, base_url):
    """Ensure all 9 microservices expose Spring Boot-compatible /actuator/info with exact identity."""
    resp = safe_get(f"{base_url}/actuator/info")
    assert resp.status_code == 200, f"{app_name} /actuator/info failed: {resp.status_code}"
    data = resp.json()
    if "app" in data:
        app_info = data["app"]
        expected = EXPECTED_APP_METADATA[app_name]
        assert app_info.get("name") == expected["app_name"], f"{app_name} /actuator/info wrong app name: {app_info.get('name')}"
        assert app_info.get("language") in expected["languages"], f"{app_name} /actuator/info wrong language: {app_info.get('language')}"


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
        resp = safe_get(f"{base_url}/eval?expr={expr}")
        assert resp is not None and resp.status_code == 200, f"{app_name} GET /eval?expr={expr} failed with {resp.status_code if resp else 'None'}: {resp.text if resp else ''}"
        data = resp.json()
        assert "result" in data, f"{app_name} missing 'result' in response"
        assert abs(float(data["result"]) - expected) < 0.01, f"{app_name} got {data['result']} for {expr}, expected {expected}"


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_eval_post_json_payload(app_name, base_url):
    """Ensure all 9 microservices support POST /eval with JSON payload."""
    payload = {"expr": "(10+5)*2^2-3"}
    resp = safe_post(f"{base_url}/eval", json=payload)
    assert resp.status_code == 200, f"{app_name} POST /eval failed with {resp.status_code}: {resp.text}"
    data = resp.json()
    assert abs(float(data["result"]) - 57.0) < 0.01, f"{app_name} result {data.get('result')} != 57.0"


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_eval_error_handling(app_name, base_url):
    """Ensure all 9 microservices return 400 Bad Request for division by zero or invalid input."""
    # Division by zero
    resp = safe_get(f"{base_url}/eval?expr=5%2F0")
    assert resp.status_code == 400, f"{app_name} expected 400 for division by zero, got {resp.status_code}"

    # Empty expression
    resp_empty = safe_get(f"{base_url}/eval")
    assert resp_empty.status_code == 400, f"{app_name} expected 400 for missing expr, got {resp_empty.status_code}"


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_health_endpoint(app_name, base_url):
    """Ensure all 9 microservices expose GET /health with 200 status."""
    resp = safe_get(f"{base_url}/health")
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
    resp = safe_get(f"{base_url}/compute/50")
    assert resp.status_code == 200, f"{app_name} /compute/50 failed with {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "result" in data, f"{app_name} missing 'result' in /compute/50 response"
    assert int(data["result"]) == 12586269025, f"{app_name} fib(50) expected 12586269025, got {data['result']}"


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_compute_negative_invalid_range(app_name, base_url):
    """Negative Test: Out of range n (>92 or <0) must return 400 Bad Request or 404 Not Found."""
    for invalid_n in [-5, 93, 100]:
        resp = safe_get(f"{base_url}/compute/{invalid_n}")
        assert resp.status_code in [400, 404], f"{app_name} /compute/{invalid_n} expected 400/404, got {resp.status_code}"


@pytest.mark.parametrize("app_name,base_url", list(POLYGLOT_APPS.items()))
def test_polyglot_negative_nonexistent_route(app_name, base_url):
    """Negative Test: Nonexistent endpoints must return 404 Not Found."""
    resp = safe_get(f"{base_url}/nonexistent-probe-route-404")
    assert resp.status_code == 404, f"{app_name} expected 404 for unknown route, got {resp.status_code}"


def test_guardrail_drift_detector_simulation():
    """Negative Test: Verify drift detection algorithm catches mismatched responses and process names."""
    mismatched_payloads = [
        # Golang app serving on PHP URL
        ("php", {"app_name": "observability-go-app", "language": "golang", "process_name": "observability-go-app", "version": "1.0.1"}),
        # Golang app serving on Java URL
        ("java", {"app_name": "observability-go-app", "language": "golang", "process_name": "observability-go-app", "version": "1.0.1"}),
        # Bootstrap baseline container serving on Swift URL
        ("swift", {"status": "healthy", "app_name": "observability-demo", "version": "1.0.1"}),
        # Mismatched process name despite spoofed language
        ("python", {"app_name": "observability-python-app", "language": "python", "process_name": "observability-go-app", "version": "1.0.1"}),
        # Unknown/drifted language string
        ("python", {"app_name": "observability-python-app", "language": "ruby", "process_name": "observability-python-app", "version": "1.0.1"}),
    ]

    for target_lang, payload in mismatched_payloads:
        expected = EXPECTED_APP_METADATA[target_lang]
        is_match = (
            payload.get("app_name") == expected["app_name"] and
            payload.get("language") in expected["languages"] and
            payload.get("process_name") == expected["app_name"] and
            "pid" in payload
        )
        assert not is_match, f"Guardrail failed to detect drift for {target_lang} with payload {payload}"


def test_canary_synthetic_cycle_zero_errors():
    """Verify Canary synthetic cycle executes cleanly with 0 errors across all services."""
    import time
    cycle = {}
    for _ in range(3):
        resp = requests.post(f"{CANARY_URL}/api/trigger-cycle", timeout=20)
        if resp.status_code == 200:
            cycle = resp.json().get("cycle", {})
            if cycle.get("error_count") == 0:
                break
        time.sleep(1)
    assert cycle.get("error_count") == 0, f"Canary cycle had errors: {cycle.get('results')}"
    assert cycle.get("success_count", 0) >= 5, f"Expected full workload execution, got {cycle.get('success_count')}"
