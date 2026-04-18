"""TokenBucket rate limiter tests against a fake Redis."""

from __future__ import annotations

from typing import Any

import pytest

from apps.sau_worker.rate_limit import RateLimitDecision, TokenBucket


class _FakeRedis:
    """Minimal in-memory fake of the redis-py methods TokenBucket uses."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.expirations: dict[str, int] = {}

    def zremrangebyscore(self, key: str, _min: Any, max_score: float) -> int:
        bucket = self.zsets.setdefault(key, {})
        doomed = [m for m, s in bucket.items() if s <= float(max_score)]
        for m in doomed:
            bucket.pop(m, None)
        return len(doomed)

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def zrange(self, key: str, start: int, end: int, withscores: bool = False):
        bucket = self.zsets.get(key, {})
        items = sorted(bucket.items(), key=lambda kv: kv[1])
        # end inclusive when end >= 0; ignore negatives in this fake.
        slc = items[start : end + 1] if end >= 0 else items[start:]
        if not withscores:
            return [k for k, _ in slc]
        return slc

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        bucket = self.zsets.setdefault(key, {})
        added = 0
        for m, s in mapping.items():
            if m not in bucket:
                added += 1
            bucket[m] = s
        return added

    def expire(self, key: str, ttl: int) -> int:
        self.expirations[key] = int(ttl)
        return 1


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


class TestTokenBucket:
    def test_allows_first_call_under_capacity(self, fake_redis):
        bucket = TokenBucket(
            fake_redis, prefix="t", capacity=3, window_sec=60
        )
        decision = bucket.try_acquire("acc-1")
        assert decision.allowed is True
        assert decision.retry_after_seconds == 0

    def test_blocks_after_capacity_exhausted(self, fake_redis):
        bucket = TokenBucket(
            fake_redis, prefix="t", capacity=2, window_sec=60
        )
        assert bucket.try_acquire("acc-1").allowed is True
        assert bucket.try_acquire("acc-1").allowed is True
        third = bucket.try_acquire("acc-1")
        assert third.allowed is False
        assert third.retry_after_seconds > 0

    def test_each_bucket_is_isolated(self, fake_redis):
        bucket = TokenBucket(
            fake_redis, prefix="t", capacity=1, window_sec=60
        )
        assert bucket.try_acquire("acc-1").allowed is True
        # acc-1 is full but acc-2 still has room.
        assert bucket.try_acquire("acc-1").allowed is False
        assert bucket.try_acquire("acc-2").allowed is True

    def test_wait_or_acquire_returns_false_after_max_wait(self, fake_redis):
        bucket = TokenBucket(
            fake_redis, prefix="t", capacity=1, window_sec=60
        )
        assert bucket.try_acquire("acc-1").allowed is True

        sleeps: list[float] = []

        def fake_sleep(secs):
            sleeps.append(secs)

        # max_wait_sec=0 means no waiting — must return False immediately.
        ok = bucket.wait_or_acquire(
            "acc-1", max_wait_sec=0, sleep_func=fake_sleep
        )
        assert ok is False

    def test_zero_capacity_rejected(self, fake_redis):
        with pytest.raises(ValueError):
            TokenBucket(fake_redis, prefix="t", capacity=0, window_sec=60)

    def test_negative_window_rejected(self, fake_redis):
        with pytest.raises(ValueError):
            TokenBucket(fake_redis, prefix="t", capacity=1, window_sec=-1)
