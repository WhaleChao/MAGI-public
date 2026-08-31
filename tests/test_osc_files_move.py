# -*- coding: utf-8 -*-
"""Paperclip 檔案管理：移動與回收區操作回歸測試。"""
from __future__ import annotations

from pathlib import Path
from io import BytesIO
from urllib.parse import urlencode
from unittest.mock import patch
import builtins
import concurrent.futures
import errno
import os
import subprocess
import threading
import time

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin, login_user


def _client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.secret_key = "test"
    login = LoginManager()
    login.init_app(app)

    class TestUser(UserMixin):
        id = "test-user"

    @login.user_loader
    def _load_user(_user_id):
        return TestUser()

    from api.blueprints.osc_files import osc_files_bp

    app.register_blueprint(osc_files_bp)
    return app.test_client()


def _directory_route_app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.secret_key = "test"
    login = LoginManager()
    login.init_app(app)

    class TestUser(UserMixin):
        id = "test-user"

    @login.user_loader
    def _load_user(_user_id):
        return TestUser()

    from api.blueprints.osc_cases import osc_bp
    from api.blueprints.osc_files import osc_files_bp

    app.register_blueprint(osc_bp)
    app.register_blueprint(osc_files_bp)
    return app


def _client_with_role(role: str):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = False
    app.secret_key = "test"
    login = LoginManager()
    login.init_app(app)

    class TestUser(UserMixin):
        def __init__(self, user_role: str):
            self.id = f"{user_role}-user"
            self.role = user_role

    @login.user_loader
    def _load_user(_user_id):
        return TestUser(role)

    @app.route("/login")
    def _login():
        login_user(TestUser(role))
        return "ok"

    from api.blueprints.osc_files import osc_files_bp

    app.register_blueprint(osc_files_bp)
    client = app.test_client()
    client.get("/login")
    return client


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


def test_move_file_does_not_overwrite_existing_target(tmp_path: Path):
    client = _client()
    src_dir = tmp_path / "來源"
    dst_dir = tmp_path / "目標"
    src_dir.mkdir()
    dst_dir.mkdir()
    src = src_dir / "同名.pdf"
    dst = dst_dir / "同名.pdf"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(tmp_path)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.post(
            "/api/osc/folders/move",
            json={
                "base_path": str(tmp_path),
                "source_relative_path": "來源/同名.pdf",
                "target_relative_path": "目標",
            },
        )

    assert r.status_code == 409
    data = r.get_json()
    assert data["ok"] is False
    assert data["error"] == "target_exists"
    assert src.read_bytes() == b"new"
    assert dst.read_bytes() == b"old"


def test_move_folder_into_itself_is_rejected(tmp_path: Path):
    client = _client()
    folder = tmp_path / "證據資料"
    child = folder / "子資料夾"
    child.mkdir(parents=True)
    (folder / "內容.txt").write_text("payload", encoding="utf-8")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(tmp_path)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.post(
            "/api/osc/folders/move",
            json={
                "base_path": str(tmp_path),
                "source_relative_path": "證據資料",
                "target_relative_path": "證據資料/子資料夾",
            },
        )

    assert r.status_code == 400
    data = r.get_json()
    assert data["ok"] is False
    assert data["error"] == "nested_target"
    assert folder.is_dir()
    assert child.is_dir()
    assert (folder / "內容.txt").read_text(encoding="utf-8") == "payload"


def test_upload_multi_accepts_batch_files_and_reports_conflicts(tmp_path: Path):
    client = _client()
    case_dir = tmp_path / "案件A"
    case_dir.mkdir()
    (case_dir / "既有.pdf").write_bytes(b"old")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.post(
            "/api/osc/files/upload-multi",
            data={
                "base_path": str(case_dir),
                "files": [
                    (BytesIO(b"%PDF-a"), "新證據A.pdf"),
                    (BytesIO(b"%PDF-b"), "新證據B.pdf"),
                    (BytesIO(b"%PDF-old"), "既有.pdf"),
                ],
            },
            content_type="multipart/form-data",
        )

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["succeeded"] == 2
    assert data["failed"] == 1
    assert (case_dir / "新證據A.pdf").read_bytes() == b"%PDF-a"
    assert (case_dir / "新證據B.pdf").read_bytes() == b"%PDF-b"
    conflict = [item for item in data["results"] if item.get("name") == "既有.pdf"][0]
    assert conflict["error"] == "file_exists"


def test_upload_multi_preserves_folder_paths_and_overwrites_when_confirmed(tmp_path: Path):
    client = _client()
    case_dir = tmp_path / "案件A"
    case_dir.mkdir()
    existing = case_dir / "證據資料" / "同名.pdf"
    existing.parent.mkdir()
    existing.write_bytes(b"old")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        conflict = client.post(
            "/api/osc/files/upload-multi",
            data={
                "base_path": str(case_dir),
                "files": [(BytesIO(b"%PDF-new"), "同名.pdf")],
                "relative_paths": ["證據資料/同名.pdf"],
            },
            content_type="multipart/form-data",
        )

    assert conflict.status_code == 200
    assert conflict.get_json()["results"][0]["error"] == "file_exists"
    assert existing.read_bytes() == b"old"

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        overwrite = client.post(
            "/api/osc/files/upload-multi",
            data={
                "base_path": str(case_dir),
                "overwrite": "1",
                "files": [
                    (BytesIO(b"%PDF-new"), "同名.pdf"),
                    (BytesIO(b"%PDF-child"), "新檔.pdf"),
                ],
                "relative_paths": ["證據資料/同名.pdf", "證據資料/子資料夾/新檔.pdf"],
            },
            content_type="multipart/form-data",
        )

    assert overwrite.status_code == 200
    data = overwrite.get_json()
    assert data["succeeded"] == 2
    assert existing.read_bytes() == b"%PDF-new"
    assert (case_dir / "證據資料" / "子資料夾" / "新檔.pdf").read_bytes() == b"%PDF-child"


def test_upload_multi_blocked_overwrite_keeps_original_file(tmp_path: Path):
    client = _client()
    case_dir = tmp_path / "案件A"
    case_dir.mkdir()
    existing = case_dir / "重要.pdf"
    existing.write_bytes(b"old-safe-content")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.post(
            "/api/osc/files/upload-multi",
            data={
                "base_path": str(case_dir),
                "overwrite": "1",
                "files": [(BytesIO(b"MZ disguised executable"), "重要.pdf")],
            },
            content_type="multipart/form-data",
        )

    assert r.status_code == 200
    data = r.get_json()
    assert data["results"][0]["error"] == "blocked_content_signature"
    assert existing.read_bytes() == b"old-safe-content"


