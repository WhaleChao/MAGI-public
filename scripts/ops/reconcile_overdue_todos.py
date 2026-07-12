#!/usr/bin/env python3
"""Reconcile stale overdue todos without hiding unresolved legal work."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("MAGI_ROOT_DIR") or Path(__file__).resolve().parents[2]).expanduser()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OCCURRENCE_TYPES = {
    "行事曆事件",
    "律見",
    "來所提供資料",
    "會議",
    "調解",
    "開庭",
    "閱卷",
    "準備程序",
    "審理",
    "調查",
}
CLOSED_CASE_STATUSES = {"已結案", "已結案，待報結", "已結案待報結", "已結案，待送出"}
ACTIVE_LAF_STATUSES = {"進行中", "已開辦", "開辦", "已回報開辦"}
OUTGOING_EVIDENCE_TYPES = {"補正", "陳報"}


def _next_business_day(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _source_kind(source_file: Any) -> str:
    text = str(source_file or "").strip().lower()
    if text.startswith("gcal_import:") or text.startswith("gcal_mirror:"):
        return "calendar"
    return "document"


def _is_optional_or_nonactionable(row: dict) -> bool:
    text = f"{row.get('description') or ''} {row.get('source_file') or ''}"
    todo_type = str(row.get("todo_type") or "")
    if todo_type == "異議" and "調解不成立證明書" in text:
        return True
    if todo_type in {"提出資料", "異議", "確認"} and any(term in text for term in ("得於", "得以", "如有異議")):
        return True
    return False


def _document_day(name: str, path: Path) -> date | None:
    match = re.match(r"^(20\d{6})", name)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None


def _has_strict_outgoing_evidence(row: dict) -> bool:
    todo_type = str(row.get("todo_type") or "")
    if todo_type not in OUTGOING_EVIDENCE_TYPES:
        return False
    folder_path = str(row.get("folder_path") or "").strip()
    due = row.get("todo_date")
    if not folder_path or not isinstance(due, date):
        return False
    try:
        from api.case_path_mapper import translate_case_path_to_local

        local = translate_case_path_to_local(folder_path, require_existing=True, for_write=False)
    except Exception:
        return False
    if not local or not os.path.isdir(local):
        return False
    keywords = {"補正", "陳報"}
    for subdir in ("04_我方歷次書狀", "11_回執"):
        base = Path(local) / subdir
        if not base.is_dir():
            continue
        try:
            paths = base.rglob("*")
            for path in paths:
                if not path.is_file() or path.name.startswith((".", "~")):
                    continue
                if not any(keyword in path.name for keyword in keywords):
                    continue
                doc_day = _document_day(path.name, path)
                if doc_day and doc_day >= due:
                    return True
        except OSError:
            continue
    return False


def classify_todo(row: dict, *, verified_ids: set[int] | None = None) -> tuple[str, str]:
    """Return ``(action, reason)`` where action is complete, archive, or escalate."""
    verified_ids = verified_ids or set()
    todo_id = int(row.get("id") or 0)
    todo_type = str(row.get("todo_type") or "").strip()
    if todo_type == "逾期確認":
        return "skip", "already_escalated"
    if todo_id in verified_ids:
        return "complete", "manually_verified_evidence"
    if todo_type in OCCURRENCE_TYPES:
        return ("archive", "past_calendar_occurrence") if _source_kind(row.get("source_file")) == "calendar" else ("complete", "past_occurrence")
    if str(row.get("case_status") or "") in CLOSED_CASE_STATUSES:
        return "complete", "case_closed"
    if todo_type == "法扶" and str(row.get("legal_aid_status") or "") in ACTIVE_LAF_STATUSES:
        return "complete", "laf_already_started"
    if _is_optional_or_nonactionable(row):
        return "complete", "optional_or_nonactionable"
    if _has_strict_outgoing_evidence(row):
        return "complete", "outgoing_document_evidence"
    return "escalate", "no_verifiable_completion_evidence"


def _connect():
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except Exception:
        pass
    import pymysql

    return pymysql.connect(
        host=os.environ["OSC_DB_HOST"],
        port=int(os.environ.get("OSC_DB_PORT", "3306")),
        user=os.environ["OSC_DB_USER"],
        password=os.environ["OSC_DB_PASSWORD"],
        database=os.environ["OSC_DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _load_overdue(conn, cutoff: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              t.*, COALESCE(c.status, '') AS case_status,
              COALESCE(c.folder_path, '') AS folder_path,
              COALESCE(c.legal_aid_status, '') AS legal_aid_status
            FROM case_todos t
            LEFT JOIN cases c ON c.case_number=t.case_number AND c.tenant_id=t.tenant_id
            WHERE t.status='pending' AND t.todo_date < %s
            ORDER BY t.todo_date, t.id
            """,
            (cutoff,),
        )
        return list(cur.fetchall() or [])


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_value) + "\n", encoding="utf-8")
    tmp.replace(path)


