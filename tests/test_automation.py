from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import liveops.automation as automation
import liveops.service as service
import liveops.settings as settings_module
from liveops.db import Base
from liveops.models import CollectionRun, HourlyWatch, ScreenshotRequest, ShiftReport, ShiftSchedule
from liveops.portal import APP_TIMEZONE
from liveops.settings import DEFAULT_SETTINGS


def at(hour=10, minute=30, second=45, day=28):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=APP_TIMEZONE)


async def drain(dispatcher):
    while dispatcher._tasks:
        await asyncio.gather(*list(dispatcher._tasks))


@pytest.fixture
def state(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = {**DEFAULT_SETTINGS, "feishu_app_id": "test-app", "feishu_app_secret": "test-secret",
        "feishu_chat_id": "anchor-chat", "feishu_hourly_chat_id": "hourly-chat",
        "live_author_id": "author", "qianchuan_account_id": "account", "settle_delay_seconds": "30"}
    clock = [at()]
    room = {"live_room_id": "101", "author_id": "author", "live_start": at(8, 30, 0).isoformat(),
            "is_living": True, "live_end": None}
    captures, data_calls, sends, watches, alerts = [], [], [], [], []
    failures = {}

    class Bot:
        def __init__(self, app, secret, chat):
            self.chat = chat

        async def send_report(self, caption, images, **kwargs):
            sends.append((self.chat, caption, images))
            if self.chat in failures:
                raise failures[self.chat]
            return {"message_id": f"message-{len(sends)}"}

        async def send_alert(self, *args, **kwargs):
            alerts.append(self.chat)
            return {"message_id": f"alert-{len(alerts)}"}

        async def close(self):
            pass

    def images(room_id):
        return [{"path": "test.png", "relative_path": "test.png", "name": "测试截图",
                 "captured_at": clock[0].isoformat(), "live_room_id": room_id}]

    async def watch(values, pinned):
        watches.append(pinned)
        return dict(room) if room else None

    async def screenshots(**kwargs):
        captures.append(("screenshots", kwargs))
        assert "baseline" not in kwargs
        selected = kwargs["room"] or {**room, "live_room_id": kwargs["schedule"].live_room_id or "101"}
        return {"screenshots": images(selected["live_room_id"]), "room": selected,
                "page_errors": [], "collected_at": clock[0].isoformat()}

    async def report(**kwargs):
        data_calls.append(kwargs)
        schedule = kwargs["schedule"]
        room_id = schedule.live_room_id or "101"
        result_images = images(room_id) if kwargs["screenshots_enabled"] else []
        if result_images:
            captures.append(("report", kwargs))
        kwargs["on_room_confirmed"]({"live_room_id": room_id})
        if kwargs.get("on_captured"):
            await kwargs["on_captured"](result_images)
        source = {"gmv": 100, "qianchuan_coupon": 5, "paid_users": 1, "product_click_users": 2,
                  "viewers": 10, "watch_count": 100, "gpm": 1050, "interaction_rate": 1.25,
                  "new_followers": 1, "stay_seconds": 60, "spend": 10, "exposure": 100}
        snapshot = {"live_room_id": room_id, "qianchuan_account_id": "account",
                    "qianchuan": {"stat_cost_for_roi2": 10, "live_show_count_for_roi2": 100},
                    "counters": {"gmv": 100}}
        return {"source": source, "responses": [], "screenshots": result_images, "page_errors": [],
                "schedule_start": service.schedule_start(schedule).isoformat(),
                "schedule_end": service.schedule_end(schedule).isoformat(), "live_room_id": room_id,
                "is_final": True, "collected_at": clock[0].isoformat(), "author_id": "author",
                "segments": [{"room": {**room, "live_room_id": room_id}, "source": {
                    "segment_end": clock[0].isoformat()}, "snapshot": snapshot, "responses": [],
                    "qianchuan_account_id": "account", "captured_at": clock[0].isoformat()}]}

    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service, "db_settings", lambda *args: dict(settings))
    monkeypatch.setattr(settings_module, "db_settings", lambda *args: dict(settings))
    monkeypatch.setattr(service, "now_local", lambda: clock[0])
    monkeypatch.setattr(automation, "watch_live_room", watch)
    monkeypatch.setattr(automation, "collect_screenshots", screenshots)
    monkeypatch.setattr(service, "collect_report", report)
    monkeypatch.setattr(automation, "FeishuClient", Bot)
    monkeypatch.setattr(service, "FeishuClient", Bot)
    yield SimpleNamespace(db=sessions, settings=settings, clock=clock, room=room, captures=captures,
                          data=data_calls, sends=sends, watches=watches, failures=failures, alerts=alerts)
    engine.dispose()


