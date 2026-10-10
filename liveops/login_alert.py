"""Feishu handoff adapter for the reused Douyin login state machine."""

from __future__ import annotations

from contextvars import ContextVar

from liveops.feishu import FeishuClient

_settings: ContextVar[dict[str, str] | None] = ContextVar("douyin_login_alert_settings", default=None)


def set_alert_settings(settings: dict[str, str]):
    return _settings.set(settings)


def reset_alert_settings(token) -> None:
    _settings.reset(token)


async def notify_human_handoff(*, platform: str, company: str, shop: str, purpose: str) -> None:
    settings = _settings.get() or {}
    client = FeishuClient(
        settings.get("feishu_app_id", ""),
        settings.get("feishu_app_secret", ""),
        settings.get("feishu_chat_id", ""),
    )
    try:
        await client.send_alert(
            f"{platform} {company} {shop}：{purpose}。请打开当前 Cloak profile 完成人工验证。",
            settings.get("feishu_alert_open_id", ""),
        )
    finally:
        await client.close()
