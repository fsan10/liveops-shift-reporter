"""Interval metrics, scoped baselines and conservative cross-session aggregation."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from liveops.portal import (
    _compass_data,
    _funnel_click_users,
    _latest_screen_data,
    _money_yuan,
    _screen_metric,
    _unwrap_value,
    _valid_response,
    qianchuan_snapshot,
    trend_metrics,
)
from liveops.sessions import covered_minute_start, local_datetime, segment_window

SEGMENT_VERSION = "2026-10-08-shift-segments-v1"
COUNT_KEYS = ("paid_users", "product_click_users", "viewers")
QIAN_KEYS = {
    "coupon": "total_pay_order_coupon_amount_realtime_for_roi2",
    "subsidy": "total_ecom_platform_subsidy_amount_realtime_for_roi2",
    "spend": "stat_cost_for_roi2",
    "exposure": "live_show_count_for_roi2",
}


def room_baseline(baseline: dict[str, Any] | None, room_id: str, account_id: str) -> dict[str, Any]:
    baseline = baseline or {}
    candidate = (baseline.get("rooms") or {}).get(room_id)
    if candidate is None and str(baseline.get("live_room_id") or "") == room_id:
        candidate = baseline
    if not isinstance(candidate, dict):
        return {}
    if candidate.get("qianchuan_account_id") and candidate["qianchuan_account_id"] != account_id:
        return {}
    return candidate


def _anchor_payment(responses: list[dict[str, Any]], left: datetime, right: datetime) -> float | None:
    shifts: dict[str, tuple[datetime, datetime]] = {}
    for item in responses:
        if item.get("path_full", "").endswith("/anchor_shift_list"):
            for row in _compass_data(item, "info_list").get("info_list", []):
                start, end = local_datetime(row.get("live_start_ts")), local_datetime(row.get("live_end_ts"))
                if start and end:
                    shifts[str(row.get("id"))] = (start, end)
    # Lidar's anchor_id is the shift RECORD id, not the stable anchor configuration id.
    values: dict[str, float] = {}
    for item in responses:
        if item.get("path_full", "").endswith("/anchor_lidar_detail"):
            for row in _compass_data(item, "info_list").get("info_list", []):
                key = str(row.get("anchor_id"))
                window = shifts.get(key)
                if window and window[0] == left.replace(second=0, microsecond=0) and window[1] == right.replace(second=0, microsecond=0):
                    value = _money_yuan(_unwrap_value(row.get("pay_amt")), cents=True)
                    if value is not None:
                        values[key] = value
    return sum(values.values()) if values else None


def segment_source_values(
    *, responses: list[dict[str, Any]], room: dict[str, Any], schedule_start: datetime,
    schedule_end: datetime, captured_at: datetime, baseline: dict[str, Any] | None,
    account_id: str,
) -> dict[str, Any]:
    responses = [row for row in responses if _valid_response(row)]
    left, right, full_room = segment_window(room, schedule_start, schedule_end, captured_at)
    trend_left = covered_minute_start(left, room)
    trend = trend_metrics(responses, trend_left, right)
    notes: list[str] = []
    problems: list[str] = []
    if trend_left > left or (right.second and right != local_datetime(room.get("live_end")) and right != captured_at):
        problems.append("排班边界位于分钟桶内，分钟接口不能精确分割。")
    room_id = room["live_room_id"]
    qian = qianchuan_snapshot(responses)
    base = room_baseline(baseline, room_id, account_id)
    start_zero = left == local_datetime(room.get("live_start"))
    baseline_missing: list[str] = []
    baseline_conflicts: list[str] = []
    start_snapshot = {} if start_zero else base.get("qianchuan", {})
    if not start_zero and base:
        stamp = local_datetime(base.get("captured_at"))
        if stamp is None or abs((stamp - left).total_seconds()) > 1:
            problems.append("千川起始快照与班次边界有时间偏差，差分为近似值。")
    room_end = local_datetime(room.get("live_end"))
    ended_after_window = bool(room_end and room_end > right)
    if qian and (ended_after_window or (captured_at > right and room.get("is_living"))):
        problems.append("千川结束快照晚于班次结束，差分可能包含结束后的消耗。")

    def delta(name: str) -> float | None:
        key = QIAN_KEYS[name]
        current = qian.get(key)
        prior = 0 if start_zero else start_snapshot.get(key)
        if current is None or prior is None:
            baseline_missing.append(key)
            return None
        if ended_after_window:
            baseline_missing.append(key)
            return None
        result = float(current) - float(prior)
        if result < -0.01:
            baseline_conflicts.append(key)
            return None
        return max(0.0, result)

    pro = _latest_screen_data(responses, "/live_screen/core_data", "core_data")
    basic = _latest_screen_data(responses, "/basic_live_screen/base_info", "online_user_ucnt")
    coupon, subsidy = delta("coupon"), delta("subsidy")
    payment_minutes = set()
    for item in responses:
        if item.get("path_full", "").endswith("/trend_analysis_v2"):
            for points in _compass_data(item, "trend_map").get("trend_map", {}).values():
                for point in points:
                    stamp = local_datetime(point.get("date_time"))
                    if point.get("point_name") == "pay_amt" and stamp and trend_left <= stamp < right and _money_yuan(point.get("vertical")) is not None:
                        payment_minutes.add(stamp.replace(second=0, microsecond=0))
    expected_last = min(right, captured_at).replace(second=0, microsecond=0)
    if room.get("live_end") and right == local_datetime(room["live_end"]) and right.second:
        expected_last += timedelta(minutes=1)
    expected = set()
    minute = trend_left
    while minute < expected_last:
        expected.add(minute)
        minute += timedelta(minutes=1)
    gmv = trend.get("gmv_trend") if expected <= payment_minutes else None
    if gmv is None:
        gmv = _anchor_payment(responses, left, right)
        if gmv is None:
            problems.append("用户支付金额分钟数据未完整覆盖片段，金额留空。")
    if not full_room:
        notes.append("分钟成交/点击/进入人数存在重复计数风险，去重人数及相关比率留空。")
    if not start_zero and not base:
        problems.append("缺少本场次的班次起始支付基线，千川差分留空。")
    watch_count = trend.get("watch_count")
    result = {
        "mapping_version": SEGMENT_VERSION,
        "gmv": gmv,
        "qianchuan_coupon": coupon + subsidy if coupon is not None and subsidy is not None else None,
        "spend": delta("spend"), "exposure": delta("exposure"),
        "paid_users": _screen_metric(pro, "pay_ucnt") if full_room else None,
        "product_click_users": _funnel_click_users(responses) if full_room else None,
        "viewers": _money_yuan(_unwrap_value(basic.get("online_user_ucnt"))) if full_room else None,
        "new_followers": trend.get("new_followers"),
        "gpm": qian.get("total_live_pay_order_gpm_realtime_for_roi2") if full_room else None,
        "interaction_rate": _screen_metric(pro, "interact_watch_ucnt_ratio") if full_room else None,
        "stay_seconds": _money_yuan(_unwrap_value(basic.get("avg_watch_duration"))) if full_room else None,
        "daily_exposure": None,
        "watch_count": watch_count,
        "occurrence_counts": {key: trend.get(key) for key in COUNT_KEYS},
        "live_room_id": room_id, "qianchuan_account_id": account_id,
        "segment_start": left.isoformat(), "segment_end": right.isoformat(),
        "actual_duration_seconds": (right - left).total_seconds(), "full_room": full_room,
        "baseline": {"source": "room_start_zero"} if start_zero else base,
        "baseline_missing": baseline_missing, "baseline_conflicts": baseline_conflicts,
        "quality_notes": notes, "quality_issues": problems,
        "count_scope": "room_unique" if full_room else "unavailable_interval_unique",
        "end_snapshot_valid": not ended_after_window,
        "source_paths": {"gmv": "trend_analysis_v2.pay_amt（用户支付金额，分转元）"},
    }
    if full_room and result["new_followers"] is None:
        result["new_followers"] = _money_yuan(_unwrap_value(basic.get("incr_fans_cnt")))
    return result


def aggregate_segment_sources(segments: list[dict[str, Any]]) -> dict[str, Any]:
    if not segments:
        raise RuntimeError("班次没有可用的直播场次片段。")
    sources = [item["source"] for item in segments]
    ids = [item["room"]["live_room_id"] for item in segments]
    if len(ids) != len(set(ids)):
        raise RuntimeError("同一场次片段重复，拒绝重复累加。")
    ordered = sorted(sources, key=lambda item: item["segment_start"])
    for prior, following in zip(ordered, ordered[1:]):
        if prior["segment_end"] > following["segment_start"]:
            raise RuntimeError("同一直播账号的场次时间重叠，无法安全归属班次。")
    result = dict(sources[0])
    for key in ("gmv", "qianchuan_coupon", "spend", "exposure", "new_followers", "watch_count"):
        values = [source.get(key) for source in sources]
        result[key] = sum(values) if all(value is not None for value in values) else None
    result["live_room_ids"] = ids
    result["segment_count"] = len(segments)
    result["segment_start"] = min(source["segment_start"] for source in sources)
    result["segment_end"] = max(source["segment_end"] for source in sources)
    result["occurrence_counts"] = {
        key: sum(source["occurrence_counts"][key] for source in sources)
        if all(source["occurrence_counts"].get(key) is not None for source in sources) else None
        for key in COUNT_KEYS
    }
    result["actual_duration_seconds"] = sum(source["actual_duration_seconds"] for source in sources)
    result["quality_notes"] = list(dict.fromkeys(note for source in sources for note in source["quality_notes"]))
    result["quality_issues"] = list(dict.fromkeys(note for source in sources for note in source["quality_issues"]))
    result["baseline_missing"] = list(dict.fromkeys(key for source in sources for key in source["baseline_missing"]))
    result["baseline_conflicts"] = list(dict.fromkeys(key for source in sources for key in source["baseline_conflicts"]))
    result["daily_exposure"] = None  # Filled from final per-room snapshots after the last daily shift.
    result["product_click_conversion"] = None
    if len(sources) > 1:
        for key in (*COUNT_KEYS, "gpm", "interaction_rate", "stay_seconds"):
            result[key] = None
        result["count_scope"] = "unavailable_cross_room_unique"
        result["quality_notes"].append("本班次跨场次，平台未提供联合去重人数和对应比率；相关字段留空。")
    return result


def recompute_segments(segments: list[dict[str, Any]], baseline: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    for item in segments:
        item["source"] = segment_source_values(
            responses=item["responses"], room=item["room"], schedule_start=start,
            schedule_end=end, captured_at=local_datetime(item["captured_at"]),
            baseline=baseline, account_id=item["qianchuan_account_id"],
        )
    return aggregate_segment_sources(segments)
