"""Standalone mock of the sau-api surface — used by Dify P1 dev/CI.

This file does NOT depend on the real sau modules (no Playwright, no
Celery, no Redis). It implements just enough wire shape to let Dify's
SocialPublishService exercise the full scan-to-auth + check + delete
loop against an in-process FastAPI app.

Run:
    SAU_INTERNAL_TOKEN=$(openssl rand -hex 32) \\
        uv run python scripts/sau_mock.py --port 8001

Then in Dify api/.env:
    SAU_BASE_URL=http://127.0.0.1:8001
    SAU_INTERNAL_TOKEN=<same token>
    SOCIAL_PUBLISH_ENABLED=true

Behaviour notes:
- Sessions auto-progress on a configurable timer:
    waiting -> scanned (after MOCK_SCAN_DELAY_SEC, default 4s)
    scanned -> success (after MOCK_AUTH_DELAY_SEC, default 6s)
- Each session generates a deterministic fake sau_account_id so the
  reconcile path on the Dify side has a stable identity.
- All cookie-file probes return ``valid=true`` once a session reached
  success; ``cookie_missing`` otherwise.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import secrets
import time
from typing import Any, Literal

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel

logger = logging.getLogger("sau-mock")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

Platform = Literal["douyin", "xhs", "ks"]


# ---------- config ----------

MOCK_SCAN_DELAY_SEC = float(os.getenv("MOCK_SCAN_DELAY_SEC", "4"))
MOCK_AUTH_DELAY_SEC = float(os.getenv("MOCK_AUTH_DELAY_SEC", "6"))


def _load_token() -> str:
    token = os.getenv("SAU_INTERNAL_TOKEN", "")
    if not token or len(token) < 16:
        raise RuntimeError("SAU_INTERNAL_TOKEN must be set and >=16 chars")
    return token


_EXPECTED_TOKEN = _load_token()


def verify_token(x_sau_token: str | None = Header(default=None, alias="X-Sau-Token")) -> None:
    if x_sau_token is None or not secrets.compare_digest(x_sau_token, _EXPECTED_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")


# ---------- in-memory state ----------

# session_id -> {tenant_id, platform, started_at, status, sau_account_id, profile}
_sessions: dict[str, dict[str, Any]] = {}
# (tenant_id, platform, sau_account_id) -> bool valid
_cookies: dict[tuple[str, str, str], bool] = {}


def _fake_qr_png_base64() -> str:
    """A 1x1 transparent PNG as a stand-in for the real QR image."""
    raw = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    return f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"


def _resolve_status(session: dict[str, Any]) -> str:
    elapsed = time.monotonic() - float(session["started_at"])
    if elapsed >= MOCK_SCAN_DELAY_SEC + MOCK_AUTH_DELAY_SEC:
        return "success"
    if elapsed >= MOCK_SCAN_DELAY_SEC:
        return "scanned"
    return "waiting"


# ---------- app ----------

app = FastAPI(title="sau-mock", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "sau-mock"}


class LoginRequest(BaseModel):
    tenant_id: str
    platform: Platform
    session_id: str
    sau_account_id: str | None = None


@app.post("/login", dependencies=[Depends(verify_token)])
def start_login(req: LoginRequest) -> dict[str, Any]:
    sau_account_id = req.sau_account_id or f"mock-{secrets.token_hex(6)}"
    _sessions[req.session_id] = {
        "tenant_id": req.tenant_id,
        "platform": req.platform,
        "started_at": time.monotonic(),
        "sau_account_id": sau_account_id,
        "profile": {
            "display_name": f"测试账号-{sau_account_id[-4:]}",
            "avatar_url": "https://placehold.co/120x120?text=mock",
        },
    }
    logger.info(
        "session created", extra={"session_id": req.session_id, "tenant_id": req.tenant_id}
    )
    return {"qr_image_base64": _fake_qr_png_base64(), "expires_in": 180}


@app.get(
    "/login/status/{session_id}",
    dependencies=[Depends(verify_token)],
)
def login_status(session_id: str) -> dict[str, Any]:
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    current = _resolve_status(session)
    payload: dict[str, Any] = {
        "status": current,
        "sau_account_id": None,
        "profile": None,
        "message": None,
    }
    if current == "success":
        payload["sau_account_id"] = session["sau_account_id"]
        payload["profile"] = session["profile"]
        # Persist cookie so subsequent /accounts/{id}/check returns valid.
        _cookies[(session["tenant_id"], session["platform"], session["sau_account_id"])] = True
    elif current == "scanned":
        # Hint UI; identity not yet finalised.
        payload["sau_account_id"] = session["sau_account_id"]
    return payload


@app.get(
    "/accounts/{sau_account_id}/check",
    dependencies=[Depends(verify_token)],
)
def check_account(
    sau_account_id: str,
    tenant_id: str = Query(..., min_length=1),
    platform: Platform = Query(...),
) -> dict[str, Any]:
    valid = _cookies.get((tenant_id, platform, sau_account_id), False)
    return {"valid": valid, "reason": "cookie_present" if valid else "cookie_missing"}


@app.post(
    "/accounts/{sau_account_id}/delete",
    dependencies=[Depends(verify_token)],
)
def delete_account(
    sau_account_id: str,
    tenant_id: str = Query(..., min_length=1),
    platform: Platform = Query(...),
    _: Any = Body(default=None),
) -> dict[str, Any]:
    key = (tenant_id, platform, sau_account_id)
    existed = key in _cookies
    _cookies.pop(key, None)
    return {"deleted": existed}


# ---------- entry ----------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
