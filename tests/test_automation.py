from __future__ import annotations

import asyncio
import copy
from datetime import datetime, time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import liveops.automation as screenshots
import liveops.hourly as hourly
import liveops.service as service
import liveops.settings as settings_module
from liveops.db import Base
from liveops.models import (
    CollectionRun,
    ScreenshotRequest,
    ShiftHourDetail,
    ShiftHourState,
    ShiftReport,
    ShiftSchedule,
)
from liveops.portal import APP_TIMEZONE
from liveops.settings import DEFAULT_SETTINGS
from liveops.snapshots import QIAN_KEYS, handoff_source


def at(hour=8, minute=0, second=31, day=30):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=APP_TIMEZONE)


async def drain(dispatcher):
    while dispatcher._tasks:
        await asyncio.gather(*list(dispatcher._tasks))


@pytest.fixture
def state(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = {**DEFAULT_SETTINGS, "hourly_screenshot_enabled": "true", "feishu_app_id": "test-app",
        "feishu_app_secret": "test-secret", "feishu_chat_id": "anchor-chat", "feishu_hourly_chat_id": "hourly-chat",
        "live_author_id": "author", "qianchuan_account_id": "account",
        "bitable_table_month": "2026-10", "bitable_table_mode": "manual"}
    clock, counter = [at()], [100]
    captures, calls, sends, alerts, writes, failures = [], [], [], [], [], {}
    room = {"live_room_id": "101", "author_id": "author", "live_start": at(7, 0, 0).isoformat(),
            "live_end": None, "is_living": True}

    class Bot:
        def __init__(self, app, secret, chat):
            self.chat = chat

        async def send_report(self, caption, images):
            sends.append((self.chat, caption, images))
            if self.chat in failures:
                raise failures[self.chat]
            return {"message_id": f"message-{len(sends)}"}

        async def send_alert(self, *args, **kwargs):
            alerts.append(self.chat)
            return {"message_id": "alert"}

        async def close(self):
            pass

    class Bitable:
        records = {}

        def __init__(self, *args):
            pass

        async def ensure_hour_hierarchy(self, parent_field, view_id):
            return {"parent_field": parent_field, "field_id": "field-parent", "view_id": "view-hours"}

        async def ensure_sync_key(self, field_name):
            pass

        async def upsert_record(self, **kwargs):
            writes.append(kwargs)
            key = kwargs["sync_key"]
            self.records.setdefault(key, f"record-{len(self.records)}")
            return {"record_id": kwargs.get("record_id") or self.records[key], "missing_mappings": []}

        async def delete_record(self, record_id):
            pass

        async def close(self):
            pass

    def images():
        return [{"live_room_id": "101", "path": "test.png", "relative_path": "test.png",
                 "name": "测试截图", "captured_at": clock[0].isoformat()}]

    async def collect(**kwargs):
        calls.append(kwargs)
        selected = kwargs["schedule"]
        value = counter[0]
        snapshot = {"live_room_id": "101", "qianchuan_account_id": "account", "captured_at": clock[0].isoformat(),
            "counters": {"gmv": value, "paid_users": value/10, "product_click_users": value/5,
                         "viewers": value, "new_followers": value/100},
            "qianchuan": {field: value*factor for field, factor in zip(QIAN_KEYS.values(), (0.1, 0.02, 0.2, 100))},
            "cards": {"gpm": 2000, "interaction_rate": 0.05, "stay_seconds": 60}}
        source = handoff_source(snapshot=snapshot, room=room, baseline=kwargs["baseline"],
            start=service.schedule_start(selected), end=service.schedule_end(selected))
        shots = images() if kwargs["screenshots_enabled"] else []
        if shots:
            captures.append(kwargs["run_id"])
        if kwargs.get("on_room_confirmed"):
            kwargs["on_room_confirmed"]({"live_room_id": "101"})
        if kwargs.get("on_captured"):
            await kwargs["on_captured"](shots)
        return {"live_room_id": "101", "shop_name": "测试店", "source": source, "screenshots": shots,
            "responses": [], "page_errors": [], "collected_at": clock[0].isoformat(), "is_final": True,
            "schedule_start": service.schedule_start(selected).isoformat(),
            "schedule_end": service.schedule_end(selected).isoformat(), "author_id": "author",
            "segments": [{"room": dict(room), "source": source, "snapshot": snapshot, "responses": [],
                          "qianchuan_account_id": "account", "captured_at": clock[0].isoformat()}]}

    async def capture_only(**kwargs):
        captures.append(kwargs["run_id"])
        return {"room": room, "screenshots": images(), "page_errors": [], "collected_at": clock[0].isoformat()}

    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service, "db_settings", lambda *args: dict(settings))
    monkeypatch.setattr(settings_module, "db_settings", lambda *args: dict(settings))
    monkeypatch.setattr(service, "now_local", lambda: clock[0])
    monkeypatch.setattr(service, "collect_report", collect)
    monkeypatch.setattr(screenshots, "collect_screenshots", capture_only)
    monkeypatch.setattr(screenshots, "FeishuClient", Bot)
    monkeypatch.setattr(service, "FeishuClient", Bot)
    monkeypatch.setattr(hourly, "BitableClient", Bitable)
    monkeypatch.setattr(service, "BitableClient", Bitable)
    yield SimpleNamespace(db=sessions, settings=settings, clock=clock, counter=counter, captures=captures,
        calls=calls, sends=sends, alerts=alerts, failures=failures, writes=writes, room=room)
    engine.dispose()


