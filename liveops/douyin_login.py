# -*- coding: utf-8 -*-
"""抖店登录态恢复与店铺选择。

抖店 profile 启动后通常已经处于首页；登录态失效时会把深链重定向到
``/login/common``。本模块只负责恢复登录态和选择 profile 对应的店铺，
不读取或打印凭据，也不尝试绕过第三方滑块验证码。

滑块是有意保留的人机校验。检测到真实验证码 iframe 后，脚本会发送不含
敏感 URL 的飞书人工接手提醒，并等待用户在已启动的 profile 中完成验证；
验证失败的 ``/captcha/verify`` HTTP 500 只作为失败信号记录，页面会自行刷新
下一题。这样订单、账单和千川三个入口可以共享同一套登录状态机。
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from .login_alert import notify_human_handoff

if TYPE_CHECKING:
    from playwright.async_api import Browser, Locator, Page


DOUYIN_HOME_URL = "https://fxg.jinritemai.com/ffa/mshop/homepage/index"
DOUYIN_LOGIN_URL = "https://fxg.jinritemai.com/login/common?channel=zhaoshang"
DOUYIN_HOME_RE = re.compile(r"^https://fxg\.jinritemai\.com/ffa/mshop/homepage/index(?:[/?#]|$)")

# These are the selectors confirmed by the current login page or supplied by the
# operator.  The page has changed its wrapper attributes before, so callers must
# use the semantic fallbacks below as well.
LOGIN_ROOT_XPATH = '//*[@d="root"]/div/div[1]/div/div/div/div'
EMAIL_LOGIN_XPATH = '//*[@id="ecomLoginForm"]/section/div[2]/div[2]'
AGREEMENT_XPATH = '//*[@id="root"]/div/div[1]/div/div/div/div/div[2]/div[4]/div[1]/label/span/input'
CAPTCHA_IFRAME_XPATH = '//*[@id="captcha_container"]/iframe'
STORE_ROOT_XPATH = '//*[@id="root"]/div/div[1]/div/div/div/div/div/div'
STORE_LIST_XPATH = '//*[@id="root"]/div/div[1]/div/div/div/div/div/div/div[3]'
STORE_FIRST_NAME_XPATH = '//*[@id="root"]/div/div[1]/div/div/div/div/div/div/div[3]/div[1]/div[2]/div[1]'

CAPTCHA_VERIFY_PATH = "/captcha/verify"
CAPTCHA_TIMEOUT_SECONDS = 300.0
LOGIN_STATE_TIMEOUT_SECONDS = 45.0
EMAIL_FORM_MARKER_TEXT = "邮箱停用或忘记"
MOBILE_FORM_MARKER_TEXTS = (
    "收不到验证码",
    "手机号停用或忘记",
    "手机号首次登录即自动注册",
)
EMAIL_SWITCH_SELECTOR = ".account-center-switch-button.email"
EMAIL_SWITCH_TIMEOUT_SECONDS = 30.0
CAPTCHA_IFRAME_SELECTORS = (
    f"xpath={CAPTCHA_IFRAME_XPATH}",
    "#captcha_container > iframe",
    "iframe#captcha_container",
    'iframe[src*="verify.zijieapi.com"]',
    'iframe[src*="rmc.bytedance.com"]',
    'iframe[src*="/verifycenter/captcha/"]',
    'iframe[id*="captcha" i]',
    'iframe[name*="captcha" i]',
)


class DouyinCaptchaRequired(RuntimeError):
    """Raised only when a human CAPTCHA handoff timed out."""


def _xpath(page: "Page", expression: str) -> "Locator":
    return page.locator(f"xpath={expression}")


async def _visible_count(locator: "Locator") -> int:
    """Count visible matches without trusting hidden stale React nodes."""

    count = await locator.count()
    visible = 0
    for index in range(count):
        try:
            if await locator.nth(index).is_visible():
                visible += 1
        except Exception:
            continue
    return visible


async def _visible_locator(locator: "Locator") -> "Locator | None":
    """Return a visible match instead of relying on a stale last node."""

    for index in range(await locator.count()):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


async def _body_text(page: "Page") -> str:
    try:
        return (await page.locator("body").inner_text(timeout=2_000)).strip()
    except Exception:
        return ""


async def _title(page: "Page") -> str:
    try:
        return (await page.title()).strip()
    except Exception:
        return ""


async def captcha_iframe_count(page: "Page") -> int:
    """Return visible captcha iframe count, including the observed variants."""

    matches = set()
    for selector in CAPTCHA_IFRAME_SELECTORS:
        locator = page.locator(selector)
        for index in range(await locator.count()):
            node = locator.nth(index)
            try:
                if await node.is_visible():
                    # Element handles are not hashable across Playwright versions;
                    # the selector/index pair is sufficient for a de-duplicated
                    # diagnostic count.
                    matches.add((selector, index))
            except Exception:
                continue

    # The first two selectors can describe the same iframe.  A boolean is all
    # the state machine needs, while this count remains useful in diagnostics.
    return 1 if matches else 0


async def captcha_present(page: "Page") -> bool:
    # Use the visible outer iframe as the source of truth.  A completed
    # challenge can leave a detached frame with the old verify URL in
    # ``page.frames``; treating that stale frame as active would block the
    # successful login forever.
    return bool(await captcha_iframe_count(page))


def is_captcha_verify_failure(url: str, status: int) -> bool:
    """Classify the platform's HTTP 500 CAPTCHA failure response."""

    return CAPTCHA_VERIFY_PATH in url and status == 500


