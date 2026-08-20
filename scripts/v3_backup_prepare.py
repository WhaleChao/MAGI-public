#!/usr/bin/env python3
"""Prepare and restore-verify V3 cutover backup evidence without touching live data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_COVERAGE = ("sqlite", "website_assets", "website_data")
CONTENT_MANIFEST = "backup-content.json"


class BackupBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourceEntry:
    source: Path
    source_relative: str
    backup_relative: str
    is_directory: bool
    device: int
    inode: int
    size: int
    mtime_ns: int
    mode: int
    sha256: str

    @classmethod
    def capture(
        cls,
        source: Path,
        *,
        source_relative: str,
        backup_relative: str,
        is_directory: bool,
    ) -> "SourceEntry":
        metadata = source.lstat()
        expected_type = stat.S_ISDIR if is_directory else stat.S_ISREG
        if source.is_symlink() or not expected_type(metadata.st_mode):
            raise BackupBlocked(f"mutable source contains an unsafe entry: {source_relative}")
        digest = "" if is_directory else _sha256(source)
        after_hash = source.lstat()
        if (
            after_hash.st_dev,
            after_hash.st_ino,
            after_hash.st_size,
            after_hash.st_mtime_ns,
            stat.S_IMODE(after_hash.st_mode),
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            stat.S_IMODE(metadata.st_mode),
        ):
            raise BackupBlocked(f"mutable source changed while hashing: {source_relative}")
        return cls(
            source=source,
            source_relative=source_relative,
            backup_relative=backup_relative,
            is_directory=is_directory,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            mode=stat.S_IMODE(metadata.st_mode),
            sha256=digest,
        )

    def signature(self) -> tuple[object, ...]:
        return (
            self.source_relative,
            self.backup_relative,
            self.is_directory,
            self.device,
            self.inode,
            self.size,
            self.mtime_ns,
            self.mode,
            self.sha256,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _safe_source(root: Path, path: Path) -> Path:
    if path.expanduser().is_symlink():
        raise BackupBlocked(f"database source must not be a symlink: {path}")
    try:
        source = path.expanduser().resolve(strict=True)
        source.relative_to(root)
    except (OSError, ValueError) as exc:
        raise BackupBlocked(f"database source escapes or is missing from V2 root: {path}") from exc
    if not source.is_file() or source.stat().st_size <= 0:
        raise BackupBlocked(f"database source is empty or not a file: {source}")
    return source


def _safe_website_root(path: Path, *, v2_root: Path) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise BackupBlocked("website root must be an absolute non-symlink directory")
    try:
        root = raw.resolve(strict=True)
    except OSError as exc:
        raise BackupBlocked(f"website root is unavailable: {exc}") from exc
    # The current V2 deployment keeps whalechao.github.io beneath its release
    # root, while a future deployment may mount it externally.  Both layouts
    # are safe as long as the website is not the V2 root (or an ancestor of it)
    # and backup output remains separate.  The scanner below still limits
    # coverage to the explicit mutable data/assets trees.
    if not root.is_dir() or root == v2_root or root in v2_root.parents:
        raise BackupBlocked("website root must be a dedicated website directory")
    for name in ("data", "assets"):
        child = root / name
        if child.is_symlink() or not child.is_dir():
            raise BackupBlocked(f"website mutable directory is missing or unsafe: {name}")
    return root


def _quick_check(path: Path) -> str:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
    return str(row[0]) if row else "missing_result"


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as input_db:
        with sqlite3.connect(destination) as output_db:
            input_db.backup(output_db)
            output_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if _quick_check(destination) != "ok":
        raise BackupBlocked(f"SQLite backup quick_check failed: {source}")


def _scan_website(root: Path) -> tuple[SourceEntry, ...]:
    entries: list[SourceEntry] = []

    def scan(directory: Path, relative: Path) -> None:
        relative_text = relative.as_posix()
        entries.append(
            SourceEntry.capture(
                directory,
                source_relative=relative_text,
                backup_relative=(Path("website") / relative).as_posix(),
                is_directory=True,
            )
        )
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise BackupBlocked(f"website mutable directory is unreadable: {relative_text}") from exc
        for child in children:
            child_path = Path(child.path)
            child_relative = relative / child.name
            if child.is_symlink():
                raise BackupBlocked(
                    f"website mutable source contains a symlink: {child_relative.as_posix()}"
                )
            if child.is_dir(follow_symlinks=False):
                scan(child_path, child_relative)
            elif child.is_file(follow_symlinks=False):
                entries.append(
                    SourceEntry.capture(
                        child_path,
                        source_relative=child_relative.as_posix(),
                        backup_relative=(Path("website") / child_relative).as_posix(),
                        is_directory=False,
                    )
                )
            else:
                raise BackupBlocked(
                    f"website mutable source contains a special file: {child_relative.as_posix()}"
                )

    scan(root / "data", Path("data"))
    scan(root / "assets", Path("assets"))
    return tuple(entries)


def _same_signature(entry: SourceEntry, metadata: os.stat_result) -> bool:
    return (
        metadata.st_dev == entry.device
        and metadata.st_ino == entry.inode
        and metadata.st_size == entry.size
        and metadata.st_mtime_ns == entry.mtime_ns
        and stat.S_IMODE(metadata.st_mode) == entry.mode
    )


def _copy_mutable_file(entry: SourceEntry, destination: Path) -> dict[str, Any]:
    descriptor = os.open(entry.source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_signature(entry, before):
            raise BackupBlocked(f"website mutable source changed before copy: {entry.source_relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(descriptor, "rb", closefd=False) as source, destination.open("xb") as output:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(descriptor)
        if not _same_signature(entry, after) or digest.hexdigest() != entry.sha256:
            raise BackupBlocked(f"website mutable source changed during copy: {entry.source_relative}")
    finally:
        os.close(descriptor)
    destination.chmod(entry.mode)
    return {
        "scope": "website_data" if entry.source_relative.startswith("data/") else "website_assets",
        "source": entry.source_relative,
        "backup": entry.backup_relative,
        "sha256": digest.hexdigest(),
        "size": destination.stat().st_size,
        "mode": f"{entry.mode:04o}",
    }


def _directory_row(entry: SourceEntry) -> dict[str, Any]:
    scope = "website_data" if entry.source_relative == "data" or entry.source_relative.startswith("data/") else "website_assets"
    return {
        "scope": scope,
        "source": entry.source_relative,
        "backup": entry.backup_relative,
        "mode": f"{entry.mode:04o}",
    }


def _validate_manifest(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise BackupBlocked("backup content manifest schema_version must equal 2")
    databases = payload.get("databases")
    mutable_files = payload.get("mutable_files")
    mutable_directories = payload.get("mutable_directories")
    if not isinstance(databases, list) or not databases:
        raise BackupBlocked("backup content manifest has no SQLite databases")
    if not isinstance(mutable_files, list) or not isinstance(mutable_directories, list):
        raise BackupBlocked("backup content manifest mutable inventory is invalid")
    expected_scopes = set(REQUIRED_COVERAGE[1:])
    directory_scopes = {
        row.get("scope") for row in mutable_directories if isinstance(row, dict)
    }
    if not expected_scopes.issubset(directory_scopes):
        raise BackupBlocked("backup content manifest lacks website data/assets coverage")
    all_rows = [*databases, *mutable_files]
    seen: set[str] = set()
    for row in all_rows:
        if not isinstance(row, dict):
            raise BackupBlocked("backup content manifest file row is invalid")
        backup = row.get("backup")
        digest = row.get("sha256")
        size = row.get("size")
        relative = Path(str(backup))
        if (
            not isinstance(backup, str)
            or not backup
            or relative.is_absolute()
            or ".." in relative.parts
            or backup in seen
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise BackupBlocked("backup content manifest file row is invalid")
        if row in databases:
            if not isinstance(row.get("source"), str) or row.get("quick_check") != "ok":
                raise BackupBlocked("backup content manifest SQLite row is invalid")
        else:
            scope = row.get("scope")
            source = row.get("source")
            if (
                scope not in expected_scopes
                or not isinstance(source, str)
                or not source.startswith("data/" if scope == "website_data" else "assets/")
                or not isinstance(row.get("mode"), str)
                or not re.fullmatch(r"0[0-7]{3}", row["mode"])
            ):
                raise BackupBlocked("backup content manifest mutable file row is invalid")
        seen.add(backup)
    directory_names: set[str] = set()
    for row in mutable_directories:
        if not isinstance(row, dict):
            raise BackupBlocked("backup content manifest directory row is invalid")
        backup = row.get("backup")
        relative = Path(str(backup))
        if (
            not isinstance(backup, str)
            or not backup
            or relative.is_absolute()
            or ".." in relative.parts
            or backup in directory_names
            or backup in seen
            or not isinstance(row.get("mode"), str)
            or not re.fullmatch(r"0[0-7]{3}", row["mode"])
        ):
            raise BackupBlocked("backup content manifest directory row is invalid")
        directory_names.add(backup)
    required_roots = {
        "website/data": "website_data",
        "website/assets": "website_assets",
    }
    by_directory = {row["backup"]: row for row in mutable_directories}
    if any(
        root not in by_directory or by_directory[root].get("scope") != scope
        for root, scope in required_roots.items()
    ):
        raise BackupBlocked("backup content manifest lacks exact website data/assets roots")
    return databases, mutable_files, mutable_directories


def _safe_member_name(name: str) -> bool:
    path = Path(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts


def verify_backup(
    *,
    archive_path: Path,
    archive_sha256: str,
    restore_dir: Path,
) -> dict[str, Any]:
    """Restore an archive only into a new sandbox and verify every manifest entry."""

    raw_archive = archive_path.expanduser()
    if (
        not raw_archive.is_absolute()
        or raw_archive.is_symlink()
        or not SHA256_RE.fullmatch(archive_sha256)
    ):
        raise BackupBlocked("backup archive must be an absolute hash-bound non-symlink file")
    archive = raw_archive.resolve(strict=True)
    if not archive.is_file() or _sha256(archive) != archive_sha256:
        raise BackupBlocked("backup archive SHA-256 mismatch")
    raw_restore = restore_dir.expanduser()
    if not raw_restore.is_absolute() or raw_restore.is_symlink():
        raise BackupBlocked("restore sandbox must be a new absolute path")
    restore = raw_restore.resolve(strict=False)
    if restore.exists():
        raise BackupBlocked("restore sandbox must be a new absolute path")
    if not restore.parent.is_dir() or _overlaps(restore, archive):
        raise BackupBlocked("restore sandbox parent is unsafe")

    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)) or any(not _safe_member_name(name) for name in names):
                raise BackupBlocked("backup archive contains duplicate or unsafe paths")
            by_name = {member.name: member for member in members}
            manifest_member = by_name.get(CONTENT_MANIFEST)
            if manifest_member is None or not manifest_member.isfile():
                raise BackupBlocked("backup archive content manifest is missing")
            manifest_stream = bundle.extractfile(manifest_member)
            if manifest_stream is None:
                raise BackupBlocked("backup archive content manifest is unreadable")
            manifest_bytes = manifest_stream.read(16 * 1024 * 1024 + 1)
            if len(manifest_bytes) > 16 * 1024 * 1024:
                raise BackupBlocked("backup content manifest is too large")
            try:
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise BackupBlocked("backup content manifest is invalid") from exc
            databases, mutable_files, mutable_directories = _validate_manifest(manifest)
            source_roots = manifest.get("source_roots")
            if not isinstance(source_roots, dict) or set(source_roots) != {"v2", "website"}:
                raise BackupBlocked("backup source root bindings are missing")
            for value in source_roots.values():
                if not isinstance(value, str) or not Path(value).is_absolute():
                    raise BackupBlocked("backup source root binding is invalid")
                if _overlaps(restore, Path(value).resolve(strict=False)):
                    raise BackupBlocked("restore sandbox overlaps a protected source root")

            expected_files = {CONTENT_MANIFEST} | {
                row["backup"] for row in [*databases, *mutable_files]
            }
            expected_directories = {row["backup"] for row in mutable_directories}
            actual_files = {member.name for member in members if member.isfile()}
            actual_directories = {member.name.rstrip("/") for member in members if member.isdir()}
            if actual_files != expected_files or actual_directories != expected_directories:
                raise BackupBlocked("backup archive members differ from the content manifest")
            if any(not (member.isfile() or member.isdir()) for member in members):
                raise BackupBlocked("backup archive contains links or special files")

            restore.mkdir(mode=0o700)
            for row in sorted(mutable_directories, key=lambda item: len(Path(item["backup"]).parts)):
                destination = restore / row["backup"]
                destination.mkdir(parents=True, exist_ok=False)
                destination.chmod(int(row["mode"], 8))
            file_rows = {row["backup"]: row for row in [*databases, *mutable_files]}
            for name in sorted(expected_files - {CONTENT_MANIFEST}):
                member = by_name[name]
                source = bundle.extractfile(member)
                if source is None:
                    raise BackupBlocked(f"backup archive member is unreadable: {name}")
                destination = restore / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                row = file_rows[name]
                if destination.stat().st_size != row["size"] or _sha256(destination) != row["sha256"]:
                    raise BackupBlocked(f"restored file verification failed: {name}")
                if isinstance(row.get("mode"), str):
                    destination.chmod(int(row["mode"], 8))
            manifest_path = restore / CONTENT_MANIFEST
            with manifest_path.open("xb") as handle:
                handle.write(manifest_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            for row in databases:
                if _quick_check(restore / row["backup"]) != "ok":
                    raise BackupBlocked(f"restored SQLite verification failed: {row['source']}")

        return {
            "schema_version": 2,
            "actual_restore_performed": True,
            "status": "passed",
            "backup_sha256": archive_sha256,
            "content_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "verified_scopes": list(REQUIRED_COVERAGE),
            "verified_databases": len(databases),
            "verified_mutable_files": len(mutable_files),
            "verified_mutable_directories": len(mutable_directories),
            "restore_root": str(restore),
        }
    except BaseException:
        if restore.exists() and not restore.is_symlink():
            shutil.rmtree(restore)
        raise


def prepare_backup(
    *,
    source_root: Path,
    database_paths: Sequence[Path],
    website_root: Path,
    output_dir: Path,
    campaign_id: str,
    release_sha: str,
    hardware_id: str,
    gate_config_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = source_root.expanduser().resolve(strict=True)
    website = _safe_website_root(website_root, v2_root=root)
    output = output_dir.expanduser().resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise BackupBlocked("backup output directory must not already exist")
    if (
        not output.parent.is_dir()
        or _overlaps(output, root)
        or _overlaps(output, website)
    ):
        raise BackupBlocked("backup output, V2 source, and website roots must be separate")
    if not database_paths:
        raise BackupBlocked("at least one SQLite database is required")
    if not COMMIT_RE.fullmatch(release_sha):
        raise BackupBlocked("release_sha must be a Git commit digest")
    if not SHA256_RE.fullmatch(gate_config_sha256):
        raise BackupBlocked("gate_config_sha256 must be lowercase SHA-256")

    sources = tuple(_safe_source(root, path) for path in database_paths)
    relative_sources = [path.relative_to(root) for path in sources]
    if len(relative_sources) != len(set(relative_sources)):
        raise BackupBlocked("database sources contain duplicates")
    website_entries = _scan_website(website)

    output.mkdir(mode=0o700)
    backup_tree = output / "sqlite"
    backup_tree.mkdir()
    website_tree = output / "website"
    website_tree.mkdir()
    database_rows: list[dict[str, Any]] = []
    mutable_files: list[dict[str, Any]] = []
    mutable_directories = [
        _directory_row(entry) for entry in website_entries if entry.is_directory
    ]
    try:
        for source, relative in zip(sources, relative_sources, strict=True):
            destination = backup_tree / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _backup_sqlite(source, destination)
            database_rows.append(
                {
                    "source": relative.as_posix(),
                    "backup": (Path("sqlite") / relative).as_posix(),
                    "sha256": _sha256(destination),
                    "size": destination.stat().st_size,
                    "quick_check": "ok",
                }
            )
        for entry in website_entries:
            destination = output / entry.backup_relative
            if entry.is_directory:
                destination.mkdir(parents=True, exist_ok=True)
                destination.chmod(entry.mode)
            else:
                mutable_files.append(_copy_mutable_file(entry, destination))
        if [entry.signature() for entry in website_entries] != [
            entry.signature() for entry in _scan_website(website)
        ]:
            raise BackupBlocked("website mutable source changed while preparing backup")

        manifest_payload = {
            "schema_version": 2,
            "coverage": list(REQUIRED_COVERAGE),
            "source_roots": {"v2": str(root), "website": str(website)},
            "databases": database_rows,
            "mutable_files": mutable_files,
            "mutable_directories": mutable_directories,
        }
        content_manifest = output / CONTENT_MANIFEST
        _write_json(content_manifest, manifest_payload)
        content_manifest_sha256 = _sha256(content_manifest)
        archive = output / "v2-state-and-website-backup.tar.gz"
        with tarfile.open(archive, "x:gz") as bundle:
            bundle.add(content_manifest, arcname=CONTENT_MANIFEST, recursive=False)
            for row in mutable_directories:
                info = tarfile.TarInfo(row["backup"])
                info.type = tarfile.DIRTYPE
                info.mode = int(row["mode"], 8)
                info.mtime = 0
                bundle.addfile(info)
            for row in [*database_rows, *mutable_files]:
                bundle.add(output / row["backup"], arcname=row["backup"], recursive=False)
        archive_sha = _sha256(archive)

        with tempfile.TemporaryDirectory(
            prefix="magi-v3-restore-drill-parent-",
            dir=output.parent,
        ) as temporary:
            restore_root = Path(temporary) / "restored"
            verification = verify_backup(
                archive_path=archive,
                archive_sha256=archive_sha,
                restore_dir=restore_root,
            )

        created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        drill = output / "restore-drill.json"
        drill_payload = {
            **verification,
            "restore_root": "temporary_sandbox_removed",
            "performed_at": created_at,
        }
        _write_json(drill, drill_payload)
        metadata = {
            "schema_version": 2,
            "campaign_id": campaign_id,
            "release_sha": release_sha,
            "hardware_id": hardware_id,
            "gate_config_sha256": gate_config_sha256,
            "source_release_sha": release_sha,
            "created_at": created_at,
            "artifact_path": archive.name,
            "sha256": archive_sha,
            "coverage": list(REQUIRED_COVERAGE),
            "database_count": len(database_rows),
            "mutable_file_count": len(mutable_files),
            "mutable_directory_count": len(mutable_directories),
            "content_manifest": {
                "path": content_manifest.name,
                "sha256": content_manifest_sha256,
            },
            "restore_drill": {
                "actual_restore_performed": True,
                "status": "passed",
                "backup_sha256": archive_sha,
                "content_manifest_sha256": content_manifest_sha256,
                "verified_scopes": list(REQUIRED_COVERAGE),
                "verified_databases": len(database_rows),
                "verified_mutable_files": len(mutable_files),
                "verified_mutable_directories": len(mutable_directories),
                "evidence_path": drill.name,
                "evidence_sha256": _sha256(drill),
            },
        }
        _write_json(output / "backup-metadata.json", metadata)
        return metadata
    except BaseException:
        (output / "backup-metadata.json").unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, action="append", required=True)
    parser.add_argument("--website-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        report = prepare_backup(
            source_root=args.source_root,
            database_paths=args.database,
            website_root=args.website_root,
            output_dir=args.output_dir,
            campaign_id=args.campaign_id,
            release_sha=args.release_sha,
            hardware_id=args.hardware_id,
            gate_config_sha256=args.gate_config_sha256,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps({"status": "passed", **report}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
