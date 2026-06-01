from __future__ import annotations


def test_closed_rows_includes_closed_active_residue(monkeypatch):
    from scripts.ops import slow_archive_closed_cases as mod

    captured = {}

    def fake_exec(sql, params=(), fetch="none"):
        captured["sql"] = sql
        captured["params"] = params
        captured["fetch"] = fetch
        return (
            [
                {
                    "id": "case-1",
                    "case_number": "2025-0002",
                    "client_name": "游秀鈴",
                    "status": "已結案",
                    "legal_aid_status": "已結案，待送出",
                    "folder_path": r"Z:\lumi63181107\01_案件\法扶案件\刑事\2025-0002-游秀鈴-一審-傷害致死",
                }
            ],
            None,
        )

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)

    rows = mod._closed_rows(case_number="2025-0002", limit=1)

    assert rows and rows[0]["case_number"] == "2025-0002"
    assert "03_工作資料" in captured["sql"]
    assert "01_案件" in captured["sql"]
    assert captured["params"] == ("2025-0002", 1)


def test_active_source_for_active_path_prefers_smb_over_cloud(monkeypatch, tmp_path):
    from scripts.ops import slow_archive_closed_cases as mod

    cloud = tmp_path / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件" / "case"
    smb = tmp_path / "Volumes" / "homes" / "lumi63181107" / "01_案件" / "case"
    cloud.mkdir(parents=True)
    smb.mkdir(parents=True)

    monkeypatch.setattr(mod, "local_case_path_candidates", lambda _path: [str(cloud), str(smb)])
    monkeypatch.setattr(mod, "_is_dir_quick", lambda path: path.exists() and path.is_dir())

    row = {"folder_path": r"Z:\lumi63181107\01_案件\case", "case_number": "2025-0002"}

    assert mod._active_source_for(row) == str(smb)


def test_active_source_for_closed_path_prefers_smb_residue(monkeypatch, tmp_path):
    from scripts.ops import slow_archive_closed_cases as mod

    cloud = tmp_path / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件" / "法扶案件" / "刑事" / "case"
    smb = tmp_path / "Volumes" / "homes" / "lumi63181107" / "01_案件" / "法扶案件" / "刑事" / "case"
    cloud.mkdir(parents=True)
    smb.mkdir(parents=True)

    monkeypatch.setattr(mod, "_active_candidates_for_closed_path", lambda _path: [str(cloud), str(smb)])
    monkeypatch.setattr(mod, "_is_dir_quick", lambda path: path.exists() and path.is_dir())

    row = {
        "folder_path": r"Y:\lumi\03_工作資料\10_結案\法扶案件\刑事\case",
        "case_number": "2025-0002",
    }

    assert mod._active_source_for(row) == str(smb)
