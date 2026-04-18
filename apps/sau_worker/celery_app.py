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
    # Default priority for tasks dispatched without an explicit value —
    # corresponds to the "mid" tier in the Dify-side TierResolver.
    task_default_priority=5,
    # P3: priority queueing on Redis broker. Without these knobs Celery
    # ignores the per-task ``priority`` kw-arg and FIFOs everything.
    broker_transport_options={
        "priority_steps": list(range(10)),
        "sep": ":",
        "queue_order_strategy": "priority",
    },
)
