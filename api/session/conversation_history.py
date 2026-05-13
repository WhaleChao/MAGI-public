from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


_DEFAULT_TTL_DAYS = 7
_DB_PATH = Path(__file__).resolve().parents[2] / ".runtime" / "conversation_history.sqlite3"
_SINGLETON = None
_SINGLETON_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConversationHistoryStore:
    """SQLite-backed short-term conversation history (Layer 1)."""

    def __init__(self, db_path: Optional[str] = None, ttl_days: int = _DEFAULT_TTL_DAYS) -> None:
        self.db_path = str(db_path or _DB_PATH)
        self.ttl_days = max(1, int(ttl_days))
        self._lock = threading.RLock()
        self._ensure_db()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)

    def _ensure_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_history_session_ts "
                "ON conversation_history(session_id, ts)"
            )
            conn.commit()

    def append(self, session_id: str, role: str, content: str, metadata: Optional[dict[str, Any]] = None) -> None:
        text = str(content or "").strip()
        if not session_id or not role or not text:
            return
        payload = json.dumps(dict(metadata or {}), ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO conversation_history(session_id, role, content, ts, metadata) VALUES (?, ?, ?, ?, ?)",
                (str(session_id), str(role), text, _utcnow().isoformat(), payload),
            )
            conn.commit()
        self.purge_expired()

    def last_n(self, session_id: str, n: int = 20) -> list[dict[str, Any]]:
        limit = max(1, int(n))
        self.purge_expired()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, ts, metadata FROM conversation_history "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (str(session_id), limit),
            ).fetchall()
        results = []
        for role, content, ts, metadata_json in reversed(rows):
            try:
                metadata = json.loads(metadata_json or "{}")
            except Exception:
                metadata = {}
            results.append({"role": role, "content": content, "ts": ts, "metadata": metadata})
        return results

    def last_sessions(self, n: int = 3) -> list[str]:
        limit = max(1, int(n))
        self.purge_expired()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, MAX(ts) AS last_ts FROM conversation_history "
                "GROUP BY session_id ORDER BY last_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row[0] for row in rows]

    def clear_session(self, session_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM conversation_history WHERE session_id = ?", (str(session_id),))
            conn.commit()

    def purge_expired(self) -> int:
        cutoff = (_utcnow() - timedelta(days=self.ttl_days)).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM conversation_history WHERE ts < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount or 0)


def get_conversation_history() -> ConversationHistoryStore:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = ConversationHistoryStore()
    return _SINGLETON
