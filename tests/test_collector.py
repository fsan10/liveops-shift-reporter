import pytest

import liveops.collector as collector_module
from liveops.collector import _refresh_or_goto, _save_screenshot
from liveops.portal import ResponseCollector, portal_urls


class NavigationPage:
    def __init__(self, url):
        self.url = url
        self.reloads = 0
        self.navigations = []

    async def reload(self, **_kwargs):
        self.reloads += 1

    async def goto(self, url, **_kwargs):
        self.navigations.append(url)
        self.url = url

    async def wait_for_timeout(self, _timeout):
        pass


async def test_repeated_collection_keeps_realtime_tabs_and_reloads_funnel():
    urls = portal_urls("123", "456")
    for kind in ("pro", "base", "qianchuan"):
        page = NavigationPage(urls[kind])
        assert await _refresh_or_goto(page, urls[kind], refresh=False)
        assert page.reloads == 0
        assert page.navigations == []
    page = NavigationPage(urls["detail"])
    assert not await _refresh_or_goto(page, urls["detail"], refresh=True)
    assert page.reloads == 1


@pytest.mark.parametrize("retry_succeeds", [True, False])
async def test_qianchuan_render_failure_retries_with_a_fresh_response_boundary(
    monkeypatch, retry_succeeds
):
    collector = ResponseCollector()
    attempts = []

    async def ready(_page, current_collector, **kwargs):
        attempts.append(kwargs)
        current_collector.responses.append({"source": "qianchuan"})
        if len(attempts) == 1 or not retry_succeeds:
            raise RuntimeError("千川指标卡未完成渲染")

    monkeypatch.setattr(collector_module, "_qianchuan_payment_ready", ready)
    if retry_succeeds:
        await collector_module._prepare_qianchuan(None, collector, since=0, reused=False)
    else:
        with pytest.raises(RuntimeError, match="未完成渲染"):
            await collector_module._prepare_qianchuan(None, collector, since=0, reused=False)
    assert len(attempts) == 2
    assert attempts[1]["renew"] is True
    assert attempts[1]["since"] == 1


async def test_realtime_tab_with_wrong_room_or_fragment_navigates_to_target():
    target = portal_urls("123", "456")["pro"]
    for wrong in (target.replace("123", "999"), target.replace("#data", "#ad")):
        page = NavigationPage(wrong)
        assert not await _refresh_or_goto(page, target, refresh=False)
        assert page.navigations == [target]
        assert page.reloads == 0


async def test_qianchuan_tab_with_wrong_advertiser_navigates_to_configured_account():
    target = portal_urls("123", "456")["qianchuan"]
    page = NavigationPage(target.replace("aavid=456", "aavid=999"))
    assert not await _refresh_or_goto(page, target, refresh=False)
    assert page.navigations == [target]


class ScreenshotLocator:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def wait_for(self, **_kwargs):
        return None

    async def screenshot(self, *, path, **_kwargs):
        if self.page.fail_screenshot:
            raise RuntimeError("capture failed")
        self.page.captured_viewport = self.page.viewport.copy()
        with open(path, "wb") as image:
            image.write(b"png")


class ScreenshotPage:
    def __init__(self, *, width=615, height=418, screen_width=1360, screen_height=728):
        self.viewport = {"width": width, "height": height}
        self.metrics = {
            **self.viewport,
            "screenWidth": screen_width,
            "screenHeight": screen_height,
        }
        self.captured_viewport = None
        self.fail_screenshot = False

    async def evaluate(self, expression, *_args):
        if "window.innerWidth" in expression:
            return self.metrics
        return None

    async def set_viewport_size(self, size):
        self.viewport = size.copy()

    async def wait_for_function(self, *_args, **_kwargs):
        return None

    async def wait_for_timeout(self, _timeout):
        return None

    def locator(self, _selector):
        return ScreenshotLocator(self)

    async def screenshot(self, *, path, **_kwargs):
        self.captured_viewport = self.viewport.copy()
        with open(path, "wb") as image:
            image.write(b"png")


async def test_element_screenshot_expands_compact_viewport_then_restores_it(monkeypatch, tmp_path):
    monkeypatch.setattr(collector_module, "ARTIFACT_ROOT", tmp_path)
    page = ScreenshotPage()
    result = await _save_screenshot(
        page,
        {
            "code": "pro_traffic",
            "name": "专业版流量分析",
            "screenshot_mode": "element",
            "selector": "#scaleContainer",
        },
        "run-1",
        collector_module.date(2026, 10, 9),
        1,
    )

    assert page.captured_viewport == {"width": 1360, "height": 728}
    assert page.viewport == {"width": 615, "height": 418}
    assert result["size_bytes"] == 3


