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
            # Re-prompt scenario: caller's outer loop entered maybe_emit_challenge
            # again for the same browser tab. The session might be in any state:
            #
            #   - "completed": we just consumed an action but the page DOM
            #     hasn't fully cleared yet — DON'T reset, just bail and let
            #     the outer loop sleep/retry. Otherwise we wipe the user's
            #     just-submitted action and they get stuck waiting again.
            #   - "aborted": already terminal, nothing to do.
            #   - "awaiting_user" / "user_submitted": still in flight, leave
            #     it alone; the poll loop below will pick up where it was.
            current = challenge_sessions.get(state["session_id"])
            if current is None:
                # Session expired — start over.
                state["session_id"] = None
                logger.warning(
                    "challenge session %s vanished between emits; recreating",
                    state["session_id"],
                )
                # Recurse-style: re-call ourselves to create a fresh one.
                return await callback(payload)
            if current.status == "aborted":
                # Already terminal — bail out cleanly.
                logger.info(
                    "challenge_callback re-entered after aborted session=%s; returning noop",
                    state["session_id"],
                )
                return {"command": "noop"}
            if current.status == "completed":
                # We marked the session completed when we forwarded the
                # user's submit_code action to the uploader. But the
                # uploader is calling us AGAIN — meaning the SMS dialog
                # is STILL up. Two scenarios:
                #
                #   1. DOM hasn't caught up yet (1-2s after a successful
                #      submit, before the SPA navigates away). Returning
                #      noop here lets the outer loop sleep + retry detect.
                #   2. 抖音 rejected the code (wrong digits, expired, etc.)
                #      and rendered an error tip in the dialog. Sleeping
                #      won't help — the dialog will still be up after the
                #      sleep. We should reset the session to
                #      ``awaiting_user`` so dify keeps the modal open and
                #      the user can submit a fresh code.
                #
                # We can't easily distinguish those two from inside this
                # callback (we don't have ``page`` here). Pragmatic
                # heuristic: noop the FIRST re-entry (covers scenario 1's
                # transient DOM lag); on the SECOND re-entry within ~5s,
                # reset for retry (scenario 2 — the page genuinely
                # didn't navigate away).
                state["completed_reentries"] = state.get("completed_reentries", 0) + 1
                if state["completed_reentries"] < 2:
                    logger.info(
                        "challenge_callback re-entered after completed session=%s "
                        "(attempt %d); returning noop to wait for SPA",
                        state["session_id"],
                        state["completed_reentries"],
                    )
                    return {"command": "noop"}
                # Second re-entry — the dialog didn't navigate away.
                # Treat as a wrong-code rejection and reset for retry.
                logger.warning(
                    "challenge_callback re-entered %d times after completed "
                    "session=%s — assuming submit was rejected, resetting "
                    "for user retry",
                    state["completed_reentries"],
                    state["session_id"],
                )
                challenge_sessions.update(
                    state["session_id"],
                    status="awaiting_user",
                    pending_action=None,
                    last_result={
                        "detail": "平台拒绝了上一次验证码，请重新输入正确的 6 位短信验证码",
                    },
                )
                state["completed_reentries"] = 0
                # Fall through to the poll loop below to await the new
                # user input.
            # Otherwise the session is still awaiting/submitted — fall
            # through to the poll loop below; no reset needed.

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
        poll_count = 0
        while True:
            session = challenge_sessions.get(session_id)
            poll_count += 1
            if poll_count <= 3 or poll_count % 20 == 0:
                # Log first few polls + every 30s thereafter so the operator
                # can see the callback is alive and what it's reading.
                logger.info(
                    "challenge_callback poll #%d session=%s status=%s pending=%s",
                    poll_count,
                    session_id,
                    session.status if session else "<none>",
                    bool(session and session.pending_action),
                )
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
                    "challenge_callback consumed user action: session=%s command=%s",
                    session_id,
                    action.get("command"),
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
