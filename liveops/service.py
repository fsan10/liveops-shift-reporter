from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from liveops.alerts import diagnose_failure
from liveops.automation import ScreenshotAutomation
from liveops.bitable import BitableClient
from liveops.collector import collect_baseline, collect_report
from liveops.db import SessionLocal
from liveops.feishu import FeishuClient
from liveops.formulas import FORMULA_VERSION, build_report_metrics
from liveops.models import (
    CollectionRun,
    CollectorModule,
    ScreenshotRequest,
    ShiftInterruption,
    ShiftReport,
    ShiftSchedule,
)
from liveops.portal import qianchuan_snapshot, report_source_values
from liveops.segments import recompute_segments
from liveops.settings import SettingsStore, db_settings, screenshot_chat, setting_enabled
from liveops.snapshots import (
    aggregate_daily_room_exposure,
    capture_snapshot,
    recompute_handoffs,
)

logger = logging.getLogger(__name__)
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Shanghai"))
RUN_COLUMNS = "id, schedule_id, kind, trigger, status, message, missing_fields, started_at, finished_at, created_at"


def now_local() -> datetime:
    return datetime.now(APP_TIMEZONE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def schedule_start(schedule: ShiftSchedule) -> datetime:
    return datetime.combine(schedule.work_date, schedule.start_time, tzinfo=APP_TIMEZONE)


def schedule_end(schedule: ShiftSchedule) -> datetime:
    end_date = (
        schedule.work_date + timedelta(days=1)
        if schedule.end_time <= schedule.start_time
        else schedule.work_date
    )
    return datetime.combine(end_date, schedule.end_time, tzinfo=APP_TIMEZONE)


def _format_caption(template: str, schedule: ShiftSchedule, metrics: dict[str, Any]) -> str:
    start = schedule.start_time
    end = schedule.end_time
    start_label = str(start.hour) if start.minute == 0 else start.strftime("%H:%M")
    values: dict[str, Any] = {
        "host_name": schedule.host_name,
        "host": schedule.host_name,
        "promoter": schedule.promoter,
        "date": schedule.work_date.strftime("%Y.%m.%d"),
        "start_time": start_label,
        "start_time_full": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
        "duration_hours": metrics.get("duration_hours", ""),
        "gmv": metrics.get("gmv", ""),
        "overall_amount": metrics.get("overall_amount", ""),
        "overall_roi": metrics.get("overall_roi", ""),
    }

    class MissingFormatKey(dict[str, Any]):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return template.format_map(MissingFormatKey(values))
    except (ValueError, IndexError):
        return f"以上主播{schedule.host_name}{start_label}-{end.strftime('%H:%M')}点的直播数据"


def _integration_ready(settings: dict[str, str], prefix: str) -> bool:
    if prefix == "feishu":
        return bool(
            settings.get("feishu_app_id")
            and settings.get("feishu_app_secret")
            and settings.get("feishu_chat_id")
        )
    return bool(
        settings.get("feishu_app_id")
        and settings.get("feishu_app_secret")
        and settings.get("bitable_app_token")
        and settings.get("bitable_table_id")
    )


def _baseline_from_report(report: ShiftReport) -> dict[str, Any] | None:
    raw = report.raw_json or {}
    if raw.get("is_final") is False:
        return None
    if "is_final" not in raw and raw.get("collected_at") and raw.get("schedule_end"):
        if datetime.fromisoformat(raw["collected_at"]) < datetime.fromisoformat(raw["schedule_end"]):
            return None
    segments = raw.get("segments") or []
    if segments:
        rooms = {
            item["room"]["live_room_id"]: {
                **(item.get("snapshot") or capture_snapshot(
                    item["responses"], item["room"]["live_room_id"],
                    item["qianchuan_account_id"], item["captured_at"],
                )),
                "live_room_id": item["room"]["live_room_id"],
                "qianchuan_account_id": item["qianchuan_account_id"],
                "qianchuan": (item.get("snapshot") or {}).get("qianchuan") or qianchuan_snapshot(item["responses"]),
                "captured_at": item["captured_at"],
            }
            for item in segments if item["source"].get("end_snapshot_valid", True)
        }
        last = max(segments, key=lambda item: item["source"]["segment_end"])
        room_id = last["room"]["live_room_id"]
        if room_id not in rooms:
            return None
        return {**rooms[room_id], "rooms": rooms, "author_id": raw.get("author_id", ""),
                "source": "previous_shift_end", "source_run_id": report.latest_run_id,
                "live_end": last["room"].get("live_end")}
    qianchuan = qianchuan_snapshot(raw.get("responses") or [])
    if not qianchuan:
        return None
    return {
        **capture_snapshot(raw.get("responses") or [], report.live_room_id,
                           (raw.get("source") or {}).get("qianchuan_account_id", ""),
                           raw.get("collected_at") or report.created_at.isoformat()),
        "live_room_id": report.live_room_id,
        "qianchuan": qianchuan,
        "captured_at": raw.get("collected_at") or report.created_at.isoformat(),
        "source": "previous_shift_end",
        "source_run_id": report.latest_run_id,
    }


def _previous_shift(db: Any, schedule: ShiftSchedule) -> ShiftSchedule | None:
    return db.scalar(
        select(ShiftSchedule)
        .where(
            ShiftSchedule.work_date == schedule.work_date,
            ShiftSchedule.enabled.is_(True),
            ShiftSchedule.start_time < schedule.start_time,
        )
        .order_by(ShiftSchedule.start_time.desc())
        .limit(1)
    )


def _is_last_schedule_for_date(db: Any, schedule: ShiftSchedule) -> bool:
    schedules = db.scalars(
        select(ShiftSchedule).where(
            ShiftSchedule.work_date == schedule.work_date,
            ShiftSchedule.enabled.is_(True),
        )
    ).all()
    if not schedules:
        return False
    last = max(schedules, key=lambda row: (schedule_end(row), schedule_start(row)))
    return last.id == schedule.id


def _exposure_segment(
    *, room_id: str, account_id: str, captured_at: str | None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    captured = snapshot or {
        "live_room_id": room_id,
        "qianchuan_account_id": account_id,
        "captured_at": captured_at,
        "qianchuan": {},
    }
    return {
        "room": {"live_room_id": room_id},
        "qianchuan_account_id": account_id,
        "captured_at": captured_at,
        "snapshot": captured,
        "source": {"live_room_id": room_id, "qianchuan_account_id": account_id},
    }


def _report_exposure_segments(
    report: ShiftReport, schedule: ShiftSchedule | None
) -> list[dict[str, Any]]:
    raw = report.raw_json or {}
    stored = raw.get("segments") or []
    if stored:
        result: list[dict[str, Any]] = []
        for item in stored:
            segment = dict(item)
            source = segment.get("source") or {}
            room = segment.get("room") or {}
            room_id = str(room.get("live_room_id") or source.get("live_room_id") or report.live_room_id)
            account_id = str(
                segment.get("qianchuan_account_id")
                or source.get("qianchuan_account_id")
                or (segment.get("snapshot") or {}).get("qianchuan_account_id")
                or (schedule.qianchuan_account_id if schedule else "")
            )
            snapshot = segment.get("snapshot") or source.get("snapshot")
            captured_at = (
                segment.get("captured_at") or source.get("captured_at")
                or (snapshot or {}).get("captured_at") or source.get("segment_end")
                or raw.get("collected_at")
            )
            if snapshot is None and segment.get("responses"):
                try:
                    snapshot = capture_snapshot(
                        segment["responses"], room_id, account_id, str(captured_at or "")
                    )
                except (RuntimeError, TypeError, ValueError):
                    snapshot = None
            result.append(_exposure_segment(
                room_id=room_id, account_id=account_id,
                captured_at=str(captured_at) if captured_at else None,
                snapshot=snapshot,
            ))
        return result

    source = raw.get("source") or {}
    room_id = str(source.get("live_room_id") or report.live_room_id or "")
    account_id = str(
        source.get("qianchuan_account_id") or (schedule.qianchuan_account_id if schedule else "")
    )
    captured_at = source.get("captured_at") or raw.get("collected_at") or source.get("segment_end")
    snapshot = source.get("snapshot")
    responses = raw.get("responses") or []
    response_room_ids = sorted({
        str(item.get("live_room_id")) for item in responses if item.get("live_room_id")
    })
    if snapshot is None and len(response_room_ids) > 1 and captured_at:
        recovered: list[dict[str, Any]] = []
        for response_room_id in response_room_ids:
            room_responses = [
                item for item in responses if str(item.get("live_room_id") or "") == response_room_id
            ]
            response_accounts = {
                str(item.get("qianchuan_account_id")) for item in room_responses
                if item.get("qianchuan_account_id")
            }
            room_account = next(iter(response_accounts), account_id) if len(response_accounts) <= 1 else ""
            room_snapshot = None
            if len(response_accounts) <= 1:
                try:
                    room_snapshot = capture_snapshot(
                        room_responses, response_room_id, room_account, str(captured_at)
                    )
                except (RuntimeError, TypeError, ValueError):
                    room_snapshot = None
            recovered.append(_exposure_segment(
                room_id=response_room_id, account_id=room_account,
                captured_at=str(captured_at), snapshot=room_snapshot,
            ))
        return recovered
    if snapshot is None and responses and room_id and captured_at:
        try:
            snapshot = capture_snapshot(responses, room_id, account_id, str(captured_at))
        except (RuntimeError, TypeError, ValueError):
            snapshot = None
    return [_exposure_segment(
        room_id=room_id, account_id=account_id,
        captured_at=str(captured_at) if captured_at else None,
        snapshot=snapshot,
    )]


def _daily_exposure_for_date(
    db: Any, work_date: date, current_segments: list[dict[str, Any]]
) -> dict[str, Any]:
    schedules = db.scalars(
        select(ShiftSchedule).where(ShiftSchedule.work_date == work_date)
    ).all()
    schedules_by_id = {row.id: row for row in schedules}
    reports = db.scalars(
        select(ShiftReport).where(ShiftReport.work_date == work_date)
    ).all()
    segments: list[dict[str, Any]] = []
    for report in reports:
        segments.extend(_report_exposure_segments(report, schedules_by_id.get(report.schedule_id)))
    interruptions = db.scalars(
        select(ShiftInterruption)
        .join(ShiftSchedule, ShiftInterruption.schedule_id == ShiftSchedule.id)
        .where(ShiftSchedule.work_date == work_date)
    ).all()
    segments.extend(dict(row.capture_json) for row in interruptions)
    segments.extend(current_segments)
    return aggregate_daily_room_exposure(segments)


def _current_baseline(db: Any, schedule: ShiftSchedule) -> dict[str, Any]:
    previous = _previous_shift(db, schedule)
    if previous is None:
        return {"source": "first_shift_zero", "qianchuan": {}, "captured_at": None}
    report = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == previous.id))
    baseline = _baseline_from_report(report) if report else None
    if baseline is None:
        return {"source": "previous_shift_missing", "qianchuan": {}, "rooms": {}}
    return baseline


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def integration_not_configured_status(settings: dict[str, str], integration: str) -> str:
    requirements = {
        "feishu": [
            ("feishu_app_id", "飞书 App ID"),
            ("feishu_app_secret", "飞书 App Secret"),
            ("feishu_chat_id", "群聊 Chat ID"),
        ],
        "bitable": [
            ("feishu_app_id", "飞书 App ID"),
            ("feishu_app_secret", "飞书 App Secret"),
            ("bitable_app_token", "多维表格 app_token"),
            ("bitable_table_id", "数据表 table_id"),
        ],
    }
    missing = [
        label for key, label in requirements[integration] if not settings.get(key, "").strip()
    ]
    if not missing:
        return "not_configured: 配置未完整，检查系统配置。"
    return "not_configured: 尚缺 " + "、".join(missing)


