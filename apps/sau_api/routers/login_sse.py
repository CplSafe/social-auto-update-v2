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
import importlib
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


# P4 → P5: KS removed entirely (no upstream cookie_gen). The lookup table
# stays around so adding a new platform is one line, not a refactor.
_PLATFORM_LOGIN_SUPPORT: dict[str, tuple[str, str] | None] = {
    "douyin": ("uploader.douyin_uploader.main", "douyin_cookie_gen"),
    "xhs": ("uploader.xiaohongshu_uploader.main", "xiaohongshu_cookie_gen"),
}


@router.post("/login")
async def start_login(req: LoginRequest) -> dict[str, Any]:
    cookie_gen_target = _PLATFORM_LOGIN_SUPPORT.get(req.platform)
    if cookie_gen_target is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"platform {req.platform!r} scan-to-auth is not yet supported"
            ),
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
        # Lazily import the platform-specific cookie_gen so the stub-only path
        # doesn't pull in Playwright / patchright at module import time, and
        # so the xhs path doesn't accidentally hit douyin uploader code.
        module_path, fn_name = cookie_gen_target
        module = importlib.import_module(module_path)
        cookie_gen_fn = getattr(module, fn_name)

        # P7: build the SMS challenge callback for the login flow. When 抖音
        # / 小红书 pop a SMS verification page after the QR scan, the upstream
        # ``_wait_for_*_login`` loop calls this; we register a challenge_session
        # in Redis and surface its id on the LoginSession so dify can render
        # a「输入短信验证码」modal.
        from apps.sau_worker._challenge_callback import make_challenge_callback

        def _on_challenge_session(session) -> None:
            # Push the challenge_session_id onto the LoginSession registry
            # asynchronously — this hook is called from a sync context inside
            # the async callback, so we schedule via call_soon_threadsafe.
            asyncio.create_task(
                registry.update(
                    req.session_id,
                    challenge_session_id=session.session_id,
                    status="awaiting_user",
                )
            )

        challenge_callback = make_challenge_callback(
            tenant_id=req.tenant_id,
            sau_account_id=sau_account_id,
            platform=req.platform,
            on_session_created=_on_challenge_session,
        )

        poll_interval = int(os.getenv("SAU_LOGIN_POLL_INTERVAL_SEC", "3"))
        max_checks = int(os.getenv("SAU_LOGIN_MAX_CHECKS", "60"))
        # Default to headless. Only set SAU_LOGIN_HEADLESS=0 explicitly for local debug.
        headless = os.getenv("SAU_LOGIN_HEADLESS", "1").strip().lower() not in ("0", "false", "no")
        try:
            result = await cookie_gen_fn(
                str(cookie_path),
                qrcode_callback=qrcode_callback,
                poll_interval=poll_interval,
                max_checks=max_checks,
                headless=headless,
                challenge_callback=challenge_callback,
            )
        except asyncio.CancelledError:
            # Cancellation is the expected shutdown / expiry / timeout path —
            # don't downgrade the (already-terminal) session status. Just let
            # the cancellation propagate so Playwright's async-with cleans up.
            raise
        except Exception as exc:  # noqa: BLE001 — log + propagate to session
            logger.exception(
                "%s crashed",
                fn_name,
                extra={"session_id": req.session_id},
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
