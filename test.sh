#!/usr/bin/env bash
# test.sh — quick smoke test of all Flask endpoints.
# Requires the stack to be running (start.sh or launch.sh first).
set -euo pipefail

BASE="http://localhost:5000"
PASS=0
FAIL=0

check() {
    local desc="$1"
    local expected="$2"
    local actual="$3"
    if echo "$actual" | grep -q "$expected"; then
        echo "  PASS  $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $desc"
        echo "        expected to contain: $expected"
        echo "        got: $(echo "$actual" | head -c 200)"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== GET / ==="
check "home page" "Observability Lab" "$(curl -s "$BASE/")"

echo ""
echo "=== GET /compute/<n> ==="
for n in 5 10 20; do
    # retry up to 5 times
    for attempt in $(seq 1 5); do
        resp=$(curl -s "$BASE/compute/$n")
        code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/compute/$n")
        [ "$code" = "200" ] && break
    done
    check "compute $n" "\"result\"" "$resp"
done

echo ""
echo "=== POST /auditlog ==="
resp=$(curl -s -X POST "$BASE/auditlog" -d "source=smoke-test")
check "auditlog POST status ok"    '"status":"ok"'   "$resp"
check "auditlog POST has audit_id" '"audit_id"'      "$resp"

echo ""
echo "=== GET /auditlog ==="
resp=$(curl -s "$BASE/auditlog")
check "auditlog GET status ok"     '"status":"ok"'   "$resp"

echo ""
echo "=== GET /auditlog/stats ==="
resp=$(curl -s "$BASE/auditlog/stats")
check "auditlog stats total_rows"  '"total_rows"'    "$resp"
check "auditlog stats percentiles" '"percentiles"'   "$resp"

echo ""
echo "=== GET /eval ==="
check "eval 3+4"              '"result":7.0'   "$(curl -s "$BASE/eval?expr=3%2B4")"
check "eval 2*3"              '"result":6.0'   "$(curl -s "$BASE/eval?expr=2*3")"
check "eval 2^10"             '"result":1024.0' "$(curl -s "$BASE/eval?expr=2%5E10")"
check "eval nested PEMDAS"    '"result":44.4'  "$(curl -s "$BASE/eval?expr=((3%2B4)*6%5E2%2F5)%2B1-7")"
check "eval division by zero" '"error"'        "$(curl -s "$BASE/eval?expr=5%2F0")"
check "eval missing param"    '"error"'        "$(curl -s "$BASE/eval")"

echo ""
echo "=== POST /eval (JSON) ==="
check "eval POST json" '"result":20.0' \
    "$(curl -s -X POST "$BASE/eval" -H 'Content-Type: application/json' -d '{"expr":"(2+3)*4"}')"

echo ""
echo "=== Canary container ==="
canary_status=$(docker inspect canary --format '{{.State.Status}}' 2>/dev/null || echo "not found")
if [ "$canary_status" = "running" ]; then
    echo "  PASS  canary container is running"
    PASS=$((PASS + 1))
else
    echo "  FAIL  canary container status: $canary_status"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
