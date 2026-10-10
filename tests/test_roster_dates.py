from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import liveops.app as app_module
from liveops.db import SessionLocal
from liveops.models import CollectionRun, ScheduleDay, ShiftSchedule


@pytest.fixture
def roster_dates(client, monkeypatch):
    monkeypatch.setattr(
        app_module, "now_local",
        lambda: datetime(2037, 10, 9, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    with SessionLocal() as db:
        rows = [
            ShiftSchedule(
                work_date=date(2037, 10, day), start_time=time(6), end_time=time(10),
                host_name=f"日期限制测试{day}", live_room_id="test", enabled=False,
            )
            for day in (8, 9, 10)
        ]
        db.add_all(rows)
        db.add(ScheduleDay(work_date=date(2037, 10, 8), rest="保留历史备注", shoot="保留拍摄"))
        db.commit()
        ids = {row.work_date.day: row.id for row in rows}
    yield ids
    with SessionLocal() as db:
        for run in db.scalars(select(CollectionRun).where(CollectionRun.schedule_id.in_(
            select(ShiftSchedule.id).where(ShiftSchedule.work_date.between(date(2037, 10, 1), date(2037, 10, 31)))
        ))).all():
            db.delete(run)
        db.flush()
        for row in db.scalars(select(ShiftSchedule).where(
            ShiftSchedule.work_date.between(date(2037, 10, 1), date(2037, 10, 31))
        )).all():
            db.delete(row)
        for row in db.scalars(select(ScheduleDay).where(
            ScheduleDay.work_date.between(date(2037, 10, 1), date(2037, 10, 31))
        )).all():
            db.delete(row)
        db.commit()


def payload(day):
    return {"work_date": f"2037-10-{day:02}", "start_time": "12:00", "end_time": "14:00",
            "host_name": "新增测试主播", "enabled": False}


def test_historical_mutations_rejected_without_changing_rows(client, roster_dates):
    historical_id = roster_dates[8]
    requests = [
        ("POST", "/api/schedules", payload(8)),
        ("PUT", f"/api/schedules/{historical_id}", payload(9)),
        ("PUT", f"/api/schedules/{roster_dates[10]}", payload(8)),
        ("DELETE", f"/api/schedules/{historical_id}", None),
        ("POST", f"/api/schedules/{historical_id}/toggle", None),
        ("PUT", "/api/schedule-days/2037-10-08", {"rest": "改写", "shoot": "改写"}),
        ("POST", "/api/schedules/auto-fill", {"year": 2037, "month": 9}),
        ("POST", "/api/schedules/auto-fill", {"year": 2037, "month": 9, "replace": True}),
    ]
    for method, path, body in requests:
        response = client.request(method, path, json=body)
        assert response.status_code == 409, (method, path, response.text)
        assert "历史" in response.json()["detail"]
    with SessionLocal() as db:
        row = db.get(ShiftSchedule, historical_id)
        assert row.work_date == date(2037, 10, 8)
        assert row.host_name == "日期限制测试8"
        assert row.enabled is False
        assert db.get(ShiftSchedule, roster_dates[10]).work_date == date(2037, 10, 10)
        assert db.get(ScheduleDay, date(2037, 10, 8)).rest == "保留历史备注"
    assert len(client.get("/api/schedules?work_date=2037-10-08").json()["items"]) == 1


@pytest.mark.parametrize("day", [9, 10])
def test_today_and_future_dates_still_allow_roster_edits(client, roster_dates, day):
    created = client.post("/api/schedules", json=payload(day))
    assert created.status_code == 200, created.text
    schedule_id = created.json()["item"]["id"]
    updated = payload(day) | {"host_name": "修改后的主播"}
    assert client.put(f"/api/schedules/{schedule_id}", json=updated).status_code == 200
    assert client.post(f"/api/schedules/{schedule_id}/toggle").status_code == 200
    assert client.put(f"/api/schedule-days/2037-10-{day:02}", json={"rest": "休息"}).status_code == 200
    assert client.delete(f"/api/schedules/{schedule_id}").status_code == 200


def test_current_month_template_reset_preserves_history_and_its_runs(client, roster_dates):
    with SessionLocal() as db:
        db.add(CollectionRun(id="readonly-history-run", schedule_id=roster_dates[8],
                             kind="report", trigger="user", status="success"))
        db.commit()
    response = client.post("/api/schedules/auto-fill", json={"year": 2037, "month": 10, "replace": True})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items and all(row["work_date"] >= "2037-10-09" for row in items)
    with SessionLocal() as db:
        assert db.get(ShiftSchedule, roster_dates[8]).host_name == "日期限制测试8"
        assert db.get(CollectionRun, "readonly-history-run").schedule_id == roster_dates[8]
        assert db.get(ScheduleDay, date(2037, 10, 8)).shoot == "保留拍摄"
        assert db.get(ScheduleDay, date(2037, 10, 1)) is None


def test_current_month_auto_fill_skips_past_dates(client, roster_dates):
    with SessionLocal() as db:
        for schedule_id in roster_dates.values():
            db.delete(db.get(ShiftSchedule, schedule_id))
        db.delete(db.get(ScheduleDay, date(2037, 10, 8)))
        db.commit()
    response = client.post("/api/schedules/auto-fill", json={"year": 2037, "month": 10})
    assert response.status_code == 200, response.text
    assert min(row["work_date"] for row in response.json()["items"]) == "2037-10-09"
    days = client.get("/api/schedules?year=2037&month=10").json()["days"]
    assert len(days) == 23 and all(day >= "2037-10-09" for day in days)
