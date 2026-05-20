from pathlib import Path

from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFAutomationManager


def _automation_for_folder_guard(logs):
    auto = LAFAutomationManager.__new__(LAFAutomationManager)
    auto.db_manager = object()
    auto.log = logs.append
    return auto


def test_recreate_existing_case_skips_closed_archive_path(tmp_path):
    logs = []
    auto = _automation_for_folder_guard(logs)

    result = auto._recreate_folder_for_existing_case(
        db_case={
            "case_number": "2025-0033",
            "client_name": "中央選舉委員會",
            "status": "已結案",
            "legal_aid_status": "",
            "folder_path": r"Y:\lumi\03_工作資料\10_結案\一般案件\行政\2025-0033-中央選舉委員會-一審-公民投票法",
        },
        case_type="行政",
        case_stage="一審",
        case_reason="公民投票法",
        target_root=str(tmp_path),
    )

    assert result is None
    assert not any(tmp_path.rglob("2025-0033-*"))
    assert any("跳過進行中資料夾重建" in line for line in logs)


def test_discover_closed_archive_folder_by_case_number(tmp_path):
    logs = []
    auto = _automation_for_folder_guard(logs)
    archive_folder = (
        tmp_path
        / "法扶案件"
        / "消費者債務清理"
        / "2025-0051-莊宸銘-消費者債務清理-更生"
    )
    (archive_folder / "03_結案資料").mkdir(parents=True)

    result = auto._discover_case_folder_by_number("2025-0051", [str(tmp_path)], max_depth=3)

    assert result == str(archive_folder)


def test_recreate_existing_case_allows_active_case(tmp_path):
    logs = []
    auto = _automation_for_folder_guard(logs)

    result = auto._recreate_folder_for_existing_case(
        db_case={
            "case_number": "2026-0099",
            "client_name": "測試當事人",
            "status": "進行中",
            "legal_aid_status": "進行中",
            "folder_path": r"Z:\lumi63181107\01_案件\法扶案件\行政\2026-0099-測試當事人-一審-測試案由",
        },
        case_type="行政",
        case_stage="一審",
        case_reason="測試案由",
        target_root=str(tmp_path),
    )

    assert result
    created = Path(result)
    assert created.is_dir()
    assert created.name == "2026-0099-測試當事人-一審-測試案由"
    assert (created / "01_法扶資料" / ".gitkeep").exists()
