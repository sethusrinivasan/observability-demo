#!/bin/bash
# redeploy.sh — replace app and canary containers with freshly built images.
# Handles root-owned containers by killing their host PIDs directly.

set -e

kill_container() {
    local name="$1"
    local pid
    pid=$(docker inspect "$name" --format '{{.State.Pid}}' 2>/dev/null || echo "")
    if [ -n "$pid" ] && [ "$pid" != "0" ]; then
        echo "  Killing $name (PID $pid)..."
        kill -9 "$pid" 2>/dev/null || true
        sleep 2
    fi
    docker rm -f "$name" 2>/dev/null || true
}

echo "=== Rebuilding images ==="
docker build -t observability-demo-observability-demo .
docker build -t observability-demo-canary ./canary

echo ""
echo "=== Replacing app container ==="
kill_container observability-demo
docker run -d \
    --name observability-demo \
    --network observability-demo_observability-network \
    --restart unless-stopped \
    -p 5000:5000 \
    -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
    -e OTEL_SERVICE_NAME=observability-demo \
    -e POSTGRES_HOST=postgres \
    -e POSTGRES_PORT=5432 \
    -e POSTGRES_DB=observability \
    -e POSTGRES_USER=observability \
    -e POSTGRES_PASSWORD=observability \
    observability-demo-observability-demo
echo "  observability-demo started"

echo ""
echo "=== Replacing canary container ==="
kill_container canary
docker run -d \
    --name canary \
    --network observability-demo_observability-network \
    --restart unless-stopped \
    -e APP_BASE_URL=http://observability-demo:5000 \
    -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
    -e CANARY_TPS=24 \
    observability-demo-canary
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
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "NAME|observability-demo|canary"
