"""Pure natural-language calendar planning for Traditional Chinese requests.

This module intentionally has no database, Google Calendar, HTTP, or filesystem
dependencies.  It turns a user request into a reviewable draft; an adapter must
perform any real calendar mutation only after an explicit confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
import re
from typing import Any, Iterable, Mapping


TAIPEI_TIMEZONE = "Asia/Taipei"
DEFAULT_DURATION = timedelta(hours=1)


class CalendarIntent(str, Enum):
    CREATE = "create"
    QUERY = "query"
    MODIFY = "modify"
    CANCEL = "cancel"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DateMatch:
    value: date | None
    text: str
    span: tuple[int, int]
    errors: tuple[str, ...] = ()
    ambiguities: tuple[str, ...] = ()


@dataclass(frozen=True)
class TimeMatch:
    value: time | None
    text: str
    span: tuple[int, int]
    errors: tuple[str, ...] = ()
    ambiguities: tuple[str, ...] = ()


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    rrule: str | None = None
    event_id: str = ""
    status: str = "confirmed"


@dataclass(frozen=True)
class CalendarDraft:
    intent: CalendarIntent
    source_text: str
    title: str = ""
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    rrule: str | None = None
    target_hint: str = ""
    query_start: datetime | None = None
    query_end: datetime | None = None
    missing_fields: tuple[str, ...] = ()
    ambiguities: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    timezone: str = TAIPEI_TIMEZONE

    @property
    def needs_clarification(self) -> bool:
        return bool(self.missing_fields or self.ambiguities or self.errors)

    @property
    def is_mutating(self) -> bool:
        return self.intent in {CalendarIntent.CREATE, CalendarIntent.MODIFY, CalendarIntent.CANCEL}


@dataclass(frozen=True)
class ConfirmationResult:
    accepted: bool | None
    reason: str


@dataclass(frozen=True)
class EventChecks:
    duplicates: tuple[CalendarEvent, ...]
    conflicts: tuple[CalendarEvent, ...]

    @property
    def has_duplicate(self) -> bool:
        return bool(self.duplicates)

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)


@dataclass(frozen=True)
class _Schedule:
    start: datetime | None
    end: datetime | None
    all_day: bool
    rrule: str | None
    missing_fields: tuple[str, ...]
    ambiguities: tuple[str, ...]
    assumptions: tuple[str, ...]
    errors: tuple[str, ...]
    date_matches: tuple[DateMatch, ...]
    time_matches: tuple[TimeMatch, ...]


_WEEKDAY_BY_CHAR = {
    "一": (0, "MO"),
    "二": (1, "TU"),
    "三": (2, "WE"),
    "四": (3, "TH"),
    "五": (4, "FR"),
    "六": (5, "SA"),
    "日": (6, "SU"),
    "天": (6, "SU"),
}
_RRULE_WEEKDAY = {value: key for key, value in _WEEKDAY_BY_CHAR.values()}

_GREGORIAN_DATE_RE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})\s*(?:年|[./-])\s*(?P<month>0?[1-9]|1[0-2])\s*(?:月|[./-])\s*(?P<day>3[01]|[12]\d|0?[1-9])(?:日|號)?"
)
_ROC_DATE_RE = re.compile(
    r"(?:民國\s*)?(?P<year>1\d{2})\s*(?:年|[./-])\s*(?P<month>0?[1-9]|1[0-2])\s*(?:月|[./-])\s*(?P<day>3[01]|[12]\d|0?[1-9])(?:日|號)?"
)
_MONTH_DAY_RE = re.compile(
    r"(?<![\d年])(?P<month>0?[1-9]|1[0-2])\s*(?:月|/)\s*(?P<day>3[01]|[12]\d|0?[1-9])(?:日|號)?"
)
_NEXT_WEEKDAY_RE = re.compile(r"下(?:週|周|星期)\s*(?P<weekday>[一二三四五六日天])")
_THIS_WEEKDAY_RE = re.compile(r"(?:這|本)(?:週|周|星期)\s*(?P<weekday>[一二三四五六日天])")
_TIME_RE = re.compile(
    r"(?:(?P<period>上午|早上|清晨|凌晨|下午|晚上|傍晚|中午)\s*)?"
    r"(?P<hour>[01]?\d|2[0-3])"
    r"(?:(?:[:：](?P<colon_minute>[0-5]\d))|(?:點(?:(?P<half>半)|(?P<point_minute>[0-5]?\d)分?)?))"
)
_DURATION_RE = re.compile(r"(?P<count>\d{1,3})\s*(?P<unit>小時|分鐘|分)(?!鐘)")
_WEEKLY_RE = re.compile(r"每(?:週|周|星期)\s*(?P<weekday>[一二三四五六日天])?")
_MONTHLY_RE = re.compile(r"每月\s*(?P<day>3[01]|[12]\d|0?[1-9])?\s*(?:日|號)?")
_ALL_DAY_RE = re.compile(r"(?:全天|整天|全日)")
_MODIFY_RE = re.compile(r"(?:改到|改為|修改為|變更為|延後到|提前到)")


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))


def _safe_date(year: int, month: int, day: int) -> tuple[date | None, tuple[str, ...]]:
    try:
        return date(year, month, day), ()
    except ValueError:
        return None, ("invalid_date",)


def _add_date_candidate(candidates: list[DateMatch], value: date | None, match: re.Match[str], *, errors: tuple[str, ...] = (), ambiguities: tuple[str, ...] = ()) -> None:
    candidates.append(DateMatch(value=value, text=match.group(0), span=match.span(), errors=errors, ambiguities=ambiguities))


def find_date_expressions(text: str, *, reference_date: date) -> tuple[DateMatch, ...]:
    """Find explicit and relative Taiwanese date expressions without I/O."""
    candidates: list[DateMatch] = []

    for match in _GREGORIAN_DATE_RE.finditer(text):
        value, errors = _safe_date(int(match["year"]), int(match["month"]), int(match["day"]))
        _add_date_candidate(candidates, value, match, errors=errors)

    for match in _ROC_DATE_RE.finditer(text):
        value, errors = _safe_date(int(match["year"]) + 1911, int(match["month"]), int(match["day"]))
        _add_date_candidate(candidates, value, match, errors=errors)

    relative_values = {
        "今天": reference_date,
        "今日": reference_date,
        "明天": reference_date + timedelta(days=1),
        "後天": reference_date + timedelta(days=2),
    }
    for word, value in relative_values.items():
        for match in re.finditer(word, text):
            _add_date_candidate(candidates, value, match)

    week_start = reference_date - timedelta(days=reference_date.weekday())
    for match in _NEXT_WEEKDAY_RE.finditer(text):
        offset, _ = _WEEKDAY_BY_CHAR[match["weekday"]]
        _add_date_candidate(candidates, week_start + timedelta(days=7 + offset), match)
    for match in _THIS_WEEKDAY_RE.finditer(text):
        offset, _ = _WEEKDAY_BY_CHAR[match["weekday"]]
        _add_date_candidate(candidates, week_start + timedelta(days=offset), match)

    for match in _MONTH_DAY_RE.finditer(text):
        value, errors = _safe_date(reference_date.year, int(match["month"]), int(match["day"]))
        _add_date_candidate(candidates, value, match, errors=errors, ambiguities=("year_assumed",) if not errors else ())

    selected: list[DateMatch] = []
    for candidate in sorted(candidates, key=lambda item: (item.span[0], -(item.span[1] - item.span[0]))):
        if any(candidate.span[0] < other.span[1] and other.span[0] < candidate.span[1] for other in selected):
            continue
        selected.append(candidate)
    return tuple(sorted(selected, key=lambda item: item.span))


def parse_date_expression(text: str, *, reference_date: date) -> DateMatch:
    """Return the first date expression, preserving errors and ambiguity metadata."""
    matches = find_date_expressions(text, reference_date=reference_date)
    if matches:
        return matches[0]
    return DateMatch(value=None, text="", span=(-1, -1), errors=("date_not_found",))


def find_time_expressions(text: str) -> tuple[TimeMatch, ...]:
    matches: list[TimeMatch] = []
    for match in _TIME_RE.finditer(text):
        hour = int(match["hour"])
        minute_text = match["colon_minute"] or match["point_minute"]
        minute = 30 if match["half"] else int(minute_text or 0)
        period = match["period"] or ""
        if period in {"下午", "晚上", "傍晚"} and hour < 12:
            hour += 12
        elif period == "中午" and 1 <= hour < 12:
            hour += 12
        if hour > 23 or minute > 59:
            matches.append(TimeMatch(value=None, text=match.group(0), span=match.span(), errors=("invalid_time",)))
        else:
            matches.append(TimeMatch(value=time(hour, minute), text=match.group(0), span=match.span()))
    return tuple(matches)


def _remove_spans(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        if start < 0:
            continue
        for index in range(start, min(end, len(chars))):
            chars[index] = " "
    return "".join(chars)


def _clean_title(text: str, *, date_matches: Iterable[DateMatch] = (), time_matches: Iterable[TimeMatch] = ()) -> str:
    spans = [item.span for item in date_matches] + [item.span for item in time_matches]
    spans += [match.span() for match in _WEEKLY_RE.finditer(text)]
    spans += [match.span() for match in _MONTHLY_RE.finditer(text)]
    spans += [match.span() for match in _ALL_DAY_RE.finditer(text)]
    spans += [match.span() for match in _DURATION_RE.finditer(text)]
    clean = _remove_spans(text, spans)
    clean = re.sub(r"(?:幫我|請|我要|我想|新增(?:一個)?|建立(?:一個)?|加入(?:到)?行事曆|加入行程|安排|排定|預約|提醒我|提醒|行事曆|行程)", " ", clean)
    clean = re.sub(r"(?:從|到|至|在|於)\s*", " ", clean)
    clean = re.sub(r"\s+", " ", clean)
    clean = clean.strip(" ，,。！!；;：:")
    return "" if not clean.strip(" 的") else clean


def detect_intent(text: str) -> CalendarIntent:
    normalized = re.sub(r"\s+", "", text)
    if any(token in normalized for token in ("取消", "刪除", "移除")):
        return CalendarIntent.CANCEL
    if _MODIFY_RE.search(normalized) or any(token in normalized for token in ("修改", "變更", "延後", "提前")):
        return CalendarIntent.MODIFY
    if any(token in normalized for token in ("新增", "建立", "加入", "排定", "預約", "提醒")):
        return CalendarIntent.CREATE
    if any(token in normalized for token in ("查詢", "查看", "查一下", "有哪些", "行程", "空檔", "空閒")) and any(
        token in normalized for token in ("查詢", "查看", "查一下", "有哪些", "空檔", "空閒", "今天", "明天", "後天", "本週", "這週", "下週")
    ):
        return CalendarIntent.QUERY
    if "安排" in normalized:
        return CalendarIntent.CREATE
    if any(token in normalized for token in ("查詢", "查看", "查一下", "有哪些", "行程", "空檔", "空閒")):
        return CalendarIntent.QUERY
    return CalendarIntent.UNKNOWN


def _parse_duration(text: str, time_matches: Iterable[TimeMatch]) -> timedelta | None:
    occupied = [item.span for item in time_matches]
    for match in _DURATION_RE.finditer(text):
        if any(match.start() < end and start < match.end() for start, end in occupied):
            continue
        count = int(match["count"])
        return timedelta(hours=count) if match["unit"] == "小時" else timedelta(minutes=count)
    return None


def _next_weekday(reference_date: date, weekday: int) -> date:
    offset = (weekday - reference_date.weekday()) % 7
    return reference_date + timedelta(days=offset)


def _next_month_day(reference_date: date, day: int) -> date | None:
    year, month = reference_date.year, reference_date.month
    for _ in range(24):
        value, errors = _safe_date(year, month, day)
        if not errors and value and value >= reference_date:
            return value
        month += 1
        if month == 13:
            year += 1
            month = 1
    return None


def parse_rrule(text: str, *, start_date: date | None = None) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Return ``(rrule, ambiguities, assumptions)`` for weekly/monthly language."""
    weekly = _WEEKLY_RE.search(text)
    if weekly:
        day_text = weekly.group("weekday")
        assumptions: list[str] = []
        if day_text:
            _, byday = _WEEKDAY_BY_CHAR[day_text]
        elif start_date:
            byday = _RRULE_WEEKDAY[start_date.weekday()]
            assumptions.append("recurrence_weekday_from_start")
        else:
            return "RRULE:FREQ=WEEKLY", ("recurrence_weekday_unspecified",), ()
        return f"RRULE:FREQ=WEEKLY;BYDAY={byday}", (), tuple(assumptions)

    monthly = _MONTHLY_RE.search(text)
    if monthly:
        day_text = monthly.group("day")
        assumptions = []
        if day_text:
            day = int(day_text)
        elif start_date:
            day = start_date.day
            assumptions.append("recurrence_monthday_from_start")
        else:
            return "RRULE:FREQ=MONTHLY", ("recurrence_monthday_unspecified",), ()
        if not 1 <= day <= 31:
            return None, ("invalid_recurrence_monthday",), ()
        return f"RRULE:FREQ=MONTHLY;BYMONTHDAY={day}", (), tuple(assumptions)
    return None, (), ()


