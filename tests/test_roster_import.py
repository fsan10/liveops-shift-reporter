from datetime import date, datetime, time
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import delete, func, select

import liveops.app as app_module
from liveops.db import SessionLocal
from liveops.models import ScheduleDay, ShiftSchedule
from liveops.roster_import import HEADERS, MAX_FILE_BYTES, read_roster


@pytest.fixture(autouse=True)
def roster_import_dates(client, monkeypatch):
    monkeypatch.setattr(app_module, "now_local", lambda: datetime(2042, 11, 9, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai")))
    yield
    with SessionLocal() as db:
        for model in (ShiftSchedule, ScheduleDay):
            db.execute(delete(model).where(model.work_date.between(date(2042, 11, 1), date(2042, 11, 30))))
        db.commit()


def excel(rows, headers=HEADERS):
    workbook = Workbook()
    workbook.active.append(headers)
    for row in rows:
        workbook.active.append(row)
    content = BytesIO()
    workbook.save(content)
    workbook.close()
    return content.getvalue()


def upload(client, content, preview=True, filename="roster.xlsx"):
    return client.post(f"/api/schedules/import?preview={str(preview).lower()}", files={"file": (filename, content)})


def stored():
    with SessionLocal() as db:
        shifts = db.scalar(select(func.count()).select_from(ShiftSchedule).where(ShiftSchedule.work_date.between(date(2042, 11, 1), date(2042, 11, 30))))
        notes = db.scalar(select(func.count()).select_from(ScheduleDay).where(ScheduleDay.work_date.between(date(2042, 11, 1), date(2042, 11, 30))))
        return shifts, notes


def test_preview_is_read_only_commit_is_idempotent_and_preserves_notes(client):
    content = excel([
        ["", date(2042, 11, 8), "07:00-10:00", "历史示例", "", "不应写入", ""],
        ["", date(2042, 11, 9), "07:00-10:00", "示例A", "投手A", "示例休息", "示例拍摄"],
        ["", None, "10:00-14:00", "示例B", "投手B", "", ""],
        ["", date(2042, 11, 10), "", "", "", "仅备注", ""],
    ])
    preview = upload(client, content)
    assert preview.status_code == 200
    assert preview.json()["counts"] == {"new": 3, "duplicate": 0, "historical": 1, "error": 0}
    assert preview.json()["schedule_count"] == 2
    assert stored() == (0, 0)
    committed = upload(client, content, False)
    assert committed.status_code == 200 and committed.json()["imported"]
    assert stored() == (2, 2)
    retry = upload(client, content, False).json()
    assert retry["counts"] == {"new": 0, "duplicate": 3, "historical": 1, "error": 0}
    assert stored() == (2, 2)
    with SessionLocal() as db:
        row = db.scalar(select(ShiftSchedule).where(ShiftSchedule.work_date == date(2042, 11, 9)))
        row.enabled = False
        row.live_room_id = "preserve-bound-room"
        db.commit()
    assert upload(client, content, False).json()["schedule_count"] == 0
    with SessionLocal() as db:
        row = db.scalar(select(ShiftSchedule).where(ShiftSchedule.host_name == "示例A"))
        assert row.enabled is False and row.live_room_id == "preserve-bound-room"


@pytest.mark.parametrize("bad_row", [
    ["", "2042-11-09", "09:00-12:00", "冲突主播", "", "", ""],
    ["", "2042-11-09", "12:00-14:00", "示例A", "", "", ""],
    ["", "2042-11-09", "12:00-12:00", "示例C", "", "", ""],
    ["", "2042-11-09", "无效时段", "示例C", "", "", ""],
    ["", "2042-11-09", "12:00-14:00", "=A1", "", "", ""],
    ["", "日期错误", "12:00-14:00", "示例C", "", "", ""],
], ids=["overlap", "duplicate-host", "equal-times", "bad-range", "formula", "bad-date"])
def test_bad_row_prevents_entire_batch(client, bad_row):
    content = excel([["", "2042-11-09", "07:00-10:00", "示例A", "", "备注", ""], bad_row])
    response = upload(client, content, False)
    assert response.status_code == 409
    assert response.json()["valid"] is False
    assert response.json()["rows"][-1]["row"] == 3
    assert response.json()["rows"][-1]["status"] == "error"
    assert stored() == (0, 0)


def test_midnight_overlap_across_dates_and_existing_rows(client):
    with SessionLocal() as db:
        db.add(ShiftSchedule(work_date=date(2042, 11, 9), start_time=time(22), end_time=time(2), host_name="已有主播", live_room_id="", enabled=False))
        db.commit()
    content = excel([["", "2042-11-10", "01:00-04:00", "次日主播", "", "", ""]])
    assert upload(client, content, False).status_code == 409
    assert stored() == (1, 0)
    content = excel([["", "2042-11-10", "02:00-04:00", "次日主播", "", "", ""]])
    assert upload(client, content, False).status_code == 200


def test_revalidates_between_preview_and_commit(client):
    content = excel([["", "2042-11-09", "07:00-10:00", "示例A", "", "", ""]])
    assert upload(client, content).json()["valid"]
    with SessionLocal() as db:
        db.add(ShiftSchedule(work_date=date(2042, 11, 9), start_time=time(8), end_time=time(11), host_name="其他修改", live_room_id="", enabled=False))
        db.commit()
    assert upload(client, content, False).status_code == 409
    assert stored() == (1, 0)


def test_day_note_conflict_never_overwrites_or_partially_imports(client):
    with SessionLocal() as db:
        db.add(ScheduleDay(work_date=date(2042, 11, 9), rest="保留备注", shoot=""))
        db.commit()
    content = excel([["", "2042-11-09", "07:00-10:00", "示例A", "", "其他备注", ""]])
    assert upload(client, content, False).status_code == 409
    assert stored() == (0, 1)
    with SessionLocal() as db:
        assert db.get(ScheduleDay, date(2042, 11, 9)).rest == "保留备注"


def test_historical_rows_skip_validation_and_note_count_uses_distinct_dates(client):
    content = excel([
        ["", "2042-11-08", "无效时段", "历史示例", "", "", ""],
        ["", "2042-11-09", "07:00-10:00", "示例A", "", "休息", ""],
        ["", None, "10:00-14:00", "示例B", "", "", "拍摄"],
    ])
    result = upload(client, content, False).json()
    assert result["imported"] and result["note_count"] == 1
    assert result["counts"]["historical"] == 1
    with SessionLocal() as db:
        note = db.get(ScheduleDay, date(2042, 11, 9))
        assert (note.rest, note.shoot) == ("休息", "拍摄")


def test_split_excel_time_cells_and_duplicate_rows(client):
    content = excel([
        [datetime(2042, 11, 9), time(20, 30), time(1), "示例A", "示例投手"],
        [None, time(20, 30), time(1), "示例A", "示例投手"],
    ], ["日期", "开始时间", "结束时间", "主播", "运营"])
    result = upload(client, content, False).json()
    assert result["schedule_count"] == 1
    assert result["counts"]["duplicate"] == 1


def test_csv_export_format_blank_dates_and_utf8_bom(client):
    content = '\ufeff星期,日期,时段,主播,投手,休息,日常拍摄\r\n,2042/11/9,7:00-10:00,示例A,投手A,,\r\n,,10:00-14:00,示例B,,,'.encode()
    assert upload(client, content, False, "导出.csv").json()["schedule_count"] == 2


@pytest.mark.parametrize("content,filename", [
    (b"invalid", "roster.xlsx"), (b"", "roster.xlsx"), (b"invalid", "roster.xls"),
    (b"a" * (MAX_FILE_BYTES + 1), "roster.xlsx"),
    (excel([], ["wrong"]), "roster.xlsx"),
], ids=["corrupt", "empty", "legacy-xls", "oversized", "bad-headers"])
def test_invalid_upload_is_explained_without_writes(client, content, filename):
    response = upload(client, content, False, filename)
    assert response.status_code == 422
    assert response.json()["detail"]
    assert stored() == (0, 0)


def test_template_has_no_private_data_and_round_trips(client):
    response = client.get("/api/schedules/import-template")
    assert response.status_code == 200
    workbook = load_workbook(BytesIO(response.content))
    assert workbook.sheetnames == ["排班导入", "填写说明"]
    assert [cell.value for cell in workbook.active[1]] == HEADERS
    assert workbook.active["B2"].value.date() == date(2042, 11, 9)
    assert workbook.active.freeze_panes == "A2"
    workbook.close()
    result = upload(client, response.content).json()
    assert result["valid"] and result["schedule_count"] == 2
    assert stored() == (0, 0)


def test_parser_limits_rows_and_reports_invalid_formula():
    with pytest.raises(ValueError, match="最多导入"):
        read_roster(("日期,时段,主播\n" + "2042-11-09,7:00-10:00,示例A\n" * 2001).encode(), "roster.csv")
