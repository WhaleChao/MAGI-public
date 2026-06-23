# -*- coding: utf-8 -*-
"""
OSC headless todo extraction.

This intentionally focuses on filename-based parsing because:
- The OSC workflow relies on correct "收文日/文到日" for relative deadlines.
- pdf-namer already normalizes filenames to include YYYYMMDD.
"""

from __future__ import annotations
import json
import logging

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Dict, List, Optional
try:
    import holidays
except Exception:
    holidays = None  # type: ignore[assignment]


def _portable_basename(value: str) -> str:
    return re.split(r"[\\/]+", str(value or "").strip())[-1]


def _parse_roc_year_to_ad(raw: str) -> int | None:
    try:
        year = int(raw)
    except Exception:
        return None
    if year >= 1911:
        return year
    if 100 <= year <= 130:
        return year + 1911
    return None


def _collect_source_year_candidates(value: str) -> list[int]:
    text = str(value or "").strip()
    if not text:
        return []
    normalized = text.replace("\\", "/")
    candidates: list[int] = []

    def _add_candidate(raw: int) -> None:
        if not raw:
            return
        if raw < 2010 or raw > datetime.now().year + 3:
            return
        if raw not in candidates:
            candidates.append(raw)

    # Case-folder prefix: /2025-0049-xxx/ or similar
    for m in re.finditer(r"(?:^|/)(20\d{2})-(\d{3,8})(?:[\\/._-]|$)", normalized):
        _add_candidate(int(m.group(1)))

    # ROC year marker: 114年度
    for m in re.finditer(r"(\d{2,3})年度", normalized):
        year = _parse_roc_year_to_ad(m.group(1))
        _add_candidate(year or 0)

    # Explicit year mentions that can appear in OCR/source text (e.g., 115年、2025年、2025/06)
    for m in re.finditer(r"(?:20\d{2}|\d{2,3})(?=[年/.])", normalized):
        year_raw = _parse_roc_year_to_ad(m.group(0))
        _add_candidate(year_raw or 0)

    # ROC/AD tokens in compact document dates like 2025/06/17, 115/06/17
    for m in re.finditer(r"((?:20\d{2}|\d{3})[/.]\d{1,2}[/.]\d{1,2})", normalized):
        parts = re.split(r"[/.]", m.group(1))
        year_raw = _parse_roc_year_to_ad(parts[0] or "")
        _add_candidate(year_raw or 0)

    # Fallback scan for ROC/AD year-month tokens from text (e.g., 文到115年5月)
    for m in re.finditer(r"(?:民國)?(\d{2,4})年(\d{1,2})月", normalized):
        year_raw = _parse_roc_year_to_ad(m.group(1))
        _add_candidate(year_raw or 0)

    return candidates


def extract_document_date_from_filename(filename: str, file_path: str = "") -> Optional[datetime]:
    """
    Extract "document received date" from filename.
    Priority:
    - Prefix YYYYMMDD
    - Prefix YYYY-MM-DD / YYYY.MM.DD
    - Prefix ROC YYYMMDD / YYY-MM-DD / YYY/MM/DD
    """
    name = _portable_basename(filename or file_path)
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 33, exc_info=True)

    m = re.match(r"^(\d{4})[-\.](\d{2})[-\.](\d{2})", name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 40, exc_info=True)

    m = re.match(r"^(\d{3})[-/]?(\d{2})[-/]?(\d{2})(?:\s|$)", name)
    if m:
        try:
            roc_year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            if 100 <= roc_year <= 130:
                return datetime(roc_year + 1911, month, day)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 51, exc_info=True)

    # Fallback: try file mtime (only if exists)
    if file_path and os.path.exists(file_path):
        try:
            return datetime.fromtimestamp(os.path.getmtime(file_path))
        except Exception:
            return None
    return None


def extract_base_year_from_filename(filename: str, file_path: str = "", document_date: Optional[datetime] = None) -> int:
    """Return the OSC base year for yearless hearing dates.

    This keeps the original preference order but adds conservative additional
    in-path hints for legacy cases:
      - folder path case number: 2025-xxxx
      - ROC annual marker: 114年度
    """
    name = _portable_basename(filename or file_path)
    m = re.match(r"^(20\d{2})(\d{2})(\d{2})", name)
    if m:
        try:
            datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return int(m.group(1))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 70, exc_info=True)

    candidates = _collect_source_year_candidates(f"{filename}\n{file_path}")
    if candidates:
        for candidate in candidates:
            if 2010 <= candidate <= datetime.now().year + 3:
                return candidate

    m = re.search(r"(\d{3})年度", name)
    if m:
        try:
            roc_year = int(m.group(1))
            if 100 < roc_year < 130:
                return roc_year + 1911
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 79, exc_info=True)

    doc_date = document_date or extract_document_date_from_filename(filename, file_path)
    if doc_date:
        return doc_date.year
    return datetime.now().year