def test_file_write_routes_require_operator_when_login_enabled(tmp_path: Path):
    client = _client_with_role("viewer")

    r = client.post(
        "/api/osc/folders/mkdir",
        json={"base_path": str(tmp_path), "name": "新資料夾"},
    )

    assert r.status_code == 403
    assert r.get_json()["error"] == "forbidden"


def test_chunked_upload_uses_actor_namespaced_session_dir(tmp_path: Path, monkeypatch):
    client = _client()
    case_dir = tmp_path / "案件A"
    case_dir.mkdir()

    from api.blueprints import osc_files as mod

    chunk_root = tmp_path / "chunks"
    monkeypatch.setattr(mod, "_CHUNK_TMP_DIR", chunk_root)
    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.post(
            "/api/osc/files/upload-chunked",
            data={
                "session_id": "sessionA",
                "chunk_index": "0",
                "total_chunks": "2",
                "filename": "大型卷證.pdf",
                "base_path": str(case_dir),
                "chunk": (BytesIO(b"part-0"), "chunk.part"),
            },
            content_type="multipart/form-data",
        )

    assert r.status_code == 200
    assert r.get_json()["finalized"] is False
    namespace_dirs = [p for p in chunk_root.iterdir() if p.is_dir()]
    assert len(namespace_dirs) == 1
    assert (namespace_dirs[0] / "sessionA" / "000000.part").read_bytes() == b"part-0"
    assert not (chunk_root / "sessionA").exists()


def test_delete_action_moves_folder_to_trash(tmp_path: Path):
    client = _client()
    case_dir = tmp_path / "案件A"
    target = case_dir / "誤建資料夾"
    target.mkdir(parents=True)
    (target / "內容.txt").write_text("payload", encoding="utf-8")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(tmp_path)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.post(
            "/api/osc/folders/move",
            json={
                "base_path": str(tmp_path),
                "source_relative_path": "案件A/誤建資料夾",
                "to_trash": True,
            },
        )

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["source_exists"] is False
    assert not target.exists()
    trashed = list((tmp_path / ".trash").glob("誤建資料夾_*"))
    assert len(trashed) == 1
    assert (trashed[0] / "內容.txt").read_text(encoding="utf-8") == "payload"


def test_share_file_creates_opaque_download_link(tmp_path: Path, monkeypatch):
    client = _client()
    src = tmp_path / "卷證.pdf"
    src.write_bytes(b"%PDF-share")

    from api.blueprints import osc_files as mod

    monkeypatch.setattr(mod, "_SHARE_STORE_PATH", tmp_path / "shares.json")
    monkeypatch.setenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL", "https://paperclip-share.example.test")
    with patch("api.blueprints.osc_files._resolve_safe_file", return_value=str(src)):
        r = client.post("/api/osc/files/share", json={"path": str(src), "ttl_sec": 600})

        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["url"].startswith("https://paperclip-share.example.test/s/")
        assert "/s/" in data["url"]
        assert "卷證" not in data["url"]

        token = data["url"].rstrip("/").split("/s/", 1)[1]
        row = mod._load_share_store()["shares"][mod._share_token_hash(token)]
        assert row["actor_namespace"].startswith("anonymous-")
        download = client.get(f"/s/{token}")

    assert download.status_code == 200
    assert download.data == b"%PDF-share"


def test_share_download_streams_without_send_file(tmp_path: Path, monkeypatch):
    client = _client()
    src = tmp_path / "卷證.pdf"
    src.write_bytes(b"%PDF-share-stream")

    from api.blueprints import osc_files as mod

    def fail_send_file(*_args, **_kwargs):
        raise AssertionError("shared files should stream staged files directly")

    monkeypatch.setattr(mod, "send_file", fail_send_file)
    monkeypatch.setattr(mod, "_SHARE_STORE_PATH", tmp_path / "shares.json")
    monkeypatch.setenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL", "https://paperclip-share.example.test")
    with patch("api.blueprints.osc_files._resolve_safe_file", return_value=str(src)):
        r = client.post("/api/osc/files/share", json={"path": str(src), "ttl_sec": 600})
        token = r.get_json()["url"].rstrip("/").split("/s/", 1)[1]
        download = client.get(f"/s/{token}", headers={"Range": "bytes=5-9"})

    assert download.status_code == 206
    assert download.data == b"share"
    assert download.headers["Content-Range"].endswith(f"/{src.stat().st_size}")


def test_share_download_chinese_pdf_has_mobile_safe_ascii_filename(tmp_path: Path, monkeypatch):
    client = _client()
    src = tmp_path / "支出表.pdf"
    src.write_bytes(b"%PDF-share-filename")

    from api.blueprints import osc_files as mod

    monkeypatch.setattr(mod, "_SHARE_STORE_PATH", tmp_path / "shares.json")
    monkeypatch.setenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL", "https://paperclip-share.example.test")
    with patch("api.blueprints.osc_files._resolve_safe_file", return_value=str(src)):
        r = client.post("/api/osc/files/share", json={"path": str(src), "ttl_sec": 600})
        token = r.get_json()["url"].rstrip("/").split("/s/", 1)[1]
        download = client.get(f"/s/{token}")

    assert download.status_code == 200
    cd = download.headers["Content-Disposition"]
    assert 'filename="paperclip.pdf"' in cd
    assert "filename*=UTF-8''" in cd
    assert "%E6%94%AF%E5%87%BA%E8%A1%A8.pdf" in cd


def test_share_download_serves_cached_copy_when_original_is_unavailable(tmp_path: Path, monkeypatch):
    client = _client()
    src = tmp_path / "卷證.pdf"
    src.write_bytes(b"%PDF-cached-share")

    from api.blueprints import osc_files as mod

    monkeypatch.setattr(mod, "_SHARE_STORE_PATH", tmp_path / "shares.json")
    monkeypatch.setenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL", "https://paperclip-share.example.test")
    with patch("api.blueprints.osc_files._resolve_safe_file", return_value=str(src)):
        r = client.post("/api/osc/files/share", json={"path": str(src), "ttl_sec": 600})

    assert r.status_code == 200
    token = r.get_json()["url"].rstrip("/").split("/s/", 1)[1]
    row = mod._load_share_store()["shares"][mod._share_token_hash(token)]
    assert Path(row["staged_path"]).is_file()
    assert Path(row["staged_path"]).read_bytes() == b"%PDF-cached-share"

    src.unlink()
    download = client.get(f"/s/{token}")

    assert download.status_code == 200
    assert download.data == b"%PDF-cached-share"


