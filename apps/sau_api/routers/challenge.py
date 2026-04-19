"""SMS challenge relay endpoints.

Wire dify ↔ sau worker via Redis-backed challenge_session. The runner
(or login flow) creates a session when it detects an SMS challenge; dify
polls + dispatches user actions through these endpoints.

All routes require the standard ``X-Sau-Token`` header (mounted under
the protected router group in ``main.py``).

Routes:

- ``GET  /challenge/{session_id}``           — current session snapshot
- ``POST /challenge/{session_id}/trigger-sms`` — user clicked "send code"
- ``POST /challenge/{session_id}/submit-code`` — user typed the OTP
- ``POST /challenge/{session_id}/abort``     — user gave up
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from apps.sau_api import challenge_sessions

logger = logging.getLogger(__name__)
router = APIRouter()


class SubmitCodeRequest(BaseModel):
    code: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")


def _snapshot(session: challenge_sessions.ChallengeSession) -> dict[str, Any]:
    """Trim the session payload before returning it over the wire.

    We deliberately drop ``page_url`` (could leak account context) and
    ``last_result`` internal fields when sending to dify — the caller
    only needs the user-facing status + kind."""
    return {
        "session_id": session.session_id,
        "platform": session.platform,
        "kind": session.kind,
        "status": session.status,
        "last_action_detail": (
            (session.last_result or {}).get("detail")
            if session.last_result
            else None
        ),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _require_session(session_id: str) -> challenge_sessions.ChallengeSession:
    session = challenge_sessions.get(session_id)
    if session is None:
        # 404 here is correct — the URL path *is* the resource id.
        raise HTTPException(status_code=404, detail="challenge session not found or expired")
    return session


def _require_awaiting(session: challenge_sessions.ChallengeSession) -> None:
    """The user is only allowed to dispatch an action while we're waiting
    for them. Once the runner consumes an action (status=user_submitted)
    or the session is terminal, further requests are rejected."""
    if session.status not in ("awaiting_user", "user_submitted"):
        raise HTTPException(
            status_code=409,
            detail=f"challenge session is in terminal state {session.status!r}",
        )
    if session.pending_action is not None and session.status == "user_submitted":
        raise HTTPException(
            status_code=409,
            detail="another action is already pending consumption by the worker",
        )


@router.get("/challenge/{session_id}")
async def get_challenge(session_id: str) -> dict[str, Any]:
    session = _require_session(session_id)
    return _snapshot(session)


@router.post("/challenge/{session_id}/trigger-sms")
async def trigger_sms(session_id: str) -> dict[str, Any]:
    session = _require_session(session_id)
    _require_awaiting(session)
    updated = challenge_sessions.update(
        session_id,
        pending_action={"command": "trigger_sms"},
        status="user_submitted",
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="session vanished mid-update")
    return _snapshot(updated)


@router.post("/challenge/{session_id}/submit-code")
async def submit_code(session_id: str, payload: SubmitCodeRequest) -> dict[str, Any]:
    session = _require_session(session_id)
    _require_awaiting(session)
    updated = challenge_sessions.update(
        session_id,
        pending_action={"command": "submit_code", "code": payload.code},
        status="user_submitted",
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="session vanished mid-update")
    return _snapshot(updated)


@router.post("/challenge/{session_id}/abort")
async def abort_challenge(session_id: str) -> dict[str, Any]:
    session = _require_session(session_id)
    if session.status in ("completed", "aborted"):
        # Idempotent — re-aborting an already-aborted session is fine.
        return _snapshot(session)
    updated = challenge_sessions.update(
        session_id,
        pending_action={"command": "abort"},
        status="user_submitted",
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="session vanished mid-update")
    return _snapshot(updated)
