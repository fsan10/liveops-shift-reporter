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


def test_run_history_includes_missed_plans_in_local_time_without_duplicate_executions(api_client):
    from datetime import datetime, timezone
    from urllib.parse import quote

    from liveops.models import CollectionRun, ScreenshotRequest
    from liveops.portal import APP_TIMEZONE

    def local(hour):
        return datetime(2098, 3, 15, hour, tzinfo=APP_TIMEZONE)

    with service.SessionLocal() as db:
        for key, hour in [("captured-22", 22), ("captured-19", 19)]:
            db.add(CollectionRun(id=key, kind="report", trigger="scheduler", status="succeeded",
                created_at=local(hour).astimezone(timezone.utc), started_at=local(hour).astimezone(timezone.utc)))
        db.flush()
        for key, hour, status, run_id in [
            ("shift-hour:1:21:+08:00", 21, "missed", None),
            ("missed-20", 20, "missed", None),
            ("shared-22", 22, "captured", "captured-22"),
            ("pending-23", 23, "pending", None),
            ("skipped-18", 18, "skipped", None),
        ]:
            db.add(ScreenshotRequest(id=key, target="hourly", planned_at=local(hour), status=status,
                run_id=run_id, created_at=local(23).astimezone(timezone.utc),
                delivery_json={"reason": "未执行的计划"}))
        db.commit()
    pages = [api_client.get(f"/api/runs?page={page}&page_size=2").json() for page in (1, 2, 3)]
    assert all(page["total"] == 5 for page in pages)
    rows = [row for page in pages for row in page["items"]]
    assert [row["id"] for row in rows] == ["captured-22", "screenshot-request:shift-hour:1:21:+08:00",
        "screenshot-request:missed-20", "captured-19", "screenshot-request:skipped-18"]
    assert api_client.get("/api/runs?limit=6").json()["items"] == rows
    missed = rows[1]
    assert missed["status"] == "missed" and missed["entry_type"] == "screenshot_request"
    assert missed["planned_at"] == local(21).isoformat()
    assert missed["started_at"] is None and missed["finished_at"] is None
    assert missed["result"]["executed"] is False
    detail = api_client.get("/api/runs/" + quote(missed["id"], safe=""))
    assert detail.status_code == 200 and detail.json()["item"] == missed
    assert api_client.get("/api/runs/screenshot-request:missing").status_code == 404
    assert api_client.get("/api/runs/screenshot-request:shared-22").status_code == 404
    assert api_client.get("/api/runs/screenshot-request:pending-23").status_code == 404
    assert api_client.get("/api/runs?page=4&page_size=2").json()["items"] == []
    assert api_client.get("/api/status").json()["running_count"] == 0


def test_report_date_range_filters_before_pagination_and_keeps_day_query_compatible(api_client):
    from datetime import date, time

    from liveops.models import CollectionRun, ShiftReport, ShiftSchedule

    with service.SessionLocal() as db:
        for index, day in enumerate([date(2026, 10, 1), date(2026, 10, 15), date(2026, 11, 1)]):
            schedule = ShiftSchedule(work_date=day, start_time=time(7), end_time=time(11), host_name="测试主播", live_room_id="test")
            db.add(schedule)
            db.flush()
            run = CollectionRun(id=f"date-range-{index}", schedule_id=schedule.id, kind="report", trigger="scheduler", status="succeeded")
            db.add(run)
            db.flush()
            db.add(ShiftReport(schedule_id=schedule.id, latest_run_id=run.id, work_date=day, start_time=time(7), end_time=time(11),
                host_name="测试主播", shop_name="test", live_room_id="test", metrics_json={"gmv": index + 1}))
        db.commit()
    first = api_client.get("/api/reports?date_start=2026-10-01&date_end=2026-10-31&page_size=1").json()
    second = api_client.get("/api/reports?date_start=2026-10-01&date_end=2026-10-31&page_size=1&page=2").json()
    assert first["total"] == second["total"] == 2
    assert first["items"][0]["work_date"] == "2026-10-15"
    assert second["items"][0]["work_date"] == "2026-10-01"
    assert api_client.get("/api/reports?work_date=2026-10-15").json()["total"] == 1
    assert api_client.get("/api/reports?date_start=2026-11-01&date_end=2026-10-01").status_code == 422


