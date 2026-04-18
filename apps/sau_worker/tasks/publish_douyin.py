import logging
from typing import Any

from sau_contracts import PUBLISH_DOUYIN, PUBLISH_DOUYIN_QUEUE

from apps.sau_worker.celery_app import app

logger = logging.getLogger(__name__)


@app.task(name=PUBLISH_DOUYIN, queue=PUBLISH_DOUYIN_QUEUE, bind=True)
def publish_douyin(
    self: Any,
    tenant_id: str,
    sau_account_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    logger.info(
        "publish_douyin received",
        extra={
            "task_id": self.request.id,
            "tenant_id": tenant_id,
            "sau_account_id": sau_account_id,
            "payload_keys": sorted(payload.keys()),
        },
    )
    return {"status": "stub_ok", "platform": "douyin"}
