"""Shared publish-task plumbing for douyin / xhs / ks.

Each platform's Celery task is a one-line wrapper around ``run_publish``;
all the per-task logic (rate limit, concurrency gate, cookie resolution,
URL download, finally cleanup) lives here.

The platform-specific bits are bound into ``PlatformBinding``:

- ``cookie_path_subdir`` — subdirectory under SAU_COOKIE_ROOT/tenant_X/
- ``import_uploader`` — lazy importer returning ``(cookie_auth, video_cls)``
- ``apply_platform_extras`` — optional hook to mutate the constructor
  kwargs based on the per-task ``platform_payload`` dict (e.g. squeezing
  ``location`` into ``desc`` for douyin/xhs since the upstream upload
  pipeline doesn't expose set_location as a constructor field).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

PlatformName = Literal["douyin", "xhs", "ks"]


# ---------- platform-agnostic helpers ----------


def resolve_cookie_path(
    tenant_id: str, platform: str, sau_account_id: str
) -> Path:
    """Mirror the path-traversal guard from apps/sau_api/cookie_paths.py
    so malformed task kwargs can't escape the cookie root."""
    if not tenant_id or "/" in tenant_id or ".." in tenant_id:
        raise ValueError("invalid tenant_id")
    if not sau_account_id or "/" in sau_account_id or ".." in sau_account_id:
        raise ValueError("invalid sau_account_id")
    if platform not in ("douyin", "xhs", "ks"):
        raise ValueError(f"invalid platform {platform!r}")
    root = Path(os.getenv("SAU_COOKIE_ROOT", "/app/sau_data/cookies"))
    return root / f"tenant_{tenant_id}" / platform / f"{sau_account_id}.json"


