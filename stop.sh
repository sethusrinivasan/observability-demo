#!/usr/bin/env bash
# Stop the local stack and keep data volumes.
set -euo pipefail
cd "$(dirname "$0")"

if ! docker ps >/dev/null 2>&1; then
    echo "ERROR: Cannot run Docker commands. Is the daemon up, and is this user in the docker group?"
    exit 1
fi

docker compose --profile tunnel --profile showcase down --remove-orphans
echo "Stack stopped. Volumes were kept."
echo "To delete volumes as well: ./launch_docker.sh --teardown-only"
