import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError

import liveops.collector as collector

HISTORY_PATH = "/compass_api/shop/live/live_overview/live_room_detail_v2"


def payloads():
    return {
        collector.REALTIME_PATH: {"status_code": 0, "data": {"card_list": []}},
        HISTORY_PATH: {"status_code": 0, "data": {"module_data": {
            "shop_live_list_room_detail": {"compass_general_table_value": {
                "data": [], "page_result": {"total": 0},
            }},
        }}},
    }


class Page:
    def __init__(self, scenario):
        self.scenario = scenario
        self.listeners = {"response": [], "framenavigated": [], "request": []}
        self.main_frame = object()
        self.url = "about:blank"
        self.attempts = 0
        self.reads = 0

    def on(self, event, callback):
        self.listeners[event].append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def navigate(self, url):
        self.url = url
        for callback in self.listeners["framenavigated"]:
            callback(self.main_frame)

    def respond(self, path, body=None, *, broken=False, delay=0, status=200):
        async def json():
            self.reads += 1
            if delay:
                await asyncio.sleep(delay)
            if broken:
                raise RuntimeError("Network.getResponseBody: navigated away")
            return body

        request = self.request(path)
        response = SimpleNamespace(url=request.url, request=request, status=status, json=json)
        for callback in self.listeners["response"]:
            callback(response)

    def valid(self):
        for path, body in payloads().items():
            self.respond(path, body)

    def request(self, path):
        class Request:
            url = "https://compass.jinritemai.com" + path

        request = Request()
        for callback in self.listeners["request"]:
            callback(request)
        return request

    async def goto(self, *args, **kwargs):
        self.attempts += 1
        self.navigate(collector.LIVE_OVERVIEW_URL)
        await self.scenario(self)


@pytest.fixture(autouse=True)
def short_wait(monkeypatch):
    monkeypatch.setattr(collector, "PAGE_TIMEOUT_MS", 150)
    monkeypatch.setattr(collector, "COMPASS_READY_STABLE_SECONDS", 0.005)


async def test_reads_bodies_during_navigation_and_waits_for_sso_recovery():
    async def scenario(page):
        for path in payloads():
            page.respond(path, broken=True)
        await asyncio.sleep(0)
        assert page.reads == 2  # body reads start before goto completes
        page.navigate("https://compass.jinritemai.com/login/")
        page.respond(collector.REALTIME_PATH, {"status_code": 11001})
        await asyncio.sleep(0)
        page.navigate(collector.LIVE_OVERVIEW_URL)
        page.valid()

    page = Page(scenario)
    result = await collector._overview_payloads(page)
    assert result == tuple(payloads().values())
    assert page.attempts == 1
    assert all(not listeners for listeners in page.listeners.values())


async def test_late_previous_document_response_cannot_satisfy_readiness():
    async def scenario(page):
        for path, body in payloads().items():
            page.respond(path, body, delay=0.02)
        page.navigate(collector.LIVE_OVERVIEW_URL)
        page.respond(collector.REALTIME_PATH, payloads()[collector.REALTIME_PATH])

    page = Page(scenario)
    with pytest.raises(RuntimeError, match="重试一次"):
        await collector._overview_payloads(page)
    assert page.attempts == 2
    assert all(not listeners for listeners in page.listeners.values())


async def test_request_started_before_navigation_and_responding_after_is_discarded():
    async def scenario(page):
        request = page.request(HISTORY_PATH)
        page.navigate(collector.LIVE_OVERVIEW_URL)
        async def json():
            return payloads()[HISTORY_PATH]
        for callback in page.listeners["response"]:
            callback(SimpleNamespace(url=request.url, request=request, status=200, json=json))
        page.respond(collector.REALTIME_PATH, payloads()[collector.REALTIME_PATH])

    with pytest.raises(RuntimeError, match="重试一次"):
        await collector._overview_payloads(Page(scenario))


async def test_discovery_retries_only_once_and_accepts_valid_empty_lists():
    async def scenario(page):
        if page.attempts == 1:
            page.respond(collector.REALTIME_PATH, {"status_code": 11001})
        else:
            page.valid()

    page = Page(scenario)
    assert await collector._overview_payloads(page) == tuple(payloads().values())
    assert page.attempts == 2


@pytest.mark.parametrize("body,status", [({"status_code": 11001}, 200), ({"data": {}}, 200), ({}, 401)])
async def test_invalid_login_or_api_payload_never_becomes_empty_rooms(body, status):
    async def scenario(page):
        page.respond(collector.REALTIME_PATH, body, status=status)
        page.respond(HISTORY_PATH, payloads()[HISTORY_PATH])

    page = Page(scenario)
    with pytest.raises(RuntimeError, match="登录/场次接口未就绪"):
        await collector._overview_payloads(page)
    assert page.attempts == 2


async def test_sso_aborted_navigation_can_still_deliver_valid_responses():
    async def scenario(page):
        page.valid()
        raise PlaywrightError("net::ERR_ABORTED")

    assert await collector._overview_payloads(Page(scenario)) == tuple(payloads().values())


async def test_cancellation_removes_only_owned_listeners_and_cancels_readers():
    cancelled = asyncio.Event()

    async def scenario(page):
        async def json():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        for callback in page.listeners["response"]:
            request = page.request(HISTORY_PATH)
            callback(SimpleNamespace(url=request.url, request=request, status=200, json=json))

    page = Page(scenario)
    def external(response):
        pass
    page.on("response", external)
    task = asyncio.create_task(collector._overview_payloads(page))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert page.listeners["response"] == [external]
    assert page.listeners["framenavigated"] == []
