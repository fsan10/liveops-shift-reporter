from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from liveops.bitable import BitableClient
from liveops.feishu import FeishuClient
from liveops.service import integration_not_configured_status


def test_not_configured_sync_status_lists_missing_settings():
    settings = {
        "feishu_app_id": "app",
        "feishu_app_secret": "secret",
        "feishu_chat_id": "chat",
        "bitable_app_token": "",
        "bitable_table_id": "",
    }
    assert integration_not_configured_status(settings, "bitable") == (
        "not_configured: 尚缺 多维表格 app_token、数据表 table_id"
    )
    assert integration_not_configured_status({"feishu_app_id": "app"}, "feishu") == (
        "not_configured: 尚缺 飞书 App Secret、群聊 Chat ID"
    )


@pytest.mark.asyncio
async def test_feishu_uploads_images_then_sends_caption_post(tmp_path):
    paths: list[str] = []
    progress: list[str] = []
    sent_payload: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        paths.append(request.url.path)
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "test-token", "expire": 7200}
            )
        if request.url.path.endswith("/im/v1/images"):
            return httpx.Response(200, json={"code": 0, "data": {"image_key": f"img-{len(paths)}"}})
        if request.url.path.endswith("/im/v1/messages"):
            sent_payload = json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "msg-123"}})
        return httpx.Response(404, json={"code": 1, "msg": "unexpected endpoint"})

    client = FeishuClient("app-id", "app-secret", "chat-id")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    screenshot_paths = []
    for index in range(5):
        path = tmp_path / f"shot-{index}.png"
        path.write_bytes(b"fake-png")
        screenshot_paths.append({"path": str(path)})
    try:
        result = await client.send_report(
            "以上主播测试主播6-10点的直播数据", screenshot_paths,
            on_progress=progress.append,
        )
    finally:
        await client.close()

    assert result == {"message_id": "msg-123", "image_count": 5}
    assert paths.count("/open-apis/im/v1/images") == 5
    assert paths[-1] == "/open-apis/im/v1/messages"
    assert len(progress) == 6
    content = json.loads(sent_payload["content"])
    rows = content["zh_cn"]["content"]
    assert len(rows) == 6
    assert all(row[0]["tag"] == "img" for row in rows[:5])
    assert rows[5][0]["text"] == "以上主播测试主播6-10点的直播数据"
    assert content["zh_cn"]["title"] == "直播数据报告："


@pytest.mark.asyncio
async def test_bitable_maps_field_types_and_creates_report_record():
    request_log: list[tuple[str, str, dict | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_json = json.loads(request.content) if request.content else None
        request_log.append((request.method, request.url.path, request_json))
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "test-token"})
        if request.url.path.endswith("/fields"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {"field_name": "日期", "type": 5},
                            {"field_name": "GMV", "type": 2},
                            {"field_name": "主播", "type": 1},
                            {"field_name": "SourceID", "type": 1},
                        ]
                    },
                },
            )
        if request.method == "POST" and request.url.path.endswith("/records"):
            return httpx.Response(200, json={"code": 0, "data": {"record": {"record_id": "rec-1"}}})
        return httpx.Response(404, json={"code": 1, "msg": "unexpected endpoint"})

    client = BitableClient("app-id", "app-secret", "app-token", "table-id")
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await client.upsert_record(
            values={
                "work_date": date(2026, 9, 29).isoformat(),
                "gmv": 10328.7,
                "host_name": "主播甲",
            },
            field_mapping={"work_date": "日期", "gmv": "GMV", "host_name": "主播"},
            sync_key_field="采集编号",
            sync_key="shift-12",
        )
    finally:
        await client.close()

    body = next(
        payload
        for method, path, payload in request_log
        if method == "POST" and path.endswith("/records")
    )
    fields = body["fields"]
    assert result["record_id"] == "rec-1"
    assert result["missing_mappings"] == []
    assert fields["GMV"] == 10328.7
    assert fields["主播"] == "主播甲"
    assert fields["SourceID"] == "shift-12"
    assert isinstance(fields["日期"], int)


@pytest.mark.asyncio
async def test_bitable_deletes_cold_record_by_record_id():
    request_log: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_log.append((request.method, request.url.path))
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "test-token"})
        if request.method == "DELETE" and request.url.path.endswith("/records/rec-old"):
            return httpx.Response(200, json={"code": 0, "data": {"deleted": True}})
        return httpx.Response(404, json={"code": 1, "msg": "unexpected endpoint"})

    client = BitableClient("app-id", "app-secret", "app-token", "table-id")
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client.delete_record("rec-old")
    finally:
        await client.close()

    assert (
        "DELETE",
        "/open-apis/bitable/v1/apps/app-token/tables/table-id/records/rec-old",
    ) in request_log
