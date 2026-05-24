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
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
LATEST_PATH = ROOT / ".runtime" / "osc_events_refresh_latest.json"


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


def _run_pdf_calendar_scan(args: argparse.Namespace) -> dict[str, Any]:
    """Scan court PDFs with the same extractor used by the OSC web PDF tool."""
    from api.blueprints import osc_pdf

    limit = max(1, int(getattr(args, "pdf_limit", 240)))
    max_pages = max(1, min(int(getattr(args, "pdf_max_pages", 8)), 20))
    dry_run = bool(getattr(args, "dry_run", False))
    started = time.monotonic()
    scanned = inserted = updated = skipped = todo_count = event_count = warning_count = 0
    sample_items: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        targets = osc_pdf._iter_all_case_pdf_targets(limit=limit)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            "limit": limit,
            "max_pages": max_pages,
        }

    for path, case_number, client_name in targets:
        try:
            item = osc_pdf._scan_pdf_for_calendar(
                path,
                case_number=case_number,
                client_name=client_name,
                max_pages=max_pages,
                include_share_link=False,
            )
            scanned += 1
            todos = item.get("todos") or []
            events = item.get("events") or []
            todo_count += len(todos)
            event_count += len(events)
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
                        "event_count": len(events),
                        "write_result": write_result,
                        "todos": todos[:3],
                    }
                )
        except Exception as exc:
            if len(errors) < 20:
                errors.append(f"{path.name}: {type(exc).__name__}: {str(exc)[:200]}")

    return {
        "ok": not errors,
        "dry_run": dry_run,
        "limit": limit,
        "max_pages": max_pages,
        "targets": len(targets),
        "scanned": scanned,
        "todo_count": todo_count,
        "event_count": event_count,
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
        "warnings": [],
    }

    if not args.calendar_only:
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

        if not getattr(args, "skip_pdf_todos", False):
            try:
                result["pdf_calendar_scan"] = _run_pdf_calendar_scan(args)
                if not result["pdf_calendar_scan"].get("ok"):
                    result["ok"] = False
            except Exception as exc:
                result["ok"] = False
                result["pdf_calendar_scan"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

        if not getattr(args, "skip_transcript_todos", False):
            try:
                transcript_mod = _load_transcript_todo_module()
                transcript_limit = max(1, int(getattr(args, "transcript_limit", 120)))
                transcript_tail_pages = max(1, int(getattr(args, "transcript_tail_pages", 3)))
                paths = transcript_mod._iter_pdf_targets("", limit=transcript_limit)
                scan = transcript_mod.scan_targets(paths, tail_pages=transcript_tail_pages)
                if bool(getattr(args, "dry_run", False)):
                    write = {"dry_run": True, "inserted": 0, "updated": 0, "skipped": 0, "past_skipped": 0}
                else:
                    write = transcript_mod.apply_high_confidence(scan.get("items") or [])
                result["transcript_todos"] = {
                    "ok": True,
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
    parser.add_argument("--lookahead-days", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_LOOKAHEAD_DAYS", "180")))
    parser.add_argument("--transcript-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_TRANSCRIPT_LIMIT", "120")))
    parser.add_argument("--transcript-tail-pages", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_TRANSCRIPT_TAIL_PAGES", "3")))
    parser.add_argument("--pdf-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_PDF_LIMIT", "240")))
    parser.add_argument("--pdf-max-pages", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_PDF_MAX_PAGES", "8")))
    parser.add_argument("--skip-pdf-todos", action="store_true")
    parser.add_argument("--skip-transcript-todos", action="store_true")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--calendar-only", action="store_true")
    parser.add_argument("--force-rebuild", action="store_true")
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
