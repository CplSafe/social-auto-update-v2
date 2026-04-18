import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

Platform = Literal["douyin", "xhs", "ks"]

_COOKIE_ROOT = Path(os.getenv("SAU_COOKIE_ROOT", "/app/sau_data/cookies"))


def _resolve_cookie_path(tenant_id: str, platform: Platform, sau_account_id: str) -> Path:
    if not tenant_id or "/" in tenant_id or ".." in tenant_id:
        raise HTTPException(status_code=400, detail="invalid tenant_id")
    if not sau_account_id or "/" in sau_account_id or ".." in sau_account_id:
        raise HTTPException(status_code=400, detail="invalid sau_account_id")
    return _COOKIE_ROOT / f"tenant_{tenant_id}" / platform / f"{sau_account_id}.json"


@router.get("/accounts/{sau_account_id}/check")
async def check_account(
    sau_account_id: str,
    tenant_id: str = Query(..., min_length=1),
    platform: Platform = Query(...),
) -> dict[str, object]:
    """Lightweight check: only verifies cookie file presence.

    Real cookie validity (calling upstream `cookie_auth`) is deferred to P1
    where we wire the playwright bridge.
    """
    cookie_path = _resolve_cookie_path(tenant_id, platform, sau_account_id)
    if not cookie_path.exists():
        return {"valid": False, "reason": "cookie_missing"}
    return {"valid": True, "reason": "cookie_present"}


@router.post("/accounts/{sau_account_id}/delete")
async def delete_account(
    sau_account_id: str,
    tenant_id: str = Query(..., min_length=1),
    platform: Platform = Query(...),
) -> dict[str, object]:
    cookie_path = _resolve_cookie_path(tenant_id, platform, sau_account_id)
    if cookie_path.exists():
        cookie_path.unlink()
        return {"deleted": True}
    return {"deleted": False, "reason": "cookie_missing"}
