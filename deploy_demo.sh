#!/usr/bin/env bash
# deploy_demo.sh — Root wrapper forwarding to deployment/deploy_demo.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/deployment/deploy_demo.sh" "$@"
