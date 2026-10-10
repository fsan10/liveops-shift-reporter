"""Operational shift handoffs at actual capture times, scoped to a platform room.

Counts are changes in the platform's cumulative counters, not interval unique
users. Rate/average cards retain their portal snapshot scope; never subtract them.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from liveops.portal import (
    _funnel_click_users,
    _latest_screen_data,
    _money_yuan,
    _screen_metric,
    _unwrap_value,
    _valid_response,
    qianchuan_snapshot,
)
from liveops.sessions import local_datetime

SNAPSHOT_VERSION = "2026-10-09-manual-handoffs-v1"
ADDITIVE_KEYS = ("gmv", "paid_users", "product_click_users", "viewers", "new_followers")
QIAN_KEYS = {
    "coupon": "total_pay_order_coupon_amount_realtime_for_roi2",
    "subsidy": "total_ecom_platform_subsidy_amount_realtime_for_roi2",
    "spend": "stat_cost_for_roi2",
    "exposure": "live_show_count_for_roi2",
}


def capture_snapshot(responses: list[dict[str, Any]], room_id: str, account_id: str,
                     captured_at: str) -> dict[str, Any]:
    valid = [r for r in responses if _valid_response(r)]
    for row in valid:
        if row.get("live_room_id") and row["live_room_id"] != room_id:
            raise RuntimeError("快照响应场次不匹配。")
        if row.get("qianchuan_account_id") and row["qianchuan_account_id"] != account_id:
            raise RuntimeError("快照响应广告账户不匹配。")
    pro = _latest_screen_data(valid, "/live_screen/core_data", "core_data")
    basic = _latest_screen_data(valid, "/basic_live_screen/base_info", "online_user_ucnt")
    # pay_amt is user payment, in fen. pay_amt_all includes other amounts.
    gmv = _screen_metric(pro, "pay_amt")
    return {
        "live_room_id": room_id, "qianchuan_account_id": account_id,
        "captured_at": captured_at, "qianchuan": qianchuan_snapshot(valid),
        "counters": {
            "gmv": gmv / 100 if gmv is not None else None,
            "paid_users": _screen_metric(pro, "pay_ucnt"),
            "product_click_users": _funnel_click_users(valid),
            "viewers": _money_yuan(_unwrap_value(basic.get("online_user_ucnt"))),
            "new_followers": _money_yuan(_unwrap_value(basic.get("incr_fans_cnt"))),
        },
        "cards": {
            "gpm": qianchuan_snapshot(valid).get("total_live_pay_order_gpm_realtime_for_roi2"),
            "interaction_rate": _screen_metric(pro, "interact_watch_ucnt_ratio"),
            "stay_seconds": _money_yuan(_unwrap_value(basic.get("avg_watch_duration"))),
        },
    }


def aggregate_daily_room_exposure(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum the latest full-room exposure snapshot once for every room seen that day."""
    latest: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    unorderable_rooms: set[str] = set()

    for segment in segments:
        source = segment.get("source") or {}
        snapshot = segment.get("snapshot") or source.get("snapshot") or {}
        room = segment.get("room") or {}
        room_id = str(
            room.get("live_room_id") or source.get("live_room_id") or snapshot.get("live_room_id") or ""
        )
        if not room_id:
            issues.append("存在无法识别直播间 ID 的片段，日曝光无法完整汇总。")
            continue
        snapshot_room_id = str(snapshot.get("live_room_id") or "")
        if snapshot_room_id and snapshot_room_id != room_id:
            issues.append(f"直播间 {room_id} 的快照实际属于 {snapshot_room_id}，日曝光无法安全汇总。")
            continue

        qian = snapshot.get("qianchuan") or {}
        value = qian.get(QIAN_KEYS["exposure"])
        try:
            exposure = float(value) if value is not None else None
        except (TypeError, ValueError):
            exposure = None
        if exposure is not None and (not math.isfinite(exposure) or exposure < 0):
            exposure = None

        account_ids = {
            str(value) for value in (
                snapshot.get("qianchuan_account_id"),
                segment.get("qianchuan_account_id"),
                source.get("qianchuan_account_id"),
            ) if value
        }
        if len(account_ids) > 1:
            issues.append(f"直播间 {room_id} 的快照广告账户不一致，日曝光无法安全汇总。")
            continue
        account_id = next(iter(account_ids), "")
        captured_value = (
            snapshot.get("captured_at")
            or segment.get("captured_at")
            or source.get("captured_at")
        )
        captured_at = local_datetime(captured_value)
        if captured_at is None:
            unorderable_rooms.add(room_id)
            issues.append(f"直播间 {room_id} 缺少快照时间，无法确认日曝光是否为最终值。")
            continue

        candidate = {
            "live_room_id": room_id,
            "exposure": exposure,
            "qianchuan_account_id": account_id,
            "captured_at": captured_at.isoformat(),
            "_captured": captured_at,
        }
        previous = latest.get(room_id)
        if previous is None or captured_at > previous["_captured"]:
            latest[room_id] = candidate
        elif captured_at == previous["_captured"] and (
            exposure != previous["exposure"] or account_id != previous["qianchuan_account_id"]
        ):
            issues.append(f"直播间 {room_id} 的同一时刻快照数值冲突，日曝光无法安全汇总。")

    rooms = [
        {key: value for key, value in item.items() if key != "_captured"}
        for item in sorted(latest.values(), key=lambda row: row["live_room_id"])
    ]
    accounts = {item["qianchuan_account_id"] for item in rooms if item["qianchuan_account_id"]}
    if len(accounts) > 1:
        issues.append("当天直播间使用了多个千川账户，日曝光不能跨账户相加。")
    if any(not item["qianchuan_account_id"] for item in rooms):
        issues.append("部分直播间缺少千川账户标识，日曝光无法确认同一数据口径。")
    if not rooms:
        issues.append("当天没有可用的直播间曝光快照。")
    if any(item["exposure"] is None for item in rooms):
        issues.append("至少一个直播间缺少有效的整体曝光快照，日曝光不做不完整求和。")
    if unorderable_rooms:
        issues.append("至少一个直播间快照时间不可用，日曝光不做不完整求和。")

    value = (
        sum(item["exposure"] for item in rooms)
        if rooms and not issues
        else None
    )
    return {
        "version": "room-final-exposure-v1",
        "value": value,
        "rooms": rooms,
        "issues": list(dict.fromkeys(issues)),
        "aggregation": "每个不同直播间 ID 取当天最新整体曝光后求和",
    }


