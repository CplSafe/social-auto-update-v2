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
#
# 抖音 SMS 验证是一个**两步**流程：
#
#   STEP A. 「身份验证」选择页：上面有「接收短信验证码」+「发送短信验证」
#           两个选项 button。这只是一个跳转中间页，用户不需要看到。
#   STEP B. 「接收短信验证码」输入页：上面有「短信已发送至 ***」+ 验证码
#           输入框 + 验证按钮 + "58s 后重新发送" 倒计时。这是真正需要用户
#           操作的页面（输入验证码）。
#
# 我们的策略：
#   - 检测到 STEP A → ``perform_sms_action(advance_to_input)`` 自动点击
#     「接收短信验证码」前进到 STEP B。这一步不暴露给 dify 用户。
#   - 检测到 STEP B → 暴露给 dify。用户点 trigger_sms 让我们点「重新发送」
#     （进 STEP B 已经自动发了一次，这个按钮是 60s 倒计时之后才能点）；
#     或直接 submit_code 把验证码 fill 进去。

# STEP A: 「身份验证」选择页关键字。要求 2 个都命中（降低误判）。
_SMS_CHOOSER_MARKERS = (
    "身份验证",
    "接收短信验证码",
)
_SMS_CHOOSER_MIN_MARKERS = 2

# STEP B: 「接收短信验证码」输入页关键字。「短信已发送至」是这一页的
# 强特征（其他页面不会出现），单独命中即可；其他作为后备。
_SMS_INPUT_PAGE_MARKERS = (
    "短信已发送至",
    "请输入验证码",
    "请输入手机号收到的",
)


async def detect_sms_challenge(page) -> ChallengeKind | None:
    """Return ``"sms"`` if the page is on either step of the SMS challenge.

    Distinguishes the two steps via :func:`_detect_sms_step` — see that
    helper for the precise classification. Callers that only need a binary
    "is this an SMS flow?" answer use this; callers that need to know
    which step (chooser vs input page) call ``_detect_sms_step`` directly.
    """
    step = await _detect_sms_step(page)
    return "sms" if step is not None else None


async def _detect_sms_step(page) -> Literal["chooser", "input"] | None:
    """Classify which step of the SMS challenge the page is on, or None.

    The order matters: input-page detection runs first because the
    chooser's '短信验证码' phrase is technically a substring of the input
    page's '接收短信验证码' header — without ordering we'd false-positive
    a chooser hit on an input page.
    """
    try:
        # STEP B (input page) — strong single marker.
        for marker in _SMS_INPUT_PAGE_MARKERS:
            if await page.get_by_text(marker).count() > 0:
                return "input"
        # STEP A (chooser page) — needs both markers to avoid matching a
        # generic page that just mentions "身份验证" in some help text.
        hits = 0
        for marker in _SMS_CHOOSER_MARKERS:
            if await page.get_by_text(marker).count() > 0:
                hits += 1
        if hits >= _SMS_CHOOSER_MIN_MARKERS:
            return "chooser"
        return None
    except Exception:
        # Page closed / navigated mid-check — treat as no challenge.
        return None


# ---------- page actions ----------


# STEP A → STEP B: the「接收短信验证码」row on the chooser page. Clicking
# it advances to the input page; the chooser is just a router. Clickable
# element on 抖音 isn't a real <button> — it's a div with role-ish
# semantics, so :has-text matching is the most resilient.
_CHOOSER_ADVANCE_SELECTORS = (
    'button:has-text("接收短信验证码")',
    'div[role="button"]:has-text("接收短信验证码")',
    'div:has-text("接收短信验证码")',
    'a:has-text("接收短信验证码")',
)

# STEP B: the「重新发送」/ trigger-resend button. Initial entry to the
# input page already dispatches one SMS automatically (the page header
# says "短信已发送至 ***" the moment you land); the user only ever needs
# this button when the first SMS didn't arrive and the 60s countdown is
# done.
_TRIGGER_SMS_SELECTORS = (
    'button:has-text("重新发送")',
    'a:has-text("重新发送")',
    'span:has-text("重新发送")',
    # Legacy upstream selectors kept as a fallback for non-douyin platforms
    # that may use different wording.
    'button:has-text("获取验证码")',
    'button:has-text("获取短信验证码")',
)

# STEP B: the SMS code input field. Prefer placeholder-based matching
# since both platforms use it consistently.
_SMS_INPUT_SELECTORS = (
    'input[placeholder*="请输入验证码"]',
    'input[placeholder*="验证码"]',
    'input[type="tel"][maxlength="6"]',
    'input[type="text"][maxlength="6"]',
)

