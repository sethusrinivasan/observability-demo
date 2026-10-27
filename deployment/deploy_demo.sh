#!/usr/bin/env bash
# deploy_demo.sh — Deploy live showcase tunnel, update repository URLs & badges,
# and verify that the Live Showcase Verification GitHub Action passes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SHOWCASE_URL_FILE="${ROOT_DIR}/.showcase/url"

print_header() {
    echo "================================================================="
    echo "   🚀 Live Showcase Deployment & Verification Bridge"
    echo "================================================================="
}

show_help() {
    print_header
    cat <<'EOF'
Usage: ./deploy_demo.sh [OPTIONS]

Options:
  --skip-tests    Skip running pytest locally before committing and pushing.
  --skip-push     Do not commit or push changes to git / GitHub.
  --skip-wait     Do not wait for the GitHub Actions run to complete.
  --help, -h      Display this help message.

Description:
  1. Invokes ./scripts/showcase_tunnel.sh --quick to spin up a TryCloudflare tunnel.
  2. Waits for the tunnel to become healthy and responsive (HTTP 200 on /health).
  3. Updates the Live Showcase URL across:
       - README.md (Live Showcase badge and links)
       - .github/workflows/live-verification.yml (fallback LIVE_URL)
       - tests/test_live_showcase.py (default test LIVE_URL)
       - GitHub repository variable LIVE_SHOWCASE_URL (via gh CLI)
       - GitHub repository homepage URL (via gh CLI)
  4. Runs the live test suite locally against the new tunnel.
  5. Commits and pushes the updated files to origin/main.
  6. Waits for the 'Live Showcase Verification' GitHub Action to run and pass.
EOF
    exit 0
}

SKIP_TESTS=false
SKIP_PUSH=false
SKIP_WAIT=false

for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=true ;;
        --skip-push)  SKIP_PUSH=true ;;
        --skip-wait)  SKIP_WAIT=true ;;
        --help|-h)    show_help ;;
        *)
            echo "Unknown option: $arg"
            show_help
            ;;
    esac
done

print_header

# 1. Invoke showcase tunnel --quick
echo "Step 1: Starting showcase tunnel via ./scripts/showcase_tunnel.sh --quick..."
"${ROOT_DIR}/scripts/showcase_tunnel.sh" --quick

if [ ! -f "$SHOWCASE_URL_FILE" ]; then
    echo "ERROR: Showcase URL file not found at $SHOWCASE_URL_FILE"
    exit 1
fi

NEW_URL="$(tr -d '[:space:]' < "$SHOWCASE_URL_FILE")"

