from fastapi import APIRouter

from apps.sau_worker.celery_app import app as celery_app

router = APIRouter()


@router.get("/tasks/{sau_task_id}")
async def get_task(sau_task_id: str) -> dict[str, object]:
    res = celery_app.AsyncResult(sau_task_id)
    payload: dict[str, object] = {"sau_task_id": sau_task_id, "state": res.state}
    if res.ready():
        if res.successful():
            payload["result"] = res.result
        else:
            payload["error"] = str(res.result)
    else:
        # P7: while the task is still running, expose any custom meta the
        # worker published via ``self.update_state(meta=...)``. We use this
        # to surface ``challenge_session_id`` so dify can render the SMS
        # verification modal without having to wait for the task to finish.
        info = res.info
        if isinstance(info, dict):
            payload["meta"] = info
    return payload
