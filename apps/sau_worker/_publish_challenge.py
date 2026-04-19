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

# STEP B: 「接收短信验证码」输入页关键字。两种 variant 都要覆盖：
#   - 登录路径：进 input 页时已自动发送 SMS，header 显示「短信已发送至 ***」
#   - 发布路径：直接弹「接收短信验证码」弹窗，header 是「请输入当前手机号
#     177***** 收到的短信验证码」，需要用户先点「获取验证码」才会发送
# 任一关键字命中即可识别为 input 页面。
_SMS_INPUT_PAGE_MARKERS = (
    "短信已发送至",
    "请输入当前手机号",
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
                logger.info(
                    "SMS challenge detected: step=input (marker=%r)",
                    marker,
                )
                return "input"
        # STEP A (chooser page) — needs both markers to avoid matching a
        # generic page that just mentions "身份验证" in some help text.
        hits: list[str] = []
        for marker in _SMS_CHOOSER_MARKERS:
            if await page.get_by_text(marker).count() > 0:
                hits.append(marker)
        if len(hits) >= _SMS_CHOOSER_MIN_MARKERS:
            logger.info(
                "SMS challenge detected: step=chooser (markers=%s)",
                hits,
            )
            return "chooser"
        return None
    except Exception:
        # Page closed / navigated mid-check — treat as no challenge.
        logger.debug("detect_sms_step raised", exc_info=True)
        return None


# ---------- page actions ----------


# STEP A → STEP B: the「接收短信验证码」row on the chooser page. Clicking
# it advances to the input page; the chooser is just a router. 抖音's
# clickable row is a deeply-nested <div> tree without role/button
# semantics, so we use Playwright's text engine (get_by_text) at click
# time instead of CSS :has-text — text engine matches the leaf node
# carrying the visible label, then we click() walks up to the nearest
# clickable ancestor automatically.
_CHOOSER_ADVANCE_TEXT = "接收短信验证码"

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


async def _dump_page_for_debug(page, label: str) -> None:
    """Save the current page HTML + screenshot under SAU_TMP_DIR for
    operator inspection when our SMS automation can't find what it
    expected. Helps diagnose抖音 DOM changes without making the user
    re-trigger the flow each time we need a closer look.

    Best-effort: never raise — failure here is purely diagnostic noise.
    """
    import os as _os
    import time as _time
    try:
        tmp_root = _os.getenv(
            "SAU_TMP_DIR", "/Users/guijinhao/Documents/social-auto-upload/.sau_data/tmp",
        )
        debug_dir = _os.path.join(tmp_root, "challenge_debug")
        _os.makedirs(debug_dir, exist_ok=True)
        ts = _time.strftime("%Y%m%d_%H%M%S")
        html_path = _os.path.join(debug_dir, f"{label}_{ts}.html")
        png_path = _os.path.join(debug_dir, f"{label}_{ts}.png")
        try:
            html = await page.content()
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(html)
        except Exception:
            logger.debug("page.content() failed", exc_info=True)
        try:
            await page.screenshot(path=png_path, full_page=True, timeout=5000)
        except Exception:
            logger.debug("page.screenshot() failed", exc_info=True)
        logger.warning(
            "SMS automation snapshot saved: html=%s png=%s — please send these "
            "to the dify team if the SMS flow keeps failing",
            html_path,
            png_path,
        )
    except Exception:
        logger.debug("debug dump failed", exc_info=True)


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


async def _detect_sms_error(page) -> str | None:
    """Return the text of any error tip 抖音 has rendered inside the SMS
    dialog after a submit attempt, or None if there's no error.

    抖音 puts wrong-code errors in a div with class
    ``uc_verification_err_tip-XXX`` (suffix is hashed but the prefix is
    stable). The element exists in the DOM whether or not there's an
    error — when empty it has no text content. We only return non-empty
    text content.
    """
    for scope_label, scope in await _find_sms_dialog_scopes(page):
        try:
            err = scope.locator('[class*="uc_verification_err_tip-"]').first
            if await err.count() == 0:
                continue
            text = (await err.inner_text(timeout=1000)).strip()
            if text:
                logger.info(
                    "SMS error tip detected (scope=%s): %r",
                    scope_label, text,
                )
                return text
        except Exception:
            logger.debug(
                "error-tip probe failed (scope=%s)", scope_label, exc_info=True,
            )
    # Fallback: look for inline red text via common Semi/抖音 error classes.
    for sel in (
        '[class*="error"]:visible',
        '[class*="err-tip"]:visible',
        '[class*="errorMessage"]:visible',
    ):
        try:
            for scope_label, scope in await _find_sms_dialog_scopes(page):
                loc = scope.locator(sel).first
                if await loc.count() == 0:
                    continue
                text = (await loc.inner_text(timeout=1000)).strip()
                # Filter out generic "no error" placeholders + truncate
                # (some Semi error spans render the entire error code).
                if text and len(text) <= 50:
                    logger.info(
                        "SMS error fallback hit (scope=%s sel=%s): %r",
                        scope_label, sel, text,
                    )
                    return text
        except Exception:
            pass
    return None


async def _find_sms_dialog_scopes(page) -> list[tuple[str, Any]]:
    """Find Locator scopes that are guaranteed inside the SMS dialog
    (and NOT the underlying creator/login page that lives in the same
    DOM but is overlaid by the modal).

    Returns ``[(label, locator), ...]`` ordered by preference; callers
    iterate until a child element search succeeds.

    抖音 renders both the modal and the underlying page in the same
    DOM tree. Inputs/buttons in the underlying page match the same
    selectors as the modal, and ``.first`` picks the underlying one
    because it appears earlier in document order. Scoping every
    locator to the modal element fixes that.
    """
    scopes: list[tuple[str, Any]] = []
    # Strategy A (BEST): #uc-second-verify is抖音's stable id for the
    # second-factor verification modal root. Confirmed via DOM inspection
    # to wrap both the input and submit buttons. Uses a CSS id selector
    # which is the cheapest possible lookup and least ambiguous.
    try:
        d = page.locator("#uc-second-verify").first
        if await d.count() > 0 and await d.is_visible():
            scopes.append(("#uc-second-verify", d))
    except Exception:
        pass
    # Strategy B: ARIA role=dialog with the SMS dialog's name.
    try:
        d = page.get_by_role("dialog", name="接收短信验证码").first
        if await d.count() > 0 and await d.is_visible():
            scopes.append(("role=dialog[接收短信验证码]", d))
    except Exception:
        pass
    # Strategy C: any visible role=dialog (covers cases where the dialog
    # has no accessible name but is still aria-marked).
    try:
        d = page.get_by_role("dialog").first
        if await d.count() > 0 and await d.is_visible():
            scopes.append(("role=dialog", d))
    except Exception:
        pass
    # Strategy D: anchor to the「短信已发送至」header text and walk up to
    # the nearest <article> ancestor. The header text only appears
    # inside the SMS dialog, so this gives us the right scope even
    # when ARIA roles are missing.
    try:
        header = page.get_by_text("短信已发送至").first
        if await header.count() > 0:
            article = header.locator("xpath=ancestor::article[1]").first
            if await article.count() > 0:
                scopes.append(("article-ancestor[短信已发送至]", article))
            else:
                # If there's no article ancestor, fall back to the nearest
                # ancestor with role=dialog or class containing modal/dialog.
                fallback = header.locator(
                    "xpath=ancestor::*[@role='dialog' or "
                    "contains(@class,'modal') or contains(@class,'dialog')][1]"
                ).first
                if await fallback.count() > 0:
                    scopes.append(("dialog-ish-ancestor[短信已发送至]", fallback))
    except Exception:
        pass
    if not scopes:
        # No scoped lookup possible — fall back to the page itself; the
        # caller will log this and may still misfire.
        logger.warning(
            "SMS dialog scope: no scoped locator available; falling back to "
            "page-level lookup which may match underlying-page elements",
        )
        scopes.append(("<page>", page))
    return scopes


async def _fill_sms_code(page, code: str) -> bool:
    """Fill the SMS verification code into 抖音's input field.

    抖音 input observed in the wild:

        <input name="button-input" id="button-input" type="tel"
               class="input-lrnhMm" placeholder="请输入验证码"
               autocomplete="off" maxlength="6" ...>

    The id is stable. We pick exactly ONE input (no candidate fan-out)
    and use a single focus → select-all → keyboard.type sequence so the
    SPA's onChange handlers fire on every keystroke. fill() is unreliable
    because Semi Design's controlled input intercepts the synthesized
    React event and frequently drops it.

    Returns True only when the value reads back as the code we wrote.
    """
    # CRITICAL: 抖音 renders the underlying creator/login page AND the SMS
    # dialog in the same DOM tree. Without scoping, ``.first`` picks the
    # input on the underlying page (earlier in document order) — fill
    # writes invisibly, user-visible modal stays empty. _find_sms_dialog_scopes
    # returns scopes guaranteed to be inside the modal.
    target = None
    matched_selector = None
    selector_candidates = (
        'input#button-input',                  # 抖音 — stable id
        'input[placeholder="请输入验证码"]',   # 抖音 — exact placeholder
        'input[type="tel"][maxlength="6"]',    # generic OTP fallback
    )
    for scope_label, scope in await _find_sms_dialog_scopes(page):
        for sel in selector_candidates:
            try:
                loc = scope.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    target = loc
                    matched_selector = f"{scope_label} → {sel}"
                    break
            except Exception:
                logger.debug(
                    "selector probe failed: scope=%s sel=%s",
                    scope_label, sel, exc_info=True,
                )
        if target is not None:
            break

    if target is None:
        logger.warning("SMS fill: no visible code input found in any dialog scope")
        return False

    try:
        import asyncio
        await target.scroll_into_view_if_needed(timeout=2000)

        # Read pre-state so we can see if the field already has stale data
        # (debugging notes mentioned the field stuck at "123" between attempts).
        try:
            pre_value = (await target.input_value(timeout=1000)).strip()
        except Exception:
            pre_value = "<unreadable>"
        logger.info(
            "SMS fill pre-state: selector=%s pre_value=%r target_code=%r",
            matched_selector, pre_value, code,
        )

        # Step 1: focus.
        await target.click(timeout=2000)

        # Step 2: clear. fill("") is the single most reliable reset for a
        # controlled React input — one synthetic change event with empty
        # value. Falls back to keyboard select-all+backspace.
        try:
            await target.fill("", timeout=2000)
        except Exception:
            logger.debug("fill('') failed, trying select-all+backspace")
            try:
                await page.keyboard.press("ControlOrMeta+a")
                await page.keyboard.press("Backspace")
            except Exception:
                pass

        # Re-focus in case fill('') blurred us.
        await target.click(timeout=1000)

        # Step 3: type each digit individually, logging the input value
        # after every keystroke so we can see exactly which keystroke
        # gets dropped if any.
        for i, ch in enumerate(code):
            await page.keyboard.press(ch, delay=120)
            try:
                cur = (await target.input_value(timeout=500)).strip()
            except Exception:
                cur = "<unreadable>"
            logger.info(
                "SMS fill keystroke %d/%d: pressed=%r value_now=%r",
                i + 1, len(code), ch, cur,
            )

        # Settle delay — let any debounced React state update flush.
        await asyncio.sleep(0.3)

        # Verify the value actually landed.
        actual = (await target.input_value(timeout=2000)).strip()
        if actual == code:
            logger.info(
                "SMS code filled successfully (selector=%s length=%d)",
                matched_selector, len(code),
            )
            return True
        logger.warning(
            "SMS code fill verification failed (selector=%s): wrote %r, read %r",
            matched_selector, code, actual,
        )
        return False
    except Exception:
        logger.warning("SMS fill exception", exc_info=True)
        return False


async def _click_sms_trigger(page) -> bool:
    """Click the「获取验证码」/「重新发送」trigger inside the SMS dialog.

    Two button-text variants we care about:
      - 「获取验证码」: dialog just opened (publish flow), no SMS sent yet.
      - 「重新发送」: in input page after first SMS sent (login flow),
        clickable only after the 60s countdown ends.

    Both share the same DOM shape as the chooser-row「接收短信验证码」:
    a deeply-nested element holding the visible text. Reuses the same
    text-engine + ancestor-walk strategy used in _click_chooser_row,
    but scoped to the SMS dialog so we don't accidentally click a
    similarly-named button on the underlying creator page.
    """
    for label in ("获取验证码", "重新发送"):
        for scope_label, scope in await _find_sms_dialog_scopes(page):
            try:
                text_loc = scope.get_by_text(label, exact=True).first
                if await text_loc.count() == 0:
                    continue
                if not await text_loc.is_visible():
                    continue
                # Walk up to the nearest clickable ancestor (covers
                # 抖音's <span>-with-onClick row + <div>-as-button cases).
                clickable_xpath = (
                    "ancestor-or-self::*[@role='button' or "
                    "name()='button' or "
                    "contains(@style, 'cursor: pointer') or "
                    "contains(@class, 'btn') or "
                    "contains(@class, 'button') or "
                    "contains(@class, 'item')][1]"
                )
                ancestor = text_loc.locator(f"xpath={clickable_xpath}").first
                target = ancestor if await ancestor.count() > 0 else text_loc
                # Don't click if it's in a disabled state — many countdown
                # buttons keep the text but add a disabled class.
                try:
                    cls = await target.get_attribute("class") or ""
                    if "disabled" in cls.lower():
                        logger.info(
                            "SMS trigger: '%s' present but disabled (countdown?)",
                            label,
                        )
                        return False
                except Exception:
                    pass
                await target.scroll_into_view_if_needed(timeout=2000)
                await target.click(timeout=5000)
                logger.info(
                    "SMS trigger clicked: %s (scope=%s, label=%s)",
                    label, scope_label, label,
                )
                return True
            except Exception:
                logger.debug(
                    "SMS trigger attempt failed: scope=%s label=%s",
                    scope_label, label, exc_info=True,
                )
    logger.warning("SMS trigger: neither「获取验证码」nor「重新发送」found in any dialog scope")
    return False


async def _click_sms_submit(page) -> bool:
    """Click the「验证」/ submit button on the SMS input page.

    抖音's submit "button" is NOT a <button> element — it's a plain <div>
    with class ``uc_verification_component_btn-...`` and the literal text
    "验证". Confirmed via DOM dump:

        <div class="uc_verification_component_btn-EQNDAT
                    content-Fpuout primary-Npo6wt large-WT6qX5">验证</div>

    Sister「取消」button has the same class structure with ``secondary-...``
    instead of ``primary-...``. Both live inside the SMS dialog scope, so
    we scope-then-match like we do for the input.

    Match strategies in order (each scoped to the SMS dialog):
      A. ARIA role=button name=验证 — works if 抖音 ever fixes accessibility
      B. ``.uc_verification_component_btn-...`` class — 抖音's verification
         component prefix; combined with primary-* picks the submit button
         specifically (not「取消」)
      C. Any element with exact inner_text == "验证" — last resort
    """
    target = None
    matched_label = None
    for scope_label, scope in await _find_sms_dialog_scopes(page):
        # Strategy A: ARIA role (would be ideal if 抖音 added role="button").
        try:
            loc = scope.get_by_role("button", name="验证", exact=True).first
            if await loc.count() > 0 and await loc.is_visible():
                target = loc
                matched_label = f"{scope_label} → role=button[验证]"
                break
        except Exception:
            logger.debug("get_by_role 验证 button failed (scope=%s)", scope_label, exc_info=True)

        # Strategy B: 抖音 verification-component class. The class name has
        # a hash suffix (uc_verification_component_btn-EQNDAT) so we use a
        # prefix-matcher. ``primary-*`` filters to the submit (not 取消).
        try:
            loc = scope.locator(
                '[class*="uc_verification_component_btn-"][class*="primary-"]'
            ).first
            if await loc.count() > 0 and await loc.is_visible():
                target = loc
                matched_label = f"{scope_label} → uc_verification_component_btn[primary]"
                break
        except Exception:
            logger.debug("uc_verification_component_btn lookup failed", exc_info=True)

        # Strategy C: any element whose inner_text is exactly "验证".
        # Avoids "验证码" / "验证失败" / "验证码登录" via exact match. We
        # check several common inline-clickable element types — covers
        # the <div> case 抖音 actually uses + future div→button changes.
        for tag in ("div", "span", "button", "a"):
            try:
                els = scope.locator(f"{tag}:visible")
                n = await els.count()
                for j in range(min(n, 30)):
                    el = els.nth(j)
                    try:
                        txt = (await el.inner_text()).strip()
                        if txt == "验证":
                            target = el
                            matched_label = f"{scope_label} → {tag}:visible[inner_text='验证']"
                            break
                    except Exception:
                        pass
                if target is not None:
                    break
            except Exception:
                pass
        if target is not None:
            break

    if target is None:
        logger.warning("SMS submit: no '验证' button found in any dialog scope")
        return False
    logger.info("SMS submit target located: %s", matched_label)

    try:
        await target.scroll_into_view_if_needed(timeout=2000)
        # Wait up to 3s for the button to become enabled — 抖音's button
        # is bound to the input's validity state (length === maxlength)
        # which their React code re-evaluates on the next tick after
        # the keystroke handler runs.
        #
        # 抖音's submit isn't a real <button>, it's a <div> — so
        # is_enabled() will always be True (no disabled attribute on a
        # div). Their disabled state is encoded in a class suffix like
        # ``disabled-XXX``. Best we can do is poll the class for a brief
        # window; if it never goes from disabled → enabled we click anyway
        # because the alternative (hard-fail) is worse for the user.
        import asyncio
        try:
            for _ in range(15):  # 15 * 200ms = 3s
                try:
                    cls = await target.get_attribute("class") or ""
                except Exception:
                    cls = ""
                if "disabled" not in cls.lower():
                    break
                await asyncio.sleep(0.2)
            else:
                logger.info(
                    "SMS submit: button class still contains 'disabled' "
                    "after 3s — clicking anyway",
                )
        except Exception:
            logger.debug("disabled-class poll failed", exc_info=True)
        await target.click(timeout=5000)
        logger.info("SMS submit '验证' button clicked")
        return True
    except Exception:
        logger.warning("SMS submit click exception", exc_info=True)
        return False


async def _click_chooser_row(page) -> bool:
    """Click the「接收短信验证码」row on the chooser page.

    抖音's row is a deep <div> tree where the visible text and the
    clickable container are different nodes. ``get_by_text`` finds the
    leaf carrying the label, then we walk up via xpath ancestor lookup
    to find a clickable container (cursor:pointer, role=button, or just
    the immediate flex row). Falls back to clicking the text leaf
    directly — Playwright's ``click()`` already trampolines into the
    nearest event-handling ancestor for us when it can.
    """
    text_loc = page.get_by_text(_CHOOSER_ADVANCE_TEXT, exact=True).first
    try:
        if await text_loc.count() == 0:
            # Fallback to non-exact match — abrupt whitespace / extra
            # decoration around the label can break exact-match.
            text_loc = page.get_by_text(_CHOOSER_ADVANCE_TEXT).first
        if await text_loc.count() == 0:
            return False
        # Walk up to the nearest ancestor that looks clickable (covers
        # 抖音's div-with-onClick rows). Falls back to the text leaf.
        clickable_xpath = (
            "ancestor-or-self::*[@role='button' or "
            "contains(@style, 'cursor: pointer') or "
            "contains(@class, 'item') or "
            "contains(@class, 'card') or "
            "contains(@class, 'row')][1]"
        )
        clickable = text_loc.locator(f"xpath={clickable_xpath}").first
        target = clickable if await clickable.count() > 0 else text_loc
        try:
            await target.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        await target.click(timeout=5000)
        return True
    except Exception as exc:
        logger.warning("chooser row click failed: %s", exc, exc_info=True)
        # Last-resort: try the text leaf directly without ancestor walking.
        try:
            await text_loc.click(timeout=3000)
            return True
        except Exception:
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
        # Two scenarios on 抖音:
        #   - Login flow: SMS already auto-dispatched on input-page entry,
        #     so trigger_sms is effectively「重新发送」(disabled during 60s
        #     countdown).
        #   - Publish flow: dialog opens with NO SMS sent yet, user has
        #     to click「获取验证码」first.
        # _click_sms_trigger handles both — scopes to the SMS dialog and
        # tries「获取验证码」/「重新发送」labels in order.
        ok = await _click_sms_trigger(page)
        return {
            "ok": ok,
            "detail": "短信触发成功" if ok else "未找到「获取验证码」/「重新发送」按钮（可能仍在倒计时）",
        }
    if command == "submit_code":
        code = (action.get("code") or "").strip()
        if not (code.isdigit() and 4 <= len(code) <= 8):
            return {"ok": False, "detail": "验证码格式不合法"}
        filled = await _fill_sms_code(page, code)
        if not filled:
            await _dump_page_for_debug(page, "sms_fill_failed")
            return {"ok": False, "detail": "未找到验证码输入框，或填入后值未保留"}
        # Brief pause: 抖音's submit button is bound to the input's
        # validity state, which their code re-evaluates on the next tick.
        # Without this pause we sometimes click while the button is still
        # disabled.
        import asyncio
        try:
            await asyncio.sleep(0.5)
        except Exception:
            pass
        clicked = await _click_sms_submit(page)
        if not clicked:
            await _dump_page_for_debug(page, "sms_submit_failed")
            return {
                "ok": False,
                "detail": "验证码已填入但未点到验证按钮（可能仍是禁用状态）",
            }

        # Post-click verification: wait briefly, then check whether 抖音
        # surfaced a wrong-code error message inside the dialog. If so,
        # surface that to the user instead of misleadingly reporting
        # success — they need to re-enter a fresh code.
        try:
            await asyncio.sleep(1.2)
        except Exception:
            pass
        err_text = await _detect_sms_error(page)
        if err_text:
            logger.warning("SMS submit: dialog reported error: %s", err_text)
            return {
                "ok": False,
                "detail": f"平台拒绝了验证码：{err_text}",
            }
        return {"ok": True, "detail": "验证码已提交"}
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
            advanced = await _click_chooser_row(page)
            if not advanced:
                logger.warning(
                    "chooser page detected but「接收短信验证码」row not clickable",
                )
                raise VerificationAbortedError(
                    kind="sms",
                    reason="chooser advance button missing",
                )
            logger.info("clicked chooser「接收短信验证码」, waiting for SPA navigation")
            # Give the SPA a beat to navigate before re-detecting.
            try:
                import asyncio
                await asyncio.sleep(2.0)
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

        if action is None or action.get("command") is None:
            # Callback returned nothing actionable — treat as abort so we
            # don't busy-loop here.
            raise VerificationAbortedError(
                kind="sms",
                reason="callback returned no action",
            )

        if action.get("command") == "noop":
            # Callback explicitly chose to do nothing this round (typically
            # because the previous action was already consumed and the DOM
            # hasn't fully cleared). Bail out cleanly so the upstream caller's
            # outer loop can sleep + re-poll the page state.
            logger.debug(
                "challenge_callback returned noop; exiting maybe_emit_challenge",
            )
            return

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
        # away and detect returns None on the next pass. If it failed
        # (wrong code, button still disabled, error tip surfaced), the
        # dialog is still up — _challenge_callback's re-entry branch
        # observes the situation (status=completed but DOM unchanged)
        # and decides whether to noop or reset for retry.


# ---------- exceptions ----------


class VerificationAbortedError(Exception):
    """Raised inside upstream upload() when the callback signals that the
    user can't / won't complete the SMS verification. The wrapping runner
    catches this and reports a typed error back to dify."""

    def __init__(self, *, kind: ChallengeKind, reason: str) -> None:
        super().__init__(f"verification aborted: kind={kind}, reason={reason}")
        self.kind = kind
        self.reason = reason
