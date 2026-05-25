#!/usr/bin/env python3
"""Backfill MAGI share links into PDF-created OSC todos.

PDF-derived todos must keep a clickable MAGI share URL in the description so
Google Calendar users can verify the source document.  This repair is bounded
and conservative: it only updates future/pending PDF-source todos that do not
already contain a MAGI share link or share-status marker.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SKILL_DIR = ROOT / "skills" / "osc-orchestrator"
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from api.blueprints.osc_pdf import _append_calendar_source_reference, _create_calendar_share_link  # noqa: E402
from api.osc.utils import _osc_exec, _osc_resolve_existing_local_path  # noqa: E402


def _source_pdf_name(source_file: str) -> str:
    raw = str(source_file or "").strip()
    if "#" in raw:
        raw = raw.split("#", 1)[0]
    return Path(raw).name if raw else ""


def _first_existing(candidates: list[str]) -> Path | None:
    for value in candidates:
        if not value:
            continue
        resolved = _osc_resolve_existing_local_path(value, prefer_dir=False)
        if resolved:
            return Path(resolved)
    return None


def _find_under(folder: Path, filename: str, *, timeout_sec: float = 6.0) -> Path | None:
    if not filename or not folder.exists() or not folder.is_dir():
        return None
    started = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in {"node_modules", "__pycache__"}]
        if time.monotonic() - started > timeout_sec:
            return None
        if filename in filenames:
            return Path(dirpath) / filename
    return None


def _resolve_todo_pdf(row: dict[str, Any]) -> Path | None:
    source_file = str(row.get("source_file") or "").strip()
    filename = _source_pdf_name(source_file)
    if not filename:
        return None

    candidates = [source_file.split("#", 1)[0]]
    doc_rows, _ = _osc_exec(
        """
        SELECT file_path
        FROM document_index
        WHERE (case_number=%s OR %s='')
          AND (file_name=%s OR file_path LIKE %s)
        ORDER BY modified_date DESC, id DESC
        LIMIT 10
        """,
        (row.get("case_number") or "", row.get("case_number") or "", filename, f"%{filename}"),
        fetch="all",
    )
    for doc in doc_rows or []:
        candidates.append(str((doc or {}).get("file_path") or ""))

    found = _first_existing(candidates)
    if found:
        return found

    case_rows, _ = _osc_exec(
        """
        SELECT folder_path
        FROM cases
        WHERE case_number=%s
        ORDER BY updated_at DESC, id DESC
        LIMIT 3
        """,
        (row.get("case_number") or "",),
        fetch="all",
    )
    for case in case_rows or []:
        folder = _osc_resolve_existing_local_path(str((case or {}).get("folder_path") or ""), prefer_dir=True)
        if not folder:
            continue
        found = _find_under(Path(folder), filename)
        if found:
            return found
    return None


def _candidate_rows(limit: int, case_number: str = "") -> list[dict[str, Any]]:
    where_case = "AND case_number=%s" if case_number else ""
    params: list[Any] = []
    if case_number:
        params.append(case_number)
    params.append(limit)
    rows, _ = _osc_exec(
        f"""
        SELECT id, case_number, client_name, todo_type, todo_date, todo_time, description, source_file, google_calendar_id
        FROM case_todos
        WHERE todo_date >= CURDATE()
          AND (status IS NULL OR status='' OR status='pending')
          AND COALESCE(source_file,'') LIKE '%%.pdf%%'
          AND COALESCE(description,'') NOT LIKE '%%MAGI分享連結：%%'
          AND COALESCE(description,'') NOT LIKE '%%MAGI分享狀態：%%'
          {where_case}
        ORDER BY todo_date ASC, id ASC
        LIMIT %s
        """,
        tuple(params),
        fetch="all",
    )
    return [dict(r) for r in rows or []]


def backfill(*, limit: int, case_number: str = "", execute: bool = False) -> dict[str, Any]:
    rows = _candidate_rows(limit, case_number=case_number)
    out: dict[str, Any] = {"ok": True, "execute": execute, "scanned": len(rows), "updated": 0, "unresolved": 0, "items": []}
    for row in rows:
        pdf_path = _resolve_todo_pdf(row)
        item = {
            "id": row.get("id"),
            "case_number": row.get("case_number"),
            "todo_date": str(row.get("todo_date") or ""),
            "source_file": row.get("source_file"),
            "resolved_path": str(pdf_path or ""),
            "status": "resolved" if pdf_path else "unresolved",
        }
        if not pdf_path:
            out["unresolved"] += 1
            out["items"].append(item)
            continue

        share = _create_calendar_share_link(pdf_path) if execute else {"ok": True, "url": "dry-run"}
        new_desc = _append_calendar_source_reference(str(row.get("description") or ""), source_path=pdf_path, share=share)
        item["share_ok"] = bool(share.get("ok"))
        item["share_error"] = share.get("error") or ""
        if execute:
            _osc_exec("UPDATE case_todos SET description=%s WHERE id=%s", (new_desc, row.get("id")), fetch="none")
            out["updated"] += 1
            item["status"] = "updated"
        out["items"].append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill MAGI share links into future PDF-created OSC todos.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--case-number", default="")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    result = backfill(limit=max(1, args.limit), case_number=args.case_number.strip(), execute=bool(args.execute))
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
