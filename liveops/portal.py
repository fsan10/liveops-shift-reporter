from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from liveops.cloak import current_endpoint

logger = logging.getLogger(__name__)
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Shanghai"))
SOURCE_MAPPING_VERSION = "2026-10-08-payment-screen-api-v2"
QIANCHUAN_OVERVIEW = "board_roi2_overview_conf_next"
QIANCHUAN_REQUIRED_METRICS = {
    "stat_cost_for_roi2": "消耗",
    "total_pay_order_coupon_amount_realtime_for_roi2": "千川券",
    "total_ecom_platform_subsidy_amount_realtime_for_roi2": "电商平台补贴",
    "live_show_count_for_roi2": "曝光",
    "total_live_pay_order_gpm_realtime_for_roi2": "GPM",
    "total_prepay_and_pay_order_realtime_roi2": "整体支付ROI卡片",
    "total_pay_order_gmv_include_coupon_realtime_for_roi2": "整体成交金额卡片",
}

DETAIL_METRIC_PATHS = (
    "/compass_api/shop/live/live_room/trade",
    "/business_api/shop/live_room/tab/core_index",
    "/business_api/shop/live_room/core_index_group",
    "/business_api/shop/live_room/flow/gmv_interaction",
    "/business_api/shop/live_room/flow/trend_analysis_v2",
    "/compass_api/shop/live/live_room/liveroom_base_info",
)
SCREEN_METRIC_PATHS = (
    "/compass_api/shop/live/live_screen/core_data",
    "/compass_api/shop/live/live_screen/anchor_shift_list",
    "/compass_api/shop/live/live_screen/anchor_lidar_detail",
    "/compass_api/content_live/shop/live_screen/blend_trend_v3",
    "/compass_api/shop/live/basic_live_screen/data_digest",
    "/compass_api/shop/live/basic_live_screen/base_info",
)
TREND_FIELDS = (
    "pay_amt",
    "pay_ucnt",
    "product_click_ucnt",
    "interaction_rate",
    "incr_fans_cnt",
    "per_capita_viewing_time",
    "gpm",
)
DETAIL_URL = "https://compass.jinritemai.com/shop/live-detail"
PRO_URL = "https://compass.jinritemai.com/screen/live/shop"
BASE_URL = "https://compass.jinritemai.com/screen/shop/main"
QIANCHUAN_URL = "https://qianchuan.jinritemai.com/board-next"
FUNNEL_XPATH = '//*[@id="root"]/div[2]/div/div/div[3]/div/div[2]/div[4]'


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _qianchuan_summary(request_body: Any, response_body: Any) -> dict[str, Any]:
    if not isinstance(response_body, dict):
        return {"data_set_key": "", "totals": {}}
    data = response_body.get("data") or {}
    stats = data.get("StatsData") or {} if isinstance(data, dict) else {}
    totals = stats.get("Totals") or {} if isinstance(stats, dict) else {}
    keys = [
        "total_pay_order_coupon_amount_realtime_for_roi2",
        "total_ecom_platform_subsidy_amount_realtime_for_roi2",
        "stat_cost_for_overall_roi2",
        "stat_cost_for_roi2",
        "total_prepay_and_pay_order_realtime_roi2",
        "total_pay_order_gmv_include_coupon_realtime_for_roi2",
        "stat_cost",
        "live_show_count_for_roi2",
        "live_watch_count_for_roi2",
        "live_watch_ucount_for_roi2",
        "total_live_pay_order_gpm_realtime_for_roi2",
        "total_order_settle_amount_realtime_for_overall_roi2_1h",
        "total_prepay_and_pay_settle_realtime_overall_roi2_1h",
        "total_live_pay_amt_for_roi2",
        "live_product_click_count_for_roi2",
        "total_pay_order_count_for_roi2",
    ]
    selected = {key: _json_safe(totals[key]) for key in keys if key in totals}
    request_body = request_body if isinstance(request_body, dict) else {}
    scopes = {
        row.get("Field"): (row.get("Values") or [""])[0]
        for row in (request_body.get("Filters") or {}).get("Conditions", [])
    }
    return {
        "data_set_key": str(request_body.get("DataSetKey", "")),
        "start_time": request_body.get("StartTime"),
        "end_time": request_body.get("EndTime"),
        "metric_names": _json_safe(request_body.get("Metrics", [])),
        "totals": selected,
        "status_code": response_body.get("status_code"),
        "message": str(response_body.get("message", ""))[:300],
        "live_room_id": str(scopes.get("room_id") or ""),
        "qianchuan_account_id": str(scopes.get("advertiser_id") or ""),
    }


