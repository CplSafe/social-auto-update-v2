"""Publish-to-Douyin Celery task.

Thin wrapper over the shared ``run_publish`` runner. The runner owns
rate limit, tenant concurrency gate, cookie path resolution, video
download, error classification and tmp-file cleanup. The platform-
specific bits (which uploader, which extras hook) live in
``_publish_runner.DOUYIN``.
"""

from __future__ import annotations

from typing import Any

from sau_contracts import PUBLISH_DOUYIN, PUBLISH_DOUYIN_QUEUE

from apps.sau_worker._publish_rate_limit import hooks_for, tenant_gate
from apps.sau_worker._publish_runner import DOUYIN, run_publish
from apps.sau_worker.celery_app import app


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
    return run_publish(
        self,
        binding=DOUYIN,
        tenant_id=tenant_id,
        sau_account_id=sau_account_id,
        payload=payload,
        video_path=video_path,
        video_url=video_url,
        tier_concurrent=tier_concurrent,
        rate_limit_hooks=hooks_for("douyin"),
        tenant_gate_factory=tenant_gate,
    )
