# -*- coding: utf-8 -*-
"""Tests for /api/osc/documents/stamp endpoint (P0 蓋章 UI 整合).

不執行真正 PDF 操作（會呼叫 doc-producer subprocess），改 monkey-patch
subprocess.run 模擬 skill 回應，驗證：
  1. 路由註冊正確
  2. payload 驗證（必填、copy_type 白名單、副檔名白名單）
  3. 成功路徑回傳結構
  4. skill 失敗時錯誤往上拋

不測 login_required 包裝（需 server.py 完整 login_manager 設定，超出單元測試範圍）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_login import LoginManager


@pytest.fixture
def app():
    a = Flask(__name__)
    a.config["TESTING"] = True
    a.config["LOGIN_DISABLED"] = True
    a.secret_key = "test"
    lm = LoginManager()
    lm.init_app(a)
    from api.blueprints.osc_cases import osc_bp
    a.register_blueprint(osc_bp)
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def test_stamp_route_registered(app):
    rules = [str(r) for r in app.url_map.iter_rules()]
    assert "/api/osc/documents/stamp" in rules
    assert "/api/osc/documents/stamp-preview" in rules
    assert "/api/osc/documents/finalize" in rules


def test_stamp_validates_missing_file_path(client):
    r = client.post("/api/osc/documents/stamp", json={})
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False
    assert "file_path required" in body["error"]


def test_stamp_validates_copy_type(client):
    r = client.post(
        "/api/osc/documents/stamp",
        json={"file_path": "/tmp/x.pdf", "copy_type": "invalid"},
    )
    assert r.status_code == 400
    body = r.get_json()
    assert "copy_type" in body["error"]


def test_stamp_rejects_nonexistent_file(client):
    r = client.post(
        "/api/osc/documents/stamp",
        json={"file_path": "/nonexistent/path/never_exists.pdf", "copy_type": "正本"},
    )
    assert r.status_code in (404, 403)
    body = r.get_json()
    assert body["ok"] is False


def test_stamp_rejects_unsupported_extension(client, tmp_path, monkeypatch):
    # mock allowed-roots 讓 /tmp 可被接受
    txt = tmp_path / "fake.txt"
    txt.write_text("x")

    def _safe(p):
        return True

    def _candidates(p):
        return [Path(p)] if isinstance(p, str) else [Path(str(p))]

    def _resolve(c):
        for x in c:
            if x.exists():
                return x
        return None

    from api.blueprints import osc_cases
    monkeypatch.setattr(osc_cases, "_osc_is_safe_local_path", _safe)
    monkeypatch.setattr(osc_cases, "_osc_local_path_candidates", _candidates)
    monkeypatch.setattr(osc_cases, "_osc_resolve_existing_local_path", _resolve)

    r = client.post(
        "/api/osc/documents/stamp",
        json={"file_path": str(txt), "copy_type": "正本"},
    )
    assert r.status_code == 400
    body = r.get_json()
    assert "unsupported file type" in body["error"]


def test_stamp_success_pdf(client, tmp_path, monkeypatch):
    """PDF → mark task → 預期 output_path 從 result.output 取出。"""
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    from api.blueprints import osc_cases
    monkeypatch.setattr(osc_cases, "_osc_is_safe_local_path", lambda p: True)
    monkeypatch.setattr(
        osc_cases, "_osc_local_path_candidates", lambda p: [Path(p)]
    )
    monkeypatch.setattr(
        osc_cases, "_osc_resolve_existing_local_path", lambda c: c[0]
    )

    # mock subprocess.run to return successful skill output
    fake_result = {
        "success": True,
        "output": str(tmp_path / "in_正本.pdf"),
        "error": "",
    }

    class FakeProc:
        stdout = json.dumps(fake_result)
        stderr = ""

    with patch("subprocess.run", return_value=FakeProc()):
        r = client.post(
            "/api/osc/documents/stamp",
            json={
                "file_path": str(pdf),
                "copy_type": "正本",
                "add_poa": True,
                "add_sent_to_opponent": True,
            },
        )

    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    assert body["copy_type"] == "正本"
    assert body["add_poa"] is True
    assert body["add_sent_to_opponent"] is True
    assert body["task"] == "mark"
    assert body["output_path"].endswith("in_正本.pdf")


def test_stamp_passes_manual_stamp_center_to_skill(client, tmp_path, monkeypatch):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    from api.blueprints import osc_cases
    monkeypatch.setattr(osc_cases, "_osc_is_safe_local_path", lambda p: True)
    monkeypatch.setattr(osc_cases, "_osc_local_path_candidates", lambda p: [Path(p)])
    monkeypatch.setattr(osc_cases, "_osc_resolve_existing_local_path", lambda c: c[0])

    captured = {}

    class FakeProc:
        stdout = json.dumps({"success": True, "output": str(tmp_path / "in_繕本.pdf")})
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    with patch("subprocess.run", side_effect=fake_run):
        r = client.post(
            "/api/osc/documents/stamp",
            json={"file_path": str(pdf), "copy_type": "繕本", "stamp_center": {"x": 123, "y": 456}},
        )

    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["stamp_center"] == {"x": 123.0, "y": 456.0}
    task_arg = captured["cmd"][3]
    assert '"stamp_center": {"x": 123.0, "y": 456.0}' in task_arg


def test_stamp_success_docx_uses_produce(client, tmp_path, monkeypatch):
    """DOCX → produce task → 預期從 outputs.marked 取 output_path。"""
    docx = tmp_path / "draft.docx"
    docx.write_bytes(b"PK\x03\x04")

    from api.blueprints import osc_cases
    monkeypatch.setattr(osc_cases, "_osc_is_safe_local_path", lambda p: True)
    monkeypatch.setattr(
        osc_cases, "_osc_local_path_candidates", lambda p: [Path(p)]
    )
    monkeypatch.setattr(
        osc_cases, "_osc_resolve_existing_local_path", lambda c: c[0]
    )

    fake_result = {
        "success": True,
        "outputs": {
            "pdf": str(tmp_path / "draft.pdf"),
            "marked": str(tmp_path / "draft_繕本.pdf"),
        },
        "error": "",
    }

    class FakeProc:
        stdout = json.dumps(fake_result)
        stderr = ""

    with patch("subprocess.run", return_value=FakeProc()):
        r = client.post(
            "/api/osc/documents/stamp",
            json={"file_path": str(docx), "copy_type": "繕本"},
        )

    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    assert body["task"] == "produce"
    assert body["output_path"].endswith("_繕本.pdf")


def test_stamp_skill_failure(client, tmp_path, monkeypatch):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    from api.blueprints import osc_cases
    monkeypatch.setattr(osc_cases, "_osc_is_safe_local_path", lambda p: True)
    monkeypatch.setattr(
        osc_cases, "_osc_local_path_candidates", lambda p: [Path(p)]
    )
    monkeypatch.setattr(
        osc_cases, "_osc_resolve_existing_local_path", lambda c: c[0]
    )

    fake_result = {
        "success": False,
        "output": "",
        "error": "PyMuPDF 檔案損壞",
    }

    class FakeProc:
        stdout = json.dumps(fake_result)
        stderr = ""

    with patch("subprocess.run", return_value=FakeProc()):
        r = client.post(
            "/api/osc/documents/stamp",
            json={"file_path": str(pdf), "copy_type": "正本"},
        )

    assert r.status_code == 500
    body = r.get_json()
    assert body["ok"] is False
    assert "PyMuPDF" in body["error"]


def test_stamp_preview_returns_last_page_image(client, tmp_path, monkeypatch):
    source = tmp_path / "preview.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 120), "最後一頁", fontname="china-ss", fontsize=14)
    doc.save(source)
    doc.close()

    from api.blueprints import osc_cases
    monkeypatch.setattr(osc_cases, "_osc_is_safe_local_path", lambda p: True)
    monkeypatch.setattr(osc_cases, "_osc_local_path_candidates", lambda p: [Path(p)])
    monkeypatch.setattr(osc_cases, "_osc_resolve_existing_local_path", lambda c: c[0])

    r = client.post("/api/osc/documents/stamp-preview", json={"file_path": str(source)})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    assert body["image_data"].startswith("data:image/png;base64,")
    assert body["page_width"] == 595
    assert body["page_height"] == 842


def test_finalize_pdf_generates_copies_labels_and_evidence(client, tmp_path, monkeypatch):
    source = tmp_path / "民事準備書狀.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 120), "書狀本文", fontname="china-ss", fontsize=14)
    doc.save(source)
    doc.close()

    evidence = tmp_path / "原證1 合約.pdf"
    evid_doc = fitz.open()
    evid_page = evid_doc.new_page(width=842, height=595)
    evid_page.insert_text((72, 120), "證據內容", fontname="china-ss", fontsize=14)
    evid_doc.save(evidence)
    evid_doc.close()

    from api.blueprints import osc_cases
    monkeypatch.setattr(osc_cases, "_osc_is_safe_local_path", lambda p: True)
    monkeypatch.setattr(osc_cases, "_osc_local_path_candidates", lambda p: [Path(p)])
    monkeypatch.setattr(osc_cases, "_osc_resolve_existing_local_path", lambda c: c[0])
    monkeypatch.setattr(osc_cases, "_export_file_meta", lambda p: {"success": True, "path": p})
    monkeypatch.setattr(osc_cases, "_osc_log_activity", lambda *a, **k: None)

    r = client.post(
        "/api/osc/documents/finalize",
        json={
            "file_path": str(source),
            "num_copies": 1,
            "add_poa": True,
            "add_sent_to_opponent": True,
            "include_evidence": True,
        },
    )

    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    final = Path(body["output_path"])
    assert final.exists()
    assert body["outputs"]["evidence_count"] == 1
    out_doc = fitz.open(final)
    try:
        text = "\n".join(p.get_text("text") for p in out_doc)
        assert out_doc.page_count == 6  # 正本/繕本/留底各 1 頁，每份後面各接 1 頁證據
        assert "正本" in text
        assert "繕本" in text
        assert "留底" in text
        assert "附委任狀" in text
        assert "繕本已送對造" in text
        assert "原\n證\n1" in text
        assert sum(len(p.get_images(full=True)) for p in out_doc) >= 3
    finally:
        out_doc.close()
