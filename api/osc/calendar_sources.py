"""Shared OSC todo/calendar source classification helpers."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

GCAL_IMPORT_PREFIX = "gcal_import"
CALENDAR_TODO_TYPE = "行事曆事件"


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_google_calendar_import(row: dict[str, Any] | None) -> bool:
    return _text((row or {}).get("source_file")).startswith(GCAL_IMPORT_PREFIX)


def is_calendar_todo(row: dict[str, Any] | None) -> bool:
    data = row or {}
    return is_google_calendar_import(data) or _text(data.get("todo_type")) == CALENDAR_TODO_TYPE


def todo_source_key(row: dict[str, Any] | None) -> str:
    if is_google_calendar_import(row):
        return "gcal_import"
    if is_calendar_todo(row):
        return "calendar_todo"
    return "case_todos"


def todo_source_label(row: dict[str, Any] | None) -> str:
    key = todo_source_key(row)
    if key == "gcal_import":
        return "Google 日曆匯入"
    if key == "calendar_todo":
        return "行事曆事件待辦"
    return "OSC 建立"


def calendar_todo_source_sql(source_file_col: str = "source_file", todo_type_col: str = "todo_type") -> str:
    return f"(COALESCE({source_file_col}, '') LIKE 'gcal_import%%' OR COALESCE({todo_type_col}, '')='行事曆事件')"


def osc_todo_source_sql(source_file_col: str = "source_file", todo_type_col: str = "todo_type") -> str:
    return (
        f"(COALESCE({source_file_col}, '') NOT LIKE 'gcal_import%%' "
        f"AND COALESCE({todo_type_col}, '') <> '行事曆事件')"
    )


def todo_source_api_fields(row: dict[str, Any] | None) -> dict[str, Any]:
    key = todo_source_key(row)
    return {
        "source_kind": key,
        "source_label": todo_source_label(row),
        "is_calendar_source": key in {"gcal_import", "calendar_todo"},
    }


def calendar_todo_to_event(row: dict[str, Any]) -> dict[str, Any]:
    date_part = _text(row.get("todo_date"))
    time_part = _text(row.get("todo_time"))
    if date_part and time_part:
        start = f"{date_part} {time_part[:8]}"
    else:
        start = date_part
    end = start
    try:
        if date_part and time_part:
            from datetime import datetime

            end_dt = datetime.fromisoformat(start.replace(" ", "T")) + timedelta(hours=1)
            end = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        end = start
    fields = todo_source_api_fields(row)
    return {
        "id": row.get("id"),
        "event_id": f"todo:{row.get('id')}",
        "title": row.get("todo_type") or CALENDAR_TODO_TYPE,
        "summary": row.get("description") or "",
        "description": row.get("description") or "",
        "start_date": start,
        "end_date": end,
        "color": "#f59e0b" if fields["source_kind"] == "gcal_import" else "#0ea5e9",
        "location": "",
        "is_all_day": 0 if time_part else 1,
        "reminder_minutes": 0,
        "case_number": row.get("case_number") or "",
        "created_date": row.get("created_date") or "",
        "updated_date": row.get("completed_date") or row.get("created_date") or "",
        "source_table": "case_todos",
        "todo_id": row.get("id"),
        "todo_status": row.get("status") or "",
        **fields,
    }