async def _store_selection_visible(page: "Page") -> bool:
    body = await _body_text(page)
    if "请选择店铺" not in body:
        return False
    if await _visible_count(_xpath(page, STORE_ROOT_XPATH)):
        return True
    if await _visible_count(_xpath(page, STORE_LIST_XPATH)):
        return True

    # The current React store picker keeps the same semantic text but has
    # changed its generated DOM nesting (for example, ``index_roleItem__...``
    # and ``index_introName__...``).  Use class fragments only as a bounded
    # fallback; the body marker above prevents a login page with unrelated
    # generated classes from being classified as a store picker.
    for selector in (
        '[class*="roleItem"]',
        '[class*="roleList"]',
        '[class*="introName"]',
    ):
        if await _visible_count(page.locator(selector)):
            return True
    return False


async def login_surface_visible(page: "Page") -> bool:
    """Detect the login surface using the supplied XPath plus stable fallbacks."""

    if await _store_selection_visible(page):
        return False
    if await _visible_count(_xpath(page, LOGIN_ROOT_XPATH)):
        return True
    for selector in (
        "#ecomLoginForm",
        'input[placeholder="请输入邮箱"]',
        'input[placeholder*="邮箱"]',
        'input[type="password"]',
    ):
        if await _visible_count(page.locator(selector)):
            return True
    title = await _title(page)
    return "登录" in title and "抖店" in title


async def page_state(page: "Page") -> str:
    """Return ``home``, ``login``, ``shop`` or ``other`` for the current page."""

    if await _store_selection_visible(page):
        return "shop"
    if await login_surface_visible(page):
        return "login"
    if DOUYIN_HOME_RE.match(page.url):
        # A client-side redirect briefly leaves the requested home URL in
        # place before the login page mounts.  During that gap the title is
        # empty; do not report a false successful login.  A real logged-in
        # home has a non-empty non-login title (currently ``首页``).
        title = await _title(page)
        if title and "登录" not in title:
            return "home"
    return "other"


