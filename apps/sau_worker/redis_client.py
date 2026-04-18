"""Process-wide Redis client for sau workers.

Connects to the same Redis instance Celery uses (``SAU_BROKER_URL``) but
on a dedicated DB to keep the rate-limit / concurrency keys out of the
broker namespace. Lazy so test suites can monkey-patch the module-level
``_client`` and the redis import never has to fire in CI.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import redis

_client: redis.Redis | None = None


def _build_url() -> str:
    """Derive the auxiliary-Redis URL from SAU_AUX_REDIS_URL or fall back
    to swapping the DB index of SAU_BROKER_URL."""
    explicit = os.getenv("SAU_AUX_REDIS_URL")
    if explicit:
        return explicit
    broker = os.environ["SAU_BROKER_URL"]
    parsed = urlparse(broker)
    # Replace the path (DB index) with the aux DB (default 4).
    aux_db = os.getenv("SAU_AUX_REDIS_DB", "4")
    netloc = parsed.netloc
    return f"{parsed.scheme}://{netloc}/{aux_db}"


def get_redis_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(_build_url(), decode_responses=False)
    return _client


def reset_for_tests() -> None:
    """Drop the cached client so tests can rebuild against a fake."""
    global _client
    _client = None
