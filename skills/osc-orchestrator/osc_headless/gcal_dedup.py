# -*- coding: utf-8 -*-
"""
Google Calendar dedup helpers for OSC orchestrator.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time
from typing import Any, Dict, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - py<3.9 fallback
    ZoneInfo = None  # type: ignore


_CASE_NUMBER_RE = re.compile(r"\b\d{4}[-‐‑‒–—]\d{4}\b")
_COURT_CASE_RE = re.compile(r"\d{2,3}年度[^\s]{1,20}?字第?\d{1,6}號?")
_BRACKET_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")
_FILE_NOISE_RE = re.compile(r"(\.pdf\b|_[12]\d{7}\b|\(\d+\)\b)", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


def _to_halfwidth(value: str) -> str:
    if not value:
        return ""
    out = []
    for ch in value:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    raw = _coerce_text(value)
    if not raw:
        return ""
    if "T" in raw:
        raw = raw.split("T", 1)[0]
    if " " in raw:
        raw = raw.split(" ", 1)[0]
    return raw[:10]


def _normalize_time(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    if isinstance(value, time):
        return value.strftime("%H:%M")
    raw = _coerce_text(value)
    if not raw:
        return ""
    if "T" in raw:
        raw = raw.split("T", 1)[1]
    if "+" in raw:
        raw = raw.split("+", 1)[0]
    if "Z" in raw:
        raw = raw.replace("Z", "")
    if " " in raw:
        raw = raw.split(" ", 1)[0]
    parts = raw.split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    return ""


def _extract_case_from_text(text: str) -> str:
    t = _coerce_text(text)
    if not t:
        return ""
    m = _CASE_NUMBER_RE.search(t)
    if m:
        return m.group(0)
    m2 = _COURT_CASE_RE.search(t)
    if m2:
        return m2.group(0).replace("第0", "第")
    return ""


def is_invalid_case_key(case_key: str) -> bool:
    c = _coerce_text(case_key)
    if not c:
        return True
    if re.fullmatch(r"\d{3,4}", c):
        return True
    if re.fullmatch(r"(19|20)\d{2}", c):
        return True
    return False


def normalize_case_key(todo_or_event: Dict[str, Any]) -> Tuple[str, str]:
    fields = (
        ("case_number", "case_number"),
        ("magi_case_number", "magi_case_number"),
        ("laf_case_no", "laf_case_no"),
        ("legal_aid_number", "legal_aid_number"),
        ("application_no", "application_no"),
        ("court_case_no", "court_case_no"),
        ("court_case_number", "court_case_number"),
    )
    for field, source in fields:
        raw = _coerce_text(todo_or_event.get(field))
        if not raw:
            continue
        found = _extract_case_from_text(raw) or raw
        if is_invalid_case_key(found):
            continue
        return found, source

    # Look into nested Google extended properties.
    ext = todo_or_event.get("extendedProperties") or {}
    prv = ext.get("private") if isinstance(ext, dict) else {}
    if isinstance(prv, dict):
        for key in ("magi_case_number", "case_number"):
            raw = _coerce_text(prv.get(key))
            if not raw:
                continue
            found = _extract_case_from_text(raw) or raw
            if is_invalid_case_key(found):
                continue
            return found, f"extended_private:{key}"

    for field in ("summary", "title", "description"):
        found = _extract_case_from_text(todo_or_event.get(field))
        if found and not is_invalid_case_key(found):
            return found, f"text:{field}"

    # Last fallback: client name.
    name = _coerce_text(todo_or_event.get("client_name"))
    if name:
        return name, "client_name_fallback"
    return "", "missing"


def classify_event_kind(text: str, todo_type: str = "") -> str:
    t = f"{_coerce_text(todo_type)} {_coerce_text(text)}"
    if any(k in t for k in ("開庭", "準備程序", "言詞辯論", "審理程序", "訊問", "調解", "協商程序")):
        return "hearing"
    if any(k in t for k in ("補正", "繳費", "上訴", "抗告", "再抗告", "異議", "陳述意見", "提出資料", "期限")):
        return "deadline"
    if any(k in t for k in ("開會", "會議", "律見", "接見", "視訊會議", "法律諮詢", "來所")):
        return "meeting"
    if any(k in t for k in ("閱卷", "影卷", "調卷")):
        return "review"
    if any(k in t for k in ("電話聯繫", "通話", "電聯", "聯繫", "聯絡")):
        return "contact"
    if any(k in t for k in ("法扶開辦末日", "法扶")):
        return "laf_admin"
    return "other"


def normalize_subject(text: str, *, case_key: str = "") -> str:
    s = _to_halfwidth(_coerce_text(text))
    if not s:
        return ""
    s = _BRACKET_PREFIX_RE.sub("", s)
    s = re.sub(r"\[[^\]]+\]", " ", s)
    s = _FILE_NOISE_RE.sub("", s)
    # Remove common emoji-like symbols and punctuation noise.
    s = re.sub(r"[⚖️📝✅❌📅🔔•★☆■□◆◇▶◀▶️]", " ", s)
    s = re.sub(r"[-_–—:：;；,，。./\\|]+", " ", s)
    if case_key:
        s = s.replace(case_key, " ")
    # Also strip embedded case tokens.
    s = _CASE_NUMBER_RE.sub(" ", s)
    s = re.sub(r"\b\d{4}\s+\d{4}\b", " ", s)
    s = _COURT_CASE_RE.sub(" ", s)
    s = s.replace("（", " ").replace("）", " ").replace("(", " ").replace(")", " ")
    s = _SPACE_RE.sub(" ", s).strip()
    return s


def _event_start_date_time(event: Dict[str, Any], tz: str = "Asia/Taipei") -> Tuple[str, str]:
    start = event.get("start") if isinstance(event, dict) else {}
    if not isinstance(start, dict):
        return "", ""
    date_only = _coerce_text(start.get("date"))
    if date_only:
        return _normalize_date(date_only), ""
    dt_str = _coerce_text(start.get("dateTime"))
    if not dt_str:
        return "", ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo and ZoneInfo is not None:
            dt = dt.astimezone(ZoneInfo(tz))
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except Exception:
        return _normalize_date(dt_str), _normalize_time(dt_str)


def build_dedup_key_from_todo(todo: Dict[str, Any], tz: str = "Asia/Taipei") -> str:
    case_key, _ = normalize_case_key(todo)
    todo_type = _coerce_text(todo.get("todo_type")) or _coerce_text(todo.get("type"))
    date_key = _normalize_date(todo.get("todo_date") or todo.get("start_date"))
    time_key = _normalize_time(todo.get("todo_time") or todo.get("start_time"))
    payload_text = " ".join(
        x for x in (
            todo_type,
            _coerce_text(todo.get("summary")),
            _coerce_text(todo.get("title")),
            _coerce_text(todo.get("description")),
        ) if x
    )
    kind = classify_event_kind(payload_text, todo_type=todo_type)
    subject = normalize_subject(payload_text, case_key=case_key)
    subject_hash = hashlib.sha1(subject.encode("utf-8")).hexdigest()[:8] if subject else "none"
    return (
        f"v1|case:{case_key or 'unknown'}|kind:{kind}|"
        f"date:{date_key or 'unknown'}|time:{time_key or 'all_day'}|subject:{subject_hash}"
    )


def build_dedup_key_from_gcal_event(event: Dict[str, Any], tz: str = "Asia/Taipei") -> str:
    case_key, _ = normalize_case_key(event)
    date_key, time_key = _event_start_date_time(event, tz=tz)
    summary = _coerce_text(event.get("summary"))
    desc = _coerce_text(event.get("description"))
    kind = classify_event_kind(f"{summary} {desc}", todo_type="")
    subject = normalize_subject(f"{summary} {desc}", case_key=case_key)
    subject_hash = hashlib.sha1(subject.encode("utf-8")).hexdigest()[:8] if subject else "none"
    return (
        f"v1|case:{case_key or 'unknown'}|kind:{kind}|"
        f"date:{date_key or 'unknown'}|time:{time_key or 'all_day'}|subject:{subject_hash}"
    )


def confidence_for_match(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    key_a = _coerce_text(a.get("dedup_key"))
    key_b = _coerce_text(b.get("dedup_key"))
    if key_a and key_b and key_a == key_b and "case:unknown|" not in key_a:
        return "high"

    case_a, _ = normalize_case_key(a)
    case_b, _ = normalize_case_key(b)
    if not case_a or not case_b or is_invalid_case_key(case_a) or is_invalid_case_key(case_b):
        return "low"
    if case_a != case_b:
        return "low"

    text_a = " ".join(
        _coerce_text(a.get(k))
        for k in ("todo_type", "type", "summary", "title", "description")
        if _coerce_text(a.get(k))
    )
    text_b = " ".join(
        _coerce_text(b.get(k))
        for k in ("todo_type", "type", "summary", "title", "description")
        if _coerce_text(b.get(k))
    )
    kind_a = classify_event_kind(text_a, todo_type=_coerce_text(a.get("todo_type") or a.get("type")))
    kind_b = classify_event_kind(text_b, todo_type=_coerce_text(b.get("todo_type") or b.get("type")))

    date_a = _normalize_date(a.get("todo_date") or a.get("start_date"))
    date_b = _normalize_date(b.get("todo_date") or b.get("start_date"))
    if (not date_a or not date_b) and isinstance(a.get("start"), dict):
        date_a, _ = _event_start_date_time(a)
    if (not date_a or not date_b) and isinstance(b.get("start"), dict):
        date_b, _ = _event_start_date_time(b)
    if not date_a or not date_b or date_a != date_b:
        return "low"

    time_a = _normalize_time(a.get("todo_time") or a.get("start_time"))
    time_b = _normalize_time(b.get("todo_time") or b.get("start_time"))
    if (not time_a) and isinstance(a.get("start"), dict):
        _, time_a = _event_start_date_time(a)
    if (not time_b) and isinstance(b.get("start"), dict):
        _, time_b = _event_start_date_time(b)

    if kind_a == kind_b and time_a and time_b and time_a == time_b:
        return "high"
    if kind_a == kind_b:
        return "medium"
    return "low"


__all__ = [
    "build_dedup_key_from_gcal_event",
    "build_dedup_key_from_todo",
    "classify_event_kind",
    "confidence_for_match",
    "is_invalid_case_key",
    "normalize_case_key",
    "normalize_subject",
]
