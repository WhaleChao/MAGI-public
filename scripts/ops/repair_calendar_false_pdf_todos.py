#!/usr/bin/env python3
"""Mark and remove PDF-scanner calendar todos created from the wrong source year."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SINCE = "2026-01-01"
FALSE_MARKER = "誤同步：PDF來源日期與待辦基準日不一致，已自日曆移除"


def _portable_basename(value: Any) -> str:
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


def _collect_source_year_candidates(value: Any) -> list[int]:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return []
    candidates: list[int] = []

    def _add_candidate(raw_year: int | None) -> None:
        if raw_year is None:
            return
        current_year = datetime.now().year
        if raw_year < 2010 or raw_year > current_year + 3:
            return
        if raw_year not in candidates:
            candidates.append(raw_year)

    # Case folder prefix, e.g., 2025-0049-林洋宇
    for m in re.finditer(r"(?:^|/)(20\d{2})-(?:\d{3,8})(?:[\\/._-]|$)", text):
        _add_candidate(int(m.group(1)))

    # ROC annual marker, e.g., 114年度
    for m in re.finditer(r"(?:^|[^\d])(\d{3})年度", text):
        _add_candidate(_parse_roc_year_to_ad(m.group(1)))

    # Explicit ROC/AD year mentions (e.g., 115年/2025年)
    for m in re.finditer(r"(?:民國)?(\d{2,4})年", text):
        _add_candidate(_parse_roc_year_to_ad(m.group(1)))

    return candidates


def _source_context_for_year_inference(row: dict[str, Any], *, allow_fallback: bool = False) -> list[Any]:
    base = [
        row.get("source_file"),
        row.get("description"),
    ]
    if allow_fallback:
        base.extend([row.get("case_number"), row.get("client_name")])
    return base


def _infer_source_base_year_from_row(row: dict[str, Any]) -> tuple[int | None, bool]:
    for source in _source_context_for_year_inference(row, allow_fallback=False):
        source_date = _source_document_date(source)
        if source_date:
            return source_date.year, True
    for source in _source_context_for_year_inference(row, allow_fallback=False):
        for year in _collect_source_year_candidates(source):
            if year:
                return year, True

    for source in _source_context_for_year_inference(row, allow_fallback=True):
        if source in (row.get("source_file"), row.get("description")):
            continue
        for year in _collect_source_year_candidates(source):
            if year:
                return year, False
    return None, False


def _source_context_text(row: dict[str, Any]) -> str:
    return "\n".join(
        str(value or "")
        for value in _source_context_for_year_inference(row, allow_fallback=True)
        if str(value or "").strip()
    )


def _source_context_has_explicit_same_todo_date(row: dict[str, Any], todo_date: date) -> bool:
    text = _source_context_text(row)
    for match in re.finditer(r"(?:民國)?(\d{2,4})年(\d{1,2})月(\d{1,2})日", text):
        try:
            year = int(match.group(1))
            if year < 1911:
                year += 1911
            if year == todo_date.year and int(match.group(2)) == todo_date.month and int(match.group(3)) == todo_date.day:
                return True
        except Exception:
            continue
    return False


def _source_document_date(value: Any) -> date | None:
    name = _portable_basename(value)
    for pattern in (
        r"^(20\d{2})(\d{2})(\d{2})",
        r"^(20\d{2})[-.](\d{1,2})[-.](\d{1,2})",
        r"^(\d{3})[-/]?(\d{2})[-/]?(\d{2})(?:\s|$)",
    ):
        match = re.search(pattern, name)
        if not match:
            continue
        try:
            year = int(match.group(1))
            if year < 1911:
                year += 1911
            return date(year, int(match.group(2)), int(match.group(3)))
        except Exception:
            continue
    return None


def _description_base_month_day(value: Any) -> str:
    match = re.search(r"\((\d{2}/\d{2})文到\)|基準日\s*(\d{2}/\d{2})", str(value or ""))
    if not match:
        return ""
    return str(match.group(1) or match.group(2) or "")


def _parse_todo_date(value: Any) -> date | None:
    try:
        return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _explicit_older_source_date_reason(row: dict[str, Any], todo_date: date) -> str:
    text = _source_context_text(row)
    for match in re.finditer(r"(?:民國)?(\d{2,4})年(\d{1,2})月(\d{1,2})日", text):
        try:
            year = int(match.group(1))
            if year < 1911:
                year += 1911
            if year < todo_date.year and int(match.group(2)) == todo_date.month and int(match.group(3)) == todo_date.day:
                return f"explicit_source_year_{year}_shifted_to_{todo_date.year}"
        except Exception:
            continue
    return ""


_HEARING_TODO_TYPES = {"開庭", "準備程序", "言詞辯論", "調解", "審理", "審理程序", "審判程序", "宣判", "訊問", "調查"}


def _source_mentions_todo_month_day(source: Any, todo_date: date) -> bool:
    compact = re.sub(r"\s+", "", str(source or ""))
    tokens = {
        f"{todo_date.month}月{todo_date.day}日",
        f"{todo_date.month:02d}月{todo_date.day:02d}日",
        f"{todo_date.month}月{todo_date.day}號",
        f"{todo_date.month:02d}月{todo_date.day:02d}號",
        f"{todo_date.month}/{todo_date.day}",
        f"{todo_date.month}/{todo_date.day:02d}",
        f"{todo_date.month:02d}/{todo_date.day}",
        f"{todo_date.month:02d}/{todo_date.day:02d}",
    }
    return any(token in compact for token in tokens)


def _future_year_shift_reason(row: dict[str, Any], todo_date: date) -> str:
    source_date = _source_document_date(row.get("source_file"))
    source_year, has_source_year = _infer_source_base_year_from_row(row)
    if source_year is None or todo_date.year <= source_year:
        return ""
    todo_type = str(row.get("todo_type") or "").strip()
    if todo_type not in _HEARING_TODO_TYPES:
        return ""
    if not _source_mentions_todo_month_day(_source_context_text(row), todo_date):
        return ""
    if _source_context_has_explicit_same_todo_date(row, todo_date):
        return ""
    if not has_source_year and not _explicit_older_source_date_reason(row, todo_date):
        return ""
    return f"future_year_shift_from_old_source:{source_date.isoformat() if source_date else source_year}_to_{todo_date.isoformat()}"


def classify_false_pdf_todo(row: dict[str, Any]) -> str:
    source = row.get("source_file") or ""
    if not source or str(source).startswith(("gcal_import:", "manual", "laf_progress_report_cooldown:")):
        return ""
    todo_date = _parse_todo_date(row.get("todo_date"))
    if not todo_date:
        return ""
    reasons: list[str] = []
    source_date = _source_document_date(source)
    base_md = _description_base_month_day(row.get("description"))
    if source_date and base_md and base_md != source_date.strftime("%m/%d"):
        reasons.append(f"doc_date_mismatch:{source_date.isoformat()}_vs_{base_md}")
    if not source_date and base_md == datetime.now().strftime("%m/%d"):
        reasons.append(f"scan_day_fallback_without_source_date:{base_md}")
    explicit_reason = _explicit_older_source_date_reason(row, todo_date)
    if explicit_reason:
        reasons.append(explicit_reason)
    future_shift_reason = _future_year_shift_reason(row, todo_date)
    if future_shift_reason:
        reasons.append(future_shift_reason)
    return ";".join(reasons)


def _load_action_module():
    path = ROOT / "skills" / "osc-orchestrator" / "action.py"
    spec = importlib.util.spec_from_file_location("_magi_osc_action_for_calendar_repair", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _active_candidate_rows(since: str, limit: int) -> list[dict[str, Any]]:
    sys.path.insert(0, str(ROOT))
    from api.osc.utils import _osc_exec  # type: ignore

    rows, _ = _osc_exec(
        """
        SELECT id, case_number, client_name, todo_type, todo_date, todo_time,
               description, status, source_file, google_calendar_id,
               google_calendar_event_id, created_date
        FROM case_todos
        WHERE todo_date >= %s
          AND (status IS NULL OR status='' OR LOWER(status) NOT IN (
                'completed','已完成','done','closed','cancelled','canceled','取消','deleted'
              ))
          AND COALESCE(source_file,'') NOT LIKE 'gcal_import%%'
          AND COALESCE(source_file,'') NOT LIKE 'manual%%'
        ORDER BY todo_date ASC, id ASC
        LIMIT %s
        """,
        (since, max(1, min(limit, 10000))),
        fetch="all",
    )
    return [dict(row) for row in (rows or []) if isinstance(row, dict)]


def find_false_pdf_todos(*, since: str = DEFAULT_SINCE, limit: int = 3000) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in _active_candidate_rows(since, limit):
        reason = classify_false_pdf_todo(row)
        if not reason:
            continue
        source = str(row.get("source_file") or "")
        row["false_reason"] = reason
        row["source_document_date"] = str(_source_document_date(source) or "")
        row["source_basename"] = _portable_basename(source)
        out.append(row)
    return out


def _calendar_id() -> str:
    sys.path.insert(0, str(ROOT))
    from api.osc.utils import _osc_exec  # type: ignore

    try:
        row, _ = _osc_exec("SELECT value FROM settings WHERE `key`=%s LIMIT 1", ("gcal_calendar_id",), fetch="one")
        if isinstance(row, dict) and str(row.get("value") or "").strip():
            return str(row.get("value")).strip()
    except Exception:
        pass
    return "primary"


def _google_service():
    action = _load_action_module()
    credentials_path = action._default_gcal_credentials_path()
    token_path = action._default_gcal_token_path()
    service_info = action._build_google_calendar_service(credentials_path, token_path, interactive=False)
    if not service_info.get("ok"):
        raise RuntimeError(str(service_info.get("error") or "gcal_service_failed"))
    return service_info.get("service"), action


def _delete_google_event(service: Any, action: Any, *, calendar_id: str, event_id: str) -> tuple[bool, str]:
    if not event_id:
        return True, "no_google_calendar_id"
    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return True, "deleted"
    except Exception as exc:
        if action._is_google_gone_error(exc):
            return True, "already_gone"
        return False, f"{type(exc).__name__}: {str(exc)[:180]}"


def _delete_calendar_event_cache(event_id: str) -> tuple[bool, str]:
    event_id = str(event_id or "").strip()
    if not event_id:
        return True, "no_local_cache_event_id"
    sys.path.insert(0, str(ROOT))
    from api.osc.utils import _osc_exec  # type: ignore

    try:
        _osc_exec("DELETE FROM calendar_events WHERE event_id=%s", (event_id,), fetch="none")
        return True, "deleted"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:180]}"


def _mark_deleted(row: dict[str, Any], reason: str, *, calendar_delete_status: str) -> None:
    sys.path.insert(0, str(ROOT))
    from api.osc.utils import _osc_exec  # type: ignore

    gid = str(row.get("google_calendar_id") or "").strip()
    desc = str(row.get("description") or "").strip()
    note = f"[{FALSE_MARKER}；原因={reason}；calendar={calendar_delete_status}；原event={gid or '無'}]"
    if FALSE_MARKER not in desc:
        desc = f"{note}\n{desc}".strip()
    _osc_exec(
        """
        UPDATE case_todos
        SET status='deleted',
            completed_date=NOW(),
            google_calendar_event_id=IF(COALESCE(google_calendar_event_id,'')='', %s, google_calendar_event_id),
            google_calendar_id='',
            description=%s
        WHERE id=%s
        """,
        (gid, desc, int(row["id"])),
        fetch="none",
    )


def run_repair(*, since: str, limit: int, dry_run: bool) -> dict[str, Any]:
    rows = find_false_pdf_todos(since=since, limit=limit)
    out: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "matched": len(rows),
        "calendar_deleted": 0,
        "calendar_already_gone": 0,
        "calendar_cache_deleted": 0,
        "db_marked_deleted": 0,
        "calendar_cache_failed": 0,
        "failed": 0,
        "items": [],
    }
    if dry_run or not rows:
        out["items"] = [
            {
                "id": row.get("id"),
                "case_number": row.get("case_number"),
                "client_name": row.get("client_name"),
                "todo_type": row.get("todo_type"),
                "todo_date": str(row.get("todo_date") or ""),
                "todo_time": str(row.get("todo_time") or ""),
                "google_calendar_id": row.get("google_calendar_id"),
                "google_calendar_event_id": row.get("google_calendar_event_id"),
                "false_reason": row.get("false_reason"),
                "source_basename": row.get("source_basename"),
            }
            for row in rows[:80]
        ]
        return out

    calendar_id = _calendar_id()
    service, action = _google_service()
    for row in rows:
        gid = str(row.get("google_calendar_id") or "").strip()
        cache_ids = [
            str(v or "").strip()
            for v in (
                row.get("google_calendar_id"),
                row.get("google_calendar_event_id"),
            )
            if str(v or "").strip()
        ]
        cache_ids = list(dict.fromkeys(cache_ids))
        ok, status = _delete_google_event(service, action, calendar_id=calendar_id, event_id=gid)
        if not ok:
            out["failed"] += 1
            if len(out["items"]) < 80:
                out["items"].append({"id": row.get("id"), "google_calendar_id": gid, "error": status})
            continue
        if status == "deleted":
            out["calendar_deleted"] += 1
        elif status == "already_gone":
            out["calendar_already_gone"] += 1
        for cache_id in cache_ids:
            cache_ok, cache_status = _delete_calendar_event_cache(cache_id)
            if cache_ok:
                out["calendar_cache_deleted"] += 1
            else:
                out["calendar_cache_failed"] += 1
                if len(out["items"]) < 80:
                    out["items"].append(
                        {
                            "id": row.get("id"),
                            "google_calendar_id": gid,
                            "calendar_cache_id": cache_id,
                            "calendar_cache_status": cache_status,
                        }
                    )
        _mark_deleted(row, str(row.get("false_reason") or ""), calendar_delete_status=status)
        out["db_marked_deleted"] += 1
        if len(out["items"]) < 80:
            out["items"].append(
                {
                    "id": row.get("id"),
                    "case_number": row.get("case_number"),
                    "client_name": row.get("client_name"),
                    "todo_type": row.get("todo_type"),
                    "todo_date": str(row.get("todo_date") or ""),
                    "todo_time": str(row.get("todo_time") or ""),
                    "google_calendar_id": gid,
                    "calendar_cache_deleted": len(cache_ids),
                    "calendar_status": status,
                    "false_reason": row.get("false_reason"),
                }
            )
    if out["failed"]:
        out["ok"] = False
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--limit", type=int, default=3000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    out = run_repair(since=args.since, limit=args.limit, dry_run=not args.apply)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
