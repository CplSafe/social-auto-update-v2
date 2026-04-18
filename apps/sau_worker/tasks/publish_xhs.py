"""Publish-to-Xiaohongshu Celery task — same shape as publish_douyin."""

from __future__ import annotations

from typing import Any

from sau_contracts import PUBLISH_XHS, PUBLISH_XHS_QUEUE

from apps.sau_worker._publish_rate_limit import hooks_for, tenant_gate
from apps.sau_worker._publish_runner import XHS, run_publish
from apps.sau_worker.celery_app import app


@app.task(name=PUBLISH_XHS, queue=PUBLISH_XHS_QUEUE, bind=True)
def publish_xhs(
    self: Any,
    tenant_id: str,
    sau_account_id: str,
    payload: dict[str, Any],
    video_path: str | None = None,
    video_url: str | None = None,
    tier_concurrent: int | None = None,
) -> dict[str, Any]:
    return run_publish(
        self,
        binding=XHS,
        tenant_id=tenant_id,
        sau_account_id=sau_account_id,
        payload=payload,
        video_path=video_path,
        video_url=video_url,
        tier_concurrent=tier_concurrent,
        rate_limit_hooks=hooks_for("xhs"),
        tenant_gate_factory=tenant_gate,
    )
