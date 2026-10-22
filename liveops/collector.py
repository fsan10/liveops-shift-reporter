from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from liveops.douyin_login import DOUYIN_HOME_URL, goto_with_douyin_login
from liveops.login_alert import reset_alert_settings, set_alert_settings
from liveops.portal import (
    APP_TIMEZONE,
    FUNNEL_XPATH,
    QIANCHUAN_OVERVIEW,
    ResponseCollector,
    browser_context,
    extract_shift_records,
    portal_urls,
    qianchuan_snapshot,
    schedule_end_datetime,
    shift_datetime,
)
from liveops.sessions import (
    confirmed_room,
    history_rooms,
    local_datetime,
    overlapping_rooms,
    realtime_rooms,
    select_author,
)
from liveops.snapshots import capture_snapshot, combine_handoffs, handoff_source

logger = logging.getLogger(__name__)

ARTIFACT_ROOT = Path(os.getenv("ARTIFACT_DIR", "var/artifacts"))
WAIT_AFTER_NAVIGATION_MS = int(os.getenv("PORTAL_SETTLE_MS", "1800"))
PAGE_TIMEOUT_MS = int(os.getenv("PORTAL_TIMEOUT_MS", "45000"))
COMPASS_READY_STABLE_SECONDS = 0.75
CAPTURE_MIN_VIEWPORT = {"width": 1280, "height": 720}
CAPTURE_FALLBACK_VIEWPORT = {"width": 1920, "height": 1080}
LIVE_OVERVIEW_URL = "https://compass.jinritemai.com/shop/live-overview"
REALTIME_PATH = "/compass_api/shop/live/live_list/realtime"
REALTIME_TAB = 'xpath=//*[@id="rc-tabs-1-tab-realTime"]/span/div'
DETAIL_FALLBACK = 'xpath=//*[@id="rc-tabs-0-panel-live_room_list"]/dynamic-module-wrapper/dynamic-module-wrapper/div/div[2]/div/div/div/div/div/div/div/div/div/div/table/tbody/tr[2]/td[13]/div/div/a'
PRO_FALLBACK = 'xpath=//*[@id="rc-tabs-0-panel-live_room_list"]/dynamic-module-wrapper/dynamic-module-wrapper/div/div[2]/div/div/div/div/div/div/div/div/div/div/table/tbody/tr[2]/td[13]/div/a[2]'

QIANCHUAN_MODE_INPUT = "#full-screen-main-content .ovui-select input"
QIANCHUAN_PAYMENT_OPTION = "xpath=/html/body/div[3]/div/div/div/div/div[3]/div/span/span"
QIANCHUAN_CARD_ROOT = '//*[@id="full-screen-main-content"]/div[1]/div[2]/div[1]/div[2]'
QIANCHUAN_CARD_XPATHS = {
    "消耗": QIANCHUAN_CARD_ROOT + "/div[2]/div[1]/div[2]/p[1]",
    "整体支付ROI": QIANCHUAN_CARD_ROOT + "/div[2]/div[2]/div[2]/p[1]",
    "整体成交金额": QIANCHUAN_CARD_ROOT + "/div[2]/div[3]/div[2]/p[1]",
    "GPM": QIANCHUAN_CARD_ROOT + "/div[3]/div[1]/div[2]/p[1]",
    "千川券": QIANCHUAN_CARD_ROOT + "/div[3]/div[4]/div[2]/p[1]",
    "电商平台补贴": QIANCHUAN_CARD_ROOT + "/div[3]/div[5]/div[2]/p[1]",
    "曝光": QIANCHUAN_CARD_ROOT + "/div[3]/div[7]/div[2]/p[1]/span",
}
QIANCHUAN_CARD_FIELDS = {
    "消耗": "stat_cost_for_roi2",
    "整体支付ROI": "total_prepay_and_pay_order_realtime_roi2",
    "整体成交金额": "total_pay_order_gmv_include_coupon_realtime_for_roi2",
    "GPM": "total_live_pay_order_gpm_realtime_for_roi2",
    "千川券": "total_pay_order_coupon_amount_realtime_for_roi2",
    "电商平台补贴": "total_ecom_platform_subsidy_amount_realtime_for_roi2",
    "曝光": "live_show_count_for_roi2",
}

DEFAULT_MODULES: list[dict[str, Any]] = [
    {
        "code": "detail_funnel",
        "name": "成交转化漏斗",
        "page_kind": "detail",
        "screenshot_mode": "element",
        "selector": FUNNEL_XPATH,
        "section_label": "成交转化漏斗",
        "sort_order": 1,
        "required_for_data": True,
        "notes": "详情页转化漏斗单独截图；商品点击人数从同页漏斗接口读取。",
    },
    {
        "code": "pro_traffic",
        "name": "专业版流量分析",
        "page_kind": "professional",
        "screenshot_mode": "full_page",
        "selector": "",
        "section_label": "流量分析",
        "sort_order": 2,
        "required_for_data": True,
        "notes": "专业版直播大屏切到流量分析栏目后整页截图。",
    },
    {
        "code": "pro_anchor",
        "name": "专业版主播分析",
        "page_kind": "professional",
        "screenshot_mode": "full_page",
        "selector": "",
        "section_label": "主播分析",
        "sort_order": 3,
        "required_for_data": True,
        "notes": "主播轮班与主播能力指标来自主播分析栏目接口。",
    },
    {
        "code": "base_full",
        "name": "直播大屏基础版",
        "page_kind": "base",
        "screenshot_mode": "full_page",
        "selector": "",
        "section_label": "",
        "sort_order": 4,
        "required_for_data": True,
        "notes": "基础版整页截图，并读取累计观看人数、增粉和人均观看时长。",
    },
    {
        "code": "qianchuan_full",
        "name": "巨量千川大屏",
        "page_kind": "qianchuan",
        "screenshot_mode": "full_page",
        "selector": "",
        "section_label": "",
        "sort_order": 5,
        "required_for_data": True,
        "notes": "千川整页截图与投放、券、曝光、ROI JSON 数据。",
    },
]


