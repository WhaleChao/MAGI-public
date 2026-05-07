"""
OSC ↔ Google Calendar 同步
================================
push_todo_to_gcal       : 推送 case_todos 待辦到 GCal（全天事件）
push_calendar_event_to_gcal : 推送 calendar_events 到 GCal（時段事件）
run_sync                : 主入口，回 stats dict

欄位說明：
  todos.google_calendar_id     → GCal event id（已存 = PATCH，否則 INSERT）
  calendar_events.google_event_id → 同上

政策：
  - MAGI → GCal 推送至 settings.gcal_calendar_id
  - GCal → MAGI 匯入會掃 settings.gcal_import_calendar_ids；未設定時掃使用者可見的所有日曆
  - dry_run=True 時不呼叫任何 GCal write API，也不寫入 DB
  - 掃未來 30 天內、未刪除的 todo / calendar_event
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_PATH = Path.home() / ".magi" / "google" / "token.json"

# ── credentials helpers ───────────────────────────────────────────────────────


def _load_creds():
    if not TOKEN_PATH.exists():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            TOKEN_PATH.chmod(0o600)
        return creds
    except Exception as exc:
        logger.warning("gcal_sync._load_creds failed: %s", exc)
        return None


def _build_service(creds):
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _split_calendar_ids(raw: str | None) -> list[str]:
    values = [x.strip() for x in re.split(r"[,;\n]+", raw or "") if x.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_conn():
    """Get a DB connection via existing osc helper."""
    try:
        sys.path.insert(0, "/Users/ai/Desktop/MAGI_v2")
        from api.osc.utils import _osc_exec  # noqa: F401 – ping to verify import
        from api.db_helper import get_cursor

        conn = get_cursor.__self__ if hasattr(get_cursor, "__self__") else None
        if conn is None:
            # Fall back to direct mysql connection
            import importlib
            mod = importlib.import_module("api.db_helper")
            return getattr(mod, "_get_connection", lambda: None)()
        return conn
    except Exception as exc:
        logger.debug("_get_conn: %s", exc)
        return None


def _osc_exec_sql(sql: str, params: tuple = (), fetch: str = "all"):
    """Wrapper calling _osc_exec from api.osc.utils."""
    from api.osc.utils import _osc_exec

    return _osc_exec(sql, params, fetch=fetch)


def _get_setting_value(key: str, default: str = "") -> str:
    try:
        row, _ = _osc_exec_sql("SELECT value FROM settings WHERE `key`=%s", (key,), fetch="one")
        if row:
            if isinstance(row, dict):
                return str(row.get("value") or default)
            return str(row[0] or default)
    except Exception as exc:
        logger.debug("setting lookup failed for %s: %s", key, exc)
    return default


# ── event builders ────────────────────────────────────────────────────────────


def _make_todo_event(todo: dict) -> dict:
    case_number = todo.get("case_number") or ""
    desc = (todo.get("description") or "")[:40]
    full_desc = todo.get("description") or ""
    due_str = str(todo.get("todo_date") or todo.get("due_date") or date.today().isoformat())
    # Normalise date object → iso string
    if hasattr(due_str, "isoformat"):
        due_str = due_str.isoformat()

    return {
        "summary": f"[{case_number}] {desc}",
        "start": {"date": due_str},
        "end": {"date": due_str},
        "description": f"案號：{case_number}\n{full_desc}",
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 1440}],
        },
    }


def _make_cal_event(ev: dict) -> dict:
    case_number = ev.get("case_number") or ""
    title = (ev.get("title") or ev.get("description") or "")[:60]
    start_str = str(ev.get("start_date") or ev.get("event_date") or date.today().isoformat())
    end_str = str(ev.get("end_date") or start_str)
    if hasattr(start_str, "isoformat"):
        start_str = start_str.isoformat()
    if hasattr(end_str, "isoformat"):
        end_str = end_str.isoformat()

    return {
        "summary": f"[{case_number}] {title}" if case_number else title,
        "start": {"date": start_str},
        "end": {"date": end_str},
        "description": ev.get("description") or "",
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 1440}],
        },
    }


def _list_import_calendar_ids(service, configured: str = "") -> list[str]:
    explicit = _split_calendar_ids(configured)
    if explicit:
        return explicit
    try:
        items = (
            service.calendarList()
            .list(minAccessRole="reader", showHidden=True)
            .execute()
            .get("items", [])
        )
        return [str(item.get("id") or "").strip() for item in items if item.get("id")]
    except Exception as exc:
        logger.warning("calendarList lookup failed, falling back to primary: %s", exc)
        return ["primary"]


def _event_start_date_time(event: dict) -> tuple[str, str]:
    start = event.get("start") or {}
    if not isinstance(start, dict):
        return "", ""
    date_only = str(start.get("date") or "").strip()
    if date_only:
        return date_only, ""
    date_time = str(start.get("dateTime") or "").strip()
    if not date_time:
        return "", ""
    day = date_time[:10]
    match = re.search(r"T(\d{2}:\d{2})", date_time)
    return day, (match.group(1) if match else "")


def _classify_todo_type(summary: str) -> str:
    for kw, todo_type in [
        ("開庭", "開庭"),
        ("期日", "期日"),
        ("調解", "調解"),
        ("期限", "期限"),
        ("補正", "補正"),
        ("繳費", "繳費"),
        ("閱卷", "閱卷"),
        ("筆錄", "筆錄"),
        ("提出", "提出"),
        ("答辯", "答辯"),
        ("法扶", "法扶"),
    ]:
        if kw in summary:
            return todo_type
    return "行事曆事件"


def _extract_case_number(summary: str, description: str) -> str:
    case_number_re = re.compile(
        r"(\d{4}-\d{4}|\d{2,3}年度?[^\s，,。；;：:]{0,8}字第?\d+號?|\d{2,3}[^\s，,。；;：:]{1,8}\d+號?)"
    )
    for text in (summary or "", description or ""):
        match = case_number_re.search(text)
        if match:
            return match.group(1).strip()
    return ""


def import_gcal_events_to_todos(service, *, dry_run: bool = False, lookback_days: int = 30, lookahead_days: int = 180) -> dict:
    stats: dict[str, Any] = {
        "imported": 0,
        "import_skipped": 0,
        "import_errors": [],
        "import_calendars": [],
    }
    configured_ids = _get_setting_value("gcal_import_calendar_ids", "")
    calendar_ids = _list_import_calendar_ids(service, configured_ids)
    stats["import_calendars"] = calendar_ids[:20]

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=max(0, lookback_days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=max(1, lookahead_days))).strftime("%Y-%m-%dT%H:%M:%SZ")

    existing_ids: set[str] = set()
    try:
        rows, _ = _osc_exec_sql(
            """
            SELECT google_calendar_id
            FROM case_todos
            WHERE google_calendar_id IS NOT NULL AND google_calendar_id != ''
            """,
            fetch="all",
        )
        for row in rows or []:
            gid = row.get("google_calendar_id") if isinstance(row, dict) else row[0]
            if gid:
                existing_ids.add(str(gid))
    except Exception as exc:
        stats["import_errors"].append(f"existing query failed: {exc}")
        return stats

    for calendar_id in calendar_ids:
        try:
            page_token = None
            while True:
                list_kwargs = {
                    "calendarId": calendar_id,
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": True,
                    "orderBy": "startTime",
                    "showDeleted": False,
                    "maxResults": 250,
                }
                if page_token:
                    list_kwargs["pageToken"] = page_token
                response = service.events().list(**list_kwargs).execute()
                for event in response.get("items", []) or []:
                    event_id = str(event.get("id") or "").strip()
                    if not event_id or event_id in existing_ids:
                        stats["import_skipped"] += 1
                        continue
                    summary = str(event.get("summary") or "").strip()
                    if not summary:
                        stats["import_skipped"] += 1
                        continue
                    description = str(event.get("description") or "").strip()
                    start_date, start_time = _event_start_date_time(event)
                    if not start_date:
                        stats["import_skipped"] += 1
                        continue
                    if dry_run:
                        stats["imported"] += 1
                        continue
                    _osc_exec_sql(
                        """
                        INSERT INTO case_todos
                          (case_number, client_name, todo_type, todo_date, todo_time,
                           description, source_file, status, google_calendar_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s)
                        """,
                        (
                            _extract_case_number(summary, description),
                            "",
                            _classify_todo_type(summary),
                            start_date,
                            start_time or None,
                            summary[:500],
                            f"gcal_import:{calendar_id}",
                            event_id,
                        ),
                        fetch="none",
                    )
                    existing_ids.add(event_id)
                    stats["imported"] += 1
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:
            stats["import_errors"].append(f"{calendar_id}: {exc}")
    return stats


# ── push helpers ──────────────────────────────────────────────────────────────


def push_todo_to_gcal(service, calendar_id: str, todo: dict) -> dict:
    """
    Push a todo to GCal.
    If todo.google_calendar_id already set → PATCH (update).
    Otherwise → INSERT, return created event dict.
    """
    event_body = _make_todo_event(todo)
    existing_gid = (todo.get("google_calendar_id") or "").strip()

    if existing_gid:
        result = (
            service.events()
            .patch(calendarId=calendar_id, eventId=existing_gid, body=event_body)
            .execute()
        )
    else:
        result = (
            service.events()
            .insert(calendarId=calendar_id, body=event_body)
            .execute()
        )
    return result


def push_calendar_event_to_gcal(service, calendar_id: str, ev: dict) -> dict:
    """Push a calendar_event row to GCal."""
    event_body = _make_cal_event(ev)
    existing_gid = (ev.get("google_event_id") or "").strip()

    if existing_gid:
        result = (
            service.events()
            .patch(calendarId=calendar_id, eventId=existing_gid, body=event_body)
            .execute()
        )
    else:
        result = (
            service.events()
            .insert(calendarId=calendar_id, body=event_body)
            .execute()
        )
    return result


# ── main sync ─────────────────────────────────────────────────────────────────


def run_sync(dry_run: bool = False, conn=None) -> dict:  # noqa: ARG001
    """
    Main entry point.

    Returns:
        {"pushed": int, "skipped": int, "errors": list[str]}
    """
    stats: dict[str, Any] = {"pushed": 0, "skipped": 0, "errors": []}

    creds = _load_creds()
    if creds is None or not creds.valid:
        stats["errors"].append("No valid GCal credentials. Run OAuth first.")
        return stats

    calendar_id = _get_setting_value("gcal_calendar_id", "primary") or "primary"

    service = _build_service(creds)

    try:
        stats.update(import_gcal_events_to_todos(service, dry_run=dry_run))
    except Exception as exc:
        stats.setdefault("import_errors", []).append(str(exc))

    horizon = (date.today() + timedelta(days=30)).isoformat()
    today_str = date.today().isoformat()

    # ── Sync case_todos ───────────────────────────────────────────────────────
    try:
        from api.osc.utils import _osc_exec

        rows, cols = _osc_exec(
            """
            SELECT id, case_number, client_name, description, todo_date,
                   google_calendar_id
            FROM case_todos
            WHERE todo_date >= %s
              AND todo_date <= %s
              AND (status IS NULL OR status != 'deleted')
            ORDER BY todo_date
            LIMIT 200
            """,
            (today_str, horizon),
            fetch="all",
        )
    except Exception as exc:
        stats["errors"].append(f"todos query failed: {exc}")
        rows, cols = [], []

    col_names = [c[0] if hasattr(c, "__getitem__") else str(c) for c in (cols or [])]

    for row in rows or []:
        todo = dict(zip(col_names, row)) if col_names else {}
        if not todo.get("id"):
            # fallback for tuple rows without col names
            todo = {
                "id": row[0],
                "case_number": row[1],
                "client_name": row[2],
                "description": row[3],
                "todo_date": row[4],
                "google_calendar_id": row[5] if len(row) > 5 else None,
            }

        if not todo.get("todo_date"):
            stats["skipped"] += 1
            continue

        if dry_run:
            stats["pushed"] += 1
            continue

        try:
            from googleapiclient.errors import HttpError

            result = push_todo_to_gcal(service, calendar_id, todo)
            event_id = result.get("id", "")

            # Write back event_id if newly created
            if event_id and not (todo.get("google_calendar_id") or "").strip():
                _osc_exec(
                    "UPDATE case_todos SET google_calendar_id=%s WHERE id=%s",
                    (event_id, todo["id"]),
                    fetch="none",
                )
            stats["pushed"] += 1

        except Exception as exc:
            logger.warning("push_todo id=%s failed: %s", todo.get("id"), exc)
            stats["errors"].append(f"todo id={todo.get('id')}: {exc}")

    return stats
