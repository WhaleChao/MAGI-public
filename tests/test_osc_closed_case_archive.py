from __future__ import annotations


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


def test_auto_archive_closed_case_skips_existing_target(tmp_path, monkeypatch):
    from api.blueprints import osc_cases as mod

    source = tmp_path / "01_案件" / "A001_王小明"
    source.mkdir(parents=True)
    archive = tmp_path / "99_結案案件"
    target = archive / source.name
    target.mkdir(parents=True)

    def fake_exec(sql, params=(), fetch="none"):
        if fetch == "one":
            return {
                "id": "case-1",
                "case_number": "A001",
                "client_name": "王小明",
                "status": "已結案",
                "folder_path": str(source),
            }, None
        return {"rowcount": 1}, None

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)
    monkeypatch.setattr(mod, "_osc_get_closed_archive_base", lambda: str(archive))
    monkeypatch.setattr(mod, "_osc_local_path_candidates", lambda raw: [str(raw)])
    monkeypatch.setattr(mod, "_osc_norm_path", lambda raw: str(raw))

    result = mod._osc_auto_archive_closed_case("case-1")

    assert result["ok"] is False
    assert result["reason"] == "target_exists"
    assert source.exists()
    assert target.exists()
