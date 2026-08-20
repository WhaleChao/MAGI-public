# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import os
import time
from io import BytesIO
from pathlib import Path

import fitz
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_login import LoginManager


@pytest.fixture(autouse=True)
def _calendar_source_root(tmp_path, monkeypatch):
    """Keep PDF calendar target fixtures within the explicit scanner corpus."""
    monkeypatch.setenv("OSC_PDF_CALENDAR_ALLOWED_ROOTS", str(tmp_path))


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


def test_pdf_calendar_scan_text_uses_roc_case_year_when_filename_has_no_received_date(client, tmp_path, monkeypatch):
    path = tmp_path / "花蓮地方法院115年度司消債調字第69號民事庭通知書.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()
    old = time.mktime((2025, 5, 1, 9, 0, 0, 0, 0, -1))
    os.utime(path, (old, old))

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", lambda *a, **k: (None if k.get("fetch") == "one" else [], {}))
    monkeypatch.setattr("api.blueprints.osc_pdf._pdf_text", lambda *a, **k: "本院定於6月1日下午3時50分調解。")
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2025-0127", "client_name": "曾昌義", "write": False},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["todo_count"] == 1
    todo = body["items"][0]["todos"][0]
    assert todo["type"] == "調解"
    assert todo["date"] == "2026-06-01"
    assert todo["time"] == "15:50"


def test_pdf_calendar_scan_text_uses_legacy_case_folder_year_when_text_has_no_year(client, tmp_path, monkeypatch):
    case_dir = (
        tmp_path
        / "01_案件"
        / "法扶案件"
        / "消費者債務清理"
        / "2025-0049-林洋宇-消費者債務清理-更生"
        / "09_法院通知與程序裁定"
    )
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / "法院通知書.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", lambda *a, **k: (None if k.get("fetch") == "one" else [], {}))
    monkeypatch.setattr("api.blueprints.osc_pdf._pdf_text", lambda *a, **k: "本院定於6月17日下午2時調解。")
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2025-0049", "client_name": "林洋宇", "write": False},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["todo_count"] == 1
    todo = body["items"][0]["todos"][0]
    assert todo["type"] == "調解"
    assert todo["date"] == "2025-06-17"
    assert todo["time"] == "14:00"


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


def test_pdf_calendar_scan_dedupes_response_deadline_type_drift(tmp_path):
    from api.blueprints import osc_pdf

    items = osc_pdf._dedupe_todos(
        [
            {
                "type": "提出資料",
                "date": "2026-06-08",
                "time": "",
                "description": "📝 PDF 擷取：如有異議，10日內提出資料（基準日 05/29）",
                "source": "pdf_text",
            },
            {
                "type": "陳報",
                "date": "2026-06-08",
                "time": "",
                "description": "📝 10日內陳報 (05/29文到)",
                "source": "filename",
            },
        ]
    )

    assert len(items) == 1
    assert items[0]["type"] == "陳報"


def test_pdf_calendar_scan_keeps_distinct_payment_and_correction_same_day(tmp_path):
    from api.blueprints import osc_pdf

    items = osc_pdf._dedupe_todos(
        [
            {
                "type": "補正",
                "date": "2026-06-08",
                "time": "",
                "description": "補正委任狀",
                "source": "pdf_text",
            },
            {
                "type": "繳費",
                "date": "2026-06-08",
                "time": "",
                "description": "繳納裁判費",
                "source": "pdf_text",
            },
        ]
    )

    assert len(items) == 2
    assert {item["type"] for item in items} == {"補正", "繳費"}


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