def _schedule_from_text(text: str, *, reference_date: date) -> _Schedule:
    date_matches = find_date_expressions(text, reference_date=reference_date)
    time_matches = find_time_expressions(text)
    errors = [error for item in (*date_matches, *time_matches) for error in item.errors]
    ambiguities = [ambiguity for item in date_matches for ambiguity in item.ambiguities]
    assumptions: list[str] = []
    all_day = bool(_ALL_DAY_RE.search(text))
    valid_dates = [item.value for item in date_matches if item.value]
    valid_times = [item.value for item in time_matches if item.value]
    rrule, recurrence_ambiguities, recurrence_assumptions = parse_rrule(
        text,
        start_date=valid_dates[0] if valid_dates else None,
    )
    ambiguities.extend(recurrence_ambiguities)
    assumptions.extend(recurrence_assumptions)

    if rrule and not valid_dates:
        if "BYDAY=" in rrule:
            byday = rrule.rsplit("=", 1)[1]
            valid_dates = [_next_weekday(reference_date, _RRULE_WEEKDAY[byday])]
            assumptions.append("recurrence_start_inferred")
        elif "BYMONTHDAY=" in rrule:
            inferred = _next_month_day(reference_date, int(rrule.rsplit("=", 1)[1]))
            if inferred:
                valid_dates = [inferred]
                assumptions.append("recurrence_start_inferred")

    missing: list[str] = []
    if not valid_dates:
        missing.append("start_date")
    if not all_day and not valid_times:
        missing.append("start_time")
    if errors or missing:
        return _Schedule(
            start=None,
            end=None,
            all_day=all_day,
            rrule=rrule,
            missing_fields=tuple(missing),
            ambiguities=_unique(ambiguities),
            assumptions=_unique(assumptions),
            errors=_unique(errors),
            date_matches=date_matches,
            time_matches=time_matches,
        )

    start_day = valid_dates[0]
    if all_day:
        end_day = valid_dates[1] if len(valid_dates) > 1 else start_day
        start = datetime.combine(start_day, time.min)
        end = datetime.combine(end_day + timedelta(days=1), time.min)
    else:
        start = datetime.combine(start_day, valid_times[0])
        if len(valid_times) > 1:
            end_day = valid_dates[1] if len(valid_dates) > 1 else start_day
            end = datetime.combine(end_day, valid_times[1])
            if end <= start and len(valid_dates) == 1:
                end += timedelta(days=1)
                assumptions.append("cross_day_inferred")
            elif end <= start:
                errors.append("end_before_start")
        else:
            duration = _parse_duration(text, time_matches)
            end = start + (duration or DEFAULT_DURATION)
            assumptions.append("duration_from_text" if duration else "default_duration_60_minutes")

    return _Schedule(
        start=start,
        end=end,
        all_day=all_day,
        rrule=rrule,
        missing_fields=tuple(missing),
        ambiguities=_unique(ambiguities),
        assumptions=_unique(assumptions),
        errors=_unique(errors),
        date_matches=date_matches,
        time_matches=time_matches,
    )


