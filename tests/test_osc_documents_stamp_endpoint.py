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
