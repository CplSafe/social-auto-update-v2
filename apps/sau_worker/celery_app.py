import os

from celery import Celery

_BROKER_URL = os.environ["SAU_BROKER_URL"]
_RESULT_BACKEND = os.environ["SAU_RESULT_BACKEND"]

app = Celery(
    "sau",
    broker=_BROKER_URL,
    backend=_RESULT_BACKEND,
    include=[
        "apps.sau_worker.tasks.publish_douyin",
        "apps.sau_worker.tasks.publish_xhs",
        "apps.sau_worker.tasks.publish_ks",
    ],
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="publish_douyin",
)
