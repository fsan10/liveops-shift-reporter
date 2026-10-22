import asyncio
import json
from datetime import date, time

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import liveops.service as service
from liveops.db import Base
from liveops.feishu import FeishuClient, FeishuError
from liveops.models import CollectionRun, ShiftReport, ShiftSchedule

SETTINGS = {"feishu_app_id": "app", "feishu_app_secret": "secret",
            "feishu_chat_id": "chat", "feishu_alert_open_id": "owner"}


@pytest.fixture
def state(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service, "db_settings", lambda *args: SETTINGS)
    with sessions() as db:
        schedule = ShiftSchedule(work_date=date(2026, 10, 12), start_time=time(7),
                                 end_time=time(11, 30), host_name="测试主播", live_room_id="")
        db.add(schedule)
        db.flush()
        db.add(CollectionRun(id="run-test", schedule_id=schedule.id, kind="report", trigger="user",
                             status="failed", message="Network.getResponseBody: navigated away from https://private/?token=SECRET",
                             result_json={"steps": [{"stage": "room", "status": "info"}]}))
        db.commit()
    yield sessions
    engine.dispose()


@pytest.fixture
def bot(monkeypatch):
    class Bot:
        sent = []
        closed = 0
        failure = None

        def __init__(self, *args):
            pass

        async def send_alert(self, message, open_id, *, title):
            self.sent.append((message, open_id, title))
            await asyncio.sleep(0)
            if self.failure:
                raise self.failure
            return {"message_id": "msg-alert"}

        async def close(self):
            Bot.closed += 1

    monkeypatch.setattr(service, "FeishuClient", Bot)
    return Bot


async def test_failed_run_notifies_once_with_context_and_without_raw_url(state, bot):
    dispatcher = service.RunDispatcher(None)
    await asyncio.gather(*(dispatcher._notify_run_problem("run-test", SETTINGS) for _ in range(2)))
    assert len(bot.sent) == 1 and bot.closed == 1
    text, mention, title = bot.sent[0]
    assert title == "直播运营异常提醒" and mention == "owner"
    assert all(part in text for part in ["07:00–11:30", "测试主播", "场次确认", "罗盘页面导航", "run-test"])
    assert "https://private" not in text and "SECRET" not in text
    with state() as db:
        run = db.get(CollectionRun, "run-test")
        assert run.result_json["exception_alert"]["message_id"] == "msg-alert"
        assert run.status == "failed" and "Network.getResponseBody" in run.message


async def test_delivery_error_preserves_original_failure_and_does_not_loop(state, bot):
    bot.failure = RuntimeError("delivery failed secret https://private")
    dispatcher = service.RunDispatcher(None)
    await dispatcher._notify_run_problem("run-test", SETTINGS)
    await dispatcher._notify_run_problem("run-test", SETTINGS)
    with state() as db:
        run = db.get(CollectionRun, "run-test")
        assert run.status == "failed" and "Network.getResponseBody" in run.message
        assert run.result_json["exception_alert"]["status"] == "failed"
        assert "https://" not in run.result_json["exception_alert"]["error"]
    assert len(bot.sent) == 1 and bot.closed == 1


async def test_unconfigured_notifications_have_explicit_audit_status(state, bot):
    await service.RunDispatcher(None)._notify_run_problem("run-test", {})
    assert not bot.sent
    with state() as db:
        assert db.get(CollectionRun, "run-test").result_json["exception_alert"]["status"] == "not_configured"


@pytest.mark.parametrize("status,missing,integrations,notify", [
    ("partial", ["页面采集:转化漏斗截图失败"], {}, True),
    ("partial", ["GMV"], {}, True),
    ("succeeded", [], {"bitable": "failed: connection timeout"}, True),
    ("partial", [], {"feishu": "failed: permission"}, True),
    ("partial", ["临时快照:班次尚未结束"], {}, False),
    ("succeeded", [], {"feishu": "sent", "bitable": "synced"}, False),
])
async def test_terminal_issue_selection(state, bot, status, missing, integrations, notify):
    with state() as db:
        run = db.get(CollectionRun, "run-test")
        run.status = status
        run.missing_fields = missing
        run.result_json = {"integrations": integrations}
        db.commit()
    await service.RunDispatcher(None)._notify_run_problem("run-test", SETTINGS)
    assert bool(bot.sent) == notify


async def test_execute_failure_automatically_notifies_and_releases_queue(state, bot, monkeypatch):
    async def failed_collect(**kwargs):
        kwargs["progress"]("room", "等待场次发现", "info")
        raise RuntimeError("Network.getResponseBody: navigated away")

    monkeypatch.setattr(service, "collect_report", failed_collect)
    dispatcher = service.RunDispatcher(None)
    with state() as db:
        schedule_id = db.get(CollectionRun, "run-test").schedule_id
    dispatcher._queued.add(("report", schedule_id))
    await dispatcher._execute("run-test")
    assert len(bot.sent) == 1
    assert not dispatcher._queued and not dispatcher._browser_lock.locked()
    with state() as db:
        run = db.get(CollectionRun, "run-test")
        assert run.status == "failed"
        assert "Network.getResponseBody" in run.message
        assert run.result_json["exception_alert"]["status"] == "sent"


async def test_integration_failure_marks_run_partial_and_notifies(state, bot, monkeypatch):
    async def collected(**kwargs):
        return {"source": {}, "responses": [], "screenshots": [], "page_errors": [],
                "schedule_start": "2026-10-12T07:00:00+08:00",
                "schedule_end": "2026-10-12T11:30:00+08:00", "live_room_id": "123"}

    async def sync(self, schedule_id, settings, run_id, *args, **kwargs):
        with state() as db:
            report = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule_id))
            report.feishu_status = "sent"
            report.bitable_status = "failed: permission"
            db.commit()
        self._log_step(run_id, "bitable", "同步失败", "failed")

    monkeypatch.setattr(service, "collect_report", collected)
    monkeypatch.setattr(service, "build_report_metrics", lambda **kwargs: ({}, []))
    monkeypatch.setattr(service, "_daily_exposure_for_date", lambda *args: {"value": None, "issues": []})
    monkeypatch.setattr(service.RunDispatcher, "_backfill_daily_exposure", lambda *args: [])
    monkeypatch.setattr(service.RunDispatcher, "_sync_integrations", sync)
    await service.RunDispatcher(None)._execute("run-test")
    with state() as db:
        run = db.get(CollectionRun, "run-test")
        assert run.status == "partial", run.message
        assert run.result_json["integrations"]["bitable"].startswith("failed")
        assert run.result_json["exception_alert"]["status"] == "sent"
    assert len(bot.sent) == 1 and "多维表格" in bot.sent[0][0]


@pytest.mark.parametrize("api_body,works", [
    ({"code": 0, "data": {"message_id": "msg-live"}}, True),
    ({"code": 230013, "msg": "permission denied"}, False),
    ({"code": 0, "data": {}}, False),
])
async def test_feishu_alert_checks_business_response_and_message_id(api_body, works):
    posts = []

    async def handler(request):
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        posts.append(json.loads(request.content))
        return httpx.Response(200, json=api_body)

    client = FeishuClient("app", "secret", "chat")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        if works:
            assert await client.send_alert("异常测试", "owner", title="直播运营异常提醒") == {"message_id": "msg-live"}
        else:
            with pytest.raises(FeishuError):
                await client.send_alert("异常测试", "owner", title="直播运营异常提醒")
    finally:
        await client.close()
    content = json.loads(posts[0]["content"])["zh_cn"]
    assert content["title"] == "直播运营异常提醒"
    assert content["content"][0][1]["user_id"] == "owner"
