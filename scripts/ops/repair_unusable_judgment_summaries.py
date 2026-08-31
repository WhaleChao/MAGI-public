#!/usr/bin/env python3
"""Repair or remove unusable judgment-summary payloads safely.

This command deliberately treats the judgment source row and its summary as
different assets:

* a source-bound replacement is written when deterministic extraction passes;
* otherwise the bad summary payload is cleared and the source row is queued for
  NVIDIA review;
* a source hash already marked as having no reusable opinion is cleared without
  retrying; and
* derived ``legal_insights`` placeholder rows are deleted only after backup.

Judgment full text, JID, case number, court and source URL are never deleted.
The operation is read-only unless ``--apply`` is supplied.  Every mutation is
preceded by a gzip JSONL backup outside the immutable release.
"""

from __future__ import annotations

import argparse
from magi_v3 import fcntl_compat as fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.domains.judgment_summary_quality import (  # noqa: E402
    _GENERIC_REASON_RE,
    build_extractive_practice_summary,
    evaluate_practice_summary,
    infer_case_issue,
)
from api.osc.insight_filters import (  # noqa: E402
    is_non_extractable_legal_insight,
    non_extractable_legal_insight_sql_where,
)
from api.runtime_paths import get_runtime_dir  # noqa: E402
from scripts.ops import judgment_summary_staged_backfill as staged  # noqa: E402
from scripts.ops import resummary_legacy_judgments_quality as legacy  # noqa: E402


DEFAULT_REPORT = get_runtime_dir() / "judgment_summary_cleanup_latest.json"
DEFAULT_BACKUP_ROOT = get_runtime_dir() / "backups" / "judgment_summary_cleanup"
APPLY_CONFIRMATION_PHRASE = "CLEAR-UNUSABLE-SUMMARIES"


def _source_sha(text: object) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _append_backup(handle: Any, *, action: str, table: str, row: dict[str, Any]) -> None:
    handle.write(
        json.dumps(
            {
                "backed_up_at": datetime.now().astimezone().isoformat(),
                "action": action,
                "table": table,
                "row": row,
            },
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
        + "\n"
    )
    handle.flush()


def _court_backup_row(row: dict[str, Any], *, reason: str) -> dict[str, Any]:
    """Back up the changed payload without duplicating immutable full text."""

    out = dict(row)
    source = str(out.pop("full_text", "") or "")
    out["full_text_sha256"] = _source_sha(source)
    out["full_text_chars"] = len(source)
    out["cleanup_reason"] = reason
    return out


def _court_quality(row: dict[str, Any]) -> tuple[bool, str, int]:
    summary = str(row.get("summary") or "").strip()
    source = str(row.get("full_text") or "").strip()
    issue = infer_case_issue(
        source,
        str(row.get("case_number") or ""),
        str(row.get("case_type") or ""),
    ) if source else str(row.get("case_type") or "")
    if not summary:
        return True, "empty_payload", 0
    if not source:
        reason = "non_extractable_without_source" if is_non_extractable_legal_insight(summary) else "missing_source"
        return False, reason, 0
    quality = evaluate_practice_summary(summary, source, issue)
    return bool(quality.ok and quality.score >= 70), quality.reason or "score_below_70", int(quality.score)


def _fetch_court_batch(conn: Any, *, after_id: int, batch_size: int) -> list[dict[str, Any]]:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, jid, court_name, case_number, case_type, judgment_date,
               summary, full_text, source_url, crawled_at, tenant_id
        FROM court_judgments
        WHERE id > %s AND TRIM(COALESCE(summary, '')) <> ''
        ORDER BY id ASC
        LIMIT %s
        """,
        (int(after_id), int(batch_size)),
    )
    rows = list(cur.fetchall() or [])
    cur.close()
    return rows


def _delete_placeholder_legal_insights(conn: Any, backup: Any, *, apply: bool) -> int:
    normalized = (
        "REPLACE(REPLACE(REPLACE(REPLACE(CONCAT_WS('', "
        "court_reference, insight_text, document_name, case_reason, raw_text"
        "), ' ', ''), '\\n', ''), '\\r', ''), '\\t', '')"
    )
    where, params = non_extractable_legal_insight_sql_where(normalized)
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT * FROM legal_insights WHERE " + where + " ORDER BY id ASC",
        params,
    )
    rows = list(cur.fetchall() or [])
    cur.close()
    if not apply or not rows:
        return len(rows)
    for row in rows:
        _append_backup(backup, action="delete_derived_placeholder", table="legal_insights", row=row)
    ids = [int(row["id"]) for row in rows]
    deleted = 0
    cur = conn.cursor()
    for start in range(0, len(ids), 250):
        chunk = ids[start : start + 250]
        cur.execute(
            f"DELETE FROM legal_insights WHERE id IN ({','.join(['%s'] * len(chunk))})",
            tuple(chunk),
        )
        deleted += int(cur.rowcount or 0)
    conn.commit()
    cur.close()
    return deleted


def _clear_degraded_archive_payloads(conn: Any, backup: Any, *, apply: bool) -> int:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT * FROM judgment_archive
        WHERE TRIM(COALESCE(summary_text, '')) <> ''
          AND (
            COALESCE(is_degraded, 0)=1
            OR CHAR_LENGTH(summary_text) < 80
            OR summary_text NOT LIKE '%## 實務見解%'
            OR summary_text LIKE '%抽取式快篩%'
            OR summary_text LIKE '%請您提供%'
            OR summary_text LIKE '%無可擷取%'
          )
        ORDER BY id ASC
        """
    )
    rows = list(cur.fetchall() or [])
    cur.close()
    if not apply or not rows:
        return len(rows)
    for row in rows:
        _append_backup(backup, action="clear_unusable_summary", table="judgment_archive", row=row)
    ids = [int(row["id"]) for row in rows]
    changed = 0
    cur = conn.cursor()
    for start in range(0, len(ids), 250):
        chunk = ids[start : start + 250]
        cur.execute(
            f"UPDATE judgment_archive SET summary_text=NULL, is_degraded=1 "
            f"WHERE id IN ({','.join(['%s'] * len(chunk))})",
            tuple(chunk),
        )
        changed += int(cur.rowcount or 0)
    conn.commit()
    cur.close()
    return changed


