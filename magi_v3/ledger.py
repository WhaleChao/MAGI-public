"""Durable SQLite WAL job ledger for the MAGI V3 control core."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .errors import InvalidTransition, JobNotFound, LeaseConflict, LedgerError
from .state import JobStatus, ensure_transition


WORKER_CLASSES = (
    "light",
    "browser",
    "document",
    "transcription",
    "integration",
    "model",
    "maintenance",
)
HEAVY_WORKER_CLASSES = frozenset(set(WORKER_CLASSES) - {"light"})
SIDE_EFFECT_CLASSES = (
    "none",
    "read_only",
    "local_draft",
    "reversible_write",
    "external_commit",
    "destructive",
)
WRITE_SIDE_EFFECT_CLASSES = frozenset(
    {"local_draft", "reversible_write", "external_commit", "destructive"}
)
DANGEROUS_SIDE_EFFECT_CLASSES = frozenset({"external_commit", "destructive"})
PRIORITY_WEIGHTS = {"P0": 100, "P1": 90, "P2": 70, "P3": 40, "P4": 10}
PRIORITY_PREEMPTIBLE = {"P0": False, "P1": False, "P2": True, "P3": True, "P4": True}
SAFE_PREEMPTION_SIDE_EFFECT_CLASSES = frozenset({"none", "read_only"})
COMMIT_PHASES = frozenset(
    {"not_applicable", "prepared", "committing", "committed", "ambiguous", "verified"}
)
RESOURCE_CLAIM_KEYS = frozenset(
    {"memory_mb", "metal_mb", "cpu_percent", "disk_io", "nas_io", "network", "browser_tokens"}
)
_IO_CLASSES = frozenset({"none", "light", "heavy"})
_DEFAULT_RESOURCE_CLAIM = {
    "memory_mb": 0,
    "metal_mb": 0,
    "cpu_percent": 0,
    "disk_io": "none",
    "nas_io": "none",
    "network": "none",
    "browser_tokens": 0,
}
_METRIC_FIELDS = {
    "duration_ms": int,
    "queue_ms": int,
    "worker_start_ms": int,
    "model_load_ms": int,
    "ttft_ms": int,
    "generation_ms": int,
    "peak_rss_mb": (int, float),
    "peak_footprint_mb": (int, float),
    "process_group_peak_footprint_mb": (int, float),
    "peak_metal_mb": (int, float),
    "swapout_delta_mb": (int, float),
    "compressor_delta_mb": (int, float),
    "cpu_seconds": (int, float),
    "child_process_peak": int,
    "child_reap_ms": int,
    "page_faults": int,
    "disk_read_bytes": int,
    "disk_write_bytes": int,
    "input_bytes": int,
    "output_bytes": int,
    "input_tokens": int,
    "output_tokens": int,
    "tokens_per_second": (int, float),
    "provider": (str, type(None)),
    "model": (str, type(None)),
}
_MIN_CONFIRMATION_TOKEN_LENGTH = 16


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    current = value or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"invalid {field_name} timestamp") from exc
    if parsed.tzinfo is None:
        raise LedgerError(f"{field_name} timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_resource_claim(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != RESOURCE_CLAIM_KEYS:
        raise ValueError("resource_claim must contain exactly the canonical resource keys")
    claim = dict(value)
    for key in ("memory_mb", "metal_mb", "cpu_percent", "browser_tokens"):
        if type(claim[key]) is not int or claim[key] < 0:
            raise ValueError(f"resource_claim.{key} must be a non-negative integer")
    if claim["cpu_percent"] > 1000:
        raise ValueError("resource_claim.cpu_percent must be <= 1000")
    if claim["browser_tokens"] > 1:
        raise ValueError("resource_claim.browser_tokens must be <= 1")
    for key in ("disk_io", "nas_io", "network"):
        if claim[key] not in _IO_CLASSES:
            raise ValueError(f"resource_claim.{key} has an unsupported class")
    return claim


def _canonical_artifacts(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)):
        raise ValueError("artifacts must be an array")
    artifacts: list[dict[str, Any]] = []
    allowed = {"kind", "uri", "sha256", "size_bytes"}
    for item in value:
        if not isinstance(item, Mapping) or not {"kind", "uri"} <= set(item) <= allowed:
            raise ValueError("artifact must contain kind/uri and no unknown fields")
        artifact = dict(item)
        if not isinstance(artifact["kind"], str) or not 1 <= len(artifact["kind"]) <= 64:
            raise ValueError("artifact.kind is invalid")
        if not isinstance(artifact["uri"], str) or not 1 <= len(artifact["uri"]) <= 4096:
            raise ValueError("artifact.uri is invalid")
        digest = artifact.get("sha256")
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in digest)
        ):
            raise ValueError("artifact.sha256 is invalid")
        size = artifact.get("size_bytes")
        if size is not None and (type(size) is not int or size < 0):
            raise ValueError("artifact.size_bytes is invalid")
        artifacts.append(artifact)
    return artifacts


def _canonical_receipts(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)):
        raise ValueError("side_effect_receipts must be an array")
    receipts: list[dict[str, Any]] = []
    allowed = {"kind", "reference", "committed_at", "idempotency_key"}
    for item in value:
        if not isinstance(item, Mapping) or not {"kind", "reference", "committed_at"} <= set(item) <= allowed:
            raise ValueError("side-effect receipt is missing required fields or has unknown fields")
        receipt = dict(item)
        if not isinstance(receipt["kind"], str) or not 1 <= len(receipt["kind"]) <= 64:
            raise ValueError("side-effect receipt kind is invalid")
        if not isinstance(receipt["reference"], str) or not 1 <= len(receipt["reference"]) <= 512:
            raise ValueError("side-effect receipt reference is invalid")
        if not isinstance(receipt["committed_at"], str):
            raise ValueError("side-effect receipt committed_at is invalid")
        _parse_timestamp(receipt["committed_at"], "side_effect_receipt.committed_at")
        key = receipt.get("idempotency_key")
        if key is not None and (not isinstance(key, str) or len(key) > 256):
            raise ValueError("side-effect receipt idempotency_key is invalid")
        receipts.append(receipt)
    return receipts


def _canonical_metrics(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not set(value) <= set(_METRIC_FIELDS):
        raise ValueError("metrics contains unknown fields")
    metrics = dict(value)
    for key, metric in metrics.items():
        expected = _METRIC_FIELDS[key]
        if isinstance(metric, bool) or not isinstance(metric, expected):
            raise ValueError(f"metrics.{key} has an invalid type")
        if key not in {"provider", "model", "compressor_delta_mb"} and metric < 0:
            raise ValueError(f"metrics.{key} must be non-negative")
        if isinstance(metric, float) and not math.isfinite(metric):
            raise ValueError(f"metrics.{key} must be finite")
    return metrics


def _canonical_error(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidTransition("error payload must be an object")
    code = value.get("code")
    if not isinstance(code, str) or not code.strip() or len(code) > 128:
        raise InvalidTransition("error payload requires a valid code")
    message = value.get("message", code)
    if not isinstance(message, str) or not message or len(message) > 4000:
        raise InvalidTransition("error payload requires a valid message")
    detail = value.get("detail", {})
    if not isinstance(detail, Mapping):
        raise InvalidTransition("error detail must be an object")
    merged_detail = dict(detail)
    merged_detail.update(
        {key: item for key, item in value.items() if key not in {"code", "message", "detail"}}
    )
    result: dict[str, Any] = {"code": code, "message": message}
    if merged_detail:
        result["detail"] = merged_detail
    return result


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_confirmation_token() -> str:
    """Return a caller-visible challenge token; the ledger stores only its digest."""

    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class JobSpec:
    capability: str
    operation: str
    worker_class: str
    input: Mapping[str, Any]
    job_id: str | None = None
    side_effect_class: str = "none"
    priority_class: str = "P3"
    scheduled_for: datetime | None = None
    latest_start_at: datetime | None = None
    deadline_at: datetime | None = None
    not_before: datetime | None = None
    max_attempts: int = 1
    timeout_sec: int = 600
    queue_ttl_sec: int = 86400
    preemptible: bool | None = None
    resource_claim: Mapping[str, Any] = field(
        default_factory=lambda: dict(_DEFAULT_RESOURCE_CLAIM)
    )
    commit_phase: str | None = None
    idempotency_key: str | None = None
    confirmation_token: str | None = field(default=None, repr=False, compare=False)
    confirmation_expires_at: datetime | None = None

    def validate(self) -> None:
        if not self.capability.strip() or not self.operation.strip():
            raise ValueError("capability and operation are required")
        if self.worker_class not in WORKER_CLASSES:
            raise ValueError(f"unsupported worker class: {self.worker_class}")
        if self.side_effect_class not in SIDE_EFFECT_CLASSES:
            raise ValueError(f"unsupported side-effect class: {self.side_effect_class}")
        if self.priority_class not in PRIORITY_WEIGHTS:
            raise ValueError(f"unsupported priority class: {self.priority_class}")
        if self.preemptible is not None and type(self.preemptible) is not bool:
            raise ValueError("preemptible must be a boolean")
        expected_preemptible = PRIORITY_PREEMPTIBLE[self.priority_class]
        if self.preemptible is not None and self.preemptible is not expected_preemptible:
            raise ValueError("preemptible conflicts with the configured priority class")
        _canonical_resource_claim(self.resource_claim)
        expected_phase = (
            "prepared" if self.side_effect_class in WRITE_SIDE_EFFECT_CLASSES else "not_applicable"
        )
        if self.commit_phase is not None and self.commit_phase != expected_phase:
            raise ValueError("initial commit_phase conflicts with side_effect_class")
        if self.side_effect_class in WRITE_SIDE_EFFECT_CLASSES and not (
            self.idempotency_key and self.idempotency_key.strip()
        ):
            raise ValueError("write side-effect classes require a non-empty idempotency_key")
        if self.side_effect_class in DANGEROUS_SIDE_EFFECT_CLASSES:
            if not self.confirmation_token or len(self.confirmation_token) < _MIN_CONFIRMATION_TOKEN_LENGTH:
                raise ValueError(
                    "external_commit and destructive jobs require a confirmation challenge token"
                )
            if self.confirmation_expires_at is None:
                raise ValueError(
                    "external_commit and destructive jobs require confirmation_expires_at"
                )
        elif self.confirmation_token is not None or self.confirmation_expires_at is not None:
            raise ValueError("confirmation challenges are only valid for external_commit or destructive jobs")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("max_attempts must be in [1, 100]")
        if not 1 <= self.timeout_sec <= 86400:
            raise ValueError("timeout_sec must be in [1, 86400]")
        if not 1 <= self.queue_ttl_sec <= 604800:
            raise ValueError("queue_ttl_sec must be in [1, 604800]")
        if self.job_id is not None and not 1 <= len(self.job_id) <= 128:
            raise ValueError("job_id must be in [1, 128] characters")
        if len(self.capability) > 128 or len(self.operation) > 128:
            raise ValueError("capability and operation must be <= 128 characters")
        if self.idempotency_key is not None and len(self.idempotency_key) > 256:
            raise ValueError("idempotency_key must be <= 256 characters")
        try:
            _json(dict(self.input))
        except (TypeError, ValueError) as exc:
            raise ValueError("input must be a JSON-serializable object") from exc


@dataclass(frozen=True, slots=True)
class JobRecord:
    schema_version: str
    job_id: str
    capability: str
    operation: str
    worker_class: str
    side_effect_class: str
    priority_class: str
    priority: int
    status: JobStatus
    input: Any
    result: Any
    error: Any
    created_at: str
    scheduled_for: str
    latest_start_at: str
    deadline_at: str
    not_before: str
    started_at: str | None
    finished_at: str | None
    attempt_count: int
    max_attempts: int
    timeout_sec: int
    queue_ttl_sec: int
    preemptible: bool
    resource_claim: Any
    commit_phase: str
    ambiguous_side_effect: bool
    idempotency_key: str | None
    confirmation_expires_at: str | None
    confirmed_at: str | None
    business_completed: bool
    artifacts: Any
    side_effect_receipts: Any
    metrics: Any
    version: int

    def to_envelope(self) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "capability": self.capability,
            "operation": self.operation,
            "worker_class": self.worker_class,
            "side_effect_class": self.side_effect_class,
            "priority_class": self.priority_class,
            "status": self.status.value,
            "created_at": self.created_at,
            "scheduled_for": self.scheduled_for,
            "latest_start_at": self.latest_start_at,
            "deadline_at": self.deadline_at,
            "not_before": self.not_before,
            "attempt": self.attempt_count,
            "max_attempts": self.max_attempts,
            "timeout_sec": self.timeout_sec,
            "queue_ttl_sec": self.queue_ttl_sec,
            "preemptible": self.preemptible,
            "resource_claim": dict(self.resource_claim),
            "commit_phase": self.commit_phase,
            "ambiguous_side_effect": self.ambiguous_side_effect,
            "input": self.input,
            "business_completed": self.business_completed,
            "artifacts": [dict(item) for item in self.artifacts],
            "side_effect_receipts": [dict(item) for item in self.side_effect_receipts],
            "metrics": dict(self.metrics),
        }
        if self.idempotency_key is not None:
            envelope["idempotency_key"] = self.idempotency_key
        if self.result is not None:
            envelope["result"] = self.result
        if self.error is not None:
            envelope["error"] = self.error
        if self.started_at is not None:
            envelope["started_at"] = self.started_at
        if self.finished_at is not None:
            envelope["finished_at"] = self.finished_at
        if self.side_effect_class in DANGEROUS_SIDE_EFFECT_CLASSES:
            envelope["confirmation"] = {
                "required": True,
                "reason": "dangerous_side_effect",
                "expires_at": self.confirmation_expires_at,
            }
        return envelope


@dataclass(frozen=True, slots=True)
class JobLease:
    job: JobRecord
    token: str
    owner_id: str
    acquired_at: str
    expires_at: str
    attempt_number: int


@dataclass(frozen=True, slots=True)
class FencedLease:
    job: JobRecord
    token: str
    owner_id: str
    attempt_number: int
    fenced_at: str
    fence_generation: int
    worker_pid: int | None


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    outbox_id: str
    job_id: str | None
    topic: str
    payload: Any
    idempotency_key: str
    status: str
    attempt_count: int
    locked_by: str | None
    lock_expires_at: str | None
    claim_token: str | None
    claim_generation: int
    delivery_expires_at: str
    max_attempts: int
    dead_lettered_at: str | None


@dataclass(frozen=True, slots=True)
class OutboxSpec:
    topic: str
    payload: Mapping[str, Any]
    idempotency_key: str
    outbox_id: str | None = None
    ttl_seconds: int = 86400
    max_attempts: int = 5

    def validate(self) -> None:
        if not self.topic.strip() or not self.idempotency_key.strip():
            raise ValueError("topic and idempotency_key are required")
        if not 1 <= self.ttl_seconds <= 604800:
            raise ValueError("outbox ttl_seconds must be in [1, 604800]")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("outbox max_attempts must be in [1, 100]")


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    capability TEXT NOT NULL,
    operation TEXT NOT NULL,
    worker_class TEXT NOT NULL CHECK(worker_class IN ('light','browser','document','transcription','integration','model','maintenance')),
    side_effect_class TEXT NOT NULL CHECK(side_effect_class IN ('none','read_only','local_draft','reversible_write','external_commit','destructive')),
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    timeout_sec INTEGER NOT NULL,
    idempotency_key TEXT,
    business_completed INTEGER NOT NULL DEFAULT 0 CHECK(business_completed IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_unique
    ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_dispatch_idx
    ON jobs(status, scheduled_for, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    worker_pid INTEGER,
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT,
    error_json TEXT,
    metrics_json TEXT,
    UNIQUE(job_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS leases (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
    lease_token TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leases_expiry_idx ON leases(expires_at);

CREATE TABLE IF NOT EXISTS outbox (
    outbox_id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(job_id) ON DELETE SET NULL,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    locked_by TEXT,
    lock_expires_at TEXT,
    provider_reference TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_delivery_idx
    ON outbox(status, next_attempt_at, created_at);
"""

