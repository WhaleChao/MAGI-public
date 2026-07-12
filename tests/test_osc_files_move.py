# -*- coding: utf-8 -*-
"""Paperclip 檔案管理：移動與回收區操作回歸測試。"""
from __future__ import annotations

from pathlib import Path
from io import BytesIO
from urllib.parse import urlencode
from unittest.mock import patch
import builtins
import errno
import time

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
        return [{"name": "卷證.pdf", "is_dir": False, "size": 4, "mtime": 1}]

    monkeypatch.setattr(mod, "_is_network_nas_path", lambda _path: True)
    monkeypatch.setattr(mod, "_osc_shell_nas_helper_request", unavailable_helper)
    monkeypatch.setattr(mod, "_listdir_with_metadata_via_subprocess", subprocess_fallback)

    rows = mod._listdir_with_metadata_with_retry(str(tmp_path), max_attempts=1)

    assert rows == [{"name": "卷證.pdf", "is_dir": False, "size": 4, "mtime": 1}]
    assert calls == {"helper": 1, "fallback": 1}


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
