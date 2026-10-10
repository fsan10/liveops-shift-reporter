from __future__ import annotations

import json
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from liveops.feishu import FeishuError, _json_response

BASE_URL = "https://open.feishu.cn/open-apis"


class BitableClient:
    def __init__(self, app_id: str, app_secret: str, app_token: str, table_id: str) -> None:
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.app_token = app_token.strip()
        self.table_id = table_id.strip()
        self._token = ""
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=15.0))

    async def close(self) -> None:
        await self._http.aclose()

    def _check(self) -> None:
        if not all((self.app_id, self.app_secret, self.app_token, self.table_id)):
            raise FeishuError("请配置飞书 App ID、App Secret、多维表格 app_token 和 table_id。")

    async def _access_token(self) -> str:
        self._check()
        if self._token:
            return self._token
        response = await self._http.post(
            f"{BASE_URL}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        payload = _json_response(response)
        self._token = str(payload.get("tenant_access_token", ""))
        if not self._token:
            raise FeishuError("飞书没有返回 tenant_access_token。")
        return self._token

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        token = await self._access_token()
        response = await self._http.request(
            method,
            f"{BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        return _json_response(response)

    async def list_fields(self) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields?page_size=100",
        )
        return (payload.get("data") or {}).get("items", [])

    async def check_access(self) -> dict[str, Any]:
        fields = await self.list_fields()
        return {
            "readable": True,
            "field_count": len(fields),
            "field_names": [field.get("field_name", "") for field in fields],
        }

    async def delete_record(self, record_id: str) -> None:
        if not record_id.strip():
            return
        payload = await self._request(
            "DELETE",
            f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/{record_id}",
        )
        data = payload.get("data") or {}
        if data.get("deleted") is False:
            raise FeishuError(f"飞书未删除多维表格记录 {record_id}。")

    async def upsert_record(
        self,
        *,
        values: dict[str, Any],
        field_mapping: dict[str, str],
        record_id: str = "",
        sync_key_field: str = "",
        sync_key: str = "",
    ) -> dict[str, Any]:
        fields_schema = await self.list_fields()
        fields_by_name = {str(row.get("field_name")): row for row in fields_schema}
        fields: dict[str, Any] = {}
        missing_mappings: list[str] = []
        for source_key, field_name in field_mapping.items():
            if source_key not in values or not field_name:
                continue
            if field_name not in fields_by_name:
                missing_mappings.append(field_name)
                continue
            schema = fields_by_name[field_name]
            fields[field_name] = _coerce_field(values[source_key], int(schema.get("type", 1)))
        effective_sync_key_field = sync_key_field
        if effective_sync_key_field not in fields_by_name and "SourceID" in fields_by_name:
            effective_sync_key_field = "SourceID"
        if effective_sync_key_field and effective_sync_key_field in fields_by_name and sync_key:
            fields[effective_sync_key_field] = sync_key
        elif sync_key_field and sync_key:
            missing_mappings.append(sync_key_field)
        if not fields:
            raise FeishuError("多维表格字段映射为空；请核对配置页的列名和实际表头。")
        endpoint = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
        if record_id:
            payload = await self._request("PUT", f"{endpoint}/{record_id}", json={"fields": fields})
        else:
            payload = await self._request("POST", endpoint, json={"fields": fields})
        data = payload.get("data") or {}
        return {
            "record_id": str(data.get("record", {}).get("record_id", record_id)),
            "created_or_updated": True,
            "missing_mappings": missing_mappings,
            "written_fields": list(fields),
        }


def _coerce_field(value: Any, field_type: int) -> Any:
    if value is None:
        return None
    # Bitable date fields use Unix milliseconds; text date fields remain ISO strings.
    if field_type == 5 and isinstance(value, str):
        try:
            parsed = datetime.combine(
                date.fromisoformat(value), time.min, tzinfo=ZoneInfo("Asia/Shanghai")
            )
            return int(parsed.timestamp() * 1000)
        except ValueError:
            try:
                parsed_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return int(parsed_dt.timestamp() * 1000)
            except ValueError:
                return value
    if field_type in {2, 20, 21} and isinstance(value, (int, float)):
        return value
    if field_type == 7:
        return bool(value)
    if isinstance(value, (str, int, float, bool)):
        return str(value) if field_type == 1 else value
    return json.dumps(value, ensure_ascii=False)
