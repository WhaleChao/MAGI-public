# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

import fitz
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_login import LoginManager


@pytest.fixture
def app():
    a = Flask(__name__)
    a.config["TESTING"] = True
    a.config["LOGIN_DISABLED"] = True
    a.secret_key = "test_secret"
    LoginManager().init_app(a)
    from api.blueprints.osc_pdf import osc_pdf_bp

    a.register_blueprint(osc_pdf_bp)
    return a


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sample_pdf(tmp_path):
    path = tmp_path / "案件PDF.pdf"
    doc = fitz.open()
    for idx in range(1, 4):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {idx} Paperclip PDF test", fontsize=14)
    doc.save(path)
    doc.close()
    return path


def _assert_pdf(path: str | Path, expected_pages: int | None = None):
    out = Path(path)
    assert out.exists()
    doc = fitz.open(out)
    try:
        if expected_pages is not None:
            assert doc.page_count == expected_pages
    finally:
        doc.close()


def test_pdf_routes_registered(app):
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/osc/pdf/info" in rules
    assert "/api/osc/pdf/action" in rules
    assert "/api/osc/pdf/upload" in rules
    assert "/api/osc/pdf/calendar-scan" in rules


def test_pdf_info(client, sample_pdf):
    r = client.get("/api/osc/pdf/info", query_string={"path": str(sample_pdf)})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["item"]["page_count"] == 3
    assert body["item"]["file_name"] == "案件PDF.pdf"


def test_pdf_rejects_non_pdf(client, tmp_path):
    txt = tmp_path / "note.txt"
    txt.write_text("not pdf", encoding="utf-8")
    r = client.post("/api/osc/pdf/action", json={"action": "info", "file_path": str(txt)})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_pdf_upload(client, sample_pdf):
    r = client.post(
        "/api/osc/pdf/upload",
        data={"file": (BytesIO(sample_pdf.read_bytes()), "upload-test.pdf")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    uploaded = Path(body["path"])
    assert uploaded.exists()
    assert body["item"]["page_count"] == 3


def test_pdf_extract_text(client, sample_pdf):
    r = client.post(
        "/api/osc/pdf/action",
        json={"action": "extract_text", "file_path": str(sample_pdf), "pages": "1,3"},
    )
    assert r.status_code == 200
    output = Path(r.get_json()["outputs"][0])
    assert output.exists()
    text = output.read_text(encoding="utf-8")
    assert "Paperclip PDF test" in text


def test_pdf_rotate_extract_split_merge_watermark_optimize_encrypt(client, sample_pdf, tmp_path):
    rotate = client.post(
        "/api/osc/pdf/action",
        json={"action": "rotate", "file_path": str(sample_pdf), "pages": "1", "angle": 90},
    ).get_json()
    _assert_pdf(rotate["outputs"][0], 3)
    rotated_doc = fitz.open(rotate["outputs"][0])
    try:
        assert rotated_doc[0].rotation == 90
    finally:
        rotated_doc.close()

    extract = client.post(
        "/api/osc/pdf/action",
        json={"action": "extract_pages", "file_path": str(sample_pdf), "pages": "2-3"},
    ).get_json()
    _assert_pdf(extract["outputs"][0], 2)

    split = client.post(
        "/api/osc/pdf/action",
        json={"action": "split_ranges", "file_path": str(sample_pdf), "pages": "1-2,3"},
    ).get_json()
    assert len(split["outputs"]) == 2
    _assert_pdf(split["outputs"][0], 2)
    _assert_pdf(split["outputs"][1], 1)

    other = tmp_path / "其他附件.pdf"
    other_doc = fitz.open()
    other_doc.new_page().insert_text((72, 72), "合併附件")
    other_doc.save(other)
    other_doc.close()
    merged = client.post(
        "/api/osc/pdf/action",
        json={"action": "merge", "file_path": str(sample_pdf), "other_paths": str(other)},
    ).get_json()
    _assert_pdf(merged["outputs"][0], 4)

    watermark = client.post(
        "/api/osc/pdf/action",
        json={"action": "watermark", "file_path": str(sample_pdf), "text": "閱卷用"},
    ).get_json()
    _assert_pdf(watermark["outputs"][0], 3)

    optimized = client.post(
        "/api/osc/pdf/action",
        json={"action": "optimize", "file_path": str(sample_pdf)},
    ).get_json()
    _assert_pdf(optimized["outputs"][0], 3)

    encrypted = client.post(
        "/api/osc/pdf/action",
        json={"action": "encrypt", "file_path": str(sample_pdf), "password": "secret123"},
    ).get_json()
    enc_doc = fitz.open(encrypted["outputs"][0])
    try:
        assert enc_doc.needs_pass
        assert enc_doc.authenticate("secret123") > 0
        assert enc_doc.page_count == 3
    finally:
        enc_doc.close()


def test_pdf_calendar_scan_preview_detects_hearing(client, tmp_path, monkeypatch):
    path = tmp_path / "20260501 6月12日上午10時30分開庭.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "臺灣花蓮地方法院通知 定於民國115年6月12日上午10時30分開庭", fontsize=12)
    doc.save(path)
    doc.close()

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", lambda *a, **k: (None, {}))
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2026-0001", "client_name": "王小明", "write": False},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["todo_count"] >= 1
    todo = body["items"][0]["todos"][0]
    assert todo["type"] == "開庭"
    assert todo["date"] == "2026-06-12"
    assert todo["time"] == "10:30"
    assert body["items"][0]["events"][0]["case_number"] == "2026-0001"


def test_pdf_calendar_scan_preview_detects_filename_when_pdf_has_no_text(client, tmp_path, monkeypatch):
    path = tmp_path / "20260514 臺東地方檢察署115年度偵字第9號開庭通知（陳建華；訂115年5月27日早上10時40分開庭）.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", lambda *a, **k: (None if k.get("fetch") == "one" else [], {}))
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2026-0038", "client_name": "陳建華", "write": False},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["todo_count"] == 1
    todo = body["items"][0]["todos"][0]
    assert todo["type"] == "開庭"
    assert todo["date"] == "2026-05-27"
    assert todo["time"] == "10:40"
    assert body["items"][0]["events"][0]["case_number"] == "2026-0038"


def test_pdf_calendar_scan_preview_detects_all_day_filename_deadline(client, tmp_path, monkeypatch):
    path = tmp_path / "20260326 花蓮地方法院函（宣愛華；請於文到十日內提出資料）.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", lambda *a, **k: (None if k.get("fetch") == "one" else [], {}))
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2025-0067", "client_name": "宣愛華", "write": False},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["todo_count"] == 1
    todo = body["items"][0]["todos"][0]
    assert todo["type"] == "提出資料"
    assert todo["date"] == "2026-04-07"
    assert todo["time"] == ""
    event = body["items"][0]["events"][0]
    assert event["is_all_day"] == 1
    assert event["start_date"] == "2026-04-07"


