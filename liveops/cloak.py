"""Resolve the current Cloak Login CDP endpoint through its local MCP server.

The endpoint is intentionally ephemeral: it is never read from or written to the
application settings database.  The profile is selected by ID when configured,
otherwise by its exact business-facing name.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

MCP_URL = os.getenv("CLOAK_MCP_URL", "http://127.0.0.1:19610/mcp")


def _unpack(payload: dict[str, Any]) -> Any:
    if "error" in payload:
        raise RuntimeError(f"Cloak MCP 调用失败：{payload['error'].get('message', '未知错误')}")
    result = payload.get("result") or {}
    if result.get("isError"):
        raise RuntimeError("Cloak MCP 调用失败：" + str(result.get("content", ""))[:300])
    for item in result.get("content", []):
        if item.get("type") == "text":
            return json.loads(item["text"])
    return result.get("structuredContent", result)


async def current_endpoint(settings: dict[str, str]) -> str:
    """Reuse the configured profile if active, or start that profile once."""
    profile_id = settings.get("cloak_profile_id", "").strip()
    profile_name = settings.get("cloak_profile_name", "").strip()
    if not profile_id and not profile_name:
        raise RuntimeError("请先配置 Cloak profile 名称或 ID。")

    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
        async def rpc(method: str, params: dict[str, Any] | None = None) -> Any:
            rpc.next_id += 1
            response = await client.post(
                MCP_URL, headers=headers,
                json={"jsonrpc": "2.0", "id": rpc.next_id, "method": method, **({"params": params} if params is not None else {})},
            )
            response.raise_for_status()
            return response

        rpc.next_id = 0  # type: ignore[attr-defined]
        try:
            init = await rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "liveops", "version": "0.1"}})
        except httpx.HTTPError as exc:
            raise RuntimeError("Cloak Login MCP 服务不可用；请先启动 Cloak Login。") from exc
        session_id = init.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        _unpack(init.json())
        await client.post(MCP_URL, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

        async def call(name: str, arguments: dict[str, Any]) -> Any:
            response = await rpc("tools/call", {"name": name, "arguments": arguments})
            return _unpack(response.json())

        active = await call("list_active", {})
        if not isinstance(active, list):
            raise RuntimeError("Cloak Login 返回的运行 profile 列表格式异常。")
        match = next((p for p in active if (profile_id and p.get("id") == profile_id) or (not profile_id and p.get("name") == profile_name)), None)
        if match:
            endpoint = str(match.get("wsEndpoint") or "")
            if endpoint:
                return endpoint
        if not profile_id:
            profiles = await call("list_profiles", {})
            if not isinstance(profiles, list):
                raise RuntimeError("Cloak Login 返回的 profile 列表格式异常。")
            match = next((p for p in profiles if p.get("name") == profile_name), None)
            if not match:
                raise RuntimeError(f"Cloak Login 中找不到 profile：{profile_name}")
            profile_id = str(match["id"])
        started = await call("start_profile", {"id": profile_id})
        endpoint = str(started.get("wsEndpoint") or "") if isinstance(started, dict) else ""
        if not endpoint:
            raise RuntimeError("Cloak Login 已启动 profile，但未返回 CDP 地址。")
        return endpoint