def handoff_source(*, snapshot: dict[str, Any], room: dict[str, Any],
                   baseline: dict[str, Any] | None, start: datetime, end: datetime,
                   allow_new_room: bool = False) -> dict[str, Any]:
    baseline = baseline or {}
    room_id = snapshot["live_room_id"]
    base_room = str(baseline.get("live_room_id") or "")
    prior_end = local_datetime(baseline.get("live_end"))
    new_start = local_datetime(room["live_start"])
    boundary_restart = bool(prior_end and prior_end <= start and new_start >= start)
    zero = baseline.get("source") == "first_shift_zero" or allow_new_room or boundary_restart
    if not zero and base_room and base_room != room_id:
        raise RuntimeError("直播场次已变化，请在对应排班点击“标记中断”保存旧场片段后再采集。")
    if base_room == room_id and baseline.get("qianchuan_account_id") not in (None, "", snapshot["qianchuan_account_id"]):
        raise RuntimeError("广告账户与交接基线不一致，不能跨账户相减。")
    if base_room == room_id:
        zero = baseline.get("source") == "first_shift_zero"
    missing: list[str] = []
    conflicts: list[str] = []

    def delta(current: Any, prior: Any, key: str) -> float | None:
        if current is None or prior is None:
            missing.append(key)
            return None
        difference = float(current) - float(prior)
        if difference < -0.01:
            conflicts.append(key)
            return None
        return max(0.0, difference)

    source = {key: delta(snapshot["counters"].get(key),
                         0 if zero else (baseline.get("counters") or {}).get(key), key)
              for key in ADDITIVE_KEYS}
    qian = {key: delta(snapshot["qianchuan"].get(field),
                       0 if zero else (baseline.get("qianchuan") or {}).get(field), field)
            for key, field in QIAN_KEYS.items()}
    source.update(snapshot["cards"])
    source.update({
        "mapping_version": SNAPSHOT_VERSION,
        "qianchuan_coupon": qian["coupon"] + qian["subsidy"]
        if qian["coupon"] is not None and qian["subsidy"] is not None else None,
        "spend": qian["spend"], "exposure": qian["exposure"],
        "daily_exposure": None, "product_click_conversion": None,
        "live_room_id": room_id, "qianchuan_account_id": snapshot["qianchuan_account_id"],
        "baseline": {"source": "room_start_zero"} if zero else baseline,
        "baseline_missing": missing, "baseline_conflicts": conflicts,
        "segment_start": max(start, local_datetime(room["live_start"])).isoformat(),
        "segment_end": end.isoformat(), "captured_at": snapshot["captured_at"],
        "end_snapshot_valid": True, "count_scope": "cumulative_counter_increment",
        "card_scope": "room_end_snapshot",
        "quality_notes": ["人数按同场累计计数器净增量统计；GPM、互动率、停留采用该场结束采集时的页面卡片。"],
        "quality_issues": [],
        "snapshot": snapshot,
    })
    left = local_datetime(source["segment_start"])
    right = min(end, local_datetime(room.get("live_end")) or end)
    source["actual_duration_seconds"] = max(0, (right - left).total_seconds())
    return source