def module_dict(module: Any) -> dict[str, Any]:
    if isinstance(module, dict):
        return module
    return {
        key: getattr(module, key)
        for key in (
            "code",
            "name",
            "page_kind",
            "url_template",
            "screenshot_mode",
            "selector",
            "section_label",
            "sort_order",
            "enabled",
            "required_for_data",
            "notes",
        )
    }


async def _goto(page: Page, url: str) -> None:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    if response is not None and response.status >= 400:
        raise RuntimeError(f"页面返回 HTTP {response.status}: {page.url.split('?')[0]}")
    await page.wait_for_timeout(WAIT_AFTER_NAVIGATION_MS)


def _room_id(url: str) -> str:
    return parse_qs(urlparse(url).query).get("live_room_id", [""])[0]


async def _refresh_or_goto(page: Page, url: str, *, refresh: bool = True) -> bool:
    """Reload detail/discovery pages; keep matching realtime dashboards in place."""
    current, target = urlparse(page.url), urlparse(url)
    if (
        current.netloc == target.netloc
        and current.path == target.path
        and current.fragment == target.fragment
        and _room_id(page.url) == _room_id(url)
        and (
            target.path != "/board-next"
            or parse_qs(current.query).get("aavid") == parse_qs(target.query).get("aavid")
        )
    ):
        if not refresh:
            return True
        response = await page.reload(wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        if response is not None and response.status >= 400:
            raise RuntimeError(f"页面刷新返回 HTTP {response.status}: {current.path}")
        await page.wait_for_timeout(WAIT_AFTER_NAVIGATION_MS)
    else:
        await _goto(page, url)
    if "/login/" in page.url:
        raise RuntimeError("页面跳转到登录页，需恢复抖音登录态。")
    return False


async def _request_screen_data(page: Page, kind: str) -> None:
    """Repeat the observed GET inside the page; its SDK supplies fresh signing.

    Request URLs/session parameters stay in browser memory, never in settings or logs.
    An old signed URL can produce HTTP 200 with business error 11001.
    """
    suffix = {
        "pro": "/compass_api/shop/live/live_screen/core_data",
        "base": "/compass_api/shop/live/basic_live_screen/base_info",
    }[kind]
    await page.evaluate(
        """async suffix => {
        const entry = performance.getEntriesByType('resource')
            .filter(e => new URL(e.name).pathname === suffix).at(-1);
        if (!entry) return;
        const url = new URL(entry.name);
        if (url.origin !== location.origin || url.searchParams.get('room_id') !==
            new URL(location.href).searchParams.get('live_room_id')) return;
        for (const key of ['_lid', 'msToken', 'a_bogus', 'verifyFp', 'fp'])
            url.searchParams.delete(key);
        await new Promise(resolve => {
            const xhr = new XMLHttpRequest();
            xhr.open('GET', url.toString()); xhr.timeout = 15000;
            xhr.onload = xhr.onerror = xhr.ontimeout = () => resolve();
            xhr.send();
        });
    }""",
        suffix,
    )


async def _wait_screen_data(
    page: Page, collector: ResponseCollector, kind: str, *, since: int, reused: bool
) -> None:
    if reused:
        await _request_screen_data(page, kind)
    try:
        await collector.wait_for_data(kind, since=since, timeout=15)
    except RuntimeError:
        await _request_screen_data(page, kind)
        await collector.wait_for_data(kind, since=since, timeout=30)


async def _select_qianchuan_payment(page: Page, *, renew: bool = False) -> None:
    selected = page.locator(QIANCHUAN_MODE_INPUT).first
    await selected.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)
    await page.wait_for_function(
        "selector => ['乘方数据','全域净成交数据','全域支付数据'].includes(document.querySelector(selector)?.value)",
        arg=QIANCHUAN_MODE_INPUT, timeout=PAGE_TIMEOUT_MS,
    )
    current = await selected.input_value()
    if current == "全域支付数据" and not renew:
        return
    # Ended rooms stop polling; choosing the selected option does not request
    # cards again. Cycle the card view to trigger the site's own API.
    if current == "全域支付数据":
        await selected.click(timeout=5000)
        await page.get_by_text("全域净成交数据", exact=True).click(timeout=5000)
    await selected.click(timeout=5000)
    option = page.get_by_text("全域支付数据", exact=True)
    for _ in range(20):
        chosen = None
        for index in range(await option.count()):
            if await option.nth(index).is_visible():
                chosen = index
                break
        if chosen is not None:
            await option.nth(chosen).click(timeout=5000)
            break
        await page.wait_for_timeout(250)
    else:
        raise RuntimeError("千川数据口径选项未加载，无法选择全域支付数据。")
    await page.wait_for_function(
        "selector => document.querySelector(selector)?.value === '全域支付数据'",
        arg=QIANCHUAN_MODE_INPUT,
        timeout=5000,
    )


async def _prepare_qianchuan(
    page: Page, collector: ResponseCollector, *, since: int, reused: bool
) -> None:
    for attempt in range(2):
        try:
            await _qianchuan_payment_ready(
                page,
                collector,
                since=since,
                renew=reused or attempt > 0,
                timeout=20 if attempt == 0 else 30,
            )
            return
        except (RuntimeError, PlaywrightTimeoutError):
            if attempt == 1:
                raise
            # A fresh response and a rendered screenshot must both succeed.
            since = len(collector.responses)


