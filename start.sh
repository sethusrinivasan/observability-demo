#!/usr/bin/env bash
# start.sh — Bring up the full polyglot observability stack in the background.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: Docker or Docker Compose plugin is not installed or not in PATH."
    echo "Run ./setup.sh first to configure prerequisites on this machine."
    exit 1
fi

echo "================================================================="
echo "   🚀 Starting Polyglot Observability Playground"
echo "================================================================="
docker compose up -d --build --remove-orphans

echo ""
echo "=== Microservices & Observability Stack ==="
docker compose ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || docker compose ps

echo ""
echo "  Canary Dashboard:  http://localhost:8085  (Main Operations & Chaos Center)"
echo "  Grafana LGTM:      http://localhost:3000  (admin / admin)"
echo "  Prometheus:        http://localhost:9090"
echo "  Tempo:             http://localhost:3200"
echo "  Loki:              http://localhost:3100"
echo "  Mimir:             http://localhost:9009"
echo ""
echo "  Polyglot Microservice Endpoints:"
echo "    Python:          http://localhost:5000"
echo "    Java:            http://localhost:8080"
echo "    Rust:            http://localhost:8083"
echo "    Node.js:         http://localhost:8084"
echo "    Go:              http://localhost:8086"
echo "    .NET:            http://localhost:8087"
echo "    C:               http://localhost:8088"
echo "    PHP:             http://localhost:8089"
echo "    Swift:           http://localhost:8091"
echo ""
echo "  Helpful Shortcuts:"
echo "    Smoke test stack:  ./test.sh"
echo "    Live Showcase:     ./scripts/showcase_tunnel.sh --quick"
echo "    Stop containers:   ./stop.sh"