async def wait_for_page_state(page: "Page", timeout: float = LOGIN_STATE_TIMEOUT_SECONDS) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    last_state = "other"
    home_stable_polls = 0
    while asyncio.get_running_loop().time() < deadline:
        last_state = await page_state(page)
        if last_state == "home":
            # The requested homepage can briefly paint its title before the
            # unauthenticated client redirect mounts /login/common.  Require
            # a stable home observation and re-check the login surface before
            # allowing callers to skip recovery.
            if await login_surface_visible(page) or "/login/" in page.url:
                last_state = "login"
                return last_state
            home_stable_polls += 1
            if home_stable_polls >= 4:
                await asyncio.sleep(0.5)
                if await login_surface_visible(page) or "/login/" in page.url:
                    return "login"
                if await page_state(page) == "home":
                    return "home"
        elif last_state != "other":
            return last_state
        else:
            home_stable_polls = 0
        await asyncio.sleep(0.25)
    return last_state


async def _click_compat(locator: "Locator", *, timeout: int = 8_000) -> None:
    try:
        await locator.click(timeout=timeout)
    except Exception as first_error:
        # The SDK tab can be continuously re-rendered while its CSS bar is
        # animating.  A forced Playwright click keeps the target semantic and
        # avoids waiting on a stale stability snapshot; the short evaluate
        # fallback is bounded so a detached React node cannot stall retries.
        try:
            await locator.click(timeout=min(timeout, 2_000), force=True)
            return
        except Exception:
            try:
                await locator.evaluate("element => element.click()", timeout=2_000)
                return
            except Exception:
                raise first_error


async def _click_shop_card(page: "Page", locator: "Locator") -> None:
    """Click the store card's real mouse-down target after locating it by text."""
    await locator.scroll_into_view_if_needed(timeout=5_000)
    box = await locator.bounding_box()
    viewport = await page.evaluate("({width: innerWidth, height: innerHeight})")
    fully_in_view = bool(
        box
        and box["width"] > 0
        and box["height"] > 0
        and box["x"] >= 0
        and box["y"] >= 0
        and box["x"] + box["width"] <= viewport["width"]
        and box["y"] + box["height"] <= viewport["height"]
    )
    if fully_in_view:
        # The current store-list card wires selection to onMouseDown on the
        # card itself.  Use a real pointer sequence so React receives the same
        # event as an operator click; the name node alone has no handler.
        await page.mouse.move(
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
        )
        await page.mouse.down()
        await page.mouse.up()
        return
    # Store cards can sit in a horizontally clipped list, leaving the center
    # point outside the viewport even after vertical scrolling.  Dispatch the
    # card's own mousedown in that case; this preserves the page's normal
    # handler without relying on an off-screen coordinate.
    await locator.dispatch_event(
        "mousedown",
        {"bubbles": True, "cancelable": True, "button": 0, "buttons": 1},
    )


async def _visible_exact_text(page: "Page", text: str) -> bool:
    """Find visible semantic text while ignoring detached React remnants."""

    form = page.locator("#ecomLoginForm")
    return await _visible_locator(form.get_by_text(text, exact=True)) is not None


async def email_login_form_visible(page: "Page") -> bool:
    """Return true only after the email-specific form marker is visible."""

    return await _visible_exact_text(page, EMAIL_FORM_MARKER_TEXT)


async def mobile_login_form_visible(page: "Page") -> bool:
    """Return true when the phone form's semantic help text is visible."""

    for text in MOBILE_FORM_MARKER_TEXTS:
        if await _visible_exact_text(page, text):
            return True
    return False


async def _wait_for_email_login_form(page: "Page", *, timeout: float) -> bool:
    """Require the email marker across two polls before proceeding."""

    deadline = asyncio.get_running_loop().time() + timeout
    stable_polls = 0
    while asyncio.get_running_loop().time() < deadline:
        if await email_login_form_visible(page):
            stable_polls += 1
            if stable_polls >= 2:
                return True
        else:
            stable_polls = 0
        await asyncio.sleep(0.25)
    return False