def test_expired_share_prune_removes_cached_copy(tmp_path: Path, monkeypatch):
    from api.blueprints import osc_files as mod

    monkeypatch.setattr(mod, "_SHARE_STORE_PATH", tmp_path / "shares.json")
    cached = tmp_path / "osc_file_share_cache" / ("a" * 64)
    cached.parent.mkdir()
    cached.write_bytes(b"cached")
    data = {"shares": {"token-hash": {"expires_at": 1, "staged_path": str(cached)}}}

    pruned = mod._prune_share_store(data)

    assert pruned["shares"] == {}
    assert not cached.exists()


def test_pdf_preview_content_url_is_encoded(tmp_path: Path):
    client = _client()
    src = tmp_path / "卷證 A&B#1.pdf"
    src.write_bytes(b"%PDF-preview")

    query = urlencode({"path": str(src)})
    with patch("api.blueprints.osc_files._osc_resolve_existing_local_path", return_value=str(src)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.get(f"/api/osc/files/preview?{query}")

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["kind"] == "pdf"
    assert "%26" in data["content_url"]
    assert "%23" in data["content_url"]
    assert "A&B#1" not in data["content_url"]


def test_structured_preview_uses_staged_file_before_reading(tmp_path: Path, monkeypatch):
    client = _client()
    src = tmp_path / "資料.csv"
    src.write_text("name\n原始\n", encoding="utf-8")
    staged = tmp_path / "staged.csv"
    staged.write_text("name\n暫存\n", encoding="utf-8")

    from api.blueprints import osc_files as mod

    seen = {}

    def fake_preview_csv(path):
        seen["path"] = path
        return {"ok": True, "headers": ["name"], "rows": [["暫存"]], "truncated": False, "row_count": 1}

    monkeypatch.setattr(mod, "_stage_file_with_retry", lambda local: str(staged))
    monkeypatch.setattr(mod.osc_preview, "preview_csv_to_rows", fake_preview_csv)
    with patch("api.blueprints.osc_files._osc_resolve_existing_local_path", return_value=str(src)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.get(f"/api/osc/files/preview?{urlencode({'path': str(src)})}")

    assert r.status_code == 200
    assert r.get_json()["rows"] == [["暫存"]]
    assert seen["path"] == str(staged)
    assert not staged.exists()


def test_share_requires_independent_base_even_on_localhost(tmp_path: Path, monkeypatch):
    client = _client()
    src = tmp_path / "卷證.pdf"
    src.write_bytes(b"%PDF-share")

    from api.blueprints import osc_files as mod

    monkeypatch.delenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("MAGI_OSC_FILE_SHARE_ALLOW_CONSOLE_BASE", raising=False)
    monkeypatch.setattr(mod, "_SHARE_STORE_PATH", tmp_path / "shares.json")
    monkeypatch.setattr(mod, "_SHARE_PUBLIC_BASE_FILE", tmp_path / "missing_share_base.txt")
    with patch("api.blueprints.osc_files._resolve_safe_file", return_value=str(src)):
        r = client.post(
            "/api/osc/files/share",
            base_url="http://127.0.0.1:5002",
            json={"path": str(src), "ttl_sec": 600},
        )

    assert r.status_code == 409
    data = r.get_json()
    assert data["ok"] is False
    assert data["error"] == "share_public_base_required"
    assert not (tmp_path / "shares.json").exists()


def test_console_share_base_requires_explicit_override(tmp_path: Path, monkeypatch):
    client = _client()
    src = tmp_path / "卷證.pdf"
    src.write_bytes(b"%PDF-share")

    from api.blueprints import osc_files as mod

    monkeypatch.delenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("MAGI_OSC_FILE_SHARE_ALLOW_CONSOLE_BASE", "1")
    monkeypatch.setattr(mod, "_SHARE_STORE_PATH", tmp_path / "shares.json")
    monkeypatch.setattr(mod, "_SHARE_PUBLIC_BASE_FILE", tmp_path / "missing_share_base.txt")
    with patch("api.blueprints.osc_files._resolve_safe_file", return_value=str(src)):
        r = client.post(
            "/api/osc/files/share",
            base_url="http://127.0.0.1:5002",
            json={"path": str(src), "ttl_sec": 600},
        )

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["url"].startswith("http://127.0.0.1:5002/s/")
    assert data["url_mode"] == "console_base_explicit"


def test_external_share_uses_independent_share_base(tmp_path: Path, monkeypatch):
    client = _client()
    src = tmp_path / "卷證.pdf"
    src.write_bytes(b"%PDF-share")

    from api.blueprints import osc_files as mod

    monkeypatch.setenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL", "https://paperclip-share.example.test")
    monkeypatch.setattr(mod, "_SHARE_STORE_PATH", tmp_path / "shares.json")
    monkeypatch.setattr(mod, "_SHARE_PUBLIC_BASE_FILE", tmp_path / "ignored_share_base.txt")
    with patch("api.blueprints.osc_files._resolve_safe_file", return_value=str(src)):
        r = client.post(
            "/api/osc/files/share",
            base_url="https://aimac-mini.tail6738b7.ts.net",
            json={"path": str(src), "ttl_sec": 600},
        )

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["url"].startswith("https://paperclip-share.example.test/s/")
    assert "aimac-mini.tail6738b7.ts.net" not in data["url"]
    assert data["url_mode"] == "independent_share_base"


def test_share_download_retries_macos_smb_deadlock(tmp_path: Path, monkeypatch):
    from api.blueprints import osc_files as mod

    src = tmp_path / "卷證.pdf"
    src.write_bytes(b"%PDF-share")
    attempts = {"n": 0}

    class FakeFile:
        def __init__(self):
            self._delivered = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError(errno.EDEADLK, "Resource deadlock avoided")
            if self._delivered:
                return b""
            self._delivered = True
            return b"%PDF-share"

    def fake_open(path, mode="r", *args, **kwargs):
        if str(path) == str(src) and mode == "rb":
            return FakeFile()
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(mod, "open", fake_open, raising=False)

    assert mod._read_file_with_retry(str(src)) == b"%PDF-share"
    assert attempts["n"] >= 1


def test_share_staging_fails_bounded_when_reader_never_reaches_eof(tmp_path: Path, monkeypatch):
    from api.blueprints import osc_files as mod

    src = tmp_path / "broken-source.pdf"
    src.write_bytes(b"x")

    class EndlessReader:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return b"y" * 1024

    def fake_open(path, mode="r", *args, **kwargs):
        if str(path) == str(src) and mode == "rb":
            return EndlessReader()
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(mod, "open", fake_open, raising=False)
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path))

    with pytest.raises(OSError, match="staged copy exceeded source size"):
        mod._stage_file_with_retry(str(src), max_attempts=1)

    staged_dir = tmp_path / "paperclip-shares"
    assert staged_dir.is_dir()
    assert list(staged_dir.iterdir()) == []


