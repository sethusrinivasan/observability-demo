#!/usr/bin/env bash
# launch.sh — full teardown then fresh deploy of the observability stack.
# Use this for a clean-slate startup (e.g. on a new machine or after config changes).
set -euo pipefail

echo "Tearing down any existing stack..."
docker compose down --remove-orphans

echo "Building and launching stack..."
docker compose up -d --build

echo ""
echo "Waiting for services to be ready..."
for service in \
    "http://localhost:5000/|App" \
    "http://localhost:3200/ready|Tempo" \
    "http://localhost:3100/ready|Loki" \
    "http://localhost:9009/ready|Mimir" \
    "http://localhost:9090/-/ready|Prometheus"; do
    url="${service%%|*}"
    name="${service##*|}"
    for i in $(seq 1 30); do
        code=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "000")
        if [ "$code" = "200" ]; then
            echo "  ✓ $name"
            break
        fi
        sleep 3
    done
done

# Grafana takes longer (DB migrations on fresh volume)
echo -n "  Waiting for Grafana"
for i in $(seq 1 40); do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/api/health 2>/dev/null || echo "000")
    if [ "$code" = "200" ]; then
        echo " ✓"
        break
    fi
    echo -n "."
    sleep 5
done

echo ""
echo "Stack is ready."
echo "  App:        http://localhost:5000"
echo "  Grafana:    http://localhost:3000"
echo "  Prometheus: http://localhost:9090"
echo "  Tempo:      http://localhost:3200"
echo "  Loki:       http://localhost:3100"
echo "  Mimir:      http://localhost:9009"
echo ""
echo "Canary is running in its own container (docker logs canary)"
