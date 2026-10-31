from __future__ import annotations

import asyncio
import copy
from datetime import date, datetime, time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import liveops.hourly as hourly
import liveops.monthly as monthly
import liveops.service as service
from liveops.bitable import BitableClient
from liveops.db import Base
from liveops.feishu import FeishuError
from liveops.models import (
    BitableMonthTable,
    BitableSyncReceipt,
    CollectionRun,
    ShiftHourDetail,
    ShiftHourState,
    ShiftReport,
    ShiftSchedule,
)
from liveops.portal import APP_TIMEZONE
from liveops.security import SecretBox
from liveops.settings import DEFAULT_SETTINGS, SECRET_KEYS, SettingsStore, db_settings


@pytest.fixture
def state(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    store = SettingsStore(SecretBox(Fernet.generate_key().decode()))
    config = {**DEFAULT_SETTINGS, "feishu_app_id": "test-app", "feishu_app_secret": "test-secret",
              "bitable_app_token": "test-base", "bitable_table_id": "legacy-table"}
    with sessions() as db:
        for key, value in config.items():
            if key not in SECRET_KEYS or value:
                store.set(db, key, value)
        db.commit()
    clock = [datetime(2026, 10, 31, 12, tzinfo=APP_TIMEZONE)]
    remote, requests, deletes, notifications, forbidden = {}, [], [], [], []
    faults = {"create": "", "write": False}

    def add_table(key, name="operator table", fields=None):
        remote[key] = {"name": name, "fields": copy.deepcopy(fields or monthly.month_fields(config)),
                       "records": {}, "views": [{"view_id": f"view-{key}", "view_name": "原有视图", "view_type": "grid"}]}
        for index, field in enumerate(remote[key]["fields"]):
            field.setdefault("field_id", f"field-{index}")

    add_table("legacy-table")
    next(field for field in remote["legacy-table"]["fields"] if field["field_name"] == "GMV")["property"] = {"formatter": "0.00"}

    class Client(BitableClient):
        async def _request(self, method, path, **kwargs):
            await asyncio.sleep(0)
            body = kwargs.get("json") or {}
            requests.append((method, path, copy.deepcopy(body)))
            root = f"/bitable/v1/apps/{self.app_token}/tables"
            if path == root:
                if method == "GET":
                    return {"data": {"items": [{"table_id": key, "name": row["name"]} for key, row in remote.items()]}}
                if method == "POST":
                    if faults["create"] == "before":
                        raise httpx.ReadTimeout("creation response unknown")
                    key = f"auto-{len(remote)}"
                    add_table(key, body["table"]["name"], body["table"]["fields"])
                    if faults["create"] == "after":
                        raise httpx.ReadTimeout("created but response lost")
                    return {"data": {"table_id": key, "default_view_id": f"view-{key}"}}
            key = path.removeprefix(root + "/").split("/", 1)[0]
            if method == "DELETE" and path == f"{root}/{key}":
                deletes.append(key)
                remote.pop(key, None)
                return {"data": {}}
            if key not in remote:
                raise FeishuError("test target does not exist")
            row = remote[key]
            if path.endswith("/fields"):
                if method == "POST":
                    field = {**body, "field_id": f"field-{len(row['fields'])}"}
                    row["fields"].append(field)
                    return {"data": {"field": field}}
                return {"data": {"items": row["fields"]}}
            if path.endswith("/views"):
                return {"data": {"items": row["views"]}}
            if "/views/" in path:
                assert method == "PATCH"
                return {"data": {}}
            if path.endswith("/records/search"):
                source = body["filter"]["conditions"][0]
                return {"data": {"items": [{"record_id": record_id} for record_id, fields in row["records"].items()
                    if fields.get(source["field_name"]) == source["value"][0]]}}
            if path.endswith("/records") and method == "POST":
                if faults["write"]:
                    raise httpx.ReadTimeout("write response unknown")
                record_id = f"rec-{len(requests)}"
                row["records"][record_id] = body["fields"]
                return {"data": {"record": {"record_id": record_id}}}
            if "/records/" in path and method == "PUT":
                if faults["write"]:
                    raise httpx.ReadTimeout("write response unknown")
                record_id = path.rsplit("/", 1)[1]
                assert record_id in row["records"], "record IDs must never be reused across tables"
                row["records"][record_id].update(body["fields"])
                return {"data": {"record": {"record_id": record_id}}}
            raise AssertionError((method, path, body))

    async def notify(self, run_id, settings):
        notifications.append(run_id)

    async def never_collect(*args, **kwargs):
        forbidden.append("collect")
        raise AssertionError("range sync must not recollect")

    async def never_send(*args, **kwargs):
        forbidden.append("send")
        raise AssertionError("range sync must not send screenshots")

    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service, "now_local", lambda: clock[0])
    monkeypatch.setattr(service, "collect_report", never_collect)
    monkeypatch.setattr(service.RunDispatcher, "_send_feishu_images", never_send)
    monkeypatch.setattr(service.RunDispatcher, "_notify_run_problem", notify)
    for module in (monthly, service, hourly):
        monkeypatch.setattr(module, "BitableClient", Client)

    def settings():
        with sessions() as db:
            return db_settings(db, store)

    def manual(table_id):
        with sessions() as db:
            previous = {key: store.get(db, key) for key in ("bitable_app_token", "bitable_table_id")}
            store.set(db, "bitable_table_id", table_id)
            db.flush()
            monthly.remember_manual_table(db, store, {"bitable_table_id": table_id}, clock[0].date(), previous)
            db.commit()

    yield SimpleNamespace(db=sessions, store=store, settings=settings, clock=clock, remote=remote,
        requests=requests, deletes=deletes, notifications=notifications, faults=faults,
        add_table=add_table, manual=manual, forbidden=forbidden)
    engine.dispose()


