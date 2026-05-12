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
_CASE_IDENTITY_CACHE: list[dict[str, str]] | None = None
_LAF_IDENTITY_CACHE: list[dict[str, str]] | None = None
OSC_CASE_PREFIX_RE = re.compile(
    r"^\s*(?:\[(20\d{2}-\d{4})\]|(20\d{2}-\d{4})(?=$|[\s：:｜|／/\\\-–—_、，,。；;\]\)]))"
)
LAF_NO_RE = re.compile(r"\d{6,8}-[A-Za-z]-\d{3}")
LAF_EVENT_EXCLUSION_KEYWORDS = (
    "聲請改期", "聲請改期中", "不出席", "取消", "改期", "不到庭",
    "法扶開辦末日", "法扶上訴", "法扶再議",
    "宣判", "宣示判決", "停班", "停課", "放假", "颱風", "天然災害",
)
LAF_MEETING_EXCLUSION_KEYWORDS = ("U會議", "Ｕ會議", "u會議", "ｕ會議")
LAF_COURT_KEYWORDS = ("開庭", "準備程序", "言詞辯論", "審理", "審理程序", "調解", "訊問", "協商程序", "調查", "調查程序", "庭期")

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


def _extract_leading_osc_case_number(summary: str, description: str = "") -> str:
    """Return the OSC system case number only when it is the event prefix."""
    for text in (summary or "", description or ""):
        match = OSC_CASE_PREFIX_RE.search(text)
        if match:
            return str(match.group(1) or match.group(2) or "").strip()
    return ""


def _load_case_identity_cache() -> list[dict[str, str]]:
    global _CASE_IDENTITY_CACHE
    if _CASE_IDENTITY_CACHE is not None:
        return _CASE_IDENTITY_CACHE
    try:
        rows, _ = _osc_exec_sql(
            """
            SELECT case_number, client_name, start_date, approval_date
            FROM cases
            WHERE COALESCE(client_name, '') != ''
              AND COALESCE(status, '') NOT IN ('已結案', '結案', 'closed', 'Closed')
            ORDER BY CHAR_LENGTH(client_name) DESC, case_number DESC
            LIMIT 1000
            """,
            fetch="all",
        )
    except Exception as exc:
        logger.debug("case identity cache failed: %s", exc)
        rows = []
    cache: list[dict[str, str]] = []
    for row in rows or []:
        case_number = str((row.get("case_number") if isinstance(row, dict) else row[0]) or "").strip()
        client_name = str((row.get("client_name") if isinstance(row, dict) else row[1]) or "").strip()
        start_date = str((row.get("start_date") if isinstance(row, dict) else row[2]) or "").strip()[:10]
        approval_date = str((row.get("approval_date") if isinstance(row, dict) else row[3]) or "").strip()[:10]
        if case_number and client_name:
            cache.append({
                "case_number": case_number,
                "client_name": client_name,
                "start_date": start_date or approval_date,
            })
    _CASE_IDENTITY_CACHE = cache
    return cache


def _load_laf_identity_cache() -> list[dict[str, str]]:
    global _LAF_IDENTITY_CACHE
    if _LAF_IDENTITY_CACHE is not None:
        return _LAF_IDENTITY_CACHE
    try:
        rows, _ = _osc_exec_sql(
            """
            SELECT case_number, client_name, laf_case_no, application_no,
                   start_date, approval_date, case_reason, case_type,
                   case_category, legal_aid_status
            FROM cases
            WHERE COALESCE(client_name, '') != ''
              AND COALESCE(status, '') NOT IN ('已結案', '結案', 'closed', 'Closed')
              AND (
                    COALESCE(laf_case_no, '') != ''
                 OR COALESCE(application_no, '') REGEXP '^[0-9]{6,8}-[A-Za-z]-[0-9]{3}$'
                 OR case_category='法律扶助案件'
                 OR case_reason LIKE '%法扶%'
                 OR case_reason LIKE '%法律扶助%'
                 OR COALESCE(legal_aid_status, '') != ''
              )
            ORDER BY CHAR_LENGTH(client_name) DESC, case_number DESC
            LIMIT 1000
            """,
            fetch="all",
        )
    except Exception as exc:
        logger.debug("LAF identity cache failed: %s", exc)
        rows = []
    cache: list[dict[str, str]] = []
    for row in rows or []:
        if isinstance(row, dict):
            case_number = str(row.get("case_number") or "").strip()
            client_name = str(row.get("client_name") or "").strip()
            laf_case_no = str(row.get("laf_case_no") or row.get("application_no") or "").strip()
            start_date = str(row.get("start_date") or row.get("approval_date") or "").strip()[:10]
            case_reason = str(row.get("case_reason") or row.get("case_type") or "").strip()
            case_category = str(row.get("case_category") or "").strip()
            legal_aid_status = str(row.get("legal_aid_status") or "").strip()
        else:
            case_number = str(row[0] or "").strip()
            client_name = str(row[1] or "").strip()
            laf_case_no = str(row[2] or row[3] or "").strip()
            start_date = str(row[4] or row[5] or "").strip()[:10]
            case_reason = str(row[6] or row[7] or "").strip()
            case_category = str(row[8] or "").strip() if len(row) > 8 else ""
            legal_aid_status = str(row[9] or "").strip() if len(row) > 9 else ""
        if case_number and client_name:
            cache.append({
                "case_number": case_number,
                "client_name": client_name,
                "laf_case_no": laf_case_no,
                "start_date": start_date,
                "case_reason": case_reason,
                "case_category": case_category,
                "legal_aid_status": legal_aid_status,
            })
    _LAF_IDENTITY_CACHE = cache
    return cache