def test_share_download_uses_system_cp_when_python_read_keeps_deadlocking(tmp_path: Path, monkeypatch):
    from api.blueprints import osc_files as mod

    src = tmp_path / "卷證.pdf"
    src.write_bytes(b"%PDF-share-cp")
    attempts = {"n": 0}

    def fake_open(path, mode="r", *args, **kwargs):
        if str(path) == str(src) and mode == "rb":
            attempts["n"] += 1
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return builtins.open(path, mode, *args, **kwargs)

    def fake_run(argv, **_kwargs):
        Path(argv[-1]).write_bytes(b"%PDF-share-cp")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(mod, "open", fake_open, raising=False)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod._read_file_with_retry(str(src)) == b"%PDF-share-cp"
    assert attempts["n"] >= 1


def test_share_download_streams_when_system_copy_fails(tmp_path: Path, monkeypatch):
    from api.blueprints import osc_files as mod

    src = tmp_path / "卷證.csv"
    payload = "name\n王小明\n".encode("utf-8")
    src.write_bytes(payload)
    copy_attempts = {"n": 0}

    def fake_run(_argv, **_kwargs):
        copy_attempts["n"] += 1
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "copy failed"})()

    monkeypatch.setattr(mod, "_should_prefer_system_copy", lambda _path: True)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod._read_file_with_retry(str(src)) == payload
    assert copy_attempts["n"] >= 1


def test_share_download_stages_even_when_source_stat_deadlocks(tmp_path: Path, monkeypatch):
    from api.blueprints import osc_files as mod

    src = tmp_path / "卷證.pdf"
    payload = b"%PDF-share-stat-deadlock"
    src.write_bytes(payload)
    attempts = {"n": 0}

    def flaky_stat(path, **_kwargs):
        if str(path) == str(src):
            attempts["n"] += 1
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return mod.os.stat(path)

    monkeypatch.setattr(mod, "_stat_with_retry", flaky_stat)

    assert mod._read_file_with_retry(str(src)) == payload
    assert attempts["n"] >= 1


def test_share_head_does_not_read_dataless_file(tmp_path: Path, monkeypatch):
    client = _client()
    src = tmp_path / "卷證.pdf"
    src.write_bytes(b"%PDF-share")

    from api.blueprints import osc_files as mod

    monkeypatch.setattr(mod, "_SHARE_STORE_PATH", tmp_path / "shares.json")
    monkeypatch.setenv("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL", "https://paperclip-share.example.test")
    with patch("api.blueprints.osc_files._resolve_safe_file", return_value=str(src)):
        r = client.post("/api/osc/files/share", json={"path": str(src), "ttl_sec": 600})
        token = r.get_json()["url"].rstrip("/").split("/s/", 1)[1]

        def fail_open(*_args, **_kwargs):
            raise AssertionError("HEAD should not read the shared file")

        monkeypatch.setattr(mod, "open", fail_open, raising=False)
        head = client.head(f"/s/{token}")

    assert head.status_code == 200
    assert head.data == b""
    assert head.headers["Content-Length"] == str(src.stat().st_size)


def test_share_gateway_only_accepts_opaque_share_paths():
    from scripts.share_gateway import TOKEN_RE

    assert TOKEN_RE.fullmatch("/s/" + ("A" * 32))
    assert not TOKEN_RE.fullmatch("/osc")
    assert not TOKEN_RE.fullmatch("/login")
    assert not TOKEN_RE.fullmatch("/api/osc/files/content")
    assert not TOKEN_RE.fullmatch("/s/too-short")


def test_nas_helper_uses_threaded_http_server():
    from http.server import ThreadingHTTPServer
    from scripts.ops import osc_shell_nas_helper as helper

    assert issubclass(helper.HTTPServer, ThreadingHTTPServer)
    assert helper.HTTPServer.daemon_threads is True
    assert helper.HTTPServer.block_on_close is False


def test_nas_helper_health_is_not_blocked_by_slow_stage(tmp_path: Path, monkeypatch):
    import threading
    from scripts.ops import osc_shell_nas_helper as helper

    source = tmp_path / "large.pdf"
    source.write_bytes(b"%PDF")
    staged = tmp_path / "staged.pdf"
    staged.write_bytes(b"%PDF")
    stage_started = threading.Event()
    release_stage = threading.Event()

    monkeypatch.setattr(helper, "_is_path_allowed", lambda _path: True)
    monkeypatch.setattr(helper, "_source_file_size_quick", lambda _path: source.stat().st_size)
    monkeypatch.setattr(helper, "_HELPER_STAGE_SLOTS", threading.BoundedSemaphore(1))

    def slow_stage(_path, *, expected_size=None):
        assert expected_size == source.stat().st_size
        stage_started.set()
        assert release_stage.wait(2.0)
        return staged

    monkeypatch.setattr(helper, "_copy_to_stage", slow_stage)
    responses: dict[int, list[tuple[int, dict]]] = {}

    def write_json(handler, status, payload):
        responses.setdefault(id(handler), []).append((status, payload))

    def handler(path: str, payload: dict | None = None):
        instance = object.__new__(helper.OscShellNASHandler)
        instance.path = path
        instance._read_json = lambda: dict(payload or {})
        return instance

    monkeypatch.setattr(helper, "_write_json", write_json)
    slow_handler = handler("/stage", {"path": str(source)})

    post_thread = threading.Thread(
        target=helper.OscShellNASHandler.do_POST,
        args=(slow_handler,),
        daemon=True,
    )
    post_thread.start()
    try:
        assert stage_started.wait(1.0)

        # The one-slot stage bulkhead must reject another expensive copy
        # immediately while the original stage owns the slot.
        busy_handler = handler("/stage", {"path": str(source)})
        busy_started = time.monotonic()
        helper.OscShellNASHandler.do_POST(busy_handler)
        assert time.monotonic() - busy_started < 0.5
        assert responses[id(busy_handler)] == [
            (429, {"ok": False, "error": "stage_busy"})
        ]

        # Exercise the real handler path in-process.  Socket bind/connect is
        # intentionally absent because the release-quality Seatbelt denies all
        # network, including loopback; the adjacent server-class test proves
        # that production dispatches requests on independent threads.
        health_handler = handler("/health")
        started = time.monotonic()
        helper.OscShellNASHandler.do_GET(health_handler)
        assert time.monotonic() - started < 0.5
        assert responses[id(health_handler)] == [(200, {"ok": True})]
    finally:
        release_stage.set()
        post_thread.join(2.0)

    assert responses[id(slow_handler)] == [
        (200, {"ok": True, "staged_path": str(staged), "size": staged.stat().st_size})
    ]


