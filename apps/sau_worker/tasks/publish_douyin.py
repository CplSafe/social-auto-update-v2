"""Publish-to-Douyin Celery task.

Runs in the **prefork** pool (each worker process owns one Chromium via
``patchright``). The worker:

1. acquires a per-account token-bucket slot (≤3/min by default — protects
   the upstream account from being rate-limited / banned),
2. acquires a per-tenant concurrency slot (tier-derived cap so a single
   workspace can't pin every worker),
3. resolves the cookie file path under SAU_COOKIE_ROOT,
4. validates the cookie via upstream ``cookie_auth`` and short-circuits to
   ``cookie_invalid`` when stale (the Dify side flips the account row to
   ``expired`` so the FE can prompt re-auth),
5. downloads the video to a tmp file when the task came in via the P3
   presigned-URL path (otherwise the file is already on disk from /postVideo),
6. drives ``DouYinVideo.main()`` with title/tags/desc,
7. unlinks the temp video file in ``finally`` regardless of outcome.

The result envelope mirrors the upstream ``_build_login_result`` shape so
the Dify side can use a single classifier for both flows. Dify maps:

    {success: true,  current_url}                   -> SUCCESS
    {success: false, status: "cookie_invalid"}      -> FAILED + auto-expire account
    {success: false, status: "rate_limited"}        -> FAILED, code=upload_rate_limited
    {success: false, status: "timeout"}             -> FAILED, code=upload_timeout
    {success: false, ...}                            -> FAILED, code=upload_failed
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sau_contracts import PUBLISH_DOUYIN, PUBLISH_DOUYIN_QUEUE

from apps.sau_worker.celery_app import app

logger = logging.getLogger(__name__)


# ---------- helpers ----------


def _resolve_cookie_path(tenant_id: str, sau_account_id: str) -> Path:
    # Mirror the path-traversal guard from apps/sau_api/cookie_paths.py so
    # malformed task kwargs can't escape the cookie root.
    if not tenant_id or "/" in tenant_id or ".." in tenant_id:
        raise ValueError("invalid tenant_id")
    if not sau_account_id or "/" in sau_account_id or ".." in sau_account_id:
        raise ValueError("invalid sau_account_id")
    root = Path(os.getenv("SAU_COOKIE_ROOT", "/app/sau_data/cookies"))
    return root / f"tenant_{tenant_id}" / "douyin" / f"{sau_account_id}.json"


def _tmp_dir() -> Path:
    root = Path(os.getenv("SAU_TMP_DIR", "/app/sau_data/tmp"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def _coerce_publish_date(value: Any) -> datetime | int:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning("ignoring unparseable publish_date %r", value)
    return 0


def _safe_unlink(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.exception("failed to unlink tmp video", extra={"path": path})


def _download_video_url(url: str, *, dest: Path) -> Path:
    """Stream the presigned URL to disk with a configurable size cap.

    Runs synchronously (we're inside a prefork Celery worker, no event
    loop). httpx.Client with ``stream=True`` keeps memory bounded.
    """
    timeout = httpx.Timeout(
        float(os.getenv("SAU_DOWNLOAD_TIMEOUT_SECONDS", "600"))
    )
    max_bytes = int(
        os.getenv("SAU_DOWNLOAD_MAX_BYTES", str(1024 * 1024 * 1024))  # 1GB
    )
    written = 0
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        # Bail out before the disk fills up; the task
                        # surfaces this as upload_failed.
                        raise RuntimeError(
                            f"video exceeds SAU_DOWNLOAD_MAX_BYTES ({max_bytes})"
                        )
                    fh.write(chunk)
    dest.chmod(0o600)
    return dest


# ---------- rate-limit + concurrency wiring ----------

# Cached singletons — one TokenBucket per (capacity, window) pair we need,
# and one TenantConcurrencyGate. Lazy so test files can monkey-patch the
# redis client before the first publish dispatch.
_PER_ACCOUNT_BUCKET = None
_PLATFORM_BUCKET = None
_TENANT_GATE = None


def _per_account_bucket():
    global _PER_ACCOUNT_BUCKET
    if _PER_ACCOUNT_BUCKET is None:
        from apps.sau_worker.rate_limit import TokenBucket
        from apps.sau_worker.redis_client import get_redis_client

        _PER_ACCOUNT_BUCKET = TokenBucket(
            get_redis_client(),
            prefix="sau:tb:douyin:account",
            capacity=int(os.getenv("SAU_RATELIMIT_PER_ACCOUNT_CAP", "3")),
            window_sec=int(os.getenv("SAU_RATELIMIT_PER_ACCOUNT_WINDOW_SEC", "60")),
        )
    return _PER_ACCOUNT_BUCKET


def _platform_bucket():
    global _PLATFORM_BUCKET
    if _PLATFORM_BUCKET is None:
        from apps.sau_worker.rate_limit import TokenBucket
        from apps.sau_worker.redis_client import get_redis_client

        _PLATFORM_BUCKET = TokenBucket(
            get_redis_client(),
            prefix="sau:tb:douyin:platform",
            capacity=int(os.getenv("SAU_RATELIMIT_PLATFORM_CAP", "20")),
            window_sec=int(os.getenv("SAU_RATELIMIT_PLATFORM_WINDOW_SEC", "60")),
        )
    return _PLATFORM_BUCKET


def _tenant_gate():
    global _TENANT_GATE
    if _TENANT_GATE is None:
        from apps.sau_worker.concurrency import TenantConcurrencyGate
        from apps.sau_worker.redis_client import get_redis_client

        _TENANT_GATE = TenantConcurrencyGate(
            get_redis_client(),
            prefix="sau:concurrent:tenant:douyin",
            ttl_sec=int(os.getenv("SAU_TENANT_GATE_TTL_SEC", "600")),
        )
    return _TENANT_GATE


# ---------- core publish coroutine ----------


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


# ---------- Celery task ----------


@app.task(name=PUBLISH_DOUYIN, queue=PUBLISH_DOUYIN_QUEUE, bind=True)
def publish_douyin(
    self: Any,
    tenant_id: str,
    sau_account_id: str,
    payload: dict[str, Any],
    video_path: str | None = None,
    video_url: str | None = None,
    tier_concurrent: int | None = None,
) -> dict[str, Any]:
    logger.info(
        "publish_douyin received",
        extra={
            "task_id": self.request.id,
            "tenant_id": tenant_id,
            "sau_account_id": sau_account_id,
            "payload_keys": sorted(payload.keys()),
            "transport": "url" if video_url else "multipart",
        },
    )

    # P3: per-account rate limit — prevents a chatty workspace from
    # tripping anti-spam on the destination platform. Bucket the
    # platform-wide ceiling separately so one outlier can't take down
    # everyone.
    per_account = _per_account_bucket()
    if not per_account.wait_or_acquire(
        sau_account_id,
        max_wait_sec=int(os.getenv("SAU_RATELIMIT_MAX_WAIT_SEC", "120")),
    ):
        _safe_unlink(video_path)
        return {
            "success": False,
            "status": "rate_limited",
            "message": "per-account rate limit",
        }
    platform = _platform_bucket()
    if not platform.wait_or_acquire(
        "_global",
        max_wait_sec=int(os.getenv("SAU_RATELIMIT_MAX_WAIT_SEC", "120")),
    ):
        _safe_unlink(video_path)
        return {
            "success": False,
            "status": "rate_limited",
            "message": "platform-wide rate limit",
        }

    # P3: per-tenant concurrency cap — the gate is held for the duration
    # of the actual publish so the cap reflects in-flight Playwright
    # browsers, not just queued tasks.
    gate = _tenant_gate()
    limit = int(tier_concurrent) if tier_concurrent else int(
        os.getenv("SAU_TENANT_GATE_DEFAULT_LIMIT", "5")
    )
    with gate.slot(
        tenant_id,
        limit=limit,
        max_wait_sec=int(os.getenv("SAU_TENANT_GATE_MAX_WAIT_SEC", "300")),
    ) as acquired:
        if not acquired:
            _safe_unlink(video_path)
            return {
                "success": False,
                "status": "rate_limited",
                "message": f"tenant {tenant_id} concurrent cap ({limit}) busy",
            }
        return _run_with_video(
            self=self,
            tenant_id=tenant_id,
            sau_account_id=sau_account_id,
            payload=payload,
            video_path=video_path,
            video_url=video_url,
        )


def _run_with_video(
    *,
    self: Any,
    tenant_id: str,
    sau_account_id: str,
    payload: dict[str, Any],
    video_path: str | None,
    video_url: str | None,
) -> dict[str, Any]:
    # Resolve cookie + materialise the video file before doing the publish.
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

    actual_video_path = video_path
    download_owns_path = False
    if actual_video_path is None:
        if not video_url:
            return {
                "success": False,
                "status": "upload_failed",
                "message": "no video supplied",
            }
        try:
            actual_video_path = str(
                _download_video_url(
                    video_url,
                    dest=_tmp_dir() / f"{uuid.uuid4().hex}.mp4",
                )
            )
            download_owns_path = True
        except Exception as exc:
            logger.exception(
                "video download failed",
                extra={"task_id": self.request.id, "video_url": video_url[:120]},
            )
            return {
                "success": False,
                "status": "upload_failed",
                "message": f"download failed: {exc}",
            }

    tags = list(payload.get("tags") or [])
    desc = payload.get("desc")
    publish_date = _coerce_publish_date(payload.get("publish_date"))

    try:
        result = asyncio.run(
            _run_douyin_publish(
                cookie_path=cookie_path,
                video_path=actual_video_path,
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
        # Always unlink whichever local file we created — the multipart
        # path puts video_path on disk via /postVideo, and the URL path
        # downloads to download_owns_path.
        _safe_unlink(actual_video_path if download_owns_path else video_path)

    return result
