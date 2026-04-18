"""Per-tenant concurrency gate for sau publish workers.

Each tenant has a tier-derived concurrent cap (``high=10``, ``mid=5``,
``low=2`` in P3). A Lua script makes the INCR + cap-check + EXPIRE
atomic in Redis so two prefork workers can't both squeeze past the
limit on the same tenant.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

# Lua script — runs server-side in Redis, so the INCR / DECR / EXPIRE
# can't interleave with another worker's check. Returns the new counter
# value (>0) on success or 0 when the cap would be exceeded.
_ACQUIRE_LUA = """
local n = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
if n > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
return n
"""


class TenantConcurrencyGate:
    """Atomic counter-with-cap, leaky on TTL.

    The TTL exists so a worker that crashes between acquire / release
    doesn't permanently consume a slot — set it to a few times the
    expected publish duration.
    """

    def __init__(
        self,
        redis_client,
        *,
        prefix: str,
        ttl_sec: int = 600,
    ) -> None:
        self._redis = redis_client
        self._prefix = prefix
        self._ttl = ttl_sec
        # SCRIPT LOAD lazily on first use; redis-py caches the SHA.
        self._script = redis_client.register_script(_ACQUIRE_LUA)

    def _key(self, tenant_id: str) -> str:
        return f"{self._prefix}:{tenant_id}"

    def try_acquire(self, tenant_id: str, *, limit: int) -> bool:
        if limit <= 0:
            raise ValueError("limit must be > 0")
        result = self._script(
            keys=[self._key(tenant_id)],
            args=[int(limit), int(self._ttl)],
        )
        return bool(int(result))

    def release(self, tenant_id: str) -> None:
        # Best-effort DECR — the EX guard means a stale counter heals
        # within ``ttl_sec`` even if a release call is lost.
        try:
            new_value = int(self._redis.decr(self._key(tenant_id)))
        except Exception:
            logger.exception("failed to release concurrency slot for %s", tenant_id)
            return
        if new_value < 0:
            # Defensive: an over-release means accounting drifted somewhere.
            # Reset to zero so the counter doesn't go further negative.
            self._redis.set(self._key(tenant_id), 0, ex=self._ttl)

    def wait_or_acquire(
        self,
        tenant_id: str,
        *,
        limit: int,
        max_wait_sec: int,
        poll_interval_sec: float = 1.0,
        sleep_func=time.sleep,
    ) -> bool:
        deadline = time.time() + max_wait_sec
        while True:
            if self.try_acquire(tenant_id, limit=limit):
                return True
            if time.time() >= deadline:
                return False
            sleep_func(poll_interval_sec)

    @contextmanager
    def slot(
        self,
        tenant_id: str,
        *,
        limit: int,
        max_wait_sec: int,
    ) -> Iterator[bool]:
        """Acquire a slot for the duration of the ``with`` block.

        Yields True if acquired, False if we timed out. Always releases
        on exit even if the caller raised."""
        acquired = self.wait_or_acquire(
            tenant_id, limit=limit, max_wait_sec=max_wait_sec
        )
        try:
            yield acquired
        finally:
            if acquired:
                self.release(tenant_id)
