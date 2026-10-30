from __future__ import annotations

import base64
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
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
from liveops.portal import schedule_end_datetime, shift_datetime
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
    work_date: date | None = None, page: int = 1, page_size: int = 20
) -> dict[str, Any]:
    current_page = max(1, page)
    per_page = max(1, min(100, page_size))
    with _db() as db:
        statement = select(ShiftReport).order_by(
            ShiftReport.work_date.desc(), ShiftReport.start_time
        )
        count_statement = select(func.count(ShiftReport.id))
        if work_date:
            statement = statement.where(ShiftReport.work_date == work_date)
            count_statement = count_statement.where(ShiftReport.work_date == work_date)
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
    page: int = 1, page_size: int | None = None, limit: int | None = None
) -> dict[str, Any]:
    current_page = max(1, page)
    requested_size = page_size if page_size is not None else limit if limit is not None else 20
    per_page = max(1, min(500, requested_size))
    with _db() as db:
        total = db.scalar(select(func.count(CollectionRun.id))) or 0
        rows = db.scalars(
            select(CollectionRun)
            .order_by(CollectionRun.created_at.desc())
            .offset((current_page - 1) * per_page)
            .limit(per_page)
        ).all()
        return {
            "items": [_run_json(row) for row in rows],
            "total": total,
            "page": current_page,
            "page_size": per_page,
        }


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    with _db() as db:
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
    with _db() as db:
        store = _store(request)
        for key, value in payload.values.items():
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
        db.commit()
        return {"settings": store.public(db), "saved": True}


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