def combine_handoffs(segments: list[dict[str, Any]]) -> dict[str, Any]:
    if not segments:
        raise RuntimeError("没有可用的交接快照。")
    room_ids = [s["room"]["live_room_id"] for s in segments]
    if len(set(room_ids)) != len(room_ids):
        raise RuntimeError("同场中断片段重复，拒绝重复累加。")
    sources = [s["source"] for s in segments]
    result = dict(sources[-1])
    for key in (*ADDITIVE_KEYS, "qianchuan_coupon", "spend", "exposure"):
        values = [s.get(key) for s in sources]
        result[key] = sum(values) if all(v is not None for v in values) else None
    result["baseline_missing"] = list(dict.fromkeys(k for s in sources for k in s["baseline_missing"]))
    result["baseline_conflicts"] = list(dict.fromkeys(k for s in sources for k in s["baseline_conflicts"]))
    result["quality_notes"] = list(dict.fromkeys(k for s in sources for k in s["quality_notes"]))
    result["quality_issues"] = list(dict.fromkeys(k for s in sources for k in s["quality_issues"]))
    result.update(live_room_ids=room_ids, segment_count=len(sources),
                  segment_start=sources[0]["segment_start"],
                  actual_duration_seconds=sum(s["actual_duration_seconds"] for s in sources))
    if len(sources) > 1:
        result["quality_notes"].append("已合并手动标记的各场累计增量，跨场人数不做联合去重；GPM、互动率、停留显示最后一场结束快照，各场原值见片段。")
    return result


def recompute_handoffs(segments: list[dict[str, Any]], baseline: dict[str, Any],
                       start: datetime) -> dict[str, Any]:
    for index, segment in enumerate(segments):
        segment["source"] = handoff_source(
            snapshot=segment["snapshot"], room=segment["room"], baseline=baseline,
            start=start, end=local_datetime(segment["source"]["segment_end"]),
            allow_new_room=index > 0,
        )
        start = local_datetime(segment["source"]["segment_end"])
    return combine_handoffs(segments)
