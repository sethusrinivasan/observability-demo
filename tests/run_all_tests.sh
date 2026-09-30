#!/bin/bash
set -e

# Navigate to project root if script called from elsewhere
cd "$(dirname "$0")/.."

echo "=== Running Python Unit & Infrastructure Tests ==="
if command -v pytest &> /dev/null; then
    pytest tests/ -v
elif python3 -m pytest --version &> /dev/null; then
    python3 -m pytest tests/ -v
elif docker ps --format '{{.Names}}' 2>/dev/null | grep -q "observability-python-app"; then
    echo "Running pytest via observability-python-app Docker container..."
    DOCKER_BIN="$(which docker 2>/dev/null || echo '/usr/bin/docker')"
    docker run --rm --network host \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v "$DOCKER_BIN":"$DOCKER_BIN" \
        -v "$(pwd):/workspace" \
        -w /workspace \
        -e PYTHONPATH=/workspace \
        observability-demo-observability-python-app \
        pytest tests/test_evaluator.py tests/test_app.py tests/test_infrastructure.py -v
else
    echo "Pytest not found and container not running, skipping pytest."
fi

echo -e "\n=== Running Multi-Language Integration Smoke Tests ==="

if kubectl get nodes &> /dev/null; then
    echo "Kubernetes cluster detected. Setting up port-forwards..."
    pkill -f "port-forward" || true
    sleep 1

    kubectl port-forward deployment/observability-python-app 30001:5000 > /dev/null 2>&1 &
    PYTHON_PF=$!
    kubectl port-forward deployment/observability-java-app 30005:8080 > /dev/null 2>&1 &
    JAVA_PF=$!
    kubectl port-forward deployment/observability-rust-app 30006:8081 > /dev/null 2>&1 &
    RUST_PF=$!
    kubectl port-forward deployment/observability-node-app 30007:8080 > /dev/null 2>&1 &
    NODE_PF=$!
    kubectl port-forward deployment/observability-go-app 30008:8080 > /dev/null 2>&1 &
    GO_PF=$!

    echo "Waiting for port-forwards to stabilize..."
    sleep 5

    python3 tests/smoke_test.py --k8s
    STATUS=$?

    kill $PYTHON_PF $JAVA_PF $RUST_PF $NODE_PF $GO_PF || true
    pkill -f "port-forward" || true
else
    echo "Testing against local Docker Compose endpoints..."
    python3 tests/smoke_test.py --docker
    STATUS=$?
fi

if [ $STATUS -eq 0 ]; then
    echo "=== VERIFICATION SUCCESSFUL ==="
else
    echo "=== VERIFICATION FAILED ==="
fi

exit $STATUS
