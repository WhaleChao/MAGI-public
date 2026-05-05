# -*- coding: utf-8 -*-
"""Paperclip 檔案管理：移動與回收區操作回歸測試。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager


def _client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.secret_key = "test"
    LoginManager().init_app(app)

    from api.blueprints.osc_files import osc_files_bp

    app.register_blueprint(osc_files_bp)
    return app.test_client()


def test_move_file_between_case_folders(tmp_path: Path):
    client = _client()
    wrong = tmp_path / "錯誤資料夾"
    right = tmp_path / "正確資料夾"
    wrong.mkdir()
    right.mkdir()
    src = wrong / "卷證.pdf"
    src.write_bytes(b"%PDF-test")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(tmp_path)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.post(
            "/api/osc/folders/move",
            json={
                "base_path": str(tmp_path),
                "source_relative_path": "錯誤資料夾/卷證.pdf",
                "target_relative_path": "正確資料夾",
            },
        )

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["new_relative_path"] == "正確資料夾/卷證.pdf"
    assert not src.exists()
    assert (right / "卷證.pdf").read_bytes() == b"%PDF-test"


def test_delete_action_moves_file_to_trash(tmp_path: Path):
    client = _client()
    case_dir = tmp_path / "案件A"
    case_dir.mkdir()
    src = case_dir / "誤上傳.docx"
    src.write_bytes(b"docx-test")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(tmp_path)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.post(
            "/api/osc/folders/move",
            json={
                "base_path": str(tmp_path),
                "source_relative_path": "案件A/誤上傳.docx",
                "to_trash": True,
            },
        )

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["to_trash"] is True
    assert data["new_relative_path"].startswith(".trash/誤上傳_")
    assert not src.exists()
    trashed = list((tmp_path / ".trash").glob("誤上傳_*.docx"))
    assert len(trashed) == 1
    assert trashed[0].read_bytes() == b"docx-test"


def test_move_file_to_case_root_is_allowed(tmp_path: Path):
    client = _client()
    wrong = tmp_path / "錯誤資料夾"
    wrong.mkdir()
    src = wrong / "要移回根目錄.txt"
    src.write_text("root-target", encoding="utf-8")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(tmp_path)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.post(
            "/api/osc/folders/move",
            json={
                "base_path": str(tmp_path),
                "source_relative_path": "錯誤資料夾/要移回根目錄.txt",
                "target_relative_path": "",
            },
        )

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["new_relative_path"] == "要移回根目錄.txt"
    assert not src.exists()
    assert (tmp_path / "要移回根目錄.txt").read_text(encoding="utf-8") == "root-target"
