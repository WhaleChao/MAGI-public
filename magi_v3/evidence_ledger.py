"""Release-bound evidence envelopes and an append-only local ledger.

The deployment marker remains the authority for which MAGI release is active.
This ledger records that identity alongside validation and operational evidence
so an artifact produced by a predecessor can remain auditable without being
projected as the current release's health.

The ledger deliberately stores metadata and a bounded JSON receipt, not legal
case content.  Compatibility ``*_latest.json`` files are projections from the
active-release view and are never used as the ledger's source of truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


EVIDENCE_ENVELOPE_SCHEMA = "magi.evidence-envelope/v2"
EVIDENCE_LEDGER_SCHEMA = "magi.evidence-ledger/v1"
ACTIVE_POINTER_SCHEMA = "magi.evidence-active-release-pointer/v1"
STATUS_CLASSES = frozenset(
    {"release_acceptance", "live_health", "business_backlog", "human_attention"}
)
OUTCOMES = frozenset(
    {"passed", "failed", "waiting", "attention", "observed", "superseded"}
)
DATA_CLASSIFICATIONS = frozenset(
    {"public", "operational", "office_confidential", "case_confidential", "secret"}
)
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceLedgerError(ValueError):
    """Raised when evidence cannot be trusted or safely persisted."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any, field: str, *, required: bool = True) -> datetime | None:
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EvidenceLedgerError(f"{field} must be an RFC3339 timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EvidenceLedgerError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceLedgerError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceLedgerError("evidence must be canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _token(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _TOKEN_RE.fullmatch(text):
        raise EvidenceLedgerError(f"{field} is invalid")
    return text


def _component(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise EvidenceLedgerError(f"{field} must be an object")
    return {
        "name": _token(value.get("name"), f"{field}.name"),
        "version": _token(value.get("version"), f"{field}.version"),
    }


def _safe_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceLedgerError("receipt must be an object")
    payload = dict(value)
    encoded = _canonical(payload)
    if len(encoded) > 64 * 1024:
        raise EvidenceLedgerError("receipt exceeds 64 KiB")
    forbidden = {"prompt", "content", "case_content", "password", "token", "secret"}
    for key in payload:
        lowered = str(key).lower()
        if any(marker in lowered for marker in forbidden):
            raise EvidenceLedgerError(f"receipt contains forbidden field: {key}")
    return payload


@dataclass(frozen=True, slots=True)
class EvidenceEnvelope:
    evidence_id: str
    release_id: str
    source_commit: str
    producer: Mapping[str, str]
    validator: Mapping[str, str]
    generated_at: str
    expires_at: str | None
    subject: str
    status_class: str
    outcome: str
    reason_code: str
    trace_id: str
    receipt: Mapping[str, Any]
    data_classification: str = "operational"

    @classmethod
    def create(
        cls,
        *,
        release_id: str,
        source_commit: str,
        producer: Mapping[str, str],
        validator: Mapping[str, str],
        subject: str,
        status_class: str,
        outcome: str,
        reason_code: str,
        trace_id: str,
        receipt: Mapping[str, Any],
        generated_at: datetime | None = None,
        expires_at: datetime | None = None,
        data_classification: str = "operational",
    ) -> "EvidenceEnvelope":
        generated = (generated_at or _utc_now()).astimezone(timezone.utc)
        expires = expires_at.astimezone(timezone.utc) if expires_at else None
        identity = {
            "release_id": release_id,
            "source_commit": source_commit,
            "producer": dict(producer),
            "validator": dict(validator),
            "generated_at": generated.isoformat(),
            "expires_at": expires.isoformat() if expires else None,
            "subject": subject,
            "status_class": status_class,
            "outcome": outcome,
            "reason_code": reason_code,
            "trace_id": trace_id,
            "receipt": dict(receipt),
            "data_classification": data_classification,
        }
        return cls(evidence_id=_digest(identity), **identity)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvidenceEnvelope":
        if value.get("schema") != EVIDENCE_ENVELOPE_SCHEMA:
            raise EvidenceLedgerError("unsupported evidence envelope schema")
        fields = {
            "evidence_id",
            "release_id",
            "source_commit",
            "producer",
            "validator",
            "generated_at",
            "expires_at",
            "subject",
            "status_class",
            "outcome",
            "reason_code",
            "trace_id",
            "receipt",
            "data_classification",
            "envelope_sha256",
        }
        unknown = set(value) - fields - {"schema"}
        if unknown:
            raise EvidenceLedgerError(
                "evidence envelope contains unknown fields: " + ", ".join(sorted(unknown))
            )
        envelope = cls(
            evidence_id=str(value.get("evidence_id") or ""),
            release_id=str(value.get("release_id") or ""),
            source_commit=str(value.get("source_commit") or ""),
            producer=_component(value.get("producer"), "producer"),
            validator=_component(value.get("validator"), "validator"),
            generated_at=str(value.get("generated_at") or ""),
            expires_at=(str(value.get("expires_at")) if value.get("expires_at") else None),
            subject=str(value.get("subject") or ""),
            status_class=str(value.get("status_class") or ""),
            outcome=str(value.get("outcome") or ""),
            reason_code=str(value.get("reason_code") or ""),
            trace_id=str(value.get("trace_id") or ""),
            receipt=_safe_receipt(value.get("receipt")),
            data_classification=str(value.get("data_classification") or ""),
        )
        envelope.validate()
        expected_sha = envelope.envelope_sha256()
        supplied_sha = value.get("envelope_sha256")
        if supplied_sha is not None and supplied_sha != expected_sha:
            raise EvidenceLedgerError("evidence envelope SHA-256 mismatch")
        return envelope

    def validate(self) -> None:
        if not _SHA256_RE.fullmatch(self.evidence_id):
            raise EvidenceLedgerError("evidence_id must be a SHA-256")
        if not _RELEASE_ID_RE.fullmatch(self.release_id):
            raise EvidenceLedgerError("release_id is invalid")
        if not _COMMIT_RE.fullmatch(self.source_commit):
            raise EvidenceLedgerError("source_commit must be a full Git commit")
        _component(self.producer, "producer")
        _component(self.validator, "validator")
        generated = _parse_time(self.generated_at, "generated_at")
        expires = _parse_time(self.expires_at, "expires_at", required=False)
        if expires is not None and generated is not None and expires <= generated:
            raise EvidenceLedgerError("expires_at must be after generated_at")
        _token(self.subject, "subject")
        if self.status_class not in STATUS_CLASSES:
            raise EvidenceLedgerError("status_class is invalid")
        if self.outcome not in OUTCOMES:
            raise EvidenceLedgerError("outcome is invalid")
        _token(self.reason_code, "reason_code")
        if not _TRACE_ID_RE.fullmatch(self.trace_id):
            raise EvidenceLedgerError("trace_id must be 16-byte lowercase hex")
        _safe_receipt(self.receipt)
        if self.data_classification not in DATA_CLASSIFICATIONS:
            raise EvidenceLedgerError("data_classification is invalid")
        identity = self._identity()
        if self.evidence_id != _digest(identity):
            raise EvidenceLedgerError("evidence_id does not match envelope identity")

    def _identity(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "source_commit": self.source_commit,
            "producer": dict(self.producer),
            "validator": dict(self.validator),
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
            "subject": self.subject,
            "status_class": self.status_class,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "trace_id": self.trace_id,
            "receipt": dict(self.receipt),
            "data_classification": self.data_classification,
        }

    def envelope_sha256(self) -> str:
        return _digest({"schema": EVIDENCE_ENVELOPE_SCHEMA, "evidence_id": self.evidence_id, **self._identity()})

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        body = {"schema": EVIDENCE_ENVELOPE_SCHEMA, "evidence_id": self.evidence_id, **self._identity()}
        body["envelope_sha256"] = self.envelope_sha256()
        return body


class EvidenceLedger:
    """Small append-only SQLite ledger with an active-release projection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    release_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    status_class TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    expires_at TEXT,
                    envelope_sha256 TEXT NOT NULL,
                    envelope_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS evidence_release_subject_time
                    ON evidence(release_id, subject, generated_at DESC, evidence_id DESC);
                CREATE TABLE IF NOT EXISTS active_release_pointer (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    release_id TEXT NOT NULL,
                    source_commit TEXT NOT NULL,
                    marker_sha256 TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                """
            )

    def append(self, envelope: EvidenceEnvelope | Mapping[str, Any]) -> dict[str, Any]:
        value = envelope if isinstance(envelope, EvidenceEnvelope) else EvidenceEnvelope.from_mapping(envelope)
        payload = value.as_dict()
        encoded = _canonical(payload).decode("utf-8")
        digest = payload["envelope_sha256"]
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT envelope_sha256 FROM evidence WHERE evidence_id = ?",
                (value.evidence_id,),
            ).fetchone()
            if existing is not None:
                if existing["envelope_sha256"] != digest:
                    raise EvidenceLedgerError("evidence_id collision")
                return payload
            connection.execute(
                """
                INSERT INTO evidence
                    (evidence_id, release_id, subject, status_class, outcome,
                     generated_at, expires_at, envelope_sha256, envelope_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.evidence_id,
                    value.release_id,
                    value.subject,
                    value.status_class,
                    value.outcome,
                    value.generated_at,
                    value.expires_at,
                    digest,
                    encoded,
                ),
            )
        return payload

    def bind_active_release(
        self,
        *,
        release_id: str,
        source_commit: str,
        marker_sha256: str,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        if not _RELEASE_ID_RE.fullmatch(release_id):
            raise EvidenceLedgerError("release_id is invalid")
        if not _COMMIT_RE.fullmatch(source_commit):
            raise EvidenceLedgerError("source_commit must be a full Git commit")
        if not _SHA256_RE.fullmatch(marker_sha256):
            raise EvidenceLedgerError("marker_sha256 is invalid")
        observed = (observed_at or _utc_now()).astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO active_release_pointer
                    (singleton, release_id, source_commit, marker_sha256, observed_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    release_id=excluded.release_id,
                    source_commit=excluded.source_commit,
                    marker_sha256=excluded.marker_sha256,
                    observed_at=excluded.observed_at
                """,
                (release_id, source_commit, marker_sha256, observed),
            )
        return {
            "schema": ACTIVE_POINTER_SCHEMA,
            "release_id": release_id,
            "source_commit": source_commit,
            "marker_sha256": marker_sha256,
            "observed_at": observed,
        }

    def bind_active_marker(
        self,
        marker_path: Path | str,
        *,
        release_manifest_path: Path | str | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Mirror a hash-bound deployment marker; never invent release identity."""

        marker = Path(marker_path).expanduser()
        if not marker.is_absolute() or marker.is_symlink() or not marker.is_file():
            raise EvidenceLedgerError("active release marker must be an absolute regular file")
        try:
            marker_bytes = marker.read_bytes()
            value = json.loads(marker_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceLedgerError("active release marker is invalid") from exc
        if not isinstance(value, Mapping) or value.get("schema") != "magi.v3.active-release/v1":
            raise EvidenceLedgerError("active release marker schema is invalid")
        release_id = str(value.get("release_id") or "")
        release_root = Path(str(value.get("release_root") or "")).expanduser()
        if (
            not release_root.is_absolute()
            or release_root.is_symlink()
            or not release_root.is_dir()
            or release_root.name != release_id
        ):
            raise EvidenceLedgerError("active release root binding is invalid")
        manifest = (
            Path(release_manifest_path).expanduser()
            if release_manifest_path is not None
            else release_root / "release-manifest.json"
        )
        if not manifest.is_absolute() or manifest.is_symlink() or not manifest.is_file():
            raise EvidenceLedgerError("release manifest must be an absolute regular file")
        try:
            manifest_bytes = manifest.read_bytes()
            release = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceLedgerError("release manifest is invalid") from exc
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if (
            not isinstance(release, Mapping)
            or release.get("release_id") != release_id
            or value.get("release_manifest_sha256") != manifest_sha256
        ):
            raise EvidenceLedgerError("active marker and release manifest identity mismatch")
        source_commit = str(release.get("commit") or "").lower()
        return self.bind_active_release(
            release_id=release_id,
            source_commit=source_commit,
            marker_sha256=hashlib.sha256(marker_bytes).hexdigest(),
            observed_at=observed_at,
        )

    def active_pointer(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT release_id, source_commit, marker_sha256, observed_at FROM active_release_pointer WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return {"schema": ACTIVE_POINTER_SCHEMA, **dict(row)}

    def latest(self, subject: str, *, active_release_only: bool = True) -> dict[str, Any] | None:
        subject = _token(subject, "subject")
        parameters: list[Any] = [subject]
        predicate = "subject = ?"
        if active_release_only:
            pointer = self.active_pointer()
            if pointer is None:
                return None
            predicate += " AND release_id = ?"
            parameters.append(pointer["release_id"])
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT envelope_json FROM evidence WHERE {predicate} ORDER BY generated_at DESC, evidence_id DESC LIMIT 1",
                parameters,
            ).fetchone()
        return json.loads(row["envelope_json"]) if row is not None else None

    def history(self, subject: str, *, limit: int = 100) -> list[dict[str, Any]]:
        subject = _token(subject, "subject")
        bounded = max(1, min(1000, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM evidence WHERE subject = ? ORDER BY generated_at DESC, evidence_id DESC LIMIT ?",
                (subject, bounded),
            ).fetchall()
        return [json.loads(row["envelope_json"]) for row in rows]

    def project_legacy_latest(self, subject: str, output: Path | str) -> dict[str, Any]:
        """Atomically write the active release's receipt as a compatibility view."""

        envelope = self.latest(subject, active_release_only=True)
        if envelope is None:
            raise EvidenceLedgerError("no active-release evidence exists for subject")
        receipt = dict(envelope["receipt"])
        receipt.update(
            {
                "release_id": envelope["release_id"],
                "generated_at": envelope["generated_at"],
                "evidence_id": envelope["evidence_id"],
                "evidence_envelope_schema": EVIDENCE_ENVELOPE_SCHEMA,
                "evidence_status_class": envelope["status_class"],
                "evidence_outcome": envelope["outcome"],
                "reason_code": envelope["reason_code"],
                "trace_id": envelope["trace_id"],
            }
        )
        target = Path(output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise EvidenceLedgerError("legacy projection must not replace a symlink")
        payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return receipt


def envelope_health_view(
    value: Mapping[str, Any],
    *,
    active_release_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a status projection without allowing predecessor evidence to fail active health."""

    envelope = EvidenceEnvelope.from_mapping(value)
    current = (now or _utc_now()).astimezone(timezone.utc)
    expired = bool(
        envelope.expires_at
        and _parse_time(envelope.expires_at, "expires_at", required=False) <= current
    )
    if envelope.release_id != active_release_id:
        status = "superseded"
    elif expired:
        status = "stale"
    elif envelope.status_class in {"business_backlog", "human_attention"}:
        status = "observed"
    elif envelope.outcome == "failed":
        status = "failed"
    elif envelope.outcome in {"waiting", "attention", "observed"}:
        status = "observed"
    else:
        status = "ok"
    return {
        "status": status,
        "ok": status in {"ok", "observed", "superseded"},
        "release_id": envelope.release_id,
        "status_class": envelope.status_class,
        "outcome": envelope.outcome,
        "reason_code": envelope.reason_code,
        "trace_id": envelope.trace_id,
        "expires_at": envelope.expires_at,
    }
