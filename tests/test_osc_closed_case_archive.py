from __future__ import annotations

import pytest


def test_closed_case_status_detection():
    from api.blueprints.osc_cases import _osc_is_closed_case_status

    assert _osc_is_closed_case_status("已結案")
    assert _osc_is_closed_case_status("已結案，待報結")
    assert _osc_is_closed_case_status("closed")
    assert not _osc_is_closed_case_status("進行中")


def test_auto_archive_closed_case_moves_folder(tmp_path, monkeypatch):
    from api.blueprints import osc_cases as mod

    source = tmp_path / "01_案件" / "A001_王小明"
    source.mkdir(parents=True)
    (source / "note.txt").write_text("case file", encoding="utf-8")
    archive = tmp_path / "99_結案案件"
    archive.mkdir()
    updates = []

    def fake_exec(sql, params=(), fetch="none"):
        if fetch == "one":
            return {
                "id": "case-1",
                "case_number": "A001",
                "client_name": "王小明",
                "status": "已結案",
                "folder_path": str(source),
            }, None
        updates.append((sql, params, fetch))
        return {"rowcount": 1}, None

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)
    monkeypatch.setattr(mod, "_osc_get_closed_archive_base", lambda: str(archive))
    monkeypatch.setattr(mod, "_osc_local_path_candidates", lambda raw: [str(raw)])
    monkeypatch.setattr(mod, "_osc_norm_path", lambda raw: str(raw))

    result = mod._osc_auto_archive_closed_case("case-1")

    target = archive / source.name
    assert result["ok"] is True
    assert result["reason"] == "moved"
    assert target.exists()
    assert (target / "note.txt").read_text(encoding="utf-8") == "case file"
    assert not source.exists()
    assert updates and updates[-1][1][0] == str(target)


def test_auto_archive_closed_case_merges_existing_target(tmp_path, monkeypatch):
    from api.blueprints import osc_cases as mod

    source = tmp_path / "01_案件" / "A001_王小明"
    (source / "04_我方歷次書狀").mkdir(parents=True)
    (source / "04_我方歷次書狀" / "書狀.pdf").write_text("new pleading", encoding="utf-8")
    archive = tmp_path / "99_結案案件"
    target = archive / source.name
    target.mkdir(parents=True)
    (target / "既有.txt").write_text("closed file", encoding="utf-8")
    updates = []

    def fake_exec(sql, params=(), fetch="none"):
        if fetch == "one":
            return {
                "id": "case-1",
                "case_number": "A001",
                "client_name": "王小明",
                "status": "已結案",
                "folder_path": str(source),
            }, None
        updates.append((sql, params, fetch))
        return {"rowcount": 1}, None

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)
    monkeypatch.setattr(mod, "_osc_get_closed_archive_base", lambda: str(archive))
    monkeypatch.setattr(mod, "_osc_local_path_candidates", lambda raw: [str(raw)])
    monkeypatch.setattr(mod, "_osc_norm_path", lambda raw: str(raw))

    result = mod._osc_auto_archive_closed_case("case-1")

    assert result["ok"] is True
    assert result["reason"] == "merged_existing_target"
    assert not source.exists()
    assert target.exists()
    assert (target / "既有.txt").read_text(encoding="utf-8") == "closed file"
    assert (target / "04_我方歷次書狀" / "書狀.pdf").read_text(encoding="utf-8") == "new pleading"
    assert updates and updates[-1][1][0] == str(target)


