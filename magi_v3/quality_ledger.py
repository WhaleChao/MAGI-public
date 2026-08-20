"""Privacy-preserving business-quality outcome ledger.

This module deliberately records only controlled vocabulary plus hashes.  It
is suitable for feeding source evolution without copying case data, paths,
calendar titles, document names, or exception text into a durable database.
It has no integrations and cannot deploy or mutate an external system.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


QUALITY_KINDS = frozenset({
    "transcript_retry_pending", "calendar_import_source_gap",
    "pdf_bookmark_backlog", "judgment_quality_backlog",
})
ACTIONABILITY = frozenset({"auto_retry", "source_repair", "human_review", "observe"})
OUTCOME_STATES = frozenset({"open", "retrying", "waiting_human", "resolved", "cancelled"})
_OWNER = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _require_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def canonical_quality_signal(signal: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a de-identified quality signal and reject unknown data."""
    if not isinstance(signal, Mapping):
        raise ValueError("quality signal must be an object")
    allowed = {"kind", "owner", "evidence_hash", "actionability", "state", "retry_at", "deadline_at", "human_required"}
    if not set(signal) <= allowed:
        raise ValueError("quality signal contains non-canonical fields")
    kind = signal.get("kind")
    owner = signal.get("owner")
    evidence_hash = signal.get("evidence_hash")
    actionability = signal.get("actionability")
    state = signal.get("state", "open")
    if kind not in QUALITY_KINDS:
        raise ValueError("unsupported quality kind")
    if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
        raise ValueError("owner must be a controlled identifier")
    if not isinstance(evidence_hash, str) or not _SHA256.fullmatch(evidence_hash.lower()):
        raise ValueError("evidence_hash must be sha256")
    if actionability not in ACTIONABILITY:
        raise ValueError("unsupported actionability")
    if state not in OUTCOME_STATES:
        raise ValueError("unsupported outcome state")
    retry_at = _require_timestamp(signal.get("retry_at"), "retry_at")
    deadline_at = _require_timestamp(signal.get("deadline_at"), "deadline_at")
    human_required = signal.get("human_required")
    if type(human_required) is not bool:
        raise ValueError("human_required must be boolean")
    if actionability == "human_review" and not human_required:
        raise ValueError("human_review requires human_required")
    if actionability == "auto_retry" and retry_at is None:
        raise ValueError("auto_retry requires retry_at")
    # No free-text fields are allowed: raw references cannot leak PII or paths.
    canonical = {
        "kind": kind, "owner": owner, "evidence_hash": evidence_hash.lower(),
        "actionability": actionability, "state": state, "retry_at": retry_at,
        "deadline_at": deadline_at, "human_required": human_required,
    }
    canonical["outcome_id"] = "qo-" + _hash(canonical)[:24]
    return canonical


def attest_release(*, release_id: str, commit_sha: str, manifest_bytes: bytes) -> dict[str, str | bool]:
    """Return a path-free immutable release attestation; it is never a deploy permit."""
    if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
        raise ValueError("invalid release_id")
    if not isinstance(commit_sha, str) or not _COMMIT.fullmatch(commit_sha.lower()):
        raise ValueError("invalid commit_sha")
    if not isinstance(manifest_bytes, bytes) or not manifest_bytes:
        raise ValueError("manifest_bytes must be non-empty bytes")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest_bytes must contain a JSON object") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest_bytes must contain a JSON object")
    if manifest.get("release_id") != release_id:
        raise ValueError("manifest release_id does not match attested release")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    body = {"release_id": release_id, "commit_sha": commit_sha.lower(), "manifest_sha256": manifest_sha256}
    return {**body, "attestation_sha256": _hash(body), "auto_deploy": False, "human_required": True}


class QualityOutcomeLedger:
    """A small SQLite ledger whose persisted schema contains no raw business data."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS quality_outcomes (
                outcome_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
                attestation_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def upsert(self, signal: Mapping[str, Any], *, attestation: Mapping[str, Any]) -> dict[str, Any]:
        payload = canonical_quality_signal(signal)
        expected = {"release_id", "commit_sha", "manifest_sha256", "attestation_sha256", "auto_deploy", "human_required"}
        if set(attestation) != expected or attestation.get("auto_deploy") is not False or attestation.get("human_required") is not True:
            raise ValueError("attestation must be a human-gated release attestation")
        if not all(isinstance(attestation.get(key), str) for key in ("release_id", "commit_sha", "manifest_sha256", "attestation_sha256")):
            raise ValueError("attestation fields are invalid")
        body = {key: str(attestation[key]) for key in ("release_id", "commit_sha", "manifest_sha256")}
        if (
            not _RELEASE_ID.fullmatch(body["release_id"])
            or not _COMMIT.fullmatch(body["commit_sha"])
            or not _SHA256.fullmatch(body["manifest_sha256"])
            or str(attestation["attestation_sha256"]) != _hash(body)
        ):
            raise ValueError("attestation integrity is invalid")
        now = _now()
        with self._connect() as db:
            row = db.execute("SELECT created_at FROM quality_outcomes WHERE outcome_id=?", (payload["outcome_id"],)).fetchone()
            db.execute("""INSERT INTO quality_outcomes(outcome_id,payload_json,attestation_json,created_at,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(outcome_id) DO UPDATE SET
                payload_json=excluded.payload_json,attestation_json=excluded.attestation_json,updated_at=excluded.updated_at""",
                (payload["outcome_id"], _json(payload), _json(dict(attestation)), str(row["created_at"]) if row else now, now))
        return self.get(payload["outcome_id"])

    def get(self, outcome_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT payload_json,attestation_json FROM quality_outcomes WHERE outcome_id=?", (outcome_id,)).fetchone()
        if row is None:
            raise KeyError("quality outcome not found")
        return {**json.loads(str(row["payload_json"])), "attestation": json.loads(str(row["attestation_json"]))}

    def list_open(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT outcome_id FROM quality_outcomes WHERE json_extract(payload_json,'$.state') NOT IN ('resolved','cancelled') ORDER BY updated_at DESC").fetchall()
        return [self.get(str(row["outcome_id"])) for row in rows]