def _infer_case_identity(summary: str, description: str, event_date: str = "") -> tuple[str, str]:
    text = f"{summary or ''}\n{description or ''}"
    case_number = _extract_case_number(summary, description)
    if case_number:
        try:
            row, _ = _osc_exec_sql(
                "SELECT case_number, client_name FROM cases WHERE case_number=%s LIMIT 1",
                (case_number,),
                fetch="one",
            )
            if row:
                if isinstance(row, dict):
                    return str(row.get("case_number") or case_number), str(row.get("client_name") or "")
                return str(row[0] or case_number), str(row[1] or "")
        except Exception:
            logger.debug("case lookup by number failed", exc_info=True)
        return case_number, ""
    for case in _load_case_identity_cache():
        name = case["client_name"]
        start_date = str(case.get("start_date") or "").strip()[:10]
        if event_date and start_date and event_date[:10] < start_date:
            continue
        if name and name in text:
            return case["case_number"], name
    return "", ""


def _classify_laf_reportable_activity(summary: str) -> str:
    text = str(summary or "")
    if not text or any(k in text for k in LAF_EVENT_EXCLUSION_KEYWORDS):
        return ""
    if any(k in text for k in LAF_MEETING_EXCLUSION_KEYWORDS):
        return ""
    if any(k in text for k in LAF_COURT_KEYWORDS):
        return "開庭"
    if any(k in text for k in ("會議", "會面", "來所", "來所提供資料", "視訊會議", "碰面", "面談", "線上面談", "來所面談", "開會", "交資料", "來所交資料", "臨時來所")):
        return "會議"
    if "律見" in text:
        return "律見"
    if any(k in text for k in ("閱卷", "影卷", "調卷")):
        return "閱卷"
    if any(k in text for k in ("電話", "電話聯繫", "通話", "電聯", "聯繫", "聯絡")):
        return "電話聯繫"
    return ""


def _is_laf_identity_case(case: dict[str, str]) -> bool:
    laf_case_no = str(case.get("laf_case_no") or "").strip()
    category = str(case.get("case_category") or "").strip()
    status = str(case.get("legal_aid_status") or "").strip()
    reason = str(case.get("case_reason") or "").strip()
    return bool(
        LAF_NO_RE.fullmatch(laf_case_no)
        or category == "法律扶助案件"
        or "法扶" in reason
        or "法律扶助" in reason
        or status
    )


def _infer_laf_reportable_event_identity(summary: str, description: str, event_date: str = "") -> tuple[str, str]:
    text = f"{summary or ''}\n{description or ''}"
    if not _classify_laf_reportable_activity(text):
        return "", ""
    explicit_case = _extract_case_number(summary, description)
    explicit_laf = LAF_NO_RE.search(text)
    matches = []
    for case in _load_laf_identity_cache():
        if not _is_laf_identity_case(case):
            continue
        if event_date and case.get("start_date") and event_date[:10] < str(case.get("start_date")):
            continue
        if explicit_case and explicit_case == case.get("case_number"):
            return case["case_number"], case["client_name"]
        if explicit_laf and explicit_laf.group(0) == case.get("laf_case_no"):
            return case["case_number"], case["client_name"]
        client_name = case.get("client_name") or ""
        if client_name and client_name in text:
            matches.append(case)
    if len(matches) == 1:
        return matches[0]["case_number"], matches[0]["client_name"]
    if len(matches) > 1:
        for case in matches:
            reason_hint = str(case.get("case_reason") or "").strip()[:2]
            if reason_hint and reason_hint in text:
                return case["case_number"], case["client_name"]
    return "", ""


def _infer_osc_owned_event_identity(summary: str, description: str) -> tuple[str, str]:
    case_number = _extract_leading_osc_case_number(summary, description)
    if not case_number:
        return "", ""
    try:
        row, _ = _osc_exec_sql(
            "SELECT case_number, client_name FROM cases WHERE case_number=%s LIMIT 1",
            (case_number,),
            fetch="one",
        )
        if row:
            if isinstance(row, dict):
                return str(row.get("case_number") or case_number), str(row.get("client_name") or "")
            return str(row[0] or case_number), str(row[1] or "")
    except Exception:
        logger.debug("OSC-owned event case lookup failed", exc_info=True)
    return case_number, ""


def _infer_importable_event_identity(summary: str, description: str, event_date: str = "") -> tuple[str, str]:
    case_number, client_name = _infer_osc_owned_event_identity(summary, description)
    if case_number:
        return case_number, client_name
    return _infer_laf_reportable_event_identity(summary, description, event_date)


def import_gcal_events_to_todos(service, *, dry_run: bool = False, lookback_days: int = 730, lookahead_days: int = 180) -> dict:
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
                    case_number, client_name = _infer_importable_event_identity(summary, description, start_date)
                    if not case_number:
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
                            case_number,
                            client_name,
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
        rows, cols = _osc_exec_sql(
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
        if isinstance(row, dict):
            todo = dict(row)
        else:
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
                _osc_exec_sql(
                    "UPDATE case_todos SET google_calendar_id=%s WHERE id=%s",
                    (event_id, todo["id"]),
                    fetch="none",
                )
            stats["pushed"] += 1

        except Exception as exc:
            logger.warning("push_todo id=%s failed: %s", todo.get("id"), exc)
            stats["errors"].append(f"todo id={todo.get('id')}: {exc}")

    return stats
