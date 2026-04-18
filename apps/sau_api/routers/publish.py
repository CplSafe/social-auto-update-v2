"""Publish endpoint — accepts a video from Dify api and dispatches the
upload to the matching Celery worker.

Wire shape:

    POST /postVideo
        multipart/form-data:
            video?: <binary>           # P2 multipart path (omit when video_url is set)
            data:   <json string>      # {tenant_id, platform, sau_account_id,
                                       #  title, tags?, desc?, publish_date?,
                                       #  priority?, video_url?}

P2 always sent the bytes inline. P3 adds a ``video_url`` envelope field
(presigned download URL) so the worker fetches the video itself,
bypassing the Dify api process for large files. Exactly one of
``video`` (multipart) or ``video_url`` (envelope field) must be set.

When the bytes are sent inline, they're persisted to
``${SAU_TMP_DIR}/<sau_task_id>.<ext>`` so the Celery worker can read
the file from disk regardless of which process runs the task. The
worker is responsible for unlinking the temp file in its ``finally``
clause. The URL path defers the disk write to the worker.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sau_contracts import (
    PUBLISH_DOUYIN,
    PUBLISH_DOUYIN_QUEUE,
    PUBLISH_XHS,
    PUBLISH_XHS_QUEUE,
)

from apps.sau_api.cookie_paths import Platform
from apps.sau_worker.celery_app import app as celery_app

logger = logging.getLogger(__name__)
router = APIRouter()

_PLATFORM_TO_TASK: dict[str, tuple[str, str]] = {
    "douyin": (PUBLISH_DOUYIN, PUBLISH_DOUYIN_QUEUE),
    "xhs": (PUBLISH_XHS, PUBLISH_XHS_QUEUE),
}


def _tmp_dir() -> Path:
    root = Path(os.getenv("SAU_TMP_DIR", "/app/sau_data/tmp"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Re-tighten permissions in case the dir already existed with a looser mode.
    root.chmod(0o700)
    return root


def _safe_suffix(filename: str) -> str:
    if "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext.isalnum() and len(ext) <= 5:
            return f".{ext}"
    return ".mp4"


def _coerce_priority(value: Any) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return 5
    return max(0, min(9, priority))


@router.post("/postVideo")
async def post_video(
    data: Annotated[str, Form(description="JSON envelope with platform metadata")],
    video: Annotated[UploadFile | None, File(description="The video to publish (multipart path)")] = None,
) -> dict[str, str]:
    try:
        envelope: dict[str, Any] = json.loads(data)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid data json: {exc}") from exc

    tenant_id = str(envelope.get("tenant_id") or "")
    platform = envelope.get("platform")
    sau_account_id = str(envelope.get("sau_account_id") or "")
    if not tenant_id or not sau_account_id or platform not in _PLATFORM_TO_TASK:
        raise HTTPException(
            status_code=400,
            detail="data must include tenant_id, sau_account_id, platform",
        )
    platform_typed: Platform = platform  # type: ignore[assignment]

    video_url = envelope.get("video_url")
    has_video_file = video is not None and (video.filename or video.size)
    if (video_url is None) == (not has_video_file):
        raise HTTPException(
            status_code=400,
            detail="provide exactly one of video (multipart) or video_url (envelope)",
        )

    task_uuid = str(uuid.uuid4())
    final_path: Path | None = None

    if has_video_file:
        # P2 path: persist the upload now so the worker has a stable path.
        assert video is not None
        raw_bytes = await video.read()
        if not raw_bytes:
            raise HTTPException(status_code=400, detail="video payload is empty")
        suffix = _safe_suffix(video.filename or "")
        tmp_root = _tmp_dir()
        final_path = tmp_root / f"{task_uuid}{suffix}"
        final_path.write_bytes(raw_bytes)
        final_path.chmod(0o600)

    payload = {
        k: v
        for k, v in envelope.items()
        if k not in {"tenant_id", "platform", "sau_account_id", "video_url", "priority"}
    }
    priority = _coerce_priority(envelope.get("priority"))

    task_name, queue = _PLATFORM_TO_TASK[platform_typed]
    try:
        async_result = celery_app.send_task(
            task_name,
            task_id=task_uuid,
            kwargs={
                "tenant_id": tenant_id,
                "sau_account_id": sau_account_id,
                "video_path": str(final_path) if final_path is not None else None,
                "video_url": str(video_url) if video_url else None,
                "payload": payload,
            },
            queue=queue,
            priority=priority,
        )
    except Exception:
        # Dispatch failed — clean up the temp file so we don't leak disk.
        if final_path is not None:
            try:
                final_path.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "failed to clean up tmp video after dispatch failure",
                    extra={"path": str(final_path)},
                )
        raise

    logger.info(
        "queued publish task",
        extra={
            "sau_task_id": async_result.id,
            "tenant_id": tenant_id,
            "platform": platform_typed,
            "transport": "url" if video_url else "multipart",
            "priority": priority,
        },
    )

    return {"sau_task_id": async_result.id}
