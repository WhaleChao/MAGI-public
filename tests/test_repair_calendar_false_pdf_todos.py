from __future__ import annotations

from scripts.ops import repair_calendar_false_pdf_todos as repair


def test_classify_false_pdf_todo_detects_legacy_case_and_source_year_shift():
    row = {
        "source_file": r"K:\SynologyDrive\01_案件\法扶案件\消費者債務清理\2025-0049-林洋宇-消費者債務清理-更生\09_法院通知或程序裁定\20250407 新北地方法院114年度司消債調字第389號民事執行處函(林洋宇；訂6月17日下午2時調解).pdf",
        "todo_type": "調解",
        "todo_date": "2026-06-17",
        "description": "⚖️ 6月17日下午2時 整調解",
        "case_number": "2025-0049",
        "client_name": "林洋宇",
    }

    reason = repair.classify_false_pdf_todo(row)

    assert reason
    assert "future_year_shift_from_old_source" in reason
    assert "2026-06-17" in reason


def test_classify_false_pdf_todo_detects_case_hint_without_0617():
    row = {
        "source_file": r"K:\\SynologyDrive\\01_案件\\2025-0130-張三\\08_判決書\\notice.pdf",
        "todo_type": "開庭",
        "todo_date": "2026-07-09",
        "description": "⚖️ 7月9日下午4時50分 審理",
        "case_number": "2025-0130",
        "client_name": "王大明",
    }

    reason = repair.classify_false_pdf_todo(row)

    assert reason
    assert "future_year_shift_from_old_source" in reason
    assert "2026-07-09" in reason


def test_classify_false_pdf_todo_not_trigger_when_only_case_number_is_legacy_hint():
    row = {
        "source_file": "/tmp/notifications/notice.pdf",
        "todo_type": "開庭",
        "todo_date": "2026-07-09",
        "description": "⚖️ 7月9日 下午4時50分 審理",
        "case_number": "2025-0130",
        "client_name": "王大明",
    }

    assert repair.classify_false_pdf_todo(row) == ""
