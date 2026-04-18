#!/bin/bash
set -e

# Navigate to project root if script called from elsewhere
cd "$(dirname "$0")/.."

echo "=== Running Python Unit Tests ==="
if command -v pytest &> /dev/null; then
    pytest tests/test_app.py
elif python3 -m pytest --version &> /dev/null; then
    python3 -m pytest tests/test_app.py
else
    echo "Pytest not found, skipping Python unit tests (local units only)."
fi

echo -e "\n=== Running Multi-Language Integration Smoke Tests (K8s) ==="

# 1. Setup port-forwards in background
# We kill any existing forwards first to avoid port conflict
pkill -f "port-forward" || true
sleep 1

kubectl port-forward deployment/observability-python-app 30001:5000 > /dev/null 2>&1 &
PYTHON_PF=$!
kubectl port-forward deployment/observability-java-app 30005:8080 > /dev/null 2>&1 &
JAVA_PF=$!
kubectl port-forward deployment/observability-rust-app 30006:8081 > /dev/null 2>&1 &
RUST_PF=$!

# 2. Wait for forwards to be ready
echo "Waiting for port-forwards to stabilize..."
sleep 5

# 3. Run smoke test
python3 tests/smoke_test.py
STATUS=$?

# 4. Cleanup
kill $PYTHON_PF $JAVA_PF $RUST_PF || true
pkill -f "port-forward" || true

if [ $STATUS -eq 0 ]; then
    echo "=== VERIFICATION SUCCESSFUL ==="
else
    echo "=== VERIFICATION FAILED ==="
fi

exit $STATUS
