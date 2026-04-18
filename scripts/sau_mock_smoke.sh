#!/usr/bin/env bash
# Boot sau-mock in the background, exercise the public surface with curl,
# and tear it down. Useful both as a docs example and as a CI smoke test.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8001}"
TOKEN="${SAU_INTERNAL_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

export SAU_INTERNAL_TOKEN="$TOKEN"
export MOCK_SCAN_DELAY_SEC="${MOCK_SCAN_DELAY_SEC:-2}"
export MOCK_AUTH_DELAY_SEC="${MOCK_AUTH_DELAY_SEC:-2}"

uv run python scripts/sau_mock.py --port "$PORT" >/tmp/sau-mock.log 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null || true' EXIT

# Wait for /health to come up (max 10s).
for _ in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; then
        break
    fi
    sleep 0.5
done

step() { printf '\n=== %s ===\n' "$*"; }

step "1. /health (no token)"
curl -sf "http://127.0.0.1:$PORT/health"
echo

step "2. /login -> qr"
START=$(curl -sf -X POST "http://127.0.0.1:$PORT/login" \
    -H "X-Sau-Token: $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"tenant_id\":\"t1\",\"platform\":\"douyin\",\"session_id\":\"$SESSION_ID\"}")
echo "$START" | head -c 120
echo

step "3. /login/status (waiting -> scanned -> success)"
for i in 1 2 3 4 5 6 7; do
    POLL=$(curl -sf "http://127.0.0.1:$PORT/login/status/$SESSION_ID" \
        -H "X-Sau-Token: $TOKEN")
    echo "poll #$i: $POLL"
    case "$POLL" in
        *'"status":"success"'*) break ;;
    esac
    sleep 1
done
case "$POLL" in
    *'"status":"success"'*) ;;
    *) echo "FAIL: never reached success" >&2; exit 1 ;;
esac

step "4. /accounts/{id}/check (should be valid now)"
SAU_ID=$(printf '%s' "$POLL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sau_account_id"])')
curl -sf "http://127.0.0.1:$PORT/accounts/$SAU_ID/check?tenant_id=t1&platform=douyin" \
    -H "X-Sau-Token: $TOKEN"
echo

step "5. /accounts/{id}/delete"
curl -sf -X POST "http://127.0.0.1:$PORT/accounts/$SAU_ID/delete?tenant_id=t1&platform=douyin" \
    -H "X-Sau-Token: $TOKEN"
echo

step "6. /accounts/{id}/check after delete (should be invalid)"
curl -sf "http://127.0.0.1:$PORT/accounts/$SAU_ID/check?tenant_id=t1&platform=douyin" \
    -H "X-Sau-Token: $TOKEN"
echo

step "7. /postVideo with multipart and tiny payload"
echo "fake video bytes" > /tmp/sau-mock-fake-video.mp4
PUBLISH=$(curl -sf -X POST "http://127.0.0.1:$PORT/postVideo" \
    -H "X-Sau-Token: $TOKEN" \
    -F 'video=@/tmp/sau-mock-fake-video.mp4;type=video/mp4' \
    -F 'data={"tenant_id":"t1","platform":"douyin","sau_account_id":"acc-1","title":"hello"}')
echo "$PUBLISH"
SAU_TASK=$(printf '%s' "$PUBLISH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sau_task_id"])')

step "8. /tasks/{id} until SUCCESS"
for i in 1 2 3 4 5; do
    STATE=$(curl -sf "http://127.0.0.1:$PORT/tasks/$SAU_TASK" -H "X-Sau-Token: $TOKEN")
    echo "poll #$i: $STATE"
    case "$STATE" in
        *'"state":"SUCCESS"'*) break ;;
    esac
    sleep 1
done
case "$STATE" in
    *'"success":true'*) ;;
    *) echo "FAIL: publish never succeeded" >&2; exit 1 ;;
esac

step "9. /postVideo with MOCK_FAIL sentinel"
FAIL_PUBLISH=$(curl -sf -X POST "http://127.0.0.1:$PORT/postVideo" \
    -H "X-Sau-Token: $TOKEN" \
    -F 'video=@/tmp/sau-mock-fake-video.mp4;type=video/mp4' \
    -F 'data={"tenant_id":"t1","platform":"douyin","sau_account_id":"acc-1","title":"MOCK_FAIL_test"}')
FAIL_TASK=$(printf '%s' "$FAIL_PUBLISH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sau_task_id"])')
sleep 4
FAIL_STATE=$(curl -sf "http://127.0.0.1:$PORT/tasks/$FAIL_TASK" -H "X-Sau-Token: $TOKEN")
echo "$FAIL_STATE"
case "$FAIL_STATE" in
    *'"success":false'*) ;;
    *) echo "FAIL: MOCK_FAIL didn't surface as failure" >&2; exit 1 ;;
esac

rm -f /tmp/sau-mock-fake-video.mp4
printf '\nALL PASS\n'
