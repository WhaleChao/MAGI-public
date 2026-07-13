#!/usr/bin/env python3
"""Resummarize legacy judgment rows with source-supported quality gates.

This script repairs old court judgment summaries without clearing existing data.
Rows are updated only when the newly generated summary passes the same guards as
the live path: not degraded, structured as legal insight, and source-supported.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
REPORT_PATH = ROOT / ".runtime" / "legacy_judgment_resummary_latest.json"


def _load_judgment_action():
    path = ROOT / "skills" / "judgment-collector" / "action.py"
    spec = importlib.util.spec_from_file_location("judgment_collector_quality_resummary", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _needs_resummary(jc, summary: object, source_text: str, case_reason: str) -> str:
    text = str(summary or "").strip()
    if not text:
        return "missing_summary"
    if len(text) < 80:
        return "too_short"
    if "## 實務見解" not in text:
        return "missing_practice_insight_section"
    if jc._is_degraded_summary(text, case_reason):
        return "degraded_summary"
    support_error = jc._summary_source_support_failure(text, source_text)
    if support_error:
        return f"source_support:{support_error}"
    return ""


def _new_summary_is_usable(jc, summary: str, source_text: str, case_reason: str) -> tuple[bool, str]:
    text = str(summary or "").strip()
    if not text:
        return False, "empty_new_summary"
    if "## 實務見解" not in text:
        return False, "missing_practice_insight_section"
    if jc._is_degraded_summary(text, case_reason):
        return False, "degraded_new_summary"
    support_error = jc._summary_source_support_failure(text, source_text)
    if support_error:
        return False, f"source_support:{support_error}"
    return True, ""


def _court_candidate_sql(*, force_all: bool, start_id: int, min_chars: int, recheck_existing: bool) -> tuple[str, list[Any]]:
    where = [
        "id >= %s",
        "full_text IS NOT NULL",
        "CHAR_LENGTH(full_text) >= %s",
        "case_number IS NOT NULL",
        "(jid LIKE 'TPS%%' OR jid LIKE 'TPH%%' OR case_number NOT REGEXP %s)",
    ]
    params: list[Any] = [
        int(start_id),
        int(min_chars),
        "司促字|促字第|司票字|票字第|補字第|附民字|續收字|司催字|司消債核字|司執字|司繼字|司聲字|全字第|暫字第|拍字第|司拍字",
    ]
    if not force_all and not recheck_existing:
        where.append(
            "(summary IS NULL OR summary = '' OR CHAR_LENGTH(summary) < 80 "
            "OR summary NOT LIKE '%%## 實務見解%%' "
            "OR summary LIKE '%%WFGY%%' OR summary LIKE '%%【摘要格式要求】%%' "
            "OR summary LIKE '%%請您提供%%' OR summary LIKE '%%抽取式快篩%%')"
        )
    sql = (
        "SELECT id, jid, case_type, case_number, summary, full_text "
        "FROM court_judgments WHERE "
        + " AND ".join(where)
        + " ORDER BY id ASC"
    )
    return sql, params


def _archive_candidate_sql(*, force_all: bool, start_id: int, recheck_existing: bool) -> tuple[str, list[Any]]:
    where = ["id >= %s", "full_text_path IS NOT NULL", "full_text_path != ''"]
    params: list[Any] = [int(start_id)]
    if not force_all and not recheck_existing:
        where.append(
            "(summary_text IS NULL OR summary_text = '' OR CHAR_LENGTH(summary_text) < 80 "
            "OR COALESCE(is_degraded, 0) = 1 "
            "OR summary_text NOT LIKE '%%## 實務見解%%' "
            "OR summary_text LIKE '%%WFGY%%' OR summary_text LIKE '%%【摘要格式要求】%%' "
            "OR summary_text LIKE '%%請您提供%%' OR summary_text LIKE '%%抽取式快篩%%')"
        )
    sql = (
        "SELECT id, case_reason, case_type, judgment_title, full_text_path, summary_text "
        "FROM judgment_archive WHERE "
        + " AND ".join(where)
        + " ORDER BY id ASC"
    )
    return sql, params


def _read_text_path(path_value: object, *, min_chars: int) -> str:
    p = Path(str(path_value or "")).expanduser()
    if not p.exists() or not p.is_file():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    text = text.strip()
    return text if len(text) >= min_chars else ""


def _fetch_rows(cur, sql: str, params: list[Any], *, limit: int) -> list[dict[str, Any]]:
    final_sql = sql
    final_params = list(params)
    if limit > 0:
        final_sql += " LIMIT %s"
        final_params.append(int(limit))
    cur.execute(final_sql, tuple(final_params))
    return list(cur.fetchall() or [])


def _process_court(jc, conn, args, report: dict[str, Any], started_at: float) -> None:
    cur = conn.cursor(dictionary=True)
    sql, params = _court_candidate_sql(
        force_all=args.force_all,
        start_id=args.start_id,
        min_chars=args.min_chars,
        recheck_existing=args.recheck_existing,
    )
    rows = _fetch_rows(cur, sql, params, limit=args.limit)
    cur.close()
    report["court_candidates"] = len(rows)
    for index, row in enumerate(rows, start=1):
        if args.max_seconds > 0 and time.monotonic() - started_at >= args.max_seconds:
            report["stopped_reason"] = "max_seconds"
            return
        rid = int(row["id"])
        jid = str(row.get("jid") or "")
        case_reason = str(row.get("case_type") or row.get("case_number") or "")
        full_text = str(row.get("full_text") or "")
        reason = "force_all" if args.force_all else _needs_resummary(jc, row.get("summary"), full_text, case_reason)
        if not reason:
            report["skipped_good"] += 1
            report["last_seen"] = {"scope": "court", "id": rid, "jid": jid}
            continue
        print(f"[court {index}/{len(rows)}] id={rid} jid={jid} reason={reason}", flush=True)
        report["processed"] += 1
        if args.dry_run:
            report["would_update"] += 1
            report["last_seen"] = {"scope": "court", "id": rid, "jid": jid}
            continue
        try:
            new_summary = jc._summarize_judgment(full_text, case_reason, timeout_sec=args.timeout)
        except Exception as exc:
            report["failed"] += 1
            report["failures"].append({"scope": "court", "id": rid, "jid": jid, "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
            continue
        ok, error = _new_summary_is_usable(jc, new_summary, full_text, case_reason)
        if not ok:
            report["failed"] += 1
            report["failures"].append({"scope": "court", "id": rid, "jid": jid, "error": error})
            continue
        up = conn.cursor()
        up.execute("UPDATE court_judgments SET summary=%s, crawled_at=CURRENT_TIMESTAMP WHERE id=%s", (new_summary, rid))
        conn.commit()
        up.close()
        report["updated"] += 1
        report["last_seen"] = {"scope": "court", "id": rid, "jid": jid}
        time.sleep(max(0.0, float(args.delay)))


def _process_archive(jc, conn, args, report: dict[str, Any], started_at: float) -> None:
    cur = conn.cursor(dictionary=True)
    sql, params = _archive_candidate_sql(
        force_all=args.force_all,
        start_id=args.start_id,
        recheck_existing=args.recheck_existing,
    )
    rows = _fetch_rows(cur, sql, params, limit=args.limit)
    cur.close()
    report["archive_candidates"] = len(rows)
    for index, row in enumerate(rows, start=1):
        if args.max_seconds > 0 and time.monotonic() - started_at >= args.max_seconds:
            report["stopped_reason"] = "max_seconds"
            return
        rid = int(row["id"])
        case_reason = str(row.get("case_reason") or row.get("case_type") or row.get("judgment_title") or "")
        full_text = _read_text_path(row.get("full_text_path"), min_chars=args.min_chars)
        if not full_text:
            report["skipped_no_text"] += 1
            continue
        reason = "force_all" if args.force_all else _needs_resummary(jc, row.get("summary_text"), full_text, case_reason)
        if not reason:
            report["skipped_good"] += 1
            report["last_seen"] = {"scope": "archive", "id": rid}
            continue
        print(f"[archive {index}/{len(rows)}] id={rid} reason={reason}", flush=True)
        report["processed"] += 1
        if args.dry_run:
            report["would_update"] += 1
            report["last_seen"] = {"scope": "archive", "id": rid}
            continue
        try:
            new_summary = jc._summarize_judgment(full_text, case_reason, timeout_sec=args.timeout)
        except Exception as exc:
            report["failed"] += 1
            report["failures"].append({"scope": "archive", "id": rid, "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
            continue
        ok, error = _new_summary_is_usable(jc, new_summary, full_text, case_reason)
        if not ok:
            report["failed"] += 1
            report["failures"].append({"scope": "archive", "id": rid, "error": error})
            continue
        up = conn.cursor()
        up.execute(
            "UPDATE judgment_archive SET summary_text=%s, is_degraded=0, crawled_at=CURRENT_TIMESTAMP WHERE id=%s",
            (new_summary, rid),
        )
        conn.commit()
        up.close()
        report["updated"] += 1
        report["last_seen"] = {"scope": "archive", "id": rid}
        time.sleep(max(0.0, float(args.delay)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Resummarize legacy judgments with source-supported quality gates")
    parser.add_argument("--scope", choices=["court", "archive", "both"], default="court")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--start-id", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("MAGI_LEGACY_RESUMMARY_TIMEOUT_SEC", "420") or "420"))
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--max-seconds", type=float, default=0)
    parser.add_argument("--min-chars", type=int, default=1200)
    parser.add_argument("--force-all", action="store_true", help="Regenerate even if current summary passes quality gates")
    parser.add_argument("--recheck-existing", action="store_true", help="Inspect existing ## 實務見解 rows for source support")
    parser.add_argument("--json-out", default=str(REPORT_PATH))
    args = parser.parse_args()

    started_at = time.monotonic()
    jc = _load_judgment_action()
    conn = jc._get_db()
    report: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "scope": args.scope,
        "limit": args.limit,
        "processed": 0,
        "would_update": 0,
        "updated": 0,
        "failed": 0,
        "skipped_good": 0,
        "skipped_no_text": 0,
        "failures": [],
        "stopped_reason": "",
    }
    try:
        if args.scope in {"court", "both"}:
            _process_court(jc, conn, args, report, started_at)
        if args.scope in {"archive", "both"} and not report.get("stopped_reason"):
            _process_archive(jc, conn, args, report, started_at)
    finally:
        conn.close()
    report["elapsed_sec"] = round(time.monotonic() - started_at, 2)
    report["failures"] = report["failures"][:20]
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