def _split_modify_text(text: str) -> tuple[str, str]:
    match = _MODIFY_RE.search(text)
    if not match:
        return text, ""
    return text[:match.start()], text[match.end():]


def _target_from_text(text: str, *, reference_date: date) -> str:
    dates = find_date_expressions(text, reference_date=reference_date)
    times = find_time_expressions(text)
    target = _clean_title(text, date_matches=dates, time_matches=times)
    target = re.sub(r"^(?:把|將|幫我|請|取消|刪除|移除|修改|變更)\s*", "", target)
    target = re.sub(r"(?:查詢|查看|查一下|有哪些|空檔|空閒|行程|日曆|行事曆)", " ", target)
    target = re.sub(r"\s+", " ", target)
    return target.strip(" 的")


def _query_range(text: str, *, reference_date: date, date_matches: tuple[DateMatch, ...]) -> tuple[datetime | None, datetime | None]:
    values = [item.value for item in date_matches if item.value]
    if values:
        start = datetime.combine(values[0], time.min)
        end_day = values[1] + timedelta(days=1) if len(values) > 1 else values[0] + timedelta(days=1)
        return start, datetime.combine(end_day, time.min)
    if "下週" in text or "下周" in text:
        start_day = reference_date - timedelta(days=reference_date.weekday()) + timedelta(days=7)
        return datetime.combine(start_day, time.min), datetime.combine(start_day + timedelta(days=7), time.min)
    if "本週" in text or "這週" in text or "本周" in text or "這周" in text:
        start_day = reference_date - timedelta(days=reference_date.weekday())
        return datetime.combine(start_day, time.min), datetime.combine(start_day + timedelta(days=7), time.min)
    return None, None


