"""Login routes — scan-to-auth implemented via background task + polling.

Despite the file name (kept for git-history continuity), this router does
NOT use Server-Sent Events. The Dify side polls
``GET /login/status/{session_id}`` every ~2s and we update an in-process
registry from a background coroutine that wraps ``douyin_cookie_gen``.

Why polling and not SSE: nginx / gunicorn proxies in the Dify deployment
default to a 60s read timeout, and SSE keep-alives don't survive that
without per-route tuning. Polling sidesteps the whole class of problems.

Env knobs:
- ``SAU_ENABLE_REAL_LOGIN``: set to "1"/"true" to actually drive Playwright
  via the upstream uploader. Off by default so CI without a browser still
  starts cleanly.
- ``SAU_LOGIN_POLL_INTERVAL_SEC`` / ``SAU_LOGIN_MAX_CHECKS``: forwarded to
  the upstream loop.
- ``SAU_LOGIN_HEADLESS``: "1" to run the browser headless.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from apps.sau_api.cookie_paths import Platform, resolve_cookie_path
from apps.sau_api.login_sessions import LoginSession, registry

logger = logging.getLogger(__name__)
router = APIRouter()

_STUB_QR = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "DUlEQVR4nGMAAQAAAAUAAQ0KLbQAAAAASUVORK5CYII="
)


def _real_login_enabled() -> bool:
    return os.getenv("SAU_ENABLE_REAL_LOGIN", "").lower() in ("1", "true", "yes")


def _new_sau_account_id() -> str:
    # uuid4 (122 bits) is collision-safe even across tenants and matches
    # the id-generation convention on the Dify side.
    return f"dy-{uuid.uuid4().hex}"


class LoginRequest(BaseModel):
    tenant_id: str
    platform: Platform
    session_id: str
    sau_account_id: str | None = None


@router.post("/login")
async def start_login(req: LoginRequest) -> dict[str, Any]:
    if req.platform != "douyin":
        # P1 only wires douyin; xhs/ks land in P4. Surface the contract gap
        # explicitly rather than silently accepting and never progressing.
        raise HTTPException(
            status_code=400,
            detail=f"platform {req.platform!r} is not supported in P1",
        )

    sau_account_id = req.sau_account_id or _new_sau_account_id()
    cookie_path = resolve_cookie_path(req.tenant_id, req.platform, sau_account_id)
    cookie_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir(exist_ok=True) silently keeps a pre-existing dir's mode, so
    # explicitly tighten it after the call. Failure here is fatal — we
    # refuse to write cookies into a world-readable directory.
    cookie_path.parent.chmod(0o700)

    session = LoginSession(
        session_id=req.session_id,
        tenant_id=req.tenant_id,
        platform=req.platform,
        sau_account_id=sau_account_id,
    )
    await registry.create(session)

    if not _real_login_enabled():
        # Stub mode: pretend the QR is being generated. Useful for early UI
        # work / CI; tests should use scripts/sau_mock.py instead.
        logger.info(
            "SAU_ENABLE_REAL_LOGIN is off; returning stub QR",
            extra={"session_id": req.session_id},
        )
        await registry.update(
            req.session_id,
            qr_image_data_url=_STUB_QR,
            status="waiting",
        )
        return {"qr_image_base64": _STUB_QR, "expires_in": 180}

    # Real path: spawn the upstream Playwright loop in the background, then
    # block briefly until the qrcode_callback fires so the caller gets the
    # QR in the same response.
    qr_ready = asyncio.Event()
    qr_holder: dict[str, str] = {}

    async def qrcode_callback(payload: dict[str, Any]) -> None:
        # Upstream emits this whenever a new QR is rendered (initial + refresh).
        url = payload.get("image_data_url") or ""
        if not url:
            return
        qr_holder["qr"] = url
        await registry.update(
            req.session_id,
            qr_image_data_url=url,
            status="waiting",
        )
        qr_ready.set()

    async def runner() -> None:
        # Imported lazily so the stub-only path doesn't require Playwright /
        # patchright at module import time.
        from uploader.douyin_uploader.main import douyin_cookie_gen  # type: ignore

        poll_interval = int(os.getenv("SAU_LOGIN_POLL_INTERVAL_SEC", "3"))
        max_checks = int(os.getenv("SAU_LOGIN_MAX_CHECKS", "60"))
        headless = os.getenv("SAU_LOGIN_HEADLESS", "1").lower() in ("1", "true", "yes")
        try:
            result = await douyin_cookie_gen(
                str(cookie_path),
                qrcode_callback=qrcode_callback,
                poll_interval=poll_interval,
                max_checks=max_checks,
                headless=headless,
            )
        except asyncio.CancelledError:
            # Cancellation is the expected shutdown / expiry / timeout path —
            # don't downgrade the (already-terminal) session status. Just let
            # the cancellation propagate so Playwright's async-with cleans up.
            raise
        except Exception as exc:  # noqa: BLE001 — log + propagate to session
            logger.exception(
                "douyin_cookie_gen crashed", extra={"session_id": req.session_id}
            )
            await registry.update(req.session_id, status="failed", message=str(exc))
            return

        if result.get("success"):
            await registry.update(req.session_id, status="success", message=None)
        else:
            terminal = "expired" if result.get("status") == "timeout" else "failed"
            await registry.update(
                req.session_id,
                status=terminal,
                message=str(result.get("message") or "")[:200],
            )

    task = asyncio.create_task(runner())
    await registry.update(req.session_id, task=task)

    # Wait up to 30s for the first qrcode callback to fire. Upstream takes
    # a few seconds to launch Chrome and reach the login page.
    try:
        await asyncio.wait_for(qr_ready.wait(), timeout=30.0)
    except TimeoutError as exc:
        # Cancel the runner so it doesn't keep a Chrome process alive in the
        # background, and mark the session terminal so subsequent
        # /login/status polls return "failed" immediately instead of
        # waiting out the 200s TTL.
        task.cancel()
        await registry.update(
            req.session_id,
            status="failed",
            message="qr-generation timeout",
        )
        raise HTTPException(
            status_code=504,
            detail="timed out waiting for QR generation",
        ) from exc

    return {"qr_image_base64": qr_holder["qr"], "expires_in": 180}


@router.get("/login/status/{session_id}")
async def login_status(session_id: str) -> dict[str, Any]:
    session = await registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    snap = session.snapshot()
    if session.status == "success" and session.profile is None:
        # Best-effort minimal profile when upstream doesn't surface one yet.
        snap["profile"] = {
            "display_name": session.sau_account_id,
            "avatar_url": None,
        }
    return snap
