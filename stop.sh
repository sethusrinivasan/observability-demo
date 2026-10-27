#!/usr/bin/env bash
# stop.sh — Stop the local observability stack while preserving data volumes.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# Auto-elevate group permissions if user is member of docker group in /etc/group
if ! docker ps >/dev/null 2>&1 && command -v sg >/dev/null 2>&1 && sg docker -c "docker ps" >/dev/null 2>&1; then
    exec sg docker -c "\"$0\" $*"
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker is not installed or not in PATH."
    exit 1
fi

echo "Stopping observability stack..."
docker compose --profile tunnel --profile showcase down --remove-orphans

echo "✓ Stack stopped. Database and telemetry volumes were preserved."
echo "To clean up and wipe volumes: ./launch_docker.sh --teardown-only"
