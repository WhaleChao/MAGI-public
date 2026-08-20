"""Fail-closed time semantics for hearings and other court occurrences."""

from __future__ import annotations

import re
from typing import Any


TIMED_OCCURRENCE_TYPES = {
    "開庭",
    "庭期",
    "言詞辯論",
    "辯論",
    "準備程序",
    "宣判",
    "判決宣示",
    "調解",
    "勘驗",
    "訊問",
    "審理",
    "審理程序",
    "庭訊",
}
ACTIONABLE_DEADLINE_TYPES = {
    "補正",
    "提出",
    "提出資料",
    "繳費",
    "繳納",
    "補繳",
    "回報",
    "陳報",
    "答辯",
    "聲請",
    "上訴",
    "抗告",
    "提交",
    "繳交",
    "寄送",
}
_PLACEHOLDER_MARKERS = ("（預留）", "(預留)", "（待確認）", "(待確認)")
_TIME_RE = re.compile(
    r"(上午|早上|中午|下午|晚上|晚間|傍晚|夜間)?\s*"
    r"([0-9０-９一二三四五六七八九十]{1,3})\s*[時点點]\s*"
    r"([0-9０-９一二三四五六七八九十]{0,3})(?:\s*分)?"
)
_VALID_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def _number(value: str) -> int | None:
    text = str(value or "").strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return 10
    if "十" in text:
        left, right = text.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    value_int = 0
    for char in text:
        if char not in digits:
            return None
        value_int = value_int * 10 + digits[char]
    return value_int


def is_timed_occurrence(todo_type: Any, description: Any = "") -> bool:
    kind = str(todo_type or "").strip()
    text = str(description or "")
    if kind in ACTIONABLE_DEADLINE_TYPES:
        return False
    if any(marker in text for marker in _PLACEHOLDER_MARKERS):
        return False
    if kind in TIMED_OCCURRENCE_TYPES:
        return True
    return any(marker in text for marker in TIMED_OCCURRENCE_TYPES)


def resolve_calendar_time(todo_time: Any, *contexts: Any) -> str:
    """Return HH:MM:SS, recoverable only from explicit text evidence."""

    supplied = str(todo_time or "").strip()
    match = _VALID_TIME_RE.fullmatch(supplied)
    if match:
        hour, minute, second = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
        if 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59:
            return f"{hour:02d}:{minute:02d}:{second:02d}"

    text = "\n".join(str(value or "") for value in contexts)
    match = _TIME_RE.search(text)
    if not match:
        return ""
    period, raw_hour, raw_minute = match.groups()
    hour = _number(raw_hour)
    minute = _number(raw_minute)
    if hour is None or minute is None or not (0 <= minute <= 59):
        return ""
    if period in {"下午", "晚上", "晚間", "傍晚", "夜間"} and hour < 12:
        hour += 12
    elif period in {"上午", "早上"} and hour == 12:
        hour = 0
    elif period == "中午" and hour < 11:
        hour += 12
    if not 0 <= hour <= 23:
        return ""
    return f"{hour:02d}:{minute:02d}:00"


def require_calendar_time(todo: dict[str, Any]) -> str:
    description = str(todo.get("description") or "")
    source_file = str(todo.get("source_file") or "")
    if str(todo.get("todo_type") or "").strip() in ACTIONABLE_DEADLINE_TYPES:
        # A deadline can legitimately mention the hearing that triggered it.
        # That historical hearing time must never turn the deadline into a
        # timed event.  Preserve an explicitly stored todo_time, however;
        # silently discarding it would change a real user-entered deadline.
        return resolve_calendar_time(todo.get("todo_time"))
    resolved = resolve_calendar_time(todo.get("todo_time"), description, source_file)
    if is_timed_occurrence(todo.get("todo_type"), description) and not resolved:
        raise ValueError("court_occurrence_time_missing")
    return resolved