def test_auto_archive_closed_case_preserves_osc_category_path(tmp_path, monkeypatch):
    from api.blueprints import osc_cases as mod

    source = tmp_path / "01_案件" / "法扶案件" / "刑事" / "2025-0010-劉信義-一審-殺人"
    source.mkdir(parents=True)
    (source / "note.txt").write_text("case file", encoding="utf-8")
    archive = tmp_path / "10_結案"
    archive.mkdir()
    updates = []

    def fake_exec(sql, params=(), fetch="none"):
        if fetch == "one":
            return {
                "id": "case-1",
                "case_number": "2025-0010",
                "client_name": "劉信義",
                "status": "已結案",
                "folder_path": str(source),
            }, None
        updates.append((sql, params, fetch))
        return {"rowcount": 1}, None

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)
    monkeypatch.setattr(mod, "_osc_get_closed_archive_base", lambda: str(archive))
    monkeypatch.setattr(mod, "_osc_local_path_candidates", lambda raw: [str(raw)])
    monkeypatch.setattr(mod, "_osc_norm_path", lambda raw: str(raw))

    result = mod._osc_auto_archive_closed_case("case-1")

    target = archive / "法扶案件" / "刑事" / source.name
    assert result["ok"] is True
    assert target.exists()
    assert (target / "note.txt").read_text(encoding="utf-8") == "case file"
    assert not source.exists()
    assert updates and updates[-1][1][0] == str(target)


def test_closed_case_folder_resolves_archive_when_db_path_is_stale(tmp_path, monkeypatch):
    from api.blueprints import osc_cases as mod

    active_root = tmp_path / "01_案件"
    archive_root = tmp_path / "10_結案"
    stale = active_root / "法扶案件" / "消債" / "2025-0051-莊宸銘"
    archived = archive_root / "法扶案件" / "消債" / "2025-0051-莊宸銘"
    stale.mkdir(parents=True)
    archived.mkdir(parents=True)
    updates = []

    monkeypatch.setattr(
        mod,
        "_get_preferred_case_roots",
        lambda include_closed=False: [str(active_root), str(archive_root)] if include_closed else [str(active_root)],
    )
    monkeypatch.setattr(mod, "_osc_archive_local_base", lambda: (str(archive_root), str(archive_root)))
    monkeypatch.setattr(mod, "_get_translate_local_path_to_canonical", lambda: (lambda p: str(p)))

    def fake_exec(sql, params=(), fetch="none"):
        updates.append((sql, params, fetch))
        return {"rowcount": 1}, None

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)

    result = mod._osc_effective_case_folder_for_row(
        {
            "id": "case-1",
            "case_number": "2025-0051",
            "client_name": "莊宸銘",
            "status": "已結案",
            "folder_path": str(stale),
        },
        update_db=True,
    )

    assert result["source"] == "closed_archive"
    assert result["local_folder"] == str(archived)
    assert result["folder_path"].replace("\\", "/") == str(archived)
    assert result["updated"] is True
    assert updates and updates[-1][1][0] == str(archived)


def test_closed_case_resolution_does_not_match_same_client_other_procedure(tmp_path, monkeypatch):
    from api.blueprints import osc_cases as mod

    active_root = tmp_path / "01_案件"
    archive_root = tmp_path / "10_結案"
    active_debt = active_root / "法扶案件" / "消費者債務清理" / "2025-0067-宣愛華Ayka lku-消費者債務清理-更生"
    closed_guardianship = archive_root / "法扶案件" / "民事" / "2025-0031-宣愛華Ayka lku-一審-改定未成年子女監護"
    active_debt.mkdir(parents=True)
    closed_guardianship.mkdir(parents=True)

    monkeypatch.setattr(
        mod,
        "_get_preferred_case_roots",
        lambda include_closed=False: [str(active_root), str(archive_root)] if include_closed else [str(active_root)],
    )
    monkeypatch.setattr(mod, "_osc_archive_local_base", lambda: (str(archive_root), str(archive_root)))
    monkeypatch.setattr(mod, "_get_translate_local_path_to_canonical", lambda: (lambda p: str(p)))
    monkeypatch.setattr(mod, "_osc_resolve_existing_local_path", lambda raw, prefer_dir=True: str(active_debt) if raw and "2025-0067" in str(raw) else "")

    result = mod._osc_effective_case_folder_for_row(
        {
            "id": "case-1",
            "case_number": "2025-0067",
            "client_name": "宣愛華Ayka lku",
            "status": "進行中",
            "legal_aid_status": "進行中",
            "folder_path": str(active_debt),
        },
        update_db=True,
    )

    assert result["source"] == "db_or_guess"
    assert result["local_folder"] == str(active_debt)
    assert result["local_folder"] != str(closed_guardianship)