def add_schedule(state, start=time(7), end=time(11, 30), host="测试主播甲"):
    with state.db() as db:
        row = ShiftSchedule(work_date=state.clock[0].date(), start_time=start, end_time=end,
                            host_name=host, live_room_id="101", qianchuan_account_id="account")
        db.add(row)
        db.commit()
        return row.id


@pytest.mark.parametrize("start,end,expected", [
    (time(7), time(11,30), [(7,0,8,0),(8,0,9,0),(9,0,10,0),(10,0,11,30)]),
    (time(8), time(12), [(8,0,9,0),(9,0,10,0),(10,0,11,0),(11,0,12,0)]),
    (time(7), time(8,30), [(7,0,8,30)]), (time(7), time(7,30), [(7,0,7,30)]),
    (time(7), time(9), [(7,0,8,0),(8,0,9,0)]),
    (time(22,30), time(2), [(22,30,23,30),(23,30,0,30),(0,30,2,0)]),
])
def test_roster_period_algorithm(start, end, expected):
    schedule = SimpleNamespace(work_date=at().date(), start_time=start, end_time=end)
    plan = hourly.shift_periods(schedule)
    assert [(left.hour,left.minute,right.hour,right.minute) for left,right in plan] == expected
    assert all(left < right for left,right in plan)
    assert all(plan[i][1] == plan[i+1][0] for i in range(len(plan)-1))


async def test_hourly_requires_roster_and_ignores_live_start_clock(state):
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert not state.calls and not state.captures
    add_schedule(state)
    state.room["live_start"] = at(6,17,0).isoformat()
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.calls) == 1
    assert state.calls[0]["schedule"].end_time == time(8)


async def collect_shift(state, dispatcher):
    for when, value in [(at(8),100),(at(9),250),(at(10),450),(at(11,30),900)]:
        state.clock[0], state.counter[0] = when, value
        await dispatcher._check_due_schedules()
        await drain(dispatcher)


