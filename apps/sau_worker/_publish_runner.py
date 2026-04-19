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

PlatformName = Literal["douyin", "xhs"]


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
    if platform not in ("douyin", "xhs"):
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
    """Stream a presigned URL to disk with a hard size cap.

    Retries on transient connection / SSL errors before giving up so a
    one-off TLS reset (common with self-signed or load-balanced backends)
    doesn't blow up an entire publish.
    """
    timeout = httpx.Timeout(
        float(os.getenv("SAU_DOWNLOAD_TIMEOUT_SECONDS", "600"))
    )
    max_bytes = int(
        os.getenv("SAU_DOWNLOAD_MAX_BYTES", str(1024 * 1024 * 1024))  # 1GB
    )
    verify_ssl = os.getenv("SAU_DOWNLOAD_VERIFY_SSL", "1").lower() not in ("0", "false", "no")
    max_retries = int(os.getenv("SAU_DOWNLOAD_RETRIES", "3"))
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        written = 0
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, verify=verify_ssl) as client:
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
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            logger.warning(
                "download attempt %d/%d failed: %s", attempt, max_retries, exc
            )
            if attempt < max_retries:
                import time
                time.sleep(2 ** attempt)
    raise last_exc if last_exc else RuntimeError("download failed without exception")


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
    tenant_id: str,
    sau_account_id: str,
    on_challenge_session=None,
) -> dict[str, Any]:
    cookie_auth, video_cls = binding.import_uploader()

    if not await cookie_auth(str(cookie_path)):
        return {
            "success": False,
            "status": "cookie_invalid",
            "message": "cookie_auth returned false",
        }

    # P7: build the SMS challenge callback so upstream upload() can hand
    # off mid-flow when抖音/小红书 pops a verification page. The callback
    # creates a Redis-backed challenge_session, polls for the user's
    # action through dify, and feeds it back to the uploader.
    from apps.sau_worker._challenge_callback import make_challenge_callback
    from apps.sau_worker._publish_challenge import VerificationAbortedError

    challenge_callback = make_challenge_callback(
        tenant_id=tenant_id,
        sau_account_id=sau_account_id,
        platform=binding.name,
        on_session_created=on_challenge_session,
    )

    uploader = video_cls(
        title=title,
        file_path=video_path,
        tags=tags,
        publish_date=publish_date,
        account_file=str(cookie_path),
        desc=desc,
        challenge_callback=challenge_callback,
        **extra_kwargs,
    )
    try:
        await uploader.main()
        return {"success": True, "current_url": "", "status": "success"}
    except VerificationAbortedError as exc:
        # User declined to complete the SMS verification — surface a
        # typed status so dify's _poll_sau classifies it specifically
        # rather than the generic upload_failed bucket.
        return {
            "success": False,
            "status": "verification_aborted",
            "message": f"用户取消了短信验证: {exc.reason}",
        }
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

    # P7: surface the challenge_session_id back into the celery task's
    # bound state so dify's _poll_sau picks it up and renders a "需要短信
    # 验证" badge. We use ``self.update_state(meta={...})`` because that's
    # the only meta channel available before the task returns.
    def _on_challenge_session(session) -> None:
        try:
            self.update_state(
                state="STARTED",
                meta={
                    "challenge_session_id": session.session_id,
                    "challenge_kind": session.kind,
                    "challenge_platform": session.platform,
                },
            )
        except Exception:
            logger.exception(
                "failed to publish challenge_session_id to celery meta",
                extra={"task_id": self.request.id},
            )

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
                tenant_id=tenant_id,
                sau_account_id=sau_account_id,
                on_challenge_session=_on_challenge_session,
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


def apply_location_extras(
    desc: str | None, platform_payload: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    """Pull ``location`` out of ``platform_payload`` and forward it as a
    constructor kwarg.

    P5 wires the upstream ``set_location`` into douyin/xhs ``upload()``,
    so the right path is to forward ``location`` to ``video_cls(...)``
    rather than stuff it into the desc footer (the P4 hack). The runner
    is the trust boundary on the sau side, so coerce defensively before
    handing the value to Playwright.
    """
    if not isinstance(platform_payload, dict):
        return desc, {}
    raw_location = platform_payload.get("location")
    location = "" if raw_location is None else str(raw_location).strip()
    if not location:
        return desc, {}
    return desc, {"location": location}


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


DOUYIN = PlatformBinding(
    name="douyin",
    import_uploader=_import_douyin,
    apply_platform_extras=apply_location_extras,
)

XHS = PlatformBinding(
    name="xhs",
    import_uploader=_import_xhs,
    apply_platform_extras=apply_location_extras,
)
