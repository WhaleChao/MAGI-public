#!/usr/bin/env python3
"""Refresh OSC-created todos and calendar-imported events on a bounded cadence.

This is intentionally conservative for NAS safety:
- scans only a bounded number of case folders per run;
- imports Google Calendar incrementally when credentials are available;
- treats missing OAuth as a non-fatal partial result so fresh installs do not
  create noisy cron failures before the user connects Google.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
LATEST_PATH = ROOT / ".runtime" / "osc_events_refresh_latest.json"
PDF_SCAN_CACHE_PATH = ROOT / ".runtime" / "pdf_calendar_scan_cache.json"
PDF_SCAN_CURSOR_PATH = ROOT / ".runtime" / "pdf_calendar_scan_cursor.json"
PDF_SCAN_RULE_VERSION = os.environ.get(
    "OSC_PDF_CALENDAR_RULE_VERSION",
    "2026-06-20-full-filename-osc-holidays",
)
HISTORY_CUTOFF_ENV = "OSC_EVENTS_REFRESH_HISTORY_CUTOFF_DATE"
DEFAULT_HISTORY_CUTOFF_DATE = "2026-01-01"
_TODO_DONE_STATUSES = (
    "已完成",
    "完成",
    "done",
    "completed",
    "deleted",
    "cancelled",
    "canceled",
    "closed",
)


class _PdfScanTimeout(TimeoutError):
    pass


def _portable_source_basename(value: Any) -> str:
    return str(re.split(r"[\\/]+", str(value or "").strip())[-1] if value else "")


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
    for m in re.finditer(r"(\d{3})年度", text):
        _add_candidate(_parse_roc_year_to_ad(m.group(1)))

    # Explicit ROC/AD year mentions in source metadata (e.g. 115年/2025年)
    for m in re.finditer(r"(?:民國)?(\d{2,4})年", text):
        _add_candidate(_parse_roc_year_to_ad(m.group(1)))

    return candidates


def _source_context_for_year_inference(todo: dict[str, Any]) -> list[Any]:
    return [
        todo.get("source_file"),
        todo.get("file"),
        todo.get("description"),
        todo.get("case_number"),
        todo.get("client_name"),
    ]


def _iter_source_context_values(todo: dict[str, Any]) -> list[str]:
    return [str(value or "") for value in _source_context_for_year_inference(todo) if str(value or "").strip()]


def _infer_source_base_year_from_todo(todo: dict[str, Any]) -> int | None:
    for source in _source_context_for_year_inference(todo):
        source_date = _source_document_date(source)
        if source_date:
            return source_date.year
    for source in _source_context_for_year_inference(todo):
        for year in _collect_source_year_candidates(source):
            if year:
                return year
    return None


def _source_document_date(value: Any) -> date | None:
    name = _portable_source_basename(value)
    patterns = (
        r"^(20\d{2})(\d{2})(\d{2})",
        r"^(20\d{2})[-.](\d{1,2})[-.](\d{1,2})",
        r"^(\d{3})[-/]?(\d{2})[-/]?(\d{2})(?:\s|$)",
    )
    for pattern in patterns:
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
    text = str(value or "")
    match = re.search(r"\((\d{2}/\d{2})文到\)|基準日\s*(\d{2}/\d{2})", text)
    if not match:
        return ""
    return str(match.group(1) or match.group(2) or "")


def _has_explicit_older_source_date_for_todo(todo: dict[str, Any], todo_date: date) -> bool:
    text = "\n".join(
        str(value or "")
        for value in (
            todo.get("source_file"),
            todo.get("file"),
            todo.get("description"),
            todo.get("case_number"),
            todo.get("client_name"),
        )
        if str(value or "").strip()
    )
    for match in re.finditer(r"(?:民國)?(\d{2,4})年(\d{1,2})月(\d{1,2})日", text):
        try:
            year = int(match.group(1))
            if year < 1911:
                year += 1911
            if year < todo_date.year and int(match.group(2)) == todo_date.month and int(match.group(3)) == todo_date.day:
                return True
        except Exception:
            continue
    return False


_HEARING_TODO_TYPES = {"開庭", "準備程序", "言詞辯論", "調解", "審理", "審理程序", "審判程序", "宣判", "訊問", "調查"}
_POSSIBLE_HEARING_AND_DEADLINE_TYPES = _HEARING_TODO_TYPES | {"補正", "上訴", "異議", "再議", "再抗告", "抗告", "繳費", "聲明", "陳報"}
_PDF_CALENDAR_QUARANTINE_REASON = "suspected_false_future_event"


def _source_context_has_explicit_same_todo_date(todo: dict[str, Any], todo_date: date) -> bool:
    text = "\n".join(_iter_source_context_values(todo))
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


def _has_explicit_todo_year_context(todo: dict[str, Any], todo_date: date) -> bool:
    """
    Return True when the source context has a clear signal for this todo year.
    """
    if _source_context_has_explicit_same_todo_date(todo, todo_date):
        return True

    for source in _iter_source_context_values(todo):
        source_date = _source_document_date(source)
        if source_date and source_date.year == todo_date.year:
            return True

    # Court-style year marks: 114年度 / 2026年度
    for source in _iter_source_context_values(todo):
        for match in re.finditer(r"(\d{2,3})年度", source):
            year = _parse_roc_year_to_ad(match.group(1))
            if year == todo_date.year:
                return True
        if source.startswith(str(todo_date.year)):
            return True

    for source in _iter_source_context_values(todo):
        for year in _collect_source_year_candidates(source):
            if year == todo_date.year:
                return True
    return False


def _source_has_low_confidence_old_year_signal(todo: dict[str, Any], todo_date: date) -> bool:
    source_context = "\n".join(_iter_source_context_values(todo))
    if not _source_mentions_todo_month_day(source_context, todo_date):
        return False
    years = [year for year in _collect_source_year_candidates(source_context) if year]
    return bool(years) and all(year < todo_date.year for year in years)


def _source_mentions_todo_month_day(source: Any, todo_date: date) -> bool:
    compact = re.sub(r"\s+", "", str(source or ""))
    tokens = {
        f"{todo_date.month}月{todo_date.day}日",
        f"{todo_date.month:02d}月{todo_date.day:02d}日",
        f"{todo_date.month}月{todo_date.day}號",
        f"{todo_date.month:02d}月{todo_date.day:02d}號",
    }
    return any(token in compact for token in tokens)


def _has_future_year_shift_from_old_source(
    todo: dict[str, Any],
    todo_date: date,
    *,
    today: date | None = None,
) -> bool:
    source = todo.get("source_file") or todo.get("file") or ""
    source_date = _source_document_date(source)
    source_year = _infer_source_base_year_from_todo(todo)
    today = today or datetime.now().date()
    if source_year is not None and todo_date.year <= source_year:
        return False
    if source_date is None:
        source_date = date(source_year, 1, 1) if source_year else None
    if not source_date:
        return False
    if source_date >= today - timedelta(days=30):
        return False
    todo_type = str(todo.get("type") or todo.get("todo_type") or "").strip()
    if todo_type not in _POSSIBLE_HEARING_AND_DEADLINE_TYPES:
        return False
    source_context = "\n".join(
        str(value or "")
        for value in (
            todo.get("source_file"),
            todo.get("file"),
            todo.get("description"),
            todo.get("case_number"),
            todo.get("client_name"),
        )
        if str(value or "").strip()
    )
    if _source_context_has_explicit_same_todo_date(todo, todo_date):
        return False
    return _source_mentions_todo_month_day(source_context, todo_date)


def _classify_pdf_todo_quarantine_reason(
    todo: dict[str, Any],
    todo_date: date,
    *,
    today: date | None = None,
) -> str | None:
    if not _has_explicit_todo_year_context(todo, todo_date):
        if _pdf_todo_uses_mismatched_source_date(todo, todo_date, today=today):
            return _PDF_CALENDAR_QUARANTINE_REASON
        if _source_has_low_confidence_old_year_signal(todo, todo_date):
            return _PDF_CALENDAR_QUARANTINE_REASON
    return None


def _pdf_todo_uses_mismatched_source_date(
    todo: dict[str, Any],
    todo_date: date,
    *,
    today: date | None = None,
) -> bool:
    source = todo.get("source_file") or todo.get("file") or ""
    today = today or datetime.now().date()
    source_date = _source_document_date(source)
    base_md = _description_base_month_day(todo.get("description"))
    if source_date and base_md and base_md != source_date.strftime("%m/%d"):
        return True
    if not source_date and base_md == today.strftime("%m/%d"):
        return True
    if _has_explicit_older_source_date_for_todo(todo, todo_date):
        return True
    return _has_future_year_shift_from_old_source(todo, todo_date, today=today)


@contextlib.contextmanager
def _pdf_scan_time_limit(seconds: int):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handle_timeout(_signum, _frame):
        raise _PdfScanTimeout(f"pdf_scan_timeout:{seconds}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_alarm = signal.alarm(0)
    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_alarm:
            signal.alarm(previous_alarm)


def _load_osc_action_module():
    path = ROOT / "skills" / "osc-orchestrator" / "action.py"
    spec = importlib.util.spec_from_file_location("_magi_osc_orchestrator_action", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_transcript_todo_module():
    path = ROOT / "skills" / "transcript-todo-extractor" / "action.py"
    spec = importlib.util.spec_from_file_location("_magi_transcript_todo_extractor", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _active_pdf_todos(
    todos: list[dict[str, Any]],
    *,
    today: date | None = None,
    max_future_days: int = 730,
    include_diagnostics: bool = False,
) -> tuple[list[dict[str, Any]], int, int] | tuple[list[dict[str, Any]], int, int, list[dict[str, Any]]]:
    """Keep only actionable PDF todos before writing them into OSC/Google."""
    today = today or datetime.now().date()
    latest = today + timedelta(days=max_future_days)
    active: list[dict[str, Any]] = []
    past_skipped = 0
    implausible_skipped = 0
    quarantined_todos: list[dict[str, Any]] = []
    for todo in todos or []:
        raw = str(todo.get("date") or "").strip()
        try:
            todo_date = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except Exception:
            implausible_skipped += 1
            continue
        if todo_date < today:
            past_skipped += 1
            continue
        if todo_date > latest:
            implausible_skipped += 1
            continue
        quarantine_reason = _classify_pdf_todo_quarantine_reason(todo, todo_date, today=today)
        if quarantine_reason:
            implausible_skipped += 1
            if include_diagnostics:
                quarantined_todos.append(
                    {
                        "quarantine_reason": quarantine_reason,
                        "type": todo.get("type") or todo.get("todo_type") or "",
                        "date": raw,
                        "description": (todo.get("description") or "").strip(),
                        "source_file": todo.get("source_file") or todo.get("file") or "",
                    }
                )
            continue
        active.append(todo)
    if include_diagnostics:
        return active, past_skipped, implausible_skipped, quarantined_todos
    return active, past_skipped, implausible_skipped


def _parse_cutoff_date(raw: Any, *, default: str = DEFAULT_HISTORY_CUTOFF_DATE) -> date:
    value = str(raw or "").strip() or default
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except Exception:
        return datetime.strptime(default, "%Y-%m-%d").date()


def _history_cutoff_date(args: argparse.Namespace | None = None) -> date:
    explicit = getattr(args, "history_cutoff_date", "") if args is not None else ""
    return _parse_cutoff_date(explicit or os.environ.get(HISTORY_CUTOFF_ENV) or DEFAULT_HISTORY_CUTOFF_DATE)


def _clamp_lookback_days_to_cutoff(lookback_days: int, cutoff: date, *, today: date | None = None) -> int:
    today = today or datetime.now().date()
    requested = max(0, int(lookback_days or 0))
    days_since_cutoff = max(0, (today - cutoff).days)
    return min(requested, days_since_cutoff)


def _complete_historical_todos(
    cutoff: date,
    *,
    dry_run: bool = False,
    status: str = "已完成",
) -> dict[str, Any]:
    """Mark pre-cutoff todos completed so old calendar rows are not recreated."""
    sys.path.insert(0, str(ROOT / "skills" / "osc-orchestrator"))
    try:
        from osc_headless.db import connect_mysql, db_config_from_env, ensure_osc_min_schema  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": f"missing_db_helpers:{type(exc).__name__}"}

    cfg = db_config_from_env(prefix="OSC_DB_")
    placeholders = ",".join(["%s"] * len(_TODO_DONE_STATUSES))
    cutoff_s = cutoff.isoformat()
    conn = None
    try:
        conn = connect_mysql(cfg)
        ensure_osc_min_schema(conn)
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM case_todos
                WHERE todo_date IS NOT NULL
                  AND todo_date < %s
                  AND (status IS NULL OR status='' OR LOWER(status) NOT IN ({placeholders}))
                """,
                (cutoff_s, *_TODO_DONE_STATUSES),
            )
            row = cur.fetchone() or {}
            matched = int((row or {}).get("c") or 0)
        finally:
            cur.close()
        if dry_run:
            return {"ok": True, "dry_run": True, "cutoff_date": cutoff_s, "matched": matched, "updated": 0, "status": status}
        cur = conn.cursor()
        try:
            cur.execute(
                f"""
                UPDATE case_todos
                SET status=%s, completed_date=COALESCE(completed_date, NOW())
                WHERE todo_date IS NOT NULL
                  AND todo_date < %s
                  AND (status IS NULL OR status='' OR LOWER(status) NOT IN ({placeholders}))
                """,
                (status, cutoff_s, *_TODO_DONE_STATUSES),
            )
            updated = int(getattr(cur, "rowcount", 0) or 0)
            conn.commit()
        finally:
            cur.close()
        return {"ok": True, "dry_run": False, "cutoff_date": cutoff_s, "matched": matched, "updated": updated, "status": status}
    except Exception as exc:
        return {"ok": False, "cutoff_date": cutoff_s, "error": f"{type(exc).__name__}: {str(exc)[:220]}"}
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def _run_pdf_calendar_scan(args: argparse.Namespace) -> dict[str, Any]:
    """Scan court PDFs with the same extractor used by the OSC web PDF tool."""
    from api.blueprints import osc_pdf

    requested_limit = max(1, int(getattr(args, "pdf_limit", 240)))
    target_limit_env = int(os.environ.get("OSC_EVENTS_REFRESH_PDF_CANDIDATE_LIMIT", "0") or "0")
    full_text_scan = os.environ.get("OSC_PDF_CALENDAR_FULL_TEXT_SCAN", "0").strip().lower() in {"1", "true", "yes", "on"}
    full_text_scan_limit = max(1, min(10000, int(os.environ.get("OSC_PDF_CALENDAR_FULL_TEXT_SCAN_LIMIT", "5000") or "5000")))
    limit = max(requested_limit, full_text_scan_limit) if full_text_scan else requested_limit
    target_limit = max(limit, min(target_limit_env, 10000)) if target_limit_env > 0 else limit
    max_pages = max(1, min(int(getattr(args, "pdf_max_pages", 8)), 20))
    dry_run = bool(getattr(args, "dry_run", False))
    scan_text = os.environ.get("OSC_PDF_CALENDAR_BULK_TEXT_ENABLE", "1").strip().lower() in {"1", "true", "yes", "on"}
    # The six-hour job must not miss filename-derived events. Keep that sweep
    # full-width; PDF text/OCR stays bounded here and becomes full-width only in
    # the nightly governance job.
    text_when_filename = os.environ.get("OSC_PDF_CALENDAR_BULK_TEXT_WHEN_FILENAME", "1").strip().lower() in {"1", "true", "yes", "on"}
    filename_sweep = os.environ.get("OSC_PDF_CALENDAR_FULL_FILENAME_SWEEP", "1").strip().lower() in {"1", "true", "yes", "on"}
    filename_sweep_limit = max(1, min(50000, int(os.environ.get("OSC_PDF_CALENDAR_FILENAME_SWEEP_LIMIT", "50000") or "50000")))
    file_timeout_sec = max(0, int(os.environ.get("OSC_PDF_CALENDAR_FILE_TIMEOUT_SEC", "12") or "12"))
    budget_sec = max(0, int(os.environ.get("OSC_PDF_CALENDAR_BUDGET_SEC", "360") or "360"))
    outer_budget = max(0, int(getattr(args, "scan_time_budget_sec", 0) or 0))
    if outer_budget:
        budget_sec = min(budget_sec, outer_budget)
    no_todo_cache_days = max(0, int(os.environ.get("OSC_PDF_CALENDAR_NO_TODO_CACHE_DAYS", "14") or "14"))
    if bool(getattr(args, "force_rebuild", False)):
        no_todo_cache_days = 0
    started = time.monotonic()
    scanned = inserted = updated = skipped = todo_count = event_count = warning_count = 0
    filename_sweep_scanned = 0
    text_scanned = 0
    filename_sweep_targets_count = 0
    text_targets_count = 0
    past_todo_count = 0
    implausible_todo_count = 0
    quarantined_todo_count = 0
    quarantined_todos: list[dict[str, Any]] = []
    timeout_count = 0
    error_count = 0
    cache_skipped = 0
    sample_items: list[dict[str, Any]] = []
    errors: list[str] = []
    cache_changed = False

    def _load_cache() -> dict[str, Any]:
        try:
            if PDF_SCAN_CACHE_PATH.exists():
                data = json.loads(PDF_SCAN_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("files", {})
                    return data
        except Exception:
            pass
        return {"version": 1, "files": {}}

    def _load_cursor() -> dict[str, Any]:
        try:
            if PDF_SCAN_CURSOR_PATH.exists():
                data = json.loads(PDF_SCAN_CURSOR_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {"version": 1, "case_offset": 0}

    def _save_cache(data: dict[str, Any]) -> None:
        files = data.get("files")
        if isinstance(files, dict) and len(files) > 20000:
            ordered = sorted(files.items(), key=lambda item: str((item[1] or {}).get("scanned_at") or ""))
            data["files"] = dict(ordered[-16000:])
        PDF_SCAN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PDF_SCAN_CACHE_PATH.with_suffix(PDF_SCAN_CACHE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(PDF_SCAN_CACHE_PATH)

    def _save_cursor(data: dict[str, Any]) -> None:
        PDF_SCAN_CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PDF_SCAN_CURSOR_PATH.with_suffix(PDF_SCAN_CURSOR_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(PDF_SCAN_CURSOR_PATH)

    def _file_signature(path: Any) -> tuple[str, int, int] | None:
        try:
            st = path.stat()
            return str(path), int(st.st_mtime), int(st.st_size)
        except Exception:
            return None

    scan_cache = _load_cache()
    cache_files = scan_cache.setdefault("files", {})
    scan_cursor = _load_cursor()
    try:
        total_case_rows = max(0, int(osc_pdf._count_all_case_pdf_case_rows()))
    except Exception:
        total_case_rows = 0
    configured_case_batch = max(1, min(500, int(os.environ.get("OSC_EVENTS_REFRESH_PDF_CASE_BATCH", "40") or "40")))
    case_batch = max(configured_case_batch, total_case_rows or configured_case_batch) if full_text_scan else configured_case_batch
    env_case_offset = os.environ.get("OSC_EVENTS_REFRESH_PDF_CASE_OFFSET")
    if full_text_scan:
        case_offset = 0
    elif env_case_offset is not None and str(env_case_offset).strip() != "":
        case_offset = max(0, int(env_case_offset or "0"))
    elif bool(getattr(args, "force_rebuild", False)):
        case_offset = 0
    else:
        case_offset = max(0, int(scan_cursor.get("case_offset") or 0))
    if total_case_rows and case_offset >= total_case_rows:
        case_offset = 0

    target_timeout_sec = max(5, min(max(10, budget_sec or 10), int(os.environ.get("OSC_PDF_CALENDAR_TARGET_TIMEOUT_SEC", "45") or "45")))

    def _load_targets(
        *,
        wanted_limit: int,
        wanted_offset: int,
        wanted_batch: int,
        filename_only: bool = False,
    ) -> list[tuple[Any, str, str]]:
        with _pdf_scan_time_limit(target_timeout_sec):
            try:
                return osc_pdf._iter_all_case_pdf_targets(
                    limit=wanted_limit,
                    case_offset=wanted_offset,
                    case_batch=wanted_batch,
                    filename_only=filename_only,
                )
            except TypeError:
                # Unit tests and older private plugins may monkeypatch the old
                # one-argument helper; keep dry-run verification compatible.
                return osc_pdf._iter_all_case_pdf_targets(limit=wanted_limit)

    target_specs: list[tuple[Any, str, str, bool, str]] = []
    target_seen: set[tuple[str, str]] = set()

    def _append_targets(items: list[tuple[Any, str, str]], *, use_text: bool, mode: str) -> None:
        nonlocal filename_sweep_targets_count, text_targets_count
        for path, case_number, client_name in items or []:
            key = (Path(str(path)).name, str(case_number or ""))
            if key in target_seen:
                # If the full filename sweep saw this first, upgrade the queued
                # item to a text/OCR fallback pass when it is also in the bounded
                # recent/rotating candidate set.
                if use_text:
                    for idx, (old_path, old_case, old_client, old_use_text, old_mode) in enumerate(target_specs):
                        old_key = (Path(str(old_path)).name, str(old_case or ""))
                        if old_key == key and not old_use_text:
                            target_specs[idx] = (old_path, old_case, old_client or client_name, True, "filename_then_text")
                            text_targets_count += 1
                            break
                continue
            target_seen.add(key)
            target_specs.append((path, case_number, client_name, use_text, mode))
            if use_text:
                text_targets_count += 1
            else:
                filename_sweep_targets_count += 1

    if filename_sweep:
        try:
            sweep_batch = max(case_batch, total_case_rows or case_batch)
            sweep_limit = max(target_limit, filename_sweep_limit)
            _append_targets(
                _load_targets(wanted_limit=sweep_limit, wanted_offset=0, wanted_batch=sweep_batch, filename_only=True),
                use_text=False,
                mode="filename_sweep",
            )
        except _PdfScanTimeout as exc:
            errors.append(f"filename_sweep_target_timeout:{str(exc)[:120]}")
        except Exception as exc:
            errors.append(f"filename_sweep_target_error:{type(exc).__name__}: {str(exc)[:160]}")

    try:
        _append_targets(
            _load_targets(wanted_limit=target_limit, wanted_offset=case_offset, wanted_batch=case_batch, filename_only=False),
            use_text=True,
            mode="text_fallback",
        )
    except _PdfScanTimeout as exc:
        return {
            "ok": False,
            "error": str(exc),
            "limit": limit,
            "candidate_limit": target_limit,
            "max_pages": max_pages,
            "target_timeout_sec": target_timeout_sec,
            "case_offset": case_offset,
            "case_batch": case_batch,
            "total_case_rows": total_case_rows,
            "filename_sweep": filename_sweep,
            "filename_sweep_targets": filename_sweep_targets_count,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            "limit": limit,
            "candidate_limit": target_limit,
            "max_pages": max_pages,
            "target_timeout_sec": target_timeout_sec,
            "case_offset": case_offset,
            "case_batch": case_batch,
            "total_case_rows": total_case_rows,
            "filename_sweep": filename_sweep,
            "filename_sweep_targets": filename_sweep_targets_count,
        }

    for path, case_number, client_name, use_text, scan_mode in target_specs:
        try:
            if use_text and text_scanned >= limit:
                continue
            if not use_text and filename_sweep_scanned >= filename_sweep_limit:
                continue
            if budget_sec and time.monotonic() - started > budget_sec:
                errors.append(f"budget_exhausted:{budget_sec}s")
                break
            signature = _file_signature(path)
            cache_key = signature[0] if signature else ""
            if use_text and signature and no_todo_cache_days:
                cached = cache_files.get(cache_key) if isinstance(cache_files, dict) else None
                if isinstance(cached, dict):
                    same_file = int(cached.get("mtime") or 0) == signature[1] and int(cached.get("size") or -1) == signature[2]
                    same_rule = str(cached.get("rule_version") or "") == PDF_SCAN_RULE_VERSION
                    cached_text_error = str(cached.get("text_error") or "").strip()
                    scanned_at = str(cached.get("scanned_at") or "")
                    try:
                        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(scanned_at)).total_seconds() / 86400
                    except Exception:
                        age_days = no_todo_cache_days + 1
                    if (
                        same_file
                        and same_rule
                        and int(cached.get("todo_count") or 0) == 0
                        and not cached_text_error
                        and age_days < no_todo_cache_days
                    ):
                        cache_skipped += 1
                        continue
            with _pdf_scan_time_limit(file_timeout_sec):
                item = osc_pdf._scan_pdf_for_calendar(
                    path,
                    case_number=case_number,
                    client_name=client_name,
                    max_pages=max_pages,
                    include_share_link=not dry_run,
                    scan_text=bool(scan_text and use_text),
                    text_when_filename=text_when_filename,
                )
            scanned += 1
            if use_text:
                text_scanned += 1
            else:
                filename_sweep_scanned += 1
            raw_todos = item.get("todos") or []
            for row in raw_todos:
                if not isinstance(row, dict):
                    continue
                if not row.get("source_file"):
                    row["source_file"] = str(path)
                row.setdefault("case_number", case_number)
                row.setdefault("client_name", client_name)
            todos, past_skipped, implausible_skipped, source_quarantine_records = _active_pdf_todos(
                raw_todos,
                include_diagnostics=True,
            )
            past_todo_count += past_skipped
            implausible_todo_count += implausible_skipped
            quarantined_todo_count += len(source_quarantine_records)
            for record in source_quarantine_records:
                record["source_case_number"] = str(case_number or item.get("case_number") or "")
                record["source_client_name"] = str(client_name or item.get("client_name") or "")
                record["scan_file"] = str(path)
                record["scan_mode"] = scan_mode
                record["pdf_path"] = str(path)
                quarantined_todos.append(record)
            todo_count += len(todos)
            event_count += len(todos)
            if signature and isinstance(cache_files, dict):
                text_error = str(item.get("text_error") or "")[:200]
                # Do not cache "no todo" when text/OCR was skipped or failed.
                # A transient OCR timeout or Synology placeholder must be retried
                # on the next sweep, otherwise court deadlines can silently vanish.
                if len(todos) == 0 and text_error:
                    if cache_key in cache_files:
                        cache_files.pop(cache_key, None)
                        cache_changed = True
                else:
                    cache_files[cache_key] = {
                        "mtime": signature[1],
                        "size": signature[2],
                        "todo_count": len(todos),
                        "rule_version": PDF_SCAN_RULE_VERSION,
                        "text_available": bool(item.get("text_available")),
                        "text_error": text_error,
                        "scan_mode": scan_mode,
                        "scanned_at": datetime.now(timezone.utc).isoformat(),
                    }
                    cache_changed = True
            if todos and not item.get("case_number"):
                warning_count += 1
            write_result = {"inserted": 0, "updated": 0, "skipped": 0}
            if todos and item.get("case_number") and not dry_run:
                write_result = osc_pdf._insert_todos_single_machine(
                    todos,
                    case_number=str(item.get("case_number") or ""),
                    client_name=str(item.get("client_name") or ""),
                    source_file=str(path),
                    allow_duplicates=False,
                )
                inserted += int(write_result.get("inserted") or 0)
                updated += int(write_result.get("updated") or 0)
                skipped += int(write_result.get("skipped") or 0)
            if todos and len(sample_items) < 12:
                sample_items.append(
                    {
                        "case_number": item.get("case_number") or case_number,
                        "client_name": item.get("client_name") or client_name,
                        "file_name": path.name,
                        "scan_mode": scan_mode,
                        "todo_count": len(todos),
                        "event_count": len(todos),
                        "write_result": write_result,
                        "todos": todos[:3],
                    }
                )
        except _PdfScanTimeout as exc:
            timeout_count += 1
            if len(errors) < 20:
                errors.append(f"{path.name}: {str(exc)[:200]}")
        except Exception as exc:
            error_count += 1
            if len(errors) < 20:
                errors.append(f"{path.name}: {type(exc).__name__}: {str(exc)[:200]}")

    if cache_changed:
        try:
            _save_cache(scan_cache)
        except Exception as exc:
            if len(errors) < 20:
                errors.append(f"cache_save_failed:{type(exc).__name__}: {str(exc)[:160]}")

    next_case_offset = 0 if full_text_scan else ((case_offset + case_batch) if total_case_rows else case_offset)
    if total_case_rows and next_case_offset >= total_case_rows:
        next_case_offset = 0
    if not dry_run and not full_text_scan:
        try:
            _save_cursor(
                {
                    "version": 1,
                    "case_offset": next_case_offset,
                    "last_case_offset": case_offset,
                    "case_batch": case_batch,
                    "total_case_rows": total_case_rows,
                    "rule_version": PDF_SCAN_RULE_VERSION,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception as exc:
            if len(errors) < 20:
                errors.append(f"cursor_save_failed:{type(exc).__name__}: {str(exc)[:160]}")

    return {
        "ok": True,
        "dry_run": dry_run,
        "limit": limit,
        "requested_limit": requested_limit,
        "candidate_limit": target_limit,
        "full_text_scan": full_text_scan,
        "full_text_scan_limit": full_text_scan_limit,
        "max_pages": max_pages,
        "scan_text": scan_text,
        "text_when_filename": text_when_filename,
        "filename_sweep": filename_sweep,
        "filename_sweep_limit": filename_sweep_limit,
        "file_timeout_sec": file_timeout_sec,
        "budget_sec": budget_sec,
        "target_timeout_sec": target_timeout_sec,
        "case_offset": case_offset,
        "case_batch": case_batch,
        "next_case_offset": next_case_offset,
        "total_case_rows": total_case_rows,
        "no_todo_cache_days": no_todo_cache_days,
        "rule_version": PDF_SCAN_RULE_VERSION,
        "targets": len(target_specs),
        "filename_sweep_targets": filename_sweep_targets_count,
        "text_targets": text_targets_count,
        "scanned": scanned,
        "filename_sweep_scanned": filename_sweep_scanned,
        "text_scanned": text_scanned,
        "cache_skipped": cache_skipped,
        "todo_count": todo_count,
        "event_count": event_count,
        "past_todo_count": past_todo_count,
        "implausible_todo_count": implausible_todo_count,
        "quarantine_todo_count": quarantined_todo_count,
        "timeout_count": timeout_count,
        "error_count": error_count,
        "quarantined_todos": quarantined_todos[:20],
        "write_result": {
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "warnings": warning_count,
        },
        "sample_items": sample_items,
        "errors": errors,
        "elapsed_sec": round(time.monotonic() - started, 3),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


def _write_latest(data: dict[str, Any], out_path: Path = LATEST_PATH) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(_json_safe(data), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out_path)


def _run_drive_case_sync_before_pdf(args: argparse.Namespace) -> dict[str, Any]:
    """Run a bounded Drive/NAS missing-file sync before PDF todo extraction.

    Google Drive and NAS intentionally keep different folder naming rules.  The
    worker handles that mapping; this hook only makes sure Drive-only PDFs reach
    the NAS before the filename/OCR todo scanner runs.
    """
    if bool(getattr(args, "dry_run", False)):
        return {"ok": True, "skipped": True, "reason": "dry_run"}
    if os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_ENABLE", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "disabled_by_env"}

    all_cases = bool(getattr(args, "drive_sync_all_cases", False) or getattr(args, "force_rebuild", False))
    download_limit = max(0, int(getattr(args, "drive_sync_download_limit", 24)))
    upload_limit = max(0, int(getattr(args, "drive_sync_upload_limit", 0)))
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "drive_case_sync_worker.py"),
        "--max-download-bytes",
        str(max(0, int(getattr(args, "drive_sync_max_download_bytes", 400_000_000)))),
        "--max-upload-bytes",
        str(max(0, int(getattr(args, "drive_sync_max_upload_bytes", 400_000_000)))),
        "--max-case-depth",
        str(max(1, int(getattr(args, "drive_sync_max_case_depth", 5)))),
        "--max-case-items",
        str(max(1, int(getattr(args, "drive_sync_max_case_items", 220)))),
        "--create-drive-folder-limit",
        str(max(0, int(getattr(args, "drive_sync_create_folder_limit", 12)))),
        "--priority-upcoming-days",
        str(max(0, int(getattr(args, "drive_sync_priority_days", 21)))),
        "--priority-case-limit",
        str(max(1, int(getattr(args, "drive_sync_priority_case_limit", 80)))),
        "--inventory-timeout-sec",
        str(max(30, int(getattr(args, "drive_sync_timeout_sec", 900)))),
    ]
    if download_limit <= 0:
        cmd.append("--no-downloads")
    else:
        cmd.extend(["--download-limit", str(download_limit)])
    if upload_limit <= 0:
        cmd.append("--no-uploads")
    else:
        cmd.extend(["--upload-limit", str(upload_limit)])
    if all_cases:
        cmd.extend([
            "--direct-all-cases",
            "--direct-all-case-limit",
            str(max(1, int(getattr(args, "drive_sync_all_case_limit", 32)))),
        ])
    else:
        cmd.extend([
            "--direct-priority-case-limit",
            str(max(1, int(getattr(args, "drive_sync_priority_direct_limit", 24)))),
        ])

    env = os.environ.copy()
    env.setdefault("MAGI_DRIVE_SYNC_LOCAL_SCAN_TIMEOUT_SEC", "5")
    env.setdefault("MAGI_DRIVE_SYNC_DRIVE_LIST_TIMEOUT_SEC", "15")
    env.setdefault("MAGI_DRIVE_SYNC_HTTP_TIMEOUT", env.get("MAGI_DRIVE_SYNC_DRIVE_LIST_TIMEOUT_SEC", "15"))
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=max(60, int(getattr(args, "drive_sync_timeout_sec", 900)) + 30),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "status": "timeout",
            "mode": "direct_all_cases" if all_cases else "priority_cases",
            "error": f"drive_case_sync_timeout:{exc.timeout}s",
        }
    stdout = (completed.stdout or "").strip()
    parsed: dict[str, Any] = {}
    if stdout:
        try:
            parsed = json.loads(stdout[stdout.find("{"):])
        except Exception:
            parsed = {"raw_stdout": stdout[-1200:]}
    if completed.returncode != 0:
        return {
            "ok": False,
            "status": "failed",
            "mode": "direct_all_cases" if all_cases else "priority_cases",
            "returncode": completed.returncode,
            "summary": parsed.get("summary") or {},
            "execution_summary": parsed.get("execution_summary") or {},
            "stderr": (completed.stderr or "")[-1200:],
        }
    return {
        "ok": bool(parsed.get("ok", True)),
        "status": "ok",
        "mode": parsed.get("mode") or ("direct_all_cases" if all_cases else "priority_cases"),
        "all_case_total": parsed.get("all_case_total", 0),
        "summary": parsed.get("summary") or {},
        "file_sync_summary": parsed.get("file_sync_summary") or {},
        "execution_summary": parsed.get("execution_summary") or {},
        "drive_folder_summary": parsed.get("drive_folder_summary") or {},
        "priority_case_numbers": (parsed.get("priority_case_numbers") or [])[:20],
        "all_case_numbers": (parsed.get("all_case_numbers") or [])[:20],
        "output_paths": parsed.get("output_paths") or {},
    }


_CALENDAR_SOURCE_AUDIT_EXCLUDE_TEXT = (
    "法扶進度",
    "未結案件進度回報",
    "進度回報末日",
    "法扶開辦",
    "開辦末日",
    "律見",
    "接見",
    "親見",
    "會議",
    "電話",
    "諮詢",
)

_CALENDAR_SOURCE_AUDIT_INCLUDE_TEXT = (
    "補正",
    "陳報",
    "提出",
    "起訴",
    "上訴",
    "抗告",
    "異議",
    "聲明",
    "承受訴訟",
    "繳費",
    "開庭",
    "調解",
    "審理",
    "準備程序",
    "言詞辯論",
    "辯論",
    "訊問",
    "確認",
    "候核",
    "法院",
    "地院",
    "高院",
    "最高法院",
    "地檢",
    "檢察署",
    "裁定",
    "判決",
    "通知",
    "函",
    "庭",
)


def _calendar_row_likely_needs_pdf_source(row: dict[str, Any]) -> bool:
    text = "".join(str(row.get(key) or "") for key in ("todo_type", "description", "client_name"))
    compact = "".join(text.split())
    if not compact:
        return False
    if any(token in compact for token in _CALENDAR_SOURCE_AUDIT_EXCLUDE_TEXT):
        return False
    return any(token in compact for token in _CALENDAR_SOURCE_AUDIT_INCLUDE_TEXT)


def _run_calendar_gap_drive_remediation(
    rows: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if not rows:
        return {"ok": True, "skipped": True, "reason": "no_calendar_source_gaps"}
    if bool(getattr(args, "dry_run", False)):
        return {"ok": True, "skipped": True, "reason": "dry_run"}
    if bool(getattr(args, "skip_drive_sync", False)):
        return {"ok": True, "skipped": True, "reason": "drive_sync_skipped_by_args"}
    if os.environ.get("OSC_EVENTS_REFRESH_SOURCE_AUDIT_DRIVE_REMEDIATE_ENABLE", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "disabled_by_env"}

    case_numbers: list[str] = []
    seen: set[str] = set()
    for row in rows:
        case_number = str(row.get("case_number") or "").strip()
        if case_number and case_number not in seen:
            seen.add(case_number)
            case_numbers.append(case_number)
    if not case_numbers:
        return {"ok": True, "skipped": True, "reason": "no_case_numbers"}

    try:
        from api.osc.drive_case_sync import run_priority_case_sync
    except Exception as exc:
        return {"ok": False, "error": f"load_drive_sync_failed:{type(exc).__name__}"}

    download_limit = max(1, min(80, int(os.environ.get("OSC_EVENTS_REFRESH_SOURCE_AUDIT_DOWNLOAD_LIMIT", "24") or "24")))
    max_download_bytes = max(
        1,
        min(
            1_500_000_000,
            int(os.environ.get("OSC_EVENTS_REFRESH_SOURCE_AUDIT_MAX_DOWNLOAD_BYTES", "400000000") or "400000000"),
        ),
    )
    try:
        report = run_priority_case_sync(
            case_numbers=case_numbers,
            root_name=os.environ.get("MAGI_DRIVE_SYNC_ROOT_NAME", "案件辦理"),
            file_diff=True,
            execute_downloads=True,
            execute_uploads=False,
            download_limit=download_limit,
            max_download_bytes=max_download_bytes,
            upload_limit=0,
            max_upload_bytes=0,
            max_case_depth=max(1, int(os.environ.get("OSC_EVENTS_REFRESH_SOURCE_AUDIT_MAX_CASE_DEPTH", "5") or "5")),
            max_case_items=max(1, int(os.environ.get("OSC_EVENTS_REFRESH_SOURCE_AUDIT_MAX_CASE_ITEMS", "260") or "260")),
            ensure_drive_case_folders=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "case_numbers": case_numbers,
            "error": f"{type(exc).__name__}: {str(exc)[:220]}",
        }

    execution = report.get("execution_result") or report.get("execution_summary") or {}
    file_sync = report.get("file_sync_plan") or report.get("file_sync_summary") or {}
    return {
        "ok": bool(report.get("ok", True)),
        "case_numbers": case_numbers,
        "download_limit": download_limit,
        "max_download_bytes": max_download_bytes,
        "summary": report.get("summary") or {},
        "file_sync_summary": file_sync.get("summary") if isinstance(file_sync, dict) else file_sync,
        "execution_summary": execution.get("summary") if isinstance(execution, dict) else execution,
        "drive_folder_summary": (report.get("drive_folder_result") or {}).get("summary") or report.get("drive_folder_summary") or {},
        "output_paths": report.get("output_paths") or {},
        "message": "已先依案件號到 Google Drive 對應資料夾補檔；若 Google 也沒有，才保留缺來源警示。",
    }


def _run_calendar_source_audit(args: argparse.Namespace) -> dict[str, Any]:
    """List imported calendar todos that still lack a MAGI/PDF source row.

    These rows may be legitimate manual entries.  The important part is that
    they are not evidence that PDF todo extraction worked, because they have no
    source PDF/share link attached.  Do not let manual rows or non-calendar
    placeholders hide this gap: a matching OSC row must point to a PDF source.
    """
    if os.environ.get("OSC_EVENTS_REFRESH_SOURCE_AUDIT_ENABLE", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "disabled_by_env"}
    try:
        skill_dir = ROOT / "skills" / "osc-orchestrator"
        if str(skill_dir) not in sys.path:
            sys.path.insert(0, str(skill_dir))
        from osc_headless.db import connect_mysql, db_config_from_env  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": f"load_db_helper_failed:{type(exc).__name__}"}

    lookahead_days = max(1, int(getattr(args, "lookahead_days", 30) or 30))
    limit = max(1, min(int(getattr(args, "gcal_push_limit", 120) or 120), 300))
    start = date.today()
    end = start + timedelta(days=lookahead_days)
    inactive = (
        "deleted",
        "calendar_deduped",
        "completed",
        "done",
        "已完成",
        "完成",
        "cancelled",
        "canceled",
        "取消",
    )
    inactive_placeholders = ",".join(["%s"] * len(inactive))
    conn = None
    try:
        conn = connect_mysql(db_config_from_env(prefix="OSC_DB_"))
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                f"""
                SELECT
                  g.id, g.case_number, g.client_name, g.todo_type, g.todo_date,
                  g.todo_time, g.status, g.source_file, LEFT(COALESCE(g.description,''), 220) AS description
                FROM case_todos g
                WHERE COALESCE(g.case_number, '') != ''
                  AND g.todo_date BETWEEN %s AND %s
                  AND (g.source_file LIKE 'gcal_import%%' OR g.todo_type = '行事曆事件')
                  AND (g.status IS NULL OR g.status = '' OR g.status NOT IN ({inactive_placeholders}))
                  AND NOT EXISTS (
                    SELECT 1
                    FROM case_todos o
                    WHERE o.case_number = g.case_number
                      AND o.id <> g.id
                      AND o.todo_date = g.todo_date
                      AND (
                        (o.todo_time = g.todo_time)
                        OR (o.todo_time IS NULL AND g.todo_time IS NULL)
                      )
                      AND (o.status IS NULL OR o.status = '' OR o.status NOT IN ({inactive_placeholders}))
                      AND COALESCE(o.source_file, '') LIKE '%%.pdf%%'
                      AND o.source_file NOT LIKE 'gcal_import%%'
                      AND o.source_file NOT LIKE 'gcal_mirror:%%'
                      AND COALESCE(o.todo_type, '') <> '行事曆事件'
                  )
                ORDER BY g.todo_date ASC, g.todo_time ASC, g.case_number ASC, g.id ASC
                LIMIT %s
                """,
                (
                    start.isoformat(),
                    end.isoformat(),
                    *inactive,
                    *inactive,
                    limit,
                ),
            )
            rows = [dict(r) for r in (cur.fetchall() or [])]
        finally:
            cur.close()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:220]}"}
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    rows = [row for row in rows if _calendar_row_likely_needs_pdf_source(row)]
    drive_remediation = _run_calendar_gap_drive_remediation(rows, args=args)

    for row in rows:
        for key in ("todo_date", "todo_time"):
            if row.get(key) is not None:
                row[key] = str(row.get(key))
    return {
        "ok": True,
        "lookahead_days": lookahead_days,
        "calendar_import_only_count": len(rows),
        "sample_items": rows[:20],
        "drive_remediation": drive_remediation,
        "message": "這些是人工/Google 匯入且看起來應由 PDF 支撐的行程；MAGI 已先嘗試從 Google Drive 補檔，補不到才保留缺來源。",
    }


def _copy_args(args: argparse.Namespace, **updates: Any) -> argparse.Namespace:
    data = dict(vars(args))
    data.update(updates)
    return argparse.Namespace(**data)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _calendar_source_audit_downloaded_count(audit: dict[str, Any]) -> int:
    remediation = audit.get("drive_remediation") if isinstance(audit, dict) else {}
    if not isinstance(remediation, dict):
        return 0
    summary = remediation.get("execution_summary") or {}
    if not isinstance(summary, dict):
        return 0
    return max(
        _safe_int(summary.get("downloaded")),
        _safe_int(summary.get("download_downloaded")),
    )


def _remove_warning(warnings: list[str], value: str) -> None:
    while value in warnings:
        warnings.remove(value)


def _run_pdf_todo_share_link_repair(args: argparse.Namespace) -> dict[str, Any]:
    if os.environ.get("OSC_EVENTS_REFRESH_SHARE_REPAIR_ENABLE", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "share_repair_disabled"}
    try:
        from scripts.ops.backfill_pdf_todo_share_links import backfill

        limit = max(1, min(1000, int(os.environ.get("OSC_EVENTS_REFRESH_SHARE_REPAIR_LIMIT", "300") or "300")))
        limit = max(limit, min(1000, int(getattr(args, "gcal_push_limit", 120) or 120) * 2))
        lookback_days = max(
            0,
            int(
                os.environ.get("OSC_PDF_TODO_SHARE_REPAIR_LOOKBACK_DAYS")
                or getattr(args, "lookback_days", 30)
                or 30
            ),
        )
        renew_within_days = max(0, int(os.environ.get("OSC_PDF_TODO_SHARE_RENEW_WITHIN_DAYS", "7") or "7"))
        return backfill(
            limit=limit,
            execute=not bool(getattr(args, "dry_run", False)),
            lookback_days=lookback_days,
            renew_within_days=renew_within_days,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}


def _run_refresh_locked(args: argparse.Namespace) -> dict[str, Any]:
    os.environ.setdefault("MAGI_GCAL_DEDUP_ENABLED", "1")
    os.environ.setdefault("MAGI_GCAL_DEDUP_DRY_RUN", "0")
    os.environ.setdefault("MAGI_GCAL_INCREMENTAL_IMPORT", "1")
    os.environ.setdefault("MAGI_GCAL_REPAIR_EXISTING", "1")

    mod = _load_osc_action_module()
    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "interval_hours": 6,
        "dry_run": bool(getattr(args, "dry_run", False)),
        "history_cutoff_date": _history_cutoff_date(args).isoformat(),
        "historical_todo_completion": {},
        "scan": {},
        "drive_case_sync": {},
        "pdf_calendar_scan": {},
        "transcript_todos": {},
        "calendar_import": {},
        "pdf_todo_share_repair": {},
        "calendar_push": {},
        "calendar_audit": {},
        "calendar_source_audit": {},
        "warnings": [],
    }
    history_cutoff = _parse_cutoff_date(result["history_cutoff_date"])
    result["historical_todo_completion"] = _complete_historical_todos(
        history_cutoff,
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    if not result["historical_todo_completion"].get("ok"):
        result["ok"] = False
        result["warnings"].append("historical_todo_completion_failed")

    def _remaining_scan_budget_sec() -> int | None:
        total = int(getattr(args, "scan_time_budget_sec", 0) or 0)
        if total <= 0:
            return None
        return max(0, total - int(time.monotonic() - started))

    def _run_transcript_todo_step() -> dict[str, Any]:
        try:
            remaining = _remaining_scan_budget_sec()
            if remaining is not None and remaining <= 0:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": f"transcript_todo_budget_exhausted:{args.scan_time_budget_sec}s",
                }
            os.environ.setdefault("TRANSCRIPT_TODO_LISTING_BUDGET_SEC", "45")
            env_timeout = int(os.environ.get("OSC_TRANSCRIPT_TODO_TIMEOUT_SEC", "0") or "0")
            transcript_timeout = env_timeout if env_timeout > 0 else 120
            min_dry_run_budget = max(1, int(os.environ.get("OSC_TRANSCRIPT_TODO_MIN_DRY_RUN_BUDGET_SEC", "120") or "120"))
            if (
                bool(getattr(args, "dry_run", False))
                and remaining is not None
                and remaining < min_dry_run_budget
            ):
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "transcript_todo_dry_run_budget_too_small",
                    "remaining_sec": remaining,
                    "required_sec": min_dry_run_budget,
                }
            if remaining is not None:
                transcript_timeout = max(1, min(transcript_timeout, remaining))
            transcript_mod = _load_transcript_todo_module()
            transcript_limit = max(1, int(getattr(args, "transcript_limit", 120)))
            transcript_tail_pages = max(1, int(getattr(args, "transcript_tail_pages", 3)))
            with _pdf_scan_time_limit(transcript_timeout):
                paths = transcript_mod._iter_pdf_targets("", limit=transcript_limit)
                scan = transcript_mod.scan_targets(paths, tail_pages=transcript_tail_pages)
                if bool(getattr(args, "dry_run", False)):
                    write = {"dry_run": True, "inserted": 0, "updated": 0, "skipped": 0, "past_skipped": 0}
                else:
                    write = transcript_mod.apply_high_confidence(scan.get("items") or [])
            return {
                "ok": True,
                "timeout_sec": transcript_timeout,
                "scanned": scan.get("scanned", 0),
                "high_count": scan.get("high_count", 0),
                "review_count": scan.get("review_count", 0),
                "past_skipped": scan.get("past_skipped", 0),
                "implausible_skipped": scan.get("implausible_skipped", 0),
                "errors_count": scan.get("errors_count", 0),
                "write_result": {
                    "inserted": write.get("inserted", 0),
                    "updated": write.get("updated", 0),
                    "skipped": write.get("skipped", 0),
                    "past_skipped": write.get("past_skipped", 0),
                },
                "sample_items": (scan.get("items") or [])[:10],
                "errors": scan.get("errors", [])[:10],
            }
        except _PdfScanTimeout as exc:
            result["warnings"].append("transcript_todo_timeout")
            return {"ok": False, "skipped": True, "error": str(exc)}
        except Exception as exc:
            result["ok"] = False
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

    if not args.calendar_only:
        if bool(getattr(args, "legacy_scan", False)):
            try:
                result["scan"] = mod.task_scan_cases(
                    {
                        "max_cases": args.max_cases,
                        "max_files_per_case": args.max_files_per_case,
                        "time_budget_sec": args.scan_time_budget_sec,
                        "dry_run": bool(getattr(args, "dry_run", False)),
                        "force_rebuild": bool(args.force_rebuild),
                    }
                )
            except Exception as exc:
                result["ok"] = False
                result["scan"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
        else:
            result["scan"] = {
                "ok": True,
                "skipped": True,
                "reason": "legacy_scan_disabled; pdf_calendar_scan is the unified bounded todo scanner",
            }

        if not getattr(args, "skip_drive_sync", False):
            result["drive_case_sync"] = _run_drive_case_sync_before_pdf(args)
            if not result["drive_case_sync"].get("ok"):
                result["warnings"].append("drive_case_sync_before_pdf_failed")

        if not getattr(args, "skip_pdf_todos", False):
            try:
                result["pdf_calendar_scan"] = _run_pdf_calendar_scan(args)
                if not result["pdf_calendar_scan"].get("ok"):
                    err = str(result["pdf_calendar_scan"].get("error") or "")
                    if err.startswith("pdf_scan_timeout"):
                        result["warnings"].append("pdf_calendar_scan_timeout")
                    else:
                        result["ok"] = False
            except Exception as exc:
                result["ok"] = False
                result["pdf_calendar_scan"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

        if not getattr(args, "skip_transcript_todos", False):
            result["transcript_todos"] = {
                "ok": True,
                "deferred": True,
                "reason": "runs_after_calendar_push_so_pdf_todos_cannot_be_blocked",
            }

    if not args.scan_only:
        if bool(getattr(args, "dry_run", False)):
            result["calendar_import"] = {"ok": True, "dry_run": True, "skipped": True}
            result["calendar_push"] = {"ok": True, "dry_run": True, "skipped": True}
        else:
            try:
                calendar_payload = {
                    "lookback_days": _clamp_lookback_days_to_cutoff(args.lookback_days, history_cutoff),
                    "lookahead_days": args.lookahead_days,
                    "limit": args.calendar_limit,
                    "incremental": True,
                    "history_cutoff_date": history_cutoff.isoformat(),
                }
                import_timeout = max(1, int(os.environ.get("OSC_EVENTS_REFRESH_GCAL_IMPORT_TIMEOUT_SEC", "180") or "180"))
                with _pdf_scan_time_limit(import_timeout):
                    cal = mod.task_gcal_import(calendar_payload)
                result["calendar_import"] = cal
                if not cal.get("ok") and cal.get("need_interactive_oauth"):
                    result["warnings"].append("google_calendar_oauth_required")
                elif not cal.get("ok"):
                    result["ok"] = False
            except _PdfScanTimeout as exc:
                result["ok"] = False
                result["warnings"].append("google_calendar_import_timeout")
                result["calendar_import"] = {"ok": False, "error": str(exc)}
            except Exception as exc:
                result["ok"] = False
                result["calendar_import"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

            try:
                share_timeout = max(1, int(os.environ.get("OSC_EVENTS_REFRESH_SHARE_REPAIR_TIMEOUT_SEC", "120") or "120"))
                with _pdf_scan_time_limit(share_timeout):
                    result["pdf_todo_share_repair"] = _run_pdf_todo_share_link_repair(args)
                if not result["pdf_todo_share_repair"].get("ok"):
                    result["warnings"].append("pdf_todo_share_repair_failed")
            except _PdfScanTimeout as exc:
                result["warnings"].append("pdf_todo_share_repair_timeout")
                result["pdf_todo_share_repair"] = {"ok": False, "error": str(exc)}
            except Exception as exc:
                result["warnings"].append("pdf_todo_share_repair_failed")
                result["pdf_todo_share_repair"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

            share_repair_ids = []
            for item in result.get("pdf_todo_share_repair", {}).get("items") or []:
                if not isinstance(item, dict) or str(item.get("status") or "") != "updated":
                    continue
                todo_id = _safe_int(item.get("id"))
                if todo_id:
                    share_repair_ids.append(todo_id)
            share_repair_updated = int(result.get("pdf_todo_share_repair", {}).get("updated") or 0)
            try:
                repair_limit = min(max(args.gcal_push_limit, args.gcal_push_limit + share_repair_updated), 400)
                push_payload = {
                    "limit": args.gcal_push_limit,
                    "repair_existing": True,
                    "repair_limit": repair_limit,
                    "repair_todo_ids": share_repair_ids[:400],
                    "repair_lookback_days": getattr(args, "lookback_days", 30),
                    "retry_max_attempts": 3,
                    "history_cutoff_date": history_cutoff.isoformat(),
                }
                push_timeout = max(1, int(os.environ.get("OSC_EVENTS_REFRESH_GCAL_PUSH_TIMEOUT_SEC", "180") or "180"))
                with _pdf_scan_time_limit(push_timeout):
                    pushed = mod.task_gcal_sync(push_payload)
                result["calendar_push"] = pushed
                if not pushed.get("ok") and pushed.get("need_interactive_oauth"):
                    result["warnings"].append("google_calendar_oauth_required")
                elif not pushed.get("ok"):
                    err = str(pushed.get("error") or "")
                    if any(key in err.lower() for key in ("credential", "oauth", "token", "invalid_grant")):
                        result["warnings"].append("google_calendar_oauth_required")
                    else:
                        result["ok"] = False
            except _PdfScanTimeout as exc:
                result["ok"] = False
                result["warnings"].append("google_calendar_push_timeout")
                result["calendar_push"] = {"ok": False, "error": str(exc)}
            except Exception as exc:
                result["ok"] = False
                result["calendar_push"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

            if not getattr(args, "skip_calendar_audit", False):
                try:
                    audit_timeout = max(1, int(os.environ.get("OSC_EVENTS_REFRESH_GCAL_AUDIT_TIMEOUT_SEC", "120") or "120"))
                    with _pdf_scan_time_limit(audit_timeout):
                        audit = mod.task_gcal_integrity_audit({"limit": args.gcal_push_limit})
                    result["calendar_audit"] = audit
                    if not audit.get("ok"):
                        if audit.get("need_interactive_oauth"):
                            result["warnings"].append("google_calendar_oauth_required")
                        elif audit.get("error"):
                            result["ok"] = False
                            result["warnings"].append("google_calendar_integrity_failed")
                        else:
                            # A consistency audit finding should remain visible
                            # but must not make the six-hour todo refresh look
                            # failed after scan/import/push already succeeded.
                            result["warnings"].append("google_calendar_integrity_needs_attention")
                except _PdfScanTimeout as exc:
                    result["warnings"].append("google_calendar_integrity_timeout")
                    result["calendar_audit"] = {"ok": False, "error": str(exc)}
                except Exception as exc:
                    result["ok"] = False
                    result["calendar_audit"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

            result["calendar_source_audit"] = _run_calendar_source_audit(args)
            if not result["calendar_source_audit"].get("ok"):
                result["warnings"].append("calendar_source_audit_failed")
            elif int(result["calendar_source_audit"].get("calendar_import_only_count") or 0) > 0:
                result["warnings"].append("calendar_import_only_without_pdf_source")

            remediation_downloaded = _calendar_source_audit_downloaded_count(result["calendar_source_audit"])
            if remediation_downloaded > 0 and not getattr(args, "skip_pdf_todos", False):
                try:
                    result["pdf_calendar_scan_after_drive_remediation"] = _run_pdf_calendar_scan(args)
                    if not result["pdf_calendar_scan_after_drive_remediation"].get("ok"):
                        err = str(result["pdf_calendar_scan_after_drive_remediation"].get("error") or "")
                        if err.startswith("pdf_scan_timeout"):
                            result["warnings"].append("pdf_calendar_rescan_after_drive_remediation_timeout")
                        else:
                            result["ok"] = False
                            result["warnings"].append("pdf_calendar_rescan_after_drive_remediation_failed")
                except Exception as exc:
                    result["ok"] = False
                    result["warnings"].append("pdf_calendar_rescan_after_drive_remediation_failed")
                    result["pdf_calendar_scan_after_drive_remediation"] = {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                    }

                if result.get("pdf_calendar_scan_after_drive_remediation", {}).get("ok"):
                    try:
                        push_payload = {
                            "limit": args.gcal_push_limit,
                            "repair_existing": True,
                            "repair_limit": args.gcal_push_limit,
                            "retry_max_attempts": 3,
                            "history_cutoff_date": history_cutoff.isoformat(),
                        }
                        push_timeout = max(1, int(os.environ.get("OSC_EVENTS_REFRESH_GCAL_PUSH_TIMEOUT_SEC", "180") or "180"))
                        with _pdf_scan_time_limit(push_timeout):
                            result["calendar_push_after_drive_remediation"] = mod.task_gcal_sync(push_payload)
                        if not result["calendar_push_after_drive_remediation"].get("ok"):
                            if result["calendar_push_after_drive_remediation"].get("need_interactive_oauth"):
                                result["warnings"].append("google_calendar_oauth_required")
                            else:
                                result["warnings"].append("calendar_push_after_drive_remediation_failed")
                    except _PdfScanTimeout as exc:
                        result["warnings"].append("calendar_push_after_drive_remediation_timeout")
                        result["calendar_push_after_drive_remediation"] = {"ok": False, "error": str(exc)}
                    except Exception as exc:
                        result["warnings"].append("calendar_push_after_drive_remediation_failed")
                        result["calendar_push_after_drive_remediation"] = {
                            "ok": False,
                            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                        }

                    audit_args = _copy_args(args, skip_drive_sync=True)
                    result["calendar_source_audit_after_drive_remediation"] = _run_calendar_source_audit(audit_args)
                    if result["calendar_source_audit_after_drive_remediation"].get("ok"):
                        remaining_count = int(
                            result["calendar_source_audit_after_drive_remediation"].get("calendar_import_only_count") or 0
                        )
                        if remaining_count <= 0:
                            _remove_warning(result["warnings"], "calendar_import_only_without_pdf_source")
                    else:
                        result["warnings"].append("calendar_source_audit_after_drive_remediation_failed")

    if not args.calendar_only and not getattr(args, "skip_transcript_todos", False):
        result["transcript_todos"] = _run_transcript_todo_step()
        write_result = result["transcript_todos"].get("write_result") or {}
        transcript_changed = int(write_result.get("inserted") or 0) + int(write_result.get("updated") or 0)
        if transcript_changed and not args.scan_only and not bool(getattr(args, "dry_run", False)):
            try:
                push_payload = {
                    "limit": min(max(20, args.gcal_push_limit), 200),
                    "repair_existing": True,
                    "repair_limit": min(max(20, args.gcal_push_limit), 200),
                    "retry_max_attempts": 3,
                    "history_cutoff_date": history_cutoff.isoformat(),
                }
                push_timeout = max(1, int(os.environ.get("OSC_EVENTS_REFRESH_GCAL_PUSH_TIMEOUT_SEC", "180") or "180"))
                with _pdf_scan_time_limit(push_timeout):
                    result["calendar_push_after_transcript"] = mod.task_gcal_sync(push_payload)
                if not result["calendar_push_after_transcript"].get("ok"):
                    if result["calendar_push_after_transcript"].get("need_interactive_oauth"):
                        result["warnings"].append("google_calendar_oauth_required")
                    else:
                        result["warnings"].append("calendar_push_after_transcript_failed")
            except _PdfScanTimeout as exc:
                result["warnings"].append("calendar_push_after_transcript_timeout")
                result["calendar_push_after_transcript"] = {"ok": False, "error": str(exc)}
            except Exception as exc:
                result["warnings"].append("calendar_push_after_transcript_failed")
                result["calendar_push_after_transcript"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

    result["elapsed_sec"] = round(time.monotonic() - started, 3)
    _write_latest(result, Path(args.json_out) if args.json_out else LATEST_PATH)
    return result


def run_refresh(args: argparse.Namespace) -> dict[str, Any]:
    from scripts.ops.background_task_locks import OSC_REFRESH_LOCK_NAME, acquire_lock, already_running_status

    lock_kind = "governance" if bool(getattr(args, "force_rebuild", False)) else "bounded"
    refresh_lock = acquire_lock(
        OSC_REFRESH_LOCK_NAME,
        owner="osc_events_refresh",
        kind=lock_kind,
        blocking=False,
    )
    if not refresh_lock.acquired:
        result = {
            **already_running_status(refresh_lock),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": bool(getattr(args, "dry_run", False)),
            "calendar_only": bool(getattr(args, "calendar_only", False)),
            "scan_only": bool(getattr(args, "scan_only", False)),
            "warnings": ["osc_refresh_already_running"],
            "message": "OSC/GCal/todo refresh is already running; this invocation was skipped to keep the canonical state path single-writer.",
        }
        _write_latest(result, Path(args.json_out) if getattr(args, "json_out", "") else LATEST_PATH)
        return result
    try:
        result = _run_refresh_locked(args)
        result["refresh_lock"] = refresh_lock.as_dict()
        _write_latest(result, Path(args.json_out) if getattr(args, "json_out", "") else LATEST_PATH)
        return result
    finally:
        refresh_lock.release()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh OSC todos and calendar-imported events.")
    parser.add_argument("--max-cases", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_MAX_CASES", "220")))
    parser.add_argument("--max-files-per-case", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_MAX_FILES_PER_CASE", "120")))
    parser.add_argument("--scan-time-budget-sec", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_SCAN_BUDGET_SEC", "1200")))
    parser.add_argument("--calendar-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_CALENDAR_LIMIT", "250")))
    parser.add_argument("--gcal-push-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_GCAL_PUSH_LIMIT", "120")))
    parser.add_argument("--lookback-days", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_LOOKBACK_DAYS", "30")))
    parser.add_argument("--lookahead-days", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_LOOKAHEAD_DAYS", "730")))
    parser.add_argument("--history-cutoff-date", default=os.environ.get(HISTORY_CUTOFF_ENV, DEFAULT_HISTORY_CUTOFF_DATE))
    parser.add_argument("--transcript-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_TRANSCRIPT_LIMIT", "120")))
    parser.add_argument("--transcript-tail-pages", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_TRANSCRIPT_TAIL_PAGES", "3")))
    parser.add_argument("--pdf-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_PDF_LIMIT", "240")))
    parser.add_argument("--pdf-max-pages", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_PDF_MAX_PAGES", "8")))
    parser.add_argument("--skip-drive-sync", action="store_true")
    parser.add_argument("--drive-sync-all-cases", action="store_true", default=os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_ALL_CASES", "0") == "1")
    parser.add_argument("--drive-sync-all-case-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_ALL_CASE_LIMIT", "32")))
    parser.add_argument("--drive-sync-priority-direct-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_PRIORITY_DIRECT_LIMIT", "24")))
    parser.add_argument("--drive-sync-priority-case-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_PRIORITY_CASE_LIMIT", "80")))
    parser.add_argument("--drive-sync-priority-days", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_PRIORITY_DAYS", "21")))
    parser.add_argument("--drive-sync-download-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_DOWNLOAD_LIMIT", "24")))
    parser.add_argument("--drive-sync-upload-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_UPLOAD_LIMIT", "0")))
    parser.add_argument("--drive-sync-max-download-bytes", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_MAX_DOWNLOAD_BYTES", "400000000")))
    parser.add_argument("--drive-sync-max-upload-bytes", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_MAX_UPLOAD_BYTES", "400000000")))
    parser.add_argument("--drive-sync-max-case-depth", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_MAX_CASE_DEPTH", "5")))
    parser.add_argument("--drive-sync-max-case-items", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_MAX_CASE_ITEMS", "220")))
    parser.add_argument("--drive-sync-create-folder-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_CREATE_FOLDER_LIMIT", "12")))
    parser.add_argument("--drive-sync-timeout-sec", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_DRIVE_SYNC_TIMEOUT_SEC", "900")))
    parser.add_argument("--skip-pdf-todos", action="store_true")
    parser.add_argument("--skip-transcript-todos", action="store_true")
    parser.add_argument("--skip-calendar-audit", action="store_true")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--calendar-only", action="store_true")
    parser.add_argument("--force-rebuild", action="store_true")
    parser.add_argument("--legacy-scan", action="store_true", default=os.environ.get("OSC_EVENTS_REFRESH_LEGACY_SCAN", "0") == "1")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_refresh(args)
    print(json.dumps(_json_safe(result), ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("ok"):
        return 0
    if "google_calendar_oauth_required" in (result.get("warnings") or []) and (result.get("scan") or {}).get("ok"):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