def test_hot_window_is_current_month_plus_previous_three_across_year_boundaries():
    assert monthly.hot_month_start(date(2026, 1, 31)) == date(2025, 10, 1)
    assert monthly.hot_month_start(date(2026, 11, 1)) == date(2026, 8, 1)


async def test_default_month_table_manual_override_rollover_and_historical_retry_use_distinct_targets(state):
    dispatcher = service.RunDispatcher(state.store)
    october = await dispatcher._bitable_settings_for_date(state.settings(), date(2026, 10, 31))
    assert october["bitable_table_id"] != "legacy-table" and october["bitable_table_mode"] == "automatic"
    assert next(field for field in state.remote[october["bitable_table_id"]]["fields"] if field["field_name"] == "GMV")["property"]["formatter"] == "0.00"
    state.add_table("manual-october")
    state.manual("manual-october")
    assert (await dispatcher._bitable_settings_for_date(october, date(2026, 10, 31)))["bitable_table_id"] == "manual-october"
    state.manual("")
    restored = await dispatcher._bitable_settings_for_date(state.settings(), date(2026, 10, 31))
    assert restored["bitable_table_id"] == october["bitable_table_id"] and restored["bitable_table_mode"] == "automatic"
    state.manual("manual-october")
    state.clock[0] = datetime(2026, 11, 1, 0, 1, tzinfo=APP_TIMEZONE)
    november = await dispatcher._bitable_settings_for_date(state.settings(), date(2026, 11, 1))
    assert november["bitable_table_id"] not in {"legacy-table", "manual-october", october["bitable_table_id"]}
    assert state.settings()["bitable_table_month"] == "2026-11"
    again = await dispatcher._bitable_settings_for_date(state.settings(), date(2026, 10, 31))
    assert again["bitable_table_id"] == "manual-october"
    assert state.settings()["bitable_table_id"] == november["bitable_table_id"]
    results = await asyncio.gather(*(dispatcher._bitable_settings_for_date(state.settings(), date(2026, 9, 30)) for _ in range(3)))
    assert len({row["bitable_table_id"] for row in results}) == 1
    restarted = service.RunDispatcher(state.store)
    assert (await restarted._bitable_settings_for_date(state.settings(), date(2026, 11, 1)))["bitable_table_id"] == november["bitable_table_id"]
    assert len([item for method, path, item in state.requests if method == "POST" and path.endswith("/tables")]) == 3


async def test_lost_create_response_is_recovered_from_saved_unique_name_without_creating_twice(state):
    dispatcher = service.RunDispatcher(state.store)
    state.faults["create"] = "after"
    with pytest.raises(httpx.ReadTimeout):
        await dispatcher._bitable_settings_for_date(state.settings(), state.clock[0].date())
    assert len(state.remote) == 2
    state.faults["create"] = ""
    target = await dispatcher._bitable_settings_for_date(state.settings(), state.clock[0].date())
    assert target["bitable_table_id"] != "legacy-table"
    assert len([1 for method, path, _ in state.requests if method == "POST" and path.endswith("/tables")]) == 1


async def test_uncertain_creation_does_not_issue_another_post_and_manual_target_can_recover(state):
    dispatcher = service.RunDispatcher(state.store)
    state.faults["create"] = "before"
    with pytest.raises(httpx.ReadTimeout):
        await dispatcher._bitable_settings_for_date(state.settings(), state.clock[0].date())
    state.faults["create"] = ""
    with pytest.raises(FeishuError, match="待确认"):
        await dispatcher._bitable_settings_for_date(state.settings(), state.clock[0].date())
    state.manual("legacy-table")
    assert (await dispatcher._bitable_settings_for_date(state.settings(), state.clock[0].date()))["bitable_table_id"] == "legacy-table"