class ResponseCollector:
    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []
        self.pending: set[asyncio.Task[Any]] = set()
        self.changed = asyncio.Event()

    def bind(self, page: Page) -> None:
        page.on("response", self._handle_event)

    def unbind(self, page: Page) -> None:
        page.remove_listener("response", self._handle_event)

    def _handle_event(self, response: Any) -> None:
        task = asyncio.create_task(self._capture(response))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _capture(self, response: Any) -> None:
        try:
            request = response.request
            url = response.url
            path = url.split("?", 1)[0]
            if "qianchuan.jinritemai.com/ad/api/data/v1/common/statQuery" in path:
                if request.method != "POST":
                    return
                try:
                    body = request.post_data_json
                except Exception:
                    body = {}
                try:
                    payload = await response.json()
                except Exception:
                    return
                self.responses.append(
                    {
                        "source": "qianchuan",
                        "path": "/ad/api/data/v1/common/statQuery",
                        "status": response.status,
                        "captured_at": datetime.now(APP_TIMEZONE).isoformat(),
                        **_qianchuan_summary(body, payload),
                    }
                )
                self.changed.set()
                return

            if not any(
                path.endswith(marker) for marker in DETAIL_METRIC_PATHS + SCREEN_METRIC_PATHS
            ):
                return
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            payload = await response.json()
            from urllib.parse import parse_qs, urlsplit

            query = parse_qs(urlsplit(request.url).query)
            self.responses.append(
                {
                    "source": "compass",
                    "path": "/".join(path.split("/")[-5:]),
                    "path_full": path,
                    "status": response.status,
                    "status_code": payload.get("code", payload.get("status_code", payload.get("st")))
                    if isinstance(payload, dict)
                    else None,
                    "captured_at": datetime.now(APP_TIMEZONE).isoformat(),
                    "live_room_id": str((query.get("room_id") or query.get("live_room_id") or [""])[0]),
                    "query_keys": sorted(
                        part.split("=", 1)[0]
                        for part in request.url.split("?", 1)[1].split("&")
                        if "=" in part
                    )
                    if "?" in request.url
                    else [],
                    "data": _json_safe(
                        payload.get("data") if isinstance(payload, dict) else payload
                    ),
                    "message": str(payload.get("msg", payload.get("message", "")))[:300]
                    if isinstance(payload, dict)
                    else "",
                }
            )
            self.changed.set()
        except Exception as exc:  # portal can close or abort individual XHRs while navigating
            logger.debug("Skipping portal response: %s", exc)

    async def wait_for_data(self, kind: str, *, since: int = 0, timeout: float = 45) -> None:
        """Wait for future responses too, rather than only already pending body reads."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            self.changed.clear()
            missing = missing_page_metrics(self.responses[since:], kind)
            if not missing:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError("等待页面数据超时，未取得有效指标：" + "、".join(missing))
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except TimeoutError:
                raise RuntimeError(
                    "等待页面数据超时，未取得有效指标：" + "、".join(missing)
                ) from None

    async def flush(self, timeout: float = 4.0) -> None:
        if self.pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self.pending), return_exceptions=True), timeout
                )
            except TimeoutError:
                logger.warning("Timed out waiting for %s portal response bodies", len(self.pending))


@asynccontextmanager
async def browser_context(
    settings: dict[str, str],
) -> AsyncIterator[tuple[Playwright, Browser | None, BrowserContext]]:
    playwright = await async_playwright().start()
    browser: Browser | None = None
    context: BrowserContext | None = None
    mode = settings.get("browser_mode", "cloak_cdp")
    try:
        if mode == "cloak_cdp":
            endpoint = await current_endpoint(settings)
            browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=20_000)
            if not browser.contexts:
                raise RuntimeError("CDP 已连接，但 profile 没有可复用的浏览器上下文。")
            context = browser.contexts[0]
        elif mode == "native":
            user_data = Path(settings.get("native_user_data_dir", "var/native-browser-profile"))
            user_data.mkdir(parents=True, exist_ok=True)
            context_options: dict[str, Any] = {}
            try:
                context_options = json.loads(settings.get("native_context_options", "{}"))
            except json.JSONDecodeError as exc:
                raise RuntimeError("原生浏览器上下文参数必须是有效 JSON。") from exc
            executable = settings.get("native_chromium_path", "").strip()
            context = await playwright.chromium.launch_persistent_context(
                str(user_data.resolve()),
                executable_path=executable or None,
                headless=False,
                args=["--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
                **context_options,
            )
        else:
            raise RuntimeError(f"不支持的浏览器模式：{mode}")
        yield playwright, browser, context
    finally:
        if mode == "native" and context is not None:
            await context.close()
        if browser is not None:
            # Closing a Playwright CDP client disconnects from the Cloak-managed browser;
            # it does not call Cloak Login's stop_profile lifecycle API.
            await browser.close()
        await playwright.stop()


def portal_urls(live_room_id: str, qianchuan_account_id: str) -> dict[str, str]:
    return {
        "detail": f"{DETAIL_URL}?{urlencode({'live_room_id': live_room_id})}",
        "pro": f"{PRO_URL}?{urlencode({'live_room_id': live_room_id})}#data",
        "base": f"{BASE_URL}?{urlencode({'live_room_id': live_room_id})}",
        "qianchuan": f"{QIANCHUAN_URL}?{urlencode({'live_room_id': live_room_id, 'aavid': qianchuan_account_id, 'ad_origin': 1, 'fromScene': 'luopan'})}",
    }


def time_from_epoch(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone(APP_TIMEZONE)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def shift_datetime(work_date: date, value: time) -> datetime:
    return datetime.combine(work_date, value, tzinfo=APP_TIMEZONE)


def schedule_end_datetime(work_date: date, start_time: time, end_time: time) -> datetime:
    end_date = work_date + timedelta(days=1) if end_time <= start_time else work_date
    return datetime.combine(end_date, end_time, tzinfo=APP_TIMEZONE)


def _data_for_path(responses: list[dict[str, Any]], suffix: str) -> list[dict[str, Any]]:
    return [
        item
        for item in responses
        if item.get("source") == "compass" and item.get("path_full", "").endswith(suffix)
    ]


def _compass_data(item: dict[str, Any], expected_key: str) -> dict[str, Any]:
    data = item.get("data") or {}
    for _ in range(3):
        if not isinstance(data, dict):
            return {}
        if expected_key in data or not isinstance(data.get("data"), dict):
            return data
        data = data["data"]
    return data if isinstance(data, dict) else {}


def _unwrap_value(obj: Any) -> Any:
    if isinstance(obj, dict):
        for key in ("value", "Value"):
            if key in obj and isinstance(obj[key], (str, int, float)):
                return obj[key]
    return obj


def _money_yuan(value: Any, cents: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)
        return (amount / 100 if cents else amount) if math.isfinite(amount) else None
    except (ValueError, TypeError):
        return None


def _metric_value(items: list[dict[str, Any]], name: str, cents: bool = False) -> float | None:
    for item in items:
        data = _compass_data(item, "core_data")
        for row in data.get("core_data", []):
            if row.get("index_name") == name:
                value = _unwrap_value(row.get("value"))
                return _money_yuan(value, cents=cents) if cents else _money_yuan(value)
    return None


def _anchor_metrics(responses: list[dict[str, Any]], host_name: str) -> dict[str, Any]:
    for item in _data_for_path(responses, "/compass_api/shop/live/live_screen/anchor_lidar_detail"):
        data = _compass_data(item, "info_list")
        for row in data.get("info_list", []):
            if row.get("name", "").strip() == host_name.strip():
                return row
    return {}


def _latest_screen_data(responses: list[dict[str, Any]], suffix: str, key: str) -> dict[str, Any]:
    for item in reversed(_data_for_path(responses, suffix)):
        data = _compass_data(item, key)
        if key in data:
            return data
    return {}


def _screen_metric(data: dict[str, Any], name: str) -> float | None:
    for row in data.get("core_data", []):
        if row.get("index_name") == name:
            return _money_yuan(_unwrap_value(row.get("value")))
    return None


def _funnel_click_users(responses: list[dict[str, Any]]) -> float | None:
    for item in reversed(
        _data_for_path(responses, "/business_api/shop/live_room/flow/gmv_interaction")
    ):
        data = _compass_data(item, "gmv_change")
        for row in data.get("gmv_change", []):
            if row.get("index_name") == "商品点击人数":
                return _money_yuan(row.get("value"))
    return None


def _valid_response(item: dict[str, Any]) -> bool:
    return 200 <= item.get("status", 200) < 300 and item.get("status_code") in (None, 0, "0")


def _finite_number(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def missing_page_metrics(responses: list[dict[str, Any]], kind: str) -> list[str]:
    """A successful HTTP response or an empty/skeleton card is not data readiness."""
    valid = [row for row in responses if _valid_response(row)]
    if kind == "qianchuan":
        rows = [
            row
            for row in valid
            if row.get("data_set_key") == QIANCHUAN_OVERVIEW
            and "stat_cost_for_roi2" in row.get("metric_names", [])
        ]
        totals = rows[-1].get("totals", {}) if rows else {}
        return [
            label
            for name, label in QIANCHUAN_REQUIRED_METRICS.items()
            if not _finite_number((totals.get(name) or {}).get("Value"))
        ]
    if kind == "pro":
        data = _latest_screen_data(valid, "/live_screen/core_data", "core_data")
        fields = {
            "GMV": _screen_metric(data, "pay_amt"),
            "成交人数": _screen_metric(data, "pay_ucnt"),
            "商品点击转化率": _screen_metric(data, "product_click_pay_ucnt_ratio"),
            "互动率": _screen_metric(data, "interact_watch_ucnt_ratio"),
        }
    elif kind == "base":
        data = _latest_screen_data(valid, "/basic_live_screen/base_info", "online_user_ucnt")
        fields = {
            label: _unwrap_value(data.get(name))
            for name, label in {
                "online_user_ucnt": "场观",
                "incr_fans_cnt": "增粉",
                "avg_watch_duration": "停留时长",
            }.items()
        }
    elif kind == "detail":
        fields = {"商品点击人数": _funnel_click_users(valid)}
    else:
        raise ValueError(f"未知页面数据类型：{kind}")
    return [label for label, value in fields.items() if not _finite_number(value)]


def qianchuan_snapshot(responses: list[dict[str, Any]]) -> dict[str, float]:
    latest: dict[str, float] = {}
    for item in responses:
        if item.get("source") != "qianchuan" or not _valid_response(item):
            continue
        # Funnel/trend totals have different scopes and must not overwrite the top cards.
        if item.get("data_set_key") not in (None, "", QIANCHUAN_OVERVIEW):
            continue
        if item.get("metric_names") and "stat_cost_for_roi2" not in item["metric_names"]:
            continue
        for name, value in (item.get("totals") or {}).items():
            if isinstance(value, dict) and value.get("Value") is not None:
                try:
                    if _finite_number(value["Value"]):
                        latest[name] = float(value["Value"])
                except (TypeError, ValueError):
                    continue
    return latest


def trend_metrics(
    responses: list[dict[str, Any]],
    start_at: datetime,
    end_at: datetime,
) -> dict[str, Any]:
    values: dict[str, list[tuple[datetime, float]]] = {}
    for item in _data_for_path(responses, "/business_api/shop/live_room/flow/trend_analysis_v2"):
        data = _compass_data(item, "trend_map")
        trend_map = data.get("trend_map", {})
        if not isinstance(trend_map, dict):
            continue
        for groups in trend_map.values():
            if not isinstance(groups, list):
                continue
            for point in groups:
                name = point.get("point_name")
                stamp = time_from_epoch(point.get("date_time"))
                try:
                    numeric = float(point.get("vertical"))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(numeric) or not name or not stamp or stamp < start_at or stamp >= end_at:
                    continue
                values.setdefault(name, []).append((stamp, numeric))

    def sum_metric(name: str) -> float | None:
        points = values.get(name, [])
        if not points:
            return None
        # Multiple requests can carry the same selected series; de-duplicate by minute.
        unique = {stamp.replace(second=0, microsecond=0): value for stamp, value in points}
        return sum(unique.values())

    def weighted_mean(name: str, weight_name: str) -> float | None:
        points = values.get(name, [])
        weights = {
            stamp.replace(second=0, microsecond=0): value
            for stamp, value in values.get(weight_name, [])
        }
        unique = {stamp.replace(second=0, microsecond=0): value for stamp, value in points}
        numerator = denominator = 0.0
        for key, value in unique.items():
            weight = weights.get(key, 0)
            numerator += value * weight
            denominator += weight
        return numerator / denominator if denominator else None

    pay_amt = sum_metric("pay_amt")
    return {
        # The Compass trend endpoint returns price values in fen; other values are counts.
        "gmv_trend": pay_amt / 100 if pay_amt is not None else None,
        "paid_users": sum_metric("pay_ucnt"),
        "product_click_users": sum_metric("product_click_ucnt"),
        "viewers": sum_metric("watch_ucnt"),
        "new_followers": sum_metric("incr_fans_cnt"),
        "interaction_rate": weighted_mean("interaction_rate", "watch_ucnt"),
        "stay_seconds": weighted_mean("per_capita_viewing_time", "watch_ucnt"),
        "gpm": weighted_mean("gpm", "watch_ucnt"),
        "watch_count": sum_metric("watch_ucnt"),
        "available_metrics": sorted(values),
    }


def extract_shift_records(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in _data_for_path(responses, "/compass_api/shop/live/live_screen/anchor_shift_list"):
        data = _compass_data(item, "info_list")
        if isinstance(data, dict) and isinstance(data.get("info_list"), list):
            return [
                {
                    "host_name": row.get("name", ""),
                    "anchor_id": str(row.get("anchor_id", "")),
                    "start_at": time_from_epoch(row.get("live_start_ts")),
                    "end_at": time_from_epoch(row.get("live_end_ts")),
                }
                for row in data["info_list"]
            ]
    return []


def snapshot_values(responses: list[dict[str, Any]], host_name: str = "") -> dict[str, Any]:
    qianchuan = qianchuan_snapshot(responses)
    core = _data_for_path(responses, "/compass_api/shop/live/live_screen/core_data")
    anchors = _anchor_metrics(responses, host_name) if host_name else {}
    anchor_gmv = _money_yuan(_unwrap_value(anchors.get("pay_amt")), cents=True) if anchors else None
    return {
        "host_gmv": anchor_gmv,
        "qianchuan": qianchuan,
        "core_data": {
            "pay_amt": _metric_value(core, "pay_amt", cents=True),
            "stat_cost": _metric_value(core, "stat_cost", cents=True),
            "gpm": _metric_value(core, "gpm", cents=True),
        },
        "captured_at": datetime.now().astimezone().isoformat(),
    }


def cumulative_delta(
    current: dict[str, Any], baseline: dict[str, Any], metric: str
) -> float | None:
    current_q = current.get("qianchuan", {})
    baseline_q = baseline.get("qianchuan", {})
    current_value, baseline_value = current_q.get(metric), baseline_q.get(metric)
    if current_value is None:
        return None
    if baseline_value is None:
        return float(current_value)
    delta = float(current_value) - float(baseline_value)
    return max(0.0, delta) if delta > -0.01 else 0.0


def report_source_values(
    *,
    responses: list[dict[str, Any]],
    schedule_start: datetime,
    schedule_end: datetime,
    host_name: str,
    baseline: dict[str, Any] | None,
    include_daily_exposure: bool,
) -> dict[str, Any]:
    responses = [row for row in responses if _valid_response(row)]
    current = snapshot_values(responses, host_name)
    baseline = baseline or {}
    qian = current.get("qianchuan", {})
    base_qian = baseline.get("qianchuan", {})
    pro = _latest_screen_data(
        responses, "/compass_api/shop/live/live_screen/core_data", "core_data"
    )
    basic = _latest_screen_data(
        responses, "/compass_api/shop/live/basic_live_screen/base_info", "online_user_ucnt"
    )
    baseline_conflicts: list[str] = []
    baseline_missing: list[str] = []

    def read_total(key: str) -> float | None:
        value = qian.get(key)
        if value is None:
            return None
        prior = 0 if baseline.get("source") == "first_shift_zero" else base_qian.get(key)
        if prior is None:
            baseline_missing.append(key)
            return None
        difference = float(value) - float(prior)
        if difference < 0:
            baseline_conflicts.append(key)
        return difference if difference >= 0 else None

    gmv = _money_yuan(_unwrap_value(pro.get("pay_amt_all")), cents=True)
    paid_users = _screen_metric(pro, "pay_ucnt")
    click_conversion = _screen_metric(pro, "product_click_pay_ucnt_ratio")
    click_users = _funnel_click_users(responses)
    if click_users is None and paid_users is not None and click_conversion:
        click_users = paid_users / click_conversion
    coupon_part = read_total("total_pay_order_coupon_amount_realtime_for_roi2")
    subsidy_part = read_total("total_ecom_platform_subsidy_amount_realtime_for_roi2")
    coupon = (
        coupon_part + subsidy_part if coupon_part is not None and subsidy_part is not None else None
    )
    spend = read_total("stat_cost_for_roi2")
    exposure = read_total("live_show_count_for_roi2")
    daily_exposure = qian.get("live_show_count_for_roi2")

    result = {
        "mapping_version": SOURCE_MAPPING_VERSION,
        "gmv": gmv,
        "qianchuan_coupon": coupon,
        "paid_users": paid_users,
        "product_click_users": click_users,
        "product_click_conversion": click_conversion,
        "viewers": _money_yuan(_unwrap_value(basic.get("online_user_ucnt"))),
        "gpm": qian.get("total_live_pay_order_gpm_realtime_for_roi2"),
        "interaction_rate": _screen_metric(pro, "interact_watch_ucnt_ratio"),
        "new_followers": _money_yuan(_unwrap_value(basic.get("incr_fans_cnt"))),
        "stay_seconds": _money_yuan(_unwrap_value(basic.get("avg_watch_duration"))),
        "spend": spend,
        "exposure": exposure,
        "daily_exposure": daily_exposure,
        "source_paths": {
            "gmv": "live_screen/core_data.pay_amt_all",
            "paid_users": "live_screen/core_data.pay_ucnt",
            "product_click_users": "live_room/flow/gmv_interaction.商品点击人数",
            "viewers": "basic_live_screen/base_info.online_user_ucnt",
            "gpm": "statQuery.board_roi2_overview_conf_next.total_live_pay_order_gpm_realtime_for_roi2",
            "spend": "statQuery.board_roi2_overview_conf_next.stat_cost_for_roi2",
        },
        "baseline_conflicts": baseline_conflicts,
        "baseline_missing": baseline_missing,
    }
    return result
