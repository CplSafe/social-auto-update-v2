"""In-memory login-session registry for the polling auth flow.

Dify's SocialPublishService creates a session_id, calls ``POST /login`` to
kick off scan-to-auth, then polls ``GET /login/status/{session_id}`` until
the session reaches a terminal state. The actual upstream
``douyin_cookie_gen`` runs as an asyncio task and writes its progress here.

Process-local state is intentional: each Dify session is short-lived (≤180s
TTL on the Dify side), and the sau-api process handles a single tenant's
sessions in one place. Multi-process deployments would need a Redis-backed
implementation; that's a P3+ concern when we shard sau workers per-platform.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

SessionStatus = Literal[
    "waiting",
    "scanned",
    "awaiting_user",  # P7: SMS challenge surfaced; user needs to act
    "success",
    "failed",
    "expired",
]
# P7: bumped from 200 to 600 because the awaiting_user state now lives
# inside this same TTL window — the user can take a few minutes to read
# their SMS and type the code.
SESSION_TTL_SECONDS = 600


@dataclass
class LoginSession:
    session_id: str
    tenant_id: str
    platform: str
    sau_account_id: str
    started_at: float = field(default_factory=time.monotonic)
    status: SessionStatus = "waiting"
    qr_image_data_url: str | None = None
    profile: dict[str, Any] | None = None
    message: str | None = None
    task: asyncio.Task[Any] | None = None
    # P7: when status == "awaiting_user", this holds the id of the
    # ``ChallengeSession`` (in apps/sau_api/challenge_sessions.py) that
    # dify can hit to drive the SMS-relay flow.
    challenge_session_id: str | None = None

    def is_terminal(self) -> bool:
        return self.status in ("success", "failed", "expired")

    def is_expired(self) -> bool:
        return time.monotonic() - self.started_at > SESSION_TTL_SECONDS

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "sau_account_id": self.sau_account_id if self.status != "waiting" else None,
            "profile": self.profile,
            "message": self.message,
            "challenge_session_id": self.challenge_session_id,
        }


class LoginSessionRegistry:
    """Thread-safe (asyncio-safe) session registry."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, LoginSession] = {}

    async def create(self, session: LoginSession) -> None:
        async with self._lock:
            self._sessions[session.session_id] = session

    async def get(self, session_id: str) -> LoginSession | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session and session.is_expired() and not session.is_terminal():
                session.status = "expired"
                # Cancel any in-flight Playwright runner so it doesn't later
                # overwrite the terminal "expired" with success/failed.
                if session.task is not None and not session.task.done():
                    session.task.cancel()
            return session

    async def update(self, session_id: str, **fields: Any) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            # Once a session is in a terminal state, refuse status downgrades
            # from late-arriving runner callbacks. Other fields (profile,
            # message, task) may still be updated for diagnostics.
            if session.is_terminal() and "status" in fields:
                fields = {k: v for k, v in fields.items() if k != "status"}
            for key, value in fields.items():
                setattr(session, key, value)

    async def cancel_all(self) -> int:
        """Cancel every live task. Called from the FastAPI shutdown hook so
        long-running Playwright runs don't survive process exit."""
        cancelled = 0
        async with self._lock:
            for session in self._sessions.values():
                task = session.task
                if task is not None and not task.done():
                    task.cancel()
                    cancelled += 1
        return cancelled

    async def reap_expired(self) -> int:
        """Drop terminal+old sessions to keep memory bounded."""
        async with self._lock:
            doomed = [
                sid
                for sid, s in self._sessions.items()
                if s.is_expired() and (s.is_terminal() or s.status == "expired")
            ]
            for sid in doomed:
                self._sessions.pop(sid, None)
            return len(doomed)


registry = LoginSessionRegistry()
