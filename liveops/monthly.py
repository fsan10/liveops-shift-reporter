"""Month-owned Bitable targets, protected manual tables and idempotent range sync."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select

from liveops.bitable import BitableClient
from liveops.feishu import FeishuError
from liveops.formulas import REPORT_COLUMNS
from liveops.models import (
    BitableMonthTable,
    BitableSyncReceipt,
    CollectionRun,
    ShiftHourDetail,
    ShiftHourState,
    ShiftReport,
)

HOT_MONTHS = 4


def hot_month_start(today: date) -> date:
    index = today.year * 12 + today.month - HOT_MONTHS
    return date(index // 12, index % 12 + 1, 1)


def base_scope(settings: dict[str, str]) -> str:
    return hashlib.sha256(settings.get("bitable_app_token", "").encode()).hexdigest()


def table_scope(settings: dict[str, str]) -> str:
    return hashlib.sha256((settings["bitable_app_token"] + "\0" + settings["bitable_table_id"]).encode()).hexdigest()


def base_ready(settings: dict[str, str]) -> bool:
    return all(settings.get(key, "").strip() for key in ("feishu_app_id", "feishu_app_secret", "bitable_app_token"))


def _canonical(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _month_row(db: Any, settings: dict[str, str], month: str) -> BitableMonthTable | None:
    return db.scalar(select(BitableMonthTable).where(
        BitableMonthTable.base_scope == base_scope(settings), BitableMonthTable.month_key == month))


def remember_manual_table(db: Any, store: Any, values: dict[str, str], today: date,
                          previous: dict[str, str] | None = None) -> None:
    """Called in the settings-save transaction; an explicit table change covers only this month."""
    month = today.strftime("%Y-%m")
    settings = {key: values.get(key, store.get(db, key)) for key in (
        "bitable_app_token", "bitable_table_id", "bitable_hourly_view_id")}
    if not settings["bitable_app_token"]:
        return
    row = _month_row(db, settings, month)
    table_changed = "bitable_table_id" in values or "bitable_app_token" in values
    if previous is not None:
        table_changed = any(settings[key] != previous.get(key, "") for key in ("bitable_app_token", "bitable_table_id"))
        if "bitable_table_id" in values and row and row.table_id != values["bitable_table_id"].strip():
            table_changed = True
    if not table_changed:
        if row and "bitable_hourly_view_id" in values:
            row.view_id = values["bitable_hourly_view_id"].strip()
        return
    table_id = settings["bitable_table_id"].strip()
    if row is None:
        row = BitableMonthTable(base_scope=base_scope(settings), month_key=month, tables_json=[])
        db.add(row)
    row.table_id, row.manual_override = table_id, bool(table_id)
    row.view_id = values.get("bitable_hourly_view_id", "").strip()
    if table_id and not any(item.get("table_id") == table_id for item in row.tables_json or []):
        owned = any(item.get("table_id") == table_id and item.get("managed")
            for other in db.scalars(select(BitableMonthTable).where(BitableMonthTable.base_scope == base_scope(settings)))
            for item in other.tables_json or [])
        row.tables_json = [*(row.tables_json or []), {"table_id": table_id, "managed": owned, "status": "active"}]
    store.set(db, "bitable_table_month", month)
    store.set(db, "bitable_table_mode", "manual" if table_id else "automatic")
    store.set(db, "bitable_hourly_view_id", row.view_id)


def month_fields(settings: dict[str, str]) -> list[dict[str, Any]]:
    mapping = json.loads(settings.get("bitable_field_mapping", "{}"))
    if not isinstance(mapping, dict):
        raise FeishuError("字段映射必须是 JSON 对象。")
    sync_key = settings.get("bitable_sync_key_field", "SourceID").strip()
    if not sync_key or sync_key in mapping.values():
        raise FeishuError("SourceID 必须使用独立的文本幂等键列。")
    kinds = {key: kind for key, _, kind in REPORT_COLUMNS}
    fields = {sync_key: {"field_name": sync_key, "type": 1}}
    for key, name in mapping.items():
        if not name:
            continue
        kind = kinds.get(key, "text")
        fields.setdefault(name, {"field_name": name, "type": 5 if kind == "date" else 1 if kind == "text" else 2})
    return list(fields.values())


class MonthlyBitableAutomation:
    def _init_monthly_bitable(self) -> None:
        self._bitable_month_lock = asyncio.Lock()
        self._bitable_range_lock = asyncio.Lock()

    def _month_client(self, settings: dict[str, str]) -> BitableClient:
        return BitableClient(settings["feishu_app_id"], settings["feishu_app_secret"],
                             settings["bitable_app_token"], settings.get("bitable_table_id", ""))

    async def _bitable_settings_for_date(self, settings: dict[str, str], work_date: date) -> dict[str, str]:
        if not base_ready(settings) or work_date < hot_month_start(self._automation_now().date()):
            return settings
        month = work_date.strftime("%Y-%m")
        if settings.get("_bitable_pin_target") == "true" and settings.get("_bitable_resolved_month") == month:
            return settings
        current_month = self._automation_now().strftime("%Y-%m")
        async with self._bitable_month_lock:
            with self._automation_db() as db:
                row = _month_row(db, settings, month)
                if row is None:
                    # Only an explicit override for this month may bypass automatic monthly routing.
                    adopt = (month == current_month and bool(settings.get("bitable_table_id"))
                             and settings.get("bitable_table_month") == current_month
                             and settings.get("bitable_table_mode") == "manual")
                    table_id = settings.get("bitable_table_id", "") if adopt else ""
                    row = BitableMonthTable(base_scope=base_scope(settings), month_key=month,
                        table_id=table_id, manual_override=adopt,
                        view_id=settings.get("bitable_hourly_view_id", "") if adopt else "",
                        tables_json=[{"table_id": table_id, "managed": False, "status": "active"}] if adopt else [])
                    db.add(row)
                    db.flush()
                if not row.table_id and not row.create_name:
                    row.create_name = f"{work_date.year}年{work_date.month:02d}月直播数据-{uuid.uuid4().hex[:8]}"
                row_id, table_id, create_name = row.id, row.table_id, row.create_name
                uncertain = row.create_uncertain
                owned_ids = {item["table_id"] for item in row.tables_json or []
                             if item.get("managed") and item.get("status") != "deleted"}
                if not table_id and month == current_month and self.settings_store is not None:
                    if self.settings_store.get(db, "bitable_app_token") == settings["bitable_app_token"]:
                        for key, value in {"bitable_table_id": "", "bitable_hourly_view_id": "",
                                "bitable_table_month": month, "bitable_table_mode": "automatic"}.items():
                            self.settings_store.set(db, key, value)
                db.commit()
            if not table_id:
                client = self._month_client(settings)
                try:
                    matches = [item for item in await client.list_tables() if item.get("name") == create_name]
                    if len(matches) > 1:
                        raise FeishuError("月表名称出现重复，停止创建；请核对后人工指定 table_id。")
                    if matches:
                        if not uncertain and matches[0].get("table_id") not in owned_ids:
                            raise FeishuError("发现同名但未登记创建请求的数据表，不自动接管；请人工指定目标表。")
                        created = {"table_id": matches[0]["table_id"], "view_id": ""}
                    else:
                        if uncertain:
                            raise FeishuError("上次建表结果待确认，完整列表暂未找到该月表；请在飞书核对或人工指定 table_id，避免重复建表。")
                        fields = month_fields(settings)
                        if client.table_id:
                            try:
                                template = {field["field_name"]: field for field in await client.list_fields()}
                            except FeishuError:
                                template = {}
                            for field in fields:
                                previous = template.get(field["field_name"], {})
                                formatter = (previous.get("property") or {}).get("formatter")
                                if field["type"] in {2, 5} and previous.get("type") == field["type"] and isinstance(formatter, str):
                                    field["property"] = {"formatter": formatter}
                        with self._automation_db() as db:
                            db.get(BitableMonthTable, row_id).create_uncertain = True
                            db.commit()
                        created = await client.create_table(create_name, fields)
                    with self._automation_db() as db:
                        row = db.get(BitableMonthTable, row_id)
                        history = list(row.tables_json or [])
                        if not any(item.get("table_id") == created["table_id"] for item in history):
                            history.append({"table_id": created["table_id"], "managed": True, "status": "active", "name": create_name})
                        row.tables_json, row.create_uncertain = history, False
                        if not row.manual_override and not row.table_id:
                            row.table_id, row.view_id = created["table_id"], created.get("view_id", "")
                        db.commit()
                finally:
                    await client.close()
            with self._automation_db() as db:
                row = db.get(BitableMonthTable, row_id)
                resolved = {**settings, "bitable_table_id": row.table_id, "bitable_hourly_view_id": row.view_id,
                            "bitable_table_month": month, "bitable_table_mode": "manual" if row.manual_override else "automatic",
                            "_bitable_resolved_month": month}
                # Do not let finishing an old-month retry change the current UI destination.
                if month == current_month and self.settings_store is not None:
                    if self.settings_store.get(db, "bitable_app_token") == settings["bitable_app_token"]:
                        for key in ("bitable_table_id", "bitable_hourly_view_id", "bitable_table_month", "bitable_table_mode"):
                            self.settings_store.set(db, key, resolved[key])
                        db.commit()
                return resolved

    async def _write_bitable_record(self, client: BitableClient, settings: dict[str, str], *,
                                     values: dict[str, Any], field_mapping: dict[str, str], sync_key: str,
                                     **_legacy: Any) -> dict[str, Any]:
        scope = table_scope(settings)
        mapped_values = {key: values[key] for key, name in field_mapping.items() if name and key in values}
        fingerprint = hashlib.sha256(json.dumps({"values": _canonical(mapped_values), "mapping": field_mapping,
            "sync_key_field": settings.get("bitable_sync_key_field", "SourceID")},
            sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        with self._automation_db() as db:
            receipt = db.scalar(select(BitableSyncReceipt).where(BitableSyncReceipt.scope == scope,
                BitableSyncReceipt.sync_key == sync_key))
            if (receipt and receipt.status == "synced" and receipt.fingerprint == fingerprint
                    and settings.get("_bitable_force") != "true"):
                return {"record_id": receipt.record_id, "missing_mappings": [], "skipped": True}
            record_id = receipt.record_id if receipt else ""
        try:
            await client.ensure_sync_key(settings.get("bitable_sync_key_field", "SourceID"))
            result = await client.upsert_record(values=values, field_mapping=field_mapping, record_id=record_id,
                sync_key_field=settings.get("bitable_sync_key_field", "SourceID"), sync_key=sync_key, lookup_sync_key=True)
        except Exception:
            with self._automation_db() as db:
                failed = db.scalar(select(BitableSyncReceipt).where(BitableSyncReceipt.scope == scope,
                    BitableSyncReceipt.sync_key == sync_key))
                if failed is None:
                    failed = BitableSyncReceipt(scope=scope, sync_key=sync_key, record_id=record_id, fingerprint=fingerprint)
                    db.add(failed)
                failed.status = "failed"
                db.commit()
            raise
        with self._automation_db() as db:
            receipt = db.scalar(select(BitableSyncReceipt).where(BitableSyncReceipt.scope == scope,
                BitableSyncReceipt.sync_key == sync_key))
            if receipt is None:
                receipt = BitableSyncReceipt(scope=scope, sync_key=sync_key)
                db.add(receipt)
            receipt.record_id, receipt.fingerprint = result["record_id"], fingerprint
            receipt.status = "partial" if result.get("missing_mappings") else "synced"
            receipt.synced_at = datetime.now(timezone.utc)
            db.commit()
        return result

    async def _archive_cold_bitable_tables(self, settings: dict[str, str]) -> tuple[int, list[str]]:
        if not base_ready(settings):
            return 0, []
        cutoff = hot_month_start(self._automation_now().date()).strftime("%Y-%m")
        async with self._bitable_month_lock:
            with self._automation_db() as db:
                rows = db.scalars(select(BitableMonthTable).where(BitableMonthTable.base_scope == base_scope(settings))).all()
                protected = {row.table_id for row in rows if row.month_key >= cutoff and row.table_id}
                protected.add(settings.get("bitable_table_id", ""))
                manual = {item["table_id"] for row in rows for item in row.tables_json or [] if not item.get("managed")}
                candidates = [(row.id, item["table_id"]) for row in rows if row.month_key < cutoff
                    for item in row.tables_json or [] if item.get("managed") and item.get("status") != "deleted"
                    and item["table_id"] not in protected | manual]
            if not candidates:
                return 0, []
            client = self._month_client(settings)
            deleted, failures = 0, []
            try:
                existing = {row["table_id"] for row in await client.list_tables()}
                for row_id, table_id in candidates:
                    try:
                        if table_id in existing:
                            await client.delete_table(table_id)
                            existing.remove(table_id)
                        scope = table_scope({**settings, "bitable_table_id": table_id})
                        with self._automation_db() as db:
                            row = db.get(BitableMonthTable, row_id)
                            row.tables_json = [{**item, "status": "deleted"} if item["table_id"] == table_id else item
                                               for item in row.tables_json]
                            for receipt in db.scalars(select(BitableSyncReceipt).where(BitableSyncReceipt.scope == scope)):
                                receipt.status = "archived"
                            for detail in db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.bitable_scope == scope)):
                                detail.bitable_status = "archived: 月表已清理，本地数据永久保留"
                                detail.bitable_record_id = ""
                            for state in db.scalars(select(ShiftHourState).where(ShiftHourState.bitable_scope == scope)):
                                state.parent_record_id = ""
                            for report in db.scalars(select(ShiftReport)):
                                if report.raw_json.get("bitable_scope") == scope:
                                    report.bitable_record_id = ""
                                    report.bitable_status = "archived: 月表已清理，本地数据永久保留"
                            db.commit()
                        deleted += 1
                    except Exception:
                        failures.append(table_id)
            finally:
                await client.close()
            return deleted, failures

    async def begin_bitable_range_sync(self, left: date, right: date, mode: str) -> CollectionRun:
        if left > right or mode not in {"incremental", "full"}:
            raise ValueError("日期范围或同步模式无效。")
        if self._bitable_range_lock.locked() or any(task.get_name() == "liveops-bitable-range-sync" for task in self._tasks):
            raise RuntimeError("已有范围同步任务运行中，请等待完成后再试。")
        with self._automation_db() as db:
            report_ids = list(db.scalars(select(ShiftReport.schedule_id).where(
                ShiftReport.work_date.between(max(left, hot_month_start(self._automation_now().date())), right))
                .order_by(ShiftReport.work_date, ShiftReport.start_time)))
            if not report_ids:
                raise ValueError("所选日期没有可同步的近 4 个月报告；更早历史仅保存在本地。")
            run = CollectionRun(id=str(uuid.uuid4()), kind="bitable_sync", trigger="user", status="queued",
                result_json={"date_start": left.isoformat(), "date_end": right.isoformat(), "mode": mode,
                             "data_collection": "disabled", "screenshot_targets": [], "schedule_ids": report_ids})
            db.add(run)
            db.commit()
        task = asyncio.create_task(self._execute_bitable_range_sync(run.id), name="liveops-bitable-range-sync")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def _execute_bitable_range_sync(self, run_id: str) -> None:
        from liveops.settings import db_settings

        async with self._bitable_range_lock:
            with self._automation_db() as db:
                run = db.get(CollectionRun, run_id)
                run.status, run.started_at = "running", datetime.now(timezone.utc)
                settings = db_settings(db, self.settings_store)
                ids, mode = run.result_json["schedule_ids"], run.result_json["mode"]
                db.commit()
            failures = []
            try:
                if not base_ready(settings):
                    raise FeishuError("飞书应用凭证或 app_token 未配置，无法同步。")
                for index, schedule_id in enumerate(ids, 1):
                    try:
                        await self._sync_integrations(schedule_id, {**settings, "_bitable_force": str(mode == "full").lower()},
                                                      run_id, send_feishu=False)
                        with self._automation_db() as db:
                            parent = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule_id))
                            if parent.bitable_status.startswith(("failed", "partial", "not_configured")):
                                failures.append(schedule_id)
                    except Exception as exc:
                        failures.append(schedule_id)
                        self._log_step(run_id, "bitable_range", f"排班 {schedule_id} 同步失败：{str(exc)[:300]}", "failed")
                    self._log_step(run_id, "bitable_range", f"范围同步进度 {index}/{len(ids)}。", "info")
                with self._automation_db() as db:
                    run = db.get(CollectionRun, run_id)
                    run.status = "partial" if failures else "succeeded"
                    run.message = f"范围同步完成：{len(ids) - len(failures)}/{len(ids)} 个主播时段成功，包含小时明细。"
                    run.result_json = {**run.result_json, "failed_schedule_ids": failures}
                    run.missing_fields = [f"排班 {key} 同步异常" for key in failures]
                    run.finished_at = datetime.now(timezone.utc)
                    db.commit()
            except Exception as exc:
                with self._automation_db() as db:
                    run = db.get(CollectionRun, run_id)
                    run.status, run.message = "failed", str(exc)[:1500]
                    run.finished_at = datetime.now(timezone.utc)
                    db.commit()
            finally:
                await self._notify_run_problem(run_id, settings)