# STEP B: the「验证」/ submit button. 抖音's button literally says "验证";
# other platforms or older flows may use "下一步" / "确定" / "提交".
_SUBMIT_BUTTON_SELECTORS = (
    'button:has-text("验证")',
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
        # On 抖音 the first SMS is already auto-dispatched when we land on
        # the input page, so this is effectively a「重新发送」. The button
        # is disabled during the 60s countdown — clicking before then is
        # a no-op as far as the platform is concerned, but our selector
        # match still returns True. Detail message reflects that nuance.
        ok = await _click_first_match(page, _TRIGGER_SMS_SELECTORS)
        return {
            "ok": ok,
            "detail": "重新发送已触发" if ok else "未找到「重新发送」按钮（可能仍在倒计时）",
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

    抖音 splits the SMS flow into two pages (see ``_detect_sms_step``).
    The chooser page (STEP A) is automatically advanced past — it's just
    a router with no meaningful user choice for our use case. Only when
    we reach the input page (STEP B) do we surface the challenge to dify.

    Raises ``VerificationAbortedError`` if the callback returns
    ``{"command": "abort"}``; the upstream caller should let it propagate
    so the wrapping runner / login flow can mark the task failed.
    """
    if challenge_callback is None:
        return

    # Cap on auto-advance attempts so a flaky chooser-page selector can't
    # spin forever burning DOM-query CPU.
    max_advances = 3
    advance_count = 0

    # The loop runs until we leave the SMS challenge entirely OR the user
    # aborts. After each user-side action we re-detect because:
    #   - submit_code success → navigates away → detect returns None
    #   - wrong code → stays on input page → re-prompt user
    while True:
        step = await _detect_sms_step(page)
        if step is None:
            return

        # STEP A: auto-click「接收短信验证码」to advance to the input page.
        # No user interaction — the chooser page is a router, not a real
        # decision point for our flow.
        if step == "chooser":
            if advance_count >= max_advances:
                logger.warning(
                    "exceeded max chooser-advance attempts (%d); aborting",
                    max_advances,
                )
                raise VerificationAbortedError(
                    kind="sms",
                    reason="chooser page advance failed repeatedly",
                )
            advance_count += 1
            advanced = await _click_first_match(page, _CHOOSER_ADVANCE_SELECTORS)
            if not advanced:
                logger.warning(
                    "chooser page detected but「接收短信验证码」button not found",
                )
                raise VerificationAbortedError(
                    kind="sms",
                    reason="chooser advance button missing",
                )
            # Give the SPA a beat to navigate before re-detecting.
            try:
                import asyncio
                await asyncio.sleep(1.5)
            except Exception:
                pass
            continue

        # STEP B: input page. Surface to dify so the user can submit the
        # OTP. Note: 抖音 already auto-dispatches the SMS the moment we
        # land on the input page (the header reads "短信已发送至 ***"),
        # so the user can usually skip ``trigger_sms`` and go straight to
        # ``submit_code``. They only need ``trigger_sms`` if the first
        # SMS didn't arrive and the 60s countdown is up.
        payload: ChallengePayload = {"kind": "sms", "page_url": page.url}
        response_or_awaitable = challenge_callback(payload)
        action: ChallengeAction | None
        if inspect.isawaitable(response_or_awaitable):
            action = await response_or_awaitable
        else:
            action = response_or_awaitable

        if action is None or action.get("command") in (None, "noop"):
            raise VerificationAbortedError(
                kind="sms",
                reason="callback returned no action",
            )

        if action.get("command") == "abort":
            raise VerificationAbortedError(
                kind="sms",
                reason="user aborted",
            )

        result = await perform_sms_action(page, action)
        logger.info(
            "challenge action executed: command=%s ok=%s detail=%s",
            action.get("command"),
            result["ok"],
            result["detail"],
        )
        # Loop: re-detect. If submit_code succeeded, the page navigates
        # away and detect returns None on the next pass.


# ---------- exceptions ----------


class VerificationAbortedError(Exception):
    """Raised inside upstream upload() when the callback signals that the
    user can't / won't complete the SMS verification. The wrapping runner
    catches this and reports a typed error back to dify."""

    def __init__(self, *, kind: ChallengeKind, reason: str) -> None:
        super().__init__(f"verification aborted: kind={kind}, reason={reason}")
        self.kind = kind
        self.reason = reason
