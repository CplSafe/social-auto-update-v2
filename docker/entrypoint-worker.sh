#!/usr/bin/env bash
set -euo pipefail

: "${SAU_INTERNAL_TOKEN:?SAU_INTERNAL_TOKEN must be set}"
: "${SAU_BROKER_URL:?SAU_BROKER_URL must be set}"
: "${SAU_RESULT_BACKEND:?SAU_RESULT_BACKEND must be set}"

./scripts/ensure_cookie_dir.sh

# P0 uses `gevent` because tasks are stubs. Before P1 wires real Playwright
# uploaders, switch to `--pool=prefork` (or `solo` if concurrency stays low):
# Playwright/Patchright is async/browser-heavy and does not coexist well
# with gevent monkey-patching.
exec uv run celery -A apps.sau_worker.celery_app worker \
    --queues=publish_douyin,publish_xhs,publish_ks \
    --pool="${SAU_WORKER_POOL:-gevent}" \
    --concurrency="${SAU_WORKER_CONCURRENCY:-4}" \
    --loglevel="${SAU_LOG_LEVEL:-info}"
