"""Unit tests for the in-memory login session registry."""

from __future__ import annotations

import asyncio
import time

from apps.sau_api.login_sessions import (
    SESSION_TTL_SECONDS,
    LoginSession,
    LoginSessionRegistry,
)


def _new_session(**overrides) -> LoginSession:
    defaults = {
        "session_id": "sid-1",
        "tenant_id": "tenant-a",
        "platform": "douyin",
        "sau_account_id": "dy-abc",
    }
    defaults.update(overrides)
    return LoginSession(**defaults)


class TestRegistryRoundTrip:
    async def test_create_and_get_round_trip(self):
        reg = LoginSessionRegistry()
        await reg.create(_new_session(session_id="sid-x"))
        got = await reg.get("sid-x")
        assert got is not None
        assert got.session_id == "sid-x"
        assert got.status == "waiting"

    async def test_get_missing_returns_none(self):
        reg = LoginSessionRegistry()
        assert await reg.get("nope") is None

    async def test_update_overwrites_named_fields_only(self):
        reg = LoginSessionRegistry()
        await reg.create(_new_session(session_id="sid-1"))
        await reg.update("sid-1", status="success", profile={"display_name": "x"})
        got = await reg.get("sid-1")
        assert got is not None
        assert got.status == "success"
        assert got.profile == {"display_name": "x"}
        # Untouched fields remain.
        assert got.tenant_id == "tenant-a"


class TestExpiry:
    async def test_expired_non_terminal_session_flips_to_expired_on_read(self):
        reg = LoginSessionRegistry()
        # Manually backdate the session past the TTL.
        s = _new_session(session_id="sid-old")
        s.started_at = time.monotonic() - SESSION_TTL_SECONDS - 1
        await reg.create(s)
        got = await reg.get("sid-old")
        assert got is not None
        assert got.status == "expired"

    async def test_expired_terminal_session_keeps_terminal_status(self):
        reg = LoginSessionRegistry()
        s = _new_session(session_id="sid-old-success", status="success")
        s.started_at = time.monotonic() - SESSION_TTL_SECONDS - 1
        await reg.create(s)
        got = await reg.get("sid-old-success")
        assert got is not None
        assert got.status == "success"

    async def test_reap_drops_expired_terminal_sessions(self):
        reg = LoginSessionRegistry()
        # Mix of fresh + expired+terminal + expired+nonterminal.
        await reg.create(_new_session(session_id="fresh"))

        s_old_done = _new_session(session_id="old-done", status="success")
        s_old_done.started_at = time.monotonic() - SESSION_TTL_SECONDS - 5
        await reg.create(s_old_done)

        # Expired non-terminal — reap only after first /get flips it to expired.
        s_old_open = _new_session(session_id="old-open")
        s_old_open.started_at = time.monotonic() - SESSION_TTL_SECONDS - 5
        await reg.create(s_old_open)
        await reg.get("old-open")

        dropped = await reg.reap_expired()
        assert dropped == 2
        assert await reg.get("fresh") is not None
        assert await reg.get("old-done") is None
        assert await reg.get("old-open") is None


class TestConcurrentAccess:
    async def test_concurrent_updates_do_not_lose_writes(self):
        reg = LoginSessionRegistry()
        await reg.create(_new_session(session_id="sid-c"))

        async def bump(value: int) -> None:
            await reg.update("sid-c", message=f"msg-{value}")

        # 50 concurrent updates — last write wins, but no exception.
        await asyncio.gather(*[bump(i) for i in range(50)])
        got = await reg.get("sid-c")
        assert got is not None
        assert got.message is not None
        assert got.message.startswith("msg-")


class TestTerminalStateGuard:
    async def test_update_does_not_overwrite_terminal_status(self):
        # A late runner callback must NOT downgrade an already-terminal
        # session — otherwise an "expired" session could flip back to
        # "success" after lazy expiry or shutdown cancellation.
        reg = LoginSessionRegistry()
        await reg.create(_new_session(session_id="sid-t", status="expired"))
        await reg.update("sid-t", status="success", message="late callback")
        got = await reg.get("sid-t")
        assert got is not None
        assert got.status == "expired"
        # Non-status fields still update.
        assert got.message == "late callback"


class TestCancelAll:
    async def test_cancel_all_cancels_only_in_flight_tasks(self):
        reg = LoginSessionRegistry()

        async def long_running() -> None:
            await asyncio.sleep(60)

        live = _new_session(session_id="sid-live")
        live.task = asyncio.create_task(long_running())
        await reg.create(live)

        # Pre-cancelled task — cancel_all must not double-count it.
        already = _new_session(session_id="sid-done", status="success")
        already.task = asyncio.create_task(long_running())
        already.task.cancel()
        try:
            await already.task
        except asyncio.CancelledError:
            pass
        await reg.create(already)

        cancelled = await reg.cancel_all()
        assert cancelled == 1
        # Yield once so the cancellation is actually delivered.
        await asyncio.sleep(0)
        assert live.task.cancelled()


class TestExpiryCancelsTask:
    async def test_lazy_expiry_cancels_in_flight_task(self):
        reg = LoginSessionRegistry()

        async def long_running() -> None:
            await asyncio.sleep(60)

        s = _new_session(session_id="sid-exp")
        s.started_at = time.monotonic() - SESSION_TTL_SECONDS - 1
        s.task = asyncio.create_task(long_running())
        await reg.create(s)

        got = await reg.get("sid-exp")
        assert got is not None
        assert got.status == "expired"
        await asyncio.sleep(0)
        assert s.task.cancelled()