async def test_retention_deletes_only_proven_robot_tables_outside_four_months(state):
    state.clock[0] = datetime(2026, 11, 15, 12, tzinfo=APP_TIMEZONE)
    settings = state.settings()
    with state.db() as db:
        for month, key, owned in [("2026-05", "manual-old", False), ("2026-06", "robot-june", True),
                ("2026-07", "robot-july", True), ("2026-08", "robot-august", True), ("2026-11", "robot-november", True)]:
            state.add_table(key)
            db.add(BitableMonthTable(base_scope=monthly.base_scope(settings), month_key=month, table_id=key,
                manual_override=not owned, tables_json=[{"table_id": key, "managed": owned, "status": "active"}]))
        schedule = ShiftSchedule(work_date=date(2026, 6, 1), start_time=time(7), end_time=time(9),
            host_name="测试主播", live_room_id="room", baseline_json={"source": "first_shift_zero"})
        db.add(schedule)
        db.flush()
        db.add(CollectionRun(id="cold-source", schedule_id=schedule.id, kind="report", trigger="scheduler", status="succeeded"))
        db.flush()
        scope = monthly.table_scope({**settings, "bitable_table_id": "robot-june"})
        report = ShiftReport(schedule_id=schedule.id, latest_run_id="cold-source", work_date=schedule.work_date,
            start_time=time(7), end_time=time(9), host_name="测试主播", shop_name="test", live_room_id="room",
            bitable_record_id="parent-cold", metrics_json={"gmv": 123.45},
            raw_json={"bitable_scope": scope, "responses": [{"fixture": True}]})
        db.add(report)
        db.add(ShiftHourState(schedule_id=schedule.id, bitable_scope=scope, parent_record_id="parent-cold",
            initial_baseline_json={"source": "first_shift_zero"}))
        db.add(ShiftHourDetail(schedule_id=schedule.id, period_index=1, status="succeeded",
            start_at=datetime(2026, 6, 1, 7, tzinfo=APP_TIMEZONE), end_at=datetime(2026, 6, 1, 8, tzinfo=APP_TIMEZONE),
            metrics_json={"gmv": 45}, raw_json={"end_baseline": {"counters": {"gmv": 45}}},
            bitable_scope=scope, bitable_record_id="child-cold"))
        db.commit()
        report_id, schedule_id = report.id, schedule.id
    settings["bitable_table_id"] = "robot-november"
    deleted, failures = await service.RunDispatcher(state.store)._archive_cold_bitable_records(settings)
    assert deleted == 2 and not failures
    assert set(state.deletes) == {"robot-june", "robot-july"}
    assert {"legacy-table", "manual-old", "robot-august", "robot-november"} <= set(state.remote)
    with state.db() as db:
        retained = db.get(ShiftReport, report_id)
        assert retained.metrics_json == {"gmv": 123.45} and retained.raw_json["responses"] == [{"fixture": True}]
        assert retained.bitable_record_id == "" and retained.bitable_status.startswith("archived")
        assert db.get(ShiftSchedule, schedule_id).baseline_json == {"source": "first_shift_zero"}
        assert db.get(ShiftHourState, schedule_id).initial_baseline_json == {"source": "first_shift_zero"}
        assert db.scalar(select(ShiftHourDetail)).raw_json["end_baseline"]["counters"]["gmv"] == 45
    assert (await service.RunDispatcher(state.store)._archive_cold_bitable_records(settings))[0] == 0


async def test_receipts_skip_unchanged_update_changes_force_refresh_and_never_mix_record_ids(state):
    dispatcher = service.RunDispatcher(state.store)
    settings = state.settings()
    client = dispatcher._month_client(settings)
    mapping, values = {"gmv": "GMV"}, {"gmv": 100, "unmapped_debug": "ignored"}
    try:
        first = await dispatcher._write_bitable_record(client, settings, values=values, field_mapping=mapping, sync_key="shift-1")
        same = await dispatcher._write_bitable_record(client, settings, values={**values, "unmapped_debug": "changed"}, field_mapping=mapping, sync_key="shift-1")
        assert same["skipped"] and same["record_id"] == first["record_id"]
        await dispatcher._write_bitable_record(client, settings, values={"gmv": 120}, field_mapping=mapping, sync_key="shift-1")
        forced = await dispatcher._write_bitable_record(client, {**settings, "_bitable_force": "true"},
            values={"gmv": 120}, field_mapping=mapping, sync_key="shift-1")
        assert "skipped" not in forced
    finally:
        await client.close()
    state.add_table("other-table")
    other_settings = {**settings, "bitable_table_id": "other-table"}
    client = dispatcher._month_client(other_settings)
    try:
        second = await dispatcher._write_bitable_record(client, other_settings, values={"gmv": 120}, field_mapping=mapping, sync_key="shift-1")
        assert second["record_id"] != first["record_id"]
    finally:
        await client.close()
    state.remote["legacy-table"]["records"].clear()
    client = dispatcher._month_client(settings)
    try:
        restored = await dispatcher._write_bitable_record(client, {**settings, "_bitable_force": "true"},
            values={"gmv": 120}, field_mapping=mapping, sync_key="shift-1")
        assert restored["record_id"] in state.remote["legacy-table"]["records"]
    finally:
        await client.close()
    with state.db() as db:
        assert len(db.scalars(select(BitableSyncReceipt)).all()) == 2


