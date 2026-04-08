# -*- coding: utf-8 -*-
"""
eventkit_bridge.py
==================
Apple Calendar 事件 + Apple 提醒事項整合模組。

透過 osascript 在 Apple Calendar 建立事件、在提醒事項建立待辦。
補充 OSC/Google Calendar，提供離線可用 + iPhone/Apple Watch 同步。

整合點：
- skills/legal/trial-prep/：偵測到開庭通知時自動建立行事曆事件
- skills/legal/laf-portal-automation/：法扶案件結案期限自動建立提醒
- pipelines/command_dispatch.py：新增指令 !開庭 [案號] [日期]

注意：庭通知書 → Google Calendar 已由 OSC 的 todo_sync + gcal_sync 處理，
本模組的價值在於 Apple 提醒事項（離線可用 + Apple Watch 推播）。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("EventKitBridge")

# ---------------------------------------------------------------------------
# 預設行事曆 / 提醒事項清單名稱
# ---------------------------------------------------------------------------
DEFAULT_CALENDAR = "MAGI 開庭"
DEFAULT_REMINDER_LIST = "MAGI 待辦"

# ---------------------------------------------------------------------------
# AppleScript 工具函式
# ---------------------------------------------------------------------------

def _run_osascript(script: str, timeout: int = 10) -> tuple[bool, str]:
    """
    執行 AppleScript，回傳 (成功, 輸出)。

    Args:
        script: AppleScript 腳本內容
        timeout: 最大等待秒數

    Returns:
        (success: bool, output: str)
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        logger.warning("osascript error (rc=%d): %s", result.returncode, result.stderr.strip())
        return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        logger.error("osascript timed out after %ds", timeout)
        return False, "timeout"
    except FileNotFoundError:
        logger.error("osascript not found — not running on macOS?")
        return False, "osascript not found"


def _escape_applescript(text: str) -> str:
    """轉義 AppleScript 字串中的特殊字元。"""
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Apple Calendar 事件
# ---------------------------------------------------------------------------

def ensure_calendar_exists(calendar_name: str = DEFAULT_CALENDAR) -> bool:
    """確認行事曆存在，不存在則建立。"""
    check_script = f'''
    tell application "Calendar"
        try
            set cal to calendar "{_escape_applescript(calendar_name)}"
            return "exists"
        on error
            make new calendar with properties {{name:"{_escape_applescript(calendar_name)}"}}
            return "created"
        end try
    end tell
    '''
    ok, output = _run_osascript(check_script)
    if ok:
        logger.info("Calendar '%s': %s", calendar_name, output)
    return ok


def create_calendar_event(
    title: str,
    start: datetime,
    end: Optional[datetime] = None,
    location: str = "",
    notes: str = "",
    calendar_name: str = DEFAULT_CALENDAR,
    all_day: bool = False,
) -> bool:
    """
    透過 osascript 在 Apple Calendar 建立事件。

    Args:
        title: 事件標題
        start: 開始時間
        end: 結束時間（預設 start + 2 小時）
        location: 地點
        notes: 備註
        calendar_name: 行事曆名稱
        all_day: 是否為全天事件

    Returns:
        True if event was created successfully
    """
    if end is None:
        end = start + timedelta(hours=2)

    title_esc = _escape_applescript(title)
    location_esc = _escape_applescript(location)
    notes_esc = _escape_applescript(notes)
    cal_esc = _escape_applescript(calendar_name)

    # 用 AppleScript date 格式
    start_str = start.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%S")

    if all_day:
        script = f'''
        tell application "Calendar"
            tell calendar "{cal_esc}"
                set startDate to current date
                set year of startDate to {start.year}
                set month of startDate to {start.month}
                set day of startDate to {start.day}
                set hours of startDate to 0
                set minutes of startDate to 0
                set seconds of startDate to 0
                set endDate to startDate + (1 * days)
                make new event with properties {{summary:"{title_esc}", start date:startDate, end date:endDate, allday event:true, location:"{location_esc}", description:"{notes_esc}"}}
            end tell
        end tell
        '''
    else:
        script = f'''
        tell application "Calendar"
            tell calendar "{cal_esc}"
                set startDate to current date
                set year of startDate to {start.year}
                set month of startDate to {start.month}
                set day of startDate to {start.day}
                set hours of startDate to {start.hour}
                set minutes of startDate to {start.minute}
                set seconds of startDate to 0
                set endDate to current date
                set year of endDate to {end.year}
                set month of endDate to {end.month}
                set day of endDate to {end.day}
                set hours of endDate to {end.hour}
                set minutes of endDate to {end.minute}
                set seconds of endDate to 0
                make new event with properties {{summary:"{title_esc}", start date:startDate, end date:endDate, location:"{location_esc}", description:"{notes_esc}"}}
            end tell
        end tell
        '''

    ok, _ = _run_osascript(script, timeout=15)
    if ok:
        logger.info("Calendar event created: %s at %s", title, start_str)
    return ok


