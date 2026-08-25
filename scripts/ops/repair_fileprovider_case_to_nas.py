#!/usr/bin/env python3
"""Transactionally repair one File Provider-only case into canonical NAS.

The source is never deleted.  The destination must not exist and must be
inside a currently mounted SMB volume.  A content manifest is sealed before
copying, verified again before promotion, and verified once more after an
exclusive same-directory rename.  The public receipt contains aggregates and
digests only; it never records case names, filenames, or paths.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.case_path_mapper import is_authoritative_nas_write_path

SCHEMA = "magi.case-fileprovider-nas-repair/v1"
CHUNK_SIZE = 1024 * 1024
LOWER_HEX64 = set("0123456789abcdef")


class RepairError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_lower_hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value != "0" * 64
        and set(value) <= LOWER_HEX64
    )


def _is_file_provider_source(path: Path) -> bool:
    normalized = path.as_posix()
    return (
        "/Library/CloudStorage/SynologyDrive-" in normalized
        or "/SynologyDrive/" in normalized
    ) and "/.magi_mounts/" not in normalized


def _regular_directory(path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        mode = path.stat().st_mode
        return stat.S_ISDIR(mode)
    except OSError:
        return False


def _hash_regular_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RepairError("source_file_contract_failed")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        if total != info.st_size:
            raise RepairError("source_file_changed_during_hash")
        return total, digest.hexdigest()
    finally:
        os.close(fd)


def build_tree_manifest(root: Path) -> dict:
    if not _regular_directory(root):
        raise RepairError("source_root_contract_failed")
    entries: list[dict] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise RepairError("source_inventory_failed") from exc
        directories: list[Path] = []
        for child in children:
            child_path = Path(child.path)
            relative = child_path.relative_to(root).as_posix()
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise RepairError("source_inventory_failed") from exc
            if stat.S_ISLNK(info.st_mode):
                raise RepairError("source_symlink_rejected")
            if stat.S_ISDIR(info.st_mode):
                entries.append({"path": relative, "type": "dir"})
                directories.append(child_path)
            elif stat.S_ISREG(info.st_mode):
                size, digest = _hash_regular_file(child_path)
                entries.append(
                    {"path": relative, "type": "file", "size": size, "sha256": digest}
                )
            else:
                raise RepairError("source_special_file_rejected")
        stack.extend(reversed(directories))

    entries.sort(key=lambda item: (item["path"], item["type"]))
    file_entries = [entry for entry in entries if entry["type"] == "file"]
    dir_entries = [entry for entry in entries if entry["type"] == "dir"]
    manifest_sha256 = _sha256_bytes(_canonical_json(entries))
    return {
        "entries": entries,
        "manifest_sha256": manifest_sha256,
        "file_count": len(file_entries),
        "directory_count": len(dir_entries),
        "total_bytes": sum(int(entry["size"]) for entry in file_entries),
    }


def _copy_file_exact(source: Path, destination: Path, expected: dict) -> None:
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    write_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_fd = os.open(source, read_flags)
    destination_fd = -1
    try:
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
            raise RepairError("source_file_contract_failed")
        destination_fd = os.open(destination, write_flags, 0o600)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source_fd, CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise RepairError("destination_write_failed")
                view = view[written:]
        os.fsync(destination_fd)
        if (
            total != int(expected["size"])
            or digest.hexdigest() != expected["sha256"]
            or source_info.st_size != total
        ):
            raise RepairError("source_changed_during_copy")
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)


def _copy_tree_exact(source: Path, stage: Path, manifest: dict) -> None:
    os.mkdir(stage, 0o700)
    for entry in manifest["entries"]:
        target = stage / entry["path"]
        if entry["type"] == "dir":
            os.mkdir(target, 0o700)
        else:
            _copy_file_exact(source / entry["path"], target, entry)


def _rename_exclusive(source: Path, destination: Path) -> None:
    """Atomic no-replace directory promotion on macOS and SMB.

    ``renamex_np(RENAME_EXCL)`` is the strongest primitive and is used when
    the filesystem supports it.  Apple's SMB client can return ``EINVAL`` or
    ``ENOTSUP`` for that flag; in that narrow case all MAGI repair promotions
    are serialized by one exclusive no-follow lock in the same directory,
    the destination is rechecked under the lock, and ordinary same-directory
    ``rename`` provides the atomic promotion.
    """
    if sys.platform != "darwin":
        raise RepairError("exclusive_rename_platform_unsupported")
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = libc.renamex_np
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    rename_excl = 0x00000004
    result = renamex_np(os.fsencode(source), os.fsencode(destination), rename_excl)
    if result != 0:
        err = ctypes.get_errno()
        if err in {errno.EEXIST, errno.ENOTEMPTY}:
            raise RepairError("destination_already_exists")
        if err not in {errno.EINVAL, errno.ENOTSUP}:
            raise RepairError("exclusive_promotion_failed")
        _rename_exclusive_smb_fallback(source, destination)


def _rename_exclusive_smb_fallback(source: Path, destination: Path) -> None:
    parent = destination.parent
    if source.parent != parent:
        raise RepairError("promotion_not_same_parent")
    lock = parent / ".magi-case-storage-repair-promotion.lock"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        lock_fd = os.open(lock, flags, 0o600)
    except FileExistsError as exc:
        raise RepairError("promotion_lock_busy") from exc
    try:
        os.write(lock_fd, b"magi-case-storage-repair/v1\n")
        os.fsync(lock_fd)
    finally:
        os.close(lock_fd)

    try:
        try:
            os.lstat(destination)
        except FileNotFoundError:
            pass
        else:
            raise RepairError("destination_already_exists")
        os.rename(source, destination)
        if source.exists() or source.is_symlink():
            raise RepairError("promotion_source_still_exists")
        if not _regular_directory(destination):
            raise RepairError("promotion_destination_contract_failed")
    except BaseException:
        raise
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # A stale lock is fail-closed for future repair attempts; the
            # caller still verifies the promoted tree before issuing receipt.
            pass


def _safe_cleanup_stage(stage: Path, parent: Path) -> None:
    try:
        if stage.parent != parent or not stage.name.startswith(".magi-case-storage-repair-"):
            return
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage)
    except OSError:
        pass


def _write_receipt_exclusive(path: Path, payload: dict) -> None:
    if not _regular_directory(path.parent):
        raise RepairError("receipt_parent_contract_failed")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags, 0o600)
    try:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RepairError("receipt_write_failed")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def repair_case_tree(
    *,
    source: Path,
    destination: Path,
    expected_source_manifest_sha256: str,
    case_identity_sha256: str,
    receipt: Path | None,
    apply: bool,
) -> dict:
    source = Path(os.path.abspath(os.path.expanduser(str(source))))
    destination = Path(os.path.abspath(os.path.expanduser(str(destination))))
    if not _is_file_provider_source(source):
        raise RepairError("source_is_not_file_provider")
    if not _regular_directory(source):
        raise RepairError("source_root_contract_failed")
    if not _is_lower_hex64(case_identity_sha256):
        raise RepairError("case_identity_digest_invalid")
    if destination.exists() or destination.is_symlink():
        raise RepairError("destination_already_exists")
    if not is_authoritative_nas_write_path(str(destination.parent)):
        raise RepairError("destination_is_not_authoritative_smb")
    if receipt is not None and (receipt.exists() or receipt.is_symlink()):
        raise RepairError("receipt_already_exists")

    started_at = _utc_now()
    source_before = build_tree_manifest(source)
    if apply and not _is_lower_hex64(expected_source_manifest_sha256):
        raise RepairError("expected_source_manifest_required")
    if apply and source_before["manifest_sha256"] != expected_source_manifest_sha256:
        raise RepairError("source_manifest_mismatch")
    if not apply:
        return {
            "schema": SCHEMA,
            "status": "ready_not_executed",
            "case_identity_sha256": case_identity_sha256,
            "source_manifest_sha256": source_before["manifest_sha256"],
            "file_count": source_before["file_count"],
            "directory_count": source_before["directory_count"],
            "total_bytes": source_before["total_bytes"],
            "source_preserved": True,
            "destination_created": False,
            "pii_included": False,
        }

    stage = destination.parent / f".magi-case-storage-repair-{uuid.uuid4().hex}"
    promoted = False
    try:
        _copy_tree_exact(source, stage, source_before)
        stage_manifest = build_tree_manifest(stage)
        source_after_copy = build_tree_manifest(source)
        if stage_manifest["manifest_sha256"] != source_before["manifest_sha256"]:
            raise RepairError("stage_manifest_mismatch")
        if source_after_copy["manifest_sha256"] != source_before["manifest_sha256"]:
            raise RepairError("source_changed_during_repair")
        if not is_authoritative_nas_write_path(str(destination.parent)):
            raise RepairError("destination_mount_changed_before_promotion")
        _rename_exclusive(stage, destination)
        promoted = True
        destination_manifest = build_tree_manifest(destination)
        source_final = build_tree_manifest(source)
        if destination_manifest["manifest_sha256"] != source_before["manifest_sha256"]:
            raise RepairError("destination_manifest_mismatch")
        if source_final["manifest_sha256"] != source_before["manifest_sha256"]:
            raise RepairError("source_changed_after_promotion")
        if not is_authoritative_nas_write_path(str(destination.parent)):
            raise RepairError("destination_mount_changed_after_promotion")

        payload = {
            "schema": SCHEMA,
            "status": "passed",
            "started_at": started_at,
            "completed_at": _utc_now(),
            "case_identity_sha256": case_identity_sha256,
            "source_manifest_sha256": source_before["manifest_sha256"],
            "destination_manifest_sha256": destination_manifest["manifest_sha256"],
            "file_count": source_before["file_count"],
            "directory_count": source_before["directory_count"],
            "total_bytes": source_before["total_bytes"],
            "source_preserved": True,
            "destination_created": True,
            "same_parent_atomic_promotion": True,
            "zero_overwrite": True,
            "pii_included": False,
        }
        if receipt is not None:
            _write_receipt_exclusive(receipt, payload)
        return payload
    finally:
        if not promoted:
            _safe_cleanup_stage(stage, destination.parent)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--case-identity-sha256", required=True)
    parser.add_argument("--expected-source-manifest-sha256", default="")
    parser.add_argument("--receipt", default="")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = repair_case_tree(
            source=Path(args.source),
            destination=Path(args.destination),
            expected_source_manifest_sha256=args.expected_source_manifest_sha256,
            case_identity_sha256=args.case_identity_sha256,
            receipt=Path(args.receipt) if args.receipt else None,
            apply=bool(args.apply),
        )
    except RepairError as exc:
        print(json.dumps({
            "schema": SCHEMA,
            "status": "failed",
            "reason": str(exc),
            "pii_included": False,
        }, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
