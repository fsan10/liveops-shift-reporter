"""Roster-owned hourly details; cumulative baselines never mutate shift handoffs."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from liveops.bitable import BitableClient
from liveops.formulas import FORMULA_VERSION, build_report_metrics
from liveops.models import (
    CollectionRun,
    CollectorModule,
    ScreenshotRequest,
    ShiftHourDetail,
    ShiftHourState,
    ShiftInterruption,
    ShiftReport,
    ShiftSchedule,
)
from liveops.portal import schedule_end_datetime, shift_datetime
from liveops.sessions import local_datetime
from liveops.settings import screenshot_chat, setting_enabled
from liveops.snapshots import combine_handoffs, handoff_source

HOURLY_VERSION = "2026-10-30-roster-hours-v1"
HOUR_GRACE_SECONDS = 300


def shift_periods(schedule: Any) -> list[tuple[datetime, datetime]]:
    """Use one-hour steps, merging a remainder shorter than two hours into the last step."""
    left = shift_datetime(schedule.work_date, schedule.start_time)
    end = schedule_end_datetime(schedule.work_date, schedule.start_time, schedule.end_time)
    result = []
    while left < end:
        right = left + timedelta(hours=1) if end - left >= timedelta(hours=2) else end
        result.append((left, right))
        left = right
    return result


def period_request_id(detail: ShiftHourDetail) -> str:
    return f"shift-hour:{detail.schedule_id}:{detail.period_index}:{local_datetime(detail.end_at).isoformat()}"


def end_baseline(capture: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    segments = capture.get("segments") or []
    if not segments:
        return None
    last = max(segments, key=lambda item: local_datetime(item["captured_at"]))
    snapshot = last.get("snapshot")
    if not snapshot or last["source"].get("end_snapshot_valid") is False:
        return None
    return {**copy.deepcopy(snapshot), "source": "hourly_previous_end", "source_run_id": run_id,
            "author_id": last["room"].get("author_id", ""), "live_end": last["room"].get("live_end"),
            "chain": "shift_hourly"}


def detail_values(capture: dict[str, Any], baseline: dict[str, Any], detail: ShiftHourDetail,
                  schedule: ShiftSchedule) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    left, end = local_datetime(detail.start_at), local_datetime(detail.end_at)
    segments = []
    for item in capture.get("segments") or []:
        right = min(end, local_datetime(item["source"]["segment_end"]))
        room_start = local_datetime(item["room"]["live_start"])
        if right <= left or room_start >= end:
            continue
        source = handoff_source(snapshot=item["snapshot"], room=item["room"], baseline=baseline,
                                start=left, end=right, allow_new_room=bool(segments))
        segments.append({**copy.deepcopy(item), "source": source})
        left = right
    if not segments:
        raise RuntimeError("小时段没有可用的结束累计快照，不能补造小时数据。")
    source = combine_handoffs(segments)
    metrics, missing = build_report_metrics(work_date=schedule.work_date,
        start_time=local_datetime(detail.start_at).time(), end_time=end.time(), host_name=schedule.host_name,
        promoter=schedule.promoter, source=source, include_daily_exposure=False)
    missing.extend(f"小时基线:{key}" for key in source.get("baseline_missing", []))
    missing.extend(f"累计回退:{key}" for key in source.get("baseline_conflicts", []))
    missing.extend(capture.get("page_errors") or [])
    raw = {"version": HOURLY_VERSION, "formula_version": FORMULA_VERSION, "source": source,
           "segments": segments, "responses": capture.get("responses", []),
           "captured_at": capture.get("collected_at"), "end_baseline": end_baseline(capture, "")}
    return metrics, raw, list(dict.fromkeys(missing))


class ShiftHourlyAutomation:
    def _recover_screenshot_requests(self) -> None:
        super()._recover_screenshot_requests()
        with self._automation_db() as db:
            for row in db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.status == "running")).all():
                row.status = "partial" if row.raw_json.get("end_baseline") else "failed"
                row.missing_fields = [*row.missing_fields, "后台执行中断，已保留累计快照与发送回执。"]
            db.commit()

    def _fail_hour_detail(self, run_id: str, message: str) -> None:
        with self._automation_db() as db:
            run = db.get(CollectionRun, run_id)
            detail_id = (run.result_json or {}).get("hour_detail_id") if run else None
            row = db.get(ShiftHourDetail, detail_id) if detail_id is not None else None
            if row:
                row.status = "partial" if row.raw_json.get("end_baseline") else "failed"
                row.missing_fields = list(dict.fromkeys([*row.missing_fields, message]))
                db.commit()

    def _ensure_hour_plan(self, db: Any, schedule: ShiftSchedule) -> list[ShiftHourDetail]:
        rows = db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule.id)
                          .order_by(ShiftHourDetail.period_index)).all()
        plan = shift_periods(schedule)
        if rows and [(local_datetime(row.start_at), local_datetime(row.end_at)) for row in rows] != plan:
            if any(row.latest_run_id for row in rows):
                raise RuntimeError("该排班已有小时采集，不能静默改变小时区间；请保留原排班审计记录。")
            for row in rows:
                db.delete(row)
            db.flush()
            rows = []
        if not rows:
            rows = [ShiftHourDetail(schedule_id=schedule.id, period_index=index, start_at=left, end_at=right,
                    status="pending", raw_json={}, metrics_json={}, missing_fields=[], screenshots_json=[])
                    for index, (left, right) in enumerate(plan, 1)]
            db.add_all(rows)
            db.flush()
        return rows

    async def _check_hourly_clock(self, settings: dict[str, str], now: datetime, schedules: list[Any]) -> None:
        if not setting_enabled(settings, "hourly_screenshot_enabled"):
            return
        delay = max(0, int(settings.get("settle_delay_seconds", "30") or 30))
        with self._automation_db() as db:
            # Legacy live-start clocks are retained for audit but no longer dispatched.
            for request in db.scalars(select(ScreenshotRequest).where(ScreenshotRequest.target == "hourly",
                ScreenshotRequest.status == "pending")).all():
                if not request.id.startswith("shift-hour:"):
                    request.status = "skipped"
                    request.delivery_json = {"status": "skipped", "reason": "已改用主播排班小时段。"}
            for schedule in schedules:
                for detail in self._ensure_hour_plan(db, schedule):
                    ready = local_datetime(detail.end_at) + timedelta(seconds=delay)
                    if ready > now or detail.status not in {"pending", "failed"}:
                        continue
                    key = period_request_id(detail)
                    request = db.get(ScreenshotRequest, key)
                    if request is None:
                        request = ScreenshotRequest(id=key, target="hourly", schedule_id=schedule.id,
                            planned_at=detail.end_at, hour_index=detail.period_index,
                            account_id=schedule.qianchuan_account_id or settings.get("qianchuan_account_id", ""),
                            room_json={"live_room_id": self._schedule_room_id(db, schedule)},
                            screenshots_json=[], delivery_json={}, status="pending")
                        db.add(request)
                    if now > ready + timedelta(seconds=HOUR_GRACE_SECONDS) and not detail.raw_json:
                        detail.status = "missed"
                        detail.missing_fields = ["错过采集时点，未补造历史小时累计快照。"]
                        if request.status == "pending":
                            request.status = "missed"
                            request.delivery_json = {"status": "missed", "reason": detail.missing_fields[0]}
                    elif detail.status == "failed" and request.status == "failed":
                        request.status = "pending"
            db.commit()

    def _shared_hourly_requests(self, db: Any, schedule: Any, settings: dict[str, str]) -> list[ScreenshotRequest]:
        if not setting_enabled(settings, "hourly_screenshot_enabled"):
            return []
        return db.scalars(select(ScreenshotRequest).where(ScreenshotRequest.schedule_id == schedule.id,
            ScreenshotRequest.target == "hourly", ScreenshotRequest.status == "pending",
            ScreenshotRequest.planned_at == schedule_end_datetime(schedule.work_date, schedule.start_time, schedule.end_time),
            ScreenshotRequest.id.startswith("shift-hour:"))).all()

    def _claim_screenshot_requests(self, run_id: str, schedule: Any, settings: dict[str, str]) -> list[str]:
        with self._automation_db() as db:
            run = db.get(CollectionRun, run_id)
            hour_run = run.kind == "shift_hour"
        if not hour_run:
            return super()._claim_screenshot_requests(run_id, schedule, settings)
        with self._automation_db() as db:
            run = db.get(CollectionRun, run_id)
            ids = []
            for key in run.result_json.get("screenshot_request_ids", []):
                request = db.get(ScreenshotRequest, key)
                if request and request.status in {"pending", "failed"} and setting_enabled(settings, "hourly_screenshot_enabled"):
                    request.run_id, request.status = run_id, "capturing"
                    ids.append(key)
            run.result_json = {**run.result_json, "screenshot_targets": ["hourly"], "screenshot_request_ids": ids}
            db.commit()
        return ids

    async def _dispatch_hourly_requests(self, settings: dict[str, str], schedules: list[Any], now: datetime) -> None:
        if not setting_enabled(settings, "hourly_screenshot_enabled") or self._queued or self._browser_lock.locked():
            return
        valid = {row.id: row for row in schedules}
        with self._automation_db() as db:
            requests = db.scalars(select(ScreenshotRequest).where(ScreenshotRequest.target == "hourly",
                ScreenshotRequest.status == "pending", ScreenshotRequest.id.startswith("shift-hour:"))
                .order_by(ScreenshotRequest.planned_at)).all()
            for request in requests:
                schedule = valid.get(request.schedule_id)
                if not schedule:
                    continue
                end = schedule_end_datetime(schedule.work_date, schedule.start_time, schedule.end_time)
                if local_datetime(request.planned_at) == end and (setting_enabled(settings, "data_collection_enabled")
                        or setting_enabled(settings, "anchor_screenshot_enabled")):
                    continue
                detail = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule.id,
                    ShiftHourDetail.period_index == request.hour_index))
                if not detail or detail.status not in {"pending", "failed"}:
                    continue
                key = ("shift_hour", detail.id)
                if self._recent_attempt(key, now):
                    continue
                run = CollectionRun(id=str(uuid.uuid4()), kind="shift_hour", trigger="scheduler", status="queued",
                    schedule_id=schedule.id, result_json={"hour_detail_id": detail.id,
                    "screenshot_request_ids": [request.id], "data_collection": "hourly_only"})
                db.add(run)
                db.flush()
                request.run_id = run.id
                db.commit()
                self._last_auto_attempt[key] = now
                self._queued.add(("shift_hour", schedule.id))
                task = asyncio.create_task(self._execute(run.id), name=f"liveops-shift-hour-{detail.id}")
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                return
            db.commit()

    async def _reuse_hourly_captures(self, settings: dict[str, str]) -> None:
        if not setting_enabled(settings, "hourly_screenshot_enabled"):
            return
        saved: list[tuple[ShiftSchedule, str, dict[str, Any], str]] = []
        with self._automation_db() as db:
            requests = db.scalars(select(ScreenshotRequest).where(ScreenshotRequest.target == "hourly",
                ScreenshotRequest.status == "pending", ScreenshotRequest.id.startswith("shift-hour:"))).all()
            for request in requests:
                schedule = db.get(ShiftSchedule, request.schedule_id)
                if not schedule or local_datetime(request.planned_at) != schedule_end_datetime(
                    schedule.work_date, schedule.start_time, schedule.end_time):
                    continue
                report = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule.id))
                if not report or not report.raw_json.get("is_final") or not report.screenshots_json:
                    continue
                baseline = end_baseline(report.raw_json, report.latest_run_id)
                if not baseline or baseline.get("qianchuan_account_id") != request.account_id:
                    continue
                request.run_id, request.status = report.latest_run_id, "captured"
                request.room_json = {"live_room_id": report.live_room_id}
                request.screenshots_json = [item for item in report.screenshots_json
                                            if item.get("live_room_id") == report.live_room_id]
                capture = {**copy.deepcopy(report.raw_json), "live_room_id": report.live_room_id,
                    "screenshots": request.screenshots_json, "shop_name": report.shop_name}
                saved.append((schedule, report.latest_run_id, capture, request.id))
            db.commit()
        for schedule, source_id, capture, request_id in saved:
            self._save_hourly_capture(schedule, source_id, capture)
            await self.begin_screenshot_delivery(source_id, "scheduler", [request_id])
            await self.begin_hourly_sync(schedule.id, "scheduler")

    async def begin_hourly_sync(self, schedule_id: int, trigger: str = "user") -> CollectionRun:
        with self._automation_db() as db:
            if db.get(ShiftSchedule, schedule_id) is None:
                raise ValueError("排班不存在。")
            run = CollectionRun(id=str(uuid.uuid4()), schedule_id=schedule_id, kind="hourly_sync", trigger=trigger,
                status="queued", result_json={"data_collection": "hourly_only", "screenshot_targets": ["hourly"]})
            db.add(run)
            db.commit()
        task = asyncio.create_task(self._execute_hourly_sync(run.id), name=f"liveops-hour-sync-{schedule_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def _execute_hourly_sync(self, run_id: str) -> None:
        from liveops.settings import db_settings

        with self._automation_db() as db:
            run = db.get(CollectionRun, run_id)
            schedule_id = run.schedule_id
            run.status, run.started_at = "running", datetime.now(timezone.utc)
            settings = db_settings(db, self.settings_store)
            db.commit()
        try:
            await self._sync_hourly_bitable(schedule_id, settings, run_id)
            with self._automation_db() as db:
                statuses = list(db.scalars(select(ShiftHourDetail.bitable_status).where(ShiftHourDetail.schedule_id == schedule_id)))
                run = db.get(CollectionRun, run_id)
                errors = [value for value in statuses if value.startswith(("failed", "partial", "not_configured"))]
                run.status = "partial" if errors else "succeeded"
                run.message = "小时明细同步完成" if not errors else "小时明细同步不完整，请检查权限和字段。"
                run.missing_fields, run.finished_at = errors, datetime.now(timezone.utc)
                db.commit()
        except Exception as exc:
            with self._automation_db() as db:
                run = db.get(CollectionRun, run_id)
                run.status, run.message, run.finished_at = "failed", str(exc)[:1500], datetime.now(timezone.utc)
                db.commit()
        finally:
            await self._notify_run_problem(run_id, settings)

    def _hour_baseline(self, db: Any, schedule: ShiftSchedule, detail: ShiftHourDetail) -> dict[str, Any]:
        from liveops.service import _current_baseline, _previous_shift

        state = db.get(ShiftHourState, schedule.id)
        if state is None:
            state = ShiftHourState(schedule_id=schedule.id)
            db.add(state)
        if state.initial_baseline_json is None:
            prior_shift = _previous_shift(db, schedule)
            prior_hour = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == prior_shift.id)
                .order_by(ShiftHourDetail.period_index.desc()).limit(1)) if prior_shift else None
            baseline = (prior_hour.raw_json or {}).get("end_baseline") if prior_hour else None
            if baseline is None:
                baseline = _current_baseline(db, schedule)
            state.initial_baseline_json = {**copy.deepcopy(baseline), "chain": "shift_hourly"}
            db.flush()
        if detail.period_index == 1:
            return copy.deepcopy(state.initial_baseline_json)
        previous = db.scalar(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule.id,
            ShiftHourDetail.period_index == detail.period_index - 1))
        baseline = (previous.raw_json or {}).get("end_baseline") if previous else None
        return copy.deepcopy(baseline or {"source": "hourly_previous_missing", "chain": "shift_hourly"})

    def _save_hourly_capture(self, schedule: ShiftSchedule, run_id: str, capture: dict[str, Any],
                             detail_id: int | None = None) -> list[int]:
        with self._automation_db() as db:
            if detail_id is None:
                requested = db.scalars(select(ScreenshotRequest.hour_index).where(ScreenshotRequest.run_id == run_id,
                    ScreenshotRequest.target == "hourly", ScreenshotRequest.id.startswith("shift-hour:"))).all()
                rows = db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule.id,
                    ShiftHourDetail.period_index.in_(requested))).all() if requested else []
            else:
                row = db.get(ShiftHourDetail, detail_id)
                rows = [row] if row else []
            saved = []
            for detail in rows:
                baseline = self._hour_baseline(db, schedule, detail)
                try:
                    metrics, raw, missing = detail_values(capture, baseline, detail, schedule)
                except RuntimeError as exc:
                    metrics, raw, missing = {}, {"segments": copy.deepcopy(capture.get("segments") or []),
                        "responses": capture.get("responses") or [], "captured_at": capture.get("collected_at"),
                        "version": HOURLY_VERSION}, [str(exc)]
                raw["end_baseline"] = end_baseline(capture, run_id)
                detail.baseline_json, detail.metrics_json, detail.raw_json = baseline, metrics, raw
                detail.latest_run_id, detail.status = run_id, "partial" if missing else "succeeded"
                detail.missing_fields, detail.screenshots_json = missing, list(capture["screenshots"])
                detail.bitable_status = "pending"
                saved.append(detail.id)
            if rows:
                # A manual recapture changes the following hour's baseline, never the shift baseline.
                following = db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule.id,
                    ShiftHourDetail.period_index > min(row.period_index for row in rows))
                    .order_by(ShiftHourDetail.period_index)).all()
                for detail in following:
                    if not detail.raw_json.get("segments") or detail.id in saved:
                        continue
                    baseline = self._hour_baseline(db, schedule, detail)
                    previous_capture = {"segments": detail.raw_json["segments"],
                        "responses": detail.raw_json.get("responses", []),
                        "collected_at": detail.raw_json.get("captured_at"), "screenshots": detail.screenshots_json}
                    try:
                        metrics, raw, missing = detail_values(previous_capture, baseline, detail, schedule)
                    except RuntimeError:
                        continue
                    raw["end_baseline"] = detail.raw_json.get("end_baseline")
                    detail.baseline_json, detail.metrics_json, detail.raw_json = baseline, metrics, raw
                    detail.missing_fields, detail.status = missing, "partial" if missing else "succeeded"
                    detail.bitable_status = "pending"
                parent = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule.id))
                if parent is None:
                    parent = ShiftReport(schedule_id=schedule.id, latest_run_id=run_id, work_date=schedule.work_date,
                        start_time=schedule.start_time, end_time=schedule.end_time, host_name=schedule.host_name,
                        promoter=schedule.promoter, shop_name=capture.get("shop_name", schedule.shop_name),
                        live_room_id=capture["live_room_id"], metrics_json={},
                        raw_json={"is_final": False, "measurement_mode": "hourly_parent_pending"},
                        feishu_status="disabled: 主播汇总待结束结算", bitable_status="pending")
                    db.add(parent)
                    db.flush()
                    db.get(ShiftSchedule, schedule.id).last_report_id = parent.id
            db.commit()
            return saved

    def _hour_interruption_issues(self, schedule: ShiftSchedule, segment: dict[str, Any], stamp: datetime) -> list[str]:
        with self._automation_db() as db:
            periods = self._ensure_hour_plan(db, schedule)
            detail = next((row for row in periods if local_datetime(row.start_at) < stamp <= local_datetime(row.end_at)), None)
            if detail is None:
                return ["中断时间没有对应的主播小时段。"]
            baseline = self._hour_baseline(db, schedule, detail)
            db.commit()
        try:
            source = handoff_source(snapshot=segment["snapshot"], room=segment["room"], baseline=baseline,
                                    start=local_datetime(detail.start_at), end=stamp)
            segment["hourly_source"] = source
            return [*source.get("baseline_missing", []), *source.get("baseline_conflicts", [])]
        except RuntimeError as exc:
            return [str(exc)]

    async def _execute_shift_hour(self, run_id: str, schedule: ShiftSchedule, settings: dict[str, str]) -> None:
        from liveops.service import collect_report

        with self._automation_db() as db:
            run = db.get(CollectionRun, run_id)
            detail = db.get(ShiftHourDetail, run.result_json["hour_detail_id"])
            baseline = self._hour_baseline(db, schedule, detail)
            modules = db.scalars(select(CollectorModule).order_by(CollectorModule.sort_order)).all()
            interruptions = [row.capture_json for row in db.scalars(select(ShiftInterruption).where(
                ShiftInterruption.schedule_id == schedule.id).order_by(ShiftInterruption.interrupted_at)).all()]
            proxy = SimpleNamespace(id=schedule.id, work_date=local_datetime(detail.start_at).date(),
                start_time=local_datetime(detail.start_at).time(), end_time=local_datetime(detail.end_at).time(),
                host_name=schedule.host_name, promoter=schedule.promoter, shop_name=schedule.shop_name,
                live_room_id=schedule.live_room_id, qianchuan_account_id=schedule.qianchuan_account_id)
            detail.status = "running"
            detail.latest_run_id = run_id
            detail.baseline_json = baseline
            run.result_json = {**run.result_json, "data_collection": "hourly_only"}
            db.commit()
        self._log_step(run_id, "hour_baseline", f"采集排班第 {detail.period_index} 段，小时基线与主播汇总基线分开。")
        async with self._browser_lock:
            capture = await collect_report(settings={**settings, "feishu_chat_id": screenshot_chat(settings, "hourly")},
                schedule=proxy, modules=modules, run_id=run_id, baseline=baseline, include_daily_exposure=False,
                interruption_segments=interruptions, screenshots_enabled=True,
                on_room_confirmed=lambda selection: self._remember_screenshot_room(schedule.id, selection["live_room_id"]),
                progress=lambda stage, message, status: self._log_step(run_id, stage, message, status))
        self._save_screenshot_capture(run_id, capture["screenshots"], capture["live_room_id"])
        self._save_hourly_capture(schedule, run_id, capture, detail.id)
        await self._deliver_screenshot_requests(run_id, settings)
        await self._sync_hourly_bitable(schedule.id, settings, run_id)
        with self._automation_db() as db:
            detail = db.get(ShiftHourDetail, detail.id)
            run = db.get(CollectionRun, run_id)
            missing = list(detail.missing_fields)
            receipts = self._delivery_summary(run_id)
            missing.extend("业务群发送异常" for row in receipts if row.get("status") in {"failed", "uncertain"})
            if detail.bitable_status.startswith(("failed", "partial")):
                missing.append("小时明细多维表格同步异常")
            run.status, run.missing_fields = ("partial" if missing else "succeeded"), missing
            run.message, run.finished_at = "主播小时明细采集完成" if not missing else "小时明细采集或同步不完整", datetime.now(timezone.utc)
            run.result_json = {**run.result_json, "hour_detail_id": detail.id, "metrics": detail.metrics_json,
                "hourly_baseline": detail.baseline_json, "hourly_end_baseline": detail.raw_json.get("end_baseline"),
                "screenshots": capture["screenshots"], "integrations": {"bitable_hours": detail.bitable_status}}
            db.commit()

    async def _sync_hourly_bitable(self, schedule_id: int, settings: dict[str, str], run_id: str) -> None:
        with self._automation_db() as db:
            rows = db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule_id,
                ShiftHourDetail.status.in_(["succeeded", "partial", "missed"]))).all()
        if not rows:
            return
        ready = all(settings.get(key) for key in ("feishu_app_id", "feishu_app_secret", "bitable_app_token", "bitable_table_id"))
        if not ready:
            with self._automation_db() as db:
                for row in rows:
                    db.get(ShiftHourDetail, row.id).bitable_status = "not_configured: 多维表格未完整配置"
                db.commit()
            return
        async with self._hour_sync_lock:
            lock = self._integration_locks.setdefault(schedule_id, asyncio.Lock())
            async with lock:
                await self._sync_hourly_bitable_locked(schedule_id, settings, run_id)

    async def _archive_hour_children(self, schedule_id: int, client: BitableClient,
                                      settings: dict[str, str]) -> None:
        scope = hashlib.sha256((settings["bitable_app_token"] + "\0" + settings["bitable_table_id"]).encode()).hexdigest()
        with self._automation_db() as db:
            rows = db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule_id,
                ShiftHourDetail.bitable_record_id != "")).all()
        for row in rows:
            if row.bitable_scope != scope:
                raise RuntimeError("小时子记录属于其他多维表格配置，未删除父记录。")
            await client.delete_record(row.bitable_record_id)
            with self._automation_db() as db:
                current = db.get(ShiftHourDetail, row.id)
                current.bitable_record_id = ""
                current.bitable_status = "archived: 小时明细保存在本地"
                db.commit()

    async def _sync_hourly_bitable_locked(self, schedule_id: int, settings: dict[str, str], run_id: str) -> None:
        scope = hashlib.sha256((settings["bitable_app_token"] + "\0" + settings["bitable_table_id"]).encode()).hexdigest()
        with self._automation_db() as db:
            schedule = db.get(ShiftSchedule, schedule_id)
            parent = db.scalar(select(ShiftReport).where(ShiftReport.schedule_id == schedule_id))
            rows = db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule_id,
                ShiftHourDetail.status.in_(["succeeded", "partial", "missed"]))
                .order_by(ShiftHourDetail.period_index)).all()
            if parent is None:
                return
            if schedule.work_date < self._automation_now().date().replace(day=1):
                for row in rows:
                    row.bitable_status = "local_only: 历史月份小时明细保存在本地"
                db.commit()
                return
            binding = db.get(ShiftHourState, schedule_id)
            if binding is None:
                binding = ShiftHourState(schedule_id=schedule_id)
                db.add(binding)
                db.flush()
            parent_id = binding.parent_record_id if binding.bitable_scope == scope else ""
            if not parent_id and not binding.bitable_scope:
                parent_id = parent.bitable_record_id
            mapping = json.loads(settings.get("bitable_field_mapping", "{}"))
            parent_values = {**parent.metrics_json, "work_date": schedule.work_date.isoformat(),
                "host_name": schedule.host_name, "promoter": schedule.promoter,
                "time_range": f"{schedule.start_time:%H:%M}-{schedule.end_time:%H:%M}",
                "duration_hours": sum((right-left).total_seconds() for left, right in shift_periods(schedule)) / 3600}
            parent_db_id = parent.id
        client = BitableClient(settings["feishu_app_id"], settings["feishu_app_secret"],
                               settings["bitable_app_token"], settings["bitable_table_id"])
        failures = []
        try:
            hierarchy = await client.ensure_hour_hierarchy(settings.get("bitable_parent_field", "父记录"),
                                                          settings.get("bitable_hourly_view_id", ""))
            result = await client.upsert_record(values=parent_values, field_mapping=mapping, record_id=parent_id,
                sync_key_field=settings.get("bitable_sync_key_field", "SourceID"), sync_key=f"shift-{schedule_id}",
                lookup_sync_key=True)
            parent_id = result["record_id"]
            with self._automation_db() as db:
                state = db.get(ShiftHourState, schedule_id)
                state.parent_record_id, state.bitable_scope = parent_id, scope
                db.get(ShiftReport, parent_db_id).bitable_record_id = parent_id
                db.commit()
            for row in rows:
                left, right = local_datetime(row.start_at), local_datetime(row.end_at)
                values = {**row.metrics_json, "work_date": schedule.work_date.isoformat(),
                    "host_name": schedule.host_name, "promoter": schedule.promoter,
                    "time_range": f"{left:%H:%M}-{right:%H:%M}", "duration_hours": (right-left).total_seconds()/3600,
                    "parent_record": [parent_id], "collection_status": row.status, "period_index": row.period_index}
                try:
                    result = await client.upsert_record(values=values,
                        field_mapping={**mapping, "parent_record": hierarchy["parent_field"]},
                        record_id=row.bitable_record_id if row.bitable_scope == scope else "",
                        sync_key_field=settings.get("bitable_sync_key_field", "SourceID"),
                        sync_key=f"shift-{schedule_id}-hour-{row.period_index}-{HOURLY_VERSION}", lookup_sync_key=True)
                    with self._automation_db() as db:
                        current = db.get(ShiftHourDetail, row.id)
                        current.bitable_record_id, current.bitable_scope = result["record_id"], scope
                        current.bitable_status = "partial: 字段映射不完整" if result["missing_mappings"] else "synced"
                        if result["missing_mappings"]:
                            failures.append(row.id)
                        db.commit()
                except Exception as exc:
                    failures.append(row.id)
                    with self._automation_db() as db:
                        db.get(ShiftHourDetail, row.id).bitable_status = f"failed: {str(exc)[:400]}"
                        db.commit()
        except Exception as exc:
            failures = [row.id for row in rows]
            with self._automation_db() as db:
                for row in rows:
                    db.get(ShiftHourDetail, row.id).bitable_status = f"failed: {str(exc)[:400]}"
                db.commit()
        finally:
            await client.close()
        with self._automation_db() as db:
            parent = db.get(ShiftReport, parent_db_id)
            parent.raw_json = {**parent.raw_json, "hourly_bitable_status": "partial" if failures else "synced"}
            if failures and not parent.bitable_status.startswith("failed"):
                parent.bitable_status = "partial: 小时明细同步不完整"
            elif not failures and parent.bitable_status.startswith("partial: 小时明细"):
                parent.bitable_status = "synced"
            elif not failures and parent.raw_json.get("measurement_mode") == "hourly_parent_pending":
                parent.bitable_status = "synced: 已建立小时父记录，主播汇总待结算"
            db.commit()
        self._log_step(run_id, "bitable_hours", "小时子记录同步完成" if not failures else "部分小时子记录未同步，请查看详情。",
                       "succeeded" if not failures else "failed")
