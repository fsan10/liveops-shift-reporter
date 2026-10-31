from __future__ import annotations

import base64
import hmac
import json
import os
from contextlib import asynccontextmanager, nullcontext
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from liveops.bitable import BitableClient
from liveops.collector import DEFAULT_MODULES
from liveops.db import SessionLocal, init_db
from liveops.feishu import FeishuClient
from liveops.formulas import REPORT_COLUMNS
from liveops.models import (
    CollectionRun,
    CollectorModule,
    ScheduleDay,
    ScreenshotRequest,
    ShiftHourDetail,
    ShiftHourState,
    ShiftInterruption,
    ShiftReport,
    ShiftSchedule,
)
from liveops.monthly import remember_manual_table
from liveops.portal import schedule_end_datetime, shift_datetime
from liveops.roster_import import MAX_FILE_BYTES, read_roster, roster_template
from liveops.security import SecretBox
from liveops.service import RunDispatcher, now_local, schedule_end, schedule_start
from liveops.settings import (
    BOOLEAN_SETTINGS,
    DEFAULT_SETTINGS,
    SECRET_KEYS,
    SettingsStore,
    db_settings,
    screenshot_chat,
)


class ScheduleInput(BaseModel):
    work_date: date
    start_time: str
    end_time: str
    host_name: str = Field(min_length=1, max_length=120)
    ops: str = Field(default="", max_length=120)
    promoter: str = Field(default="", max_length=120)
    shop_name: str = Field(default="", max_length=160)
    live_room_id: str = Field(default="", max_length=40)
    qianchuan_account_id: str = Field(default="", max_length=40)
    enabled: bool = True
    message_template: str = ""


class ScheduleDayInput(BaseModel):
    rest: str = Field(default="", max_length=500)
    shoot: str = Field(default="", max_length=500)


class InterruptionInput(BaseModel):
    interrupted_at: datetime | None = None


class AutoScheduleInput(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    replace: bool = False


class ModulePatch(BaseModel):
    enabled: bool | None = None
    screenshot_mode: str | None = None
    selector: str | None = None
    section_label: str | None = None
    url_template: str | None = None


class SettingsInput(BaseModel):
    values: dict[str, str]


class BitableRangeSyncInput(BaseModel):
    date_start: date
    date_end: date
    mode: str = "incremental"


class RunInput(BaseModel):
    schedule_id: int


def _clear_uncaptured_hour_plan(db: Session, schedule_id: int) -> None:
    rows = db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == schedule_id)).all()
    state = db.get(ShiftHourState, schedule_id)
    if any(row.latest_run_id for row in rows) or (state and state.initial_baseline_json is not None):
        raise HTTPException(status_code=409, detail="该排班已有小时采集记录，请停用以保留审计。")
    for row in rows:
        db.delete(row)
    if state:
        db.delete(state)
    db.flush()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    secret_box = SecretBox()
    store = SettingsStore(secret_box)
    app.state.settings_store = store
    app.state.dispatcher = RunDispatcher(store)
    with SessionLocal() as db:
        existing = set(db.scalars(select(CollectorModule.code)).all())
        for row in DEFAULT_MODULES:
            if row["code"] not in existing:
                db.add(CollectorModule(**row, url_template=""))
        db.commit()
    app.state.dispatcher.start_scheduler()
    yield
    await app.state.dispatcher.stop_scheduler()


app = FastAPI(title="直播运营数据采集与报表", version="0.1.0", lifespan=lifespan)
static_dir = Path(__file__).parent / "static"
SCHEDULE_TEMPLATE_PATH = Path(__file__).parent / "data" / "schedule_template_2026-10.json"
SCHEDULE_TEMPLATE_EXAMPLE_PATH = Path(__file__).parent / "data" / "schedule_template_2026-10.example.json"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _db() -> Session:
    return SessionLocal()


def _store(request: Request) -> SettingsStore:
    return request.app.state.settings_store


def _dispatcher(request: Request) -> RunDispatcher:
    return request.app.state.dispatcher


def _parse_time(value: str) -> time:
    raw = value.strip()
    for time_format in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(raw, time_format).time()
        except ValueError:
            continue
    raise HTTPException(status_code=422, detail="时刻请按 HH:MM:SS 填写，例如 16:30:00。")


def _require_editable_schedule_date(work_date: date) -> None:
    if work_date < now_local().date():
        raise HTTPException(status_code=409, detail="历史日期的排班只读，不能新增或修改。")


def _parse_schedule_payload(payload: ScheduleInput, settings: dict[str, str]) -> dict[str, Any]:
    host_name = payload.host_name.strip()
    room_id = payload.live_room_id.strip() or settings.get("live_room_id", "")
    aavid = payload.qianchuan_account_id.strip() or settings.get("qianchuan_account_id", "")
    if not host_name:
        raise HTTPException(status_code=422, detail="请填写主播姓名。")
    return {
        "work_date": payload.work_date,
        "start_time": _parse_time(payload.start_time),
        "end_time": _parse_time(payload.end_time),
        "host_name": host_name,
        "ops": payload.ops.strip(),
        "promoter": payload.promoter.strip(),
        "shop_name": payload.shop_name.strip() or settings.get("shop_name", ""),
        "live_room_id": room_id,
        "qianchuan_account_id": aavid,
        "enabled": payload.enabled,
        "message_template": payload.message_template.strip(),
    }


def _validate_schedule(db: Session, values: dict[str, Any], exclude_id: int | None = None) -> None:
    if values["start_time"] == values["end_time"]:
        raise HTTPException(status_code=422, detail="排班开始和结束时间不能相同。")
    candidate_start = shift_datetime(values["work_date"], values["start_time"])
    candidate_end = schedule_end_datetime(
        values["work_date"], values["start_time"], values["end_time"]
    )
    nearby_dates = {
        values["work_date"] - timedelta(days=1),
        values["work_date"],
        values["work_date"] + timedelta(days=1),
    }
    query = select(ShiftSchedule).where(ShiftSchedule.work_date.in_(nearby_dates))
    if exclude_id:
        query = query.where(ShiftSchedule.id != exclude_id)
    existing = db.scalars(query).all()
    for row in existing:
        row_start, row_end = schedule_start(row), schedule_end(row)
        if candidate_start < row_end and row_start < candidate_end:
            raise HTTPException(
                status_code=409,
                detail=f"排班时间与 {row.work_date} {row.start_time.strftime('%H:%M')}-{row.end_time.strftime('%H:%M')} ({row.host_name}) 重叠。",
            )
        if (
            values["enabled"]
            and row.enabled
            and row.work_date == values["work_date"]
            and row.host_name.casefold() == values["host_name"].casefold()
        ):
            raise HTTPException(
                status_code=409,
                detail=f"{row.work_date} 的主播名必须按时段区分，已存在“{row.host_name}”。",
            )


