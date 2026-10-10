import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from types import SimpleNamespace

import pytest

from liveops.formulas import build_report_metrics
from liveops.portal import APP_TIMEZONE
from liveops.snapshots import (
    aggregate_daily_room_exposure,
    capture_snapshot,
    combine_handoffs,
    handoff_source,
)


def at(hour, minute=0, second=0):
    return datetime(2098, 3, 15, hour, minute, second, tzinfo=APP_TIMEZONE)


def room(room_id="old", start=None, end=None):
    return {"live_room_id": room_id, "author_id": "author", "live_start": (start or at(7)).isoformat(),
            "live_end": end.isoformat() if end else None, "is_living": end is None}


def snapshot(room_id="old", gmv=1000, cost=500, captured=None):
    return {"live_room_id": room_id, "qianchuan_account_id": "ad", "captured_at": (captured or at(11, 31)).isoformat(),
            "counters": {"gmv": gmv, "paid_users": gmv / 100, "product_click_users": gmv / 10,
                         "viewers": gmv, "new_followers": gmv / 100},
            "qianchuan": {"stat_cost_for_roi2": cost, "live_show_count_for_roi2": cost * 10,
                          "total_pay_order_coupon_amount_realtime_for_roi2": cost / 10,
                          "total_ecom_platform_subsidy_amount_realtime_for_roi2": cost / 100},
            "cards": {"gpm": 2000, "interaction_rate": .1, "stay_seconds": 120}}


def segment(snap, selected, baseline, start, end, reset=False):
    return {"room": selected, "snapshot": snap, "responses": [], "captured_at": snap["captured_at"],
            "qianchuan_account_id": "ad", "screenshots": [], "page_errors": [],
            "source": handoff_source(snapshot=snap, room=selected, baseline=baseline,
                                      start=start, end=end, allow_new_room=reset)}


def test_normal_61_second_collection_delay_has_all_fields_and_no_warning():
    source = segment(snapshot(captured=at(10, 1, 1)), room(start=at(6, 10)),
                     {"source": "first_shift_zero"}, at(6), at(10))["source"]
    metrics, missing = build_report_metrics(work_date=date(2098, 3, 15), start_time=time(6), end_time=time(10),
        host_name="A", promoter="", source=source, include_daily_exposure=False)
    assert missing == []
    assert metrics["paid_users"] == 10
    assert metrics["interaction_rate"] == .1
    assert metrics["stay_seconds"] == 120
    assert source["quality_issues"] == []
    assert source["end_snapshot_valid"]


def test_same_room_uses_previous_actual_snapshot_for_all_counters():
    prior = {**snapshot(), "source": "previous_shift_end"}
    source = segment(snapshot(gmv=1400, cost=700, captured=at(16, 1)), room(), prior, at(11, 30), at(16))["source"]
    assert source["gmv"] == 400
    assert source["spend"] == 200
    assert source["paid_users"] == 4
    assert source["viewers"] == 400
    assert source["new_followers"] == 4
    assert source["quality_issues"] == []


def test_unmarked_room_change_requires_manual_interruption_and_wrong_account_is_rejected():
    with pytest.raises(RuntimeError, match="标记中断"):
        segment(snapshot("new"), room("new", at(14)), snapshot(), at(11, 30), at(16))
    with pytest.raises(RuntimeError, match="广告账户"):
        segment(snapshot(), room(), {**snapshot(), "qianchuan_account_id": "other"}, at(11, 30), at(16))


def test_missing_baseline_and_negative_counter_never_turn_into_zero():
    source = segment(snapshot(), room(), {}, at(11, 30), at(16))["source"]
    assert source["gmv"] is None and "gmv" in source["baseline_missing"]
    source = segment(snapshot(gmv=900), room(), snapshot(), at(11, 30), at(16))["source"]
    assert source["paid_users"] is None and "paid_users" in source["baseline_conflicts"]


