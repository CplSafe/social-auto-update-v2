#!/usr/bin/env bash
set -euo pipefail

: "${SAU_INTERNAL_TOKEN:?SAU_INTERNAL_TOKEN must be set}"
: "${SAU_BROKER_URL:?SAU_BROKER_URL must be set}"
: "${SAU_RESULT_BACKEND:?SAU_RESULT_BACKEND must be set}"

./scripts/ensure_cookie_dir.sh

# P2 onwards uses `prefork` because publish tasks drive Playwright /
# Patchright; gevent monkey-patching breaks the asyncio loop those
# uploaders need. concurrency defaults to 2 — each prefork process boots
# its own Chromium (≈1.5GB RSS), so 2 fits a 4GB container with headroom
# for the API process.
exec uv run celery -A apps.sau_worker.celery_app worker \
    --queues=publish_douyin,publish_xhs,publish_ks \
    --pool="${SAU_WORKER_POOL:-prefork}" \
    --concurrency="${SAU_WORKER_CONCURRENCY:-2}" \
    --loglevel="${SAU_LOG_LEVEL:-info}"
