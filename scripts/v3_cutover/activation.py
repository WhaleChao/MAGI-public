"""Crash-safe single-owner activation journal and atomic release marker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .core import CutoverError


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SCHEMA = "magi.v3.activation-transaction/v1"
MARKER_SCHEMA = "magi.v3.active-release/v1"
TRANSITIONS = {
    "prepared": {"v2_zero", "rollback_started"},
    "v2_zero": {"v3_files_installed", "rollback_started"},
    "v3_files_installed": {"v3_commit_intent", "rollback_started"},
    "v3_commit_intent": {"v3_committed", "rollback_started"},
    "v3_committed": {"v3_active", "rollback_started"},
    "v3_active": {"rollback_started"},
    "rollback_started": {"v3_zero", "v2_commit_intent", "v3_recovery_intent"},
    "v3_zero": {"v2_commit_intent", "v3_recovery_intent"},
    "v2_commit_intent": {"v2_committed", "v3_recovery_intent"},
    "v2_committed": {"v2_restored", "v3_recovery_intent"},
    "v3_recovery_intent": {"v3_recovery_committed"},
    "v3_recovery_committed": {"v3_active"},
    "v2_restored": {"complete"},
    "complete": set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chain_entry(
    *,
    transaction_id: str,
    sequence: int,
    phase: str,
    at: str,
    previous_entry_sha256: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    entry = {
        "transaction_id": transaction_id,
        "sequence": sequence,
        "phase": phase,
        "at": at,
        "previous_entry_sha256": previous_entry_sha256,
        "evidence": dict(evidence),
    }
    entry["entry_sha256"] = _sha256_bytes(_canonical_json(entry))
    return entry


def _validate_history(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    history = payload.get("history")
    transaction_id = payload.get("transaction_id")
    if not isinstance(history, list) or not history or not isinstance(transaction_id, str):
        raise CutoverError("activation journal history is missing")
    previous = "0" * 64
    validated: list[dict[str, Any]] = []
    for sequence, raw in enumerate(history, start=1):
        if not isinstance(raw, dict) or raw.get("sequence") != sequence:
            raise CutoverError("activation journal history sequence is invalid")
        evidence = raw.get("evidence")
        if not isinstance(evidence, dict):
            raise CutoverError("activation journal history evidence is invalid")
        expected = _chain_entry(
            transaction_id=transaction_id,
            sequence=sequence,
            phase=str(raw.get("phase") or ""),
            at=str(raw.get("at") or ""),
            previous_entry_sha256=previous,
            evidence=evidence,
        )
        if raw != expected:
            raise CutoverError("activation journal append-only hash chain is invalid")
        previous = expected["entry_sha256"]
        validated.append(expected)
    last = validated[-1]
    if (
        payload.get("sequence") != last["sequence"]
        or payload.get("phase") != last["phase"]
        or payload.get("updated_at") != last["at"]
        or any(payload.get(key) != value for key, value in last["evidence"].items())
    ):
        raise CutoverError("activation journal head does not match its hash chain")
    return validated


def _safe_parent(path: Path) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise CutoverError("activation state path must be canonical, absolute, and non-symlinked")
    raw.mkdir(parents=True, exist_ok=True, mode=0o700)
    if raw.resolve(strict=True) != raw or raw.is_symlink() or not raw.is_dir():
        raise CutoverError("activation state parent is unsafe")
    return raw


def _atomic_replace(path: Path, data: bytes, *, mode: int = 0o600) -> str:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return _sha256_bytes(data)


def _exclusive(path: Path, data: bytes, *, mode: int = 0o600) -> str:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return _sha256_bytes(data)


def _load(path: Path, *, description: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CutoverError(f"{description} is unavailable: {exc}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise CutoverError(f"{description} is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CutoverError(f"{description} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise CutoverError(f"{description} must be a JSON object")
    return value


def active_release_marker(
    path: Path,
    *,
    expected_release: str,
    expected_release_id: str | None = None,
    expected_release_root: Path | None = None,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    payload = _load(path, description="active release marker")
    if (
        payload.get("schema") != MARKER_SCHEMA
        or payload.get("schema_version") != 1
        or payload.get("release") != expected_release
        or not isinstance(payload.get("transaction_id"), str)
        or not payload["transaction_id"]
    ):
        raise CutoverError("active release marker identity mismatch")
    expected = {
        "release_id": expected_release_id,
        "release_root": str(expected_release_root) if expected_release_root else None,
        "release_manifest_sha256": expected_manifest_sha256,
    }
    for key, value in expected.items():
        if value is not None and payload.get(key) != value:
            raise CutoverError(f"active release marker {key} mismatch")
    return payload


@dataclass(slots=True)
class ActivationTransaction:
    journal_path: Path
    marker_path: Path
    transaction_id: str
    clock: Callable[[], str] = _now

    @classmethod
    def begin(
        cls,
        *,
        state_parent: Path,
        plan_sha256: str,
        release_id: str,
        release_root: Path,
        release_manifest_sha256: str,
        reconciliation_before: Mapping[str, Any],
        clock: Callable[[], str] = _now,
    ) -> "ActivationTransaction":
        if not all(
            SHA256_RE.fullmatch(value)
            for value in (plan_sha256, release_manifest_sha256)
        ):
            raise CutoverError("activation transaction hash binding is invalid")
        parent = _safe_parent(state_parent)
        journal = parent / "cutover-activation.json"
        marker = parent / "active-release.json"
        if journal.exists() or journal.is_symlink():
            previous = _load(journal, description="activation journal")
            if previous.get("phase") != "complete":
                raise CutoverError("an incomplete activation transaction requires recovery")
            journal.unlink()
        transaction_id = uuid.uuid4().hex
        current = clock()
        initial_evidence = {
            "plan_sha256": plan_sha256,
            "release_id": release_id,
            "release_root": str(release_root),
            "release_manifest_sha256": release_manifest_sha256,
            "reconciliation_before": dict(reconciliation_before),
        }
        history = [
            _chain_entry(
                transaction_id=transaction_id,
                sequence=1,
                phase="prepared",
                at=current,
                previous_entry_sha256="0" * 64,
                evidence=initial_evidence,
            )
        ]
        payload = {
            "schema": SCHEMA,
            "schema_version": 1,
            "transaction_id": transaction_id,
            "operation": "v2_to_v3_cutover",
            "phase": "prepared",
            "sequence": 1,
            "started_at": current,
            "updated_at": current,
            **initial_evidence,
            "history": history,
        }
        _exclusive(journal, _canonical_json(payload))
        return cls(journal, marker, transaction_id, clock)

    @classmethod
    def resume(
        cls,
        *,
        state_parent: Path,
        clock: Callable[[], str] = _now,
    ) -> "ActivationTransaction":
        parent = _safe_parent(state_parent)
        journal = parent / "cutover-activation.json"
        payload = _load(journal, description="activation journal")
        if payload.get("schema") != SCHEMA or not isinstance(payload.get("transaction_id"), str):
            raise CutoverError("activation journal identity mismatch")
        return cls(journal, parent / "active-release.json", payload["transaction_id"], clock)

    def document(self) -> dict[str, Any]:
        payload = _load(self.journal_path, description="activation journal")
        if payload.get("schema") != SCHEMA or payload.get("transaction_id") != self.transaction_id:
            raise CutoverError("activation journal transaction identity drifted")
        _validate_history(payload)
        return payload

    def initial_receipt(self) -> dict[str, Any]:
        payload = self.document()
        entry = dict(payload["history"][0])
        entry["journal_sha256"] = _sha256_bytes(self.journal_path.read_bytes())
        return entry

    def advance(self, phase: str, **evidence: Any) -> dict[str, Any]:
        payload = self.document()
        current_phase = payload.get("phase")
        if phase not in TRANSITIONS.get(str(current_phase), set()):
            raise CutoverError(f"invalid activation phase transition: {current_phase!r} -> {phase!r}")
        sequence = int(payload.get("sequence", 0)) + 1
        now = self.clock()
        updated = {
            **payload,
            "phase": phase,
            "sequence": sequence,
            "updated_at": now,
            **evidence,
        }
        history = list(payload.get("history", ()))
        previous_entry_sha256 = history[-1]["entry_sha256"]
        history.append(
            _chain_entry(
                transaction_id=self.transaction_id,
                sequence=sequence,
                phase=phase,
                at=now,
                previous_entry_sha256=previous_entry_sha256,
                evidence=evidence,
            )
        )
        updated["history"] = history
        digest = _atomic_replace(self.journal_path, _canonical_json(updated))
        return {
            **history[-1],
            "journal_sha256": digest,
        }

    def commit_release(
        self,
        *,
        release: str,
        release_id: str,
        release_root: Path,
        release_manifest_sha256: str,
    ) -> dict[str, Any]:
        if release not in {"v2", "v3"} or not SHA256_RE.fullmatch(release_manifest_sha256):
            raise CutoverError("activation release commit binding is invalid")
        current_phase = str(self.document().get("phase"))
        recovering_v3 = release == "v3" and current_phase not in {
            "v3_files_installed",
            "v3_commit_intent",
        }
        intent = "v3_recovery_intent" if recovering_v3 else f"{release}_commit_intent"
        committed = "v3_recovery_committed" if recovering_v3 else f"{release}_committed"
        if current_phase != intent:
            intent_receipt = self.advance(intent)
        else:
            payload = self.document()
            intent_receipt = {
                **payload["history"][-1],
                "journal_sha256": _sha256_bytes(self.journal_path.read_bytes()),
            }
        now = self.clock()
        marker = {
            "schema": MARKER_SCHEMA,
            "schema_version": 1,
            "transaction_id": self.transaction_id,
            "release": release,
            "release_id": release_id,
            "release_root": str(release_root),
            "release_manifest_sha256": release_manifest_sha256,
            "committed_at": now,
        }
        marker_sha = _atomic_replace(self.marker_path, _canonical_json(marker))
        journal = self.advance(
            committed,
            active_release_marker_sha256=marker_sha,
            active_release_marker=marker,
        )
        return {
            **journal,
            "intent_receipt": intent_receipt,
            "release": release,
            "release_id": release_id,
            "release_root": str(release_root),
            "release_manifest_sha256": release_manifest_sha256,
            "active_release_marker_sha256": marker_sha,
            "active_release_marker": marker,
            "committed_at": now,
        }


__all__ = [
    "ActivationTransaction",
    "MARKER_SCHEMA",
    "SCHEMA",
    "active_release_marker",
]