_MIGRATION_2 = """
ALTER TABLE jobs ADD COLUMN confirmation_token_sha256 TEXT;
ALTER TABLE jobs ADD COLUMN confirmation_expires_at TEXT;
ALTER TABLE jobs ADD COLUMN confirmed_at TEXT;

ALTER TABLE outbox ADD COLUMN claim_token_sha256 TEXT;
ALTER TABLE outbox ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN delivery_expires_at TEXT NOT NULL
    DEFAULT '9999-12-31T23:59:59.999+00:00';
ALTER TABLE outbox ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 5;
ALTER TABLE outbox ADD COLUMN dead_lettered_at TEXT;
"""

_MIGRATION_3 = """
ALTER TABLE leases ADD COLUMN fenced_at TEXT;
ALTER TABLE leases ADD COLUMN fence_reason TEXT;
ALTER TABLE leases ADD COLUMN fence_generation INTEGER NOT NULL DEFAULT 0;
"""

_MIGRATION_4 = """
ALTER TABLE jobs ADD COLUMN envelope_schema_version TEXT NOT NULL DEFAULT '3.0';
ALTER TABLE jobs ADD COLUMN priority_class TEXT NOT NULL DEFAULT 'P3'
    CHECK(priority_class IN ('P0','P1','P2','P3','P4'));
ALTER TABLE jobs ADD COLUMN latest_start_at TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN deadline_at TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN not_before TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN queue_ttl_sec INTEGER NOT NULL DEFAULT 86400
    CHECK(queue_ttl_sec BETWEEN 1 AND 604800);
ALTER TABLE jobs ADD COLUMN preemptible INTEGER NOT NULL DEFAULT 1
    CHECK(preemptible IN (0,1));
ALTER TABLE jobs ADD COLUMN resource_claim_json TEXT NOT NULL DEFAULT
    '{"browser_tokens":0,"cpu_percent":0,"disk_io":"none","memory_mb":0,"metal_mb":0,"nas_io":"none","network":"none"}';
ALTER TABLE jobs ADD COLUMN commit_phase TEXT NOT NULL DEFAULT 'not_applicable'
    CHECK(commit_phase IN ('not_applicable','prepared','committing','committed','ambiguous','verified'));
ALTER TABLE jobs ADD COLUMN ambiguous_side_effect INTEGER NOT NULL DEFAULT 0
    CHECK(ambiguous_side_effect IN (0,1));
ALTER TABLE jobs ADD COLUMN artifacts_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE jobs ADD COLUMN side_effect_receipts_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE jobs ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS jobs_canonical_dispatch_idx
    ON jobs(status, not_before, latest_start_at, priority DESC, scheduled_for, created_at);
"""