def test_manual_fragments_sum_once_and_following_host_only_uses_new_room_snapshot():
    old = segment(snapshot(gmv=1400, cost=700), room(end=at(13, 47)), snapshot(), at(11, 30), at(13, 47))
    new = segment(snapshot("new", 300, 150), room("new", at(14)), snapshot(), at(13, 47), at(16), True)
    combined = combine_handoffs([old, new])
    assert combined["gmv"] == 700 and combined["spend"] == 350
    assert combined["paid_users"] == 7 and combined["viewers"] == 700
    assert combined["card_scope"] == "room_end_snapshot"
    from liveops.service import _baseline_from_report
    baseline = _baseline_from_report(SimpleNamespace(raw_json={"is_final": True, "segments": [old, new]}, latest_run_id="B"))
    assert baseline["live_room_id"] == "new" and baseline["counters"]["gmv"] == 300
    third = segment(snapshot("new", 900, 450), room("new", at(14)), baseline, at(16), at(20, 30))
    assert third["source"]["gmv"] == 600 and third["source"]["spend"] == 300
    with pytest.raises(RuntimeError, match="重复"):
        combine_handoffs([old, old, new])


def test_daily_exposure_uses_latest_snapshot_per_room_and_sums_interrupted_rooms():
    snapshots = [
        snapshot("old", cost=500, captured=at(11, 31)),
        snapshot("old", cost=700, captured=at(13, 48)),
        snapshot("new", cost=150, captured=at(16, 1)),
        snapshot("new", cost=450, captured=at(20, 31)),
    ]
    segments = [
        {"room": {"live_room_id": item["live_room_id"]}, "snapshot": item,
         "captured_at": item["captured_at"], "qianchuan_account_id": "ad"}
        for item in snapshots
    ]

    result = aggregate_daily_room_exposure(segments)

    assert result["value"] == 11500
    assert {row["live_room_id"]: row["exposure"] for row in result["rooms"]} == {
        "old": 7000,
        "new": 4500,
    }
    assert result["issues"] == []


def test_daily_exposure_stays_blank_when_any_room_has_no_final_exposure():
    known = snapshot("known", cost=100, captured=at(16, 1))
    unknown = snapshot("unknown", cost=100, captured=at(17, 1))
    unknown["qianchuan"].pop("live_show_count_for_roi2")

    result = aggregate_daily_room_exposure([
        {"room": {"live_room_id": item["live_room_id"]}, "snapshot": item,
         "captured_at": item["captured_at"], "qianchuan_account_id": "ad"}
        for item in (known, unknown)
    ])

    assert result["value"] is None
    assert any("缺少有效的整体曝光快照" in issue for issue in result["issues"])


def test_last_roster_schedule_can_end_after_midnight_without_splitting_room_exposure():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from liveops.db import Base
    from liveops.models import ShiftSchedule
    from liveops.service import _is_last_schedule_for_date

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        evening = ShiftSchedule(
            work_date=at(7).date(), start_time=time(18), end_time=time(22),
            host_name="晚班", live_room_id="room-a", enabled=True,
        )
        overnight = ShiftSchedule(
            work_date=at(7).date(), start_time=time(22), end_time=time(2),
            host_name="夜班", live_room_id="room-b", enabled=True,
        )
        db.add_all([evening, overnight])
        db.flush()

        assert not _is_last_schedule_for_date(db, evening)
        assert _is_last_schedule_for_date(db, overnight)
    engine.dispose()


def test_provisional_report_cannot_supply_next_baseline():
    from liveops.service import _baseline_from_report
    assert _baseline_from_report(SimpleNamespace(raw_json={"is_final": False})) is None


def test_ended_previous_room_new_session_at_next_shift_starts_from_zero():
    baseline = {**snapshot(), "source": "previous_shift_end", "live_end": at(13, 47).isoformat()}
    result = segment(snapshot("new", 300, 150), room("new", at(16, 5)), baseline, at(16), at(20, 30))
    assert result["source"]["gmv"] == 300
    assert result["source"]["spend"] == 150