async def test_two_baseline_chains_and_one_capture_at_shift_end(state):
    schedule_id = add_schedule(state)
    dispatcher = service.RunDispatcher(None)
    await collect_shift(state, dispatcher)
    assert len(state.calls) == len(state.captures) == 4
    assert [chat for chat,_,_ in state.sends].count("hourly-chat") == 4
    assert [chat for chat,_,_ in state.sends].count("anchor-chat") == 1
    with state.db() as db:
        summary = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule_id))
        rows = db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule_id)
                          .order_by(ShiftHourDetail.period_index)).all()
        assert summary.metrics_json["gmv"] == 900
        assert [row.metrics_json["gmv"] for row in rows] == [100,150,200,450]
        assert rows[-1].metrics_json["duration_hours"] == 1.5
        assert rows[-1].metrics_json["hourly_gmv"] == 300
        assert [row.baseline_json.get("counters", {}).get("gmv",0) for row in rows] == [0,100,250,450]
        assert [row.raw_json["end_baseline"]["counters"]["gmv"] for row in rows] == [100,250,450,900]
        assert db.get(ShiftSchedule,schedule_id).baseline_json["source"] == "first_shift_zero"
        assert db.get(ShiftHourState,schedule_id).initial_baseline_json["source"] == "first_shift_zero"
        assert rows[-1].latest_run_id == summary.latest_run_id
        assert summary.raw_json["is_final"]
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.captures) == 4


@pytest.mark.parametrize("data,hour,anchor", [(False,False,False),(False,False,True),
    (False,True,False),(False,True,True),(True,False,False),(True,False,True),(True,True,False),(True,True,True)])
async def test_independent_switches_at_final_boundary(state, data, hour, anchor):
    state.settings.update(data_collection_enabled=str(data).lower(),hourly_screenshot_enabled=str(hour).lower(),
                          anchor_screenshot_enabled=str(anchor).lower())
    state.clock[0] = at(11,30)
    add_schedule(state)
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.calls) == int(data or hour)
    assert len(state.captures) == int(hour or anchor)
    assert sorted(chat for chat,_,_ in state.sends) == sorted((["hourly-chat"] if hour else [])+(["anchor-chat"] if anchor else []))
    assert state.alerts == (["hourly-chat"] if hour else [])
    with state.db() as db:
        summary = db.scalar(select(ShiftReport))
        assert bool(summary and summary.raw_json.get("is_final")) == data
        assert db.scalar(select(func.count(ShiftHourDetail.id)).where(ShiftHourDetail.latest_run_id.is_not(None))) == int(hour)


async def test_hour_details_never_supply_anchor_baseline_and_next_host_hours_are_independent(state):
    first = add_schedule(state)
    second = add_schedule(state, time(11,30),time(14),"测试主播乙")
    dispatcher = service.RunDispatcher(None)
    await collect_shift(state, dispatcher)
    state.clock[0],state.counter[0] = at(12,30),1200
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    with state.db() as db:
        row = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id==second,
            ShiftHourDetail.period_index==1))
        assert row.baseline_json["counters"]["gmv"] == 900
        assert row.metrics_json["gmv"] == 300
        assert db.get(ShiftSchedule,second).baseline_json["counters"]["gmv"] == 900
        assert db.get(ShiftHourState,first).initial_baseline_json["source"] == "first_shift_zero"


async def test_missed_hour_is_missing_and_does_not_become_zero_or_merge_into_the_next_hour(state):
    schedule_id = add_schedule(state)
    dispatcher = service.RunDispatcher(None)
    state.clock[0],state.counter[0] = at(9),250
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    with state.db() as db:
        rows = db.scalars(select(ShiftHourDetail).order_by(ShiftHourDetail.period_index)).all()
        assert rows[0].status == "missed"
        assert rows[1].status == "partial" and rows[1].metrics_json["gmv"] is None
    state.clock[0],state.counter[0] = at(10),450
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    with state.db() as db:
        row = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id==schedule_id,
            ShiftHourDetail.period_index==3))
        assert row.metrics_json["gmv"] == 200


