#!/bin/bash
# redeploy.sh — replace app and canary containers with freshly built images.
# Handles root-owned containers by killing their host PIDs directly.

set -e

NETWORK=$(docker network ls --filter name=observability-network --format '{{.Name}}' | head -1)

kill_container() {
    local name="$1"
    local pid
    pid=$(docker inspect "$name" --format '{{.State.Pid}}' 2>/dev/null || echo "")
    if [ -n "$pid" ] && [ "$pid" != "0" ]; then
        echo "  Killing $name (PID $pid)..."
        sudo kill -9 "$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
        sleep 2
    fi
    docker rm -f "$name" 2>/dev/null || true
}

echo "=== Rebuilding images ==="
docker build -t observability-python-app .
docker build -t observability-canary ./canary

echo ""
echo "=== Replacing app container ==="
kill_container observability-python-app
docker run -d \
    --name observability-python-app \
    --network "$NETWORK" \
    --restart unless-stopped \
    -p 5000:5000 \
    -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
    -e OTEL_SERVICE_NAME=observability-python-app \
    -e POSTGRES_HOST=postgres \
    -e POSTGRES_PORT=5432 \
    -e POSTGRES_DB=observability \
    -e POSTGRES_USER=observability \
    -e POSTGRES_PASSWORD=observability \
    observability-python-app
echo "  observability-python-app started"

echo ""
echo "=== Replacing canary container ==="
kill_container canary
docker run -d \
    --name canary \
    --network "$NETWORK" \
    --restart unless-stopped \
    -e APP_BASE_URL=http://observability-python-app:5000 \
    -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
    -e CANARY_TPS=24 \
    observability-canary
echo "  canary started"

echo ""
echo "=== Waiting for app ==="
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/ 2>/dev/null || echo "000")
    if [ "$code" = "200" ]; then
        echo "  App is ready"
        break
    fi
    sleep 2
done

echo ""
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "NAME|observability-python-app|canary"
