from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sau_contracts import (
    PUBLISH_DOUYIN,
    PUBLISH_DOUYIN_QUEUE,
    PUBLISH_KS,
    PUBLISH_KS_QUEUE,
    PUBLISH_XHS,
    PUBLISH_XHS_QUEUE,
)

from apps.sau_worker.celery_app import app as celery_app

router = APIRouter()

Platform = Literal["douyin", "xhs", "ks"]

_PLATFORM_TO_TASK: dict[str, tuple[str, str]] = {
    "douyin": (PUBLISH_DOUYIN, PUBLISH_DOUYIN_QUEUE),
    "xhs": (PUBLISH_XHS, PUBLISH_XHS_QUEUE),
    "ks": (PUBLISH_KS, PUBLISH_KS_QUEUE),
}


class PublishRequest(BaseModel):
    tenant_id: str
    platform: Platform
    sau_account_id: str
    payload: dict[str, object]


@router.post("/postVideo")
async def post_video(req: PublishRequest) -> dict[str, str]:
    task_name, queue = _PLATFORM_TO_TASK[req.platform]
    result = celery_app.send_task(
        task_name,
        kwargs={
            "tenant_id": req.tenant_id,
            "sau_account_id": req.sau_account_id,
            "payload": req.payload,
        },
        queue=queue,
    )
    return {"sau_task_id": result.id}
