from datetime import date

from scripts.ops.reconcile_overdue_todos import classify_todo, _next_business_day


def _row(**overrides):
    row = {
        "id": 1,
        "todo_type": "陳報",
        "description": "應於期限內陳報",
        "source_file": "court.pdf",
        "case_status": "進行中",
        "legal_aid_status": "",
        "folder_path": "",
        "todo_date": date(2026, 7, 1),
    }
    row.update(overrides)
    return row


def test_past_calendar_occurrence_is_archived():
    action, reason = classify_todo(
        _row(todo_type="開庭", source_file="gcal_import:calendar@example.invalid")
    )

    assert (action, reason) == ("archive", "past_calendar_occurrence")


def test_closed_case_todo_is_completed():
    action, reason = classify_todo(_row(case_status="已結案"))

    assert (action, reason) == ("complete", "case_closed")


def test_started_laf_case_clears_opening_deadline():
    action, reason = classify_todo(_row(todo_type="法扶", legal_aid_status="進行中"))

    assert (action, reason) == ("complete", "laf_already_started")


def test_optional_objection_is_not_left_as_mandatory_overdue():
    action, reason = classify_todo(
        _row(todo_type="提出資料", description="得於公告翌日起十日內提出異議")
    )

    assert (action, reason) == ("complete", "optional_or_nonactionable")


def test_unverified_legal_deadline_is_escalated():
    action, reason = classify_todo(_row())

    assert (action, reason) == ("escalate", "no_verifiable_completion_evidence")


def test_manual_evidence_override_is_auditable():
    action, reason = classify_todo(_row(id=4659), verified_ids={4659})

    assert (action, reason) == ("complete", "manually_verified_evidence")


def test_next_business_day_skips_weekend():
    assert _next_business_day(date(2026, 7, 10)) == date(2026, 7, 13)
    assert _next_business_day(date(2026, 7, 12)) == date(2026, 7, 13)