def test_pdf_calendar_scan_preview_detects_absolute_deadline_in_pdf_text(client, tmp_path, monkeypatch):
    path = tmp_path / "20260520 花蓮地方法院函.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", lambda *a, **k: (None if k.get("fetch") == "one" else [], {}))
    monkeypatch.setattr(
        "api.blueprints.osc_pdf._pdf_text",
        lambda *_args, **_kwargs: "請惠予於民國115年6月3日前表示意見，俾憑辦理。",
    )
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2026-0001", "client_name": "王小明", "write": False},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["todo_count"] == 1
    todo = body["items"][0]["todos"][0]
    assert todo["type"] == "陳報"
    assert todo["date"] == "2026-06-03"
    assert todo["time"] == ""


def test_pdf_calendar_scan_falls_back_to_filename_when_pdf_placeholder_unreadable(client, tmp_path, monkeypatch):
    path = tmp_path / "20260514 臺東地方檢察署115年度偵字第9號開庭通知（陳建華；訂115年5月27日早上10時40分開庭）.pdf"
    path.write_bytes(b"not a real local pdf yet")

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", lambda *a, **k: (None if k.get("fetch") == "one" else [], {}))
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2026-0038", "client_name": "陳建華", "write": False},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    item = body["items"][0]
    assert item["text_available"] is False
    assert item["text_error"]
    assert body["todo_count"] == 1
    todo = item["todos"][0]
    assert todo["type"] == "開庭"
    assert todo["date"] == "2026-05-27"
    assert todo["time"] == "10:40"


