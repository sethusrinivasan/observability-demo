#!/usr/bin/env python3
"""
smoke_test.py — multi-language end-to-end smoke tests for Python, Java, Rust, Node.js, and Go apps.
Works without external dependencies (uses standard library urllib).
Automatically detects Docker Compose vs Kubernetes NodePort environments.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _get(url, timeout=5):
    req = urllib.request.Request(url, headers={"User-Agent": "smoke-test"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _post_json(url, payload, timeout=5):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "smoke-test"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def get_base_urls():
    # 1. Explicit env vars
    py = os.getenv("PYTHON_URL")
    jv = os.getenv("JAVA_URL")
    rs = os.getenv("RUST_URL")
    nd = os.getenv("NODE_URL")
    go = os.getenv("GO_URL")
    dn = os.getenv("DOTNET_URL")
    if py and jv and rs and nd and go and dn:
        return {"python": py, "java": jv, "rust": rs, "node": nd, "go": go, "dotnet": dn}

    # 2. CLI flags
    if "--k8s" in sys.argv:
        return {
            "python": "http://localhost:30001",
            "java": "http://localhost:30005",
            "rust": "http://localhost:30006",
            "node": "http://localhost:30007",
            "go": "http://localhost:30008",
            "dotnet": "http://localhost:30009",
        }
    if "--docker" in sys.argv:
        return {
            "python": "http://localhost:5000",
            "java": "http://localhost:8080",
            "rust": "http://localhost:8083",
            "node": "http://localhost:8084",
            "go": "http://localhost:8086",
            "dotnet": "http://localhost:8087",
        }

    # 3. Auto-detect: check if localhost:5000 is reachable
    try:
        status, _ = _get("http://localhost:5000/", timeout=1)
        if status == 200:
            return {
                "python": "http://localhost:5000",
                "java": "http://localhost:8080",
                "rust": "http://localhost:8083",
                "node": "http://localhost:8084",
                "go": "http://localhost:8086",
                "dotnet": "http://localhost:8087",
            }
    except Exception:
        pass

    # 4. Fallback to K8s NodePorts
    return {
        "python": "http://localhost:30001",
        "java": "http://localhost:30005",
        "rust": "http://localhost:30006",
        "node": "http://localhost:30007",
        "go": "http://localhost:30008",
        "dotnet": "http://localhost:30009",
    }


def test_app(lang, base_url):
    print(f"Testing {lang.upper()} app at {base_url}...")

    # 1. Test / (Home)
    try:
        status, _ = _get(f"{base_url}/", timeout=5)
        print(f"  [GET /]: {status}")
        assert status == 200, f"Expected 200, got {status}"
    except Exception as e:
        print(f"  [GET /]: FAILED - {e}")
        return False

    # 2. Test /version
    try:
        status, body = _get(f"{base_url}/version", timeout=5)
        print(f"  [GET /version]: {status}")
        assert status == 200
        data = json.loads(body)
        assert data.get("language") in (lang, "nodejs", "golang", "csharp", "dotnet")
    except Exception as e:
        print(f"  [GET /version]: FAILED - {e}")
        return False

    # 3. Test /selftest
    try:
        status, body = _get(f"{base_url}/selftest", timeout=5)
        print(f"  [GET /selftest]: {status}")
        assert status == 200
        data = json.loads(body)
        assert data.get("success") is True
    except Exception as e:
        print(f"  [GET /selftest]: FAILED - {e}")
        return False

    # 4. Test /compute/10
    try:
        status, body = _get(f"{base_url}/compute/10", timeout=5)
        print(f"  [GET /compute/10]: {status}")
        assert status == 200
        data = json.loads(body)
        assert data["result"] == 55
    except Exception as e:
        print(f"  [GET /compute/10]: FAILED - {e}")
        return False

    # 5. Test /eval (POST JSON)
    try:
        payload = {"expr": "2 + 3 * 4"}
        status, body = _post_json(f"{base_url}/eval", payload, timeout=5)
        print(f"  [POST /eval]: {status}")
        assert status == 200
        data = json.loads(body)
        assert data["result"] == 14.0
    except Exception as e:
        print(f"  [POST /eval]: FAILED - {e}")
        return False

    # 6. Test /eval (GET query param)
    try:
        status, body = _get(f"{base_url}/eval?expr=2%2B3*4", timeout=5)
        print(f"  [GET /eval]: {status}")
        assert status == 200
        data = json.loads(body)
        assert data["result"] == 14.0
    except Exception as e:
        print(f"  [GET /eval]: FAILED - {e}")
        return False

    # 7. Test /auditlog
    try:
        status, body = _get(f"{base_url}/auditlog", timeout=5)
        print(f"  [GET /auditlog]: {status}")
        assert status in [200, 201], f"Expected 200/201, got {status}"
        data = json.loads(body)
        assert data.get("status") == "ok"
    except Exception as e:
        print(f"  [GET /auditlog]: FAILED - {e}")
        return False

    print(f"  {lang.upper()} app: OK\n")
    return True


def main():
    base_urls = get_base_urls()
    print(f"Target URLs: {base_urls}\n")
    all_ok = True
    for lang, url in base_urls.items():
        if not test_app(lang, url):
            all_ok = False

    if all_ok:
        print("All apps verified successfully!")
        sys.exit(0)
    else:
        print("Some tests failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