def schedules(state, *, room="101", account="account", start=time(8, 30), end=time(10, 30)):
    with state.db() as db:
        first = ShiftSchedule(work_date=state.clock[0].date(), start_time=start, end_time=end,
                              host_name="测试主播甲", live_room_id=room, qianchuan_account_id=account)
        following = ShiftSchedule(work_date=state.clock[0].date(), start_time=end, end_time=time(12, 30),
                                  host_name="测试主播乙", live_room_id=room, qianchuan_account_id=account)
        db.add_all([first, following])
        db.commit()
        return first.id, following.id


@pytest.mark.parametrize("data,hourly,anchor", [
    (False, False, False), (False, False, True), (False, True, False), (False, True, True),
    (True, False, False), (True, False, True), (True, True, False), (True, True, True),
])
async def test_all_switch_combinations_and_coincident_capture(state, data, hourly, anchor):
    state.settings.update(data_collection_enabled=str(data).lower(), hourly_screenshot_enabled=str(hourly).lower(),
                          anchor_screenshot_enabled=str(anchor).lower())
    first, following = schedules(state)
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.data) == int(data)
    assert len(state.captures) == int(hourly or anchor)
    assert sorted(chat for chat, _, _ in state.sends) == sorted(
        (["anchor-chat"] if anchor else []) + (["hourly-chat"] if hourly else []))
    with state.db() as db:
        assert db.scalar(select(func.count(ShiftReport.id))) == int(data)
        next_shift = db.get(ShiftSchedule, following)
        assert (next_shift.baseline_json is not None) == data
        if data:
            assert next_shift.baseline_json["source"] == "previous_shift_end"
            assert next_shift.baseline_json["counters"]["gmv"] == 100
        else:
            assert db.get(ShiftSchedule, first).baseline_json is None
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.captures) == int(hourly or anchor)
    assert len(state.data) == int(data)
    if not hourly:
        assert not state.watches


async def test_hourly_works_without_roster_and_starts_after_one_elapsed_hour(state):
    state.settings.update(data_collection_enabled="false", anchor_screenshot_enabled="false", hourly_screenshot_enabled="true")
    state.room["live_start"] = at(8, 35, 0).isoformat()
    state.clock[0] = at(9, 34, 30)
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert not state.captures
    state.clock[0] = at(9, 35, 45)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.captures) == 1 and not state.data
    assert state.sends[0][0] == "hourly-chat" and "第1小时" in state.sends[0][1]
    with state.db() as db:
        run = db.scalar(select(CollectionRun).where(CollectionRun.kind == "hourly_screenshot"))
        assert run.schedule_id is None and "metrics" not in run.result_json
        assert "source" not in run.result_json and "baseline" not in run.result_json


@pytest.mark.parametrize("different", ["room", "account"])
async def test_different_room_or_account_cannot_share_images(state, different):
    state.settings.update(hourly_screenshot_enabled="true")
    schedules(state, room="202" if different == "room" else "101",
              account="other-account" if different == "account" else "account")
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.captures) == 2
    assert sorted(chat for chat, _, _ in state.sends) == ["anchor-chat", "hourly-chat"]
    hourly_images = next(images for chat, _, images in state.sends if chat == "hourly-chat")
    assert hourly_images[0]["live_room_id"] == "101"


async def test_restarts_keep_hourly_receipts_and_do_not_replay_missed_hours(state):
    state.settings.update(data_collection_enabled="false", anchor_screenshot_enabled="false", hourly_screenshot_enabled="true")
    first = service.RunDispatcher(None)
    await first._check_due_schedules()
    await drain(first)
    second = service.RunDispatcher(None)
    second._recover_screenshot_requests()
    await second._check_due_schedules()
    await drain(second)
    assert len(state.captures) == len(state.sends) == 1
    with state.db() as db:
        rows = db.scalars(select(ScreenshotRequest).order_by(ScreenshotRequest.hour_index)).all()
        assert [row.status for row in rows] == ["missed", "captured"]
        assert db.scalar(select(HourlyWatch)).next_hour == 3


