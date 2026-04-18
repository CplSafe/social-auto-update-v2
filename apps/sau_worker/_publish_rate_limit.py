"""Per-platform rate-limit + concurrency factories.

One TokenBucket pair (per-account + platform-wide) per platform, plus a
single tenant concurrency gate. Cached on first use so the redis client
is only built once per worker process. Test code can drop the singletons
back to None to inject fakes.
"""

from __future__ import annotations

import os
from typing import Any, NamedTuple


class RateLimitHooks(NamedTuple):
    """Bag of two factories — passed into ``run_publish`` so each platform
    task can supply its own redis-prefixed buckets without re-implementing
    the rate-limit knobs."""

    per_account: Any  # callable returning a TokenBucket
    platform: Any  # callable returning a TokenBucket


_HOOKS_BY_PLATFORM: dict[str, RateLimitHooks] = {}
_TENANT_GATE = None


def hooks_for(platform: str) -> RateLimitHooks:
    cached = _HOOKS_BY_PLATFORM.get(platform)
    if cached is not None:
        return cached

    def _per_account_factory():
        return _build_bucket(
            prefix=f"sau:tb:{platform}:account",
            cap_env="SAU_RATELIMIT_PER_ACCOUNT_CAP",
            window_env="SAU_RATELIMIT_PER_ACCOUNT_WINDOW_SEC",
            cap_default="3",
            window_default="60",
        )

    def _platform_factory():
        return _build_bucket(
            prefix=f"sau:tb:{platform}:platform",
            cap_env="SAU_RATELIMIT_PLATFORM_CAP",
            window_env="SAU_RATELIMIT_PLATFORM_WINDOW_SEC",
            cap_default="20",
            window_default="60",
        )

    cached = RateLimitHooks(
        per_account=_lazy_singleton(_per_account_factory),
        platform=_lazy_singleton(_platform_factory),
    )
    _HOOKS_BY_PLATFORM[platform] = cached
    return cached


def tenant_gate():
    global _TENANT_GATE
    if _TENANT_GATE is None:
        from apps.sau_worker.concurrency import TenantConcurrencyGate
        from apps.sau_worker.redis_client import get_redis_client

        _TENANT_GATE = TenantConcurrencyGate(
            get_redis_client(),
            prefix="sau:concurrent:tenant",
            ttl_sec=int(os.getenv("SAU_TENANT_GATE_TTL_SEC", "600")),
        )
    return _TENANT_GATE


def reset_for_tests() -> None:
    """Drop cached singletons so tests can monkey-patch a fresh client."""
    global _TENANT_GATE
    _HOOKS_BY_PLATFORM.clear()
    _TENANT_GATE = None


# ---------- internals ----------


def _build_bucket(*, prefix: str, cap_env: str, window_env: str, cap_default: str, window_default: str):
    from apps.sau_worker.rate_limit import TokenBucket
    from apps.sau_worker.redis_client import get_redis_client

    return TokenBucket(
        get_redis_client(),
        prefix=prefix,
        capacity=int(os.getenv(cap_env, cap_default)),
        window_sec=int(os.getenv(window_env, window_default)),
    )


def _lazy_singleton(factory):
    """Memoise the result of a no-arg factory so the bucket is only
    constructed on first use, but stays a stable instance after."""
    cell = {"value": None}

    def _accessor():
        if cell["value"] is None:
            cell["value"] = factory()
        return cell["value"]

    return _accessor
