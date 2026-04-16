#!/usr/bin/env bash
# launch.sh — full teardown then fresh deploy of the observability stack.
#
# Safe to run on a new machine or after any previous partial/broken deployment.
# Handles root-owned containers (from background docker compose runs) by
# killing their host PIDs before attempting compose teardown.
#
# Usage:
#   ./launch.sh           — full teardown + fresh deploy (wipes volumes)
#   ./launch.sh --keep-data — teardown + redeploy but keep existing volumes

set -euo pipefail

KEEP_DATA=false
[[ "${1:-}" == "--keep-data" ]] && KEEP_DATA=true

# ---------------------------------------------------------------------------
# Step 1: Force-kill any root-owned containers that compose can't stop,
#         then remove all known containers by name so compose starts clean.
# ---------------------------------------------------------------------------
echo "=== Clearing any existing containers ==="
for name in $(docker ps -aq --format '{{.Names}}' 2>/dev/null); do
    pid=$(docker inspect "$name" --format '{{.State.Pid}}' 2>/dev/null || echo "0")
    if [ "$pid" != "0" ] && [ -n "$pid" ]; then
        echo "  Stopping $name (PID $pid)..."
        sudo kill -9 "$pid" 2>/dev/null || true
    fi
done
sleep 3

# Force-remove all containers by name (handles both compose and manually started)
for name in observability-demo canary otel-collector tempo redpanda mimir loki \
            grafana grafana-renderer postgres postgres-exporter prometheus; do
    docker rm -f "$name" 2>/dev/null || true
done

# Also remove any remaining containers by ID
docker ps -aq | xargs -r docker rm -f 2>/dev/null || true

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
for name in observability-demo canary otel-collector tempo redpanda mimir loki \
            grafana grafana-renderer postgres postgres-exporter prometheus; do
    docker rm -f "$name" 2>/dev/null || true
done

# Remove stale anonymous volumes
docker volume prune -f 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 3: Build and launch
# ---------------------------------------------------------------------------
echo ""
echo "=== Building and launching stack ==="
docker compose up -d --build

# ---------------------------------------------------------------------------
# Step 3b: Restore Docker iptables forwarding rules
# Docker daemon restarts wipe iptables rules for existing bridges.
# After a fresh compose up the bridge is new so rules are set correctly,
# but if the daemon was restarted previously we may need to re-add them.
# We detect the active bridge and ensure FORWARD rules exist.
# ---------------------------------------------------------------------------
sleep 5
BRIDGE=$(docker network inspect observability-demo_observability-network \
    --format '{{.Id}}' 2>/dev/null | cut -c1-12)
BRIDGE_IF="br-${BRIDGE}"
if ip link show "$BRIDGE_IF" &>/dev/null; then
    # Check if the bridge already has a FORWARD rule
    if ! sudo iptables -C FORWARD -i "$BRIDGE_IF" -o "$BRIDGE_IF" -j ACCEPT 2>/dev/null; then
        echo "  Restoring iptables rules for $BRIDGE_IF..."
        sudo iptables -I DOCKER-CT 1 -o "$BRIDGE_IF" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
        sudo iptables -I DOCKER-CT 2 -i "$BRIDGE_IF" ! -o "$BRIDGE_IF" -j ACCEPT 2>/dev/null || true
        sudo iptables -I DOCKER-CT 3 -i "$BRIDGE_IF" -o "$BRIDGE_IF" -j ACCEPT 2>/dev/null || true
        sudo iptables -I FORWARD 1 -i "$BRIDGE_IF" -o "$BRIDGE_IF" -j ACCEPT 2>/dev/null || true
        echo "  iptables rules restored"
    else
        echo "  iptables rules already present"
    fi
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
wait_for "http://localhost:5000/"       "App"        40 3

# Grafana runs DB migrations on first boot — takes longer
echo -n "  Waiting for Grafana"
for i in $(seq 1 48); do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/api/health 2>/dev/null || echo "000")
    if [ "$code" = "200" ]; then echo " ✓"; break; fi
    echo -n "."; sleep 5
done

# Canary starts after app — just confirm it's running
sleep 5
canary_status=$(docker inspect canary --format '{{.State.Status}}' 2>/dev/null || echo "not found")
if [ "$canary_status" = "running" ]; then
    echo "  ✓ Canary"
else
    echo "  ✗ Canary (status: $canary_status)"
fi

# ---------------------------------------------------------------------------
# Step 5: Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Stack status ==="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | \
    grep -E "NAMES|observability-demo|canary|grafana|prometheus|tempo|loki|mimir|otel|postgres|redpanda"

echo ""
echo "  App:        http://localhost:5000"
echo "  Grafana:    http://localhost:3000"
echo "  Prometheus: http://localhost:9090"
echo "  Tempo:      http://localhost:3200"
echo "  Loki:       http://localhost:3100"
echo "  Mimir:      http://localhost:9009"
echo ""
echo "  Canary logs: docker logs -f canary"
echo "  App logs:    docker logs -f observability-demo"