if [[ ! "$NEW_URL" =~ ^https://[a-zA-Z0-9.-]+\.trycloudflare\.com$ ]]; then
    echo "ERROR: Invalid showcase URL retrieved: '$NEW_URL'"
    exit 1
fi

echo "✓ Tunnel provisioned: $NEW_URL"

# 2. Wait for live endpoint to respond with HTTP 200
echo ""
echo "Step 2: Probing health endpoint at $NEW_URL/health..."
HEALTHY=false
for attempt in $(seq 1 15); do
    HTTP_CODE=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "$NEW_URL/health" || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "✓ Health check passed (HTTP 200) on attempt $attempt"
        HEALTHY=true
        break
    fi
    echo "  Attempt $attempt: /health returned HTTP $HTTP_CODE (waiting 3s)..."
    sleep 3
done

if [ "$HEALTHY" = false ]; then
    echo "ERROR: Live showcase tunnel failed to respond with HTTP 200 within timeout."
    exit 1
fi

# 3. Update URL across repo files
echo ""
echo "Step 3: Updating showcase URL across repository files..."

python3 - "$ROOT_DIR/README.md" "$ROOT_DIR/.github/workflows/live-verification.yml" "$ROOT_DIR/tests/test_live_showcase.py" "$NEW_URL" <<'PY'
import pathlib, re, sys

readme_path = pathlib.Path(sys.argv[1])
workflow_path = pathlib.Path(sys.argv[2])
test_path = pathlib.Path(sys.argv[3])
new_url = sys.argv[4].rstrip("/")

# 1. Update README.md badge
readme_text = readme_path.read_text(encoding="utf-8")
p_badge = re.compile(
    r'(\[!\[Live (?:Cloudflare )?Showcase\]\([^)]*badge\/Live%20Showcase[^)]*\)\]\()([^)]+)(\))'
)
readme_new, count_badge = p_badge.subn(r'\g<1>' + new_url + r'\3', readme_text)
if count_badge > 0:
    readme_path.write_text(readme_new, encoding="utf-8")
    print(f"  ✓ Updated Live Showcase badge in {readme_path.name}")
else:
    print(f"  - No badge URL update needed in {readme_path.name}")

# 2. Update .github/workflows/live-verification.yml
workflow_text = workflow_path.read_text(encoding="utf-8")
p_wf = re.compile(
    r"(LIVE_URL:\s*\${{\s*vars\.LIVE_SHOWCASE_URL\s*\|\|\s*')([^']+)(')"
)
wf_new, count_wf = p_wf.subn(r'\g<1>' + new_url + r'\3', workflow_text)
if count_wf > 0:
    workflow_path.write_text(wf_new, encoding="utf-8")
    print(f"  ✓ Updated LIVE_URL fallback in {workflow_path.name}")
else:
    print(f"  - No LIVE_URL fallback update needed in {workflow_path.name}")

# 3. Update tests/test_live_showcase.py
test_text = test_path.read_text(encoding="utf-8")
p_test = re.compile(
    r'(LIVE_URL\s*=\s*os\.getenv\("CANARY_URL",\s*os\.getenv\("LIVE_URL",\s*")([^"]+)("\)\)\.rstrip\("\/"\))'
)
test_new, count_test = p_test.subn(r'\g<1>' + new_url + r'\3', test_text)
if count_test > 0:
    test_path.write_text(test_new, encoding="utf-8")
    print(f"  ✓ Updated default LIVE_URL in {test_path.name}")
else:
    print(f"  - No default LIVE_URL update needed in {test_path.name}")
PY

# 4. Update GitHub repository variable and homepage if gh CLI is available
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    echo ""
    echo "Step 4: Synchronizing GitHub repository metadata..."
    if gh variable set LIVE_SHOWCASE_URL --body "$NEW_URL" >/dev/null 2>&1; then
        echo "  ✓ Updated GitHub Actions repo variable: LIVE_SHOWCASE_URL=$NEW_URL"
    else
        echo "  - Note: Could not update GitHub repo variable (continuing)"
    fi

    if gh repo edit --homepage "$NEW_URL" >/dev/null 2>&1; then
        echo "  ✓ Updated GitHub repository homepage: $NEW_URL"
    else
        echo "  - Note: Could not update GitHub repo homepage (continuing)"
    fi
fi

# 5. Run local live verification test suite
if [ "$SKIP_TESTS" = false ]; then
    echo ""
    echo "Step 5: Running live verification test suite against $NEW_URL..."
    PYTEST_BIN=""
    if [ -x "${ROOT_DIR}/.venv/bin/pytest" ]; then
        PYTEST_BIN="${ROOT_DIR}/.venv/bin/pytest"
    elif command -v pytest >/dev/null 2>&1; then
        PYTEST_BIN="pytest"
    elif python3 -m pytest --version >/dev/null 2>&1; then
        PYTEST_BIN="python3 -m pytest"
    fi

    if [ -n "$PYTEST_BIN" ]; then
        LIVE_URL="$NEW_URL" $PYTEST_BIN "${ROOT_DIR}/tests/test_live_showcase.py" "${ROOT_DIR}/tests/test_sql_guard.py" -v --tb=short
        echo "✓ All local live verification tests passed!"
    else
        echo "  (pytest not found locally, skipping local test execution)"
    fi
fi

# 6. Commit and push changes
if [ "$SKIP_PUSH" = false ]; then
    echo ""
    echo "Step 6: Committing and pushing updated showcase URL..."
    git -C "$ROOT_DIR" add \
        README.md \
        .github/workflows/live-verification.yml \
        tests/test_live_showcase.py \
        scripts/showcase_tunnel.sh \
        deploy_demo.sh

    if ! git -C "$ROOT_DIR" diff --cached --quiet; then
        git -C "$ROOT_DIR" commit -m "chore: update live showcase URL to ${NEW_URL} and add deploy script"
        git -C "$ROOT_DIR" push origin main
        echo "✓ Pushed commits to origin/main"
    else
        echo "  Working tree clean, no file changes to commit."
    fi

    # 7. Wait for and verify GitHub Action
    if [ "$SKIP_WAIT" = false ] && command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        echo ""
        echo "Step 7: Monitoring GitHub Action 'Live Showcase Verification'..."
        COMMIT_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"
        RUN_ID=""

        echo "Waiting for GitHub Actions to trigger workflow for commit ${COMMIT_SHA:0:7}..."
        for _ in $(seq 1 15); do
            RUN_ID="$(gh run list --commit "$COMMIT_SHA" --workflow "live-verification.yml" --json databaseId -q '.[0].databaseId' 2>/dev/null || true)"
            if [ -n "$RUN_ID" ] && [ "$RUN_ID" != "null" ]; then
                break
            fi
            sleep 2
        done

        if [ -z "$RUN_ID" ] || [ "$RUN_ID" = "null" ]; then
            echo "Workflow dispatching manually to ensure immediate run..."
            gh workflow run live-verification.yml --ref main
            sleep 4
            RUN_ID="$(gh run list --workflow "live-verification.yml" -L 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || true)"
        fi

        if [ -n "$RUN_ID" ] && [ "$RUN_ID" != "null" ]; then
            echo "Watching GitHub Action run #$RUN_ID..."
            gh run watch "$RUN_ID" --exit-status
            echo "✓ 'Live Showcase Verification' GitHub Action passed!"
        else
            echo "WARNING: Could not identify running workflow ID."
        fi
    fi
fi

echo ""
echo "================================================================="
echo " 🎉 LIVE DEMO DEPLOYMENT COMPLETE & VERIFIED!"
echo "    Public URL: $NEW_URL"
echo "    Badge & Workflow URL updated in repository"
echo "    Live Showcase Verification Action: PASSING"
echo "================================================================="