async def _email_login(page: "Page") -> None:
    """Switch to email login and verify the switch's postcondition.

    The login SDK can expose a clickable tab before React has mounted the
    selected form.  A successful click is therefore defined by the visible
    email-only help text, not by the click call returning or by an input that
    may belong to a stale render.  If the phone marker remains visible, wait
    for the UI to settle and retry the switch.
    """

    candidates = [
        page.locator(EMAIL_SWITCH_SELECTOR),
        page.get_by_text("邮箱登录", exact=True),
        _xpath(page, EMAIL_LOGIN_XPATH),
    ]
    deadline = asyncio.get_running_loop().time() + EMAIL_SWITCH_TIMEOUT_SECONDS
    click_attempts = 0
    while asyncio.get_running_loop().time() < deadline:
        if await _wait_for_email_login_form(page, timeout=0.25):
            print("[DOUYIN_LOGIN] 邮箱登录表单已就绪")
            return
        current_state = await page_state(page)
        if current_state in {"home", "shop"}:
            if current_state == "shop":
                print("[DOUYIN_LOGIN] 登录态已恢复，进入店铺选择页")
            return

        # Do not click a tab during the initial mobile-form mount.  This is
        # the observed race: the switch is painted first, while its form and
        # event handler are still being attached.
        if not await mobile_login_form_visible(page):
            await asyncio.sleep(0.25)
            continue

        visible_candidate = None
        for candidate in candidates:
            visible_candidate = await _visible_locator(candidate)
            if visible_candidate is not None:
                break
        if visible_candidate is None:
            await asyncio.sleep(0.25)
            continue

        click_attempts += 1
        try:
            await _click_compat(visible_candidate, timeout=2_000)
        except Exception:
            print("[DOUYIN_LOGIN] 邮箱登录入口在页面重绘中，稍后重试")
            await asyncio.sleep(0.5)
            continue
        print(f"[DOUYIN_LOGIN] 已点击“邮箱登录”，等待邮箱登录表单（第{click_attempts}次）")
        current_state = await page_state(page)
        if current_state in {"home", "shop"}:
            if current_state == "shop":
                print("[DOUYIN_LOGIN] 点击后已进入店铺选择页")
            return
        remaining = max(0.25, deadline - asyncio.get_running_loop().time())
        if await _wait_for_email_login_form(page, timeout=min(5.0, remaining)):
            print("[DOUYIN_LOGIN] 邮箱登录表单已就绪")
            return
        if await mobile_login_form_visible(page):
            print("[DOUYIN_LOGIN] 点击后仍为手机登录，等待页面稳定并重试")
        await asyncio.sleep(0.75)

    if not click_attempts:
        raise RuntimeError("抖店登录页未找到“邮箱登录”入口")
    raise RuntimeError("点击“邮箱登录”后未出现邮箱登录表单")


async def _autofill_saved_credentials(page: "Page", *, timeout: float = 15.0) -> None:
    """Trigger the profile browser's saved-credential autofill without reading it."""

    if not await email_login_form_visible(page):
        raise RuntimeError("抖店邮箱登录表单尚未确认，禁止继续填充登录凭据")

    form = page.locator("#ecomLoginForm")
    email_selector = 'input[name="email"], input[type="email"], input[placeholder*="邮箱"]'
    password_selector = (
        'input[name="password"], input[type="password"], '
        'input[placeholder="密码"], input[placeholder*="密码"]'
    )
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        email = await _visible_locator(form.locator(email_selector))
        password = await _visible_locator(form.locator(password_selector))
        if email is not None and password is not None:
            try:
                if await email.input_value() and await password.input_value():
                    print("[DOUYIN_LOGIN] 当前 profile 已回填邮箱登录凭据")
                    return
            except Exception:
                pass

            # Chrome profile autofill is only offered after a real field focus.
            # The keyboard selection is intentionally value-blind and never logs
            # the selected account or password.
            for target in (email, password, email):
                try:
                    await target.click(timeout=8_000)
                except Exception:
                    try:
                        await target.focus()
                    except Exception:
                        continue
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("Enter")
                await asyncio.sleep(0.4)
                current_email = await _visible_locator(form.locator(email_selector))
                current_password = await _visible_locator(form.locator(password_selector))
                try:
                    if (
                        current_email is not None
                        and current_password is not None
                        and await current_email.input_value()
                        and await current_password.input_value()
                    ):
                        print("[DOUYIN_LOGIN] 当前 profile 已回填邮箱登录凭据")
                        return
                except Exception:
                    pass
        await asyncio.sleep(0.25)

    raise RuntimeError(
        "抖店邮箱登录表单未自动填充；请在当前 profile 中选择已保存的邮箱登录凭据"
    )


