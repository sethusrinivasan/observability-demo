#!/usr/bin/env bash
# launch.sh — full teardown then fresh deploy of the observability stack.
#
# Safe to run on a new machine or after any previous partial/broken deployment.
# Stops existing containers through the Docker API before compose teardown.
#
# Run with no arguments (or --help) to see this usage message.

set -euo pipefail
cd "$(dirname "$0")"

# Check Docker permissions
if ! docker ps >/dev/null 2>&1; then
    echo "ERROR: Cannot run Docker commands. Please ensure:"
    echo "1. Docker is installed and running"
    echo "2. Current user is in the 'docker' group: sudo usermod -aG docker \$USER"
    echo "3. Run 'newgrp docker' after adding to group, or log out and back in"
    echo "4. Or run this script with sudo (not recommended)"
    exit 1
fi

KEEP_DATA=false
TEARDOWN_ONLY=false
RUN=false
ENABLE_TUNNEL=false

for arg in "$@"; do
    case "$arg" in
        --run)           RUN=true ;;
        --keep-data)     KEEP_DATA=true ;;
        --teardown-only) TEARDOWN_ONLY=true ;;
        --tunnel)        ENABLE_TUNNEL=true ;;
        --help|-h)       ;; # handled below
    esac
done

# ---------------------------------------------------------------------------
# Print help and exit when no meaningful action is requested
# ---------------------------------------------------------------------------
if [ "$RUN" = false ] && [ "$TEARDOWN_ONLY" = false ]; then
    cat <<'EOF'
Usage: launch_docker.sh [OPTIONS]

Options:
  --run              Perform a full teardown then build and launch the stack
                     (wipes all volumes by default)
  --run --keep-data  Teardown and redeploy but preserve existing data volumes
  --tunnel           Enable Cloudflare showcase tunnel (Zero-port-forwarding)
  --teardown-only    Stop and remove all containers/networks/volumes, then exit
  --help, -h         Show this help message and exit

Examples:
  ./launch_docker.sh --run                 # fresh deploy (clears all data)
  ./launch_docker.sh --run --keep-data     # redeploy, keep Postgres/Mimir/Loki data
  ./launch_docker.sh --run --tunnel        # deploy with live Cloudflare showcase tunnel
  ./launch_docker.sh --teardown-only       # clean shutdown with no redeploy
EOF
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 1: Stop and remove this project's containers so compose starts clean.
# ---------------------------------------------------------------------------
echo "=== Clearing any existing containers ==="
ids=$(docker compose ps -aq 2>/dev/null || true)
if [ -n "$ids" ]; then
    docker stop $ids >/dev/null 2>&1 || true
    docker rm -f $ids >/dev/null 2>&1 || true
fi

# Force-remove known project containers by name (compose and the showcase tunnel).
for name in observability-python-app observability-java-app observability-rust-app observability-node-app observability-go-app observability-dotnet-app observability-c-app \
            canary otel-collector tempo redpanda mimir loki \
            grafana grafana-renderer postgres postgres-exporter prometheus valkey redis-exporter \
            cloudflared cloudflared-showcase; do
    docker rm -f "$name" 2>/dev/null || true
done

# ---------------------------------------------------------------------------
# Step 2: Compose teardown — removes containers, networks, optionally volumes
# ---------------------------------------------------------------------------
echo ""
echo "=== Tearing down compose stack ==="
if [ "$KEEP_DATA" = true ]; then
    docker compose down --remove-orphans 2>/dev/null || true
    echo "  (volumes preserved)"
else
    docker compose down --volumes --remove-orphans 2>/dev/null || true
    echo "  (volumes wiped)"
fi

# Remove any leftover named containers that compose missed (e.g. manually
# started containers or those whose PIDs were killed above)
for name in observability-python-app observability-java-app observability-rust-app observability-node-app observability-go-app observability-dotnet-app observability-c-app \
            canary otel-collector tempo redpanda mimir loki \
            grafana grafana-renderer postgres postgres-exporter prometheus valkey redis-exporter \
            cloudflared cloudflared-showcase; do
    docker rm -f "$name" 2>/dev/null || true
done

