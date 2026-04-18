#!/usr/bin/env bash
set -euo pipefail

: "${SAU_INTERNAL_TOKEN:?SAU_INTERNAL_TOKEN must be set}"
: "${SAU_BROKER_URL:?SAU_BROKER_URL must be set}"
: "${SAU_RESULT_BACKEND:?SAU_RESULT_BACKEND must be set}"

./scripts/ensure_cookie_dir.sh

exec uv run uvicorn apps.sau_api.main:app \
    --host 0.0.0.0 \
    --port 8001 \
    --workers "${SAU_API_WORKERS:-2}"