def tmp_dir() -> Path:
    root = Path(os.getenv("SAU_TMP_DIR", "/app/sau_data/tmp"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def coerce_publish_date(value: Any) -> datetime | int:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning("ignoring unparseable publish_date %r", value)
    return 0


def safe_unlink(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.exception("failed to unlink tmp video", extra={"path": path})


def download_video_url(url: str, *, dest: Path) -> Path:
    """Stream a presigned URL to disk with a hard size cap."""
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
                        raise RuntimeError(
                            f"video exceeds SAU_DOWNLOAD_MAX_BYTES ({max_bytes})"
                        )
                    fh.write(chunk)
    dest.chmod(0o600)
    return dest


# ---------- platform binding ----------


@dataclass(frozen=True)
class PlatformBinding:
    name: PlatformName
    # Lazy import to avoid pulling Playwright on the stub-task / CI path.
    # Returns (cookie_auth_callable, video_class).
    import_uploader: Callable[[], tuple[Callable[[str], Awaitable[bool]], type]]
    # Optional platform-specific tweak to the desc / kwargs based on the
    # per-task ``platform_payload``. Returns the new (desc, extra_kwargs).
    apply_platform_extras: Callable[
        [str | None, dict[str, Any]], tuple[str | None, dict[str, Any]]
    ] | None = None


# ---------- the shared core ----------


async def _run_publish_async(
    *,
    binding: PlatformBinding,
    cookie_path: Path,
    video_path: str,
    title: str,
    tags: list[str],
    desc: str | None,
    publish_date: datetime | int,
    extra_kwargs: dict[str, Any],
) -> dict[str, Any]:
    cookie_auth, video_cls = binding.import_uploader()

    if not await cookie_auth(str(cookie_path)):
        return {
            "success": False,
            "status": "cookie_invalid",
            "message": "cookie_auth returned false",
        }

    uploader = video_cls(
        title=title,
        file_path=video_path,
        tags=tags,
        publish_date=publish_date,
        account_file=str(cookie_path),
        desc=desc,
        **extra_kwargs,
    )
    try:
        await uploader.main()
        return {"success": True, "current_url": "", "status": "success"}
    except Exception as exc:  # noqa: BLE001 — surface as upstream-classified failure
        return {
            "success": False,
            "status": "upload_failed",
            "message": str(exc),
        }


def run_publish(
    self: Any,
    *,
    binding: PlatformBinding,
    tenant_id: str,
    sau_account_id: str,
    payload: dict[str, Any],
    video_path: str | None,
    video_url: str | None,
    tier_concurrent: int | None,
    rate_limit_hooks,
    tenant_gate_factory,
) -> dict[str, Any]:
    """The shared publish-task body.

    ``rate_limit_hooks`` is an object with ``per_account()`` /
    ``platform()`` methods returning a ``TokenBucket`` (or compatible).
    ``tenant_gate_factory`` returns a ``TenantConcurrencyGate`` (or
    compatible). Both are passed in (rather than imported) so the
    per-platform task module can swap the redis client / cache key
    namespace as needed.
    """
    logger.info(
        "publish task received",
        extra={
            "task_id": self.request.id,
            "platform": binding.name,
            "tenant_id": tenant_id,
            "sau_account_id": sau_account_id,
            "payload_keys": sorted(payload.keys()),
            "transport": "url" if video_url else "multipart",
        },
    )

    per_account = rate_limit_hooks.per_account()
    if not per_account.wait_or_acquire(
        sau_account_id,
        max_wait_sec=int(os.getenv("SAU_RATELIMIT_MAX_WAIT_SEC", "120")),
    ):
        safe_unlink(video_path)
        return {
            "success": False,
            "status": "rate_limited",
            "message": "per-account rate limit",
        }
    platform = rate_limit_hooks.platform()
    if not platform.wait_or_acquire(
        "_global",
        max_wait_sec=int(os.getenv("SAU_RATELIMIT_MAX_WAIT_SEC", "120")),
    ):
        safe_unlink(video_path)
        return {
            "success": False,
            "status": "rate_limited",
            "message": "platform-wide rate limit",
        }

    gate = tenant_gate_factory()
    limit = int(tier_concurrent) if tier_concurrent else int(
        os.getenv("SAU_TENANT_GATE_DEFAULT_LIMIT", "5")
    )
    with gate.slot(
        tenant_id,
        limit=limit,
        max_wait_sec=int(os.getenv("SAU_TENANT_GATE_MAX_WAIT_SEC", "300")),
    ) as acquired:
        if not acquired:
            safe_unlink(video_path)
            return {
                "success": False,
                "status": "rate_limited",
                "message": f"tenant {tenant_id} concurrent cap ({limit}) busy",
            }
        return _run_with_video(
            self=self,
            binding=binding,
            tenant_id=tenant_id,
            sau_account_id=sau_account_id,
            payload=payload,
            video_path=video_path,
            video_url=video_url,
        )


def _run_with_video(
    *,
    self: Any,
    binding: PlatformBinding,
    tenant_id: str,
    sau_account_id: str,
    payload: dict[str, Any],
    video_path: str | None,
    video_url: str | None,
) -> dict[str, Any]:
    cookie_path = resolve_cookie_path(tenant_id, binding.name, sau_account_id)
    if not cookie_path.exists():
        safe_unlink(video_path)
        return {
            "success": False,
            "status": "cookie_invalid",
            "message": f"cookie file missing at {cookie_path}",
        }

    title = str(payload.get("title") or "").strip()
    if not title:
        safe_unlink(video_path)
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
                download_video_url(
                    video_url,
                    dest=tmp_dir() / f"{uuid.uuid4().hex}.mp4",
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
    publish_date = coerce_publish_date(payload.get("publish_date"))

    # Apply platform-specific tweaks to desc / extra constructor kwargs
    # based on the per-task ``platform_payload``. Platforms whose
    # uploaders don't expose set_location as a constructor argument get
    # the location squeezed into the desc footer (P4 simplification —
    # P5 will patch the upstream upload pipeline properly).
    platform_payload = payload.get("platform_payload") or {}
    extra_kwargs: dict[str, Any] = {}
    if binding.apply_platform_extras is not None:
        desc, extra_kwargs = binding.apply_platform_extras(desc, platform_payload)

    try:
        result = asyncio.run(
            _run_publish_async(
                binding=binding,
                cookie_path=cookie_path,
                video_path=actual_video_path,
                title=title,
                tags=tags,
                desc=desc,
                publish_date=publish_date,
                extra_kwargs=extra_kwargs,
            )
        )
    except Exception as exc:
        logger.exception(
            "publish worker crashed",
            extra={"task_id": self.request.id, "platform": binding.name},
        )
        result = {
            "success": False,
            "status": "upload_failed",
            "message": f"worker crashed: {exc}",
        }
    finally:
        safe_unlink(actual_video_path if download_owns_path else video_path)

    return result


# ---------- platform extras helpers ----------


def apply_location_into_desc(
    desc: str | None, platform_payload: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    """Append a ``location`` from ``platform_payload`` to the desc.

    Used by douyin and xhs because their upstream Video classes don't
    accept ``location`` in the constructor — the upstream ``set_location``
    method is defined but never wired into ``upload()``. P5 will patch
    the upload pipeline; P4 takes the pragmatic path of stuffing the
    location into the desc footer so users still see something.
    """
    if not isinstance(platform_payload, dict):
        return desc, {}
    raw_location = platform_payload.get("location")
    # ``location`` is supposed to be a string but the runner is the trust
    # boundary on the sau side — coerce defensively so a caller that sends
    # a numeric ID / nested object can't crash the worker before the
    # task's try/finally cleans up the tmp video.
    location = "" if raw_location is None else str(raw_location).strip()
    if not location:
        return desc, {}
    base = (desc or "").rstrip()
    suffix = f"\n📍 {location}" if base else f"📍 {location}"
    return base + suffix, {}


def ignore_platform_extras(
    desc: str | None, platform_payload: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    """KS uploader has no location support — drop platform_payload."""
    return desc, {}


# ---------- platform bindings (lazy-imported uploaders) ----------


def _import_douyin() -> tuple[Callable[[str], Awaitable[bool]], type]:
    from uploader.douyin_uploader.main import DouYinVideo, cookie_auth  # type: ignore

    return cookie_auth, DouYinVideo


def _import_xhs() -> tuple[Callable[[str], Awaitable[bool]], type]:
    from uploader.xiaohongshu_uploader.main import (  # type: ignore
        XiaoHongShuVideo,
        cookie_auth,
    )

    return cookie_auth, XiaoHongShuVideo


def _import_ks() -> tuple[Callable[[str], Awaitable[bool]], type]:
    from uploader.ks_uploader.main import KSVideo, cookie_auth  # type: ignore

    return cookie_auth, KSVideo


DOUYIN = PlatformBinding(
    name="douyin",
    import_uploader=_import_douyin,
    apply_platform_extras=apply_location_into_desc,
)

XHS = PlatformBinding(
    name="xhs",
    import_uploader=_import_xhs,
    apply_platform_extras=apply_location_into_desc,
)

KS = PlatformBinding(
    name="ks",
    import_uploader=_import_ks,
    apply_platform_extras=ignore_platform_extras,
)
