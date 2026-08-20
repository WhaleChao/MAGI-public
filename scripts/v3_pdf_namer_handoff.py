#!/usr/bin/env python3
"""Copy allowlisted pdf-namer learning state into the private V3 runtime.

The public manifest contains only generic state identifiers and aggregate
measurements.  It never contains source values or source/destination file
names.  A precopy is safe while V2 is live; finalization must happen only
after the caller has independently proved that V2 is stopped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:  # Support ``python scripts/v3_pdf_namer_handoff.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.v3_source_contract import LIVE_V2_HOME_RELATIVE, account_home

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = account_home() / LIVE_V2_HOME_RELATIVE / "skills" / "pdf-namer"
DEFAULT_DESTINATION = (
    account_home()
    / "Library"
    / "Application Support"
    / "MAGI"
    / "runtime"
    / "MAGI_v3"
    / "shared"
    / "pdf-namer"
)

SCHEMA = "magi.v3.pdf-namer-handoff/v1"
SHA256_LEN = 64


@dataclass(frozen=True)
class StateSpec:
    state_id: str
    source_relative: str
    destination_relative: str


# Exact paths only.  Programs, prompts, arbitrary JSON, and transient job
# payloads are deliberately impossible to select through this interface.
STATE_SPECS = (
    StateSpec("training_data", "training_data.json", "training_data.json"),
    StateSpec("corrections", "_corrections.json", "_corrections.json"),
    StateSpec("case_index", "_case_index.json", "_case_index.json"),
    StateSpec("filing_log", "_filing_log.json", "_filing_log.json"),
    StateSpec("learned_filename_rules", "_learned_filename_rules.json", "_learned_filename_rules.json"),
    StateSpec("nightly_report", "_nightly_report.json", "_nightly_report.json"),
    StateSpec("threshold_state", "_threshold_state.json", "_threshold_state.json"),
    StateSpec("database_rules_cache", "db_rules_cache.json", "db_rules_cache.json"),
    StateSpec("pending_learns", "_pending_learns.json", "_pending_learns.json"),
    StateSpec("rename_snapshot", "_rename_snapshot.json", "_rename_snapshot.json"),
    StateSpec("rename_log", "_rename_log.json", "_rename_log.json"),
    StateSpec("nightly_training_log", "_nightly_train.log", "logs/nightly_train.log"),
)


class HandoffError(RuntimeError):
    """Fail-closed handoff error whose message contains no case data."""


@dataclass(frozen=True)
class SnapshotEntry:
    spec: StateSpec
    data: bytes
    sha256: str
    size: int
    record_count: int

    def public(self) -> dict[str, Any]:
        return {
            "state_id": self.spec.state_id,
            "sha256": self.sha256,
            "size": self.size,
            "record_count": self.record_count,
        }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path_binding(path: Path) -> str:
    return _sha256_bytes(str(path).encode("utf-8"))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise HandoffError("handoff path contains a symlink component")


def _canonical_paths(source: Path, destination: Path, manifest: Path) -> tuple[Path, Path, Path]:
    raw_source = source.expanduser()
    raw_destination = destination.expanduser()
    raw_manifest = manifest.expanduser()
    if not all(path.is_absolute() for path in (raw_source, raw_destination, raw_manifest)):
        raise HandoffError("source, destination, and manifest must be absolute paths")
    for path in (raw_source, raw_destination, raw_manifest):
        _reject_symlink_components(path)
    try:
        source_path = raw_source.resolve(strict=True)
    except OSError as exc:
        raise HandoffError("source state directory is unavailable") from exc
    if not source_path.is_dir():
        raise HandoffError("source state path must be a directory")
    destination_path = raw_destination.resolve(strict=False)
    manifest_path = raw_manifest.resolve(strict=False)
    if source_path == destination_path or _is_relative_to(destination_path, source_path):
        raise HandoffError("destination must be outside the V2 source tree")
    if _is_relative_to(source_path, destination_path):
        raise HandoffError("source must be outside the V3 destination tree")
    if _is_relative_to(manifest_path, source_path) or _is_relative_to(manifest_path, destination_path):
        raise HandoffError("manifest must be outside source and destination state directories")
    return source_path, destination_path, manifest_path


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _read_source(source: Path, spec: StateSpec) -> SnapshotEntry | None:
    path = source / spec.source_relative
    if not path.exists():
        if path.is_symlink():
            raise HandoffError(f"allowlisted source state is symlinked: {spec.state_id}")
        return None
    _reject_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HandoffError(f"allowlisted source state is unreadable: {spec.state_id}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
        ):
            raise HandoffError(f"allowlisted source state is not a private regular file: {spec.state_id}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _file_signature(before) != _file_signature(after):
            raise HandoffError(f"allowlisted source state changed while reading: {spec.state_id}")
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    return SnapshotEntry(spec, data, _sha256_bytes(data), len(data), _record_count(data, spec))


def _record_count(data: bytes, spec: StateSpec) -> int:
    if spec.source_relative.endswith(".json"):
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HandoffError(f"allowlisted JSON state is invalid: {spec.state_id}") from exc
        if isinstance(value, (list, dict)):
            return len(value)
        return 1 if value is not None else 0
    return sum(1 for line in data.splitlines() if line.strip())


def _snapshot(source: Path) -> tuple[SnapshotEntry, ...]:
    entries = tuple(entry for spec in STATE_SPECS if (entry := _read_source(source, spec)) is not None)
    return entries


def _snapshot_digest(entries: tuple[SnapshotEntry, ...]) -> str:
    public = [entry.public() for entry in entries]
    encoded = json.dumps(public, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return _sha256_bytes(encoded)


def _same_snapshot(left: tuple[SnapshotEntry, ...], right: tuple[SnapshotEntry, ...]) -> bool:
    return [entry.public() for entry in left] == [entry.public() for entry in right]


def _ensure_directory(path: Path) -> None:
    _reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_components(path)
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise HandoffError("destination directory is unsafe")
    os.chmod(path, 0o700)


def _verify_destination_file(path: Path, entry: SnapshotEntry) -> None:
    _verify_private_destination_bytes(
        path,
        state_id=entry.spec.state_id,
        expected_size=entry.size,
        expected_sha256=entry.sha256,
        snapshot_name="bound snapshot",
    )


def _verify_private_destination_bytes(
    path: Path,
    *,
    state_id: str,
    expected_size: int,
    expected_sha256: str,
    snapshot_name: str,
) -> None:
    try:
        path_metadata = path.lstat()
    except OSError as exc:
        raise HandoffError(f"destination state is unavailable: {state_id}") from exc
    if (
        stat.S_ISLNK(path_metadata.st_mode)
        or not stat.S_ISREG(path_metadata.st_mode)
        or path_metadata.st_uid != os.getuid()
        or path_metadata.st_nlink != 1
        or stat.S_IMODE(path_metadata.st_mode) != 0o600
    ):
        raise HandoffError(f"destination state differs from the {snapshot_name}: {state_id}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise HandoffError(f"destination state is unavailable: {state_id}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise HandoffError(f"destination state differs from the {snapshot_name}: {state_id}")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            _file_signature(before) != _file_signature(after)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
            or stat.S_ISLNK(current.st_mode)
            or stat.S_IMODE(after.st_mode) != 0o600
            or after.st_uid != os.getuid()
            or size != expected_size
            or digest.hexdigest() != expected_sha256
        ):
            raise HandoffError(f"destination state differs from the {snapshot_name}: {state_id}")
    finally:
        os.close(descriptor)


def _verify_destination_summary(path: Path, row: dict[str, Any], spec: StateSpec) -> None:
    _verify_private_destination_bytes(
        path,
        state_id=spec.state_id,
        expected_size=row["size"],
        expected_sha256=row["sha256"],
        snapshot_name="precopy snapshot",
    )


def _write_new_atomic(path: Path, entry: SnapshotEntry) -> bool:
    """Create without overwriting; return False for an exact idempotent target."""

    _ensure_directory(path.parent)
    if path.exists() or path.is_symlink():
        _verify_destination_file(path, entry)
        return False
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(entry.data):
                offset += os.write(descriptor, entry.data[offset:])
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            _verify_destination_file(path, entry)
            return False
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _verify_destination_file(path, entry)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _replace_bound_atomic(path: Path, entry: SnapshotEntry, previous: dict[str, Any]) -> None:
    """Replace one file only while it still equals its precopy summary."""

    _ensure_directory(path.parent)
    _verify_destination_summary(path, previous, entry.spec)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(entry.data):
                offset += os.write(descriptor, entry.data[offset:])
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # Close the validation-to-replace gap as far as a filesystem API that
        # does not expose compare-and-swap permits.
        _verify_destination_summary(path, previous, entry.spec)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _verify_destination_file(path, entry)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest_payload(
    *, source: Path, destination: Path, entries: tuple[SnapshotEntry, ...], status: str
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "status": status,
        "contains_business_payload": False,
        "contains_file_names": False,
        "source_root_sha256": _path_binding(source),
        "destination_root_sha256": _path_binding(destination),
        "snapshot_sha256": _snapshot_digest(entries),
        "file_count": len(entries),
        "record_count": sum(entry.record_count for entry in entries),
        "files": [entry.public() for entry in entries],
    }


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HandoffError("handoff manifest is unavailable or invalid") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise HandoffError("handoff manifest must be owner-only 0600 regular JSON")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = path.lstat()
        if (
            _file_signature(before) != _file_signature(after)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
            or stat.S_ISLNK(current.st_mode)
            or stat.S_IMODE(after.st_mode) != 0o600
            or after.st_uid != os.getuid()
            or after.st_nlink != 1
        ):
            raise HandoffError("handoff manifest changed while being verified")
        value = json.loads(b"".join(chunks))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError("handoff manifest is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise HandoffError("handoff manifest must be owner-only 0600 regular JSON")
    return value


def _validate_public_manifest(value: dict[str, Any], *, allowed_statuses: set[str]) -> None:
    expected_keys = {
        "schema", "schema_version", "status", "contains_business_payload", "contains_file_names",
        "source_root_sha256", "destination_root_sha256", "snapshot_sha256", "file_count",
        "record_count", "files",
    }
    if set(value) != expected_keys or value.get("schema") != SCHEMA or value.get("schema_version") != 1:
        raise HandoffError("handoff manifest schema is invalid")
    if value.get("status") not in allowed_statuses:
        raise HandoffError("handoff manifest status is incomplete")
    if value.get("contains_business_payload") is not False or value.get("contains_file_names") is not False:
        raise HandoffError("handoff manifest privacy declaration is invalid")
    for key in ("source_root_sha256", "destination_root_sha256", "snapshot_sha256"):
        item = value.get(key)
        if not isinstance(item, str) or len(item) != SHA256_LEN or any(c not in "0123456789abcdef" for c in item):
            raise HandoffError("handoff manifest digest is invalid")
    files = value.get("files")
    if not isinstance(files, list) or value.get("file_count") != len(files):
        raise HandoffError("handoff manifest file summary is invalid")
    known = {spec.state_id for spec in STATE_SPECS}
    seen: set[str] = set()
    records = 0
    for row in files:
        if not isinstance(row, dict) or set(row) != {"state_id", "sha256", "size", "record_count"}:
            raise HandoffError("handoff manifest state summary is invalid")
        state_id = row.get("state_id")
        if state_id not in known or state_id in seen:
            raise HandoffError("handoff manifest contains an unallowlisted state identifier")
        seen.add(state_id)
        if not isinstance(row.get("size"), int) or isinstance(row.get("size"), bool) or row["size"] < 0:
            raise HandoffError("handoff manifest size is invalid")
        if not isinstance(row.get("record_count"), int) or isinstance(row.get("record_count"), bool) or row["record_count"] < 0:
            raise HandoffError("handoff manifest record count is invalid")
        digest = row.get("sha256")
        if not isinstance(digest, str) or len(digest) != SHA256_LEN or any(c not in "0123456789abcdef" for c in digest):
            raise HandoffError("handoff manifest state digest is invalid")
        records += row["record_count"]
    if value.get("record_count") != records:
        raise HandoffError("handoff manifest aggregate record count is invalid")


def _write_manifest_atomic(path: Path, payload: dict[str, Any], *, replace_expected: bytes | None = None) -> None:
    _ensure_directory(path.parent)
    encoded = (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()
    if path.exists() or path.is_symlink():
        current = path.read_bytes() if not path.is_symlink() else b""
        if current == encoded:
            return
        if replace_expected is None or current != replace_expected:
            raise HandoffError("refusing to overwrite a different handoff manifest")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if path.exists() and replace_expected is None:
            raise HandoffError("refusing to overwrite a different handoff manifest")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_bound_destination(destination: Path, entries: tuple[SnapshotEntry, ...]) -> None:
    allowed = {entry.spec.destination_relative for entry in entries}
    if destination.exists():
        for root, directories, files in os.walk(destination, followlinks=False):
            base = Path(root)
            if any((base / name).is_symlink() for name in (*directories, *files)):
                raise HandoffError("destination contains a symlink")
            for name in files:
                if (base / name).relative_to(destination).as_posix() not in allowed:
                    raise HandoffError("destination contains a non-allowlisted file")
    for entry in entries:
        _verify_destination_file(destination / entry.spec.destination_relative, entry)


def _verify_manifest_destination(destination: Path, manifest: dict[str, Any]) -> None:
    specs = {spec.state_id: spec for spec in STATE_SPECS}
    rows = {str(row["state_id"]): row for row in manifest["files"]}
    allowed = {specs[state_id].destination_relative for state_id in rows}
    allowed_directories = {
        parent.as_posix()
        for relative in allowed
        for parent in Path(relative).parents
        if parent != Path(".")
    }
    if destination.is_symlink() or not destination.is_dir():
        raise HandoffError("destination state root is unavailable or symlinked")
    root_metadata = destination.stat()
    if root_metadata.st_uid != os.getuid() or stat.S_IMODE(root_metadata.st_mode) != 0o700:
        raise HandoffError("destination state root must be owner-only 0700")
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for root, directories, files in os.walk(destination, followlinks=False):
        base = Path(root)
        for name in directories:
            child = base / name
            if child.is_symlink():
                raise HandoffError("destination contains a symlinked directory")
            metadata = child.stat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise HandoffError("destination directory must be owner-only 0700")
            actual_directories.add(child.relative_to(destination).as_posix())
        for name in files:
            child = base / name
            if child.is_symlink():
                raise HandoffError("destination contains a symlinked file")
            actual_files.add(child.relative_to(destination).as_posix())
    if actual_files != allowed:
        raise HandoffError("destination recursive file set differs from the precopy manifest")
    if actual_directories != allowed_directories:
        raise HandoffError("destination directory set differs from the precopy manifest")
    for state_id, row in rows.items():
        spec = specs[state_id]
        _verify_destination_summary(destination / spec.destination_relative, row, spec)


def _remove_bound_destination(path: Path, row: dict[str, Any], spec: StateSpec) -> None:
    _verify_destination_summary(path, row, spec)
    path.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _prune_empty_state_directories(destination: Path, entries: tuple[SnapshotEntry, ...]) -> None:
    required = {
        parent.as_posix()
        for entry in entries
        for parent in Path(entry.spec.destination_relative).parents
        if parent != Path(".")
    }
    candidates = {
        parent.as_posix()
        for spec in STATE_SPECS
        for parent in Path(spec.destination_relative).parents
        if parent != Path(".")
    }
    for relative in sorted(candidates - required, key=lambda value: len(Path(value).parts), reverse=True):
        path = destination / relative
        if path.is_symlink():
            raise HandoffError("destination contains a symlinked directory")
        try:
            path.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HandoffError("destination contains an unexpected non-empty directory") from exc


def precopy(source: Path, destination: Path, manifest: Path, *, apply: bool) -> dict[str, Any]:
    source_path, destination_path, manifest_path = _canonical_paths(source, destination, manifest)
    entries = _snapshot(source_path)
    payload = _manifest_payload(
        source=source_path, destination=destination_path, entries=entries, status="precopy_complete"
    )
    if not apply:
        return {**payload, "mode": "dry_run", "mutation_performed": False}
    if manifest_path.exists() or manifest_path.is_symlink():
        existing = _load_manifest(manifest_path)
        _validate_public_manifest(existing, allowed_statuses={"precopy_complete", "complete"})
        if existing != payload and existing.get("status") != "complete":
            raise HandoffError("existing handoff manifest differs from the current source snapshot")
        expected = _manifest_payload(
            source=source_path,
            destination=destination_path,
            entries=entries,
            status=str(existing.get("status")),
        )
        if existing != expected:
            raise HandoffError("existing handoff manifest binding is invalid")
        _verify_manifest_destination(destination_path, existing)
        return {**existing, "mode": "apply", "mutation_performed": False, "idempotent": True}
    _ensure_directory(destination_path)
    mutated = False
    for entry in entries:
        current = _read_source(source_path, entry.spec)
        if current is None or current.public() != entry.public():
            raise HandoffError(f"source state changed before copy: {entry.spec.state_id}")
        mutated = _write_new_atomic(destination_path / entry.spec.destination_relative, entry) or mutated
    after = _snapshot(source_path)
    if not _same_snapshot(entries, after):
        raise HandoffError("source state changed during precopy")
    _verify_bound_destination(destination_path, entries)
    _verify_manifest_destination(destination_path, payload)
    _write_manifest_atomic(manifest_path, payload)
    return {**payload, "mode": "apply", "mutation_performed": mutated or True, "idempotent": False}


def finalize(source: Path, destination: Path, manifest: Path) -> dict[str, Any]:
    source_path, destination_path, manifest_path = _canonical_paths(source, destination, manifest)
    entries = _snapshot(source_path)
    if manifest_path.exists() or manifest_path.is_symlink():
        existing = _load_manifest(manifest_path)
        _validate_public_manifest(existing, allowed_statuses={"precopy_complete", "complete"})
        if existing["source_root_sha256"] != _path_binding(source_path):
            raise HandoffError("handoff manifest source binding mismatch")
        if existing["destination_root_sha256"] != _path_binding(destination_path):
            raise HandoffError("handoff manifest destination binding mismatch")
        _verify_manifest_destination(destination_path, existing)
        if existing["status"] == "complete":
            expected = _manifest_payload(
                source=source_path, destination=destination_path, entries=entries, status="complete"
            )
            if existing != expected:
                raise HandoffError("finalized V2 source state no longer matches its evidence")
            return {**existing, "mode": "finalize", "mutation_performed": False, "idempotent": True}
        previous_bytes = manifest_path.read_bytes()
    else:
        existing = None
        previous_bytes = None
        if destination_path.exists() and any(destination_path.iterdir()):
            raise HandoffError("direct final apply requires an empty destination")

    # Snapshot once more before the first destination mutation.  V2 must be
    # stopped by the cutover executor, but this independently detects drift.
    if not _same_snapshot(entries, _snapshot(source_path)):
        raise HandoffError("V2 source state changed before final copy")

    _ensure_directory(destination_path)
    current = {entry.spec.state_id: entry for entry in entries}
    previous_rows = (
        {str(row["state_id"]): row for row in existing["files"]}
        if existing is not None
        else {}
    )
    specs = {spec.state_id: spec for spec in STATE_SPECS}
    for state_id, row in previous_rows.items():
        if state_id not in current:
            spec = specs[state_id]
            _remove_bound_destination(destination_path / spec.destination_relative, row, spec)
    for state_id, entry in current.items():
        previous = previous_rows.get(state_id)
        target = destination_path / entry.spec.destination_relative
        if previous is None:
            _write_new_atomic(target, entry)
        elif previous["sha256"] == entry.sha256 and previous["size"] == entry.size:
            _verify_destination_file(target, entry)
        else:
            _replace_bound_atomic(target, entry, previous)
    _prune_empty_state_directories(destination_path, entries)

    # This is the authoritative post-copy source re-verification required by
    # cutover.  Complete evidence is not published if any state changed.
    if not _same_snapshot(entries, _snapshot(source_path)):
        raise HandoffError("V2 source state changed during final copy")
    _verify_bound_destination(destination_path, entries)
    finalized = _manifest_payload(
        source=source_path, destination=destination_path, entries=entries, status="complete"
    )
    _verify_manifest_destination(destination_path, finalized)
    _write_manifest_atomic(manifest_path, finalized, replace_expected=previous_bytes)
    return {**finalized, "mode": "finalize", "mutation_performed": True, "idempotent": False}


def verify_manifest(
    manifest: Path,
    *,
    source: Path,
    destination: Path,
    allowed_statuses: set[str],
) -> dict[str, Any]:
    source_path, destination_path, manifest_path = _canonical_paths(source, destination, manifest)
    value = _load_manifest(manifest_path)
    _validate_public_manifest(value, allowed_statuses=allowed_statuses)
    if value["source_root_sha256"] != _path_binding(source_path):
        raise HandoffError("handoff manifest source binding mismatch")
    if value["destination_root_sha256"] != _path_binding(destination_path):
        raise HandoffError("handoff manifest destination binding mismatch")
    # Source state may keep learning between precopy and V2 ownership zero.
    # The read-only gate binds only the private precopy destination; final
    # refresh re-snapshots and re-verifies source after V2 has stopped.
    _verify_manifest_destination(destination_path, value)
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("precopy", "finalize", "verify"))
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="apply a precopy; otherwise precopy is dry-run")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "precopy":
            report = precopy(args.source, args.destination, args.manifest, apply=args.apply)
        elif args.command == "finalize":
            if args.apply:
                raise HandoffError("--apply is valid only for precopy")
            report = finalize(args.source, args.destination, args.manifest)
        else:
            if args.apply:
                raise HandoffError("--apply is valid only for precopy")
            report = verify_manifest(
                args.manifest,
                source=args.source,
                destination=args.destination,
                allowed_statuses={"complete"},
            )
    except HandoffError as exc:
        report = {
            "schema": SCHEMA,
            "status": "blocked",
            "ok": False,
            "contains_business_payload": False,
            "contains_file_names": False,
            "error": str(exc),
        }
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps({**report, "ok": True}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
