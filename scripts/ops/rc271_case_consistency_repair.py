#!/usr/bin/env python3
"""One-time, evidence-producing repair for rc271 case/calendar consistency.

The script defaults to audit-only.  ``--apply`` performs only predeclared,
content-verified repairs and writes before/after evidence.  It never deletes a
case, calendar event or source document.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_live_environment() -> None:
    for path in (
        Path.home() / "Library/LaunchAgents/com.magi.v3.supervisor.plist",
        Path.home() / "Library/LaunchAgents/com.magi.v3.control.plist",
    ):
        if not path.is_file():
            continue
        data = plistlib.loads(path.read_bytes())
        for key, value in (data.get("EnvironmentVariables") or {}).items():
            os.environ.setdefault(str(key), str(value))
    env_path = Path(os.environ.get("MAGI_ENV_FILE", "")).expanduser()
    expected = str(os.environ.get("MAGI_ENV_FILE_SHA256") or "").strip().lower()
    if env_path.is_file() and expected and _sha256(env_path) == expected:
        from dotenv import dotenv_values

        for key, value in dotenv_values(env_path, encoding="utf-8", interpolate=True).items():
            if key and value is not None:
                os.environ.setdefault(str(key), str(value))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _query_cases() -> list[dict[str, Any]]:
    from api.osc.utils import _osc_exec

    rows: list[dict[str, Any]] = []
    for case_number in ("2026-0021", "2026-0071", "2025-0034", "2025-0079", "2026-0080"):
        row, _ = _osc_exec(
            """
            SELECT id, case_number, client_name, case_category, case_type,
                   case_stage, case_reason, court_case_number, status, folder_path
              FROM cases WHERE case_number=%s LIMIT 1
            """,
            (case_number,),
            fetch="one",
        )
        if row:
            rows.append(dict(row))
    return rows


def _query_closed_case_future_transcript_todos() -> list[dict[str, Any]]:
    from api.osc.utils import _osc_exec

    rows, _ = _osc_exec(
        """
        SELECT t.id, t.case_number, t.client_name, t.todo_type, t.todo_date,
               t.todo_time, t.source_file, t.google_calendar_id,
               c.status AS case_status, c.folder_path, c.court_case_number
          FROM case_todos t
          JOIN cases c ON c.case_number=t.case_number
         WHERE t.todo_date >= CURDATE()
           AND COALESCE(t.source_file,'') LIKE '%.pdf%'
           AND LOWER(COALESCE(t.status,'')) NOT IN
               ('deleted','completed','done','cancelled','canceled','calendar_deduped')
           AND LOWER(COALESCE(c.status,'')) IN ('已結案','結案','closed','completed','done','archived')
           AND EXISTS (
               SELECT 1 FROM cases a
                WHERE a.client_name=c.client_name
                  AND a.case_number<>c.case_number
                  AND LOWER(COALESCE(a.status,'')) NOT IN
                      ('已結案','結案','closed','completed','done','archived')
           )
         ORDER BY t.todo_date, t.id
        """,
        fetch="all",
    )
    return [dict(row or {}) for row in (rows or [])]


def _transcript_sources(source_case_folder: str) -> list[Path]:
    root = Path(source_case_folder) / "08_筆錄"
    return sorted(root.glob("20260629 *筆錄*.pdf")) if root.is_dir() else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    _load_live_environment()
    cases = _query_cases()
    case_by_no = {str(row.get("case_number") or ""): row for row in cases}
    from api.osc.utils import _osc_resolve_existing_local_path

    source = case_by_no.get("2026-0021") or {}
    source_local = _osc_resolve_existing_local_path(
        str(source.get("folder_path") or ""), prefer_dir=True
    )
    transcripts = _transcript_sources(source_local) if source_local else []
    report: dict[str, Any] = {
        "ok": True,
        "applied": bool(args.apply),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases": cases,
        "closed_case_future_transcript_todos": _query_closed_case_future_transcript_todos(),
        "transcripts": [
            {"path": str(path), "sha256": _sha256(path), "size": path.stat().st_size}
            for path in transcripts
        ],
        "actions": [],
    }

    # Content was independently verified as 115年度原金訴字第88號.  Require
    # the target DB row to carry that exact identity before any rearchive.
    target = case_by_no.get("2026-0071") or {}
    target_case_no = str(target.get("court_case_number") or "").replace("000088", "88")
    target_local = _osc_resolve_existing_local_path(str(target.get("folder_path") or ""), prefer_dir=True)
    report["target_transcript_folder"] = str(Path(target_local) / "08_筆錄") if target_local else ""
    if transcripts and "115年度原金訴字第88號" not in target_case_no:
        report["ok"] = False
        report["actions"].append({"status": "blocked", "reason": "target_docket_mismatch"})
    if transcripts and not target_local:
        report["ok"] = False
        report["actions"].append({"status": "blocked", "reason": "target_folder_unavailable"})

    if args.apply and report["ok"] and transcripts:
        from api.blueprints.osc_cases import _osc_effective_case_folder_for_row
        from api.osc.utils import _osc_exec
        from api.domains.case_file_operation_lock import (
            acquire_case_file_operation_lock,
            release_case_file_operation_lock,
        )

        # Scope physical folder mutation to the explicitly reported case.  The
        # production OSC path now repairs future edits generically, but a
        # one-time maintenance tool must not rename unrelated case trees.
        active_rows, _ = _osc_exec(
            """
            SELECT * FROM cases
             WHERE case_number='2026-0080'
               AND LOWER(COALESCE(status,'')) NOT IN
                   ('已結案','結案','closed','completed','done','archived')
               AND COALESCE(folder_path,'')<>''
            """,
            fetch="all",
        )
        for row in active_rows or []:
            result = _osc_effective_case_folder_for_row(dict(row), update_db=True)
            if result.get("updated") or result.get("pending_repair"):
                report["actions"].append(
                    {
                        "status": "folder_reconciled" if result.get("updated") else "folder_repair_pending",
                        "case_number": row.get("case_number"),
                        "result": result,
                    }
                )

        target_dir = Path(target_local) / "08_筆錄"
        lock = acquire_case_file_operation_lock(owner="rc271_case_consistency_repair")
        if not lock.get("acquired"):
            report["ok"] = False
            report["actions"].append({"status": "blocked", "reason": "case_file_operation_lock_busy"})
        else:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                for source in transcripts:
                    destination = target_dir / source.name
                    if destination.exists():
                        if _sha256(destination) != _sha256(source):
                            report["ok"] = False
                            report["actions"].append({"status": "blocked", "reason": "destination_collision", "path": str(destination)})
                            continue
                        report["actions"].append({"status": "already_present", "path": str(destination)})
                        continue
                    shutil.move(str(source), str(destination))
                    report["actions"].append({"status": "moved", "from": str(source), "to": str(destination), "sha256": _sha256(destination)})
            finally:
                release_case_file_operation_lock()

        if report["ok"]:
            update, _ = _osc_exec(
                """
                UPDATE case_todos
                   SET case_number='2026-0071', client_name='李滿金', status='pending'
                 WHERE id=5315 AND case_number='2026-0021'
                """,
                fetch="none",
            )
            report["actions"].append(
                {
                    "status": "todo_rebound",
                    "todo_id": 5315,
                    "from_case": "2026-0021",
                    "to_case": "2026-0071",
                    "updated": int((update or {}).get("rowcount") or 0),
                }
            )

    out = Path(args.json_out).expanduser() if args.json_out else ROOT / ".runtime/rc271_case_consistency_repair.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
