#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.domains.judgment_value_filter import SKIP_SUMMARY, classify_judgment_record


DEFAULT_OUTPUT_ROOT = ROOT / "reports" / "judgment_value_cleanup"
CLEANUP_CATEGORIES = {
    "payment_order",
    "promissory_note",
    "fee_order",
    "attached_civil_transfer",
    "execution",
}

ALWAYS_DELETE_SKIP_CATEGORIES = {
    "fee_order",
    "execution",
}


def _summary_is_low_value(summary: Any) -> bool:
    s = str(summary or "").strip()
    if not s:
        return True
    low_markers = (
        "無可擷取之實務見解",
        "程序性文書",
        "屬程序性",
        "支付命令，屬",
        "本票裁定，屬",
    )
    return any(marker in s for marker in low_markers)


def _is_missing_text_low_value(row: Dict[str, Any]) -> bool:
    summary = str(row.get("summary") or "")
    full_text = str(row.get("full_text") or "").strip()
    return (not full_text) and "無可擷取之實務見解" in summary and "原始資料未提供判決全文" in summary


def _is_upper_protected(row: Dict[str, Any]) -> bool:
    jid = str(row.get("jid") or "").upper()
    court_name = str(row.get("court_name") or "")
    prefix = jid.split(",", 1)[0]
    return (
        prefix.startswith(("TPS", "TPH", "TPHA", "TPHM", "TPHV", "TPBA"))
        or prefix.endswith("TA")
        or "最高" in court_name
        or "高等" in court_name
        or "行政" in court_name
    )


def _load_judgment_collector_action():
    action_path = ROOT / "skills" / "judgment-collector" / "action.py"
    spec = importlib.util.spec_from_file_location("magi_judgment_collector_action", action_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load judgment-collector action.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _write_jsonl_gz(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str, sort_keys=True) + "\n")
            count += 1
    return count