def _queue_payloads(queue: dict[str, dict[str, Any]], rows: Iterable[tuple[dict[str, Any], str, str]]) -> None:
    for row, issue, reason in rows:
        staged._queue_row(
            queue,
            row,
            issue=issue,
            source_sha256=_source_sha(row.get("full_text")),
            reason=reason,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-token", default="")
    parser.add_argument("--scan-limit", type=int, default=0, help="0 means all non-empty court summaries")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--json-out", default=str(DEFAULT_REPORT))
    parser.add_argument("--backup-root", default=str(DEFAULT_BACKUP_ROOT))
    args = parser.parse_args()
    if args.apply and args.confirm_token != APPLY_CONFIRMATION_PHRASE:
        raise SystemExit(
            f"--apply requires --confirm-token {APPLY_CONFIRMATION_PHRASE}"
        )

    staged.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = staged.LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        print(json.dumps({"ok": True, "success": True, "status": "already_running"}))
        return 0

    jc = legacy._load_judgment_action()
    conn = jc._get_db()
    if not conn:
        raise SystemExit("database_unavailable")

    started_at = datetime.now().astimezone()
    run_dir = Path(args.backup_root).expanduser() / started_at.strftime("%Y%m%d_%H%M%S")
    backup_path = run_dir / "mutated_rows.jsonl.gz"
    if args.apply:
        run_dir.mkdir(parents=True, exist_ok=False)
        backup_handle: Any = gzip.open(backup_path, "wt", encoding="utf-8")
    else:
        backup_handle = None

    queue = staged._read_rows(staged.QUEUE_PATH)
    reviewed = legacy._load_reviewed_quality()
    stats: dict[str, Any] = {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "apply": bool(args.apply),
        "scanned_nonempty": 0,
        "already_usable": 0,
        "deterministic_reextracted": 0,
        "would_reextract": 0,
        "queued_for_nvidia": 0,
        "terminal_cleared": 0,
        "invalid_cleared": 0,
        "missing_source_cleared": 0,
        "quality_reasons": {},
        "archive_payloads_cleared": 0,
        "legal_placeholder_rows_deleted": 0,
        "source_rows_deleted": 0,
        "backup_path": str(backup_path) if args.apply else "",
    }

    after_id = 0
    processed = 0
    try:
        while True:
            remaining = int(args.scan_limit) - processed if args.scan_limit > 0 else int(args.batch_size)
            if args.scan_limit > 0 and remaining <= 0:
                break
            batch = _fetch_court_batch(
                conn,
                after_id=after_id,
                batch_size=min(max(1, int(args.batch_size)), remaining if args.scan_limit > 0 else int(args.batch_size)),
            )
            if not batch:
                break
            after_id = int(batch[-1]["id"])
            processed += len(batch)
            stats["scanned_nonempty"] += len(batch)
            mutations: list[tuple[str, dict[str, Any], str, str]] = []
            queue_rows: list[tuple[dict[str, Any], str, str]] = []
            for row in batch:
                usable, reason, _score = _court_quality(row)
                if usable:
                    stats["already_usable"] += 1
                    continue
                reasons = stats["quality_reasons"]
                reasons[reason] = int(reasons.get(reason) or 0) + 1
                source = str(row.get("full_text") or "").strip()
                source_sha = _source_sha(source)
                reviewed_row = reviewed.get(str(int(row["id"])), {})
                if source and str(reviewed_row.get("source_sha256") or "") == source_sha:
                    mutations.append(("clear_terminal", row, "", str(reviewed_row.get("reason") or reason)))
                    stats["terminal_cleared"] += 1
                    continue
                issue = jc.infer_case_issue(
                    source,
                    str(row.get("case_number") or ""),
                    str(row.get("case_type") or ""),
                ) if source else str(row.get("case_type") or "")
                replacement = build_extractive_practice_summary(source, issue) if len(source) >= 1200 else ""
                replacement_quality = evaluate_practice_summary(replacement, source, issue) if replacement else None
                if (
                    replacement
                    and replacement_quality is not None
                    and replacement_quality.ok
                    and replacement_quality.score >= 70
                    and not _GENERIC_REASON_RE.fullmatch(str(issue or ""))
                ):
                    mutations.append(("replace", row, replacement, "deterministic_source_bound"))
                    stats["would_reextract"] += 1
                    continue
                if source:
                    queue_rows.append((row, issue, reason or "requires_nvidia_review"))
                    mutations.append(("clear_queued", row, "", reason))
                    stats["queued_for_nvidia"] += 1
                else:
                    mutations.append(("clear_missing_source", row, "", reason))
                    stats["missing_source_cleared"] += 1

            if args.apply and mutations:
                _queue_payloads(queue, queue_rows)
                staged._save_queue(queue)
                cur = conn.cursor()
                try:
                    for action, row, replacement, reason in mutations:
                        _append_backup(
                            backup_handle,
                            action=action,
                            table="court_judgments",
                            row=_court_backup_row(row, reason=reason),
                        )
                        if action == "replace":
                            cur.execute(
                                "UPDATE court_judgments SET summary=%s, crawled_at=CURRENT_TIMESTAMP WHERE id=%s AND summary=%s",
                                (replacement, int(row["id"]), row.get("summary")),
                            )
                            stats["deterministic_reextracted"] += int(cur.rowcount or 0)
                        else:
                            cur.execute(
                                "UPDATE court_judgments SET summary=NULL WHERE id=%s AND summary=%s",
                                (int(row["id"]), row.get("summary")),
                            )
                            stats["invalid_cleared"] += int(cur.rowcount or 0)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    cur.close()
            if args.scan_limit > 0 and processed >= args.scan_limit:
                break

        stats["archive_payloads_cleared"] = _clear_degraded_archive_payloads(
            conn,
            backup_handle,
            apply=bool(args.apply),
        )
        stats["legal_placeholder_rows_deleted"] = _delete_placeholder_legal_insights(
            conn,
            backup_handle,
            apply=bool(args.apply),
        )
    finally:
        if backup_handle is not None:
            backup_handle.close()
        conn.close()
        lock.close()

    stats["completed_at"] = datetime.now().astimezone().isoformat()
    stats["success"] = True
    stats["queue_pending_after"] = len(queue)
    _write_json_atomic(Path(args.json_out).expanduser(), stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
