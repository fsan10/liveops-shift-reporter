from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx

BASE_URL = "https://open.feishu.cn/open-apis"


class FeishuError(RuntimeError):
    pass


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, chat_id: str) -> None:
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.chat_id = chat_id.strip()
        self._token = ""
        self._token_expires_at = 0.0
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=15.0))

    async def close(self) -> None:
        await self._client.aclose()

    def _require_credentials(self) -> None:
        if not self.app_id or not self.app_secret:
            raise FeishuError("请配置飞书企业自建应用 App ID 和 App Secret。")

    async def tenant_access_token(self) -> str:
        self._require_credentials()
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        response = await self._client.post(
            f"{BASE_URL}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        payload = _json_response(response)
        token = payload.get("tenant_access_token")
        if not token:
            raise FeishuError(f"获取 tenant_access_token 失败：{payload.get('msg', payload)}")
        self._token = str(token)
        self._token_expires_at = time.monotonic() + int(payload.get("expire", 7200))
        return self._token

    async def _authorized_post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        token = await self.tenant_access_token()
        response = await self._client.post(
            f"{BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        return _json_response(response)

    async def upload_image(self, image_path: str | Path) -> str:
        path = Path(image_path)
        if not path.is_file():
            raise FeishuError(f"截图文件不存在：{path}")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise FeishuError(f"截图超过飞书图片接口 10 MB 限制：{path.name}")
        token = await self.tenant_access_token()
        with path.open("rb") as image:
            response = await self._client.post(
                f"{BASE_URL}/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": (path.name, image, "image/png")},
            )
        payload = _json_response(response)
        image_key = (payload.get("data") or {}).get("image_key")
        if not image_key:
            raise FeishuError(f"上传截图失败：{payload.get('msg', payload)}")
        return str(image_key)

    async def send_report(
        self, caption: str, screenshots: list[dict[str, Any]],
        on_progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if not self.chat_id:
            raise FeishuError("请配置飞书群聊 Chat ID。")
        image_keys: list[str] = []
        for index, screenshot in enumerate(screenshots, start=1):
            image_keys.append(await self.upload_image(screenshot["path"]))
            if on_progress:
                on_progress(f"第 {index}/{len(screenshots)} 张截图上传完成。")
        content_rows: list[list[dict[str, Any]]] = [
            [{"tag": "img", "image_key": image_key}] for image_key in image_keys
        ]
        content_rows.append([{"tag": "text", "text": caption}])
        payload = await self._authorized_post(
            "/im/v1/messages?receive_id_type=chat_id",
            json={
                "receive_id": self.chat_id,
                "msg_type": "post",
                "content": json.dumps(
                    {"zh_cn": {"title": "直播数据报告：", "content": content_rows}},
                    ensure_ascii=False,
                ),
            },
        )
        if on_progress:
            on_progress("飞书群图文消息已提交。")
        return {
            "message_id": (payload.get("data") or {}).get("message_id", ""),
            "image_count": len(image_keys),
        }

    async def send_alert(self, message: str, alert_open_id: str = "") -> dict[str, Any]:
        if not self.chat_id:
            raise FeishuError("请配置飞书群聊 Chat ID。")
        content = [{"tag": "text", "text": message}]
        if alert_open_id.strip():
            content.append({"tag": "at", "user_id": alert_open_id.strip(), "user_name": "负责人"})
        payload = await self._authorized_post(
            "/im/v1/messages?receive_id_type=chat_id",
            json={
                "receive_id": self.chat_id,
                "msg_type": "post",
                "content": json.dumps(
                    {"zh_cn": {"title": "抖音登录需要处理", "content": [content]}},
                    ensure_ascii=False,
                ),
            },
        )
        return {"message_id": (payload.get("data") or {}).get("message_id", "")}

    async def send_test_message(self) -> dict[str, Any]:
        if not self.chat_id:
            raise FeishuError("请配置飞书群聊 Chat ID。")
        payload = await self._authorized_post(
            "/im/v1/messages?receive_id_type=chat_id",
            json={
                "receive_id": self.chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": "直播运营后台连接测试成功。"}, ensure_ascii=False),
            },
        )
        return {"message_id": (payload.get("data") or {}).get("message_id", ""), "sent": True}


def _json_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuError(f"飞书返回非 JSON 响应：HTTP {response.status_code}") from exc
    if response.is_error:
        message = payload.get("msg", payload) if isinstance(payload, dict) else payload
        raise FeishuError(f"飞书 API HTTP {response.status_code}：{message}")
    if not isinstance(payload, dict):
        raise FeishuError("飞书返回数据格式异常。")
    if int(payload.get("code", 0)) != 0:
        raise FeishuError(f"飞书 API 错误 {payload.get('code')}：{payload.get('msg', '')}")
    return payload