def test_nas_helper_listdir_child_returns_parallel_metadata(tmp_path: Path):
    from scripts.ops import osc_shell_nas_helper as helper

    (tmp_path / "子資料夾").mkdir()
    (tmp_path / "卷證.pdf").write_bytes(b"%PDF")

    payload = helper._listdir_payload_uncached(str(tmp_path))

    assert payload["ok"] is True
    rows = {row["name"]: row for row in payload["entries"]}
    assert rows["子資料夾"]["is_dir"] is True
    assert rows["卷證.pdf"]["is_dir"] is False
    assert rows["卷證.pdf"]["size"] == 4


def test_nas_helper_listdir_cache_avoids_duplicate_smb_reads(tmp_path: Path, monkeypatch):
    from scripts.ops import osc_shell_nas_helper as helper

    calls = []
    with helper._LISTDIR_CACHE_LOCK:
        helper._LISTDIR_CACHE.clear()
        helper._LISTDIR_PATH_LOCKS.clear()
        helper._LISTDIR_REFRESHING.clear()

    def successful_listing(path):
        calls.append(path)
        return {"ok": True, "entries": [{"name": "卷證.pdf", "is_dir": False}], "count": 1}

    monkeypatch.setattr(helper, "_listdir_payload_uncached", successful_listing)
    monkeypatch.setattr(helper, "_HELPER_LISTDIR_CACHE_FRESH_SECONDS", 30.0)

    first = helper._listdir_payload(str(tmp_path))
    second = helper._listdir_payload(str(tmp_path))
    refreshed = helper._listdir_payload(str(tmp_path), force_refresh=True)

    assert [first["cache_status"], second["cache_status"], refreshed["cache_status"]] == [
        "miss", "hit", "miss",
    ]
    assert calls == [str(tmp_path), str(tmp_path)]


def test_nas_helper_serves_stale_success_while_refreshing(tmp_path: Path, monkeypatch):
    from scripts.ops import osc_shell_nas_helper as helper

    key = os.path.realpath(str(tmp_path))
    payload = {"ok": True, "entries": [{"name": "既有卷證.pdf", "is_dir": False}], "count": 1}
    with helper._LISTDIR_CACHE_LOCK:
        helper._LISTDIR_CACHE.clear()
        helper._LISTDIR_CACHE[key] = (time.monotonic() - 10.0, payload)
    scheduled = []
    monkeypatch.setattr(helper, "_HELPER_LISTDIR_CACHE_FRESH_SECONDS", 1.0)
    monkeypatch.setattr(helper, "_HELPER_LISTDIR_CACHE_STALE_SECONDS", 60.0)
    monkeypatch.setattr(helper, "_schedule_listdir_refresh", lambda cache_key, path: scheduled.append((cache_key, path)))

    result = helper._listdir_payload(str(tmp_path))

    assert result["ok"] is True
    assert result["cache_status"] == "stale"
    assert result["entries"][0]["name"] == "既有卷證.pdf"
    assert scheduled == [(key, str(tmp_path))]


def test_nas_helper_failed_forced_refresh_keeps_recent_success(tmp_path: Path, monkeypatch):
    from scripts.ops import osc_shell_nas_helper as helper

    key = os.path.realpath(str(tmp_path))
    payload = {"ok": True, "entries": [{"name": "既有卷證.pdf", "is_dir": False}], "count": 1}
    with helper._LISTDIR_CACHE_LOCK:
        helper._LISTDIR_CACHE.clear()
        helper._LISTDIR_CACHE[key] = (time.monotonic(), payload)
    monkeypatch.setattr(
        helper,
        "_listdir_payload_uncached",
        lambda _path: (_ for _ in ()).throw(helper._ListdirHelperError(504, "timeout")),
    )

    result = helper._listdir_payload(str(tmp_path), force_refresh=True)

    assert result["ok"] is True
    assert result["cache_status"] == "stale_refresh_failed"
    assert result["entries"][0]["name"] == "既有卷證.pdf"


def test_folder_browse_lists_with_scandir_by_default(tmp_path: Path):
    client = _client()
    case_dir = tmp_path / "案件資料夾"
    case_dir.mkdir()
    (case_dir / "01_法扶資料").mkdir()
    (case_dir / "卷證.pdf").write_bytes(b"%PDF")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        r = client.get("/api/osc/folders/browse", query_string={"base_path": str(case_dir)})

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert [item["name"] for item in data["folders"]] == ["01_法扶資料"]
    assert [item["name"] for item in data["files"]] == ["卷證.pdf"]


def test_folder_browse_does_not_summarize_children_by_default(tmp_path: Path, monkeypatch):
    client = _client()
    case_dir = tmp_path / "案件資料夾"
    child = case_dir / "04_閱卷資料" / "20260715" / "手機翻拍卷宗照片"
    child.mkdir(parents=True)
    (case_dir / "04_閱卷資料" / "卷證.pdf").write_bytes(b"%PDF")

    from api.blueprints import osc_files as mod

    def fail_summary(*_args, **_kwargs):
        raise AssertionError("default folder browse must not walk child directories")

    monkeypatch.setattr(mod, "_summarize_dir", fail_summary)
    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        response = client.get(
            "/api/osc/folders/browse",
            query_string={"base_path": str(case_dir), "relative_path": "04_閱卷資料"},
        )

    assert response.status_code == 200
    data = response.get_json()
    assert [item["relative_path"] for item in data["folders"]] == ["04_閱卷資料/20260715"]
    assert [item["relative_path"] for item in data["files"]] == ["04_閱卷資料/卷證.pdf"]
    assert "child_files" not in data["folders"][0]