def chinese_to_number(chinese_str: str) -> Optional[int]:
    """Chinese number → int (supports simple forms like 十五/二十五/三十)."""
    s = (chinese_str or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    chinese_map = {
        "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
        "十": 10,
    }
    if s in chinese_map:
        return chinese_map[s]
    if "十" in s:
        a, b = s.split("十", 1)
        tens = 10 if a == "" else (chinese_map.get(a, 1) * 10)
        ones = 0 if b == "" else chinese_map.get(b, 0)
        return tens + ones
    return None


_TIME_PERIOD_RE = r"(上午|下午|早上|中午|晚上|晚間|傍晚|夜間|上|下)"
_TIME_NUMBER_RE = r"(\d{1,2}|[零一二三四五六七八九十]{1,3})"
_DAY_NUMBER_RE = r"([\d零一二三四五六七八九十]+)"
_DURATION_RE = rf"{_DAY_NUMBER_RE}(日|週|周)?"
_HEARING_LABELS = (
    "言詞辯論",
    "準備程序",
    "協商程序",
    "調解",
    "審判程序",
    "審理程序",
    "審理",
    "宣判",
    "訊問",
    "調查",
    "辯論",
    "開庭",
    "閱卷",
)
_EXCLUSION_PHRASES = ("同意不抗告", "放棄抗告", "不得抗告", "不得上訴", "簽名同意")
_REJECTION_PHRASES = ("駁回聲請", "駁回起訴", "駁回上訴", "駁回異議", "駁回抗告")
_ACTION_KEYWORDS_WITH_PRECEDENCE = ("補正", "補陳", "補提", "繳納", "繳費")
_FIXED_CONSUMER_DEBT_PATTERNS = {"司消債更字", "司消債清字", "司消債調字"}


def _parse_number_token(token: str) -> Optional[int]:
    text = (token or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return chinese_to_number(text)


def _parse_roc_or_ad_year(year_token: str) -> int:
    year = int(year_token)
    return year + 1911 if year < 1911 else year


def _parse_compact_roc_or_ad_date(token: str) -> Optional[datetime]:
    """Parse compact court dates such as 1150528 or 20260528."""
    text = re.sub(r"\D", "", token or "")
    try:
        if len(text) == 7:
            return datetime(_parse_roc_or_ad_year(text[:3]), int(text[3:5]), int(text[5:7]))
        if len(text) == 8:
            return datetime(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except Exception:
        return None
    return None


def _parse_separator_roc_or_ad_date(year_token: str, month_token: str, day_token: str) -> Optional[datetime]:
    try:
        return datetime(_parse_roc_or_ad_year(year_token), int(month_token), int(day_token))
    except Exception:
        return None


def _normalize_time_period(period: str) -> str:
    text = (period or "").strip()
    if text == "上":
        return "上午"
    if text == "下":
        return "下午"
    return text


def _time_from_period(period: str, hour_token: str, minute_token: str = "") -> Optional[tuple[int, int, str]]:
    label = _normalize_time_period(period)
    hour = _parse_number_token(hour_token)
    minute = _parse_number_token(minute_token) if (minute_token or "").strip() else 0
    if hour is None or minute is None:
        return None
    if label in {"下午", "晚上", "晚間", "傍晚", "夜間"} and hour != 12:
        hour += 12
    elif label == "中午" and hour < 11:
        hour += 12
    elif label in {"上午", "早上"} and hour == 12:
        hour = 0
    return hour, minute, label


def _time_from_period_compact(period: str, hour_or_time_token: str, minute_token: str = "") -> Optional[tuple[int, int, str, int]]:
    """Parse court compact times such as 下午1530 and 下午330.

    Returns (24h hour, minute, period label, display hour).
    """
    label = _normalize_time_period(period)
    raw = (hour_or_time_token or "").strip()
    minute_text = (minute_token or "").strip()
    if raw.isdigit() and not minute_text and len(raw) in {3, 4}:
        hour = int(raw[:-2])
        minute = int(raw[-2:])
        if hour >= 13:
            display_hour = hour - 12 if label in {"下午", "晚上", "晚間", "傍晚", "夜間"} else hour
            return hour, minute, label, display_hour
        parsed = _time_from_period(label, str(hour), str(minute))
        if parsed is None:
            return None
        hour24, minute24, period_label = parsed
        return hour24, minute24, period_label, hour
    parsed = _time_from_period(label, raw, minute_text)
    if parsed is None:
        return None
    hour24, minute, period_label = parsed
    display_hour = _parse_number_token(raw) or hour24
    if display_hour >= 13 and period_label in {"下午", "晚上", "晚間", "傍晚", "夜間"}:
        display_hour -= 12
    return hour24, minute, period_label, display_hour


def _duration_days(match: re.Match) -> Optional[int]:
    try:
        days = _parse_number_token(match.group(1))
    except IndexError:
        return None
    if days is None:
        return None
    unit = ""
    try:
        unit = str(match.group(2) or "")
    except IndexError:
        unit = ""
    if unit in {"週", "周"}:
        days *= 7
    return int(days)


@lru_cache(maxsize=1)
def _load_custom_holidays() -> Dict[date, str]:
    """Load the same custom holiday shape used by original OSC.

    Original desktop OSC stores holidays_config.json beside the app.  MAGI may
    run from source, runtime, or a bundled app, so we check a small set of stable
    candidate locations and accept either the original year/section format or a
    flat YYYY-MM-DD mapping.
    """
    candidates: list[str] = []
    for env_name in ("OSC_HOLIDAYS_CONFIG", "MAGI_HOLIDAYS_CONFIG", "HOLIDAYS_CONFIG"):
        raw = os.environ.get(env_name)
        if raw:
            candidates.append(raw)
    here = os.path.abspath(__file__)
    roots = [
        os.getcwd(),
        os.path.dirname(here),
        os.path.dirname(os.path.dirname(here)),
        os.path.dirname(os.path.dirname(os.path.dirname(here))),
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here)))),
    ]
    for root in roots:
        candidates.append(os.path.join(root, "holidays_config.json"))
        candidates.append(os.path.join(root, "json", "holidays_config.json"))

    out: Dict[date, str] = {}
    for raw_path in candidates:
        path = os.path.expanduser(str(raw_path or ""))
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
            if not isinstance(data, dict):
                continue
            for key, value in data.items():
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(key)):
                    try:
                        out[datetime.strptime(str(key), "%Y-%m-%d").date()] = str(value or "")
                    except Exception:
                        pass
                    continue
                if not str(key).isdigit() or not isinstance(value, dict):
                    continue
                for section in ("連假", "臨時假日"):
                    section_data = value.get(section)
                    if not isinstance(section_data, dict):
                        continue
                    for date_str, name in section_data.items():
                        try:
                            out[datetime.strptime(str(date_str), "%Y-%m-%d").date()] = str(name or "")
                        except Exception:
                            pass
        except Exception:
            logging.getLogger(__name__).debug("failed to load custom holidays: %s", path, exc_info=True)
    return out


