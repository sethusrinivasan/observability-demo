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
SHOWCASE_URL_FILE="${ROOT_DIR}/.showcase/url"

save_showcase_url() {
    mkdir -p "${ROOT_DIR}/.showcase"
    printf '%s\n' "$1" > "$SHOWCASE_URL_FILE"
}

clear_showcase_url() {
    rm -f "$SHOWCASE_URL_FILE"
}

publish_pages_demo_link() {
    local url="${1%/}/"
    local pages_repo="${SHOWCASE_PAGES_REPO:-${HOME}/sethusrinivasan.github.io}"
    local work=""
    local result

    if [ ! -d "${pages_repo}/.git" ] && [ ! -f "${pages_repo}/.git" ]; then
        echo "  Pages repo not found at ${pages_repo}."
        echo "  Set SHOWCASE_PAGES_REPO to publish the live demo link."
        return 0
    fi

    git -C "$pages_repo" fetch origin main
    work="$(mktemp -d)"
    git -C "$pages_repo" worktree add --detach "$work" origin/main >/dev/null

    result="$(python3 - "${work}/index.html" "$url" <<'PY'
import pathlib, re, sys
path, url = sys.argv[1], sys.argv[2]
file = pathlib.Path(path)
text = file.read_text()
pattern = re.compile(
    r'(<a href="https://github.com/sethusrinivasan/observability-demo">.*?<a class="live" href=")[^"]+(">)',
    re.DOTALL,
)
def repl(match):
    return match.group(1) + url + match.group(2)
new, count = pattern.subn(repl, text, count=1)
if count != 1:
    raise SystemExit("observability-demo Live Demo link was not found")
if new == text:
    print("unchanged")
else:
    file.write_text(new)
    print("updated")
PY
)" || {
        git -C "$pages_repo" worktree remove --force "$work" >/dev/null 2>&1 || true
        rm -rf "$work"
        echo "  Could not update the live demo link in ${pages_repo}."
        return 0
    }
    if [ "$result" = "unchanged" ]; then
        git -C "$pages_repo" worktree remove --force "$work" >/dev/null 2>&1 || true
        rm -rf "$work"
        echo "  Live demo link already points at ${url}"
        return 0
    fi

    git -C "$work" add index.html
    GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-Sethu Srinivasan}" \
    GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-sethusrinivasan@users.noreply.github.com}" \
    GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-Sethu Srinivasan}" \
    GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-sethusrinivasan@users.noreply.github.com}" \
    git -C "$work" commit -m "Point the observability-demo live demo at the current showcase tunnel."
    git -C "$work" push origin HEAD:main
    git -C "$pages_repo" worktree remove --force "$work" >/dev/null 2>&1 || true
    rm -rf "$work"
    if git -C "$pages_repo" diff --quiet && git -C "$pages_repo" diff --cached --quiet; then
        git -C "$pages_repo" merge --ff-only origin/main >/dev/null 2>&1 || true
    fi
    echo "  Published live demo link: ${url}"
}

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
  SHOWCASE_PAGES_REPO       GitHub Pages checkout whose observability-demo
                            Live Demo link is updated after --quick.
                            Defaults to ~/sethusrinivasan.github.io.
                            A changed link is committed and pushed.

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
        clear_showcase_url
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
                save_showcase_url "$Q_URL"
                echo "  Saved for the dashboard: $SHOWCASE_URL_FILE"
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
        clear_showcase_url

        echo "Launching cloudflared container connected to ${NETWORK_NAME}..."
        docker run -d \
            --name "$CONTAINER_NAME" \
            --restart unless-stopped \
            --network "$NETWORK_NAME" \
            cloudflare/cloudflared:2026.9.3 \
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
            save_showcase_url "$TUNNEL_URL"
            echo "  Public HTTPS URL:  $TUNNEL_URL"
            echo "  Saved to:          $SHOWCASE_URL_FILE"
            publish_pages_demo_link "$TUNNEL_URL" || echo "  WARNING: the public site link was not updated."
            echo "  Target Service:    Canary Telemetry & Load Dashboard (port 8085)"
            echo "  Other services:    ${TUNNEL_URL}/open/<service>/  (not ${TUNNEL_URL}:port)"
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