async def _check_agreement(page: "Page") -> None:
    candidates = [
        page.locator('#ecomLoginForm input[type="checkbox"]'),
        page.locator('#ecomLoginForm label').filter(has_text="用户协议").locator(
            'input[type="checkbox"]'
        ),
        # The current Doudian page renders the visible agreement checkbox in
        # a policy wrapper outside ``#ecomLoginForm``.  Keep this semantic
        # fallback before the legacy absolute XPath so login recovery can
        # follow the live DOM after that wrapper moved.
        page.locator('div[class*="policyWrapper"] input[type="checkbox"]'),
        page.locator('input[type="checkbox"]'),
        _xpath(page, AGREEMENT_XPATH),
    ]
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        # Autofill/session restoration can finish the login between the email
        # field becoming visible and the agreement component mounting.
        if await page_state(page) in {"home", "shop"}:
            return
        if not await email_login_form_visible(page):
            await asyncio.sleep(0.25)
            continue
        for candidate in candidates:
            checkbox = await _visible_locator(candidate)
            if checkbox is None:
                continue
            try:
                if not await checkbox.is_checked():
                    await checkbox.check(timeout=8_000)
                    print("[DOUYIN_LOGIN] 已勾选用户协议")
                return
            except Exception:
                await _click_compat(checkbox)
                print("[DOUYIN_LOGIN] 已勾选用户协议")
                return
        await asyncio.sleep(0.25)
    raise RuntimeError("抖店邮箱登录页未找到用户协议复选框")


async def _click_login_if_still_visible(page: "Page", *, timeout: float = 8.0) -> bool:
    candidates = [
        page.locator("#ecomLoginForm").get_by_role("button", name="登录", exact=True),
        page.get_by_role("button", name="登录", exact=True),
    ]
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for candidate in candidates:
            button = await _visible_locator(candidate)
            if button is None:
                continue
            try:
                if await button.is_enabled():
                    await _click_compat(button)
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.25)
    return False


async def wait_for_human_captcha(
    page: "Page", *, timeout: float = CAPTCHA_TIMEOUT_SECONDS,
    platform: str = "抖音", company: str = "", shop: str = ""
) -> str:
    """Wait for a user to finish the displayed slider challenge.

    The handoff intentionally sends only business metadata to Feishu.  It never
    extracts challenge URLs, response bodies, or tokens.  A 500 response means
    the page rejected that attempt; the widget is expected to render another
    challenge.
    """

    failure_seen = False

    def on_response(response) -> None:
        nonlocal failure_seen
        try:
            if is_captcha_verify_failure(response.url, response.status):
                failure_seen = True
        except Exception:
            pass

    page.on("response", on_response)
    try:
        print("[DOUYIN_LOGIN] 检测到登录滑块，请在已启动的 profile 中手动完成；脚本会继续等待")
        if company and shop:
            try:
                await notify_human_handoff(
                    platform=platform,
                    company=company,
                    shop=shop,
                    purpose="抖店登录滑块验证",
                )
                print("[DOUYIN_LOGIN] 已发送飞书滑块人工接手提醒")
            except Exception:
                # The relay is an alert channel, not a prerequisite for the
                # browser handoff.  Keep the profile alive even if Feishu is
                # temporarily unavailable or not configured.
                print("[DOUYIN_LOGIN] 飞书滑块提醒发送失败；仍等待人工完成")
        else:
            print("[DOUYIN_LOGIN] 缺少公司/店铺元数据，未发送飞书滑块提醒；仍等待人工完成")
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            state = await page_state(page)
            present = await captcha_present(page)
            if failure_seen:
                print("[DOUYIN_LOGIN] 本次滑块验证返回 HTTP 500，等待页面刷新下一题或人工重试")
                failure_seen = False
            if state in {"home", "shop"} and not present:
                return state
            await asyncio.sleep(0.5)
    finally:
        page.remove_listener("response", on_response)
    raise DouyinCaptchaRequired(
        f"抖店登录滑块在 {timeout:.0f} 秒内未完成；保留当前 profile 页面供人工接管"
    )


