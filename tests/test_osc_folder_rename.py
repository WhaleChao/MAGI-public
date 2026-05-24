# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from flask import Flask
from flask_login import LoginManager, UserMixin


def _login_app() -> Flask:
    app = Flask(__name__)
    app.config.update(TESTING=True, LOGIN_DISABLED=True)
    app.secret_key = "test"
    login = LoginManager()
    login.init_app(app)

    class TestUser(UserMixin):
        id = "test-user"

    @login.user_loader
    def _load_user(_user_id):
        return TestUser()

    return app


def test_case_root_folder_rename_updates_case_path_and_references(tmp_path, monkeypatch):
    from api.blueprints import osc_cases as mod
    from api.blueprints.osc_cases import osc_bp

    app = _login_app()
    app.register_blueprint(osc_bp)

    old_folder = tmp_path / "2026-0001-王小眀-一審-給付"
    old_folder.mkdir()
    (old_folder / "note.txt").write_text("case", encoding="utf-8")
    new_name = "2026-0001-王小明-一審-給付"
    new_folder = tmp_path / new_name
    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        if fetch == "one":
            return {
                "id": "case-1",
                "case_number": "2026-0001",
                "client_name": "王小明",
                "status": "進行中",
                "legal_aid_status": "",
                "folder_path": str(old_folder),
            }, None
        return {"rowcount": 1}, None

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)
    monkeypatch.setattr(mod, "_osc_is_safe_local_path", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        mod,
        "_osc_effective_case_folder_for_row",
        lambda row, update_db=False: {"folder_path": str(old_folder), "local_folder": str(old_folder), "source": "db_or_guess", "updated": False},
    )
    monkeypatch.setattr(mod, "_get_translate_local_path_to_canonical", lambda: (lambda p: str(p)))

    resp = app.test_client().post(
        "/api/osc/cases/case-1/rename-folder",
        json={"new_name": new_name},
    )
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["ok"] is True
    assert not old_folder.exists()
    assert (new_folder / "note.txt").read_text(encoding="utf-8") == "case"
    expected_db_path = str(new_folder).replace("/", "\\")
    assert data["folder_path"] == expected_db_path
    assert any("UPDATE cases SET folder_path=%s" in sql and params[0] == expected_db_path for sql, params, _ in calls)
    assert any("document_index" in sql and "file_path" in sql for sql, _params, _fetch in calls)


def test_generic_folder_rename_updates_indexed_paths(tmp_path, monkeypatch):
    from api.blueprints import osc_files as mod
    from api.blueprints.osc_files import osc_files_bp

    app = _login_app()
    app.register_blueprint(osc_files_bp)

    case_root = tmp_path / "2026-0001-王小明-一審-給付"
    typo = case_root / "06_證據資枓"
    typo.mkdir(parents=True)
    (typo / "證物.pdf").write_bytes(b"%PDF")
    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        return {"rowcount": 1}, None

    monkeypatch.setattr(mod, "_resolve_target_dir", lambda raw: str(case_root))
    monkeypatch.setattr(mod, "_osc_is_safe_local_path", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(mod, "_osc_exec", fake_exec)

    resp = app.test_client().post(
        "/api/osc/folders/rename",
        json={
            "base_path": str(case_root),
            "relative_path": "06_證據資枓",
            "new_name": "06_證據資料",
        },
    )
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["ok"] is True
    assert not typo.exists()
    assert (case_root / "06_證據資料" / "證物.pdf").is_file()
    assert data["new_relative_path"] == "06_證據資料"
    assert data["path_references"]["updated"] >= 1
    assert any("case_todos" in sql and "source_file" in sql for sql, _params, _fetch in calls)