@pytest.mark.parametrize("summary_enabled", [True, False])
async def test_partial_final_hour_keeps_cumulative_baseline_for_next_host_after_restart(state, summary_enabled):
    state.settings["data_collection_enabled"] = str(summary_enabled).lower()
    first = add_schedule(state)
    second = add_schedule(state, time(11, 30), time(14), "测试主播乙")
    state.clock[0], state.counter[0] = at(11, 30), 900
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    with state.db() as db:
        final = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == first,
            ShiftHourDetail.period_index == 4))
        assert final.status == "partial" and final.metrics_json["gmv"] is None
        assert final.baseline_json["source"] == "hourly_previous_missing"
        assert final.raw_json["end_baseline"]["counters"]["gmv"] == 900
        assert final.raw_json["end_baseline"]["source_run_id"] == final.latest_run_id
        summary = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == first))
        assert summary.raw_json.get("is_final") == summary_enabled
        if summary_enabled:
            assert summary.metrics_json["gmv"] == 900
    restarted = service.RunDispatcher(None)
    restarted._recover_screenshot_requests()
    state.clock[0], state.counter[0] = at(12, 30), 1200
    await restarted._check_due_schedules()
    await drain(restarted)
    with state.db() as db:
        first_hour = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == second,
            ShiftHourDetail.period_index == 1))
        assert first_hour.status == "succeeded"
        assert first_hour.baseline_json["counters"]["gmv"] == 900
        assert first_hour.metrics_json["gmv"] == 300
        assert db.get(ShiftHourState, second).initial_baseline_json["chain"] == "shift_hourly"
        if not summary_enabled:
            assert service._current_baseline(db, db.get(ShiftSchedule, second))["source"] == "previous_shift_missing"


async def test_failed_group_delivery_does_not_discard_valid_hour_end_baseline(state):
    schedule_id = add_schedule(state)
    state.failures["hourly-chat"] = RuntimeError("测试发送失败")
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    with state.db() as db:
        first = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule_id,
            ShiftHourDetail.period_index == 1))
        assert db.get(CollectionRun, first.latest_run_id).status == "partial"
        assert first.raw_json["end_baseline"]["counters"]["gmv"] == 100
    state.failures.clear()
    state.clock[0], state.counter[0] = at(9), 250
    restarted = service.RunDispatcher(None)
    restarted._recover_screenshot_requests()
    await restarted._check_due_schedules()
    await drain(restarted)
    with state.db() as db:
        second = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule_id,
            ShiftHourDetail.period_index == 2))
        assert second.baseline_json["counters"]["gmv"] == 100
        assert second.metrics_json["gmv"] == 150


async def test_restart_does_not_recapture_completed_hour(state):
    add_schedule(state)
    first = service.RunDispatcher(None)
    await first._check_due_schedules()
    await drain(first)
    second = service.RunDispatcher(None)
    second._recover_screenshot_requests()
    await second._check_due_schedules()
    await drain(second)
    assert len(state.calls) == len(state.captures) == len(state.sends) == 1


async def test_late_hour_request_reuses_existing_final_capture_and_keeps_summary_metrics(state):
    state.settings["hourly_screenshot_enabled"]="false"
    schedule_id=add_schedule(state)
    state.clock[0],state.counter[0]=at(11,30),900
    dispatcher=service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.calls)==1 and [chat for chat,_,_ in state.sends]==["anchor-chat"]
    state.settings["hourly_screenshot_enabled"]="true"
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.calls)==len(state.captures)==1
    assert [chat for chat,_,_ in state.sends]==["anchor-chat","hourly-chat"]
    with state.db() as db:
        parent=db.scalar(select(ShiftReport).where(ShiftReport.schedule_id==schedule_id))
        assert parent.metrics_json["gmv"]==900 and parent.raw_json["is_final"]


def test_pending_hour_parent_is_neither_an_anchor_handoff_nor_a_daily_exposure_source():
    parent=SimpleNamespace(raw_json={"is_final":False,"measurement_mode":"hourly_parent_pending"})
    assert service._baseline_from_report(parent) is None
    assert service._report_exposure_segments(parent,None)==[]


