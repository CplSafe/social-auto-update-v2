from fastapi import APIRouter, Query

from apps.sau_api.cookie_paths import Platform, resolve_cookie_path

router = APIRouter()


@router.get("/accounts/{sau_account_id}/check")
async def check_account(
    sau_account_id: str,
    tenant_id: str = Query(..., min_length=1),
    platform: Platform = Query(...),
) -> dict[str, object]:
    """Lightweight check: only verifies cookie file presence.

    Real cookie validity (calling upstream ``cookie_auth``) is intentionally
    not done here — that requires booting Playwright, which is too expensive
    for a Dify-side liveness probe. Use the auth flow to refresh.
    """
    cookie_path = resolve_cookie_path(tenant_id, platform, sau_account_id)
    if not cookie_path.exists():
        return {"valid": False, "reason": "cookie_missing"}
    return {"valid": True, "reason": "cookie_present"}


@router.post("/accounts/{sau_account_id}/delete")
async def delete_account(
    sau_account_id: str,
    tenant_id: str = Query(..., min_length=1),
    platform: Platform = Query(...),
) -> dict[str, object]:
    cookie_path = resolve_cookie_path(tenant_id, platform, sau_account_id)
    if cookie_path.exists():
        cookie_path.unlink()
        return {"deleted": True}
    return {"deleted": False, "reason": "cookie_missing"}
