#!/usr/bin/env python3
"""Read-only source-document to OSC calendar completeness audit.

This audit starts at the court/procedure PDFs, not at rows already present in
Google Calendar.  That direction matters: a missing parser run otherwise has
no database row for the ordinary calendar integrity audit to inspect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_HEARING_TYPES = {
    "開庭",
    "準備程序",
    "言詞辯論",
    "調解",
    "審理",
    "審理程序",
    "審判程序",
    "宣判",
    "訊問",
    "調查",
}
_DONE_STATUSES = {"completed", "done", "已完成", "deleted", "已刪除"}


def _text_date(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value or "").strip()[:10]


def _text_time(value: Any) -> str:
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds()) % 86400
        return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"
    raw = str(value or "").strip()
    return raw[:5] if raw else ""


def _event_type(value: Any) -> str:
    raw = str(value or "").strip()
    return "庭期" if raw in _HEARING_TYPES else raw


def _event_key(
    case_number: Any,
    todo_type: Any,
    todo_date: Any,
    todo_time: Any,
) -> tuple[str, str, str, str]:
    return (
        str(case_number or "").strip(),
        _event_type(todo_type),
        _text_date(todo_date),
        _text_time(todo_time),
    )


def _is_osc_source(row: dict[str, Any]) -> bool:
    source = str(row.get("source_file") or "").strip().lower()
    todo_type = str(row.get("todo_type") or "").strip()
    return not source.startswith(("gcal_import", "gcal_mirror:")) and todo_type != "行事曆事件"


def _has_google_event_id(row: dict[str, Any]) -> bool:
    # The legacy column name stores the Google *event* id.  Newer schemas may
    # also expose google_calendar_event_id.
    return bool(
        str(
            row.get("google_calendar_event_id")
            or row.get("google_calendar_id")
            or ""
        ).strip()
    )


def _final_document_distribution(
    targets: dict[tuple[str, str], tuple[Path, str, str]],
) -> dict[str, dict[str, int]]:
    from api.osc.case_folder_schema import is_judgment_folder_segment

    categories = ("一般案件", "法扶案件", "指定辯護案件", "無償案件")
    case_types = ("刑事", "民事", "行政", "消費者債務清理", "非訟", "法律顧問")
    result: dict[str, dict[str, int]] = {
        "case_category": {},
        "case_type": {},
        "folder_name": {},
    }
    for path, _case_number, _client_name in targets.values():
        parts = [part for part in Path(path).parts[:-1] if part]
        final_parts = [part for part in parts if is_judgment_folder_segment(part)]
        if not final_parts and not is_judgment_folder_segment(Path(path).name):
            continue
        category = next((item for item in categories if item in parts), "其他")
        case_type = next((item for item in case_types if item in parts), "其他")
        folder_name = (
            final_parts[-1]
            if final_parts
            else "（依檔名識別，資料夾非標準）"
        )
        for bucket, value in (
            ("case_category", category),
            ("case_type", case_type),
            ("folder_name", folder_name),
        ):
            result[bucket][value] = result[bucket].get(value, 0) + 1
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def build_report(
    *,
    target_limit: int = 50000,
    case_batch: int = 10000,
) -> dict[str, Any]:
    from api.blueprints import osc_pdf
    from api.osc.case_folder_schema import path_has_judgment_folder
    from scripts.ops.osc_events_refresh import _active_pdf_todos

    started = time.monotonic()
    raw_targets = osc_pdf._iter_all_case_pdf_targets(
        limit=target_limit,
        case_offset=0,
        case_batch=case_batch,
        filename_only=True,
    )
    # The indexed fast path and the NAS tree may name the same file twice.
    targets: dict[tuple[str, str], tuple[Path, str, str]] = {}
    for path, case_number, client_name in raw_targets:
        key = (str(case_number or "").strip(), Path(path).name)
        targets.setdefault(key, (Path(path), str(case_number or ""), str(client_name or "")))

    candidates: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    final_document_target_keys = {
        key
        for key, (path, _case_number, _client_name) in targets.items()
        if path_has_judgment_folder(str(path))
    }
    scan_errors: list[dict[str, str]] = []
    quarantined: list[dict[str, Any]] = []
    past_count = 0
    implausible_count = 0
    for path, case_number, client_name in targets.values():
        try:
            item = osc_pdf._scan_pdf_for_calendar(
                path,
                case_number=case_number,
                client_name=client_name,
                include_share_link=False,
                scan_text=False,
            )
        except Exception as exc:
            scan_errors.append(
                {
                    "case_number": case_number,
                    "file_name": path.name,
                    "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                }
            )
            continue
        raw_todos = item.get("todos") or []
        for todo in raw_todos:
            if isinstance(todo, dict):
                todo.setdefault("source_file", str(path))
                todo.setdefault("case_number", case_number)
                todo.setdefault("client_name", client_name)
        active, past, implausible, isolated = _active_pdf_todos(
            raw_todos,
            include_diagnostics=True,
        )
        past_count += past
        implausible_count += implausible
        quarantined.extend(isolated)
        for todo in active:
            key = _event_key(
                case_number,
                todo.get("type") or todo.get("todo_type"),
                todo.get("date"),
                todo.get("time"),
            )
            if not key[0] or not key[2]:
                continue
            record = candidates.setdefault(
                key,
                {
                    "case_number": key[0],
                    "client_name": client_name,
                    "todo_type": str(todo.get("type") or todo.get("todo_type") or ""),
                    "todo_date": key[2],
                    "todo_time": key[3],
                    "description": str(todo.get("description") or "").strip(),
                    "source_files": [],
                },
            )
            source = str(path)
            if source not in record["source_files"]:
                record["source_files"].append(source)
            if path_has_judgment_folder(source):
                record["from_final_document"] = True

    rows, _ = osc_pdf._osc_exec(
        """
        SELECT id, case_number, client_name, todo_type, todo_date, todo_time,
               description, source_file, status, google_calendar_id,
               google_calendar_event_id
        FROM case_todos
        WHERE todo_date >= CURDATE()
          AND (status IS NULL OR status='' OR LOWER(status) NOT IN
               ('completed','done','已完成','deleted','已刪除'))
        ORDER BY todo_date, todo_time, id
        """,
        fetch="all",
    )
    existing: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().lower() in _DONE_STATUSES:
            continue
        key = _event_key(
            row.get("case_number"),
            row.get("todo_type"),
            row.get("todo_date"),
            row.get("todo_time"),
        )
        existing.setdefault(key, []).append(row)

    gaps: list[dict[str, Any]] = []
    unsynced: list[dict[str, Any]] = []
    for key, candidate in sorted(candidates.items()):
        matches = existing.get(key, [])
        osc_matches = [row for row in matches if _is_osc_source(row)]
        if not osc_matches:
            gaps.append(
                {
                    **candidate,
                    "matching_calendar_import_rows": len(matches),
                    "reason": "source_pdf_has_no_active_osc_todo",
                }
            )
            continue
        # A human/shared-calendar event can legitimately predate the PDF.  In
        # that case the PDF row is retained as provenance but must not create a
        # duplicate event on the token owner's ``primary`` calendar.
        if not any(_has_google_event_id(row) for row in matches):
            unsynced.append(
                {
                    **candidate,
                    "osc_todo_ids": [int(row.get("id") or 0) for row in osc_matches],
                    "reason": "osc_todo_has_no_google_event_id",
                }
            )

    return {
        "ok": not scan_errors and not gaps and not unsynced,
        "read_only": True,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "raw_target_count": len(raw_targets),
        "unique_target_count": len(targets),
        "future_candidate_count": len(candidates),
        "final_document_target_count": len(final_document_target_keys),
        "final_document_distribution": _final_document_distribution(targets),
        "final_document_future_candidate_count": sum(
            1 for item in candidates.values() if item.get("from_final_document")
        ),
        "past_candidate_count": past_count,
        "implausible_or_quarantined_count": implausible_count,
        "scan_error_count": len(scan_errors),
        "source_gap_count": len(gaps),
        "final_document_source_gap_count": sum(
            1 for item in gaps if item.get("from_final_document")
        ),
        "google_unsynced_count": len(unsynced),
        "final_document_google_unsynced_count": sum(
            1 for item in unsynced if item.get("from_final_document")
        ),
        "scan_errors": scan_errors,
        "source_gaps": gaps,
        "google_unsynced": unsynced,
        "quarantined": quarantined[:50],
        "elapsed_sec": round(time.monotonic() - started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-limit", type=int, default=50000)
    parser.add_argument("--case-batch", type=int, default=10000)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument(
        "--allow-gaps",
        action="store_true",
        help="Return success for an audit with gaps; scan/database errors still fail.",
    )
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        from api.runtime_paths import get_env_file

        load_dotenv(get_env_file(), override=False)
    except Exception:
        pass

    report = build_report(
        target_limit=max(1, min(args.target_limit, 50000)),
        case_batch=max(1, min(args.case_batch, 10000)),
    )
    if args.json_out:
        _write_json(args.json_out.expanduser(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report["scan_error_count"]:
        return 2
    if not args.allow_gaps and (
        report["source_gap_count"] or report["google_unsynced_count"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
