"""Redis-backed challenge_session registry for SMS verification relay.

Lifecycle:

1. The runner / login flow detects an SMS challenge mid-flight and calls
   ``create(...)`` to register a session. The session moves to
   ``status="awaiting_user"``.

2. The runner / login flow then loops calling ``get(session_id)`` until
   the user has either submitted a code or aborted (or the 5-min TTL
   expires).

3. Dify polls ``GET /challenge/{id}`` to learn the session is awaiting
   user input, then makes one of three calls:

   - ``POST /challenge/{id}/trigger-sms``  (user clicked "send me the SMS")
     → server writes ``pending_action={"command": "trigger_sms"}``
   - ``POST /challenge/{id}/submit-code``  (user typed the 6-digit code)
     → server writes ``pending_action={"command": "submit_code", "code": "..."}``
   - ``POST /challenge/{id}/abort``        (user gave up)
     → server writes ``pending_action={"command": "abort"}``

4. The runner's polling loop picks up ``pending_action``, hands it back
   to the upstream upload flow as the callback's return value. The
   uploader executes the action via ``perform_sms_action`` and the
   upload either continues (if the challenge cleared) or surfaces the
   abort error.

Storage: Redis HSET at ``sau:challenge:{session_id}``. TTL 5 min hard
limit so a stuck or forgotten session can never leak resources beyond
that window.

Process-local state: this module is the SOURCE of truth across processes
— the worker writes ``status``/``last_result``, the api writes
``pending_action``. We rely on Redis atomic ops only for the simple
read-modify-write paths; the runner's polling loop does its own logic.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Hard cap on a single challenge session. Pulls double duty: TTL on the
# Redis key + safety net for the runner's polling loop.
SESSION_TTL_SECONDS = int(os.getenv("SAU_CHALLENGE_SESSION_TTL_SECONDS", "300"))

# How long the runner will block waiting for a user action before timing
# out and treating the challenge as aborted. Slightly less than TTL so
# the session is still alive when the runner gives up.
SESSION_WAIT_TIMEOUT_SECONDS = int(os.getenv("SAU_CHALLENGE_WAIT_TIMEOUT_SECONDS", "270"))


SessionStatus = Literal[
    "awaiting_user",  # waiting for /trigger-sms or /submit-code
    "user_submitted",  # user submitted, runner has not yet picked up
    "completed",       # runner consumed the action; flow continues
    "aborted",         # user clicked abort or runner timed out
]

ChallengeKind = Literal["sms"]


@dataclass
class ChallengeSession:
    session_id: str
    tenant_id: str
    sau_account_id: str
    platform: str
    kind: ChallengeKind
    page_url: str
    status: SessionStatus = "awaiting_user"
    pending_action: dict[str, Any] | None = None
    last_result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _key(session_id: str) -> str:
    return f"sau:challenge:{session_id}"


def _serialize(session: ChallengeSession) -> bytes:
    return json.dumps(session.to_dict(), ensure_ascii=False).encode("utf-8")


def _deserialize(raw: bytes | None) -> ChallengeSession | None:
    if raw is None:
        return None
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.exception("invalid challenge session payload")
        return None
    return ChallengeSession(**d)


# ---------- API used by the runner / login flow ----------


def create(
    *,
    tenant_id: str,
    sau_account_id: str,
    platform: str,
    kind: ChallengeKind,
    page_url: str,
) -> ChallengeSession:
    """Register a new challenge session and return it. The session_id is
    server-generated to keep callers from minting unguessable ids
    themselves."""
    from apps.sau_worker.redis_client import get_redis_client

    session = ChallengeSession(
        session_id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        sau_account_id=sau_account_id,
        platform=platform,
        kind=kind,
        page_url=page_url,
    )
    client = get_redis_client()
    client.set(_key(session.session_id), _serialize(session), ex=SESSION_TTL_SECONDS)
    logger.info(
        "challenge session created",
        extra={
            "session_id": session.session_id,
            "tenant_id": tenant_id,
            "platform": platform,
            "kind": kind,
        },
    )
    return session


def get(session_id: str) -> ChallengeSession | None:
    from apps.sau_worker.redis_client import get_redis_client

    client = get_redis_client()
    raw = client.get(_key(session_id))
    return _deserialize(raw)


def update(session_id: str, **fields: Any) -> ChallengeSession | None:
    """Read-modify-write a session. Caller is responsible for not
    racing two writers — the runner is the only writer for ``status``
    and ``last_result``; the api is the only writer for
    ``pending_action`` (and only when status=awaiting_user)."""
    from apps.sau_worker.redis_client import get_redis_client

    client = get_redis_client()
    raw = client.get(_key(session_id))
    session = _deserialize(raw)
    if session is None:
        return None
    for k, v in fields.items():
        setattr(session, k, v)
    session.updated_at = time.time()
    # Re-set with TTL refresh so an active session doesn't expire mid-flow.
    client.set(_key(session.session_id), _serialize(session), ex=SESSION_TTL_SECONDS)
    return session


def delete(session_id: str) -> None:
    from apps.sau_worker.redis_client import get_redis_client

    client = get_redis_client()
    client.delete(_key(session_id))