def _legacy_priority_class(priority: int) -> str:
    if priority >= 95:
        return "P0"
    if priority >= 80:
        return "P1"
    if priority >= 55:
        return "P2"
    if priority >= 25:
        return "P3"
    return "P4"


def _apply_migration_4(conn: sqlite3.Connection) -> None:
    conn.executescript(_MIGRATION_4)
    rows = conn.execute(
        "SELECT job_id, priority, side_effect_class, scheduled_for, timeout_sec, "
        "business_completed FROM jobs"
    ).fetchall()
    for row in rows:
        scheduled = _parse_timestamp(row["scheduled_for"], "scheduled_for")
        queue_ttl_sec = 86400
        latest_start = scheduled + timedelta(seconds=queue_ttl_sec)
        deadline = latest_start + timedelta(seconds=int(row["timeout_sec"]))
        priority_class = _legacy_priority_class(int(row["priority"]))
        phase = "not_applicable"
        if row["side_effect_class"] in WRITE_SIDE_EFFECT_CLASSES:
            phase = "verified" if bool(row["business_completed"]) else "prepared"
        conn.execute(
            """
            UPDATE jobs SET priority_class=?, latest_start_at=?, deadline_at=?,
                not_before=?, queue_ttl_sec=?, preemptible=?, commit_phase=?
            WHERE job_id=?
            """,
            (
                priority_class,
                _timestamp(latest_start),
                _timestamp(deadline),
                _timestamp(scheduled),
                queue_ttl_sec,
                int(PRIORITY_PREEMPTIBLE[priority_class]),
                phase,
                row["job_id"],
            ),
        )


