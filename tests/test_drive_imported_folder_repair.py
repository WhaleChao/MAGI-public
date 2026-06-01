from __future__ import annotations

from pathlib import Path

from scripts.ops.repair_drive_imported_case_folders import (
    is_noncanonical_drive_folder,
    mapped_file_relative_path,
    repair_case_folder,
)


def test_drive_imported_alias_detection_and_mapping():
    assert is_noncanonical_drive_folder("法院裁判") is True
    assert is_noncanonical_drive_folder("訊問筆錄") is True
    assert is_noncanonical_drive_folder("10_判決書") is False
    assert mapped_file_relative_path("法院裁判", "a.pdf") == "09_法院通知或程序裁定/a.pdf"
    assert mapped_file_relative_path("法院裁判", "20260101 裁定.pdf") == "09_法院通知或程序裁定/20260101 裁定.pdf"
    assert mapped_file_relative_path("法院裁判", "20260101 復權裁定.pdf") == "10_判決書/20260101 復權裁定.pdf"
    assert mapped_file_relative_path("法院裁判", "偵查案件起訴書.pdf") == "10_判決書/偵查案件起訴書.pdf"
    assert mapped_file_relative_path("起訴書", "20250306_聲請接續羈押理由書.pdf") == "09_法院通知或程序裁定/20250306_聲請接續羈押理由書.pdf"
    assert mapped_file_relative_path("法院資料", "起訴書/a.pdf") == "10_判決書/a.pdf"
    assert mapped_file_relative_path("訊問筆錄", "b.pdf") == "08_筆錄/b.pdf"


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
