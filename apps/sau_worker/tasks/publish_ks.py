import logging
from typing import Any

from sau_contracts import PUBLISH_KS, PUBLISH_KS_QUEUE

from apps.sau_worker.celery_app import app

logger = logging.getLogger(__name__)


@app.task(name=PUBLISH_KS, queue=PUBLISH_KS_QUEUE, bind=True)
def publish_ks(
    self: Any,
    tenant_id: str,
    sau_account_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    logger.info(
        "publish_ks received",
        extra={
            "task_id": self.request.id,
            "tenant_id": tenant_id,
            "sau_account_id": sau_account_id,
            "payload_keys": sorted(payload.keys()),
        },
    )
    return {"status": "stub_ok", "platform": "ks"}
