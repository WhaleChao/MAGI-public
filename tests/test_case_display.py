from __future__ import annotations

from api.case_display import display_case_label, display_client_name, folder_client_name


def test_display_client_name_prefers_case_folder_for_likely_typo():
    record = {
        "case_number": "2025-0002",
        "client_name": "遊秀鈴",
        "folder_path": "/案件/法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死",
    }

    assert display_client_name(record) == "游秀鈴"


def test_folder_client_name_works_from_subfolder_or_file_path():
    record = {
        "case_number": "2026-0045",
        "dst": "/案件/法扶案件/行政/2026-0045-李秀英-一審-勞工保險爭議/09_閱卷/卷宗.pdf",
    }

    assert folder_client_name(record) == "李秀英"


def test_folder_client_name_works_when_case_number_is_court_number():
    record = {
        "case_number": "115年度勞簡字第1號",
        "folder_path": "/案件/法扶案件/行政/2026-0045-李秀英-一審-勞工保險爭議/09_閱卷/卷宗.pdf",
    }

    assert folder_client_name(record) == "李秀英"


def test_display_case_label_uses_folder_name_before_raw_placeholder():
    record = {
        "case_number": "2026-0044",
        "client_name": "李○毅",
        "court_case_number": "115年度偵字第123號",
        "folder_path": "/案件/法扶案件/刑事/2026-0044-李子毅-偵查-傷害",
    }

    assert display_case_label(record) == "李子毅｜115年度偵字第123號"