def test_pdf_calendar_ocr_timeout_logs_without_traceback(monkeypatch, caplog):
    from api.blueprints import osc_pdf

    class Pix:
        def tobytes(self, _format):
            return b"image"

    class Page:
        def get_pixmap(self, **_kwargs):
            return Pix()

    def engine(_image_bytes):
        try:
            raise TimeoutError("pdf_scan_timeout:8s")
        except TimeoutError as exc:
            raise RuntimeError("ONNXRuntime inference failed") from exc

    monkeypatch.setattr(osc_pdf, "_get_rapid_ocr_engine", lambda: engine)

    with caplog.at_level("WARNING"):
        assert osc_pdf._ocr_pdf_page(Page()) == ""

    records = [r for r in caplog.records if "pdf calendar OCR fallback" in r.message]
    assert records
    assert records[-1].exc_info is None
    assert "timed out" in records[-1].message


def test_pdf_calendar_rapidocr_worker_timeout_logs_without_traceback(monkeypatch, caplog):
    import subprocess

    from api.blueprints import osc_pdf

    class Pix:
        def tobytes(self, _format):
            return b"image"

    class Page:
        def get_pixmap(self, **_kwargs):
            return Pix()

    def engine(_image_bytes):
        raise subprocess.TimeoutExpired(["python", "-m", "skills.engine.ocr.rapidocr_worker"], 30)

    monkeypatch.setattr(osc_pdf, "_get_rapid_ocr_engine", lambda: engine)

    with caplog.at_level("WARNING"):
        assert osc_pdf._ocr_pdf_page(Page()) == ""

    records = [r for r in caplog.records if "pdf calendar OCR fallback" in r.message]
    assert records
    assert records[-1].exc_info is None
    assert "timed out" in records[-1].message


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
    monkeypatch.setattr(
        "api.blueprints.osc_pdf._create_calendar_share_link",
        lambda p: {"ok": False, "error": "share_public_base_required"},
    )
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
    assert writer_calls[0]["source_file"] == str(path)
    assert "來源PDF：" in writer_calls[0]["todos"][0]["description"]
    assert "MAGI分享狀態：分享連結暫不可用" in writer_calls[0]["todos"][0]["description"]
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
        },
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["items"][0]["share_link"]["url"] == "https://share.example/s/token"
    assert writer_calls
    assert "MAGI分享連結：https://share.example/s/token" in writer_calls[0]["todos"][0]["description"]


def test_calendar_share_link_replaces_stale_metadata():
    from api.blueprints.osc_pdf import _append_calendar_source_reference

    desc = "\n".join(
        [
            "⚖️ 6月17日 下午2時00分 調解",
            "MAGI分享連結：https://old.example/s/old",
            "連結有效至：2026-06-01T00:00:00",
            "來源PDF：/old/path.pdf",
            "MAGI分享狀態：分享連結暫不可用（old_error）",
        ]
    )
    updated = _append_calendar_source_reference(
        desc,
        source_path=Path("/tmp/new.pdf"),
        share={"ok": True, "url": "https://new.example/s/new", "expires_at": "2026-07-01T00:00:00"},
    )

    assert "⚖️ 6月17日 下午2時00分 調解" in updated
    assert "https://new.example/s/new" in updated
    assert "2026-07-01T00:00:00" in updated
    assert "old.example" not in updated
    assert "來源PDF：" not in updated
    assert "MAGI分享狀態：" not in updated


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