class RunDispatcher(ScreenshotAutomation):
    def __init__(self, settings_store: SettingsStore) -> None:
        self.settings_store = settings_store
        self._browser_lock = asyncio.Lock()
        self._alert_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._queued: set[tuple[str, int]] = set()
        self._syncing: dict[int, int] = {}
        self._integration_locks: dict[int, asyncio.Lock] = {}
        self._last_auto_attempt: dict[tuple[str, int], datetime] = {}
        self._bitable_retention_checked_month = ""
        self._bitable_retention_retry_at: datetime | None = None
        self._scheduler_task: asyncio.Task[Any] | None = None
        self._init_screenshots()

    def _automation_db(self) -> Any:
        return SessionLocal()

    def _automation_now(self) -> datetime:
        return now_local()

    def _schedule_room_id(self, db: Any, schedule: ShiftSchedule | None) -> str:
        if schedule is None:
            return ""
        if schedule.live_room_id:
            return schedule.live_room_id
        if (schedule.baseline_json or {}).get("live_room_id"):
            return schedule.baseline_json["live_room_id"]
        previous = _previous_shift(db, schedule)
        return previous.live_room_id if previous else ""

    def _remember_screenshot_room(self, schedule_id: int, room_id: str) -> None:
        with SessionLocal() as db:
            schedule = db.get(ShiftSchedule, schedule_id)
            if schedule:
                schedule.live_room_id = room_id
            db.commit()

    def _log_step(self, run_id: str, stage: str, description: str, status: str = "info") -> None:
        entry = {
            "at": now_local().isoformat(timespec="seconds"),
            "stage": stage,
            "status": status,
            "description": description,
        }
        with SessionLocal() as db:
            run = db.get(CollectionRun, run_id)
            if run:
                result = dict(run.result_json or {})
                result["steps"] = [*(result.get("steps") or []), entry]
                run.result_json = result
                db.commit()

    async def _notify_run_problem(self, run_id: str, settings: dict[str, str]) -> None:
        with SessionLocal() as db:
            run = db.get(CollectionRun, run_id)
            result = dict(run.result_json or {}) if run else {}
            targets = list(result.get("screenshot_targets") or [])
            receipts = self._delivery_summary(run_id)
        if not targets:
            await self._notify_single_run_problem(run_id, settings)
            return
        # A successful hourly image delivery must not inherit a shift metric error.
        if "hourly" in targets and any(row["target"] == "hourly" and row.get("status") == "sent" for row in receipts):
            targets.remove("hourly")
        if result.get("data_collection") != "disabled" and run and run.schedule_id and "anchor" not in targets:
            targets.append("anchor")
        destinations: set[str] = set()
        for index, target in enumerate(targets):
            chat = screenshot_chat(settings, target)
            if chat in destinations:
                continue
            destinations.add(chat)
            await self._notify_single_run_problem(run_id, {**settings, "feishu_chat_id": chat},
                "exception_alert" if index == 0 else f"exception_alert_{target}")

    async def _notify_single_run_problem(self, run_id: str, settings: dict[str, str],
                                        receipt_key: str = "exception_alert") -> None:
        """Send at most one terminal exception alert per run, with durable delivery status."""
        async with self._alert_lock:
            with SessionLocal() as db:
                run = db.get(CollectionRun, run_id)
                if run is None or run.status not in {"failed", "partial", "succeeded"}:
                    return
                result = dict(run.result_json or {})
                if result.get(receipt_key):
                    return
                # An intentionally provisional manual capture needs no human repair.
                issues = [item for item in run.missing_fields if not item.startswith("临时快照:")]
                integrations = result.get("integrations") or {}
                broken = [name for name, status in integrations.items()
                          if str(status).startswith(("failed", "partial"))]
                if run.status != "failed" and not issues and not broken:
                    return
                schedule = db.get(ShiftSchedule, run.schedule_id) if run.schedule_id else None
                steps = result.get("steps") or []
                failed_stages = list(dict.fromkeys(
                    step["stage"] for step in steps
                    if step.get("status") == "failed" and step.get("stage") != "finish"
                ))
                if not failed_stages:
                    failed_stages = [next((step["stage"] for step in reversed(steps)
                                           if step.get("stage") not in {"finish", "exception_alert"}), "unknown")]
                labels = {"browser": "浏览器/抖店登录", "room": "场次确认", "detail": "罗盘详情",
                          "detail_funnel": "漏斗截图", "pro": "专业版", "base": "基础版",
                          "qianchuan": "千川", "baseline": "交接基线", "metrics": "指标计算",
                          "feishu": "报告群消息", "bitable": "多维表格", "start": "任务初始化",
                          "source": "接口解析", "pro_traffic": "流量截图", "pro_anchor": "主播截图",
                           "base_full": "基础版截图", "qianchuan_full": "千川截图",
                           "feishu_anchor": "主播群截图", "feishu_hourly": "业务群截图", "screenshots": "页面截图"}
                stage_text = "、".join(labels.get(stage, "采集/同步") for stage in failed_stages)
                cause, action = diagnose_failure(run.message if run.status == "failed" else "；".join(issues))
                if schedule is None:
                    action = action.replace("重新采集该排班", "确认后等待下一小时截图").replace("排班", "对应运行")
                if broken:
                    sync_cause = "同步异常：" + "、".join(labels.get(name, "同步") for name in broken) + "。"
                    cause = cause + " " + sync_cause if issues or run.status == "failed" else sync_cause
                    action += " 已保存的数据可在后台重试同步；先查看各同步结果，避免重复发送群报告。"
                period = (f"{schedule.work_date.isoformat()} {schedule.start_time:%H:%M}–{schedule.end_time:%H:%M}"
                          if schedule else "每小时直播截图（独立于主播排班）")
                host = schedule.host_name if schedule else "无需主播排班"
                message = (
                    f"{'采集失败' if run.status == 'failed' else '采集/同步不完整'}，需要人工处理。\n"
                    f"排班：{period}；主播：{host}\n"
                    f"异常时间：{now_local():%Y-%m-%d %H:%M:%S}（北京时间）\n"
                    f"失败环节：{stage_text}；缺失/异常项：{len(issues)}\n"
                    f"原因判断：{cause}\n处理建议：{action}\n"
                    f"运行编号：{run.id}（后台采集记录可查看详细错误）"
                )
                alert = {"status": "sending", "at": now_local().isoformat()}
                if not _integration_ready(settings, "feishu"):
                    alert.update(status="not_configured", error=integration_not_configured_status(settings, "feishu"))
                run.result_json = {**result, receipt_key: alert}
                db.commit()
            if alert["status"] == "not_configured":
                self._log_step(run_id, "exception_alert", alert["error"], "skipped")
                return
            client = FeishuClient(settings["feishu_app_id"], settings["feishu_app_secret"], settings["feishu_chat_id"])
            try:
                async with asyncio.timeout(55):
                    response = await client.send_alert(
                        message, settings.get("feishu_alert_open_id", ""), title="直播运营异常提醒"
                    )
                alert.update(status="sent", message_id=response["message_id"])
                self._log_step(run_id, "exception_alert", "异常提醒已发送到飞书群。", "succeeded")
            except Exception as exc:
                logger.warning("Exception alert failed for run %s (%s)", run_id, type(exc).__name__)
                alert.update(status="failed", error="异常提醒发送失败，请检查飞书配置、权限或网络。")
                self._log_step(run_id, "exception_alert", alert["error"], "failed")
            finally:
                with SessionLocal() as db:
                    current = db.get(CollectionRun, run_id)
                    if current:
                        current.result_json = {**(current.result_json or {}), receipt_key: alert}
                        db.commit()
                await client.close()

    async def _send_feishu_images(
        self, settings: dict[str, str], caption: str, screenshots: list[dict[str, Any]], run_id: str
    ) -> str:
        if not _integration_ready(settings, "feishu"):
            status = integration_not_configured_status(settings, "feishu")
            self._log_step(run_id, "feishu", status, "skipped")
            return status
        client = FeishuClient(
            settings["feishu_app_id"], settings["feishu_app_secret"], settings["feishu_chat_id"]
        )
        try:
            self._log_step(run_id, "feishu", f"开始上传 {len(screenshots)} 张截图并发送群消息。")
            await client.send_report(
                caption, screenshots,
                on_progress=lambda message: self._log_step(run_id, "feishu", message, "succeeded"),
            )
            self._log_step(run_id, "feishu", "截图和说明已发送到飞书群。", "succeeded")
            return "sent"
        except Exception as exc:
            logger.warning("Feishu send failed for run %s: %s", run_id, exc)
            self._log_step(run_id, "feishu", f"发送失败：{str(exc)[:300]}", "failed")
            return f"failed: {str(exc)[:500]}"
        finally:
            await client.close()

    def start_scheduler(self) -> None:
        if self._scheduler_task is None or self._scheduler_task.done():
            self._recover_screenshot_requests()
            for run_id in self._recovered_screenshot_runs:
                task = asyncio.create_task(self._notify_recovered_screenshot_run(run_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            self._scheduler_task = asyncio.create_task(
                self.scheduler_loop(), name="liveops-scheduler"
            )

    async def stop_scheduler(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def begin_run(self, schedule_id: int, kind: str, trigger: str = "user",
                        interrupted_at: datetime | None = None) -> CollectionRun:
        if kind == "baseline":
            raise RuntimeError("基线按主播采集顺序自动生成，无需单独记基线。")
        key = (kind, schedule_id)
        if self._queued:
            raise RuntimeError("已有采集任务正在执行，请等待完成后再采集下一位主播。")
        if schedule_id in self._syncing:
            raise RuntimeError("该主播上一次采集仍在同步飞书，请稍后重试。")
        if self._browser_lock.locked():
            raise RuntimeError("浏览器正在执行其他采集任务，请稍后再试。")
        with SessionLocal() as db:
            schedule = db.get(ShiftSchedule, schedule_id)
            if schedule is None:
                raise ValueError("排班不存在。")
            today = now_local().date()
            if trigger == "user" and schedule.work_date != today:
                when = "历史" if schedule.work_date < today else "未来"
                raise RuntimeError(f"{when}排班不允许手动执行采集任务，请选择当天排班。")
            request_data: dict[str, Any] = {}
            values = db_settings(db, self.settings_store)
            if trigger == "scheduler":
                previous_report = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule_id))
                if previous_report and (previous_report.raw_json or {}).get("is_final", True):
                    request_data["data_collection"] = "disabled"
            if kind == "interruption" and not setting_enabled(values, "data_collection_enabled"):
                raise RuntimeError("获取数据已关闭，无法采集并保存中断数据片段。")
            if kind == "report" and not (setting_enabled(values, "data_collection_enabled")
                                         or setting_enabled(values, "anchor_screenshot_enabled")):
                raise RuntimeError("获取数据和主播时段截图均已关闭。")
            if kind == "interruption":
                if not schedule.enabled:
                    raise RuntimeError("停用的排班不能标记中断。")
                stamp = interrupted_at or now_local()
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=APP_TIMEZONE)
                stamp = stamp.astimezone(APP_TIMEZONE)
                if not schedule_start(schedule) <= stamp < schedule_end(schedule) or stamp > now_local():
                    raise RuntimeError("中断时间必须在该班次内，且不能晚于当前时间。")
                request_data["interrupted_at"] = stamp.isoformat()
            run = CollectionRun(
                id=str(uuid.uuid4()),
                schedule_id=schedule_id,
                kind=kind,
                trigger=trigger,
                status="queued",
                created_at=utc_now(),
                result_json=request_data,
            )
            db.add(run)
            db.commit()
            db.refresh(run)
        self._queued.add(key)
        self._log_step(run.id, "queue", "任务已进入采集队列。")
        task = asyncio.create_task(self._execute(run.id), name=f"liveops-{kind}-{schedule_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def begin_manual_run(self, schedule_id: int) -> CollectionRun:
        return await self.begin_run(schedule_id, "report", "user")

    async def scheduler_loop(self) -> None:
        while True:
            try:
                await self._check_due_schedules()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler iteration failed")
            await asyncio.sleep(15)

    async def _check_due_schedules(self) -> None:
        now = now_local()
        with SessionLocal() as db:
            values = db_settings(db, self.settings_store)
            catchup_minutes = max(0, int(values.get("auto_catchup_minutes", "90") or 90))
            settle_seconds = max(0, int(values.get("settle_delay_seconds", "30") or 30))
            schedules = db.scalars(
                select(ShiftSchedule).where(
                    ShiftSchedule.enabled.is_(True),
                    ShiftSchedule.work_date >= (now.date() - timedelta(days=1)),
                    ShiftSchedule.work_date <= now.date(),
                )
            ).all()
            schedules.sort(key=lambda item: (item.work_date, item.start_time))
        await self._check_hourly_clock(values, now, schedules)
        await self._reuse_hourly_captures(values)
        now = now_local()
        data_enabled = setting_enabled(values, "data_collection_enabled")
        anchor_enabled = setting_enabled(values, "anchor_screenshot_enabled")
        with SessionLocal() as db:
            due: list[tuple[int, str]] = []
            for schedule in schedules:
                end_at = schedule_end(schedule)
                report_key = ("report", schedule.id)
                ready_at = end_at + timedelta(seconds=settle_seconds)
                report = db.scalar(
                    select(ShiftReport).where(ShiftReport.schedule_id == schedule.id)
                )
                report_age = (now - ready_at).total_seconds()
                raw = (report.raw_json or {}) if report else {}
                provisional = raw.get("is_final") is False
                if "is_final" not in raw and raw.get("collected_at"):
                    provisional = datetime.fromisoformat(raw["collected_at"]) < end_at
                anchor = db.get(ScreenshotRequest, f"anchor:{schedule.id}")
                # Legacy final reports already delivered their anchor screenshot.
                anchor_needed = anchor_enabled and (
                    anchor.status in {"pending", "failed"} if anchor else report is None or provisional
                )
                hourly_needed = (data_enabled or anchor_enabled) and bool(self._shared_hourly_requests(db, schedule, values))
                if (
                    ((data_enabled and (report is None or provisional)) or anchor_needed or hourly_needed)
                    and ready_at <= now
                    and 0 <= report_age <= catchup_minutes * 60
                    and not self._recent_attempt(report_key, now)
                ):
                    due.append((schedule.id, "report"))
            db.commit()

        month_key = now.strftime("%Y-%m")
        if (
            data_enabled and _integration_ready(values, "bitable")
            and self._bitable_retention_checked_month != month_key
            and (
                self._bitable_retention_retry_at is None or now >= self._bitable_retention_retry_at
            )
        ):
            archived, failed = await self._archive_cold_bitable_records(values)
            if failed:
                self._bitable_retention_retry_at = now + timedelta(minutes=15)
                logger.warning(
                    "Bitable monthly cold-data cleanup failed for %s row(s)", len(failed)
                )
            else:
                self._bitable_retention_checked_month = month_key
                self._bitable_retention_retry_at = None
                if archived:
                    logger.info(
                        "Archived %s cold Bitable row(s); SQLite keeps all reports", archived
                    )

        for schedule_id, kind in due:
            key = (kind, schedule_id)
            if self._queued or self._browser_lock.locked():
                continue
            try:
                await self.begin_run(schedule_id, kind, "scheduler")
                self._last_auto_attempt[key] = now
            except (ValueError, RuntimeError):
                continue
        await self._dispatch_hourly_requests(values, schedules, now)

    def _recent_attempt(self, key: tuple[str, int], now: datetime) -> bool:
        previous = self._last_auto_attempt.get(key)
        return previous is not None and (now - previous).total_seconds() < 180

    async def _execute(self, run_id: str) -> None:
        run_info: dict[str, Any] = {}
        feishu_task: asyncio.Task[str] | None = None
        rebased_ids: list[int] = []
        daily_exposure_ids: list[int] = []
        schedule_id: int | None = None
        run_kind = ""
        is_final_schedule = False
        with SessionLocal() as db:
            run = db.get(CollectionRun, run_id)
            if run is None:
                return
            schedule_id = run.schedule_id
            run_kind = run.kind
            schedule = db.get(ShiftSchedule, run.schedule_id) if run.schedule_id else None
            settings = db_settings(db, self.settings_store)
            modules = db.scalars(select(CollectorModule).order_by(CollectorModule.sort_order)).all()
            run.status = "running"
            run.started_at = utc_now()
            db.commit()
        self._log_step(run_id, "start", "开始执行调度任务。", "succeeded")

        key = (run_kind, schedule_id) if schedule_id is not None else (run_kind, 0)
        try:
            if schedule is None and run_kind != "hourly_screenshot":
                raise RuntimeError("当前运行任务没有关联排班。")
            screenshot_ids = self._claim_screenshot_requests(run_id, schedule, settings)
            data_enabled = (setting_enabled(settings, "data_collection_enabled") and run_kind != "hourly_screenshot"
                            and (run.result_json or {}).get("data_collection") != "disabled")
            if not data_enabled:
                if screenshot_ids:
                    await self._execute_screenshot_only(run_id, schedule, settings)
                else:
                    with SessionLocal() as db:
                        current_run = db.get(CollectionRun, run_id)
                        current_run.status = "skipped"
                        current_run.message = "相关功能已关闭，本次任务未执行。"
                        current_run.finished_at = utc_now()
                        db.commit()
                return
            if run_kind == "baseline":
                async with self._browser_lock:
                    baseline_value = await collect_baseline(
                        settings=settings, schedule=schedule, run_id=run_id
                    )
                with SessionLocal() as db:
                    current_schedule = db.get(ShiftSchedule, schedule_id)
                    current_run = db.get(CollectionRun, run_id)
                    if current_schedule:
                        current_schedule.baseline_json = baseline_value
                        current_schedule.baseline_at = utc_now()
                        current_schedule.live_room_id = baseline_value.get(
                            "live_room_id", current_schedule.live_room_id
                        )
                    if current_run:
                        current_run.result_json = baseline_value
                        current_run.status = "succeeded"
                        current_run.message = "已保存排班累计基线。"
                        current_run.finished_at = utc_now()
                    db.commit()
                return

            delay = max(0, int(settings.get("settle_delay_seconds", "30") or 30))
            if run.trigger == "scheduler" and delay:
                remaining = (
                    schedule_end(schedule) + timedelta(seconds=delay) - now_local()
                ).total_seconds()
                if remaining > 0:
                    await asyncio.sleep(remaining)
            async with self._browser_lock:
                self._log_step(run_id, "baseline", "按排班顺序读取本时段基线。")
                with SessionLocal() as db:
                    current_schedule = db.get(ShiftSchedule, schedule_id)
                    if current_schedule is None:
                        raise RuntimeError("排班已被删除。")
                    schedule = current_schedule
                    baseline = _current_baseline(db, schedule)
                    interruptions = db.scalars(select(ShiftInterruption).where(
                        ShiftInterruption.schedule_id == schedule_id
                    ).order_by(ShiftInterruption.interrupted_at, ShiftInterruption.id)).all()
                    saved_segments = [dict(row.capture_json) for row in interruptions]
                    interrupted_at = datetime.fromisoformat(run.result_json["interrupted_at"]) if run_kind == "interruption" else None
                    is_first_schedule = _previous_shift(db, schedule) is None
                    is_final_schedule = _is_last_schedule_for_date(db, schedule)
                    schedule.baseline_json = baseline
                    schedule.baseline_at = utc_now()
                    db.commit()
                if is_first_schedule:
                    self._log_step(run_id, "baseline", "当日第一条排班，累计快照从 0 开始。", "succeeded")
                else:
                    self._log_step(
                        run_id, "baseline",
                        f"按直播场次读取上一位的快照，来源运行：{baseline.get('source_run_id', '缺失')}；缺少基线不影响分钟金额采集。",
                        "info" if baseline.get("source") == "previous_shift_missing" else "succeeded",
                    )
                confirmed_room_id = schedule.live_room_id
                async def on_captured(images: list[dict[str, Any]]) -> None:
                    nonlocal feishu_task
                    if not screenshot_ids:
                        return
                    room_id = confirmed_room_id or next((item.get("live_room_id") for item in reversed(images)
                                                          if item.get("live_room_id")), "")
                    self._save_screenshot_capture(run_id, images, room_id)
                    feishu_task = asyncio.create_task(
                        self._deliver_screenshot_requests(run_id, settings, data_pending=True),
                        name=f"liveops-feishu-{run_id}",
                    )
                    await asyncio.sleep(0)
                def remember_room(selection: dict[str, Any]) -> None:
                    nonlocal confirmed_room_id
                    confirmed_room_id = selection["live_room_id"]
                    # Identity is durable even if later metric calculation fails.
                    # A retry of an old interruption must not repin a newer room.
                    with SessionLocal() as db:
                        row = db.get(ShiftSchedule, schedule_id)
                        current_run = db.get(CollectionRun, run_id)
                        if row is not None and run_kind != "interruption":
                            row.live_room_id = selection["live_room_id"]
                        if current_run is not None:
                            current_run.result_json = {
                                **(current_run.result_json or {}), "room_selection": selection,
                            }
                        db.commit()

                run_info = await collect_report(
                    settings=settings,
                    schedule=schedule,
                    modules=modules,
                    run_id=run_id,
                    baseline=baseline,
                    include_daily_exposure=is_final_schedule,
                    progress=lambda stage, description, status: self._log_step(
                        run_id, stage, description, status
                    ),
                    on_captured=on_captured if run_kind != "interruption" else None,
                    interruption_segments=saved_segments,
                    interrupted_at=interrupted_at,
                    on_room_confirmed=remember_room,
                    screenshots_enabled=bool(screenshot_ids) or (
                        run_kind == "interruption" and setting_enabled(settings, "anchor_screenshot_enabled")),
                )
                if screenshot_ids and feishu_task is None:
                    await on_captured(run_info["screenshots"])

            if run_kind == "interruption":
                segment = run_info["segments"][-1]
                segment["screenshots"] = run_info["screenshots"]
                segment["page_errors"] = run_info["page_errors"]
                missing = list(segment["source"]["baseline_missing"])
                missing.extend(run_info["page_errors"])
                missing.extend(segment["source"]["baseline_conflicts"])
                with SessionLocal() as db:
                    event = db.scalar(select(ShiftInterruption).where(
                        ShiftInterruption.schedule_id == schedule_id,
                        ShiftInterruption.live_room_id == segment["room"]["live_room_id"],
                    ))
                    if event is None:
                        event = ShiftInterruption(schedule_id=schedule_id,
                            live_room_id=segment["room"]["live_room_id"],
                            interrupted_at=interrupted_at, latest_run_id=run_id, capture_json=segment)
                        db.add(event)
                    else:
                        event.latest_run_id = run_id
                        event.capture_json = segment
                        event.interrupted_at = interrupted_at
                    prior_report = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule_id))
                    if prior_report:
                        prior_report.raw_json = {**prior_report.raw_json, "is_final": False}
                    current_run = db.get(CollectionRun, run_id)
                    current_run.status = "partial" if missing else "succeeded"
                    current_run.missing_fields = missing
                    current_run.message = "中断片段已保存，班次结束后将与新场数据合并。" if not missing else "中断快照已保存，部分数据缺失，请重试标记。"
                    current_run.finished_at = utc_now()
                    current_run.result_json = {**current_run.result_json, "segment": segment,
                                               "screenshots": run_info["screenshots"]}
                    db.commit()
                self._log_step(run_id, "interruption", current_run.message, current_run.status)
                return
            requires_daily_exposure = is_final_schedule and run_info.get("is_final", True)
            if requires_daily_exposure:
                with SessionLocal() as db:
                    daily_exposure = _daily_exposure_for_date(
                        db, schedule.work_date, run_info.get("segments") or []
                    )
                run_info["source"]["daily_exposure"] = daily_exposure["value"]
                run_info["source"]["daily_exposure_aggregation"] = daily_exposure
                if daily_exposure["issues"]:
                    run_info["source"]["quality_issues"] = list(dict.fromkeys([
                        *run_info["source"].get("quality_issues", []),
                        *(f"日曝光:{issue}" for issue in daily_exposure["issues"]),
                    ]))
            else:
                run_info["source"]["daily_exposure"] = None
            self._log_step(run_id, "metrics", "计算时段报表和缺失字段。")
            metrics, missing = build_report_metrics(
                work_date=schedule.work_date,
                start_time=schedule.start_time,
                end_time=schedule.end_time,
                host_name=schedule.host_name,
                promoter=schedule.promoter,
                source=run_info["source"],
                include_daily_exposure=requires_daily_exposure,
            )
            if run_info.get("page_errors"):
                missing.extend(f"页面采集:{item}" for item in run_info["page_errors"])
            if run_info.get("is_final") is False:
                missing.append("临时快照:班次尚未结束，结束后将重新结算。")
            missing.extend(f"数据口径:{item}" for item in run_info["source"].get("quality_issues", []))
            if run_info["source"].get("baseline_conflicts"):
                self._log_step(
                    run_id, "baseline",
                    "当前千川累计值低于上一位快照；请核对采集顺序、直播间和千川账户。涉及字段："
                    + "、".join(run_info["source"]["baseline_conflicts"]),
                    "failed",
                )
            if run_info["source"].get("baseline_missing"):
                self._log_step(
                    run_id, "baseline", "上一位快照缺少同口径指标，请重新采集上一位主播。涉及字段："
                    + "、".join(run_info["source"]["baseline_missing"]), "failed",
                )
            run_info["metrics"] = metrics
            run_info["missing_fields"] = missing
            if feishu_task:
                await feishu_task
            if screenshot_ids:
                feishu_task = asyncio.create_task(
                    self._deliver_screenshot_requests(run_id, settings, metrics=metrics),
                    name=f"liveops-feishu-{run_id}",
                )
            self._log_step(
                run_id, "metrics",
                "指标计算完成。" if not missing else "部分指标缺失：" + "、".join(missing),
                "succeeded" if not missing else "partial",
            )

            with SessionLocal() as db:
                current_run = db.get(CollectionRun, run_id)
                current_schedule = db.get(ShiftSchedule, schedule_id)
                report = db.scalar(
                    select(ShiftReport).where(ShiftReport.schedule_id == schedule_id)
                )
                if report is None:
                    report = ShiftReport(
                        schedule_id=schedule_id,
                        latest_run_id=run_id,
                        work_date=schedule.work_date,
                        start_time=schedule.start_time,
                        end_time=schedule.end_time,
                        host_name=schedule.host_name,
                        promoter=schedule.promoter,
                        shop_name=schedule.shop_name,
                        live_room_id=run_info.get("live_room_id", schedule.live_room_id),
                    )
                    db.add(report)
                report.latest_run_id = run_id
                report.live_room_id = run_info.get("live_room_id", schedule.live_room_id)
                report.metrics_json = metrics
                report.raw_json = {
                    "formula_version": FORMULA_VERSION,
                    "source": run_info["source"],
                    "responses": run_info["responses"],
                    "page_errors": run_info["page_errors"],
                    "schedule_start": run_info["schedule_start"],
                    "schedule_end": run_info["schedule_end"],
                    "collected_at": run_info.get("collected_at"),
                    "segments": run_info.get("segments", []),
                    "author_id": run_info.get("author_id", ""),
                    "room_discovery": run_info.get("room_discovery", {}),
                    "is_final": run_info.get("is_final", True),
                    "measurement_mode": run_info.get("measurement_mode", ""),
                }
                report.screenshots_json = run_info["screenshots"]
                report.missing_fields = missing
                report.feishu_status = (
                    "disabled: 主播时段截图已关闭" if not setting_enabled(settings, "anchor_screenshot_enabled")
                    else integration_not_configured_status(settings, "feishu")
                    if not _integration_ready(settings, "feishu")
                    else "pending"
                )
                report.bitable_status = (
                    integration_not_configured_status(settings, "bitable")
                    if not _integration_ready(settings, "bitable")
                    else "pending"
                )
                db.flush()
                report_id = report.id
                if current_schedule:
                    current_schedule.last_report_id = report_id
                    current_schedule.live_room_id = run_info.get(
                        "live_room_id", schedule.live_room_id
                    )
                next_baseline = _baseline_from_report(report)
                if next_baseline and next_baseline.get("live_room_id"):
                    next_schedule = db.scalar(
                        select(ShiftSchedule)
                        .where(
                            ShiftSchedule.work_date == schedule.work_date,
                            ShiftSchedule.enabled.is_(True),
                            ShiftSchedule.start_time > schedule.start_time,
                        )
                        .order_by(ShiftSchedule.start_time)
                        .limit(1)
                    )
                    if next_schedule:
                        next_schedule.baseline_json = next_baseline
                        next_schedule.baseline_at = utc_now()
                        if not next_schedule.live_room_id:
                            next_schedule.live_room_id = next_baseline["live_room_id"]
                if current_run:
                    current_run.result_json = {
                        **(current_run.result_json or {}),
                        "report_id": report_id,
                        "metrics": metrics,
                        "screenshots": run_info["screenshots"],
                    }
                    current_run.missing_fields = missing
                    current_run.message = "数据已保存，正在同步飞书群和多维表格。"
                db.commit()
            self._log_step(run_id, "database", "原始响应、截图和报表已保存到本地数据库。", "succeeded")
            rebased_ids = self._rebase_following_reports(schedule_id, run_id)
            daily_exposure_ids = (
                self._backfill_daily_exposure(schedule.work_date, daily_exposure, run_id)
                if requires_daily_exposure else []
            )
            for target_id in [schedule_id, *rebased_ids]:
                self._syncing[target_id] = self._syncing.get(target_id, 0) + 1
            for target_id in daily_exposure_ids:
                if target_id not in [schedule_id, *rebased_ids]:
                    self._syncing[target_id] = self._syncing.get(target_id, 0) + 1
            if key:
                self._queued.discard(key)
            sync_targets = list(dict.fromkeys([schedule_id, *rebased_ids, *daily_exposure_ids]))
            for target_id in sync_targets:
                await self._sync_integrations(
                    target_id,
                    settings,
                    run_id,
                    feishu_task if target_id == schedule_id else None,
                    send_feishu=target_id == schedule_id and bool(screenshot_ids),
                )
            with SessionLocal() as db:
                current_run = db.get(CollectionRun, run_id)
                if current_run:
                    current_run.result_json = {
                        **(current_run.result_json or {}),
                        "integrations": self._integration_status(schedule_id),
                    }
                    for receipt in self._delivery_summary(run_id):
                        if receipt["target"] == "hourly":
                            status = receipt.get("status", "pending")
                            current_run.result_json = {**current_run.result_json, "integrations": {
                                **current_run.result_json["integrations"],
                                "hourly_screenshots": "failed: 发送结果待确认" if status == "uncertain" else status,
                            }}
                    sync_problem = any(str(status).startswith(("failed", "partial"))
                                       for status in current_run.result_json["integrations"].values())
                    current_run.status = "partial" if missing or sync_problem else "succeeded"
                    current_run.message = (
                        "采集完成，部分字段、页面或同步异常；详情见运行记录。"
                        if missing or sync_problem else "采集完成"
                    )
                    current_run.finished_at = utc_now()
                    db.commit()
            self._log_step(run_id, "finish", current_run.message, current_run.status)
        except Exception as exc:
            logger.exception("Collection run %s failed", run_id)
            with SessionLocal() as db:
                current_run = db.get(CollectionRun, run_id)
                if current_run:
                    current_run.status = "failed"
                    current_run.message = str(exc)[:2000]
                    current_run.finished_at = utc_now()
                    if run_info:
                        current_run.result_json = {
                            **(current_run.result_json or {}),
                            "screenshots": run_info.get("screenshots", []),
                        }
                    db.commit()
            self._log_step(run_id, "finish", f"采集失败：{str(exc)[:300]}", "failed")
            with SessionLocal() as db:
                for request in db.scalars(select(ScreenshotRequest).where(
                    ScreenshotRequest.run_id == run_id, ScreenshotRequest.status == "capturing"
                )).all():
                    request.status = "failed"
                    request.delivery_json = {"status": "failed", "error": "截图执行失败，请查看运行日志。"}
                db.commit()
            if feishu_task:
                await asyncio.gather(feishu_task, return_exceptions=True)
        finally:
            try:
                await self._notify_run_problem(run_id, settings)
            except Exception:
                # Notification must never replace the collection error or block queue cleanup.
                logger.exception("Unable to record/send exception alert for run %s", run_id)
            if key:
                self._queued.discard(key)
            if schedule_id is not None:
                remaining = self._syncing.get(schedule_id, 0) - 1
                if remaining > 0:
                    self._syncing[schedule_id] = remaining
                else:
                    self._syncing.pop(schedule_id, None)
            for rebased_id in set([*rebased_ids, *daily_exposure_ids]):
                if rebased_id == schedule_id:
                    continue
                remaining = self._syncing.get(rebased_id, 0) - 1
                if remaining > 0:
                    self._syncing[rebased_id] = remaining
                else:
                    self._syncing.pop(rebased_id, None)

    def _integration_status(self, schedule_id: int) -> dict[str, str]:
        with SessionLocal() as db:
            report = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule_id))
            return (
                {"feishu": report.feishu_status, "bitable": report.bitable_status} if report else {}
            )

    def _backfill_daily_exposure(
        self, work_date: date, aggregation: dict[str, Any], run_id: str
    ) -> list[int]:
        value = aggregation.get("value")
        rounded_value = int(round(value)) if value is not None else None
        changed: list[int] = []
        with SessionLocal() as db:
            reports = db.scalars(
                select(ShiftReport).where(ShiftReport.work_date == work_date)
            ).all()
            for report in reports:
                metrics = dict(report.metrics_json or {})
                metrics["daily_exposure"] = rounded_value
                raw = dict(report.raw_json or {})
                source = dict(raw.get("source") or {})
                source["daily_exposure"] = rounded_value
                source["daily_exposure_aggregation"] = aggregation
                if aggregation.get("issues"):
                    quality = [
                        issue for issue in source.get("quality_issues", [])
                        if not str(issue).startswith("日曝光:")
                    ]
                    source["quality_issues"] = list(dict.fromkeys([
                        *quality, *(f"日曝光:{issue}" for issue in aggregation["issues"])
                    ]))
                raw["source"] = source
                raw["daily_exposure_aggregation"] = aggregation
                raw["daily_exposure_updated_at"] = now_local().isoformat()

                missing = [
                    key for key in (report.missing_fields or [])
                    if key != "daily_exposure" and not str(key).startswith("数据口径:日曝光:")
                ]
                if rounded_value is None:
                    missing.append("daily_exposure")
                report.metrics_json = metrics
                report.raw_json = raw
                report.missing_fields = missing
                report.bitable_status = "pending"

                latest_run = db.get(CollectionRun, report.latest_run_id)
                if latest_run:
                    result = dict(latest_run.result_json or {})
                    result["metrics"] = metrics
                    result["daily_exposure_aggregation"] = aggregation
                    latest_run.result_json = result
                    latest_run.missing_fields = missing
                    if latest_run.status in {"succeeded", "partial"}:
                        latest_run.status = "partial" if missing else "succeeded"
                        latest_run.message = (
                            "采集完成；当日全部直播间曝光已汇总并回填。"
                            if rounded_value is not None
                            else "采集完成，但日曝光汇总不完整；详情见数据质量说明。"
                        )
                changed.append(report.schedule_id)
            db.commit()
        self._log_step(
            run_id,
            "daily_exposure",
            (
                f"已将当日 {len(aggregation.get('rooms') or [])} 个不同直播间的最终整体曝光"
                f"（{rounded_value}）回填到 {len(changed)} 个时段。"
                if rounded_value is not None
                else "未能完整确认当日所有直播间的最终整体曝光，日曝光留空并回填质量说明。"
            ),
            "succeeded" if rounded_value is not None else "partial",
        )
        return changed

    def _rebase_following_reports(self, schedule_id: int, run_id: str) -> list[int]:
        changed: list[int] = []
        with SessionLocal() as db:
            schedule = db.get(ShiftSchedule, schedule_id)
            if schedule is None:
                return changed
            following = db.scalars(
                select(ShiftSchedule)
                .where(
                    ShiftSchedule.work_date == schedule.work_date,
                    ShiftSchedule.enabled.is_(True),
                    ShiftSchedule.start_time > schedule.start_time,
                )
                .order_by(ShiftSchedule.start_time)
            ).all()
            for row in following:
                try:
                    baseline = _current_baseline(db, row)
                except RuntimeError:
                    break
                row.baseline_json = baseline
                row.baseline_at = utc_now()
                report = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == row.id))
                if report is None:
                    continue
                raw = dict(report.raw_json or {})
                if not raw.get("responses") or not raw.get("schedule_start") or not raw.get("schedule_end"):
                    break
                if raw.get("measurement_mode") == "manual_handoffs":
                    source = recompute_handoffs(raw["segments"], baseline,
                                               datetime.fromisoformat(raw["schedule_start"]))
                elif raw.get("segments"):
                    source = recompute_segments(
                        raw["segments"], baseline, datetime.fromisoformat(raw["schedule_start"]),
                        datetime.fromisoformat(raw["schedule_end"]),
                    )
                else:
                    source = report_source_values(
                        responses=raw["responses"],
                        schedule_start=datetime.fromisoformat(raw["schedule_start"]),
                        schedule_end=datetime.fromisoformat(raw["schedule_end"]),
                        host_name=row.host_name, baseline=baseline, include_daily_exposure=False,
                    )
                daily_aggregation = raw.get("daily_exposure_aggregation")
                if daily_aggregation:
                    source["daily_exposure"] = daily_aggregation.get("value")
                    source["daily_exposure_aggregation"] = daily_aggregation
                    if daily_aggregation.get("issues"):
                        source["quality_issues"] = list(dict.fromkeys([
                            *source.get("quality_issues", []),
                            *(f"日曝光:{issue}" for issue in daily_aggregation["issues"]),
                        ]))
                metrics, missing = build_report_metrics(
                    work_date=row.work_date,
                    start_time=row.start_time,
                    end_time=row.end_time,
                    host_name=row.host_name,
                    promoter=row.promoter,
                    source=source,
                    include_daily_exposure=bool(daily_aggregation),
                )
                missing.extend(f"页面采集:{item}" for item in raw.get("page_errors") or [])
                missing.extend(f"数据口径:{item}" for item in source.get("quality_issues", []))
                raw["source"] = source
                raw["formula_version"] = FORMULA_VERSION
                raw["rebased_at"] = now_local().isoformat()
                raw["baseline_source_run_id"] = baseline.get("source_run_id")
                report.raw_json = raw
                report.metrics_json = metrics
                report.missing_fields = missing
                report.bitable_status = "pending"
                latest_run = db.get(CollectionRun, report.latest_run_id)
                if latest_run:
                    latest_run.missing_fields = missing
                    latest_run.status = "partial" if missing else "succeeded"
                    latest_run.message = "上一位主播重新采集后，本时段指标已按最新基线重算。"
                    latest_run.result_json = {**(latest_run.result_json or {}), "metrics": metrics}
                changed.append(row.id)
            db.commit()
        if changed:
            self._log_step(
                run_id, "rebase", f"已用最新基线重算后续 {len(changed)} 位主播的本地报表。", "succeeded"
            )
        return changed

    async def _archive_cold_bitable_records(
        self, settings: dict[str, str]
    ) -> tuple[int, list[str]]:
        cutoff = _month_start(now_local().date())
        with SessionLocal() as db:
            rows = db.scalars(
                select(ShiftReport).where(
                    ShiftReport.work_date < cutoff,
                    ShiftReport.bitable_record_id != "",
                )
            ).all()
            records = [(row.id, row.bitable_record_id) for row in rows]
        if not records or not _integration_ready(settings, "bitable"):
            return 0, []

        client = BitableClient(
            settings["feishu_app_id"],
            settings["feishu_app_secret"],
            settings["bitable_app_token"],
            settings["bitable_table_id"],
        )
        archived_ids: list[int] = []
        failed: list[str] = []
        try:
            for report_id, record_id in records:
                try:
                    await client.delete_record(record_id)
                    archived_ids.append(report_id)
                except Exception as exc:
                    logger.warning(
                        "Bitable cold-row deletion failed for report %s: %s",
                        report_id,
                        exc,
                    )
                    failed.append(str(report_id))
        finally:
            await client.close()

        if archived_ids:
            with SessionLocal() as db:
                archived_rows = db.scalars(
                    select(ShiftReport).where(ShiftReport.id.in_(archived_ids))
                ).all()
                for row in archived_rows:
                    row.bitable_record_id = ""
                    row.bitable_status = "archived: 仅保存在本地数据库"
                    row.synced_at = utc_now()
                db.commit()
        return len(archived_ids), failed

    async def _sync_integrations(
        self, schedule_id: int, settings: dict[str, str], run_id: str,
        feishu_task: asyncio.Task[str] | None = None,
        send_feishu: bool = True,
    ) -> None:
        lock = self._integration_locks.setdefault(schedule_id, asyncio.Lock())
        async with lock:
            await self._sync_integrations_locked(
                schedule_id, settings, run_id, feishu_task, send_feishu
            )

    async def _sync_integrations_locked(
        self, schedule_id: int, settings: dict[str, str], run_id: str,
        feishu_task: asyncio.Task[str] | None,
        send_feishu: bool,
    ) -> None:
        with SessionLocal() as db:
            report = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule_id))
            schedule = db.get(ShiftSchedule, schedule_id)
            if report is None or schedule is None:
                return
            metrics = dict(report.metrics_json or {})
            screenshots = list(report.screenshots_json or [])
            mapping_raw = settings.get("bitable_field_mapping", "{}")
            try:
                mapping = json.loads(mapping_raw)
            except json.JSONDecodeError:
                mapping = {}
            template = schedule.message_template or settings.get(
                "report_message_template", "以上主播{host_name}{start_time}-{end_time}点的直播数据"
            )
            caption = _format_caption(template, schedule, metrics)
            payload_values = {
                **metrics,
                "host_name": schedule.host_name,
                "promoter": schedule.promoter,
                "work_date": schedule.work_date.isoformat(),
                "schedule_id": schedule.id,
                "time_range": metrics.get("time_range", ""),
            }
            report_id = report.id
            record_id = report.bitable_record_id
            report_date = report.work_date

        if feishu_task is None and send_feishu:
            feishu_task = asyncio.create_task(
                self._send_feishu_images(settings, caption, screenshots, run_id)
            )

        bitable_status = integration_not_configured_status(settings, "bitable")
        new_record_id = record_id
        if _integration_ready(settings, "bitable"):
            if report_date < _month_start(now_local().date()):
                bitable_status = "local_only: 本地数据库保留完整历史，多维表格只保留本月热数据"
                new_record_id = ""
                self._log_step(run_id, "bitable", "历史月份报告仅保存在本地数据库。", "skipped")
            else:
                self._log_step(run_id, "bitable", "开始同步本月报告到飞书多维表格。")
                archived_count, archive_failures = await self._archive_cold_bitable_records(
                    settings
                )
                client = BitableClient(
                    settings["feishu_app_id"],
                    settings["feishu_app_secret"],
                    settings["bitable_app_token"],
                    settings["bitable_table_id"],
                )
                try:
                    result = await client.upsert_record(
                        values=payload_values,
                        field_mapping=mapping,
                        record_id=record_id,
                        sync_key_field=settings.get("bitable_sync_key_field", ""),
                        sync_key=f"shift-{schedule_id}",
                    )
                    new_record_id = result.get("record_id", record_id)
                    bitable_status = "partial" if result.get("missing_mappings") else "synced"
                    details: list[str] = []
                    if result.get("missing_mappings"):
                        details.append("缺少列 " + ", ".join(result["missing_mappings"]))
                    if archive_failures:
                        details.append(f"{len(archive_failures)} 条上月记录未能归档")
                    if details:
                        bitable_status = "partial: " + "；".join(details)
                    elif archived_count:
                        logger.info("Archived %s old Bitable report row(s)", archived_count)
                    self._log_step(run_id, "bitable", "多维表格同步完成。", "succeeded")
                except Exception as exc:
                    logger.warning("Bitable sync failed for report %s: %s", report_id, exc)
                    bitable_status = f"failed: {str(exc)[:500]}"
                    self._log_step(run_id, "bitable", f"同步失败：{str(exc)[:300]}", "failed")
                finally:
                    await client.close()
        else:
            self._log_step(run_id, "bitable", bitable_status, "skipped")

        feishu_status = await feishu_task if feishu_task else report.feishu_status

        with SessionLocal() as db:
            current = db.get(ShiftReport, report_id)
            if current:
                current.feishu_status = feishu_status
                current.bitable_status = bitable_status
                current.bitable_record_id = new_record_id or ""
                current.synced_at = utc_now()
                db.commit()
