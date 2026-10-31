from __future__ import annotations

import time as wall_time


def _schedule_payload(**overrides):
    payload = {
        "work_date": "2098-03-15",
        "start_time": "06:00",
        "end_time": "10:00",
        "host_name": "测试主播A",
        "promoter": "投手A",
        "shop_name": "测试店铺",
        "live_room_id": "test-room",
        "qianchuan_account_id": "test-account",
        "enabled": True,
        "message_template": "以上主播{host_name}{start_time}-{end_time}点的直播数据",
    }
    payload.update(overrides)
    return payload


def test_app_serves_dashboard_and_health(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "直播运营数据台" in page.text
    assert client.get("/static/app.js").status_code == 200
    status = client.get("/api/status").json()
    assert status["ok"] is True
    assert status["second_precision_supported"] is True
    assert len(client.get("/api/report-columns").json()["items"]) == 25


def test_schedule_ui_uses_roster_prototype_and_inline_controls(client):
    page = client.get("/").text
    assert 'id="schedule-runs"' not in page
    assert 'id="all-runs"' not in page
    assert 'data-view="runs"' in page
    assert 'id="roster-frame" src="/static/roster.html?v=20261031-roster-upload"' in page
    assert 'id="form-room"' not in page
    assert 'id="form-account"' not in page
    assert 'widgets.bundle.js' in page

    roster = client.get("/static/roster.html").text
    assert 'id="timeMask"' in roster
    assert 'id="pop"' in roster
    assert 'id="autoBtn"' in roster
    assert 'id="exportBtn"' in roster
    assert 'data-action="run"' in roster
    assert 'data-action="interrupt"' in roster
    assert 'id="interruptTime"' in roster
    assert 'id="interruptSave"' in roster
    assert 'data-action="toggle"' in roster
    assert 'data-action="delete"' in roster
    assert 'id="import-shifts"' not in roster


def test_interruption_route_passes_requested_time_to_dispatcher(client, monkeypatch):
    from datetime import datetime, timezone

    from liveops.models import CollectionRun
    from liveops.service import RunDispatcher

    requests = []

    async def begin(_self, schedule_id, kind, trigger, interrupted_at=None):
        requests.append((schedule_id, kind, trigger, interrupted_at.isoformat()))
        return CollectionRun(id="test-interruption", schedule_id=schedule_id, kind=kind,
            trigger=trigger, status="queued", created_at=datetime.now(timezone.utc),
            result_json={}, missing_fields=[], message="")

    monkeypatch.setattr(RunDispatcher, "begin_run", begin)
    response = client.post("/api/schedules/123/interrupt", json={"interrupted_at": "2026-10-09T13:47:00+08:00"})
    assert response.status_code == 200, response.text
    assert response.json()["run"]["kind"] == "interruption"
    assert requests == [(123, "interruption", "user", "2026-10-09T13:47:00+08:00")]
    assert client.post("/api/schedules/123/interrupt", json={"interrupted_at": "bad time"}).status_code == 422


def test_dashboard_uses_null_when_no_metrics_have_been_collected(client):
    totals = client.get("/api/dashboard?work_date=2098-03-15").json()["totals"]
    assert totals["gmv"] is None
    assert totals["coupon"] is None
    assert totals["overall"] is None
    assert totals["spend"] is None
    assert totals["viewers"] is None
    assert totals["roi"] is None


def test_schedule_validation_enforces_unique_hosts_and_no_overlap(client):
    first = client.post("/api/schedules", json=_schedule_payload())
    assert first.status_code == 200, first.text
    schedule_id = first.json()["item"]["id"]
    try:
        duplicate_host = client.post(
            "/api/schedules",
            json=_schedule_payload(start_time="10:00", end_time="14:00"),
        )
        assert duplicate_host.status_code == 409
        overlap = client.post(
            "/api/schedules",
            json=_schedule_payload(host_name="测试主播B", start_time="09:00", end_time="11:00"),
        )
        assert overlap.status_code == 409
        assert (
            client.get("/api/schedules?work_date=2098-03-15").json()["items"][0]["host_name"]
            == "测试主播A"
        )
        assert (
            client.post(f"/api/schedules/{schedule_id}/toggle").json()["item"]["enabled"] is False
        )
    finally:
        client.delete(f"/api/schedules/{schedule_id}")


def test_schedule_creation_needs_only_date_host_and_times(client):
    settings = client.get("/api/settings").json()["settings"]
    response = client.post(
        "/api/schedules",
        json={
            "work_date": "2099-03-15",
            "start_time": "06:00:05",
            "end_time": "10:00:03",
            "host_name": "仅填写基础字段的主播",
        },
    )
    assert response.status_code == 200, response.text
    item = response.json()["item"]
    try:
        assert item["host_name"] == "仅填写基础字段的主播"
        assert item["start_time"] == "06:00:05"
        assert item["end_time"] == "10:00:03"
        assert item["promoter"] == ""
        assert item["live_room_id"] == settings["live_room_id"]
        assert item["qianchuan_account_id"] == settings["qianchuan_account_id"]
        assert item["enabled"] is True
    finally:
        client.delete(f"/api/schedules/{item['id']}")


def test_month_auto_schedule_uses_october_weekday_templates_only_for_empty_month(client, monkeypatch):
    from datetime import date, datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from sqlalchemy import select

    import liveops.app as app_module
    from liveops.db import SessionLocal
    from liveops.models import ScheduleDay, ShiftSchedule

    monkeypatch.setattr(app_module, "now_local", lambda: datetime(2026, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(
        app_module,
        "SCHEDULE_TEMPLATE_PATH",
        Path(app_module.__file__).parent / "data" / "schedule_template_2026-10.example.json",
    )

    start, end = date(2026, 10, 1), date(2026, 11, 1)
    try:
        generated = client.post("/api/schedules/auto-fill", json={"year": 2026, "month": 10})
        assert generated.status_code == 200, generated.text
        assert generated.json()["count"] == 124

        month = client.get("/api/schedules?year=2026&month=10")
        assert month.status_code == 200, month.text
        payload = month.json()
        assert len(payload["items"]) == 124
        assert len(payload["days"]) == 31
        assert payload["items"][0]["promoter"]
        assert payload["days"]["2026-10-01"]["rest"] == ""

        saved_note = client.put(
            "/api/schedule-days/2026-10-02",
            json={"rest": "主播甲/运营乙", "shoot": "短视频拍摄"},
        )
        assert saved_note.status_code == 200, saved_note.text
        month = client.get("/api/schedules?year=2026&month=10").json()
        assert month["days"]["2026-10-02"] == {"rest": "主播甲/运营乙", "shoot": "短视频拍摄"}

        repeated = client.post("/api/schedules/auto-fill", json={"year": 2026, "month": 10})
        assert repeated.status_code == 409
    finally:
        with SessionLocal() as db:
            for schedule in db.scalars(
                select(ShiftSchedule).where(
                    ShiftSchedule.work_date >= start, ShiftSchedule.work_date < end
                )
            ).all():
                db.delete(schedule)
            for day in db.scalars(
                select(ScheduleDay).where(
                    ScheduleDay.work_date >= start, ScheduleDay.work_date < end
                )
            ).all():
                db.delete(day)
            db.commit()


def test_schedule_accepts_seconds_and_report_list_is_paginated(client):
    from datetime import date, time

    from liveops.db import SessionLocal
    from liveops.models import CollectionRun, ShiftReport, ShiftSchedule

    with SessionLocal() as db:
        schedule = ShiftSchedule(
            work_date=date(2097, 3, 15),
            start_time=time(7, 5, 3),
            end_time=time(8, 0, 4),
            host_name="分页测试主播",
            promoter="分页测试投手",
            shop_name="测试店铺",
            live_room_id="test-room",
            qianchuan_account_id="test-account",
        )
        db.add(schedule)
        db.commit()
        schedule_id = schedule.id
        run = CollectionRun(
            id="pagination-run",
            schedule_id=schedule_id,
            kind="report",
            trigger="user",
            status="succeeded",
        )
        db.add(run)
        db.commit()
        report = ShiftReport(
            schedule_id=schedule_id,
            latest_run_id="pagination-run",
            work_date=schedule.work_date,
            start_time=schedule.start_time,
            end_time=schedule.end_time,
            host_name=schedule.host_name,
            promoter=schedule.promoter,
            shop_name=schedule.shop_name,
            live_room_id=schedule.live_room_id,
        )
        db.add(report)
        db.commit()
        report_id = report.id
    try:
        schedules = client.get("/api/schedules?work_date=2097-03-15").json()["items"]
        assert schedules[0]["start_time"] == "07:05:03"
        assert schedules[0]["end_time"] == "08:00:04"
        assert schedules[0]["promoter"] == "分页测试投手"
        result = client.get("/api/reports?work_date=2097-03-15&page=1&page_size=1").json()
        assert result["total"] == 1
        assert result["page"] == 1
        assert result["page_size"] == 1
        assert result["items"][0]["id"] == report_id
        run_page = client.get("/api/runs?page=1&page_size=1").json()
        assert run_page["page"] == 1
        assert run_page["page_size"] == 1
        assert "total" in run_page
    finally:
        with SessionLocal() as db:
            row = db.get(ShiftReport, report_id)
            if row:
                db.delete(row)
            run = db.get(CollectionRun, "pagination-run")
            if run:
                db.delete(run)
            schedule = db.get(ShiftSchedule, schedule_id)
            if schedule:
                db.delete(schedule)
            db.commit()


def test_secret_settings_are_saved_but_not_returned(client):
    cdp_endpoint = "wss://profile-cdp-endpoint.private.example/session/abcdef1234567890"
    response = client.put(
        "/api/settings",
        json={
            "values": {
                "feishu_app_id": "test-app",
                "feishu_app_secret": "secret-value-for-test",
                "browser_cdp_endpoint": cdp_endpoint,
            }
        },
    )
    assert response.status_code == 200
    returned = client.get("/api/settings").json()
    assert "secret-value-for-test" not in str(returned)
    assert returned["settings"]["feishu_app_secret_configured"] is True
    assert cdp_endpoint not in str(returned)
    assert returned["settings"]["browser_cdp_endpoint_masked"] == (
        f"{cdp_endpoint[:5]}******{cdp_endpoint[-5:]}"
    )
    assert returned["settings"]["feishu_app_secret_masked"] == (
        f"{'secret-value-for-test'[:5]}******{'secret-value-for-test'[-5:]}"
    )
    from liveops.db import SessionLocal
    from liveops.models import StoredSetting

    with SessionLocal() as db:
        saved_secret = db.get(StoredSetting, "feishu_app_secret")
        assert saved_secret is not None
        assert saved_secret.encrypted is True
        assert saved_secret.value != "secret-value-for-test"
        saved_endpoint = db.get(StoredSetting, "browser_cdp_endpoint")
        assert saved_endpoint is not None
        assert saved_endpoint.encrypted is True
        assert saved_endpoint.value != cdp_endpoint
    unknown = client.put("/api/settings", json={"values": {"unexpected_setting": "x"}})
    assert unknown.status_code == 422


def test_manual_data_only_run_persists_a_report_and_run_history(client, monkeypatch):
    from datetime import time

    from sqlalchemy import select

    import liveops.service as service_module
    from liveops.db import SessionLocal
    from liveops.models import CollectionRun, ShiftReport, ShiftSchedule
    from liveops.service import now_local

    async def fake_collect_report(**kwargs):
        assert kwargs["screenshots_enabled"] is False
        return {
            "source": {
                "gmv": 100,
                "qianchuan_coupon": 5,
                "paid_users": 1,
                "product_click_users": 2,
                "viewers": 10,
                "watch_count": 100,
                "gpm": 1050,
                "interaction_rate": 1.25,
                "new_followers": 1,
                "stay_seconds": 60,
                "spend": 10,
                "exposure": 100,
                "daily_exposure": 1000,
            },
            "screenshots": [],
            "responses": [],
            "page_errors": [],
            "schedule_start": "2098-03-15T06:00:00+08:00",
            "schedule_end": "2098-03-15T10:00:00+08:00",
            "is_final": True,
            "segments": [{
                "room": {"live_room_id": "test-room"},
                "qianchuan_account_id": "test-account",
                "captured_at": service_module.now_local().isoformat(),
                "snapshot": {
                    "live_room_id": "test-room",
                    "qianchuan_account_id": "test-account",
                    "captured_at": service_module.now_local().isoformat(),
                    "qianchuan": {"live_show_count_for_roi2": 1000},
                },
                "source": {
                    "live_room_id": "test-room",
                    "qianchuan_account_id": "test-account",
                    "segment_end": service_module.now_local().isoformat(),
                },
            }],
        }

    original_settings = service_module.db_settings
    monkeypatch.setattr(service_module, "db_settings", lambda *args: {
        **original_settings(*args), "anchor_screenshot_enabled": "false"})
    monkeypatch.setattr(service_module, "collect_report", fake_collect_report)
    schedule = ShiftSchedule(
        work_date=now_local().date(),
        start_time=time(6),
        end_time=time(10),
        host_name="采集服务测试主播",
        promoter="测试投手",
        shop_name="测试店铺",
        live_room_id="test-room",
        qianchuan_account_id="test-account",
    )
    with SessionLocal() as db:
        db.add(schedule)
        db.commit()
        schedule_id = schedule.id

    try:
        response = client.post(f"/api/schedules/{schedule_id}/run")
        assert response.status_code == 200, response.text
        run_id = response.json()["run"]["id"]
        deadline = wall_time.monotonic() + 5
        run = None
        while wall_time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()["item"]
            if run["status"] not in {"queued", "running"}:
                break
            wall_time.sleep(0.02)
        assert run is not None and run["status"] == "succeeded", run
        report = client.get(f"/api/reports?work_date={schedule.work_date.isoformat()}").json()["items"]
        assert len(report) == 1
        assert report[0]["metrics"]["overall_amount"] == 105
        assert report[0]["metrics"]["overall_roi"] == 10.5
    finally:
        with SessionLocal() as db:
            report = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule_id))
            if report:
                db.delete(report)
            for run in db.scalars(
                select(CollectionRun).where(CollectionRun.schedule_id == schedule_id)
            ).all():
                db.delete(run)
            cleanup_schedule = db.get(ShiftSchedule, schedule_id)
            if cleanup_schedule:
                db.delete(cleanup_schedule)
            db.commit()


def test_manual_run_rejects_past_and_future_schedules_without_queuing(client):
    from datetime import date, time

    from sqlalchemy import select

    from liveops.db import SessionLocal
    from liveops.models import CollectionRun, ShiftSchedule

    ids = []
    with SessionLocal() as db:
        for work_date in (date(2001, 1, 1), date(2097, 1, 1)):
            row = ShiftSchedule(
                work_date=work_date, start_time=time(3, 13), end_time=time(3, 14),
                host_name="日期保护测试主播", shop_name="测试店铺",
                live_room_id="", qianchuan_account_id="test-account",
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
    try:
        for schedule_id, label in zip(ids, ("历史", "未来"), strict=True):
            for path, payload in (
                (f"/api/schedules/{schedule_id}/run", None),
                ("/api/collectors/run", {"schedule_id": schedule_id}),
            ):
                response = client.post(path, json=payload)
                assert response.status_code == 409
                assert label in response.json()["detail"]
            with SessionLocal() as db:
                assert not db.scalar(select(CollectionRun.id).where(CollectionRun.schedule_id == schedule_id))
    finally:
        with SessionLocal() as db:
            for schedule_id in ids:
                row = db.get(ShiftSchedule, schedule_id)
                if row:
                    db.delete(row)
            db.commit()


def test_completed_shift_end_snapshot_becomes_next_shift_baseline(client, monkeypatch):
    from datetime import time

    from sqlalchemy import select

    import liveops.service as service_module
    from liveops.db import SessionLocal
    from liveops.models import CollectionRun, ShiftReport, ShiftSchedule
    from liveops.service import now_local

    seen_baselines = []

    async def fake_collect_report(**kwargs):
        assert kwargs["screenshots_enabled"] is False
        seen_baselines.append(kwargs["baseline"])
        sample = len(seen_baselines) - 1
        return {
            "source": {
                "gmv": 100,
                "qianchuan_coupon": 5,
                "paid_users": 1,
                "product_click_users": 2,
                "viewers": 10,
                "watch_count": 100,
                "gpm": 1050,
                "interaction_rate": 1.25,
                "new_followers": 1,
                "stay_seconds": 60,
                "spend": 10,
                "exposure": 100,
                "daily_exposure": 1000,
            },
            "screenshots": [],
            "responses": [
                {
                    "source": "qianchuan",
                    "totals": {
                        "total_pay_order_coupon_amount_realtime_for_roi2": {"Value": 2200 + sample * 100},
                        "stat_cost_for_roi2": {"Value": 9000 + sample * 100},
                        "live_show_count_for_roi2": {"Value": 40000 + sample * 100},
                    },
                }
            ],
            "page_errors": [],
            "schedule_start": "2026-09-29T00:01:00+08:00",
            "schedule_end": "2026-09-29T00:02:00+08:00",
            "live_room_id": "test-room",
            "collected_at": "2026-09-29T00:02:30+08:00",
        }

    original_settings = service_module.db_settings
    monkeypatch.setattr(service_module, "db_settings", lambda *args: {
        **original_settings(*args), "anchor_screenshot_enabled": "false"})
    monkeypatch.setattr(service_module, "collect_report", fake_collect_report)
    work_date = now_local().date()
    with SessionLocal() as db:
        for host_name in ("自动基线测试主播A", "自动基线测试主播B", "自动基线测试主播C"):
            old = db.scalars(
                select(ShiftSchedule).where(ShiftSchedule.host_name == host_name)
            ).all()
            for row in old:
                for report in db.scalars(
                    select(ShiftReport).where(ShiftReport.schedule_id == row.id)
                ).all():
                    db.delete(report)
                for run in db.scalars(
                    select(CollectionRun).where(CollectionRun.schedule_id == row.id)
                ).all():
                    db.delete(run)
                db.delete(row)
        first = ShiftSchedule(
            work_date=work_date,
            start_time=time(0, 1),
            end_time=time(0, 2),
            host_name="自动基线测试主播A",
            shop_name="测试店铺",
            live_room_id="test-room",
            qianchuan_account_id="test-account",
        )
        following = ShiftSchedule(
            work_date=work_date,
            start_time=time(0, 2),
            end_time=time(0, 3),
            host_name="自动基线测试主播B",
            shop_name="测试店铺",
            live_room_id="test-room",
            qianchuan_account_id="test-account",
        )
        third = ShiftSchedule(
            work_date=work_date,
            start_time=time(0, 3),
            end_time=time(0, 4),
            host_name="自动基线测试主播C",
            shop_name="测试店铺",
            live_room_id="test-room",
            qianchuan_account_id="test-account",
        )
        db.add_all([first, following, third])
        db.commit()
        first_id, following_id, third_id = first.id, following.id, third.id
    try:
        response = client.post(f"/api/schedules/{first_id}/run")
        assert response.status_code == 200, response.text
        run_id = response.json()["run"]["id"]
        deadline = wall_time.monotonic() + 5
        while wall_time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()["item"]
            if run["status"] not in {"queued", "running"}:
                break
            wall_time.sleep(0.02)
        assert seen_baselines[0]["source"] == "first_shift_zero"
        assert run["finished_at"] is not None
        assert any(step["stage"] == "baseline" for step in run["result"]["steps"])
        with SessionLocal() as db:
            following = db.get(ShiftSchedule, following_id)
            assert following is not None
            assert following.baseline_json["source"] == "previous_shift_end"
            assert (
                following.baseline_json["qianchuan"][
                    "total_pay_order_coupon_amount_realtime_for_roi2"
                ]
                == 2200
            )
            assert following.baseline_json["qianchuan"]["stat_cost_for_roi2"] == 9000
        for current_id in (first_id, following_id, following_id, third_id):
            response = client.post(f"/api/schedules/{current_id}/run")
            assert response.status_code == 200, response.text
            run_id = response.json()["run"]["id"]
            deadline = wall_time.monotonic() + 5
            while wall_time.monotonic() < deadline:
                run = client.get(f"/api/runs/{run_id}").json()["item"]
                if run["status"] not in {"queued", "running"}:
                    break
                wall_time.sleep(0.02)
            assert run["status"] in {"succeeded", "partial"}
        assert seen_baselines[1]["source"] == "first_shift_zero"
        assert seen_baselines[2]["qianchuan"]["total_pay_order_coupon_amount_realtime_for_roi2"] == 2300
        assert seen_baselines[3]["qianchuan"]["total_pay_order_coupon_amount_realtime_for_roi2"] == 2300
        assert seen_baselines[4]["qianchuan"]["total_pay_order_coupon_amount_realtime_for_roi2"] == 2500
        with SessionLocal() as db:
            following = db.get(ShiftSchedule, following_id)
            assert following.baseline_json["qianchuan"]["total_pay_order_coupon_amount_realtime_for_roi2"] == 2300
            third = db.get(ShiftSchedule, third_id)
            assert third.baseline_json["qianchuan"]["total_pay_order_coupon_amount_realtime_for_roi2"] == 2500
    finally:
        with SessionLocal() as db:
            for report in db.scalars(
                select(ShiftReport).where(ShiftReport.schedule_id.in_([first_id, following_id, third_id]))
            ).all():
                db.delete(report)
            for run in db.scalars(
                select(CollectionRun).where(CollectionRun.schedule_id.in_([first_id, following_id, third_id]))
            ).all():
                db.delete(run)
            for schedule_id in (first_id, following_id, third_id):
                schedule = db.get(ShiftSchedule, schedule_id)
                if schedule:
                    db.delete(schedule)
            db.commit()


def test_monthly_retention_never_deletes_unregistered_manual_table_records(monkeypatch):
    import asyncio
    from datetime import time, timedelta

    import liveops.service as service_module
    from liveops.db import SessionLocal
    from liveops.models import CollectionRun, ShiftReport, ShiftSchedule
    from liveops.service import RunDispatcher, now_local

    previous_month = now_local().date().replace(day=1) - timedelta(days=1)
    deleted_record_ids = []

    class FakeBitableClient:
        def __init__(self, *_args):
            pass

        async def delete_record(self, record_id):
            deleted_record_ids.append(record_id)

        async def close(self):
            pass

    monkeypatch.setattr(service_module, "BitableClient", FakeBitableClient)
    with SessionLocal() as db:
        schedule = ShiftSchedule(
            work_date=previous_month,
            start_time=time(6),
            end_time=time(10),
            host_name="冷热分离测试主播",
            shop_name="测试店铺",
            live_room_id="test-room",
            qianchuan_account_id="test-account",
        )
        db.add(schedule)
        db.commit()
        schedule_id = schedule.id
        run = CollectionRun(
            id="cold-data-run",
            schedule_id=schedule_id,
            kind="report",
            trigger="user",
            status="succeeded",
        )
        db.add(run)
        db.commit()
        report = ShiftReport(
            schedule_id=schedule_id,
            latest_run_id=run.id,
            work_date=previous_month,
            start_time=schedule.start_time,
            end_time=schedule.end_time,
            host_name=schedule.host_name,
            shop_name=schedule.shop_name,
            live_room_id=schedule.live_room_id,
            bitable_record_id="rec-cold-month",
            bitable_status="synced",
        )
        db.add(report)
        db.commit()
        report_id = report.id
    settings = {
        "feishu_app_id": "app",
        "feishu_app_secret": "secret",
        "bitable_app_token": "base",
        "bitable_table_id": "table",
    }
    try:
        dispatcher = RunDispatcher(settings_store=None)
        archived, failed = asyncio.run(dispatcher._archive_cold_bitable_records(settings))
        assert archived == 0
        assert failed == []
        assert deleted_record_ids == []
        with SessionLocal() as db:
            historical = db.get(ShiftReport, report_id)
            assert historical is not None
            assert historical.bitable_record_id == "rec-cold-month"
            assert historical.bitable_status == "synced"
    finally:
        with SessionLocal() as db:
            historical = db.get(ShiftReport, report_id)
            if historical:
                db.delete(historical)
            run = db.get(CollectionRun, "cold-data-run")
            if run:
                db.delete(run)
            schedule = db.get(ShiftSchedule, schedule_id)
            if schedule:
                db.delete(schedule)
            db.commit()