def test_folder_browse_summary_remains_explicitly_available(tmp_path: Path):
    client = _client()
    case_dir = tmp_path / "案件資料夾"
    child = case_dir / "04_閱卷資料"
    child.mkdir(parents=True)
    (child / "卷證.pdf").write_bytes(b"%PDF")

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        response = client.get(
            "/api/osc/folders/browse",
            query_string={"base_path": str(case_dir), "summarize_dirs": "1"},
        )

    assert response.status_code == 200
    folder = response.get_json()["folders"][0]
    assert folder["child_files"] == 1
    assert folder["child_total_size"] == 4


def test_network_tree_does_not_probe_each_grandchild(tmp_path: Path, monkeypatch):
    client = _client()
    case_dir = tmp_path / "案件資料夾"
    child = case_dir / "04_閱卷資料"
    child.mkdir(parents=True)

    from api.blueprints import osc_files as mod

    monkeypatch.setattr(mod, "_is_network_nas_path", lambda _path: True)
    monkeypatch.setattr(
        mod,
        "_dir_metadata_map",
        lambda path: {
            "04_閱卷資料": {
                "name": "04_閱卷資料",
                "is_dir": True,
                "size": 0,
                "mtime": 1,
                "error": None,
            }
        },
    )

    def fail_grandchild_probe(*_args, **_kwargs):
        raise AssertionError("network tree must lazy-load instead of probing every child")

    monkeypatch.setattr(mod, "_has_network_subdir", fail_grandchild_probe)
    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        response = client.get(
            "/api/osc/folders/tree",
            query_string={"base_path": str(case_dir)},
        )

    assert response.status_code == 200
    assert response.get_json()["children"] == [
        {"name": "04_閱卷資料", "relative_path": "04_閱卷資料", "has_subdirs": True}
    ]


def test_network_browse_uses_bounded_metadata_helper_without_legacy_scandir(tmp_path: Path, monkeypatch):
    client = _client()
    case_dir = tmp_path / "案件資料夾"
    case_dir.mkdir()

    from api.blueprints import osc_files as mod

    monkeypatch.setattr(mod, "_is_network_nas_path", lambda _path: True)
    monkeypatch.setattr(
        mod,
        "_browse_entries_with_scandir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network browse must not use per-entry legacy scandir")
        ),
    )
    monkeypatch.setattr(
        mod,
        "_browse_entries_with_helper",
        lambda *_args, **_kwargs: (
            [{"name": "04_閱卷資料", "relative_path": "04_閱卷資料", "type": "dir"}],
            [],
            0,
        ),
    )
    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True), \
         patch("api.blueprints.osc_files._osc_isdir_quick", return_value=True):
        response = client.get(
            "/api/osc/folders/browse",
            query_string={"base_path": str(case_dir)},
        )

    assert response.status_code == 200
    assert response.get_json()["source"] == "folder_helper"


def test_file_stage_bulkhead_rejects_parallel_staging_without_blocking_workers():
    from api.osc import utils

    holding = threading.Event()
    release = threading.Event()

    def hold_slot():
        with utils._osc_file_stage_slot():
            holding.set()
            assert release.wait(2.0)

    worker = threading.Thread(target=hold_slot, daemon=True)
    worker.start()
    try:
        assert holding.wait(1.0)

        @utils._osc_stage_bulkhead
        def second_stage():
            return "unexpected"

        started = time.monotonic()
        with pytest.raises(OSError) as error:
            second_stage()
        assert error.value.errno == errno.EBUSY
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        worker.join(2.0)


def test_directory_io_bulkhead_limits_eight_parallel_requests_to_six_slots():
    from api.osc import utils

    barrier = threading.Barrier(8)
    release = threading.Event()
    six_entered = threading.Event()
    state_lock = threading.Lock()
    active = 0
    peak_active = 0

    def worker():
        nonlocal active, peak_active
        barrier.wait(timeout=2.0)
        try:
            with utils._osc_directory_io_slot():
                with state_lock:
                    active += 1
                    peak_active = max(peak_active, active)
                    if active == 6:
                        six_entered.set()
                try:
                    assert release.wait(2.0)
                finally:
                    with state_lock:
                        active -= 1
                return "entered"
        except OSError as exc:
            assert exc.errno == errno.EBUSY
            return "busy"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(8)]
        try:
            assert six_entered.wait(1.0)
            time.sleep(0.85)
        finally:
            release.set()
        results = [future.result(timeout=2.0) for future in futures]

    assert results.count("entered") == 6
    assert results.count("busy") == 2
    assert peak_active == 6


def test_directory_io_route_returns_retryable_503_when_slots_are_busy(monkeypatch):
    from api.osc import utils
    from api.blueprints import osc_files as mod

    release = threading.Event()
    six_entered = threading.Event()
    entered_lock = threading.Lock()
    entered = 0

    def hold_slot():
        nonlocal entered
        with utils._osc_directory_io_slot():
            with entered_lock:
                entered += 1
                if entered == 6:
                    six_entered.set()
            assert release.wait(2.0)

    holders = [threading.Thread(target=hold_slot, daemon=True) for _ in range(6)]
    for holder in holders:
        holder.start()
    try:
        assert six_entered.wait(1.0)
        monkeypatch.setattr(
            mod,
            "_resolve_target_dir",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("busy requests must fail before touching the NAS")
            ),
        )
        started = time.monotonic()
        response = _client().get("/api/osc/folders/browse", query_string={"base_path": "/tmp"})
        elapsed = time.monotonic() - started

        assert response.status_code == 503
        assert response.get_json()["error"] == "directory_io_busy"
        assert response.headers["Retry-After"] == "1"
        assert elapsed < 1.2
    finally:
        release.set()
        for holder in holders:
            holder.join(2.0)