async def test_full_page_screenshot_expands_compact_viewport_then_restores_it(monkeypatch, tmp_path):
    monkeypatch.setattr(collector_module, "ARTIFACT_ROOT", tmp_path)
    page = ScreenshotPage()
    await _save_screenshot(
        page,
        {
            "code": "base_full",
            "name": "直播大屏基础版",
            "screenshot_mode": "full_page",
            "selector": "",
        },
        "run-1",
        collector_module.date(2026, 10, 9),
        1,
    )

    assert page.captured_viewport == {"width": 1360, "height": 728}
    assert page.viewport == {"width": 615, "height": 418}


async def test_element_screenshot_restores_viewport_after_capture_error(monkeypatch, tmp_path):
    monkeypatch.setattr(collector_module, "ARTIFACT_ROOT", tmp_path)
    page = ScreenshotPage()
    page.fail_screenshot = True
    with pytest.raises(RuntimeError, match="capture failed"):
        await _save_screenshot(
            page,
            {
                "code": "pro_traffic",
                "name": "专业版流量分析",
                "screenshot_mode": "element",
                "selector": "#scaleContainer",
            },
            "run-1",
            collector_module.date(2026, 10, 9),
            1,
        )

    assert page.viewport == {"width": 615, "height": 418}


async def test_qianchuan_keeps_its_existing_full_page_viewport(monkeypatch, tmp_path):
    monkeypatch.setattr(collector_module, "ARTIFACT_ROOT", tmp_path)
    page = ScreenshotPage()
    await _save_screenshot(
        page,
        {
            "code": "qianchuan_full",
            "name": "巨量千川大屏",
            "screenshot_mode": "full_page",
            "selector": "",
        },
        "run-1",
        collector_module.date(2026, 10, 9),
        1,
    )

    assert page.captured_viewport == {"width": 615, "height": 418}
    assert page.viewport == {"width": 615, "height": 418}


@pytest.mark.parametrize("failure_mode", ["never", "always", "reused_once"])
async def test_report_tabs_stay_open_and_are_reused_even_after_page_failure(monkeypatch, failure_mode):
    class Page(NavigationPage):
        def __init__(self):
            super().__init__("about:blank")
            self.listeners = []
            self.closes = 0

        def is_closed(self):
            return False

        async def bring_to_front(self):
            pass

        def on(self, _event, handler):
            self.listeners.append(handler)

        def remove_listener(self, _event, handler):
            self.listeners.remove(handler)

        async def evaluate(self, *_args):
            pass

        async def close(self):
            self.closes += 1

    class Context:
        pages = []

        async def new_page(self):
            page = Page()
            self.pages.append(page)
            return page

    class Section:
        async def count(self):
            return 1

        async def click(self, **kwargs):
            pass

    async def ready(*_args, **_kwargs):
        pass

    async def section(*_args):
        return Section()

    qian_calls = []

    async def qian(*args, **kwargs):
        qian_calls.append((args[0], kwargs["reused"]))
        if failure_mode == "always" or (failure_mode == "reused_once"
                                        and kwargs["reused"] and len(qian_calls) == 2):
            raise RuntimeError("card not ready")

    monkeypatch.setattr(ResponseCollector, "wait_for_data", ready)
    monkeypatch.setattr(ResponseCollector, "flush", ready)
    monkeypatch.setattr(collector_module, "_wait_screen_data", ready)
    monkeypatch.setattr(collector_module, "_find_text", section)
    monkeypatch.setattr(collector_module, "_prepare_qianchuan", qian)
    context = Context()
    context.pages = []
    for attempt in range(3):
        _, _, errors = await collector_module._capture_report_pages(context, room_id="123", aavid="456",
            modules=[], run_id="test", work_date=collector_module.date(2026, 10, 9), schedule_id=1, screenshots=False)
        assert bool(errors) == (failure_mode == "always")
        extra = attempt if failure_mode == "always" else int(failure_mode == "reused_once" and attempt > 0)
        assert len(context.pages) == 4 + extra
        assert all(not page.listeners and page.closes == 0 for page in context.pages)
    detail = next(page for page in context.pages if "/shop/live-detail" in page.url)
    assert detail.reloads == 2
    assert all(page.reloads == 0 for page in context.pages if page is not detail)
    if failure_mode == "reused_once":
        assert qian_calls[-1] == (context.pages[-1], True)