def _schedule_json(
    db: Session, row: ShiftSchedule, report_ids: dict[int, int] | None = None
) -> dict[str, Any]:
    report_id = (
        report_ids.get(row.id)
        if report_ids is not None
        else db.scalar(select(ShiftReport.id).where(ShiftReport.schedule_id == row.id))
    )
    return {
        "id": row.id,
        "work_date": row.work_date.isoformat(),
        "start_time": row.start_time.strftime("%H:%M:%S"),
        "end_time": row.end_time.strftime("%H:%M:%S"),
        "duration_hours": round(
            (schedule_end(row) - schedule_start(row)).total_seconds() / 3600, 2
        ),
        "host_name": row.host_name,
        "ops": row.ops,
        "promoter": row.promoter,
        "shop_name": row.shop_name,
        "live_room_id": row.live_room_id,
        "qianchuan_account_id": row.qianchuan_account_id,
        "enabled": row.enabled,
        "message_template": row.message_template,
        "has_baseline": row.baseline_json is not None,
        "baseline_at": row.baseline_at.isoformat() if row.baseline_at else None,
        "report_id": report_id,
        "interruptions": [
            {"id": event.id, "live_room_id": event.live_room_id,
             "interrupted_at": event.interrupted_at.isoformat(),
             "status": "partial" if event.capture_json.get("page_errors") or
             event.capture_json["source"].get("baseline_missing") or
             event.capture_json["source"].get("baseline_conflicts") else "succeeded"}
            for event in db.scalars(select(ShiftInterruption).where(
                ShiftInterruption.schedule_id == row.id
            ).order_by(ShiftInterruption.interrupted_at)).all()
        ],
    }


def _report_json(row: ShiftReport, include_raw: bool = False) -> dict[str, Any]:
    result = {
        "id": row.id,
        "schedule_id": row.schedule_id,
        "work_date": row.work_date.isoformat(),
        "start_time": row.start_time.strftime("%H:%M"),
        "end_time": row.end_time.strftime("%H:%M"),
        "host_name": row.host_name,
        "promoter": row.promoter,
        "shop_name": row.shop_name,
        "live_room_id": row.live_room_id,
        "metrics": row.metrics_json,
        "screenshots": [
            {**item, "url": "/artifacts/" + item.get("relative_path", "")}
            for item in (row.screenshots_json or [])
        ],
        "missing_fields": row.missing_fields or [],
        "feishu_status": row.feishu_status,
        "bitable_status": row.bitable_status,
        "synced_at": row.synced_at.isoformat() if row.synced_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "summary_final": (row.raw_json or {}).get("is_final", True),
        "summary_pending": (row.raw_json or {}).get("measurement_mode") == "hourly_parent_pending",
    }
    if include_raw:
        result["raw"] = row.raw_json
    return result


def _hour_json(row: ShiftHourDetail) -> dict[str, Any]:
    from liveops.sessions import local_datetime

    return {"id": row.id, "schedule_id": row.schedule_id, "period_index": row.period_index,
            "start_at": local_datetime(row.start_at).isoformat(), "end_at": local_datetime(row.end_at).isoformat(),
            "status": row.status, "metrics": {"duration_hours": (local_datetime(row.end_at)-local_datetime(row.start_at)).total_seconds()/3600,
                                                **row.metrics_json}, "missing_fields": row.missing_fields,
            "baseline": row.baseline_json, "end_baseline": (row.raw_json or {}).get("end_baseline"),
            "captured_at": (row.raw_json or {}).get("captured_at"), "latest_run_id": row.latest_run_id,
            "bitable_status": row.bitable_status,
            "screenshots": [{**item, "url": "/artifacts/" + item.get("relative_path", "")}
                            for item in row.screenshots_json]}


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.isoformat()


def _run_json(row: CollectionRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "schedule_id": row.schedule_id,
        "kind": row.kind,
        "trigger": row.trigger,
        "status": row.status,
        "message": row.message,
        "missing_fields": row.missing_fields or [],
        "result": row.result_json or {},
        "started_at": _utc_iso(row.started_at),
        "finished_at": _utc_iso(row.finished_at),
        "created_at": _utc_iso(row.created_at),
    }


def _screenshot_history_json(row: ScreenshotRequest) -> dict[str, Any]:
    from liveops.sessions import local_datetime

    delivery = row.delivery_json or {}
    message = delivery.get("reason") or delivery.get("error") or {
        "missed": "错过计划采集时点，未补造历史截图或累计快照。",
        "skipped": "该截图计划已跳过。",
        "failed": "截图计划执行失败，请检查运行记录。",
    }.get(row.status, "")
    return {
        "id": f"screenshot-request:{row.id}", "entry_type": "screenshot_request",
        "schedule_id": row.schedule_id,
        "kind": "hourly_screenshot_plan" if row.target == "hourly" else "anchor_screenshot_plan",
        "trigger": "scheduler", "status": row.status, "message": message,
        "planned_at": local_datetime(row.planned_at).isoformat(),
        "started_at": None, "finished_at": None, "created_at": _utc_iso(row.created_at),
        "missing_fields": [message] if row.status in {"missed", "failed"} else [],
        "result": {"screenshot_request_id": row.id, "screenshot_target": row.target,
                   "planned_at": local_datetime(row.planned_at).isoformat(),
                   "capture_status": row.status, "delivery": delivery,
                   "screenshots": row.screenshots_json or [], "executed": False},
    }


