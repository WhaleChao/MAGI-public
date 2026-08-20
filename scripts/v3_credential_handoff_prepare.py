#!/usr/bin/env python3
"""Materialize mutable OAuth tokens into V3 shared secrets without exposing content."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HANDOFFS = (
    (
        "google_calendar_token",
        "google_calendar_token_source_file",
        "google_calendar_token_source_sha256",
        "google_calendar_token_file",
        True,
    ),
    (
        "laf_gmail_token",
        "laf_gmail_token_source_file",
        "laf_gmail_token_source_sha256",
        "laf_gmail_token_file",
        True,
    ),
    (
        "file_review_token",
        "file_review_token_source_file",
        "file_review_token_source_sha256",
        "file_review_token_file",
        True,
    ),
    (
        "gmail_compose_token",
        "gmail_compose_token_source_file",
        "gmail_compose_token_source_sha256",
        "gmail_compose_token_file",
        False,
    ),
    (
        "accounting_sheets_token",
        "accounting_sheets_token_source_file",
        "accounting_sheets_token_source_sha256",
        "accounting_sheets_token_file",
        True,
    ),
    (
        "drive_sync_token",
        "drive_sync_token_source_file",
        "drive_sync_token_source_sha256",
        "drive_sync_token_file",
        True,
    ),
    (
        "drive_sync_write_token",
        "drive_sync_write_token_source_file",
        "drive_sync_write_token_source_sha256",
        "drive_sync_write_token_file",
        True,
    ),
)
TARGET_LEAVES = {
    "google_calendar_token": "google_calendar_token.json",
    "laf_gmail_token": "laf_gmail_token.pickle",
    "file_review_token": "filereview_token.pickle",
    "gmail_compose_token": "gmail_compose_token.json",
    "accounting_sheets_token": "accounting_sheets_token.json",
    "drive_sync_token": "drive_sync_token.json",
    "drive_sync_write_token": "drive_sync_write_token.json",
}


class SecretHandoffError(RuntimeError):
    pass


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _regular_bytes(path: Path, *, label: str) -> bytes:
    if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
        raise SecretHandoffError(f"{label} must be a canonical non-symlink file")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SecretHandoffError(f"{label} must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
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
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
    if any(getattr(before, name) != getattr(after, name) for name in fields):
        raise SecretHandoffError(f"{label} changed while it was read")
    return b"".join(chunks)


def _atomic_replace(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    _safe_directory(path.parent)
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise SecretHandoffError("existing handoff target is unsafe")
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        path.chmod(mode)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise SecretHandoffError("handoff directory must be absolute")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            metadata = cursor.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise SecretHandoffError("handoff directory traverses an unsafe component")
            continue
        cursor.mkdir(mode=0o700)
    resolved = path.resolve(strict=True)
    if resolved != path or resolved.is_symlink() or resolved.stat().st_uid != os.getuid():
        raise SecretHandoffError("handoff directory is not canonical or user-owned")
    return resolved


def materialize_secret_handoff(
    deploy_manifest: Path,
    *,
    expected_manifest_sha256: str,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Copy only manifest-bound tokens and return/write a path-and-hash receipt."""

    manifest_payload = _regular_bytes(deploy_manifest, label="deployment manifest")
    observed_manifest_sha = _sha256(manifest_payload)
    if (
        SHA256_RE.fullmatch(expected_manifest_sha256) is None
        or observed_manifest_sha != expected_manifest_sha256
    ):
        raise SecretHandoffError("deployment manifest SHA-256 mismatch")
    try:
        manifest = json.loads(manifest_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SecretHandoffError(f"deployment manifest is invalid: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("status") != "prepared_not_installed":
        raise SecretHandoffError("deployment manifest is not a prepared deployment")
    external = manifest.get("external_inputs")
    if not isinstance(external, dict):
        raise SecretHandoffError("deployment external input contract is missing")
    runtime = Path(str(manifest.get("runtime_root") or "")).expanduser()
    if not runtime.is_absolute() or runtime.is_symlink() or runtime.resolve(strict=False) != runtime:
        raise SecretHandoffError("deployment runtime root is unsafe")
    secrets_root = runtime / "shared" / "secrets"
    _safe_directory(secrets_root)
    rows: list[dict[str, Any]] = []
    for key, source_name, source_sha_name, target_name, required in HANDOFFS:
        source_raw = external.get(source_name)
        source_sha = external.get(source_sha_name)
        target_raw = external.get(target_name)
        if target_raw != str(secrets_root / TARGET_LEAVES[key]):
            raise SecretHandoffError(f"{key} target escapes V3 shared secrets")
        target = Path(str(target_raw))
        if source_raw is None and source_sha is None and not required:
            rows.append({"key": key, "status": "optional_degraded", "target": str(target)})
            continue
        if not isinstance(source_raw, str) or not isinstance(source_sha, str):
            raise SecretHandoffError(f"{key} source binding is incomplete")
        source = Path(source_raw)
        payload = _regular_bytes(source, label=f"{key} source")
        if SHA256_RE.fullmatch(source_sha) is None or _sha256(payload) != source_sha:
            raise SecretHandoffError(f"{key} source SHA-256 mismatch")
        _atomic_replace(target, payload)
        target_payload = _regular_bytes(target, label=f"{key} target")
        metadata = target.lstat()
        if (
            _sha256(target_payload) != source_sha
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise SecretHandoffError(f"{key} target verification failed")
        rows.append(
            {
                "key": key,
                "status": "materialized",
                "source": str(source),
                "source_sha256": source_sha,
                "target": str(target),
                "target_sha256": source_sha,
            }
        )
    receipt = {
        "schema": "magi.v3.mutable-secret-handoff/v1",
        "deploy_manifest": str(deploy_manifest),
        "deploy_manifest_sha256": observed_manifest_sha,
        "rows": rows,
        "sensitive_content_recorded": False,
    }
    destination = receipt_path or secrets_root / "secret-handoff-receipt.json"
    encoded = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    _atomic_replace(destination, encoded)
    receipt["receipt_path"] = str(destination)
    receipt["receipt_sha256"] = _sha256(encoded)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy-manifest", type=Path, required=True)
    parser.add_argument("--deploy-manifest-sha256", required=True)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        result = materialize_secret_handoff(
            args.deploy_manifest,
            expected_manifest_sha256=args.deploy_manifest_sha256,
            receipt_path=args.receipt,
        )
    except (OSError, SecretHandoffError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": "materialized",
                "receipt_path": result["receipt_path"],
                "receipt_sha256": result["receipt_sha256"],
                "sensitive_content_recorded": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
