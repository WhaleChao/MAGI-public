#!/usr/bin/env python3
"""Audit and quarantine unusable Judicial Yuan summaries.

The operation is read-only unless --apply is supplied.  Applying the audit
clears only summaries that fail the release's source-support/practical-value
gate, marks their archive rows degraded, clears the matching court summary,
and requeues the corresponding raw document for the corrected pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_collector():
    name = "magi_judgment_collector_quality_audit"
    path = ROOT / "skills" / "judgment-collector" / "action.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load judgment collector: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _atomic_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)


def _start_transaction(conn) -> None:
    """Start a transaction across supported MySQL connector variants."""

    if bool(getattr(conn, "in_transaction", False)):
        return
    start = getattr(conn, "start_transaction", None)
    if callable(start):
        start()
        return
    begin = getattr(conn, "begin", None)
    if callable(begin):
        begin()
        return
    cursor = conn.cursor()
    try:
        cursor.execute("START TRANSACTION")
    finally:
        cursor.close()


def _raw_json_basenames(row: dict, jid_slugger) -> list[str]:
    names: list[str] = []
    source_path = Path(str(row.get("full_text_path") or ""))
    if source_path.name:
        names.append(source_path.with_suffix(".json").name)
    source_jid = str(row.get("source_jid") or "").strip()
    if source_jid:
        names.append(jid_slugger(source_jid) + ".json")
    return list(dict.fromkeys(name for name in names if name))


def _source_text_for_row(row: dict) -> tuple[str, str]:
    """Return the best source text without trusting a stale cache path.

    Judicial API cache files are legitimately moved by storage cleanup.  The
    canonical ``court_judgments.full_text`` copy remains source-verifiable and
    must be used before declaring an archived summary unverifiable.
    """

    source_path = Path(str(row.get("full_text_path") or ""))
    if source_path.is_file():
        return source_path.read_text(encoding="utf-8", errors="replace"), "cache_file"
    court_text = str(row.get("court_full_text") or "").strip()
    if court_text:
        return court_text, "court_judgments"
    return "", "missing"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--include-degraded-empty",
        action="store_true",
        help="also requeue previously quarantined empty degraded rows",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args()

    collector = _load_collector()
    conn = collector._get_db()
    if not conn:
        raise SystemExit("database_unavailable")
    cursor = conn.cursor(dictionary=True)
    summary_filter = (
        "(TRIM(COALESCE(ja.summary_text, '')) <> '' "
        "OR (COALESCE(ja.is_degraded, 0)=1 AND TRIM(COALESCE(ja.summary_text, ''))=''))"
        if args.include_degraded_empty
        else "TRIM(COALESCE(ja.summary_text, '')) <> ''"
    )
    sql = (
        "SELECT ja.id, ja.source_jid, ja.case_reason, ja.full_text_path, "
        "ja.summary_text, COALESCE(ja.is_degraded, 0) AS is_degraded "
        "FROM judgment_archive ja "
        "WHERE ja.source = 'judicial_api' "
        f"AND {summary_filter} "
        "ORDER BY ja.id ASC"
    )
    if args.limit > 0:
        sql += " LIMIT %s"
        cursor.execute(sql, (args.limit,))
    else:
        cursor.execute(sql)
    rows = cursor.fetchall() or []

    # The archive and court tables have different legacy utf8mb4 collations,
    # so a direct column-to-column JOIN can fail and a binary JOIN forces an
    # expensive full-table comparison.  Resolve only the JIDs whose cache file
    # moved, in indexed bounded batches; parameter literals are coerced to the
    # target column collation safely.
    missing_jids = list(
        dict.fromkeys(
            str(row.get("source_jid") or "").strip()
            for row in rows
            if str(row.get("source_jid") or "").strip()
            and not Path(str(row.get("full_text_path") or "")).is_file()
        )
    )
    court_text_by_jid: dict[str, str] = {}
    for start in range(0, len(missing_jids), 500):
        chunk = missing_jids[start : start + 500]
        placeholders = ",".join(["%s"] * len(chunk))
        cursor.execute(
            f"SELECT jid, full_text FROM court_judgments WHERE jid IN ({placeholders})",
            tuple(chunk),
        )
        for court_row in cursor.fetchall() or []:
            jid = str(court_row.get("jid") or "").strip()
            if jid:
                court_text_by_jid[jid] = str(court_row.get("full_text") or "")
    for row in rows:
        row["court_full_text"] = court_text_by_jid.get(
            str(row.get("source_jid") or "").strip(),
            "",
        )

    state_path = Path(collector.JDG_API_PROCESS_STATE_PATH)
    state = collector._load_json_file(str(state_path), {"processed": {}})
    processed = dict((state or {}).get("processed") or {})
    raw_by_basename = {Path(rel).name: rel for rel in processed}

    accepted = 0
    rejected = 0
    unverifiable = 0
    changed_archive_rows = 0
    changed_court_rows = 0
    requeued = 0
    reasons: dict[str, int] = {}
    source_origins: dict[str, int] = {}
    samples: list[dict] = []
    rejected_rows: list[tuple[dict, str, str]] = []
    backup_path = ""
    backup_sha256 = ""

    for row in rows:
        source_text, source_origin = _source_text_for_row(row)
        source_origins[source_origin] = source_origins.get(source_origin, 0) + 1
        if not source_text:
            unverifiable += 1
            reasons["missing_source_text"] = reasons.get("missing_source_text", 0) + 1
            continue
        summary = str(row.get("summary_text") or "")
        reason = (
            "previously_quarantined"
            if not summary.strip() and int(row.get("is_degraded") or 0) == 1
            else collector._summary_practical_value_failure(
                summary,
                source_text,
                str(row.get("case_reason") or ""),
            )
        )
        if not reason:
            accepted += 1
            continue
        rejected += 1
        reason_code = str(reason).split(":", 1)[0]
        reasons[reason_code] = reasons.get(reason_code, 0) + 1
        source_jid = str(row.get("source_jid") or "").strip()
        rejected_rows.append((row, reason, source_jid))
        if len(samples) < 20:
            samples.append(
                {
                    "archive_id": row.get("id"),
                    "source_jid": source_jid,
                    "reason": str(reason)[:240],
                    "summary_preview": summary[:160],
                }
            )

    if args.apply and rejected_rows:
        runtime_dir = Path(os.environ.get("MAGI_RUNTIME_DIR") or ROOT / ".runtime").expanduser()
        backup = runtime_dir / "backups" / "judgment_quality_audit" / (
            "rejected_summaries_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + ".jsonl"
        )
        _atomic_jsonl(
            backup,
            [
                {
                    "archive_id": row.get("id"),
                    "source_jid": source_jid,
                    "case_reason": row.get("case_reason"),
                    "reason": reason,
                    "summary_text": row.get("summary_text"),
                }
                for row, reason, source_jid in rejected_rows
            ],
        )
        backup_path = str(backup)
        backup_sha256 = _sha256(backup)
        try:
            _start_transaction(conn)
            for row, _reason, source_jid in rejected_rows:
                cursor.execute(
                    "UPDATE judgment_archive "
                    "SET summary_text='', is_degraded=1 "
                    "WHERE id=%s AND summary_text=%s",
                    (row["id"], row["summary_text"]),
                )
                changed_archive_rows += int(cursor.rowcount or 0)
                if source_jid:
                    cursor.execute(
                        "UPDATE court_judgments SET summary='' "
                        "WHERE jid=%s AND summary=%s",
                        (source_jid, row["summary_text"]),
                    )
                    changed_court_rows += int(cursor.rowcount or 0)
                    rel = next(
                        (
                            raw_by_basename[name]
                            for name in _raw_json_basenames(
                                row, collector._jid_slug
                            )
                            if name in raw_by_basename
                        ),
                        None,
                    )
                    if rel and rel in processed:
                        processed.pop(rel, None)
                        requeued += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        state["processed"] = processed
        state["quality_audit"] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "release_root": str(ROOT),
            "requeued": requeued,
            "reasons": reasons,
        }
        collector._save_json_file(str(state_path), state)

    cursor.close()
    conn.close()
    payload = {
        "schema_version": 1,
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_root": str(ROOT),
        "apply": bool(args.apply),
        "include_degraded_empty": bool(args.include_degraded_empty),
        "scanned": len(rows),
        "accepted": accepted,
        "rejected": rejected,
        "unverifiable": unverifiable,
        "reasons": reasons,
        "source_origins": source_origins,
        "changed_archive_rows": changed_archive_rows,
        "changed_court_rows": changed_court_rows,
        "requeued": requeued,
        "backup_path": backup_path,
        "backup_sha256": backup_sha256,
        "process_state_path": str(state_path),
        "process_state_remaining": len(processed),
        "samples": samples,
    }
    output = Path(args.json_out)
    _atomic_json(output, payload)
    payload["evidence_path"] = str(output)
    payload["evidence_sha256"] = _sha256(output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
