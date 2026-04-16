#!/usr/bin/env bash
# build.sh — build the observability-demo Docker image only (no compose)
set -euo pipefail

docker build -t observability-demo .
echo "Build complete: observability-demo"
