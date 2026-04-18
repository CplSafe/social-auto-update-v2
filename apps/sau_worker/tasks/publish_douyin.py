"""Publish-to-Douyin Celery task.

Runs in the **prefork** pool (each worker process owns one Chromium via
``patchright``). The worker:

1. resolves the cookie file path under SAU_COOKIE_ROOT,
2. validates the cookie via upstream ``cookie_auth`` and short-circuits to
   ``cookie_invalid`` when stale (the Dify side flips the account row to
   ``expired`` so the FE can prompt re-auth),
3. drives ``DouYinVideo.main()`` with title/tags/desc,
4. unlinks the temp video file in ``finally`` regardless of outcome.

The result envelope mirrors the upstream ``_build_login_result`` shape so
the Dify side can use a single classifier for both flows. Dify maps:

    {success: true,  current_url}                   -> SUCCESS
    {success: false, status: "cookie_invalid"}      -> FAILED + auto-expire account
    {success: false, status: "timeout"}             -> FAILED, code=upload_timeout
    {success: false, ...}                            -> FAILED, code=upload_failed
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from sau_contracts import PUBLISH_DOUYIN, PUBLISH_DOUYIN_QUEUE

from apps.sau_worker.celery_app import app

logger = logging.getLogger(__name__)


def _resolve_cookie_path(tenant_id: str, sau_account_id: str) -> Path:
    # Mirror the path-traversal guard from apps/sau_api/cookie_paths.py so
    # malformed task kwargs can't escape the cookie root.
    if not tenant_id or "/" in tenant_id or ".." in tenant_id:
        raise ValueError("invalid tenant_id")
    if not sau_account_id or "/" in sau_account_id or ".." in sau_account_id:
        raise ValueError("invalid sau_account_id")
    root = Path(os.getenv("SAU_COOKIE_ROOT", "/app/sau_data/cookies"))
    return root / f"tenant_{tenant_id}" / "douyin" / f"{sau_account_id}.json"


def _coerce_publish_date(value: Any) -> datetime | int:
    # P2 only supports immediate publish; treat any other value as 0 (the
    # upstream sentinel meaning "publish now"). Schedule support lands in P3.
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning("ignoring unparseable publish_date %r", value)
    return 0


async def _run_douyin_publish(
    *,
    cookie_path: Path,
    video_path: str,
    title: str,
    tags: list[str],
    desc: str | None,
    publish_date: datetime | int,
) -> dict[str, Any]:
    # Lazy import: keeps the task module importable without Playwright in
    # CI / stub environments.
    from uploader.douyin_uploader.main import DouYinVideo, cookie_auth  # type: ignore

    if not await cookie_auth(str(cookie_path)):
        return {
            "success": False,
            "status": "cookie_invalid",
            "message": "cookie_auth returned false",
        }

    uploader = DouYinVideo(
        title=title,
        file_path=video_path,
        tags=tags,
        publish_date=publish_date,
        account_file=str(cookie_path),
        desc=desc,
    )
    try:
        await uploader.main()
        return {"success": True, "current_url": "", "status": "success"}
    except Exception as exc:
        return {
            "success": False,
            "status": "upload_failed",
            "message": str(exc),
        }


@app.task(name=PUBLISH_DOUYIN, queue=PUBLISH_DOUYIN_QUEUE, bind=True)
def publish_douyin(
    self: Any,
    tenant_id: str,
    sau_account_id: str,
    video_path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    logger.info(
        "publish_douyin received",
        extra={
            "task_id": self.request.id,
            "tenant_id": tenant_id,
            "sau_account_id": sau_account_id,
            "payload_keys": sorted(payload.keys()),
            "video_path": video_path,
        },
    )

    cookie_path = _resolve_cookie_path(tenant_id, sau_account_id)
    if not cookie_path.exists():
        _safe_unlink(video_path)
        return {
            "success": False,
            "status": "cookie_invalid",
            "message": f"cookie file missing at {cookie_path}",
        }

    title = str(payload.get("title") or "").strip()
    if not title:
        _safe_unlink(video_path)
        return {
            "success": False,
            "status": "upload_failed",
            "message": "title is required",
        }

    tags = list(payload.get("tags") or [])
    desc = payload.get("desc")
    publish_date = _coerce_publish_date(payload.get("publish_date"))

    try:
        result = asyncio.run(
            _run_douyin_publish(
                cookie_path=cookie_path,
                video_path=video_path,
                title=title,
                tags=tags,
                desc=desc,
                publish_date=publish_date,
            )
        )
    except Exception as exc:
        logger.exception("publish_douyin worker crashed", extra={"task_id": self.request.id})
        result = {
            "success": False,
            "status": "upload_failed",
            "message": f"worker crashed: {exc}",
        }
    finally:
        _safe_unlink(video_path)

    return result


def _safe_unlink(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.exception("failed to unlink tmp video", extra={"path": path})
