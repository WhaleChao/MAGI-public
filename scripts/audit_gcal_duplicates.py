#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Audit duplicate Google Calendar events for MAGI.

Default behavior is dry-run. Deletions only happen with --apply.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


MAGI_ROOT = Path(__file__).resolve().parents[1]
OSC_ACTION_PATH = MAGI_ROOT / "skills" / "osc-orchestrator" / "action.py"
OSC_SKILL_DIR = MAGI_ROOT / "skills" / "osc-orchestrator"

if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))
if str(OSC_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(OSC_SKILL_DIR))

from osc_headless.gcal_dedup import (  # type: ignore
    build_dedup_key_from_gcal_event,
    confidence_for_match,
    is_invalid_case_key,
    normalize_case_key,
)


def _load_osc_action_module():
    spec = importlib.util.spec_from_file_location("osc_action_dedup_audit", OSC_ACTION_PATH)
    if not spec or not spec.loader:
        raise RuntimeError("failed_to_load_osc_action")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _score_confidence(level: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get((level or "").strip().lower(), 0)


def _event_start_repr(event: Dict[str, Any]) -> Tuple[str, str]:
    start = event.get("start") or {}
    if not isinstance(start, dict):
        return "", ""
    date_only = str(start.get("date") or "").strip()
    if date_only:
        return date_only, ""
    date_time = str(start.get("dateTime") or "").strip()
    if not date_time:
        return "", ""
    # Keep local-ish representation from payload string.
    d = date_time[:10] if len(date_time) >= 10 else date_time
    t = ""
    if "T" in date_time and len(date_time) >= 16:
        t = date_time.split("T", 1)[1][:5]
    return d, t


def _event_brief(event: Dict[str, Any], *, calendar_id: str, calendar_summary: str, dedup_key: str) -> Dict[str, Any]:
    date_key, time_key = _event_start_repr(event)
    case_key, case_source = normalize_case_key(event)
    return {
        "id": str(event.get("id") or ""),
        "calendar_id": calendar_id,
        "calendar_summary": calendar_summary,
        "summary": str(event.get("summary") or ""),
        "description": str(event.get("description") or ""),
        "date": date_key,
        "time": time_key,
        "case_key": case_key,
        "case_source": case_source,
        "dedup_key": dedup_key,
        "status": str(event.get("status") or ""),
        "updated": str(event.get("updated") or ""),
        "recurrence": event.get("recurrence"),
        "recurring_event_id": event.get("recurringEventId"),
    }


def _group_confidence(group_events: List[Dict[str, Any]]) -> Tuple[str, str]:
    if not group_events:
        return "low", "empty_group"
    calendars = {str(e.get("calendar_id") or "") for e in group_events}
    case_keys = [str(e.get("case_key") or "") for e in group_events]
    valid_cases = [k for k in case_keys if k and not is_invalid_case_key(k)]
    same_date = len({str(e.get("date") or "") for e in group_events}) == 1
    same_time = len({str(e.get("time") or "") for e in group_events}) == 1
    if len(calendars) == 1 and valid_cases and same_date and (same_time or all(not str(e.get("time") or "") for e in group_events)):
        return "high", "same_calendar_same_case_kind_date_time"
    if len(calendars) == 1 and same_date:
        return "medium", "same_calendar_same_date"
    return "low", "cross_calendar_or_weak_case_key"


def _eligible_for_delete(group: Dict[str, Any], min_conf: str) -> bool:
    if _score_confidence(group.get("confidence", "low")) < _score_confidence(min_conf):
        return False
    if not group.get("same_calendar", False):
        return False
    if not group.get("valid_case_key", False):
        return False
    return True


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")


def _write_summary_md(
    path: Path,
    *,
    scanned_calendars: List[Dict[str, str]],
    total_events: int,
    duplicate_groups: List[Dict[str, Any]],
    delete_candidates: List[Dict[str, Any]],
    deleted_count: int,
    dry_run: bool,
    time_min: str,
    time_max: str,
) -> None:
    high = sum(1 for g in duplicate_groups if g.get("confidence") == "high")
    medium = sum(1 for g in duplicate_groups if g.get("confidence") == "medium")
    low = sum(1 for g in duplicate_groups if g.get("confidence") == "low")
    lines: List[str] = []
    lines.append("# Google Calendar Duplicate Audit")
    lines.append("")
    lines.append(f"- generated_at: `{datetime.now().isoformat()}`")
    lines.append(f"- dry_run: `{str(dry_run).lower()}`")
    lines.append(f"- time_min: `{time_min}`")
    lines.append(f"- time_max: `{time_max}`")
    lines.append(f"- scanned_calendars: `{len(scanned_calendars)}`")
    lines.append(f"- total_events: `{total_events}`")
    lines.append(f"- duplicate_groups: `{len(duplicate_groups)}`")
    lines.append(f"- high_confidence: `{high}`")
    lines.append(f"- medium_confidence: `{medium}`")
    lines.append(f"- low_confidence: `{low}`")
    lines.append(f"- delete_candidates: `{len(delete_candidates)}`")
    lines.append(f"- deleted_count: `{deleted_count}`")
    lines.append("")
    lines.append("## Calendars")
    for c in scanned_calendars:
        lines.append(f"- `{c.get('id','')}` - {c.get('summary','')}")
    lines.append("")
    lines.append("## Top Duplicate Groups")
    for g in duplicate_groups[:20]:
        lines.append(
            f"- `{g.get('dedup_key','')}` | conf={g.get('confidence','low')} | "
            f"events={g.get('event_count',0)} | same_calendar={g.get('same_calendar',False)} | "
            f"case={g.get('case_key','') or 'unknown'}"
        )
    lines.append("")
    lines.append("## Delete Candidates")
    for g in delete_candidates[:20]:
        lines.append(
            f"- `{g.get('dedup_key','')}` | conf={g.get('confidence','low')} | "
            f"calendar={g.get('calendar_id','')} | duplicates={len(g.get('duplicate_event_ids',[]))}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit duplicate Google Calendar events for MAGI")
    parser.add_argument("--calendar-id", default="", help="Specific calendar id. Empty means all readable calendars.")
    parser.add_argument("--lookback-days", type=int, default=int(os.environ.get("MAGI_GCAL_DUP_AUDIT_LOOKBACK_DAYS", "730") or "730"))
    parser.add_argument("--lookahead-days", type=int, default=int(os.environ.get("MAGI_GCAL_DUP_AUDIT_LOOKAHEAD_DAYS", "365") or "365"))
    parser.add_argument("--limit-per-calendar", type=int, default=1000)
    parser.add_argument("--time-zone", default=os.environ.get("MAGI_TIME_ZONE", "Asia/Taipei"))
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("MAGI_GCAL_DUP_AUDIT_OUTPUT_DIR", str(MAGI_ROOT / "reports" / "gcal_dedup")),
    )
    parser.add_argument("--backup-path", default="", help="Optional override backup jsonl path.")
    parser.add_argument("--confidence", choices=["low", "medium", "high"], default=os.environ.get("MAGI_GCAL_DUP_AUDIT_MIN_CONFIDENCE", "high"))
    parser.add_argument("--apply", action="store_true", help="Delete duplicate events (safe filters still apply).")
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry-run mode.")
    args = parser.parse_args()

    dry_run = True
    if args.apply:
        dry_run = False
    if args.dry_run:
        dry_run = True

    osc_action = _load_osc_action_module()
    credentials_path = os.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH", "").strip() or str(MAGI_ROOT / "json" / "credentials.json")
    token_path = os.environ.get("MAGI_GOOGLE_CALENDAR_TOKEN_PATH", "").strip() or str(MAGI_ROOT / "json" / "google_calendar_token.json")
    svc = osc_action._build_google_calendar_service(credentials_path, token_path, interactive=False)
    if not svc.get("ok"):
        print(json.dumps({"ok": False, "error": svc.get("error", "gcal_service_failed")}, ensure_ascii=False))
        return 1
    service = svc.get("service")
    if not service:
        print(json.dumps({"ok": False, "error": "gcal_service_missing"}, ensure_ascii=False))
        return 1

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=max(1, args.lookback_days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=max(1, args.lookahead_days))).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.calendar_id:
        calendars = [{"id": args.calendar_id, "summary": args.calendar_id}]
    else:
        try:
            cal_items = service.calendarList().list(minAccessRole="reader").execute().get("items", [])
            calendars = [{"id": str(c.get("id") or ""), "summary": str(c.get("summary") or "")} for c in cal_items if c.get("id")]
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"calendar_list_failed:{type(e).__name__}:{e}"}, ensure_ascii=False))
            return 1
    if not calendars:
        calendars = [{"id": "primary", "summary": "primary"}]

    # Key by (calendar_id, dedup_key) so genuine intra-calendar duplicates aren't
    # merged with cross-calendar shared events (which produce the same dedup_key
    # across whalelawyer / zl.hualien but are not real duplicates to delete).
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    total_events = 0
    page_size = max(50, min(args.limit_per_calendar, 2500))
    for cal in calendars:
        cid = cal["id"]
        csum = cal["summary"]
        items: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        try:
            while True:
                res = service.events().list(
                    calendarId=cid,
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=page_size,
                    singleEvents=True,
                    orderBy="startTime",
                    pageToken=page_token,
                ).execute()
                items.extend(res.get("items", []) or [])
                page_token = res.get("nextPageToken")
                if not page_token or len(items) >= args.limit_per_calendar:
                    break
        except Exception:
            pass
        total_events += len(items)
        for ev in items:
            if not isinstance(ev, dict):
                continue
            key = build_dedup_key_from_gcal_event(ev, tz=args.time_zone)
            row = _event_brief(ev, calendar_id=cid, calendar_summary=csum, dedup_key=key)
            groups.setdefault((cid, key), []).append(row)

    duplicate_groups: List[Dict[str, Any]] = []
    delete_candidates: List[Dict[str, Any]] = []
    backup_rows: List[Dict[str, Any]] = []
    deleted_count = 0
    delete_errors: List[str] = []

    for (_group_cal_id, dedup_key), rows in groups.items():
        if len(rows) <= 1:
            continue
        rows_sorted = sorted(rows, key=lambda r: (r.get("date", ""), r.get("time", ""), r.get("updated", ""), r.get("id", "")))
        canonical = rows_sorted[0]
        others = rows_sorted[1:]
        conf, reason = _group_confidence(rows_sorted)
        case_key = str(canonical.get("case_key") or "")
        same_calendar = len({str(r.get("calendar_id") or "") for r in rows_sorted}) == 1
        valid_case_key = bool(case_key and not is_invalid_case_key(case_key))

        # Strengthen confidence with pairwise comparison.
        pair_conf = "high"
        for r in others:
            c = confidence_for_match(canonical, r)
            if _score_confidence(c) < _score_confidence(pair_conf):
                pair_conf = c
        if _score_confidence(pair_conf) < _score_confidence(conf):
            conf = pair_conf

        record = {
            "dedup_key": dedup_key,
            "confidence": conf,
            "reason": reason,
            "same_calendar": same_calendar,
            "valid_case_key": valid_case_key,
            "case_key": case_key,
            "calendar_id": canonical.get("calendar_id"),
            "canonical_event_id": canonical.get("id"),
            "duplicate_event_ids": [r.get("id") for r in others if r.get("id")],
            "event_count": len(rows_sorted),
            "events": rows_sorted,
        }
        duplicate_groups.append(record)

        if _eligible_for_delete(record, args.confidence):
            delete_candidates.append(record)
            for r in others:
                backup_rows.append(r)

    # Prepare output paths.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.output_dir) / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    backup_path = Path(args.backup_path) if args.backup_path else (run_dir / "events_backup.jsonl")

    _write_json(run_dir / "duplicates.json", duplicate_groups)
    _write_jsonl(backup_path, backup_rows)

    if not dry_run:
        for g in delete_candidates:
            cid = str(g.get("calendar_id") or "")
            canonical_id = str(g.get("canonical_event_id") or "")
            for ev in g.get("events", [])[1:]:
                eid = str(ev.get("id") or "")
                if not cid or not eid or eid == canonical_id:
                    continue
                # Skip recurring masters. (Instances are allowed; they have recurring_event_id.)
                if ev.get("recurrence"):
                    continue
                try:
                    service.events().delete(calendarId=cid, eventId=eid).execute()
                    deleted_count += 1
                except Exception as e:
                    delete_errors.append(f"{cid}:{eid}:{type(e).__name__}:{e}")

    _write_summary_md(
        run_dir / "summary.md",
        scanned_calendars=calendars,
        total_events=total_events,
        duplicate_groups=sorted(duplicate_groups, key=lambda g: (_score_confidence(g.get("confidence", "low")), g.get("event_count", 0)), reverse=True),
        delete_candidates=delete_candidates,
        deleted_count=deleted_count,
        dry_run=dry_run,
        time_min=time_min,
        time_max=time_max,
    )

    latest_summary = Path(args.output_dir) / "latest_summary.md"
    latest_summary.parent.mkdir(parents=True, exist_ok=True)
    latest_summary.write_text((run_dir / "summary.md").read_text(encoding="utf-8"), encoding="utf-8")

    out = {
        "ok": True,
        "dry_run": dry_run,
        "time_min": time_min,
        "time_max": time_max,
        "scanned_calendars": len(calendars),
        "total_events": total_events,
        "duplicate_groups": len(duplicate_groups),
        "delete_candidates": len(delete_candidates),
        "deleted_count": deleted_count,
        "delete_errors": delete_errors[:20],
        "report_dir": str(run_dir),
        "summary_path": str(run_dir / "summary.md"),
        "duplicates_path": str(run_dir / "duplicates.json"),
        "backup_path": str(backup_path),
        "latest_summary_path": str(latest_summary),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

