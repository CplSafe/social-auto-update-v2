"""Sliding-window rate limiter for sau publish tasks.

Mirrors the Dify-side ``libs.helper.RateLimiter`` (Redis sorted-set
trick). Sharded by ``sau_account_id`` for the per-account cap and by a
``_platform`` constant for the platform-wide ceiling.

Why both: a single greedy account shouldn't block the whole platform,
and the platform fanout shouldn't fall over even if every tenant is
just under their per-account budget.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


class TokenBucket:
    """Approximate sliding-window limiter backed by a Redis sorted set.

    The ``capacity / window_sec`` invariant is what callers care about
    in practice (e.g. "≤3 publishes per minute per account"). The exact
    decay shape is approximate; we never block writes that haven't yet
    been ack'd by the cluster — single-cluster Redis is the intended
    deployment.
    """

    def __init__(
        self,
        redis_client,
        *,
        prefix: str,
        capacity: int,
        window_sec: int,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if window_sec <= 0:
            raise ValueError("window_sec must be > 0")
        self._redis = redis_client
        self._prefix = prefix
        self._capacity = capacity
        self._window = window_sec

    def _key(self, bucket: str) -> str:
        return f"{self._prefix}:{bucket}"

    def try_acquire(self, bucket: str) -> RateLimitDecision:
        """Return whether the caller may proceed and (if not) the
        suggested delay before the next attempt."""
        key = self._key(bucket)
        now = time.time()
        cutoff = now - self._window

        # Drop stamps outside the window first so the size reflects the
        # current window only.
        self._redis.zremrangebyscore(key, "-inf", cutoff)
        count = int(self._redis.zcard(key) or 0)
        if count >= self._capacity:
            # Earliest stamp in the window dictates how soon a slot frees up.
            earliest = self._redis.zrange(key, 0, 0, withscores=True)
            if earliest:
                _, score = earliest[0]
                retry_after = max(1, int((float(score) + self._window) - now))
            else:
                retry_after = self._window
            return RateLimitDecision(allowed=False, retry_after_seconds=retry_after)

        # Use a uuid stamp so concurrent acquires don't share a member id.
        member = f"{uuid.uuid4().hex}:{now}"
        self._redis.zadd(key, {member: now})
        # 2x window TTL — generous enough to survive minor clock skew.
        self._redis.expire(key, self._window * 2)
        return RateLimitDecision(allowed=True, retry_after_seconds=0)

    def wait_or_acquire(
        self,
        bucket: str,
        *,
        max_wait_sec: int,
        sleep_func=time.sleep,
    ) -> bool:
        """Spin-wait up to ``max_wait_sec`` for a slot. Returns True iff
        acquired. The sleep is deliberately coarse — these are
        publish-pacing checks, not microsecond-sensitive."""
        deadline = time.time() + max_wait_sec
        while True:
            decision = self.try_acquire(bucket)
            if decision.allowed:
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            sleep_for = min(decision.retry_after_seconds, max(1, int(remaining)))
            logger.info(
                "rate-limited on %s; sleeping %ds (window cap %d/%ds)",
                bucket, sleep_for, self._capacity, self._window,
            )
            sleep_func(sleep_for)
