"""Tenant-scoped cookie file path resolution.

A single source of truth shared by ``routers/accounts.py`` and the login
flow. Path traversal guards live here so every caller benefits from them
automatically.
"""

import os
from pathlib import Path
from typing import Literal

from fastapi import HTTPException

Platform = Literal["douyin", "xhs", "ks"]

COOKIE_ROOT = Path(os.getenv("SAU_COOKIE_ROOT", "/app/sau_data/cookies"))


def resolve_cookie_path(tenant_id: str, platform: Platform, sau_account_id: str) -> Path:
    if not tenant_id or "/" in tenant_id or ".." in tenant_id:
        raise HTTPException(status_code=400, detail="invalid tenant_id")
    if not sau_account_id or "/" in sau_account_id or ".." in sau_account_id:
        raise HTTPException(status_code=400, detail="invalid sau_account_id")
    return COOKIE_ROOT / f"tenant_{tenant_id}" / platform / f"{sau_account_id}.json"