def _assert_eight_request_route_bulkhead(app, route, query, patch_target, blocked_result):
    barrier = threading.Barrier(8)
    release = threading.Event()
    six_entered = threading.Event()
    state_lock = threading.Lock()
    active = 0
    peak_active = 0

    def blocked_call(*_args, **_kwargs):
        nonlocal active, peak_active
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
            if active == 6:
                six_entered.set()
        try:
            assert release.wait(2.0)
            return blocked_result
        finally:
            with state_lock:
                active -= 1

    def worker():
        client = app.test_client()
        barrier.wait(timeout=2.0)
        response = client.get(route, query_string=query)
        payload = response.get_json(silent=True) or {}
        return response.status_code, payload.get("error")

    with patch(patch_target, side_effect=blocked_call), \
         concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(8)]
        try:
            assert six_entered.wait(1.0)
            time.sleep(0.85)
        finally:
            release.set()
        results = [future.result(timeout=2.0) for future in futures]

    assert results.count((503, "directory_io_busy")) == 2
    assert peak_active == 6


@pytest.mark.parametrize(
    ("route", "query", "patch_target", "blocked_result"),
    [
        (
            "/api/osc/cases/case-1/folder-path",
            {},
            "api.blueprints.osc_cases._osc_exec",
            (None, None),
        ),
        (
            "/api/osc/files/preview",
            {"path": "/tmp/missing.pdf"},
            "api.blueprints.osc_files._osc_resolve_existing_local_path",
            None,
        ),
        (
            "/api/osc/files/info",
            {"path": "/tmp/missing.pdf"},
            "api.blueprints.osc_files._osc_resolve_existing_local_path",
            None,
        ),
        (
            "/api/osc/files/content",
            {"path": "/tmp/missing.pdf"},
            "api.blueprints.osc_cases._osc_local_path_candidates",
            [],
        ),
    ],
)
def test_frontend_directory_routes_limit_eight_blocking_requests(
    route,
    query,
    patch_target,
    blocked_result,
):
    _assert_eight_request_route_bulkhead(
        _directory_route_app(),
        route,
        query,
        patch_target,
        blocked_result,
    )


def test_nas_helper_source_stat_is_timeout_bounded(monkeypatch):
    from scripts.ops import osc_shell_nas_helper as helper

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["python"], timeout=0.1)

    monkeypatch.setattr(helper.subprocess, "run", timed_out)
    with pytest.raises(TimeoutError, match="source_stat_timeout"):
        helper._source_file_size_quick("synthetic-stale.pdf")


def test_case_folder_entries_keep_root_relative_paths_across_three_levels(tmp_path: Path, monkeypatch):
    from api.osc import utils

    base = tmp_path / "案件資料夾"
    target = base / "04_閱卷資料" / "20260715"
    grandchild = target / "手機翻拍卷宗照片"
    grandchild.mkdir(parents=True)
    (target / "卷證.pdf").write_bytes(b"%PDF")

    monkeypatch.setattr(utils, "_osc_is_safe_local_path", lambda *_args, **_kwargs: True)
    listing = utils._osc_folder_entries(str(base), "04_閱卷資料/20260715")

    assert listing["ok"] is True
    assert listing["current_relative_path"] == "04_閱卷資料/20260715"
    assert listing["parent_relative_path"] == "04_閱卷資料"
    assert [entry["relative_path"] for entry in listing["entries"]] == [
        "04_閱卷資料/20260715/手機翻拍卷宗照片",
        "04_閱卷資料/20260715/卷證.pdf",
    ]


def test_case_folder_entries_returns_bounded_error_when_nas_metadata_times_out(tmp_path: Path, monkeypatch):
    from api.osc import utils

    base = tmp_path / "案件資料夾"
    base.mkdir()
    monkeypatch.setattr(utils, "_osc_is_safe_local_path", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(utils, "_osc_isdir_quick", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(utils, "_osc_path_needs_fs_timeout", lambda *_args, **_kwargs: True)

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["python"], timeout=1.5)

    monkeypatch.setattr(utils.subprocess, "run", timed_out)
    listing = utils._osc_folder_entries(str(base), "04_閱卷資料")

    assert listing["ok"] is False
    assert "timed out" in listing["error"]


def test_folder_browse_uses_helper_when_in_process_listing_fails(tmp_path: Path):
    client = _client()
    case_dir = tmp_path / "案件資料夾"
    case_dir.mkdir()

    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True), \
         patch("api.blueprints.osc_files._browse_entries_with_scandir", side_effect=OSError("smb list failed")), \
         patch(
             "api.blueprints.osc_files._browse_entries_with_helper",
             return_value=(
                 [{"name": "01_法扶資料", "relative_path": "01_法扶資料", "type": "dir"}],
                 [{"name": "卷證.pdf", "relative_path": "卷證.pdf", "type": "file"}],
                 0,
             ),
         ):
        r = client.get("/api/osc/folders/browse", query_string={"base_path": str(case_dir)})

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["source"] == "folder_helper"
    assert [item["name"] for item in data["folders"]] == ["01_法扶資料"]
    assert [item["name"] for item in data["files"]] == ["卷證.pdf"]


def test_listdir_metadata_falls_back_when_nas_helper_is_unavailable(tmp_path: Path, monkeypatch):
    import api.blueprints.osc_files as mod

    (tmp_path / "卷證.pdf").write_bytes(b"%PDF")
    calls = {"helper": 0, "fallback": 0}

    def unavailable_helper(path, timeout=0):
        calls["helper"] += 1
        raise OSError("[Errno 61] Connection refused")

    def subprocess_fallback(path, timeout=0):
        calls["fallback"] += 1
        assert timeout == 7.0
        return [{"name": "卷證.pdf", "is_dir": False, "size": 4, "mtime": 1}]

    monkeypatch.setattr(mod, "_is_network_nas_path", lambda _path: True)
    monkeypatch.setattr(mod, "_osc_shell_nas_helper_request", unavailable_helper)
    monkeypatch.setattr(mod, "_listdir_with_metadata_via_subprocess", subprocess_fallback)

    rows = mod._listdir_with_metadata_with_retry(str(tmp_path), max_attempts=1)

    assert rows == [{"name": "卷證.pdf", "is_dir": False, "size": 4, "mtime": 1}]
    assert calls == {"helper": 1, "fallback": 1}


