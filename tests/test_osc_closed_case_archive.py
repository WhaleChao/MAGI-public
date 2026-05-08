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
