from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from liveops.portal import APP_TIMEZONE, QIANCHUAN_OVERVIEW
from liveops.segments import aggregate_segment_sources, segment_source_values
from liveops.sessions import (
    confirmed_room,
    covered_minute_start,
    history_rooms,
    overlapping_rooms,
    realtime_rooms,
    room_record,
    select_author,
)


def at(hour, minute=0, second=0, day=8):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=APP_TIMEZONE)


def room(room_id="101", start=None, end=None):
    return {"live_room_id": room_id, "author_id": "shop-author", "live_start": (start or at(7)).isoformat(),
            "live_end": end.isoformat() if end else None, "is_living": end is None}


def responses(room_id, start, end, pay=100, cost=250):
    points = []
    stamp = start.replace(second=0, microsecond=0)
    while stamp < end:
        for key, value in [("pay_amt", pay), ("watch_ucnt", 5), ("pay_ucnt", 1), ("incr_fans_cnt", 0)]:
            points.append({"point_name": key, "date_time": stamp.timestamp(), "vertical": value})
        stamp += timedelta(minutes=1)
    return [{"source": "compass", "path_full": "/business_api/shop/live_room/flow/trend_analysis_v2",
             "live_room_id": room_id, "data": {"trend_map": {"number": points}}},
            {"source": "qianchuan", "data_set_key": QIANCHUAN_OVERVIEW, "metric_names": ["stat_cost_for_roi2"],
             "live_room_id": room_id, "totals": {"stat_cost_for_roi2": {"Value": cost},
                "total_pay_order_coupon_amount_realtime_for_roi2": {"Value": 20},
                "total_ecom_platform_subsidy_amount_realtime_for_roi2": {"Value": 1},
                "live_show_count_for_roi2": {"Value": 500}}}]


def source(data, selected_room, *, baseline=None, start=None, end=None, captured=None):
    return segment_source_values(responses=data, room=selected_room, schedule_start=start or at(11, 30),
                                 schedule_end=end or at(16), captured_at=captured or at(16),
                                 baseline=baseline, account_id="ad-account")


def test_living_room_end_ts_is_not_an_end_marker():
    item = room_record({"live_room_id": "102", "live_start_ts": at(14).timestamp(),
                        "live_end_ts": at(15).timestamp(), "live_status": 2})
    assert item["is_living"]
    assert item["live_end"] is None


def test_realtime_error_does_not_mean_offline_or_return_finished_first_row():
    with pytest.raises(RuntimeError, match="状态未知"):
        realtime_rooms({"code": 11001, "data": None})
    assert realtime_rooms({"st": 0, "data": {"card_list": []}}) == []
    with pytest.raises(RuntimeError, match="状态未知"):
        realtime_rooms({"st": 0, "data": {}})
    with pytest.raises(RuntimeError, match="结构不完整"):
        history_rooms({"st": 0, "data": {}})


def test_account_selection_requires_identity_when_multiple_accounts_are_live():
    active = [{"author_id": "other"}, {"author_id": "wanted"}]
    assert select_author(active, [], "wanted") == "wanted"
    with pytest.raises(RuntimeError, match="账号 ID"):
        select_author(active, [])


def test_interval_discovery_ignores_other_account_and_future_and_deduplicates():
    old, new = room("101", at(7), at(13, 47, 28)), room("102", at(14, 1, 31))
    other = {**old, "live_room_id": "103", "author_id": "other"}
    future = room("104", at(20))
    result = overlapping_rooms([old, new, other, future, old], at(11, 30), at(16), "shop-author")
    assert [r["live_room_id"] for r in result] == ["101", "102"]


def test_cross_midnight_room_intersects_company_business_date():
    yesterday = room("101", at(20, day=8), at(0, 30, day=9))
    tomorrow = room("102", at(0, 45, day=9), at(2, day=9))
    assert len(overlapping_rooms([yesterday, tomorrow], at(20, 30), at(1, day=9), "shop-author")) == 2


def test_detail_confirmation_rejects_different_account_and_corrects_seconds():
    response = {"path_full": "/compass_api/shop/live/live_room/liveroom_base_info",
                "data": {"author_base_info": {"author_id": "shop-author"}, "live_room_base_info": {
                    "start_time": {"value": at(14, 1, 31).timestamp()}, "is_living": True,
                    "end_time": {"value": at(15).timestamp()}}}}
    result = confirmed_room([response], room("102", at(14)))
    assert result["live_start"] == at(14, 1, 31).isoformat()
    assert result["live_end"] is None
    response["data"]["author_base_info"]["author_id"] = "other"
    with pytest.raises(RuntimeError, match="不匹配"):
        confirmed_room([response], room())
    with pytest.raises(RuntimeError, match="基础信息"):
        confirmed_room([], room())


