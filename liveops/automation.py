"""Independent screenshot clocks and durable, destination-specific delivery receipts."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from string import Formatter
from typing import Any

import httpx
from sqlalchemy import select

from liveops.collector import collect_screenshots
from liveops.feishu import FeishuClient, FeishuDeliveryUnknown
from liveops.models import (
    CollectionRun,
    CollectorModule,
    ScreenshotRequest,
    ShiftInterruption,
)
from liveops.portal import schedule_end_datetime
from liveops.sessions import local_datetime
from liveops.settings import screenshot_chat, setting_enabled

logger = logging.getLogger(__name__)
METRIC_TEMPLATE_FIELDS = {"gmv", "overall_amount", "overall_roi", "duration_hours"}


def end_at(schedule: Any) -> datetime:
    return schedule_end_datetime(schedule.work_date, schedule.start_time, schedule.end_time)










class ScreenshotAutomation:
    """Mixin; the dispatcher supplies its database, clock, logging and shared browser lock."""

    def _init_screenshots(self) -> None:
        self._delivery_lock = asyncio.Lock()
        self._hour_sync_lock = asyncio.Lock()
        self._recovered_screenshot_runs: list[str] = []

    def _recover_screenshot_requests(self) -> None:
        with self._automation_db() as db:
            interrupted: set[str] = set()
            for request in db.scalars(select(ScreenshotRequest).where(
                ScreenshotRequest.status.in_(["pending", "capturing"]), ScreenshotRequest.run_id.is_not(None)
            )).all():
                interrupted.add(request.run_id)
                request.status = "pending"
                request.run_id = None
            for request in db.scalars(select(ScreenshotRequest).where(
                ScreenshotRequest.status == "captured"
            )).all():
                if request.delivery_json.get("status") == "sending":
                    request.delivery_json = {**request.delivery_json, "status": "uncertain",
                                             "error": "后台在发送期间退出，请先核实群内是否已收到。"}
                elif not request.delivery_json.get("status"):
                    request.delivery_json = {"status": "failed", "error": "后台在发送前退出；截图已保留，可补发。"}
                run = db.get(CollectionRun, request.run_id) if request.run_id else None
                if run and run.status in {"queued", "running"}:
                    interrupted.add(run.id)
                    run.result_json = {**(run.result_json or {}), "screenshots": request.screenshots_json}
            for run_id in interrupted:
                run = db.get(CollectionRun, run_id)
                if run and run.status in {"queued", "running"}:
                    run.status = "failed"
                    run.message = "后台执行中断；已保存的截图和群发送结果仍保留，未完成的截图按时点恢复。"
                    run.finished_at = datetime.now(timezone.utc)
            db.commit()
        for run_id in interrupted:
            with self._automation_db() as db:
                run = db.get(CollectionRun, run_id)
                if run:
                    run.result_json = {**(run.result_json or {}), "screenshot_deliveries": self._delivery_summary(run_id)}
                    db.commit()

        self._recovered_screenshot_runs = list(interrupted)

    async def _notify_recovered_screenshot_run(self, run_id: str) -> None:
        from liveops.settings import db_settings

        with self._automation_db() as db:
            settings = db_settings(db, self.settings_store)
        await self._notify_run_problem(run_id, settings)


    def _anchor_request(self, db: Any, schedule: Any, settings: dict[str, str], run: Any = None) -> ScreenshotRequest:
        early = run is not None and self._automation_now() < end_at(schedule)
        request_id = f"anchor:{schedule.id}" + (f":{run.id}" if early else "")
        request = db.get(ScreenshotRequest, request_id)
        if request is None:
            request = ScreenshotRequest(id=request_id, target="anchor", schedule_id=schedule.id,
                planned_at=end_at(schedule), account_id=str(schedule.qianchuan_account_id
                    or settings.get("qianchuan_account_id", "")), room_json={}, status="pending",
                screenshots_json=[], delivery_json={})
            db.add(request)
        elif run is not None and run.trigger == "user":
            # Explicit manual recapture is intentional, unlike scheduler retries.
            request.status, request.screenshots_json, request.delivery_json = "pending", [], {}
        return request


    def _claim_screenshot_requests(self, run_id: str, schedule: Any, settings: dict[str, str]) -> list[str]:
        with self._automation_db() as db:
            run = db.get(CollectionRun, run_id)
            requests: list[ScreenshotRequest] = []
            if schedule is not None and run.kind != "interruption":
                if setting_enabled(settings, "anchor_screenshot_enabled"):
                    requests.append(self._anchor_request(db, schedule, settings, run))
                requests.extend(self._shared_hourly_requests(db, schedule, settings))
            else:
                requests = [db.get(ScreenshotRequest, key) for key in run.result_json.get("screenshot_request_ids", [])]
                requests = [row for row in requests if row is not None]
            claimed = []
            for request in requests:
                if request.status not in {"pending", "failed"}:
                    continue
                if not setting_enabled(settings, f"{request.target}_screenshot_enabled"):
                    continue
                request.status, request.run_id = "capturing", run_id
                claimed.append(request.id)
            run.result_json = {**(run.result_json or {}), "screenshot_request_ids": claimed,
                               "screenshot_targets": list(dict.fromkeys(row.target for row in requests))}
            db.commit()
            return claimed

    def _save_screenshot_capture(self, run_id: str, images: list[dict[str, Any]], room_id: str) -> None:
        with self._automation_db() as db:
            for request in db.scalars(select(ScreenshotRequest).where(
                ScreenshotRequest.run_id == run_id, ScreenshotRequest.status == "capturing"
            )).all():
                if request.target == "hourly" and request.id.startswith("shift-hour:"):
                    request.room_json = {**request.room_json, "live_room_id": room_id}
                if request.target == "hourly" and request.room_json.get("live_room_id") != room_id:
                    request.status, request.run_id = "pending", None
                    continue
                request.screenshots_json = [item for item in images if request.target == "anchor"
                                             or item.get("live_room_id") == room_id]
                request.status = "captured" if request.screenshots_json else "failed"
                if not request.screenshots_json:
                    request.delivery_json = {"status": "failed", "error": "本次没有生成可发送的截图。"}
            db.commit()

    def _screenshot_caption(self, request: ScreenshotRequest, schedule: Any, settings: dict[str, str],
                            metrics: dict[str, Any] | None) -> str:
        if request.target == "anchor":
            from liveops.service import _format_caption

            template = schedule.message_template or settings.get("report_message_template", "")
            return _format_caption(template, schedule, metrics or {key: "未采集" for key in METRIC_TEMPLATE_FIELDS})
        captured = next((item.get("captured_at") for item in reversed(request.screenshots_json)
                         if item.get("captured_at")), self._automation_now().isoformat())
        values = {"hour_index": request.hour_index, "shop_name": settings.get("shop_name", ""),
                  "live_room_id": request.room_json.get("live_room_id", ""),
                  "planned_time": local_datetime(request.planned_at).strftime("%Y-%m-%d %H:%M:%S"),
                  "captured_time": local_datetime(captured).strftime("%Y-%m-%d %H:%M:%S")}
        if request.schedule_id and request.id.startswith("shift-hour:"):
            from liveops.models import ShiftHourDetail, ShiftSchedule

            with self._automation_db() as db:
                parent = db.get(ShiftSchedule, request.schedule_id)
                detail = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == request.schedule_id,
                    ShiftHourDetail.period_index == request.hour_index))
                if detail and parent:
                    values.update({**detail.metrics_json, "host_name": parent.host_name, "promoter": parent.promoter,
                        "start_time": local_datetime(detail.start_at).strftime("%H:%M"),
                        "end_time": local_datetime(detail.end_at).strftime("%H:%M")})
        try:
            return settings["hourly_message_template"].format_map({key: "—" if value is None else value for key, value in values.items()})
        except (KeyError, ValueError, IndexError):
            return f"主播排班第{request.hour_index}段直播数据；计划 {values['planned_time']}；实际 {values['captured_time']}"

    def _delivery_summary(self, run_id: str) -> list[dict[str, Any]]:
        with self._automation_db() as db:
            return [{"request_id": row.id, "target": row.target, "capture_status": row.status,
                     "planned_at": local_datetime(row.planned_at).isoformat(), **row.delivery_json}
                    for row in db.scalars(select(ScreenshotRequest).where(ScreenshotRequest.run_id == run_id)).all()]

    async def _deliver_screenshot_requests(self, run_id: str, settings: dict[str, str],
                                          metrics: dict[str, Any] | None = None,
                                          data_pending: bool = False, log_run_id: str | None = None,
                                          retry: bool = False, request_ids: list[str] | None = None) -> str:
        log_id = log_run_id or run_id
        async with self._delivery_lock:
            with self._automation_db() as db:
                rows = db.scalars(select(ScreenshotRequest).where(ScreenshotRequest.run_id == run_id)).all()
            for row in rows:
                if request_ids is not None and row.id not in request_ids:
                    continue
                if row.status != "captured" or row.delivery_json.get("status") in {"sent", "sending", "uncertain"}:
                    continue
                if row.delivery_json.get("status") in {"failed", "not_configured"} and not retry:
                    continue
                if not setting_enabled(settings, f"{row.target}_screenshot_enabled"):
                    continue
                with self._automation_db() as db:
                    from liveops.models import ShiftSchedule

                    schedule = db.get(ShiftSchedule, row.schedule_id) if row.schedule_id else None
                if row.target == "anchor" and schedule is None:
                    continue
                template = schedule.message_template or settings.get("report_message_template", "") if schedule else ""
                try:
                    fields = {name for _, name, _, _ in Formatter().parse(template) if name}
                except ValueError:
                    fields = set()
                if row.target == "anchor" and data_pending and fields & METRIC_TEMPLATE_FIELDS:
                    continue
                if row.target == "hourly" and data_pending:
                    continue
                chat = (screenshot_chat(settings, row.target) if retry else
                        row.delivery_json.get("chat_id") or screenshot_chat(settings, row.target))
                caption = self._screenshot_caption(row, schedule, settings, metrics)
                receipt = {"status": "sending", "chat_id": chat, "caption": caption,
                           "at": self._automation_now().isoformat()}
                if not chat or not settings.get("feishu_app_id") or not settings.get("feishu_app_secret"):
                    receipt.update(status="not_configured", error=f"{'业务每小时群' if row.target == 'hourly' else '主播群'}或机器人凭证未配置。")
                with self._automation_db() as db:
                    db.get(ScreenshotRequest, row.id).delivery_json = receipt
                    db.commit()
                if receipt["status"] == "sending":
                    client = FeishuClient(settings["feishu_app_id"], settings["feishu_app_secret"], chat)
                    try:
                        result = await client.send_report(caption, row.screenshots_json)
                        if not result.get("message_id"):
                            raise RuntimeError("飞书未返回消息 ID，无法确认截图送达。")
                        receipt.update(status="sent", message_id=result["message_id"])
                    except (httpx.TransportError, FeishuDeliveryUnknown):
                        receipt.update(status="uncertain", error="网络中断，发送结果待确认；请先核实群内消息。")
                    except Exception as exc:
                        receipt.update(status="failed", error=str(exc)[:500])
                    finally:
                        await client.close()
                with self._automation_db() as db:
                    db.get(ScreenshotRequest, row.id).delivery_json = receipt
                    db.commit()
                label = "业务每小时群" if row.target == "hourly" else "主播群"
                self._log_step(log_id, f"feishu_{row.target}", f"{label}：{receipt['status']}",
                               "succeeded" if receipt["status"] == "sent" else "failed" if receipt["status"] in {"failed", "uncertain"} else "skipped")
            summary = self._delivery_summary(run_id)
            with self._automation_db() as db:
                for key in dict.fromkeys([run_id, log_id]):
                    run = db.get(CollectionRun, key)
                    if run:
                        run.result_json = {**(run.result_json or {}), "screenshot_deliveries": summary}
                db.commit()
            anchor = next((row for row in summary if row["target"] == "anchor"), None)
            if not anchor:
                return "disabled: 主播时段截图已关闭"
            status = anchor.get("status", "pending")
            if status == "uncertain":
                return "failed: 发送结果待确认，请先核实群内消息。"
            return f"{status}: {anchor['error']}" if anchor.get("error") else status

    async def begin_screenshot_delivery(self, source_run_id: str, trigger: str = "user",
                                         request_ids: list[str] | None = None) -> CollectionRun:
        with self._automation_db() as db:
            source = db.get(CollectionRun, source_run_id)
            if source is None:
                raise ValueError("原截图运行不存在。")
            source_run_id = (source.result_json or {}).get("source_run_id", source_run_id)
            rows = db.scalars(select(ScreenshotRequest).where(ScreenshotRequest.run_id == source_run_id,
                ScreenshotRequest.status == "captured")).all()
            eligible = [row for row in rows if row.delivery_json.get("status") not in {"sent", "sending", "uncertain"}
                        and (request_ids is None or row.id in request_ids)]
            if not eligible:
                raise RuntimeError("没有可补发的截图；发送结果待确认时请先核实群内消息。")
            run = CollectionRun(id=str(uuid.uuid4()), kind="screenshot_delivery", trigger=trigger,
                status="queued", result_json={"source_run_id": source_run_id,
                    "screenshot_request_ids": [row.id for row in eligible],
                    "screenshot_targets": list(dict.fromkeys(row.target for row in eligible)),
                    "data_collection": "disabled"})
            db.add(run)
            db.commit()
        task = asyncio.create_task(self._execute_screenshot_delivery(run.id), name=f"liveops-delivery-{run.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def _execute_screenshot_delivery(self, run_id: str) -> None:
        from liveops.settings import db_settings

        with self._automation_db() as db:
            run = db.get(CollectionRun, run_id)
            source_id = run.result_json["source_run_id"]
            ids = run.result_json["screenshot_request_ids"]
            settings = db_settings(db, self.settings_store)
            metrics = (db.get(CollectionRun, source_id).result_json or {}).get("metrics")
            run.status, run.started_at = "running", datetime.now(timezone.utc)
            db.commit()
        try:
            await self._deliver_screenshot_requests(source_id, settings, metrics=metrics,
                retry=True, request_ids=ids, log_run_id=run_id)
            summary = [row for row in self._delivery_summary(source_id) if row["request_id"] in ids]
            failed = [row for row in summary if row.get("status") != "sent"]
            with self._automation_db() as db:
                run = db.get(CollectionRun, run_id)
                run.status = "partial" if failed else "succeeded"
                run.message = "截图发送完成" if not failed else "部分截图未送达，请查看对应群结果。"
                run.missing_fields = [f"{row['target']}群发送未完成" for row in failed]
                run.finished_at = datetime.now(timezone.utc)
                db.commit()
        except Exception as exc:
            with self._automation_db() as db:
                run = db.get(CollectionRun, run_id)
                run.status, run.message, run.finished_at = "failed", str(exc)[:2000], datetime.now(timezone.utc)
                db.commit()
        finally:
            await self._notify_run_problem(run_id, settings)

    async def _execute_screenshot_only(self, run_id: str, schedule: Any, settings: dict[str, str]) -> None:
        self._log_step(run_id, "screenshots", "仅执行页面截图，不采集指标、基线或生成数据报表。")
        with self._automation_db() as db:
            requests = db.scalars(select(ScreenshotRequest).where(ScreenshotRequest.run_id == run_id)).all()
            if not requests:
                raise RuntimeError("本次任务没有启用的截图操作。")
            hourly = next((row for row in requests if row.target == "hourly"), None)
            room = dict(hourly.room_json) if schedule is None and hourly else None
            modules = db.scalars(select(CollectorModule).order_by(CollectorModule.sort_order)).all()
            pinned = self._schedule_room_id(db, schedule) if schedule else ""
            saved = [row.capture_json for row in db.scalars(select(ShiftInterruption).where(
                ShiftInterruption.schedule_id == schedule.id)).all()] if schedule else []
        def remember_room(selection: dict[str, Any]) -> None:
            if schedule is not None:
                self._remember_screenshot_room(schedule.id, selection["live_room_id"])
        async with self._browser_lock:
            browser_settings = {**settings, "feishu_chat_id": screenshot_chat(settings, "hourly")} if all(
                row.target == "hourly" for row in requests) else settings
            result = await collect_screenshots(settings=browser_settings, schedule=schedule, room=room, modules=modules,
                run_id=run_id, pinned_room_id=pinned, interruption_segments=saved,
                on_room_confirmed=remember_room,
                progress=lambda stage, message, status: self._log_step(run_id, stage, message, status))
        self._save_screenshot_capture(run_id, result["screenshots"], result["room"]["live_room_id"])
        await self._deliver_screenshot_requests(run_id, settings)
        summary = self._delivery_summary(run_id)
        errors = list(result["page_errors"])
        errors.extend(f"{row['target']}群发送未完成" for row in summary
                      if row.get("status") in {"failed", "uncertain", "not_configured"})
        with self._automation_db() as db:
            run = db.get(CollectionRun, run_id)
            run.status = "partial" if errors else "succeeded"
            run.missing_fields = errors
            run.message = "截图完成" if not errors else "截图或群发送不完整，请查看运行详情。"
            run.finished_at = datetime.now(timezone.utc)
            run.result_json = {**(run.result_json or {}), "screenshots": result["screenshots"],
                               "data_collection": "disabled", "room": result["room"]}
            db.commit()
        self._log_step(run_id, "finish", run.message, run.status)
