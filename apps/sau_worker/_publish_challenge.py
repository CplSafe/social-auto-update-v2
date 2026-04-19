"""SMS verification challenge: detection + page actions + callback emit.

Background: 抖音/小红书在登录扫码后或者发布操作过程中，会随机触发
「请输入手机号收到的短信验证码」二次验证。upstream uploader 完全没有
处理这种情况——patchright 会一直等待目标元素出现，最终超时失败。

Approach mirrors upstream's existing ``qrcode_callback`` pattern: every
fork-patched uploader entry point grows a ``challenge_callback`` parameter,
and we sprinkle ``await maybe_emit_challenge(page, callback)`` calls at
the handful of points in ``upload()`` / login flows where the challenge
is most likely to surface. The callback is implemented by the runner
(``_publish_runner.py``) and the login route (``login_sse.py``); both
plumb user actions through Redis-backed ``challenge_session`` state.

Module layout:

- ``detect_sms_challenge(page)`` — read-only DOM probe; returns the
  challenge kind ("sms") or None. Conservative: requires multiple
  marker keywords to co-occur so a regular login page (which mentions
  "验证码" once for the QR description) doesn't false-positive.
- ``perform_sms_action(page, action)`` — runs a user-supplied action
  ("trigger_sms" / "submit_code") on the page. This is where the
  platform-specific selectors live; keeping them out of the upstream
  fork patch makes future upstream sync trivial.
- ``maybe_emit_challenge(page, callback)`` — call site helper. The
  fork patches in ``uploader/*/main.py`` only call this; everything
  else stays in our worker module.

Callback contract:

    payload = {"kind": "sms", "page_url": "https://..."}
    response = await callback(payload)
    # response is None (callback chose not to handle, treat as no-op)
    # or a dict with "command" in {"trigger_sms", "submit_code", "abort"}
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

logger = logging.getLogger(__name__)

ChallengeKind = Literal["sms"]


class ChallengePayload(TypedDict):
    """Payload handed to ``challenge_callback`` when a challenge surfaces."""

    kind: ChallengeKind
    page_url: str


class ChallengeAction(TypedDict, total=False):
    """Response from ``challenge_callback``. Total=False so callbacks
    can return ``{}`` for no-op."""

    command: Literal["trigger_sms", "submit_code", "abort", "noop"]
    code: str  # only when command == "submit_code"


ChallengeCallback = Callable[
    [ChallengePayload],
    "Awaitable[ChallengeAction | None] | ChallengeAction | None",
]


# ---------- detection ----------

# 短信验证页面 DOM 关键字。要求：
# - 必须是显式提到「短信」「验证码」的字眼，避免误判普通登录页
# - 同时出现 ≥ 2 个关键字才算命中（降低误判率）
_SMS_MARKERS = (
    "短信验证码",
    "请输入手机号收到的",
    "获取验证码",
    "重新发送",
    "请输入验证码",
)

# Minimum markers that must co-occur for an SMS challenge to be confirmed.
# 1 marker is too lenient (the word "验证码" alone shows up on the login page);
# 2 markers reliably signals "we're on a real SMS challenge page".
_SMS_MIN_MARKERS = 2


async def detect_sms_challenge(page) -> ChallengeKind | None:
    """Return ``"sms"`` if the page is the SMS verification challenge.

    Returns ``None`` if the page is anything else — the regular login QR,
    the upload form, the publish progress screen, etc.
    """
    try:
        hits = 0
        for marker in _SMS_MARKERS:
            if await page.get_by_text(marker).count() > 0:
                hits += 1
                if hits >= _SMS_MIN_MARKERS:
                    return "sms"
        return None
    except Exception:
        # Page closed / navigated mid-check — not a challenge, just retry next tick.
        return None


# ---------- page actions ----------


# Selectors for the "get verification code" button on each platform's
# challenge page. We try them in order; the first one that matches wins.
_TRIGGER_SMS_SELECTORS = (
    'button:has-text("获取验证码")',
    'button:has-text("获取短信验证码")',
    'a:has-text("获取验证码")',
    '[role="button"]:has-text("获取验证码")',
)

# Selectors for the SMS code input field. Prefer placeholder-based
# matching since both platforms use it consistently.
_SMS_INPUT_SELECTORS = (
    'input[placeholder*="验证码"]',
    'input[type="tel"][maxlength="6"]',
    'input[type="text"][maxlength="6"]',
)

# Selectors for the "next" / "submit" button after entering the code.
_SUBMIT_BUTTON_SELECTORS = (
    'button:has-text("下一步")',
    'button:has-text("确定")',
    'button:has-text("提交")',
    'button:has-text("登录")',
)


async def _click_first_match(page, selectors: tuple[str, ...]) -> bool:
    """Click the first selector that finds at least one element. Return
    True on click, False if nothing matched (caller decides what to do)."""
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                await loc.click()
                return True
        except Exception:
            logger.debug("click attempt failed for selector %s", sel, exc_info=True)
    return False


async def _fill_first_match(page, selectors: tuple[str, ...], value: str) -> bool:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                await loc.fill(value)
                return True
        except Exception:
            logger.debug("fill attempt failed for selector %s", sel, exc_info=True)
    return False


async def perform_sms_action(page, action: ChallengeAction) -> dict[str, Any]:
    """Execute the user-requested action on the SMS challenge page.

    Returns a status dict the caller can surface back to the user:
    ``{"ok": bool, "detail": str}``.
    """
    command = action.get("command")
    if command == "trigger_sms":
        ok = await _click_first_match(page, _TRIGGER_SMS_SELECTORS)
        return {
            "ok": ok,
            "detail": "短信触发成功" if ok else "未找到「获取验证码」按钮",
        }
    if command == "submit_code":
        code = (action.get("code") or "").strip()
        if not (code.isdigit() and 4 <= len(code) <= 8):
            return {"ok": False, "detail": "验证码格式不合法"}
        filled = await _fill_first_match(page, _SMS_INPUT_SELECTORS, code)
        if not filled:
            return {"ok": False, "detail": "未找到验证码输入框"}
        clicked = await _click_first_match(page, _SUBMIT_BUTTON_SELECTORS)
        return {
            "ok": clicked,
            "detail": "验证码已提交" if clicked else "验证码已填入但未找到提交按钮",
        }
    if command == "abort":
        return {"ok": True, "detail": "用户取消验证"}
    if command == "noop":
        return {"ok": True, "detail": "noop"}
    return {"ok": False, "detail": f"未知 command: {command!r}"}


# ---------- callback emit (called from upstream uploader fork patches) ----------


async def maybe_emit_challenge(page, challenge_callback: ChallengeCallback | None) -> None:
    """Probe the page for a challenge; if found, hand off to the callback
    and execute whatever action the callback returns. Loops until the page
    no longer shows a challenge (callback succeeded) or the callback says
    to abort.

    Designed to be called from inside the upstream upload() / login flow
    at points where a challenge is most likely to surface (after page
    navigation, before clicking publish, etc.). If no callback is wired
    up, this is a single cheap DOM probe followed by an early return —
    safe to sprinkle around.

    Raises ``VerificationAbortedError`` if the callback returns
    ``{"command": "abort"}``; the upstream caller should let it propagate
    so the wrapping runner / login flow can mark the task failed.
    """
    if challenge_callback is None:
        return

    # The callback loop runs until the challenge disappears OR the user
    # aborts. We re-detect after each action because typing the wrong
    # code keeps you on the same challenge page (need to retry); typing
    # the right code clears it (we exit).
    while True:
        kind = await detect_sms_challenge(page)
        if kind is None:
            return

        payload: ChallengePayload = {"kind": kind, "page_url": page.url}
        response_or_awaitable = challenge_callback(payload)
        action: ChallengeAction | None
        if inspect.isawaitable(response_or_awaitable):
            action = await response_or_awaitable
        else:
            action = response_or_awaitable

        if action is None or action.get("command") in (None, "noop"):
            # Callback declined to handle — treat the challenge as fatal
            # so we don't busy-loop here.
            raise VerificationAbortedError(
                kind=kind,
                reason="callback returned no action",
            )

        if action.get("command") == "abort":
            raise VerificationAbortedError(
                kind=kind,
                reason="user aborted",
            )

        result = await perform_sms_action(page, action)
        logger.info(
            "challenge action executed: command=%s ok=%s detail=%s",
            action.get("command"),
            result["ok"],
            result["detail"],
        )
        # Loop: re-detect. If submit_code succeeded, the challenge page
        # navigates away and detect returns None on the next pass.


# ---------- exceptions ----------


class VerificationAbortedError(Exception):
    """Raised inside upstream upload() when the callback signals that the
    user can't / won't complete the SMS verification. The wrapping runner
    catches this and reports a typed error back to dify."""

    def __init__(self, *, kind: ChallengeKind, reason: str) -> None:
        super().__init__(f"verification aborted: kind={kind}, reason={reason}")
        self.kind = kind
        self.reason = reason