def check_event_exists(
    title: str,
    date: datetime,
    calendar_name: str = DEFAULT_CALENDAR,
) -> bool:
    """
    檢查指定日期是否已有相同標題的事件（防止重複建立）。

    Args:
        title: 事件標題
        date: 查詢日期
        calendar_name: 行事曆名稱

    Returns:
        True if event exists
    """
    title_esc = _escape_applescript(title)
    cal_esc = _escape_applescript(calendar_name)

    script = f'''
    tell application "Calendar"
        tell calendar "{cal_esc}"
            set startOfDay to current date
            set year of startOfDay to {date.year}
            set month of startOfDay to {date.month}
            set day of startOfDay to {date.day}
            set hours of startOfDay to 0
            set minutes of startOfDay to 0
            set seconds of startOfDay to 0
            set endOfDay to startOfDay + (1 * days)
            set matchingEvents to (every event whose summary is "{title_esc}" and start date >= startOfDay and start date < endOfDay)
            return (count of matchingEvents) as text
        end tell
    end tell
    '''
    ok, output = _run_osascript(script)
    if ok:
        try:
            return int(output) > 0
        except ValueError:
            pass
    return False


# ---------------------------------------------------------------------------
# Apple 提醒事項
# ---------------------------------------------------------------------------

def ensure_reminder_list_exists(list_name: str = DEFAULT_REMINDER_LIST) -> bool:
    """確認提醒事項清單存在，不存在則建立。"""
    script = f'''
    tell application "Reminders"
        try
            set rl to list "{_escape_applescript(list_name)}"
            return "exists"
        on error
            make new list with properties {{name:"{_escape_applescript(list_name)}"}}
            return "created"
        end try
    end tell
    '''
    ok, output = _run_osascript(script)
    if ok:
        logger.info("Reminder list '%s': %s", list_name, output)
    return ok


def create_reminder(
    title: str,
    due_date: Optional[datetime] = None,
    notes: str = "",
    list_name: str = DEFAULT_REMINDER_LIST,
    priority: int = 0,
) -> bool:
    """
    在 Apple 提醒事項建立待辦。

    Args:
        title: 提醒標題
        due_date: 截止日期（可選）
        notes: 備註（案件資訊等）
        list_name: 提醒事項清單名稱
        priority: 優先等級（0=無, 1=低, 5=中, 9=高）

    Returns:
        True if reminder was created successfully
    """
    title_esc = _escape_applescript(title)
    notes_esc = _escape_applescript(notes)
    list_esc = _escape_applescript(list_name)

    props = [f'name:"{title_esc}"', f'body:"{notes_esc}"']
    if priority > 0:
        props.append(f"priority:{priority}")

    if due_date:
        script = f'''
        tell application "Reminders"
            tell list "{list_esc}"
                set dueD to current date
                set year of dueD to {due_date.year}
                set month of dueD to {due_date.month}
                set day of dueD to {due_date.day}
                set hours of dueD to {due_date.hour}
                set minutes of dueD to {due_date.minute}
                set seconds of dueD to 0
                make new reminder with properties {{{", ".join(props)}, due date:dueD}}
            end tell
        end tell
        '''
    else:
        script = f'''
        tell application "Reminders"
            tell list "{list_esc}"
                make new reminder with properties {{{", ".join(props)}}}
            end tell
        end tell
        '''

    ok, _ = _run_osascript(script, timeout=15)
    if ok:
        logger.info("Reminder created: %s (due: %s)", title, due_date)
    return ok


# ---------------------------------------------------------------------------
# 法律業務便捷函式
# ---------------------------------------------------------------------------

