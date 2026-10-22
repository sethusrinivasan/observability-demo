#!/usr/bin/env bash
# deploy_aws.sh — Root wrapper forwarding to deployment/deploy_aws.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/deployment/deploy_aws.sh" "$@"