async def test_old_pending_hourly_request_is_not_claimed_by_a_late_data_catchup(state):
    state.settings.update(anchor_screenshot_enabled="false", hourly_screenshot_enabled="true")
    schedules(state)
    scope = automation.watch_scope(state.settings)
    with state.db() as db:
        db.add(HourlyWatch(scope=scope, room_json=state.room, next_hour=3, checked_at=state.clock[0]))
        db.add(ScreenshotRequest(id=f"hourly:{scope}:101:2", target="hourly", planned_at=at(10, 30, 0),
            room_json=state.room, account_id="account", hour_index=2, status="pending"))
        db.commit()
    state.clock[0] = at(11, 0, 0)
    dispatcher = service.RunDispatcher(None)
    for _ in range(2):
        await dispatcher._check_due_schedules()
        await drain(dispatcher)
    assert len(state.data) == 1 and not state.captures and not state.sends
    with state.db() as db:
        assert db.get(ScreenshotRequest, f"hourly:{scope}:101:2").status == "missed"


def test_hourly_points_cross_midnight_without_resetting():
    start = at(23, 35, 0, day=27)
    room = {"is_living": True, "live_start": start.isoformat()}
    points = automation.hourly_points(room, 1, at(1, 36, 0))
    assert points == [(1, at(0, 35, 0)), (2, at(1, 35, 0))]
    room["is_living"] = False
    assert automation.hourly_points(room, 3, at(4)) == []


async def test_late_hourly_discovery_reuses_saved_anchor_capture(state):
    state.settings.update(data_collection_enabled="false", hourly_screenshot_enabled="false")
    schedules(state)
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.captures) == 1
    state.settings["hourly_screenshot_enabled"] = "true"
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.captures) == 1
    assert [chat for chat, _, _ in state.sends] == ["anchor-chat", "hourly-chat"]
    assert state.sends[0][2] == state.sends[1][2]


async def test_late_hourly_discovery_after_restart_also_reuses_saved_capture(state):
    state.settings.update(data_collection_enabled="false", hourly_screenshot_enabled="false")
    schedules(state)
    first = service.RunDispatcher(None)
    await first._check_due_schedules()
    await drain(first)
    state.settings["hourly_screenshot_enabled"] = "true"
    restarted = service.RunDispatcher(None)
    restarted._recover_screenshot_requests()
    await restarted._check_due_schedules()
    await drain(restarted)
    assert len(state.captures) == 1 and len(state.sends) == 2


async def test_shared_capture_respects_long_shift_settle_delay(state):
    state.settings.update(hourly_screenshot_enabled="true", settle_delay_seconds="600")
    schedules(state)
    state.clock[0] = at(10, 35, 0)
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert not state.captures
    state.clock[0] = at(10, 40, 45)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.captures) == len(state.data) == 1
    assert sorted(chat for chat, _, _ in state.sends) == ["anchor-chat", "hourly-chat"]


async def test_different_planned_times_capture_separately_and_route_correctly(state):
    state.settings["hourly_screenshot_enabled"] = "true"
    state.room["live_start"] = at(8, 35, 0).isoformat()
    schedules(state)
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert [chat for chat, _, _ in state.sends] == ["anchor-chat"]
    state.clock[0] = at(10, 35, 45)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.data) == 1 and len(state.captures) == 2
    assert [chat for chat, _, _ in state.sends] == ["anchor-chat", "hourly-chat"]


async def test_one_group_failure_retries_only_that_group_without_new_capture(state):
    state.settings.update(hourly_screenshot_enabled="true")
    schedules(state)
    state.failures["hourly-chat"] = RuntimeError("permission denied")
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert [chat for chat, _, _ in state.sends].count("hourly-chat") == 1
    with state.db() as db:
        original = db.scalar(select(CollectionRun).where(CollectionRun.kind == "report"))
        assert original.status == "partial"
        original_id = original.id
    state.failures.clear()
    await dispatcher.begin_screenshot_delivery(original_id)
    await drain(dispatcher)
    assert [chat for chat, _, _ in state.sends].count("anchor-chat") == 1
    assert [chat for chat, _, _ in state.sends].count("hourly-chat") == 2
    assert len(state.captures) == 1 and len(state.data) == 1


async def test_missing_hourly_group_never_falls_back_to_anchor_group(state):
    state.settings.update(data_collection_enabled="false", anchor_screenshot_enabled="false",
                          hourly_screenshot_enabled="true", feishu_hourly_chat_id="")
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.captures) == 1 and not state.sends
    with state.db() as db:
        request = db.scalar(select(ScreenshotRequest).where(ScreenshotRequest.status == "captured"))
        assert request.delivery_json["status"] == "not_configured" and request.delivery_json["chat_id"] == ""