async def _wait_after_login_submit(
    page: "Page", *, timeout: float, captcha_timeout: float,
    platform: str, company: str, shop: str
) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await page_state(page)
        if await captcha_present(page):
            return await wait_for_human_captcha(
                page,
                timeout=captcha_timeout,
                platform=platform,
                company=company,
                shop=shop,
            )
        if state in {"home", "shop"}:
            return state
        await asyncio.sleep(0.5)
    raise RuntimeError(f"抖店登录提交后未进入首页或店铺选择页：{page.url.split('?')[0]}")


async def select_douyin_shop(page: "Page", shop: str, *, timeout: float = 45.0) -> str:
    """在登录后的店铺列表中精确选择 profile note 的 shop。"""

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await page_state(page) != "shop":
            state = await page_state(page)
            if state in {"home", "other"}:
                return state
        candidates = page.get_by_text(shop, exact=True).filter(visible=True)
        if await candidates.count():
            for index in range(await candidates.count() - 1, -1, -1):
                candidate = candidates.nth(index)
                try:
                    await candidate.scroll_into_view_if_needed(timeout=5_000)
                    await _click_compat(candidate)
                    await asyncio.sleep(0.5)
                    if not await _store_selection_visible(page):
                        # A selection click can briefly remove the list while
                        # the browser is still on /login/common.  Never call
                        # that transitional state a completed selection.
                        if "/login/" in page.url:
                            await asyncio.sleep(0.5)
                            continue
                        selected_state = await page_state(page)
                        print(f"[DOUYIN_LOGIN] 已选择目标店铺：{shop}")
                        return selected_state
                except Exception:
                    pass

                # A page revision may attach the handler to the card ancestor
                # instead of the name node.  Keep the click target text-anchored,
                # never a fixed list index, and retry the enclosing card once.
                for ancestor_xpath in (
                    "ancestor::div[contains(@class,'roleItem')][1]",
                    "ancestor::div[contains(@class,'intro__')][1]",
                ):
                    card = candidate.locator(f"xpath={ancestor_xpath}")
                    if not await _visible_count(card):
                        continue
                    try:
                        await _click_shop_card(page, card)
                        await asyncio.sleep(0.5)
                        if not await _store_selection_visible(page):
                            if "/login/" in page.url:
                                await asyncio.sleep(0.5)
                                continue
                            selected_state = await page_state(page)
                            print(f"[DOUYIN_LOGIN] 已选择目标店铺：{shop}")
                            return selected_state
                    except Exception:
                        continue
        await asyncio.sleep(0.5)
    raise RuntimeError(f"抖店登录后的店铺列表中未能进入目标店铺：{shop}")