def test_listdir_metadata_cache_coalesces_tree_and_browse_reads(tmp_path: Path, monkeypatch):
    import api.blueprints.osc_files as mod

    calls = []
    with mod._OSC_NAS_METADATA_CACHE_LOCK:
        mod._OSC_NAS_METADATA_CACHE.clear()

    def helper_listing(path, timeout=0):
        calls.append((path, timeout))
        return [{"name": "卷證.pdf", "is_dir": False, "size": 4, "mtime": 1}]

    monkeypatch.setattr(mod, "_is_network_nas_path", lambda _path: True)
    monkeypatch.setattr(mod, "_osc_shell_nas_helper_request", helper_listing)
    monkeypatch.setattr(mod, "_OSC_NAS_METADATA_CACHE_FRESH_SECONDS", 30.0)

    first = mod._listdir_with_metadata_with_retry(str(tmp_path))
    second = mod._listdir_with_metadata_with_retry(str(tmp_path))

    assert first == second
    assert len(calls) == 1


def test_listdir_metadata_force_refresh_bypasses_gateway_cache(tmp_path: Path, monkeypatch):
    import api.blueprints.osc_files as mod

    calls = []
    with mod._OSC_NAS_METADATA_CACHE_LOCK:
        mod._OSC_NAS_METADATA_CACHE.clear()

    def helper_listing(path, timeout=0, *, force_refresh=False):
        calls.append(force_refresh)
        return [{"name": "卷證.pdf", "is_dir": False, "size": 4, "mtime": len(calls)}]

    monkeypatch.setattr(mod, "_is_network_nas_path", lambda _path: True)
    monkeypatch.setattr(mod, "_osc_shell_nas_helper_request", helper_listing)

    mod._listdir_with_metadata_with_retry(str(tmp_path))
    refreshed = mod._listdir_with_metadata_with_retry(str(tmp_path), force_refresh=True)

    assert calls == [False, True]
    assert refreshed[0]["mtime"] == 2


def test_listdir_metadata_uses_recent_success_when_helper_fails(tmp_path: Path, monkeypatch):
    import api.blueprints.osc_files as mod

    key = os.path.realpath(str(tmp_path))
    rows = [{"name": "既有卷證.pdf", "is_dir": False, "size": 4, "mtime": 1}]
    with mod._OSC_NAS_METADATA_CACHE_LOCK:
        mod._OSC_NAS_METADATA_CACHE.clear()
        mod._OSC_NAS_METADATA_CACHE[key] = (time.monotonic() - 10.0, rows)

    monkeypatch.setattr(mod, "_is_network_nas_path", lambda _path: True)
    monkeypatch.setattr(mod, "_OSC_NAS_METADATA_CACHE_FRESH_SECONDS", 1.0)
    monkeypatch.setattr(mod, "_OSC_NAS_METADATA_CACHE_STALE_SECONDS", 60.0)
    monkeypatch.setattr(
        mod,
        "_osc_shell_nas_helper_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("helper unavailable")),
    )
    monkeypatch.setattr(
        mod,
        "_listdir_with_metadata_via_subprocess",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stale cache must avoid another SMB call")),
    )

    assert mod._listdir_with_metadata_with_retry(str(tmp_path)) == rows


def test_network_browse_does_not_repeat_same_failed_helper_chain(tmp_path: Path, monkeypatch):
    client = _client()
    case_dir = tmp_path / "案件資料夾"
    case_dir.mkdir()

    from api.blueprints import osc_files as mod

    helper_calls = []

    def failed_helper(*_args, **_kwargs):
        helper_calls.append(True)
        raise OSError("bounded helper failure")

    monkeypatch.setattr(mod, "_is_network_nas_path", lambda _path: True)
    monkeypatch.setattr(mod, "_browse_entries_with_helper", failed_helper)
    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True), \
         patch("api.blueprints.osc_files._osc_isdir_quick", return_value=True):
        response = client.get(
            "/api/osc/folders/browse",
            query_string={"base_path": str(case_dir)},
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.get_json()["error"] == "listdir_failed"
    assert len(helper_calls) == 1


def test_folder_tree_hides_listdir_timeout_details_from_ui(tmp_path: Path, monkeypatch):
    client = _client()
    case_dir = tmp_path / "案件資料夾"
    case_dir.mkdir()

    from api.blueprints import osc_files as mod

    monkeypatch.setattr(mod, "_dir_metadata_map", lambda _path: (_ for _ in ()).throw(
        OSError("listdir metadata timed out: 2.5s trace_id=private")
    ))
    with patch("api.blueprints.osc_files._resolve_target_dir", return_value=str(case_dir)), \
         patch("api.blueprints.osc_files._osc_is_safe_local_path", return_value=True):
        response = client.get("/api/osc/folders/tree", query_string={"base_path": str(case_dir)})

    assert response.status_code == 503
    assert response.get_json() == {
        "ok": False,
        "error": "listdir_failed",
        "message": "暫時無法讀取資料夾。請稍後重新整理，或確認 NAS 連線。",
    }


def test_osc_listdir_quick_uses_subprocess_fallback_after_timeout(tmp_path: Path, monkeypatch):
    import api.osc.utils as utils

    (tmp_path / "子資料夾").mkdir()
    (tmp_path / "卷證.pdf").write_bytes(b"%PDF")
    real_listdir = utils.os.listdir

    def slow_listdir(path):
        time.sleep(0.5)
        return real_listdir(path)

    monkeypatch.setattr(utils, "_osc_path_needs_fs_timeout", lambda _path: True)
    monkeypatch.setattr(utils, "_osc_finder_list_folder", lambda _path: None)
    monkeypatch.setattr(utils.os, "listdir", slow_listdir)

    names = utils._osc_listdir_quick(str(tmp_path), timeout=0.01)

    assert set(names) == {"子資料夾", "卷證.pdf"}


def test_share_gateway_forwards_mobile_range_without_session_headers():
    from scripts.share_gateway import build_upstream_headers

    headers = build_upstream_headers(
        {
            "User-Agent": "Mobile Safari",
            "Range": "bytes=0-1023",
            "If-Range": '"etag"',
            "Cookie": "session=secret",
            "Authorization": "Bearer secret",
        },
        "203.0.113.10",
    )

    assert headers["User-Agent"] == "Mobile Safari"
    assert headers["Range"] == "bytes=0-1023"
    assert headers["If-Range"] == '"etag"'
    assert headers["X-Paperclip-Share-Gateway"] == "1"
    assert "Cookie" not in headers
    assert "Authorization" not in headers
