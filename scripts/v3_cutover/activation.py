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

from magi_v3 import fcntl_compat as fcntl

from .core import CutoverError


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", frozenset())
_STAT_SUPPORTS_DIR_FD = os.stat in getattr(os, "supports_dir_fd", frozenset())
_STAT_SUPPORTS_NOFOLLOW = os.stat in getattr(
    os, "supports_follow_symlinks", frozenset()
)
SCHEMA = "magi.v3.activation-transaction/v1"
ROTATION_SCHEMA = "magi.v3.activation-transaction/v2"
ROTATION_OPERATION = "v3_to_v3_rotation"
MARKER_SCHEMA = "magi.v3.active-release/v1"
ACTIVE_RELEASE_STATE_SCHEMA = "magi.v3.active-release-state/v1"
ACTIVE_RELEASE_DEPLOYMENT_SCHEMA = "magi.v3.active-release-deployment/v1"
ACTIVE_V3_RESTART_PHASES = frozenset({
    "v3_committed", "v3_active", "v3_recovery_committed",
    "candidate_committed", "candidate_active",
})
ACTIVE_RELEASE_ADMISSION_LOCK = ".formal-gateway-restart-admission.lock"
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
ROTATION_TRANSITIONS = {
    "prepared": {"previous_v3_zero"},
    "previous_v3_zero": {"candidate_files_installed"},
    "candidate_files_installed": {"candidate_commit_intent"},
    "candidate_commit_intent": {"candidate_committed"},
    "candidate_committed": {"candidate_active"},
    "candidate_active": set(),
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


def verify_active_release_snapshot(
    marker: Mapping[str, Any],
    journal: Mapping[str, Any],
    *,
    expected_release: str,
    allowed_phases: frozenset[str],
) -> dict[str, Any]:
    """Verify a marker/journal pair and return its PII-free stable identity.

    A durable restart lease can bind the stable identity, while every restart
    must still prove that the hash-chained journal remains in an explicitly
    active phase. Entering rollback therefore revokes restart admission before
    any service stop is attempted.
    """
    if expected_release not in {"v2", "v3"}:
        raise CutoverError("active release snapshot expected release is invalid")
    if (
        type(allowed_phases) is not frozenset
        or not allowed_phases
        or any(
            type(phase) is not str
            or phase not in {*TRANSITIONS, *ROTATION_TRANSITIONS}
            for phase in allowed_phases
        )
    ):
        raise CutoverError("active release snapshot phases are invalid")
    try:
        raw = json.dumps(
            {"marker": marker, "journal": journal},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if not 1 <= len(raw) <= 8 * 1024 * 1024:
            raise CutoverError("active release snapshot exceeds its byte bound")
        detached = json.loads(raw)
    except CutoverError:
        raise
    except Exception as exc:
        raise CutoverError("active release snapshot is not stable JSON") from exc
    if (
        type(detached) is not dict
        or set(detached) != {"marker", "journal"}
        or type(detached["marker"]) is not dict
        or type(detached["journal"]) is not dict
    ):
        raise CutoverError("active release snapshot requires exact roots")
    marker_value = detached["marker"]
    journal_value = detached["journal"]
    marker_fields = {
        "schema", "schema_version", "transaction_id", "release",
        "release_id", "release_root", "release_manifest_sha256",
        "committed_at",
    }
    if set(marker_value) != marker_fields:
        raise CutoverError("active release marker requires exact fields")
    transaction_id = marker_value.get("transaction_id")
    release_id = marker_value.get("release_id")
    release_root = marker_value.get("release_root")
    manifest_sha256 = marker_value.get("release_manifest_sha256")
    committed_at = marker_value.get("committed_at")
    try:
        committed_observation = datetime.fromisoformat(committed_at)
    except (TypeError, ValueError) as exc:
        raise CutoverError("active release marker commit time is invalid") from exc
    if (
        marker_value.get("schema") != MARKER_SCHEMA
        or marker_value.get("schema_version") != 1
        or marker_value.get("release") != expected_release
        or type(transaction_id) is not str
        or not re.fullmatch(r"[0-9a-f]{32}", transaction_id)
        or type(release_id) is not str
        or not RELEASE_ID_RE.fullmatch(release_id)
        or type(release_root) is not str
        or not Path(release_root).is_absolute()
        or Path(release_root).resolve(strict=False) != Path(release_root)
        or type(manifest_sha256) is not str
        or not SHA256_RE.fullmatch(manifest_sha256)
        or type(committed_at) is not str
        or committed_observation.tzinfo is None
    ):
        raise CutoverError("active release marker identity mismatch")
    history = _validate_history(journal_value)
    legacy = (
        journal_value.get("schema") == SCHEMA
        and journal_value.get("operation") == "v2_to_v3_cutover"
    )
    rotation = (
        journal_value.get("schema") == ROTATION_SCHEMA
        and journal_value.get("operation") == ROTATION_OPERATION
        and expected_release == "v3"
    )
    if (
        not (legacy or rotation)
        or journal_value.get("transaction_id") != transaction_id
        or journal_value.get("phase") not in allowed_phases
    ):
        raise CutoverError(
            "activation journal is not in an allowed active release state"
        )
    initial = history[0]
    initial_evidence = initial["evidence"]
    plan_sha256 = initial_evidence.get("plan_sha256")
    commit_phases = (
        {"candidate_committed"}
        if rotation
        else {f"{expected_release}_committed"}
    )
    if expected_release == "v3" and not rotation:
        commit_phases.add("v3_recovery_committed")
    commits = [entry for entry in history if entry["phase"] in commit_phases]
    if not commits:
        raise CutoverError("activation journal has no active release commit")
    commit_evidence = commits[-1]["evidence"]
    marker_sha256 = _sha256_bytes(_canonical_json(marker_value))
    initial_release_matches = expected_release != "v3" or (
        initial_evidence.get("release_id") == release_id
        and initial_evidence.get("release_root") == release_root
        and initial_evidence.get("release_manifest_sha256") == manifest_sha256
    )
    journal_release_matches = expected_release != "v3" or (
        journal_value.get("release_id") == release_id
        and journal_value.get("release_root") == release_root
        and journal_value.get("release_manifest_sha256") == manifest_sha256
    )
    if (
        initial["phase"] != "prepared"
        or initial.get("transaction_id") != transaction_id
        or type(plan_sha256) is not str
        or not SHA256_RE.fullmatch(plan_sha256)
        or not initial_release_matches
        or set(commit_evidence) != {
            "active_release_marker_sha256", "active_release_marker"
        }
        or commit_evidence.get("active_release_marker") != marker_value
        or commit_evidence.get("active_release_marker_sha256") != marker_sha256
        or not journal_release_matches
        or journal_value.get("plan_sha256") != plan_sha256
        or journal_value.get("active_release_marker") != marker_value
        or journal_value.get("active_release_marker_sha256") != marker_sha256
    ):
        raise CutoverError(
            "activation journal does not bind the committed active release"
        )
    stable_identity = {
        "transaction_id": transaction_id,
        "release": expected_release,
        "release_id": release_id,
        "release_root_sha256": hashlib.sha256(
            ("magi-v3-active-release-root/v1:" + release_root).encode("utf-8")
        ).hexdigest(),
        "release_manifest_sha256": manifest_sha256,
        "plan_sha256": plan_sha256,
    }
    return {
        "schema": ACTIVE_RELEASE_STATE_SCHEMA,
        **stable_identity,
        "phase": journal_value["phase"],
        "journal_head_entry_sha256": history[-1]["entry_sha256"],
        "active_release_identity_sha256": _sha256_bytes(
            json.dumps(
                stable_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ),
        "pii_included": False,
    }


def verify_active_release_deployment(
    active_release_state: Mapping[str, Any],
    release_manifest_raw: bytes,
    *,
    expected_source_git_commit: str,
) -> dict[str, Any]:
    """Bind one verified active state to its immutable release source.

    ``active_release_state`` proves which manifest was committed by the
    hash-chained cutover journal.  The raw manifest bytes then prove that the
    committed release was built from the exact promoted source commit.  This
    distinction is important: the promotion baseline is the release active
    *before* cutover and must never be reused as the post-cutover identity.
    """
    try:
        state_raw = json.dumps(
            active_release_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        state = json.loads(state_raw)
    except Exception as exc:
        raise CutoverError("active release deployment state is not stable JSON") from exc
    expected_state_fields = {
        "schema", "transaction_id", "release", "release_id",
        "release_root_sha256", "release_manifest_sha256", "plan_sha256",
        "phase", "journal_head_entry_sha256",
        "active_release_identity_sha256", "pii_included",
    }
    if (
        type(state) is not dict
        or set(state) != expected_state_fields
        or state.get("schema") != ACTIVE_RELEASE_STATE_SCHEMA
        or state.get("release") != "v3"
        or state.get("phase") not in ACTIVE_V3_RESTART_PHASES
        or state.get("pii_included") is not False
    ):
        raise CutoverError("active release deployment state is invalid")
    if (
        type(state.get("transaction_id")) is not str
        or not re.fullmatch(r"[0-9a-f]{32}", state["transaction_id"])
        or type(state.get("release_id")) is not str
        or not RELEASE_ID_RE.fullmatch(state["release_id"])
        or any(
            type(state.get(field)) is not str
            or not SHA256_RE.fullmatch(state[field])
            for field in (
                "release_root_sha256", "release_manifest_sha256",
                "plan_sha256", "journal_head_entry_sha256",
                "active_release_identity_sha256",
            )
        )
    ):
        raise CutoverError("active release deployment state identity is invalid")
    stable_identity = {
        "transaction_id": state["transaction_id"],
        "release": state["release"],
        "release_id": state["release_id"],
        "release_root_sha256": state["release_root_sha256"],
        "release_manifest_sha256": state["release_manifest_sha256"],
        "plan_sha256": state["plan_sha256"],
    }
    if state["active_release_identity_sha256"] != _sha256_bytes(
        json.dumps(
            stable_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ):
        raise CutoverError("active release deployment state identity drift")
    if (
        type(release_manifest_raw) is not bytes
        or not 1 <= len(release_manifest_raw) <= 16 * 1024 * 1024
        or _sha256_bytes(release_manifest_raw)
        != state.get("release_manifest_sha256")
    ):
        raise CutoverError("active release manifest bytes do not match the committed state")
    if (
        type(expected_source_git_commit) is not str
        or not re.fullmatch(r"[0-9a-f]{40}", expected_source_git_commit)
    ):
        raise CutoverError("active release expected source commit is invalid")
    try:
        manifest = json.loads(release_manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverError("active release manifest is not JSON") from exc
    if (
        type(manifest) is not dict
        or manifest.get("schema_version") != 1
        or manifest.get("immutable") is not True
        or manifest.get("release_id") != state.get("release_id")
        or manifest.get("commit") != expected_source_git_commit
        or type(manifest.get("source_snapshot_sha256")) is not str
        or not SHA256_RE.fullmatch(manifest["source_snapshot_sha256"])
        or manifest.get("release_sha256") != manifest["source_snapshot_sha256"]
    ):
        raise CutoverError("active release manifest identity/source drift")
    rows = manifest.get("files")
    if type(rows) is not list or not rows:
        raise CutoverError("active release manifest file inventory is missing")
    snapshot: list[dict[str, Any]] = []
    paths: set[str] = set()
    for index, row in enumerate(rows):
        if type(row) is not dict or set(row) != {"path", "sha256", "size", "mode"}:
            raise CutoverError(f"active release manifest file {index} is invalid")
        path = row["path"]
        digest = row["sha256"]
        size = row["size"]
        mode = row["mode"]
        relative = Path(path) if type(path) is str else Path()
        if (
            type(path) is not str
            or not path
            or relative.is_absolute()
            or ".." in relative.parts
            or path in paths
            or type(digest) is not str
            or not SHA256_RE.fullmatch(digest)
            or type(size) is not int
            or isinstance(size, bool)
            or size < 0
            or type(mode) is not str
            or not re.fullmatch(r"0[0-7]{3}", mode)
        ):
            raise CutoverError(f"active release manifest file {index} is invalid")
        paths.add(path)
        snapshot.append({"path": path, "sha256": digest, "size": size, "mode": mode})
    if [row["path"] for row in snapshot] != sorted(paths):
        raise CutoverError("active release manifest file inventory is not sorted")
    snapshot_sha256 = _sha256_bytes(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if snapshot_sha256 != manifest["source_snapshot_sha256"]:
        raise CutoverError("active release manifest content snapshot drift")
    return {
        "schema": ACTIVE_RELEASE_DEPLOYMENT_SCHEMA,
        "active_release_identity_sha256": state[
            "active_release_identity_sha256"
        ],
        "release_id": state["release_id"],
        "release_manifest_sha256": state["release_manifest_sha256"],
        "source_git_commit": expected_source_git_commit,
        "release_snapshot_sha256": snapshot_sha256,
        "pii_included": False,
    }


def load_verified_active_release_deployment(
    marker: Mapping[str, Any],
    active_release_state: Mapping[str, Any],
    *,
    expected_source_git_commit: str,
) -> dict[str, Any]:
    """Securely load the manifest named by one verified active marker."""
    try:
        state_raw = json.dumps(
            active_release_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        state_value = json.loads(state_raw)
        marker_raw = json.dumps(
            marker,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        marker_value = json.loads(marker_raw)
    except Exception as exc:
        raise CutoverError("active release deployment inputs are unstable") from exc
    if type(state_value) is not dict or type(marker_value) is not dict:
        raise CutoverError("active release deployment inputs must be objects")
    release_root_value = marker_value.get("release_root")
    if type(release_root_value) is not str:
        raise CutoverError("active release deployment root is missing")
    release_root = Path(release_root_value)
    expected_root_sha256 = hashlib.sha256(
        ("magi-v3-active-release-root/v1:" + release_root_value).encode("utf-8")
    ).hexdigest()
    if (
        not release_root.is_absolute()
        or release_root.resolve(strict=False) != release_root
        or state_value.get("release_root_sha256")
        != expected_root_sha256
    ):
        raise CutoverError("active release deployment root drift")
    try:
        root_metadata = release_root.lstat()
    except OSError as exc:
        raise CutoverError("active release deployment root is unavailable") from exc
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) & 0o022
    ):
        raise CutoverError("active release deployment root is unsafe")
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not _OPEN_SUPPORTS_DIR_FD
        or not _STAT_SUPPORTS_DIR_FD
        or not _STAT_SUPPORTS_NOFOLLOW
    ):
        raise CutoverError("active release deployment requires secure path primitives")
    root_descriptor = -1
    reopened_root_descriptor = -1
    descriptor = -1
    try:
        root_descriptor = os.open(
            release_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        opened_root = os.fstat(root_descriptor)
        before = os.stat(
            "release-manifest.json",
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or not 1 <= before.st_size <= 16 * 1024 * 1024
        ):
            raise CutoverError("active release manifest is unsafe")
        descriptor = os.open(
            "release-manifest.json",
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
        opened = os.fstat(descriptor)
        raw = b""
        while len(raw) <= 16 * 1024 * 1024:
            chunk = os.read(descriptor, min(1024 * 1024, 16 * 1024 * 1024 + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
        leaf = os.stat(
            "release-manifest.json",
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        reopened_root_descriptor = os.open(
            release_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        reopened_root = os.fstat(reopened_root_descriptor)
        current_root = release_root.lstat()
    except CutoverError:
        raise
    except OSError as exc:
        raise CutoverError("active release manifest could not be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if reopened_root_descriptor >= 0:
            os.close(reopened_root_descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)
    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev, value.st_ino, stat.S_IMODE(value.st_mode),
            value.st_uid, value.st_nlink, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns,
        )
    if (
        len(raw) != before.st_size
        or len(raw) > 16 * 1024 * 1024
        or identity(root_metadata) != identity(opened_root)
        or identity(root_metadata) != identity(reopened_root)
        or identity(root_metadata) != identity(current_root)
        or identity(before) != identity(opened)
        or identity(before) != identity(after)
        or identity(before) != identity(leaf)
    ):
        raise CutoverError("active release manifest changed while being read")
    return verify_active_release_deployment(
        state_value,
        raw,
        expected_source_git_commit=expected_source_git_commit,
    )


@dataclass(slots=True)
class ActiveReleaseAdmission:
    """Cross-process revocation/admission lock with explicit early release."""

    descriptor: int
    parent_descriptor: int
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        error: BaseException | None = None
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        except BaseException as exc:
            error = exc
        try:
            os.close(self.descriptor)
        except BaseException as exc:
            error = error or exc
        try:
            os.close(self.parent_descriptor)
        except BaseException as exc:
            error = error or exc
        if error is not None:
            raise CutoverError("active release admission lock release failed") from error

    def __enter__(self) -> "ActiveReleaseAdmission":
        if self._released:
            raise CutoverError("active release admission lock is already released")
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


def acquire_active_release_admission(state_parent: Path) -> ActiveReleaseAdmission:
    """Acquire the fixed owner-only lock shared by restart and rollback."""
    state = state_parent.expanduser()
    if (
        not state.is_absolute()
        or state.resolve(strict=False) != state
        or state.is_symlink()
    ):
        raise CutoverError("active release admission parent is unsafe")
    # The lock lives one directory above the mutable state directory.  A
    # rename/replacement of the state directory therefore cannot split restart
    # and rollback onto two different lock inodes.
    parent = state.parent
    state_descriptor = -1
    parent_descriptor = -1
    reopened_parent_descriptor = -1
    descriptor = -1
    try:
        state_descriptor = os.open(
            state,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        state_metadata = os.fstat(state_descriptor)
        if (
            not stat.S_ISDIR(state_metadata.st_mode)
            or state_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(state_metadata.st_mode) & 0o077
        ):
            raise CutoverError("active release state parent is not private")
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        parent_metadata = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(parent_metadata.st_mode) & 0o077
        ):
            raise CutoverError("active release admission parent is not private")
        descriptor = os.open(
            ACTIVE_RELEASE_ADMISSION_LOCK,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        leaf = os.stat(
            ACTIVE_RELEASE_ADMISSION_LOCK,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or (before.st_dev, before.st_ino) != (leaf.st_dev, leaf.st_ino)
        ):
            raise CutoverError("active release admission lock leaf is unsafe")
        # Restart and rollback must never wait forever behind a wedged
        # authority startup.  A busy admission is a bounded fail-closed result;
        # the cutover/rollback coordinator can report and retry explicitly.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        after = os.fstat(descriptor)
        current = os.stat(
            ACTIVE_RELEASE_ADMISSION_LOCK,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        reopened_parent_descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        reopened_parent = os.fstat(reopened_parent_descriptor)
        current_state = os.stat(state, follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_nlink)
            != (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_nlink)
            or (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_nlink)
            != (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_nlink)
            or (
                reopened_parent.st_dev, reopened_parent.st_ino,
                reopened_parent.st_mode, reopened_parent.st_uid,
            ) != (
                parent_metadata.st_dev, parent_metadata.st_ino,
                parent_metadata.st_mode, parent_metadata.st_uid,
            )
            or (
                current_state.st_dev, current_state.st_ino,
                current_state.st_mode, current_state.st_uid,
            ) != (
                state_metadata.st_dev, state_metadata.st_ino,
                state_metadata.st_mode, state_metadata.st_uid,
            )
        ):
            raise CutoverError("active release admission lock identity drifted")
        os.close(reopened_parent_descriptor)
        reopened_parent_descriptor = -1
        result = ActiveReleaseAdmission(descriptor, parent_descriptor)
        descriptor = -1
        parent_descriptor = -1
        return result
    except CutoverError:
        raise
    except OSError as exc:
        raise CutoverError("active release admission lock is unavailable") from exc
    finally:
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        if reopened_parent_descriptor >= 0:
            os.close(reopened_parent_descriptor)
        if state_descriptor >= 0:
            os.close(state_descriptor)


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


@dataclass(slots=True)
class V3RotationTransaction:
    """Crash-auditable V3-to-V3 marker/journal transaction.

    The executor must persist rollback bytes before calling :meth:`begin`.
    This class then replaces the historical journal, proves zero ownership,
    records candidate installation, and commits the marker before any new
    service may start.  Rollback restoration is intentionally owned by the
    executor because an immutable predecessor may not understand schema v2.
    """

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
        previous_marker_sha256: str,
        previous_journal_sha256: str,
        previous_release_id: str,
        previous_release_root: Path,
        previous_release_manifest_sha256: str,
        candidate_release_id: str,
        candidate_release_root: Path,
        candidate_release_manifest_sha256: str,
        candidate_deployment_manifest_sha256: str,
        rollback_deployment_manifest_sha256: str,
        reconciliation_before: Mapping[str, Any],
        clock: Callable[[], str] = _now,
    ) -> "V3RotationTransaction":
        digests = (
            plan_sha256,
            previous_marker_sha256,
            previous_journal_sha256,
            previous_release_manifest_sha256,
            candidate_release_manifest_sha256,
            candidate_deployment_manifest_sha256,
            rollback_deployment_manifest_sha256,
        )
        if any(not SHA256_RE.fullmatch(value) for value in digests):
            raise CutoverError("V3 rotation transaction hash binding is invalid")
        if (
            not RELEASE_ID_RE.fullmatch(previous_release_id)
            or not RELEASE_ID_RE.fullmatch(candidate_release_id)
            or previous_release_id == candidate_release_id
        ):
            raise CutoverError("V3 rotation release identity is invalid")
        previous_root = previous_release_root.resolve(strict=True)
        candidate_root = candidate_release_root.resolve(strict=True)
        if previous_root == candidate_root:
            raise CutoverError("V3 rotation requires distinct immutable releases")
        parent = _safe_parent(state_parent)
        journal = parent / "cutover-activation.json"
        marker = parent / "active-release.json"
        if (
            not marker.is_file()
            or marker.is_symlink()
            or _sha256_bytes(marker.read_bytes()) != previous_marker_sha256
        ):
            raise CutoverError("V3 rotation previous marker drifted before begin")
        if (
            not journal.is_file()
            or journal.is_symlink()
            or _sha256_bytes(journal.read_bytes()) != previous_journal_sha256
        ):
            raise CutoverError("V3 rotation previous journal drifted before begin")
        current_marker = active_release_marker(
            marker,
            expected_release="v3",
            expected_release_id=previous_release_id,
            expected_release_root=previous_root,
            expected_manifest_sha256=previous_release_manifest_sha256,
        )
        transaction_id = uuid.uuid4().hex
        current = clock()
        initial_evidence = {
            "plan_sha256": plan_sha256,
            "previous_marker_sha256": previous_marker_sha256,
            "previous_journal_sha256": previous_journal_sha256,
            "previous_transaction_id": current_marker["transaction_id"],
            "previous_release_id": previous_release_id,
            "previous_release_root": str(previous_root),
            "previous_release_manifest_sha256": previous_release_manifest_sha256,
            "release_id": candidate_release_id,
            "release_root": str(candidate_root),
            "release_manifest_sha256": candidate_release_manifest_sha256,
            "candidate_deployment_manifest_sha256": (
                candidate_deployment_manifest_sha256
            ),
            "rollback_deployment_manifest_sha256": (
                rollback_deployment_manifest_sha256
            ),
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
            "schema": ROTATION_SCHEMA,
            "schema_version": 2,
            "transaction_id": transaction_id,
            "operation": ROTATION_OPERATION,
            "phase": "prepared",
            "sequence": 1,
            "started_at": current,
            "updated_at": current,
            **initial_evidence,
            "history": history,
        }
        _atomic_replace(journal, _canonical_json(payload))
        return cls(journal, marker, transaction_id, clock)

    def document(self) -> dict[str, Any]:
        payload = _load(self.journal_path, description="V3 rotation journal")
        if (
            payload.get("schema") != ROTATION_SCHEMA
            or payload.get("schema_version") != 2
            or payload.get("operation") != ROTATION_OPERATION
            or payload.get("transaction_id") != self.transaction_id
        ):
            raise CutoverError("V3 rotation journal identity drifted")
        _validate_history(payload)
        return payload

    def advance(self, phase: str, **evidence: Any) -> dict[str, Any]:
        payload = self.document()
        current_phase = str(payload.get("phase") or "")
        if phase not in ROTATION_TRANSITIONS.get(current_phase, set()):
            raise CutoverError(
                f"invalid V3 rotation phase transition: {current_phase!r} -> {phase!r}"
            )
        sequence = int(payload["sequence"]) + 1
        now = self.clock()
        history = list(payload["history"])
        history.append(
            _chain_entry(
                transaction_id=self.transaction_id,
                sequence=sequence,
                phase=phase,
                at=now,
                previous_entry_sha256=history[-1]["entry_sha256"],
                evidence=evidence,
            )
        )
        updated = {
            **payload,
            "phase": phase,
            "sequence": sequence,
            "updated_at": now,
            **evidence,
            "history": history,
        }
        digest = _atomic_replace(self.journal_path, _canonical_json(updated))
        return {**history[-1], "journal_sha256": digest}

    def commit_candidate(self) -> dict[str, Any]:
        payload = self.document()
        if payload.get("phase") != "candidate_files_installed":
            raise CutoverError("V3 rotation candidate is not ready to commit")
        intent = self.advance("candidate_commit_intent")
        payload = self.document()
        now = self.clock()
        marker = {
            "schema": MARKER_SCHEMA,
            "schema_version": 1,
            "transaction_id": self.transaction_id,
            "release": "v3",
            "release_id": payload["release_id"],
            "release_root": payload["release_root"],
            "release_manifest_sha256": payload["release_manifest_sha256"],
            "committed_at": now,
        }
        marker_sha256 = _atomic_replace(self.marker_path, _canonical_json(marker))
        receipt = self.advance(
            "candidate_committed",
            active_release_marker_sha256=marker_sha256,
            active_release_marker=marker,
        )
        return {
            **receipt,
            "intent_receipt": intent,
            "active_release_marker": marker,
            "active_release_marker_sha256": marker_sha256,
        }

    def mark_active(self, *, reconciliation_after: Mapping[str, Any]) -> dict[str, Any]:
        return self.advance(
            "candidate_active",
            reconciliation_after=dict(reconciliation_after),
        )


__all__ = [
    "ACTIVE_RELEASE_STATE_SCHEMA",
    "ACTIVE_RELEASE_ADMISSION_LOCK",
    "ACTIVE_V3_RESTART_PHASES",
    "ActiveReleaseAdmission",
    "ActivationTransaction",
    "MARKER_SCHEMA",
    "SCHEMA",
    "ROTATION_OPERATION",
    "ROTATION_SCHEMA",
    "ROTATION_TRANSITIONS",
    "V3RotationTransaction",
    "active_release_marker",
    "acquire_active_release_admission",
    "verify_active_release_snapshot",
]
