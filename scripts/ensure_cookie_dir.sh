#!/usr/bin/env bash
set -euo pipefail

ROOT="${SAU_COOKIE_ROOT:-/app/sau_data/cookies}"
mkdir -p "$ROOT"
chmod 0700 "$ROOT"
