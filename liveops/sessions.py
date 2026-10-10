"""Platform sessions are source partitions; company shifts remain the report key."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from liveops.portal import APP_TIMEZONE, time_from_epoch


def valid_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and all(
        payload.get(key) in (None, 0, "0") for key in ("code", "st", "status_code")
    )


def local_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=APP_TIMEZONE) if value.tzinfo is None else value.astimezone(APP_TIMEZONE)
    if isinstance(value, str) and not value.isdigit():
        try:
            result = datetime.fromisoformat(value.replace("/", "-"))
            return local_datetime(result)
        except ValueError:
            return None
    return time_from_epoch(value)


def room_record(row: dict[str, Any], *, realtime: bool = False) -> dict[str, Any]:
    author = row.get("author") or {}
    start = local_datetime(row.get("live_start_ts") or row.get("live_room_start_time"))
    living = realtime or row.get("live_status") in (2, "2")
    end = None if living else local_datetime(row.get("live_end_ts"))
    return {
        "live_room_id": str(row.get("live_room_id") or ""),
        "author_id": str(author.get("author_id") or ""),
        "author_name": str(author.get("author_nick_name") or author.get("nick_name") or ""),
        "live_start": start.isoformat() if start else None,
        "live_end": end.isoformat() if end else None,
        "is_living": living,
        "live_status": row.get("live_status", 2 if realtime else None),
    }


def history_rooms(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    if not valid_payload(payload):
        raise RuntimeError("罗盘历史场次接口返回业务错误，不能确认班次覆盖范围。")
    module = (payload.get("data") or {}).get("module_data") or {}
    table = (module.get("shop_live_list_room_detail") or {}).get("compass_general_table_value") or {}
    if not isinstance(table.get("data"), list) or "total" not in (table.get("page_result") or {}):
        raise RuntimeError("历史场次列表结构不完整，不能将未知状态当成空列表。")
    rows = table.get("data") or []
    result = []
    for row in rows:
        cells = row.get("cell_info") or {}
        room = (cells.get("room") or {}).get("room") or {}
        record = room_record(room)
        record["is_self"] = (cells.get("author_type") or {}).get("value", {}).get("value") == 0
        if record["live_room_id"].isdigit():
            result.append(record)
    return result, int((table.get("page_result") or {}).get("total") or 0)


def realtime_rooms(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not valid_payload(payload):
        raise RuntimeError("罗盘实时接口返回业务错误，当前直播状态未知。")
    if not isinstance((payload.get("data") or {}).get("card_list"), list):
        raise RuntimeError("实时场次列表结构不完整，当前直播状态未知。")
    return [room_record(row, realtime=True) for row in (payload.get("data") or {}).get("card_list") or []
            if str(row.get("live_room_id") or "").isdigit()]


def select_author(active: list[dict[str, Any]], history: list[dict[str, Any]], author_id: str = "") -> str:
    if author_id:
        return author_id
    candidates = {row["author_id"] for row in active if row.get("author_id")}
    if not candidates:
        candidates = {row["author_id"] for row in history if row.get("is_self") and row.get("author_id")}
    if len(candidates) != 1:
        raise RuntimeError("无法唯一确认直播账号，请在系统配置填写直播账号 ID（author_id）。")
    return candidates.pop()


def overlapping_rooms(rooms: list[dict[str, Any]], start: datetime, end: datetime, author_id: str) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for room in rooms:
        room_start = local_datetime(room.get("live_start"))
        room_end = local_datetime(room.get("live_end"))
        if room.get("author_id") != author_id or room_start is None:
            continue
        if room_start < end and (room_end is None or room_end > start):
            unique[room["live_room_id"]] = room
    return sorted(unique.values(), key=lambda row: row["live_start"])


def confirmed_room(responses: list[dict[str, Any]], discovered: dict[str, Any]) -> dict[str, Any]:
    """Use the detail API's seconds, rather than the list's rounded/different start."""
    result = dict(discovered)
    confirmed = False
    for item in responses:
        if not valid_payload({"status_code": item.get("status_code")}):
            continue
        if not item.get("path_full", "").endswith("/liveroom_base_info"):
            continue
        data = item.get("data") or {}
        data = data.get("data", data)
        base = data.get("live_room_base_info") or {}
        author = data.get("author_base_info") or {}
        if author.get("author_id") and result.get("author_id") and str(author["author_id"]) != result["author_id"]:
            raise RuntimeError("直播详情接口账号与场次发现结果不匹配。")
        start = local_datetime((base.get("start_time") or {}).get("value"))
        end = local_datetime((base.get("end_time") or {}).get("value"))
        if start:
            confirmed = True
            result["author_id"] = str(author.get("author_id") or result.get("author_id") or "")
            result.update(live_start=start.isoformat(), is_living=bool(base.get("is_living")))
            result["live_end"] = end.isoformat() if end and not result["is_living"] else None
    if not confirmed:
        raise RuntimeError("未获得有效的场次基础信息，不能确认准确起止时间。")
    return result


def segment_window(room: dict[str, Any], start: datetime, end: datetime, captured: datetime) -> tuple[datetime, datetime, bool]:
    room_start = local_datetime(room.get("live_start"))
    room_end = local_datetime(room.get("live_end"))
    if room_start is None:
        raise RuntimeError("直播场次缺少有效开播时间。")
    last = room_end or captured
    left, right = max(start, room_start), min(end, last, captured)
    if right <= left:
        raise RuntimeError("直播场次与本班次没有可采集的时间交集。")
    full_room = start <= room_start and last <= end
    return left, right, full_room


def covered_minute_start(left: datetime, room: dict[str, Any]) -> datetime:
    # A 14:01:31 room's first bucket is labelled 14:01. It contains only this room.
    room_start = local_datetime(room.get("live_start"))
    if room_start and left == room_start:
        return left.replace(second=0, microsecond=0)
    if left.second or left.microsecond:
        return left.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return left