def _resolve_taiwan_holidays(base_year: int):
    if holidays is None:
        class _FallbackHolidayCalendar:
            def get(self, _d: date):
                return None

        return _FallbackHolidayCalendar()
    try:
        return holidays.Taiwan(years=range(base_year - 1, base_year + 3))
    except Exception:
        class _FallbackHolidayCalendar:
            def get(self, _d: date):
                return None

        return _FallbackHolidayCalendar()


def is_tw_holiday(d: date, tw) -> bool:
    if d in _load_custom_holidays():
        return True
    name = tw.get(d)
    if name:
        if "補行上班日" in str(name):
            return False
        return True
    return (d.weekday() >= 5)


def next_workday(dt: datetime, tw: holidays.Taiwan) -> datetime:
    d = dt.date()
    while is_tw_holiday(d, tw):
        d = d + timedelta(days=1)
    return datetime.combine(d, dt.time())


def _filename_segments(filename: str) -> List[str]:
    stem = os.path.splitext(_portable_basename(filename or ""))[0]
    return [seg.strip() for seg in re.split(r"[；;]", stem) if seg.strip()] or [stem]


def _is_judgment_folder(file_path: str) -> bool:
    return "判決書" in str(file_path or "")


def _contains_exclusion(segment: str, pattern: str, todo_type: str) -> bool:
    if not any(phrase in segment for phrase in _EXCLUSION_PHRASES):
        return False
    if (
        (pattern in _ACTION_KEYWORDS_WITH_PRECEDENCE or todo_type in _ACTION_KEYWORDS_WITH_PRECEDENCE)
        and any(phrase in segment for phrase in _REJECTION_PHRASES)
    ):
        return False
    return True


