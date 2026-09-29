#!/usr/bin/env bash
# start.sh — bring the full observability stack up in the background.
# Removes orphaned containers from previous runs before starting.
set -euo pipefail

echo "Starting observability stack..."
docker compose up -d --build --remove-orphans
echo ""
echo "Services:"
docker compose ps
echo ""
echo "Python:     http://localhost:5000"
echo "Java:       http://localhost:8080"
echo "Rust:       http://localhost:8083"
echo "Grafana:    http://localhost:3000  (admin / admin)"
echo "Prometheus: http://localhost:9090"
echo "Canary:     docker logs canary"