def reconcile(*, apply: bool, cutoff: date, verified_ids: set[int]) -> dict:
    conn = _connect()
    rows: list[dict] = []
    try:
        rows = _load_overdue(conn, cutoff)
        decisions = []
        for row in rows:
            action, reason = classify_todo(row, verified_ids=verified_ids)
            decisions.append({"row": row, "action": action, "reason": reason})

        counts = Counter(item["action"] for item in decisions)
        reasons = Counter(item["reason"] for item in decisions)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = ROOT / ".runtime" / f"overdue_todo_reconcile_backup_{stamp}.json"
        if apply:
            _write_json(backup_path, rows)
            review_date = _next_business_day(cutoff)
            with conn.cursor() as cur:
                for item in decisions:
                    row = item["row"]
                    action = item["action"]
                    reason = item["reason"]
                    todo_id = int(row["id"])
                    if action == "skip":
                        continue
                    if action in {"complete", "archive"}:
                        status = "calendar_deduped" if action == "archive" else "completed"
                        cur.execute(
                            "UPDATE case_todos SET status=%s, completed_date=COALESCE(completed_date,NOW()) WHERE id=%s AND status='pending'",
                            (status, todo_id),
                        )
                        continue
                    marker = f"【MAGI逾期治理：原待辦#{todo_id}】"
                    cur.execute(
                        """
                        SELECT id FROM case_todos
                        WHERE status='pending' AND todo_type='逾期確認' AND description LIKE %s
                        LIMIT 1
                        """,
                        (f"%{marker}%",),
                    )
                    existing = cur.fetchone()
                    if not existing:
                        original = str(row.get("description") or "").strip()
                        description = (
                            f"{marker}\n原期限：{row.get('todo_date')}／原類型：{row.get('todo_type')}\n"
                            f"尚無可驗證的完成證據，請確認是否已辦理；確認後即可結束本待辦。\n{original}"
                        )
                        cur.execute(
                            """
                            INSERT INTO case_todos
                              (case_number,client_name,todo_type,todo_date,todo_time,description,status,
                               google_calendar_event_id,source_file,google_calendar_id,tenant_id)
                            VALUES (%s,%s,'逾期確認',%s,NULL,%s,'pending',NULL,%s,NULL,%s)
                            """,
                            (
                                row.get("case_number") or "",
                                row.get("client_name"),
                                review_date,
                                description,
                                row.get("source_file"),
                                row.get("tenant_id") or "magi-primary",
                            ),
                        )
                    cur.execute(
                        """
                        UPDATE case_todos
                        SET status='cancelled', completed_date=COALESCE(completed_date,NOW()),
                            description=CONCAT(COALESCE(description,''), %s)
                        WHERE id=%s AND status='pending'
                        """,
                        (f"\n【MAGI逾期治理】已升級為 {review_date} 的逾期確認待辦；原因：{reason}。", todo_id),
                    )
            conn.commit()
        else:
            conn.rollback()

        public_items = [
            {"id": int(item["row"]["id"]), "action": item["action"], "reason": item["reason"]}
            for item in decisions
        ]
        return {
            "ok": True,
            "applied": apply,
            "cutoff": cutoff.isoformat(),
            "matched": len(rows),
            "counts": dict(counts),
            "reasons": dict(reasons),
            "backup_path": str(backup_path) if apply else "",
            "items": public_items,
        }
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "applied": False, "matched": len(rows), "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="治理 MAGI 逾期待辦")
    parser.add_argument("--apply", action="store_true", help="實際更新資料庫；預設僅預演")
    parser.add_argument("--cutoff", default=date.today().isoformat(), help="只處理此日期以前的 pending 待辦")
    parser.add_argument("--verified-id", action="append", type=int, default=[], help="人工核對已有完成證據的待辦 ID")
    parser.add_argument("--json-out", default=str(ROOT / ".runtime" / "overdue_todo_reconcile_latest.json"))
    args = parser.parse_args(argv)
    try:
        cutoff = date.fromisoformat(args.cutoff)
    except ValueError:
        parser.error("--cutoff 必須是 YYYY-MM-DD")
    result = reconcile(apply=args.apply, cutoff=cutoff, verified_ids=set(args.verified_id))
    _write_json(Path(args.json_out), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_value))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
