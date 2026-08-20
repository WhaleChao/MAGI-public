#!/usr/bin/env python3
"""Stage and verify the five immutable external inputs for a sealed V3 release.

The staging phase intentionally depends only on an immutable release manifest and
five explicit V2 source paths.  It never depends on a deployment manifest, so a
production deployment can bind the staged copy without a circular prerequisite.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


DEFAULT_TARGET_ROOT = (
    Path.home()
    / "Library"
    / "Application Support"
    / "MAGI"
    / "runtime"
    / "MAGI_v3"
    / "shared"
    / "external"
)
RECEIPT_NAME = "static-external-receipt.json"
SCHEMA = "magi.v3.static-external-staging/v2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SOURCE_SPECS = (
    ("environment", "file", ".env"),
    ("website", "tree", "website"),
    ("runtime_config", "file", "config.json"),
    ("google_credentials", "file", "google-credentials.json"),
    ("accounting_credentials", "file", "accounting-credentials.json"),
)


class StaticExternalStagingError(RuntimeError):
    """A release, source, target, receipt, or deployment binding is unsafe."""


@dataclass(frozen=True, slots=True)
class ReleaseContext:
    release_id: str
    release_manifest_sha256: str
    release_snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class SourceBinding:
    logical_id: str
    kind: str
    source: Path
    target_leaf: str


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    logical_id: str
    kind: str
    relative_path: str
    sha256: str | None
    size: int


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _validate_sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise StaticExternalStagingError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_existing(path: Path, *, label: str, directory: bool) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute():
        raise StaticExternalStagingError(f"{label} must be absolute")
    try:
        metadata = raw.lstat()
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise StaticExternalStagingError(f"{label} is unavailable") from exc
    if resolved != raw or stat.S_ISLNK(metadata.st_mode):
        raise StaticExternalStagingError(f"{label} must be canonical and non-symlinked")
    predicate = stat.S_ISDIR if directory else stat.S_ISREG
    if not predicate(metadata.st_mode):
        required = "directory" if directory else "regular file"
        raise StaticExternalStagingError(f"{label} must be a {required}")
    return resolved


def _stable_regular_bytes(path: Path, *, label: str) -> bytes:
    canonical = _canonical_existing(path, label=label, directory=False)
    declared = canonical.lstat()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(canonical, flags)
    except OSError as exc:
        raise StaticExternalStagingError(f"{label} could not be opened safely") from exc
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or any(
            getattr(before, field) != getattr(declared, field) for field in fields
        ):
            raise StaticExternalStagingError(f"{label} changed before its stable read")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise StaticExternalStagingError(f"{label} could not be read safely") from exc
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise StaticExternalStagingError(f"{label} changed during its stable read")
    if len(payload) != before.st_size:
        raise StaticExternalStagingError(f"{label} changed during its stable read")
    return payload


def _load_json(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StaticExternalStagingError(f"{label} is not a JSON object") from exc
    if not isinstance(parsed, dict):
        raise StaticExternalStagingError(f"{label} is not a JSON object")
    return parsed


def load_release_context(
    release_manifest: Path,
    *,
    expected_release_manifest_sha256: str,
) -> ReleaseContext:
    expected = _validate_sha(
        expected_release_manifest_sha256, label="release manifest SHA-256"
    )
    manifest_bytes = _stable_regular_bytes(release_manifest, label="release manifest")
    if _sha256(manifest_bytes) != expected:
        raise StaticExternalStagingError("release manifest SHA-256 mismatch")
    manifest = _load_json(manifest_bytes, label="release manifest")
    release_id = manifest.get("release_id")
    snapshot = _validate_sha(
        manifest.get("source_snapshot_sha256"), label="release source snapshot SHA-256"
    )
    if (
        manifest.get("schema_version") != 1
        or manifest.get("immutable") is not True
        or not isinstance(release_id, str)
        or SAFE_ID_RE.fullmatch(release_id) is None
        or manifest.get("release_sha256") != snapshot
    ):
        raise StaticExternalStagingError("release manifest is not an immutable V3 identity")
    return ReleaseContext(release_id, expected, snapshot)


def _explicit_sources(
    *,
    env_file: Path,
    website_root: Path,
    config_file: Path,
    google_credentials_file: Path,
    accounting_credentials_file: Path,
) -> tuple[SourceBinding, ...]:
    supplied = (
        env_file,
        website_root,
        config_file,
        google_credentials_file,
        accounting_credentials_file,
    )
    rows: list[SourceBinding] = []
    for (logical_id, kind, target_leaf), source in zip(SOURCE_SPECS, supplied, strict=True):
        rows.append(
            SourceBinding(
                logical_id,
                kind,
                _canonical_existing(
                    source, label=f"{logical_id} source", directory=kind == "tree"
                ),
                target_leaf,
            )
        )
    return tuple(rows)


def _tree_entries(root: Path, *, logical_id: str) -> tuple[SnapshotEntry, ...]:
    root = _canonical_existing(root, label=f"{logical_id} tree", directory=True)
    rows: list[SnapshotEntry] = []

    def visit(directory: Path, relative: PurePosixPath) -> None:
        before = directory.lstat()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise StaticExternalStagingError(f"{logical_id} tree could not be scanned") from exc
        for child in children:
            if child.name in {".", ".."} or "/" in child.name or "\x00" in child.name:
                raise StaticExternalStagingError(f"{logical_id} tree contains an unsafe name")
            path = directory / child.name
            try:
                metadata = path.lstat()
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise StaticExternalStagingError(f"{logical_id} tree changed while scanned") from exc
            child_relative = relative / child.name
            relative_text = child_relative.as_posix()
            if resolved != path or not path.is_relative_to(root):
                raise StaticExternalStagingError(f"{logical_id} tree contains a path escape")
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                rows.append(SnapshotEntry(logical_id, "directory", relative_text, None, 0))
                visit(path, child_relative)
            elif stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                payload = _stable_regular_bytes(path, label=f"{logical_id} file")
                rows.append(
                    SnapshotEntry(logical_id, "file", relative_text, _sha256(payload), len(payload))
                )
            else:
                raise StaticExternalStagingError(
                    f"{logical_id} tree may contain only regular directories and files"
                )
        after = directory.lstat()
        for field in ("st_dev", "st_ino", "st_mode", "st_mtime_ns"):
            if getattr(before, field) != getattr(after, field):
                raise StaticExternalStagingError(f"{logical_id} tree changed while scanned")

    visit(root, PurePosixPath())
    return tuple(rows)


def _source_entries(sources: Sequence[SourceBinding]) -> tuple[SnapshotEntry, ...]:
    rows: list[SnapshotEntry] = []
    for source in sources:
        if source.kind == "tree":
            rows.extend(_tree_entries(source.source, logical_id=source.logical_id))
        else:
            payload = _stable_regular_bytes(source.source, label=f"{source.logical_id} source")
            rows.append(
                SnapshotEntry(source.logical_id, "file", ".", _sha256(payload), len(payload))
            )
    return tuple(sorted(rows, key=lambda row: (row.logical_id, row.relative_path, row.kind)))


def _entry_payload(row: SnapshotEntry) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "logical_id": row.logical_id,
        "relative_path": row.relative_path,
        "sha256": row.sha256,
        "size": row.size,
    }


def _snapshot_sha(rows: Sequence[SnapshotEntry]) -> str:
    return _sha256(_json_bytes([_entry_payload(row) for row in rows]))


def _summaries(rows: Sequence[SnapshotEntry]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for logical_id, kind, _target_leaf in SOURCE_SPECS:
        selected = tuple(row for row in rows if row.logical_id == logical_id)
        files = tuple(row for row in selected if row.kind == "file")
        directories = tuple(row for row in selected if row.kind == "directory")
        result.append(
            {
                "logical_id": logical_id,
                "kind": kind,
                "file_count": len(files),
                "directory_count": len(directories),
                "byte_count": sum(row.size for row in files),
                "content_sha256": (
                    files[0].sha256 if kind == "file" and len(files) == 1 else None
                ),
                "snapshot_sha256": _snapshot_sha(selected),
            }
        )
    return result


def snapshot_static_sources(
    release_manifest: Path,
    *,
    expected_release_manifest_sha256: str,
    env_file: Path,
    website_root: Path,
    config_file: Path,
    google_credentials_file: Path,
    accounting_credentials_file: Path,
) -> dict[str, Any]:
    context = load_release_context(
        release_manifest,
        expected_release_manifest_sha256=expected_release_manifest_sha256,
    )
    sources = _explicit_sources(
        env_file=env_file,
        website_root=website_root,
        config_file=config_file,
        google_credentials_file=google_credentials_file,
        accounting_credentials_file=accounting_credentials_file,
    )
    rows = _source_entries(sources)
    return {
        "status": "snapshotted_not_staged",
        "schema": SCHEMA,
        "context": _context_payload(context),
        "logical_inputs": _summaries(rows),
        "source_snapshot_sha256": _snapshot_sha(rows),
        "source_paths_recorded": False,
        "sensitive_content_recorded": False,
    }


def _ensure_target_parent(target: Path) -> Path:
    raw = target.expanduser()
    if not raw.is_absolute() or raw.name in {"", ".", ".."}:
        raise StaticExternalStagingError("target root must be an absolute leaf path")
    parent = raw.parent
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists() and not cursor.is_symlink():
        missing.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    if cursor.is_symlink() or cursor.resolve(strict=True) != cursor:
        raise StaticExternalStagingError("target parent traverses a symlink")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    if parent.resolve(strict=True) != parent or parent.is_symlink():
        raise StaticExternalStagingError("target parent must be canonical and non-symlinked")
    if parent.stat().st_uid != os.getuid():
        raise StaticExternalStagingError("target parent must be user-owned")
    return raw


def _make_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def _copy_sources(
    sources: Sequence[SourceBinding], staging: Path, expected: Sequence[SnapshotEntry]
) -> None:
    by_key = {(row.logical_id, row.relative_path, row.kind): row for row in expected}
    for source in sources:
        target = staging / source.target_leaf
        if source.kind == "file":
            payload = _stable_regular_bytes(source.source, label=f"{source.logical_id} source")
            if _sha256(payload) != by_key[(source.logical_id, ".", "file")].sha256:
                raise StaticExternalStagingError(f"{source.logical_id} changed before copy")
            _write_exclusive(target, payload)
            continue
        _make_directory(target)
        for row in (entry for entry in expected if entry.logical_id == source.logical_id):
            destination = target / row.relative_path
            if row.kind == "directory":
                _make_directory(destination)
                continue
            payload = _stable_regular_bytes(
                source.source / row.relative_path, label="website file"
            )
            if _sha256(payload) != row.sha256:
                raise StaticExternalStagingError("website changed before copy")
            _write_exclusive(destination, payload)


def _target_entries(target: Path) -> tuple[SnapshotEntry, ...]:
    target = _canonical_existing(target, label="static external target", directory=True)
    if stat.S_IMODE(target.lstat().st_mode) != 0o700 or target.stat().st_uid != os.getuid():
        raise StaticExternalStagingError("static external target directory mode/owner is unsafe")
    expected_names = {row[2] for row in SOURCE_SPECS} | {RECEIPT_NAME}
    if {entry.name for entry in os.scandir(target)} - expected_names:
        raise StaticExternalStagingError("static external target contains unexpected entries")
    rows: list[SnapshotEntry] = []
    for logical_id, kind, target_leaf in SOURCE_SPECS:
        path = target / target_leaf
        if kind == "tree":
            if stat.S_IMODE(path.lstat().st_mode) != 0o700:
                raise StaticExternalStagingError("website directory mode must be 0700")
            tree_rows = _tree_entries(path, logical_id=logical_id)
            for row in tree_rows:
                metadata = (path / row.relative_path).lstat()
                required_mode = 0o700 if row.kind == "directory" else 0o600
                if (
                    stat.S_IMODE(metadata.st_mode) != required_mode
                    or metadata.st_uid != os.getuid()
                    or (row.kind == "file" and metadata.st_nlink != 1)
                ):
                    raise StaticExternalStagingError("website target mode/owner is unsafe")
            rows.extend(tree_rows)
            continue
        payload = _stable_regular_bytes(path, label=f"{logical_id} target")
        metadata = path.lstat()
        if (
            stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise StaticExternalStagingError(f"{logical_id} target mode/owner is unsafe")
        rows.append(SnapshotEntry(logical_id, "file", ".", _sha256(payload), len(payload)))
    return tuple(sorted(rows, key=lambda row: (row.logical_id, row.relative_path, row.kind)))


def _context_payload(context: ReleaseContext) -> dict[str, str]:
    return {
        "release_id": context.release_id,
        "release_manifest_sha256": context.release_manifest_sha256,
        "release_snapshot_sha256": context.release_snapshot_sha256,
    }


def _build_receipt(
    context: ReleaseContext,
    source_rows: Sequence[SnapshotEntry],
    target_rows: Sequence[SnapshotEntry],
) -> dict[str, Any]:
    files = tuple(row for row in target_rows if row.kind == "file")
    directories = tuple(row for row in target_rows if row.kind == "directory")
    return {
        "schema": SCHEMA,
        "status": "staged_not_installed",
        "context": _context_payload(context),
        "logical_inputs": _summaries(target_rows),
        "file_count": len(files),
        "directory_count": len(directories),
        "byte_count": sum(row.size for row in files),
        "source_snapshot_sha256": _snapshot_sha(source_rows),
        "target_snapshot_sha256": _snapshot_sha(target_rows),
        "source_paths_recorded": False,
        "sensitive_content_recorded": False,
    }


def _read_receipt(target: Path) -> tuple[dict[str, Any], bytes]:
    path = target / RECEIPT_NAME
    payload = _stable_regular_bytes(path, label="static external receipt")
    metadata = path.lstat()
    if (
        stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise StaticExternalStagingError("static external receipt mode/owner is unsafe")
    return _load_json(payload, label="static external receipt"), payload


def _verify_existing_payload(target: Path) -> tuple[dict[str, Any], tuple[SnapshotEntry, ...], bytes]:
    rows = _target_entries(target)
    receipt, receipt_bytes = _read_receipt(target)
    exact_keys = {
        "schema", "status", "context", "logical_inputs", "file_count",
        "directory_count", "byte_count", "source_snapshot_sha256",
        "target_snapshot_sha256", "source_paths_recorded", "sensitive_content_recorded",
    }
    if set(receipt) != exact_keys:
        raise StaticExternalStagingError("static external receipt fields mismatch")
    context = receipt.get("context")
    if not isinstance(context, dict) or set(context) != {
        "release_id", "release_manifest_sha256", "release_snapshot_sha256"
    }:
        raise StaticExternalStagingError("static external receipt context fields mismatch")
    if (
        not isinstance(context.get("release_id"), str)
        or SAFE_ID_RE.fullmatch(context["release_id"]) is None
        or any(
            not isinstance(context.get(key), str) or SHA256_RE.fullmatch(context[key]) is None
            for key in ("release_manifest_sha256", "release_snapshot_sha256")
        )
    ):
        raise StaticExternalStagingError("static external receipt context is invalid")
    if receipt.get("schema") != SCHEMA or receipt.get("status") != "staged_not_installed":
        raise StaticExternalStagingError("static external receipt schema/status mismatch")
    target_sha = _snapshot_sha(rows)
    if (
        receipt.get("target_snapshot_sha256") != target_sha
        or receipt.get("source_snapshot_sha256") != target_sha
        or receipt.get("logical_inputs") != _summaries(rows)
    ):
        raise StaticExternalStagingError("static external target snapshot mismatch")
    files = tuple(row for row in rows if row.kind == "file")
    directories = tuple(row for row in rows if row.kind == "directory")
    if (
        receipt.get("file_count") != len(files)
        or receipt.get("directory_count") != len(directories)
        or receipt.get("byte_count") != sum(row.size for row in files)
        or receipt.get("source_paths_recorded") is not False
        or receipt.get("sensitive_content_recorded") is not False
    ):
        raise StaticExternalStagingError("static external receipt counts/privacy mismatch")
    return receipt, rows, receipt_bytes


def _atomic_exchange(left: Path, right: Path) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        at_fdcwd = -2
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        at_fdcwd = -100
    else:
        return False
    if function is None:
        return False
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(at_fdcwd, os.fsencode(left), at_fdcwd, os.fsencode(right), 0x2) == 0:
        return True
    raise StaticExternalStagingError(
        f"atomic directory exchange failed (errno {ctypes.get_errno()})"
    )


def _publish(staging: Path, target: Path, *, refreshing: bool) -> None:
    if not refreshing:
        try:
            os.replace(staging, target)
        except OSError as exc:
            raise StaticExternalStagingError("fresh atomic publish failed") from exc
        return
    if _atomic_exchange(staging, target):
        shutil.rmtree(staging)
        return
    raise StaticExternalStagingError("platform lacks atomic directory exchange for refresh")


def stage_static_external(
    release_manifest: Path,
    *,
    expected_release_manifest_sha256: str,
    env_file: Path,
    website_root: Path,
    config_file: Path,
    google_credentials_file: Path,
    accounting_credentials_file: Path,
    expected_source_snapshot_sha256: str,
    target_root: Path = DEFAULT_TARGET_ROOT,
    refresh_expected_target_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Copy an explicit, pre-hashed V2 source snapshot without a deploy manifest."""

    context = load_release_context(
        release_manifest,
        expected_release_manifest_sha256=expected_release_manifest_sha256,
    )
    sources = _explicit_sources(
        env_file=env_file,
        website_root=website_root,
        config_file=config_file,
        google_credentials_file=google_credentials_file,
        accounting_credentials_file=accounting_credentials_file,
    )
    expected_source_sha = _validate_sha(
        expected_source_snapshot_sha256, label="expected source snapshot SHA-256"
    )
    target = _ensure_target_parent(target_root)
    exists = target.exists() or target.is_symlink()
    if target.is_symlink():
        raise StaticExternalStagingError("target root must not be a symlink")
    if exists and refresh_expected_target_snapshot_sha256 is None:
        raise StaticExternalStagingError("target exists; refresh requires its exact old snapshot SHA-256")
    if not exists and refresh_expected_target_snapshot_sha256 is not None:
        raise StaticExternalStagingError("refresh snapshot was supplied for a missing target")
    if refresh_expected_target_snapshot_sha256 is not None:
        expected_old = _validate_sha(
            refresh_expected_target_snapshot_sha256, label="refresh target snapshot SHA-256"
        )
        _old_receipt, old_rows, _old_bytes = _verify_existing_payload(target)
        if _snapshot_sha(old_rows) != expected_old:
            raise StaticExternalStagingError("refresh target snapshot SHA-256 mismatch")
    source_rows = _source_entries(sources)
    if _snapshot_sha(source_rows) != expected_source_sha:
        raise StaticExternalStagingError("explicit source snapshot SHA-256 mismatch")
    staging = target.parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    try:
        _make_directory(staging)
        _copy_sources(sources, staging, source_rows)
        source_after = _source_entries(sources)
        if source_after != source_rows:
            raise StaticExternalStagingError("source snapshot changed while staging")
        target_rows = _target_entries(staging)
        if target_rows != source_rows:
            raise StaticExternalStagingError("staged target differs from source snapshot")
        receipt = _build_receipt(context, source_rows, target_rows)
        _write_exclusive(staging / RECEIPT_NAME, _json_bytes(receipt))
        _verify_existing_payload(staging)
        _publish(staging, target, refreshing=exists)
        return verify_static_external(
            release_manifest,
            expected_release_manifest_sha256=expected_release_manifest_sha256,
            target_root=target,
            source_paths={source.logical_id: source.source for source in sources},
            expected_source_snapshot_sha256=expected_source_sha,
        )
    finally:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)


