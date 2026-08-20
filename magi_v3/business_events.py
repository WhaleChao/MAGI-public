"""Small, durable event queue for incremental business reconciliation.

The queue deliberately stores only routing metadata.  Documents and message
bodies stay in their owning systems; this ledger merely records that a case
has new evidence and should be reconciled.  SQLite WAL plus a short lease lets
the cron owner recover after a crash without starting another resident daemon.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_CASE_NUMBER = re.compile(r"^20\d{2}-\d{4}$")
_SAFE_TOKEN = re.compile(r"[^a-zA-Z0-9_.:-]+")
_ROUTING_PAYLOAD_KEYS = frozenset({"evidence_kind"})


def default_ledger_path() -> Path:
    configured = os.environ.get("MAGI_BUSINESS_EVENT_LEDGER", "").strip()
    if configured:
        return Path(configured).expanduser()
    shared = os.environ.get("MAGI_SHARED_STATE_DIR", "").strip()
    if shared:
        return Path(shared).expanduser() / "runtime" / "business_events.sqlite3"
    return Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "business_events.sqlite3"


def _safe_token(value: Any, *, limit: int = 80) -> str:
    return _SAFE_TOKEN.sub("_", str(value or "").strip())[:limit]


def _source_digest(value: Any) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return raw.lower()
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


class BusinessEventLedger:
    """Bounded, fail-closed queue with idempotent evidence receipts."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or default_ledger_path()).expanduser()
        if self.path.exists() and self.path.is_symlink():
            raise ValueError("business event ledger must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        for candidate in (
            self.path,
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ):
            if candidate.exists():
                os.chmod(candidate, 0o600)
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS business_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    case_number TEXT NOT NULL DEFAULT '',
                    source_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    priority INTEGER NOT NULL DEFAULT 50,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    not_before REAL NOT NULL,
                    lease_until REAL,
                    result_json TEXT,
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(event_type, domain, case_number, source_sha256)
                );
                CREATE INDEX IF NOT EXISTS business_events_ready
                    ON business_events(status, not_before, priority, created_at);
                """
            )

    def emit(
        self,
        *,
        event_type: str,
        domain: str,
        source: Any,
        case_number: str = "",
        payload: dict[str, Any] | None = None,
        priority: int = 50,
        not_before: float | None = None,
    ) -> dict[str, Any]:
        event_type = _safe_token(event_type)
        domain = _safe_token(domain)
        case_number = str(case_number or "").strip()
        if not event_type or not domain:
            raise ValueError("event_type and domain are required")
        if case_number and not _CASE_NUMBER.fullmatch(case_number):
            raise ValueError("case_number must use YYYY-NNNN")
        digest = _source_digest(source)
        now = time.time()
        safe_payload = {
            _safe_token(key, limit=40): value
            for key, value in (payload or {}).items()
            if isinstance(value, (str, int, float, bool, type(None)))
            and key in _ROUTING_PAYLOAD_KEYS
        }
        encoded = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True)[:2000]
        event_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO business_events
                    (event_id,event_type,domain,case_number,source_sha256,payload_json,
                     priority,status,attempts,not_before,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'queued',0,?,?,?)
                """,
                (
                    event_id,
                    event_type,
                    domain,
                    case_number,
                    digest,
                    encoded,
                    max(0, min(100, int(priority))),
                    float(not_before if not_before is not None else now),
                    now,
                    now,
                ),
            )
            inserted = cursor.rowcount == 1
            row = conn.execute(
                "SELECT event_id,status,attempts FROM business_events "
                "WHERE event_type=? AND domain=? AND case_number=? AND source_sha256=?",
                (event_type, domain, case_number, digest),
            ).fetchone()
            conn.execute("COMMIT")
        return {"event_id": str(row["event_id"]), "status": str(row["status"]), "attempts": int(row["attempts"]), "inserted": inserted}

    def claim(
        self,
        *,
        limit: int = 8,
        lease_seconds: int = 300,
        event_types: tuple[str, ...] = ("case_evidence_changed",),
    ) -> list[dict[str, Any]]:
        now = time.time()
        limit = max(1, min(32, int(limit)))
        lease_until = now + max(30, min(1800, int(lease_seconds)))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE business_events SET status='queued',lease_until=NULL,updated_at=? "
                "WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?",
                (now, now),
            )
            safe_types = tuple(_safe_token(value) for value in event_types if _safe_token(value))
            if not safe_types:
                conn.execute("ROLLBACK")
                return []
            placeholders = ",".join("?" for _ in safe_types)
            rows = conn.execute(
                "SELECT * FROM business_events WHERE status IN ('queued','deferred') "
                f"AND event_type IN ({placeholders}) AND not_before<=? "
                "ORDER BY priority DESC,created_at ASC LIMIT ?",
                (*safe_types, now, limit),
            ).fetchall()
            ids = [str(row["event_id"]) for row in rows]
            for event_id in ids:
                conn.execute(
                    "UPDATE business_events SET status='running',attempts=attempts+1,"
                    "lease_until=?,updated_at=? WHERE event_id=?",
                    (lease_until, now, event_id),
                )
            conn.execute("COMMIT")
        return [
            {
                "event_id": str(row["event_id"]),
                "event_type": str(row["event_type"]),
                "domain": str(row["domain"]),
                "case_number": str(row["case_number"]),
                "source_sha256": str(row["source_sha256"]),
                "payload": json.loads(str(row["payload_json"] or "{}")),
                "attempts": int(row["attempts"]) + 1,
            }
            for row in rows
        ]

    def complete(self, event_id: str, result: dict[str, Any] | None = None) -> None:
        safe_result = {
            _safe_token(key, limit=40): value
            for key, value in (result or {}).items()
            if isinstance(value, (str, int, float, bool, type(None)))
        }
        self._finish(event_id, "succeeded", json.dumps(safe_result, ensure_ascii=False)[:2000], "", 0)

    def defer(self, event_id: str, *, reason_code: str, delay_seconds: int = 900) -> None:
        self._finish(event_id, "deferred", None, _safe_token(reason_code), max(30, min(86400, int(delay_seconds))))

    def fail(self, event_id: str, *, reason_code: str) -> None:
        self._finish(event_id, "failed", None, _safe_token(reason_code), 0)

    def _finish(self, event_id: str, status: str, result: str | None, error: str, delay: int) -> None:
        now = time.time()
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE business_events SET status=?,result_json=?,error_code=?,"
                "not_before=?,lease_until=NULL,updated_at=? WHERE event_id=? AND status='running'",
                (status, result, error, now + delay, now, str(event_id)),
            ).rowcount
        if changed != 1:
            raise ValueError("event is not owned by this worker")

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute("SELECT status,COUNT(*) AS count FROM business_events GROUP BY status").fetchall()
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM business_events WHERE status IN ('queued','deferred','running')"
            ).fetchone()[0]
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "ok": counts.get("failed", 0) == 0,
            "counts": counts,
            "oldest_pending_age_seconds": max(0, int(now - oldest)) if oldest else 0,
        }

    def prune(self, *, keep_days: int = 30, max_rows: int = 20000) -> int:
        cutoff = time.time() - max(1, int(keep_days)) * 86400
        removed = 0
        with self._connect() as conn:
            removed += conn.execute(
                "DELETE FROM business_events WHERE status='succeeded' AND updated_at<?", (cutoff,)
            ).rowcount
            excess = int(conn.execute("SELECT COUNT(*) FROM business_events").fetchone()[0]) - max(1000, int(max_rows))
            if excess > 0:
                removed += conn.execute(
                    "DELETE FROM business_events WHERE event_id IN (SELECT event_id FROM business_events "
                    "WHERE status='succeeded' ORDER BY updated_at ASC LIMIT ?)", (excess,)
                ).rowcount
        return removed


def emit_case_evidence_event(
    *, domain: str, case_number: str, source: Any, evidence_kind: str
) -> dict[str, Any]:
    return BusinessEventLedger().emit(
        event_type="case_evidence_changed",
        domain=domain,
        case_number=case_number,
        source=source,
        payload={"evidence_kind": _safe_token(evidence_kind)},
        priority=80,
    )
