#!/usr/bin/env python3
"""Safely carry V2 LAF Gmail deduplication state into V3.

The compatibility manifest contains Gmail message identifiers only.  It must be
created after V2 is quiesced (or recreated if the source changes), and imported
before V3 starts.  Import is explicit, hash-bound, transactional, idempotent,
and writes both dedup stores used by the LAF monitor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence


SCHEMA_VERSION = 1
MANIFEST_KIND = "magi.v3.laf-gmail-dedup-compat"
CATEGORY = "email_laf"
LOCK_NAME = "magi:v3:laf-gmail-dedup-import"
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_MESSAGE_ID_BYTES = 255
MAX_ENV_BYTES = 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LAFDedupBlocked(RuntimeError):
    """The compatibility state cannot be proven safe to import."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_message_id(value: Any) -> str:
    if not isinstance(value, str):
        raise LAFDedupBlocked("processed Gmail state contains a non-string message id")
    if (
        not value
        or value != value.strip()
        or not value.isprintable()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_MESSAGE_ID_BYTES
    ):
        raise LAFDedupBlocked("processed Gmail state contains an unsafe message id")
    return value


def _source_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_nlink,
    )


def _read_source(path: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise LAFDedupBlocked("processed Gmail source must be an absolute non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(raw, flags)
    except OSError as exc:
        raise LAFDedupBlocked(f"processed Gmail source is unavailable: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size > MAX_SOURCE_BYTES
        ):
            raise LAFDedupBlocked("processed Gmail source is not a safe owner-controlled file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw_bytes = handle.read(MAX_SOURCE_BYTES + 1)
        after = os.fstat(descriptor)
        if len(raw_bytes) > MAX_SOURCE_BYTES or _source_signature(before) != _source_signature(after):
            raise LAFDedupBlocked("processed Gmail source changed while being captured")
    finally:
        os.close(descriptor)

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LAFDedupBlocked("processed Gmail source is not valid UTF-8 JSON") from exc
    if isinstance(payload, list):
        values = payload
        source_format = "legacy_list"
    elif isinstance(payload, dict):
        # LAFGmailMonitor historically loaded a mapping as set(mapping), so all
        # keys are authoritative regardless of the legacy value shape.
        values = list(payload)
        source_format = "legacy_mapping_keys"
    else:
        raise LAFDedupBlocked("processed Gmail source must contain a JSON list or object")
    records = tuple(sorted({_validate_message_id(value) for value in values}))
    if len(records) != len(values):
        raise LAFDedupBlocked("processed Gmail source contains duplicate message ids")
    resolved = raw.resolve(strict=True)
    row = {
        "path": str(resolved),
        "sha256": _sha256_bytes(raw_bytes),
        "size": len(raw_bytes),
        "format": source_format,
        "record_count": len(records),
    }
    return row, records


def _records_sha256(records: Sequence[str]) -> str:
    return _sha256_bytes(_canonical_json(list(records)))


def _sources_sha256(sources: Sequence[dict[str, Any]]) -> str:
    return _sha256_bytes(_canonical_json(list(sources)))


def _write_owner_only(path: Path, payload: dict[str, Any]) -> str:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw:
        raise LAFDedupBlocked("compatibility manifest output must be a new absolute file")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(raw.parent, directory_flags)
    except OSError as exc:
        raise LAFDedupBlocked("compatibility manifest parent is unavailable or unsafe") from exc
    try:
        parent_before = os.fstat(directory)
        if not stat.S_ISDIR(parent_before.st_mode) or parent_before.st_uid != os.getuid():
            raise LAFDedupBlocked("compatibility manifest parent is not owner-controlled")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(raw.name, flags, 0o600, dir_fd=directory)
        except OSError as exc:
            raise LAFDedupBlocked("compatibility manifest output is no longer a new safe file") from exc
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            created = os.fstat(descriptor)
            current = os.stat(raw.name, dir_fd=directory, follow_symlinks=False)
            if (
                not stat.S_ISREG(created.st_mode)
                or stat.S_IMODE(created.st_mode) != 0o600
                or created.st_uid != os.getuid()
                or created.st_nlink != 1
                or (created.st_dev, created.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise LAFDedupBlocked("compatibility manifest output identity changed while writing")
        finally:
            os.close(descriptor)
        os.fsync(directory)
        parent_after = os.fstat(directory)
        try:
            parent_path = raw.parent.lstat()
        except OSError as exc:
            raise LAFDedupBlocked("compatibility manifest parent changed while writing") from exc
        if (
            (parent_before.st_dev, parent_before.st_ino)
            != (parent_after.st_dev, parent_after.st_ino)
            or (parent_after.st_dev, parent_after.st_ino)
            != (parent_path.st_dev, parent_path.st_ino)
            or stat.S_ISLNK(parent_path.st_mode)
        ):
            raise LAFDedupBlocked("compatibility manifest parent changed while writing")
    finally:
        os.close(directory)
    return _sha256_bytes(encoded)


def create_manifest(
    source_paths: Iterable[Path],
    output: Path,
    *,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    paths = tuple(source_paths)
    if not paths:
        raise LAFDedupBlocked("at least one processed Gmail source is required")
    source_rows: list[dict[str, Any]] = []
    union: set[str] = set()
    seen_paths: set[str] = set()
    for path in paths:
        row, records = _read_source(path)
        if row["path"] in seen_paths:
            raise LAFDedupBlocked("processed Gmail sources contain the same file more than once")
        source_rows.append(row)
        seen_paths.add(row["path"])
        union.update(records)
    source_rows.sort(key=lambda row: row["path"])
    records = sorted(union)
    observed = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "category": CATEGORY,
        "created_at": observed.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_count": len(source_rows),
        "sources": source_rows,
        "source_snapshot_sha256": _sources_sha256(source_rows),
        "record_count": len(records),
        "records_sha256": _records_sha256(records),
        "records": records,
        "contains_business_payload": False,
    }
    manifest_sha256 = _write_owner_only(output, payload)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "snapshot_created",
        "manifest": str(output.expanduser()),
        "manifest_sha256": manifest_sha256,
        "source_count": len(source_rows),
        "record_count": len(records),
        "contains_business_payload": False,
        "mutation_performed": False,
    }


def _load_manifest_file(path: Path, expected_sha256: str) -> dict[str, Any]:
    raw = path.expanduser()
    if (
        not raw.is_absolute()
        or raw.is_symlink()
        or not SHA256_RE.fullmatch(expected_sha256)
    ):
        raise LAFDedupBlocked("compatibility manifest must be an absolute hash-bound file")
    if raw.resolve(strict=False) != raw:
        raise LAFDedupBlocked("compatibility manifest path is not canonical")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(raw.parent, directory_flags)
    except OSError as exc:
        raise LAFDedupBlocked("compatibility manifest parent is unavailable or unsafe") from exc
    try:
        parent_before = os.fstat(directory)
        parent_path = raw.parent.lstat()
        if (
            not stat.S_ISDIR(parent_before.st_mode)
            or (parent_before.st_dev, parent_before.st_ino)
            != (parent_path.st_dev, parent_path.st_ino)
            or stat.S_ISLNK(parent_path.st_mode)
        ):
            raise LAFDedupBlocked("compatibility manifest parent identity is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(raw.name, flags, dir_fd=directory)
        except OSError as exc:
            raise LAFDedupBlocked("compatibility manifest is unavailable or unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise LAFDedupBlocked("compatibility manifest ownership or mode is invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw_bytes = handle.read(MAX_SOURCE_BYTES + 1)
            after = os.fstat(descriptor)
            current = os.stat(raw.name, dir_fd=directory, follow_symlinks=False)
            if (
                len(raw_bytes) > MAX_SOURCE_BYTES
                or _source_signature(before) != _source_signature(after)
                or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
                or stat.S_ISLNK(current.st_mode)
                or _sha256_bytes(raw_bytes) != expected_sha256
            ):
                raise LAFDedupBlocked(
                    "compatibility manifest ownership, mode, or SHA-256 is invalid"
                )
        finally:
            os.close(descriptor)
        parent_after = os.fstat(directory)
        current_parent = raw.parent.lstat()
        if (
            (parent_before.st_dev, parent_before.st_ino)
            != (parent_after.st_dev, parent_after.st_ino)
            or (parent_after.st_dev, parent_after.st_ino)
            != (current_parent.st_dev, current_parent.st_ino)
            or stat.S_ISLNK(current_parent.st_mode)
        ):
            raise LAFDedupBlocked("compatibility manifest parent changed while reading")
    finally:
        os.close(directory)
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LAFDedupBlocked("compatibility manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise LAFDedupBlocked("compatibility manifest root must be an object")
    return payload


def load_verified_manifest(
    path: Path,
    expected_sha256: str,
    *,
    revalidate_sources: bool = True,
) -> dict[str, Any]:
    payload = _load_manifest_file(path, expected_sha256)
    sources = payload.get("sources")
    records = payload.get("records")
    if not (
        payload.get("schema_version") == SCHEMA_VERSION
        and payload.get("kind") == MANIFEST_KIND
        and payload.get("category") == CATEGORY
        and payload.get("contains_business_payload") is False
        and isinstance(sources, list)
        and bool(sources)
        and isinstance(records, list)
    ):
        raise LAFDedupBlocked("compatibility manifest identity is invalid")
    checked_records = [_validate_message_id(value) for value in records]
    if checked_records != sorted(set(checked_records)):
        raise LAFDedupBlocked("compatibility manifest records must be unique and sorted")
    if (
        payload.get("record_count") != len(checked_records)
        or payload.get("records_sha256") != _records_sha256(checked_records)
        or payload.get("source_count") != len(sources)
        or payload.get("source_snapshot_sha256") != _sources_sha256(sources)
    ):
        raise LAFDedupBlocked("compatibility manifest counters or content hashes are invalid")
    source_paths: list[str] = []
    union: set[str] = set()
    for row in sources:
        if not isinstance(row, dict):
            raise LAFDedupBlocked("compatibility manifest source row is invalid")
        source_path = row.get("path")
        if not isinstance(source_path, str) or not Path(source_path).is_absolute():
            raise LAFDedupBlocked("compatibility manifest source path is invalid")
        source_paths.append(source_path)
        if revalidate_sources:
            actual_row, actual_records = _read_source(Path(source_path))
            if actual_row != row:
                raise LAFDedupBlocked("processed Gmail source changed after the snapshot")
            union.update(actual_records)
    if source_paths != sorted(set(source_paths)):
        raise LAFDedupBlocked("compatibility manifest sources must be unique and sorted")
    if revalidate_sources and sorted(union) != checked_records:
        raise LAFDedupBlocked("processed Gmail source union no longer matches the manifest")
    return payload


class DedupStore(Protocol):
    def validate_schema(self) -> None: ...
    def acquire_lock(self, timeout_seconds: int) -> bool: ...
    def release_lock(self) -> None: ...
    def begin(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def laf_record_count(self, message_id: str) -> int: ...
    def dedup_record_count(self, message_id: str) -> int: ...
    def ensure_dedup_record(self, message_id: str) -> None: ...
    def insert_laf_record(self, message_id: str) -> None: ...


def import_verified_manifest(
    manifest: dict[str, Any],
    store: DedupStore,
    *,
    apply: bool,
    lock_timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Import one already verified manifest into both durable dedup stores."""
    if lock_timeout_seconds < 0 or lock_timeout_seconds > 60:
        raise LAFDedupBlocked("dedup import lock timeout must be between 0 and 60 seconds")
    records = manifest.get("records")
    if not isinstance(records, list) or any(not isinstance(item, str) for item in records):
        raise LAFDedupBlocked("verified manifest records are unavailable")
    store.validate_schema()
    if not store.acquire_lock(lock_timeout_seconds):
        raise LAFDedupBlocked("another LAF dedup migration owns the database lock")
    committed = False
    inserted_laf = 0
    inserted_dedup = 0
    existing_laf = 0
    existing_dedup = 0
    try:
        store.begin()
        observations: list[tuple[str, int, int]] = []
        for message_id in records:
            laf_count = int(store.laf_record_count(message_id))
            dedup_count = int(store.dedup_record_count(message_id))
            if laf_count < 0 or laf_count > 1 or dedup_count < 0 or dedup_count > 1:
                raise LAFDedupBlocked("durable LAF dedup store contains ambiguous duplicate rows")
            observations.append((message_id, laf_count, dedup_count))
            existing_laf += laf_count
            existing_dedup += dedup_count
        if apply:
            for message_id, laf_count, dedup_count in observations:
                if dedup_count == 0:
                    store.ensure_dedup_record(message_id)
                    inserted_dedup += 1
                if laf_count == 0:
                    store.insert_laf_record(message_id)
                    inserted_laf += 1
            for message_id in records:
                if (
                    int(store.laf_record_count(message_id)) != 1
                    or int(store.dedup_record_count(message_id)) != 1
                ):
                    raise LAFDedupBlocked("post-import durable LAF dedup verification failed")
            store.commit()
            committed = True
        else:
            store.rollback()
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "imported" if apply else "dry_run",
            "apply": apply,
            "transaction_committed": committed,
            "record_count": len(records),
            "existing_laf_email_records": existing_laf,
            "existing_dedup_registry": existing_dedup,
            "missing_laf_email_records_before_import": len(records) - existing_laf,
            "missing_dedup_registry_before_import": len(records) - existing_dedup,
            "inserted_laf_email_records": inserted_laf,
            "inserted_dedup_registry": inserted_dedup,
            "records_sha256": manifest.get("records_sha256"),
            "contains_business_payload": False,
            "mutation_performed": committed,
        }
    except BaseException:
        if not committed:
            with suppress(Exception):
                store.rollback()
        raise
    finally:
        with suppress(Exception):
            store.release_lock()


def verify_imported_manifest(
    manifest: dict[str, Any],
    store: DedupStore,
    *,
    lock_timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Verify the committed manifest in both stores using a new transaction.

    This deliberately does not rely on the reads performed inside the apply
    transaction.  Cutover uses it after commit so V3 cannot start unless every
    manifest identifier is durably visible in both tables.
    """

    if lock_timeout_seconds < 0 or lock_timeout_seconds > 60:
        raise LAFDedupBlocked("dedup verification lock timeout must be between 0 and 60 seconds")
    records = manifest.get("records")
    if not isinstance(records, list) or any(not isinstance(item, str) for item in records):
        raise LAFDedupBlocked("verified manifest records are unavailable")
    store.validate_schema()
    if not store.acquire_lock(lock_timeout_seconds):
        raise LAFDedupBlocked("another LAF dedup migration owns the database lock")
    transaction_started = False
    try:
        store.begin()
        transaction_started = True
        for message_id in records:
            if (
                int(store.laf_record_count(message_id)) != 1
                or int(store.dedup_record_count(message_id)) != 1
            ):
                raise LAFDedupBlocked("committed LAF dedup dual-table verification failed")
        store.rollback()
        transaction_started = False
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "dual_store_verified",
            "record_count": len(records),
            "laf_email_records_verified": len(records),
            "dedup_registry_verified": len(records),
            "records_sha256": manifest.get("records_sha256"),
            "contains_business_payload": False,
            "mutation_performed": False,
        }
    finally:
        if transaction_started:
            with suppress(Exception):
                store.rollback()
        with suppress(Exception):
            store.release_lock()


class MariaDBDedupStore:
    """Minimal transaction adapter for the two existing MariaDB tables."""

    def __init__(self, connection: Any):
        self.connection = connection
        self.columns: dict[str, dict[str, str]] = {}

    def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(query, params)
            return list(cursor.fetchall())
        finally:
            cursor.close()

    def _execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        cursor = self.connection.cursor()
        try:
            cursor.execute(query, params)
        finally:
            cursor.close()

    def _scalar(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        rows = self._fetchall(query, params)
        return rows[0][0] if rows else None

    def validate_schema(self) -> None:
        tables = self._fetchall(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (%s,%s)",
            ("dedup_registry", "laf_email_records"),
        )
        engines = {str(name): str(engine or "").upper() for name, engine in tables}
        if engines != {"dedup_registry": "INNODB", "laf_email_records": "INNODB"}:
            raise LAFDedupBlocked("LAF dedup tables must both exist and use InnoDB")
        columns = self._fetchall(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, EXTRA "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME IN (%s,%s)",
            ("dedup_registry", "laf_email_records"),
        )
        self.columns = {}
        for table, column, data_type, nullable, extra in columns:
            self.columns.setdefault(str(table), {})[str(column)] = "|".join(
                (str(data_type or "").lower(), str(nullable or ""), str(extra or "").lower())
            )
        if not {"category", "item_key", "status"}.issubset(self.columns.get("dedup_registry", {})):
            raise LAFDedupBlocked("dedup_registry schema is incomplete")
        if "gmail_message_id" not in self.columns.get("laf_email_records", {}):
            raise LAFDedupBlocked("laf_email_records schema is incomplete")
        indexes = self._fetchall(
            "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME=%s ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            ("dedup_registry",),
        )
        grouped: dict[str, tuple[int, list[str]]] = {}
        for name, non_unique, column, _sequence in indexes:
            current = grouped.setdefault(str(name), (int(non_unique), []))
            current[1].append(str(column))
        if not any(non_unique == 0 and columns == ["category", "item_key"] for non_unique, columns in grouped.values()):
            raise LAFDedupBlocked("dedup_registry lacks a unique category/item_key key")

    def acquire_lock(self, timeout_seconds: int) -> bool:
        return int(self._scalar("SELECT GET_LOCK(%s,%s)", (LOCK_NAME, timeout_seconds)) or 0) == 1

    def release_lock(self) -> None:
        self._scalar("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))

    def begin(self) -> None:
        self.connection.start_transaction(isolation_level="SERIALIZABLE")

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def laf_record_count(self, message_id: str) -> int:
        return int(
            self._scalar(
                "SELECT COUNT(*) FROM `laf_email_records` "
                "WHERE `gmail_message_id`=%s FOR UPDATE",
                (message_id,),
            )
            or 0
        )

    def dedup_record_count(self, message_id: str) -> int:
        return int(
            self._scalar(
                "SELECT COUNT(*) FROM `dedup_registry` "
                "WHERE `category`=%s AND `item_key`=%s FOR UPDATE",
                (CATEGORY, message_id),
            )
            or 0
        )

    def ensure_dedup_record(self, message_id: str) -> None:
        metadata = json.dumps(
            {"source": MANIFEST_KIND, "business_payload": False},
            ensure_ascii=False,
            sort_keys=True,
        )
        self._execute(
            "INSERT INTO `dedup_registry` "
            "(`category`,`item_key`,`status`,`metadata`,`notified_at`) "
            "VALUES (%s,%s,%s,%s,UTC_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE `item_key`=VALUES(`item_key`)",
            (CATEGORY, message_id, "done", metadata),
        )

    def insert_laf_record(self, message_id: str) -> None:
        columns = self.columns.get("laf_email_records", {})
        values: dict[str, Any] = {"gmail_message_id": message_id}
        id_spec = columns.get("id", "")
        if id_spec and "auto_increment" not in id_spec:
            values["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{MANIFEST_KIND}:{message_id}"))
        optional = {
            "subject": "",
            "sender": "",
            "processed_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "status": "migrated_dedup_only",
            "case_number": "",
            "created_case_id": "",
            "error_message": "",
            "created_date": datetime.now(timezone.utc).replace(tzinfo=None),
        }
        for name, value in optional.items():
            if name in columns:
                values[name] = value
        ordered = sorted(values)
        quoted = ",".join(f"`{name}`" for name in ordered)
        placeholders = ",".join(["%s"] * len(ordered))
        self._execute(
            f"INSERT INTO `laf_email_records` ({quoted}) VALUES ({placeholders})",
            tuple(values[name] for name in ordered),
        )


def _connect_from_environment(
    env_file: Path | None = None,
    *,
    expected_sha256: str | None = None,
) -> Any:
    values: Any = os.environ
    if env_file is not None:
        raw = env_file.expanduser()
        if (
            not raw.is_absolute()
            or raw.resolve(strict=False) != raw
            or (expected_sha256 is not None and not SHA256_RE.fullmatch(expected_sha256))
        ):
            raise LAFDedupBlocked("DB env file must be an absolute non-symlink file")
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            directory = os.open(raw.parent, directory_flags)
        except OSError as exc:
            raise LAFDedupBlocked("DB env file parent is unavailable or unsafe") from exc
        try:
            parent_before = os.fstat(directory)
            parent_path = raw.parent.lstat()
            if (
                not stat.S_ISDIR(parent_before.st_mode)
                or (parent_before.st_dev, parent_before.st_ino)
                != (parent_path.st_dev, parent_path.st_ino)
                or stat.S_ISLNK(parent_path.st_mode)
            ):
                raise LAFDedupBlocked("DB env file parent identity is unsafe")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(raw.name, flags, dir_fd=directory)
            except OSError as exc:
                raise LAFDedupBlocked("DB env file is unavailable or unsafe") from exc
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_uid != os.getuid()
                    or before.st_nlink != 1
                ):
                    raise LAFDedupBlocked("DB env file is not owner-only 0600")
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    encoded = handle.read(MAX_ENV_BYTES + 1)
                after = os.fstat(descriptor)
                current = os.stat(raw.name, dir_fd=directory, follow_symlinks=False)
                if (
                    len(encoded) > MAX_ENV_BYTES
                    or _source_signature(before) != _source_signature(after)
                    or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
                    or stat.S_ISLNK(current.st_mode)
                    or (
                        expected_sha256 is not None
                        and _sha256_bytes(encoded) != expected_sha256
                    )
                ):
                    raise LAFDedupBlocked(
                        "DB env file identity or SHA-256 changed during consumption"
                    )
            finally:
                os.close(descriptor)
            parent_after = os.fstat(directory)
            current_parent = raw.parent.lstat()
            if (
                (parent_before.st_dev, parent_before.st_ino)
                != (parent_after.st_dev, parent_after.st_ino)
                or (parent_after.st_dev, parent_after.st_ino)
                != (current_parent.st_dev, current_parent.st_ino)
                or stat.S_ISLNK(current_parent.st_mode)
            ):
                raise LAFDedupBlocked("DB env file parent changed during consumption")
        finally:
            os.close(directory)
        try:
            from dotenv import dotenv_values

            # Parse the verified bytes in memory; never reopen the path and
            # never copy DB secrets into the process environment.
            values = dotenv_values(stream=StringIO(encoded.decode("utf-8")), interpolate=False)
        except ImportError as exc:
            raise LAFDedupBlocked("python-dotenv is required for --env-file") from exc
        except UnicodeError as exc:
            raise LAFDedupBlocked("DB env file is not valid UTF-8") from exc

    def configured(primary: str, fallback: str, default: str) -> str:
        value = values.get(primary) or values.get(fallback) or default
        return str(value)

    try:
        import mysql.connector

        return mysql.connector.connect(
            host=configured("OSC_DB_HOST", "DB_HOST", "127.0.0.1"),
            port=int(configured("OSC_DB_PORT", "DB_PORT", "3306")),
            user=configured("OSC_DB_USER", "DB_USER", "casper_service"),
            password=configured("OSC_DB_PASSWORD", "DB_PASSWORD", ""),
            database="law_firm_data",
            autocommit=False,
            connection_timeout=5,
        )
    except Exception as exc:
        raise LAFDedupBlocked(f"MariaDB connection failed: {type(exc).__name__}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V2→V3 LAF Gmail dedup compatibility handoff")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot", help="create an owner-only compatibility manifest")
    snapshot.add_argument("--source", action="append", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="verify manifest binding and unchanged sources")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--manifest-sha256", required=True)
    importer = subparsers.add_parser("import-db", help="transactionally populate both dedup tables")
    importer.add_argument("--manifest", type=Path, required=True)
    importer.add_argument("--manifest-sha256", required=True)
    importer.add_argument("--env-file", type=Path)
    importer.add_argument("--lock-timeout", type=int, default=10)
    importer.add_argument("--apply", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = _parser().parse_args(argv)
    if args.command == "snapshot":
        return 0, create_manifest(args.source, args.output)
    manifest = load_verified_manifest(args.manifest, args.manifest_sha256)
    if args.command == "verify":
        return 0, {
            "schema_version": SCHEMA_VERSION,
            "status": "verified",
            "record_count": manifest["record_count"],
            "records_sha256": manifest["records_sha256"],
            "source_count": manifest["source_count"],
            "contains_business_payload": False,
            "mutation_performed": False,
        }
    connection = _connect_from_environment(args.env_file)
    try:
        result = import_verified_manifest(
            manifest,
            MariaDBDedupStore(connection),
            apply=bool(args.apply),
            lock_timeout_seconds=int(args.lock_timeout),
        )
    finally:
        with suppress(Exception):
            connection.close()
    return 0, result


def main(argv: list[str] | None = None) -> int:
    try:
        code, report = run(argv)
    except LAFDedupBlocked as exc:
        code = 2
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "ok": False,
            "error": str(exc),
            "mutation_performed": False,
        }
    except Exception as exc:  # Keep message ids and connector details out of CLI output.
        code = 2
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "ok": False,
            "error": f"unexpected_import_failure:{type(exc).__name__}",
            "mutation_performed": False,
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
