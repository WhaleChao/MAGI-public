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


def test_pdf_calendar_scan_write_inserts_todo_and_calendar(client, tmp_path, monkeypatch):
    path = tmp_path / "20260501 裁定（應於10日內補正）.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "本裁定送達後10日內補正", fontsize=12)
    doc.save(path)
    doc.close()

    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        if fetch == "all":
            return [], {}
        if fetch == "one":
            return None, {}
        return {"lastrowid": 1, "rowcount": 1}, {}

    monkeypatch.setattr("api.blueprints.osc_pdf._osc_exec", fake_exec)
    r = client.post(
        "/api/osc/pdf/calendar-scan",
        json={"file_path": str(path), "case_number": "2026-0002", "client_name": "林小華", "write": True},
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["todo_inserted"] >= 1
    assert body["event_inserted"] >= 1
    joined_sql = "\n".join(c[0] for c in calls)
    assert "INSERT INTO case_todos" in joined_sql
    assert "INSERT INTO calendar_events" in joined_sql
