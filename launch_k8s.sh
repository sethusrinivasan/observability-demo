#!/usr/bin/env bash
# launch_k8s.sh — Root wrapper forwarding to deployment/launch_k8s.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/deployment/launch_k8s.sh" "$@"