def test_new_room_first_partial_minute_is_included():
    new = room("102", at(14, 1, 31))
    result = source(responses("102", at(14, 1), at(14, 3)), new, captured=at(14, 3))
    assert result["gmv"] == 2
    assert result["spend"] == 250
    assert result["baseline"]["source"] == "room_start_zero"
    assert covered_minute_start(at(14, 1, 31), new) == at(14, 1)


def test_partial_shift_does_not_use_whole_room_card_or_minute_uv_as_unique_users():
    old = room("101", at(7), at(13, 47, 28))
    data = responses("101", at(11, 30), at(13, 47, 28))
    data.append({"source": "compass", "path_full": "/compass_api/shop/live/live_screen/core_data",
                 "data": {"pay_amt_all": {"value": 2400000}, "core_data": [
                     {"index_name": "pay_ucnt", "value": {"value": 123}}]}})
    result = source(data, old)
    assert result["gmv"] == 138
    assert result["paid_users"] is None
    assert result["viewers"] is None
    assert result["occurrence_counts"]["paid_users"] == 138


@pytest.mark.parametrize("baseline", [
    {"live_room_id": "999", "qianchuan": {"stat_cost_for_roi2": 100}},
    {"rooms": {"101": {"qianchuan_account_id": "wrong", "qianchuan": {"stat_cost_for_roi2": 100}}}},
])
def test_even_positive_cross_room_or_advertiser_delta_is_rejected(baseline):
    result = source(responses("101", at(11, 30), at(13, 47)), room("101", at(7), at(13, 47)), baseline=baseline)
    assert result["spend"] is None
    assert "stat_cost_for_roi2" in result["baseline_missing"]


def test_same_room_payment_baseline_and_negative_conflict():
    baseline = {"live_room_id": "101", "captured_at": at(11, 30).isoformat(),
                "qianchuan": {"stat_cost_for_roi2": 200}}
    result = source(responses("101", at(11, 30), at(13, 47)), room("101", at(7), at(13, 47)), baseline=baseline)
    assert result["spend"] == 50
    baseline["qianchuan"]["stat_cost_for_roi2"] = 300
    result = source(responses("101", at(11, 30), at(13, 47)), room("101", at(7), at(13, 47)), baseline=baseline)
    assert result["spend"] is None
    assert result["baseline_conflicts"] == ["stat_cost_for_roi2"]


def test_multiple_rooms_aggregate_money_but_missing_part_is_not_zero():
    old = room("101", at(7), at(13, 47, 28))
    new = room("102", at(14, 1, 31))
    a = source(responses("101", at(11, 30), at(13, 47, 28)), old)
    b = source(responses("102", at(14, 1), at(16)), new)
    result = aggregate_segment_sources([{"room": old, "source": a}, {"room": new, "source": b}])
    assert result["gmv"] == 138 + 119
    assert result["actual_duration_seconds"] == 15357
    assert result["spend"] is None  # Old part has no baseline; new part is 250.
    assert result["paid_users"] is None
    assert result["daily_exposure"] is None
    assert result["count_scope"] == "unavailable_cross_room_unique"
    with pytest.raises(RuntimeError, match="重复"):
        aggregate_segment_sources([{"room": old, "source": a}, {"room": old, "source": a}])
    b["segment_start"] = a["segment_start"]
    with pytest.raises(RuntimeError, match="重叠"):
        aggregate_segment_sources([{"room": old, "source": a}, {"room": new, "source": b}])


def test_baseline_handoff_uses_last_segment_not_flat_responses_from_other_room():
    from liveops.service import _baseline_from_report

    a, b = room("101", at(7), at(13)), room("102", at(14))
    segments = [{"room": r, "qianchuan_account_id": "ad-account", "captured_at": at(16).isoformat(),
                 "responses": responses(r["live_room_id"], at(14), at(16), cost=cost),
                 "source": {"segment_end": end.isoformat()}}
                for r, cost, end in [(a, 9000, at(13)), (b, 1000, at(16))]]
    report = SimpleNamespace(raw_json={"segments": segments, "is_final": True, "author_id": "shop-author"},
                             latest_run_id="run", live_room_id="102")
    value = _baseline_from_report(report)
    assert value["live_room_id"] == "102"
    assert value["qianchuan"]["stat_cost_for_roi2"] == 1000
    assert value["rooms"]["101"]["qianchuan"]["stat_cost_for_roi2"] == 9000
    report.raw_json["is_final"] = False
    assert _baseline_from_report(report) is None