async def test_failed_group_retry_uses_corrected_configuration_for_that_same_purpose(state):
    state.settings.update(data_collection_enabled="false", hourly_screenshot_enabled="true")
    schedules(state)
    state.failures["hourly-chat"] = RuntimeError("invalid chat")
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    with state.db() as db:
        source_id = db.scalar(select(ScreenshotRequest).where(ScreenshotRequest.target == "hourly",
            ScreenshotRequest.status == "captured")).run_id
    state.settings["feishu_hourly_chat_id"] = "corrected-hourly-chat"
    await dispatcher.begin_screenshot_delivery(source_id)
    await drain(dispatcher)
    assert sorted(chat for chat, _, _ in state.sends[:2]) == ["anchor-chat", "hourly-chat"]
    assert state.sends[-1][0] == "corrected-hourly-chat"
    assert len(state.captures) == 1


async def test_transport_timeout_is_uncertain_and_never_automatically_resent(state):
    state.settings.update(data_collection_enabled="false", anchor_screenshot_enabled="false", hourly_screenshot_enabled="true")
    state.failures["hourly-chat"] = httpx.ReadTimeout("response lost")
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    with state.db() as db:
        request = db.scalar(select(ScreenshotRequest).where(ScreenshotRequest.status == "captured"))
        assert request.delivery_json["status"] == "uncertain"
        run_id = request.run_id
    with pytest.raises(RuntimeError, match="核实"):
        await dispatcher.begin_screenshot_delivery(run_id)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert len(state.sends) == 1


async def test_hourly_metrics_are_never_requested_and_stopped_room_produces_no_capture(state):
    state.settings.update(data_collection_enabled="true", anchor_screenshot_enabled="true", hourly_screenshot_enabled="true")
    state.room.update(is_living=False, live_end=at(9).isoformat())
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert not state.data and not state.captures and not state.sends


async def test_hourly_only_does_not_depend_on_roster_room_or_baseline(state):
    state.settings.update(data_collection_enabled="false", anchor_screenshot_enabled="false", hourly_screenshot_enabled="true")
    first, _ = schedules(state, room="old-roster-room")
    with state.db() as db:
        row = db.get(ShiftSchedule, first)
        row.baseline_json = {"live_room_id": "old-baseline-room"}
        db.commit()
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert state.watches == [""] and len(state.captures) == 1 and not state.data
    assert state.sends[0][2][0]["live_room_id"] == "101"
    with state.db() as db:
        row = db.get(ShiftSchedule, first)
        assert row.live_room_id == "old-roster-room"
        assert row.baseline_json == {"live_room_id": "old-baseline-room"}


async def test_hourly_discovery_failure_is_audited_once_until_recovery(state, monkeypatch):
    state.settings.update(data_collection_enabled="false", anchor_screenshot_enabled="false", hourly_screenshot_enabled="true")

    async def unavailable(*args):
        raise RuntimeError("罗盘登录/场次接口未就绪")

    monkeypatch.setattr(automation, "watch_live_room", unavailable)
    dispatcher = service.RunDispatcher(None)
    for _ in range(2):
        await dispatcher._check_due_schedules()
        state.clock[0] += timedelta(minutes=1)
    assert state.alerts == ["hourly-chat"] and not state.captures and not state.data
    with state.db() as db:
        run = db.scalar(select(CollectionRun))
        assert run.kind == "hourly_watch" and run.status == "failed"
        assert run.result_json["exception_alert"]["status"] == "sent"


async def test_metric_failure_does_not_notify_successful_hourly_group(state, monkeypatch):
    state.settings.update(hourly_screenshot_enabled="true")
    schedules(state)
    monkeypatch.setattr(service, "build_report_metrics", lambda **kwargs: ({}, ["GMV"]))
    dispatcher = service.RunDispatcher(None)
    await dispatcher._check_due_schedules()
    await drain(dispatcher)
    assert sorted(chat for chat, _, _ in state.sends) == ["anchor-chat", "hourly-chat"]
    assert state.alerts == ["anchor-chat"]


async def test_recovery_does_not_resend_a_group_with_a_persisted_sending_state(state):
    with state.db() as db:
        db.add(CollectionRun(id="stopped", kind="hourly_screenshot", trigger="scheduler", status="running"))
        db.flush()
        db.add(ScreenshotRequest(id="recovered", target="hourly", planned_at=state.clock[0],
            room_json=state.room, run_id="stopped", status="captured", screenshots_json=[{"path": "test.png"}],
            delivery_json={"status": "sending", "chat_id": "hourly-chat"}))
        db.commit()
    dispatcher = service.RunDispatcher(None)
    dispatcher._recover_screenshot_requests()
    with state.db() as db:
        assert db.get(ScreenshotRequest, "recovered").delivery_json["status"] == "uncertain"
    await dispatcher._deliver_screenshot_requests("stopped", state.settings)
    assert not state.sends
