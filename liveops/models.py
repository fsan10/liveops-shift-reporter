from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from liveops.db import Base


class ShiftSchedule(Base):
    __tablename__ = "shift_schedules"
    __table_args__ = (
        UniqueConstraint("work_date", "start_time", "end_time", name="uq_shift_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    host_name: Mapped[str] = mapped_column(String(120))
    promoter: Mapped[str] = mapped_column(String(120), default="")
    ops: Mapped[str] = mapped_column(String(120), default="")
    shop_name: Mapped[str] = mapped_column(String(160), default="")
    live_room_id: Mapped[str] = mapped_column(String(40), index=True)
    qianchuan_account_id: Mapped[str] = mapped_column(String(40), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    message_template: Mapped[str] = mapped_column(Text, default="")
    baseline_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    baseline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_report_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    runs: Mapped[list["CollectionRun"]] = relationship(
        back_populates="schedule", foreign_keys="CollectionRun.schedule_id"
    )


class ScheduleDay(Base):
    __tablename__ = "schedule_days"

    work_date: Mapped[date] = mapped_column(Date, primary_key=True)
    rest: Mapped[str] = mapped_column(Text, default="")
    shoot: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class CollectorModule(Base):
    __tablename__ = "collector_modules"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    page_kind: Mapped[str] = mapped_column(String(48))
    url_template: Mapped[str] = mapped_column(Text)
    screenshot_mode: Mapped[str] = mapped_column(String(24), default="full_page")
    selector: Mapped[str] = mapped_column(Text, default="")
    section_label: Mapped[str] = mapped_column(String(80), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    required_for_data: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("shift_schedules.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(24), index=True)  # baseline, report, manual
    trigger: Mapped[str] = mapped_column(String(24))  # scheduler, user
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    schedule: Mapped[ShiftSchedule | None] = relationship(
        back_populates="runs", foreign_keys=[schedule_id]
    )


class ShiftReport(Base):
    __tablename__ = "shift_reports"
    __table_args__ = (UniqueConstraint("schedule_id", name="uq_report_schedule"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("shift_schedules.id"), index=True)
    latest_run_id: Mapped[str] = mapped_column(ForeignKey("collection_runs.id"))
    work_date: Mapped[date] = mapped_column(Date, index=True)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    host_name: Mapped[str] = mapped_column(String(120))
    promoter: Mapped[str] = mapped_column(String(120), default="")
    shop_name: Mapped[str] = mapped_column(String(160))
    live_room_id: Mapped[str] = mapped_column(String(40))
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    screenshots_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    feishu_status: Mapped[str] = mapped_column(Text, default="not_configured")
    bitable_status: Mapped[str] = mapped_column(Text, default="not_configured")
    bitable_record_id: Mapped[str] = mapped_column(String(100), default="")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class StoredSetting(Base):
    __tablename__ = "stored_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ShiftInterruption(Base):
    __tablename__ = "shift_interruptions"
    __table_args__ = (UniqueConstraint("schedule_id", "live_room_id", name="uq_interruption_room"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("shift_schedules.id"), index=True)
    live_room_id: Mapped[str] = mapped_column(String(40))
    latest_run_id: Mapped[str] = mapped_column(ForeignKey("collection_runs.id"))
    interrupted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    capture_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class HourlyWatch(Base):
    """Only room identity and the hourly clock; never metric/baseline snapshots."""

    __tablename__ = "hourly_watches"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    room_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    next_hour: Mapped[int] = mapped_column(Integer, default=1)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScreenshotRequest(Base):
    """One intended screenshot delivery, optionally sharing another request's capture."""

    __tablename__ = "screenshot_requests"

    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    target: Mapped[str] = mapped_column(String(16))  # anchor, hourly
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("shift_schedules.id"), nullable=True, index=True)
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    room_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    account_id: Mapped[str] = mapped_column(String(40), default="")
    hour_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("collection_runs.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    screenshots_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    delivery_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
