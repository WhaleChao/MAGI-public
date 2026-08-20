"""Process-safe request rate limiting for self-host and SaaS deployments.

The original gateway kept counters in a module-level dictionary.  That state
was lost on restart and was independent in every worker, so adding workers
silently multiplied the configured limit.  This module stores only a SHA-256
client identity in a small SQLite database under the mutable runtime root.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from api.runtime_paths import get_runtime_dir


logger = logging.getLogger(__name__)
DEFAULT_LIMITS = {"webhook": 120, "api": 60}
DEFAULT_WINDOW_SECONDS = 60
_FALLBACK_LOCK = threading.Lock()
_FALLBACK: dict[tuple[str, str], tuple[int, int]] = {}


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def formal_saas_mode() -> bool:
    raw = str(os.environ.get("MAGI_DEPLOYMENT_MODE") or "").strip().lower()
    return _truthy(os.environ.get("MAGI_SAAS_MODE")) or raw in {
        "saas",
        "formal_saas",
        "managed_saas",
        "multi_tenant_saas",
    }


def default_database_path() -> Path:
    configured = str(os.environ.get("MAGI_RATE_LIMIT_DB_PATH") or "").strip()
    return Path(configured).expanduser() if configured else get_runtime_dir() / "rate_limit.sqlite3"


def hash_client_identity(identity: str) -> str:
    return hashlib.sha256(str(identity or "unknown").encode("utf-8", errors="ignore")).hexdigest()


def inspect_rate_limit_storage(path: Path | str | None = None) -> dict[str, object]:
    """Perform a bounded, read-only integrity check for readiness reporting."""
    target = Path(path or default_database_path()).expanduser()
    report: dict[str, object] = {
        "ok": False,
        "status": "invalid",
        "absolute": target.is_absolute(),
        "exists": target.exists(),
        "safe_metadata": False,
        "integrity": "not_checked",
        "issue": "",
    }
    try:
        if not target.is_absolute():
            raise RateLimitStorageError("rate-limit database path must be absolute")
        if target.is_symlink():
            raise RateLimitStorageError("rate-limit database path must not be a symlink")
        if not target.exists():
            parent = target.parent
            if parent.exists() and parent.is_symlink():
                raise RateLimitStorageError("rate-limit parent must not be a symlink")
            if parent.exists() and not parent.is_dir():
                raise RateLimitStorageError("rate-limit parent must be a directory")
            report.update(
                ok=bool(parent.exists() and os.access(parent, os.W_OK)),
                status="ready_to_initialize" if parent.exists() and os.access(parent, os.W_OK) else "unavailable",
                safe_metadata=True,
                integrity="not_initialized",
            )
            return report

        metadata = target.lstat()
        safe_metadata = (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) & (stat.S_IRWXG | stat.S_IRWXO) == 0
        )
        report["safe_metadata"] = safe_metadata
        if not safe_metadata:
            raise RateLimitStorageError("rate-limit database must be one 0600 regular file")
        uri = f"file:{target.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rate_windows'"
            ).fetchone()
        integrity = str(quick_check[0] if quick_check else "missing")
        if integrity.lower() != "ok" or table is None:
            raise RateLimitStorageError("rate-limit database integrity or schema check failed")
        report.update(ok=True, status="verified", integrity="ok")
        return report
    except (OSError, sqlite3.Error, RateLimitStorageError) as exc:
        report["issue"] = str(exc)
        return report


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    rejected: bool
    limit: int
    count: int
    retry_after: int
    backend: str
    reason: str = ""


class RateLimitStorageError(RuntimeError):
    pass


class DurableRateLimiter:
    def __init__(
        self,
        path: Path | str | None = None,
        *,
        limits: Mapping[str, int] | None = None,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path or default_database_path()).expanduser()
        self.limits = {
            str(key): max(1, int(value))
            for key, value in (limits or DEFAULT_LIMITS).items()
        }
        self.window_seconds = max(1, int(window_seconds))
        self.clock = clock

    def _prepare_path(self) -> None:
        if not self.path.is_absolute():
            raise RateLimitStorageError("rate-limit database path must be absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RateLimitStorageError("rate-limit database path must not be a symlink")
        if self.path.exists():
            metadata = self.path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RateLimitStorageError("rate-limit database must be one regular file")

    def _connect(self) -> sqlite3.Connection:
        self._prepare_path()
        connection = sqlite3.connect(str(self.path), timeout=2.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_windows (
                category TEXT NOT NULL,
                client_hash TEXT NOT NULL,
                window_start INTEGER NOT NULL,
                request_count INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (category, client_hash)
            ) WITHOUT ROWID
            """
        )
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return connection

    def check(self, category: str, client_identity: str) -> RateLimitDecision:
        category = str(category or "api").strip().lower() or "api"
        limit = int(self.limits.get(category, self.limits.get("api", 60)))
        now = int(self.clock())
        window_start = now - (now % self.window_seconds)
        client_hash = hash_client_identity(client_identity)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT window_start, request_count FROM rate_windows "
                    "WHERE category=? AND client_hash=?",
                    (category, client_hash),
                ).fetchone()
                if row is None or int(row[0]) != window_start:
                    count = 1
                    connection.execute(
                        "INSERT INTO rate_windows "
                        "(category, client_hash, window_start, request_count, updated_at) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(category, client_hash) DO UPDATE SET "
                        "window_start=excluded.window_start, "
                        "request_count=excluded.request_count, updated_at=excluded.updated_at",
                        (category, client_hash, window_start, count, now),
                    )
                    rejected = False
                else:
                    previous = int(row[1])
                    rejected = previous >= limit
                    count = previous if rejected else previous + 1
                    connection.execute(
                        "UPDATE rate_windows SET request_count=?, updated_at=? "
                        "WHERE category=? AND client_hash=?",
                        (count, now, category, client_hash),
                    )
                if now % 97 == 0:
                    connection.execute(
                        "DELETE FROM rate_windows WHERE updated_at < ?",
                        (now - self.window_seconds * 3,),
                    )
                connection.commit()
        except (OSError, sqlite3.Error, RateLimitStorageError) as exc:
            raise RateLimitStorageError(str(exc)) from exc

        retry_after = max(1, window_start + self.window_seconds - now) if rejected else 0
        return RateLimitDecision(
            rejected=rejected,
            limit=limit,
            count=count,
            retry_after=retry_after,
            backend="sqlite",
        )