async def test_collector_reuses_saved_interruption_without_recapturing_old_room(monkeypatch):
    import liveops.collector as collector
    old = segment(snapshot(gmv=1400, cost=700), room(end=at(13, 47)), snapshot(), at(11, 30), at(13, 47))
    old["screenshots"] = [{"name": "旧场漏斗", "live_room_id": "old"}]
    new_room = room("new", at(14))
    calls, delivered = [], []

    @asynccontextmanager
    async def context(_settings):
        yield None, None, object()

    async def login(*args):
        pass

    async def overview(*args):
        return [new_room], []

    async def capture(_context, **kwargs):
        calls.append(kwargs["room_id"])
        return [], [{"name": "新场漏斗"}], []

    async def delivery(images):
        delivered.append(images)

    monkeypatch.setattr(collector, "browser_context", context)
    monkeypatch.setattr(collector, "ensure_shop_login", login)
    monkeypatch.setattr(collector, "_overview_data", overview)
    monkeypatch.setattr(collector, "_capture_report_pages", capture)
    monkeypatch.setattr(collector, "_automatic_room_for", lambda _: True)
    monkeypatch.setattr(collector, "confirmed_room", lambda _responses, selected: selected)
    monkeypatch.setattr(collector, "capture_snapshot", lambda _r, room_id, _ad, _stamp: snapshot(room_id, 300, 150))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at(16, 1)
    monkeypatch.setattr(collector, "datetime", Clock)
    schedule = SimpleNamespace(id=1, work_date=at(7).date(), start_time=time(11, 30), end_time=time(16), live_room_id="old", qianchuan_account_id="ad")
    result = await collector.collect_report(settings={"live_author_id": "author"}, schedule=schedule,
        modules=[], run_id="test", baseline=snapshot(), include_daily_exposure=False,
        interruption_segments=[old], on_captured=delivery)
    assert calls == ["new"]
    assert result["source"]["gmv"] == 700
    assert len(delivered) == 1 and len(delivered[0]) == 2
    assert result["segments"][-1]["snapshot"]["counters"]["gmv"] == 300


def test_capture_snapshot_uses_user_payment_and_checks_raw_scope():
    responses = [{"source": "compass", "live_room_id": "old", "path_full": "/live_screen/core_data",
                  "data": {"pay_amt_all": {"value": 99999}, "core_data": [
                      {"index_name": "pay_amt", "value": {"value": 12000}},
                      {"index_name": "pay_ucnt", "value": {"value": 3}}]}}]
    captured = capture_snapshot(responses, "old", "ad", at(10, 1).isoformat())
    assert captured["counters"]["gmv"] == 120
    with pytest.raises(RuntimeError, match="场次"):
        capture_snapshot(responses, "new", "ad", at(10).isoformat())


def test_readiness_waits_for_user_payment_instead_of_combined_amount():
    from liveops.portal import missing_page_metrics

    data = {"pay_amt_all": {"value": 99999}, "core_data": []}
    responses = [{"source": "compass", "path_full": "/live_screen/core_data", "data": data}]
    assert "GMV" in missing_page_metrics(responses, "pro")
    data["core_data"].append({"index_name": "pay_amt", "value": {"value": 10000}})
    assert "GMV" not in missing_page_metrics(responses, "pro")