def parse_calendar_request(text: str, *, reference_date: date) -> CalendarDraft:
    """Parse a calendar request into a non-executable draft.

    ``reference_date`` is mandatory so callers and tests can make relative-date
    interpretation deterministic.
    """
    source = str(text or "").strip()
    intent = detect_intent(source)
    source_dates = find_date_expressions(source, reference_date=reference_date)
    source_times = find_time_expressions(source)
    if intent is CalendarIntent.UNKNOWN and (source_dates or source_times or _WEEKLY_RE.search(source) or _MONTHLY_RE.search(source)):
        intent = CalendarIntent.CREATE

    if intent is CalendarIntent.MODIFY:
        target_text, schedule_text = _split_modify_text(source)
        schedule = _schedule_from_text(schedule_text, reference_date=reference_date)
        target_hint = _target_from_text(target_text, reference_date=reference_date)
        target_dates = find_date_expressions(target_text, reference_date=reference_date)
        query_start, query_end = _query_range(target_text, reference_date=reference_date, date_matches=target_dates)
        missing = list(schedule.missing_fields)
        if not target_hint:
            missing.append("target_event")
        if not schedule_text.strip():
            missing.append("new_schedule")
        return CalendarDraft(
            intent=intent,
            source_text=source,
            start=schedule.start,
            end=schedule.end,
            all_day=schedule.all_day,
            rrule=schedule.rrule,
            target_hint=target_hint,
            query_start=query_start,
            query_end=query_end,
            missing_fields=_unique(missing),
            ambiguities=schedule.ambiguities,
            assumptions=schedule.assumptions,
            errors=schedule.errors,
        )

    if intent is CalendarIntent.CANCEL:
        target_hint = _target_from_text(source, reference_date=reference_date)
        query_start, query_end = _query_range(source, reference_date=reference_date, date_matches=source_dates)
        ambiguities: tuple[str, ...] = ()
        missing: tuple[str, ...] = ()
        if not target_hint and not query_start:
            missing = ("target_event",)
        elif not target_hint:
            ambiguities = ("multiple_events_may_match",)
        return CalendarDraft(
            intent=intent,
            source_text=source,
            target_hint=target_hint,
            query_start=query_start,
            query_end=query_end,
            missing_fields=missing,
            ambiguities=ambiguities,
        )

    if intent is CalendarIntent.QUERY:
        query_start, query_end = _query_range(source, reference_date=reference_date, date_matches=source_dates)
        terms = _target_from_text(source, reference_date=reference_date)
        return CalendarDraft(
            intent=intent,
            source_text=source,
            target_hint=terms,
            query_start=query_start,
            query_end=query_end,
        )

    if intent is CalendarIntent.CREATE:
        schedule = _schedule_from_text(source, reference_date=reference_date)
        title = _clean_title(source, date_matches=schedule.date_matches, time_matches=schedule.time_matches)
        missing = list(schedule.missing_fields)
        if not title:
            missing.append("title")
        return CalendarDraft(
            intent=intent,
            source_text=source,
            title=title,
            start=schedule.start,
            end=schedule.end,
            all_day=schedule.all_day,
            rrule=schedule.rrule,
            missing_fields=_unique(missing),
            ambiguities=schedule.ambiguities,
            assumptions=schedule.assumptions,
            errors=schedule.errors,
        )

    return CalendarDraft(
        intent=CalendarIntent.UNKNOWN,
        source_text=source,
        missing_fields=("intent",),
        errors=("calendar_intent_not_recognized",),
    )