def test_pdf_calendar_scan_write_uses_single_machine_todo_writer(client, tmp_path, monkeypatch):
    path = tmp_path / "20260501 裁定（應於10日內補正）.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "本裁定送達後10日內補正", fontsize=12)
    doc.save(path)
    doc.close()

    osc_exec_calls = []
    writer_calls = []

    def fake_exec(sql, params=(), fetch="none"):
        osc_exec_calls.append((sql, params, fetch))
        if fetch == "all":
            return [], {}
        if fetch == "one":
            return None, {}
        return {"lastrowid": 1, "rowcount": 1}, {}

    def fake_writer(todos, *, case_number, client_name, source_file, allow_duplicates):
        writer_calls.append(
            {
                "todos": todos,
                "case_number": case_number,
                "client_name": client_name,
                "source_file": source_file,
                "allow_duplicates": allow_duplicates,
            }
        )
        return {"inserted": len(todos), "updated": 0, "skipped": 0}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr("api.blueprints.osc_pdf._insert_todos_single_machine", fake_writer)
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2026-0002", "client_name": "林小華", "write": True},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["todo_inserted"] >= 1
    assert body["event_inserted"] == 0
    assert writer_calls
    assert writer_calls[0]["case_number"] == "2026-0002"
    assert writer_calls[0]["client_name"] == "林小華"
    assert writer_calls[0]["source_file"] == path.name
    joined_sql = "\n".join(c[0] for c in osc_exec_calls)
    assert "INSERT INTO calendar_events" not in joined_sql


def test_pdf_calendar_scan_write_can_embed_magi_share_link(client, tmp_path, monkeypatch):
    path = tmp_path / "20260501 裁定（應於10日內補正）.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "本裁定送達後10日內補正", fontsize=12)
    doc.save(path)
    doc.close()

    osc_exec_calls = []
    writer_calls = []

    def fake_exec(sql, params=(), fetch="none"):
        osc_exec_calls.append((sql, params, fetch))
        if fetch == "all":
            return [], {}
        if fetch == "one":
            return None, {}
        return {"lastrowid": 1, "rowcount": 1}, {}

    def fake_writer(todos, *, case_number, client_name, source_file, allow_duplicates):
        writer_calls.append(
            {
                "todos": todos,
                "case_number": case_number,
                "client_name": client_name,
                "source_file": source_file,
                "allow_duplicates": allow_duplicates,
            }
        )
        return {"inserted": len(todos), "updated": 0, "skipped": 0}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr("api.blueprints.osc_pdf._insert_todos_single_machine", fake_writer)
    monkeypatch.setattr(
        "api.blueprints.osc_pdf._create_calendar_share_link",
        lambda p: {"ok": True, "url": "https://share.example/s/token", "expires_at": "2026-06-01T00:00:00"},
    )
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={
            "file_path": str(path),
            "case_number": "2026-0002",
            "client_name": "林小華",
            "write": True,
            "include_share_link": True,
        },
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["items"][0]["share_link"]["url"] == "https://share.example/s/token"
    assert writer_calls
    assert "MAGI分享連結：https://share.example/s/token" in writer_calls[0]["todos"][0]["description"]


def test_all_case_pdf_targets_translate_windows_case_path(tmp_path, monkeypatch):
    from api.blueprints import osc_pdf

    case_dir = tmp_path / "01_案件" / "法扶案件" / "消費者債務清理" / "2025-0121-高弘軒-消費者債務清理-更生"
    notice_dir = case_dir / "02_法院通知與程序裁定"
    notice_dir.mkdir(parents=True)
    pdf = notice_dir / "20260513 花蓮地方法院115年度司消債調字第73號民事庭通知書（高弘軒；訂6月1日下午4時行調解程序）.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% fake target listing only\n")

    monkeypatch.setattr(
        "api.blueprints.osc_pdf._osc_exec",
        lambda *a, **k: (
            [
                {
                    "case_number": "2025-0121",
                    "client_name": "高弘軒",
                    "folder_path": r"Z:\lumi63181107\01_案件\法扶案件\消費者債務清理\2025-0121-高弘軒-消費者債務清理-更生",
                }
            ],
            {},
        ),
    )
    monkeypatch.setattr(
        "api.case_path_mapper.local_case_path_candidates",
        lambda p: [str(case_dir)],
    )

    targets = osc_pdf._iter_all_case_pdf_targets(limit=10)

    assert targets == [(pdf.resolve(), "2025-0121", "高弘軒")]


