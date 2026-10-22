#!/usr/bin/env bash
# launch_docker.sh — Root wrapper forwarding to deployment/launch_docker.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/deployment/launch_docker.sh" "$@"
