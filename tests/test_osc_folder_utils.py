from casper_ecosystem.law_firm_orchestrators.osc.folder_utils import (
    build_full_case_path,
    resolve_type_folder,
)


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
