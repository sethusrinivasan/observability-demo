#!/usr/bin/env bash
# tests/test_with_aws_emulator.sh
# Automated test runner for Observability Demo using the local AWS Emulator (https://aws.amazon.com/about-aws/whats-new/2026/11/aws-emulator-now-available/)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

PORT="${EMULATOR_PORT:-9297}"
BIND_ADDR="${EMULATOR_BIND:-127.0.0.1}"
ENDPOINT="https://${BIND_ADDR}:${PORT}"
EMU_DIR="${HOME}/.aws-emulator"
BIN_PATH="${HOME}/.local/bin/aws-emulator"

echo "====================================================================="
echo "  AWS Emulator Automated Test Suite"
echo "  Target: ${ENDPOINT}"
echo "====================================================================="

# Ensure telemetry is completely disabled across all layers
mkdir -p "${HOME}/.aws/emulator"
cat << 'EOF' > "${HOME}/.aws/emulator/config.toml"
[telemetry]
enabled = false
EOF

export EMULATOR_TELEMETRY="off"
export AWS_CLI_TELEMETRY_OPTOUT="1"
export AWS_CSM_ENABLED="false"

# 1. Check or install aws-emulator binary
if [ ! -x "${BIN_PATH}" ] && ! command -v aws-emulator &>/dev/null; then
    echo "⚙️ aws-emulator not found in PATH. Checking local installation..."
    if [ -x "${EMU_DIR}/opt/aws-emulator/bin/aws-emulator" ]; then
        mkdir -p "${HOME}/.local/bin"
        ln -sf "${EMU_DIR}/opt/aws-emulator/bin/aws-emulator" "${BIN_PATH}"
    else
        echo "⬇️ Downloading AWS Emulator LTS package from downloads.aws.com..."
        mkdir -p "${EMU_DIR}"
        TEMP_DEB="/tmp/aws-emulator-install-$$.deb"
        ARCH="$(uname -m)"
        if [ "${ARCH}" = "aarch64" ] || [ "${ARCH}" = "arm64" ]; then
            PKG_ARCH="arm64"
        else
            PKG_ARCH="amd64"
        fi
        curl -fsSL "https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator_${PKG_ARCH}.deb" -o "${TEMP_DEB}"
        dpkg -x "${TEMP_DEB}" "${EMU_DIR}"
        rm -f "${TEMP_DEB}"
        mkdir -p "${HOME}/.local/bin"
        ln -sf "${EMU_DIR}/opt/aws-emulator/bin/aws-emulator" "${BIN_PATH}"
        echo "✅ Installed AWS Emulator to ${BIN_PATH}"
    fi
fi

if command -v aws-emulator &>/dev/null; then
    EMU_EXEC="aws-emulator"
else
    EMU_EXEC="${BIN_PATH}"
fi

echo "🔍 AWS Emulator Version: $("${EMU_EXEC}" --version 2>&1 | tr '\n' ' ')"

# 2. Check if emulator is already running or start a fresh instance
STARTED_EMU=0
if curl -sk "${ENDPOINT}/__admin/health" | grep -q "ok"; then
    echo "⚡ Detected existing AWS Emulator running on ${ENDPOINT}"
else
    echo "🚀 Launching AWS Emulator on port ${PORT} (bind: ${BIND_ADDR})..."
    "${EMU_EXEC}" -b "${BIND_ADDR}" -p "${PORT}" --telemetry off -a off &
    EMU_PID=$!
    STARTED_EMU=1

    # Cleanup hook
    cleanup() {
        if [ "${STARTED_EMU}" -eq 1 ] && [ -n "${EMU_PID:-}" ] && kill -0 "${EMU_PID}" 2>/dev/null; then
            echo "🛑 Stopping AWS Emulator (PID: ${EMU_PID})..."
            kill "${EMU_PID}" 2>/dev/null || true
            wait "${EMU_PID}" 2>/dev/null || true
        fi
    }
    trap cleanup EXIT INT TERM

    echo "⏳ Waiting for AWS Emulator health check..."
    READY=0
    for i in {1..20}; do
        if curl -sk "${ENDPOINT}/__admin/health" | grep -q "ok"; then
            READY=1
            break
        fi
        sleep 0.5
    done

    if [ "${READY}" -ne 1 ]; then
        echo "❌ AWS Emulator failed to start within 10 seconds."
        exit 1
    fi
    echo "✅ AWS Emulator is healthy at ${ENDPOINT}/__admin/health"
fi

# 3. Export SDK & CLI environment variables
CA_PATH="${HOME}/.aws/emulator/${PORT}/ca.pem"
export AWS_ENDPOINT_URL="${ENDPOINT}"
export AWS_DEFAULT_REGION="us-east-2"
export AWS_REGION="us-east-2"
export AWS_ACCESS_KEY_ID="test"
export AWS_SECRET_ACCESS_KEY="test"

if [ -f "${CA_PATH}" ]; then
    export AWS_CA_BUNDLE="${CA_PATH}"
    export SSL_CERT_FILE="${CA_PATH}"
fi

echo "📋 Testing environment configured:"
echo "   AWS_ENDPOINT_URL = ${AWS_ENDPOINT_URL}"
echo "   AWS_REGION       = ${AWS_REGION}"
if [ -f "${CA_PATH}" ]; then
    echo "   AWS_CA_BUNDLE    = ${AWS_CA_BUNDLE}"
fi

# 4. Run Pytest against the emulator
PYTEST_BIN=""
if [ -x "${REPO_ROOT}/.venv/bin/pytest" ]; then
    PYTEST_BIN="${REPO_ROOT}/.venv/bin/pytest"
elif command -v pytest &>/dev/null; then
    PYTEST_BIN="pytest"
else
    PYTEST_BIN="python3 -m pytest"
fi

echo "🧪 Running test suite with AWS Emulator..."
"${PYTEST_BIN}" tests/test_aws_emulator.py "$@"

echo ""
echo "🎉 AWS Emulator test execution completed successfully!"