def create_trial_events(
    case_number: str,
    trial_date: datetime,
    court: str = "",
    client: str = "",
    calendar_name: str = DEFAULT_CALENDAR,
    reminder_list: str = DEFAULT_REMINDER_LIST,
) -> list[str]:
    """
    根據開庭資訊自動建立行事曆事件 + 提醒。

    建立：
    1. 開庭事件（Apple Calendar）
    2. 開庭前 3 天提醒（Apple 提醒事項）
    3. 開庭前 1 天提醒（Apple 提醒事項）

    Args:
        case_number: 案號
        trial_date: 開庭日期時間
        court: 法院名稱
        client: 當事人姓名
        calendar_name: 行事曆名稱
        reminder_list: 提醒事項清單名稱

    Returns:
        建立成功的項目列表
    """
    created = []

    event_title = f"開庭：{case_number}"
    notes = f"當事人：{client}" if client else ""

    # 檢查是否已存在
    if check_event_exists(event_title, trial_date, calendar_name):
        logger.info("Event already exists: %s on %s", event_title, trial_date.date())
        return ["已存在（跳過）"]

    # 確保行事曆和提醒清單存在
    ensure_calendar_exists(calendar_name)
    ensure_reminder_list_exists(reminder_list)

    # 1. 開庭事件
    if create_calendar_event(
        title=event_title,
        start=trial_date,
        end=trial_date + timedelta(hours=2),
        location=court,
        notes=notes,
        calendar_name=calendar_name,
    ):
        created.append("開庭事件")

    # 2. 開庭前 3 天提醒
    prep_date = trial_date - timedelta(days=3)
    if prep_date > datetime.now():
        if create_reminder(
            title=f"準備開庭：{case_number}（3天後）",
            due_date=prep_date,
            notes=f"法院：{court}\n當事人：{client}\n請確認書狀、證據已備齊",
            list_name=reminder_list,
            priority=5,
        ):
            created.append("開庭前3天提醒")

    # 3. 開庭前 1 天提醒
    day_before = trial_date - timedelta(days=1)
    if day_before > datetime.now():
        if create_reminder(
            title=f"明天開庭：{case_number}",
            due_date=day_before,
            notes=f"法院：{court}\n當事人：{client}\n最後確認所有文件",
            list_name=reminder_list,
            priority=9,
        ):
            created.append("開庭前1天提醒")

    return created


def create_case_deadline_reminder(
    case_number: str,
    deadline_type: str,
    deadline_date: datetime,
    client: str = "",
    notes: str = "",
    reminder_list: str = DEFAULT_REMINDER_LIST,
) -> bool:
    """
    建立案件法定期限提醒。

    常見期限類型：
    - 答辯（收到起訴狀後 20 天）
    - 上訴（判決送達後 20 天）
    - 書狀提出（依法院命令）
    - 法扶結案

    Args:
        case_number: 案號
        deadline_type: 期限類型描述
        deadline_date: 期限日期
        client: 當事人姓名
        notes: 額外備註
        reminder_list: 提醒事項清單名稱

    Returns:
        True if reminder was created
    """
    title = f"期限：{case_number} — {deadline_type}"
    body_parts = []
    if client:
        body_parts.append(f"當事人：{client}")
    body_parts.append(f"期限類型：{deadline_type}")
    body_parts.append(f"截止日期：{deadline_date.strftime('%Y-%m-%d')}")
    if notes:
        body_parts.append(notes)

    return create_reminder(
        title=title,
        due_date=deadline_date,
        notes="\n".join(body_parts),
        list_name=reminder_list,
        priority=9,
    )


# ---------------------------------------------------------------------------
# 指令解析
# ---------------------------------------------------------------------------

# !開庭 113勞訴19 2026-05-01 09:30
_RE_TRIAL_CMD = re.compile(
    r"!開庭\s+"
    r"(.+?)\s+"                           # 案號
    r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})"     # 日期
    r"(?:\s+(\d{1,2}:\d{2}))?"           # 時間（可選）
)


def parse_trial_command(message: str) -> Optional[dict]:
    """
    解析 !開庭 指令。

    格式：!開庭 [案號] [日期] [時間]
    範例：!開庭 113勞訴19 2026-05-01 09:30

    Returns:
        {"case_number": str, "trial_date": datetime} 或 None
    """
    m = _RE_TRIAL_CMD.search(message)
    if not m:
        return None

    case_number = m.group(1).strip()
    date_str = m.group(2)
    time_str = m.group(3) or "09:30"

    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        return {"case_number": case_number, "trial_date": dt}
    except ValueError:
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M")
            return {"case_number": case_number, "trial_date": dt}
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# CLI 入口（測試用）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if "--test-event" in sys.argv:
        print("Testing calendar event creation...")
        ensure_calendar_exists()
        ok = create_calendar_event(
            title="MAGI 測試事件",
            start=datetime.now() + timedelta(hours=1),
            notes="這是自動化測試事件",
        )
        print(f"Event created: {ok}")

    elif "--test-reminder" in sys.argv:
        print("Testing reminder creation...")
        ensure_reminder_list_exists()
        ok = create_reminder(
            title="MAGI 測試提醒",
            due_date=datetime.now() + timedelta(days=1),
            notes="這是自動化測試提醒",
        )
        print(f"Reminder created: {ok}")

    elif "--test-trial" in sys.argv:
        print("Testing trial event creation...")
        results = create_trial_events(
            case_number="113年度勞訴字第19號",
            trial_date=datetime.now() + timedelta(days=7),
            court="臺灣臺北地方法院",
            client="測試當事人",
        )
        print(f"Created: {results}")

    else:
        print("Usage:")
        print("  python eventkit_bridge.py --test-event     # Test calendar event")
        print("  python eventkit_bridge.py --test-reminder  # Test reminder")
        print("  python eventkit_bridge.py --test-trial     # Test trial workflow")