def test_folder_browser_lists_closed_archive_when_active_path_is_stale(tmp_path, monkeypatch):
    from flask import Flask
    from flask_login import LoginManager, UserMixin
    from api.blueprints import osc_cases as mod
    from api.blueprints.osc_cases import osc_bp

    app = Flask(__name__)
    app.config.update(TESTING=True, LOGIN_DISABLED=True)
    app.secret_key = "test"
    lm = LoginManager()
    lm.init_app(app)

    class _User(UserMixin):
        id = "test-user"

    @lm.user_loader
    def _load_user(_user_id):
        return _User()

    app.register_blueprint(osc_bp)

    active_root = tmp_path / "01_案件"
    archive_root = tmp_path / "10_結案"
    stale = active_root / "法扶案件" / "刑事" / "2025-0010-劉信義-一審-殺人"
    archived = archive_root / "法扶案件" / "刑事" / "2025-0010-劉信義-一審-殺人"
    archived.mkdir(parents=True)
    (archived / "note.txt").write_text("closed file", encoding="utf-8")
    updates = []

    monkeypatch.setattr(
        mod,
        "_get_preferred_case_roots",
        lambda include_closed=False: [str(active_root), str(archive_root)] if include_closed else [str(active_root)],
    )
    monkeypatch.setattr(mod, "_osc_archive_local_base", lambda: (str(archive_root), str(archive_root)))
    monkeypatch.setattr(mod, "_get_translate_local_path_to_canonical", lambda: (lambda p: str(p)))
    monkeypatch.setattr(mod, "_osc_resolve_existing_local_path", lambda raw, prefer_dir=True: str(raw) if raw and str(raw) == str(archived) else "")

    def fake_folder_entries(base_path, relative_path="", limit=240):
        return {
            "ok": True,
            "base_path": str(base_path),
            "current_path": str(base_path),
            "current_relative_path": "",
            "parent_relative_path": "",
            "entries": [{"name": "note.txt", "relative_path": "note.txt", "type": "file"}],
        }

    monkeypatch.setattr(mod, "_osc_folder_entries", fake_folder_entries)

    def fake_exec(sql, params=(), fetch="none"):
        if fetch == "one":
            return {
                "id": "case-1",
                "case_number": "2025-0010",
                "client_name": "劉信義",
                "status": "已結案",
                "folder_path": str(stale),
            }, None
        updates.append((sql, params, fetch))
        return {"rowcount": 1}, None

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)

    resp = app.test_client().get("/api/osc/cases/case-1/folder-browser")
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["folder_exists"] is True
    assert data["folder_source"] == "closed_archive"
    assert data["local_folder"] == str(archived)
    assert any(entry["name"] == "note.txt" for entry in data["entries"])
    assert updates and updates[-1][1][0] == str(archived)


def test_case_identity_rejects_ambiguous_client_name(monkeypatch):
    from api.osc import drafts

    def fake_exec(sql, params=(), fetch="none"):
        assert fetch == "all"
        return [
            {"id": "case-1", "case_number": "2025-0031", "client_name": "宣愛華Ayka lku", "case_stage": "一審", "case_reason": "改定未成年子女監護"},
            {"id": "case-2", "case_number": "2025-0067", "client_name": "宣愛華Ayka lku", "case_stage": "", "case_reason": "更生"},
        ], None

    monkeypatch.setattr(drafts, "_osc_exec", fake_exec)

    with pytest.raises(ValueError, match="ambiguous_client_name"):
        drafts._osc_get_case_identity_by_payload({"client_name": "宣愛華Ayka lku"})