def test_history_parser_reads_status_author_and_total():
    payload = {"st": 0, "data": {"module_data": {"shop_live_list_room_detail": {
        "compass_general_table_value": {"page_result": {"total": 55}, "data": [{"cell_info": {
            "author_type": {"value": {"value": 0}}, "room": {"room": {
                "live_room_id": "101", "live_start_ts": at(7).timestamp(), "live_end_ts": at(13).timestamp(),
                "live_status": 4, "author": {"author_id": "shop-author"}}}}}]}}}}}
    rows, total = history_rooms(payload)
    assert total == 55 and rows[0]["is_self"]
    assert rows[0]["is_living"] is False


@pytest.mark.parametrize("missing_value", [None, float("nan"), float("inf")])
def test_incomplete_or_invalid_payment_minutes_do_not_produce_partial_money_total(missing_value):
    selected = room("101", at(7), at(13, 47))
    data = responses("101", at(11, 30), at(13, 47))
    data[0]["data"]["trend_map"]["number"][0]["vertical"] = missing_value
    result = source(data, selected)
    assert result["gmv"] is None
    assert any("未完整覆盖" in issue for issue in result["quality_issues"])


def test_delayed_baseline_is_disclosed_and_does_not_silently_claim_exact_cost():
    baseline = {"live_room_id": "101", "captured_at": at(11, 31).isoformat(),
                "qianchuan": {"stat_cost_for_roi2": 200}}
    result = source(responses("101", at(11, 30), at(13, 47)), room("101", at(7), at(13, 47)), baseline=baseline)
    assert result["spend"] == 50
    assert any("近似值" in issue for issue in result["quality_issues"])


def test_finished_room_after_shift_end_cannot_supply_earlier_shift_cost_or_end_baseline():
    selected = room("101", at(7), at(13, 47))
    result = source(responses("101", at(7), at(13, 47)), selected, start=at(7), end=at(11, 30))
    assert result["gmv"] == 270
    assert result["spend"] is None
    assert result["end_snapshot_valid"] is False


async def test_payment_dropdown_selects_visible_option_instead_of_hidden_duplicate():
    from liveops.collector import _select_qianchuan_payment

    clicked = []

    class Option:
        def __init__(self, index):
            self.index = index

        async def is_visible(self):
            return self.index == 1

        async def click(self, **_kwargs):
            clicked.append(self.index)

    class Locator:
        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            pass

        async def input_value(self):
            return "乘方数据"

        async def click(self, **_kwargs):
            pass

        async def count(self):
            return 2

        def nth(self, index):
            return Option(index)

    class Page:
        def locator(self, _selector):
            return Locator()

        def get_by_text(self, *_args, **_kwargs):
            return Locator()

        async def wait_for_function(self, *_args, **_kwargs):
            pass

        async def wait_for_timeout(self, _timeout):
            pass

    await _select_qianchuan_payment(Page())
    assert clicked == [1]


async def test_scheduler_finalizes_provisional_report_and_skips_final_report(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import liveops.service as module
    from liveops.db import Base
    from liveops.models import CollectionRun, ShiftReport, ShiftSchedule

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as db:
        schedule = ShiftSchedule(work_date=at(7).date(), start_time=at(11, 30).time(),
                                 end_time=at(16).time(), host_name="主播", live_room_id="101")
        db.add(schedule)
        db.flush()
        db.add(CollectionRun(id="early", schedule_id=schedule.id, kind="manual", trigger="user"))
        db.flush()
        report = ShiftReport(schedule_id=schedule.id, latest_run_id="early", work_date=schedule.work_date,
                             start_time=schedule.start_time, end_time=schedule.end_time, host_name="主播",
                             shop_name="测试店", live_room_id="101", raw_json={"is_final": False})
        db.add(report)
        db.commit()
        report_id, schedule_id = report.id, schedule.id
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(module, "now_local", lambda: at(16, 1))
    monkeypatch.setattr(module, "db_settings", lambda *_: {"auto_catchup_minutes": "90", "settle_delay_seconds": "0"})
    dispatcher = module.RunDispatcher(None)
    calls = []

    async def begin(*args):
        calls.append(args)

    monkeypatch.setattr(dispatcher, "begin_run", begin)
    await dispatcher._check_due_schedules()
    assert calls == [(schedule_id, "report", "scheduler")]
    with sessions() as db:
        db.get(ShiftReport, report_id).raw_json = {"is_final": True}
        db.commit()
    dispatcher._last_auto_attempt.clear()
    await dispatcher._check_due_schedules()
    assert len(calls) == 1
    engine.dispose()
