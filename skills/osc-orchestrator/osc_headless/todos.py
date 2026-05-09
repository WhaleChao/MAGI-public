# -*- coding: utf-8 -*-
"""
OSC headless todo extraction.

This intentionally focuses on filename-based parsing because:
- The OSC workflow relies on correct "收文日/文到日" for relative deadlines.
- pdf-namer already normalizes filenames to include YYYYMMDD.
"""

from __future__ import annotations
import logging

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import holidays


def extract_document_date_from_filename(filename: str, file_path: str = "") -> Optional[datetime]:
    """
    Extract "document received date" from filename.
    Priority:
    - Prefix YYYYMMDD
    - Prefix YYYY-MM-DD / YYYY.MM.DD
    """
    name = os.path.basename(filename or "")
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

    # Fallback: try file mtime (only if exists)
    if file_path and os.path.exists(file_path):
        try:
            return datetime.fromtimestamp(os.path.getmtime(file_path))
        except Exception:
            return None
    return None


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


def is_tw_holiday(d: date, tw: holidays.Taiwan) -> bool:
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


def get_default_patterns() -> Dict[str, List[Dict]]:
    return {
        "補正": [
            {"pattern": r"應於本裁定送達後(\d+)日內補正", "pattern_type": "relative", "days": None},
            {"pattern": r"請於文到(\d+)日內補正", "pattern_type": "relative", "days": None},
            {"pattern": r"文到(\d+)日內.*?補正", "pattern_type": "relative", "days": None},
            {"pattern": r"命.+?於(\d+)日內補正", "pattern_type": "relative", "days": None},
            {"pattern": r"應於(\d+)日內補正", "pattern_type": "relative", "days": None},
            {"pattern": r"(\d+)日內補正", "pattern_type": "relative", "days": None},
        ],
        "上訴": [
            {"pattern": r"上訴期間.*?送達.*?(\d+)日內", "pattern_type": "relative", "days": None},
            {"pattern": r"如不服本判決.*?(\d+)日內.*?上訴", "pattern_type": "relative", "days": None},
            {"pattern": r"應於判決送達後(\d+)日內提起上訴", "pattern_type": "relative", "days": None},
            {"pattern": r"(\d+)日內提起上訴", "pattern_type": "relative", "days": None},
        ],
        "陳述意見": [
            {"pattern": r"應於文到(\d+)日內陳述意見", "pattern_type": "relative", "days": None},
            {"pattern": r"限於(\d+)日內.+?陳述意見", "pattern_type": "relative", "days": None},
            {"pattern": r"文到(\d+)日內陳述意見", "pattern_type": "relative", "days": None},
            {"pattern": r"(\d+)日內陳述意見", "pattern_type": "relative", "days": None},
        ],
        "繳費": [
            {"pattern": r"應於文到(\d+)日內繳納.*?(?:規費|裁判費)", "pattern_type": "relative", "days": None},
            {"pattern": r"限(\d+)日內.*?繳納.*?(?:裁判費|規費)", "pattern_type": "relative", "days": None},
            {"pattern": r"(\d+)日內繳納.*?(?:裁判費|規費)", "pattern_type": "relative", "days": None},
        ],
        "閱卷期限": [
            {"pattern": r"應於(\d+)日內.*?閱卷", "pattern_type": "relative", "days": None},
            {"pattern": r"閱卷期限.*?(\d+)日", "pattern_type": "relative", "days": None},
            {"pattern": r"(\d+)日.*?閱卷", "pattern_type": "relative", "days": None},
        ],
        "開庭": [
            {
                "pattern": r"(?:定|訂)於?(?:民國)?(\d{2,3})年(\d{1,2})月(\d{1,2})日([上下])午(\d{1,2})時(\d*)分?.*?(開庭|準備程序|言詞辯論|調解|審理|宣判)?",
                "pattern_type": "absolute_time_roc",
                "days": None,
            },
            {
                "pattern": r"(?:定|訂)?於?(\d{1,2})月(\d{1,2})日([上下])午(\d{1,2})時(\d*)分?.*?(開庭|準備程序|言詞辯論|調解|審理|宣判)?",
                "pattern_type": "absolute_time",
                "days": None,
            },
        ],
    }


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
        (r"(\d+)日內補正", "補正"),
        (r"(\d+)日內上訴", "上訴"),
        (r"(\d+)日內陳述意見", "陳述意見"),
        (r"(\d+)日內繳納", "繳費"),
        (r"(\d+)日內閱卷", "閱卷期限"),
    ]
    for pat, dtype in _BRACKET_PATTERNS:
        pm = re.search(pat, bracket_text)
        if pm:
            return {"deadline_type": dtype, "days": int(pm.group(1)), "source": "filename_bracket"}
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

    base_year = document_date.year
    tw = holidays.Taiwan(years=range(base_year - 1, base_year + 3))

    all_patterns = patterns or get_default_patterns()
    type_priority = [
        "繳費", "補正", "開庭", "準備程序", "審理程序", "言詞辯論",
        "陳報", "提出資料", "陳述意見", "閱卷期限", "閱卷", "答辯", "訊問",
        "異議", "抗告", "上訴", "再抗告",
    ]

    matched = False
    for todo_type in type_priority:
        if matched:
            break
        if todo_type not in all_patterns:
            continue

        for pattern_data in all_patterns[todo_type]:
            pattern = pattern_data["pattern"]
            try:
                m = re.search(pattern, filename, re.IGNORECASE)
                if not m:
                    continue
                matched = True

                todo: Dict = {"type": todo_type, "deadline_type": todo_type, "file": filename, "source_file": filename}
                pattern_type = pattern_data.get("pattern_type", "")
                preset_days = pattern_data.get("days")

                if pattern_type in ("relative", "relative_chinese"):
                    if preset_days is not None:
                        days = int(preset_days)
                    else:
                        if pattern_type == "relative_chinese":
                            days = int(chinese_to_number(m.group(1)) or 0)
                        else:
                            days = int(m.group(1))
                    deadline = document_date + timedelta(days=days)
                    adjusted = next_workday(deadline, tw)
                    todo["date"] = adjusted.strftime("%Y-%m-%d")
                    todo["datetime"] = adjusted
                    todo["time"] = ""
                    todo["description"] = f"📝 {days}日內{todo_type} ({document_date.strftime('%m/%d')}文到)"
                    todos.append(todo)

                elif pattern_type in ("absolute", "absolute_time", "absolute_time_roc"):
                    if pattern_type == "absolute_time_roc":
                        year_to_use = int(m.group(1)) + 1911
                        month, day = int(m.group(2)), int(m.group(3))
                        period_group = 4
                    else:
                        month, day = int(m.group(1)), int(m.group(2))
                        year_to_use = base_year
                        period_group = 3

                        roc_match = re.search(r"(\d{3})年度?", filename)
                        if roc_match:
                            explicit_year = int(roc_match.group(1)) + 1911
                            if abs(explicit_year - year_to_use) < 2:
                                year_to_use = explicit_year

                    dt = datetime(year_to_use, month, day, 9, 0)

                    if pattern_type in ("absolute_time", "absolute_time_roc") and len(m.groups()) >= period_group + 1:
                        period = m.group(period_group)
                        hour_str = m.group(period_group + 1)
                        minute_str = m.group(period_group + 2) if len(m.groups()) >= period_group + 2 and m.group(period_group + 2) else "0"
                        proc = m.group(period_group + 3) if len(m.groups()) >= period_group + 3 else ""
                        if proc and proc != "開庭":
                            todo["type"] = proc
                            todo["deadline_type"] = proc
                        hour, minute = int(hour_str), int(minute_str)
                        original_hour = hour
                        if period == "下" and hour != 12:
                            hour += 12
                        elif period == "上" and hour == 12:
                            hour = 0
                        dt = dt.replace(hour=hour, minute=minute)
                        todo["description"] = f"⚖️ {month}月{day}日 {period}午{original_hour}時{minute:02d}分 {todo['type']}"
                    else:
                        todo["description"] = f"⚖️ {month}月{day}日 {todo_type}"

                    todo["date"] = dt.strftime("%Y-%m-%d")
                    todo["time"] = dt.strftime("%H:%M")
                    todo["datetime"] = dt
                    todos.append(todo)

                break
            except (re.error, ValueError, IndexError):
                continue

    if not matched:
        bracket_todo = _extract_todo_from_filename(filename)
        if bracket_todo:
            days = int(bracket_todo["days"])
            deadline = document_date + timedelta(days=days)
            adjusted = next_workday(deadline, tw)
            todos.append({
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

    return todos
