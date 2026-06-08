from __future__ import annotations

from pathlib import Path

from scripts.ops.repair_drive_imported_case_folders import (
    cleanup_duplicate_quarantine,
    is_noncanonical_drive_folder,
    mapped_file_relative_path,
    repair_case_folder,
    repair_case_tree,
)


def test_drive_imported_alias_detection_and_mapping():
    assert is_noncanonical_drive_folder("法院裁判") is True
    assert is_noncanonical_drive_folder("開庭通知") is True
    assert is_noncanonical_drive_folder("電子筆錄") is True
    assert is_noncanonical_drive_folder("地檢署起訴書") is True
    assert is_noncanonical_drive_folder("訊問筆錄") is True
    assert is_noncanonical_drive_folder("10_判決書") is False
    assert mapped_file_relative_path("法院裁判", "a.pdf") == "09_法院通知或程序裁定/a.pdf"
    assert mapped_file_relative_path("法院裁判", "20260101 裁定.pdf") == "09_法院通知或程序裁定/20260101 裁定.pdf"
    assert mapped_file_relative_path("法院裁判", "20260101 復權裁定.pdf") == "10_判決書/20260101 復權裁定.pdf"
    assert mapped_file_relative_path("法院裁判", "偵查案件起訴書.pdf") == "10_判決書/偵查案件起訴書.pdf"
    assert mapped_file_relative_path("開庭通知", "a.pdf") == "09_法院通知或程序裁定/a.pdf"
    assert mapped_file_relative_path("法院裁定", "20260101 復權裁定.pdf") == "10_判決書/20260101 復權裁定.pdf"
    assert mapped_file_relative_path("起訴書", "20250306_聲請接續羈押理由書.pdf") == "09_法院通知或程序裁定/20250306_聲請接續羈押理由書.pdf"
    assert mapped_file_relative_path("法院資料", "起訴書/a.pdf") == "10_判決書/a.pdf"
    assert mapped_file_relative_path("電子筆錄", "b.pdf") == "08_筆錄/b.pdf"
    assert mapped_file_relative_path("訊問筆錄", "b.pdf") == "08_筆錄/b.pdf"
    assert is_noncanonical_drive_folder("游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等") is True
    assert mapped_file_relative_path(
        "游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等",
        "上訴理由一狀.pdf",
    ) == "04_我方歷次書狀/上訴理由一狀.pdf"
    assert mapped_file_relative_path(
        "李明志-1131106-I-007-消費者債務清理事件",
        "更生方案.pdf",
    ) == "04_我方歷次書狀/更生方案.pdf"


def test_repair_case_folder_moves_alias_files_without_overwrite(tmp_path: Path):
    case = tmp_path / "2025-0002-游秀鈴-一審-傷害致死"
    src = case / "法院裁判" / "判決.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"judgment")

    report = repair_case_folder(case, apply=True)

    assert report["errors"] == []
    assert (case / "10_判決書" / "判決.pdf").read_bytes() == b"judgment"
    assert not src.exists()
    assert "法院裁判" in report["alias_folders"]


def test_repair_case_folder_deletes_exact_duplicate_only_when_requested(tmp_path: Path):
    case = tmp_path / "2025-0002-游秀鈴-一審-傷害致死"
    src = case / "訊問筆錄" / "t.pdf"
    dst = case / "08_筆錄" / "t.pdf"
    src.parent.mkdir(parents=True)
    dst.parent.mkdir(parents=True)
    src.write_bytes(b"same")
    dst.write_bytes(b"same")

    dry = repair_case_folder(case, apply=False, delete_duplicate=True)
    assert dry["duplicates"][0]["action"] == "delete_duplicate"
    assert src.exists()

    applied = repair_case_folder(case, apply=True, delete_duplicate=True)
    assert applied["duplicates"][0]["action"] == "delete_duplicate"
    assert not src.exists()
    assert dst.read_bytes() == b"same"


def test_repair_case_folder_reports_conflict_for_different_content(tmp_path: Path):
    case = tmp_path / "2025-0002-游秀鈴-一審-傷害致死"
    src = case / "信件" / "mail.pdf"
    dst = case / "12_信件往返" / "mail.pdf"
    src.parent.mkdir(parents=True)
    dst.parent.mkdir(parents=True)
    src.write_bytes(b"drive")
    dst.write_bytes(b"nas")

    report = repair_case_folder(case, apply=True, delete_duplicate=True)

    assert report["planned_moves"] == []
    assert report["conflicts"][0]["reason"] == "target_exists_different_content"
    assert src.exists()
    assert dst.read_bytes() == b"nas"


