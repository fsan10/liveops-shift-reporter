from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy.orm import Session

from liveops.models import StoredSetting
from liveops.security import SecretBox

SECRET_KEYS = {
    "browser_cdp_endpoint",
    "feishu_app_secret",
}

BOOLEAN_SETTINGS = {
    "data_collection_enabled": True,
    "hourly_screenshot_enabled": False,
    "anchor_screenshot_enabled": True,
}


def setting_enabled(settings: dict[str, str], key: str) -> bool:
    default = BOOLEAN_SETTINGS[key]
    return settings.get(key, str(default).lower()).strip().lower() == "true"


def screenshot_chat(settings: dict[str, str], target: str) -> str:
    # The existing Chat ID remains the anchor destination on upgrade.
    key = "feishu_hourly_chat_id" if target == "hourly" else "feishu_chat_id"
    return settings.get(key, "").strip()


def _masked_secret(value: str) -> str:
    value = value.strip()
    if len(value) <= 2:
        return "******" if value else ""
    visible = min(5, max(1, (len(value) - 1) // 2))
    return f"{value[:visible]}******{value[-visible:]}"


DEFAULT_SETTINGS: dict[str, str] = {
    "roster_anchor_pool": json.dumps(
        [], ensure_ascii=False
    ),
    "roster_promoter_pool": json.dumps([], ensure_ascii=False),
    "browser_mode": os.getenv("BROWSER_MODE", "cloak_cdp"),
    "browser_cdp_endpoint": os.getenv("CLOAK_CDP_ENDPOINT", ""),
    "cloak_profile_id": os.getenv("CLOAK_PROFILE_ID", ""),
    "cloak_profile_name": os.getenv("CLOAK_PROFILE_NAME", ""),
    "native_chromium_path": os.getenv("NATIVE_CHROMIUM_PATH", ""),
    "native_user_data_dir": os.getenv("NATIVE_USER_DATA_DIR", "var/native-browser-profile"),
    "native_context_options": os.getenv(
        "NATIVE_CONTEXT_OPTIONS",
        '{"locale":"zh-CN","timezone_id":"Asia/Shanghai","viewport":{"width":1920,"height":1080},"device_scale_factor":1}',
    ),
    "shop_name": os.getenv("DEFAULT_SHOP_NAME", ""),
    "live_room_id": os.getenv("DEFAULT_LIVE_ROOM_ID", ""),
    "live_author_id": os.getenv("DEFAULT_LIVE_AUTHOR_ID", ""),
    "qianchuan_account_id": os.getenv("DEFAULT_QIANCHUAN_ACCOUNT_ID", ""),
    "feishu_app_id": "",
    "feishu_app_secret": "",
    "feishu_chat_id": "",
    "feishu_hourly_chat_id": "",
    "feishu_alert_open_id": "",
    "bitable_app_token": "",
    "bitable_table_id": "",
    "bitable_sync_key_field": "SourceID",
    "bitable_field_mapping": json.dumps(
        {
            "work_date": "日期",
            "time_range": "时段",
            "duration_hours": "时长/h",
            "host_name": "主播",
            "promoter": "投手",
            "gmv": "GMV",
            "qianchuan_coupon": "千川券",
            "overall_amount": "整体（GMV+券）",
            "hourly_gmv": "单小时成交额",
            "paid_users": "成交人数",
            "avg_order_value": "平均客单价",
            "product_click_users": "商品点击人数",
            "product_click_conversion": "商品点击转化率",
            "viewers": "场观",
            "watch_conversion": "观看成交率",
            "gpm": "GPM",
            "interaction_rate": "互动率",
            "new_followers": "增粉",
            "new_follower_rate": "增粉率",
            "stay_seconds": "停留（s）",
            "spend": "消耗",
            "exposure": "曝光",
            "overall_roi": "整体ROI",
            "cpm": "CPM",
            "daily_exposure": "日曝光",
        },
        ensure_ascii=False,
    ),
    "report_message_template": "以上主播{host_name}{start_time}-{end_time}点的直播数据",
    "hourly_message_template": "开播第{hour_index}小时直播截图\n计划时间：{planned_time}\n实际截图时间：{captured_time}",
    **{key: str(value).lower() for key, value in BOOLEAN_SETTINGS.items()},
    "settle_delay_seconds": "30",
    "auto_catchup_minutes": "90",
}


class SettingsStore:
    def __init__(self, secret_box: SecretBox) -> None:
        self.secret_box = secret_box

    def get(self, db: Session, key: str, default: str | None = None) -> str:
        row = db.get(StoredSetting, key)
        if not row:
            return DEFAULT_SETTINGS.get(key, default or "")
        return self.secret_box.decrypt(row.value) if row.encrypted else row.value

    def is_configured(self, db: Session, key: str) -> bool:
        return bool(self.get(db, key, "").strip())

    def set(self, db: Session, key: str, value: str) -> None:
        encrypted = key in SECRET_KEYS
        stored_value = self.secret_box.encrypt(value) if encrypted and value else value
        row = db.get(StoredSetting, key)
        if row is None:
            row = StoredSetting(key=key, value=stored_value, encrypted=encrypted)
            db.add(row)
        else:
            row.value = stored_value
            row.encrypted = encrypted

    def many(self, db: Session, keys: list[str]) -> dict[str, str]:
        return {key: self.get(db, key) for key in keys}

    def public(self, db: Session) -> dict[str, Any]:
        keys = set(DEFAULT_SETTINGS)
        plain = {key: self.get(db, key) for key in keys if key not in SECRET_KEYS}
        plain["browser_cdp_endpoint_configured"] = self.is_configured(db, "browser_cdp_endpoint")
        plain["feishu_app_secret_configured"] = self.is_configured(db, "feishu_app_secret")
        plain["browser_cdp_endpoint_masked"] = _masked_secret(self.get(db, "browser_cdp_endpoint"))
        plain["feishu_app_secret_masked"] = _masked_secret(self.get(db, "feishu_app_secret"))
        return plain


def db_settings(db: Session, store: SettingsStore) -> dict[str, str]:
    return {key: store.get(db, key) for key in DEFAULT_SETTINGS}
