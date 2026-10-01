#!/usr/bin/env bash
# showcase_tunnel.sh — Zero-port-forwarding public showcase tunnel via Cloudflare
#
# Enables a live public showcase on residential broadband without
# router port-forwarding, static IPs, or exposing home network ports.
#
# Modes:
#   1. Quick Tunnel (TryCloudflare) — Instant public HTTPS URL, zero account needed.
#   2. Permanent Named Tunnel (Cloudflare Zero Trust) — Custom domain via token.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
NETWORK_NAME="observability-demo_observability-network"
CONTAINER_NAME="cloudflared-showcase"

print_header() {
    echo "================================================================="
    echo "   🚀 Cloudflare Tunnel — Live Showcase Bridge"
    echo "================================================================="
}

show_help() {
    print_header
    cat <<'EOF'
Usage: ./scripts/showcase_tunnel.sh [MODE / OPTIONS]

Modes:
  --quick            Launch an instant, free TryCloudflare public HTTPS tunnel
                     (No Cloudflare account or domain required).
  --token <TOKEN>    Start a permanent named Cloudflare Zero Trust tunnel using
                     your tunnel token from the Cloudflare dashboard.
  --status           Check the running state and active URL of the tunnel.
  --stop             Stop and remove the running showcase tunnel container.
  --help, -h         Display this help message.

Environment Variables:
  CLOUDFLARE_TUNNEL_TOKEN   If set in environment or .env, running with no args
                            or --token will use this token automatically.

Examples:
  ./scripts/showcase_tunnel.sh --quick
  ./scripts/showcase_tunnel.sh --token eyJhIjoi...
  ./scripts/showcase_tunnel.sh --status
  ./scripts/showcase_tunnel.sh --stop
EOF
    exit 0
}

# Load .env if present
if [ -f "${ROOT_DIR}/.env" ]; then
    # shellcheck disable=SC2046
    export $(grep -v '^#' "${ROOT_DIR}/.env" | xargs -r) 2>/dev/null || true
fi

MODE="${1:-}"

if [ -z "$MODE" ] || [ "$MODE" = "--help" ] || [ "$MODE" = "-h" ]; then
    show_help
fi

case "$MODE" in
    --stop)
        print_header
        echo "Stopping showcase tunnel..."
        docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
        docker compose -f "${ROOT_DIR}/docker-compose.yaml" --profile tunnel stop cloudflared 2>/dev/null || true
        echo "✓ Showcase tunnel stopped."
        exit 0
        ;;

    --status)
        print_header
        if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            echo "Status: Quick Tunnel RUNNING (Container: $CONTAINER_NAME)"
            Q_URL=$(docker logs "$CONTAINER_NAME" 2>&1 | grep -o 'https://[a-zA-Z0-9.-]*\.trycloudflare\.com' | head -n 1 || true)
            if [ -n "$Q_URL" ]; then
                echo "  Public URL: $Q_URL"
            else
                echo "  (Public URL provisioning in progress... check logs: docker logs $CONTAINER_NAME)"
            fi
        elif docker ps --format '{{.Names}}' | grep -q "^cloudflared$"; then
            echo "Status: Permanent Named Tunnel RUNNING (Container: cloudflared)"
            docker logs --tail 10 cloudflared 2>&1 | tail -n 5
        else
            echo "Status: STOPPED (No active tunnel container)"
        fi
        exit 0
        ;;

    --token)
        TOKEN="${2:-${CLOUDFLARE_TUNNEL_TOKEN:-}}"
        if [ -z "$TOKEN" ]; then
            echo "ERROR: Missing tunnel token. Pass it via: ./scripts/showcase_tunnel.sh --token <TOKEN>"
            echo "Or set CLOUDFLARE_TUNNEL_TOKEN in .env"
            exit 1
        fi
        print_header
        echo "Starting permanent Cloudflare Zero Trust tunnel via Docker Compose..."
        export CLOUDFLARE_TUNNEL_TOKEN="$TOKEN"
        cd "${ROOT_DIR}"
        docker compose --profile tunnel up -d cloudflared
        echo "✓ Cloudflare Tunnel is connected to your Cloudflare Zero Trust network."
        echo "  Configure your public domain hostnames in the Cloudflare Zero Trust dashboard:"
        echo "    - showcase.yourdomain.com -> http://canary:8085"
        echo "    - grafana.yourdomain.com  -> http://grafana:3000"
        exit 0
        ;;

    --quick)
        print_header
        echo "Initializing instant TryCloudflare quick tunnel for Canary dashboard..."

        # Verify docker network exists
        if ! docker network ls --format '{{.Name}}' | grep -q "^${NETWORK_NAME}$"; then
            echo "ERROR: Observability Docker network ($NETWORK_NAME) not found."
            echo "Please ensure the demo stack is running first: ./launch_docker.sh --run"
            exit 1
        fi

        # Stop existing quick tunnel if running
        docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

        echo "Launching cloudflared container connected to ${NETWORK_NAME}..."
        docker run -d \
            --name "$CONTAINER_NAME" \
            --restart unless-stopped \
            --network "$NETWORK_NAME" \
            cloudflare/cloudflared:latest \
            tunnel --no-autoupdate --url http://canary:8085 >/dev/null

        echo -n "Waiting for public HTTPS tunnel URL to be provisioned"
        TUNNEL_URL=""
        for _ in $(seq 1 20); do
            sleep 1
            echo -n "."
            URL_LINE=$(docker logs "$CONTAINER_NAME" 2>&1 | grep -o 'https://[a-zA-Z0-9.-]*\.trycloudflare\.com' | head -n 1 || true)
            if [ -n "$URL_LINE" ]; then
                TUNNEL_URL="$URL_LINE"
                break
            fi
        done
        echo ""

        if [ -n "$TUNNEL_URL" ]; then
            echo ""
            echo "================================================================="
            echo " 🎉 LIVE PUBLIC SHOWCASE URL READY!"
            echo "================================================================="
            echo ""
            echo "  Public HTTPS URL:  $TUNNEL_URL"
            echo "  Target Service:    Canary Telemetry & Load Dashboard (port 8085)"
            echo ""
            echo "  Features:"
            echo "    ✓ End-to-end HTTPS with valid TLS certificate"
            echo "    ✓ Completely bypasses inbound port blocks & CGNAT from your internet service provider"
            echo "    ✓ Zero open router ports on your home gateway"
            echo "    ✓ Accessible from anywhere in the world on mobile & desktop"
            echo ""
            echo "  To view logs:    docker logs -f $CONTAINER_NAME"
            echo "  To stop tunnel:  ./scripts/showcase_tunnel.sh --stop"
            echo "================================================================="
        else
            echo "WARNING: Tunnel URL took longer than expected to appear."
            echo "Check logs manually: docker logs $CONTAINER_NAME"
        fi
        exit 0
        ;;

    *)
        echo "Unknown option: $MODE"
        show_help
        ;;
esac