def _verify_deploy_binding(
    deploy_manifest: Path,
    *,
    expected_deploy_manifest_sha256: str,
    context: ReleaseContext,
    target: Path,
    receipt_sha256: str,
    receipt: dict[str, Any],
) -> str:
    expected = _validate_sha(
        expected_deploy_manifest_sha256, label="deployment manifest SHA-256"
    )
    payload = _stable_regular_bytes(deploy_manifest, label="deployment manifest")
    if _sha256(payload) != expected:
        raise StaticExternalStagingError("deployment manifest SHA-256 mismatch")
    deploy = _load_json(payload, label="deployment manifest")
    if (
        deploy.get("status") != "prepared_not_installed"
        or deploy.get("release_id") != context.release_id
        or deploy.get("release_manifest_sha256") != context.release_manifest_sha256
        or deploy.get("static_external_receipt") != str(target / RECEIPT_NAME)
        or deploy.get("static_external_receipt_sha256") != receipt_sha256
        or deploy.get("static_external_target_snapshot_sha256")
        != receipt["target_snapshot_sha256"]
    ):
        raise StaticExternalStagingError("deployment static external context mismatch")
    external = deploy.get("external_inputs")
    if not isinstance(external, dict):
        raise StaticExternalStagingError("deployment external input contract is missing")
    summaries = {row["logical_id"]: row for row in receipt["logical_inputs"]}
    required = {
        "env_file": str(target / ".env"),
        "env_file_sha256": summaries["environment"]["content_sha256"],
        "website_root": str(target / "website"),
        "laf_config_file": str(target / "config.json"),
        "laf_config_sha256": summaries["runtime_config"]["content_sha256"],
        "google_credentials_file": str(target / "google-credentials.json"),
        "google_credentials_sha256": summaries["google_credentials"]["content_sha256"],
        "accounting_credentials_file": str(target / "accounting-credentials.json"),
        "accounting_credentials_sha256": summaries["accounting_credentials"]["content_sha256"],
        "static_external_receipt": str(target / RECEIPT_NAME),
        "static_external_receipt_sha256": receipt_sha256,
        "static_external_target_snapshot_sha256": receipt["target_snapshot_sha256"],
    }
    if any(external.get(key) != value for key, value in required.items()):
        raise StaticExternalStagingError("deployment static external paths/hashes mismatch")
    return expected