# Exit here if --teardown-only was requested
if [ "$TEARDOWN_ONLY" = true ]; then
    echo ""
    echo "=== Teardown complete (--teardown-only; skipping build and launch) ==="
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 3: Build and launch
# ---------------------------------------------------------------------------
echo ""
echo "=== Building and launching stack ==="
if [ "$ENABLE_TUNNEL" = true ] || [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
    echo "  (Cloudflare showcase tunnel profile enabled)"
    docker compose --profile tunnel up -d --build
else
    docker compose up -d --build
fi

# ---------------------------------------------------------------------------
# Step 4: Wait for all services to be ready
# ---------------------------------------------------------------------------
echo ""
echo "=== Waiting for services ==="

wait_for() {
    local url="$1" name="$2" retries="${3:-30}" interval="${4:-3}"
    for i in $(seq 1 "$retries"); do
        code=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "000")
        if [ "$code" = "200" ]; then
            echo "  ✓ $name"
            return 0
        fi
        sleep "$interval"
    done
    echo "  ✗ $name (timed out after $((retries * interval))s)"
    return 1
}

wait_for "http://localhost:3200/ready"  "Tempo"      60 5
wait_for "http://localhost:3100/ready"  "Loki"       30 3
wait_for "http://localhost:9009/ready"  "Mimir"      30 3
wait_for "http://localhost:9090/-/ready" "Prometheus" 20 3
wait_for "http://localhost:5000/"       "Python"     40 3
wait_for "http://localhost:8080/actuator/health/liveness" "Java" 40 3
wait_for "http://localhost:8083/"       "Rust"       40 3
wait_for "http://localhost:8084/actuator/health/liveness" "Node" 40 3
wait_for "http://localhost:8086/actuator/health/liveness" "Go"   40 3
wait_for "http://localhost:8087/actuator/health/liveness" ".NET" 40 3
wait_for "http://localhost:8088/actuator/health/liveness" "C" 40 3

# Grafana runs DB migrations on first boot — takes longer
echo -n "  Waiting for Grafana"
grafana_ok=false
for i in $(seq 1 48); do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/api/health 2>/dev/null || echo "000")
    if [ "$code" = "200" ]; then echo " ✓"; grafana_ok=true; break; fi
    echo -n "."; sleep 5
done
if [ "$grafana_ok" = false ]; then
    echo " ✗"
fi

# Canary starts after app — just confirm it's running
sleep 5
canary_status=$(docker inspect canary --format '{{.State.Status}}' 2>/dev/null || echo "not found")
if [ "$canary_status" = "running" ]; then
    echo "  ✓ Canary"
    canary_cookie="$(mktemp)"
    curl -s -m 5 -c "$canary_cookie" -X POST http://localhost:8085/api/login \
        -H 'Content-Type: application/json' \
        -d '{"username":"demouser","password":"demo"}' >/dev/null || true
    curl -s -m 5 -b "$canary_cookie" -X POST http://localhost:8085/api/markers \
        -H 'Content-Type: application/json' \
        -d '{"kind":"deploy","target":"stack","tag":"deploy"}' >/dev/null || true
    rm -f "$canary_cookie"
else
    echo "  ✗ Canary (status: $canary_status)"
fi

# ---------------------------------------------------------------------------
# Step 5: Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Stack status ==="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | \
    grep -E "NAMES|observability-|canary|grafana|prometheus|tempo|loki|mimir|otel|postgres|redpanda|valkey|redis-exporter"

echo ""
echo "  Python:     http://localhost:5000"
echo "  Java:       http://localhost:8080"
echo "  Rust:       http://localhost:8083"
echo "  Node:       http://localhost:8084"
echo "  Go:         http://localhost:8086"
echo "  .NET:       http://localhost:8087"
echo "  C:          http://localhost:8088"
echo "  Grafana:    http://localhost:3000  (admin / admin)"
echo "  Prometheus: http://localhost:9090"
echo "  Tempo:      http://localhost:3200"
echo "  Loki:       http://localhost:3100"
echo "  Mimir:      http://localhost:9009"
if [ -f .showcase/url ]; then
    echo "  Showcase:   $(head -n 1 .showcase/url)"
    echo "              Dependency links on that page use /open/<service>/, not :port"
fi
if [ "$ENABLE_TUNNEL" = true ] || [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
    echo "  Showcase:   Cloudflare Tunnel active (see ./scripts/showcase_tunnel.sh --status)"
else
    echo "  Showcase:   Run ./scripts/showcase_tunnel.sh --quick for instant public HTTPS"
fi
echo ""
echo "  Canary logs: docker logs -f canary"
