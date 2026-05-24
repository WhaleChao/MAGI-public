from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MAGI_ROOT = Path(__file__).resolve().parents[1]
ACTION_PATH = MAGI_ROOT / "skills" / "transcript-todo-extractor" / "action.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("transcript_todo_extractor_test", ACTION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pdf_path(tmp_path: Path, name: str = "20260519 準備程序筆錄.pdf") -> Path:
    folder = tmp_path / "刑事" / "2025-0001-測試當事人-一審-詐欺" / "05_筆錄"
    folder.mkdir(parents=True)
    return folder / name


def test_candidate_review_creates_seven_day_tracking_todo(tmp_path):
    mod = _load_module()
    pdf = _pdf_path(tmp_path, "20260519 訊問筆錄.pdf")
    pages = [(1, "法官諭知：本案候核辦。")]

    items = mod.extract_candidates_from_pages(
        pages,
        pdf_path=pdf,
        transcript_date="2026-05-19",
        case_number="2025-0001",
        client_name="測試當事人",
    )

    assert len(items) == 1
    assert items[0].confidence == "high"
    assert items[0].type == "追蹤"
    assert items[0].date == "2026-05-26"
    assert items[0].rule == "candidate_review_7_days"
    assert "候核辦" in items[0].description


def test_scheduled_hearing_is_high_confidence(tmp_path):
    mod = _load_module()
    pdf = _pdf_path(tmp_path)
    pages = [
        (
            1,
            "審判長諭知：本案定於115年1月14日上午10時行準備程序，兩造均請到庭。",
        )
    ]

    items = mod.extract_candidates_from_pages(
        pages,
        pdf_path=pdf,
        transcript_date="2026-05-19",
        case_number="2025-0001",
        client_name="測試當事人",
    )

    assert len(items) == 1
    assert items[0].type == "準備程序"
    assert items[0].date == "2026-01-14"
    assert items[0].time == "10:00"
    assert items[0].confidence == "high"


def test_ocr_bogus_roc_year_is_not_scheduled_todo(tmp_path):
    mod = _load_module()
    pdf = _pdf_path(tmp_path, "20201207 準備程序筆錄.pdf")
    pages = [
        (
            1,
            "406年11月8日民事調查證據聲請續狀第8頁有王文孝供述內容。",
        )
    ]

    items = mod.extract_candidates_from_pages(
        pages,
        pdf_path=pdf,
        transcript_date="2020-12-07",
        case_number="2025-0001",
        client_name="測試當事人",
    )

    assert items == []


def test_relative_deadline_uses_transcript_date(tmp_path):
    mod = _load_module()
    pdf = _pdf_path(tmp_path)
    pages = [(1, "法官諭知：請被告於7日內補正相關資料。")]

    items = mod.extract_candidates_from_pages(
        pages,
        pdf_path=pdf,
        transcript_date="2026-05-19",
        case_number="2025-0001",
        client_name="測試當事人",
    )

    assert len(items) == 1
    assert items[0].type == "補正"
    assert items[0].date == "2026-05-26"
    assert items[0].rule == "relative_deadline"


def test_pre_hearing_instruction_uses_seven_business_days_before_next_hearing(tmp_path):
    mod = _load_module()
    pdf = _pdf_path(tmp_path)
    pages = [
        (
            1,
            "\n".join(
                [
                    "審判長諭知：本案定於115年1月14日上午10時行準備程序。",
                    "法官諭知：請兩造於下次準備程序期日前提出證據能力意見及爭點整理。",
                ]
            ),
        )
    ]

    items = mod.extract_candidates_from_pages(
        pages,
        pdf_path=pdf,
        transcript_date="2026-05-19",
        case_number="2025-0001",
        client_name="測試當事人",
    )

    prep = [x for x in items if x.type == "庭前準備"]
    assert prep
    assert prep[0].confidence == "high"
    assert prep[0].date == "2026-01-05"
    assert prep[0].rule == "pre_hearing_seven_business_days"


def test_at_hearing_instruction_without_specific_due_date_uses_seven_business_days(tmp_path):
    mod = _load_module()
    pdf = _pdf_path(tmp_path)
    pages = [
        (
            1,
            "\n".join(
                [
                    "審判長諭知：本案定於115年1月14日上午10時行準備程序。",
                    "法官諭知：請被告開庭時攜帶相關存摺及交易明細到庭說明。",
                ]
            ),
        )
    ]

    items = mod.extract_candidates_from_pages(
        pages,
        pdf_path=pdf,
        transcript_date="2026-05-19",
        case_number="2025-0001",
        client_name="測試當事人",
    )

    prep = [x for x in items if x.type == "庭前準備"]
    assert prep
    assert prep[0].confidence == "high"
    assert prep[0].date == "2026-01-05"
    assert prep[0].rule == "pre_hearing_seven_business_days"


def test_rights_warning_is_not_todo(tmp_path):
    mod = _load_module()
    pdf = _pdf_path(tmp_path)
    pages = [(1, "法官問：你是否知道有緘默權？被告答：知道。")]

    items = mod.extract_candidates_from_pages(
        pages,
        pdf_path=pdf,
        transcript_date="2026-05-19",
        case_number="2025-0001",
        client_name="測試當事人",
    )

    assert items == []
