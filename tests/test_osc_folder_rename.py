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


def test_case_folder_reconcile_reports_repair_for_stale_db_path(monkeypatch):
    from api.blueprints import osc_cases as mod

    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        return {"rowcount": 1}, None

    row = {
        "id": "case-53",
        "case_number": "2026-0053",
        "client_name": "劉玲均",
        "case_category": "一般案件",
        "case_type": "民事",
        "case_stage": "一審",
        "case_reason": "確認本票債權不存在",
        "status": "進行中",
        "legal_aid_status": "",
        "folder_path": r"Z:\lumi63181107\01_案件\一般案件\民事\2026-0053-劉玲昀-一審-確認本票債權不存在",
    }

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)
    monkeypatch.setattr(mod, "_osc_resolve_existing_local_path", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(mod, "_osc_find_sibling_case_folder", lambda *_args, **_kwargs: "")

    resolved = mod._osc_effective_case_folder_for_row(row, update_db=True)

    assert resolved["updated"] is False
    assert resolved["source"] == "metadata_expected"
    assert "劉玲均" in resolved["folder_path"]
    assert "劉玲昀" not in resolved["folder_path"]
    assert resolved["pending_repair"] is True
    assert resolved["suggested_folder_path"] == resolved["folder_path"]
    assert not any("UPDATE cases SET folder_path=%s" in sql for sql, _params, _fetch in calls)
    assert not any("document_index" in sql and "file_path" in sql for sql, _params, _fetch in calls)


def test_expected_case_folder_name_hydrates_missing_metadata(monkeypatch):
    from api.blueprints import osc_cases as mod

    def fake_exec(sql, params=(), fetch="none"):
        if fetch == "one" and "FROM cases" in sql:
            return {
                "case_number": "2026-0053",
                "client_name": "劉玲均",
                "case_category": "一般案件",
                "case_type": "民事",
                "case_stage": "一審",
                "case_reason": "確認本票債權不存在",
            }, None
        return {"rowcount": 0}, None

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)

    name = mod._osc_expected_case_folder_name({
        "id": "case-53",
        "case_number": "2026-0053",
        "client_name": "劉玲均",
    })

    assert name == "2026-0053-劉玲均-一審-確認本票債權不存在"


def test_case_folder_reconcile_reports_repair_without_renaming_existing_local_folder(tmp_path, monkeypatch):
    from api.blueprints import osc_cases as mod

    old_folder = tmp_path / "2026-0053-劉玲昀-一審-確認本票債權不存在"
    old_folder.mkdir()
    (old_folder / "note.txt").write_text("keep", encoding="utf-8")
    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        return {"rowcount": 1}, None

    row = {
        "id": "case-53",
        "case_number": "2026-0053",
        "client_name": "劉玲均",
        "case_category": "一般案件",
        "case_type": "民事",
        "case_stage": "一審",
        "case_reason": "確認本票債權不存在",
        "status": "進行中",
        "legal_aid_status": "",
        "folder_path": str(old_folder),
    }

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)
    monkeypatch.setattr(mod, "_osc_is_safe_local_path", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(mod, "_get_translate_local_path_to_canonical", lambda: (lambda p: str(p)))
    monkeypatch.setattr(mod, "_osc_find_sibling_case_folder", lambda *_args, **_kwargs: "")

    resolved = mod._osc_reconcile_case_folder_name(row, str(old_folder), str(old_folder), update_db=True)

    new_folder = tmp_path / "2026-0053-劉玲均-一審-確認本票債權不存在"
    assert resolved["updated"] is False
    assert resolved["source"] == "metadata_expected"
    assert resolved["pending_repair"] is True
    assert resolved["suggested_local_folder"] == str(new_folder)
    assert old_folder.exists()
    assert not new_folder.exists()
    assert (old_folder / "note.txt").read_text(encoding="utf-8") == "keep"
    assert not any("UPDATE cases SET folder_path=%s" in sql for sql, _params, _fetch in calls)


def test_path_reference_replace_ignores_missing_optional_schema(monkeypatch):
    from api.osc import utils as mod

    def fake_exec(sql, params=(), fetch="none"):
        if "court_judgments" in sql:
            raise RuntimeError("1054 (42S22): Unknown column 'source_file' in 'SET'")
        return {"rowcount": 0}, None

    monkeypatch.setattr(mod, "translate_local_path_to_canonical", lambda p: p)

    result = mod._osc_replace_path_prefix_references(
        r"Z:\lumi63181107\01_案件\一般案件\民事\2026-0053-舊名",
        r"Z:\lumi63181107\01_案件\一般案件\民事\2026-0053-新名",
        exec_fn=fake_exec,
    )

    assert result["attempted"] >= 1
    assert result["errors"] == []
