from __future__ import annotations

from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

FORMULA_VERSION = "2026-10-09-manual-handoffs-v4"

REPORT_COLUMNS: list[tuple[str, str, str]] = [
    ("work_date", "日期", "date"),
    ("time_range", "时段", "text"),
    ("duration_hours", "时长/h", "number"),
    ("host_name", "主播", "text"),
    ("promoter", "投手", "text"),
    ("gmv", "GMV", "currency"),
    ("qianchuan_coupon", "千川券", "currency"),
    ("overall_amount", "整体（GMV+券）", "currency"),
    ("hourly_gmv", "单小时成交额", "currency"),
    ("paid_users", "成交人数", "integer"),
    ("avg_order_value", "平均客单价", "currency"),
    ("product_click_users", "商品点击人数", "integer"),
    ("product_click_conversion", "商品点击转化率", "percent"),
    ("viewers", "场观", "integer"),
    ("watch_conversion", "观看成交率", "percent"),
    ("gpm", "GPM", "currency"),
    ("interaction_rate", "互动率", "percent"),
    ("new_followers", "增粉", "integer"),
    ("new_follower_rate", "增粉率", "percent"),
    ("stay_seconds", "停留（s）", "number"),
    ("spend", "消耗", "currency"),
    ("exposure", "曝光", "integer"),
    ("overall_roi", "整体ROI", "number"),
    ("cpm", "CPM", "currency"),
    ("daily_exposure", "日曝光", "integer"),
]

RATIO_KEYS = {
    "product_click_conversion",
    "watch_conversion",
    "interaction_rate",
    "new_follower_rate",
}
INTEGER_KEYS = {
    "paid_users",
    "product_click_users",
    "viewers",
    "new_followers",
    "exposure",
    "daily_exposure",
}


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _rounded(value: Any, places: int = 2) -> float | None:
    number = _decimal(value)
    if number is None:
        return None
    quantum = Decimal("1") if places == 0 else Decimal("1").scaleb(-places)
    return float(number.quantize(quantum, rounding=ROUND_HALF_UP))


def _divide(numerator: Any, denominator: Any) -> Decimal | None:
    n, d = _decimal(numerator), _decimal(denominator)
    if n is None or d is None or d == 0:
        return None
    return n / d


def _ratio(value: Any) -> Decimal | None:
    ratio = _decimal(value)
    if ratio is None:
        return None
    return ratio / 100 if ratio > 1 else ratio


def build_report_metrics(
    *,
    work_date: date,
    start_time: time,
    end_time: time,
    host_name: str,
    promoter: str,
    source: dict[str, Any],
    include_daily_exposure: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Build the requested 25-column row; ratio values are stored as fractions."""
    start_minutes = start_time.hour * 60 + start_time.minute
    end_minutes = end_time.hour * 60 + end_time.minute
    if end_minutes <= start_minutes:
        end_minutes += 24 * 60
    duration_hours = Decimal(end_minutes - start_minutes) / Decimal(60)

    gmv = _decimal(source.get("gmv"))
    coupon = _decimal(source.get("qianchuan_coupon"))
    paid_users = _decimal(source.get("paid_users"))
    product_click_users = _decimal(source.get("product_click_users"))
    viewers = _decimal(source.get("viewers"))
    new_followers = _decimal(source.get("new_followers"))
    spend = _decimal(source.get("spend"))
    exposure = _decimal(source.get("exposure"))

    overall = gmv + coupon if gmv is not None and coupon is not None else None
    gpm = _decimal(source.get("gpm"))
    click_conversion = _decimal(source.get("product_click_conversion"))
    if click_conversion is None:
        click_conversion = _divide(paid_users, product_click_users)

    result: dict[str, Any] = {
        "work_date": work_date.isoformat(),
        "time_range": f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}",
        "duration_hours": _rounded(duration_hours, 2),
        "host_name": host_name,
        "promoter": promoter,
        "gmv": _rounded(gmv),
        "qianchuan_coupon": _rounded(coupon),
        "overall_amount": _rounded(overall),
        "hourly_gmv": _rounded(_divide(gmv, duration_hours)),
        "paid_users": _rounded(paid_users, 0),
        "avg_order_value": _rounded(_divide(gmv, paid_users)),
        "product_click_users": _rounded(product_click_users, 0),
        "product_click_conversion": _rounded(click_conversion, 4),
        "viewers": _rounded(viewers, 0),
        "watch_conversion": _rounded(_divide(paid_users, viewers), 4),
        "gpm": _rounded(gpm),
        "interaction_rate": _rounded(_ratio(source.get("interaction_rate")), 4),
        "new_followers": _rounded(new_followers, 0),
        "new_follower_rate": _rounded(_divide(new_followers, viewers), 4),
        "stay_seconds": _rounded(source.get("stay_seconds")),
        "spend": _rounded(spend),
        "exposure": _rounded(exposure, 0),
        "overall_roi": _rounded(_divide(overall, spend)),
        "cpm": _rounded(
            _divide(spend, exposure) * 1000 if spend is not None and exposure else None
        ),
        "daily_exposure": _rounded(source.get("daily_exposure"), 0),
        "actual_duration_hours": _rounded(_divide(source.get("actual_duration_seconds"), 3600)),
    }

    missing = [
        key
        for key, _, _ in REPORT_COLUMNS
        if result.get(key) is None and (key != "daily_exposure" or include_daily_exposure)
    ]
    return result, missing


def display_number(value: Any, kind: str) -> str:
    number = _decimal(value)
    if number is None:
        return "—"
    if kind == "percent":
        return f"{_rounded(number * 100, 2):,.2f}%"
    if kind == "integer":
        return f"{_rounded(number, 0):,.0f}"
    if kind == "currency":
        return f"{_rounded(number):,.2f}"
    return f"{_rounded(number):,.2f}"
