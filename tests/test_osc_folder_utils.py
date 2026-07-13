from api.osc import case_folder_schema as schema
from api.osc.folder_utils import (
    SUBFOLDERS,
    build_full_case_path,
    create_folder_structure,
    resolve_type_folder,
)
from casper_ecosystem.law_firm_orchestrators.osc import folder_utils as legacy_folder_utils
from api.case_path_mapper import local_synology_path_candidates


def test_attached_civil_folder_stays_under_civil_even_with_criminal_words():
    assert resolve_type_folder("", "一審", "刑事附帶民事") == "民事"
    assert resolve_type_folder("民事", "一審", "刑事附帶民事") == "民事"


def test_build_full_case_path_uses_civil_folder_for_attached_civil_laf_case():
    path = build_full_case_path(
        "/cases",
        "2026-0037",
        "馬碌枝Uli Mangququ",
        case_type="",
        case_category="法律扶助案件",
        case_stage="一審",
        case_reason="刑事附帶民事",
    )
    assert "/法扶案件/民事/" in path


def test_build_full_case_path_strips_laf_suspected_reason_prefix():
    path = build_full_case_path(
        "/cases",
        "2026-0071",
        "李滿金",
        case_type="刑事",
        case_category="法律扶助案件",
        case_stage="一審",
        case_reason="涉詐欺、洗錢防制法",
    )
    assert path.endswith("/法扶案件/刑事/2026-0071-李滿金-一審-詐欺、洗錢防制法")
    assert "-涉詐欺" not in path


def test_build_full_case_path_keeps_substantive_foreign_law_reason():
    path = build_full_case_path(
        "/cases",
        "2026-0072",
        "測試",
        case_type="民事",
        case_category="一般案件",
        case_stage="一審",
        case_reason="涉外民事法律適用法",
    )
    assert path.endswith("/一般案件/民事/2026-0072-測試-一審-涉外民事法律適用法")


def test_case_creation_subfolders_come_from_shared_schema():
    assert SUBFOLDERS == {category: list(folders) for category, folders in schema.CASE_SUBFOLDERS.items()}
    assert legacy_folder_utils.SUBFOLDERS == SUBFOLDERS


def test_case_creation_uses_canonical_judgment_folder_not_legacy_alias(tmp_path):
    result = create_folder_structure(str(tmp_path / "case"), "法律扶助案件")

    assert result["ok"] is True
    assert schema.judgment_folder_name(10) in result["subfolders"]
    assert schema.legacy_judgment_folder_name(10) not in result["subfolders"]
    assert (tmp_path / "case" / schema.judgment_folder_name(10)).is_dir()
    assert not (tmp_path / "case" / schema.legacy_judgment_folder_name(10)).exists()


def test_all_schema_case_subfolders_keep_old_judgment_name_as_alias_only():
    for folders in schema.CASE_SUBFOLDERS.values():
        assert not any(schema.strip_number_prefix(name) == schema.LEGACY_JUDGMENT_FOLDER_LABEL for name in folders)
        assert any(schema.strip_number_prefix(name) == schema.JUDGMENT_FOLDER_LABEL for name in folders)


def test_closing_folder_names_are_schema_owned():
    assert schema.closing_folder_names() == ("03_結案資料", "04_結案資料", "結案資料")


def test_cloudstorage_homes_path_also_maps_to_smb_volume_candidate():
    path = "/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件/法扶案件/民事/測試案/卷證.pdf"
    candidates = local_synology_path_candidates(path)
    assert "/Volumes/homes/home/01_案件/法扶案件/民事/測試案/卷證.pdf" in candidates


def test_cloudstorage_homes_path_prefers_smb_before_cloud_placeholder(monkeypatch):
    import api.case_path_mapper as mapper

    mapper._ACTIVE_SMB_ROOT_CACHE["roots"] = None
    mapper._ACTIVE_SMB_ROOT_CACHE["expires"] = 0
    monkeypatch.setattr(mapper, "_discover_active_smb_share_roots", lambda: ["/Volumes/homes/lumi63181107"])

    path = "/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件/法扶案件/民事/測試案/卷證.pdf"
    candidates = mapper.local_synology_path_candidates(path)

    assert candidates[0] == "/Volumes/homes/lumi63181107/01_案件/法扶案件/民事/測試案/卷證.pdf"
    assert path in candidates
    assert candidates.index(path) > candidates.index("/Volumes/homes/lumi63181107/01_案件/法扶案件/民事/測試案/卷證.pdf")


def test_write_resolution_rejects_cloudstorage_when_smb_unavailable(monkeypatch):
    import api.case_path_mapper as mapper

    path = "/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件/法扶案件/民事/測試案/卷證.pdf"
    mapper._ACTIVE_SMB_ROOT_CACHE["roots"] = None
    mapper._ACTIVE_SMB_ROOT_CACHE["expires"] = 0
    monkeypatch.setattr(mapper, "_discover_active_smb_share_roots", lambda: [])
    monkeypatch.setattr(mapper, "_is_dir_accessible", lambda candidate: candidate == path)

    assert mapper.local_case_path_candidates(path, for_write=True) == []
    result = mapper.resolve_case_path_for_write(path)
    assert result["ok"] is False
    assert result["status"] == "pending"
    assert result["reason"] == "mount_required"
    assert result["error"] == "case_path_write_mount_required"


def test_windows_home_path_prefers_mounted_homes_account(monkeypatch):
    import api.case_path_mapper as mapper

    mapper._ACTIVE_SMB_ROOT_CACHE["roots"] = None
    mapper._ACTIVE_SMB_ROOT_CACHE["expires"] = 0
    monkeypatch.setattr(
        mapper,
        "_mount_output_lines",
        lambda: ["//lumi63181107@192.168.1.3/homes on /Volumes/homes (smbfs, mounted by ai)"],
    )
    monkeypatch.setattr(
        mapper,
        "_is_dir_accessible",
        lambda path: str(path).rstrip("/") in {
            "/Volumes/homes/lumi63181107",
            "/Volumes/homes/lumi63181107/01_案件",
        },
    )

    path = "Z:/home/01_案件/法扶案件/刑事/測試案/卷證.pdf"
    candidates = mapper.local_synology_path_candidates(path)

    assert candidates[0] == "/Volumes/homes/lumi63181107/01_案件/法扶案件/刑事/測試案/卷證.pdf"
