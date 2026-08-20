from api.case_display import display_client_name, normalize_person_name
from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFCaseTypeParser


def _record(master: str, folder: str) -> dict[str, str]:
    return {
        "case_number": "2026-9998",
        "client_name": master,
        "folder_path": f"cases/2026-9998-{folder}-一審-測試",
    }


def test_laf_master_name_is_not_rewritten_by_folder_variant() -> None:
    assert display_client_name(_record("林于翔", "林於翔")) == "林于翔"
    assert display_client_name(_record("游秀鈴", "遊秀鈴")) == "游秀鈴"
    assert display_client_name(_record("王台銘", "王臺銘")) == "王台銘"


def test_variant_normalisation_is_lookup_only() -> None:
    assert normalize_person_name("遊秀鈴") == normalize_person_name("游秀鈴")
    assert normalize_person_name("王臺銘") == normalize_person_name("王台銘")


def test_folder_name_only_fills_missing_or_unusable_master() -> None:
    assert display_client_name(_record("", "林于翔")) == "林于翔"
    assert display_client_name(_record("當事人", "王台銘")) == "王台銘"


def test_laf_email_party_spelling_is_preserved_verbatim() -> None:
    subjects = {
        "林于翔": "【法扶花蓮分會派案通知】林于翔-1150814-H-001-刑事第一審辯護-竊盜",
        "游秀鈴": "【法扶臺北分會派案通知】游秀鈴-1150814-A-002-刑事第二審辯護-過失致死",
        "王台銘": "【法扶臺北分會派案通知】王台銘-1150814-A-003-消費者債務清理事件-清算",
    }
    for expected, subject in subjects.items():
        parsed = LAFCaseTypeParser.parse_subject(subject)
        assert parsed is not None
        assert parsed.client_name == expected
