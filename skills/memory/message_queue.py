"""SQLite-backed inbound message queue for at-least-once delivery.

Modelled on skills/memory/job_queue.py.  Every webhook handler persists
the message *before* returning OK, then the background worker claims /
completes / fails it.  On daemon restart, stale "processing" rows are
reset to "pending" for automatic retry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid

logger = logging.getLogger("MessageQueue")

_DB_DIR = os.path.join(
    os.environ.get("MAGI_DATA_DIR", "").strip()
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".agent"),
    "mq",
)

_DB_PATH = os.path.join(_DB_DIR, "message_queue.db")

_CREATE_SQL = """\
CREATE TABLE IF NOT EXISTS inbound_messages (
    id             TEXT PRIMARY KEY,
    platform       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    user_id        TEXT NOT NULL,
    user_text      TEXT NOT NULL DEFAULT '',
    role           TEXT NOT NULL DEFAULT 'user',
    channel_id     TEXT NOT NULL DEFAULT '',
    reply_token    TEXT DEFAULT '',
    chat_id        TEXT DEFAULT '',
    attachment     TEXT DEFAULT '{}',
    correlation_id TEXT DEFAULT '',
    error          TEXT DEFAULT '',
    attempts       INTEGER DEFAULT 0,
    max_attempts   INTEGER DEFAULT 3,
    worker_pid     INTEGER DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    finished_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_inbound_status ON inbound_messages(status);
CREATE INDEX IF NOT EXISTS idx_inbound_created ON inbound_messages(created_at);
"""

_init_lock = threading.Lock()


# -- helpers ----------------------------------------------------------------

def _now() -> float:
    return time.time()


def _row_to_dict(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {}
    return dict(row)


# -- connection management --------------------------------------------------

class _ConnectionProxy:
    """Proxy that behaves like a SQLite connection and a context manager."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._conn.close()
        return False


def _open_conn(db_path: str | None = None) -> sqlite3.Connection:
    """Open a fresh SQLite connection (caller MUST close it)."""
    path = db_path or _DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


# -- MessageQueue class -----------------------------------------------------

class MessageQueue:
    def __init__(self, db_path: str | None = None):
        self._db = db_path or _DB_PATH
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self._db), exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_CREATE_SQL)

    def _conn(self) -> _ConnectionProxy:
        return _ConnectionProxy(_open_conn(self._db))

    # ---- public API -------------------------------------------------------

    def enqueue(
        self,
        platform: str,
        user_id: str,
        user_text: str,
        role: str = "user",
        channel_id: str = "",
        reply_token: str = "",
        chat_id: str = "",
        attachment: str | None = None,
        correlation_id: str = "",
    ) -> str:
        """Persist message, return msg_id.

        Dedup: same (platform, user_id, text_hash) within a 5-second bucket
        is silently skipped and returns the existing msg_id.
        """
        now = _now()
        text_hash = hashlib.sha1(
            (user_text or "").encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        bucket = int(now) // 5  # 5-second window

        with self._lock:
            with self._conn() as conn:
                # Dedup check
                row = conn.execute(
                    """SELECT id FROM inbound_messages
                       WHERE platform = ? AND user_id = ?
                         AND substr(id, -16) = ?
                         AND created_at >= ?""",
                    (platform, user_id, text_hash, now - 5),
                ).fetchone()
                if row:
                    logger.debug("MQ dedup hit: %s", row["id"])
                    return row["id"]

                msg_id = f"{int(now)}_{uuid.uuid4().hex[:8]}_{text_hash}"
                conn.execute(
                    """INSERT INTO inbound_messages
                       (id, platform, status, user_id, user_text, role,
                        channel_id, reply_token, chat_id, attachment,
                        correlation_id, attempts, created_at, updated_at)
                       VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (
                        msg_id,
                        platform,
                        str(user_id or ""),
                        str(user_text or ""),
                        str(role or "user"),
                        str(channel_id or ""),
                        str(reply_token or ""),
                        str(chat_id or ""),
                        str(attachment or "{}"),
                        str(correlation_id or ""),
                        now,
                        now,
                    ),
                )
                conn.commit()
        logger.info("MQ enqueued %s platform=%s user=%s", msg_id, platform, user_id)
        return msg_id

    def claim(self, msg_id: str) -> dict | None:
        """Atomically set status='processing', worker_pid, attempts+=1.

        Returns message dict or None if not claimable.
        """
        now = _now()
        pid = os.getpid()
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute(
                    """UPDATE inbound_messages
                       SET status = 'processing',
                           worker_pid = ?,
                           attempts = attempts + 1,
                           updated_at = ?
                       WHERE id = ? AND status IN ('pending', 'processing')
                         AND (worker_pid = 0 OR worker_pid = ?)""",
                    (pid, now, msg_id, pid),
                )
                conn.commit()
                if cur.rowcount == 0:
                    return None
                row = conn.execute(
                    "SELECT * FROM inbound_messages WHERE id = ?", (msg_id,)
                ).fetchone()
                return _row_to_dict(row)

    def complete(self, msg_id: str) -> None:
        """Mark done."""
        now = _now()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """UPDATE inbound_messages
                       SET status = 'done', finished_at = ?, updated_at = ?,
                           worker_pid = 0, error = ''
                       WHERE id = ?""",
                    (now, now, msg_id),
                )
                conn.commit()

    def fail(self, msg_id: str, error: str = "") -> None:
        """Mark failed.  If attempts < max_attempts, reset to 'pending' for retry.
        Otherwise mark 'abandoned'."""
        now = _now()
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT attempts, max_attempts FROM inbound_messages WHERE id = ?",
                    (msg_id,),
                ).fetchone()
                if not row:
                    return
                if row["attempts"] < row["max_attempts"]:
                    conn.execute(
                        """UPDATE inbound_messages
                           SET status = 'pending', error = ?,
                               updated_at = ?, worker_pid = 0
                           WHERE id = ?""",
                        (str(error or "")[:2000], now, msg_id),
                    )
                else:
                    conn.execute(
                        """UPDATE inbound_messages
                           SET status = 'abandoned', error = ?,
                               finished_at = ?, updated_at = ?, worker_pid = 0
                           WHERE id = ?""",
                        (str(error or "")[:2000], now, now, msg_id),
                    )
                conn.commit()

    def recover_stale(self, stale_seconds: int = 300) -> int:
        """Find messages stuck in 'processing' for > stale_seconds.
        Reset to 'pending' (or 'abandoned' if max attempts reached)."""
        cutoff = _now() - stale_seconds
        recovered = 0
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT id, attempts, max_attempts, worker_pid
                       FROM inbound_messages
                       WHERE status = 'processing' AND updated_at < ?""",
                    (cutoff,),
                ).fetchall()
                now = _now()
                for row in rows:
                    mid = row["id"]
                    wpid = row["worker_pid"]
                    # Skip if worker is still alive
                    if wpid > 0:
                        try:
                            os.kill(wpid, 0)
                            continue  # process alive, not stale
                        except OSError:
                            pass  # process dead
                    if row["attempts"] >= row["max_attempts"]:
                        conn.execute(
                            """UPDATE inbound_messages
                               SET status = 'abandoned',
                                   error = 'stale: exceeded max attempts',
                                   finished_at = ?, updated_at = ?, worker_pid = 0
                               WHERE id = ?""",
                            (now, now, mid),
                        )
                    else:
                        conn.execute(
                            """UPDATE inbound_messages
                               SET status = 'pending', updated_at = ?, worker_pid = 0
                               WHERE id = ?""",
                            (now, mid),
                        )
                    recovered += 1
                conn.commit()
        if recovered:
            logger.info("MQ recovered %d stale messages", recovered)
        return recovered

    def pending_count(self) -> int:
        """Count pending messages."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM inbound_messages WHERE status = 'pending'"
            ).fetchone()
            return row["cnt"] if row else 0

    def cleanup_old(self, days: int = 7) -> int:
        """Delete 'done' messages older than N days."""
        cutoff = _now() - (days * 86400)
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute(
                    "DELETE FROM inbound_messages WHERE status = 'done' AND created_at < ?",
                    (cutoff,),
                )
                conn.commit()
                deleted = cur.rowcount
        if deleted:
            logger.info("MQ cleaned up %d old messages (> %d days)", deleted, days)
        return deleted

    def stats(self) -> dict:
        """Return message queue statistics."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM inbound_messages GROUP BY status"
            ).fetchall()
            by_status = {r["status"]: r["cnt"] for r in rows}
        total = sum(by_status.values())
        active = by_status.get("pending", 0) + by_status.get("processing", 0)
        return {"total": total, "active": active, "by_status": by_status}


# -- Module-level singleton -------------------------------------------------

_mq: MessageQueue | None = None


def get_queue() -> MessageQueue:
    global _mq
    if _mq is None:
        with _init_lock:
            if _mq is None:
                _mq = MessageQueue()
    return _mq
