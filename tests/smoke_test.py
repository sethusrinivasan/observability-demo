import requests
import sys

BASE_URLS = {
    "python": "http://localhost:30001",
    "java": "http://localhost:30005",
    "rust": "http://localhost:30006",
}

def test_app(lang, base_url):
    print(f"Testing {lang.upper()} app at {base_url}...")
    
    # 1. Test / (Home)
    try:
        r = requests.get(f"{base_url}/", timeout=5)
        print(f"  [GET /]: {r.status_code}")
        assert r.status_code == 200
    except Exception as e:
        print(f"  [GET /]: FAILED - {e}")
        return False

    # 2. Test /compute/10
    try:
        r = requests.get(f"{base_url}/compute/10", timeout=5)
        print(f"  [GET /compute/10]: {r.status_code}")
        assert r.status_code == 200
        data = r.json()
        assert data["result"] == 55
    except Exception as e:
        print(f"  [GET /compute/10]: FAILED - {e}")
        return False

    # 3. Test /eval (POST)
    try:
        payload = {"expr": "2 + 3 * 4"}
        r = requests.post(f"{base_url}/eval", json=payload, timeout=5)
        print(f"  [POST /eval]: {r.status_code}")
        assert r.status_code == 200
        data = r.json()
        assert data["result"] == 14.0
    except Exception as e:
        print(f"  [POST /eval]: FAILED - {e}")
        return False

    # 4. Test /auditlog
    try:
        r = requests.get(f"{base_url}/auditlog", timeout=5)
        print(f"  [GET /auditlog]: {r.status_code}")
        assert r.status_code in [201, 200]  # Python returns 201, Java/Rust might return 201 or 200
    except Exception as e:
        print(f"  [GET /auditlog]: FAILED - {e}")
        return False

    print(f"  {lang.upper()} app: OK\n")
    return True

def main():
    all_ok = True
    for lang, url in BASE_URLS.items():
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
