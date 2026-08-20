#!/usr/bin/env python3
"""Resummarize legacy judgment rows with source-supported quality gates.

This script repairs old court judgment summaries without clearing existing data.
Rows are updated only when the newly generated summary passes the same guards as
the live path: not degraded, structured as legal insight, and source-supported.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from magi_v3 import fcntl_compat as fcntl  # noqa: E402
RUNTIME_DIR = Path(os.environ.get("MAGI_RUNTIME_DIR", "").strip() or ROOT / ".runtime").expanduser()
REPORT_PATH = RUNTIME_DIR / "legacy_judgment_resummary_latest.json"
BACKUP_DIR = RUNTIME_DIR / "backups" / "judgment_resummary"
LOCK_PATH = RUNTIME_DIR / "legacy_judgment_resummary.lock"
CURSOR_PATH = RUNTIME_DIR / "legacy_judgment_resummary_cursor.json"
REJECTION_PATH = RUNTIME_DIR / "legacy_judgment_resummary_rejections.jsonl"
REVIEWED_PATH = RUNTIME_DIR / "legacy_judgment_resummary_reviewed.json"


def _load_judgment_action():
    path = ROOT / "skills" / "judgment-collector" / "action.py"
    spec = importlib.util.spec_from_file_location("judgment_collector_quality_resummary", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _needs_resummary(
    jc,
    summary: object,
    source_text: str,
    case_reason: str,
    court_name: str = "",
) -> str:
    text = str(summary or "").strip()
    if not text:
        return "missing_summary"
    if len(text) < 80:
        return "too_short"
    if "## 實務見解" not in text:
        return "missing_practice_insight_section"
    if jc._is_degraded_summary(text, case_reason):
        return "degraded_summary"
    quality_error = jc._summary_practical_value_failure(
        text,
        source_text,
        case_reason,
    )
    if quality_error:
        return f"practical_quality:{quality_error}"
    from api.domains.judgment_summary_quality import evaluate_practice_ready_summary

    ready = evaluate_practice_ready_summary(
        text,
        source_text,
        case_reason,
        court_name,
    )
    if not ready.ok:
        return f"practice_ready:{ready.reason or 'not_ready'}"
    return ""


def _new_summary_is_usable(
    jc,
    summary: str,
    source_text: str,
    case_reason: str,
    court_name: str = "",
) -> tuple[bool, str]:
    text = str(summary or "").strip()
    if not text:
        return False, "empty_new_summary"
    if "## 實務見解" not in text:
        return False, "missing_practice_insight_section"
    if jc._is_degraded_summary(text, case_reason):
        return False, "degraded_new_summary"
    quality_error = jc._summary_practical_value_failure(
        text,
        source_text,
        case_reason,
    )
    if quality_error:
        return False, f"practical_quality:{quality_error}"
    from api.domains.judgment_summary_quality import evaluate_practice_ready_summary

    ready = evaluate_practice_ready_summary(
        text,
        source_text,
        case_reason,
        court_name,
    )
    if not ready.ok:
        return False, f"practice_ready:{ready.reason or 'not_ready'}"
    return True, ""


def _generate_summary(jc, source_text: str, case_reason: str, args) -> str:
    args.last_summary_meta = {}
    if args.summary_mode == "extractive":
        return jc._extractive_judgment_summary(
            source_text,
            case_reason,
            max_chars=args.max_summary_chars,
        )
    if args.summary_mode == "nvidia":
        from api.domains.judgment_nvidia_summary import summarize_with_nvidia

        result = summarize_with_nvidia(
            source_text,
            case_reason,
            timeout_sec=args.timeout,
            max_chars=args.max_summary_chars,
        )
        args.last_summary_meta = result.audit_dict()
        if result.success:
            return result.summary
        if result.reviewed_no_insight:
            return ""
        raise RuntimeError(result.error or "nvidia_summary_failed")
    return jc._summarize_judgment(
        source_text,
        case_reason,
        timeout_sec=args.timeout,
    )


def _count_summary_meta(report: dict[str, Any], meta: dict[str, Any]) -> None:
    if not meta:
        return
    report["provider_calls"] = int(report.get("provider_calls") or 0) + 1
    model = str(meta.get("model") or "unknown")
    models = report.setdefault("provider_models", {})
    models[model] = int(models.get(model) or 0) + 1
    if meta.get("success"):
        report["provider_accepted"] = int(report.get("provider_accepted") or 0) + 1
    elif meta.get("reviewed_no_insight"):
        report["provider_no_insight"] = int(report.get("provider_no_insight") or 0) + 1
    else:
        report["provider_failed"] = int(report.get("provider_failed") or 0) + 1


def _append_backup(args, payload: dict[str, Any]) -> None:
    path = getattr(args, "backup_path", None)
    if not path:
        raise RuntimeError("backup_path_missing")
    record = {
        **payload,
        "backed_up_at": datetime.now().astimezone().isoformat(),
    }
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_resume_cursor(default: int = 1) -> int:
    try:
        payload = json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
        return max(1, int(payload.get("next_start_id") or default))
    except Exception:
        return max(1, int(default))


def _save_resume_cursor(next_start_id: int, report: dict[str, Any]) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "next_start_id": max(1, int(next_start_id)),
        "updated_at": datetime.now().astimezone().isoformat(),
        "last_seen": report.get("last_seen") or {},
    }
    tmp = CURSOR_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CURSOR_PATH)


def _record_quality_rejection(payload: dict[str, Any]) -> None:
    REJECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **payload,
        "reviewed_at": datetime.now().astimezone().isoformat(),
    }
    with REJECTION_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_reviewed_quality() -> dict[str, dict[str, Any]]:
    """Load the compact ledger, falling back to the append-only evidence log."""
    try:
        payload = json.loads(REVIEWED_PATH.read_text(encoding="utf-8"))
        rows = payload.get("rows") if isinstance(payload, dict) else {}
        if isinstance(rows, dict):
            return {
                str(key): value
                for key, value in rows.items()
                if isinstance(value, dict)
            }
    except Exception:
        pass
    rows: dict[str, dict[str, Any]] = {}
    try:
        for line in REJECTION_PATH.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if str(record.get("scope") or "") != "court":
                continue
            rid = str(int(record.get("id") or 0))
            if rid != "0":
                rows[rid] = record
    except Exception:
        pass
    return rows


def _save_reviewed_quality(rows: dict[str, dict[str, Any]]) -> None:
    REVIEWED_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "updated_at": datetime.now().astimezone().isoformat(),
        "reviewed_no_usable_insight": len(rows),
        "rows": rows,
    }
    tmp = REVIEWED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REVIEWED_PATH)


def _write_report_atomic(args, report: dict[str, Any], started_at: float, *, status: str) -> None:
    """Persist resumable progress without exposing a partial JSON document."""
    report["status"] = status
    report["elapsed_sec"] = round(time.monotonic() - started_at, 2)
    report["updated_at"] = datetime.now().astimezone().isoformat()
    elapsed = max(float(report["elapsed_sec"]), 0.01)
    report["updates_per_minute"] = round(float(report.get("updated") or 0) * 60.0 / elapsed, 1)
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)


def _checkpoint(args, report: dict[str, Any], started_at: float) -> None:
    if int(report.get("processed") or 0) and int(report.get("processed") or 0) % 20 == 0:
        _write_report_atomic(args, report, started_at, status="running")


def _court_candidate_sql(
    *,
    force_all: bool,
    start_id: int,
    min_chars: int,
    recheck_existing: bool,
    row_ids: list[int] | None = None,
) -> tuple[str, list[Any]]:
    exact_ids = list(dict.fromkeys(int(value) for value in (row_ids or []) if int(value) > 0))
    where = [
        "full_text IS NOT NULL",
        "CHAR_LENGTH(full_text) >= %s",
        "case_number IS NOT NULL",
        "(jid LIKE 'TPS%%' OR jid LIKE 'TPH%%' OR case_number NOT REGEXP %s)",
    ]
    params: list[Any] = [
        int(min_chars),
        "司促字|促字第|司票字|票字第|補字第|附民字|續收字|司催字|司消債核字|司執字|司繼字|司聲字|全字第|暫字第|拍字第|司拍字",
    ]
    if exact_ids:
        where.insert(0, f"id IN ({','.join(['%s'] * len(exact_ids))})")
        params = [*exact_ids, *params]
    else:
        where.insert(0, "id >= %s")
        params.insert(0, int(start_id))
    if not force_all and not recheck_existing:
        where.append(
            "(summary IS NULL OR summary = '' OR CHAR_LENGTH(summary) < 80 "
            "OR summary NOT LIKE '%%## 實務見解%%' "
            "OR summary LIKE '%%WFGY%%' OR summary LIKE '%%【摘要格式要求】%%' "
            "OR summary LIKE '%%請您提供%%' OR summary LIKE '%%抽取式快篩%%')"
        )
    sql = (
        "SELECT id, jid, court_name, case_type, case_number, summary, full_text "
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
        "SELECT id, case_reason, case_type, judgment_title, full_text_path, summary_text, is_degraded "
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
        row_ids=args.row_id,
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
        full_text = str(row.get("full_text") or "")
        source_sha256 = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
        reviewed = args.reviewed_quality.get(str(rid), {})
        if (
            not args.recheck_reviewed
            and str(reviewed.get("source_sha256") or "") == source_sha256
        ):
            report["reviewed_skipped"] += 1
            report["last_seen"] = {"scope": "court", "id": rid, "jid": jid}
            report["next_start_id"] = rid + 1
            continue
        case_reason = jc.infer_case_issue(
            full_text,
            str(row.get("case_number") or ""),
            str(row.get("case_type") or ""),
        )
        report.setdefault("inferred_issue_counts", {})
        report["inferred_issue_counts"][case_reason] = (
            int(report["inferred_issue_counts"].get(case_reason) or 0) + 1
        )
        reason = "force_all" if args.force_all else _needs_resummary(
            jc,
            row.get("summary"),
            full_text,
            case_reason,
            str(row.get("court_name") or ""),
        )
        if not reason:
            report["skipped_good"] += 1
            report["last_seen"] = {"scope": "court", "id": rid, "jid": jid}
            report["next_start_id"] = rid + 1
            continue
        print(f"[court {index}/{len(rows)}] id={rid} jid={jid} reason={reason}", flush=True)
        report["processed"] += 1
        if args.dry_run:
            report["would_update"] += 1
            report["last_seen"] = {"scope": "court", "id": rid, "jid": jid}
            report["next_start_id"] = rid + 1
            _checkpoint(args, report, started_at)
            continue
        try:
            new_summary = _generate_summary(jc, full_text, case_reason, args)
            _count_summary_meta(report, getattr(args, "last_summary_meta", {}))
        except Exception as exc:
            _count_summary_meta(report, getattr(args, "last_summary_meta", {}))
            report["failed"] += 1
            report["failures"].append({"scope": "court", "id": rid, "jid": jid, "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
            _checkpoint(args, report, started_at)
            continue
        ok, error = _new_summary_is_usable(
            jc,
            new_summary,
            full_text,
            case_reason,
            str(row.get("court_name") or ""),
        )
        if not ok:
            summary_meta = getattr(args, "last_summary_meta", {})
            if summary_meta.get("reviewed_no_insight"):
                error = f"nvidia_no_usable_insight:{summary_meta.get('error') or error}"
            report["quality_rejected"] += 1
            _record_quality_rejection(
                {
                    "scope": "court",
                    "id": rid,
                    "jid": jid,
                    "case_reason": case_reason,
                    "reason": error,
                    "source_sha256": source_sha256,
                }
            )
            args.reviewed_quality[str(rid)] = {
                "id": rid,
                "jid": jid,
                "case_reason": case_reason,
                "reason": error,
                "source_sha256": source_sha256,
                "reviewed_at": datetime.now().astimezone().isoformat(),
            }
            report["last_seen"] = {"scope": "court", "id": rid, "jid": jid}
            report["next_start_id"] = rid + 1
            _checkpoint(args, report, started_at)
            continue
        _append_backup(
            args,
            {
                "table": "court_judgments",
                "id": rid,
                "jid": jid,
                "old_summary": str(row.get("summary") or ""),
                "new_summary": new_summary,
                "source_sha256": source_sha256,
                "summary_provenance": getattr(args, "last_summary_meta", {}),
            },
        )
        up = conn.cursor()
        up.execute("UPDATE court_judgments SET summary=%s, crawled_at=CURRENT_TIMESTAMP WHERE id=%s", (new_summary, rid))
        conn.commit()
        up.close()
        report["updated"] += 1
        args.reviewed_quality.pop(str(rid), None)
        report["last_seen"] = {"scope": "court", "id": rid, "jid": jid}
        report["next_start_id"] = rid + 1
        _checkpoint(args, report, started_at)
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
        reason = "force_all" if args.force_all else _needs_resummary(
            jc,
            row.get("summary_text"),
            full_text,
            case_reason,
            str(row.get("judgment_title") or ""),
        )
        if not reason:
            report["skipped_good"] += 1
            report["last_seen"] = {"scope": "archive", "id": rid}
            continue
        print(f"[archive {index}/{len(rows)}] id={rid} reason={reason}", flush=True)
        report["processed"] += 1
        if args.dry_run:
            report["would_update"] += 1
            report["last_seen"] = {"scope": "archive", "id": rid}
            _checkpoint(args, report, started_at)
            continue
        try:
            new_summary = _generate_summary(jc, full_text, case_reason, args)
            _count_summary_meta(report, getattr(args, "last_summary_meta", {}))
        except Exception as exc:
            _count_summary_meta(report, getattr(args, "last_summary_meta", {}))
            report["failed"] += 1
            report["failures"].append({"scope": "archive", "id": rid, "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
            _checkpoint(args, report, started_at)
            continue
        ok, error = _new_summary_is_usable(
            jc,
            new_summary,
            full_text,
            case_reason,
            str(row.get("judgment_title") or ""),
        )
        if not ok:
            report["failed"] += 1
            report["failures"].append({"scope": "archive", "id": rid, "error": error})
            _checkpoint(args, report, started_at)
            continue
        _append_backup(
            args,
            {
                "table": "judgment_archive",
                "id": rid,
                "old_summary": str(row.get("summary_text") or ""),
                "old_is_degraded": int(row.get("is_degraded") or 0),
                "new_summary": new_summary,
                "source_sha256": hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
                "summary_provenance": getattr(args, "last_summary_meta", {}),
            },
        )
        up = conn.cursor()
        up.execute(
            "UPDATE judgment_archive SET summary_text=%s, is_degraded=0, crawled_at=CURRENT_TIMESTAMP WHERE id=%s",
            (new_summary, rid),
        )
        conn.commit()
        up.close()
        report["updated"] += 1
        report["last_seen"] = {"scope": "archive", "id": rid}
        report["next_start_id"] = rid + 1
        _checkpoint(args, report, started_at)
        time.sleep(max(0.0, float(args.delay)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Resummarize legacy judgments with source-supported quality gates")
    parser.add_argument("--scope", choices=["court", "archive", "both"], default="court")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--start-id", type=int, default=1)
    parser.add_argument(
        "--row-id",
        action="append",
        type=int,
        default=[],
        help="Process an exact court_judgments row; repeat for a controlled batch.",
    )
    parser.add_argument("--no-resume", action="store_true", help="Ignore the persisted forward cursor")
    parser.add_argument(
        "--recheck-reviewed",
        action="store_true",
        help="Recheck rows previously reviewed as having no usable source-bound insight",
    )
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("MAGI_LEGACY_RESUMMARY_TIMEOUT_SEC", "420") or "420"))
    parser.add_argument("--summary-mode", choices=["extractive", "llm", "nvidia"], default="extractive")
    parser.add_argument("--max-summary-chars", type=int, default=1800)
    parser.add_argument("--backup-json", default="")
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--max-seconds", type=float, default=0)
    parser.add_argument("--min-chars", type=int, default=1200)
    parser.add_argument("--force-all", action="store_true", help="Regenerate even if current summary passes quality gates")
    parser.add_argument("--recheck-existing", action="store_true", help="Inspect existing ## 實務見解 rows for source support")
    parser.add_argument("--json-out", default=str(REPORT_PATH))
    args = parser.parse_args()
    args.row_id = list(dict.fromkeys(int(value) for value in args.row_id if int(value) > 0))
    if args.row_id and args.scope != "court":
        parser.error("--row-id is supported only with --scope court")

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        report = {
            "ok": True,
            "success": True,
            "skipped": True,
            "status": "already_running",
            "scope": args.scope,
            "summary_mode": args.summary_mode,
            "limit": args.limit,
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        # Preserve the owner process' running progress artifact.  A duplicate
        # invocation must not replace truthful in-flight evidence.
        print(json.dumps(report, ensure_ascii=False, indent=2))
        lock_handle.close()
        return 0

    if not args.row_id and not args.no_resume and int(args.start_id) <= 1:
        args.start_id = _load_resume_cursor(1)
    args.reviewed_quality = _load_reviewed_quality()

    if not args.dry_run:
        backup_path = (
            Path(args.backup_json).expanduser()
            if str(args.backup_json or "").strip()
            else BACKUP_DIR / f"judgment_resummary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        )
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.exists() and backup_path.stat().st_size:
            parser.error(f"refusing to overwrite non-empty backup: {backup_path}")
        backup_path.touch(exist_ok=True)
        args.backup_path = backup_path
    else:
        args.backup_path = None

    started_at = time.monotonic()
    jc = _load_judgment_action()
    conn = jc._get_db()
    report: dict[str, Any] = {
        "ok": True,
        "success": True,
        "status": "running",
        "started_at": datetime.now().astimezone().isoformat(),
        "dry_run": bool(args.dry_run),
        "scope": args.scope,
        "summary_mode": args.summary_mode,
        "limit": args.limit,
        "targeted_row_ids": args.row_id,
        "backup_json": str(args.backup_path or ""),
        "processed": 0,
        "would_update": 0,
        "updated": 0,
        "failed": 0,
        "quality_rejected": 0,
        "provider_calls": 0,
        "provider_accepted": 0,
        "provider_no_insight": 0,
        "provider_failed": 0,
        "provider_models": {},
        "reviewed_skipped": 0,
        "reviewed_no_usable_insight": len(args.reviewed_quality),
        "skipped_good": 0,
        "skipped_no_text": 0,
        "failures": [],
        "stopped_reason": "",
    }
    _write_report_atomic(args, report, started_at, status="running")
    if conn is None:
        report["ok"] = False
        report["success"] = False
        report["stopped_reason"] = "database_unavailable"
        report["failures"].append(
            {
                "scope": args.scope,
                "error": "database_unavailable",
            }
        )
        _write_report_atomic(args, report, started_at, status="failed")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
        return 1
    try:
        if args.scope in {"court", "both"}:
            _process_court(jc, conn, args, report, started_at)
        if args.scope in {"archive", "both"} and not report.get("stopped_reason"):
            _process_archive(jc, conn, args, report, started_at)
    finally:
        conn.close()
    report["failures"] = report["failures"][:20]
    if not args.dry_run:
        _save_reviewed_quality(args.reviewed_quality)
    report["reviewed_no_usable_insight"] = len(args.reviewed_quality)
    if args.row_id:
        report["cursor_unchanged"] = True
    else:
        next_start_id = int(report.get("next_start_id") or args.start_id)
        if not int(report.get("court_candidates") or 0) and args.scope in {"court", "both"}:
            # The cursor reached the end.  Wrap only for the next scheduled pass;
            # do not repeat rejected rows inside this run.
            next_start_id = 1
            report["cursor_wrapped"] = True
        _save_resume_cursor(next_start_id, report)
    report["success"] = not bool(report.get("failed"))
    final_status = "completed" if report["success"] else "completed_with_failures"
    _write_report_atomic(args, report, started_at, status=final_status)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    lock_handle.close()
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