def test_run_time_field_kind_and_status_filters_include_only_real_matching_times(api_client):
    from datetime import datetime, timezone

    from liveops.models import CollectionRun, ScreenshotRequest
    from liveops.portal import APP_TIMEZONE

    def local(day, hour, minute=0):
        return datetime(2026, 10, day, hour, minute, tzinfo=APP_TIMEZONE)

    midnight = datetime(2026, 11, 1, tzinfo=APP_TIMEZONE)
    with service.SessionLocal() as db:
        for key, kind, status, task_at, finished in [
            ("cross-midnight", "report", "succeeded", local(31, 23, 45), midnight.replace(minute=5)),
            ("hour-partial", "shift_hour", "partial", local(31, 22), local(31, 22, 1)),
            ("november-failed", "baseline", "failed", midnight.replace(hour=1), midnight.replace(hour=1, minute=1)),
        ]:
            db.add(CollectionRun(id=key, kind=kind, trigger="scheduler", status=status,
                created_at=task_at.astimezone(timezone.utc), started_at=task_at.astimezone(timezone.utc),
                finished_at=finished.astimezone(timezone.utc)))
        db.add(ScreenshotRequest(id="missed-at-23", target="hourly", status="missed", planned_at=local(31, 23),
            created_at=midnight.replace(hour=2).astimezone(timezone.utc)))
        db.commit()
    query = "/api/runs?start_at=2026-10-31T00:00:00&end_at=2026-10-31T23:59:59"
    assert api_client.get(query).json()["total"] == 3
    assert api_client.get(query + "&page_size=1").json()["total"] == 3
    finished = api_client.get(query + "&time_field=finished").json()
    assert [row["id"] for row in finished["items"]] == ["hour-partial"]
    missed = api_client.get(query + "&kinds=hourly_screenshot_plan&statuses=missed").json()
    assert missed["total"] == 1 and missed["items"][0]["id"] == "screenshot-request:missed-at-23"
    assert api_client.get(query + "&kinds=hourly_screenshot_plan&time_field=finished").json()["total"] == 0
    next_day = api_client.get("/api/runs?start_at=2026-11-01T00:00:00&end_at=2026-11-01T23:59:59&time_field=finished&statuses=succeeded").json()
    assert [row["id"] for row in next_day["items"]] == ["cross-midnight"]
    assert api_client.get(query + "&kinds=report,shift_hour&statuses=partial").json()["total"] == 1
    assert api_client.get(query + "&time_field=created").status_code == 422
    assert api_client.get(query + "&statuses=invalid").status_code == 422
    assert api_client.get("/api/runs?start_at=2026-11-01T00:00:00&end_at=2026-10-31T00:00:00").status_code == 422


def test_manual_target_is_month_scoped_and_changing_base_clears_old_table_and_view(api_client):
    from sqlalchemy import select

    from liveops.models import BitableMonthTable

    result = api_client.put("/api/settings", json={"values": {"bitable_app_token": "test-base", "bitable_table_id": "manual-a",
        "bitable_hourly_view_id": "view-a"}})
    assert result.status_code == 200
    saved = result.json()["settings"]
    assert saved["bitable_table_mode"] == "manual"
    assert saved["bitable_table_month"] == service.now_local().strftime("%Y-%m")
    with service.SessionLocal() as db:
        row = db.scalar(select(BitableMonthTable))
        assert row.manual_override and row.table_id == "manual-a" and row.view_id == "view-a"
        assert row.tables_json[0]["managed"] is False
    assert api_client.put("/api/settings", json={"values": {"bitable_table_month": "2026-01"}}).status_code == 422
    changed = api_client.put("/api/settings", json={"values": {"bitable_app_token": "another-base"}}).json()["settings"]
    assert changed["bitable_table_id"] == "" and changed["bitable_hourly_view_id"] == ""
    assert changed["bitable_table_mode"] == "automatic"


@pytest.mark.parametrize("values", [{"bitable_field_mapping": "[]"}, {"bitable_field_mapping": '{"gmv":1}'},
                                    {"bitable_sync_key_field": ""}, {"bitable_parent_field": "  "}])
def test_invalid_bitable_schema_settings_are_rejected_before_background_work(api_client, values):
    assert api_client.put("/api/settings", json={"values": values}).status_code == 422


def test_range_sync_endpoint_validates_dates_and_does_not_accept_empty_or_cold_range(api_client):
    assert api_client.post("/api/bitable/sync-range", json={"date_start": "2026-10-31", "date_end": "2026-10-01", "mode": "full"}).status_code == 422
    assert api_client.post("/api/bitable/sync-range", json={"date_start": "2026-10-01", "date_end": "2026-10-31", "mode": "unknown"}).status_code == 422
    assert api_client.post("/api/bitable/sync-range", json={"date_start": "2020-01-01", "date_end": "2020-01-31", "mode": "incremental"}).status_code == 422