def verify_static_external(
    release_manifest: Path,
    *,
    expected_release_manifest_sha256: str,
    target_root: Path = DEFAULT_TARGET_ROOT,
    expected_target_snapshot_sha256: str | None = None,
    source_paths: dict[str, Path] | None = None,
    expected_source_snapshot_sha256: str | None = None,
    deploy_manifest: Path | None = None,
    expected_deploy_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Reverify a staged target and optionally source and final deploy bindings."""

    context = load_release_context(
        release_manifest,
        expected_release_manifest_sha256=expected_release_manifest_sha256,
    )
    target = _canonical_existing(target_root, label="static external target", directory=True)
    receipt, target_rows, receipt_bytes = _verify_existing_payload(target)
    if receipt.get("context") != _context_payload(context):
        raise StaticExternalStagingError("static external receipt release context mismatch")
    target_sha = _snapshot_sha(target_rows)
    if expected_target_snapshot_sha256 is not None and target_sha != _validate_sha(
        expected_target_snapshot_sha256, label="expected target snapshot SHA-256"
    ):
        raise StaticExternalStagingError("expected target snapshot SHA-256 mismatch")
    source_verified = False
    if source_paths is not None or expected_source_snapshot_sha256 is not None:
        required_ids = {row[0] for row in SOURCE_SPECS}
        if source_paths is None or set(source_paths) != required_ids:
            raise StaticExternalStagingError("source reverify requires exactly five logical paths")
        sources = _explicit_sources(
            env_file=source_paths["environment"],
            website_root=source_paths["website"],
            config_file=source_paths["runtime_config"],
            google_credentials_file=source_paths["google_credentials"],
            accounting_credentials_file=source_paths["accounting_credentials"],
        )
        expected_source = _validate_sha(
            expected_source_snapshot_sha256, label="expected source snapshot SHA-256"
        )
        source_rows = _source_entries(sources)
        if (
            _snapshot_sha(source_rows) != expected_source
            or expected_source != receipt["source_snapshot_sha256"]
            or source_rows != target_rows
        ):
            raise StaticExternalStagingError("source snapshot no longer matches staged target")
        source_verified = True
    receipt_sha = _sha256(receipt_bytes)
    deploy_sha: str | None = None
    if deploy_manifest is not None or expected_deploy_manifest_sha256 is not None:
        if deploy_manifest is None or expected_deploy_manifest_sha256 is None:
            raise StaticExternalStagingError("deploy reverify requires manifest path and SHA-256")
        deploy_sha = _verify_deploy_binding(
            deploy_manifest,
            expected_deploy_manifest_sha256=expected_deploy_manifest_sha256,
            context=context,
            target=target,
            receipt_sha256=receipt_sha,
            receipt=receipt,
        )
    return {
        "status": "verified",
        "schema": SCHEMA,
        "context": _context_payload(context),
        "logical_inputs": receipt["logical_inputs"],
        "file_count": receipt["file_count"],
        "directory_count": receipt["directory_count"],
        "byte_count": receipt["byte_count"],
        "source_snapshot_sha256": receipt["source_snapshot_sha256"],
        "target_snapshot_sha256": target_sha,
        "receipt_sha256": receipt_sha,
        "source_verified": source_verified,
        "deploy_manifest_sha256": deploy_sha,
        "sensitive_content_recorded": False,
    }


def _add_release_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--release-manifest-sha256", required=True)


def _add_source_arguments(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument("--env-file", type=Path, required=required)
    parser.add_argument("--website-root", type=Path, required=required)
    parser.add_argument("--config-file", type=Path, required=required)
    parser.add_argument("--google-credentials-file", type=Path, required=required)
    parser.add_argument("--accounting-credentials-file", type=Path, required=required)


def _source_kwargs(args: argparse.Namespace) -> dict[str, Path]:
    return {
        "env_file": args.env_file,
        "website_root": args.website_root,
        "config_file": args.config_file,
        "google_credentials_file": args.google_credentials_file,
        "accounting_credentials_file": args.accounting_credentials_file,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    _add_release_arguments(snapshot_parser)
    _add_source_arguments(snapshot_parser)
    stage_parser = subparsers.add_parser("stage")
    _add_release_arguments(stage_parser)
    _add_source_arguments(stage_parser)
    stage_parser.add_argument("--expected-source-snapshot-sha256", required=True)
    stage_parser.add_argument("--target-root", type=Path, default=DEFAULT_TARGET_ROOT)
    stage_parser.add_argument("--refresh-expected-target-snapshot-sha256")
    verify_parser = subparsers.add_parser("verify")
    _add_release_arguments(verify_parser)
    _add_source_arguments(verify_parser, required=False)
    verify_parser.add_argument("--target-root", type=Path, default=DEFAULT_TARGET_ROOT)
    verify_parser.add_argument("--expected-target-snapshot-sha256")
    verify_parser.add_argument("--expected-source-snapshot-sha256")
    verify_parser.add_argument("--verify-source", action="store_true")
    verify_parser.add_argument("--deploy-manifest", type=Path)
    verify_parser.add_argument("--deploy-manifest-sha256")
    try:
        args = parser.parse_args(argv)
        if args.command == "snapshot":
            result = snapshot_static_sources(
                args.release_manifest,
                expected_release_manifest_sha256=args.release_manifest_sha256,
                **_source_kwargs(args),
            )
        elif args.command == "stage":
            result = stage_static_external(
                args.release_manifest,
                expected_release_manifest_sha256=args.release_manifest_sha256,
                expected_source_snapshot_sha256=args.expected_source_snapshot_sha256,
                target_root=args.target_root,
                refresh_expected_target_snapshot_sha256=(
                    args.refresh_expected_target_snapshot_sha256
                ),
                **_source_kwargs(args),
            )
        else:
            supplied_sources = _source_kwargs(args)
            if args.verify_source and (
                args.expected_source_snapshot_sha256 is None
                or any(value is None for value in supplied_sources.values())
            ):
                raise StaticExternalStagingError(
                    "--verify-source requires all five source paths and expected source SHA-256"
                )
            if not args.verify_source and (
                args.expected_source_snapshot_sha256 is not None
                or any(value is not None for value in supplied_sources.values())
            ):
                raise StaticExternalStagingError(
                    "source arguments require --verify-source"
                )
            result = verify_static_external(
                args.release_manifest,
                expected_release_manifest_sha256=args.release_manifest_sha256,
                target_root=args.target_root,
                expected_target_snapshot_sha256=args.expected_target_snapshot_sha256,
                source_paths=(
                    {
                        "environment": supplied_sources["env_file"],
                        "website": supplied_sources["website_root"],
                        "runtime_config": supplied_sources["config_file"],
                        "google_credentials": supplied_sources[
                            "google_credentials_file"
                        ],
                        "accounting_credentials": supplied_sources[
                            "accounting_credentials_file"
                        ],
                    }
                    if args.verify_source
                    else None
                ),
                expected_source_snapshot_sha256=(
                    args.expected_source_snapshot_sha256 if args.verify_source else None
                ),
                deploy_manifest=args.deploy_manifest,
                expected_deploy_manifest_sha256=args.deploy_manifest_sha256,
            )
    except (OSError, StaticExternalStagingError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