def confirmation_required(draft: CalendarDraft) -> bool:
    """Mutations need explicit confirmation only after all fields are clear."""
    return draft.is_mutating and not draft.needs_clarification


def confirm_draft(draft: CalendarDraft, reply: str) -> ConfirmationResult:
    if not confirmation_required(draft):
        return ConfirmationResult(accepted=None, reason="confirmation_not_available_until_draft_is_complete")
    normalized = re.sub(r"\s+", "", str(reply or "")).lower()
    if normalized in {"確認", "確認送出", "是", "好", "可以", "同意", "送出", "yes", "y"}:
        return ConfirmationResult(accepted=True, reason="confirmed")
    if normalized in {"不要", "否", "不", "取消", "先不要", "no", "n"}:
        return ConfirmationResult(accepted=False, reason="declined")
    return ConfirmationResult(accepted=None, reason="confirmation_response_ambiguous")


def preview_draft(draft: CalendarDraft) -> dict[str, Any]:
    """Return a serializable preview for a UI or chat confirmation step."""
    event = None
    if draft.start and draft.end:
        event = {
            "title": draft.title,
            "start": draft.start.isoformat(),
            "end": draft.end.isoformat(),
            "all_day": draft.all_day,
            "rrule": draft.rrule,
            "timezone": draft.timezone,
        }
    return {
        "intent": draft.intent.value,
        "event": event,
        "target_hint": draft.target_hint,
        "query": {
            "start": draft.query_start.isoformat() if draft.query_start else None,
            "end": draft.query_end.isoformat() if draft.query_end else None,
        },
        "missing_fields": list(draft.missing_fields),
        "ambiguities": list(draft.ambiguities),
        "assumptions": list(draft.assumptions),
        "errors": list(draft.errors),
        "requires_confirmation": confirmation_required(draft),
    }


