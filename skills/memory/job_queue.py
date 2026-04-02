"""
SQLite-backed persistent job queue for MAGI.

Replaces JSON-file-per-job pattern with a single SQLite DB (WAL mode).
Supports: enqueue, claim, complete, fail, abandon, resume, cleanup.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("JobQueue")

_DB_DIR = os.path.join(
    os.environ.get("MAGI_DATA_DIR", "").strip()
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".agent"),
    "jobs",
)

_DB_PATH = os.path.join(_DB_DIR, "job_queue.db")

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    job_type     TEXT NOT NULL DEFAULT 'attachment',
    status       TEXT NOT NULL DEFAULT 'queued',
    platform     TEXT NOT NULL DEFAULT '',
    user_id      TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL DEFAULT 'user',
    user_text    TEXT NOT NULL DEFAULT '',
    chat_id      TEXT NOT NULL DEFAULT '',
    reply_to_message_id INTEGER,
    payload      TEXT NOT NULL DEFAULT '{}',
    result       TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0,
    worker_pid   INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs (created_at);
"""

_init_lock = threading.Lock()
_db_initialized = False


def _open_conn() -> sqlite3.Connection:
    """Open a fresh SQLite connection (caller MUST close it)."""
    global _db_initialized
    os.makedirs(_DB_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    if not _db_initialized:
        with _init_lock:
            if not _db_initialized:
                conn.executescript(_CREATE_TABLE)
                _db_initialized = True
    return conn


@contextmanager
def _get_conn():
    """Context manager that opens and closes a SQLite connection."""
    conn = _open_conn()
    try:
        yield conn
    finally:
        conn.close()


def _now() -> float:
    return time.time()


def _row_to_dict(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {}
    d = dict(row)
    # Deserialize payload JSON
    try:
        d["payload"] = json.loads(d.get("payload") or "{}")
    except Exception:
        d["payload"] = {}
    return d


# ── Public API ────────────────────────────────────────────────────────


def enqueue(
    *,
    job_type: str = "attachment",
    platform: str = "LINE",
    user_id: str = "",
    role: str = "user",
    user_text: str = "",
    chat_id: str = "",
    reply_to_message_id: int | None = None,
    payload: dict | None = None,
) -> str:
    """Create a new job and return its ID."""
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO jobs
               (id, job_type, status, platform, user_id, role, user_text,
                chat_id, reply_to_message_id, payload, attempts,
                created_at, updated_at)
               VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (
                job_id,
                job_type,
                str(platform or "LINE"),
                str(user_id or "").strip(),
                str(role or "user"),
                str(user_text or ""),
                str(chat_id or "").strip(),
                int(reply_to_message_id or 0) or None,
                json.dumps(payload or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()
    logger.info("Enqueued job %s type=%s platform=%s", job_id, job_type, platform)
    return job_id


def read(job_id: str) -> dict:
    """Read a job by ID. Returns empty dict if not found."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row)


def update_payload(job_id: str, patch: dict | None = None) -> dict:
    """Merge payload JSON for a job and return the updated row."""
    current = read(job_id)
    if not current:
        return {}
    payload = current.get("payload") if isinstance(current.get("payload"), dict) else {}
    if isinstance(patch, dict):
        payload.update(patch)
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), now, job_id),
        )
        conn.commit()
    return read(job_id)


def claim(job_id: str) -> bool:
    """
    Atomically claim a job for processing.
    Returns True if successfully claimed, False if already running.
    """
    now = _now()
    pid = os.getpid()
    with _get_conn() as conn:
        cur = conn.execute(
            """UPDATE jobs
               SET status = 'running',
                   worker_pid = ?,
                   attempts = attempts + 1,
                   started_at = ?,
                   updated_at = ?
               WHERE id = ? AND status IN ('queued', 'running')
                 AND (worker_pid = 0 OR worker_pid = ?)""",
            (pid, now, now, job_id, pid),
        )
        conn.commit()
        return cur.rowcount > 0


def complete(job_id: str, result: str = "") -> None:
    """Mark a job as successfully completed."""
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """UPDATE jobs
               SET status = 'done', result = ?, error = '',
                   finished_at = ?, updated_at = ?, worker_pid = 0
               WHERE id = ?""",
            (str(result or "")[:4000], now, now, job_id),
        )
        conn.commit()


def fail(job_id: str, error: str = "") -> None:
    """Mark a job as failed."""
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """UPDATE jobs
               SET status = 'failed', error = ?,
                   finished_at = ?, updated_at = ?, worker_pid = 0
               WHERE id = ?""",
            (str(error or "")[:2000], now, now, job_id),
        )
        conn.commit()


def abandon(job_id: str, reason: str = "") -> None:
    """Mark a job as abandoned (exceeded max retries)."""
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """UPDATE jobs
               SET status = 'abandoned', error = ?,
                   finished_at = ?, updated_at = ?, worker_pid = 0
               WHERE id = ?""",
            (str(reason or "exceeded max attempts")[:2000], now, now, job_id),
        )
        conn.commit()


def list_by_status(*statuses: str, limit: int = 100) -> list[dict]:
    """List jobs by status(es), ordered by creation time."""
    with _get_conn() as conn:
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at LIMIT ?",
            (*statuses, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_all(limit: int = 200) -> list[dict]:
    """List all jobs, most recent first."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def count_by_status() -> dict[str, int]:
    """Return {status: count} summary."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def recover_stale_running(max_attempts: int = 3) -> tuple[int, int]:
    """
    Recover jobs stuck in 'running' whose worker PID is dead.
    Returns (resumed_count, abandoned_count).
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, attempts, worker_pid FROM jobs WHERE status IN ('queued', 'running')"
        ).fetchall()

        resumed = 0
        abandoned_count = 0
        now = _now()

        for row in rows:
            job_id = row["id"]
            attempts = row["attempts"]
            worker_pid = row["worker_pid"]

            # If running with a live worker, skip
            if row["worker_pid"] and _pid_alive(worker_pid):
                continue

            if attempts >= max_attempts:
                conn.execute(
                    "UPDATE jobs SET status='abandoned', error=?, updated_at=?, worker_pid=0 WHERE id=?",
                    (f"exceeded {max_attempts} attempts", now, job_id),
                )
                abandoned_count += 1
            else:
                # Reset to queued for retry
                conn.execute(
                    "UPDATE jobs SET status='queued', worker_pid=0, updated_at=? WHERE id=?",
                    (now, job_id),
                )
                resumed += 1

        conn.commit()
    if resumed or abandoned_count:
        logger.info("Job recovery: resumed=%d abandoned=%d", resumed, abandoned_count)
    return resumed, abandoned_count


def cleanup_old(days: int = 30) -> int:
    """Delete completed/failed/abandoned jobs older than N days."""
    cutoff = _now() - (days * 86400)
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'failed', 'abandoned') AND created_at < ?",
            (cutoff,),
        )
        conn.commit()
        deleted = cur.rowcount
    if deleted:
        logger.info("Cleaned up %d old jobs (> %d days)", deleted, days)
    return deleted


def stats() -> dict:
    """Return job queue statistics for health check."""
    counts = count_by_status()
    total = sum(counts.values())
    active = counts.get("queued", 0) + counts.get("running", 0)
    return {"total": total, "active": active, "by_status": counts}