def test_repair_case_folder_can_move_conflict_with_safe_suffix(tmp_path: Path):
    case = tmp_path / "2025-0007-張偉銘-一審-傷害致死"
    src = case / "電子筆錄" / "20250626 準備程序筆錄.pdf"
    dst = case / "08_筆錄" / "20250626 準備程序筆錄.pdf"
    src.parent.mkdir(parents=True)
    dst.parent.mkdir(parents=True)
    src.write_bytes(b"drive-different")
    dst.write_bytes(b"nas")

    report = repair_case_folder(case, apply=True, move_conflicts_with_suffix=True)

    moved = case / "08_筆錄" / "20250626 準備程序筆錄（Drive匯入差異）.pdf"
    assert report["conflicts"] == []
    assert len(report["conflict_moves"]) == 1
    assert moved.read_bytes() == b"drive-different"
    assert dst.read_bytes() == b"nas"
    assert not src.exists()


def test_repair_case_folder_unpacks_downloaded_drive_case_shell(tmp_path: Path):
    case = tmp_path / "2025-0002-游秀鈴-一審-傷害致死"
    src = case / "游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等" / "上訴理由一狀.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"appeal")

    report = repair_case_folder(case, apply=True)

    assert report["errors"] == []
    assert (case / "04_我方歷次書狀" / "上訴理由一狀.pdf").read_bytes() == b"appeal"
    assert not src.exists()
    assert "游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等" in report["alias_folders"]


def test_repair_case_folder_moves_misfiled_transcript_from_judgment_folder(tmp_path: Path):
    case = tmp_path / "2026-0028-劉信義-一審-殺人"
    src = case / "10_判決書" / "20260601 花蓮地方法院調解筆錄.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"mediation transcript")

    report = repair_case_folder(case, apply=True)

    dst = case / "08_筆錄" / "20260601 花蓮地方法院調解筆錄.pdf"
    assert report["errors"] == []
    assert len(report["canonical_misfile_moves"]) == 1
    assert dst.read_bytes() == b"mediation transcript"
    assert not src.exists()


def test_repair_case_folder_moves_procedural_ruling_from_judgment_folder(tmp_path: Path):
    case = tmp_path / "2026-0028-劉信義-一審-殺人"
    src = (
        case
        / "10_判決書"
        / "20260602 花蓮地方法院115年度聲字第169號刑事裁定（准許參與本案訴訟）.pdf"
    )
    src.parent.mkdir(parents=True)
    src.write_bytes(b"procedural ruling")

    report = repair_case_folder(case, apply=True)

    dst = (
        case
        / "09_法院通知或程序裁定"
        / "20260602 花蓮地方法院115年度聲字第169號刑事裁定（准許參與本案訴訟）.pdf"
    )
    assert report["errors"] == []
    assert len(report["canonical_misfile_moves"]) == 1
    assert dst.read_bytes() == b"procedural ruling"
    assert not src.exists()


def test_repair_case_folder_keeps_terminal_ruling_in_judgment_folder(tmp_path: Path):
    case = tmp_path / "2026-0001-測試-更生"
    src = case / "10_判決書" / "20260602 花蓮地方法院115年度消債更字第1號免責裁定.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"terminal ruling")

    report = repair_case_folder(case, apply=True)

    assert report["errors"] == []
    assert report["canonical_misfile_moves"] == []
    assert src.read_bytes() == b"terminal ruling"


def test_cleanup_duplicate_quarantine_deletes_only_when_original_exists(tmp_path: Path):
    case = tmp_path / "2025-0058-李明志-更生"
    original = case / "04_我方歷次書狀" / "更生方案.pdf"
    duplicate = case / ".duplicates" / "1777120868" / "更生方案.pdf"
    unique = case / ".duplicates" / "1777120868" / "只有隔離區有的檔案.pdf"
    original.parent.mkdir(parents=True)
    duplicate.parent.mkdir(parents=True)
    original.write_bytes(b"same-plan")
    duplicate.write_bytes(b"same-plan")
    unique.write_bytes(b"unique")

    dry = cleanup_duplicate_quarantine(case, apply=False)
    assert len(dry["safe_duplicates"]) == 1
    assert dry["kept_unique"] == [str(unique)]
    assert duplicate.exists()

    applied = cleanup_duplicate_quarantine(case, apply=True)
    assert len(applied["safe_duplicates"]) == 1
    assert not duplicate.exists()
    assert unique.exists()
    assert original.read_bytes() == b"same-plan"


def test_repair_case_tree_scans_case_folders_and_reports_summary(tmp_path: Path):
    root = tmp_path / "01_案件"
    case = root / "法扶案件" / "刑事" / "2025-0002-游秀鈴-一審-傷害致死"
    src = case / "游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等" / "上訴理由一狀.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"appeal")

    report = repair_case_tree(root, apply=True, max_cases=10)

    assert report["case_count"] == 1
    assert report["summary"]["planned_moves"] == 1
    assert (case / "04_我方歷次書狀" / "上訴理由一狀.pdf").exists()