async def _qianchuan_payment_ready(
    page: Page, collector: ResponseCollector, *, since: int, renew: bool, timeout: float
) -> None:
    await _select_qianchuan_payment(page, renew=renew)
    await collector.wait_for_data("qianchuan", since=since, timeout=timeout)
    overview = next(
        row
        for row in reversed(collector.responses[since:])
        if row.get("data_set_key") == QIANCHUAN_OVERVIEW
        and "stat_cost_for_roi2" in row.get("metric_names", [])
        and row.get("status_code") in (None, 0, "0")
        and 200 <= row.get("status", 200) < 300
    )
    cards = {
        label: {"path": path, "value": overview["totals"][QIANCHUAN_CARD_FIELDS[label]]["Value"]}
        for label, path in QIANCHUAN_CARD_XPATHS.items()
    }
    # A mode label can change before its old numbers are replaced. Confirm that
    # the screenshot's seven card values match the selected payment response.
    try:
        await page.wait_for_function(
            """({selector, cards}) => {
        if (document.querySelector(selector)?.value !== '全域支付数据') return false;
        const root = document.querySelector('#full-screen-main-content');
        if (!root?.innerText.includes('整体消耗(元)')) return false;
        return Object.values(cards).every(({path, value}) => {
            const e = document.evaluate(path, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            if (!e || e.getBoundingClientRect().width <= 0 || !/\\d/.test(e.textContent) ||
                e.textContent.includes('--')) return false;
            const text = e.textContent.replace(/[,\\s¥￥%]/g, '');
            const factor = text.includes('亿') ? 1e8 : text.includes('万') ? 1e4 : 1;
            const displayed = Number.parseFloat(text) * factor;
            return Number.isFinite(displayed) && Math.abs(displayed - Number(value)) <=
                (factor === 1 ? 0.015 : factor * 0.005);
        });
    }""",
            arg={"selector": QIANCHUAN_MODE_INPUT, "cards": cards},
            timeout=min(PAGE_TIMEOUT_MS, int(timeout * 1000)),
        )
    except PlaywrightTimeoutError:
        raise RuntimeError(
            "全域支付接口已返回，但千川指标卡未完成渲染或数值与接口不一致：" + "、".join(cards)
        ) from None


async def _page_for(context: BrowserContext, path: str, *, preferred: Page | None = None) -> Page:
    pages = [page for page in context.pages if not page.is_closed()]
    for page in pages:
        if urlparse(page.url).path == path:
            return page
    if preferred is not None and not preferred.is_closed():
        return preferred
    return await context.new_page()


async def _click_fallback(context: BrowserContext, page: Page, selector: str) -> Page:
    existing = set(context.pages)
    await page.locator(selector).click(timeout=8000)
    await page.wait_for_timeout(1300)
    opened = [candidate for candidate in context.pages if candidate not in existing]
    result = opened[-1] if opened else page
    await result.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    return result


async def _fallback_route(context: BrowserContext, kind: str, room_id: str) -> Page:
    """Use the operator's documented clicks only after a direct route fails."""
    if kind in {"detail", "pro"}:
        overview = await _page_for(context, "/shop/live-overview")
        await _refresh_or_goto(overview, LIVE_OVERVIEW_URL)
        tab = overview.locator(REALTIME_TAB)
        if await tab.count():
            await tab.click(timeout=5000)
        selector = DETAIL_FALLBACK if kind == "detail" else PRO_FALLBACK
        result = await _click_fallback(context, overview, selector)
    else:
        pro = await _page_for(context, "/screen/live/shop")
        await _refresh_or_goto(pro, portal_urls(room_id, "")["pro"], refresh=False)
        if kind == "base":
            await pro.locator(
                'xpath=//*[@id="scaleContainer"]/div[1]/div/span[1]/span[2]/div/div'
            ).click(timeout=8000)
            result = await _click_fallback(
                context, pro, "xpath=/html/body/div[5]/div/div/div/div/div[2]/div[1]"
            )
        else:
            await pro.locator('xpath=//*[@id="scaleContainer"]/div[2]/a[4]/div').click(timeout=8000)
            result = await _click_fallback(
                context, pro, 'xpath=//*[@id="fullpage"]/div/div[4]/div/div/div/div/button/span'
            )
    if _room_id(result.url) != room_id:
        raise RuntimeError(f"页面点击兜底后直播间 ID 不匹配：{kind}")
    return result


