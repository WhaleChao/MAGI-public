from casper_ecosystem.law_firm_orchestrators.osc.folder_utils import (
    build_full_case_path,
    resolve_type_folder,
)
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


def test_cloudstorage_homes_path_also_maps_to_smb_volume_candidate():
    path = "/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件/法扶案件/民事/測試案/卷證.pdf"
    candidates = local_synology_path_candidates(path)
    assert "/Volumes/homes/lumi63181107/01_案件/法扶案件/民事/測試案/卷證.pdf" in candidates
