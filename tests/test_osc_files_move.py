# -*- coding: utf-8 -*-
"""Paperclip 檔案管理：移動與回收區操作回歸測試。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch
import builtins
import errno

from flask import Flask
from flask_login import LoginManager, UserMixin


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
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError(errno.EDEADLK, "Resource deadlock avoided")
            return b"%PDF-share"

    def fake_open(path, mode="r", *args, **kwargs):
        if str(path) == str(src) and mode == "rb":
            return FakeFile()
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(mod, "open", fake_open, raising=False)

    assert mod._read_file_with_retry(str(src)) == b"%PDF-share"
    assert attempts["n"] >= 1


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