async def _overview_payloads(page: Page) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read bodies immediately and wait through Compass SSO document changes."""
    history_path = "/compass_api/shop/live/live_overview/live_room_detail_v2"
    loop = asyncio.get_running_loop()
    generation = 0
    changed_at = loop.time()
    payloads: dict[str, dict[str, Any]] = {}
    request_documents: dict[Any, int] = {}
    readers: set[asyncio.Task[None]] = set()
    last_problem = "尚未收到完整场次接口"

    def navigated(frame: Any) -> None:
        nonlocal generation, changed_at
        if frame == page.main_frame:
            generation += 1
            changed_at = loop.time()
            payloads.clear()

    async def read(response: Any, path: str, document: int) -> None:
        nonlocal last_problem
        try:
            if response.status != 200:
                last_problem = f"场次接口 HTTP {response.status}"
                if generation == document:
                    payloads.pop(path, None)
                return
            body = await response.json()
            if path == REALTIME_PATH:
                realtime_rooms(body)
            else:
                history_rooms(body)
            if generation == document:
                payloads[path] = body
        except Exception:
            # Redirected response bodies, login business errors and incomplete
            # structures are transient candidates, never empty room lists.
            last_problem = "场次接口正文失效、登录未完成或列表结构无效"
            if generation == document:
                payloads.pop(path, None)

    def requested(request: Any) -> None:
        parsed = urlparse(request.url)
        if parsed.netloc != "compass.jinritemai.com" or parsed.path not in (REALTIME_PATH, history_path):
            return
        request_documents[request] = generation

    def received(response: Any) -> None:
        document = request_documents.pop(response.request, None)
        if document is None or document != generation:
            return
        task = asyncio.create_task(read(response, urlparse(response.url).path, document))
        readers.add(task)
        task.add_done_callback(readers.discard)

    async def attempt() -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            await page.goto(LIVE_OVERVIEW_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        except PlaywrightError as exc:
            if "ERR_ABORTED" not in str(exc):
                raise
            # The site's own SSO navigation can interrupt the requested route.
        while True:
            if (urlparse(page.url).path == "/shop/live-overview"
                    and loop.time() - changed_at >= COMPASS_READY_STABLE_SECONDS
                    and REALTIME_PATH in payloads and history_path in payloads):
                return payloads[REALTIME_PATH], payloads[history_path]
            await asyncio.sleep(0.1)

    page.on("framenavigated", navigated)
    page.on("request", requested)
    page.on("response", received)
    try:
        for number in range(2):
            generation += 1
            payloads.clear()
            changed_at = loop.time()
            try:
                return await asyncio.wait_for(attempt(), timeout=PAGE_TIMEOUT_MS / 1000)
            except TimeoutError:
                logger.warning("Compass discovery readiness timed out (attempt %s): %s", number + 1, last_problem)
        raise RuntimeError(
            "罗盘登录/场次接口未就绪，等待并重试一次后仍失败：" + last_problem
            + "。请在保留的罗盘标签页确认登录、验证和访问权限，再重新采集。"
        )
    finally:
        page.remove_listener("response", received)
        page.remove_listener("request", requested)
        page.remove_listener("framenavigated", navigated)
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


async def _overview_data(context: BrowserContext, start: datetime | None = None, end: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    page = await _page_for(context, "/shop/live-overview")
    history_path = "/compass_api/shop/live/live_overview/live_room_detail_v2"
    active_payload, history_payload = await _overview_payloads(page)
    active = realtime_rooms(active_payload)
    if start is None:
        return active, history_rooms(history_payload)[0]
    rooms: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_no = 1
    while True:
        # Clone the observed route, leaving signing inside the page's own SDK.
        payload = await page.evaluate(
            """({path, begin, end, pageNo}) => new Promise((resolve, reject) => {
            const entry = performance.getEntriesByType('resource').find(e => new URL(e.name).pathname === path);
            if (!entry) { reject(new Error('场次列表请求不存在')); return; }
            const url = new URL(entry.name);
            for (const key of ['_lid', 'msToken', 'a_bogus', 'verifyFp', 'fp']) url.searchParams.delete(key);
            Object.entries({begin_date:begin, end_date:end, date_type:'4', a_type:'-1', page_no:String(pageNo), page_size:'50'}).forEach(([k,v])=>url.searchParams.set(k,v));
            // Remove account/name filters selected in a previous operator session.
            for (const key of ['author_id', 'author_ids', 'keyword']) url.searchParams.delete(key);
            const xhr = new XMLHttpRequest(); xhr.open('GET', url.toString()); xhr.timeout=20000;
            xhr.onload=()=>{if(xhr.status!==200){reject(new Error('场次列表 HTTP 失败'));return;}try{resolve(JSON.parse(xhr.responseText));}catch{reject(new Error('场次列表非 JSON'));}};
            xhr.onerror=xhr.ontimeout=()=>reject(new Error('场次列表请求失败'));
            xhr.send();
        })""",
            {"path": history_path, "begin": start.strftime("%Y/%m/%d 00:00:00"),
             "end": (end or start).strftime("%Y/%m/%d 00:00:00"), "pageNo": page_no},
        )
        rows, total = history_rooms(payload)
        if not rows and len(rooms) < total:
            raise RuntimeError("历史场次分页不完整，停止生成报表。")
        ids = [row["live_room_id"] for row in rows]
        if seen.intersection(ids) or len(ids) != len(set(ids)):
            raise RuntimeError("历史场次分页重复，不能保证完整性。")
        seen.update(ids)
        rooms.extend(rows)
        if len(rooms) >= total:
            return active, rooms
        page_no += 1
        if page_no > 100:
            raise RuntimeError("历史场次超过分页上限，不能保证完整性。")


async def discover_current_room(context: BrowserContext, author_id: str = "") -> dict[str, Any]:
    active, history = await _overview_data(context)
    target = select_author(active, history, author_id)
    matches = [room for room in active if room["author_id"] == target]
    if len(matches) != 1:
        raise RuntimeError("配置账号当前没有唯一的正在直播场次；结束场次不会冒充正在直播。")
    return {**matches[0], "source": "compass_realtime_api"}


async def discover_room_sessions(context: BrowserContext, start: datetime, end: datetime, author_id: str = "") -> dict[str, Any]:
    active, history = await _overview_data(context, start, end)
    target = select_author(active, history, author_id)
    active_ids = {room["live_room_id"] for room in active if room["author_id"] == target}
    if len(active_ids) > 1:
        raise RuntimeError("配置账号返回多个正在直播场次，停止自动归属。")
    # The realtime start field may be absent; retain the list's start until detail confirmation.
    by_id = {room["live_room_id"]: room for room in history}
    for room in active:
        old = by_id.get(room["live_room_id"], {})
        by_id[room["live_room_id"]] = {**old, **{k:v for k,v in room.items() if v is not None}}
    rooms = overlapping_rooms(list(by_id.values()), start, min(end, datetime.now(APP_TIMEZONE)), target)
    if not rooms:
        raise RuntimeError("该排班时间内没有匹配账号的直播场次。")
    return {"source": "compass_interval_sessions", "author_id": target,
            "active_room_id": next(iter(active_ids), ""), "rooms": rooms}


async def _report_room(
    context: BrowserContext, *, settings: dict[str, str], schedule: Any,
    baseline: dict[str, Any] | None, saved: list[dict[str, Any]],
    start: datetime, end: datetime, interrupted_at: datetime | None,
) -> dict[str, Any]:
    """Keep a confirmed room across handoffs; only a manual mark unlocks discovery."""
    baseline = baseline or {}
    author = settings.get("live_author_id", "") or str(baseline.get("author_id") or "")
    pinned = str(schedule.live_room_id or baseline.get("live_room_id")
                 or settings.get("live_room_id", "")).strip()
    marked = {s["room"]["live_room_id"] for s in saved}
    cutoff = interrupted_at or min(end, datetime.now(APP_TIMEZONE))
    prior = [s for s in saved if local_datetime(s["source"]["segment_end"]) < cutoff]
    left = max((local_datetime(s["source"]["segment_end"]) for s in prior), default=start)

    # Retrying the same mark must recapture the old room, even if a new one is live.
    retry = next((s for s in saved if interrupted_at is not None
                  and local_datetime(s["source"]["segment_end"]) == interrupted_at), None)
    if retry:
        target = {**retry["room"], "author_id": author or retry["room"].get("author_id", "")}
        return {"rooms": [target], "author_id": target["author_id"], "source": "interruption_retry"}
    if pinned and pinned not in marked:
        source = "schedule_room" if schedule.live_room_id else "handoff_room"
        if not schedule.live_room_id and pinned != str(baseline.get("live_room_id") or ""):
            source = "configured_room"
        return {"rooms": [{"live_room_id": pinned, "author_id": author}],
                "author_id": author, "source": source}
    if not _automatic_room_for(schedule):
        raise RuntimeError("非当天排班无法自动发现直播间 ID，请在排班中填写直播间 ID。")

    # Use the business interval, including both dates of a midnight handoff.
    active, history = await _overview_data(context, start, cutoff)
    author = select_author(active, history, author)
    by_id = {r["live_room_id"]: r for r in history}
    for r in active:
        by_id[r["live_room_id"]] = {**by_id.get(r["live_room_id"], {}),
                                     **{k: v for k, v in r.items() if v is not None}}
    candidates = [r for r in overlapping_rooms(list(by_id.values()), left, cutoff, author)
                  if r["live_room_id"] not in marked]
    audit = {"author_id": author, "query_start": start.isoformat(),
             "query_end": cutoff.isoformat(),
             "active_room_ids": [r["live_room_id"] for r in active if r["author_id"] == author],
             "candidate_room_ids": [r["live_room_id"] for r in candidates]}
    if interrupted_at is not None and candidates:
        # An explicit mark can locate the latest old room before its marked time.
        candidates = candidates[-1:]
    if len(candidates) == 1:
        return {**audit, "rooms": candidates,
                "source": "manual_restart" if saved else "initial_discovery"}
    if not candidates and saved and interrupted_at is None:
        last = max(saved, key=lambda s: local_datetime(s["source"]["segment_end"]))
        old = by_id.get(last["room"]["live_room_id"])
        if old and old["author_id"] == author and old.get("is_living") is False:
            return {**audit, "rooms": [old], "source": "interruption_without_restart"}
    raise RuntimeError(
        f"班次日期范围 {start.date()} 至 {cutoff.date()} 内，账号 {author} "
        f"匹配 {len(candidates)} 个未保存的场次；"
        + ("存在多场直播，请逐场标记中断，不能自动选最新场。" if candidates
           else "未找到可归属场次，请核对已保存场次和中断记录。")
    )


async def ensure_shop_login(context: BrowserContext, settings: dict[str, str]) -> None:
    """Reuse an FXG tab and the existing merchant login recovery state machine."""
    page = next(
        (
            item
            for item in context.pages
            if not item.is_closed() and urlparse(item.url).netloc == "fxg.jinritemai.com"
        ),
        None,
    )
    if page is None:
        page = await _page_for(context, "/ffa/mshop/homepage/index")
    token = set_alert_settings(settings)
    try:
        await goto_with_douyin_login(
            page,
            DOUYIN_HOME_URL,
            settings.get("shop_name", ""),
            company=settings.get("shop_name", ""),
            login_alert_purpose="抖店登录态失效",
        )
    finally:
        reset_alert_settings(token)


def _automatic_room_for(schedule: Any) -> bool:
    now = datetime.now(APP_TIMEZONE)
    end = schedule_end_datetime(schedule.work_date, schedule.start_time, schedule.end_time)
    return schedule.work_date == now.date() or end.date() == now.date()


async def _find_text(page: Page, text: str) -> Any:
    exact = page.get_by_text(text, exact=True).last
    if await exact.count() == 0:
        exact = page.get_by_text(text, exact=False).last
    return exact


async def _prepare_capture_viewport(page: Page, module_code: str) -> dict[str, int] | None:
    """Expand a compact Cloak window's CSS viewport only while capturing.

    Compass can lay out a fixed-size dashboard for the visible page even when
    its screenshot root is much larger. A temporary desktop viewport lets the
    page recalculate its dashboard scale without resizing the actual window.
    Qianchuan already renders correctly in the compact profile, so preserve its
    current viewport and screenshot path.
    """
    if module_code == "qianchuan_full":
        return None

    metrics = await page.evaluate(
        """() => ({
            width: window.innerWidth,
            height: window.innerHeight,
            screenWidth: window.screen?.availWidth,
            screenHeight: window.screen?.availHeight
        })"""
    )
    original = {"width": int(metrics["width"]), "height": int(metrics["height"])}
    if (
        original["width"] >= CAPTURE_MIN_VIEWPORT["width"]
        and original["height"] >= CAPTURE_MIN_VIEWPORT["height"]
    ):
        return None

    def desktop_dimension(value: Any, *, axis: str) -> int:
        minimum = CAPTURE_MIN_VIEWPORT[axis]
        fallback = CAPTURE_FALLBACK_VIEWPORT[axis]
        try:
            screen_dimension = int(value)
        except (TypeError, ValueError):
            screen_dimension = fallback
        if screen_dimension <= 0:
            screen_dimension = fallback
        # Use the real display size when available, with a desktop-sized floor
        # for remote profiles whose screen metadata is missing or very small.
        return max(minimum, min(screen_dimension, fallback))

    target = {
        "width": max(original["width"], desktop_dimension(metrics.get("screenWidth"), axis="width")),
        "height": max(original["height"], desktop_dimension(metrics.get("screenHeight"), axis="height")),
    }
    await page.set_viewport_size(target)
    try:
        await page.wait_for_function(
            "size => window.innerWidth >= size.width && window.innerHeight >= size.height",
            arg=target,
            timeout=5_000,
        )
        await page.wait_for_timeout(100)
    except Exception:
        await page.set_viewport_size(original)
        raise
    return original


async def _save_screenshot(
    page: Page,
    module: dict[str, Any],
    run_id: str,
    work_date: date,
    schedule_id: int | str,
) -> dict[str, Any]:
    directory = ARTIFACT_ROOT / work_date.isoformat() / str(schedule_id) / run_id
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{module['code']}.png"
    mode = module["screenshot_mode"]
    restore_viewport = await _prepare_capture_viewport(page, module["code"])
    try:
        if mode == "element":
            selector = str(module.get("selector") or FUNNEL_XPATH)
            target = (
                page.locator(f"xpath={selector}")
                if selector.startswith(("/", "("))
                else page.locator(selector)
            )
            if module["code"] == "detail_funnel":
                # The portal changes positional wrappers between realtime and ended
                # rooms. Keep the configured XPath, then locate the same funnel by title.
                if not await target.count() or not await target.is_visible():
                    title = page.get_by_text("成交转化漏斗", exact=True).last
                    await title.wait_for(state="visible", timeout=10_000)
                    target = title.locator("..")
                await target.locator("canvas").first.wait_for(state="visible", timeout=10_000)
            await target.wait_for(state="visible", timeout=10_000)
            try:
                await target.screenshot(path=str(destination), animations="disabled", timeout=8_000)
            except PlaywrightTimeoutError:
                # Live funnel widgets keep animating; a clipped page capture does not wait
                # for the element to become stable and still contains only that element.
                box = await target.bounding_box(timeout=5_000)
                if not box or box["width"] <= 0 or box["height"] <= 0:
                    raise
                await page.screenshot(
                    path=str(destination), clip=box, animations="disabled", timeout=10_000
                )
        elif mode == "full_page":
            await page.screenshot(
                path=str(destination), full_page=True, animations="disabled", timeout=PAGE_TIMEOUT_MS
            )
        else:
            raise RuntimeError(f"未知截图模式：{mode}")
    finally:
        if restore_viewport is not None:
            try:
                await page.set_viewport_size(restore_viewport)
            except Exception:
                logger.exception("Failed to restore the browser viewport after screenshot capture")
                raise
    return {
        "code": module["code"],
        "name": module["name"],
        "mode": mode,
        "path": str(destination.resolve()),
        "relative_path": str(destination.resolve().relative_to(ARTIFACT_ROOT.resolve())),
        "size_bytes": destination.stat().st_size,
        "captured_at": datetime.now().astimezone().isoformat(),
    }


async def _capture_report_pages(
    context: BrowserContext,
    *,
    room_id: str,
    aavid: str,
    modules: list[dict[str, Any]],
    run_id: str,
    work_date: date,
    schedule_id: int | str,
    screenshots: bool,
    progress: Callable[[str, str, str], None] | None = None,
    on_captured: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    urls = portal_urls(room_id, aavid)
    collector = ResponseCollector()
    bound_pages: set[Page] = set()
    reused_pages: dict[str, bool] = {}

    async def load(kind: str, path: str) -> Page:
        page = next(
            (
                candidate
                # A successful recovery tab should be reused before an older,
                # retained tab whose stopped-room initialization has stalled.
                for candidate in (reversed(context.pages) if kind == "qianchuan" else context.pages)
                if not candidate.is_closed()
                and urlparse(candidate.url).path == path
                and _room_id(candidate.url) == room_id
            ),
            None,
        )
        if page is None:
            page = await context.new_page()
        if page not in bound_pages:
            collector.bind(page)
            bound_pages.add(page)
        try:
            # Background Chrome tabs can suspend the portal's initialization/polling.
            await page.bring_to_front()
            reused_pages[kind] = await _refresh_or_goto(page, urls[kind], refresh=kind == "detail")
        except Exception as direct_error:
            logger.warning(
                "Direct %s route failed, trying documented DOM fallback: %s", kind, direct_error
            )
            page = await _fallback_route(context, kind, room_id)
            reused_pages[kind] = True
            if page not in bound_pages:
                collector.bind(page)
                bound_pages.add(page)
            await page.bring_to_front()
        return page

    captured: list[dict[str, Any]] = []
    page_errors: list[str] = []
    configured = {module["code"]: module for module in modules}
    enabled = {code: module for code, module in configured.items() if module.get("enabled", True)}

    def note(stage: str, description: str, status: str = "info") -> None:
        if progress:
            progress(stage, description, status)

    try:
        # Detail page: capture funnel JSON and the requested element screenshot.
        try:
            note("detail", "进入罗盘直播详情页，读取商品点击漏斗数据。")
            since = len(collector.responses)
            page = await load("detail", "/shop/live-detail")
            try:
                await collector.wait_for_data("detail", since=since, timeout=15)
            except RuntimeError as exc:
                note("detail", str(exc) + "；将尝试由成交人数和商品点击转化率计算。", "info")
            if screenshots and "detail_funnel" in enabled:
                try:
                    captured.append(
                        await _save_screenshot(
                            page, enabled["detail_funnel"], run_id, work_date, schedule_id
                        )
                    )
                    note("detail_funnel", "直播转化漏斗截图已保存。", "succeeded")
                except Exception as exc:
                    page_errors.append(f"转化漏斗截图：{exc}")
                    note("detail_funnel", f"转化漏斗截图失败：{str(exc)[:300]}", "failed")
            await collector.flush(timeout=12)
            note("detail", "详情页接口已请求。", "succeeded")
        except Exception as exc:
            page_errors.append(f"详情页：{exc}")
            note("detail", f"详情页失败：{str(exc)[:300]}", "failed")

        # Professional edition: the shift API is requested after entering 主播分析.
        try:
            note("pro", "进入罗盘专业版，读取流量分析与主播分析。")
            since = len(collector.responses)
            page = await load("pro", "/screen/live/shop")
            await _wait_screen_data(page, collector, "pro", since=since, reused=reused_pages["pro"])
            traffic_name = configured.get("pro_traffic", {}).get("section_label") or "流量分析"
            traffic = await _find_text(page, traffic_name)
            if await traffic.count() == 0:
                raise RuntimeError(f"无法定位“{traffic_name}”栏目。")
            await page.evaluate("window.scrollTo(0, 0)")
            try:
                await traffic.click(timeout=2500)
            except PlaywrightTimeoutError:
                await traffic.evaluate("element => element.click()")
            await page.wait_for_timeout(800)
            if screenshots and "pro_traffic" in enabled:
                captured.append(
                    await _save_screenshot(
                        page, enabled["pro_traffic"], run_id, work_date, schedule_id
                    )
                )
                note("pro_traffic", "流量分析截图已保存。", "succeeded")
            anchor_name = configured.get("pro_anchor", {}).get("section_label") or "主播分析"
            anchor = await _find_text(page, anchor_name)
            if await anchor.count() == 0:
                raise RuntimeError(f"无法定位“{anchor_name}”栏目。")
            await page.evaluate("window.scrollTo(0, 0)")
            try:
                await anchor.click(timeout=2500)
            except PlaywrightTimeoutError:
                await anchor.evaluate("element => element.click()")
            await page.wait_for_timeout(1300)
            if screenshots and "pro_anchor" in enabled:
                captured.append(
                    await _save_screenshot(
                        page, enabled["pro_anchor"], run_id, work_date, schedule_id
                    )
                )
                note("pro_anchor", "主播分析截图已保存。", "succeeded")
        except Exception as exc:
            page_errors.append(f"专业版页面：{exc}")
            note("pro", f"专业版页面失败：{str(exc)[:300]}", "failed")

        # Basic-screen base_info supplies viewers, followers and watch duration.
        try:
            note("base", "进入罗盘基础版，读取场观、增粉和停留时长。")
            since = len(collector.responses)
            page = await load("base", "/screen/shop/main")
            await _wait_screen_data(
                page, collector, "base", since=since, reused=reused_pages["base"]
            )
            if screenshots and "base_full" in enabled:
                captured.append(
                    await _save_screenshot(
                        page, enabled["base_full"], run_id, work_date, schedule_id
                    )
                )
                note("base_full", "基础版整页截图已保存。", "succeeded")
            note("base", "基础版接口已请求。", "succeeded")
        except Exception as exc:
            page_errors.append(f"基础版页面：{exc}")
            note("base", f"基础版页面失败：{str(exc)[:300]}", "failed")

        # Qianchuan contains structured statQuery JSON and the fifth full-page screenshot.
        try:
            note("qianchuan", "进入千川页面，选择全域支付数据并等待指标和卡片加载完成。")
            since = len(collector.responses)
            page = await load("qianchuan", "/board-next")
            try:
                await _prepare_qianchuan(page, collector, since=since, reused=reused_pages["qianchuan"])
            except (RuntimeError, PlaywrightTimeoutError):
                if not reused_pages["qianchuan"]:
                    raise
                # Some retained ended-room boards keep cards but lose their
                # initialization context: mode changes/refresh produce no XHR.
                # Preserve that tab and initialize this same room once in a new
                # tab, then verify fresh payment responses against its cards.
                note("qianchuan", "原千川标签未取得有效新数据，保留原页并重新初始化同场页面。", "info")
                since = len(collector.responses)
                page = await context.new_page()
                collector.bind(page)
                bound_pages.add(page)
                await page.bring_to_front()
                await _goto(page, urls["qianchuan"])
                await _prepare_qianchuan(page, collector, since=since, reused=False)
            note(
                "qianchuan",
                "全域支付数据已就绪，消耗、千川券、补贴、曝光和 GPM 接口均完整。",
                "succeeded",
            )
            if screenshots and "qianchuan_full" in enabled:
                captured.append(
                    await _save_screenshot(
                        page, enabled["qianchuan_full"], run_id, work_date, schedule_id
                    )
                )
                note("qianchuan_full", "千川整页截图已保存。", "succeeded")
        except Exception as exc:
            page_errors.append(f"千川页面：{exc}")
            note("qianchuan", f"千川页面失败：{str(exc)[:300]}", "failed")

        if on_captured:
            note("screenshots", f"已截取 {len(captured)} 张图，立即启动飞书群发送。")
            await on_captured(list(captured))
        note("responses", "保存已确认就绪的指标接口响应。")
        await collector.flush(timeout=12)
        note("responses", f"已保存 {len(collector.responses)} 个接口响应。", "succeeded")
    finally:
        for page in bound_pages:
            if not page.is_closed():
                collector.unbind(page)
    return collector.responses, captured, page_errors


async def collect_report(
    *,
    settings: dict[str, str],
    schedule: Any,
    modules: list[Any],
    run_id: str,
    baseline: dict[str, Any] | None,
    include_daily_exposure: bool,
    progress: Callable[[str, str, str], None] | None = None,
    on_captured: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    interruption_segments: list[dict[str, Any]] | None = None,
    interrupted_at: datetime | None = None,
    on_room_confirmed: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    shop = settings.get("shop_name", "")
    room_id = str(schedule.live_room_id or settings.get("live_room_id", "")).strip()
    aavid = str(schedule.qianchuan_account_id or settings.get("qianchuan_account_id", "")).strip()
    if not aavid:
        raise RuntimeError("请配置千川广告账户 ID。")
    module_rows = [module_dict(module) for module in modules]
    start_at = shift_datetime(schedule.work_date, schedule.start_time)
    end_at = schedule_end_datetime(schedule.work_date, schedule.start_time, schedule.end_time)
    room_discovery: dict[str, Any] = {}
    segments: list[dict[str, Any]] = list(interruption_segments or []) if interrupted_at is None else []
    responses: list[dict[str, Any]] = []
    screenshots: list[dict[str, Any]] = []
    errors: list[str] = []
    if progress:
        progress("browser", "连接已登录浏览器会话并检查店铺登录。", "info")
    async with browser_context(settings) as (_, _browser, context):
        await ensure_shop_login(context, settings)
        if progress:
            progress("browser", "店铺登录状态正常。", "succeeded")
            progress("room", "确认排班场次；需要发现时等待罗盘登录过渡及有效列表接口。", "info")
        room_discovery = await _report_room(
            context, settings=settings, schedule=schedule, baseline=baseline,
            saved=list(interruption_segments or []), start=start_at, end=end_at,
            interrupted_at=interrupted_at,
        )
        rooms = room_discovery["rooms"]
        if progress:
            progress("room", f"采集目标：{rooms[0]['live_room_id']}；来源：{room_discovery['source']}。", "succeeded")
        for room in rooms:
            room_id = room["live_room_id"]
            if interrupted_at is None and any(s["room"]["live_room_id"] == room_id for s in segments):
                if room_discovery["source"] == "interruption_without_restart":
                    # No restart: finalize the already captured fragments once.
                    room_id = segments[-1]["room"]["live_room_id"]
                    break
                raise RuntimeError("已保存该场中断快照，请等待新场开播后再完成班次报告。")
            if progress:
                progress("room", f"采集场次 {room_id} 的班次片段。", "info")
            room_responses, images, page_errors = await _capture_report_pages(
                context, room_id=room_id, aavid=aavid, modules=module_rows,
                run_id=f"{run_id}/{room_id}", work_date=schedule.work_date,
                schedule_id=schedule.id, screenshots=True, progress=progress,
            )
            captured_at = datetime.now(APP_TIMEZONE)
            room = confirmed_room(room_responses, room)
            if local_datetime(room["live_start"]) >= (interrupted_at or min(end_at, captured_at)):
                raise RuntimeError("已绑定场次在本次采集区间之后开播，拒绝归入该班次。")
            if room.get("live_end") and local_datetime(room["live_end"]) <= start_at:
                raise RuntimeError("已绑定场次在本班次开始前结束，请核对场次 ID。")
            room_discovery["rooms"] = [room]
            room_discovery["author_id"] = room["author_id"]
            for row in room_responses:
                if row.get("live_room_id") and row["live_room_id"] != room_id:
                    raise RuntimeError("采集响应场次不匹配，拒绝混入其他直播间。")
                if row.get("qianchuan_account_id") and row["qianchuan_account_id"] != aavid:
                    raise RuntimeError("千川响应广告账户不匹配，拒绝混用消耗数据。")
                row["live_room_id"] = room_id
            if on_room_confirmed:
                on_room_confirmed({**room_discovery, "live_room_id": room_id})
            snapshot = capture_snapshot(room_responses, room_id, aavid, captured_at.isoformat())
            interval_end = interrupted_at or min(end_at, captured_at)
            earlier = [s for s in (interruption_segments or [])
                       if s["room"]["live_room_id"] != room_id
                       and local_datetime(s["source"]["segment_end"]) <= interval_end]
            reset = bool(segments or (interrupted_at is not None and earlier))
            left = local_datetime(earlier[-1]["source"]["segment_end"]) if earlier else start_at
            if reset and local_datetime(room["live_start"]) < left:
                raise RuntimeError("新场开播时间早于已保存的中断边界，不能从零累加。")
            source = handoff_source(
                snapshot=snapshot, room=room, start=left,
                end=interval_end, baseline=baseline, allow_new_room=reset,
            )
            for image in images:
                image.update(live_room_id=room_id, segment_start=source["segment_start"], segment_end=source["segment_end"])
                image["name"] += f" · 场次 {room_id}"
            segments.append({"room": room, "source": source, "responses": room_responses,
                             "captured_at": captured_at.isoformat(), "qianchuan_account_id": aavid,
                             "snapshot": snapshot})
            responses.extend(room_responses)
            screenshots.extend(images)
            errors.extend(f"场次 {room_id}：{error}" for error in page_errors)
    if progress:
        progress("source", "开始解析原始接口响应和时段指标。", "info")
    if interrupted_at is None:
        # Saved fragments carry their original raw responses and screenshots.
        for segment in segments:
            if segment not in (interruption_segments or []):
                continue
            responses.extend(segment["responses"])
            screenshots.extend(segment.get("screenshots", []))
            errors.extend(segment.get("page_errors", []))
    source = combine_handoffs(segments)
    if on_captured:
        await on_captured(screenshots)
    if progress:
        progress("source", "原始接口响应解析完成。", "succeeded")
    return {
        "shop_name": shop,
        "live_room_id": room_id,
        "room_discovery": room_discovery,
        "qianchuan_account_id": aavid,
        "collected_at": datetime.now().astimezone().isoformat(),
        "schedule_start": start_at.isoformat(),
        "schedule_end": end_at.isoformat(),
        "source": source,
        "responses": responses,
        "screenshots": screenshots,
        "page_errors": errors,
        "segments": segments,
        "author_id": room_discovery.get("author_id", ""),
        "is_final": interrupted_at is None and datetime.now(APP_TIMEZONE) >= end_at,
        "measurement_mode": "manual_handoffs",
    }


async def collect_baseline(
    *, settings: dict[str, str], schedule: Any, run_id: str
) -> dict[str, Any]:
    room_id = str(schedule.live_room_id or settings.get("live_room_id", "")).strip()
    aavid = str(schedule.qianchuan_account_id or settings.get("qianchuan_account_id", "")).strip()
    if not aavid:
        raise RuntimeError("请配置千川广告账户 ID。")
    collector = ResponseCollector()
    async with browser_context(settings) as (_, _browser, context):
        await ensure_shop_login(context, settings)
        if _automatic_room_for(schedule):
            room_id = (await discover_current_room(context, settings.get("live_author_id", "")))["live_room_id"]
        elif not room_id:
            raise RuntimeError("非当天排班无法自动发现直播间 ID，请在排班中填写直播间 ID。")
        page = await _page_for(context, "/board-next")
        collector.bind(page)
        try:
            await page.bring_to_front()
            reused = await _refresh_or_goto(
                page, portal_urls(room_id, aavid)["qianchuan"], refresh=False
            )
            await _prepare_qianchuan(page, collector, since=0, reused=reused)
            await collector.flush(timeout=12)
        finally:
            collector.unbind(page)
    values = {
        "live_room_id": room_id,
        "qianchuan": qianchuan_snapshot(collector.responses),
        "responses": collector.responses,
        "captured_at": datetime.now().astimezone().isoformat(),
    }
    if not values["qianchuan"]:
        raise RuntimeError("千川页面没有返回可用的累计基线；请确认直播间和广告账户选择正确。")
    return values


async def discover_live_shifts(
    settings: dict[str, str], room_id: str | None = None
) -> list[dict[str, Any]]:
    target_room_id = (room_id or settings.get("live_room_id", "")).strip()
    aavid = settings.get("qianchuan_account_id", "").strip()
    collector = ResponseCollector()
    async with browser_context(settings) as (_, _browser, context):
        await ensure_shop_login(context, settings)
        if not room_id:
            target_room_id = (await discover_current_room(context))["live_room_id"]
        if not target_room_id:
            raise RuntimeError("无法获取当前直播间 ID。")
        page = await _page_for(context, "/screen/live/shop")
        collector.bind(page)
        try:
            await _refresh_or_goto(page, portal_urls(target_room_id, aavid)["pro"])
            anchor = await _find_text(page, "主播分析")
            if await anchor.count():
                await anchor.click(timeout=5000)
            await page.wait_for_timeout(1800)
            await collector.flush(timeout=5)
        finally:
            collector.unbind(page)
    shifts = extract_shift_records(collector.responses)
    for shift in shifts:
        shift["live_room_id"] = target_room_id
    return shifts