async def ensure_douyin_login(
    page: "Page",
    shop: str,
    *,
    timeout: float = LOGIN_STATE_TIMEOUT_SECONDS,
    captcha_timeout: float = CAPTCHA_TIMEOUT_SECONDS,
    platform: str = "抖音",
    company: str = "",
) -> bool:
    """Ensure the current page has a valid login and the requested shop.

    Returns ``True`` when this call had to recover a login or select a shop.
    A normal logged-in home/业务页 returns ``False`` so callers can continue.
    """

    state = await wait_for_page_state(page, timeout=timeout)
    if state == "home":
        return False
    if state == "shop":
        await select_douyin_shop(page, shop, timeout=timeout)
        return True
    if state != "login":
        return False

    await _email_login(page)
    state = await page_state(page)
    if state == "home":
        return True
    if state == "shop":
        await select_douyin_shop(page, shop, timeout=timeout)
        return True
    await _autofill_saved_credentials(page)
    await _check_agreement(page)
    await asyncio.sleep(0.5)

    # The currently observed page submits immediately after the agreement is
    # checked.  Older revisions leave the button for a second explicit click.
    if await page_state(page) == "login" and not await captcha_present(page):
        if not await _click_login_if_still_visible(page):
            raise RuntimeError("抖店邮箱登录按钮未启用；profile 凭据或用户协议未完成")

    try:
        state = await _wait_after_login_submit(
            page,
            timeout=timeout,
            captcha_timeout=captcha_timeout,
            platform=platform,
            company=company,
            shop=shop,
        )
    except DouyinCaptchaRequired:
        # Keep this branch explicit so callers get the same actionable error if
        # manual intervention did not finish within the configured window.
        raise
    if state == "shop":
        await select_douyin_shop(page, shop, timeout=timeout)
    elif state == "login" and await captcha_present(page):
        state = await wait_for_human_captcha(
            page,
            timeout=captcha_timeout,
            platform=platform,
            company=company,
            shop=shop,
        )
        if state == "shop":
            await select_douyin_shop(page, shop, timeout=timeout)
    return True


async def goto_with_douyin_login(
    page: "Page", url: str, shop: str, *, timeout: float = LOGIN_STATE_TIMEOUT_SECONDS,
    platform: str = "抖音", company: str = "", login_alert_purpose: str | None = None
) -> str:
    """Navigate to a Douyin destination and verify the session postcondition.

    ``login_alert_purpose`` is opt-in for callers whose login loss needs a
    Feishu notice even when the saved credentials can restore the session
    without a CAPTCHA.  The shared order/bill callers keep the historical
    behavior; Qianchuan opts in because its profile is independently logged
    in and must be surfaced to the operator when that session expires.
    """

    login_alert_sent = False
    for attempt in range(3):
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # Business pages are intentionally classified as ``other``; the state
        # helper returns that immediately instead of waiting for business DOM.
        # Login pages may mount their form just after DOMContentLoaded, so use
        # the bounded state wait before allowing a caller to touch selectors.
        state = await wait_for_page_state(page, timeout=timeout)

        if state == "shop":
            print(f"[DOUYIN_LOGIN] 已登录，正在选择目标店铺：{shop}")
            await select_douyin_shop(page, shop, timeout=timeout)
            continue

        login_url = "/login/" in page.url
        if state == "login" or login_url or await login_surface_visible(page):
            if login_alert_purpose and not login_alert_sent and company and shop:
                try:
                    await notify_human_handoff(
                        platform=platform,
                        company=company,
                        shop=shop,
                        purpose=login_alert_purpose,
                    )
                    login_alert_sent = True
                    print("[DOUYIN_LOGIN] 已发送登录态失效飞书提醒")
                except Exception:
                    # Login recovery remains usable when the alert relay is
                    # temporarily unavailable; the failure is visible in the
                    # run log and the profile remains available for recovery.
                    print("[DOUYIN_LOGIN] 登录态失效飞书提醒发送失败；继续登录恢复")
            print("[DOUYIN_LOGIN] 检测到抖音登录框，调用登录恢复脚本")
            await ensure_douyin_login(
                page,
                shop,
                timeout=timeout,
                platform=platform,
                company=company,
            )
            continue

        return state

    final_state = await page_state(page)
    if final_state in {"login", "shop"} or "/login/" in page.url:
        raise RuntimeError(
            f"抖音登录会话校验失败，目标页仍未进入业务页面：{page.url.split('?')[0]}"
        )
    return final_state


async def find_or_create_douyin_page(browser: "Browser") -> tuple["Page", bool]:
    """Reuse any existing FXG tab, including the login tab, before creating one."""

    for context in browser.contexts:
        for page in context.pages:
            if not page.is_closed() and "fxg.jinritemai.com" in page.url:
                return page, False
    if not browser.contexts:
        raise RuntimeError("CDP 浏览器中没有可用的浏览器上下文")
    return await browser.contexts[0].new_page(), True