def test_all_case_pdf_targets_discovers_numbered_notice_folder_semantically(
    tmp_path, monkeypatch
):
    from api.blueprints import osc_pdf

    case_dir = (
        tmp_path
        / "01_案件"
        / "一般案件"
        / "刑事"
        / "2026-0062-游瑩詰-偵查-洗錢防制法"
    )
    notice_dir = case_dir / "07_法院通知或程序裁定"
    notice_dir.mkdir(parents=True)
    pdf = notice_dir / (
        "20260729 新北地方檢察署115年度偵字第39543號通知"
        "（游瑩詰；訂8月10日下午3時40分開庭）.pdf"
    )
    pdf.write_bytes(b"%PDF-1.4\n")

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM case_todos" in sql or "FROM document_index" in sql:
            return [], {}
        if "FROM cases" in sql:
            return [
                {
                    "case_number": "2026-0062",
                    "client_name": "游瑩詰",
                    "folder_path": str(case_dir),
                    "status": "進行中",
                }
            ], {}
        return [], {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr(
        "api.case_path_mapper.local_case_path_candidates",
        lambda p: [str(case_dir)],
    )

    targets = osc_pdf._iter_all_case_pdf_targets(
        limit=10,
        filename_only=True,
    )

    assert targets == [(pdf.resolve(), "2026-0062", "游瑩詰")]
    assert osc_pdf._is_pdf_calendar_source_dir_name(notice_dir.name)


def test_all_case_pdf_targets_discovers_final_documents_across_folder_schemas(
    tmp_path, monkeypatch
):
    from api.blueprints import osc_pdf

    fixtures = [
        ("一般案件", "民事", "2026-0101", "甲", "08_法院裁判"),
        ("法扶案件", "刑事", "2026-0102", "乙", "10_不起訴處分書"),
        ("指定辯護案件", "刑事", "2026-0103", "丙", "法院資料/法院裁定"),
        ("無償案件", "行政", "2026-0104", "丁", "09_終局文件"),
    ]
    rows = []
    expected = set()
    for category, case_type, case_number, client_name, relative_folder in fixtures:
        case_dir = tmp_path / "01_案件" / category / case_type / f"{case_number}-{client_name}-測試"
        final_dir = case_dir / relative_folder
        final_dir.mkdir(parents=True)
        pdf = final_dir / f"20260730 {case_number} 判決或裁定.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        rows.append(
            {
                "case_number": case_number,
                "client_name": client_name,
                "folder_path": str(case_dir),
                "status": "進行中",
            }
        )
        expected.add((pdf.resolve(), case_number, client_name))

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM case_todos" in sql or "FROM document_index" in sql:
            return [], {}
        if "FROM cases" in sql:
            return rows, {}
        return [], {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr(
        "api.case_path_mapper.local_case_path_candidates",
        lambda p: [p],
    )

    targets = set(
        osc_pdf._iter_all_case_pdf_targets(
            limit=20,
            case_batch=20,
            filename_only=True,
        )
    )

    assert expected <= targets


def test_all_case_pdf_targets_uses_document_index_when_case_number_missing(tmp_path, monkeypatch):
    from api.blueprints import osc_pdf

    case_dir = tmp_path / "01_案件" / "法扶案件" / "刑事" / "2026-0059-吳志炳-一審-公共危險"
    notice_dir = case_dir / "01_法扶資料" / "專員來信"
    notice_dir.mkdir(parents=True)
    pdf = notice_dir / "(有紙本)吳志炳 114原交易49-1150731下午1530開庭.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM case_todos" in sql:
            return [], {}
        if "FROM document_index" in sql:
            return [
                {
                    "case_number": None,
                    "file_path": str(pdf),
                    "file_name": pdf.name,
                    "party": "吳志炳",
                    "subfolder_name": "專員來信",
                    "modified_date": "2026-06-04 08:00:00",
                    "id": 1,
                }
            ], {}
        if "FROM cases" in sql:
            return [
                {
                    "case_number": "2026-0059",
                    "client_name": "吳志炳",
                    "folder_path": str(case_dir),
                    "status": "進行中",
                }
            ], {}
        return [], {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr("api.case_path_mapper.local_case_path_candidates", lambda p: [str(case_dir)])

    targets = osc_pdf._iter_all_case_pdf_targets(limit=10, filename_only=True)

    assert targets == [(pdf, "2026-0059", "吳志炳")]


def test_all_case_pdf_targets_walks_laf_staff_court_mail_without_index(tmp_path, monkeypatch):
    from api.blueprints import osc_pdf

    case_dir = tmp_path / "01_案件" / "法扶案件" / "刑事" / "2026-0038-陳建華-偵查-傷害"
    staff_dir = case_dir / "01_法扶資料" / "專員來信"
    staff_dir.mkdir(parents=True)
    court_pdf = staff_dir / "20260514 臺東地方檢察署115年度偵字第9號開庭通知（陳建華；訂115年5月27日早上10時40分開庭）.pdf"
    court_pdf.write_bytes(b"%PDF-1.4\n")
    legal_aid_pdf = staff_dir / "扶助律師接案通知書_1150512-J-005_1150514.pdf"
    legal_aid_pdf.write_bytes(b"%PDF-1.4\n")

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM case_todos" in sql:
            return [], {}
        if "FROM document_index" in sql:
            return [], {}
        if "FROM cases" in sql:
            return [
                {
                    "case_number": "2026-0038",
                    "client_name": "陳建華",
                    "folder_path": str(case_dir),
                    "status": "進行中",
                }
            ], {}
        return [], {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr("api.case_path_mapper.local_case_path_candidates", lambda p: [str(case_dir)])

    targets = osc_pdf._iter_all_case_pdf_targets(limit=10)

    assert targets == [(court_pdf.resolve(), "2026-0038", "陳建華")]
    assert osc_pdf._is_pdf_calendar_candidate_path(court_pdf)
    assert not osc_pdf._is_pdf_calendar_candidate_path(legal_aid_pdf)


def test_pdf_calendar_scan_detects_compact_roc_datetime_filename(client, tmp_path, monkeypatch):
    path = tmp_path / "(有紙本)吳志炳 114原交易49-1150731下午1530開庭.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", lambda *a, **k: (None if k.get("fetch") == "one" else [], {}))
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2026-0059", "client_name": "吳志炳", "write": False},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["todo_count"] == 1
    todo = body["items"][0]["todos"][0]
    assert todo["type"] == "開庭"
    assert todo["date"] == "2026-07-31"
    assert todo["time"] == "15:30"


def test_all_case_pdf_targets_recent_sweep_reaches_fresh_pdf_outside_cursor_batch(tmp_path, monkeypatch):
    from api.blueprints import osc_pdf

    old_case_dir = tmp_path / "old" / "2026-0001-舊案"
    old_notice_dir = old_case_dir / "02_法院通知與程序裁定"
    old_notice_dir.mkdir(parents=True)
    old_pdf = old_notice_dir / "20260501 舊通知.pdf"
    old_pdf.write_bytes(b"%PDF-1.4\n")

    fresh_case_dir = tmp_path / "fresh" / "2026-0002-新案"
    fresh_notice_dir = fresh_case_dir / "02_法院通知與程序裁定"
    fresh_notice_dir.mkdir(parents=True)
    fresh_pdf = fresh_notice_dir / "20260528 新通知（訂115年6月9日上午10時開庭）.pdf"
    fresh_pdf.write_bytes(b"%PDF-1.4\n")

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM case_todos" in sql:
            return [], {}
        if "LIMIT %s OFFSET %s" in sql:
            return [
                {
                    "case_number": "2026-0001",
                    "client_name": "舊案",
                    "folder_path": str(old_case_dir),
                }
            ], {}
        if "FROM cases" in sql:
            return [
                {
                    "case_number": "2026-0002",
                    "client_name": "新案",
                    "folder_path": str(fresh_case_dir),
                }
            ], {}
        return [], {}

    monkeypatch.setenv("OSC_PDF_CALENDAR_RECENT_SWEEP_HOURS", "96")
    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr("api.case_path_mapper.local_case_path_candidates", lambda p: [str(p)])

    targets = osc_pdf._iter_all_case_pdf_targets(limit=5, case_offset=40, case_batch=1)

    assert (fresh_pdf.resolve(), "2026-0002", "新案") in targets


def test_all_case_pdf_targets_keeps_closing_prep_statuses_for_final_doc_deadlines(tmp_path, monkeypatch):
    from api.blueprints import osc_pdf

    seen_sql = []

    def fake_exec(sql, params=(), fetch="none"):
        seen_sql.append(sql)
        if "COUNT(*)" in sql:
            return {"count": 0}, {}
        if "FROM cases" in sql:
            return [], {}
        if "FROM case_todos" in sql:
            return [], {}
        return [], {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)

    assert osc_pdf._count_all_case_pdf_case_rows() == 0
    assert osc_pdf._iter_all_case_pdf_targets(limit=5) == []
    joined = "\n".join(seen_sql)
    assert "NOT LIKE '%已結案%'" in joined
    assert "NOT LIKE '%結案中%'" not in joined
    assert "NOT LIKE '%待報結%'" not in joined
    assert "NOT LIKE '%待送出%'" not in joined


def test_all_case_pdf_targets_includes_closing_case_judgment_folder(tmp_path, monkeypatch):
    from api.blueprints import osc_pdf

    case_dir = tmp_path / "01_案件" / "法扶案件" / "刑事" / "2026-0050-林建豐-一審-過失傷害"
    judgment_dir = case_dir / "10_判決書或終局裁定及處分"
    judgment_dir.mkdir(parents=True)
    judgment_pdf = judgment_dir / "20260623 臺東地方法院115年度原交簡字第10號刑事簡易判決（林建豐）.pdf"
    judgment_pdf.write_bytes(b"%PDF-1.4\n")

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM case_todos" in sql:
            return [], {}
        if "FROM document_index" in sql:
            return [], {}
        if "FROM cases" in sql:
            return [
                {
                    "case_number": "2026-0050",
                    "client_name": "林建豐",
                    "folder_path": str(case_dir),
                    "status": "結案中",
                }
            ], {}
        return [], {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr("api.case_path_mapper.local_case_path_candidates", lambda p: [str(case_dir)])

    targets = osc_pdf._iter_all_case_pdf_targets(limit=10)

    assert targets == [(judgment_pdf.resolve(), "2026-0050", "林建豐")]

    filename_targets = osc_pdf._iter_all_case_pdf_targets(limit=10, filename_only=True)

    assert (judgment_pdf.resolve(), "2026-0050", "林建豐") in filename_targets


def test_final_document_priority_scan_does_not_trust_closed_case_status(
    tmp_path, monkeypatch
):
    from api.blueprints import osc_pdf

    case_dir = tmp_path / "01_案件" / "一般案件" / "民事" / "2026-0105-戊-一審-測試"
    judgment_dir = case_dir / "08_法院裁判"
    judgment_dir.mkdir(parents=True)
    judgment_pdf = judgment_dir / "20260731 民事判決（20日內上訴）.pdf"
    judgment_pdf.write_bytes(b"%PDF-1.4\n")
    closed_row = {
        "case_number": "2026-0105",
        "client_name": "戊",
        "folder_path": str(case_dir),
        "status": "已結案",
    }

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM case_todos" in sql or "FROM document_index" in sql:
            return [], {}
        if "FROM cases" in sql:
            if "NOT LIKE '%已結案%'" in sql:
                return [], {}
            return [closed_row], {}
        return [], {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr(
        "api.case_path_mapper.local_case_path_candidates",
        lambda p: [str(case_dir)],
    )

    targets = osc_pdf._iter_all_case_pdf_targets(
        limit=10,
        case_batch=10,
        filename_only=True,
    )

    assert (judgment_pdf.resolve(), "2026-0105", "戊") in targets


def test_final_document_scan_dedupes_known_copy_across_case_numbers(
    tmp_path, monkeypatch
):
    from api.blueprints import osc_pdf

    filename = "士林地院114年度附民字第1289號刑事附帶民事判決_吳倩茹_115.7.27.pdf"
    active_dir = tmp_path / "01_案件" / "法扶案件" / "民事" / "2025-0079-吳倩茹-一審-侵權行為"
    closed_dir = tmp_path / "10_結案" / "法扶案件" / "刑事" / "2026-0012-吳倩茹-一審-洗錢防制法"
    active_pdf = active_dir / "10_判決書或終局裁定及處分" / filename
    closed_pdf = closed_dir / "10_判決書或終局裁定及處分" / filename
    active_pdf.parent.mkdir(parents=True)
    closed_pdf.parent.mkdir(parents=True)
    active_pdf.write_bytes(b"%PDF-same\n")
    closed_pdf.write_bytes(b"%PDF-same\n")
    rows = [
        {
            "case_number": "2025-0079",
            "client_name": "吳倩茹",
            "folder_path": str(active_dir),
            "status": "進行中",
        },
        {
            "case_number": "2026-0012",
            "client_name": "吳倩茹",
            "folder_path": str(closed_dir),
            "status": "已結案",
        },
    ]

    def fake_exec(sql, params=(), fetch="none"):
        if "SELECT case_number, source_file" in sql:
            return [
                {
                    "case_number": "2025-0079",
                    "source_file": str(active_pdf),
                }
            ], {}
        if "FROM document_index" in sql:
            return [], {}
        if "FROM cases" in sql:
            if "NOT LIKE '%已結案%'" in sql:
                return [rows[0]], {}
            return rows, {}
        return [], {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    monkeypatch.setattr(
        "api.case_path_mapper.local_case_path_candidates",
        lambda p: [p],
    )

    targets = osc_pdf._iter_all_case_pdf_targets(
        limit=20,
        case_batch=20,
        filename_only=True,
    )

    assert (active_pdf.resolve(), "2025-0079", "吳倩茹") in targets
    assert (closed_pdf.resolve(), "2026-0012", "吳倩茹") not in targets


def test_pdf_calendar_scan_preview_detects_judgment_folder_appeal_all_day(client, tmp_path, monkeypatch):
    judgment_dir = tmp_path / "2026-0050-林建豐-一審-過失傷害" / "10_判決書或終局裁定及處分"
    judgment_dir.mkdir(parents=True)
    path = judgment_dir / "20260623 臺東地方法院115年度原交簡字第10號刑事簡易判決（林建豐；主文：犯過失傷害罪）.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", lambda *a, **k: (None if k.get("fetch") == "one" else [], {}))
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2026-0050", "client_name": "林建豐", "write": False},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    todo = body["items"][0]["todos"][0]
    assert todo["type"] == "上訴"
    assert todo["date"] == "2026-07-13"
    assert todo["time"] == ""
    event = body["items"][0]["events"][0]
    assert event["is_all_day"] == 1
    assert event["start_date"] == "2026-07-13"
    assert event["end_date"] == "2026-07-14"


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


def test_pdf_text_extracts_shared_time_multiple_dates(tmp_path):
    from api.blueprints import osc_pdf

    path = tmp_path / "20260601 花蓮地方法院通知.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    todos = osc_pdf._extract_todos_from_pdf_text(path, "本院定於6月12日、6月19日下午2時調解。")

    hearings = [row for row in todos if row["type"] == "調解"]
    assert [(row["date"], row["time"]) for row in hearings] == [
        ("2026-06-12", "14:00"),
        ("2026-06-19", "14:00"),
    ]


def test_pdf_text_extracts_compact_roc_datetime(tmp_path):
    from api.blueprints import osc_pdf

    path = tmp_path / "20260701 花蓮地方法院通知.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    todos = osc_pdf._extract_todos_from_pdf_text(path, "本院定於1150731下午1530開庭。")

    assert any(row["type"] == "開庭" and row["date"] == "2026-07-31" and row["time"] == "15:30" for row in todos)


def test_pdf_text_accepts_zhonghua_minguo_absolute_deadline(tmp_path):
    from api.blueprints import osc_pdf

    path = tmp_path / "20260601 花蓮地方法院通知.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    todos = osc_pdf._extract_todos_from_pdf_text(path, "請於中華民國115年6月30日前補正。")

    assert any(row["type"] == "補正" and row["date"] == "2026-06-30" for row in todos)


def test_pdf_text_extracts_challenge_deadlines(tmp_path):
    from api.blueprints import osc_pdf

    path = tmp_path / "20260601 花蓮地方法院裁定.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    todos = osc_pdf._extract_todos_from_pdf_text(path, "如不服本裁定，應於送達後10日內提出異議。")

    assert any(row["type"] == "異議" and row["date"] == "2026-06-11" for row in todos)


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


def test_pdf_calendar_scan_keeps_original_osc_rules_before_tentative_fallback(tmp_path):
    from api.blueprints import osc_pdf

    path = tmp_path / "20260528 臺灣花蓮地方法院通知（王小明；訂115年6月9日上午10時開庭）.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    item = osc_pdf._scan_pdf_for_calendar(
        path,
        case_number="2026-0001",
        client_name="王小明",
        scan_text=False,
    )

    assert item["todos"]
    assert item["todos"][0]["type"] == "開庭"
    assert item["todos"][0]["date"] == "2026-06-09"
    assert item["todos"][0]["time"] == "10:00"
    assert item["todos"][0].get("source") != "pdf_tentative_no_deadline"


def test_pdf_calendar_scan_tentative_14_days_when_court_pdf_has_no_deadline(tmp_path):
    from api.blueprints import osc_pdf

    path = tmp_path / "20260528 臺北高等行政法院開庭方式意願徵詢表（李秀英）.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    fixed_mtime = time.mktime((2026, 5, 28, 9, 0, 0, 0, 0, -1))
    os.utime(path, (fixed_mtime, fixed_mtime))

    item = osc_pdf._scan_pdf_for_calendar(
        path,
        case_number="2026-0045",
        client_name="李秀英",
        scan_text=False,
    )

    assert item["todos"]
    assert item["todos"][0]["type"] == "確認"
    assert item["todos"][0]["date"] == "2026-06-11"
    assert item["todos"][0]["source"] == "pdf_tentative_no_deadline"


def test_pdf_calendar_scan_no_tentative_for_plain_ruling_or_forwarding_letter(tmp_path):
    from api.blueprints import osc_pdf

    ruling = tmp_path / "20260602 花蓮地方法院115年度聲字第169號刑事裁定（劉信義；主文：准許聲請人A女之父、A女之母參與本案訴訟）.pdf"
    forwarding = tmp_path / "20250821 高等法院114年度聲再字第157號刑事庭函（蕭仁俊；主旨：檢送刑事再審抗告理由續狀）.pdf"
    for path in (ruling, forwarding):
        path.write_bytes(b"%PDF-1.4\n")
        item = osc_pdf._scan_pdf_for_calendar(path, case_number="2026-0001", client_name="測試", scan_text=False)
        assert item["todos"] == []


def test_pdf_text_deadline_requires_deadline_cue_for_absolute_dates(tmp_path):
    from api.blueprints import osc_pdf

    path = tmp_path / "20260602 花蓮地方法院刑事裁定（劉信義）.pdf"
    text = "臺灣花蓮地方法院115年度蒞字第001346號 提出資料 115年6月2日 主文准許參與訴訟"

    assert osc_pdf._extract_todos_from_pdf_text(path, text) == []


def test_pdf_text_uses_ocr_fallback_when_native_text_empty(tmp_path, monkeypatch):
    from api.blueprints import osc_pdf

    path = tmp_path / "scan.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()

    monkeypatch.setattr(osc_pdf, "_ocr_pdf_page", lambda page: "OCR 補正內容")

    text = osc_pdf._pdf_text(path, max_pages=1)

    assert "OCR 補正內容" in text


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