def test_all_case_pdf_targets_prioritizes_unprocessed_recent_pdfs(tmp_path, monkeypatch):
    from api.blueprints import osc_pdf

    case_dir = tmp_path / "01_案件" / "一般案件" / "民事" / "2026-0001-測試-一審-測試"
    notice_dir = case_dir / "02_法院通知與程序裁定"
    notice_dir.mkdir(parents=True)
    processed = notice_dir / "20260501 已處理通知.pdf"
    fresh = notice_dir / "20260520 新通知.pdf"
    processed.write_bytes(b"%PDF-1.4\n")
    fresh.write_bytes(b"%PDF-1.4\n")

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM cases" in sql:
            return [
                {
                    "case_number": "2026-0001",
                    "client_name": "測試",
                    "folder_path": str(case_dir),
                }
            ], {}
        if "FROM case_todos" in sql:
            return [{"case_number": "2026-0001", "source_file": processed.name}], {}
        return [], {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr("api.case_path_mapper.local_case_path_candidates", lambda p: [str(case_dir)])

    targets = osc_pdf._iter_all_case_pdf_targets(limit=1)

    assert targets == [(fresh.resolve(), "2026-0001", "測試")]


def test_all_case_pdf_targets_prioritizes_todo_like_pdf_names(tmp_path, monkeypatch):
    from api.blueprints import osc_pdf
    import os
    import time

    case_dir = tmp_path / "01_案件" / "一般案件" / "民事" / "2026-0001-測試-一審-測試"
    notice_dir = case_dir / "02_法院通知與程序裁定"
    notice_dir.mkdir(parents=True)
    no_todo = notice_dir / "20260525 普通函文.pdf"
    todo_like = notice_dir / "20260520 花蓮地方法院函（請於115年6月3日前表示意見）.pdf"
    no_todo.write_bytes(b"%PDF-1.4\n")
    todo_like.write_bytes(b"%PDF-1.4\n")
    now = time.time()
    os.utime(no_todo, (now, now))
    os.utime(todo_like, (now - 86400, now - 86400))

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM cases" in sql:
            return [
                {
                    "case_number": "2026-0001",
                    "client_name": "測試",
                    "folder_path": str(case_dir),
                }
            ], {}
        if "FROM case_todos" in sql:
            return [], {}
        return [], {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr("api.case_path_mapper.local_case_path_candidates", lambda p: [str(case_dir)])

    targets = osc_pdf._iter_all_case_pdf_targets(limit=1)

    assert targets == [(todo_like.resolve(), "2026-0001", "測試")]


def test_pdf_calendar_scan_skips_text_for_large_pdf_when_filename_has_todo(tmp_path, monkeypatch):
    from api.blueprints import osc_pdf

    path = tmp_path / "20260514 臺東地方檢察署115年度偵字第9號開庭通知（陳建華；訂115年5月27日早上10時40分開庭）.pdf"
    with path.open("wb") as fh:
        fh.truncate(9 * 1024 * 1024)

    def fail_text(*_args, **_kwargs):
        raise AssertionError("_pdf_text should not be called for large filename-sufficient PDFs")

    monkeypatch.setattr(osc_pdf, "_pdf_text", fail_text)
    item = osc_pdf._scan_pdf_for_calendar(path, case_number="2026-0038", client_name="陳建華")

    assert item["todos"]
    assert item["todos"][0]["date"] == "2026-05-27"
    assert item["text_error"] == "skipped_text_filename_todos"


def test_gcal_sync_todo_event_title_includes_client_and_type():
    skill_dir = ROOT / "skills" / "osc-orchestrator"
    sys.path.insert(0, str(skill_dir))
    import gcal_sync  # type: ignore

    event = gcal_sync._make_todo_event(
        {
            "case_number": "2025-0084",
            "client_name": "王臺銘",
            "todo_type": "補正",
            "todo_date": "2026-05-19",
            "todo_time": "09:30",
            "description": "20260505 臺北地方法院裁定，14日內補正",
            "source_file": "/cases/2025-0084/補正.pdf",
        }
    )

    assert event["summary"] == "[2025-0084] 王臺銘 補正"
    assert event["start"]["dateTime"].startswith("2026-05-19T09:30")
    assert "當事人：王臺銘" in event["description"]
    assert "來源檔案：/cases/2025-0084/補正.pdf" in event["description"]
