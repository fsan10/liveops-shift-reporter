from contextlib import asynccontextmanager
from datetime import datetime, time
from types import SimpleNamespace

import pytest

import liveops.collector as collector
from liveops.portal import APP_TIMEZONE


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=APP_TIMEZONE)


def room(room_id="101", start=None, end=None):
    return {"live_room_id": room_id, "author_id": "author", "is_self": True,
            "live_start": (start or at(10, 7)).isoformat(),
            "live_end": end.isoformat() if end else None, "is_living": end is None}


def schedule(room_id=""):
    return SimpleNamespace(id=52, work_date=at(10, 7).date(), start_time=time(20, 30),
                           end_time=time(1), live_room_id=room_id, qianchuan_account_id="ad")


def saved_room(room_id="101", end=None):
    end = end or at(10, 22)
    return {"room": room(room_id, end=end), "source": {"segment_end": end.isoformat()}}


async def resolve(monkeypatch, *, pin="", baseline=None, saved=None, interrupted=None):
    monkeypatch.setattr(collector, "_automatic_room_for", lambda _: True)
    return await collector._report_room(
        None, settings={"live_author_id": "author"}, schedule=schedule(pin),
        baseline=baseline, saved=saved or [], start=at(10, 20, 30), end=at(11, 1),
        interrupted_at=interrupted,
    )


@pytest.mark.parametrize("use_schedule", [True, False])
async def test_midnight_uses_bound_room_without_requiring_online_discovery(monkeypatch, use_schedule):
    async def unavailable(*args):
        pytest.fail("Bound room must not query realtime or default-day history")

    monkeypatch.setattr(collector, "_overview_data", unavailable)
    value = await resolve(monkeypatch, pin="101" if use_schedule else "",
                          baseline={"live_room_id": "101", "author_id": "author",
                                    "qianchuan": {}})
    assert value["rooms"][0]["live_room_id"] == "101"
    assert value["source"] == ("schedule_room" if use_schedule else "handoff_room")


async def test_initial_offline_discovery_queries_both_business_dates(monkeypatch):
    calls = []

    async def overview(_context, start, end):
        calls.append((start, end))
        return [], [room(end=at(11, 0, 56))]

    monkeypatch.setattr(collector, "_overview_data", overview)
    value = await resolve(monkeypatch)
    assert calls == [(at(10, 20, 30), at(11, 1))]
    assert value["rooms"][0]["live_room_id"] == "101"
    assert value["rooms"][0]["is_living"] is False


async def test_manual_mark_unlocks_old_room_and_finds_ended_restart(monkeypatch):
    async def overview(_context, start, end):
        return [], [room(end=at(10, 22)), room("102", at(10, 22, 10), at(11, 0, 56))]

    monkeypatch.setattr(collector, "_overview_data", overview)
    value = await resolve(monkeypatch, pin="101", saved=[saved_room()])
    assert value["source"] == "manual_restart"
    assert value["rooms"][0]["live_room_id"] == "102"


async def test_restart_binding_survives_repeat_capture_and_old_mark_retry(monkeypatch):
    async def unavailable(*args):
        pytest.fail("Confirmed restart/retried old mark must not discover another online room")

    monkeypatch.setattr(collector, "_overview_data", unavailable)
    value = await resolve(monkeypatch, pin="102", saved=[saved_room()])
    assert value["rooms"][0]["live_room_id"] == "102"
    value = await resolve(monkeypatch, pin="102", saved=[saved_room()], interrupted=at(10, 22))
    assert value["rooms"][0]["live_room_id"] == "101"
    assert value["source"] == "interruption_retry"


async def test_multiple_unmarked_restarts_are_not_silently_replaced_by_latest(monkeypatch):
    async def overview(*args):
        return [], [room("102", at(10, 22, 10), at(10, 23)),
                    room("103", at(10, 23, 10), at(11, 0, 56))]

    monkeypatch.setattr(collector, "_overview_data", overview)
    with pytest.raises(RuntimeError, match="逐场标记"):
        await resolve(monkeypatch, pin="101", saved=[saved_room()])


async def test_mark_without_restart_finalizes_saved_room_only_when_offline(monkeypatch):
    async def overview(*args):
        return [], [room(end=at(10, 22))]

    monkeypatch.setattr(collector, "_overview_data", overview)
    value = await resolve(monkeypatch, pin="101", saved=[saved_room()])
    assert value["source"] == "interruption_without_restart"

    async def still_live(*args):
        return [room()], []

    monkeypatch.setattr(collector, "_overview_data", still_live)
    with pytest.raises(RuntimeError, match="未找到可归属"):
        await resolve(monkeypatch, pin="101", saved=[saved_room()])