async def test_each_group_retries_independently_without_resampling(state):
    add_schedule(state)
    state.failures["hourly-chat"] = RuntimeError("permission denied")
    state.clock[0] = at(11,30)
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    with state.db() as db:
        source = db.scalar(select(CollectionRun).where(CollectionRun.kind=="report"))
        source_id = source.id
    state.failures.clear()
    await dispatcher.begin_screenshot_delivery(source_id)
    await drain(dispatcher)
    assert [chat for chat,_,_ in state.sends].count("anchor-chat") == 1
    assert [chat for chat,_,_ in state.sends].count("hourly-chat") == 2
    assert len(state.captures) == len(state.calls) == 1


async def test_uncertain_delivery_is_not_automatically_resent(state):
    add_schedule(state)
    state.failures["hourly-chat"] = httpx.ReadTimeout("response lost")
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    with state.db() as db:
        request = db.scalar(select(ScreenshotRequest).where(ScreenshotRequest.status=="captured"))
        assert request.delivery_json["status"] == "uncertain"
        source = request.run_id
    with pytest.raises(RuntimeError,match="核实"):
        await dispatcher.begin_screenshot_delivery(source)
    assert len(state.sends)==1


async def test_native_bitable_children_keep_one_parent_and_stable_child_keys(state):
    state.settings.update(bitable_app_token="test-base",bitable_table_id="test-table")
    schedule_id = add_schedule(state)
    dispatcher = service.RunDispatcher(None)
    await collect_shift(state,dispatcher)
    with state.db() as db:
        rows = db.scalars(select(ShiftHourDetail).order_by(ShiftHourDetail.period_index)).all()
        parent = db.scalar(select(ShiftReport))
        assert all(row.bitable_status=="synced" for row in rows)
        parent_id = parent.bitable_record_id
        ids = [row.bitable_record_id for row in rows]
        source = parent.latest_run_id
    await dispatcher._sync_hourly_bitable(schedule_id,state.settings,source)
    with state.db() as db:
        assert [row.bitable_record_id for row in db.scalars(select(ShiftHourDetail).order_by(ShiftHourDetail.period_index))]==ids
    child_writes = [row for row in state.writes if "parent_record" in row["values"]]
    assert all(row["values"]["parent_record"]==[parent_id] for row in child_writes)
    assert len({row["sync_key"] for row in child_writes})==4


async def test_hour_recapture_recomputes_following_hour_without_mutating_summary_baseline(state):
    schedule_id = add_schedule(state)
    dispatcher = service.RunDispatcher(None)
    await collect_shift(state,dispatcher)
    with state.db() as db:
        first = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.period_index==1))
        summary = db.scalar(select(ShiftReport))
        schedule = db.get(ShiftSchedule,schedule_id)
        capture = {**copy.deepcopy(first.raw_json),"live_room_id":"101","screenshots":first.screenshots_json,
            "collected_at":first.raw_json["captured_at"]}
        capture["segments"][0]["snapshot"]["counters"]["gmv"]=120
        source = first.latest_run_id
        original_summary = copy.deepcopy(summary.metrics_json)
    dispatcher._save_hourly_capture(schedule,source,capture,first.id)
    with state.db() as db:
        second = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.period_index==2))
        assert second.baseline_json["counters"]["gmv"]==120
        assert second.metrics_json["gmv"]==130
        assert db.scalar(select(ShiftReport)).metrics_json==original_summary


async def test_overnight_hour_rows_keep_parent_business_date_and_last_merged_period(state):
    schedule_id=add_schedule(state,time(22,30),time(2))
    dispatcher=service.RunDispatcher(None)
    for when,value in [(at(23,30),100),(at(0,30,day=31),250),(at(2,day=31),900)]:
        state.clock[0],state.counter[0]=when,value
        await dispatcher._check_due_schedules()
        await drain(dispatcher)
    with state.db() as db:
        rows=db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id==schedule_id)
                        .order_by(ShiftHourDetail.period_index)).all()
        assert [row.metrics_json["gmv"] for row in rows]==[100,150,650]
        assert rows[-1].metrics_json["duration_hours"]==1.5
        assert all(row.metrics_json["work_date"]=="2026-10-30" for row in rows)
        assert db.scalar(select(ShiftReport)).work_date==at().date()


