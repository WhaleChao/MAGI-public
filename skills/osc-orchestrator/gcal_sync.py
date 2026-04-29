"""
OSC → Google Calendar 單向同步
================================
push_todo_to_gcal       : 推送 case_todos 待辦到 GCal（全天事件）
push_calendar_event_to_gcal : 推送 calendar_events 到 GCal（時段事件）
run_sync                : 主入口，回 stats dict

欄位說明：
  todos.google_calendar_id     → GCal event id（已存 = PATCH，否則 INSERT）
  calendar_events.google_event_id → 同上

政策：
  - 單向 MAGI → GCal，不實作反向
  - dry_run=True 時不呼叫任何 GCal write API
  - 掃未來 30 天內、未刪除的 todo / calendar_event
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
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

    # Read calendar_id from settings
    try:
        from api.osc.utils import _osc_exec

        row, _ = _osc_exec(
            "SELECT value FROM settings WHERE `key`=%s",
            ("gcal_calendar_id",),
            fetch="one",
        )
        calendar_id = (row[0] if row else None) or "primary"
    except Exception:
        calendar_id = "primary"

    service = _build_service(creds)

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