async def test_full_collection_confirms_ended_bound_room_and_same_room_delta(monkeypatch):
    confirmed = []
    old = room(end=at(11, 0, 56))

    @asynccontextmanager
    async def context(_settings):
        yield None, None, object()

    async def login(*args):
        pass

    async def unavailable(*args):
        pytest.fail("Offline final collection cannot require realtime discovery")

    async def capture(_context, **kwargs):
        assert kwargs["room_id"] == "101"
        return [{"path_full": "/compass_api/shop/live/live_room/liveroom_base_info",
                 "status_code": 0, "live_room_id": "101", "data": {
                     "author_base_info": {"author_id": "author"}, "live_room_base_info": {
                         "start_time": {"value": at(10, 7).timestamp()},
                         "end_time": {"value": at(11, 0, 56).timestamp()}, "is_living": False}}}], [], []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at(11, 1, 1)

    base = {"live_room_id": "101", "author_id": "author", "qianchuan_account_id": "ad",
            "source": "previous_shift_end", "counters": {"gmv": 100},
            "qianchuan": {"stat_cost_for_roi2": 10}}
    snap = {**base, "counters": {"gmv": 250}, "qianchuan": {"stat_cost_for_roi2": 30},
            "cards": {}, "captured_at": at(11, 1, 1).isoformat()}
    monkeypatch.setattr(collector, "browser_context", context)
    monkeypatch.setattr(collector, "ensure_shop_login", login)
    monkeypatch.setattr(collector, "_overview_data", unavailable)
    monkeypatch.setattr(collector, "_capture_report_pages", capture)
    monkeypatch.setattr(collector, "capture_snapshot", lambda *args: snap)
    monkeypatch.setattr(collector, "datetime", Clock)
    result = await collector.collect_report(settings={}, schedule=schedule("101"), modules=[],
        run_id="offline-final", baseline=base, include_daily_exposure=True,
        on_room_confirmed=confirmed.append)
    assert result["is_final"]
    assert result["author_id"] == "author"
    assert result["segments"][0]["room"]["live_end"] == old["live_end"]
    assert result["source"]["gmv"] == 150
    assert result["source"]["spend"] == 20
    assert confirmed[0]["live_room_id"] == "101"


async def test_confirmed_identity_persists_when_later_collection_fails(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import liveops.service as service
    from liveops.db import Base
    from liveops.models import CollectionRun, ShiftSchedule

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service, "db_settings", lambda *args: {"settle_delay_seconds": "0"})
    monkeypatch.setattr(service, "now_local", lambda: at(10, 23))

    async def collect(**kwargs):
        kwargs["on_room_confirmed"]({"live_room_id": "101", "author_id": "author",
                                      "source": "initial_discovery"})
        raise RuntimeError("later metrics unavailable")

    monkeypatch.setattr(service, "collect_report", collect)
    with sessions() as db:
        row = ShiftSchedule(work_date=at(10, 7).date(), start_time=time(20, 30),
                            end_time=time(1), host_name="night", live_room_id="")
        db.add(row)
        db.flush()
        row_id = row.id
        db.add(CollectionRun(id="fail-after-room", schedule_id=row_id, kind="report", trigger="user"))
        db.commit()
    await service.RunDispatcher(None)._execute("fail-after-room")
    with sessions() as db:
        assert db.get(ShiftSchedule, row_id).live_room_id == "101"
        run = db.get(CollectionRun, "fail-after-room")
        assert run.status == "failed"
        assert run.result_json["room_selection"]["author_id"] == "author"
    engine.dispose()


@pytest.mark.parametrize("next_pin", ["", "102"])
async def test_incomplete_ad_data_still_hands_off_identity_without_repinning_next_room(monkeypatch, next_pin):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import liveops.service as service
    from liveops.db import Base
    from liveops.models import CollectionRun, ShiftSchedule
    from liveops.snapshots import handoff_source

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service, "db_settings", lambda *args: {"settle_delay_seconds": "0"})
    monkeypatch.setattr(service, "now_local", lambda: at(10, 20, 31))

    async def collect(**kwargs):
        selected = room()
        snap = {"live_room_id": "101", "qianchuan_account_id": "ad",
                "captured_at": at(10, 20, 31).isoformat(), "qianchuan": {},
                "counters": {"gmv": 100}, "cards": {}}
        source = handoff_source(snapshot=snap, room=selected, baseline=kwargs["baseline"],
                                start=at(10, 7), end=at(10, 20, 30))
        part = {"room": selected, "snapshot": snap, "source": source,
                "captured_at": snap["captured_at"], "qianchuan_account_id": "ad", "responses": []}
        return {"live_room_id": "101", "author_id": "author", "source": source,
                "segments": [part], "responses": [], "screenshots": [], "page_errors": [],
                "schedule_start": at(10, 7).isoformat(), "schedule_end": at(10, 20, 30).isoformat(),
                "collected_at": snap["captured_at"], "is_final": True}

    monkeypatch.setattr(service, "collect_report", collect)
    with sessions() as db:
        first = ShiftSchedule(work_date=at(10, 7).date(), start_time=time(7), end_time=time(20, 30),
                              host_name="first", live_room_id="", qianchuan_account_id="ad")
        following = ShiftSchedule(work_date=at(10, 7).date(), start_time=time(20, 30), end_time=time(1),
                                  host_name="night", live_room_id=next_pin, qianchuan_account_id="ad")
        db.add_all([first, following])
        db.flush()
        next_id = following.id
        db.add(CollectionRun(id="missing-ad", schedule_id=first.id, kind="report", trigger="user"))
        db.commit()
    await service.RunDispatcher(None)._execute("missing-ad")
    with sessions() as db:
        following = db.get(ShiftSchedule, next_id)
        assert following.live_room_id == (next_pin or "101")
        assert following.baseline_json["live_room_id"] == "101"
        assert following.baseline_json["author_id"] == "author"
        assert following.baseline_json["qianchuan"] == {}
    engine.dispose()