async def test_interrupted_hour_merges_saved_fragment_but_hands_only_new_room_end_to_next_hour(state):
    schedule_id=add_schedule(state)
    dispatcher=service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    with state.db() as db:
        first=db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.period_index==1))
        second=db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.period_index==2))
        schedule=db.get(ShiftSchedule,schedule_id)
        baseline=copy.deepcopy(first.raw_json["end_baseline"])
        old=copy.deepcopy(first.raw_json["segments"][0])
    old["room"]["live_end"]=at(8,30,0).isoformat()
    old["source"]["segment_end"]=at(8,30,0).isoformat()
    old["snapshot"]["counters"]["gmv"]=300
    old["captured_at"]=at(8,30).isoformat()
    new=copy.deepcopy(old)
    new["room"].update(live_room_id="202",live_start=at(8,30,0).isoformat(),live_end=None)
    new["snapshot"]["live_room_id"]="202"
    new["snapshot"]["counters"]["gmv"]=50
    new["captured_at"]=at(9).isoformat()
    new["source"]["segment_end"]=at(9,0,0).isoformat()
    capture={"segments":[old,new],"collected_at":at(9).isoformat()}
    metrics,raw,missing=hourly.detail_values(capture,baseline,second,schedule)
    assert metrics["gmv"]==250
    next_baseline=hourly.end_baseline(capture,"new-room-run")
    assert next_baseline["live_room_id"]=="202" and next_baseline["counters"]["gmv"]==50
    assert raw["source"]["segment_count"]==2


async def test_restart_releases_incomplete_hour_state_without_resending_uncertain_receipts(state):
    schedule_id=add_schedule(state)
    first=service.RunDispatcher(None)
    with state.db() as db:
        schedule=db.get(ShiftSchedule,schedule_id)
        detail=first._ensure_hour_plan(db,schedule)[0]
        run=CollectionRun(id="interrupted-hour",kind="shift_hour",schedule_id=schedule_id,trigger="scheduler",status="running")
        db.add(run)
        db.flush()
        detail.latest_run_id,detail.status="interrupted-hour","running"
        db.add(ScreenshotRequest(id=hourly.period_request_id(detail),target="hourly",schedule_id=schedule_id,
            planned_at=detail.end_at,hour_index=1,run_id=run.id,status="capturing"))
        db.commit()
    restarted=service.RunDispatcher(None)
    restarted._recover_screenshot_requests()
    with state.db() as db:
        assert db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.period_index==1)).status=="failed"
    await restarted._check_due_schedules()
    await drain(restarted)
    assert len(state.captures)==1


async def test_retention_preserves_manually_configured_table_and_all_local_hour_data(state):
    state.settings.update(bitable_app_token="test-base",bitable_table_id="test-table")
    schedule_id=add_schedule(state)
    dispatcher=service.RunDispatcher(None)
    await collect_shift(state,dispatcher)
    deleted=[]
    original=service.BitableClient.delete_record
    async def delete_record(self, record_id):
        deleted.append(record_id)
        await original(self,record_id)
    service.BitableClient.delete_record=delete_record
    try:
        with state.db() as db:
            parent_id=db.scalar(select(ShiftReport)).bitable_record_id
            child_ids=[row.bitable_record_id for row in db.scalars(select(ShiftHourDetail).order_by(ShiftHourDetail.period_index))]
        state.clock[0]=datetime(2027,3,1,8,tzinfo=APP_TIMEZONE)
        archived,failed=await dispatcher._archive_cold_bitable_records(state.settings)
        assert archived==0 and not failed
        assert deleted==[]
        with state.db() as db:
            assert db.scalar(select(func.count(ShiftHourDetail.id)))==4
            assert [row.bitable_record_id for row in db.scalars(select(ShiftHourDetail).order_by(ShiftHourDetail.period_index))]==child_ids
            assert db.get(ShiftHourState,schedule_id).parent_record_id==parent_id
    finally:
        service.BitableClient.delete_record=original
