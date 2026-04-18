#!/usr/bin/env bash
set -euo pipefail

: "${SAU_INTERNAL_TOKEN:?SAU_INTERNAL_TOKEN must be set}"
: "${SAU_BROKER_URL:?SAU_BROKER_URL must be set}"
: "${SAU_RESULT_BACKEND:?SAU_RESULT_BACKEND must be set}"

./scripts/ensure_cookie_dir.sh

# IMPORTANT: workers=1 is mandatory until LoginSessionRegistry moves to
# Redis. The login-session state is process-local; with >1 worker, /login
# and /login/status would land on different processes and the polling loop
# would 404 forever. Override SAU_API_WORKERS at your own risk.
exec uv run uvicorn apps.sau_api.main:app \
    --host 0.0.0.0 \
    --port 8001 \
    --workers "${SAU_API_WORKERS:-1}"
