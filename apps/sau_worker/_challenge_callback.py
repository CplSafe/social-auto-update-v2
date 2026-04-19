"""Runner-side challenge_callback factory.

Upstream uploader code calls ``challenge_callback({kind, page_url})``
when an SMS challenge surfaces. This module produces a callback that:

1. Creates a Redis-backed ``ChallengeSession`` so dify can see this task
   needs user input (and so dify can dispatch the user's actions back).

2. Polls Redis waiting for the user (via dify) to call one of:
   ``/challenge/{id}/trigger-sms``, ``/submit-code``, ``/abort``.

3. Returns the user's chosen action to the uploader, which executes it
   on the page (via ``perform_sms_action``). The uploader re-checks the
   page DOM; if the challenge cleared, normal flow resumes.

4. Marks the session ``status=completed`` (or ``aborted``) when the
   uploader exits the maybe_emit_challenge loop.

Concurrency note: each publish task runs in its own celery worker with
prefork pool=1, so there's no in-process contention. The worker's
challenge_callback owns the session for its lifetime; dify is the only
external writer of ``pending_action``.

Importantly: this module also publishes the session_id back through the
task's bound ``self.update_state(meta=...)`` so dify can correlate
``social_publish_tasks.id`` ↔ ``challenge_session.session_id`` without
a separate notification channel. The dify-side
``social_publish_task_service._poll_sau`` already polls task state — it
just needs to look at ``meta`` for the session_id.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from apps.sau_api import challenge_sessions
from apps.sau_worker._publish_challenge import (
    ChallengeAction,
    ChallengePayload,
)

logger = logging.getLogger(__name__)

# How often to poll Redis for the user's response. 1.5s is a balance:
# fast enough that the uploader doesn't sit idle long after the user
# submits, slow enough not to hammer Redis with O(N_sessions) hits/sec.
POLL_INTERVAL_SEC = 1.5


def make_challenge_callback(
    *,
    tenant_id: str,
    sau_account_id: str,
    platform: str,
    on_session_created=None,
):
    """Return a ``challenge_callback`` suitable for passing into
    ``DouYinVideo(challenge_callback=...)`` etc.

    ``on_session_created`` is an optional sync hook invoked exactly once,
    with the freshly-minted ``ChallengeSession``, so the celery task can
    publish ``session_id`` to its bound ``self.update_state(meta=...)``
    for dify to pick up.

    The returned callback is created per upload task; it remembers its
    own session_id across multiple invocations within the same upload
    (in case the uploader emits the challenge multiple times — say,
    after a wrong code triggers re-prompt).
    """
    state: dict[str, Any] = {"session_id": None}

    async def callback(payload: ChallengePayload) -> ChallengeAction:
        # First emit: create a fresh session in Redis. Subsequent emits
        # reuse the same session (so wrong-code retries don't spawn a
        # new dify modal each time).
        if state["session_id"] is None:
            session = challenge_sessions.create(
                tenant_id=tenant_id,
                sau_account_id=sau_account_id,
                platform=platform,
                kind=payload["kind"],
                page_url=payload["page_url"],
            )
            state["session_id"] = session.session_id
            if on_session_created is not None:
                try:
                    on_session_created(session)
                except Exception:
                    logger.exception("on_session_created hook raised")
        else:
            # Re-prompt scenario: reset the session back to awaiting_user
            # so dify can collect a new code without confusion.
            challenge_sessions.update(
                state["session_id"],
                status="awaiting_user",
                pending_action=None,
                last_result={"detail": "请重新获取/输入验证码"},
            )

        session_id = state["session_id"]
        logger.info(
            "challenge_callback awaiting user response",
            extra={"session_id": session_id, "kind": payload["kind"]},
        )

        # Poll Redis for the user's action. The session TTL caps the total
        # wait time; we additionally bail at SESSION_WAIT_TIMEOUT_SECONDS
        # (slightly less than TTL) so the uploader gets a clean abort
        # before Redis evicts the session.
        deadline = asyncio.get_event_loop().time() + challenge_sessions.SESSION_WAIT_TIMEOUT_SECONDS
        while True:
            session = challenge_sessions.get(session_id)
            if session is None:
                logger.warning(
                    "challenge session vanished — TTL expired before user responded",
                    extra={"session_id": session_id},
                )
                return {"command": "abort"}

            if session.status == "user_submitted" and session.pending_action:
                action = session.pending_action
                # Mark consumed so the api won't accept a stacked action
                # before this one resolves.
                challenge_sessions.update(
                    session_id,
                    status=("aborted" if action.get("command") == "abort" else "completed"),
                    pending_action=None,
                )
                logger.info(
                    "challenge_callback consumed user action",
                    extra={"session_id": session_id, "command": action.get("command")},
                )
                return action  # type: ignore[return-value]

            if asyncio.get_event_loop().time() > deadline:
                logger.warning(
                    "challenge_callback timed out waiting for user",
                    extra={"session_id": session_id},
                )
                challenge_sessions.update(
                    session_id,
                    status="aborted",
                    last_result={"detail": "等待用户操作超时"},
                )
                return {"command": "abort"}

            await asyncio.sleep(POLL_INTERVAL_SEC)

    return callback
