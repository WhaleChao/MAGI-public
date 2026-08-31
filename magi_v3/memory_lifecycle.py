"""MemoryRecord v2 identity, retention, tombstone, and index consistency.

This registry stores metadata and hashes, never memory content.  It governs
Agent memory only; formal legal files and evidence remain under their existing
records-management workflows and cannot be deleted through this API.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Mapping

from api.session.provenance import default_confidence_for_source, parse_source_provenance


MEMORY_SCHEMA = "magi.memory-record/v2"
MEMORY_ID_RE = re.compile(r"^mem-[0-9a-f]{32}$")
BACKENDS = (
    "primary",
    "keeper",
    "mariadb",
    "faiss",
    "knowledge_graph",
    "obsidian",
    "backup_index",
)
TERMINAL_DELETE_STATES = frozenset({"not_present", "tombstoned", "not_applicable"})
FORMAL_RETENTION_POLICIES = frozenset({"formal_legal_record", "legal_hold", "statutory_record"})
FORMAL_SOURCE_TYPES = frozenset(
    {
        "case_file",
        "evidence",
        "formal_legal_record",
        "judicial_document",
        "laf_attachment",
        "transcript_original",
    }
)


class MemoryLifecycleError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())


def content_sha256(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8", errors="replace")).hexdigest()


def canonical_memory_id(source: str, content: str) -> str:
    material = (_canonical_text(source) + "\n" + _canonical_text(content)).encode("utf-8", errors="replace")
    return "mem-" + hashlib.sha256(material).hexdigest()[:32]


def _safe_case_scope(value: Any) -> str:
    result = str(value or "").strip()
    if len(result) > 128 or any(ord(char) < 32 for char in result):
        raise MemoryLifecycleError("case_scope is invalid")
    return result


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    content_sha256: str
    source: str
    source_type: str
    source_id: str = ""
    case_scope: str = ""
    confidence: float = 0.0
    retention_policy: str = "agent_memory_default"
    legal_hold: bool = False
    status: str = "active"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    archived_at: str = ""
    tombstoned_at: str = ""
    tombstone_reason: str = ""
    supersedes: str = ""
    index_sync: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not MEMORY_ID_RE.fullmatch(self.memory_id):
            raise MemoryLifecycleError("memory_id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256):
            raise MemoryLifecycleError("content_sha256 is invalid")
        if self.status not in {"active", "archived", "tombstoned"}:
            raise MemoryLifecycleError("memory status is invalid")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise MemoryLifecycleError("memory confidence must be between 0 and 1")
        _safe_case_scope(self.case_scope)

    @property
    def protected_legal_record(self) -> bool:
        return (
            self.legal_hold
            or self.retention_policy in FORMAL_RETENTION_POLICIES
            or self.source_type in FORMAL_SOURCE_TYPES
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema": MEMORY_SCHEMA, **asdict(self), "protected_legal_record": self.protected_legal_record}


class MemoryLifecycleStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @classmethod
    def from_env(cls) -> "MemoryLifecycleStore":
        explicit = str(os.environ.get("MAGI_MEMORY_LIFECYCLE_DB") or "").strip()
        if explicit:
            return cls(Path(explicit))
        runtime = str(os.environ.get("MAGI_RUNTIME_DIR") or "").strip()
        if runtime:
            return cls(Path(runtime).expanduser() / "memory" / "memory_lifecycle.sqlite3")
        return cls(
            Path.home()
            / "Library"
            / "Application Support"
            / "MAGI"
            / "runtime"
            / "MAGI_v3"
            / "shared"
            / "memory"
            / "memory_lifecycle.sqlite3"
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS memory_records (
                    memory_id TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '',
                    case_scope TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    retention_policy TEXT NOT NULL,
                    legal_hold INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT NOT NULL DEFAULT '',
                    tombstoned_at TEXT NOT NULL DEFAULT '',
                    tombstone_reason TEXT NOT NULL DEFAULT '',
                    supersedes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS memory_backend_state (
                    memory_id TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(memory_id, backend),
                    FOREIGN KEY(memory_id) REFERENCES memory_records(memory_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS memory_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_hash ON memory_records(content_sha256, source);
                CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_records(status, updated_at);
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _event_receipt(memory_id: str, event_type: str, actor: str, reason: str, created_at: str) -> str:
        payload = json.dumps(
            {
                "memory_id": memory_id,
                "event_type": event_type,
                "actor": actor,
                "reason": reason,
                "created_at": created_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _event(self, connection: sqlite3.Connection, memory_id: str, event_type: str, actor: str, reason: str) -> str:
        created_at = utc_now()
        receipt = self._event_receipt(memory_id, event_type, actor, reason, created_at)
        connection.execute(
            "INSERT INTO memory_events(memory_id,event_type,actor,reason,created_at,receipt_sha256) VALUES(?,?,?,?,?,?)",
            (memory_id, event_type, actor, reason[:500], created_at, receipt),
        )
        return receipt

    def register(
        self,
        content: str,
        source: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        actor: str = "magi-memory-writer",
    ) -> MemoryRecord:
        meta = dict(metadata or {})
        provenance = parse_source_provenance(source)
        source_type = str(meta.get("source_type") or provenance.source_type or "unknown").strip().lower()
        source_id = str(meta.get("source_id") or provenance.source_id or "").strip()[:256]
        case_scope = _safe_case_scope(meta.get("case_scope") or meta.get("case") or "")
        confidence = float(
            meta.get("confidence")
            if meta.get("confidence") is not None
            else provenance.confidence
            or default_confidence_for_source(source_type, verified=provenance.verified, role=provenance.role)
        )
        confidence = max(0.0, min(1.0, confidence))
        retention_policy = str(meta.get("retention_policy") or "agent_memory_default").strip().lower()
        legal_hold = bool(meta.get("legal_hold", False))
        memory_id = canonical_memory_id(source, content)
        digest = content_sha256(content)
        now = utc_now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT status,created_at,archived_at,tombstoned_at,tombstone_reason,supersedes FROM memory_records WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
            if existing is None:
                status, created_at, archived_at, tombstoned_at, tombstone_reason, supersedes = (
                    "active",
                    now,
                    "",
                    "",
                    "",
                    str(meta.get("supersedes") or ""),
                )
            else:
                status = str(existing["status"])
                created_at = str(existing["created_at"])
                archived_at = str(existing["archived_at"])
                tombstoned_at = str(existing["tombstoned_at"])
                tombstone_reason = str(existing["tombstone_reason"])
                supersedes = str(existing["supersedes"])
            connection.execute(
                """
                INSERT INTO memory_records(
                    memory_id,content_sha256,source,source_type,source_id,case_scope,confidence,
                    retention_policy,legal_hold,status,created_at,updated_at,archived_at,
                    tombstoned_at,tombstone_reason,supersedes
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    source_type=excluded.source_type,source_id=excluded.source_id,
                    case_scope=excluded.case_scope,confidence=excluded.confidence,
                    retention_policy=excluded.retention_policy,
                    legal_hold=MAX(memory_records.legal_hold,excluded.legal_hold),updated_at=excluded.updated_at
                """,
                (
                    memory_id,
                    digest,
                    str(source)[:250],
                    source_type,
                    source_id,
                    case_scope,
                    confidence,
                    retention_policy,
                    int(legal_hold),
                    status,
                    created_at,
                    now,
                    archived_at,
                    tombstoned_at,
                    tombstone_reason,
                    supersedes,
                ),
            )
            for backend in BACKENDS:
                initial = "present" if backend in {"primary", "keeper", "mariadb"} else "pending"
                connection.execute(
                    "INSERT OR IGNORE INTO memory_backend_state(memory_id,backend,status,checked_at) VALUES(?,?,?,?)",
                    (memory_id, backend, initial, now),
                )
            self._event(connection, memory_id, "registered", actor, "memory metadata registered")
        return self.get(memory_id)

    def get(self, memory_id: str) -> MemoryRecord:
        if not MEMORY_ID_RE.fullmatch(str(memory_id or "")):
            raise MemoryLifecycleError("exact memory_id is required")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_records WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None:
                raise MemoryLifecycleError("memory record was not found")
            states = connection.execute(
                "SELECT backend,status,checked_at,receipt_sha256,detail FROM memory_backend_state WHERE memory_id=?",
                (memory_id,),
            ).fetchall()
        data = dict(row)
        data["legal_hold"] = bool(data["legal_hold"])
        data["index_sync"] = {
            str(item["backend"]): {
                "status": str(item["status"]),
                "checked_at": str(item["checked_at"]),
                "receipt_sha256": str(item["receipt_sha256"]),
                "detail": str(item["detail"]),
            }
            for item in states
        }
        return MemoryRecord(**data)

    def archive(self, memory_id: str, *, reason: str, actor: str) -> MemoryRecord:
        record = self.get(memory_id)
        if record.status == "tombstoned":
            raise MemoryLifecycleError("tombstoned memory cannot be archived")
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE memory_records SET status='archived',archived_at=?,updated_at=? WHERE memory_id=?",
                (now, now, memory_id),
            )
            self._event(connection, memory_id, "archived", actor, str(reason or "archive requested"))
        return self.get(memory_id)

    def correct(
        self,
        memory_id: str,
        corrected_content: str,
        *,
        reason: str,
        actor: str,
    ) -> tuple[MemoryRecord, MemoryRecord]:
        original = self.get(memory_id)
        if not str(corrected_content or "").strip():
            raise MemoryLifecycleError("corrected content is required")
        if canonical_memory_id(original.source, corrected_content) == original.memory_id:
            raise MemoryLifecycleError("correction must change memory content")
        replacement = self.register(
            corrected_content,
            original.source,
            metadata={
                "source_type": original.source_type,
                "source_id": original.source_id,
                "case_scope": original.case_scope,
                "confidence": original.confidence,
                "retention_policy": original.retention_policy,
                "legal_hold": original.legal_hold,
                "supersedes": original.memory_id,
            },
            actor=actor,
        )
        archived = self.archive(original.memory_id, reason=reason, actor=actor)
        return archived, replacement

    def tombstone(self, memory_id: str, *, reason: str, actor: str) -> MemoryRecord:
        record = self.get(memory_id)
        if record.protected_legal_record:
            raise MemoryLifecycleError(
                "formal legal records and legal-hold material cannot be deleted through Agent memory"
            )
        why = str(reason or "").strip()
        if not why:
            raise MemoryLifecycleError("tombstone reason is required")
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE memory_records
                SET status='tombstoned',tombstoned_at=?,tombstone_reason=?,updated_at=?
                WHERE memory_id=?
                """,
                (now, why[:500], now, memory_id),
            )
            connection.execute(
                "UPDATE memory_backend_state SET status='pending_delete',checked_at=?,receipt_sha256='',detail='' WHERE memory_id=?",
                (now, memory_id),
            )
            self._event(connection, memory_id, "tombstoned", actor, why)
        return self.get(memory_id)

    def mark_backend(
        self,
        memory_id: str,
        backend: str,
        status: str,
        *,
        receipt_sha256: str = "",
        detail: str = "",
    ) -> MemoryRecord:
        self.get(memory_id)
        if backend not in BACKENDS:
            raise MemoryLifecycleError("memory backend is invalid")
        if status not in {"present", "pending", "pending_delete", "tombstoned", "not_present", "not_applicable", "failed"}:
            raise MemoryLifecycleError("memory backend status is invalid")
        if receipt_sha256 and not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256):
            raise MemoryLifecycleError("backend receipt SHA-256 is invalid")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_backend_state(memory_id,backend,status,checked_at,receipt_sha256,detail)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(memory_id,backend) DO UPDATE SET
                    status=excluded.status,checked_at=excluded.checked_at,
                    receipt_sha256=excluded.receipt_sha256,detail=excluded.detail
                """,
                (memory_id, backend, status, utc_now(), receipt_sha256, str(detail)[:500]),
            )
        return self.get(memory_id)

    def deletion_consistency(self, memory_id: str) -> dict[str, Any]:
        record = self.get(memory_id)
        states = {backend: record.index_sync.get(backend, {}).get("status", "missing") for backend in BACKENDS}
        complete = record.status == "tombstoned" and all(
            status in TERMINAL_DELETE_STATES for status in states.values()
        )
        return {
            "schema": "magi.memory-deletion-consistency/v1",
            "memory_id": memory_id,
            "status": "complete" if complete else "pending",
            "complete": complete,
            "backends": states,
            "formal_legal_files_affected": False,
        }

    def is_tombstoned(self, source: str, content: str) -> bool:
        memory_id = canonical_memory_id(source, content)
        try:
            return self.get(memory_id).status == "tombstoned"
        except MemoryLifecycleError:
            # Compatibility fallback for sources annotated by local recall.
            clean_source = str(source or "").replace(" [Local]", "")
            if clean_source == source:
                return False
            try:
                return self.get(canonical_memory_id(clean_source, content)).status == "tombstoned"
            except MemoryLifecycleError:
                return False


def filter_tombstoned_results(
    results: list[dict[str, Any]],
    *,
    store: MemoryLifecycleStore | None = None,
) -> list[dict[str, Any]]:
    registry = store or MemoryLifecycleStore.from_env()
    candidates: list[tuple[dict[str, Any], tuple[str, ...]]] = []
    all_ids: set[str] = set()
    for item in results or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        content = str(item.get("content") or "")
        ids = [canonical_memory_id(source, content)]
        clean_source = source.replace(" [Local]", "")
        if clean_source != source:
            ids.append(canonical_memory_id(clean_source, content))
        candidates.append((item, tuple(ids)))
        all_ids.update(ids)
    tombstoned: set[str] = set()
    if all_ids:
        ordered = sorted(all_ids)
        placeholders = ",".join("?" for _ in ordered)
        with registry._connect() as connection:
            rows = connection.execute(
                f"SELECT memory_id FROM memory_records WHERE status='tombstoned' AND memory_id IN ({placeholders})",
                ordered,
            ).fetchall()
        tombstoned = {str(row[0]) for row in rows}
    filtered: list[dict[str, Any]] = []
    for item, ids in candidates:
        if any(memory_id in tombstoned for memory_id in ids):
            continue
        filtered.append(item)
    return filtered


BackendAdapter = Callable[[MemoryRecord], Mapping[str, Any]]


def propagate_tombstone(
    store: MemoryLifecycleStore,
    memory_id: str,
    adapters: Mapping[str, BackendAdapter],
) -> dict[str, Any]:
    record = store.get(memory_id)
    if record.status != "tombstoned":
        raise MemoryLifecycleError("memory must be tombstoned before propagation")
    for backend in BACKENDS:
        adapter = adapters.get(backend)
        if adapter is None:
            continue
        try:
            result = dict(adapter(record))
            status = str(result.get("status") or "failed")
            receipt = str(result.get("receipt_sha256") or "")
            detail = str(result.get("detail") or "")
        except Exception as exc:
            status, receipt, detail = "failed", "", type(exc).__name__
        store.mark_backend(memory_id, backend, status, receipt_sha256=receipt, detail=detail)
    return store.deletion_consistency(memory_id)


__all__ = [
    "BACKENDS",
    "FORMAL_RETENTION_POLICIES",
    "FORMAL_SOURCE_TYPES",
    "MEMORY_SCHEMA",
    "MemoryLifecycleError",
    "MemoryLifecycleStore",
    "MemoryRecord",
    "canonical_memory_id",
    "content_sha256",
    "filter_tombstoned_results",
    "propagate_tombstone",
]