def _run_history_page(
    db: Session, offset: int, size: int, *, start_at: datetime | None = None,
    end_at: datetime | None = None, time_field: str = "task", kinds: set[str] | None = None,
    statuses: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Merge actual runs and unexecuted terminal plans without duplicating shared captures."""
    from liveops.portal import APP_TIMEZONE
    from liveops.sessions import local_datetime

    run_time = CollectionRun.finished_at if time_field == "finished" else func.coalesce(CollectionRun.started_at, CollectionRun.created_at)
    run_filters = [CollectionRun.finished_at.is_not(None)] if time_field == "finished" else []
    plan_filters = [CollectionRun.id.is_(None), ScreenshotRequest.status.in_(["missed", "skipped", "failed"])]
    if time_field == "finished":
        plan_filters.append(False)  # Unexecuted plans have no completion time.
    if kinds:
        run_filters.append(CollectionRun.kind.in_(kinds))
        targets = [target for target, kind in [("hourly", "hourly_screenshot_plan"), ("anchor", "anchor_screenshot_plan")] if kind in kinds]
        plan_filters.append(ScreenshotRequest.target.in_(targets))
    if statuses:
        run_filters.append(CollectionRun.status.in_(statuses))
        plan_filters.append(ScreenshotRequest.status.in_(statuses))
    for value, is_start in [(start_at, True), (end_at, False)]:
        if value is None:
            continue
        aware = value.replace(tzinfo=APP_TIMEZONE) if value.tzinfo is None else value
        utc = aware.astimezone(timezone.utc).replace(tzinfo=None)
        local = aware.astimezone(APP_TIMEZONE).replace(tzinfo=None)
        run_filters.append(run_time >= utc if is_start else run_time <= utc)
        plan_filters.append(ScreenshotRequest.planned_at >= local if is_start else ScreenshotRequest.planned_at <= local)
    total = (db.scalar(select(func.count(CollectionRun.id)).where(*run_filters)) or 0) + (db.scalar(
        select(func.count(ScreenshotRequest.id)).outerjoin(
            CollectionRun, CollectionRun.id == ScreenshotRequest.run_id).where(*plan_filters)) or 0)
    if offset >= total:
        return [], total
    # Fetch only ordering metadata until pagination is resolved; raw responses stay out of this merge.
    count = offset + size
    runs = db.execute(select(CollectionRun.id, run_time).where(*run_filters).order_by(
        run_time.desc(), CollectionRun.id.desc()).limit(count)).all()
    plans = db.execute(select(ScreenshotRequest.id, ScreenshotRequest.planned_at).outerjoin(
        CollectionRun, CollectionRun.id == ScreenshotRequest.run_id).where(*plan_filters).order_by(
            ScreenshotRequest.planned_at.desc(), ScreenshotRequest.id.desc()).limit(count)).all()
    entries = [(datetime.fromisoformat(_utc_iso(at)), "run", key) for key, at in runs]
    entries.extend((local_datetime(at).astimezone(timezone.utc), "plan", key) for key, at in plans)
    entries.sort(reverse=True)
    selected = entries[offset:offset + size]
    run_ids = [key for _, kind, key in selected if kind == "run"]
    plan_ids = [key for _, kind, key in selected if kind == "plan"]
    run_rows = {row.id: row for row in db.scalars(select(CollectionRun).where(CollectionRun.id.in_(run_ids)))}
    plan_rows = {row.id: row for row in db.scalars(select(ScreenshotRequest).where(ScreenshotRequest.id.in_(plan_ids)))}
    return [(_run_json(run_rows[key]) if kind == "run" else _screenshot_history_json(plan_rows[key]))
            for _, kind, key in selected], total


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    username, password = os.getenv("ADMIN_USERNAME", ""), os.getenv("ADMIN_PASSWORD", "")
    if username and password:
        auth = request.headers.get("authorization", "")
        valid = False
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                supplied_user, supplied_pass = decoded.split(":", 1)
                valid = hmac.compare_digest(supplied_user, username) and hmac.compare_digest(
                    supplied_pass, password
                )
            except (ValueError, UnicodeDecodeError):
                valid = False
        if not valid:
            return JSONResponse(
                {"detail": "需要管理员账号和密码。"},
                status_code=401,
                headers={"WWW-Authenticate": "Basic"},
            )
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def home() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/status")
async def app_status(request: Request) -> dict[str, Any]:
    store = _store(request)
    with _db() as db:
        settings = db_settings(db, store)
        running = db.scalars(
            select(CollectionRun).where(CollectionRun.status.in_(["queued", "running"]))
        ).all()
        modules = db.scalar(select(CollectorModule.code).limit(1)) is not None
    return {
        "ok": True,
        "second_precision_supported": True,
        "features": {"report_date_range": True, "run_filters": True, "bitable_monthly": True},
        "timezone": os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
        "browser_mode": settings.get("browser_mode", "cloak_cdp"),
        "browser_configured": bool(
            settings.get("cloak_profile_id") or settings.get("cloak_profile_name")
        )
        if settings.get("browser_mode") == "cloak_cdp"
        else True,
        "running_count": len(running),
        "collector_modules_seeded": modules,
        "current_time": now_local().isoformat(),
    }


@app.get("/api/dashboard")
async def dashboard(request: Request, work_date: date | None = None) -> dict[str, Any]:
    selected = work_date or now_local().date()
    with _db() as db:
        rows = db.scalars(
            select(ShiftReport)
            .where(ShiftReport.work_date == selected)
            .order_by(ShiftReport.start_time)
        ).all()
        reports = [_report_json(row) for row in rows]
        metrics = [row.metrics_json or {} for row in rows]

        def total(key: str, places: int = 2) -> float | None:
            values = [float(row[key]) for row in metrics if row.get(key) is not None]
            return round(sum(values), places) if values else None

        total_gmv = total("gmv")
        total_coupon = total("qianchuan_coupon")
        total_overall = total("overall_amount")
        total_spend = total("spend")
        total_viewers = total("viewers", 0)
        roi = (
            total_overall / total_spend
            if total_overall is not None and total_spend is not None and total_spend
            else None
        )
        schedule_count = len(
            db.scalars(select(ShiftSchedule).where(ShiftSchedule.work_date == selected)).all()
        )
    return {
        "work_date": selected.isoformat(),
        "schedule_count": schedule_count,
        "report_count": len(reports),
        "totals": {
            "gmv": total_gmv,
            "coupon": total_coupon,
            "overall": total_overall,
            "spend": total_spend,
            "viewers": total_viewers,
            "roi": round(roi, 2) if roi is not None else None,
        },
        "series": [
            {
                "label": f"{row['start_time']} {row['host_name']}",
                "gmv": row["metrics"].get("gmv"),
                "overall": row["metrics"].get("overall_amount"),
                "roi": row["metrics"].get("overall_roi"),
                "viewers": row["metrics"].get("viewers"),
            }
            for row in reports
        ],
        "reports": reports,
    }


@app.get("/api/schedules")
async def list_schedules(
    request: Request,
    work_date: date | None = None,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    if (year is None) != (month is None):
        raise HTTPException(status_code=422, detail="按月读取时请同时提供 year 和 month。")
    if year is not None and month is not None:
        if year < 2000 or year > 2100 or month < 1 or month > 12:
            raise HTTPException(status_code=422, detail="年月无效。")
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1)
        selected = start
        period = select(ShiftSchedule).where(
            ShiftSchedule.work_date >= start, ShiftSchedule.work_date < end
        )
        day_period = select(ScheduleDay).where(
            ScheduleDay.work_date >= start, ScheduleDay.work_date < end
        )
    else:
        selected = work_date or now_local().date()
        period = select(ShiftSchedule).where(ShiftSchedule.work_date == selected)
        day_period = select(ScheduleDay).where(ScheduleDay.work_date == selected)
    with _db() as db:
        rows = db.scalars(
            period.order_by(ShiftSchedule.work_date, ShiftSchedule.start_time)
        ).all()
        days = {
            row.work_date.isoformat(): {"rest": row.rest, "shoot": row.shoot}
            for row in db.scalars(day_period).all()
        }
        report_ids = {
            schedule_id: report_id
            for report_id, schedule_id in db.execute(
                select(ShiftReport.id, ShiftReport.schedule_id).where(
                    ShiftReport.schedule_id.in_([row.id for row in rows])
                )
            ).all()
        } if rows else {}
        result = {
            "work_date": selected.isoformat(),
            "items": [_schedule_json(db, row, report_ids) for row in rows],
            "days": days,
        }
        if year is not None and month is not None:
            result["year"] = year
            result["month"] = month
        return result


@app.put("/api/schedule-days/{work_date}")
async def update_schedule_day(work_date: date, payload: ScheduleDayInput) -> dict[str, Any]:
    _require_editable_schedule_date(work_date)
    with _db() as db:
        row = db.get(ScheduleDay, work_date)
        rest, shoot = payload.rest.strip(), payload.shoot.strip()
        if not rest and not shoot:
            if row is not None:
                db.delete(row)
            db.commit()
            return {"work_date": work_date.isoformat(), "rest": "", "shoot": ""}
        if row is None:
            row = ScheduleDay(work_date=work_date)
            db.add(row)
        row.rest, row.shoot = rest, shoot
        db.commit()
        return {
            "work_date": work_date.isoformat(),
            "rest": row.rest,
            "shoot": row.shoot,
        }


@app.post("/api/schedules/auto-fill")
async def auto_fill_schedules(payload: AutoScheduleInput, request: Request) -> dict[str, Any]:
    start = date(payload.year, payload.month, 1)
    end = date(payload.year + (payload.month == 12), payload.month % 12 + 1, 1)
    editable_start = max(start, now_local().date())
    if editable_start >= end:
        raise HTTPException(status_code=409, detail="历史月份的排班只读，不能自动排班或恢复模板。")
    template_path = (
        SCHEDULE_TEMPLATE_PATH
        if SCHEDULE_TEMPLATE_PATH.is_file()
        else SCHEDULE_TEMPLATE_EXAMPLE_PATH
    )
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="10 月排班模板读取失败。") from exc
    templates_by_weekday: dict[int, list[dict[str, Any]]] = {}
    for template_day in template["days"]:
        template_date = datetime.strptime(template_day["date"], "%Y/%m/%d").date()
        templates_by_weekday.setdefault(template_date.weekday(), []).append(template_day)

    with _db() as db:
        has_schedules = db.scalar(
            select(ShiftSchedule.id).where(
                ShiftSchedule.work_date >= start, ShiftSchedule.work_date < end
            ).limit(1)
        )
        has_day_notes = db.scalar(
            select(ScheduleDay.work_date).where(
                ScheduleDay.work_date >= start, ScheduleDay.work_date < end
            ).limit(1)
        )
        if has_schedules or has_day_notes:
            if not payload.replace:
                raise HTTPException(
                    status_code=409,
                    detail="该月已有排班或日备注，自动排班不会覆盖；请先选择空月份。",
                )
            existing_rows = db.scalars(
                select(ShiftSchedule).where(
                    ShiftSchedule.work_date >= editable_start, ShiftSchedule.work_date < end
                )
            ).all()
            if existing_rows and db.scalar(
                select(ShiftReport.id).where(
                    ShiftReport.schedule_id.in_([row.id for row in existing_rows])
                ).limit(1)
            ):
                raise HTTPException(
                    status_code=409,
                    detail="该月已有采集报告，不能恢复模板排班；可逐条停用以保留报告。",
                )
            if existing_rows and db.scalar(
                select(CollectionRun.id).where(
                    CollectionRun.schedule_id.in_([row.id for row in existing_rows])
                ).limit(1)
            ):
                raise HTTPException(
                    status_code=409,
                    detail="该月已有采集记录，不能恢复模板排班；可逐条停用以保留记录。",
                )
            for row in existing_rows:
                _clear_uncaptured_hour_plan(db, row.id)
                db.delete(row)
            for note in db.scalars(
                select(ScheduleDay).where(
                    ScheduleDay.work_date >= editable_start, ScheduleDay.work_date < end
                )
            ).all():
                db.delete(note)
            db.flush()
        settings = db_settings(db, _store(request))
        weekday_occurrences: dict[int, int] = {}
        created: list[ShiftSchedule] = []
        day = start
        while day < end:
            weekday = day.weekday()
            candidates = templates_by_weekday.get(weekday, [])
            if not candidates:
                raise HTTPException(status_code=500, detail=f"10 月模板缺少星期 {weekday}。")
            occurrence = weekday_occurrences.get(weekday, 0)
            weekday_occurrences[weekday] = occurrence + 1
            source = candidates[occurrence % len(candidates)]
            if day < editable_start:
                day += timedelta(days=1)
                continue
            db.add(ScheduleDay(work_date=day, rest=source.get("rest", ""), shoot=source.get("shoot", "")))
            for shift in source["shifts"]:
                start_text, end_text = shift["time"].split("-", 1)
                start_time = _parse_time(start_text)
                end_time = _parse_time(end_text)
                values = {
                    "work_date": day,
                    "start_time": start_time,
                    "end_time": end_time,
                    "host_name": shift["anchor"].strip(),
                    "ops": "",
                    "promoter": shift.get("ops", "").strip(),
                    "shop_name": settings.get("shop_name", ""),
                    "live_room_id": settings.get("live_room_id", ""),
                    "qianchuan_account_id": settings.get("qianchuan_account_id", ""),
                    "enabled": True,
                    "message_template": "",
                }
                _validate_schedule(db, values)
                row = ShiftSchedule(**values)
                db.add(row)
                db.flush()
                created.append(row)
            day += timedelta(days=1)
        db.commit()
        return {
            "year": payload.year,
            "month": payload.month,
            "count": len(created),
            "items": [_schedule_json(db, row) for row in created],
        }


@app.get("/api/schedules/import-template")
async def download_roster_template() -> Response:
    content = await run_in_threadpool(roster_template, now_local().date())
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="roster-import-template.xlsx"'})


@app.post("/api/schedules/import")
async def import_schedules(request: Request, file: UploadFile, preview: bool = True) -> Any:
    try:
        content = await file.read(MAX_FILE_BYTES + 1)
    finally:
        await file.close()
    try:
        rows = await run_in_threadpool(read_roster, content, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    today = now_local().date()
    results: list[dict[str, Any]] = []
    counts = {"new": 0, "duplicate": 0, "historical": 0, "error": 0}
    created_count = 0
    note_dates: set[date] = set()
    with _db() as db:
        settings = db_settings(db, _store(request))
        for source in rows:
            item: dict[str, Any] = {
                "row": source.number,
                "work_date": source.work_date.isoformat() if source.work_date else "",
                "time": f"{source.start_time}-{source.end_time}" if source.start_time else "",
                "host_name": source.host_name, "promoter": source.promoter,
                "rest": source.rest, "shoot": source.shoot,
            }
            try:
                if source.work_date and source.work_date < today:
                    item.update(status="historical", message="历史日期只读，跳过。")
                else:
                    if source.error:
                        raise HTTPException(status_code=422, detail=source.error)
                    note = db.get(ScheduleDay, source.work_date)
                    changes = {}
                    for key in ("rest", "shoot"):
                        incoming = getattr(source, key)
                        current = getattr(note, key) if note else ""
                        if incoming and current and incoming != current:
                            raise HTTPException(status_code=409, detail="休息或拍摄备注与已有内容冲突，请先在页面核对。")
                        if incoming and not current:
                            changes[key] = incoming
                    new_shift = False
                    if source.host_name:
                        payload = ScheduleInput(
                            work_date=source.work_date, start_time=source.start_time,
                            end_time=source.end_time, host_name=source.host_name,
                            promoter=source.promoter,
                        )
                        values = _parse_schedule_payload(payload, settings)
                        existing = db.scalar(select(ShiftSchedule).where(
                            ShiftSchedule.work_date == source.work_date,
                            ShiftSchedule.start_time == values["start_time"],
                            ShiftSchedule.end_time == values["end_time"],
                        ))
                        duplicate = existing and existing.host_name == values["host_name"] and existing.promoter == values["promoter"]
                        if not duplicate:
                            _validate_schedule(db, values)
                            db.add(ShiftSchedule(**values))
                            new_shift = True
                    if changes:
                        if note is None:
                            note = ScheduleDay(work_date=source.work_date, rest="", shoot="")
                            db.add(note)
                        for key, value in changes.items():
                            setattr(note, key, value)
                    try:
                        db.flush()
                    except IntegrityError:
                        db.rollback()
                        return JSONResponse(status_code=409, content={
                            "detail": "排班已被其他操作修改，请重新预览后导入。",
                        })
                    created_count += int(new_shift)
                    if changes:
                        note_dates.add(source.work_date)
                    if new_shift or changes:
                        item.update(status="new", message="新增班次" if new_shift else "新增日期备注")
                    else:
                        item.update(status="duplicate", message="内容已存在，跳过；保留现有配置和启停状态。")
            except HTTPException as exc:
                item.update(status="error", message=str(exc.detail))
            counts[item["status"]] += 1
            results.append(item)
        valid = counts["error"] == 0
        result = {"preview": preview, "valid": valid, "counts": counts, "rows": results,
                  "schedule_count": created_count, "note_count": len(note_dates), "imported": False}
        if preview or not valid:
            db.rollback()
        else:
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise HTTPException(status_code=409, detail="排班已被其他操作修改，请重新预览后导入。") from exc
            result["imported"] = True
        if not preview and not valid:
            return JSONResponse(status_code=409, content=result)
        return result


@app.post("/api/schedules")
async def create_schedule(payload: ScheduleInput, request: Request) -> dict[str, Any]:
    _require_editable_schedule_date(payload.work_date)
    store = _store(request)
    with _db() as db:
        values = _parse_schedule_payload(payload, db_settings(db, store))
        _validate_schedule(db, values)
        schedule = ShiftSchedule(**values)
        db.add(schedule)
        db.commit()
        db.refresh(schedule)
        return {"item": _schedule_json(db, schedule)}


@app.put("/api/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: int, payload: ScheduleInput, request: Request
) -> dict[str, Any]:
    store = _store(request)
    with _db() as db:
        schedule = db.get(ShiftSchedule, schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404, detail="排班不存在。")
        _require_editable_schedule_date(schedule.work_date)
        _require_editable_schedule_date(payload.work_date)
        values = _parse_schedule_payload(payload, db_settings(db, store))
        captured_hour = db.scalar(select(ShiftHourDetail.id).where(ShiftHourDetail.schedule_id == schedule_id,
            ShiftHourDetail.latest_run_id.is_not(None)).limit(1))
        if captured_hour and any(values[key] != getattr(schedule, key) for key in ("work_date", "start_time", "end_time")):
            raise HTTPException(status_code=409, detail="该排班已有小时采集，不能修改日期或时段；请保留原记录。")
        _validate_schedule(db, values, exclude_id=schedule_id)
        for key, value in values.items():
            setattr(schedule, key, value)
        db.commit()
        db.refresh(schedule)
        return {"item": _schedule_json(db, schedule)}


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int) -> dict[str, bool]:
    with _db() as db:
        schedule = db.get(ShiftSchedule, schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404, detail="排班不存在。")
        _require_editable_schedule_date(schedule.work_date)
        if db.scalar(select(ShiftReport.id).where(ShiftReport.schedule_id == schedule_id)) or db.scalar(
            select(ShiftInterruption.id).where(ShiftInterruption.schedule_id == schedule_id)
        ) or db.scalar(select(ScreenshotRequest.id).where(ScreenshotRequest.schedule_id == schedule_id)):
            raise HTTPException(
                status_code=409, detail="该排班已有报告，不能直接删除；请停用排班以保留审计记录。"
            )
        _clear_uncaptured_hour_plan(db, schedule_id)
        db.delete(schedule)
        db.commit()
        return {"ok": True}


@app.post("/api/schedules/{schedule_id}/toggle")
async def toggle_schedule(schedule_id: int) -> dict[str, Any]:
    with _db() as db:
        schedule = db.get(ShiftSchedule, schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404, detail="排班不存在。")
        _require_editable_schedule_date(schedule.work_date)
        schedule.enabled = not schedule.enabled
        db.commit()
        db.refresh(schedule)
        return {"item": _schedule_json(db, schedule)}


@app.post("/api/schedules/{schedule_id}/baseline")
async def create_baseline(schedule_id: int, request: Request) -> dict[str, Any]:
    try:
        run = await _dispatcher(request).begin_run(schedule_id, "baseline", "user")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": _run_json(run)}


@app.post("/api/schedules/{schedule_id}/run")
async def run_schedule(schedule_id: int, request: Request) -> dict[str, Any]:
    try:
        run = await _dispatcher(request).begin_manual_run(schedule_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": _run_json(run)}


@app.post("/api/schedules/{schedule_id}/interrupt")
async def interrupt_schedule(schedule_id: int, payload: InterruptionInput, request: Request) -> dict[str, Any]:
    try:
        run = await _dispatcher(request).begin_run(schedule_id, "interruption", "user", payload.interrupted_at)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": _run_json(run)}


@app.get("/api/collectors")
async def list_collectors() -> dict[str, Any]:
    with _db() as db:
        rows = db.scalars(select(CollectorModule).order_by(CollectorModule.sort_order)).all()
        return {
            "items": [
                {
                    "code": row.code,
                    "name": row.name,
                    "page_kind": row.page_kind,
                    "screenshot_mode": row.screenshot_mode,
                    "selector": row.selector,
                    "section_label": row.section_label,
                    "enabled": row.enabled,
                    "required_for_data": row.required_for_data,
                    "notes": row.notes,
                }
                for row in rows
            ]
        }


@app.patch("/api/collectors/{code}")
async def update_collector(code: str, payload: ModulePatch) -> dict[str, Any]:
    values = payload.model_dump(exclude_unset=True)
    if "screenshot_mode" in values and values["screenshot_mode"] not in {"full_page", "element"}:
        raise HTTPException(status_code=422, detail="截图模式只能是 element 或 full_page。")
    if "selector" in values and len(values["selector"]) > 500:
        raise HTTPException(status_code=422, detail="选择器不能超过 500 字符。")
    with _db() as db:
        row = db.get(CollectorModule, code)
        if row is None:
            raise HTTPException(status_code=404, detail="采集模块不存在。")
        for key, value in values.items():
            setattr(row, key, value)
        db.commit()
        return {
            "ok": True,
            "code": row.code,
            "enabled": row.enabled,
            "screenshot_mode": row.screenshot_mode,
        }


@app.post("/api/collectors/run")
async def run_collectors(payload: RunInput, request: Request) -> dict[str, Any]:
    try:
        run = await _dispatcher(request).begin_manual_run(payload.schedule_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": _run_json(run)}


@app.get("/api/reports")
async def list_reports(
    work_date: date | None = None, page: int = 1, page_size: int = 20,
    date_start: date | None = None, date_end: date | None = None,
) -> dict[str, Any]:
    left, right = date_start or date_end or work_date, date_end or date_start or work_date
    if left and right and left > right:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期。")
    current_page = max(1, page)
    per_page = max(1, min(100, page_size))
    with _db() as db:
        statement = select(ShiftReport).order_by(
            ShiftReport.work_date.desc(), ShiftReport.start_time
        )
        count_statement = select(func.count(ShiftReport.id))
        if left and right:
            statement = statement.where(ShiftReport.work_date.between(left, right))
            count_statement = count_statement.where(ShiftReport.work_date.between(left, right))
        total = db.scalar(count_statement) or 0
        rows = db.scalars(statement.offset((current_page - 1) * per_page).limit(per_page)).all()
        counts = dict(db.execute(select(ShiftHourDetail.schedule_id, func.count(ShiftHourDetail.id))
            .where(ShiftHourDetail.schedule_id.in_([row.schedule_id for row in rows]))
            .group_by(ShiftHourDetail.schedule_id)).all())
        return {
            "items": [{**_report_json(row), "hour_count": counts.get(row.schedule_id, 0)} for row in rows],
            "total": total,
            "page": current_page,
            "page_size": per_page,
        }


@app.get("/api/reports/{report_id}")
async def get_report(report_id: int) -> dict[str, Any]:
    with _db() as db:
        row = db.get(ShiftReport, report_id)
        if row is None:
            raise HTTPException(status_code=404, detail="报表不存在。")
        return {"item": _report_json(row, include_raw=True)}


@app.get("/api/reports/{report_id}/hours")
async def report_hours(report_id: int) -> dict[str, Any]:
    with _db() as db:
        parent = db.get(ShiftReport, report_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="主播汇总记录不存在。")
        rows = db.scalars(select(ShiftHourDetail).where(ShiftHourDetail.schedule_id == parent.schedule_id)
                          .order_by(ShiftHourDetail.period_index)).all()
        state = db.get(ShiftHourState, parent.schedule_id)
        return {"items": [_hour_json(row) for row in rows],
                "initial_baseline": state.initial_baseline_json if state else None,
                "summary_final": (parent.raw_json or {}).get("is_final", True)}


@app.post("/api/reports/{report_id}/sync-hours")
async def sync_report_hours(report_id: int, request: Request) -> dict[str, Any]:
    with _db() as db:
        parent = db.get(ShiftReport, report_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="主播汇总记录不存在。")
        schedule_id = parent.schedule_id
    return {"run": _run_json(await _dispatcher(request).begin_hourly_sync(schedule_id))}


@app.get("/api/runs")
async def list_runs(
    page: int = 1, page_size: int | None = None, limit: int | None = None,
    start_at: datetime | None = None, end_at: datetime | None = None,
    time_field: str = "task", kinds: str = "", statuses: str = "",
) -> dict[str, Any]:
    from liveops.sessions import local_datetime

    known_kinds = {"report", "baseline", "interruption", "shift_hour", "hourly_sync", "bitable_sync",
                   "hourly_screenshot", "hourly_watch", "screenshot_delivery",
                   "hourly_screenshot_plan", "anchor_screenshot_plan"}
    known_statuses = {"queued", "running", "succeeded", "partial", "failed", "missed", "skipped"}
    selected_kinds = set(filter(None, kinds.split(",")))
    selected_statuses = set(filter(None, statuses.split(",")))
    if time_field not in {"task", "finished"} or selected_kinds - known_kinds or selected_statuses - known_statuses:
        raise HTTPException(status_code=422, detail="任务时间、任务类型或状态筛选项无效。")
    if start_at and end_at and local_datetime(start_at) > local_datetime(end_at):
        raise HTTPException(status_code=422, detail="开始时间不能晚于结束时间。")
    current_page = max(1, page)
    requested_size = page_size if page_size is not None else limit if limit is not None else 20
    per_page = max(1, min(500, requested_size))
    with _db() as db:
        items, total = _run_history_page(db, (current_page - 1) * per_page, per_page,
            start_at=start_at, end_at=end_at, time_field=time_field, kinds=selected_kinds, statuses=selected_statuses)
        return {
            "items": items,
            "total": total,
            "page": current_page,
            "page_size": per_page,
        }


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    with _db() as db:
        if run_id.startswith("screenshot-request:"):
            plan = db.get(ScreenshotRequest, run_id.removeprefix("screenshot-request:"))
            if (plan is None or plan.status not in {"missed", "skipped", "failed"}
                    or (plan.run_id and db.get(CollectionRun, plan.run_id) is not None)):
                raise HTTPException(status_code=404, detail="截图计划记录不存在。")
            return {"item": _screenshot_history_json(plan)}
        row = db.get(CollectionRun, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="运行记录不存在。")
        return {"item": _run_json(row)}


@app.get("/api/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    with _db() as db:
        return {"settings": _store(request).public(db), "secret_keys": sorted(SECRET_KEYS)}


@app.get("/api/automation/status")
async def automation_status(request: Request) -> dict[str, Any]:
    from liveops.hourly import shift_periods
    from liveops.sessions import local_datetime

    with _db() as db:
        values = db_settings(db, _store(request))
        now = now_local()
        schedules = db.scalars(select(ShiftSchedule).where(ShiftSchedule.enabled.is_(True),
            ShiftSchedule.work_date >= now.date() - timedelta(days=1), ShiftSchedule.work_date <= now.date())).all()
        upcoming = sorted((right, left, row, index) for row in schedules
                          for index, (left, right) in enumerate(shift_periods(row), 1) if right > now)
        next_at, left, schedule, index = upcoming[0] if upcoming else (None, None, None, None)
        recent = db.scalars(select(ScreenshotRequest).order_by(ScreenshotRequest.created_at.desc()).limit(10)).all()
        return {"hourly_enabled": values["hourly_screenshot_enabled"] == "true",
                "room_id": schedule.live_room_id if schedule else "",
                "next_hourly_at": next_at.isoformat() if next_at else None,
                "next_host_name": schedule.host_name if schedule else "",
                "next_period": f"{left:%H:%M}-{next_at:%H:%M}" if upcoming else "",
                "clock_source": "roster", "last_checked_at": now.isoformat(), "observation_failed": False,
                "recent_requests": [{"target": row.target, "planned_at": local_datetime(row.planned_at).isoformat(),
                    "status": row.status, "delivery_status": row.delivery_json.get("status", "pending"),
                    "run_id": row.run_id} for row in recent]}


@app.post("/api/runs/{run_id}/retry-screenshots")
async def retry_screenshots(run_id: str, request: Request) -> dict[str, Any]:
    try:
        run = await _dispatcher(request).begin_screenshot_delivery(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": _run_json(run)}


@app.put("/api/settings")
async def save_settings(payload: SettingsInput, request: Request) -> dict[str, Any]:
    allowed = set(DEFAULT_SETTINGS)
    unknown = set(payload.values) - allowed
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知设置项：{', '.join(sorted(unknown))}")
    if {"bitable_table_month", "bitable_table_mode"}.intersection(payload.values):
        raise HTTPException(status_code=422, detail="月表归属和模式由系统维护；修改 table_id 即可人工指定本月目标表。")
    guard = (_dispatcher(request)._bitable_month_lock
             if {"bitable_app_token", "bitable_table_id", "bitable_hourly_view_id"}.intersection(payload.values)
             else nullcontext())
    async with guard:
        with _db() as db:
            store = _store(request)
            previous_bitable = {key: store.get(db, key) for key in ("bitable_app_token", "bitable_table_id")}
            for key, value in payload.values.items():
                if key in {"bitable_sync_key_field", "bitable_parent_field"} and not value.strip():
                    raise HTTPException(status_code=422, detail="幂等键列名和父记录字段不能为空。")
                if key == "bitable_field_mapping":
                    try:
                        mapping = json.loads(value)
                        if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
                            raise ValueError("invalid mapping")
                    except (ValueError, TypeError) as exc:
                        raise HTTPException(status_code=422, detail="字段映射必须是字段名对应列名的 JSON 对象。") from exc
                if key in BOOLEAN_SETTINGS and value not in {"true", "false"}:
                    raise HTTPException(status_code=422, detail="功能开关只能是 true 或 false。")
                if key == "hourly_message_template":
                    from string import Formatter

                    try:
                        allowed_fields = {"hour_index", "shop_name", "live_room_id", "planned_time", "captured_time",
                                          "host_name", "promoter", "start_time", "end_time", "duration_hours",
                                          "gmv", "overall_amount", "overall_roi"}
                        fields = {name for _, name, _, _ in Formatter().parse(value) if name}
                        if not fields <= allowed_fields:
                            raise ValueError("unknown hourly template field")
                        value.format_map(dict.fromkeys(allowed_fields, 1))
                    except (ValueError, KeyError, IndexError, AttributeError) as exc:
                        raise HTTPException(status_code=422, detail="每小时截图文案的变量或格式无效。") from exc
                if key in SECRET_KEYS and not value.strip():
                    continue
                if key == "browser_mode" and value not in {"cloak_cdp", "native"}:
                    raise HTTPException(
                        status_code=422, detail="浏览器模式只能是 cloak_cdp 或 native。"
                    )
                if key == "native_context_options":
                    try:
                        parsed = json.loads(value)
                        if not isinstance(parsed, dict):
                            raise TypeError("must be object")
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise HTTPException(
                            status_code=422, detail="原生浏览器上下文选项必须是 JSON 对象。"
                        ) from exc
                if key in {"roster_anchor_pool", "roster_promoter_pool"}:
                    try:
                        people = json.loads(value)
                        if (
                            not isinstance(people, list)
                            or len(people) > 100
                            or any(
                                not isinstance(name, str)
                                or not name.strip()
                                or len(name.strip()) > 120
                                or any(ord(char) < 32 for char in name)
                                for name in people
                            )
                            or len({name.strip() for name in people}) != len(people)
                        ):
                            raise ValueError("invalid people list")
                    except (json.JSONDecodeError, ValueError, TypeError) as exc:
                        raise HTTPException(
                            status_code=422, detail="排班人员名单必须是无重复姓名的列表。"
                        ) from exc
                    value = json.dumps([name.strip() for name in people], ensure_ascii=False)
                store.set(db, key, value)
            effective_values = dict(payload.values)
            if ("bitable_app_token" in payload.values and "bitable_table_id" not in payload.values
                    and payload.values["bitable_app_token"] != previous_bitable["bitable_app_token"]):
                store.set(db, "bitable_table_id", "")
                effective_values["bitable_table_id"] = ""
            db.flush()
            remember_manual_table(db, store, effective_values, now_local().date(), previous_bitable)
            db.commit()
            return {"settings": store.public(db), "saved": True}


@app.post("/api/bitable/sync-range")
async def sync_bitable_range(payload: BitableRangeSyncInput, request: Request) -> dict[str, Any]:
    try:
        run = await _dispatcher(request).begin_bitable_range_sync(payload.date_start, payload.date_end, payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": _run_json(run)}


@app.post("/api/integrations/feishu/check")
async def check_feishu(request: Request) -> dict[str, Any]:
    with _db() as db:
        settings = db_settings(db, _store(request))
    client = FeishuClient(
        settings.get("feishu_app_id", ""),
        settings.get("feishu_app_secret", ""),
        settings.get("feishu_chat_id", ""),
    )
    try:
        token = await client.tenant_access_token()
        return {
            "ok": True,
            "token_obtained": bool(token),
            "chat_id_configured": bool(settings.get("feishu_chat_id")),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


@app.post("/api/integrations/feishu/send-test")
async def send_feishu_test(request: Request, target: str = "anchor") -> dict[str, Any]:
    if target not in {"anchor", "hourly"}:
        raise HTTPException(status_code=422, detail="测试目标只能是主播群或业务每小时群。")
    with _db() as db:
        settings = db_settings(db, _store(request))
    client = FeishuClient(
        settings.get("feishu_app_id", ""),
        settings.get("feishu_app_secret", ""),
        screenshot_chat(settings, target),
    )
    try:
        return {"ok": True, **await client.send_test_message()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


@app.post("/api/integrations/bitable/check")
async def check_bitable(request: Request) -> dict[str, Any]:
    with _db() as db:
        settings = db_settings(db, _store(request))
    client = BitableClient(
        settings.get("feishu_app_id", ""),
        settings.get("feishu_app_secret", ""),
        settings.get("bitable_app_token", ""),
        settings.get("bitable_table_id", ""),
    )
    try:
        return {"ok": True, **await client.check_access()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


@app.get("/api/bitable/fields")
async def bitable_fields(request: Request) -> dict[str, Any]:
    with _db() as db:
        settings = db_settings(db, _store(request))
    client = BitableClient(
        settings.get("feishu_app_id", ""),
        settings.get("feishu_app_secret", ""),
        settings.get("bitable_app_token", ""),
        settings.get("bitable_table_id", ""),
    )
    try:
        return {"items": await client.list_fields()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


@app.get("/api/report-columns")
async def report_columns() -> dict[str, Any]:
    return {
        "items": [{"key": key, "label": label, "kind": kind} for key, label, kind in REPORT_COLUMNS]
    }


@app.get("/artifacts/{file_path:path}")
async def read_artifact(file_path: str) -> FileResponse:
    root = Path(os.getenv("ARTIFACT_DIR", "var/artifacts")).resolve()
    candidate = (root / file_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="路径越界。") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="截图不存在。")
    return FileResponse(candidate)


@app.exception_handler(Exception)
async def unhandled_exception(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc) or "服务器内部错误。"}, status_code=500)