def _coerce_event(value: CalendarEvent | Mapping[str, Any]) -> CalendarEvent:
    if isinstance(value, CalendarEvent):
        return value
    start_raw = value.get("start")
    end_raw = value.get("end")
    if isinstance(start_raw, str):
        start_raw = datetime.fromisoformat(start_raw)
    if isinstance(end_raw, str):
        end_raw = datetime.fromisoformat(end_raw)
    if not isinstance(start_raw, datetime) or not isinstance(end_raw, datetime):
        raise ValueError("calendar_event_requires_datetime_start_and_end")
    return CalendarEvent(
        title=str(value.get("title") or ""),
        start=start_raw,
        end=end_raw,
        all_day=bool(value.get("all_day", False)),
        rrule=str(value["rrule"]) if value.get("rrule") else None,
        event_id=str(value.get("event_id") or value.get("id") or ""),
        status=str(value.get("status") or "confirmed"),
    )


def event_from_draft(draft: CalendarDraft) -> CalendarEvent:
    if draft.intent is not CalendarIntent.CREATE or draft.needs_clarification or not draft.start or not draft.end:
        raise ValueError("draft_is_not_a_complete_create_event")
    return CalendarEvent(
        title=draft.title,
        start=draft.start,
        end=draft.end,
        all_day=draft.all_day,
        rrule=draft.rrule,
    )


def normalize_title(title: str) -> str:
    return re.sub(r"[\s\-_,，。；;：:]+", "", str(title or "")).casefold()


def event_fingerprint(event: CalendarEvent | Mapping[str, Any]) -> tuple[str, datetime, datetime, bool, str]:
    item = _coerce_event(event)
    return (normalize_title(item.title), item.start, item.end, item.all_day, item.rrule or "")


def find_duplicates(candidate: CalendarEvent | Mapping[str, Any], existing_events: Iterable[CalendarEvent | Mapping[str, Any]]) -> tuple[CalendarEvent, ...]:
    fingerprint = event_fingerprint(candidate)
    return tuple(
        event
        for event in (_coerce_event(item) for item in existing_events)
        if event.status.lower() not in {"cancelled", "canceled", "已取消"} and event_fingerprint(event) == fingerprint
    )


def events_overlap(left: CalendarEvent | Mapping[str, Any], right: CalendarEvent | Mapping[str, Any]) -> bool:
    first = _coerce_event(left)
    second = _coerce_event(right)
    return first.start < second.end and second.start < first.end


def find_conflicts(candidate: CalendarEvent | Mapping[str, Any], existing_events: Iterable[CalendarEvent | Mapping[str, Any]]) -> tuple[CalendarEvent, ...]:
    item = _coerce_event(candidate)
    conflicts: list[CalendarEvent] = []
    for existing in (_coerce_event(value) for value in existing_events):
        if existing.status.lower() in {"cancelled", "canceled", "已取消"}:
            continue
        if item.event_id and item.event_id == existing.event_id:
            continue
        if events_overlap(item, existing):
            conflicts.append(existing)
    return tuple(conflicts)


def check_event_safety(candidate: CalendarEvent | Mapping[str, Any], existing_events: Iterable[CalendarEvent | Mapping[str, Any]]) -> EventChecks:
    existing = tuple(existing_events)
    return EventChecks(
        duplicates=find_duplicates(candidate, existing),
        conflicts=find_conflicts(candidate, existing),
    )
