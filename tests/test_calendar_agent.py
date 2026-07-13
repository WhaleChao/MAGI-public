from datetime import date, datetime

import pytest

from api.domains import calendar_agent as agent


REFERENCE = date(2026, 7, 6)  # Monday


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今天", date(2026, 7, 6)),
        ("明天", date(2026, 7, 7)),
        ("後天", date(2026, 7, 8)),
        ("下週三", date(2026, 7, 15)),
        ("民國115年7月12日", date(2026, 7, 12)),
        ("115/7/12", date(2026, 7, 12)),
        ("2026-07-12", date(2026, 7, 12)),
    ],
)
def test_parse_taiwanese_dates(text, expected):
    parsed = agent.parse_date_expression(text, reference_date=REFERENCE)

    assert parsed.value == expected
    assert parsed.errors == ()


def test_month_day_reports_assumed_year_and_invalid_date_reports_error():
    assumed = agent.parse_date_expression("7月12日", reference_date=REFERENCE)
    invalid = agent.parse_date_expression("民國115年2月30日", reference_date=REFERENCE)

    assert assumed.value == date(2026, 7, 12)
    assert assumed.ambiguities == ("year_assumed",)
    assert invalid.value is None
    assert invalid.errors == ("invalid_date",)


def test_create_timed_event_has_preview_and_confirmation_requirement():
    draft = agent.parse_calendar_request("請新增明天下午3點和客戶開會", reference_date=REFERENCE)

    assert draft.intent is agent.CalendarIntent.CREATE
    assert draft.title == "和客戶開會"
    assert draft.start == datetime(2026, 7, 7, 15, 0)
    assert draft.end == datetime(2026, 7, 7, 16, 0)
    assert draft.assumptions == ("default_duration_60_minutes",)
    assert agent.confirmation_required(draft) is True
    assert agent.preview_draft(draft)["event"]["start"] == "2026-07-07T15:00:00"
    assert agent.confirm_draft(draft, "確認").accepted is True
    assert agent.confirm_draft(draft, "先不要").accepted is False


def test_missing_create_fields_and_unknown_intent_are_returned_not_executed():
    missing = agent.parse_calendar_request("新增明天的行程", reference_date=REFERENCE)
    unknown = agent.parse_calendar_request("幫我處理一下", reference_date=REFERENCE)

    assert set(missing.missing_fields) == {"start_time", "title"}
    assert missing.needs_clarification is True
    assert agent.confirmation_required(missing) is False
    assert unknown.intent is agent.CalendarIntent.UNKNOWN
    assert unknown.errors == ("calendar_intent_not_recognized",)


def test_all_day_and_cross_day_events_use_exclusive_end():
    all_day = agent.parse_calendar_request("新增7月12日到7月13日全天教育訓練", reference_date=REFERENCE)
    cross_day = agent.parse_calendar_request("新增7月12日晚上11點到7月13日凌晨1點系統維護", reference_date=REFERENCE)

    assert all_day.all_day is True
    assert all_day.start == datetime(2026, 7, 12, 0, 0)
    assert all_day.end == datetime(2026, 7, 14, 0, 0)
    assert cross_day.start == datetime(2026, 7, 12, 23, 0)
    assert cross_day.end == datetime(2026, 7, 13, 1, 0)


def test_weekly_and_monthly_recurrence_emit_rrule_and_infer_start():
    weekly = agent.parse_calendar_request("每週三下午3點部門例會", reference_date=REFERENCE)
    monthly = agent.parse_calendar_request("每月5日晚上8點繳房租", reference_date=REFERENCE)

    assert weekly.rrule == "RRULE:FREQ=WEEKLY;BYDAY=WE"
    assert weekly.start == datetime(2026, 7, 8, 15, 0)
    assert "recurrence_start_inferred" in weekly.assumptions
    assert monthly.rrule == "RRULE:FREQ=MONTHLY;BYMONTHDAY=5"
    assert monthly.start == datetime(2026, 8, 5, 20, 0)


def test_query_modify_and_cancel_intents_produce_non_executable_drafts():
    query = agent.parse_calendar_request("查詢下週有哪些行程", reference_date=REFERENCE)
    specific_query = agent.parse_calendar_request("查詢下週三有哪些行程", reference_date=REFERENCE)
    modify = agent.parse_calendar_request("把明天的客戶會議改到後天上午10點", reference_date=REFERENCE)
    cancel = agent.parse_calendar_request("取消明天的客戶會議", reference_date=REFERENCE)

    assert query.intent is agent.CalendarIntent.QUERY
    assert query.query_start == datetime(2026, 7, 13, 0, 0)
    assert query.query_end == datetime(2026, 7, 20, 0, 0)
    assert agent.confirmation_required(query) is False
    assert specific_query.query_start == datetime(2026, 7, 15, 0, 0)
    assert specific_query.query_end == datetime(2026, 7, 16, 0, 0)

    assert modify.intent is agent.CalendarIntent.MODIFY
    assert modify.target_hint == "客戶會議"
    assert modify.start == datetime(2026, 7, 8, 10, 0)
    assert agent.confirmation_required(modify) is True

    assert cancel.intent is agent.CalendarIntent.CANCEL
    assert cancel.target_hint == "客戶會議"
    assert agent.confirmation_required(cancel) is True


def test_explicit_query_date_range_includes_the_second_day():
    query = agent.parse_calendar_request("查詢7月12日到7月13日有哪些行程", reference_date=REFERENCE)

    assert query.query_start == datetime(2026, 7, 12, 0, 0)
    assert query.query_end == datetime(2026, 7, 14, 0, 0)


def test_cancel_with_only_date_reports_ambiguous_target():
    draft = agent.parse_calendar_request("取消明天的行程", reference_date=REFERENCE)

    assert draft.intent is agent.CalendarIntent.CANCEL
    assert draft.ambiguities == ("multiple_events_may_match",)
    assert draft.needs_clarification is True


def test_duplicate_and_conflict_checks_are_pure_and_ignore_cancelled_events():
    candidate = agent.CalendarEvent("客戶會議", datetime(2026, 7, 7, 15), datetime(2026, 7, 7, 16))
    duplicate = agent.CalendarEvent("客戶 會議", datetime(2026, 7, 7, 15), datetime(2026, 7, 7, 16), event_id="same")
    conflict = agent.CalendarEvent("內部會議", datetime(2026, 7, 7, 15, 30), datetime(2026, 7, 7, 16, 30))
    cancelled = agent.CalendarEvent("取消的會議", datetime(2026, 7, 7, 15, 15), datetime(2026, 7, 7, 15, 45), status="cancelled")

    checks = agent.check_event_safety(candidate, [duplicate, conflict, cancelled])

    assert checks.duplicates == (duplicate,)
    assert checks.conflicts == (duplicate, conflict)
    assert agent.events_overlap(candidate, conflict) is True
    assert agent.event_fingerprint(candidate) == agent.event_fingerprint(duplicate)


def test_event_from_draft_rejects_incomplete_or_non_create_drafts():
    complete = agent.parse_calendar_request("新增明天上午10點開會", reference_date=REFERENCE)
    query = agent.parse_calendar_request("查詢明天行程", reference_date=REFERENCE)

    assert agent.event_from_draft(complete).title == "開會"
    with pytest.raises(ValueError, match="draft_is_not_a_complete_create_event"):
        agent.event_from_draft(query)
