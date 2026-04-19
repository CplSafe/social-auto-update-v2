import logging
import os

from celery import Celery

# Make our app loggers (apps.sau_worker.*, uploader.*) visible at the
# celery worker console — without this, celery's logging config swallows
# everything that isn't a celery internal log line, so SMS challenge
# diagnostics ("step=input via #uc-second-verify", "auto-clicked
# 获取验证码", etc.) never surface and operators can't tell what's
# happening inside the chromium tab. Honour SAU_LOG_LEVEL for prod
# tuning. Using force=True so celery's own handler wiring (which runs
# later in the worker bootstrap) doesn't undo our level.
_log_level = os.getenv("SAU_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)

_BROKER_URL = os.environ["SAU_BROKER_URL"]
_RESULT_BACKEND = os.environ["SAU_RESULT_BACKEND"]

app = Celery(
    "sau",
    broker=_BROKER_URL,
    backend=_RESULT_BACKEND,
    include=[
        "apps.sau_worker.tasks.publish_douyin",
        "apps.sau_worker.tasks.publish_xhs",
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
