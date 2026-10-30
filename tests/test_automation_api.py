from __future__ import annotations

import importlib

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import liveops.service as service
from liveops.db import Base


@pytest.fixture
def api_client(monkeypatch):
    module = importlib.import_module("liveops.app")
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("APP_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(module, "init_db", lambda: None)
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service.RunDispatcher, "start_scheduler", lambda self: None)
    with TestClient(module.app) as client:
        yield client
    engine.dispose()


def test_three_switches_and_two_destinations_save_independently(api_client):
    before = api_client.get("/api/settings").json()["settings"]
    assert before["data_collection_enabled"] == "true"
    assert before["anchor_screenshot_enabled"] == "true"
    assert before["hourly_screenshot_enabled"] == "false"
    values = {"data_collection_enabled": "true", "anchor_screenshot_enabled": "false",
              "hourly_screenshot_enabled": "true", "feishu_chat_id": "anchor-destination",
              "feishu_hourly_chat_id": "business-destination"}
    assert api_client.put("/api/settings", json={"values": values}).status_code == 200
    after = api_client.get("/api/settings").json()["settings"]
    assert all(after[key] == value for key, value in values.items())
    page = api_client.get("/").text
    assert all(f'id="{key}"' in page for key in values)
    status = api_client.get("/api/automation/status").json()
    assert status["hourly_enabled"] and status["next_hourly_at"] is None


@pytest.mark.parametrize("values", [
    {"data_collection_enabled": "1"}, {"hourly_screenshot_enabled": "yes"},
    {"anchor_screenshot_enabled": ""}, {"hourly_message_template": "{unsupported_field}"},
    {"hourly_message_template": "{hour_index.__class__}"}, {"hourly_message_template": "{unfinished"},
])
def test_invalid_switches_and_hourly_templates_are_rejected(api_client, values):
    assert api_client.put("/api/settings", json={"values": values}).status_code == 422


def test_test_messages_use_the_selected_group_and_reject_unknown_targets(api_client, monkeypatch):
    module = importlib.import_module("liveops.app")
    destinations = []

    class Bot:
        def __init__(self, app, secret, chat):
            destinations.append(chat)

        async def send_test_message(self):
            return {"message_id": "test-receipt", "sent": True}

        async def close(self):
            pass

    monkeypatch.setattr(module, "FeishuClient", Bot)
    api_client.put("/api/settings", json={"values": {
        "feishu_chat_id": "anchor-destination", "feishu_hourly_chat_id": "business-destination"}})
    assert api_client.post("/api/integrations/feishu/send-test?target=anchor").status_code == 200
    assert api_client.post("/api/integrations/feishu/send-test?target=hourly").status_code == 200
    assert destinations == ["anchor-destination", "business-destination"]
    assert api_client.post("/api/integrations/feishu/send-test?target=other").status_code == 422
    assert api_client.post("/api/runs/unknown/retry-screenshots").status_code == 404


def test_hours_api_exposes_independent_baselines_without_double_counting_parent_rows(api_client):
    from datetime import datetime, time

    from sqlalchemy import select

    from liveops.models import (
        CollectionRun,
        ShiftHourDetail,
        ShiftHourState,
        ShiftReport,
        ShiftSchedule,
    )
    from liveops.portal import APP_TIMEZONE

    today = service.now_local().date()
    with service.SessionLocal() as db:
        schedule=ShiftSchedule(work_date=today,start_time=time(7),end_time=time(11,30),host_name="测试主播",live_room_id="101")
        db.add(schedule)
        db.flush()
        db.add(CollectionRun(id="hour-api",schedule_id=schedule.id,kind="shift_hour",trigger="scheduler",status="succeeded"))
        db.flush()
        parent=ShiftReport(schedule_id=schedule.id,latest_run_id="hour-api",work_date=today,
            start_time=time(7),end_time=time(11,30),host_name="测试主播",shop_name="测试店",live_room_id="101",
            raw_json={"is_final":False,"measurement_mode":"hourly_parent_pending"},metrics_json={})
        db.add(parent)
        db.add(ShiftHourState(schedule_id=schedule.id,initial_baseline_json={"source":"first_shift_zero"}))
        db.add(ShiftHourDetail(schedule_id=schedule.id,period_index=4,
            start_at=datetime.combine(today,time(10),tzinfo=APP_TIMEZONE),
            end_at=datetime.combine(today,time(11,30),tzinfo=APP_TIMEZONE),latest_run_id="hour-api",status="succeeded",
            metrics_json={"gmv":450,"duration_hours":1.5},baseline_json={"counters":{"gmv":450}},
            raw_json={"end_baseline":{"counters":{"gmv":900}}}))
        db.commit()
        parent_id,schedule_id=parent.id,schedule.id
    rows=api_client.get(f"/api/reports?work_date={today.isoformat()}").json()
    assert rows["total"]==1 and rows["items"][0]["hour_count"]==1
    assert rows["items"][0]["summary_pending"] and not rows["items"][0]["summary_final"]
    details=api_client.get(f"/api/reports/{parent_id}/hours").json()
    assert details["items"][0]["metrics"]["duration_hours"]==1.5
    assert details["items"][0]["baseline"]["counters"]["gmv"]==450
    assert details["items"][0]["end_baseline"]["counters"]["gmv"]==900
    assert api_client.get("/api/reports/9999/hours").status_code==404
    changed=api_client.put(f"/api/schedules/{schedule_id}",json={"work_date":today.isoformat(),"start_time":"07:00",
        "end_time":"12:00","host_name":"测试主播","live_room_id":"101"})
    assert changed.status_code==409
    with service.SessionLocal() as db:
        assert db.scalar(select(ShiftSchedule)).end_time==time(11,30)


def test_unexecuted_hour_plans_do_not_prevent_schedule_deletion(api_client):
    from datetime import time

    from liveops.models import ShiftSchedule

    with service.SessionLocal() as db:
        row=ShiftSchedule(work_date=service.now_local().date(),start_time=time(7),end_time=time(11,30),
                          host_name="未执行排班",live_room_id="101")
        db.add(row)
        db.commit()
        schedule_id=row.id
        service.RunDispatcher(None)._ensure_hour_plan(db,row)
        db.commit()
    assert api_client.delete(f"/api/schedules/{schedule_id}").status_code==200
