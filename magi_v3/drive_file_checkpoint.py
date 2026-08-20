"""Durable, PII-free progress state for one Drive/NAS case chunk.

The sync report may contain private case paths, but this checkpoint never does.
Callers give raw locators only to the token helpers; the serialized state keeps
fixed-length SHA-256 tokens, counters, sizes, checksums, and phase names.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
_PHASES = {
    "starting",
    "resolve_case",
    "scan_plan",
    "hash_files",
    "download",
    "upload",
    "verify",
    "case_complete",
    "timeout",
    "interrupted",
}
_OUTCOMES = {"downloaded_verified", "uploaded_verified", "verified_existing"}
_DEFERRED_REASONS = {
    "data_integrity_review",
    "semantic_path_collision_requires_human_review",
}


class DriveFileCheckpointError(RuntimeError):
    """Raised when progress state cannot be trusted safely."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _digest(*parts: object) -> str:
    payload = "\0".join(str(part or "") for part in parts).encode(
        "utf-8", errors="surrogateescape"
    )
    return hashlib.sha256(payload).hexdigest()


def _valid_digest(value: object) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)


def _valid_partial_row(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        byte_count = int(row.get("bytes"))
    except (TypeError, ValueError):
        return False
    return bool(
        byte_count >= 0
        and _valid_digest(row.get("source_fingerprint"))
        and _valid_digest(row.get("prefix_proof"))
    )


def case_token(worker_kind: str, canonical_case_id: str) -> str:
    return _digest("drive-case-v1", worker_kind, canonical_case_id)


def source_fingerprint(
    *,
    direction: str,
    case_key: str,
    locator: str,
    size: int | None = None,
    modified: object = "",
    checksum: str = "",
    opaque_source_id: str = "",
) -> str:
    return _digest(
        "drive-source-v1",
        direction,
        case_key,
        locator,
        int(size or 0),
        modified,
        checksum,
        opaque_source_id,
    )


def item_token(*, direction: str, case_key: str, source_key: str) -> str:
    return _digest("drive-item-v1", direction, case_key, source_key)


def proof_hash(*parts: object) -> str:
    return _digest("drive-proof-v1", *parts)


def snapshot_hash(tokens: Iterable[str]) -> str:
    clean = sorted({str(token) for token in tokens if _valid_digest(token)})
    return _digest("drive-snapshot-v1", *clean)


def _strict_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace a private JSON file durably, including the containing directory."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            dir_fd = -1
        if dir_fd >= 0:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class DriveFileCheckpoint:
    """One active all-files case checkpoint guarded by the worker singleton lock."""

    def __init__(
        self,
        path: Path,
        *,
        case_key: str,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not _valid_digest(case_key):
            raise DriveFileCheckpointError("invalid_case_token")
        self.path = Path(path)
        self.journal_path = self.path.with_suffix(self.path.suffix + ".journal")
        self.parts_dir = self.path.with_suffix(self.path.suffix + ".parts")
        self._on_progress = on_progress
        self.data = self._load_or_initialize(str(case_key))

    def _load_or_initialize(self, case_key: str) -> dict[str, Any]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            for private_path in (self.path, self.journal_path):
                if private_path.exists():
                    os.chmod(private_path, 0o600)
        except OSError as exc:
            raise DriveFileCheckpointError("checkpoint_private_mode_failed") from exc
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise DriveFileCheckpointError("checkpoint_malformed") from exc
            if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
                raise DriveFileCheckpointError("checkpoint_schema_unsupported")
            if not isinstance(payload.get("completed"), dict) or not isinstance(
                payload.get("hash_cache"), dict
            ):
                raise DriveFileCheckpointError("checkpoint_shape_invalid")
            self._validate_loaded_payload(payload)
            old_case = str(payload.get("case_token") or "")
            if old_case != case_key:
                if not (
                    payload.get("case_complete") is True
                    or payload.get("case_terminal_deferred") is True
                ):
                    raise DriveFileCheckpointError("incomplete_checkpoint_case_mismatch")
                payload = {}
            if payload:
                return self._replay_journal(payload)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "case_token": case_key,
            "snapshot_hash": "",
            "phase": "starting",
            "checkpoint_seq": 0,
            "last_progress_at": _now(),
            "case_complete": False,
            "hash_cache": {},
            "completed": {},
            "partials": {},
        }
        self.data = payload
        self._persist(notify=False)
        return payload

    @staticmethod
    def _validate_loaded_payload(payload: dict[str, Any]) -> None:
        """Reject structurally plausible but unprovable completion state."""

        if not _valid_digest(payload.get("case_token")):
            raise DriveFileCheckpointError("checkpoint_case_token_invalid")
        phase = str(payload.get("phase") or "")
        if phase not in _PHASES:
            raise DriveFileCheckpointError("checkpoint_phase_invalid")
        try:
            sequence = int(payload.get("checkpoint_seq") or 0)
        except (TypeError, ValueError) as exc:
            raise DriveFileCheckpointError("checkpoint_sequence_invalid") from exc
        if sequence < 0:
            raise DriveFileCheckpointError("checkpoint_sequence_invalid")
        snapshot = str(payload.get("snapshot_hash") or "")
        tokens = payload.get("snapshot_tokens") or []
        if snapshot and not _valid_digest(snapshot):
            raise DriveFileCheckpointError("checkpoint_snapshot_invalid")
        if not isinstance(tokens, list) or any(not _valid_digest(token) for token in tokens):
            raise DriveFileCheckpointError("checkpoint_snapshot_tokens_invalid")
        if snapshot and snapshot != snapshot_hash(tokens):
            raise DriveFileCheckpointError("checkpoint_snapshot_mismatch")
        if "snapshot_item_count" in payload:
            try:
                snapshot_count = int(payload["snapshot_item_count"])
            except (TypeError, ValueError) as exc:
                raise DriveFileCheckpointError(
                    "checkpoint_snapshot_count_invalid"
                ) from exc
            if snapshot_count != len(set(tokens)):
                raise DriveFileCheckpointError("checkpoint_snapshot_count_mismatch")

        for token, row in (payload.get("hash_cache") or {}).items():
            if (
                not _valid_digest(token)
                or not isinstance(row, dict)
                or not _valid_digest(row.get("source_fingerprint"))
                or len(str(row.get("md5") or "")) != 32
                or any(
                    ch not in "0123456789abcdef"
                    for ch in str(row.get("md5") or "").lower()
                )
            ):
                raise DriveFileCheckpointError("checkpoint_hash_cache_invalid")
        for token, row in (payload.get("completed") or {}).items():
            if (
                not _valid_digest(token)
                or not isinstance(row, dict)
                or row.get("outcome") not in _OUTCOMES
                or not _valid_digest(row.get("source_fingerprint"))
                or not _valid_digest(row.get("destination_proof"))
            ):
                raise DriveFileCheckpointError("checkpoint_completion_invalid")
        partials = payload.get("partials") or {}
        if not isinstance(partials, dict):
            raise DriveFileCheckpointError("checkpoint_partials_invalid")
        for token, row in partials.items():
            if not _valid_digest(token) or not _valid_partial_row(row):
                raise DriveFileCheckpointError("checkpoint_partial_invalid")
        if payload.get("case_complete") is True:
            expected = {str(token) for token in tokens}
            if not snapshot or not expected.issubset(set(payload.get("completed") or {})):
                raise DriveFileCheckpointError("checkpoint_false_completion")
        if payload.get("case_terminal_deferred") is True and (
            not snapshot or payload.get("deferred_reason") not in _DEFERRED_REASONS
        ):
            raise DriveFileCheckpointError("checkpoint_false_deferred_terminal")

    def _replay_journal(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.journal_path.exists():
            return payload
        base_seq = int(payload.get("checkpoint_seq") or 0)
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            raise DriveFileCheckpointError("checkpoint_journal_unreadable") from exc
        for line in lines:
            try:
                event = json.loads(line)
            except Exception as exc:
                raise DriveFileCheckpointError("checkpoint_journal_malformed") from exc
            if not isinstance(event, dict):
                raise DriveFileCheckpointError("checkpoint_journal_shape_invalid")
            seq = int(event.get("seq") or 0)
            if seq <= base_seq:
                continue
            if seq != int(payload.get("checkpoint_seq") or base_seq) + 1:
                raise DriveFileCheckpointError("checkpoint_journal_sequence_gap")
            op = str(event.get("op") or "")
            token = str(event.get("token") or "")
            row = event.get("row")
            if not _valid_digest(token) or not isinstance(row, dict):
                raise DriveFileCheckpointError("checkpoint_journal_event_invalid")
            if op == "hash":
                md5 = str(row.get("md5") or "").lower()
                if (
                    not _valid_digest(row.get("source_fingerprint"))
                    or len(md5) != 32
                    or any(ch not in "0123456789abcdef" for ch in md5)
                ):
                    raise DriveFileCheckpointError("checkpoint_journal_hash_invalid")
                payload.setdefault("hash_cache", {})[token] = row
            elif op == "completed":
                if any(
                    not _valid_digest(row.get(key))
                    for key in ("source_fingerprint", "destination_proof")
                ) or row.get("outcome") not in _OUTCOMES:
                    raise DriveFileCheckpointError("checkpoint_journal_completion_invalid")
                payload.setdefault("completed", {})[token] = row
                payload.setdefault("partials", {}).pop(token, None)
            elif op == "partial":
                if not _valid_partial_row(row):
                    raise DriveFileCheckpointError("checkpoint_journal_partial_invalid")
                payload.setdefault("partials", {})[token] = row
            else:
                raise DriveFileCheckpointError("checkpoint_journal_operation_invalid")
            payload["checkpoint_seq"] = seq
            payload["last_progress_at"] = str(event.get("at") or "")
        return payload

    @property
    def case_key(self) -> str:
        return str(self.data.get("case_token") or "")

    def _persist(
        self,
        *,
        notify: bool = True,
        journal_event: dict[str, Any] | None = None,
    ) -> None:
        self.data["checkpoint_seq"] = int(self.data.get("checkpoint_seq") or 0) + 1
        self.data["last_progress_at"] = _now()
        if journal_event is None:
            _strict_atomic_json(self.path, self.data)
            self._truncate_journal()
        else:
            event = {
                "seq": int(self.data["checkpoint_seq"]),
                "at": str(self.data["last_progress_at"]),
                **journal_event,
            }
            self._append_journal(event)
        if notify and self._on_progress is not None:
            self._on_progress(self.public_summary())

    def _append_journal(self, event: dict[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(
            self.journal_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(self.journal_path, 0o600)

    def _truncate_journal(self) -> None:
        if not self.journal_path.exists():
            return
        fd = os.open(self.journal_path, os.O_WRONLY | os.O_TRUNC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(self.journal_path, 0o600)

    def public_summary(self) -> dict[str, Any]:
        partials = self.data.get("partials") or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "case_token": self.case_key,
            "snapshot_hash": str(self.data.get("snapshot_hash") or ""),
            "phase": str(self.data.get("phase") or "starting"),
            "checkpoint_seq": int(self.data.get("checkpoint_seq") or 0),
            "last_progress_at": str(self.data.get("last_progress_at") or ""),
            "hash_cached_count": len(self.data.get("hash_cache") or {}),
            "completed_count": len(self.data.get("completed") or {}),
            "partial_count": len(partials),
            "partial_bytes": sum(
                max(0, int(row.get("bytes") or 0))
                for row in partials.values()
                if isinstance(row, dict)
            ),
            "case_complete": self.data.get("case_complete") is True,
            "case_terminal_deferred": self.data.get("case_terminal_deferred") is True,
        }

    def set_phase(self, phase: str) -> None:
        phase = str(phase or "").strip().lower()
        if phase not in _PHASES:
            raise DriveFileCheckpointError("invalid_checkpoint_phase")
        self.data["phase"] = phase
        self._persist()

    def bind_snapshot(self, tokens: Iterable[str]) -> str:
        raw = [str(token) for token in tokens]
        if any(not _valid_digest(token) for token in raw):
            raise DriveFileCheckpointError("invalid_snapshot_item_token")
        clean = sorted(set(raw))
        digest = snapshot_hash(clean)
        previous = str(self.data.get("snapshot_hash") or "")
        self.data["snapshot_hash"] = digest
        self.data["snapshot_item_count"] = len(clean)
        self.data["snapshot_tokens"] = clean
        self.data["case_terminal_deferred"] = False
        self.data.pop("deferred_reason", None)
        self.data["partials"] = {
            token: row
            for token, row in (self.data.get("partials") or {}).items()
            if token in set(clean)
        }
        if previous and previous != digest:
            # Retain individually source-bound proofs, but never claim the case
            # complete merely because the new snapshot has the same count.
            self.data["case_complete"] = False
            self.data["phase"] = "scan_plan"
        self._prune_parts(set(clean))
        self._persist()
        return digest

    def _prune_parts(self, live_tokens: set[str]) -> None:
        if not self.parts_dir.exists():
            return
        for path in self.parts_dir.glob("*.part"):
            if path.stem not in live_tokens:
                try:
                    path.unlink()
                except OSError:
                    pass

    def part_path(self, token: str) -> Path:
        if not _valid_digest(token):
            raise DriveFileCheckpointError("invalid_item_token")
        self.parts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.parts_dir, 0o700)
        except OSError:
            pass
        return self.parts_dir / f"{token}.part"

    def cached_hash(self, token: str, source_key: str) -> str:
        row = (self.data.get("hash_cache") or {}).get(str(token))
        if not isinstance(row, dict) or row.get("source_fingerprint") != source_key:
            return ""
        value = str(row.get("md5") or "").lower()
        return value if len(value) == 32 and all(ch in "0123456789abcdef" for ch in value) else ""

    def cache_hash(self, token: str, source_key: str, md5: str) -> None:
        value = str(md5 or "").lower()
        if (
            not _valid_digest(token)
            or not _valid_digest(source_key)
            or len(value) != 32
            or any(ch not in "0123456789abcdef" for ch in value)
        ):
            raise DriveFileCheckpointError("invalid_hash_cache_proof")
        cache = self.data.setdefault("hash_cache", {})
        cache[str(token)] = {
            "source_fingerprint": str(source_key),
            "md5": value,
        }
        self._persist(
            journal_event={"op": "hash", "token": str(token), "row": cache[str(token)]}
        )

    def completed(self, token: str, source_key: str) -> dict[str, Any]:
        row = (self.data.get("completed") or {}).get(str(token))
        if not isinstance(row, dict) or row.get("source_fingerprint") != source_key:
            return {}
        return dict(row)

    def partial(self, token: str, source_key: str) -> dict[str, Any]:
        row = (self.data.get("partials") or {}).get(str(token))
        if not isinstance(row, dict) or row.get("source_fingerprint") != source_key:
            return {}
        return dict(row)

    def record_partial(
        self,
        token: str,
        source_key: str,
        *,
        byte_count: int,
        prefix_proof: str,
    ) -> None:
        if any(not _valid_digest(value) for value in (token, source_key, prefix_proof)):
            raise DriveFileCheckpointError("invalid_partial_proof")
        if int(byte_count) < 0:
            raise DriveFileCheckpointError("invalid_partial_byte_count")
        partials = self.data.setdefault("partials", {})
        partials[str(token)] = {
            "source_fingerprint": str(source_key),
            "prefix_proof": str(prefix_proof),
            "bytes": int(byte_count),
        }
        self._persist(
            journal_event={
                "op": "partial",
                "token": str(token),
                "row": partials[str(token)],
            }
        )

    def mark_completed(
        self,
        token: str,
        source_key: str,
        *,
        outcome: str,
        destination_proof: str,
        byte_count: int = 0,
        verified: bool = False,
    ) -> None:
        if not verified:
            raise DriveFileCheckpointError("completion_requires_verification")
        if outcome not in _OUTCOMES:
            raise DriveFileCheckpointError("invalid_completion_outcome")
        if any(not _valid_digest(value) for value in (token, source_key, destination_proof)):
            raise DriveFileCheckpointError("invalid_completion_proof")
        completed = self.data.setdefault("completed", {})
        completed[str(token)] = {
            "source_fingerprint": str(source_key),
            "outcome": outcome,
            "destination_proof": str(destination_proof),
            "bytes": max(0, int(byte_count or 0)),
            "verified_at": _now(),
        }
        self.data.setdefault("partials", {}).pop(str(token), None)
        self._persist(
            journal_event={
                "op": "completed",
                "token": str(token),
                "row": completed[str(token)],
            }
        )

    def mark_case_complete(self) -> None:
        snapshot = str(self.data.get("snapshot_hash") or "")
        completed = self.data.get("completed") or {}
        expected_tokens = {
            str(token)
            for token in (self.data.get("snapshot_tokens") or [])
            if _valid_digest(token)
        }
        if len(snapshot) != 64 or not expected_tokens.issubset(set(completed)):
            raise DriveFileCheckpointError("case_completion_without_all_file_proofs")
        self.data["case_complete"] = True
        self.data["phase"] = "case_complete"
        self._persist()

    def mark_case_deferred(self, reason: str) -> None:
        if reason not in _DEFERRED_REASONS:
            raise DriveFileCheckpointError("invalid_case_deferred_reason")
        self.data["case_complete"] = False
        self.data["case_terminal_deferred"] = True
        self.data["deferred_reason"] = reason
        self.data["phase"] = "case_complete"
        self._persist()

    def discard_after_cursor_commit(self) -> None:
        if not (
            self.data.get("case_complete") is True
            or self.data.get("case_terminal_deferred") is True
        ):
            raise DriveFileCheckpointError("cannot_discard_incomplete_checkpoint")
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.journal_path.unlink()
        except FileNotFoundError:
            pass
        if self.parts_dir.exists():
            for part in self.parts_dir.glob("*.part"):
                try:
                    part.unlink()
                except OSError:
                    pass
            try:
                self.parts_dir.rmdir()
            except OSError:
                pass
