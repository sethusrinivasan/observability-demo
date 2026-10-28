#!/usr/bin/env bash
# tests/run_all_tests.sh — Unified test runner for Observability Demo
set -e

# Navigate to project root if script called from elsewhere
cd "$(dirname "$0")/.."

show_help() {
    cat <<EOF
Usage: ./test.sh [OPTIONS]

Unified test runner for Observability Demo.

Options:
  --unit        Run Python unit and logic tests (evaluator, sql guard, canary features)
  --infra       Run infrastructure tests (Postgres, Redis, Tempo, etc.)
  --smoke       Run multi-language microservice smoke tests against local stack
  --live        Run live showcase end-to-end tests
  --audit       Run AWS Canary and Telemetry Lake live audit
  --all         Run all test suites sequentially
  -h, --help    Show this help message

With no options, runs unit tests and automatically detects running local containers/clusters for smoke tests.
EOF
    exit 0
}

MODE="auto"
EXTRA_PYTEST_ARGS=()

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            show_help
            ;;
        --unit)
            MODE="unit"
            ;;
        --infra)
            MODE="infra"
            ;;
        --smoke)
            MODE="smoke"
            ;;
        --live)
            MODE="live"
            ;;
        --audit)
            MODE="audit"
            ;;
        --all)
            MODE="all"
            ;;
        *)
            EXTRA_PYTEST_ARGS+=("$arg")
            ;;
    esac
done

run_pytest() {
    local targets=("$@")
    if [ -x ".venv/bin/pytest" ]; then
        .venv/bin/pytest "${targets[@]}" "${EXTRA_PYTEST_ARGS[@]}"
    elif command -v pytest &> /dev/null; then
        pytest "${targets[@]}" "${EXTRA_PYTEST_ARGS[@]}"
    elif python3 -m pytest --version &> /dev/null; then
        python3 -m pytest "${targets[@]}" "${EXTRA_PYTEST_ARGS[@]}"
    else
        echo "Error: pytest is not installed. Run ./setup.sh or pip install -r requirements-dev.txt"
        return 1
    fi
}

run_smoke() {
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
        kubectl port-forward deployment/observability-dotnet-app 30009:8080 > /dev/null 2>&1 &
        DOTNET_PF=$!
        kubectl port-forward deployment/observability-c-app 30010:8080 > /dev/null 2>&1 &
        C_PF=$!
        kubectl port-forward deployment/observability-php-app 30011:8080 > /dev/null 2>&1 &
        PHP_PF=$!
        kubectl port-forward deployment/observability-swift-app 30012:8080 > /dev/null 2>&1 &
        SWIFT_PF=$!

        echo "Waiting for port-forwards to stabilize..."
        sleep 5

        python3 tests/smoke_test.py --k8s
        local status=$?

        kill $PYTHON_PF $JAVA_PF $RUST_PF $NODE_PF $GO_PF $DOTNET_PF $C_PF $PHP_PF $SWIFT_PF 2>/dev/null || true
        pkill -f "port-forward" 2>/dev/null || true
        return $status
    else
        echo "Testing against local Docker Compose endpoints..."
        python3 tests/smoke_test.py --docker
    fi
}

case "$MODE" in
    unit)
        echo "=== Running Python Unit Tests ==="
        run_pytest tests/test_evaluator.py tests/test_sql_guard.py tests/test_canary_features.py tests/test_query_fingerprints_and_plan.py tests/test_rds_cost_breakdown.py tests/test_aws_service_quotas.py -v
        ;;
    infra)
        echo "=== Running Infrastructure Tests ==="
        run_pytest tests/test_infrastructure.py -v
        ;;
    smoke)
        echo "=== Running Multi-Language Integration Smoke Tests ==="
        run_smoke
        ;;
    live)
        echo "=== Running Live Showcase Tests ==="
        run_pytest tests/test_live_showcase.py -v
        ;;
    audit)
        echo "=== Running AWS Canary & Lake Audit Tests ==="
        run_pytest tests/test_aws_canary_audit.py -v
        ;;
    all)
        echo "=== Running All Test Suites ==="
        run_pytest tests/test_evaluator.py tests/test_sql_guard.py tests/test_canary_features.py tests/test_query_fingerprints_and_plan.py tests/test_rds_cost_breakdown.py tests/test_aws_service_quotas.py -v
        run_smoke
        run_pytest tests/test_live_showcase.py tests/test_aws_canary_audit.py -v
        ;;
    auto)
        echo "=== Running Python Unit Tests ==="
        run_pytest tests/test_evaluator.py tests/test_sql_guard.py tests/test_canary_features.py tests/test_query_fingerprints_and_plan.py tests/test_rds_cost_breakdown.py tests/test_aws_service_quotas.py -v

        if kubectl get nodes &> /dev/null || (command -v docker &>/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "observability-"); then
            echo -e "\n=== Running Multi-Language Smoke Tests (Active Stack Detected) ==="
            run_smoke
        else
            echo -e "\n(Local containers/K8s cluster not currently running; skipping smoke tests. Use ./start.sh to launch stack)"
        fi
        ;;
esac

echo -e "\n=== All Requested Tests Passed Successfully ==="