async def test_failed_full_refresh_remains_retryable_by_incremental_sync(state):
    dispatcher, settings = service.RunDispatcher(state.store), state.settings()
    client = dispatcher._month_client(settings)
    try:
        options = {"values": {"gmv": 100}, "field_mapping": {"gmv": "GMV"}, "sync_key": "shift-1"}
        await dispatcher._write_bitable_record(client, settings, **options)
        state.faults["write"] = True
        with pytest.raises(httpx.ReadTimeout):
            await dispatcher._write_bitable_record(client, {**settings, "_bitable_force": "true"}, **options)
        state.faults["write"] = False
        assert not (await dispatcher._write_bitable_record(client, settings, **options)).get("skipped")
    finally:
        await client.close()


async def drain(dispatcher):
    while dispatcher._tasks:
        await asyncio.gather(*list(dispatcher._tasks))


async def test_range_sync_routes_each_month_and_supports_incremental_and_full_parent_child_updates(state):
    state.clock[0] = datetime(2026, 11, 15, 12, tzinfo=APP_TIMEZONE)
    with state.db() as db:
        for index, day in enumerate([date(2026, 10, 31), date(2026, 11, 1)]):
            schedule = ShiftSchedule(work_date=day, start_time=time(7), end_time=time(9), host_name="测试主播", live_room_id="test-room")
            db.add(schedule)
            db.flush()
            source = CollectionRun(id=f"source-{index}", schedule_id=schedule.id, kind="report", trigger="scheduler", status="succeeded")
            db.add(source)
            db.flush()
            db.add(ShiftReport(schedule_id=schedule.id, latest_run_id=source.id, work_date=day,
                start_time=time(7), end_time=time(9), host_name=schedule.host_name, shop_name="test", live_room_id="test-room",
                metrics_json={"gmv": 100, "duration_hours": 2, "time_range": "07:00-09:00"}, raw_json={"is_final": True}))
            db.add(ShiftHourDetail(schedule_id=schedule.id, period_index=1, start_at=datetime.combine(day, time(7), APP_TIMEZONE),
                end_at=datetime.combine(day, time(8), APP_TIMEZONE), status="succeeded", latest_run_id=source.id,
                metrics_json={"gmv": 40}, raw_json={"end_baseline": {"counters": {"gmv": 40}}}))
        db.commit()
    dispatcher = service.RunDispatcher(state.store)
    run = await dispatcher.begin_bitable_range_sync(date(2026, 10, 31), date(2026, 11, 1), "incremental")
    with pytest.raises(RuntimeError, match="运行中"):
        await dispatcher.begin_bitable_range_sync(date(2026, 10, 31), date(2026, 11, 1), "full")
    await drain(dispatcher)
    with state.db() as db:
        saved_run = db.get(CollectionRun, run.id)
        assert saved_run.status == "succeeded", (
            [row.bitable_status for row in db.scalars(select(ShiftReport))],
            [row.bitable_status for row in db.scalars(select(ShiftHourDetail))],
            [step["description"] for step in saved_run.result_json.get("steps", []) if step.get("status") == "failed"],
        )
        assert all(row.bitable_status == "synced" for row in db.scalars(select(ShiftHourDetail)))
    populated = [row for row in state.remote.values() if row["records"]]
    assert len(populated) == 2 and all(len(row["records"]) == 2 for row in populated)
    def writes():
        return [(method, path) for method, path, _ in state.requests
                if method in {"POST", "PUT"} and "/records" in path and not path.endswith("search")]
    before = len(writes())
    await dispatcher.begin_bitable_range_sync(date(2026, 10, 31), date(2026, 11, 1), "incremental")
    await drain(dispatcher)
    assert len(writes()) == before
    await dispatcher.begin_bitable_range_sync(date(2026, 10, 31), date(2026, 11, 1), "full")
    await drain(dispatcher)
    assert len(writes()) > before and all(len(row["records"]) == 2 for row in populated)
    assert not state.forbidden
    assert len(state.notifications) == 3
