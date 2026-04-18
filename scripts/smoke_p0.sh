#!/usr/bin/env bash
# Verifies the P0 acceptance criteria from
# docs/plans/2026-04-18-sau-p0-plan.md (sections J 1-13).
set -euo pipefail

API="${API:-http://127.0.0.1:8001}"
TOKEN="${SAU_INTERNAL_TOKEN:?SAU_INTERNAL_TOKEN must be set}"

step() { printf '\n=== %s ===\n' "$*"; }
ok()   { printf '  PASS: %s\n' "$*"; }
fail() { printf '  FAIL: %s\n' "$*" >&2; exit 1; }

step "1-2  containers up and healthy"
docker compose -f docker/docker-compose.yml ps --format '{{.Name}} {{.Status}}'

step "3  GET /health (no token)"
body=$(curl -fsS "$API/health")
[[ "$body" == *'"status":"ok"'* ]] && ok "health ok" || fail "got: $body"

step "4  GET /accounts/.../check without token -> 401"
code=$(curl -s -o /dev/null -w '%{http_code}' \
    "$API/accounts/fake/check?tenant_id=t1&platform=douyin")
[[ "$code" == "401" ]] && ok "401 without token" || fail "got: $code"

step "5  wrong token -> 401"
code=$(curl -s -o /dev/null -w '%{http_code}' \
    -H 'X-Sau-Token: wrong' \
    "$API/accounts/fake/check?tenant_id=t1&platform=douyin")
[[ "$code" == "401" ]] && ok "401 with wrong token" || fail "got: $code"

step "6  valid token, no cookie file -> {valid:false,reason:cookie_missing}"
body=$(curl -fsS \
    -H "X-Sau-Token: $TOKEN" \
    "$API/accounts/fake/check?tenant_id=t1&platform=douyin")
[[ "$body" == *'cookie_missing'* ]] && ok "cookie_missing" || fail "got: $body"

step "7-8 sau_contracts importable inside containers"
docker exec sau-api python -c "from sau_contracts import PUBLISH_DOUYIN; assert PUBLISH_DOUYIN == 'sau.publish.douyin'; print('api ok')"
docker exec sau-worker python -c "from sau_contracts import PUBLISH_DOUYIN; assert PUBLISH_DOUYIN == 'sau.publish.douyin'; print('worker ok')"
ok "sau_contracts present"

step "9  worker has registered tasks"
docker logs sau-worker 2>&1 | grep -E 'sau\.publish\.(douyin|xhs|ks)' | head -3 \
  || fail "tasks not registered"
ok "tasks registered"

step "10 enqueue a stub task and observe execution"
task_id=$(docker exec sau-api python - <<'PY'
from apps.sau_worker.celery_app import app
r = app.send_task(
    "sau.publish.douyin",
    kwargs={"tenant_id": "t1", "sau_account_id": "a1", "payload": {}},
    queue="publish_douyin",
)
print(r.id)
PY
)
echo "  task_id=$task_id"
sleep 2
docker logs sau-worker 2>&1 | grep -F "$task_id" | head -3 \
  || fail "worker didn't pick up task $task_id"
ok "worker executed task"

step "11 cookie root mode 0700"
mode=$(docker exec sau-api stat -c '%a' /app/sau_data/cookies)
[[ "$mode" == "700" ]] && ok "0700" || fail "got: $mode"

step "12 process runs as 'sau' (non-root)"
who=$(docker exec sau-api whoami)
[[ "$who" == "sau" ]] && ok "non-root" || fail "got: $who"

step "13 patchright chromium present"
docker exec sau-api sh -c 'ls /ms-playwright/chromium-*/chrome-linux/chrome' \
  || fail "chromium binary missing"
ok "chromium installed"

printf '\nALL PASS\n'