def _chunks(values: List[str], size: int = 500) -> Iterable[List[str]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _fetch_archive_rows(cur, jids: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not jids:
        return rows
    for chunk in _chunks(jids):
        placeholders = ",".join(["%s"] * len(chunk))
        cur.execute(f"SELECT * FROM judgment_archive WHERE source_jid IN ({placeholders})", tuple(chunk))
        rows.extend(cur.fetchall() or [])
    return rows


def _delete_by_jids(cur, table: str, column: str, jids: List[str]) -> int:
    deleted = 0
    for chunk in _chunks(jids):
        placeholders = ",".join(["%s"] * len(chunk))
        cur.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", tuple(chunk))
        deleted += int(cur.rowcount or 0)
    return deleted


def build_cleanup_report(*, include_generic_procedural: bool = False, limit: int = 0) -> Dict[str, Any]:
    action = _load_judgment_collector_action()
    conn = action._get_db()
    if not conn:
        raise RuntimeError("db_connect_failed")
    cur = conn.cursor(dictionary=True)
    sql = (
        "SELECT id, jid, court_name, case_number, case_type, judgment_date, summary, full_text, source_url "
        "FROM court_judgments ORDER BY id"
    )
    if limit and limit > 0:
        sql += " LIMIT %s"
        cur.execute(sql, (int(limit),))
    else:
        cur.execute(sql)

    candidates: List[Dict[str, Any]] = []
    category_counts: Dict[str, int] = {}
    disposition_counts: Dict[str, int] = {}
    scanned = 0
    for row in cur:
        scanned += 1
        decision = classify_judgment_record(
            jid=row.get("jid") or "",
            court_name=row.get("court_name") or "",
            case_number=row.get("case_number") or "",
            case_reason=row.get("case_type") or "",
            title=row.get("case_number") or "",
            full_text=row.get("full_text") or "",
        )
        category_counts[decision.category] = category_counts.get(decision.category, 0) + 1
        disposition_counts[decision.disposition] = disposition_counts.get(decision.disposition, 0) + 1
        cleanup_category = decision.category in CLEANUP_CATEGORIES or (
            include_generic_procedural and decision.category == "procedural_ruling"
        )
        can_delete_skip = decision.disposition == SKIP_SUMMARY and cleanup_category
        can_delete_missing_text = (not _is_upper_protected(row)) and _is_missing_text_low_value(row)
        if can_delete_skip or can_delete_missing_text:
            if can_delete_missing_text and not can_delete_skip:
                decision = decision.__class__("SKIP_SUMMARY", "missing_full_text_no_extractable", 0.9, "missing_full_text")
            candidates.append(
                {
                    "id": row.get("id"),
                    "jid": row.get("jid") or "",
                    "court_name": row.get("court_name") or "",
                    "case_number": row.get("case_number") or "",
                    "case_type": row.get("case_type") or "",
                    "judgment_date": row.get("judgment_date"),
                    "source_url": row.get("source_url") or "",
                    "decision": decision.to_dict(),
                    "text_preview": (row.get("full_text") or "")[:260].replace("\n", " "),
                    "summary_preview": (row.get("summary") or "")[:180].replace("\n", " "),
                    "_backup_row": row,
                }
            )
    jids = [c["jid"] for c in candidates if c.get("jid")]
    archive_rows = _fetch_archive_rows(cur, jids)
    cur.close()
    conn.close()
    return {
        "generated_at": datetime.now().isoformat(),
        "scanned": scanned,
        "candidate_count": len(candidates),
        "candidate_jids": len(jids),
        "archive_candidate_count": len(archive_rows),
        "cleanup_categories": sorted(CLEANUP_CATEGORIES),
        "include_generic_procedural": include_generic_procedural,
        "category_counts": category_counts,
        "disposition_counts": disposition_counts,
        "candidates": candidates,
        "archive_rows": archive_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean high-confidence low-value judgment rows from DB")
    parser.add_argument("--apply", action="store_true", help="Delete candidate rows after writing backups")
    parser.add_argument("--include-generic-procedural", action="store_true", help="Also delete generic pure procedural rulings")
    parser.add_argument("--limit", type=int, default=0, help="Scan only first N court_judgments rows")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    args = parser.parse_args()

    report = build_cleanup_report(include_generic_procedural=args.include_generic_procedural, limit=args.limit)
    run_dir = Path(args.output_root) / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    backup_court_rows = [dict(c.pop("_backup_row")) for c in report["candidates"]]
    court_backup_path = run_dir / "court_judgments_candidates.jsonl.gz"
    archive_backup_path = run_dir / "judgment_archive_candidates.jsonl.gz"
    _write_jsonl_gz(court_backup_path, backup_court_rows)
    _write_jsonl_gz(archive_backup_path, report["archive_rows"])

    sample_candidates = report["candidates"][:40]
    summary = {
        k: v
        for k, v in report.items()
        if k not in {"candidates", "archive_rows"}
    }
    summary.update(
        {
            "apply": bool(args.apply),
            "run_dir": str(run_dir),
            "court_backup_path": str(court_backup_path),
            "archive_backup_path": str(archive_backup_path),
            "sample_candidates": sample_candidates,
        }
    )

    deleted_court = 0
    deleted_archive = 0
    if args.apply and backup_court_rows:
        action = _load_judgment_collector_action()
        conn = action._get_db()
        if not conn:
            raise RuntimeError("db_connect_failed_apply")
        cur = conn.cursor()
        jids = [str(r.get("jid") or "") for r in backup_court_rows if str(r.get("jid") or "")]
        deleted_archive = _delete_by_jids(cur, "judgment_archive", "source_jid", jids)
        deleted_court = _delete_by_jids(cur, "court_judgments", "jid", jids)
        conn.commit()
        cur.close()
        conn.close()
    summary["deleted_court_judgments"] = deleted_court
    summary["deleted_judgment_archive"] = deleted_archive

    _write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