def _relative_days_from_segment(segment: str) -> Optional[int]:
    text = segment or ""
    patterns = [
        rf"文到(?:後)?{_DURATION_RE}(?:內)?",
        rf"送達(?:後|翌日起){_DURATION_RE}(?:內)?",
        rf"{_DURATION_RE}內",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        days = _parse_number_token(m.group(1))
        if days is None:
            continue
        unit = ""
        if len(m.groups()) >= 2:
            unit = str(m.group(2) or "")
        if unit in {"週", "周"}:
            days *= 7
        return int(days)
    return None


def get_default_patterns() -> Dict[str, List[Dict]]:
    return {
        "補正": [
            {"pattern": rf"應於本裁定送達後{_DURATION_RE}內補正", "pattern_type": "relative", "days": None},
            {"pattern": rf"應於本裁定送達後{_DURATION_RE}內.*?補正", "pattern_type": "relative", "days": None},
            {"pattern": rf"請於文到(?:後)?{_DURATION_RE}內補正", "pattern_type": "relative", "days": None},
            {"pattern": rf"文到(?:後)?{_DURATION_RE}內.*?補正", "pattern_type": "relative", "days": None},
            {"pattern": rf"命.+?於{_DURATION_RE}內補正", "pattern_type": "relative", "days": None},
            {"pattern": rf"應於{_DURATION_RE}內補正", "pattern_type": "relative", "days": None},
            {"pattern": rf"應於{_DURATION_RE}內.*?補正", "pattern_type": "relative", "days": None},
            {"pattern": rf"{_DURATION_RE}內補正", "pattern_type": "relative", "days": None},
            {"pattern": rf"{_DURATION_RE}內.*?補正", "pattern_type": "relative", "days": None},
            {"pattern": "補正", "pattern_type": "relative", "days": None},
            {"pattern": "補陳", "pattern_type": "relative", "days": None},
            {"pattern": "補提", "pattern_type": "relative", "days": None},
            {"pattern": "補件", "pattern_type": "relative", "days": None},
        ],
        "上訴": [
            {"pattern": rf"上訴期間.*?送達.*?{_DURATION_RE}內", "pattern_type": "relative", "days": None},
            {"pattern": rf"如不服本判決.*?{_DURATION_RE}內.*?上訴", "pattern_type": "relative", "days": None},
            {"pattern": rf"應於判決送達後{_DURATION_RE}內提起上訴", "pattern_type": "relative", "days": None},
            {"pattern": rf"{_DURATION_RE}內提起上訴", "pattern_type": "relative", "days": None},
            {"pattern": "判決", "pattern_type": "fixed", "days": 20},
        ],
        "抗告": [
            {"pattern": "羈押裁定", "pattern_type": "fixed", "days": 10},
            {"pattern": "民事裁定", "pattern_type": "fixed", "days": 10},
            {"pattern": "刑事裁定", "pattern_type": "fixed", "days": 10},
            {"pattern": "家事裁定", "pattern_type": "fixed", "days": 10},
            {"pattern": "裁定", "pattern_type": "fixed", "days": 10},
        ],
        "再抗告": [
            {"pattern": "再抗告", "pattern_type": "relative", "days": None},
        ],
        "再議": [
            {"pattern": "不起訴處分書", "pattern_type": "fixed", "days": 10},
        ],
        "異議": [
            {"pattern": "異議", "pattern_type": "relative", "days": None},
            {"pattern": "支付命令", "pattern_type": "fixed", "days": 20},
            {"pattern": "司消債更字", "pattern_type": "fixed", "days": 10},
            {"pattern": "司消債清字", "pattern_type": "fixed", "days": 10},
            {"pattern": "司消債調字", "pattern_type": "fixed", "days": 10},
        ],
        "陳述意見": [
            {"pattern": rf"應於文到{_DURATION_RE}內陳述意見", "pattern_type": "relative", "days": None},
            {"pattern": rf"限於{_DURATION_RE}內.+?陳述意見", "pattern_type": "relative", "days": None},
            {"pattern": rf"文到{_DURATION_RE}內陳述意見", "pattern_type": "relative", "days": None},
            {"pattern": rf"{_DURATION_RE}內陳述意見", "pattern_type": "relative", "days": None},
        ],
        "陳報": [
            {"pattern": rf"(?:請)?(?:於)?(?:文到後|文到|送達翌日起|送達後){_DURATION_RE}內.*?(?:陳報|回覆|表示意見|確答|陳明)", "pattern_type": "relative", "days": None},
            {"pattern": rf"{_DURATION_RE}內.*?(?:陳報|回覆|表示意見|確答|陳明)", "pattern_type": "relative", "days": None},
            {"pattern": "陳報", "pattern_type": "relative", "days": None},
            {"pattern": "回復", "pattern_type": "relative", "days": None},
            {"pattern": "回覆", "pattern_type": "relative", "days": None},
        ],
        "提出資料": [
            {"pattern": rf"(?:請)?(?:於)?(?:文到後|文到|送達翌日起|送達後){_DURATION_RE}內.*?(?:提出|檢送|補提).{{0,20}}?(?:資料|文件|清冊|報告書|截圖|證據)", "pattern_type": "relative", "days": None},
            {"pattern": rf"{_DURATION_RE}內.*?(?:提出|檢送|補提).{{0,20}}?(?:資料|文件|清冊|報告書|截圖|證據)", "pattern_type": "relative", "days": None},
            {"pattern": "提出", "pattern_type": "relative", "days": None},
        ],
        "繳費": [
            {"pattern": rf"應於文到(?:後)?{_DURATION_RE}內繳納.*?(?:規費|裁判費)", "pattern_type": "relative", "days": None},
            {"pattern": rf"限{_DURATION_RE}內.*?繳納.*?(?:裁判費|規費)", "pattern_type": "relative", "days": None},
            {"pattern": rf"{_DURATION_RE}內繳納.*?(?:裁判費|規費)", "pattern_type": "relative", "days": None},
        ],
        "閱卷期限": [
            {"pattern": rf"應於{_DURATION_RE}內.*?閱卷", "pattern_type": "relative", "days": None},
            {"pattern": rf"閱卷期限.*?{_DURATION_RE}", "pattern_type": "relative", "days": None},
            {"pattern": rf"{_DURATION_RE}.*?閱卷", "pattern_type": "relative", "days": None},
        ],
        "答辯": [
            {"pattern": "答辯", "pattern_type": "relative", "days": None},
        ],
        "聲請": [
            {"pattern": "聲請", "pattern_type": "relative", "days": None},
        ],
        "開庭": [
            {
                "pattern": rf"(?:定|訂)於?(?:民國)?(\d{{2,4}})年(\d{{1,2}})月(\d{{1,2}})日{_TIME_PERIOD_RE}{_TIME_NUMBER_RE}時([零一二三四五六七八九十\d]{{0,3}})(?:分|整)?.*?(開庭|準備程序|協商程序|言詞辯論|調解|審理|宣判|訊問|調查|辯論|閱卷)?",
                "pattern_type": "absolute_time_roc",
                "days": None,
            },
            {
                "pattern": rf"(?:定|訂)?於?(?<!年)(\d{{1,2}})月(\d{{1,2}})日{_TIME_PERIOD_RE}{_TIME_NUMBER_RE}時([零一二三四五六七八九十\d]{{0,3}})(?:分|整)?.*?(開庭|準備程序|協商程序|言詞辯論|調解|審理|宣判|訊問|調查|辯論|閱卷)?",
                "pattern_type": "absolute_time",
                "days": None,
            },
        ],
    }


def _infer_hearing_procedure_type(text: str) -> str:
    """Infer the court procedure label when the regex stops before the trailing words."""
    s = text or ""
    for label in _HEARING_LABELS:
        if label in s:
            if label in {"審判程序", "審理程序"}:
                return "審理"
            return label
    return ""


def _append_unique_todo(todos: List[Dict], todo: Dict) -> None:
    key = (str(todo.get("type") or ""), str(todo.get("date") or ""), str(todo.get("time") or ""))
    for existing in todos:
        existing_key = (
            str(existing.get("type") or ""),
            str(existing.get("date") or ""),
            str(existing.get("time") or ""),
        )
        if existing_key == key:
            return
    todos.append(todo)


def _todo_sort_key(todo: Dict) -> tuple[str, str, str]:
    return (
        str(todo.get("date") or "9999-12-31"),
        str(todo.get("time") or "00:00"),
        str(todo.get("type") or ""),
    )


def _infer_absolute_deadline_type(context: str) -> Optional[str]:
    text = context or ""
    data_request = re.search(
        r"(?:提出|檢送|檢附|補送|補提).{0,20}(?:資料|文件|清冊|報告書|截圖|證據)"
        r"|(?:資料|文件|清冊|報告書|截圖|證據).{0,20}(?:提出|檢送|檢附|補送|補提)",
        text,
    )
    ordered = [
        ("繳費", ("繳納", "繳費", "裁判費", "規費", "聲請費")),
        ("補正", ("補正", "補繳", "補提")),
        ("陳述意見", ("陳述意見",)),
        ("陳報", ("陳報", "回覆", "表示意見", "具狀表示", "確答", "陳明", "說明")),
        ("上訴", ("上訴",)),
        ("抗告", ("抗告",)),
        ("閱卷期限", ("閱卷",)),
    ]
    for todo_type, keywords in ordered:
        if any(keyword in text for keyword in keywords):
            return todo_type
    if data_request:
        return "提出資料"
    return None


def _extract_absolute_deadline_todos(filename: str, document_date: datetime) -> List[Dict]:
    """Extract court deadlines written as an exact ROC/AD date.

    Examples seen in OSC folders:
    - 114年4月21日前表示意見
    - 請惠予於115年6月3日前陳報
    """
    text = re.sub(r"\s+", "", filename or "")
    if not text:
        return []
    todos: List[Dict] = []
    date_pat = re.compile(
        r"(?:請惠予|應|請|命|限|惠予)?(?:於)?(?:民國)?(\d{2,4})年(\d{1,2})月(\d{1,2})日(?:以前|前)[，,、\s]*([^）)]{0,90})"
    )
    for m in date_pat.finditer(text):
        before = text[max(0, m.start() - 24) : m.start()]
        tail = m.group(4) or ""
        context = f"{before}{tail}"
        matched_text = m.group(0) or ""
        if not (
            re.match(r"(請惠予|應|請|命|限|惠予)", matched_text)
            or re.search(r"(請惠予|應於|請於|命於|限於|惠予於|應|請|命|限).{0,16}$", before)
        ):
            continue
        todo_type = _infer_absolute_deadline_type(context)
        if not todo_type:
            continue
        try:
            dt = datetime(_parse_roc_or_ad_year(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            continue
        if dt.date() < document_date.date() - timedelta(days=3650):
            continue
        _append_unique_todo(
            todos,
            {
                "type": todo_type,
                "deadline_type": todo_type,
                "file": filename,
                "source_file": filename,
                "source": "filename_absolute_date",
                "date": dt.strftime("%Y-%m-%d"),
                "datetime": dt,
                "time": "",
                "description": f"📝 {dt.month}月{dt.day}日前{todo_type}",
            },
        )

    separated_date_pat = re.compile(
        r"(?:期限|至|應於|限於|請於|命於|繳費期限|繳費日期)[:：]?\s*(\d{2,4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s*\d{1,2}[:：]\d{2})?"
    )
    for m in separated_date_pat.finditer(text):
        before = text[max(0, m.start() - 28) : m.start()]
        after = text[m.end() : m.end() + 40]
        context = f"{before}{after}"
        todo_type = _infer_absolute_deadline_type(context)
        if not todo_type:
            continue
        dt = _parse_separator_roc_or_ad_date(m.group(1), m.group(2), m.group(3))
        if not dt or dt.date() < document_date.date() - timedelta(days=3650):
            continue
        _append_unique_todo(
            todos,
            {
                "type": todo_type,
                "deadline_type": todo_type,
                "file": filename,
                "source_file": filename,
                "source": "filename_absolute_date",
                "date": dt.strftime("%Y-%m-%d"),
                "datetime": dt,
                "time": "",
                "description": f"📝 {dt.month}月{dt.day}日{todo_type}",
            },
        )

    compact_date_pat = re.compile(
        r"(?:繳費期限|繳費日期|期限|至|應於|限於|請於|命於)[:：]?\s*(\d{7,8})(?!\d)"
    )
    for m in compact_date_pat.finditer(text):
        before = text[max(0, m.start() - 28) : m.start()]
        after = text[m.end() : m.end() + 40]
        context = f"{before}{after}"
        todo_type = _infer_absolute_deadline_type(context)
        if not todo_type:
            continue
        dt = _parse_compact_roc_or_ad_date(m.group(1))
        if not dt or dt.date() < document_date.date() - timedelta(days=3650):
            continue
        _append_unique_todo(
            todos,
            {
                "type": todo_type,
                "deadline_type": todo_type,
                "file": filename,
                "source_file": filename,
                "source": "filename_absolute_date",
                "date": dt.strftime("%Y-%m-%d"),
                "datetime": dt,
                "time": "",
                "description": f"📝 {dt.month}月{dt.day}日{todo_type}",
            },
        )
    return sorted(todos, key=_todo_sort_key)


def _hearing_datetime(
    *,
    year_token: str,
    month_token: str,
    day_token: str,
    period_token: str,
    hour_token: str,
    minute_token: str,
    document_date: datetime,
    explicit_year: bool,
) -> Optional[tuple[datetime, str, int, int]]:
    try:
        year = _parse_roc_or_ad_year(year_token) if explicit_year else int(year_token)
        month = int(month_token)
        day = int(day_token)
        parsed_time = _time_from_period(period_token, hour_token, minute_token)
        if parsed_time is None:
            return None
        hour, minute, period_label = parsed_time
        dt = datetime(year, month, day, hour, minute)
        if not explicit_year and dt.date() < document_date.date() - timedelta(days=30):
            # Conservative: when fallback year is already from an older source hint,
            # keep that year to avoid rolling legacy cases into the future.
            if year >= document_date.year:
                dt = dt.replace(year=year + 1)
        original_hour = _parse_number_token(hour_token) or hour
        return dt, period_label, original_hour, minute
    except Exception:
        return None


def _make_hearing_todo(
    *,
    filename: str,
    kind: str,
    dt: datetime,
    period_label: str,
    original_hour: int,
    minute: int,
) -> Dict:
    todo_type = kind or "開庭"
    return {
        "type": todo_type,
        "deadline_type": todo_type,
        "file": filename,
        "source_file": filename,
        "description": f"⚖️ {dt.month}月{dt.day}日 {period_label}{original_hour}時{minute:02d}分 {todo_type}",
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M"),
        "datetime": dt,
    }


def _extract_hearing_sequence_todos(filename: str, document_date: datetime, base_year: Optional[int] = None) -> List[Dict]:
    """Extract every hearing datetime in filenames with multiple sessions."""
    kind = _infer_hearing_procedure_type(filename) or "開庭"
    year_for_yearless = base_year or document_date.year
    label_alt = "|".join(map(re.escape, _HEARING_LABELS))
    todos: List[Dict] = []

    explicit_pat = re.compile(
        rf"(?:民國)?(\d{{2,4}})年(\d{{1,2}})月(\d{{1,2}})日{_TIME_PERIOD_RE}{_TIME_NUMBER_RE}時([零一二三四五六七八九十\d]{{0,3}})(?:分|整)?"
    )
    for m in explicit_pat.finditer(filename):
        parsed = _hearing_datetime(
            year_token=m.group(1),
            month_token=m.group(2),
            day_token=m.group(3),
            period_token=m.group(4),
            hour_token=m.group(5),
            minute_token=m.group(6),
            document_date=document_date,
            explicit_year=True,
        )
        if parsed:
            dt, period_label, original_hour, minute = parsed
            _append_unique_todo(
                todos,
                _make_hearing_todo(
                    filename=filename,
                    kind=kind,
                    dt=dt,
                    period_label=period_label,
                    original_hour=original_hour,
                    minute=minute,
                ),
            )

    yearless_pat = re.compile(
        rf"(?<!年)(\d{{1,2}})月(\d{{1,2}})日{_TIME_PERIOD_RE}{_TIME_NUMBER_RE}時([零一二三四五六七八九十\d]{{0,3}})(?:分|整)?"
    )
    for m in yearless_pat.finditer(filename):
        parsed = _hearing_datetime(
            year_token=str(year_for_yearless),
            month_token=m.group(1),
            day_token=m.group(2),
            period_token=m.group(3),
            hour_token=m.group(4),
            minute_token=m.group(5),
            document_date=document_date,
            explicit_year=False,
        )
        if parsed:
            dt, period_label, original_hour, minute = parsed
            _append_unique_todo(
                todos,
                _make_hearing_todo(
                    filename=filename,
                    kind=kind,
                    dt=dt,
                    period_label=period_label,
                    original_hour=original_hour,
                    minute=minute,
                ),
            )

    compact_pat = re.compile(
        rf"(?<!\d)(\d{{7,8}}){_TIME_PERIOD_RE}(\d{{1,4}})(?:時([0-5]?\d))?(?:分|整)?.{{0,24}}?({label_alt})?"
    )
    for m in compact_pat.finditer(filename):
        dt = _parse_compact_roc_or_ad_date(m.group(1))
        if not dt:
            continue
        parsed_time = _time_from_period_compact(m.group(2), m.group(3), m.group(4) or "")
        if parsed_time is None:
            continue
        hour, minute, period_label, original_hour = parsed_time
        label = m.group(5) or kind or "開庭"
        dt = dt.replace(hour=hour, minute=minute)
        if dt.date() < document_date.date() - timedelta(days=3650):
            continue
        _append_unique_todo(
            todos,
            _make_hearing_todo(
                filename=filename,
                kind=label,
                dt=dt,
                period_label=period_label,
                original_hour=original_hour,
                minute=minute,
            ),
        )

    shared_time_pat = re.compile(
        rf"(?P<dates>(?:(?:\d{{2,4}}年)?\d{{1,2}}月\d{{1,2}}日[、，,及和\s]*){{2,}}){_TIME_PERIOD_RE}{_TIME_NUMBER_RE}時([零一二三四五六七八九十\d]{{0,3}})(?:分|整)?"
    )
    date_pat = re.compile(r"(?:(\d{2,4})年)?(\d{1,2})月(\d{1,2})日")
    for m in shared_time_pat.finditer(filename):
        period_token = m.group(2)
        hour_token = m.group(3)
        minute_token = m.group(4)
        for dm in date_pat.finditer(m.group("dates")):
            explicit_year = bool(dm.group(1))
            parsed = _hearing_datetime(
                year_token=dm.group(1) or str(year_for_yearless),
                month_token=dm.group(2),
                day_token=dm.group(3),
                period_token=period_token,
                hour_token=hour_token,
                minute_token=minute_token,
                document_date=document_date,
                explicit_year=explicit_year,
            )
            if parsed:
                dt, period_label, original_hour, minute = parsed
                _append_unique_todo(
                    todos,
                    _make_hearing_todo(
                        filename=filename,
                        kind=kind,
                        dt=dt,
                        period_label=period_label,
                        original_hour=original_hour,
                        minute=minute,
                    ),
                )

    explicit_date_only_pat = re.compile(
        rf"(?:定|訂)於?(?:民國)?(\d{{2,4}})年(\d{{1,2}})月(\d{{1,2}})日(?!上午|下午|早上|中午|晚上|晚間|傍晚|夜間|上|下).{{0,30}}?({label_alt})"
    )
    for m in explicit_date_only_pat.finditer(filename):
        if re.search(r"(?:上午|下午|早上|中午|晚上|晚間|傍晚|夜間|上|下).{0,8}時", m.group(0)):
            continue
        dt = _parse_separator_roc_or_ad_date(m.group(1), m.group(2), m.group(3))
        if not dt:
            continue
        label = "審理" if m.group(4) in {"審理程序", "審判程序"} else (m.group(4) or kind or "開庭")
        _append_unique_todo(
            todos,
            {
                "type": label,
                "deadline_type": label,
                "file": filename,
                "source_file": filename,
                "description": f"⚖️ {dt.month}月{dt.day}日 {label}",
                "date": dt.strftime("%Y-%m-%d"),
                "time": "",
                "datetime": dt,
            },
        )

    yearless_date_only_pat = re.compile(
        rf"(?:定|訂)?於?(\d{{1,2}})月(\d{{1,2}})日(?!上午|下午|早上|中午|晚上|晚間|傍晚|夜間|上|下).{{0,30}}?({label_alt})"
    )
    for m in yearless_date_only_pat.finditer(filename):
        if re.search(r"(?:上午|下午|早上|中午|晚上|晚間|傍晚|夜間|上|下).{0,8}時", m.group(0)):
            continue
        try:
            dt = datetime(year_for_yearless, int(m.group(1)), int(m.group(2)))
            if dt.date() < document_date.date() - timedelta(days=30):
                if year_for_yearless < document_date.year:
                    pass
                else:
                    dt = dt.replace(year=dt.year + 1)
        except Exception:
            continue
        label = "審理" if m.group(3) in {"審理程序", "審判程序"} else (m.group(3) or kind or "開庭")
        _append_unique_todo(
            todos,
            {
                "type": label,
                "deadline_type": label,
                "file": filename,
                "source_file": filename,
                "description": f"⚖️ {dt.month}月{dt.day}日 {label}",
                "date": dt.strftime("%Y-%m-%d"),
                "time": "",
                "datetime": dt,
            },
        )

    return sorted(todos, key=_todo_sort_key)


def _extract_todo_from_filename(filename: str) -> Optional[Dict]:
    """Extract todo type and deadline from pdf-namer bracket supplemental info.

    Parses the bracket section of a filename like:
      20241015 裁定（王大明；應於15日內補正）.pdf
    Returns dict with deadline_type and days, or None if not matched.
    """
    m = re.search(r"[（(]([^）)]+)[）)]", filename)
    if not m:
        return None
    bracket_text = m.group(1)

    _BRACKET_PATTERNS = [
        (rf"{_DURATION_RE}內補正", "補正"),
        (rf"{_DURATION_RE}內.*?補正", "補正"),
        (rf"{_DURATION_RE}內上訴", "上訴"),
        (rf"{_DURATION_RE}內陳述意見", "陳述意見"),
        (rf"{_DURATION_RE}內.*?(?:陳報|回覆|表示意見|確答|陳明)", "陳報"),
        (rf"{_DURATION_RE}內.*?(?:提出|檢送|補提).{{0,20}}?(?:資料|文件|清冊|報告書|截圖|證據)", "提出資料"),
        (rf"{_DURATION_RE}內繳納", "繳費"),
        (rf"{_DURATION_RE}內閱卷", "閱卷期限"),
    ]
    for pat, dtype in _BRACKET_PATTERNS:
        pm = re.search(pat, bracket_text)
        if pm:
            days = _duration_days(pm)
            if days is not None:
                return {"deadline_type": dtype, "days": int(days), "source": "filename_bracket"}
    return None


def extract_todos_from_filename(
    filename: str,
    file_path: str = "",
    *,
    patterns: Optional[Dict[str, List[Dict]]] = None,
) -> List[Dict]:
    """
    OSC-compatible todo extraction from filename (headless).
    """
    todos: List[Dict] = []

    document_date = extract_document_date_from_filename(filename, file_path)
    if not document_date:
        # As a last resort, treat as "today" to avoid crashing; caller can override by renaming.
        document_date = datetime.now()

    base_year = extract_base_year_from_filename(filename, file_path, document_date)
    tw = _resolve_taiwan_holidays(base_year)

    all_patterns = patterns or get_default_patterns()
    type_priority = [
        "繳費", "補正", "開庭", "準備程序", "協商程序", "審理程序", "言詞辯論",
        "調解", "陳報", "提出資料", "陳述意見", "閱卷期限", "閱卷", "答辯", "訊問",
        "異議", "抗告", "上訴", "再抗告", "再議", "聲請",
    ]

    segments = _filename_segments(filename)
    fixed_patterns: List[tuple[str, Dict, str]] = []
    matched_types: set[str] = set()
    matched_relative_segments: set[str] = set()
    for todo_type in type_priority:
        if todo_type not in all_patterns:
            continue

        for pattern_data in all_patterns[todo_type]:
            pattern = pattern_data["pattern"]
            pattern_type = pattern_data.get("pattern_type", "")
            if pattern_type == "fixed":
                fixed_patterns.append((todo_type, pattern_data, pattern))
                continue
            try:
                matched_segment = ""
                matched: Optional[re.Match] = None
                for segment in segments:
                    m = re.search(pattern, segment, re.IGNORECASE)
                    if m:
                        matched = m
                        matched_segment = segment
                        break
                if not matched:
                    continue
                m = matched
                if _contains_exclusion(matched_segment, pattern, todo_type):
                    continue
                if pattern_type in ("relative", "relative_chinese") and matched_segment in matched_relative_segments:
                    continue
                if todo_type in matched_types:
                    break

                todo: Dict = {"type": todo_type, "deadline_type": todo_type, "file": filename, "source_file": filename}
                preset_days = pattern_data.get("days")

                if pattern_type in ("relative", "relative_chinese"):
                    if preset_days is not None:
                        days = int(preset_days)
                    else:
                        parsed_days = _duration_days(m)
                        if parsed_days is None:
                            parsed_days = _relative_days_from_segment(matched_segment)
                        if parsed_days is None:
                            continue
                        days = int(parsed_days)
                    deadline = document_date + timedelta(days=days)
                    adjusted = next_workday(deadline, tw)
                    todo["date"] = adjusted.strftime("%Y-%m-%d")
                    todo["datetime"] = adjusted
                    todo["time"] = ""
                    todo["description"] = f"📝 {days}日內{todo_type} ({document_date.strftime('%m/%d')}文到)"
                    _append_unique_todo(todos, todo)
                    matched_types.add(todo_type)
                    matched_relative_segments.add(matched_segment)

                elif pattern_type in ("absolute", "absolute_time", "absolute_time_roc"):
                    if pattern_type == "absolute_time_roc":
                        year_to_use = _parse_roc_or_ad_year(m.group(1))
                        month, day = int(m.group(2)), int(m.group(3))
                        period_group = 4
                    else:
                        month, day = int(m.group(1)), int(m.group(2))
                        year_to_use = base_year
                        period_group = 3

                    dt = datetime(year_to_use, month, day, 9, 0)
                    if pattern_type == "absolute_time" and dt.date() < document_date.date() - timedelta(days=30) and year_to_use >= document_date.year:
                        dt = dt.replace(year=year_to_use + 1)

                    if pattern_type in ("absolute_time", "absolute_time_roc") and len(m.groups()) >= period_group + 1:
                        period = m.group(period_group)
                        hour_str = m.group(period_group + 1)
                        minute_str = m.group(period_group + 2) if len(m.groups()) >= period_group + 2 and m.group(period_group + 2) else "0"
                        proc = m.group(period_group + 3) if len(m.groups()) >= period_group + 3 else ""
                        if not proc:
                            proc = _infer_hearing_procedure_type(matched_segment or filename)
                        if proc and proc != "開庭":
                            todo["type"] = proc
                            todo["deadline_type"] = proc
                        parsed_time = _time_from_period(period, hour_str, minute_str)
                        if parsed_time is None:
                            continue
                        hour, minute, period_label = parsed_time
                        original_hour = _parse_number_token(hour_str) or hour
                        dt = dt.replace(hour=hour, minute=minute)
                        todo["description"] = f"⚖️ {month}月{day}日 {period_label}{original_hour}時{minute:02d}分 {todo['type']}"
                    else:
                        todo["description"] = f"⚖️ {month}月{day}日 {todo_type}"

                    todo["date"] = dt.strftime("%Y-%m-%d")
                    todo["time"] = dt.strftime("%H:%M")
                    todo["datetime"] = dt
                    _append_unique_todo(todos, todo)
                    matched_types.add(todo_type)

                break
            except (re.error, ValueError, IndexError):
                continue

    if not matched_types and _is_judgment_folder(file_path):
        fixed_priority = {
            "司消債更字": 1,
            "司消債清字": 1,
            "司消債調字": 1,
            "羈押裁定": 3,
            "不起訴處分書": 3,
            "民事裁定": 5,
            "刑事裁定": 5,
            "家事裁定": 5,
            "支付命令": 6,
            "裁定": 8,
            "判決": 10,
        }
        fixed_patterns.sort(key=lambda item: (fixed_priority.get(item[2], 99), -len(item[2])))
        for todo_type, pattern_data, pattern in fixed_patterns:
            if pattern not in filename:
                continue
            if pattern in _FIXED_CONSUMER_DEBT_PATTERNS and "裁定" not in filename:
                continue
            if _contains_exclusion(filename, pattern, todo_type):
                continue
            try:
                days = int(pattern_data.get("days") or 0)
            except Exception:
                days = 0
            if days <= 0:
                continue
            adjusted = next_workday(document_date + timedelta(days=days), tw)
            _append_unique_todo(
                todos,
                {
                    "type": todo_type,
                    "deadline_type": todo_type,
                    "file": filename,
                    "source_file": filename,
                    "source": "filename_fixed_judgment_folder",
                    "date": adjusted.strftime("%Y-%m-%d"),
                    "datetime": adjusted,
                    "time": "",
                    "description": f"📝 {days}日內{todo_type} ({document_date.strftime('%m/%d')}文到)",
                },
            )
            matched_types.add(todo_type)
            break

    if not matched_types:
        bracket_todo = _extract_todo_from_filename(filename)
        if bracket_todo:
            days = int(bracket_todo["days"])
            deadline = document_date + timedelta(days=days)
            adjusted = next_workday(deadline, tw)
            _append_unique_todo(todos, {
                "type": bracket_todo["deadline_type"],
                "deadline_type": bracket_todo["deadline_type"],
                "file": filename,
                "source_file": filename,
                "source": bracket_todo.get("source", "filename_bracket"),
                "date": adjusted.strftime("%Y-%m-%d"),
                "datetime": adjusted,
                "time": "",
                "description": f"📝 {days}日內{bracket_todo['deadline_type']} ({document_date.strftime('%m/%d')}文到)",
            })

    for todo in _extract_absolute_deadline_todos(filename, document_date):
        _append_unique_todo(todos, todo)

    for todo in _extract_hearing_sequence_todos(filename, document_date, base_year):
        _append_unique_todo(todos, todo)

    return sorted(todos, key=_todo_sort_key)
