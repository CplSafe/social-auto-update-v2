"""TenantConcurrencyGate tests with a fake Redis that runs the Lua
script as Python."""

from __future__ import annotations

from typing import Any

import pytest

from apps.sau_worker.concurrency import TenantConcurrencyGate


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    # The gate uses ``register_script`` then calls the returned object as
    # ``script(keys=[...], args=[...])``. Match that surface here.
    def register_script(self, src: str):
        del src  # we evaluate the contract directly, no Lua interpreter.

        def runner(keys, args):
            key = keys[0]
            limit = int(args[0])
            ttl = int(args[1])
            n = self.kv.get(key, 0) + 1
            self.kv[key] = n
            self.ttls[key] = ttl
            if n > limit:
                self.kv[key] = n - 1
                return 0
            return n

        return runner

    def decr(self, key: str) -> int:
        n = max(0, self.kv.get(key, 0) - 1)
        self.kv[key] = n
        return n

    def set(self, key: str, value: int, ex: int | None = None) -> bool:
        self.kv[key] = int(value)
        if ex is not None:
            self.ttls[key] = ex
        return True


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


class TestTenantConcurrencyGate:
    def test_acquire_under_limit_then_release(self, fake_redis):
        gate = TenantConcurrencyGate(fake_redis, prefix="g", ttl_sec=60)
        assert gate.try_acquire("tenant-a", limit=2) is True
        assert gate.try_acquire("tenant-a", limit=2) is True
        # Third acquire is denied AND must roll back the INCR.
        assert gate.try_acquire("tenant-a", limit=2) is False
        # Counter should be at limit, not above.
        assert fake_redis.kv["g:tenant-a"] == 2

        gate.release("tenant-a")
        assert gate.try_acquire("tenant-a", limit=2) is True

    def test_release_can_recover_drift_to_zero(self, fake_redis):
        gate = TenantConcurrencyGate(fake_redis, prefix="g", ttl_sec=60)
        # Force the counter to 0 then over-release: must clamp to 0.
        gate.release("tenant-a")
        gate.release("tenant-a")
        assert fake_redis.kv["g:tenant-a"] == 0

    def test_invalid_limit_raises(self, fake_redis):
        gate = TenantConcurrencyGate(fake_redis, prefix="g", ttl_sec=60)
        with pytest.raises(ValueError):
            gate.try_acquire("tenant-a", limit=0)

    def test_slot_context_manager_releases_on_exception(self, fake_redis):
        gate = TenantConcurrencyGate(fake_redis, prefix="g", ttl_sec=60)
        with pytest.raises(RuntimeError):
            with gate.slot("tenant-a", limit=2, max_wait_sec=0) as acquired:
                assert acquired is True
                raise RuntimeError("boom")
        # Even though the body raised, the slot must have been released.
        assert fake_redis.kv["g:tenant-a"] == 0

    def test_slot_yields_false_when_already_at_cap(self, fake_redis):
        gate = TenantConcurrencyGate(fake_redis, prefix="g", ttl_sec=60)
        # Pre-fill the gate.
        gate.try_acquire("tenant-a", limit=1)
        # Second caller times out immediately (max_wait_sec=0).
        with gate.slot("tenant-a", limit=1, max_wait_sec=0) as acquired:
            assert acquired is False
