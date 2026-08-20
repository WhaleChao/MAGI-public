#!/usr/bin/env python3
"""High-throughput, source-bound judgment summary backfill.

The job has two bounded stages:

1. review a small durable queue with NVIDIA's identifier-only selector; and
2. scan a much larger forward batch locally, writing only exact-source
   summaries that pass the strict quality gate and queueing the remainder.

No row is lost when the provider is unavailable: the forward cursor and the
provider review queue are persisted independently.  Provider or selection
failures are operationally deferred rather than reported as completed work.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from magi_v3 import fcntl_compat as fcntl  # noqa: E402

from api.domains.judgment_summary_quality import (  # noqa: E402
    _GENERIC_REASON_RE,
    evaluate_practice_ready_summary,
)
from scripts.ops import resummary_legacy_judgments_quality as legacy  # noqa: E402


RUNTIME_DIR = Path(
    os.environ.get("MAGI_RUNTIME_DIR", "").strip() or ROOT / ".runtime"
).expanduser()
REPORT_PATH = RUNTIME_DIR / "legacy_judgment_resummary_latest.json"
QUEUE_PATH = RUNTIME_DIR / "legacy_judgment_nvidia_review_queue.json"
LOCK_PATH = RUNTIME_DIR / "legacy_judgment_staged_backfill.lock"
BACKUP_DIR = RUNTIME_DIR / "backups" / "judgment_resummary"
NVIDIA_BUDGET_PATH = RUNTIME_DIR / "legacy_judgment_nvidia_daily_budget.json"


def _read_rows(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("rows") if isinstance(payload, dict) else {}
        if isinstance(rows, dict):
            return {
                str(key): dict(value)
                for key, value in rows.items()
                if isinstance(value, dict)
            }
    except Exception:
        pass
    return {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def _write_report(report: dict[str, Any], started: float, *, status: str) -> None:
    report["status"] = status
    report["success"] = status in {"completed", "already_running"}
    report["ok"] = report["success"]
    report["elapsed_sec"] = round(time.monotonic() - started, 2)
    report["updated_at"] = datetime.now().astimezone().isoformat()
    _write_json_atomic(REPORT_PATH, report)


def _save_queue(rows: dict[str, dict[str, Any]]) -> None:
    _write_json_atomic(
        QUEUE_PATH,
        {
            "schema_version": 1,
            "updated_at": datetime.now().astimezone().isoformat(),
            "pending": len(rows),
            "rows": rows,
        },
    )


def _append_backup(path: Path, payload: dict[str, Any]) -> None:
    record = {**payload, "backed_up_at": datetime.now().astimezone().isoformat()}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _source_sha(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _fetch_rows(
    conn: Any,
    *,
    start_id: int = 1,
    limit: int,
    row_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    cur = conn.cursor(dictionary=True)
    sql, params = legacy._court_candidate_sql(
        force_all=bool(row_ids),
        start_id=start_id,
        min_chars=1200,
        # The forward cursor must inspect existing structured summaries too:
        # old source-bound rows can still be unusable for practice because
        # they omit the court's application or score below the live threshold.
        recheck_existing=True,
        row_ids=row_ids,
    )
    rows = legacy._fetch_rows(cur, sql, params, limit=limit)
    cur.close()
    return rows


def _store_summary(
    conn: Any,
    *,
    row: dict[str, Any],
    summary: str,
    source_sha256: str,
    provenance: dict[str, Any],
    backup_path: Path,
) -> None:
    _append_backup(
        backup_path,
        {
            "table": "court_judgments",
            "id": int(row["id"]),
            "jid": str(row.get("jid") or ""),
            "old_summary": str(row.get("summary") or ""),
            "new_summary": summary,
            "source_sha256": source_sha256,
            "summary_provenance": provenance,
        },
    )
    cur = conn.cursor()
    cur.execute(
        "UPDATE court_judgments SET summary=%s, crawled_at=CURRENT_TIMESTAMP WHERE id=%s",
        (summary, int(row["id"])),
    )
    conn.commit()
    cur.close()


def _clear_invalid_summary(
    conn: Any,
    *,
    row: dict[str, Any],
    source_sha256: str,
    reason: str,
    backup_path: Path,
) -> int:
    """Remove only a rejected summary payload while retaining its source."""

    old_summary = str(row.get("summary") or "")
    if not old_summary.strip():
        return 0
    _append_backup(
        backup_path,
        {
            "table": "court_judgments",
            "id": int(row["id"]),
            "jid": str(row.get("jid") or ""),
            "old_summary": old_summary,
            "new_summary": "",
            "source_sha256": source_sha256,
            "summary_provenance": {"stage": "invalid_payload_cleanup", "reason": reason},
        },
    )
    cur = conn.cursor()
    cur.execute(
        "UPDATE court_judgments SET summary=NULL WHERE id=%s AND summary=%s",
        (int(row["id"]), old_summary),
    )
    changed = int(cur.rowcount or 0)
    conn.commit()
    cur.close()
    return changed


def _queue_row(
    queue: dict[str, dict[str, Any]],
    row: dict[str, Any],
    *,
    issue: str,
    source_sha256: str,
    reason: str,
) -> None:
    key = str(int(row["id"]))
    previous = queue.get(key, {})
    if str(previous.get("source_sha256") or "") != source_sha256:
        previous = {}
    queue[key] = {
        "id": int(row["id"]),
        "jid": str(row.get("jid") or ""),
        "case_reason": issue,
        "source_sha256": source_sha256,
        "queued_at": str(previous.get("queued_at") or datetime.now().astimezone().isoformat()),
        "last_local_reason": reason,
        "attempts": int(previous.get("attempts") or 0),
        "last_attempt_at": str(previous.get("last_attempt_at") or ""),
        "next_retry_at": str(previous.get("next_retry_at") or ""),
        "last_provider_error": str(previous.get("last_provider_error") or ""),
    }


def _due_queue_ids(
    queue: dict[str, dict[str, Any]],
    *,
    limit: int,
    now: datetime,
) -> list[int]:
    due: list[tuple[int, str, str, int]] = []
    for value in queue.values():
        rid = int(value.get("id") or 0)
        if rid <= 0:
            continue
        raw_next = str(value.get("next_retry_at") or "")
        try:
            next_at = datetime.fromisoformat(raw_next) if raw_next else None
        except ValueError:
            next_at = None
        if next_at is not None and next_at.timestamp() > now.timestamp():
            continue
        # Give new and repeatedly deferred matters turns alike.  Sorting only
        # by enqueue time lets the oldest provider failure consume every
        # limited NVIDIA window after its retry becomes due.
        due.append((
            int(value.get("attempts") or 0),
            str(value.get("last_selected_at") or ""),
            str(value.get("queued_at") or ""),
            rid,
        ))
    due.sort()
    return [rid for _attempts, _selected, _queued, rid in due[: max(0, int(limit))]]


def _nvidia_budget_remaining(*, requested: int, now: datetime) -> int:
    """Persist a daily provider-call ceiling; no provider is contacted here."""
    try:
        ceiling = max(0, int(os.environ.get("MAGI_NVIDIA_RESUMMARY_DAILY_BUDGET", "24")))
    except (TypeError, ValueError):
        ceiling = 24
    day = now.date().isoformat()
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(NVIDIA_BUDGET_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    used = int(payload.get("used") or 0) if payload.get("day") == day else 0
    return max(0, min(int(requested), ceiling - used))


def _record_nvidia_budget(*, calls: int, now: datetime) -> None:
    if calls <= 0:
        return
    day = now.date().isoformat()
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(NVIDIA_BUDGET_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    used = int(payload.get("used") or 0) if payload.get("day") == day else 0
    _write_json_atomic(NVIDIA_BUDGET_PATH, {"schema_version": 1, "day": day, "used": used + calls})


def _nvidia_resource_deferred() -> str:
    try:
        from scripts.ops import resource_governor

        decision = resource_governor.classify(resource_governor.collect_snapshot())
        if decision.level in {"throttle", "core_only", "critical"}:
            return "resource_pressure:" + decision.level
    except Exception:
        return "resource_check_unavailable"
    return ""


def _terminal_review(
    reviewed: dict[str, dict[str, Any]],
    *,
    row: dict[str, Any],
    issue: str,
    source_sha256: str,
    reason: str,
) -> None:
    rid = int(row["id"])
    reviewed[str(rid)] = {
        "id": rid,
        "jid": str(row.get("jid") or ""),
        "case_reason": issue,
        "reason": reason,
        "source_sha256": source_sha256,
        "reviewed_at": datetime.now().astimezone().isoformat(),
    }


def _review_nvidia_queue(
    jc: Any,
    conn: Any,
    queue: dict[str, dict[str, Any]],
    reviewed: dict[str, dict[str, Any]],
    report: dict[str, Any],
    *,
    limit: int,
    timeout: int,
    backup_path: Path,
) -> None:
    if limit <= 0 or not queue:
        return
    now = datetime.now().astimezone()
    resource_reason = _nvidia_resource_deferred()
    if resource_reason:
        report["nvidia_deferred"] += min(len(queue), limit)
        report["nvidia_defer_reason"] = resource_reason
        return
    allowed = _nvidia_budget_remaining(requested=limit, now=now)
    if allowed <= 0:
        report["nvidia_deferred"] += min(len(queue), limit)
        report["nvidia_defer_reason"] = "daily_provider_budget_exhausted"
        return
    ids = _due_queue_ids(queue, limit=allowed, now=now)
    rows = _fetch_rows(conn, limit=len(ids), row_ids=ids) if ids else []
    by_id = {int(row["id"]): row for row in rows}
    for rid in ids:
        key = str(rid)
        row = by_id.get(rid)
        if row is None:
            queue.pop(key, None)
            report["nvidia_missing_source"] += 1
            continue
        source = str(row.get("full_text") or "")
        queue.setdefault(key, {})["last_selected_at"] = now.isoformat()
        source_sha256 = _source_sha(source)
        issue = jc.infer_case_issue(
            source,
            str(row.get("case_number") or ""),
            str(row.get("case_type") or ""),
        )
        if not legacy._needs_resummary(
            jc,
            row.get("summary"),
            source,
            issue,
            str(row.get("court_name") or ""),
        ):
            queue.pop(key, None)
            report["nvidia_stale_skipped"] += 1
            continue
        from api.domains.judgment_nvidia_summary import summarize_with_nvidia

        result = summarize_with_nvidia(source, issue, timeout_sec=timeout)
        report["provider_calls"] += 1
        _record_nvidia_budget(calls=1, now=now)
        report.setdefault("provider_models", {})
        model = result.model or "unknown"
        report["provider_models"][model] = int(report["provider_models"].get(model) or 0) + 1
        if result.success:
            usable, reason = legacy._new_summary_is_usable(
                jc,
                result.summary,
                source,
                issue,
                str(row.get("court_name") or ""),
            )
            quality = evaluate_practice_ready_summary(
                result.summary,
                source,
                issue,
                str(row.get("court_name") or ""),
            )
            if usable and quality.ok:
                _store_summary(
                    conn,
                    row=row,
                    summary=result.summary,
                    source_sha256=source_sha256,
                    provenance={"stage": "nvidia_selector", **result.audit_dict()},
                    backup_path=backup_path,
                )
                queue.pop(key, None)
                reviewed.pop(key, None)
                report["nvidia_updated"] += 1
                report["updated"] += 1
                continue
            error = reason or quality.reason or "nvidia_quality_rejected"
        else:
            error = result.error or "nvidia_unknown_failure"
        if result.reviewed_no_insight:
            _terminal_review(
                reviewed,
                row=row,
                issue=issue,
                source_sha256=source_sha256,
                reason=error,
            )
            queue.pop(key, None)
            report["invalid_payloads_cleared"] += _clear_invalid_summary(
                conn,
                row=row,
                source_sha256=source_sha256,
                reason=error,
                backup_path=backup_path,
            )
            report["nvidia_no_insight"] += 1
            continue
        entry = queue.get(key, {})
        attempts = int(entry.get("attempts") or 0) + 1
        non_provider_failure = not error.startswith("provider:")
        if non_provider_failure and attempts >= 3:
            _terminal_review(
                reviewed,
                row=row,
                issue=issue,
                source_sha256=source_sha256,
                reason=f"repeated_source_bound_rejection:{error}",
            )
            queue.pop(key, None)
            report["invalid_payloads_cleared"] += _clear_invalid_summary(
                conn,
                row=row,
                source_sha256=source_sha256,
                reason=f"repeated_source_bound_rejection:{error}",
                backup_path=backup_path,
            )
            report["nvidia_no_insight"] += 1
            continue
        delay_minutes = min(24 * 60, 30 * (2 ** min(attempts - 1, 5)))
        entry.update(
            {
                "id": rid,
                "jid": str(row.get("jid") or ""),
                "case_reason": issue,
                "source_sha256": source_sha256,
                "attempts": attempts,
                "last_attempt_at": now.isoformat(),
                "next_retry_at": (now + timedelta(minutes=delay_minutes)).isoformat(),
                "last_provider_error": error,
            }
        )
        queue[key] = entry
        report["nvidia_deferred"] += 1


def _scan_local(
    jc: Any,
    conn: Any,
    queue: dict[str, dict[str, Any]],
    reviewed: dict[str, dict[str, Any]],
    report: dict[str, Any],
    *,
    limit: int,
    min_score: int,
    backup_path: Path,
    dry_run: bool = False,
) -> None:
    start_id = legacy._load_resume_cursor(1)
    rows = _fetch_rows(conn, start_id=start_id, limit=limit)
    report["scan_start_id"] = start_id
    report["local_candidates"] = len(rows)
    next_start_id = start_id
    for row in rows:
        rid = int(row["id"])
        key = str(rid)
        source = str(row.get("full_text") or "")
        source_sha256 = _source_sha(source)
        issue = jc.infer_case_issue(
            source,
            str(row.get("case_number") or ""),
            str(row.get("case_type") or ""),
        )
        next_start_id = rid + 1
        if not legacy._needs_resummary(
            jc,
            row.get("summary"),
            source,
            issue,
            str(row.get("court_name") or ""),
        ):
            queue.pop(key, None)
            reviewed.pop(key, None)
            report["skipped_good"] += 1
            continue
        terminal = reviewed.get(key, {})
        if str(terminal.get("source_sha256") or "") == source_sha256:
            report["terminal_review_skipped"] += 1
            if not dry_run:
                report["invalid_payloads_cleared"] += _clear_invalid_summary(
                    conn,
                    row=row,
                    source_sha256=source_sha256,
                    reason=str(terminal.get("reason") or "terminal_review_no_usable_insight"),
                    backup_path=backup_path,
                )
            continue
        queued = queue.get(key, {})
        if str(queued.get("source_sha256") or "") == source_sha256:
            report["already_queued"] += 1
            if not dry_run:
                report["invalid_payloads_cleared"] += _clear_invalid_summary(
                    conn,
                    row=row,
                    source_sha256=source_sha256,
                    reason=str(queued.get("last_local_reason") or "already_queued_for_review"),
                    backup_path=backup_path,
                )
            continue
        summary = jc._extractive_judgment_summary(source, issue, max_chars=1800)
        quality = evaluate_practice_ready_summary(
            summary,
            source,
            issue,
            str(row.get("court_name") or ""),
            min_score=min_score,
        )
        generic_issue = bool(_GENERIC_REASON_RE.fullmatch(str(issue or "")))
        if quality.ok and quality.score >= min_score and not generic_issue:
            if dry_run:
                report["local_would_update"] += 1
            else:
                _store_summary(
                    conn,
                    row=row,
                    summary=summary,
                    source_sha256=source_sha256,
                    provenance={
                        "stage": "deterministic_source_bound",
                        "quality_score": quality.score,
                        "quality_reason": quality.reason,
                        "source_supported_spans": quality.source_supported_spans,
                    },
                    backup_path=backup_path,
                )
                report["local_updated"] += 1
                report["updated"] += 1
            continue
        reason = quality.reason or (
            "generic_issue_requires_nvidia_review" if generic_issue else "local_score_below_threshold"
        )
        _queue_row(
            queue,
            row,
            issue=issue,
            source_sha256=source_sha256,
            reason=reason,
        )
        report["local_queued"] += 1
        if not dry_run:
            report["invalid_payloads_cleared"] += _clear_invalid_summary(
                conn,
                row=row,
                source_sha256=source_sha256,
                reason=reason,
                backup_path=backup_path,
            )
        report.setdefault("local_reasons", {})
        report["local_reasons"][reason] = int(report["local_reasons"].get(reason) or 0) + 1
    if not rows:
        next_start_id = 1
        report["cursor_wrapped"] = True
    report["next_start_id"] = next_start_id
    if not dry_run:
        legacy._save_resume_cursor(next_start_id, report)


def main() -> int:
    global REPORT_PATH
    default_report_path = REPORT_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-limit", type=int, default=240)
    parser.add_argument("--nvidia-limit", type=int, default=4)
    parser.add_argument("--nvidia-timeout", type=int, default=150)
    parser.add_argument("--local-min-score", type=int, default=80)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-out")
    args = parser.parse_args()
    if args.json_out:
        REPORT_PATH = Path(args.json_out).expanduser()
    elif args.dry_run:
        REPORT_PATH = Path("/tmp/magi_judgment_staged_backfill_dry_run.json")
    else:
        REPORT_PATH = default_report_path
    lock_path = LOCK_PATH
    if args.dry_run:
        lock_path = REPORT_PATH.parent / ".magi_judgment_staged_backfill_dry_run.lock"

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"ok": True, "success": True, "status": "already_running"}))
        lock.close()
        return 0

    started = time.monotonic()
    report: dict[str, Any] = {
        "schema_version": 2,
        "started_at": datetime.now().astimezone().isoformat(),
        "pipeline": "source_bound_staged",
        "scan_limit": max(1, int(args.scan_limit)),
        "nvidia_limit": max(0, int(args.nvidia_limit)),
        "local_min_score": max(60, min(100, int(args.local_min_score))),
        "updated": 0,
        "local_updated": 0,
        "local_would_update": 0,
        "local_queued": 0,
        "already_queued": 0,
        "terminal_review_skipped": 0,
        "skipped_good": 0,
        "nvidia_updated": 0,
        "nvidia_no_insight": 0,
        "nvidia_deferred": 0,
        "nvidia_missing_source": 0,
        "nvidia_stale_skipped": 0,
        "invalid_payloads_cleared": 0,
        "provider_calls": 0,
        "provider_models": {},
        "local_reasons": {},
        "failures": [],
        "dry_run": bool(args.dry_run),
    }
    backup_root = REPORT_PATH.parent / "backups" if args.dry_run else BACKUP_DIR
    backup_path = backup_root / (
        "judgment_staged_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jsonl"
    )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.touch(exist_ok=False)
    report["backup_json"] = str(backup_path)
    queue = _read_rows(QUEUE_PATH)
    reviewed = legacy._load_reviewed_quality()
    try:
        jc = legacy._load_judgment_action()
        conn = jc._get_db()
        if conn is None:
            raise RuntimeError("database_unavailable")
        try:
            if not args.dry_run:
                _review_nvidia_queue(
                    jc,
                    conn,
                    queue,
                    reviewed,
                    report,
                    limit=max(0, int(args.nvidia_limit)),
                    timeout=max(60, int(args.nvidia_timeout)),
                    backup_path=backup_path,
                )
            _scan_local(
                jc,
                conn,
                queue,
                reviewed,
                report,
                limit=max(1, int(args.scan_limit)),
                min_score=report["local_min_score"],
                backup_path=backup_path,
                dry_run=bool(args.dry_run),
            )
        finally:
            conn.close()
        if not args.dry_run:
            _save_queue(queue)
            legacy._save_reviewed_quality(reviewed)
        report["pending_nvidia_review"] = len(queue)
        report["reviewed_no_usable_insight"] = len(reviewed)
        # NVIDIA review, not local discovery, is the usable-summary bottleneck.
        report["first_pass_daily_capacity"] = report["nvidia_limit"] * 96
        _write_report(report, started, status="completed")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        report["failures"].append(
            {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        )
        report["pending_nvidia_review"] = len(queue)
        if not args.dry_run:
            _save_queue(queue)
        _write_report(report, started, status="failed")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