def _fallback_check(
    category: str,
    client_identity: str,
    *,
    limits: Mapping[str, int],
    window_seconds: int,
    now: int,
) -> RateLimitDecision:
    limit = int(limits.get(category, limits.get("api", 60)))
    window_start = now - (now % window_seconds)
    key = (category, hash_client_identity(client_identity))
    with _FALLBACK_LOCK:
        previous_start, previous_count = _FALLBACK.get(key, (window_start, 0))
        if previous_start != window_start:
            previous_count = 0
        rejected = previous_count >= limit
        count = previous_count if rejected else previous_count + 1
        _FALLBACK[key] = (window_start, count)
    retry_after = max(1, window_start + window_seconds - now) if rejected else 0
    return RateLimitDecision(
        rejected=rejected,
        limit=limit,
        count=count,
        retry_after=retry_after,
        backend="memory_fallback",
        reason="durable_storage_unavailable",
    )


def check_rate_limit(
    category: str,
    client_identity: str,
    *,
    limiter: DurableRateLimiter | None = None,
    fail_closed: bool | None = None,
) -> RateLimitDecision:
    active = limiter or DurableRateLimiter()
    try:
        return active.check(category, client_identity)
    except RateLimitStorageError as exc:
        closed = formal_saas_mode() if fail_closed is None else bool(fail_closed)
        logger.error("durable rate-limit backend unavailable: %s", exc)
        if closed:
            return RateLimitDecision(
                rejected=True,
                limit=int(active.limits.get(category, active.limits.get("api", 60))),
                count=0,
                retry_after=active.window_seconds,
                backend="unavailable_fail_closed",
                reason="durable_storage_unavailable",
            )
        return _fallback_check(
            category,
            client_identity,
            limits=active.limits,
            window_seconds=active.window_seconds,
            now=int(active.clock()),
        )


__all__ = (
    "DurableRateLimiter",
    "RateLimitDecision",
    "RateLimitStorageError",
    "check_rate_limit",
    "default_database_path",
    "formal_saas_mode",
    "hash_client_identity",
    "inspect_rate_limit_storage",
)