async def test_dispatcher_interruption_retry_restart_and_next_shift_handoff(monkeypatch):
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    import liveops.service as service
    from liveops.db import Base
    from liveops.models import CollectionRun, ShiftInterruption, ShiftReport, ShiftSchedule

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service, "db_settings", lambda *args: {"settle_delay_seconds": "0"})
    clock = [at(11, 31)]
    monkeypatch.setattr(service, "now_local", lambda: clock[0])

    sync_calls = []
    daily_collection_flags = []

    async def sync(_self, *args, **kwargs):
        sync_calls.append((args[0], kwargs.get("send_feishu", True)))

    async def collect(**kwargs):
        daily_collection_flags.append((kwargs["schedule"].host_name, kwargs["include_daily_exposure"]))
        schedule = kwargs["schedule"]
        start, end = service.schedule_start(schedule), kwargs["interrupted_at"] or service.schedule_end(schedule)
        if schedule.host_name == "A":
            snap, selected = snapshot(), room()
        elif kwargs["interrupted_at"]:
            snap, selected = snapshot(gmv=1400, cost=700, captured=clock[0]), room(end=at(13, 47))
        else:
            snap, selected = snapshot("new", 300 if schedule.host_name == "B" else 900,
                                      150 if schedule.host_name == "B" else 450, clock[0]), room("new", at(14))
        saved = [] if kwargs["interrupted_at"] else kwargs["interruption_segments"]
        current = segment(snap, selected, kwargs["baseline"], start, end, bool(saved))
        current["responses"] = [{"test": "raw"}]
        parts = [*saved, current]
        return {"segments": parts, "responses": [{"test": "raw"}], "source": combine_handoffs(parts),
                "screenshots": [], "page_errors": [], "schedule_start": start.isoformat(), "schedule_end": end.isoformat(),
                "collected_at": clock[0].isoformat(), "live_room_id": snap["live_room_id"],
                "is_final": not kwargs["interrupted_at"], "measurement_mode": "manual_handoffs"}

    monkeypatch.setattr(service, "collect_report", collect)
    monkeypatch.setattr(service.RunDispatcher, "_sync_integrations", sync)
    ids = []
    with sessions() as db:
        for name, start, end in [("A", time(7), time(11, 30)), ("B", time(11, 30), time(16)), ("C", time(16), time(20, 30))]:
            row = ShiftSchedule(work_date=at(7).date(), start_time=start, end_time=end, host_name=name,
                                live_room_id="old", qianchuan_account_id="ad")
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
    dispatcher = service.RunDispatcher(None)

    async def run(which, kind="report", interrupted=None):
        run = await dispatcher.begin_run(ids[which], kind, "user", interrupted)
        await asyncio.gather(*list(dispatcher._tasks))
        with sessions() as db:
            result = db.get(CollectionRun, run.id)
            assert result.status == "succeeded", result.message
        return run

    await run(0)
    clock[0] = at(13, 48)
    await run(1, "interruption", at(13, 47))
    await run(1, "interruption", at(13, 47))
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ShiftInterruption)) == 1
        assert db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == ids[1])) is None
        assert db.get(ShiftSchedule, ids[2]).baseline_json["source"] == "previous_shift_missing"
    # A fresh dispatcher reads the persisted interruption, not in-memory state.
    dispatcher = service.RunDispatcher(None)
    clock[0] = at(16, 1)
    await run(1)
    with sessions() as db:
        second = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == ids[1]))
        assert second.metrics_json["gmv"] == 700
        assert second.metrics_json["spend"] == 350
        assert db.get(ShiftSchedule, ids[2]).baseline_json["counters"]["gmv"] == 300
    clock[0] = at(20, 31)
    sync_calls.clear()
    await run(2)
    with sessions() as db:
        third = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == ids[2]))
        assert third.metrics_json["gmv"] == 600 and third.metrics_json["spend"] == 300
        reports = db.scalars(select(ShiftReport).order_by(ShiftReport.schedule_id)).all()
        assert [row.metrics_json["daily_exposure"] for row in reports] == [11500, 11500, 11500]
        assert all(row.raw_json["source"]["daily_exposure"] == 11500 for row in reports)
        assert all(row.missing_fields.count("daily_exposure") == 0 for row in reports)
    assert {schedule_id for schedule_id, _ in sync_calls} == set(ids)
    assert [schedule_id for schedule_id, send_feishu in sync_calls if send_feishu] == [ids[2]]
    assert daily_collection_flags[0] == ("A", False)
    assert daily_collection_flags[-1] == ("C", True)
    assert all(not include for _, include in daily_collection_flags[1:-1])
    engine.dispose()
