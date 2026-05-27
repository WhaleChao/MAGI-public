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
import signal
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
LATEST_PATH = ROOT / ".runtime" / "osc_events_refresh_latest.json"
PDF_SCAN_CACHE_PATH = ROOT / ".runtime" / "pdf_calendar_scan_cache.json"
PDF_SCAN_CURSOR_PATH = ROOT / ".runtime" / "pdf_calendar_scan_cursor.json"
PDF_SCAN_RULE_VERSION = os.environ.get("OSC_PDF_CALENDAR_RULE_VERSION", "2026-05-27-original-osc-rules")


class _PdfScanTimeout(TimeoutError):
    pass


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
) -> tuple[list[dict[str, Any]], int, int]:
    """Keep only actionable PDF todos before writing them into OSC/Google."""
    today = today or datetime.now().date()
    latest = today + timedelta(days=max_future_days)
    active: list[dict[str, Any]] = []
    past_skipped = 0
    implausible_skipped = 0
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
        active.append(todo)
    return active, past_skipped, implausible_skipped


def _run_pdf_calendar_scan(args: argparse.Namespace) -> dict[str, Any]:
    """Scan court PDFs with the same extractor used by the OSC web PDF tool."""
    from api.blueprints import osc_pdf

    limit = max(1, int(getattr(args, "pdf_limit", 240)))
    target_limit_env = int(os.environ.get("OSC_EVENTS_REFRESH_PDF_CANDIDATE_LIMIT", "0") or "0")
    target_limit = max(limit, min(target_limit_env, 5000)) if target_limit_env > 0 else limit
    max_pages = max(1, min(int(getattr(args, "pdf_max_pages", 8)), 20))
    dry_run = bool(getattr(args, "dry_run", False))
    scan_text = os.environ.get("OSC_PDF_CALENDAR_BULK_TEXT_ENABLE", "1").strip().lower() in {"1", "true", "yes", "on"}
    text_when_filename = os.environ.get("OSC_PDF_CALENDAR_BULK_TEXT_WHEN_FILENAME", "1").strip().lower() in {"1", "true", "yes", "on"}
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
    past_todo_count = 0
    implausible_todo_count = 0
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
    case_batch = max(1, min(500, int(os.environ.get("OSC_EVENTS_REFRESH_PDF_CASE_BATCH", "40") or "40")))
    env_case_offset = os.environ.get("OSC_EVENTS_REFRESH_PDF_CASE_OFFSET")
    if env_case_offset is not None and str(env_case_offset).strip() != "":
        case_offset = max(0, int(env_case_offset or "0"))
    elif bool(getattr(args, "force_rebuild", False)):
        case_offset = 0
    else:
        case_offset = max(0, int(scan_cursor.get("case_offset") or 0))
    if total_case_rows and case_offset >= total_case_rows:
        case_offset = 0

    target_timeout_sec = max(5, min(max(10, budget_sec or 10), int(os.environ.get("OSC_PDF_CALENDAR_TARGET_TIMEOUT_SEC", "45") or "45")))
    try:
        with _pdf_scan_time_limit(target_timeout_sec):
            try:
                targets = osc_pdf._iter_all_case_pdf_targets(limit=target_limit, case_offset=case_offset, case_batch=case_batch)
            except TypeError:
                # Unit tests and older private plugins may monkeypatch the old
                # one-argument helper; keep dry-run verification compatible.
                targets = osc_pdf._iter_all_case_pdf_targets(limit=target_limit)
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
        }

    for path, case_number, client_name in targets:
        try:
            if scanned >= limit:
                break
            if budget_sec and time.monotonic() - started > budget_sec:
                errors.append(f"budget_exhausted:{budget_sec}s")
                break
            signature = _file_signature(path)
            cache_key = signature[0] if signature else ""
            if signature and no_todo_cache_days:
                cached = cache_files.get(cache_key) if isinstance(cache_files, dict) else None
                if isinstance(cached, dict):
                    same_file = int(cached.get("mtime") or 0) == signature[1] and int(cached.get("size") or -1) == signature[2]
                    same_rule = str(cached.get("rule_version") or "") == PDF_SCAN_RULE_VERSION
                    scanned_at = str(cached.get("scanned_at") or "")
                    try:
                        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(scanned_at)).total_seconds() / 86400
                    except Exception:
                        age_days = no_todo_cache_days + 1
                    if same_file and same_rule and int(cached.get("todo_count") or 0) == 0 and age_days < no_todo_cache_days:
                        cache_skipped += 1
                        continue
            with _pdf_scan_time_limit(file_timeout_sec):
                item = osc_pdf._scan_pdf_for_calendar(
                    path,
                    case_number=case_number,
                    client_name=client_name,
                    max_pages=max_pages,
                    include_share_link=not dry_run,
                    scan_text=scan_text,
                    text_when_filename=text_when_filename,
                )
            scanned += 1
            raw_todos = item.get("todos") or []
            todos, past_skipped, implausible_skipped = _active_pdf_todos(raw_todos)
            past_todo_count += past_skipped
            implausible_todo_count += implausible_skipped
            todo_count += len(todos)
            event_count += len(todos)
            if signature and isinstance(cache_files, dict):
                cache_files[cache_key] = {
                    "mtime": signature[1],
                    "size": signature[2],
                    "todo_count": len(todos),
                    "rule_version": PDF_SCAN_RULE_VERSION,
                    "text_available": bool(item.get("text_available")),
                    "text_error": str(item.get("text_error") or "")[:200],
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
                    source_file=path.name,
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

    next_case_offset = (case_offset + case_batch) if total_case_rows else case_offset
    if total_case_rows and next_case_offset >= total_case_rows:
        next_case_offset = 0
    if not dry_run:
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
        "candidate_limit": target_limit,
        "max_pages": max_pages,
        "scan_text": scan_text,
        "text_when_filename": text_when_filename,
        "file_timeout_sec": file_timeout_sec,
        "budget_sec": budget_sec,
        "target_timeout_sec": target_timeout_sec,
        "case_offset": case_offset,
        "case_batch": case_batch,
        "next_case_offset": next_case_offset,
        "total_case_rows": total_case_rows,
        "no_todo_cache_days": no_todo_cache_days,
        "rule_version": PDF_SCAN_RULE_VERSION,
        "targets": len(targets),
        "scanned": scanned,
        "cache_skipped": cache_skipped,
        "todo_count": todo_count,
        "event_count": event_count,
        "past_todo_count": past_todo_count,
        "implausible_todo_count": implausible_todo_count,
        "timeout_count": timeout_count,
        "error_count": error_count,
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


def run_refresh(args: argparse.Namespace) -> dict[str, Any]:
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
        "scan": {},
        "pdf_calendar_scan": {},
        "transcript_todos": {},
        "calendar_import": {},
        "calendar_push": {},
        "calendar_audit": {},
        "warnings": [],
    }

    def _remaining_scan_budget_sec() -> int | None:
        total = int(getattr(args, "scan_time_budget_sec", 0) or 0)
        if total <= 0:
            return None
        return max(0, total - int(time.monotonic() - started))

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
            try:
                remaining = _remaining_scan_budget_sec()
                if remaining is not None and remaining <= 0:
                    result["transcript_todos"] = {
                        "ok": True,
                        "skipped": True,
                        "reason": f"transcript_todo_budget_exhausted:{args.scan_time_budget_sec}s",
                    }
                else:
                    env_timeout = int(os.environ.get("OSC_TRANSCRIPT_TODO_TIMEOUT_SEC", "0") or "0")
                    transcript_timeout = env_timeout if env_timeout > 0 else 120
                    min_dry_run_budget = max(1, int(os.environ.get("OSC_TRANSCRIPT_TODO_MIN_DRY_RUN_BUDGET_SEC", "120") or "120"))
                    if (
                        bool(getattr(args, "dry_run", False))
                        and remaining is not None
                        and remaining < min_dry_run_budget
                    ):
                        result["transcript_todos"] = {
                            "ok": True,
                            "skipped": True,
                            "reason": "transcript_todo_dry_run_budget_too_small",
                            "remaining_sec": remaining,
                            "required_sec": min_dry_run_budget,
                        }
                    else:
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
                        result["transcript_todos"] = {
                            "ok": True,
                            "timeout_sec": transcript_timeout,
                            "scanned": scan.get("scanned", 0),
                            "high_count": scan.get("high_count", 0),
                            "review_count": scan.get("review_count", 0),
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
                result["transcript_todos"] = {"ok": False, "skipped": True, "error": str(exc)}
            except Exception as exc:
                result["ok"] = False
                result["transcript_todos"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

    if not args.scan_only:
        if bool(getattr(args, "dry_run", False)):
            result["calendar_import"] = {"ok": True, "dry_run": True, "skipped": True}
            result["calendar_push"] = {"ok": True, "dry_run": True, "skipped": True}
        else:
            try:
                calendar_payload = {
                    "lookback_days": args.lookback_days,
                    "lookahead_days": args.lookahead_days,
                    "limit": args.calendar_limit,
                    "incremental": True,
                }
                cal = mod.task_gcal_import(calendar_payload)
                result["calendar_import"] = cal
                if not cal.get("ok") and cal.get("need_interactive_oauth"):
                    result["warnings"].append("google_calendar_oauth_required")
                elif not cal.get("ok"):
                    result["ok"] = False
            except Exception as exc:
                result["ok"] = False
                result["calendar_import"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

            try:
                push_payload = {
                    "limit": args.gcal_push_limit,
                    "repair_existing": True,
                    "repair_limit": args.gcal_push_limit,
                    "retry_max_attempts": 3,
                }
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
            except Exception as exc:
                result["ok"] = False
                result["calendar_push"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

            if not getattr(args, "skip_calendar_audit", False):
                try:
                    audit = mod.task_gcal_integrity_audit({"limit": args.gcal_push_limit})
                    result["calendar_audit"] = audit
                    if not audit.get("ok"):
                        if audit.get("need_interactive_oauth"):
                            result["warnings"].append("google_calendar_oauth_required")
                        else:
                            result["ok"] = False
                            result["warnings"].append("google_calendar_integrity_needs_attention")
                except Exception as exc:
                    result["ok"] = False
                    result["calendar_audit"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

    result["elapsed_sec"] = round(time.monotonic() - started, 3)
    _write_latest(result, Path(args.json_out) if args.json_out else LATEST_PATH)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh OSC todos and calendar-imported events.")
    parser.add_argument("--max-cases", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_MAX_CASES", "220")))
    parser.add_argument("--max-files-per-case", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_MAX_FILES_PER_CASE", "120")))
    parser.add_argument("--scan-time-budget-sec", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_SCAN_BUDGET_SEC", "1200")))
    parser.add_argument("--calendar-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_CALENDAR_LIMIT", "250")))
    parser.add_argument("--gcal-push-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_GCAL_PUSH_LIMIT", "120")))
    parser.add_argument("--lookback-days", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_LOOKBACK_DAYS", "30")))
    parser.add_argument("--lookahead-days", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_LOOKAHEAD_DAYS", "730")))
    parser.add_argument("--transcript-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_TRANSCRIPT_LIMIT", "120")))
    parser.add_argument("--transcript-tail-pages", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_TRANSCRIPT_TAIL_PAGES", "3")))
    parser.add_argument("--pdf-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_PDF_LIMIT", "240")))
    parser.add_argument("--pdf-max-pages", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_PDF_MAX_PAGES", "8")))
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