class JobLedger:
    """Short-transaction SQLite ledger safe for multiple local processes."""

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        if self.busy_timeout_ms < 100:
            raise ValueError("busy_timeout_ms must be >= 100")

    def initialize(self) -> None:
        """Create the parent directory, enable WAL, and apply migrations."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            if 1 not in applied:
                conn.executescript(_MIGRATION_1)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _timestamp()),
                )
            if 2 not in applied:
                conn.executescript(_MIGRATION_2)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, _timestamp()),
                )
            if 3 not in applied:
                conn.executescript(_MIGRATION_3)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, _timestamp()),
                )
            if 4 not in applied:
                _apply_migration_4(conn)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, _timestamp()),
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _connect_readonly(self) -> Iterator[sqlite3.Connection]:
        """Open an evidence reader without mutating WAL or database settings."""

        uri = self.path.expanduser().resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA query_only=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    def schema_version(self) -> int:
        with self._connect_readonly() as conn:
            row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
            return int(row[0])

    def ping(self) -> bool:
        try:
            with self._connect_readonly() as conn:
                return conn.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def create_job(self, spec: JobSpec, *, now: datetime | None = None) -> JobRecord:
        spec.validate()
        job_id = spec.job_id or uuid.uuid4().hex
        instant = now or _utcnow()
        now_text = _timestamp(instant)
        scheduled_dt = _parse_timestamp(
            _timestamp(spec.scheduled_for or instant), "scheduled_for"
        )
        not_before_dt = _parse_timestamp(
            _timestamp(spec.not_before or scheduled_dt), "not_before"
        )
        latest_start_dt = _parse_timestamp(
            _timestamp(
                spec.latest_start_at
                or (scheduled_dt + timedelta(seconds=spec.queue_ttl_sec))
            ),
            "latest_start_at",
        )
        deadline_dt = _parse_timestamp(
            _timestamp(
                spec.deadline_at
                or (latest_start_dt + timedelta(seconds=spec.timeout_sec))
            ),
            "deadline_at",
        )
        if not scheduled_dt <= not_before_dt <= latest_start_dt:
            raise ValueError("job timing must satisfy scheduled_for <= not_before <= latest_start_at")
        if latest_start_dt > scheduled_dt + timedelta(seconds=spec.queue_ttl_sec):
            raise ValueError("latest_start_at exceeds queue_ttl_sec")
        if deadline_dt < latest_start_dt + timedelta(seconds=spec.timeout_sec):
            raise ValueError("deadline_at does not leave the declared timeout window")
        scheduled_for = _timestamp(scheduled_dt)
        not_before = _timestamp(not_before_dt)
        latest_start_at = _timestamp(latest_start_dt)
        deadline_at = _timestamp(deadline_dt)
        dangerous = spec.side_effect_class in DANGEROUS_SIDE_EFFECT_CLASSES
        confirmation_expires_at = (
            _timestamp(spec.confirmation_expires_at) if spec.confirmation_expires_at is not None else None
        )
        if dangerous and confirmation_expires_at <= now_text:
            raise ValueError("confirmation challenge must expire in the future")
        initial_status = JobStatus.NEEDS_CONFIRMATION if dangerous else JobStatus.QUEUED
        confirmation_digest = _token_digest(spec.confirmation_token) if dangerous else None
        preemptible = PRIORITY_PREEMPTIBLE[spec.priority_class]
        resource_claim = _canonical_resource_claim(spec.resource_claim)
        commit_phase = (
            "prepared" if spec.side_effect_class in WRITE_SIDE_EFFECT_CLASSES else "not_applicable"
        )
        try:
            with self._immediate() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs(
                        job_id, capability, operation, worker_class, side_effect_class,
                        priority, priority_class, status, input_json, created_at, scheduled_for,
                        latest_start_at, deadline_at, not_before, max_attempts, timeout_sec,
                        queue_ttl_sec, preemptible, resource_claim_json, commit_phase,
                        idempotency_key, updated_at, confirmation_token_sha256,
                        confirmation_expires_at, envelope_schema_version, artifacts_json,
                        side_effect_receipts_json, metrics_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        spec.capability,
                        spec.operation,
                        spec.worker_class,
                        spec.side_effect_class,
                        PRIORITY_WEIGHTS[spec.priority_class],
                        spec.priority_class,
                        initial_status.value,
                        _json(dict(spec.input)),
                        now_text,
                        scheduled_for,
                        latest_start_at,
                        deadline_at,
                        not_before,
                        spec.max_attempts,
                        spec.timeout_sec,
                        spec.queue_ttl_sec,
                        int(preemptible),
                        _json(resource_claim),
                        commit_phase,
                        spec.idempotency_key,
                        now_text,
                        confirmation_digest,
                        confirmation_expires_at,
                        "3.0",
                        "[]",
                        "[]",
                        "{}",
                    ),
                )
                row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"job or idempotency key already exists: {job_id}") from exc
        return self._job_from_row(row)

    def get_job(self, job_id: str) -> JobRecord:
        with self._connect_readonly() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return self._job_from_row(row)

    def recent_jobs(self, *, limit: int = 100) -> tuple[JobRecord, ...]:
        """Return a bounded, newest-first read model for operations support.

        This deliberately returns canonical records rather than raw SQLite rows,
        so support/observability consumers get the same invariant checks as the
        dispatcher and cannot silently turn corrupt state into a green report.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("recent job limit must be in [1, 1000]")
        with self._connect_readonly() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC, job_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return tuple(self._job_from_row(row) for row in rows)

    def confirm_job(
        self,
        job_id: str,
        confirmation_token: str,
        *,
        now: datetime | None = None,
    ) -> JobRecord:
        """Atomically consume a valid dangerous-operation challenge and queue the job."""

        if not confirmation_token:
            raise LedgerError("confirmation rejected")
        timestamp = _timestamp(now)
        supplied_digest = _token_digest(confirmation_token)
        with self._immediate() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            if JobStatus(row["status"]) is not JobStatus.NEEDS_CONFIRMATION:
                raise InvalidTransition("job is not awaiting confirmation")
            expected_digest = row["confirmation_token_sha256"]
            expires_at = row["confirmation_expires_at"]
            if (
                not expected_digest
                or not expires_at
                or expires_at <= timestamp
                or not hmac.compare_digest(expected_digest, supplied_digest)
            ):
                raise LedgerError("confirmation rejected")
            ensure_transition(JobStatus.NEEDS_CONFIRMATION, JobStatus.QUEUED)
            conn.execute(
                """
                UPDATE jobs SET status=?, confirmed_at=?, confirmation_token_sha256=NULL,
                    confirmation_expires_at=NULL, commit_phase='prepared',
                    ambiguous_side_effect=0, version=version+1, updated_at=?
                WHERE job_id=? AND status=?
                """,
                (
                    JobStatus.QUEUED.value,
                    timestamp,
                    timestamp,
                    job_id,
                    JobStatus.NEEDS_CONFIRMATION.value,
                ),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job_from_row(updated)

    def issue_confirmation_challenge(
        self,
        job_id: str,
        confirmation_token: str,
        confirmation_expires_at: datetime,
        *,
        now: datetime | None = None,
    ) -> JobRecord:
        """Replace an ambiguous dangerous job's consumed/expired one-time challenge."""

        if len(confirmation_token) < _MIN_CONFIRMATION_TOKEN_LENGTH:
            raise ValueError("confirmation challenge token is too short")
        timestamp = _timestamp(now)
        expires_at = _timestamp(confirmation_expires_at)
        if expires_at <= timestamp:
            raise ValueError("confirmation challenge must expire in the future")
        with self._immediate() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            if (
                JobStatus(row["status"]) is not JobStatus.NEEDS_CONFIRMATION
                or row["side_effect_class"] not in DANGEROUS_SIDE_EFFECT_CLASSES
            ):
                raise InvalidTransition("only a dangerous job awaiting confirmation can be challenged")
            conn.execute(
                """
                UPDATE jobs SET confirmation_token_sha256=?, confirmation_expires_at=?,
                    confirmed_at=NULL, version=version+1, updated_at=? WHERE job_id=?
                """,
                (_token_digest(confirmation_token), expires_at, timestamp, job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job_from_row(updated)

    def transition_job(
        self,
        job_id: str,
        target: JobStatus | str,
        *,
        expected: JobStatus | str | None = None,
        result: Any = None,
        error: Any = None,
        business_completed: bool | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        """Perform one explicit state transition under an immediate transaction."""

        timestamp = _timestamp(now)
        with self._immediate() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            active_lease = conn.execute(
                "SELECT 1 FROM leases WHERE job_id=?", (job_id,)
            ).fetchone()
            if active_lease is not None:
                raise LeaseConflict("leased jobs require a token-bound transition")
            current = JobStatus(row["status"])
            if expected is not None and current is not JobStatus(expected):
                raise InvalidTransition(
                    f"expected {JobStatus(expected).value}, found {current.value}"
                )
            normalized_target = JobStatus(target)
            if (
                current is JobStatus.NEEDS_CONFIRMATION
                and normalized_target is JobStatus.QUEUED
                and row["side_effect_class"] in DANGEROUS_SIDE_EFFECT_CLASSES
            ):
                raise InvalidTransition("dangerous jobs must use confirm_job to leave needs_confirmation")
            complete = bool(row["business_completed"]) if business_completed is None else business_completed
            target_status = ensure_transition(current, normalized_target, business_completed=complete)
            if target_status in {JobStatus.FAILED, JobStatus.TIMED_OUT} and error is None:
                raise InvalidTransition(f"{target_status.value} requires an error payload")
            canonical_error = _canonical_error(error) if error is not None else None
            started = row["started_at"]
            if target_status is JobStatus.RUNNING and started is None:
                started = timestamp
            finished = timestamp if target_status.value in {
                "succeeded", "degraded", "failed", "deferred", "skipped", "cancelled", "timed_out"
            } else None
            conn.execute(
                """
                UPDATE jobs SET status=?, result_json=?, error_json=?, business_completed=?,
                    started_at=?, finished_at=?, version=version+1, updated_at=?
                WHERE job_id=?
                """,
                (
                    target_status.value,
                    _json(result) if result is not None else row["result_json"],
                    (
                        None
                        if target_status is JobStatus.SUCCEEDED
                        else (
                            _json(canonical_error)
                            if canonical_error is not None
                            else row["error_json"]
                        )
                    ),
                    int(complete),
                    started,
                    finished,
                    timestamp,
                    job_id,
                ),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job_from_row(updated)

    def lease_next(
        self,
        owner_id: str,
        *,
        worker_classes: Sequence[str] | None = None,
        priority_classes: Sequence[str] | None = None,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> JobLease | None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        classes = tuple(worker_classes or WORKER_CLASSES)
        unknown = set(classes) - set(WORKER_CLASSES)
        if unknown:
            raise ValueError(f"unsupported worker classes: {sorted(unknown)}")
        priorities = tuple(priority_classes or PRIORITY_WEIGHTS)
        unknown_priorities = set(priorities) - set(PRIORITY_WEIGHTS)
        if unknown_priorities:
            raise ValueError(
                f"unsupported priority classes: {sorted(unknown_priorities)}"
            )
        if not classes or not priorities:
            raise ValueError("worker_classes and priority_classes cannot be empty")
        instant = now or _utcnow()
        now_text = _timestamp(instant)
        class_placeholders = ",".join("?" for _ in classes)
        priority_placeholders = ",".join("?" for _ in priorities)
        with self._immediate() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE status=? AND scheduled_for<=? AND not_before<=?
                  AND latest_start_at>=? AND deadline_at>?
                  AND (
                      SELECT COUNT(*) FROM attempts
                      WHERE attempts.job_id=jobs.job_id
                        AND attempts.status<>'preempted'
                  )<max_attempts
                  AND worker_class IN ({class_placeholders})
                  AND priority_class IN ({priority_placeholders})
                ORDER BY priority DESC, scheduled_for ASC, created_at ASC
                LIMIT 1
                """,
                (
                    JobStatus.QUEUED.value,
                    now_text,
                    now_text,
                    now_text,
                    now_text,
                    *classes,
                    *priorities,
                ),
            ).fetchone()
            if row is None:
                return None
            expires = _timestamp(
                min(
                    instant + timedelta(seconds=lease_seconds),
                    _parse_timestamp(row["deadline_at"], "deadline_at"),
                )
            )
            ensure_transition(JobStatus.QUEUED, JobStatus.LEASED)
            attempt_number = int(row["attempt_count"]) + 1
            token = uuid.uuid4().hex
            conn.execute(
                "UPDATE jobs SET status=?, attempt_count=?, version=version+1, updated_at=? WHERE job_id=?",
                (JobStatus.LEASED.value, attempt_number, now_text, row["job_id"]),
            )
            conn.execute(
                """
                INSERT INTO attempts(job_id, attempt_number, status, owner_id)
                VALUES (?, ?, ?, ?)
                """,
                (row["job_id"], attempt_number, JobStatus.LEASED.value, owner_id),
            )
            conn.execute(
                """
                INSERT INTO leases(job_id, lease_token, owner_id, attempt_number,
                                   acquired_at, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (row["job_id"], token, owner_id, attempt_number, now_text, now_text, expires),
            )
            leased_row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return JobLease(
            job=self._job_from_row(leased_row),
            token=token,
            owner_id=owner_id,
            acquired_at=now_text,
            expires_at=expires,
            attempt_number=attempt_number,
        )

    def mark_running(
        self,
        token: str,
        *,
        owner_id: str,
        attempt_number: int,
        worker_pid: int | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        timestamp = _timestamp(now)
        with self._immediate() as conn:
            lease, row = self._lease_and_job_locked(
                conn, token, timestamp, owner_id=owner_id, attempt_number=attempt_number
            )
            ensure_transition(JobStatus(row["status"]), JobStatus.RUNNING)
            conn.execute(
                """
                UPDATE jobs SET status=?, started_at=COALESCE(started_at, ?),
                    version=version+1, updated_at=? WHERE job_id=?
                """,
                (JobStatus.RUNNING.value, timestamp, timestamp, row["job_id"]),
            )
            conn.execute(
                """
                UPDATE attempts SET status=?, worker_pid=?, started_at=?
                WHERE job_id=? AND attempt_number=?
                """,
                (
                    JobStatus.RUNNING.value,
                    worker_pid,
                    timestamp,
                    row["job_id"],
                    lease["attempt_number"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._job_from_row(updated)

    def bind_worker_pid(
        self,
        token: str,
        *,
        owner_id: str,
        attempt_number: int,
        worker_pid: int,
        now: datetime | None = None,
    ) -> None:
        if worker_pid <= 0:
            raise ValueError("worker_pid must be positive")
        timestamp = _timestamp(now)
        with self._immediate() as conn:
            lease, row = self._lease_and_job_locked(
                conn, token, timestamp, owner_id=owner_id, attempt_number=attempt_number
            )
            if JobStatus(row["status"]) is not JobStatus.RUNNING:
                raise LeaseConflict("worker pid can only bind to a running lease")
            attempt = conn.execute(
                "SELECT worker_pid FROM attempts WHERE job_id=? AND attempt_number=?",
                (row["job_id"], lease["attempt_number"]),
            ).fetchone()
            if attempt is None or attempt["worker_pid"] is not None:
                raise LeaseConflict("worker pid is already bound or attempt is missing")
            conn.execute(
                """
                UPDATE attempts SET worker_pid=? WHERE job_id=? AND attempt_number=?
                """,
                (worker_pid, row["job_id"], lease["attempt_number"]),
            )

    def abandon_unstarted_lease(
        self,
        token: str,
        *,
        owner_id: str,
        attempt_number: int,
        error: Mapping[str, Any],
        now: datetime | None = None,
    ) -> JobRecord:
        """Atomically defer a leased job when no worker process was started."""

        timestamp = _timestamp(now)
        with self._immediate() as conn:
            lease, row = self._lease_and_job_locked(
                conn, token, timestamp, owner_id=owner_id, attempt_number=attempt_number
            )
            if JobStatus(row["status"]) is not JobStatus.LEASED:
                raise LeaseConflict("only an unstarted leased job can be abandoned")
            ensure_transition(JobStatus.LEASED, JobStatus.DEFERRED)
            canonical_error = _canonical_error(error)
            conn.execute(
                """
                UPDATE jobs SET status=?, error_json=?, finished_at=?,
                    version=version+1, updated_at=?
                WHERE job_id=?
                """,
                (
                    JobStatus.DEFERRED.value,
                    _json(canonical_error),
                    timestamp,
                    timestamp,
                    row["job_id"],
                ),
            )
            conn.execute(
                """
                UPDATE attempts SET status=?, finished_at=?, error_json=?
                WHERE job_id=? AND attempt_number=?
                """,
                (
                    JobStatus.DEFERRED.value,
                    timestamp,
                    _json(canonical_error),
                    row["job_id"],
                    lease["attempt_number"],
                ),
            )
            conn.execute("DELETE FROM leases WHERE lease_token=?", (token,))
            updated = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._job_from_row(updated)

    def heartbeat_lease(
        self,
        token: str,
        *,
        owner_id: str,
        attempt_number: int,
        extend_seconds: int,
        now: datetime | None = None,
    ) -> str:
        if extend_seconds < 1:
            raise ValueError("extend_seconds must be positive")
        instant = now or _utcnow()
        now_text = _timestamp(instant)
        with self._immediate() as conn:
            _lease, row = self._lease_and_job_locked(
                conn, token, now_text, owner_id=owner_id, attempt_number=attempt_number
            )
            expires = _timestamp(
                min(
                    instant + timedelta(seconds=extend_seconds),
                    _parse_timestamp(row["deadline_at"], "deadline_at"),
                )
            )
            conn.execute(
                "UPDATE leases SET heartbeat_at=?, expires_at=? WHERE lease_token=?",
                (now_text, expires, token),
            )
        return expires

    def suspend_lease(
        self,
        token: str,
        target: JobStatus | str,
        *,
        owner_id: str,
        attempt_number: int,
        result: Any = None,
        error: Any = None,
        now: datetime | None = None,
    ) -> JobRecord:
        normalized = JobStatus(target)
        if normalized not in {
            JobStatus.DEFERRED,
            JobStatus.NEEDS_CONFIRMATION,
            JobStatus.WAITING_CHILDREN,
            JobStatus.AWAITING_INPUT,
        }:
            raise InvalidTransition("suspend_lease requires a suspended target state")
        return self._finish_lease(
            token,
            normalized,
            owner_id=owner_id,
            attempt_number=attempt_number,
            result=result,
            error=error,
            now=now,
        )

    def _commit_worker_result(
        self,
        token: str,
        target: JobStatus | str,
        *,
        owner_id: str,
        attempt_number: int,
        result: Any = None,
        error: Any = None,
        metrics: Mapping[str, Any] | None = None,
        business_completed: bool = False,
        outbox: Sequence[OutboxSpec] = (),
        now: datetime | None = None,
    ) -> JobRecord:
        """Dispatcher-only primitive for atomically committing a terminal worker result."""

        normalized = JobStatus(target)
        if normalized not in {
            JobStatus.SUCCEEDED,
            JobStatus.DEGRADED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }:
            raise InvalidTransition("worker result commit requires a terminal target state")
        return self._finish_lease(
            token,
            normalized,
            owner_id=owner_id,
            attempt_number=attempt_number,
            result=result,
            error=error,
            metrics=metrics,
            business_completed=business_completed,
            outbox=outbox,
            now=now,
        )

    def _finish_lease(
        self,
        token: str,
        target: JobStatus | str,
        *,
        owner_id: str,
        attempt_number: int,
        result: Any = None,
        error: Any = None,
        metrics: Mapping[str, Any] | None = None,
        business_completed: bool = False,
        outbox: Sequence[OutboxSpec] = (),
        now: datetime | None = None,
    ) -> JobRecord:
        for outbox_spec in outbox:
            outbox_spec.validate()
        extracted_artifacts: Sequence[Mapping[str, Any]] = ()
        extracted_receipts: Sequence[Mapping[str, Any]] = ()
        if isinstance(result, Mapping):
            raw_artifacts = result.get("artifacts", ())
            raw_receipts = result.get("side_effect_receipts", ())
            if isinstance(raw_artifacts, Sequence):
                extracted_artifacts = raw_artifacts
            elif raw_artifacts is not None:
                raise ValueError("result.artifacts must be an array")
            if isinstance(raw_receipts, Sequence):
                extracted_receipts = raw_receipts
            elif raw_receipts is not None:
                raise ValueError("result.side_effect_receipts must be an array")
        artifacts = _canonical_artifacts(extracted_artifacts)
        receipts = _canonical_receipts(extracted_receipts)
        canonical_metrics = _canonical_metrics(metrics or {})
        instant = now or _utcnow()
        timestamp = _timestamp(instant)
        with self._immediate() as conn:
            lease, row = self._lease_and_job_locked(
                conn, token, timestamp, owner_id=owner_id, attempt_number=attempt_number
            )
            target_status = ensure_transition(
                JobStatus(row["status"]), target, business_completed=business_completed
            )
            if target_status in {JobStatus.FAILED, JobStatus.TIMED_OUT} and error is None:
                raise InvalidTransition(f"{target_status.value} requires an error payload")
            canonical_error = _canonical_error(error) if error is not None else None
            if target_status not in {
                JobStatus.SUCCEEDED,
                JobStatus.DEGRADED,
                JobStatus.FAILED,
                JobStatus.DEFERRED,
                JobStatus.CANCELLED,
                JobStatus.TIMED_OUT,
                JobStatus.NEEDS_CONFIRMATION,
                JobStatus.WAITING_CHILDREN,
                JobStatus.AWAITING_INPUT,
            }:
                raise InvalidTransition(f"finish_lease cannot target {target_status.value}")
            terminal = target_status in {
                JobStatus.SUCCEEDED,
                JobStatus.DEGRADED,
                JobStatus.FAILED,
                JobStatus.DEFERRED,
                JobStatus.CANCELLED,
                JobStatus.TIMED_OUT,
            }
            finished_at = timestamp if terminal else None
            commit_phase = row["commit_phase"]
            ambiguous_side_effect = bool(row["ambiguous_side_effect"])
            if row["side_effect_class"] in WRITE_SIDE_EFFECT_CLASSES:
                if target_status is JobStatus.SUCCEEDED:
                    if row["side_effect_class"] in DANGEROUS_SIDE_EFFECT_CLASSES and not receipts:
                        raise InvalidTransition(
                            "dangerous successful completion requires a side-effect receipt"
                        )
                    commit_phase = "verified"
                    ambiguous_side_effect = False
                elif target_status in {JobStatus.FAILED, JobStatus.TIMED_OUT}:
                    commit_phase = "ambiguous"
                    ambiguous_side_effect = True
            conn.execute(
                """
                UPDATE jobs SET status=?, result_json=?, error_json=?, business_completed=?,
                    finished_at=?, artifacts_json=?, side_effect_receipts_json=?, metrics_json=?,
                    commit_phase=?, ambiguous_side_effect=?, version=version+1, updated_at=?
                WHERE job_id=?
                """,
                (
                    target_status.value,
                    _json(result) if result is not None else row["result_json"],
                    (
                        None
                        if target_status is JobStatus.SUCCEEDED
                        else (
                            _json(canonical_error)
                            if canonical_error is not None
                            else row["error_json"]
                        )
                    ),
                    int(business_completed),
                    finished_at,
                    _json(artifacts),
                    _json(receipts),
                    _json(canonical_metrics),
                    commit_phase,
                    int(ambiguous_side_effect),
                    timestamp,
                    row["job_id"],
                ),
            )
            conn.execute(
                """
                UPDATE attempts SET status=?, finished_at=?, result_json=?, error_json=?, metrics_json=?
                WHERE job_id=? AND attempt_number=?
                """,
                (
                    target_status.value,
                    timestamp,
                    _json(result) if result is not None else None,
                    _json(canonical_error) if canonical_error is not None else None,
                    _json(canonical_metrics),
                    row["job_id"],
                    lease["attempt_number"],
                ),
            )
            for outbox_spec in outbox:
                self._insert_outbox_locked(
                    conn,
                    outbox_spec,
                    job_id=row["job_id"],
                    now=instant,
                )
            conn.execute("DELETE FROM leases WHERE lease_token=?", (token,))
            updated = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._job_from_row(updated)

    def requeue_expired_leases(self, *, now: datetime | None = None) -> int:
        raise LedgerError(
            "expired leases must be fenced, have their process group drained, and then resolved"
        )

    def fence_preemptible_lease(
        self,
        token: str,
        *,
        owner_id: str,
        attempt_number: int,
        incoming_priority_class: str,
        now: datetime | None = None,
    ) -> FencedLease:
        """Fence one lower-priority safe heavy lease before terminating its group."""

        if incoming_priority_class not in PRIORITY_WEIGHTS:
            raise ValueError("incoming priority class is invalid")
        timestamp = _timestamp(now)
        with self._immediate() as conn:
            lease = conn.execute(
                "SELECT * FROM leases WHERE lease_token=?", (token,)
            ).fetchone()
            if (
                lease is None
                or lease["owner_id"] != owner_id
                or int(lease["attempt_number"]) != int(attempt_number)
                or lease["fenced_at"] is not None
                or lease["expires_at"] <= timestamp
            ):
                raise LeaseConflict(
                    "preemption lease token, owner, attempt, or lifetime does not match"
                )
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease["job_id"],)
            ).fetchone()
            if row is None or JobStatus(row["status"]) is not JobStatus.RUNNING:
                raise LeaseConflict("only a running lease can be preempted")
            if (
                not bool(row["preemptible"])
                or row["worker_class"] not in HEAVY_WORKER_CLASSES
                or row["side_effect_class"]
                not in SAFE_PREEMPTION_SIDE_EFFECT_CLASSES
                or PRIORITY_WEIGHTS[incoming_priority_class]
                <= PRIORITY_WEIGHTS[str(row["priority_class"])]
            ):
                raise LeaseConflict(
                    "lease is not a lower-priority safe preemptible heavy worker"
                )
            reason = f"priority_preempted:{incoming_priority_class}"
            conn.execute(
                """
                UPDATE leases SET fenced_at=?, fence_reason=?,
                    fence_generation=fence_generation+1
                WHERE lease_token=?
                """,
                (timestamp, reason, token),
            )
            fenced = conn.execute(
                "SELECT * FROM leases WHERE lease_token=?", (token,)
            ).fetchone()
            attempt = conn.execute(
                "SELECT worker_pid FROM attempts WHERE job_id=? AND attempt_number=?",
                (lease["job_id"], attempt_number),
            ).fetchone()
        return FencedLease(
            job=self._job_from_row(row),
            token=token,
            owner_id=owner_id,
            attempt_number=attempt_number,
            fenced_at=str(fenced["fenced_at"]),
            fence_generation=int(fenced["fence_generation"]),
            worker_pid=None if attempt is None else attempt["worker_pid"],
        )

    def cancel_preemption_fence(
        self,
        token: str,
        *,
        owner_id: str,
        attempt_number: int,
        fence_generation: int,
    ) -> JobRecord:
        """Restore a still-owned worker lease when preemption did not drain it."""

        with self._immediate() as conn:
            lease = conn.execute(
                "SELECT * FROM leases WHERE lease_token=?", (token,)
            ).fetchone()
            if (
                lease is None
                or lease["owner_id"] != owner_id
                or int(lease["attempt_number"]) != int(attempt_number)
                or lease["fenced_at"] is None
                or int(lease["fence_generation"]) != int(fence_generation)
                or not str(lease["fence_reason"] or "").startswith(
                    "priority_preempted:"
                )
            ):
                raise LeaseConflict("preemption fence generation does not match")
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease["job_id"],)
            ).fetchone()
            if row is None or JobStatus(row["status"]) is not JobStatus.RUNNING:
                raise LeaseConflict("preempted worker is no longer running")
            conn.execute(
                "UPDATE leases SET fenced_at=NULL, fence_reason=NULL WHERE lease_token=?",
                (token,),
            )
        return self._job_from_row(row)

    def resolve_preempted_lease(
        self,
        token: str,
        *,
        owner_id: str,
        attempt_number: int,
        fence_generation: int,
        process_group_gone: bool,
        incoming_priority_class: str,
        now: datetime | None = None,
    ) -> JobRecord:
        """Atomically mark the attempt preempted and requeue after PGID proof."""

        if not process_group_gone:
            raise LeaseConflict(
                "cannot requeue a preempted lease while its process group exists"
            )
        if incoming_priority_class not in PRIORITY_WEIGHTS:
            raise ValueError("incoming priority class is invalid")
        timestamp = _timestamp(now)
        with self._immediate() as conn:
            lease = conn.execute(
                "SELECT * FROM leases WHERE lease_token=?", (token,)
            ).fetchone()
            expected_reason = f"priority_preempted:{incoming_priority_class}"
            if (
                lease is None
                or lease["owner_id"] != owner_id
                or int(lease["attempt_number"]) != int(attempt_number)
                or lease["fenced_at"] is None
                or int(lease["fence_generation"]) != int(fence_generation)
                or lease["fence_reason"] != expected_reason
            ):
                raise LeaseConflict("preempted lease generation does not match")
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease["job_id"],)
            ).fetchone()
            if (
                row is None
                or JobStatus(row["status"]) is not JobStatus.RUNNING
                or not bool(row["preemptible"])
                or row["worker_class"] not in HEAVY_WORKER_CLASSES
                or row["side_effect_class"]
                not in SAFE_PREEMPTION_SIDE_EFFECT_CLASSES
            ):
                raise LeaseConflict("preempted job safety contract no longer matches")
            error = _canonical_error(
                {
                    "code": "worker_preempted_requeued",
                    "incoming_priority_class": incoming_priority_class,
                }
            )
            conn.execute(
                """
                UPDATE jobs SET status=?, error_json=?, finished_at=NULL,
                    version=version+1, updated_at=? WHERE job_id=?
                """,
                (
                    JobStatus.QUEUED.value,
                    _json(error),
                    timestamp,
                    row["job_id"],
                ),
            )
            conn.execute(
                """
                UPDATE attempts SET status='preempted', finished_at=?, error_json=?
                WHERE job_id=? AND attempt_number=?
                """,
                (timestamp, _json(error), row["job_id"], attempt_number),
            )
            conn.execute("DELETE FROM leases WHERE lease_token=?", (token,))
            updated = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._job_from_row(updated)

    def mark_preemption_recovery_required(
        self,
        token: str,
        *,
        owner_id: str,
        attempt_number: int,
        fence_generation: int,
        process_group_gone: bool,
        error: Mapping[str, Any],
        now: datetime | None = None,
    ) -> JobRecord:
        """Fail closed after PGID drain when automatic requeue could not commit."""

        if not process_group_gone:
            raise LeaseConflict(
                "cannot close a preemption recovery lease while its process group exists"
            )
        timestamp = _timestamp(now)
        canonical_error = _canonical_error(error)
        with self._immediate() as conn:
            lease = conn.execute(
                "SELECT * FROM leases WHERE lease_token=?", (token,)
            ).fetchone()
            if (
                lease is None
                or lease["owner_id"] != owner_id
                or int(lease["attempt_number"]) != int(attempt_number)
                or lease["fenced_at"] is None
                or int(lease["fence_generation"]) != int(fence_generation)
                or not str(lease["fence_reason"] or "").startswith(
                    "priority_preempted:"
                )
            ):
                raise LeaseConflict("preemption recovery fence generation does not match")
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease["job_id"],)
            ).fetchone()
            if row is None or JobStatus(row["status"]) is not JobStatus.RUNNING:
                raise LeaseConflict("preemption recovery job is not running")
            conn.execute(
                """
                UPDATE jobs SET status=?, error_json=?, finished_at=?,
                    version=version+1, updated_at=? WHERE job_id=?
                """,
                (
                    JobStatus.DEFERRED.value,
                    _json(canonical_error),
                    timestamp,
                    timestamp,
                    row["job_id"],
                ),
            )
            conn.execute(
                """
                UPDATE attempts SET status='preempted_recovery_required',
                    finished_at=?, error_json=?
                WHERE job_id=? AND attempt_number=?
                """,
                (timestamp, _json(canonical_error), row["job_id"], attempt_number),
            )
            conn.execute("DELETE FROM leases WHERE lease_token=?", (token,))
            updated = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._job_from_row(updated)

    def fence_lease(
        self,
        token: str,
        *,
        owner_id: str,
        attempt_number: int,
        reason: str = "lease_expired",
        now: datetime | None = None,
    ) -> FencedLease:
        """Atomically reject future heartbeat/commit without changing job dispatch state."""

        timestamp = _timestamp(now)
        with self._immediate() as conn:
            lease = conn.execute(
                "SELECT * FROM leases WHERE lease_token=?", (token,)
            ).fetchone()
            if (
                lease is None
                or lease["owner_id"] != owner_id
                or int(lease["attempt_number"]) != int(attempt_number)
            ):
                raise LeaseConflict("lease token, owner, or attempt generation does not match")
            if lease["fenced_at"] is None:
                if lease["expires_at"] > timestamp:
                    raise LeaseConflict("cannot fence an unexpired lease")
                conn.execute(
                    """
                    UPDATE leases SET fenced_at=?, fence_reason=?,
                        fence_generation=fence_generation+1 WHERE lease_token=?
                    """,
                    (timestamp, reason, token),
                )
                lease = conn.execute(
                    "SELECT * FROM leases WHERE lease_token=?", (token,)
                ).fetchone()
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease["job_id"],)
            ).fetchone()
            attempt = conn.execute(
                "SELECT worker_pid FROM attempts WHERE job_id=? AND attempt_number=?",
                (lease["job_id"], attempt_number),
            ).fetchone()
        return FencedLease(
            job=self._job_from_row(row),
            token=token,
            owner_id=owner_id,
            attempt_number=attempt_number,
            fenced_at=lease["fenced_at"],
            fence_generation=int(lease["fence_generation"]),
            worker_pid=None if attempt is None else attempt["worker_pid"],
        )

    def resolve_fenced_lease(
        self,
        token: str,
        *,
        owner_id: str,
        attempt_number: int,
        fence_generation: int,
        process_group_gone: bool,
        now: datetime | None = None,
    ) -> JobRecord:
        """Resolve only after the owning dispatcher proves the worker group is gone."""

        if not process_group_gone:
            raise LeaseConflict("cannot resolve a fenced lease while its process group exists")
        timestamp = _timestamp(now)
        with self._immediate() as conn:
            lease = conn.execute(
                "SELECT * FROM leases WHERE lease_token=?", (token,)
            ).fetchone()
            if (
                lease is None
                or lease["owner_id"] != owner_id
                or int(lease["attempt_number"]) != int(attempt_number)
                or lease["fenced_at"] is None
                or int(lease["fence_generation"]) != int(fence_generation)
            ):
                raise LeaseConflict("fenced lease generation does not match")
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease["job_id"],)
            ).fetchone()
            if row["side_effect_class"] in WRITE_SIDE_EFFECT_CLASSES:
                target = JobStatus.NEEDS_CONFIRMATION
                error = {"code": "lease_expired_side_effect_unknown"}
            elif int(row["attempt_count"]) < int(row["max_attempts"]):
                target = JobStatus.QUEUED
                error = {"code": "lease_expired_requeued"}
            else:
                target = JobStatus.TIMED_OUT
                error = {"code": "lease_expired_attempts_exhausted"}
            terminal = target is JobStatus.TIMED_OUT
            canonical_error = _canonical_error(error)
            ambiguous = row["side_effect_class"] in WRITE_SIDE_EFFECT_CLASSES
            conn.execute(
                """
                UPDATE jobs SET status=?, error_json=?, finished_at=?, confirmed_at=?,
                    commit_phase=?, ambiguous_side_effect=?, version=version+1, updated_at=?
                WHERE job_id=?
                """,
                (
                    target.value,
                    _json(canonical_error),
                    timestamp if terminal else None,
                    None if target is JobStatus.NEEDS_CONFIRMATION else row["confirmed_at"],
                    "ambiguous" if ambiguous else row["commit_phase"],
                    int(ambiguous),
                    timestamp,
                    row["job_id"],
                ),
            )
            conn.execute(
                """
                UPDATE attempts SET status=?, finished_at=?, error_json=?
                WHERE job_id=? AND attempt_number=?
                """,
                (
                    JobStatus.TIMED_OUT.value,
                    timestamp,
                    _json(canonical_error),
                    row["job_id"],
                    attempt_number,
                ),
            )
            conn.execute("DELETE FROM leases WHERE lease_token=?", (token,))
            updated = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._job_from_row(updated)

    def enqueue_outbox(
        self,
        *,
        topic: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        job_id: str | None = None,
        outbox_id: str | None = None,
        ttl_seconds: int = 86400,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> OutboxRecord:
        spec = OutboxSpec(
            topic=topic,
            payload=payload,
            idempotency_key=idempotency_key,
            outbox_id=outbox_id,
            ttl_seconds=ttl_seconds,
            max_attempts=max_attempts,
        )
        spec.validate()
        instant = now or _utcnow()
        with self._immediate() as conn:
            row = self._insert_outbox_locked(conn, spec, job_id=job_id, now=instant)
        return self._outbox_from_row(row)

    def get_outbox(self, outbox_id: str) -> OutboxRecord:
        with self._connect_readonly() as conn:
            row = conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        if row is None:
            raise LedgerError(f"outbox record not found: {outbox_id}")
        return self._outbox_from_row(row)

    def claim_outbox(
        self,
        owner_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> OutboxRecord | None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        instant = now or _utcnow()
        now_text = _timestamp(instant)
        expires = _timestamp(instant + timedelta(seconds=lease_seconds))
        with self._immediate() as conn:
            conn.execute(
                """
                UPDATE outbox SET status='pending', locked_by=NULL, lock_expires_at=NULL,
                    claim_token_sha256=NULL, updated_at=?
                WHERE status='sending' AND lock_expires_at<=?
                """,
                (now_text, now_text),
            )
            conn.execute(
                """
                UPDATE outbox SET status='failed', dead_lettered_at=?, locked_by=NULL,
                    lock_expires_at=NULL, claim_token_sha256=NULL, updated_at=?
                WHERE status IN ('pending','failed') AND dead_lettered_at IS NULL
                  AND (delivery_expires_at<=? OR attempt_count>=max_attempts)
                """,
                (now_text, now_text, now_text),
            )
            row = conn.execute(
                """
                SELECT * FROM outbox
                WHERE status IN ('pending','failed') AND next_attempt_at<=?
                  AND dead_lettered_at IS NULL AND delivery_expires_at>?
                  AND attempt_count<max_attempts
                ORDER BY created_at LIMIT 1
                """,
                (now_text, now_text),
            ).fetchone()
            if row is None:
                return None
            claim_token = secrets.token_urlsafe(32)
            conn.execute(
                """
                UPDATE outbox SET status='sending', attempt_count=attempt_count+1,
                    locked_by=?, lock_expires_at=?, claim_token_sha256=?,
                    claim_generation=claim_generation+1, updated_at=? WHERE outbox_id=?
                """,
                (owner_id, expires, _token_digest(claim_token), now_text, row["outbox_id"]),
            )
            updated = conn.execute(
                "SELECT * FROM outbox WHERE outbox_id=?", (row["outbox_id"],)
            ).fetchone()
        return self._outbox_from_row(updated, claim_token=claim_token)

    def mark_outbox_sent(
        self,
        outbox_id: str,
        owner_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        provider_reference: str | None = None,
        now: datetime | None = None,
    ) -> OutboxRecord:
        return self._finish_outbox(
            outbox_id,
            owner_id,
            claim_token=claim_token,
            claim_generation=claim_generation,
            status="sent",
            provider_reference=provider_reference,
            error=None,
            retry_after_seconds=0,
            now=now,
        )

    def mark_outbox_failed(
        self,
        outbox_id: str,
        owner_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        error: str,
        retry_after_seconds: int = 60,
        now: datetime | None = None,
    ) -> OutboxRecord:
        return self._finish_outbox(
            outbox_id,
            owner_id,
            claim_token=claim_token,
            claim_generation=claim_generation,
            status="failed",
            provider_reference=None,
            error=error,
            retry_after_seconds=retry_after_seconds,
            now=now,
        )

    def counts(self) -> dict[str, int]:
        with self._connect_readonly() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status")
            return {str(row["status"]): int(row["count"]) for row in rows}

    @staticmethod
    def _insert_outbox_locked(
        conn: sqlite3.Connection,
        spec: OutboxSpec,
        *,
        job_id: str | None,
        now: datetime,
    ) -> sqlite3.Row:
        spec.validate()
        record_id = spec.outbox_id or uuid.uuid4().hex
        timestamp = _timestamp(now)
        delivery_expires_at = _timestamp(now + timedelta(seconds=spec.ttl_seconds))
        try:
            conn.execute(
                """
                INSERT INTO outbox(
                    outbox_id, job_id, topic, payload_json, idempotency_key, status,
                    created_at, updated_at, next_attempt_at, delivery_expires_at,
                    max_attempts
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    job_id,
                    spec.topic,
                    _json(dict(spec.payload)),
                    spec.idempotency_key,
                    timestamp,
                    timestamp,
                    timestamp,
                    delivery_expires_at,
                    spec.max_attempts,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"duplicate or invalid outbox record: {record_id}") from exc
        return conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (record_id,)).fetchone()

    def _finish_outbox(
        self,
        outbox_id: str,
        owner_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        status: str,
        provider_reference: str | None,
        error: str | None,
        retry_after_seconds: int,
        now: datetime | None,
    ) -> OutboxRecord:
        instant = now or _utcnow()
        now_text = _timestamp(instant)
        retry_at = _timestamp(instant + timedelta(seconds=max(0, retry_after_seconds)))
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"outbox record not found: {outbox_id}")
            expected_digest = row["claim_token_sha256"]
            claim_valid = bool(
                claim_token
                and expected_digest
                and hmac.compare_digest(expected_digest, _token_digest(claim_token))
            )
            if (
                row["status"] != "sending"
                or row["locked_by"] != owner_id
                or int(row["claim_generation"]) != int(claim_generation)
                or row["lock_expires_at"] is None
                or row["lock_expires_at"] <= now_text
                or row["delivery_expires_at"] <= now_text
                or not claim_valid
            ):
                raise LeaseConflict(f"outbox record is not leased by {owner_id}")
            dead_lettered_at = None
            if status == "failed" and (
                int(row["attempt_count"]) >= int(row["max_attempts"])
                or retry_at >= row["delivery_expires_at"]
            ):
                dead_lettered_at = now_text
            conn.execute(
                """
                UPDATE outbox SET status=?, updated_at=?, next_attempt_at=?, locked_by=NULL,
                    lock_expires_at=NULL, claim_token_sha256=NULL, provider_reference=?,
                    last_error=?, dead_lettered_at=? WHERE outbox_id=?
                """,
                (
                    status,
                    now_text,
                    retry_at,
                    provider_reference,
                    error,
                    dead_lettered_at,
                    outbox_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
        return self._outbox_from_row(updated)

    def _lease_and_job_locked(
        self,
        conn: sqlite3.Connection,
        token: str,
        now_text: str,
        *,
        owner_id: str,
        attempt_number: int,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        lease = conn.execute(
            "SELECT * FROM leases WHERE lease_token=?", (token,)
        ).fetchone()
        if (
            lease is None
            or lease["owner_id"] != owner_id
            or int(lease["attempt_number"]) != int(attempt_number)
            or lease["fenced_at"] is not None
            or lease["expires_at"] <= now_text
        ):
            raise LeaseConflict("lease is missing or expired")
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (lease["job_id"],)).fetchone()
        if row is None:
            raise LeaseConflict("leased job no longer exists")
        if row["deadline_at"] <= now_text:
            raise LeaseConflict("job deadline has expired")
        return lease, row

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        try:
            schema_version = str(row["envelope_schema_version"])
            priority_class = str(row["priority_class"])
            side_effect_class = str(row["side_effect_class"])
            commit_phase = str(row["commit_phase"])
            status = JobStatus(row["status"])
            input_payload = _decode(row["input_json"])
            result = _decode(row["result_json"])
            error = _decode(row["error_json"])
            resource_claim = _canonical_resource_claim(_decode(row["resource_claim_json"]))
            artifacts = _canonical_artifacts(_decode(row["artifacts_json"]))
            receipts = _canonical_receipts(_decode(row["side_effect_receipts_json"]))
            metrics = _canonical_metrics(_decode(row["metrics_json"]))
            created_at = _parse_timestamp(row["created_at"], "created_at")
            scheduled_for = _parse_timestamp(row["scheduled_for"], "scheduled_for")
            not_before = _parse_timestamp(row["not_before"], "not_before")
            latest_start_at = _parse_timestamp(row["latest_start_at"], "latest_start_at")
            deadline_at = _parse_timestamp(row["deadline_at"], "deadline_at")
            timeout_sec = int(row["timeout_sec"])
            queue_ttl_sec = int(row["queue_ttl_sec"])
            preemptible_raw = int(row["preemptible"])
            ambiguous_raw = int(row["ambiguous_side_effect"])
            business_completed_raw = int(row["business_completed"])
            if schema_version != "3.0":
                raise ValueError("unsupported envelope schema version")
            if priority_class not in PRIORITY_WEIGHTS:
                raise ValueError("unsupported priority class")
            if side_effect_class not in SIDE_EFFECT_CLASSES:
                raise ValueError("unsupported side-effect class")
            if commit_phase not in COMMIT_PHASES:
                raise ValueError("unsupported commit phase")
            if not isinstance(input_payload, dict):
                raise ValueError("input payload is not an object")
            if preemptible_raw not in {0, 1} or ambiguous_raw not in {0, 1}:
                raise ValueError("boolean ledger field is not canonical")
            if bool(preemptible_raw) is not PRIORITY_PREEMPTIBLE[priority_class]:
                raise ValueError("preemptible conflicts with priority class")
            if business_completed_raw not in {0, 1}:
                raise ValueError("business_completed is not canonical")
            if not 1 <= timeout_sec <= 86400 or not 1 <= queue_ttl_sec <= 604800:
                raise ValueError("timeout or queue TTL is outside the contract")
            if not scheduled_for <= not_before <= latest_start_at:
                raise ValueError("invalid scheduling order")
            if latest_start_at > scheduled_for + timedelta(seconds=queue_ttl_sec):
                raise ValueError("latest start exceeds queue TTL")
            if deadline_at < latest_start_at + timedelta(seconds=timeout_sec):
                raise ValueError("deadline does not contain the timeout window")
            ambiguous = bool(ambiguous_raw)
            if ambiguous is not (commit_phase == "ambiguous"):
                raise ValueError("ambiguous_side_effect conflicts with commit_phase")
            if side_effect_class not in WRITE_SIDE_EFFECT_CLASSES and commit_phase != "not_applicable":
                raise ValueError("read-only job has a mutating commit phase")
            if status in {JobStatus.FAILED, JobStatus.TIMED_OUT}:
                error = _canonical_error(error)
            elif error is not None:
                error = _canonical_error(error)
            if status in {
                JobStatus.SUCCEEDED,
                JobStatus.DEGRADED,
                JobStatus.FAILED,
                JobStatus.DEFERRED,
                JobStatus.SKIPPED,
                JobStatus.CANCELLED,
                JobStatus.TIMED_OUT,
            } and row["finished_at"] is None:
                raise ValueError("terminal/deferred job lacks finished_at")
            if status is JobStatus.SUCCEEDED and not bool(business_completed_raw):
                raise ValueError("succeeded job lacks business completion")
            if side_effect_class in {
                "reversible_write",
                "external_commit",
                "destructive",
            } and not str(row["idempotency_key"] or "").strip():
                raise ValueError("mutating job lacks idempotency key")
        except (InvalidTransition, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LedgerError(f"job row violates canonical envelope invariants: {exc}") from exc

        return JobRecord(
            schema_version=schema_version,
            job_id=row["job_id"],
            capability=row["capability"],
            operation=row["operation"],
            worker_class=row["worker_class"],
            side_effect_class=side_effect_class,
            priority_class=priority_class,
            priority=int(row["priority"]),
            status=status,
            input=input_payload,
            result=result,
            error=error,
            created_at=row["created_at"],
            scheduled_for=row["scheduled_for"],
            latest_start_at=row["latest_start_at"],
            deadline_at=row["deadline_at"],
            not_before=row["not_before"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            timeout_sec=timeout_sec,
            queue_ttl_sec=queue_ttl_sec,
            preemptible=bool(preemptible_raw),
            resource_claim=resource_claim,
            commit_phase=commit_phase,
            ambiguous_side_effect=ambiguous,
            idempotency_key=row["idempotency_key"],
            confirmation_expires_at=row["confirmation_expires_at"],
            confirmed_at=row["confirmed_at"],
            business_completed=bool(business_completed_raw),
            artifacts=artifacts,
            side_effect_receipts=receipts,
            metrics=metrics,
            version=int(row["version"]),
        )

    @staticmethod
    def _outbox_from_row(
        row: sqlite3.Row,
        *,
        claim_token: str | None = None,
    ) -> OutboxRecord:
        return OutboxRecord(
            outbox_id=row["outbox_id"],
            job_id=row["job_id"],
            topic=row["topic"],
            payload=_decode(row["payload_json"]),
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            attempt_count=int(row["attempt_count"]),
            locked_by=row["locked_by"],
            lock_expires_at=row["lock_expires_at"],
            claim_token=claim_token,
            claim_generation=int(row["claim_generation"]),
            delivery_expires_at=row["delivery_expires_at"],
            max_attempts=int(row["max_attempts"]),
            dead_lettered_at=row["dead_lettered_at"],
        )
