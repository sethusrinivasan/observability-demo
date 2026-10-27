#!/usr/bin/env bash
# test.sh — Root test runner forwarding to tests/run_all_tests.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/tests/run_all_tests.sh" "$@"
