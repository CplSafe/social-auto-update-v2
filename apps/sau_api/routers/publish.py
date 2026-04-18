"""Publish endpoint — accepts multipart upload from Dify api and dispatches
the bytes to the matching Celery worker.

Wire shape (P2):

    POST /postVideo
        multipart/form-data:
            video: <binary>          # the actual file
            data:  <json string>     # {tenant_id, platform, sau_account_id,
                                     #  title, tags?, desc?, publish_date?}

The JSON envelope keeps the Dify side from having to know the exact set of
form fields; everything user-tunable lives inside ``data`` and the worker
parses it.

The video bytes are persisted to ``${SAU_TMP_DIR}/<sau_task_id>.<ext>`` so
the Celery worker can read the file from disk regardless of which process
runs the task. The worker is responsible for unlinking the temp file in
its ``finally`` clause.
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
    PUBLISH_KS,
    PUBLISH_KS_QUEUE,
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
    "ks": (PUBLISH_KS, PUBLISH_KS_QUEUE),
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


@router.post("/postVideo")
async def post_video(
    video: Annotated[UploadFile, File(description="The video to publish")],
    data: Annotated[str, Form(description="JSON envelope with platform metadata")],
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

    raw_bytes = await video.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="video payload is empty")

    # Mint the task id locally so the on-disk filename matches what the
    # worker logs and what Dify polls. Celery accepts a pre-set task_id on
    # send_task and uses it verbatim.
    task_uuid = str(uuid.uuid4())
    suffix = _safe_suffix(video.filename or "")
    tmp_root = _tmp_dir()
    final_path = tmp_root / f"{task_uuid}{suffix}"
    final_path.write_bytes(raw_bytes)
    final_path.chmod(0o600)

    payload = {
        k: v
        for k, v in envelope.items()
        if k not in {"tenant_id", "platform", "sau_account_id"}
    }

    task_name, queue = _PLATFORM_TO_TASK[platform_typed]
    try:
        async_result = celery_app.send_task(
            task_name,
            task_id=task_uuid,
            kwargs={
                "tenant_id": tenant_id,
                "sau_account_id": sau_account_id,
                "video_path": str(final_path),
                "payload": payload,
            },
            queue=queue,
        )
    except Exception:
        # Dispatch failed — clean up the temp file so we don't leak disk.
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
            "size_bytes": len(raw_bytes),
        },
    )

    return {"sau_task_id": async_result.id}
