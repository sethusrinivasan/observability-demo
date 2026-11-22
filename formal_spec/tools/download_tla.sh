#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_JAR="${SCRIPT_DIR}/tla2tools.jar"

if [ -f "${TARGET_JAR}" ]; then
  echo "tla2tools.jar already present at ${TARGET_JAR}"
  exit 0
fi

echo "Downloading TLA+ Tools (v1.8.0)..."
curl -fsSL -o "${TARGET_JAR}" "https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar"
echo "Download complete: ${TARGET_JAR}"
